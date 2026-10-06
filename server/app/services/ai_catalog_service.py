"""模型目录与价格同步（docs/08-zhiqiapi-integration.md §11；docs/03 B.16；docs/04 §6.15、§7.15）。

- ``sync_models(db)``：持 ``lock:ai:models_sync``（300s）拉取 ``GET /v1/models`` + ``GET /api/pricing_new``，以 ``model_id``
  upsert ``ai_models``（两边并集），本次未出现于 ``/v1/models`` 的模型 ``is_available=0``；刷新 ``cache:ai:models:catalog``、
  清 ``cache:ai:models:options:*``；校验启用路由与 GEO/SEO 引擎引用的模型：消失 → ``force_open(model_unavailable)`` +
  ``ai_breaker_open``，重新出现 → ``reset()`` 并自动解决告警（§11.4）。
- ``catalog_entry(db, model_id)``：读 ``cache:ai:models:catalog``，键缺失时从 ``ai_models``（``is_available=1``）重建并回填。
- ``list_models`` / ``get_model`` / ``model_options``：``GET /admin/ai/models*``（不受数据范围约束）。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.database import after_commit
from app.core.exceptions import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_UPSTREAM_ERROR,
    BusinessError,
)
from app.core.locks import acquire_lock, release_lock
from app.core.redis import cache_delete_prefix, cache_get_json, cache_set_json
from app.core.zhiqi import catalog, get_client
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import ModelInfo, PricingEntry
from app.models import AiModel, CapabilityRoute, utcnow
from app.schemas.common import iso_utc
from app.services import settings_service
from app.services.data_scope_service import SYSTEM_SCOPE

logger = logging.getLogger(__name__)

CATALOG_CACHE_KEY = "cache:ai:models:catalog"
MODELS_CACHE_PREFIX = "cache:ai:models:"
OPTIONS_CACHE_PREFIX = "cache:ai:models:options:"
OPTIONS_CACHE_TTL = 60
SYNC_LOCK_KEY = "lock:ai:models_sync"
SYNC_LOCK_TTL = 300
MODALITIES = ("text", "image", "video")


# =====================================================================
# 编解码
# =====================================================================


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def _num(value: Decimal | None) -> float | int | None:
    if value is None:
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def catalog_ttl(cfg: dict[str, Any] | None) -> int:
    """``max(catalog.sync_interval_seconds, 3600) + 600``（默认 4200s，始终长于同步间隔）。"""
    interval = int(((cfg or {}).get("catalog") or {}).get("sync_interval_seconds") or 0)
    return max(interval, 3600) + 600


# =====================================================================
# 目录缓存 cache:ai:models:catalog
# =====================================================================


def _routing_config(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "ai_routing_config")


def rebuild_catalog_cache(db: Session) -> dict[str, dict[str, Any]]:
    """从 ``ai_models``（``is_available=1``）重建目录缓存并按同 TTL 回填。"""
    rows = db.execute(
        select(AiModel.model_id, AiModel.owned_by, AiModel.supported_endpoint_types_json).where(AiModel.is_available == True)  # noqa: E712
    ).all()
    data = {
        model_id: {"owned_by": owned_by, "supported_endpoint_types": _loads(types_json, []) or []}
        for model_id, owned_by, types_json in rows
    }
    cache_set_json(CATALOG_CACHE_KEY, data, catalog_ttl(_routing_config(db)))
    return data


def catalog_entry(db: Session, model_id: str) -> ModelInfo | None:
    """模型目录项（供 ``catalog.protocol_for``）：键缺失时由 ``ai_models`` 回填；回填后仍无该模型返回 ``None``（目录缺失）。"""
    data = cache_get_json(CATALOG_CACHE_KEY)
    if not isinstance(data, dict):
        data = rebuild_catalog_cache(db)
    entry = data.get(model_id)
    if not isinstance(entry, dict):
        return None
    return ModelInfo(id=model_id, owned_by=entry.get("owned_by"), supported_endpoint_types=list(entry.get("supported_endpoint_types") or []))


def invalidate_model_caches() -> int:
    return cache_delete_prefix(MODELS_CACHE_PREFIX)


# =====================================================================
# 同步（§11.1、§11.2、§11.4）
# =====================================================================


def _apply_pricing(row: AiModel, entry: PricingEntry, vendors: dict[int, str]) -> None:
    row.vendor_id = entry.vendor_id
    row.vendor_name = vendors.get(entry.vendor_id) if entry.vendor_id is not None else None
    row.description = entry.description
    row.tags_json = _dumps(entry.tags or [])
    row.icon = (entry.icon or None) and entry.icon[:500]
    row.cover_url = (entry.cover_url or None) and entry.cover_url[:500]
    row.quota_type = int(entry.quota_type or 0)
    row.model_ratio = _decimal(entry.model_ratio)
    row.model_price = _decimal(entry.model_price)
    row.completion_ratio = _decimal(entry.completion_ratio)
    row.cache_ratio = _decimal(entry.cache_ratio)
    row.create_cache_ratio = _decimal(entry.create_cache_ratio)
    row.enable_groups_json = _dumps(entry.enable_groups or [])
    row.billing_mode = (entry.billing_mode or None) and entry.billing_mode[:32]
    row.billing_expr = (entry.billing_expr or None) and entry.billing_expr[:255]
    row.model_price_type = (entry.model_price_type or None) and entry.model_price_type[:32]
    row.sort_order = int(entry.sort_order or 0)
    row.raw_pricing_json = _dumps(entry.raw)


def _referenced_models(db: Session) -> set[tuple[str, str]]:
    """启用路由的主 / 备模型 + 启用 GEO 引擎模型（geo_check）+ SEO 引擎覆盖模型（seo_check），按 (capability, model) 去重。"""
    refs: set[tuple[str, str]] = set()
    routes = db.scalars(select(CapabilityRoute).where(CapabilityRoute.is_enabled == True)).all()  # noqa: E712
    for route in routes:
        models = [route.primary_model, *(_loads(route.fallback_models_json, []) or [])]
        for model in models:
            if isinstance(model, str) and model.strip():
                refs.add((route.capability, model.strip()))
    geo = settings_service.get_config(db, "geo_engines")
    for engine in geo.get("engines") or []:
        if isinstance(engine, dict) and engine.get("enabled") and str(engine.get("model") or "").strip():
            refs.add(("geo_check", str(engine["model"]).strip()))
    seo = settings_service.get_config(db, "seo_providers")
    for engine in (seo.get("engines") or {}).values():
        if isinstance(engine, dict) and str(engine.get("model") or "").strip():
            refs.add(("seo_check", str(engine["model"]).strip()))
    return refs


def _check_referenced_models(db: Session, present: set[str], cfg: dict[str, Any]) -> dict[str, list[str]]:
    """§11.4：不在目录的引用模型 ``force_open(model_unavailable)`` + 告警；重新出现的 ``reset()`` + 自动解决。"""
    from app.services import ai_gateway_service, alert_service  # 避免循环导入

    breaker = ai_gateway_service.get_breaker(cfg)
    opened: list[str] = []
    restored: list[str] = []
    for capability, model in sorted(_referenced_models(db)):
        target_key = f"{capability}:{model}"
        if model not in present:
            if breaker.force_open(capability, model, reason="model_unavailable"):
                opened.append(target_key)
                alert_service.raise_alert(
                    db, SYSTEM_SCOPE, "ai_breaker_open",
                    target_type="ai_model", target_key=target_key,
                    title=f"AI 模型已下架：{target_key}",
                    message=f"模型 {model} 不在 zhiqiapi 模型目录（/v1/models）中，能力 {capability} 将跳过该模型直至其重新出现",
                    payload={"capability": capability, "model": model, "reason": "model_unavailable"},
                )
        elif breaker.reason(capability, model) == "model_unavailable" and breaker.reset(capability, model):
            restored.append(target_key)
            alert_service.resolve_alert(db, SYSTEM_SCOPE, "ai_breaker_open", "ai_model", target_key)
    return {"opened": opened, "restored": restored}


def sync_models(db: Session) -> dict[str, Any]:
    """立即同步模型目录与价格（周期任务与 ``POST /admin/ai/models/sync`` 共用）。

    返回 ``{total, added, updated, unavailable, synced_at, request_ids:{models, pricing}}``；
    ``lock:ai:models_sync`` 占用中 → 409；上游拉取失败 → 5021（不写 ``ai_models``，释放锁）。
    """
    token = acquire_lock(SYNC_LOCK_KEY, SYNC_LOCK_TTL)
    if not token:
        raise BusinessError("模型目录正在同步，请稍后再试", code=CODE_CONFLICT, http_status=409, data=None)
    try:
        return _sync_models_locked(db)
    finally:
        release_lock(SYNC_LOCK_KEY, token)


def _sync_models_locked(db: Session) -> dict[str, Any]:
    from app.services.ai_gateway_service import get_retry_policy  # 避免循环导入

    client = get_client()
    cfg = _routing_config(db)
    policy = get_retry_policy(cfg)  # 幂等 GET 按 ai_routing_config.retry 全量重试（docs/08 §4.2、§8.2）
    try:
        models, models_request_id = catalog.list_models(client, retry=policy)
        pricing, pricing_request_id = catalog.list_pricing(client, retry=policy)
    except ZhiqiError as err:
        logger.warning("模型目录同步失败 category=%s request_id=%s", err.category.value, err.request_id or "-")
        raise BusinessError(
            "上游服务调用失败",
            code=CODE_UPSTREAM_ERROR,
            data={"error_category": err.category.value, "request_id": err.request_id, "model": None, "hint": None},
        ) from err

    now = utcnow()
    by_model: dict[str, ModelInfo] = {}
    for info in models:
        if info.id and info.id not in by_model:
            by_model[info.id] = info
    by_pricing: dict[str, PricingEntry] = {}
    for entry in pricing.models:
        if entry.model_name and entry.model_name not in by_pricing:
            by_pricing[entry.model_name] = entry
    vendors: dict[int, str] = {}
    for vendor in pricing.vendors or []:
        try:
            vendors[int(vendor.get("id"))] = str(vendor.get("name") or "")[:80] or None  # type: ignore[assignment]
        except (TypeError, ValueError):
            continue

    union = list(dict.fromkeys([*by_model.keys(), *by_pricing.keys()]))
    existing = {row.model_id: row for row in db.scalars(select(AiModel)).all()}
    added = updated = 0
    for model_id in union:
        model_id_db = model_id[:120]
        row = existing.get(model_id_db)
        if row is None:
            row = AiModel(model_id=model_id_db, synced_at=now)
            db.add(row)
            existing[model_id_db] = row
            added += 1
        else:
            updated += 1
        info = by_model.get(model_id)
        entry = by_pricing.get(model_id)
        if entry is not None:
            _apply_pricing(row, entry, vendors)
        if info is not None:
            row.owned_by = (info.owned_by or None) and str(info.owned_by)[:80]
        types = list(info.supported_endpoint_types) if info is not None and info.supported_endpoint_types else (
            list(entry.supported_endpoint_types) if entry is not None else []
        )
        row.supported_endpoint_types_json = _dumps(types)
        row.modalities_json = _dumps(sorted(catalog.derive_modalities(types)))

    present = {m[:120] for m in by_model}
    for model_id, row in existing.items():
        if model_id in present:
            row.is_available = True
            row.last_seen_at = now
        else:
            row.is_available = False
        row.synced_at = now
    db.flush()
    unavailable = int(db.scalar(select(func.count(AiModel.id)).where(AiModel.is_available == False)) or 0)  # noqa: E712

    checks = _check_referenced_models(db, present, cfg)
    catalog_data = {
        model_id[:120]: {"owned_by": info.owned_by, "supported_endpoint_types": list(info.supported_endpoint_types or [])}
        for model_id, info in by_model.items()
    }
    ttl = catalog_ttl(cfg)

    def _refresh_caches() -> None:
        cache_delete_prefix(MODELS_CACHE_PREFIX)
        cache_set_json(CATALOG_CACHE_KEY, catalog_data, ttl)

    after_commit(db, _refresh_caches)
    db.commit()
    result = {
        "total": len(union),
        "added": added,
        "updated": updated,
        "unavailable": unavailable,
        "synced_at": iso_utc(now),
        "request_ids": {"models": models_request_id, "pricing": pricing_request_id},
    }
    logger.info(
        "模型目录同步完成 total=%s added=%s updated=%s unavailable=%s breaker_opened=%s restored=%s request_ids=%s",
        result["total"], added, updated, unavailable, checks["opened"], checks["restored"], result["request_ids"],
    )
    return result


# =====================================================================
# 查询（GET /admin/ai/models*）
# =====================================================================


def model_item(row: AiModel, *, include_raw: bool = False) -> dict[str, Any]:
    """模型对象（docs/04 §7.15）；``raw_pricing`` 只在详情返回。"""
    item: dict[str, Any] = {
        "id": row.id,
        "model_id": row.model_id,
        "owned_by": row.owned_by,
        "vendor_id": row.vendor_id,
        "vendor_name": row.vendor_name,
        "description": row.description,
        "tags": _loads(row.tags_json, []) or [],
        "icon": row.icon,
        "cover_url": row.cover_url,
        "supported_endpoint_types": _loads(row.supported_endpoint_types_json, []) or [],
        "modalities": _loads(row.modalities_json, []) or [],
        "quota_type": row.quota_type,
        "model_ratio": _num(row.model_ratio),
        "model_price": _num(row.model_price),
        "completion_ratio": _num(row.completion_ratio),
        "cache_ratio": _num(row.cache_ratio),
        "create_cache_ratio": _num(row.create_cache_ratio),
        "enable_groups": _loads(row.enable_groups_json, []) or [],
        "billing_mode": row.billing_mode,
        "billing_expr": row.billing_expr,
        "model_price_type": row.model_price_type,
        "sort_order": row.sort_order,
        "is_available": bool(row.is_available),
        "last_seen_at": iso_utc(row.last_seen_at),
        "last_health_status": row.last_health_status,
        "last_health_at": iso_utc(row.last_health_at),
        "last_health_latency_ms": row.last_health_latency_ms,
        "synced_at": iso_utc(row.synced_at),
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }
    if include_raw:
        item["raw_pricing"] = _loads(row.raw_pricing_json, None)
    return item


def _modality_condition(modality: str) -> Any:
    return AiModel.modalities_json.like(f'%"{modality}"%')


def list_models(
    db: Session,
    *,
    modality: str | None = None,
    vendor_id: int | None = None,
    is_available: bool | None = None,
    keyword: str | None = None,
    include_hidden: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """分页；默认隐藏 ``is_available=0 AND last_seen_at < now − catalog.hide_unavailable_after_days`` 的模型。"""
    conditions: list[Any] = []
    if modality:
        conditions.append(_modality_condition(modality))
    if vendor_id is not None:
        conditions.append(AiModel.vendor_id == vendor_id)
    if is_available is not None:
        conditions.append(AiModel.is_available == bool(is_available))
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        conditions.append(or_(AiModel.model_id.ilike(like), AiModel.vendor_name.ilike(like), AiModel.description.ilike(like)))
    if not include_hidden:
        days = int((_routing_config(db).get("catalog") or {}).get("hide_unavailable_after_days") or 7)
        cutoff = utcnow() - timedelta(days=days)
        conditions.append(not_(and_(AiModel.is_available == False, AiModel.last_seen_at < cutoff)))  # noqa: E712
    stmt = select(AiModel).where(*conditions)
    total = int(db.scalar(select(func.count(AiModel.id)).where(*conditions)) or 0)
    rows = db.scalars(
        stmt.order_by(AiModel.sort_order.asc(), AiModel.model_id.asc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return [model_item(r) for r in rows], total


def get_model(db: Session, model_pk: int) -> dict[str, Any]:
    row = db.get(AiModel, model_pk)
    if row is None:
        raise BusinessError("模型不存在", code=CODE_NOT_FOUND, http_status=404)
    return model_item(row, include_raw=True)


def get_model_by_id(db: Session, model_id: str) -> AiModel | None:
    return db.scalar(select(AiModel).where(AiModel.model_id == model_id))


def model_options(db: Session, *, modality: str | None = None, is_available: bool | None = True) -> list[dict[str, Any]]:
    """不分页选项 ``[{model_id,vendor_name,modalities,is_available,last_health_status,quota_type}]``，
    缓存 ``cache:ai:models:options:{modality}:{is_available}`` 60s；``modalities_json`` 为空集的模型不可选。"""
    key = f"{OPTIONS_CACHE_PREFIX}{modality or 'all'}:{'all' if is_available is None else int(bool(is_available))}"
    cached = cache_get_json(key)
    if isinstance(cached, list):
        return cached
    conditions: list[Any] = [AiModel.modalities_json.is_not(None), AiModel.modalities_json != "[]"]
    if modality:
        conditions.append(_modality_condition(modality))
    if is_available is not None:
        conditions.append(AiModel.is_available == bool(is_available))
    rows = db.scalars(select(AiModel).where(*conditions).order_by(AiModel.sort_order.asc(), AiModel.model_id.asc())).all()
    items = []
    for row in rows:
        modalities = _loads(row.modalities_json, []) or []
        if not modalities:
            continue
        items.append(
            {
                "model_id": row.model_id,
                "vendor_name": row.vendor_name,
                "modalities": modalities,
                "is_available": bool(row.is_available),
                "last_health_status": row.last_health_status,
                "quota_type": row.quota_type,
            }
        )
    cache_set_json(key, items, OPTIONS_CACHE_TTL)
    return items


def model_supports(row: AiModel | None, modality: str) -> bool:
    """模型存在且 ``modalities_json`` 含该模态。"""
    return row is not None and modality in (_loads(row.modalities_json, []) or [])
