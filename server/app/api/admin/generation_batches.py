"""生成批次（``/admin/generation-batches``，docs/04 §6.12；docs/09 §9；docs/13 §6.3）。

全部路由声明 ``get_data_scope``：按 ``project_id IN P`` 过滤，``/{id}*`` 的目标须可见（否则 404）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, BatchKind, BatchStatus
from app.schemas.common import PageParams
from app.services import generation_service
from app.services.data_scope_service import DataScope

router = APIRouter()

BatchId = Path(..., gt=0, description="批次 ID")


@router.get("", summary="生成批次列表")
def list_batches(
    project_id: int | None = Query(None, gt=0),
    kind: BatchKind | None = Query(None),
    status: BatchStatus | None = Query(None),
    created_by: int | None = Query(None, gt=0),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("content.batches.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = generation_service.list_batches(
        db, scope, page=pagination.page, page_size=pagination.page_size, project_id=project_id, kind=kind, status=status,
        created_by=created_by,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.get("/{batch_id}", summary="批次详情（含根任务摘要 tasks[]）")
def get_batch(
    batch_id: int = BatchId,
    _admin: Admin = Depends(require_permission("content.batches.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(generation_service.batch_detail(db, scope, batch_id))


@router.post("/{batch_id}/cancel", summary="取消批次（未开始的根任务置 cancelled，运行中的不打断）")
def cancel_batch(
    batch_id: int = BatchId,
    admin: Admin = Depends(require_permission("content.batches.cancel")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(generation_service.cancel_batch(db, scope, batch_id, admin_id=admin.id))


@router.post("/{batch_id}/retry", summary="重试批次（只重跑尚无重试子任务的 failed 根任务）")
def retry_batch(
    batch_id: int = BatchId,
    admin: Admin = Depends(require_permission("content.batches.retry")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(generation_service.retry_batch(db, scope, batch_id, admin_id=admin.id))
