"""SSRF 安全抓取（docs/11-link-backfill-and-monitoring.md §6.1、docs/10-media-generation.md §11.1）。

本文件当前提供：
- ``normalize_public_url``：语法级校验与规范化（复用 ``urls.canonicalize_public_url``，不做 DNS），失败抛 ``FetchBlocked``；
- ``assert_public_url``：在此之上做主机黑名单与 DNS 解析后的公网地址校验；
- ``stream_public_bytes``：媒体转存专用的第三方 CDN 下载（``follow_redirects=False``、逐跳 ``assert_public_url``、≤ 3 跳、
  不带 Authorization，只带 User-Agent），返回 ``DownloadResult(source="cdn", …)``，失败抛 ``ZhiqiError(TRANSFER_FAILED)``；
- ``read_media_stream``：流式写入 + Content-Type 白名单 + 魔数校验 + 字节上限（``ZhiqiClient.stream_download`` 共用）。

链接删除检测用的 ``FetchConfig`` / ``PageResult`` / ``fetch_public_bytes`` / ``fetch_page`` / ``robots_allowed`` 由链接监控阶段补充到本文件，
复用这里的 ``FetchError`` / ``FetchBlocked`` / ``assert_public_url``。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import BinaryIO
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.config import settings
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
