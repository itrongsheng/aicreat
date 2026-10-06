"""模型目录与价格目录（docs/08-zhiqiapi-integration.md §3.4、§3.5、§5.8、§6.5、§11.3）。

``list_models`` / ``list_pricing`` 只做拉取与解析（不读库）；落库、目录缓存与回填由 ``ai_catalog_service`` 负责。
``protocol_for`` 的 ``model`` 由调用方经 ``ai_catalog_service.catalog_entry(db, model_id)`` 取得，``None`` 表示目录缺失。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.zhiqi.client import ZhiqiClient, ZhiqiResponse
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import (
    IMAGE_PROTOCOLS,
    RetryPolicy,
    TEXT_PROTOCOLS,
    ErrorCategory,
    ModelInfo,
    PricingCatalog,
    PricingEntry,
    Protocol,
)

logger = logging.getLogger("app.core.zhiqi")

TEXT_ENDPOINTS = ("openai", "openai-response", "anthropic")
IMAGE_ENDPOINTS = ("image-generation", "image-edit", "image-generation-async")
VIDEO_ENDPOINT = "openai-video"
# 文本协议 ↔ 端点类型（按此顺序取首个可用）
TEXT_ENDPOINT_PROTOCOLS: tuple[tuple[str, Protocol], ...] = (
    ("openai", Protocol.OPENAI_CHAT),
    ("openai-response", Protocol.OPENAI_RESPONSES),
    ("anthropic", Protocol.ANTHROPIC_MESSAGES),
)
PROTOCOL_ENDPOINT = {protocol: endpoint for endpoint, protocol in TEXT_ENDPOINT_PROTOCOLS}


# ---------------------------------------------------------------- 字段转换


def _str_list(value: Any) -> list[str]:
    """数组或逗号分隔字符串 → 去空白的字符串列表（JSON 字符串形式的数组也兼容）。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                return _str_list(json.loads(text))
            except ValueError:
                pass
        return [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    return [str(value).strip()]


def _opt_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _items(body: Any, keys: tuple[str, ...]) -> list[Any]:
    """列表外层包装以官方文档为准：兼容顶层数组、``data[]`` 与 ``data.{keys}[]``。"""
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return value
    data = body.get("data")
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _require_json(response: ZhiqiResponse, what: str) -> Any:
    if response.json is None:
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, f"{what}响应为空", http_status=response.http_status, request_id=response.request_id)
    return response.json


# ---------------------------------------------------------------- 拉取


def parse_models(body: Any) -> list[ModelInfo]:
    models: list[ModelInfo] = []
    seen: set[str] = set()
    for item in _items(body, ("data", "models", "items")):
        if not isinstance(item, dict):
            continue
        model_id = _opt_str(item.get("id") or item.get("model_name"))
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(
            ModelInfo(id=model_id, owned_by=_opt_str(item.get("owned_by")), supported_endpoint_types=_str_list(item.get("supported_endpoint_types")))
        )
    return models


def list_models(client: ZhiqiClient, *, timeout: float = 30, retry: RetryPolicy | None = None) -> tuple[list[ModelInfo], str | None]:
    """``GET /v1/models`` → ``(models, request_id)``；``retry`` 为幂等 GET 的客户端重试策略（§4.2 ``retry``，调用方按
    ``ai_routing_config.retry`` 构造注入；``None`` 不重试）。"""
    response = client.get("/v1/models", timeout=timeout, retry=retry)
    models = parse_models(_require_json(response, "模型目录"))
    logger.info("zhiqi 模型目录 total=%s request_id=%s", len(models), response.request_id or "-")
    return models, response.request_id


def parse_pricing_entry(item: dict[str, Any]) -> PricingEntry | None:
    model_name = _opt_str(item.get("model_name") or item.get("model") or item.get("id"))
    if not model_name:
        return None
    return PricingEntry(
        model_name=model_name,
        description=_opt_str(item.get("description")),
        cover_url=_opt_str(item.get("cover_url")),
        tags=_str_list(item.get("tags")),
        vendor_id=_opt_int(item.get("vendor_id")),
        sort_order=_opt_int(item.get("sort_order")) or 0,
        quota_type=_opt_int(item.get("quota_type")) or 0,
        model_ratio=_opt_float(item.get("model_ratio")),
        model_price=_opt_float(item.get("model_price")),
        completion_ratio=_opt_float(item.get("completion_ratio")),
        cache_ratio=_opt_float(item.get("cache_ratio")),
        create_cache_ratio=_opt_float(item.get("create_cache_ratio")),
        enable_groups=_str_list(item.get("enable_groups")),
        supported_endpoint_types=_str_list(item.get("supported_endpoint_types")),
        billing_mode=_opt_str(item.get("billing_mode")),
        billing_expr=_opt_str(item.get("billing_expr")),
        icon=_opt_str(item.get("icon")),
        model_price_type=_opt_str(item.get("model_price_type")),
        raw=item,
    )


def parse_pricing(body: Any) -> PricingCatalog:
    models: list[PricingEntry] = []
    seen: set[str] = set()
    for item in _items(body, ("data", "models", "items", "list")):
        if not isinstance(item, dict):
            continue
        entry = parse_pricing_entry(item)
        if entry is None or entry.model_name in seen:
            continue
        seen.add(entry.model_name)
        models.append(entry)
    vendors_raw = _items(body, ("vendors",)) if isinstance(body, dict) else []
    vendors = [v for v in vendors_raw if isinstance(v, dict)]
    caps: Any = None
    if isinstance(body, dict):
        caps = body.get("model_parameter_capabilities")
        if caps is None and isinstance(body.get("data"), dict):
            caps = body["data"].get("model_parameter_capabilities")
    return PricingCatalog(models=models, vendors=vendors, model_parameter_capabilities=caps if isinstance(caps, dict) else {})


def list_pricing(client: ZhiqiClient, *, timeout: float = 30, retry: RetryPolicy | None = None) -> tuple[PricingCatalog, str | None]:
    """``GET /api/pricing_new``（origin 拼接，Bearer）→ ``(catalog, request_id)``；``retry`` 同 ``list_models``。"""
    response = client.get("/api/pricing_new", timeout=timeout, retry=retry)
    catalog = parse_pricing(_require_json(response, "价格目录"))
    logger.info("zhiqi 价格目录 total=%s vendors=%s request_id=%s", len(catalog.models), len(catalog.vendors), response.request_id or "-")
    return catalog, response.request_id


# ---------------------------------------------------------------- 推导


def derive_modalities(supported_endpoint_types: list[str]) -> set[str]:
    """``openai`` / ``openai-response`` / ``anthropic`` → text；``image-generation`` / ``image-edit`` /
    ``image-generation-async`` → image；``openai-video`` → video（可多模态）。"""
    types = {str(t).strip() for t in (supported_endpoint_types or [])}
    modalities: set[str] = set()
    if types & set(TEXT_ENDPOINTS):
        modalities.add("text")
    if types & set(IMAGE_ENDPOINTS):
        modalities.add("image")
    if VIDEO_ENDPOINT in types:
        modalities.add("video")
    return modalities


def protocol_for(model: ModelInfo | None, preferred: Protocol) -> Protocol | None:
    """协议预选（§6.5）：

    - ``model=None``（目录缺失）→ ``preferred``，交由上游 404 判定；
    - 文本：``preferred`` 可用则用之，否则按 openai → openai_chat、openai-response → openai_responses、anthropic → anthropic_messages
      取首个可用；无文本端点 → ``None``（网关记 model_unrouted，不发 HTTP）；
    - 图片：有任一图片端点 → 一律 ``image_async``（不按目录预选同步）；否则 ``None``；
    - 视频：有 ``openai-video`` → ``video``，否则 ``None``。
    """
    preferred = Protocol(preferred)
    if model is None:
        return preferred
    types = {str(t).strip() for t in (model.supported_endpoint_types or [])}
    if preferred in TEXT_PROTOCOLS:
        if PROTOCOL_ENDPOINT[preferred] in types:
            return preferred
        for endpoint, protocol in TEXT_ENDPOINT_PROTOCOLS:
            if endpoint in types:
                return protocol
        return None
    if preferred in IMAGE_PROTOCOLS:
        return Protocol.IMAGE_ASYNC if types & set(IMAGE_ENDPOINTS) else None
    if preferred == Protocol.VIDEO:
        return Protocol.VIDEO if VIDEO_ENDPOINT in types else None
    return None


def sync_fallback_protocol(model: ModelInfo | None, *, has_reference_images: bool) -> Protocol:
    """§6.5「图片同步回退端点」：无参考图 → ``image_sync``；有参考图：目录含 ``image-edit`` 或目录缺失 → ``image_edit``，否则 ``image_sync``。"""
    if not has_reference_images:
        return Protocol.IMAGE_SYNC
    if model is None or "image-edit" in {str(t).strip() for t in (model.supported_endpoint_types or [])}:
        return Protocol.IMAGE_EDIT
    return Protocol.IMAGE_SYNC
