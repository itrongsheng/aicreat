"""SEO 提供器 ``baidu_ai_search``（仅 ``baidu``；docs/11 §7.2、docs/03 B.22）。

百度 AI 搜索官方检索 API（``providers.baidu_ai_search.endpoint``，缺省 ``https://qianfan.baidubce.com/v2/ai_search/web_search``；
路径来自 BRIEF，主机与鉴权头 / 请求 / 响应字段以百度官方文档为准）。凭据取 ``credential_env`` 指向的环境变量
（缺省 ``SEO_BAIDU_AI_SEARCH_API_KEY``），以 ``Authorization: Bearer <key>`` 发送；``query_by`` 含 ``url`` 与 ``title`` 时
各检索一次（``top_k``、``timeout_seconds`` 取配置）。

判定：任一结果 URL 按 ``url_or_domain`` 命中（共享域名平台忽略域名命中）→ ``indexed``；全部查询均未命中 → ``not_indexed``。
凭据为空或 Mock 模式 → ``unknown`` + ``error_category=auth_failed`` + ``error_message=credential_missing``（不发请求，
``query_text`` 为 NULL）；HTTP 失败按 docs/08 分类。``query_text`` 记实际检索词（两次以 `` | `` 连接）；``model`` /
``request_id`` / ``ai_task_id`` 恒为 NULL。不创建 ``ai_tasks``，不写库。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.zhiqi.types import Citation, ErrorCategory
from app.models import PublishLink
from app.services.index_providers.base import (
    MSG_CREDENTIAL_MISSING,
    CheckContext,
    CheckResult,
    citation_dict,
    match_citations,
    matched_evidence,
)
from app.services.index_providers.http import ProviderHTTPError, env_value, request_json

PROVIDER = "baidu_ai_search"
DEFAULT_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
DEFAULT_CREDENTIAL_ENV = "SEO_BAIDU_AI_SEARCH_API_KEY"
QUERY_JOINER = " | "
MATCH_MODE = "url_or_domain"


def _references(data: Any) -> list[Citation]:
    """响应中的检索结果：``references[]``（``url`` / ``title`` / ``content``|``snippet``）；兼容 ``result.references``。"""
    if not isinstance(data, Mapping):
        return []
    items = data.get("references")
    if items is None and isinstance(data.get("result"), Mapping):
        items = data["result"].get("references")
    result: list[Citation] = []
    for item in items or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("url"), str) or not item["url"].strip():
            continue
        snippet = item.get("content") if isinstance(item.get("content"), str) else item.get("snippet")
        result.append(Citation(
            url=item["url"].strip(),
            title=item.get("title") if isinstance(item.get("title"), str) else None,
            snippet=snippet[:1000] if isinstance(snippet, str) else None,
            source="annotation",
        ))
    return result


def build_queries(ctx: CheckContext, query_by: list[str]) -> list[str]:
    queries: list[str] = []
    if "url" in query_by and ctx.url:
        queries.append(ctx.url)
    if "title" in query_by and (ctx.title or "").strip():
        queries.append(ctx.title.strip())
    return queries


class BaiduAiSearchProvider:
    code = PROVIDER

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self._config = dict(config or {})

    def _credential_env(self, config: Mapping[str, Any] | None = None) -> str:
        cfg = {**self._config, **dict(config or {})}
        return str(cfg.get("credential_env") or DEFAULT_CREDENTIAL_ENV)

    def available(self, config: Mapping[str, Any] | None = None) -> tuple[bool, str | None]:
        if settings.zhiqi_mock_mode:
            return False, "mock_mode"
        if not env_value(self._credential_env(config)):
            return False, MSG_CREDENTIAL_MISSING
        return True, None

    def check(self, db: Session, link: PublishLink, engine: str, query_by: list[str], *, ctx: CheckContext) -> CheckResult:
        del db
        cfg = {**self._config, **dict(ctx.provider_config or {})}
        ok, _reason = self.available(cfg)
        evidence: dict[str, Any] = {"source": PROVIDER, "engine": engine}
        if not ok:
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence,
                               error_category=ErrorCategory.AUTH_FAILED.value, error_message=MSG_CREDENTIAL_MISSING)
        started = time.monotonic()
        queries = build_queries(ctx, list(query_by or []))
        query_text = QUERY_JOINER.join(queries) or None
        evidence["queries"] = queries
        if not queries:
            return CheckResult(status="unknown", provider=PROVIDER, evidence=evidence, query_text=None,
                               error_category=ErrorCategory.INVALID_RESPONSE.value, error_message="没有可检索的 URL 或标题")
        key = env_value(self._credential_env(cfg))
        endpoint = str(cfg.get("endpoint") or DEFAULT_ENDPOINT)
        timeout = float(cfg.get("timeout_seconds") or 30)
        top_k = int(cfg.get("top_k") or 10)
        all_refs: list[Citation] = []
        notes: dict[str, Any] = {}
        try:
            for query in queries:
                data = request_json(
                    "POST", endpoint, timeout=timeout,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "messages": [{"role": "user", "content": query}],
                        "search_source": "baidu_search_v2",
                        "resource_type_filter": [{"type": "web", "top_k": top_k}],
                    },
                )
                refs = _references(data)
                all_refs.extend(refs)
                match_mode, citation, confidence = match_citations(refs, link, MATCH_MODE, ctx.platform_code, notes=notes)
                if citation is not None:
                    evidence.update({"citations": [citation_dict(c) for c in all_refs[:50]],
                                     "matched": matched_evidence(match_mode, citation), "matched_query": query,
                                     "domain_match_ignored": bool(notes.get("domain_match_ignored"))})
                    return CheckResult(
                        status="indexed", provider=PROVIDER, match_mode=match_mode, query_text=query_text,
                        evidence_title=citation.title, evidence_snippet=citation.snippet, evidence_url=citation.url,
                        evidence=evidence, confidence=confidence, duration_ms=int((time.monotonic() - started) * 1000),
                    )
        except ProviderHTTPError as exc:
            evidence["http_status"] = exc.http_status
            return CheckResult(status="unknown", provider=PROVIDER, query_text=query_text, evidence=evidence,
                               duration_ms=int((time.monotonic() - started) * 1000),
                               error_category=exc.category, error_message=exc.message)
        evidence.update({"citations": [citation_dict(c) for c in all_refs[:50]], "matched": None,
                         "domain_match_ignored": bool(notes.get("domain_match_ignored"))})
        return CheckResult(status="not_indexed", provider=PROVIDER, match_mode="none", query_text=query_text, evidence=evidence,
                           duration_ms=int((time.monotonic() - started) * 1000))


__all__ = ["PROVIDER", "BaiduAiSearchProvider", "build_queries"]
