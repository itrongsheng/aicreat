"""SEO 提供器 ``google_search_console``（仅 ``google``，仅自有已验证站点；docs/11 §7.2、docs/03 B.22）。

URL Inspection API：``POST https://searchconsole.googleapis.com/v1/urlInspection/index:inspect``
``{"inspectionUrl": link.url, "siteUrl": SEO_GSC_SITE_URL}``。服务账号 JSON（``credential_env`` 指向的环境变量，缺省
``SEO_GSC_CREDENTIALS_FILE``，值为文件路径）经 ``google-auth`` 签发 JWT 断言，用 httpx 向 ``token_uri`` 换取访问令牌
（进程内缓存至过期前 5 分钟），再用 httpx 调用检查接口；字段以 Google 官方文档为准。

判定：``inspectionResult.indexStatusResult.verdict=PASS`` 且 ``coverageState`` 表示已编入索引 → ``indexed``，否则
``not_indexed``。Mock 模式或凭据 / 站点为空 → ``unknown`` + ``auth_failed`` + ``credential_missing``；非自有站点 →
``unknown`` + ``not_own_site``；超配额（429 或 403 含 quota）→ ``rate_limited``。``query_text`` / ``model`` /
``request_id`` / ``ai_task_id`` 恒为 NULL；不写库。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.zhiqi.types import ErrorCategory
from app.models import PublishLink
from app.services.index_providers.base import MSG_CREDENTIAL_MISSING, MSG_NOT_OWN_SITE, CheckContext, CheckResult
from app.services.index_providers.http import ProviderHTTPError, belongs_to_site, env_value, request_json

PROVIDER = "google_search_console"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
DEFAULT_CREDENTIAL_ENV = "SEO_GSC_CREDENTIALS_FILE"
DEFAULT_SITE_ENV = "SEO_GSC_SITE_URL"
TOKEN_LIFETIME_SECONDS = 3600
TOKEN_REFRESH_MARGIN_SECONDS = 300

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}


def _access_token(credentials_file: str, timeout: float) -> str:
    """服务账号 JWT 断言 → 访问令牌（按凭据文件路径缓存）。凭据文件不可读 / 格式错误 → ``auth_failed``。"""
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(credentials_file)
        if cached and cached[1] - TOKEN_REFRESH_MARGIN_SECONDS > now:
            return cached[0]
    try:
        from google.auth import crypt
        from google.auth import jwt as google_jwt

        with open(credentials_file, encoding="utf-8") as fh:
            info = json.load(fh)
        signer = crypt.RSASigner.from_service_account_info(info)
        token_uri = str(info.get("token_uri") or DEFAULT_TOKEN_URI)
        assertion = google_jwt.encode(signer, {
            "iss": info["client_email"], "scope": SCOPE, "aud": token_uri,
            "iat": int(now), "exp": int(now) + TOKEN_LIFETIME_SECONDS,
        })
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProviderHTTPError(ErrorCategory.AUTH_FAILED.value, f"服务账号凭据不可用：{type(exc).__name__}") from None
    data = request_json(
        "POST", token_uri, timeout=timeout,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
              "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion},
    )
    token = data.get("access_token") if isinstance(data, Mapping) else None
    if not isinstance(token, str) or not token:
        raise ProviderHTTPError(ErrorCategory.AUTH_FAILED.value, "令牌响应缺少 access_token")
    expires_in = int(data.get("expires_in") or TOKEN_LIFETIME_SECONDS)
    with _token_lock:
        _token_cache[credentials_file] = (token, now + expires_in)
    return token


def clear_token_cache() -> None:
    with _token_lock:
        _token_cache.clear()


def is_indexed(data: Any) -> tuple[bool, dict[str, Any]]:
    result = data.get("inspectionResult") if isinstance(data, Mapping) else None
    status = result.get("indexStatusResult") if isinstance(result, Mapping) else None
    if not isinstance(status, Mapping):
        return False, {}
    summary = {k: status.get(k) for k in ("verdict", "coverageState", "indexingState", "lastCrawlTime", "pageFetchState",
                                          "googleCanonical", "robotsTxtState") if k in status}
    coverage = str(status.get("coverageState") or "").lower()
    indexed_coverage = "indexed" in coverage and "not indexed" not in coverage and "unindexed" not in coverage
    return status.get("verdict") == "PASS" and indexed_coverage, summary


class GoogleSearchConsoleProvider:
    code = PROVIDER

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._config = dict(config or {})

    def _envs(self, config: Mapping[str, Any] | None) -> tuple[str, str]:
        cfg = {**self._config, **dict(config or {})}
        return str(cfg.get("credential_env") or DEFAULT_CREDENTIAL_ENV), str(cfg.get("site_url_env") or DEFAULT_SITE_ENV)

    def available(self, config: Mapping[str, Any] | None = None) -> tuple[bool, str | None]:
        if settings.zhiqi_mock_mode:
            return False, "mock_mode"
        cred_env, site_env = self._envs(config)
        if not env_value(cred_env) or not env_value(site_env):
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
        cred_env, site_env = self._envs(cfg)
        site = env_value(site_env)
        if not belongs_to_site(link.domain, site):
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence, error_message=MSG_NOT_OWN_SITE)
        timeout = float(cfg.get("timeout_seconds") or 30)
        started = time.monotonic()
        try:
            token = _access_token(env_value(cred_env), timeout)
            data = request_json(
                "POST", INSPECT_URL, timeout=timeout,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"inspectionUrl": link.url, "siteUrl": site},
            )
        except ProviderHTTPError as exc:
            evidence["http_status"] = exc.http_status
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence,
                               duration_ms=int((time.monotonic() - started) * 1000),
                               error_category=exc.category, error_message=exc.message)
        indexed, summary = is_indexed(data)
        evidence["index_status"] = summary
        return CheckResult(
            status="indexed" if indexed else "not_indexed",
            provider=PROVIDER,
            match_mode="url" if indexed else "none",
            evidence_url=link.url if indexed else None,
            evidence=evidence,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


__all__ = ["PROVIDER", "GoogleSearchConsoleProvider", "clear_token_cache", "is_indexed"]
