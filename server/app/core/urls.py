"""URL 规范化、哈希去重与平台识别工具（docs/11-link-backfill-and-monitoring.md §4.2）。

``normalized_url`` 只用于去重与展示；删除检测抓取始终使用原始 ``url``。
``canonicalize_public_url`` 是纯语法级校验与规范化（不做 DNS），``safe_fetch.normalize_public_url``
在其之上把 ``InvalidURLError`` 转为 ``FetchBlocked``。
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from functools import lru_cache
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = {
    "spm", "from", "share_token", "sharer_shareid", "sharer_sharetime", "srcid", "chksm", "mpshare",
    "scene", "subscene", "xsec_token", "xsec_source", "log_from", "wid", "wfr", "for", "fbclid", "gclid",
}

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (80, 443)
DEFAULT_PORTS = {"http": 80, "https": 443}
PATH_SAFE_CHARS = "/:@!$&'()*+,;=-._~"
MAX_URL_LENGTH = 1000


class InvalidURLError(ValueError):
    """URL 语法级校验失败（scheme / userinfo / 端口 / 主机）。``str(exc)`` 为中文说明。"""


def _encode_host(host: str) -> str:
    host = host.strip().rstrip(".").lower()
    if not host:
        raise InvalidURLError("URL 缺少主机名")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
    try:
        encoded = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidURLError("URL 主机名不合法") from exc
    if not re.fullmatch(r"[a-z0-9.-]+", encoded) or ".." in encoded or encoded.startswith(("-", ".")):
        raise InvalidURLError("URL 主机名不合法")
    return encoded


def canonicalize_public_url(value: str, *, allow_http: bool = True) -> str:
    """语法级校验 + 规范化：scheme ∈ {http, https}（``allow_http=False`` 时仅 https）、禁止 userinfo、
    端口 ∈ {缺省, 80, 443}、主机 IDNA 小写、折叠 path 中的 ``//``；拒绝 ``javascript:`` / ``file:`` / ``data:`` 等。

    保留 query 与 fragment 原样；失败抛 ``InvalidURLError``；不做 DNS。
    """
    if not isinstance(value, str):
        raise InvalidURLError("URL 必须是字符串")
    raw = value.strip()
    if not raw:
        raise InvalidURLError("URL 不能为空")
    if len(raw) > MAX_URL_LENGTH:
        raise InvalidURLError(f"URL 长度不能超过 {MAX_URL_LENGTH} 个字符")
    if any(ch in raw for ch in ("\r", "\n", "\t", "\x00", " ")):
        raise InvalidURLError("URL 不能包含空白或控制字符")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise InvalidURLError("URL 格式错误") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidURLError("URL 只允许 http 或 https 协议")
    if scheme == "http" and not allow_http:
        raise InvalidURLError("URL 只允许 https 协议")
    if not parts.netloc:
        raise InvalidURLError("URL 缺少主机名")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise InvalidURLError("URL 不允许包含用户名或密码")
    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidURLError("URL 端口不合法") from exc
    if port is not None and port not in ALLOWED_PORTS:
        raise InvalidURLError("URL 端口只允许 80 或 443")
    host = _encode_host(parts.hostname or "")
    netloc = host if port is None else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "")
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def normalize_url(url: str) -> str:
    """去重用规范化 URL。

    1. ``canonicalize_public_url``：scheme / host 小写、IDNA、userinfo 与端口校验、折叠 path 中的 ``//``；
    2. 去 fragment；去默认端口（http:80 / https:443）；
    3. query：``parse_qsl(keep_blank_values=True)`` → 去掉 ``TRACKING_PARAMS`` 与 ``utm_*`` → 按 key 稳定排序 → ``urlencode``；
    4. path：``quote(unquote(path), safe=...)`` 统一百分号编码；去尾部 ``/``（根路径 ``/`` 保留）。
    """
    parts = urlsplit(canonicalize_public_url(url))
    scheme = parts.scheme
    host = parts.netloc.rsplit(":", 1)[0] if parts.port is not None else parts.netloc
    netloc = host if parts.port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{parts.port}"

    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)]
    pairs.sort(key=lambda kv: kv[0])
    query = urlencode(pairs)

    path = quote(unquote(parts.path), safe=PATH_SAFE_CHARS)
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(normalized_url: str) -> str:
    """SHA-256 hex（64 位），写入 ``publish_links.url_hash``（UNIQUE）。"""
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def extract_domain(url: str) -> str:
    """hostname 小写（IDNA）、去前导 ``www.``，写入 ``publish_links.domain``；无法解析返回 ``""``。"""
    try:
        hostname = urlsplit((url or "").strip()).hostname or ""
    except ValueError:
        return ""
    hostname = hostname.rstrip(".").lower()
    if not hostname:
        return ""
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        logger.warning("URL 规则正则无法编译 %r: %s", pattern, exc)
        return None


def match_url_patterns(url: str, patterns: list[str] | None) -> bool:
    """任一正则 ``re.search(pattern, url, re.IGNORECASE)`` 命中即 ``True``；非法正则跳过。"""
    if not url or not patterns:
        return False
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern:
            continue
        compiled = _compile(pattern)
        if compiled is not None and compiled.search(url):
            return True
    return False
