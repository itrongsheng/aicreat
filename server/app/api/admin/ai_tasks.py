"""AI 任务（``/admin/ai/tasks``，docs/04 §6.15、§7.17；docs/08 §7.7）。

按 ``project_id IN P`` 过滤（无项目的探测任务只对总后台可见，docs/13 §6.3）；``/export`` 先于 ``/{id}`` 注册。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import (
    Admin,
    AiTaskOperation,
    AiTaskStatus,
    AiTaskTargetType,
    AiTaskTriggerType,
    Capability,
    ErrorCategory,
)
from app.schemas.ai import RowKind
from app.schemas.common import PageParams
from app.services import ai_task_service
from app.services.data_scope_service import DataScope

router = APIRouter()

TaskId = Path(..., gt=0, description="任务 ID")


class TaskFilters:
    """``/admin/ai/tasks`` 与 ``/export`` 共用的筛选参数（``row_kind`` 缺省：列表 ``root``、导出 ``attempt``）。"""

    def __init__(
        self,
        row_kind: RowKind | None = Query(None, description="root（列表缺省）/ attempt（导出缺省）/ all"),
        project_id: int | None = Query(None, gt=0),
        capability: Capability | None = Query(None),
        operation: AiTaskOperation | None = Query(None),
        model: str | None = Query(None, max_length=120),
        status: AiTaskStatus | None = Query(None),
        error_category: ErrorCategory | None = Query(None),
        trigger_type: AiTaskTriggerType | None = Query(None),
        batch_id: int | None = Query(None, gt=0),
        root_task_id: int | None = Query(None, gt=0),
        target_type: AiTaskTargetType | None = Query(None),
        target_id: int | None = Query(None, gt=0),
        request_id: str | None = Query(None, max_length=64),
        start: datetime | None = Query(None, description="ISO 8601 UTC，含"),
        end: datetime | None = Query(None, description="ISO 8601 UTC，不含"),
    ) -> None:
        self.values: dict[str, Any] = {
            "row_kind": row_kind, "project_id": project_id, "capability": capability, "operation": operation, "model": model,
            "status": status, "error_category": error_category, "trigger_type": trigger_type, "batch_id": batch_id,
            "root_task_id": root_task_id, "target_type": target_type, "target_id": target_id, "request_id": request_id,
            "start": start, "end": end,
        }

    def as_kwargs(self, default_row_kind: str) -> dict[str, Any]:
        values = {k: v for k, v in self.values.items() if v is not None}
        values.setdefault("row_kind", default_row_kind)
        return values


@router.get("", summary="AI 任务列表")
def list_tasks(
    filters: TaskFilters = Depends(),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("ai.tasks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = ai_task_service.list_tasks(
        db, scope, page=pagination.page, page_size=pagination.page_size, **filters.as_kwargs("root")
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.get("/export", summary="导出 AI 任务 CSV")
def export_tasks(
    filters: TaskFilters = Depends(),
    format: str = Query("csv", pattern="^csv$"),  # noqa: A002 - 与 ExportParams 一致的查询参数名
    _admin: Admin = Depends(require_permission("ai.tasks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> Response:
    del format
    filename, content = ai_task_service.export_tasks(db, scope, **filters.as_kwargs("attempt"))
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/{task_id}", summary="AI 任务详情")
def get_task(
    task_id: int = TaskId,
    _admin: Admin = Depends(require_permission("ai.tasks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_task_service.get_task_detail(db, scope, task_id))


@router.post("/{task_id}/retry", summary="重试根任务")
def retry_task(
    task_id: int = TaskId,
    admin: Admin = Depends(require_permission("ai.tasks.retry")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_task_service.retry_task(db, scope, task_id, admin_id=admin.id))


@router.post("/{task_id}/cancel", summary="取消根任务")
def cancel_task(
    task_id: int = TaskId,
    admin: Admin = Depends(require_permission("ai.tasks.cancel")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_task_service.cancel_task(db, scope, task_id, admin_id=admin.id))
