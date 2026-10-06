"""SEO 提供器 ``bing_webmaster``（仅 ``bing``，仅自有已验证站点；docs/11 §7.2、docs/03 B.22）。

Bing Webmaster API（``apikey`` 取 ``credential_env`` 指向的环境变量，缺省 ``SEO_BING_WEBMASTER_API_KEY``；站点取
``site_url_env``，缺省 ``SEO_BING_SITE_URL``）的 ``GetUrlInfo`` 查询该 URL 的索引 / 抓取信息（方法名与字段以 Microsoft 官方
文档为准）：返回 ``UrlInfo`` 且已被抓取（``LastCrawledDate`` 为有效时间、``HttpStatus`` 为 200 或缺省）→ ``indexed``；
否则 ``not_indexed``。

- Mock 模式或凭据 / 站点为空 → ``unknown`` + ``auth_failed`` + ``credential_missing``（不发请求）；
- ``link.domain``（含子域）不属于站点域名 → ``unknown`` + ``error_message=not_own_site``（不计 ``error_category``）；
- ``query_text`` / ``model`` / ``request_id`` / ``ai_task_id`` 恒为 NULL；HTTP 失败按 docs/08 分类；不写库。
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.zhiqi.types import ErrorCategory
from app.models import PublishLink
from app.services.index_providers.base import MSG_CREDENTIAL_MISSING, MSG_NOT_OWN_SITE, CheckContext, CheckResult
from app.services.index_providers.http import ProviderHTTPError, belongs_to_site, env_value, request_json

PROVIDER = "bing_webmaster"
API_URL = "https://ssl.bing.com/webmaster/api.svc/json/GetUrlInfo"
DEFAULT_CREDENTIAL_ENV = "SEO_BING_WEBMASTER_API_KEY"
DEFAULT_SITE_ENV = "SEO_BING_SITE_URL"
_DATE_RE = re.compile(r"/Date\((-?\d+)")


def _crawled(value: Any) -> bool:
    """``/Date(1696550400000)/`` 形式的时间是否有效（``DateTime.MinValue`` 序列化为负数）。"""
    if not isinstance(value, str):
        return False
    match = _DATE_RE.search(value)
    return bool(match) and int(match.group(1)) > 0


def is_indexed(data: Any) -> bool:
    info = data.get("d") if isinstance(data, Mapping) else None
    if not isinstance(info, Mapping) or not info.get("Url"):
        return False
    status = info.get("HttpStatus")
    if status not in (None, 0, 200):
        return False
    if info.get("IsPage") is False:
        return False
    return _crawled(info.get("LastCrawledDate"))


class BingWebmasterProvider:
    code = PROVIDER

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._config = dict(config or {})

    def _envs(self, config: Mapping[str, Any] | None) -> tuple[str, str]:
        cfg = {**self._config, **dict(config or {})}
        return str(cfg.get("credential_env") or DEFAULT_CREDENTIAL_ENV), str(cfg.get("site_url_env") or DEFAULT_SITE_ENV)

    def available(self, config: Mapping[str, Any] | None = None) -> tuple[bool, str | None]:
        if settings.zhiqi_mock_mode:
            return False, "mock_mode"
        key_env, site_env = self._envs(config)
        if not env_value(key_env) or not env_value(site_env):
            return False, MSG_CREDENTIAL_MISSING
        return True, None

    def check(self, db: Session, link: PublishLink, engine: str, query_by: list[str], *, ctx: CheckContext) -> CheckResult:
        del db, query_by
        cfg = {**self._config, **dict(ctx.provider_config or {})}
        evidence: dict[str, Any] = {"source": PROVIDER, "engine": engine}
        ok, _reason = self.available(cfg)
        if not ok:
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence,
                               error_category=ErrorCategory.AUTH_FAILED.value, error_message=MSG_CREDENTIAL_MISSING)
        key_env, site_env = self._envs(cfg)
        site = env_value(site_env)
        if not belongs_to_site(link.domain, site):
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence, error_message=MSG_NOT_OWN_SITE)
        started = time.monotonic()
        try:
            data = request_json(
                "GET", API_URL, timeout=float(cfg.get("timeout_seconds") or 30),
                params={"apikey": env_value(key_env), "siteUrl": site, "url": link.url},
                headers={"Accept": "application/json"},
            )
        except ProviderHTTPError as exc:
            evidence["http_status"] = exc.http_status
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence,
                               duration_ms=int((time.monotonic() - started) * 1000),
                               error_category=exc.category, error_message=exc.message)
        info = data.get("d") if isinstance(data, Mapping) and isinstance(data.get("d"), Mapping) else {}
        evidence["url_info"] = {k: info.get(k) for k in ("Url", "HttpStatus", "IsPage", "LastCrawledDate", "DiscoveryDate",
                                                         "DocumentSize", "AnchorCount") if k in info}
        indexed = is_indexed(data)
        return CheckResult(
            status="indexed" if indexed else "not_indexed",
            provider=PROVIDER,
            match_mode="url" if indexed else "none",
            evidence_url=link.url if indexed else None,
            evidence=evidence,
            confidence=None,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = ["PROVIDER", "BingWebmasterProvider", "is_indexed"]
