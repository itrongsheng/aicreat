"""文本三协议（docs/08-zhiqiapi-integration.md §3.3、§5.5）。

``openai_chat`` → ``POST /v1/chat/completions``；``openai_responses`` → ``POST /v1/responses``；
``anthropic_messages`` → ``POST /v1/messages``（origin 拼接，头 ``anthropic-version: 2023-06-01``，未核实）。
首版固定非流式；``TextRequest.metadata`` 只经 ``client.post(metadata=…)`` 交给 Mock，不进入请求体。
"""

from __future__ import annotations

import json
import re
from typing import Any
from typing import Protocol as TypingProtocol

from app.core.zhiqi.client import ZhiqiClient, ZhiqiResponse
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import (
    Citation,
    ErrorCategory,
    Protocol,
    RetryPolicy,
    TextRequest,
    TextResult,
)

ENDPOINTS: dict[Protocol, str] = {
    Protocol.OPENAI_CHAT: "/v1/chat/completions",
    Protocol.OPENAI_RESPONSES: "/v1/responses",
    Protocol.ANTHROPIC_MESSAGES: "/v1/messages",
}
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_JSON_INSTRUCTION = "只输出 JSON，不要围栏。"
JSON_FORMAT = {"type": "json_object"}

# 参数降级重试时去掉的字段（unsupported_parameter，§9.2）
DEGRADE_DROP_FIELDS = ("response_format", "temperature", "top_p")


class TextProvider(TypingProtocol):
    def complete(self, client: ZhiqiClient, req: TextRequest, *, timeout: float, retry: RetryPolicy | None) -> TextResult: ...


# ---------------------------------------------------------------- 请求体


def _content_text(content: Any) -> str:
    """消息 content 可能是字符串或 parts 数组（``[{type:text,text}]``），统一转为字符串。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return str(content)


def _split_messages(messages: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    systems: list[str] = []
    others: list[dict[str, Any]] = []
    for msg in messages or []:
        role = str(msg.get("role", "user"))
        if role == "system":
            text = _content_text(msg.get("content"))
            if text:
                systems.append(text)
        else:
            others.append({"role": role, "content": msg.get("content", "")})
    return systems, others


def _filter_extra(req: TextRequest, passthrough: dict[str, list[str]] | None) -> dict[str, Any]:
    allowed = set((passthrough or {}).get(req.protocol.value, []) or [])
    return {k: v for k, v in (req.extra or {}).items() if k in allowed}


def build_payload(req: TextRequest, passthrough: dict[str, list[str]] | None, *, degraded: bool = False) -> dict[str, Any]:
    """协议对应请求体（§3.3 映射表）；``extra`` 按 ``passthrough[protocol]`` 白名单过滤后合并；
    ``degraded=True``：去掉 ``response_format`` / ``temperature`` / ``top_p``，``max_tokens`` → ``max_completion_tokens``（仅 chat）。"""
    protocol = Protocol(req.protocol)
    json_mode = (req.response_format or "text").lower() == "json"
    systems, others = _split_messages(req.messages)
    payload: dict[str, Any] = {"model": req.model}

    if protocol == Protocol.OPENAI_CHAT:
        messages = [{"role": "system", "content": s} for s in systems] + others
        payload["messages"] = messages
        payload["max_completion_tokens" if degraded else "max_tokens"] = int(req.max_tokens)
        if not degraded:
            payload["temperature"] = req.temperature
            if req.top_p is not None:
                payload["top_p"] = req.top_p
            if json_mode:
                payload["response_format"] = dict(JSON_FORMAT)
        if req.stop:
            payload["stop"] = list(req.stop)
        payload["stream"] = False
    elif protocol == Protocol.OPENAI_RESPONSES:
        if systems:
            payload["instructions"] = "\n\n".join(systems)
        payload["input"] = others
        payload["max_output_tokens"] = int(req.max_tokens)
        if not degraded:
            payload["temperature"] = req.temperature
            if req.top_p is not None:
                payload["top_p"] = req.top_p
            if json_mode:
                payload["text"] = {"format": dict(JSON_FORMAT)}
        # stop：Responses 无此字段，忽略
    elif protocol == Protocol.ANTHROPIC_MESSAGES:
        system_text = "\n\n".join(systems)
        if json_mode:
            system_text = f"{system_text}\n\n{ANTHROPIC_JSON_INSTRUCTION}" if system_text else ANTHROPIC_JSON_INSTRUCTION
        if system_text:
            payload["system"] = system_text
        payload["messages"] = [m for m in others if m["role"] in ("user", "assistant")]
        payload["max_tokens"] = int(req.max_tokens)
        if not degraded:
            payload["temperature"] = req.temperature
            if req.top_p is not None:
                payload["top_p"] = req.top_p
        if req.stop:
            payload["stop_sequences"] = list(req.stop)
    else:
        raise ValueError(f"不是文本协议：{protocol.value}")

    for key, value in _filter_extra(req, passthrough).items():
        if key in ("model", "messages", "input", "stream"):
            continue  # 核心字段不允许被透传覆盖
        if key == "text" and isinstance(value, dict) and isinstance(payload.get("text"), dict):
            payload["text"] = {**value, **payload["text"]}  # 透传的 text 选项与 JSON 模式 format 合并
        else:
            payload[key] = value
    return payload


def degraded_param_names(req: TextRequest, passthrough: dict[str, list[str]] | None = None) -> list[str]:
    """参数降级重试去掉 / 改名的字段（写 ``response_meta_json.degraded_params``）。"""
    normal = build_payload(req, passthrough)
    degraded = build_payload(req, passthrough, degraded=True)
    names = [k for k in normal if k not in degraded]
    if Protocol(req.protocol) == Protocol.OPENAI_RESPONSES and "text" in normal and normal.get("text") != degraded.get("text"):
        names = [n for n in names if n != "text"] + ["text.format"]
    return names


# ---------------------------------------------------------------- 调用


def complete(
    client: ZhiqiClient,
    req: TextRequest,
    *,
    timeout: float | None = None,
    retry: RetryPolicy | None = None,
    passthrough: dict[str, list[str]] | None = None,
    degraded: bool = False,
) -> TextResult:
    """按 ``req.protocol`` 分发（``degraded=True`` 为网关的参数降级重试）。"""
    protocol = Protocol(req.protocol)
    if protocol == Protocol.OPENAI_CHAT:
        return complete_openai_chat(client, req, timeout=timeout, retry=retry, passthrough=passthrough, degraded=degraded)
    if protocol == Protocol.OPENAI_RESPONSES:
        return complete_openai_responses(client, req, timeout=timeout, retry=retry, passthrough=passthrough, degraded=degraded)
    if protocol == Protocol.ANTHROPIC_MESSAGES:
        return complete_anthropic_messages(client, req, timeout=timeout, retry=retry, passthrough=passthrough, degraded=degraded)
    raise ValueError(f"不是文本协议：{protocol.value}")


def _call(
    client: ZhiqiClient, req: TextRequest, protocol: Protocol, *, timeout: float | None, retry: RetryPolicy | None,
    passthrough: dict[str, list[str]] | None, degraded: bool, headers: dict[str, str] | None = None,
) -> TextResult:
    payload = build_payload(req, passthrough, degraded=degraded)
    response = client.post(
        ENDPOINTS[protocol], json=payload, timeout=timeout, retry=retry, metadata=dict(req.metadata or {}), headers=headers
    )
    result = parse_result(protocol, response, req.model)
    # 客户端幂等重试历次 request_id（未重试时为 []），网关写 response_meta_json.retry_request_ids[]
    result.retry_request_ids = response.retry_request_ids  # type: ignore[attr-defined]
    return result


def complete_openai_chat(client, req, *, timeout=None, retry=None, passthrough=None, degraded=False) -> TextResult:
    """``POST /v1/chat/completions``。"""
    return _call(client, req, Protocol.OPENAI_CHAT, timeout=timeout, retry=retry, passthrough=passthrough, degraded=degraded)


def complete_openai_responses(client, req, *, timeout=None, retry=None, passthrough=None, degraded=False) -> TextResult:
    """``POST /v1/responses``。"""
    return _call(client, req, Protocol.OPENAI_RESPONSES, timeout=timeout, retry=retry, passthrough=passthrough, degraded=degraded)


def complete_anthropic_messages(client, req, *, timeout=None, retry=None, passthrough=None, degraded=False) -> TextResult:
    """``POST /v1/messages``（base 去掉尾部 ``/v1``，头 ``anthropic-version``）。"""
    return _call(
        client, req, Protocol.ANTHROPIC_MESSAGES, timeout=timeout, retry=retry, passthrough=passthrough, degraded=degraded,
        headers={"anthropic-version": ANTHROPIC_VERSION},
    )


# ---------------------------------------------------------------- 响应解析


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _invalid(message: str, response: ZhiqiResponse) -> ZhiqiError:
    return ZhiqiError(
        ErrorCategory.INVALID_RESPONSE, message, http_status=response.http_status, request_id=response.request_id,
        raw=response.json if isinstance(response.json, dict) else {},
    )


def parse_result(protocol: Protocol, response: ZhiqiResponse, model: str) -> TextResult:
    """按映射表取文本、``finish_reason``、tokens；``usage`` 缺失记 0 且 ``usage_missing=True``；结构不符抛 ``INVALID_RESPONSE``。"""
    raw = response.json
    if not isinstance(raw, dict):
        raise _invalid("文本响应不是 JSON 对象", response)
    protocol = Protocol(protocol)
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    prompt_tokens = completion_tokens = cache_tokens = 0

    if protocol == Protocol.OPENAI_CHAT:
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _invalid("响应缺少 choices[0]", response)
        message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
        text = _content_text(message.get("content"))
        finish_reason = choices[0].get("finish_reason")
        if usage is not None:
            prompt_tokens = _int(usage.get("prompt_tokens"))
            completion_tokens = _int(usage.get("completion_tokens"))
            details = usage.get("prompt_tokens_details")
            cache_tokens = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    elif protocol == Protocol.OPENAI_RESPONSES:
        output = raw.get("output")
        texts: list[str] = []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        elif isinstance(raw.get("output_text"), str):
            texts.append(raw["output_text"])
        else:
            raise _invalid("响应缺少 output[]", response)
        text = "".join(texts)
        finish_reason = raw.get("status")
        incomplete = raw.get("incomplete_details")
        if isinstance(incomplete, dict) and incomplete.get("reason"):
            finish_reason = incomplete.get("reason")
        if usage is not None:
            prompt_tokens = _int(usage.get("input_tokens"))
            completion_tokens = _int(usage.get("output_tokens"))
            details = usage.get("input_tokens_details")
            cache_tokens = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    elif protocol == Protocol.ANTHROPIC_MESSAGES:
        content = raw.get("content")
        if not isinstance(content, list):
            raise _invalid("响应缺少 content[]", response)
        text = "".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
        finish_reason = raw.get("stop_reason")
        if usage is not None:
            prompt_tokens = _int(usage.get("input_tokens"))
            completion_tokens = _int(usage.get("output_tokens"))
            cache_tokens = _int(usage.get("cache_read_input_tokens"))
    else:
        raise ValueError(f"不是文本协议：{protocol.value}")

    return TextResult(
        text=text,
        model=model,
        request_id=response.request_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cache_tokens=cache_tokens,
        usage_missing=usage is None,
        finish_reason=str(finish_reason) if finish_reason is not None else None,
        citations=extract_citations(text, raw),
        latency_ms=response.latency_ms,
        http_status=response.http_status,
        raw=raw,
    )


# ---------------------------------------------------------------- 引用与 JSON

_MD_LINK_RE = re.compile(r"\[([^\]\n]{0,300})\]\((https?://[^\s)<>]+)\)")
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}（）【】「」《》，。；！？、]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?'\"*_~`"


def _annotation_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """三协议的引用字段：``choices[0].message.annotations[]`` / ``output[].content[].annotations[]`` / ``content[].citations[]``。"""
    items: list[dict[str, Any]] = []
    choices = raw.get("choices")
    if isinstance(choices, list):
        for choice in choices[:1]:
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and isinstance(message.get("annotations"), list):
                items.extend(a for a in message["annotations"] if isinstance(a, dict))
    output = raw.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            for part in item.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("annotations"), list):
                    items.extend(a for a in part["annotations"] if isinstance(a, dict))
    content = raw.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("citations"), list):
                items.extend(c for c in part["citations"] if isinstance(c, dict))
    return items


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _clean_url(url: str) -> str:
    return url.strip().rstrip(_TRAILING_PUNCT)


def extract_citations(text: str, raw: dict[str, Any]) -> list[Citation]:
    """先取响应 ``annotations`` / ``citations`` 字段（``source="annotation"``）；无则解析回答中的 Markdown 链接
    （``markdown_link``）与裸 URL（``plain_url``）；Mock 回答（``raw["mock"] is True``）一律 ``source="mock"``。去重保序。"""
    raw = raw if isinstance(raw, dict) else {}
    is_mock = raw.get("mock") is True
    result: list[Citation] = []
    seen: set[str] = set()

    def add(url: str | None, title: str | None, snippet: str | None, source: str) -> None:
        if not url:
            return
        url = _clean_url(url)
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            return
        seen.add(url)
        result.append(Citation(url=url, title=title, snippet=snippet, source="mock" if is_mock else source))

    for item in _annotation_items(raw):
        nested = item.get("url_citation") if isinstance(item.get("url_citation"), dict) else {}
        url = _str_or_none(item.get("url")) or _str_or_none(nested.get("url"))
        title = _str_or_none(item.get("title")) or _str_or_none(nested.get("title"))
        snippet = (
            _str_or_none(item.get("cited_text")) or _str_or_none(item.get("snippet"))
            or _str_or_none(nested.get("snippet")) or _str_or_none(item.get("content") if isinstance(item.get("content"), str) else None)
        )
        add(url, title, snippet, "annotation")
    if result:
        return result

    text = text or ""
    for match in _MD_LINK_RE.finditer(text):
        add(match.group(2), match.group(1).strip() or None, None, "markdown_link")
    stripped = _MD_LINK_RE.sub(" ", text)
    for match in _URL_RE.finditer(stripped):
        add(match.group(0), None, None, "plain_url")
    return result


_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def extract_json(text: str) -> Any:
    """去掉 Markdown 代码围栏后 ``json.loads``；整体解析失败时取首个 ``{``/``[`` 到最后一个 ``}``/``]`` 的片段再试；
    仍失败抛 ``ZhiqiError(INVALID_RESPONSE)``。"""
    if text is None:
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出为空，无法解析 JSON")
    body = text.strip()
    match = _FENCE_RE.match(body)
    if match:
        body = match.group(1).strip()
    if not body:
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出为空，无法解析 JSON")
    try:
        return json.loads(body)
    except ValueError:
        pass
    starts = [i for i in (body.find("{"), body.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(body.rfind("}"), body.rfind("]"))
        if end > start:
            try:
                return json.loads(body[start : end + 1])
            except ValueError:
                pass
    raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不是合法 JSON")
