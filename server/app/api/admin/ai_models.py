"""模型目录（``/admin/ai/models``，docs/04 §6.15、§7.15；docs/08 §11.5）。

``ai_models`` 是平台配置，不受数据范围约束（docs/13 §4.3）。静态子路径 ``/options`` 与 ``/sync`` 先于 ``/{id}`` 注册。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, Modality
from app.schemas.common import PageParams
from app.services import ai_catalog_service

router = APIRouter()


@router.get("", summary="模型目录（分页）")
def list_models(
    modality: Modality | None = Query(None, description="text / image / video"),
    vendor_id: int | None = Query(None, ge=0),
    is_available: bool | None = Query(None),
    keyword: str | None = Query(None, max_length=120, description="模型 ID / 厂商 / 描述模糊匹配"),
    include_hidden: bool = Query(False, description="显示 is_available=0 且久未出现（catalog.hide_unavailable_after_days）的模型"),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("ai.models.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = ai_catalog_service.list_models(
        db, modality=modality, vendor_id=vendor_id, is_available=is_available, keyword=keyword, include_hidden=include_hidden,
        page=pagination.page, page_size=pagination.page_size,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.get("/options", summary="模型选项（不分页，ModelSelect 用）")
def model_options(
    modality: Modality | None = Query(None, description="text / image / video"),
    is_available: bool | None = Query(True, description="缺省只返回可用模型"),
    _admin: Admin = Depends(require_permission("ai.models.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_catalog_service.model_options(db, modality=modality, is_available=is_available))


@router.post("/sync", summary="立即同步模型目录与价格")
def sync_models(
    _admin: Admin = Depends(require_permission("ai.models.sync")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """``GET /v1/models`` + ``GET /api/pricing_new``（锁 ``lock:ai:models_sync``，占用中 409；上游失败 5021）。"""
    return ok(ai_catalog_service.sync_models(db))


@router.get("/{model_pk}", summary="模型详情（含 raw_pricing）")
def get_model(
    model_pk: int = Path(..., gt=0),
    _admin: Admin = Depends(require_permission("ai.models.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_catalog_service.get_model(db, model_pk))
