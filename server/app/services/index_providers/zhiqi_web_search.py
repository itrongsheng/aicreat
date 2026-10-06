"""SEO 提供器 ``zhiqi_web_search``（默认；docs/11 §7.2、§7.3）。

经 ``ai_gateway_service.complete_text``（``capability=seo_check``、``model_override=engines.<e>.model or None``、
``protocol_override=providers.zhiqi_web_search.protocol``、``extra``、读超时 ``timeout_seconds``）向具备联网检索能力的模型
提问（模板 ``sys_seo_query``，可被项目 ``default_templates_json["seo_query"]`` 覆盖），再按 §7.3 的八种情形判定：

1. **工具证据**：``TextResult.citations`` 中 ``source ∈ {annotation, mock}`` 的项（上游检索工具产生的 ``annotations`` /
   ``citations`` 字段，Mock 引用视同）；
2. **自述线索**：模型自己写出的 URL——JSON ``evidence[].url`` 与回答正文中的 Markdown 链接 / 裸 URL；提示词本身含
   ``URL：{{url}}``，未联网的模型原样回显即可「命中」，因此自述线索只用于标记 ``evidence_unverified``，不能单独判 ``indexed``。

结果反映的是该联网模型检索工具的可见性，**不等价于目标引擎的官方收录口径**。不请求任何搜索引擎结果页。
根任务由 ``index_check_service`` 预先创建（``ctx.root_task``）；本模块不写业务表。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.core.zhiqi import text as zhiqi_text
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import Citation, ErrorCategory
from app.models import PublishLink
from app.services import ai_gateway_service as gateway
from app.services import prompt_template_service as pts
from app.services.index_providers.base import (
    MSG_EVIDENCE_UNVERIFIED,
    MSG_NO_SEARCH_EVIDENCE,
    TOOL_EVIDENCE_SOURCES,
    CheckContext,
    CheckResult,
    citation_dict,
    excerpt,
    failure_result,
    is_mock_response,
    match_citations,
    matched_evidence,
    sentence_with,
    unique_citations,
)

PROVIDER = "zhiqi_web_search"
KIND = "seo_query"
OPERATION = "seo_check"
DEFAULT_TEMPLATE_CODE = "sys_seo_query"
MIN_ANSWER_CHARS = 10

# §7.3 判定表的置信度
CONF_TOOL_HIT_JSON = {"url": Decimal("1.0"), "domain": Decimal("0.6")}
CONF_TOOL_HIT_OTHER = {"url": Decimal("0.9"), "domain": Decimal("0.5")}
CONF_UNVERIFIED = Decimal("0.3")
CONF_NOT_INDEXED_JSON = Decimal("0.8")
CONF_NOT_INDEXED_OTHER = Decimal("0.5")


def build_variables(ctx: CheckContext, query_by: list[str]) -> dict[str, Any]:
    """``query_by`` 决定注入：不含 ``title`` 时 ``title`` 传空串（模板已说明「标题为空时只按 URL 检索」），不含 ``url`` 同理。"""
    return {
        "url": ctx.url if "url" in query_by else "",
        "title": ctx.title if "title" in query_by else "",
        "domain": ctx.domain,
        "engine_name": ctx.engine_name,
        "language": ctx.language or "",
    }


def _json_answer(answer: str) -> tuple[bool, bool | None, Any, list[Citation]]:
    """``(是否 JSON 回答, indexed 值, 解析结果, JSON evidence[].url 自述线索)``；只有含 ``indexed`` 字段的对象算 JSON 回答。"""
    try:
        parsed = zhiqi_text.extract_json(answer)
    except ZhiqiError:
        return False, None, None, []
    claims: list[Citation] = []
    if isinstance(parsed, Mapping):
        for item in parsed.get("evidence") or []:
            if isinstance(item, Mapping) and isinstance(item.get("url"), str) and item["url"].strip():
                claims.append(Citation(
                    url=item["url"].strip(),
                    title=item.get("title") if isinstance(item.get("title"), str) else None,
                    snippet=item.get("snippet") if isinstance(item.get("snippet"), str) else None,
                    source="json_evidence",
                ))
        if "indexed" in parsed:
            return True, bool(parsed.get("indexed")), parsed, claims
    return False, None, parsed, claims


def judge_answer(
    answer: str,
    citations: list[Citation],
    link: Any,
    *,
    mode: str,
    platform_code: str,
) -> dict[str, Any]:
    """§7.3 八种情形（调用异常一行由调用方处理）：返回 ``{status, match_mode, confidence, citation, error_category,
    error_message, parsed, notes, tool, claims}``。"""
    notes: dict[str, Any] = {}
    out: dict[str, Any] = {"match_mode": "none", "confidence": None, "citation": None, "error_category": None,
                           "error_message": None, "parsed": None, "notes": notes, "tool": [], "claims": []}
    if len((answer or "").strip()) < MIN_ANSWER_CHARS:
        out.update(status="unknown", error_category=ErrorCategory.INVALID_RESPONSE.value, error_message="回答为空")
        return out
    tool = [c for c in citations if c.source in TOOL_EVIDENCE_SOURCES]
    is_json, indexed, parsed, json_claims = _json_answer(answer)
    text_claims = zhiqi_text.extract_citations(answer, {})            # 正文中的 Markdown 链接 / 裸 URL（自述线索）
    claims = unique_citations([*json_claims, *text_claims])
    out.update(parsed=parsed, tool=tool, claims=claims)

    tool_mode, tool_hit, _ = match_citations(tool, link, mode, platform_code, notes=notes)
    if tool_hit is not None and tool_mode in ("url", "domain"):
        table = CONF_TOOL_HIT_JSON if (is_json and indexed) else CONF_TOOL_HIT_OTHER
        out.update(status="indexed", match_mode=tool_mode, confidence=table[tool_mode], citation=tool_hit)
        return out
    claim_notes: dict[str, Any] = {}
    _claim_mode, claim_hit, _ = match_citations(claims, link, mode, platform_code, notes=claim_notes)
    if claim_notes.get("domain_match_ignored"):
        notes["domain_match_ignored"] = True
    if claim_hit is not None or (is_json and indexed):
        out.update(status="unknown", confidence=CONF_UNVERIFIED, error_message=MSG_EVIDENCE_UNVERIFIED, citation=claim_hit)
        return out
    if not tool:
        out.update(status="unknown", error_message=MSG_NO_SEARCH_EVIDENCE)
        return out
    out.update(status="not_indexed", confidence=CONF_NOT_INDEXED_JSON if is_json else CONF_NOT_INDEXED_OTHER)
    return out


class ZhiqiWebSearchProvider:
    code = PROVIDER

    def available(self) -> tuple[bool, str | None]:
        """经 zhiqiapi 网关调用（Mock 模式由 MockZhiqiClient 应答），不依赖额外凭据。"""
        return True, None

    def check(self, db: Session, link: PublishLink, engine: str, query_by: list[str], *, ctx: CheckContext) -> CheckResult:
        started = time.monotonic()
        provider_cfg = dict(ctx.provider_config or {})
        engine_cfg = dict(ctx.engine_config or {})
        query_text: str | None = None
        evidence: dict[str, Any] = {"source": PROVIDER, "engine": engine, "engine_name": ctx.engine_name}
        try:
            if ctx.root_task is None:
                raise RuntimeError("zhiqi_web_search 需要预先创建的根任务")
            template = pts.resolve_template_for_code(
                db, KIND, ctx.project_id, ctx.language, str(provider_cfg.get("prompt_template_code") or DEFAULT_TEMPLATE_CODE)
            )
            system, user = pts.render(template, build_variables(ctx, list(query_by or [])))
            query_text = user
            evidence["query_text"] = user
            timeout = provider_cfg.get("timeout_seconds")
            result, attempt = gateway.complete_text(
                db,
                root_task=ctx.root_task,
                messages=pts.messages_for(system, user),
                params=pts.call_params(template),
                response_format="json",
                extra=dict(provider_cfg.get("extra") or {}),
                model_override=(str(engine_cfg.get("model") or "").strip() or None),
                protocol_override=provider_cfg.get("protocol") or None,
                timeout_override=float(timeout) if timeout else None,
                metadata=pts.call_metadata(template, OPERATION),
            )
        except Exception as exc:  # noqa: BLE001 - 提供器内部不得抛出未分类异常（§7.1）
            return failure_result(PROVIDER, exc, query_text=query_text, evidence=evidence,
                                  duration_ms=int((time.monotonic() - started) * 1000))

        answer = result.text or ""
        mode = str(provider_cfg.get("match_mode") or "url_or_domain")
        verdict = judge_answer(answer, list(result.citations or []), link, mode=mode, platform_code=ctx.platform_code)
        citation: Citation | None = verdict["citation"]
        if is_mock_response(result.raw):
            evidence["source"] = "mock"
        evidence.update({
            "parsed": verdict["parsed"],
            "citations": [citation_dict(c) for c in verdict["tool"]],
            "claims": [citation_dict(c) for c in verdict["claims"]],
            "matched": matched_evidence(verdict["match_mode"], citation),
            "domain_match_ignored": bool(verdict["notes"].get("domain_match_ignored")),
            "answer_excerpt": excerpt(answer),
            "dropped": False,
        })
        status = verdict["status"]
        hit = citation if status == "indexed" else None
        return CheckResult(
            status=status,
            provider=PROVIDER,
            match_mode=verdict["match_mode"],
            query_text=query_text,
            evidence_title=hit.title if hit else None,
            evidence_snippet=(hit.snippet or sentence_with(answer, hit.url)) if hit else None,
            evidence_url=hit.url if hit else None,
            evidence=evidence,
            confidence=verdict["confidence"],
            ai_task_id=attempt.id,
            request_id=result.request_id,
            model=result.model or attempt.model,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_category=verdict["error_category"],
            error_message=verdict["error_message"],
        )


__all__ = ["PROVIDER", "ZhiqiWebSearchProvider", "build_variables", "judge_answer"]
