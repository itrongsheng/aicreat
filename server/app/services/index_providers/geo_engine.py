"""GEO 引用检测引擎 ``ZhiqiModelEngine``（``provider="zhiqi_model"``；docs/11 §8.2~§8.5）。

- ``precheck``（调用前、不调用上游、不写库）：只对 ``parse.match_mode=title`` 的引擎做防护——解析出的模板 ``user_prompt``
  含 ``{{title}}``（如项目 ``default_templates_json`` 覆盖了模板），或 ``keyword`` 为空（§8.3 会以 ``title`` 代替）时返回
  ``unknown`` + ``error_message=title_in_prompt``；``index_check_service`` 命中时不创建根任务、不调用 ``check``；
- ``check``：渲染 ``sys_geo_query``（``keyword`` 为空时以 ``title`` 代替）→ ``complete_text``（引擎自身 ``model`` /
  ``protocol`` 作为 ``model_override`` / ``protocol_override``，**不回退**到 ``capability_routes(geo_check)`` 主模型；读超时
  ``timeout_seconds``；``extra`` 透传）→ ``extract_citations`` → 判定：

  * 回答里没有任何引用 URL → ``unknown`` + ``no_search_evidence``（不计 ``not_cited``）；
  * ``url`` / ``domain`` / ``url_or_domain``：``match_citations`` 命中 → ``cited``（``url`` 1.0 / ``domain`` 0.6），否则
    ``not_cited``，不做标题兜底；共享域名平台的同域引用不算命中（``domain_match_ignored``）；
  * ``title``（须显式配置）：按句最大相似度 ``>= title_fuzzy_threshold`` → ``cited``（``confidence`` = 相似度）。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import PublishLink
from app.services import ai_gateway_service as gateway
from app.services import prompt_template_service as pts
from app.services.index_providers.base import (
    MSG_NO_SEARCH_EVIDENCE,
    MSG_TITLE_IN_PROMPT,
    CheckContext,
    CheckResult,
    citation_dict,
    excerpt,
    failure_result,
    has_title_placeholder,
    is_mock_response,
    match_citations,
    matched_evidence,
    sentence_with,
)

PROVIDER = "zhiqi_model"
KIND = "geo_query"
OPERATION = "geo_check"
DEFAULT_TEMPLATE_CODE = "sys_geo_query"
DEFAULT_THRESHOLD = 0.8


def _parse(engine: dict[str, Any]) -> dict[str, Any]:
    parse = engine.get("parse") if isinstance(engine.get("parse"), dict) else {}
    return {
        "match_mode": str(parse.get("match_mode") or "url_or_domain"),
        "title_fuzzy_threshold": float(parse.get("title_fuzzy_threshold") if parse.get("title_fuzzy_threshold") is not None
                                       else DEFAULT_THRESHOLD),
    }


def _template(db: Session, engine: dict[str, Any], ctx: CheckContext) -> Any:
    return pts.resolve_template_for_code(
        db, KIND, ctx.project_id, ctx.language, str(engine.get("prompt_template_code") or DEFAULT_TEMPLATE_CODE)
    )


def build_variables(ctx: CheckContext) -> dict[str, Any]:
    """``keyword`` 为空时以 ``title`` 代替（§8.3）；``url`` / ``domain`` 只供自定义模板使用（默认模板不写入提问正文）。"""
    keyword = (ctx.keyword or "").strip() or ctx.title
    return {"keyword": keyword, "title": ctx.title, "url": ctx.url, "domain": ctx.domain, "engine_name": ctx.engine_name,
            "language": ctx.language or ""}


class ZhiqiModelEngine:
    code = PROVIDER

    def precheck(self, db: Session, link: PublishLink, engine: dict, *, ctx: CheckContext) -> CheckResult | None:
        parse = _parse(engine)
        if parse["match_mode"] != "title":
            return None
        if not (ctx.keyword or "").strip():
            return CheckResult(status="unknown", provider=PROVIDER, error_message=MSG_TITLE_IN_PROMPT,
                               evidence={"source": PROVIDER, "engine": ctx.engine, "reason": "keyword_empty"})
        try:
            template = _template(db, engine, ctx)
        except Exception as exc:  # noqa: BLE001 - 模板解析失败按普通失败处理（不创建根任务）
            return failure_result(PROVIDER, exc, evidence={"source": PROVIDER, "engine": ctx.engine})
        if has_title_placeholder(template.user_prompt):
            return CheckResult(status="unknown", provider=PROVIDER, error_message=MSG_TITLE_IN_PROMPT,
                               evidence={"source": PROVIDER, "engine": ctx.engine, "reason": "template_has_title",
                                         "template_code": template.code})
        return None

    def check(self, db: Session, link: PublishLink, engine: dict, *, ctx: CheckContext) -> CheckResult:
        started = time.monotonic()
        parse = _parse(engine)
        query_text: str | None = None
        evidence: dict[str, Any] = {"source": PROVIDER, "engine": ctx.engine}
        try:
            if ctx.root_task is None:
                raise RuntimeError("GEO 引擎检测需要预先创建的根任务")
            template = _template(db, engine, ctx)
            system, user = pts.render(template, build_variables(ctx))
            query_text = user
            evidence["query_text"] = user
            timeout = engine.get("timeout_seconds")
            result, attempt = gateway.complete_text(
                db,
                root_task=ctx.root_task,
                messages=pts.messages_for(system, user),
                params=pts.call_params(template),
                response_format="text",
                extra=dict(engine.get("extra") or {}),
                model_override=(str(engine.get("model") or "").strip() or None),
                protocol_override=engine.get("protocol") or None,
                timeout_override=float(timeout) if timeout else None,
                metadata=pts.call_metadata(template, OPERATION),
            )
        except Exception as exc:  # noqa: BLE001 - 不抛出未分类异常（§7.1）
            return failure_result(PROVIDER, exc, query_text=query_text, evidence=evidence,
                                  duration_ms=int((time.monotonic() - started) * 1000))

        answer = result.text or ""
        citations = list(result.citations or [])
        notes: dict[str, Any] = {}
        if is_mock_response(result.raw):
            evidence["source"] = "mock"
        evidence["citations"] = [citation_dict(c) for c in citations]
        evidence["answer_excerpt"] = excerpt(answer)
        common = dict(provider=PROVIDER, query_text=query_text, ai_task_id=attempt.id, request_id=result.request_id,
                      model=result.model or attempt.model, duration_ms=int((time.monotonic() - started) * 1000))
        if not citations:
            evidence.update({"matched": None, "domain_match_ignored": False})
            return CheckResult(status="unknown", error_message=MSG_NO_SEARCH_EVIDENCE, evidence=evidence, **common)
        mode = parse["match_mode"]
        match_mode, citation, confidence = match_citations(
            citations, link, mode, ctx.platform_code, notes=notes, answer=answer, title=ctx.title,
            threshold=parse["title_fuzzy_threshold"],
        )
        evidence["matched"] = matched_evidence(match_mode, citation)
        evidence["domain_match_ignored"] = bool(notes.get("domain_match_ignored"))
        if mode == "title":
            evidence["title_similarity"] = notes.get("title_similarity", 0.0)
        if match_mode == "none":
            return CheckResult(status="not_cited", match_mode="none", evidence=evidence, confidence=None, **common)
        snippet = None
        if citation is not None:
            snippet = citation.snippet or sentence_with(answer, citation.url)
        return CheckResult(
            status="cited",
            match_mode=match_mode,
            evidence_title=citation.title if citation else None,
            evidence_snippet=snippet,
            evidence_url=citation.url if citation else None,
            evidence=evidence,
            confidence=confidence if isinstance(confidence, Decimal) else Decimal(str(confidence)),
            **common,
        )


__all__ = ["PROVIDER", "ZhiqiModelEngine", "build_variables"]
