"""项目（``/admin/projects``，docs/04 §6.7、§7.3；docs/09 §4；docs/13 §6.3、§6.4、§7.2）。

全部路由声明 ``get_data_scope``：列表只含可见项目，``/{id}*`` 的目标须为可见项目（否则 404）。
静态子路径 ``owner-options`` 必须在 ``GET /{id}`` 之前注册（docs/04 §6.0）。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, ProjectStatus
from app.schemas.common import PageParams
from app.schemas.project import ProjectCreate, ProjectRoutesBody, ProjectUpdate
from app.services import data_scope_service, project_service
from app.services.data_scope_service import DataScope

router = APIRouter()

ProjectId = Path(..., gt=0, description="项目 ID")


@router.get("", summary="项目列表")
def list_projects(
    keyword: str | None = Query(None, max_length=100, description="名称 / slug / 行业 / 品牌名模糊匹配"),
    status: ProjectStatus | None = Query(None),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("content.projects.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = project_service.list_projects(
        db, scope, page=pagination.page, page_size=pagination.page_size, keyword=keyword, status=status,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="创建项目")
def create_project(
    body: ProjectCreate,
    request: Request,
    admin: Admin = Depends(require_permission("content.projects.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = project_service.create_project(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"创建项目 {item['name']}"
    return ok(item)


@router.get("/owner-options", summary="负责人候选（不分页）")
def owner_options(
    _admin: Admin = Depends(require_permission("content.projects.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(data_scope_service.owner_options(db, scope))


@router.get("/{project_id}", summary="项目详情（含项目级路由覆盖）")
def get_project(
    project_id: int = ProjectId,
    _admin: Admin = Depends(require_permission("content.projects.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(project_service.get_project(db, scope, project_id))


@router.put("/{project_id}", summary="编辑项目（owner_id 变化即转移负责人）")
def update_project(
    body: ProjectUpdate,
    request: Request,
    project_id: int = ProjectId,
    admin: Admin = Depends(require_permission("content.projects.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, transfer_summary = project_service.update_project(db, scope, project_id, body.changes(), admin_id=admin.id)
    if transfer_summary:
        request.state.audit_summary = transfer_summary
    return ok(item)


@router.delete("/{project_id}", summary="删除项目（仅已归档且无子对象）")
def delete_project(
    project_id: int = ProjectId,
    _admin: Admin = Depends(require_permission("content.projects.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project_service.delete_project(db, scope, project_id)
    return ok(None)


@router.put("/{project_id}/routes", summary="保存项目级模型覆盖")
def save_routes(
    body: ProjectRoutesBody,
    project_id: int = ProjectId,
    admin: Admin = Depends(require_permission("content.projects.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    routes = [route.model_dump() for route in body.routes]
    return ok(project_service.save_project_routes(db, scope, project_id, routes, admin_id=admin.id))


@router.post("/{project_id}/archive", summary="归档项目")
def archive_project(
    project_id: int = ProjectId,
    _admin: Admin = Depends(require_permission("content.projects.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(project_service.archive_project(db, scope, project_id))


@router.post("/{project_id}/unarchive", summary="恢复项目")
def unarchive_project(
    project_id: int = ProjectId,
    _admin: Admin = Depends(require_permission("content.projects.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(project_service.unarchive_project(db, scope, project_id))


@router.get("/{project_id}/overview", summary="项目 KPI（结构同 /admin/stats/overview）")
def project_overview(
    project_id: int = ProjectId,
    range_: Literal["today", "7d", "30d"] = Query("7d", alias="range"),
    _admin: Admin = Depends(require_permission("content.projects.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(project_service.overview(db, scope, project_id, range_=range_))
