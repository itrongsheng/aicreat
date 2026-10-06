"""无密钥 Mock（docs/08-zhiqiapi-integration.md §5.12、§13；docs/10-media-generation.md §10）。

``ZHIQI_API_KEY`` 为空时 ``get_client()`` 返回 ``MockZhiqiClient``：按 ``path`` 分发到本模块的生成函数，不发 HTTP；
``request_id = "mock-" + uuid4 hex``；每次文本 / 图片 / 视频调用向 ``mock:usage_logs`` LPUSH 一条 ``type=2`` 伪日志
（LTRIM 0 999、EXPIRE 86400），对账、熔断、健康探测、额度等流程与真实模式完全相同。
图片 / 视频任务状态机存 ``mock:task:{task_id}``（Hash ``{kind, status, polls, urls}``，TTL 3600s）。
错误路径（media_storage、content_blocked、404、transfer_failed 等）不在这里随机模拟，由测试夹具 monkeypatch 覆盖。
"""

from __future__ import annotations

import json
import logging
import random
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import redis as redis_lib

from app.core.config import settings
from app.core.redis import redis_client
from app.core.zhiqi.client import ZhiqiClient, ZhiqiResponse
from app.core.zhiqi.errors import ZhiqiError, classify_error, error_message_from_body
from app.core.zhiqi.types import DownloadResult, ErrorCategory, Timeouts
from app.core.zhiqi.usage import estimate_tokens

logger = logging.getLogger("app.core.zhiqi")

MOCK_ASSETS_DIR = Path(__file__).resolve().parent / "mock_assets"
PLACEHOLDER_PNG = "placeholder.png"
PLACEHOLDER_MP4 = "placeholder.mp4"

USAGE_LOGS_KEY = "mock:usage_logs"
USAGE_LOGS_MAX = 1000
USAGE_LOGS_TTL = 86400
TASK_KEY_PREFIX = "mock:task:"
TASK_TTL = 3600

MOCK_TEXT_MODEL = "mock-text"
MOCK_IMAGE_MODEL = "mock-image"
MOCK_VIDEO_MODEL = "mock-video"

# 模拟延迟（毫秒）：文本 200~800、提交 100~300；SIMULATE_LATENCY=False 时不 sleep（测试用），但 latency_ms 照常非 0
SIMULATE_LATENCY = True
TEXT_LATENCY_MS = (200, 800)
SUBMIT_LATENCY_MS = (100, 300)
FAST_LATENCY_MS = (5, 30)
# geo_query / seo_query「命中」分支概率（§13.2）
HIT_PROBABILITY = 0.7
MOCK_SOURCE_BASE = "https://mock-source.invalid/ref/"

IMAGE_RESOLUTIONS = ("1080p", "2k", "4k")
IMAGE_ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16")
IMAGE_FORBIDDEN_FIELDS = ("size", "quality", "ratio")
KEYWORD_INTENTS = ("informational", "navigational", "transactional", "commercial", "unknown")
KEYWORD_TYPES = ("core", "long_tail", "question")
IMAGE_PROMPT_TEXT = (
    "Flat-style editorial illustration of the article topic, clean composition, soft natural light, "
    "muted color palette, high detail, no text, no watermark"
)

_rng = random.Random()


class MockHTTPError(Exception):
    """Mock 生成函数内的上游错误（转为 ``ZhiqiError`` 时按真实规则 ``classify_error``）。"""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _error_body(message: str, code: str, type_: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "code": code}}


def _new_request_id() -> str:
    return "mock-" + uuid.uuid4().hex


def _redis() -> redis_lib.Redis:
    return redis_client


def placeholder_url(name: str) -> str:
    return f"{settings.public_base_url.strip().rstrip('/')}/media/mock/{name}"


# =====================================================================
# 文本
# =====================================================================


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else str(part.get("text", "")) for part in content if isinstance(part, (str, dict))
        )
    return str(content)


def _split_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    """三协议请求体 → ``(system 文本, 用户/其它消息文本)``。"""
    systems: list[str] = []
    users: list[str] = []
    if isinstance(payload.get("instructions"), str):
        systems.append(payload["instructions"])
    system = payload.get("system")
    if system is not None:
        systems.append(_text_of(system))
    messages = payload.get("messages")
    if messages is None:
        messages = payload.get("input")
    if isinstance(messages, str):
        users.append(messages)
    elif isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            text = _text_of(msg.get("content"))
            (systems if msg.get("role") == "system" else users).append(text)
    return "\n".join(s for s in systems if s), "\n".join(u for u in users if u)


_KIND_ALIASES = {
    "keyword": "keyword", "title": "title", "outline": "outline", "content": "content", "section": "section",
    "rewrite": "rewrite", "expand": "rewrite", "shorten": "rewrite", "restyle": "rewrite",
    "seo_meta": "seo_meta", "faq": "faq", "image_prompt": "image_prompt", "geo_query": "geo_query", "seo_query": "seo_query",
    "route_probe": "route_probe",
}
_OPERATION_KINDS = {
    "keyword_generate": "keyword", "title_generate": "title", "content_outline": "outline", "content_rewrite": "rewrite",
    "image_prompt": "image_prompt", "geo_check": "geo_query", "seo_check": "seo_query", "route_probe": "route_probe",
}
# (特征文案, kind)：按顺序首个命中（section 须在 content 之前；均取自 09/10/11 系统模板原文）
_PROMPT_FEATURES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("收录核查",), "seo_query"),
    (("正在向 AI 助手提问", "列出来源链接"), "geo_query"),
    (("关键词策划",), "keyword"),
    (("标题策划",), "title"),
    (("文章结构策划",), "outline"),
    (("逐节撰写", "当前小节"), "section"),
    (("SEO 编辑", "seo_description"), "seo_meta"),
    (("条 FAQ", "元素字段：q"), "faq"),
    (("图片提示词", "image prompt", "英文提示词"), "image_prompt"),
    (("资深编辑", "待处理文本"), "rewrite"),
    (("专业内容写作者", "完整正文"), "content"),
)
_CAPABILITY_KINDS = {"keyword": "keyword", "title": "title", "rewrite": "rewrite", "content": "content"}


def detect_template(payload: dict[str, Any], metadata: dict[str, Any] | None) -> str:
    """识别模板种类：``metadata`` 显式种类（``template_kind`` / ``kind`` / ``template_code`` / ``operation``）→
    ``capability`` 为 geo_check / seo_check → 提示词特征 → ``capability`` 兜底 → ``other``。"""
    meta = metadata or {}
    for key in ("template_kind", "prompt_kind", "kind"):
        value = str(meta.get(key) or "").strip().lower()
        if value in _KIND_ALIASES:
            return _KIND_ALIASES[value]
    code = str(meta.get("template_code") or "").strip().lower()
    if code.startswith("sys_") and code[4:] in _KIND_ALIASES:
        return _KIND_ALIASES[code[4:]]
    operation = str(meta.get("operation") or "").strip().lower()
    if operation in _OPERATION_KINDS:
        return _OPERATION_KINDS[operation]
    capability = str(meta.get("capability") or "").strip().lower()
    if capability == "geo_check":
        return "geo_query"
    if capability == "seo_check":
        return "seo_query"
    system, user = _split_prompt(payload)
    if user.strip().lower() == "ping" and not system:
        return "route_probe"
    haystack = f"{system}\n{user}"
    for features, kind in _PROMPT_FEATURES:
        if any(feature in haystack for feature in features):
            return kind
    return _CAPABILITY_KINDS.get(capability, "other")


def _data_tag(text: str, label: str) -> str | None:
    """取 ``{label}：<data>…</data>`` 中的值。"""
    match = re.search(re.escape(label) + r"\s*[:：]\s*<data>\s*(.*?)\s*</data>", text, re.DOTALL)
    if match:
        value = match.group(1).strip()
        return value if value and value != "（未提供）" else None
    return None


def _int_after(text: str, patterns: tuple[str, ...], default: int) -> int:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return default


_FILLER = (
    "围绕{kw}，首先要明确目标读者的核心诉求，再结合真实场景给出可执行的方法。",
    "在实际操作中，{kw}的关键在于持续记录数据、复盘结果并逐步优化细节。",
    "很多团队在{kw}上容易忽视基础准备，建议先梳理流程，再分阶段推进。",
    "对比不同方案时，可以从成本、效率、风险与可维护性四个维度评估{kw}。",
    "结合行业经验，{kw}最好以小步快跑的方式验证，避免一次性投入过大。",
)


def _paragraph(keyword: str, seed: int, length: int) -> str:
    out: list[str] = []
    i = seed
    while sum(len(s) for s in out) < length:
        out.append(_FILLER[i % len(_FILLER)].format(kw=keyword))
        i += 1
    return "".join(out)


def _gen_keywords(text: str) -> str:
    seeds_raw = _data_tag(text, "种子词") or "示例关键词"
    seeds = [s.strip() for s in re.split(r"[、,，;；\n]", seeds_raw) if s.strip()] or ["示例关键词"]
    items = []
    for i in range(20):
        seed = seeds[i % len(seeds)][:30]
        items.append(
            {
                "keyword": f"{seed}{('教程', '推荐', '怎么选', '价格', '对比')[i % 5]}{i + 1}",
                "intent": KEYWORD_INTENTS[i % len(KEYWORD_INTENTS)],
                "keyword_type": KEYWORD_TYPES[i % len(KEYWORD_TYPES)],
                "difficulty": 20 + (i * 7) % 70,
                "heat": 30 + (i * 11) % 65,
                "reason": f"Mock：由种子词「{seed}」扩展的候选关键词",
            }
        )
    return json.dumps(items, ensure_ascii=False)


_TITLE_PATTERNS = (
    "{kw}完全指南：从入门到精通",
    "{kw}怎么做？10 个实用技巧",
    "2026 年{kw}趋势与实践",
    "新手必看的{kw}避坑清单",
    "{kw}实战案例拆解",
    "一文读懂{kw}的核心要点",
    "{kw}常见问题与解决方案",
    "高效{kw}的 5 个步骤",
    "{kw}工具与方法对比",
    "为什么{kw}值得投入",
)


def _gen_titles(text: str) -> str:
    keyword = (_data_tag(text, "关键词") or "示例主题")[:30]
    count = max(1, min(20, _int_after(text, (r"生成\s*(\d+)\s*个标题", r"生成\s*(\d+)\s*个"), 5)))
    items = []
    for i in range(count):
        title = _TITLE_PATTERNS[i % len(_TITLE_PATTERNS)].format(kw=keyword)
        if i >= len(_TITLE_PATTERNS):
            title = f"{title}（{i + 1}）"
        items.append({"title": title, "ai_score": round(6.0 + (i % 4) * 0.9, 1)})
    return json.dumps(items, ensure_ascii=False)


def _gen_outline(text: str) -> str:
    keyword = (_data_tag(text, "主关键词") or _data_tag(text, "文章标题") or "示例主题")[:30]
    max_sections = _int_after(text, (r"不超过\s*(\d+)\s*个",), 5)
    count = max(4, min(6, max_sections))
    middle = ("核心概念与背景", "具体方法与步骤", "常见误区与对策", "工具与资源推荐")
    items = [{"heading": "引言", "level": 2, "points": [f"为什么关注{keyword}", "本文能解决的问题"]}]
    for i in range(count - 2):
        items.append({"heading": f"{keyword}的{middle[i % len(middle)]}", "level": 2, "points": ["要点一", "要点二", "要点三"]})
    items.append({"heading": "总结与行动建议", "level": 2, "points": ["回顾关键结论", "下一步行动"]})
    return json.dumps(items, ensure_ascii=False)


def _is_html(text: str) -> bool:
    # 只看「输出格式：html」这类参数行；系统模板说明里的「format=html 时……」不算
    return bool(re.search(r"(输出格式|format)\s*[:：]\s*html", text, re.IGNORECASE))


def _gen_content(text: str) -> str:
    keyword = (_data_tag(text, "主关键词") or _data_tag(text, "文章标题") or "示例主题")[:30]
    target = max(300, min(20000, _int_after(text, (r"约\s*(\d+)\s*字", r"目标字数[:：]\s*(\d+)"), 1500)))
    headings = [m.strip() for m in re.findall(r"^##\s+(.+)$", text, re.MULTILINE)][:6]
    if len(headings) < 3:
        headings = [f"{keyword}的核心概念", f"{keyword}的实践方法", f"{keyword}的常见误区"]
    per = max(80, target // (len(headings) + 1))
    html = _is_html(text)
    parts: list[str] = []
    for i, heading in enumerate(headings):
        body = _paragraph(keyword, i, per)
        parts.append(f"<h2>{heading}</h2>\n<p>{body}</p>" if html else f"## {heading}\n\n{body}")
    faq = f"{keyword}适合哪些人？适合希望系统提升相关能力的读者，可从基础步骤开始实践。"
    parts.append(f"<h2>常见问题</h2>\n<p>{faq}</p>" if html else f"## 常见问题\n\n**{keyword}适合哪些人？** {faq}")
    return "\n\n".join(parts)


def _gen_section(text: str) -> str:
    keyword = (_data_tag(text, "主关键词") or "示例主题")[:30]
    target = max(150, min(8000, _int_after(text, (r"本节约\s*(\d+)\s*字", r"约\s*(\d+)\s*字"), 500)))
    section = _data_tag(text, "当前小节") or ""
    match = re.search(r"^##\s+(.+)$", section, re.MULTILINE)
    heading = match.group(1).strip() if match else f"{keyword}要点"
    body = _paragraph(keyword, len(heading), target)
    if _is_html(text):
        return f"<h2>{heading}</h2>\n<p>{body}</p>"
    return f"## {heading}\n\n{body}"


def _gen_rewrite(text: str) -> str:
    blocks = [b.strip() for b in re.findall(r"<text>\s*(.*?)\s*</text>", text, re.DOTALL) if b.strip() not in ("", "…")]
    original = blocks[-1] if blocks else text.strip()[:2000]
    if not original:
        original = "（Mock 改写内容）"
    if original.lstrip().startswith(("#", "<")):
        return f"{original}\n\n（Mock 改写）"
    return f"（Mock 改写）{original}\n\n（Mock 改写）"


def _gen_seo_meta(text: str) -> str:
    keyword = (_data_tag(text, "主关键词") or "示例主题")[:20]
    title = (_data_tag(text, "文章标题") or keyword)[:40]
    meta = {
        "summary": f"本文围绕{keyword}展开，介绍核心概念、实践方法与常见误区，帮助读者快速上手并避免常见问题。",
        "seo_title": f"{keyword}：{title}"[:60],
        "seo_description": (
            f"想了解{keyword}？本文系统梳理{keyword}的核心概念、实践步骤、常见误区与工具推荐，"
            f"结合真实场景给出可执行的方法与检查清单，适合新手入门与团队实践参考，帮助你更高效地完成相关工作并持续优化效果。"
        )[:160],
        "seo_keywords": [keyword, f"{keyword}教程", f"{keyword}方法", f"{keyword}技巧"],
    }
    return json.dumps(meta, ensure_ascii=False)


def _gen_faq(text: str) -> str:
    keyword = (_data_tag(text, "主关键词") or "示例主题")[:20]
    count = max(1, min(20, _int_after(text, (r"生成\s*(\d+)\s*条", r"(\d+)\s*条\s*FAQ"), 3)))
    questions = ("是什么", "适合哪些人", "如何开始", "需要多长时间", "有哪些常见误区", "如何衡量效果")
    items = []
    for i in range(count):
        q = f"{keyword}{questions[i % len(questions)]}？" + (f"（{i + 1}）" if i >= len(questions) else "")
        a = (
            f"关于{keyword}{questions[i % len(questions)]}，正文给出的建议是先明确目标与现状，再按步骤实施并持续复盘。"
            f"实践中应结合自身资源选择合适的方法，避免一次投入过大，逐步验证效果后再扩大规模。"
        )
        items.append({"q": q, "a": a})
    return json.dumps(items, ensure_ascii=False)


def _gen_index_answer(kind: str, metadata: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """geo_query / seo_query：``metadata.target_url`` 存在时 70% 命中（引用目标 URL），否则 / 其余按「未收录」分支只引用
    ``https://mock-source.invalid/ref/<hex>``；两个分支都带引用（Mock 引用视同工具证据）。"""
    target = metadata.get("target_url")
    target = target.strip() if isinstance(target, str) and target.strip() else None
    hit = bool(target) and _rng.random() < HIT_PROBABILITY
    if hit:
        url, title, snippet = target, "目标文章", "Mock 检索命中目标页面"
    else:
        url, title, snippet = f"{MOCK_SOURCE_BASE}{uuid.uuid4().hex[:12]}", "Mock 参考资料", "Mock 检索结果（非目标页面）"
    citations = [{"url": url, "title": title, "snippet": snippet}]
    if kind == "seo_query":
        answer = json.dumps({"indexed": hit, "evidence": [{"url": url, "title": title, "snippet": snippet}]}, ensure_ascii=False)
    else:
        answer = f"以下是值得参考的资料：\n\n1. [{title}]({url})：{snippet}。"
    return answer, citations


def generate_text(payload: dict[str, Any], metadata: dict[str, Any] | None) -> tuple[str, list[dict[str, str]], str]:
    """共用生成器：返回 ``(回答文本, 引用列表, 模板种类)``。"""
    metadata = metadata or {}
    kind = detect_template(payload, metadata)
    system, user = _split_prompt(payload)
    text = f"{system}\n{user}"
    citations: list[dict[str, str]] = []
    if kind == "keyword":
        answer = _gen_keywords(text)
    elif kind == "title":
        answer = _gen_titles(text)
    elif kind == "outline":
        answer = _gen_outline(text)
    elif kind == "content":
        answer = _gen_content(text)
    elif kind == "section":
        answer = _gen_section(text)
    elif kind == "rewrite":
        answer = _gen_rewrite(text)
    elif kind == "seo_meta":
        answer = _gen_seo_meta(text)
    elif kind == "faq":
        answer = _gen_faq(text)
    elif kind == "image_prompt":
        answer = IMAGE_PROMPT_TEXT
    elif kind in ("geo_query", "seo_query"):
        answer, citations = _gen_index_answer(kind, metadata)
    elif kind == "route_probe":
        answer = "pong"
    else:
        answer = user[:200] or "mock"
    return answer, citations, kind


def _usage_tokens(payload: dict[str, Any], answer: str) -> tuple[int, int]:
    system, user = _split_prompt(payload)
    return max(1, estimate_tokens(f"{system}\n{user}".strip())), max(1, estimate_tokens(answer))


def mock_chat(payload: dict, metadata: dict) -> dict:
    """``POST /v1/chat/completions`` 的 Mock 响应（``choices[0].message.content`` + ``annotations`` + ``usage``）。"""
    answer, citations, _kind = generate_text(payload, metadata)
    prompt_tokens, completion_tokens = _usage_tokens(payload, answer)
    message: dict[str, Any] = {"role": "assistant", "content": answer}
    if citations:
        message["annotations"] = [{"type": "url_citation", "url_citation": dict(c)} for c in citations]
    return {
        "id": f"chatcmpl-mock-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model") or MOCK_TEXT_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
        "mock": True,
    }


def mock_responses(payload: dict, metadata: dict) -> dict:
    """``POST /v1/responses`` 的 Mock 响应（``output[].content[]`` 的 ``output_text`` + ``annotations``）。"""
    answer, citations, _kind = generate_text(payload, metadata)
    prompt_tokens, completion_tokens = _usage_tokens(payload, answer)
    part: dict[str, Any] = {"type": "output_text", "text": answer, "annotations": [{"type": "url_citation", **c} for c in citations]}
    return {
        "id": f"resp_mock_{uuid.uuid4().hex[:16]}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": payload.get("model") or MOCK_TEXT_MODEL,
        "output": [{"type": "message", "id": f"msg_mock_{uuid.uuid4().hex[:12]}", "role": "assistant", "content": [part]}],
        "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
        "mock": True,
    }


def mock_messages(payload: dict, metadata: dict) -> dict:
    """``POST /v1/messages`` 的 Mock 响应（``content[]`` 的 ``text`` + ``citations``、``stop_reason``）。"""
    answer, citations, _kind = generate_text(payload, metadata)
    prompt_tokens, completion_tokens = _usage_tokens(payload, answer)
    block: dict[str, Any] = {"type": "text", "text": answer}
    if citations:
        block["citations"] = [
            {"type": "web_search_result_location", "url": c["url"], "title": c["title"], "cited_text": c["snippet"]} for c in citations
        ]
    return {
        "id": f"msg_mock_{uuid.uuid4().hex[:16]}",
        "type": "message",
        "role": "assistant",
        "model": payload.get("model") or MOCK_TEXT_MODEL,
        "content": [block],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
        "mock": True,
    }


# =====================================================================
# 图片 / 视频状态机
# =====================================================================


def _create_task(task_id: str, kind: str, model: str, url: str) -> None:
    key = TASK_KEY_PREFIX + task_id
    pipe = _redis().pipeline()
    pipe.hset(key, mapping={"kind": kind, "status": "queued", "polls": 0, "urls": json.dumps([url]), "model": model, "created_at": int(time.time())})
    pipe.expire(key, TASK_TTL)
    pipe.execute()


def _advance(task_id: str, kind: str) -> tuple[int, dict[str, str]]:
    key = TASK_KEY_PREFIX + task_id
    r = _redis()
    if not r.exists(key):
        raise MockHTTPError(404, _error_body(f"task {task_id} not found", "task_not_found"))
    data = r.hgetall(key) or {}
    if data.get("kind") != kind:
        raise MockHTTPError(404, _error_body(f"task {task_id} not found", "task_not_found"))
    polls = int(r.hincrby(key, "polls", 1))
    return polls, data


def _validate_image_payload(payload: dict[str, Any]) -> None:
    for name in IMAGE_FORBIDDEN_FIELDS:
        if name in payload:
            raise MockHTTPError(400, _error_body(f"unsupported_parameter: {name}", "unsupported_parameter"))
    if not str(payload.get("model") or "").strip() or not str(payload.get("prompt") or "").strip():
        raise MockHTTPError(400, _error_body("invalid parameter: model and prompt are required", "invalid_parameter"))
    if int(payload.get("n", 1) or 1) != 1:
        raise MockHTTPError(400, _error_body("unsupported_parameter: n must be 1", "unsupported_parameter"))
    if payload.get("resolution", "1080p") not in IMAGE_RESOLUTIONS:
        raise MockHTTPError(400, _error_body("unsupported_parameter: resolution", "unsupported_parameter"))
    if payload.get("aspect_ratio", "16:9") not in IMAGE_ASPECT_RATIOS:
        raise MockHTTPError(400, _error_body("unsupported_parameter: aspect_ratio", "unsupported_parameter"))
    refs = payload.get("reference_image_urls") or []
    if not isinstance(refs, list) or len(refs) > 9:
        raise MockHTTPError(400, _error_body("unsupported_parameter: reference_image_urls", "unsupported_parameter"))


def mock_image_async(payload: dict) -> tuple[int, dict]:
    """``202 {id:"task_mock_<hex>", status:"queued"}``；契约外字段 / 枚举不符 → 400 ``unsupported_parameter``。"""
    _validate_image_payload(payload)
    task_id = f"task_mock_{uuid.uuid4().hex[:16]}"
    _create_task(task_id, "image", str(payload.get("model")), placeholder_url(PLACEHOLDER_PNG))
    return 202, {"id": task_id, "status": "queued"}


def mock_image_status(task_id: str) -> dict:
    """每次 GET ``polls += 1``：1 次 queued、2 次 in_progress（progress=50）、3 次起 succeeded（``data[0].url`` 为占位图）。"""
    polls, data = _advance(task_id, "image")
    if polls <= 1:
        status, progress = "queued", 0
    elif polls == 2:
        status, progress = "in_progress", 50
    else:
        status, progress = "succeeded", 100
    _redis().hset(TASK_KEY_PREFIX + task_id, "status", status)
    body: dict[str, Any] = {"id": task_id, "object": "image.generation.task", "status": status, "progress": progress}
    if status == "succeeded":
        body["data"] = [{"url": u} for u in json.loads(data.get("urls") or "[]")]
    return body


def mock_video_submit(payload: dict) -> dict:
    """``{id:"vidtask_mock_<hex>", status:"queued"}``（multipart 在 ``request`` 内返回 415）。"""
    if not str(payload.get("model") or "").strip() or not str(payload.get("prompt") or "").strip():
        raise MockHTTPError(400, _error_body("invalid parameter: model and prompt are required", "invalid_parameter"))
    task_id = f"vidtask_mock_{uuid.uuid4().hex[:16]}"
    _create_task(task_id, "video", str(payload.get("model")), placeholder_url(PLACEHOLDER_MP4))
    return {"id": task_id, "object": "video", "status": "queued", "progress": 0}


_VIDEO_PROGRESS = {2: 25, 3: 60, 4: 99}


def mock_video_status(task_id: str) -> dict:
    """1 次 queued、2~4 次 in_progress（progress 25/60/99）、5 次起 succeeded（``data[].url`` 为占位视频）。"""
    polls, data = _advance(task_id, "video")
    if polls <= 1:
        status, progress = "queued", 0
    elif polls <= 4:
        status, progress = "in_progress", _VIDEO_PROGRESS[polls]
    else:
        status, progress = "succeeded", 100
    _redis().hset(TASK_KEY_PREFIX + task_id, "status", status)
    body: dict[str, Any] = {"id": task_id, "object": "video", "status": status, "progress": progress}
    if status == "succeeded":
        body["data"] = [{"url": u} for u in json.loads(data.get("urls") or "[]")]
        body["expires_at"] = int(time.time()) + TASK_TTL
    return body


# =====================================================================
# 目录 / 价格 / 用量
# =====================================================================

MOCK_MODELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MOCK_TEXT_MODEL, ("openai", "openai-response", "anthropic")),
    (MOCK_IMAGE_MODEL, ("image-generation", "image-generation-async", "image-edit")),
    (MOCK_VIDEO_MODEL, ("openai-video",)),
)


def mock_models() -> dict:
    return {
        "object": "list",
        "success": True,
        "data": [
            {"id": model_id, "object": "model", "created": 0, "owned_by": "mock", "supported_endpoint_types": list(types)}
            for model_id, types in MOCK_MODELS
        ],
    }


def mock_pricing() -> dict:
    descriptions = {MOCK_TEXT_MODEL: "Mock 文本模型", MOCK_IMAGE_MODEL: "Mock 图片模型", MOCK_VIDEO_MODEL: "Mock 视频模型"}
    data = []
    for index, (model_id, types) in enumerate(MOCK_MODELS):
        data.append(
            {
                "model_name": model_id, "description": descriptions[model_id], "cover_url": "", "tags": "mock",
                "vendor_id": 1, "sort_order": index, "quota_type": 0, "model_ratio": 1, "model_price": 0,
                "completion_ratio": 1, "cache_ratio": 1, "create_cache_ratio": 1, "enable_groups": ["default"],
                "supported_endpoint_types": list(types), "billing_mode": "", "billing_expr": "", "icon": "", "model_price_type": "",
            }
        )
    return {
        "success": True,
        "data": data,
        "vendors": [{"id": 1, "name": "mock", "icon": "", "description": "Mock 供应商"}],
        "model_parameter_capabilities": {},
    }


def push_usage_log(
    *, request_id: str, model: str, prompt_tokens: int, completion_tokens: int, request_path: str, task_id: str | None = None
) -> None:
    """向 ``mock:usage_logs`` LPUSH 一条 ``type=2`` 伪日志（quota = 本地估算值：mock 价格 model_ratio=1、completion_ratio=1）。"""
    quota = max(1, int(prompt_tokens) + int(completion_tokens))
    entry = {
        "id": uuid.uuid4().int >> 76,
        "request_id": request_id,
        "type": 2,
        "model_name": model,
        "group": "default",
        "token_name": "mock",
        "quota": quota,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "other": {"group_ratio": 1, "model_ratio": 1, "completion_ratio": 1, "cache_tokens": 0, "request_path": request_path},
        "created_at": int(time.time()),
    }
    if task_id:
        entry["task_id"] = task_id
    try:
        pipe = _redis().pipeline()
        pipe.lpush(USAGE_LOGS_KEY, json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        pipe.ltrim(USAGE_LOGS_KEY, 0, USAGE_LOGS_MAX - 1)
        pipe.expire(USAGE_LOGS_KEY, USAGE_LOGS_TTL)
        pipe.execute()
    except redis_lib.RedisError as exc:
        logger.warning("Mock 用量日志写入失败: %s", exc)


def mock_token_logs() -> dict:
    """``LRANGE mock:usage_logs 0 999``（新在前）组装成 ``/api/log/token`` 响应。"""
    items = []
    for raw in _redis().lrange(USAGE_LOGS_KEY, 0, USAGE_LOGS_MAX - 1):
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            items.append(item)
    return {"success": True, "message": "", "data": items}


# =====================================================================
# 客户端
# =====================================================================

_IMAGE_STATUS_RE = re.compile(r"^/v1/images/generations/([^/]+)$")
_VIDEO_STATUS_RE = re.compile(r"^/v1/videos/([^/]+)$")
_VIDEO_CONTENT_RE = re.compile(r"^/v1/videos/([^/]+)/content$")


class MockZhiqiClient(ZhiqiClient):
    is_mock = True

    def __init__(
        self,
        *,
        base_url: str = "https://zhiqiapi.com/v1",
        api_key: str = "",
        timeouts: Timeouts | None = None,
        user_agent: str = "aicreat/0.1",
        simulate_latency: bool | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url=base_url, api_key="", timeouts=timeouts or Timeouts(), user_agent=user_agent, **kwargs)
        self._simulate_latency = simulate_latency

    # ------------------------------------------------------------ 内部

    def _latency(self, bounds: tuple[int, int]) -> int:
        latency_ms = _rng.randint(*bounds)
        simulate = SIMULATE_LATENCY if self._simulate_latency is None else self._simulate_latency
        if simulate:
            time.sleep(latency_ms / 1000)
        return latency_ms

    def _relative(self, path: str, absolute: bool) -> str:
        if absolute or "://" in path:
            if not self.is_origin_url(path):
                return path  # 未知路径 → 404
            path = urlsplit(path).path
            prefix = urlsplit(self.origin).path.rstrip("/")
            if prefix and path.startswith(prefix):
                path = path[len(prefix):]
        return urlsplit(path).path.rstrip("/") or "/"

    def request(self, method: str, path: str, **kw: Any) -> ZhiqiResponse:
        """按 ``path`` 分发到生成函数；未知路径 → 404（``route_missing``）。"""
        method = method.upper()
        rel = self._relative(path, bool(kw.get("absolute")))
        request_id = _new_request_id()
        payload = kw.get("json") if isinstance(kw.get("json"), dict) else None
        metadata = kw.get("metadata") or {}
        try:
            status, body, latency_ms = self._dispatch(method, rel, payload, metadata, kw, request_id)
        except MockHTTPError as exc:
            status, body, latency_ms = exc.status, exc.body, self._latency(FAST_LATENCY_MS)
        except redis_lib.RedisError as exc:
            raise ZhiqiError(
                ErrorCategory.UPSTREAM_UNAVAILABLE, f"Mock 状态存储不可用：{type(exc).__name__}", request_id=request_id
            ) from exc
        logger.info("zhiqi(mock) %s %s http_status=%s latency_ms=%s request_id=%s", method, rel, status, latency_ms, request_id)
        if not 200 <= status < 300:
            category, pre_submit = classify_error(status, body)
            err = ZhiqiError(
                category, error_message_from_body(body, fallback=f"HTTP {status}"), http_status=status, request_id=request_id,
                pre_submit=pre_submit, raw=body,
            )
            err.retry_request_ids = [request_id]
            raise err
        return ZhiqiResponse(
            http_status=status, json=body, text=json.dumps(body, ensure_ascii=False),
            headers={"content-type": "application/json", "x-oneapi-request-id": request_id},
            request_id=request_id, latency_ms=latency_ms,
        )

    def _dispatch(
        self, method: str, rel: str, payload: dict[str, Any] | None, metadata: dict[str, Any], kw: dict[str, Any], request_id: str
    ) -> tuple[int, Any, int]:
        multipart = bool(kw.get("multipart") or kw.get("files"))
        if method == "POST" and rel in ("/v1/chat/completions", "/v1/responses", "/v1/messages"):
            if payload is None:
                raise MockHTTPError(400, _error_body("invalid parameter: JSON body required", "invalid_parameter"))
            generator = {"/v1/chat/completions": mock_chat, "/v1/responses": mock_responses, "/v1/messages": mock_messages}[rel]
            body = generator(payload, metadata)
            usage = body.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            completion_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            push_usage_log(
                request_id=request_id, model=str(payload.get("model") or MOCK_TEXT_MODEL),
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, request_path=rel,
            )
            return 200, body, self._latency(TEXT_LATENCY_MS)
        if method == "POST" and rel == "/v1/images/generations/async":
            if payload is None:
                raise MockHTTPError(400, _error_body("invalid parameter: JSON body required", "invalid_parameter"))
            status, body = mock_image_async(payload)
            push_usage_log(
                request_id=request_id, model=str(payload.get("model")), prompt_tokens=estimate_tokens(str(payload.get("prompt") or "")),
                completion_tokens=0, request_path=rel,
            )
            return status, body, self._latency(SUBMIT_LATENCY_MS)
        if method == "POST" and rel in ("/v1/images/generations", "/v1/images/edits"):
            if rel == "/v1/images/edits":
                form = kw.get("data")
                items = list(form.items()) if isinstance(form, dict) else list(form or [])
                fields: dict[str, Any] = {}
                images = [str(v) for k, v in items if k == "image"]
                for k, v in items:
                    if k != "image":
                        fields[k] = v
                if not images or len(images) > 9:
                    raise MockHTTPError(400, _error_body("invalid parameter: image must be 1~9 public URLs", "invalid_parameter"))
                model, prompt = str(fields.get("model") or ""), str(fields.get("prompt") or "")
            else:
                if payload is None:
                    raise MockHTTPError(400, _error_body("invalid parameter: JSON body required", "invalid_parameter"))
                _validate_image_payload(payload)
                model, prompt = str(payload.get("model") or ""), str(payload.get("prompt") or "")
            if not model or not prompt:
                raise MockHTTPError(400, _error_body("invalid parameter: model and prompt are required", "invalid_parameter"))
            push_usage_log(request_id=request_id, model=model, prompt_tokens=estimate_tokens(prompt), completion_tokens=0, request_path=rel)
            return 200, {"created": int(time.time()), "data": [{"url": placeholder_url(PLACEHOLDER_PNG)}]}, self._latency(SUBMIT_LATENCY_MS)
        if method == "POST" and rel == "/v1/videos":
            if multipart or payload is None:
                raise MockHTTPError(415, _error_body("unsupported media type: /v1/videos requires application/json", "unsupported_media_type"))
            body = mock_video_submit(payload)
            push_usage_log(
                request_id=request_id, model=str(payload.get("model")), prompt_tokens=estimate_tokens(str(payload.get("prompt") or "")),
                completion_tokens=0, request_path=rel,
            )
            return 200, body, self._latency(SUBMIT_LATENCY_MS)
        if method == "GET":
            if rel == "/v1/models":
                return 200, mock_models(), self._latency(FAST_LATENCY_MS)
            if rel == "/api/pricing_new":
                return 200, mock_pricing(), self._latency(FAST_LATENCY_MS)
            if rel == "/api/log/token":
                return 200, mock_token_logs(), self._latency(FAST_LATENCY_MS)
            match = _IMAGE_STATUS_RE.match(rel)
            if match and match.group(1) != "async":
                return 200, mock_image_status(match.group(1)), self._latency(FAST_LATENCY_MS)
            match = _VIDEO_STATUS_RE.match(rel)
            if match:
                return 200, mock_video_status(match.group(1)), self._latency(FAST_LATENCY_MS)
        raise MockHTTPError(404, _error_body(f"Invalid URL ({method} {rel})", "not_found"))

    def stream_download(
        self,
        url_or_path: str,
        dest: BinaryIO,
        *,
        max_bytes: int,
        timeout: float,
        source: str = "origin",
        allowed_content_types: tuple[str, ...] = ("image/", "video/", "application/octet-stream", "binary/octet-stream"),
    ) -> DownloadResult:
        """含 ``/media/mock/`` 的 URL 直接从 ``mock_assets/`` 复制文件；``/v1/videos/{id}/content`` 复制 ``placeholder.mp4``；
        不发 HTTP。返回 ``DownloadResult(source="mock", request_id="mock-…", http_status=200, request_ids=[同值])``。"""
        del timeout, source, allowed_content_types
        request_id = _new_request_id()
        path = urlsplit(url_or_path).path if "://" in url_or_path else url_or_path.split("?", 1)[0]
        name: str | None = None
        if "/media/mock/" in path:
            name = path.rsplit("/media/mock/", 1)[1]
        elif _VIDEO_CONTENT_RE.match(self._relative(url_or_path, "://" in url_or_path)):
            name = PLACEHOLDER_MP4
        if not name or name not in (PLACEHOLDER_PNG, PLACEHOLDER_MP4):
            err = ZhiqiError(ErrorCategory.TRANSFER_FAILED, f"Mock 模式无法下载：{path}", http_status=404, request_id=request_id)
            err.retry_request_ids = [request_id]
            raise err
        file_path = MOCK_ASSETS_DIR / name
        size = file_path.stat().st_size
        if size > max_bytes:
            err = ZhiqiError(ErrorCategory.TRANSFER_FAILED, f"文件超过大小上限 {max_bytes} 字节", http_status=200, request_id=request_id)
            err.retry_request_ids = [request_id]
            raise err
        with open(file_path, "rb") as fh:
            shutil.copyfileobj(fh, dest)
        logger.info("zhiqi(mock) GET %s http_status=200 latency_ms=0 request_id=%s", path, request_id)
        return DownloadResult(source="mock", request_id=request_id, http_status=200, request_ids=[request_id])
