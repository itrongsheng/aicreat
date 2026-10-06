"""标题（``/admin/titles``，docs/04 §6.10、§7.5；docs/09 §7；docs/13 §6.3、§7.1、§7.5）。

全部路由声明 ``get_data_scope``：列表按 ``project_id IN P`` 过滤，``/{id}*`` 的目标须可见（否则 404）；生成的 ``project_id`` 与
手工新增的 ``keyword_id`` 须可见。静态子路径 ``generate`` / ``batch-status`` 先于 ``/{id}`` 注册（docs/04 §6.0）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, ContentStyle, TitleStatus
from app.schemas.common import PageParams
from app.schemas.title import (
    BatchStatusBody,
    TitleCreateBody,
    TitleGenerateBody,
    TitleScoreBody,
    TitleUpdateBody,
)
from app.services import title_service
from app.services.data_scope_service import DataScope

router = APIRouter()

TitleId = Path(..., gt=0, description="标题 ID")


@router.get("", summary="标题列表（created_at DESC, id DESC）")
def list_titles(
    project_id: int | None = Query(None, gt=0),
    keyword_id: int | None = Query(None, gt=0),
    status: TitleStatus | None = Query(None),
    style: ContentStyle | None = Query(None),
    batch_id: int | None = Query(None, gt=0),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("content.titles.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = title_service.list_titles(
        db, scope, page=pagination.page, page_size=pagination.page_size, project_id=project_id, keyword_id=keyword_id,
        status=status, style=style, batch_id=batch_id,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="手工新增标题")
def create_title(
    body: TitleCreateBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.titles.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = title_service.create_title(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"新增标题 {item['title'][:60]}"
    return ok(item)


@router.post("/generate", summary="生成标题（每个关键词一个根任务）")
def generate_titles(
    body: TitleGenerateBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.titles.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = title_service.generate_titles(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_summary = (
        f"生成标题：项目 #{body.project_id}，批次 #{data['batch_id']}，关键词 {len(body.keyword_ids)} 个 × {body.count}"
    )
    return ok(data)


@router.post("/batch-status", summary="批量采用 / 弃用 / 恢复")
def batch_status(
    body: BatchStatusBody,
    admin: Admin = Depends(require_permission("content.titles.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.batch_status(db, scope, body.ids, body.action, admin_id=admin.id))


@router.get("/{title_id}", summary="标题详情")
def get_title(
    title_id: int = TitleId,
    _admin: Admin = Depends(require_permission("content.titles.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.get_title(db, scope, title_id))


@router.put("/{title_id}", summary="编辑标题（首次编辑保留 original_title）")
def update_title(
    body: TitleUpdateBody,
    title_id: int = TitleId,
    admin: Admin = Depends(require_permission("content.titles.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.update_title(db, scope, title_id, body.changes(), admin_id=admin.id))


@router.delete("/{title_id}", summary="删除标题（仅无内容关联）")
def delete_title(
    title_id: int = TitleId,
    _admin: Admin = Depends(require_permission("content.titles.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    title_service.delete_title(db, scope, title_id)
    return ok(None)


@router.post("/{title_id}/score", summary="人工打分（0~10，步长 0.5）")
def score_title(
    body: TitleScoreBody,
    title_id: int = TitleId,
    _admin: Admin = Depends(require_permission("content.titles.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.score_title(db, scope, title_id, body.manual_score))


@router.post("/{title_id}/adopt", summary="采用标题（要求关键词已采用）")
def adopt_title(
    title_id: int = TitleId,
    admin: Admin = Depends(require_permission("content.titles.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.adopt_title(db, scope, title_id, admin_id=admin.id))


@router.post("/{title_id}/discard", summary="弃用标题")
def discard_title(
    title_id: int = TitleId,
    admin: Admin = Depends(require_permission("content.titles.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.discard_title(db, scope, title_id, admin_id=admin.id))


@router.post("/{title_id}/restore", summary="恢复标题（discarded → candidate）")
def restore_title(
    title_id: int = TitleId,
    admin: Admin = Depends(require_permission("content.titles.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(title_service.restore_title(db, scope, title_id, admin_id=admin.id))
