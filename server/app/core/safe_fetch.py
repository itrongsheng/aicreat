"""SSRF 安全抓取（docs/11-link-backfill-and-monitoring.md §6.1、docs/10-media-generation.md §11.1）。

本文件当前提供：
- ``normalize_public_url``：语法级校验与规范化（复用 ``urls.canonicalize_public_url``，不做 DNS），失败抛 ``FetchBlocked``；
- ``assert_public_url``：在此之上做主机黑名单与 DNS 解析后的公网地址校验；
- ``stream_public_bytes``：媒体转存专用的第三方 CDN 下载（``follow_redirects=False``、逐跳 ``assert_public_url``、≤ 3 跳、
  不带 Authorization，只带 User-Agent），返回 ``DownloadResult(source="cdn", …)``，失败抛 ``ZhiqiError(TRANSFER_FAILED)``；
- ``read_media_stream``：流式写入 + Content-Type 白名单 + 魔数校验 + 字节上限（``ZhiqiClient.stream_download`` 共用）。

链接删除检测与平台规则测试（docs/11 §6.1）：

- ``FetchConfig`` / ``PageResult``；
- ``fetch_public_bytes``：``follow_redirects=False`` 手动跟随 3xx（每跳先 ``assert_public_url``，> ``max_redirects`` 抛
  ``FetchError("too_many_redirects")``）、流式读取 ``max_response_bytes + 1``（超限抛 ``FetchError("response_too_large")``）、
  4xx/5xx 原样返回；只发送 User-Agent / Accept / Accept-Language / 平台白名单头，不发送 Cookie / Authorization，不复用上一跳 Cookie；
- ``fetch_page``：``respect_robots=True`` 时先 ``robots_allowed``（不允许抛 ``FetchBlocked("blocked_by_robots")``），
  HTML 按 charset（Content-Type → ``<meta charset>`` → utf-8）解码并 ``fingerprint.extract_main_text``；
- ``robots_allowed``：Redis 缓存 ``robots:{domain}``（86400s；robots.txt 404/410 视为允许；其它失败视为不允许并缓存 900s）。

本模块不提供任何 SSRF 豁免开关；测试中以 monkeypatch 替换 ``assert_public_url``（仅测试进程生效，docs/11 §16.2）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import redis

from app.core.config import settings
from app.core.fingerprint import extract_main_text
from app.core.storage import sniff_media_type
from app.core.urls import InvalidURLError, canonicalize_public_url
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import DownloadResult, ErrorCategory

logger = logging.getLogger(__name__)

BLOCKED_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
BLOCKED_HOST_SUFFIXES = (".local", ".internal", ".localhost")
DEFAULT_MEDIA_TYPES: tuple[str, ...] = ("image/", "video/", "application/octet-stream", "binary/octet-stream")
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE = 64 * 1024
_SNIFF_BYTES = 64


class FetchError(RuntimeError):
    """网络错误 / 超时 / 响应超限 / 重定向过多 / DNS 失败。``reason`` 为机器可读原因码（缺省同 ``str(exc)``）。"""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.reason = reason or message


class FetchBlocked(FetchError):
    """SSRF 拒绝（非公网地址、非法 scheme / 端口 / userinfo）或 robots 禁止。``reason`` 缺省 ``ssrf_blocked``。"""

    def __init__(self, message: str, *, reason: str = "ssrf_blocked") -> None:
        super().__init__(message, reason=reason)


# ---------------------------------------------------------------- URL 校验


def normalize_public_url(value: str, *, allow_http: bool = True) -> str:
    """语法级校验：scheme ∈ {http, https}（``allow_http=False`` 时仅 https）、禁止 userinfo、端口 ∈ {缺省, 80, 443}、
    主机 IDNA 小写、折叠 path 中的 ``//``；失败抛 ``FetchBlocked``（``str(exc)`` 为中文说明）；不做 DNS。"""
    try:
        return canonicalize_public_url(value, allow_http=allow_http)
    except InvalidURLError as exc:
        raise FetchBlocked(str(exc)) from exc


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """公网地址：``is_global`` 且非多播 / 保留 / 回环 / 链路本地 / 未指定；IPv4 映射 / 6to4 等内嵌地址按内嵌 IPv4 判定。"""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return False
    if ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_private:
        return False
    return bool(ip.is_global)


def _host_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.strip("[]").rstrip(".").lower()


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise FetchError("dns_failed") from exc
    addresses = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        if addr not in addresses:
            addresses.append(addr)
    if not addresses:
        raise FetchError("dns_failed")
    return addresses


def assert_public_url(value: str, *, allow_http: bool = True) -> str:
    """``normalize_public_url`` + 主机黑名单（localhost、``*.local``、``*.internal``、``*.localhost``）+
    IP 字面量与 ``getaddrinfo`` 全部地址均须为公网地址；返回规范化 URL。

    非公网 → ``FetchBlocked``；DNS 解析失败 → ``FetchError("dns_failed")``。
    """
    url = normalize_public_url(value, allow_http=allow_http)
    host = _host_of(url)
    if not host:
        raise FetchBlocked("URL 缺少主机名")
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise FetchBlocked(f"不允许访问内部主机：{host}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(literal):
            raise FetchBlocked(f"不允许访问非公网地址：{host}")
        return url
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    for addr in _resolve(host, port):
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise FetchBlocked(f"无法识别的解析地址：{addr}") from exc
        if not is_public_ip(ip):
            raise FetchBlocked(f"主机 {host} 解析为非公网地址")
    return url


# ---------------------------------------------------------------- 媒体流


class MediaStreamError(Exception):
    """``read_media_stream`` 的校验失败（超限 / Content-Type / 魔数 / 读取失败），由调用方转为 ``ZhiqiError(TRANSFER_FAILED)``。"""


def content_type_allowed(content_type: str | None, allowed_types: tuple[str, ...]) -> bool:
    """``Content-Type`` 主类型（去参数、小写）以 ``allowed_types`` 任一项开头（项以 ``/`` 结尾时为前缀匹配）。"""
    if not content_type:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    for allowed in allowed_types:
        allowed = allowed.strip().lower()
        if not allowed:
            continue
        if allowed.endswith("/"):
            if main.startswith(allowed):
                return True
        elif main == allowed:
            return True
    return False


def _sniff_consistent(content_type: str, sniffed: str) -> bool:
    main = content_type.split(";", 1)[0].strip().lower()
    if main.startswith("image/"):
        return sniffed.startswith("image/")
    if main.startswith("video/"):
        return sniffed.startswith("video/")
    return True  # application/octet-stream 等：魔数可识别即可


def read_media_stream(
    response: httpx.Response, dest: BinaryIO, *, max_bytes: int, allowed_types: tuple[str, ...]
) -> tuple[int, str]:
    """把已打开的流式响应写入 ``dest``：校验 Content-Type 白名单、``Content-Length`` 与实际字节上限、
    首部魔数（``storage.sniff_media_type``，且与 Content-Type 主类型一致）。返回 ``(写入字节数, 识别出的 MIME)``。"""
    content_type = response.headers.get("content-type", "")
    if not content_type_allowed(content_type, allowed_types):
        raise MediaStreamError(f"Content-Type 不允许：{content_type or '缺失'}")
    declared = response.headers.get("content-length")
    if declared and declared.strip().isdigit() and int(declared) > max_bytes:
        raise MediaStreamError(f"文件超过大小上限 {max_bytes} 字节")
    total = 0
    head = b""
    sniffed: str | None = None
    try:
        for chunk in response.iter_bytes(_CHUNK_SIZE):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise MediaStreamError(f"文件超过大小上限 {max_bytes} 字节")
            if sniffed is None:
                head = (head + chunk)[:_SNIFF_BYTES]
                if len(head) >= _SNIFF_BYTES:
                    sniffed = _check_magic(head, content_type)
            dest.write(chunk)
    except httpx.HTTPError as exc:
        raise MediaStreamError(f"读取下载内容失败：{type(exc).__name__}") from exc
    if sniffed is None:
        sniffed = _check_magic(head, content_type)
    return total, sniffed


def _check_magic(head: bytes, content_type: str) -> str:
    sniffed = sniff_media_type(head)
    if sniffed is None:
        raise MediaStreamError("文件魔数无法识别为图片或视频")
    if not _sniff_consistent(content_type, sniffed):
        raise MediaStreamError(f"文件魔数 {sniffed} 与 Content-Type {content_type} 不符")
    return sniffed


def stream_public_bytes(
    url: str,
    dest: BinaryIO,
    *,
    max_bytes: int,
    allowed_types: tuple[str, ...] = DEFAULT_MEDIA_TYPES,
    max_redirects: int = 3,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> DownloadResult:
    """媒体转存专用的第三方 CDN 下载。

    ``follow_redirects=False``，每一跳先 ``assert_public_url`` 再请求，最多 ``max_redirects`` 跳；只带 ``User-Agent``
    （不带 Authorization / Cookie）；``timeout`` 为读超时。成功返回 ``DownloadResult(source="cdn", request_id=None,
    http_status=最终一跳状态码, request_ids=[])``；任何失败抛 ``ZhiqiError(TRANSFER_FAILED)``，携带已收到的 ``http_status``。
    ``transport`` 仅供测试注入。
    """
    allow_http = True
    headers = {"User-Agent": settings.zhiqi_user_agent, "Accept": "image/*,video/*,*/*;q=0.5"}
    started = time.monotonic()
    current = url
    redirects = 0
    last_status: int | None = None
    client = httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
        transport=transport,
        trust_env=transport is None,
    )
    try:
        while True:
            try:
                current = assert_public_url(current, allow_http=allow_http)
            except FetchError as exc:
                raise ZhiqiError(
                    ErrorCategory.TRANSFER_FAILED, f"下载地址未通过公网校验：{exc.message}", http_status=last_status
                ) from exc
            try:
                with client.stream("GET", current, headers=headers) as response:
                    last_status = response.status_code
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise ZhiqiError(ErrorCategory.TRANSFER_FAILED, "重定向缺少 Location", http_status=last_status)
                        redirects += 1
                        if redirects > max_redirects:
                            raise ZhiqiError(ErrorCategory.TRANSFER_FAILED, "too_many_redirects", http_status=last_status)
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status_code < 300:
                        raise ZhiqiError(
                            ErrorCategory.TRANSFER_FAILED, f"下载失败：HTTP {response.status_code}", http_status=last_status
                        )
                    try:
                        size, _mime = read_media_stream(response, dest, max_bytes=max_bytes, allowed_types=allowed_types)
                    except MediaStreamError as exc:
                        raise ZhiqiError(ErrorCategory.TRANSFER_FAILED, str(exc), http_status=last_status) from exc
            except httpx.HTTPError as exc:
                raise ZhiqiError(
                    ErrorCategory.TRANSFER_FAILED, f"下载请求失败：{type(exc).__name__}", http_status=last_status
                ) from exc
            latency_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "cdn GET %s http_status=%s latency_ms=%s bytes=%s redirects=%s",
                urlsplit(current).netloc, last_status, latency_ms, size, redirects,
            )
            return DownloadResult(source="cdn", request_id=None, http_status=int(last_status), request_ids=[])
    finally:
        client.close()


# ---------------------------------------------------------------- 链接删除检测抓取（docs/11 §6.1）

DEFAULT_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
DEFAULT_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")
CONNECT_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 10.0
POOL_TIMEOUT_SECONDS = 5.0
MAX_PAGE_TEXT_CHARS = 40000
# 平台 fetch_config.headers 白名单（docs/11 §5.3；schemas/platform.py 保存时已校验，这里再兜底过滤一次）
ALLOWED_EXTRA_HEADERS = frozenset({"accept-language", "referer"})
FORBIDDEN_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})
ROBOTS_CACHE_PREFIX = "robots:"
ROBOTS_ALLOW_TTL_SECONDS = 86400
ROBOTS_DENY_TTL_SECONDS = 900
ROBOTS_MAX_BYTES = 512 * 1024
_META_CHARSET_RE = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)


def _monitor_user_agent() -> str:
    return getattr(settings, "monitor_user_agent", None) or "aicreatLinkMonitor/1.0 (+https://example.com/contact)"


@dataclass(frozen=True)
class FetchConfig:
    """单次抓取的限制（``link_check_service.build_fetch_config`` 以 ``monitoring_config.link_check`` 为底、平台非空键覆盖）。"""

    timeout_seconds: float = 15
    max_response_bytes: int = 2_097_152
    max_redirects: int = 3
    user_agent: str = field(default_factory=_monitor_user_agent)
    headers: dict[str, str] = field(default_factory=dict)       # 已经 schemas/platform.py 白名单校验
    allow_http: bool = True
    respect_robots: bool = False


@dataclass
class PageResult:
    status: int
    final_url: str
    redirect_chain: list[tuple[int, str]]      # [(302, "https://…"), …]：发出重定向的每一跳（状态码, 该跳 URL）
    headers: dict[str, str]                    # 小写键；排除 set-cookie
    is_html: bool
    title: str | None
    text: str                                  # 正文纯文本（≤ 40000 字符）
    response_bytes: int
    duration_ms: int

    @property
    def redirect_count(self) -> int:
        return len(self.redirect_chain)

    @property
    def redirects(self) -> list[str]:
        """证据中的 ``redirects``：重定向经过的 URL 字符串数组。"""
        return [url for _status, url in self.redirect_chain]


def _with_chain(exc: FetchError, chain: list[tuple[int, str]], status: int | None = None) -> FetchError:
    """给异常附上已经过的重定向链与最近一跳状态码（判定证据用）。"""
    exc.redirect_chain = list(chain)  # type: ignore[attr-defined]
    if status is not None:
        exc.http_status = status  # type: ignore[attr-defined]
    return exc


def request_headers(config: FetchConfig, accept: str = DEFAULT_ACCEPT) -> dict[str, str]:
    """只发送 User-Agent / Accept / Accept-Language 与平台白名单头（``Accept-Language`` / ``Referer`` / ``X-*``）；
    ``Cookie`` / ``Authorization`` / ``Proxy-Authorization`` 与其它头一律丢弃；值含换行的头丢弃。"""
    headers = {
        "User-Agent": (config.user_agent or _monitor_user_agent()).strip(),
        "Accept": accept,
        "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
    }
    for name, value in (config.headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        lowered = name.strip().lower()
        if not lowered or lowered in FORBIDDEN_HEADERS or "\r" in value or "\n" in value:
            continue
        if lowered in ALLOWED_EXTRA_HEADERS or lowered.startswith("x-"):
            canonical = "Accept-Language" if lowered == "accept-language" else ("Referer" if lowered == "referer" else name.strip())
            headers[canonical] = value.strip()
    return headers


def _summary_headers(headers: httpx.Headers) -> dict[str, str]:
    """响应头（小写键，排除 ``set-cookie``）；同名多值以 ``, `` 合并。"""
    result: dict[str, str] = {}
    for key, value in headers.multi_items():
        lowered = key.lower()
        if lowered == "set-cookie":
            continue
        result[lowered] = f"{result[lowered]}, {value}" if lowered in result else value
    return result


def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.strip().isdigit() and int(declared) > max_bytes:
        raise FetchError("response_too_large")
    buffer = bytearray()
    for chunk in response.iter_bytes(_CHUNK_SIZE):
        if not chunk:
            continue
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise FetchError("response_too_large")
    return bytes(buffer)


def _http_error(exc: httpx.HTTPError, config: FetchConfig) -> FetchError:
    """httpx 异常 → ``FetchError``：消息只含异常类型与简短原因（不含响应体），如 ``ReadTimeout after 15s``。"""
    name = type(exc).__name__
    if isinstance(exc, httpx.ConnectTimeout):
        return FetchError(f"{name} after {CONNECT_TIMEOUT_SECONDS:g}s", reason="timeout")
    if isinstance(exc, httpx.TimeoutException):
        return FetchError(f"{name} after {float(config.timeout_seconds):g}s", reason="timeout")
    return FetchError(name, reason="network_error")


def fetch_public_bytes(
    url: str,
    *,
    config: FetchConfig,
    accept: str = DEFAULT_ACCEPT,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes, str, int, dict[str, str], list[tuple[int, str]]]:
    """SSRF 安全抓取，返回 ``(body, final_url, status, headers, redirect_chain)``。

    ``httpx.Client(follow_redirects=False, timeout=Timeout(connect=5, read=timeout_seconds, write=10, pool=5))``；
    手动跟随 3xx（``Location`` 相对地址 ``urljoin``，每跳先 ``assert_public_url``），跳数 > ``max_redirects`` 抛
    ``FetchError("too_many_redirects")``；流式读取超过 ``max_response_bytes`` 抛 ``FetchError("response_too_large")``；
    4xx/5xx 不抛、原样返回；每跳前清空 Cookie 罐，不复用上一跳的响应 Cookie。``transport`` 仅供测试注入。
    """
    headers = request_headers(config, accept)
    chain: list[tuple[int, str]] = []
    current = url
    max_redirects = max(0, int(config.max_redirects))
    max_bytes = max(1, int(config.max_response_bytes))
    client = httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(
            connect=CONNECT_TIMEOUT_SECONDS, read=float(config.timeout_seconds), write=WRITE_TIMEOUT_SECONDS, pool=POOL_TIMEOUT_SECONDS
        ),
        transport=transport,
        trust_env=transport is None,
    )
    try:
        while True:
            try:
                current = assert_public_url(current, allow_http=config.allow_http)
            except FetchError as exc:
                raise _with_chain(exc, chain) from exc
            client.cookies.clear()
            try:
                with client.stream("GET", current, headers=headers) as response:
                    status = int(response.status_code)
                    location = response.headers.get("location")
                    if status in REDIRECT_STATUSES and location:
                        chain.append((status, current))
                        if len(chain) > max_redirects:
                            raise _with_chain(FetchError("too_many_redirects"), chain, status)
                        current = urljoin(current, location.strip())
                        continue
                    try:
                        body = _read_limited(response, max_bytes)
                    except FetchError as exc:
                        raise _with_chain(exc, chain, status) from exc
                    return body, current, status, _summary_headers(response.headers), chain
            except httpx.HTTPError as exc:
                raise _with_chain(_http_error(exc, config), chain) from exc
    finally:
        client.close()


def _charset_of(content_type: str, body: bytes) -> str:
    match = re.search(r"charset\s*=\s*\"?([A-Za-z0-9_.:-]+)", content_type or "", re.IGNORECASE)
    if match:
        return match.group(1)
    meta = _META_CHARSET_RE.search(body[:4096])
    if meta:
        return meta.group(1).decode("ascii", "ignore")
    return "utf-8"


def decode_html(body: bytes, content_type: str) -> str:
    """按 charset（Content-Type → ``<meta charset>`` → utf-8）解码，``errors="replace"``；未知编码回退 utf-8。"""
    charset = _charset_of(content_type, body)
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def is_html_content_type(content_type: str | None) -> bool:
    main = (content_type or "").split(";", 1)[0].strip().lower()
    return main in HTML_CONTENT_TYPES


def fetch_page(url: str, *, config: FetchConfig, transport: httpx.BaseTransport | None = None) -> PageResult:
    """抓取页面：``respect_robots=True`` 时先 ``robots_allowed``（不允许抛 ``FetchBlocked("blocked_by_robots")``）；
    ``fetch_public_bytes`` → Content-Type 为 ``text/html`` / ``application/xhtml+xml`` 时解码并 ``extract_main_text``，
    否则 ``is_html=False``、``title=None``、``text=""``。"""
    started = time.monotonic()
    if config.respect_robots and not robots_allowed(
        url, user_agent=config.user_agent, timeout=float(config.timeout_seconds), allow_http=config.allow_http, transport=transport
    ):
        raise FetchBlocked("blocked_by_robots", reason="blocked_by_robots")
    body, final_url, status, headers, chain = fetch_public_bytes(url, config=config, transport=transport)
    content_type = headers.get("content-type", "")
    is_html = is_html_content_type(content_type)
    title: str | None = None
    text = ""
    if is_html:
        title, text = extract_main_text(decode_html(body, content_type))
        text = text[:MAX_PAGE_TEXT_CHARS]
    return PageResult(
        status=status,
        final_url=final_url,
        redirect_chain=chain,
        headers=headers,
        is_html=is_html,
        title=title,
        text=text,
        response_bytes=len(body),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _robots_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{ROBOTS_CACHE_PREFIX}{(parts.netloc or '').lower()}"


def _robots_cache_get(key: str) -> dict | None:
    from app.core.redis import redis_client

    try:
        raw = redis_client.get(key)
    except redis.RedisError as exc:
        logger.warning("读取 robots 缓存失败 key=%s: %s", key, exc)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _robots_cache_set(key: str, value: dict, ttl: int) -> None:
    from app.core.redis import redis_client

    try:
        redis_client.set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), ex=ttl)
    except redis.RedisError as exc:
        logger.warning("写入 robots 缓存失败 key=%s: %s", key, exc)


def robots_allowed(
    url: str,
    *,
    user_agent: str,
    timeout: float,
    allow_http: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """robots.txt 是否允许抓取 ``url``（缓存 ``robots:{domain}`` JSON ``{allowed, fetched_at, rules?}``）。

    robots.txt 返回 2xx → 按规则判定并缓存 86400s；404/410 → 视为允许（缓存 86400s）；其它失败（4xx/5xx、网络错误、
    SSRF 拒绝）→ 视为不允许并缓存 900s（严格模式）。缓存的是 robots.txt 文本，不同路径共用同一份。
    """
    try:
        target = normalize_public_url(url, allow_http=allow_http)
    except FetchBlocked:
        return False
    key = _robots_key(target)
    cached = _robots_cache_get(key)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if cached is None:
        parts = urlsplit(target)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        config = FetchConfig(timeout_seconds=timeout, max_response_bytes=ROBOTS_MAX_BYTES, max_redirects=3,
                             user_agent=user_agent, allow_http=allow_http)
        try:
            body, _final, status, _headers, _chain = fetch_public_bytes(
                robots_url, config=config, accept="text/plain,*/*;q=0.1", transport=transport
            )
        except FetchError as exc:
            logger.info("robots.txt 获取失败 %s: %s，按不允许处理", parts.netloc, exc.reason)
            cached = {"allowed": False, "fetched_at": now_iso}
            _robots_cache_set(key, cached, ROBOTS_DENY_TTL_SECONDS)
        else:
            if status in (404, 410):
                cached = {"allowed": True, "fetched_at": now_iso}
                _robots_cache_set(key, cached, ROBOTS_ALLOW_TTL_SECONDS)
            elif 200 <= status < 300:
                cached = {"allowed": True, "fetched_at": now_iso, "rules": body.decode("utf-8", errors="replace")}
                _robots_cache_set(key, cached, ROBOTS_ALLOW_TTL_SECONDS)
            else:
                cached = {"allowed": False, "fetched_at": now_iso}
                _robots_cache_set(key, cached, ROBOTS_DENY_TTL_SECONDS)
    if not cached.get("allowed"):
        return False
    rules = cached.get("rules")
    if not rules:
        return True
    parser = RobotFileParser()
    parser.parse(str(rules).splitlines())
    return bool(parser.can_fetch(user_agent or _monitor_user_agent(), target))
