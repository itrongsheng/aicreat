"""zhiqiapi 错误类型与分类（docs/08-zhiqiapi-integration.md §5.3、§9）。

- ``classify_error``：按 §9.1 的 13 条顺序判定（首个命中即返回），返回 ``(category, pre_submit)``；
- ``is_retryable``：类别层面是否可重试（仅 upstream_unavailable / rate_limited / timeout / invalid_response）；
- ``compute_retryable``：``ZhiqiError.retryable`` 的计算（HTTP 500/504 固定 False，502/503 仅幂等请求为 True）；
- ``is_fallbackable``：按 ``ai_routing_config.fallback`` 判定候选链切换（``never_fallback_on`` 优先）；
- ``classify_task_failure``：异步任务 ``failed`` 体分类（§9.4）；
- ``sanitize_error_message``：写 ``ai_tasks.error_message`` 前截断 500 字符并脱敏。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.zhiqi.types import ErrorCategory

RETRYABLE_CATEGORIES = frozenset(
    {ErrorCategory.UPSTREAM_UNAVAILABLE, ErrorCategory.RATE_LIMITED, ErrorCategory.TIMEOUT, ErrorCategory.INVALID_RESPONSE}
)

# §9.1 各条的文案关键词（小写比较）
QUOTA_KEYWORDS = ("quota", "额度", "balance")
CONTENT_BLOCKED_KEYWORDS = ("content_policy", "moderation", "sensitive", "safety", "敏感", "违规")
UNSUPPORTED_PARAMETER_KEYWORDS = ("unsupported_parameter", "invalid parameter", "not supported")
# 「model 关键字」：前四个单独出现即视为模型级（渠道/下架是按模型配置的）；"not exist" 必须与 model / 模型 同时出现，
# 以免把「route does not exist」这类路由级 404 误判为 model_unrouted（其余 404 默认 route_missing）
MODEL_KEYWORDS_STANDALONE = ("unrouted", "no available channel", "下架", "无可用渠道")
MODEL_KEYWORDS_WITH_MODEL = ("not exist",)

DEFAULT_FALLBACK_ON = frozenset(
    {
        "upstream_unavailable", "rate_limited", "timeout", "route_missing", "model_unrouted", "breaker_open",
        "media_storage", "invalid_response", "unsupported_parameter", "unknown",
    }
)
DEFAULT_NEVER_FALLBACK_ON = frozenset({"quota_exceeded", "auth_failed", "content_blocked", "transfer_failed", "cancelled"})

MAX_ERROR_MESSAGE_CHARS = 500


class ZhiqiError(Exception):
    """一次上游调用（或本地前置判定）的失败。``str(exc)`` 为 ``message``。"""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        http_status: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        pre_submit: bool = False,
        raw: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.category: ErrorCategory = ErrorCategory(category)
        self.message: str = message
        self.http_status: int | None = http_status
        self.request_id: str | None = request_id
        # = is_retryable(category) AND (请求幂等 OR pre_submit)；HTTP 500/504 固定 False；HTTP 502/503 仅 idempotent=True 时为 True
        self.retryable: bool = retryable
        # True = 请求未发出（ConnectError/ConnectTimeout/PoolTimeout），重发不会重复计费
        self.pre_submit: bool = pre_submit
        self.raw: dict = raw if raw is not None else {}
        # 客户端幂等重试过程中收到的全部非空 request_id（含最后一次），供网关写 response_meta_json.retry_request_ids[]
        self.retry_request_ids: list[str] = []
        # HTTP 429 响应头 Retry-After（秒），客户端退避时使用
        self.retry_after: float | None = None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"ZhiqiError(category={self.category.value}, http_status={self.http_status}, "
            f"request_id={self.request_id!r}, message={self.message!r})"
        )


class ZhiqiBreakerOpen(ZhiqiError):
    """本地熔断器打开（或全局暂停），未发起 HTTP。category 固定 BREAKER_OPEN。"""

    def __init__(self, message: str = "熔断器打开，未发起调用", **kwargs: Any) -> None:
        kwargs.pop("category", None)
        super().__init__(ErrorCategory.BREAKER_OPEN, message, **kwargs)


# ---------------------------------------------------------------- 分类


def _body_text(body: dict | list | str | bytes | None) -> str:
    """把错误体转为小写文本用于关键词匹配（dict/list 序列化，保留中文）。"""
    if body is None:
        return ""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body.lower()
    try:
        return json.dumps(body, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(body).lower()


def _contains(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _has_model_keyword(text: str) -> bool:
    if _contains(text, MODEL_KEYWORDS_STANDALONE):
        return True
    return _contains(text, MODEL_KEYWORDS_WITH_MODEL) and ("model" in text or "模型" in text)


PRE_SUBMIT_EXCEPTIONS: tuple[type[BaseException], ...] = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# 请求体已发出或可能已发出（上游可能已受理）：读/写超时与握手后断开（ReadError/WriteError 同属「握手后断开」）
MAYBE_SUBMITTED_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError,
)


def classify_error(
    http_status: int | None, body: dict | str | None, exc: Exception | None = None
) -> tuple[ErrorCategory, bool]:
    """按 §9.1 判定顺序返回 ``(category, pre_submit)``（首个命中即返回）。"""
    # 1. 连接阶段错误（请求未发出）
    if exc is not None and isinstance(exc, PRE_SUBMIT_EXCEPTIONS):
        return ErrorCategory.UPSTREAM_UNAVAILABLE, True
    # 2. 请求体已发出或可能已发出
    if exc is not None and isinstance(exc, MAYBE_SUBMITTED_EXCEPTIONS):
        return ErrorCategory.TIMEOUT, False
    text = _body_text(body)
    status = int(http_status) if http_status is not None else None
    # 3. 401
    if status == 401:
        return ErrorCategory.AUTH_FAILED, False
    # 4. 402，或 403 且文案含额度词
    if status == 402 or (status == 403 and _contains(text, QUOTA_KEYWORDS)):
        return ErrorCategory.QUOTA_EXCEEDED, False
    # 5. 其它 403
    if status == 403:
        return ErrorCategory.AUTH_FAILED, False
    # 6. 429
    if status == 429:
        return ErrorCategory.RATE_LIMITED, False
    # 7. 400/422 审核文案
    if status in (400, 422) and _contains(text, CONTENT_BLOCKED_KEYWORDS):
        return ErrorCategory.CONTENT_BLOCKED, False
    # 8. 400/422 参数文案
    if status in (400, 422) and _contains(text, UNSUPPORTED_PARAMETER_KEYWORDS):
        return ErrorCategory.UNSUPPORTED_PARAMETER, False
    # 9. 400/404/503 且含 model 关键字
    if status in (400, 404, 503) and _has_model_keyword(text):
        return ErrorCategory.MODEL_UNROUTED, False
    # 10. 其余 404 一律 route_missing
    if status == 404:
        return ErrorCategory.ROUTE_MISSING, False
    # 11. 5xx 网关类
    if status in (500, 502, 503, 504):
        return ErrorCategory.UPSTREAM_UNAVAILABLE, False
    # 12. JSON 解析失败
    if exc is not None and isinstance(exc, ValueError):
        return ErrorCategory.INVALID_RESPONSE, False
    # 13. 其它
    return ErrorCategory.UNKNOWN, False


def is_retryable(category: ErrorCategory) -> bool:
    """类别层面：仅 UPSTREAM_UNAVAILABLE / RATE_LIMITED / TIMEOUT / INVALID_RESPONSE。"""
    return ErrorCategory(category) in RETRYABLE_CATEGORIES


def compute_retryable(category: ErrorCategory, *, http_status: int | None, idempotent: bool, pre_submit: bool) -> bool:
    """``ZhiqiError.retryable``：``is_retryable(category) AND (idempotent OR pre_submit)``；HTTP 500/504 固定 False，502/503 仅幂等为 True。"""
    if not is_retryable(category):
        return False
    if http_status in (500, 504):
        return False
    if http_status in (502, 503):
        return idempotent
    return idempotent or pre_submit


def is_fallbackable(category: ErrorCategory, config: dict) -> bool:
    """按 ``ai_routing_config.fallback`` 判定是否可切换备选（``never_fallback_on`` 优先；``enabled=false`` 一律不切换）。

    ``config`` 可传完整 ``ai_routing_config`` 或其 ``fallback`` 节。
    """
    cfg = config or {}
    fb = cfg["fallback"] if isinstance(cfg.get("fallback"), dict) else cfg
    if not fb.get("enabled", True):
        return False
    value = ErrorCategory(category).value
    never = set(fb.get("never_fallback_on", DEFAULT_NEVER_FALLBACK_ON))
    if value in never:
        return False
    return value in set(fb.get("fallback_on", DEFAULT_FALLBACK_ON))


def classify_task_failure(error_code: str | None, error_message: str | None) -> ErrorCategory:
    """异步任务 ``failed`` 体：含 ``media_storage`` → MEDIA_STORAGE；含审核文案 → CONTENT_BLOCKED；其它 → UNKNOWN。"""
    text = f"{error_code or ''} {error_message or ''}".lower()
    if "media_storage" in text:
        return ErrorCategory.MEDIA_STORAGE
    if _contains(text, CONTENT_BLOCKED_KEYWORDS):
        return ErrorCategory.CONTENT_BLOCKED
    return ErrorCategory.UNKNOWN


# ---------------------------------------------------------------- 文案提取与脱敏

_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:key|api_key|apikey|access_key|token|access_token|secret|signature|sig)=)[^&#\s\"']*")


def sanitize_error_message(message: str | None, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str | None:
    """去掉 Bearer 令牌、``sk-`` 密钥与 URL 查询串中的 key/token，截断至 ``limit`` 字符。"""
    if message is None:
        return None
    text = str(message)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SK_RE.sub("sk-***", text)
    text = _QUERY_SECRET_RE.sub(r"\1***", text)
    return text[:limit]


def error_message_from_body(body: dict | list | str | None, fallback: str = "") -> str:
    """从上游错误体提取可读文案：``error.message`` / ``message`` / ``error``（字符串）/ ``detail``，否则原文前 500 字符。"""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("message", "msg", "detail"):
                if isinstance(err.get(key), str) and err[key].strip():
                    code = err.get("code") or err.get("type")
                    return f"{code}: {err[key]}" if isinstance(code, str) and code and code not in err[key] else err[key]
        if isinstance(err, str) and err.strip():
            return err
        for key in ("message", "msg", "detail", "error_message"):
            if isinstance(body.get(key), str) and body[key].strip():
                return body[key]
        try:
            return json.dumps(body, ensure_ascii=False)[:MAX_ERROR_MESSAGE_CHARS]
        except (TypeError, ValueError):
            return fallback
    if isinstance(body, list):
        try:
            return json.dumps(body, ensure_ascii=False)[:MAX_ERROR_MESSAGE_CHARS]
        except (TypeError, ValueError):
            return fallback
    if isinstance(body, str) and body.strip():
        return body.strip()[:MAX_ERROR_MESSAGE_CHARS]
    return fallback
