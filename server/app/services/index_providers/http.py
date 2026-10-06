"""非 zhiqi 提供器（``baidu_ai_search`` / ``bing_webmaster`` / ``google_search_console``）共用的 HTTP 与凭据工具
（docs/11 §7.2；错误分类与 docs/08 一致：401/403 → ``auth_failed``、429 → ``rate_limited``、5xx → ``upstream_unavailable``、
超时 → ``timeout``）。

- 只调用各平台的**官方 API**（端点来自 ``seo_providers`` 配置或固定的官方地址），不请求搜索结果页 HTML；
- 凭据只从环境变量读取（``credential_env`` / ``site_url_env`` 指向的变量名，经 ``app.core.config.settings`` 读取），
  永不写入 settings、日志或证据；错误信息经 ``sanitize_error_message`` 脱敏；
- ``client_factory`` 可在测试中替换为 ``httpx.MockTransport``，生产代码不提供任何旁路。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from app.core.config import settings
from app.core.zhiqi.errors import sanitize_error_message
from app.core.zhiqi.types import ErrorCategory
from app.services.index_providers.base import domain_of


class ProviderHTTPError(Exception):
    """官方 API 调用失败：``category`` 为 docs/08 错误分类。"""

    def __init__(self, category: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.message = sanitize_error_message(message) or category
        self.http_status = http_status


def _default_factory(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), follow_redirects=False)


client_factory: Callable[[float], httpx.Client] = _default_factory


def env_value(name: str | None) -> str:
    """读取环境变量（经 ``Settings``，与 ``configured`` 判定同源）；未知或为空返回 ``""``。"""
    if not name:
        return ""
    return str(getattr(settings, str(name).lower(), "") or "").strip()


def category_for_status(status: int, body_text: str = "") -> str:
    if status in (401, 403):
        if status == 403 and "quota" in body_text.lower():
            return ErrorCategory.RATE_LIMITED.value
        return ErrorCategory.AUTH_FAILED.value
    if status == 429:
        return ErrorCategory.RATE_LIMITED.value
    if status >= 500:
        return ErrorCategory.UPSTREAM_UNAVAILABLE.value
    return ErrorCategory.INVALID_RESPONSE.value


def request_json(method: str, url: str, *, timeout: float, **kwargs: Any) -> Any:
    """发送请求并返回 JSON；HTTP 失败、超时、网络错误与非 JSON 响应一律抛 ``ProviderHTTPError``（不含响应体原文）。"""
    try:
        with client_factory(float(timeout)) as client:
            response = client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderHTTPError(ErrorCategory.TIMEOUT.value, f"{type(exc).__name__} after {timeout:g}s") from None
    except httpx.HTTPError as exc:
        raise ProviderHTTPError(ErrorCategory.UPSTREAM_UNAVAILABLE.value, f"{type(exc).__name__}") from None
    if response.status_code >= 400:
        snippet = response.text[:200] if response.text else ""
        raise ProviderHTTPError(
            category_for_status(response.status_code, snippet), f"HTTP {response.status_code}", http_status=response.status_code
        )
    try:
        return response.json()
    except ValueError:
        raise ProviderHTTPError(ErrorCategory.INVALID_RESPONSE.value, "响应不是合法 JSON", http_status=response.status_code) from None


def site_domain(site_url: str) -> str:
    """站点标识 → 域名：``sc-domain:example.com`` → ``example.com``；URL 前缀 → ``extract_domain``（去 ``www.``）。"""
    text = (site_url or "").strip()
    if text.lower().startswith("sc-domain:"):
        return text.split(":", 1)[1].strip().lower().rstrip(".").removeprefix("www.")
    if "://" not in text:
        text = f"https://{text}"
    return domain_of(text)


def belongs_to_site(link_domain: str, site_url: str) -> bool:
    """``link.domain``（含子域）属于站点域名。"""
    site = site_domain(site_url)
    domain = (link_domain or "").lower()
    return bool(site) and (domain == site or domain.endswith(f".{site}"))


__all__ = [
    "ProviderHTTPError",
    "belongs_to_site",
    "category_for_status",
    "client_factory",
    "env_value",
    "request_json",
    "site_domain",
]
