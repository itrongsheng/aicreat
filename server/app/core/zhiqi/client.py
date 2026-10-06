"""zhiqiapi HTTP 客户端（docs/08-zhiqiapi-integration.md §5.4、§8.2、§14）。

- 只持有 ``base_url`` / ``api_key`` / ``timeouts`` / ``user_agent``；重试策略按调用以 ``retry: RetryPolicy`` 注入；
- ``/v1/*`` 与 ``/api/*`` 一律 ``origin + path`` 并带 ``Authorization: Bearer``；``absolute=True`` 直接用 ``path``，只带 User-Agent；
- 每次响应读取 ``x-oneapi-request-id``（无论状态码）；非 2xx 抛 ``ZhiqiError``（``classify_error``），始终附带 ``request_id``；
- 客户端重试不产生新尝试行：``request_id`` 为最后一次 HTTP 的值，历次值经 ``headers["x-aicreat-retry-request-ids"]``
  （本地拼装、逗号分隔）与 ``ZhiqiError.retry_request_ids`` 带出；
- 日志只记 ``method path http_status latency_ms request_id``，不记请求体与密钥。
"""

from __future__ import annotations

import email.utils
import json as jsonlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, BinaryIO
from urllib.parse import urlencode, urlsplit

import httpx

from app.core import safe_fetch as _safe_fetch
from app.core.config import settings
from app.core.zhiqi.errors import (
    ZhiqiError,
    classify_error,
    compute_retryable,
    error_message_from_body,
)
from app.core.zhiqi.types import DownloadResult, ErrorCategory, RetryPolicy, Timeouts

logger = logging.getLogger("app.core.zhiqi")

REQUEST_ID_HEADER = "x-oneapi-request-id"
RETRY_REQUEST_IDS_HEADER = "x-aicreat-retry-request-ids"
DEFAULT_DOWNLOAD_TYPES: tuple[str, ...] = ("image/", "video/", "application/octet-stream", "binary/octet-stream")


@dataclass
class ZhiqiResponse:
    http_status: int; json: dict[str, Any] | list | None; text: str; headers: dict[str, str]
    request_id: str | None; latency_ms: int

    @property
    def retry_request_ids(self) -> list[str]:
        """客户端重试历次 ``request_id``（含最后一次）；未发生重试时为 ``[]``。"""
        raw = self.headers.get(RETRY_REQUEST_IDS_HEADER, "")
        return [item for item in raw.split(",") if item]


def strip_v1(base_url: str) -> str:
    """``base_url`` 去掉尾部 ``/v1``（只去末段，保留协议、主机与其余路径前缀）。"""
    base = (base_url or "").strip().rstrip("/")
    base = base.removesuffix("/v1")
    return base.rstrip("/")


def backoff_seconds(policy: RetryPolicy, failure_no: int, *, retry_after: float | None = None, rand: Callable[[float, float], float] = random.uniform) -> float:
    """第 ``failure_no`` 次失败（从 1 起）后的等待秒数（§8.2）：

    ``d_n = min(max_seconds, base_seconds × 2^(n−1))``；``jitter`` 时 ``× uniform(0.5, 1.0)``；
    429 带 ``Retry-After`` 时 ``max(Retry-After, d_n)``，上限 ``max_seconds``。
    """
    d_n = min(policy.max_seconds, policy.base_seconds * (2 ** (max(1, failure_no) - 1)))
    if policy.jitter:
        d_n = d_n * rand(0.5, 1.0)
    if retry_after is not None:
        d_n = max(retry_after, d_n)
    return max(0.0, min(policy.max_seconds, d_n))


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After``：秒数或 HTTP-date；无法解析返回 ``None``。"""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return max(0.0, parsed.timestamp() - time.time())


class ZhiqiClient:
    """zhiqiapi 客户端。``transport`` / ``sleep`` 仅供测试注入（``httpx.MockTransport``、跳过真实等待）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeouts: Timeouts,
        user_agent: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self._api_key = api_key or ""
        self.timeouts = timeouts
        self.user_agent = user_agent
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=timeouts.connect, read=timeouts.read, write=timeouts.write, pool=timeouts.pool),
            follow_redirects=False,
            transport=transport,
        )

    def __repr__(self) -> str:  # 不暴露密钥
        return f"{type(self).__name__}(base_url={self.base_url!r}, mock={self.is_mock})"

    # ------------------------------------------------------------ 属性

    @property
    def is_mock(self) -> bool:
        return False

    @property
    def origin(self) -> str:
        """``base_url`` 去掉尾部 ``/v1``（与 ``Settings.zhiqi_origin`` 同值）。"""
        return strip_v1(self.base_url)

    def is_origin_url(self, url: str) -> bool:
        """绝对 URL 的 scheme + host + port 是否与 ``base_url`` 相同（上游 origin → ``stream_download``，否则走 ``safe_fetch``）。"""
        try:
            a, b = urlsplit(url), urlsplit(self.origin)
        except ValueError:
            return False
        if not a.scheme or not a.netloc:
            return False
        return (a.scheme.lower(), (a.hostname or "").lower(), a.port) == (b.scheme.lower(), (b.hostname or "").lower(), b.port)

    # ------------------------------------------------------------ 内部

    def _timeout(self, read: float | None) -> httpx.Timeout:
        t = self.timeouts
        return httpx.Timeout(connect=t.connect, read=read if read is not None else t.read, write=t.write, pool=t.pool)

    def _url_and_headers(self, path: str, *, absolute: bool) -> tuple[str, dict[str, str]]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if absolute:
            return path, headers
        if not path.startswith("/") or "://" in path:
            raise ValueError(f"相对路径必须以 / 开头：{path!r}")
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return self.origin + path, headers

    @staticmethod
    def _log_path(url: str) -> str:
        parts = urlsplit(url)
        return parts.path or "/"

    def _build_content(
        self,
        *,
        json: dict | None,
        data: dict | list[tuple[str, str]] | None,
        files: list[tuple[str, Any]] | None,
        multipart: bool,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        elif multipart or files:
            items = list(data.items()) if isinstance(data, dict) else list(data or [])
            parts: list[tuple[str, Any]] = [(str(k), (None, "" if v is None else str(v))) for k, v in items]
            parts.extend(files or [])
            kwargs["files"] = parts
        elif data is not None:
            items = list(data.items()) if isinstance(data, dict) else list(data)
            kwargs["content"] = urlencode([(str(k), "" if v is None else str(v)) for k, v in items]).encode()
            kwargs["headers_extra"] = {"Content-Type": "application/x-www-form-urlencoded"}
        return kwargs

    def _send_once(
        self, method: str, url: str, headers: dict[str, str], content_kwargs: dict[str, Any], timeout: httpx.Timeout, *, idempotent: bool
    ) -> ZhiqiResponse:
        kwargs = dict(content_kwargs)
        extra_headers = kwargs.pop("headers_extra", None)
        send_headers = {**headers, **(extra_headers or {})}
        started = time.monotonic()
        try:
            resp = self._http.request(method, url, headers=send_headers, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            category, pre_submit = classify_error(None, None, exc)
            logger.info("zhiqi %s %s http_status=- latency_ms=%s request_id=- error=%s", method, self._log_path(url), latency_ms, type(exc).__name__)
            raise ZhiqiError(
                category,
                f"请求 zhiqiapi 失败：{type(exc).__name__}",
                http_status=None,
                request_id=None,
                retryable=compute_retryable(category, http_status=None, idempotent=idempotent, pre_submit=pre_submit),
                pre_submit=pre_submit,
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        request_id = resp.headers.get(REQUEST_ID_HEADER) or None
        logger.info(
            "zhiqi %s %s http_status=%s latency_ms=%s request_id=%s", method, self._log_path(url), resp.status_code, latency_ms, request_id or "-"
        )
        text = resp.text
        parsed: Any = None
        parse_error: Exception | None = None
        if text.strip():
            try:
                parsed = jsonlib.loads(text)
            except ValueError as exc:
                parse_error = exc
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        if not 200 <= resp.status_code < 300:
            body: Any = parsed if parsed is not None else text
            category, pre_submit = classify_error(resp.status_code, body)
            message = error_message_from_body(body, fallback=f"HTTP {resp.status_code}") or f"HTTP {resp.status_code}"
            err = ZhiqiError(
                category,
                message,
                http_status=resp.status_code,
                request_id=request_id,
                retryable=compute_retryable(category, http_status=resp.status_code, idempotent=idempotent, pre_submit=pre_submit),
                pre_submit=pre_submit,
                raw=parsed if isinstance(parsed, dict) else {"text": text[:2000]},
            )
            err.retry_after = parse_retry_after(resp_headers.get("retry-after"))
            raise err
        if parse_error is not None:
            category, pre_submit = classify_error(resp.status_code, text, parse_error)
            raise ZhiqiError(
                category,
                "zhiqiapi 响应不是合法 JSON",
                http_status=resp.status_code,
                request_id=request_id,
                retryable=compute_retryable(category, http_status=resp.status_code, idempotent=idempotent, pre_submit=pre_submit),
                pre_submit=pre_submit,
                raw={"text": text[:2000]},
            )
        return ZhiqiResponse(
            http_status=resp.status_code, json=parsed, text=text, headers=resp_headers, request_id=request_id, latency_ms=latency_ms
        )

    # ------------------------------------------------------------ 公共

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        data: dict | list[tuple[str, str]] | None = None,
        files: list[tuple[str, Any]] | None = None,
        multipart: bool = False,
        timeout: float | None = None,
        idempotent: bool = False,
        absolute: bool = False,
        retry: RetryPolicy | None = None,
        metadata: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> ZhiqiResponse:
        """发起一次逻辑调用（可能含客户端幂等重试）。

        ``metadata`` 为本地上下文，``ZhiqiClient`` 忽略（不进请求体、请求头与日志）；``headers`` 为附加请求头
        （如 ``anthropic-version``），不能覆盖 ``Authorization`` / ``User-Agent``。
        ``retry=None`` 不重试；给定时：``idempotent=True`` 按 ``retry.retry_on`` 全量指数退避重试（HTTP 500/504 除外）；
        ``idempotent=False`` 仅在 ``err.retryable``（提交前错误）或 ``rate_limited``（遵守 ``Retry-After``）时重试。
        """
        del metadata  # 只供 MockZhiqiClient 使用
        url, base_headers = self._url_and_headers(path, absolute=absolute)
        if headers:
            for key, value in headers.items():
                if key.lower() not in ("authorization", "user-agent"):
                    base_headers[key] = value
        content_kwargs = self._build_content(json=json, data=data, files=files, multipart=multipart)
        timeout_obj = self._timeout(timeout)
        max_attempts = max(1, retry.max_attempts) if retry is not None else 1
        request_ids: list[str] = []
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send_once(method.upper(), url, base_headers, content_kwargs, timeout_obj, idempotent=idempotent)
            except ZhiqiError as err:
                if err.request_id:
                    request_ids.append(err.request_id)
                if retry is None or attempt >= max_attempts or not self._should_retry(err, retry, idempotent=idempotent):
                    err.retry_request_ids = list(request_ids)
                    raise
                retry_after = err.retry_after if err.category == ErrorCategory.RATE_LIMITED else None
                delay = backoff_seconds(retry, attempt, retry_after=retry_after)
                logger.info(
                    "zhiqi retry %s %s attempt=%s/%s category=%s sleep=%.2fs",
                    method.upper(), self._log_path(url), attempt, max_attempts, err.category.value, delay,
                )
                if delay > 0:
                    self._sleep(delay)
                continue
            if response.request_id:
                request_ids.append(response.request_id)
            if attempt > 1 and request_ids:
                response.headers[RETRY_REQUEST_IDS_HEADER] = ",".join(request_ids)
            return response

    @staticmethod
    def _should_retry(err: ZhiqiError, retry: RetryPolicy, *, idempotent: bool) -> bool:
        if err.category not in retry.retry_on:
            return False
        if idempotent:
            return err.retryable
        return err.retryable or err.category == ErrorCategory.RATE_LIMITED

    def get(self, path: str, **kw: Any) -> ZhiqiResponse:
        kw.setdefault("idempotent", True)
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> ZhiqiResponse:
        kw.setdefault("idempotent", False)
        return self.request("POST", path, **kw)

    def stream_download(
        self,
        path: str,
        dest: BinaryIO,
        *,
        max_bytes: int,
        timeout: float,
        source: str = "origin",
        allowed_content_types: tuple[str, ...] = DEFAULT_DOWNLOAD_TYPES,
    ) -> DownloadResult:
        """下载上游 origin 的媒体（带 Bearer）：只接受相对路径（如 ``/v1/videos/{id}/content``）或 origin 与
        ``base_url`` 相同的绝对 URL；第三方 CDN 的绝对 URL 必须改走 ``safe_fetch.stream_public_bytes``。

        成功返回 ``DownloadResult(source, request_id, http_status, request_ids)``；超限 / Content-Type 不在集合 /
        魔数校验失败 / 读取失败抛 ``ZhiqiError(TRANSFER_FAILED)``，携带已收到响应头的 ``request_id`` 与 ``http_status``。
        """
        if path.startswith("/") and "://" not in path:
            url = self.origin + path
        elif self.is_origin_url(path):
            url = path
        else:
            raise ZhiqiError(ErrorCategory.TRANSFER_FAILED, "第三方 URL 须经 safe_fetch.stream_public_bytes 下载")
        headers = {"User-Agent": self.user_agent}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        started = time.monotonic()
        request_id: str | None = None
        status: int | None = None
        log_path = self._log_path(url)
        try:
            with self._http.stream("GET", url, headers=headers, timeout=self._timeout(timeout)) as resp:
                status = resp.status_code
                request_id = resp.headers.get(REQUEST_ID_HEADER) or None
                if not 200 <= status < 300:
                    raise ZhiqiError(
                        ErrorCategory.TRANSFER_FAILED, f"下载失败：HTTP {status}", http_status=status, request_id=request_id
                    )
                try:
                    _safe_fetch.read_media_stream(resp, dest, max_bytes=max_bytes, allowed_types=allowed_content_types)
                except _safe_fetch.MediaStreamError as exc:
                    raise ZhiqiError(ErrorCategory.TRANSFER_FAILED, str(exc), http_status=status, request_id=request_id) from exc
        except ZhiqiError as err:
            logger.info(
                "zhiqi GET %s http_status=%s latency_ms=%s request_id=%s error=transfer_failed",
                log_path, status if status is not None else "-", int((time.monotonic() - started) * 1000), request_id or "-",
            )
            err.retry_request_ids = [request_id] if request_id else []
            raise
        except httpx.HTTPError as exc:
            logger.info(
                "zhiqi GET %s http_status=%s latency_ms=%s request_id=%s error=%s",
                log_path, status if status is not None else "-", int((time.monotonic() - started) * 1000), request_id or "-", type(exc).__name__,
            )
            err = ZhiqiError(
                ErrorCategory.TRANSFER_FAILED, f"下载请求失败：{type(exc).__name__}", http_status=status, request_id=request_id
            )
            err.retry_request_ids = [request_id] if request_id else []
            raise err from exc
        logger.info(
            "zhiqi GET %s http_status=%s latency_ms=%s request_id=%s", log_path, status, int((time.monotonic() - started) * 1000), request_id or "-"
        )
        return DownloadResult(source=source, request_id=request_id, http_status=int(status), request_ids=[request_id] if request_id else [])

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------- 单例


def build_timeouts() -> Timeouts:
    """按环境变量构造客户端默认超时（读超时为文本默认值；单次调用的 ``timeout`` 只覆盖 read）。"""
    return Timeouts(connect=float(settings.zhiqi_timeout_connect_seconds), read=float(settings.zhiqi_timeout_text_seconds), write=30.0, pool=10.0)


@lru_cache(maxsize=1)
def get_client() -> ZhiqiClient:
    """进程内单例：``ZHIQI_API_KEY`` 为空 → ``MockZhiqiClient``，否则 ``ZhiqiClient``。"""
    kwargs: dict[str, Any] = {"base_url": settings.zhiqi_base_url, "timeouts": build_timeouts(), "user_agent": settings.zhiqi_user_agent}
    if settings.zhiqi_mock_mode:
        from app.core.zhiqi.mock import MockZhiqiClient

        return MockZhiqiClient(api_key="", **kwargs)
    return ZhiqiClient(api_key=settings.zhiqi_api_key.strip(), **kwargs)


def reset_client() -> None:
    """关闭并清除单例（测试或切换密钥后使用）。"""
    if get_client.cache_info().currsize:
        try:
            get_client().close()
        except Exception:
            logger.debug("关闭 zhiqi 客户端失败", exc_info=True)
    get_client.cache_clear()
