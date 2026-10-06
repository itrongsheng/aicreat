"""用户管理（``/admin/admins``，docs/07 §6.2）：列表、新增、详情、编辑、启用 / 禁用、重置密码；不提供物理删除。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin
from app.schemas.admin_rbac import AdminCreate, AdminResetPasswordBody, AdminStatusBody, AdminUpdate
from app.schemas.common import PageParams
from app.services import admin_rbac_service

router = APIRouter()

AdminId = Annotated[int, Path(gt=0, description="用户 ID")]


@router.get("", summary="用户列表")
def list_admins(
    keyword: str | None = Query(None, max_length=50, description="匹配 username / display_name"),
    group_id: int | None = Query(None, gt=0),
    is_active: bool | None = Query(None),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("security.admins.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = admin_rbac_service.list_admins(
        db, keyword=keyword, group_id=group_id, is_active=is_active, page=pagination.page, page_size=pagination.page_size
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="新增用户")
def create_admin(
    body: AdminCreate,
    request: Request,
    admin: Admin = Depends(require_permission("security.admins.create")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.create_admin(db, admin, body)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = summary
    return ok(item)


@router.get("/{admin_id}", summary="用户详情")
def get_admin(
    admin_id: AdminId,
    _admin: Admin = Depends(require_permission("security.admins.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_rbac_service.get_admin_detail(db, admin_id))


@router.put("/{admin_id}", summary="编辑用户（显示名 / 用户组）")
def update_admin(
    body: AdminUpdate,
    request: Request,
    admin_id: AdminId,
    admin: Admin = Depends(require_permission("security.admins.update")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.update_admin(db, admin, admin_id, body)
    request.state.audit_summary = summary
    return ok(item)


@router.patch("/{admin_id}/status", summary="启用 / 禁用用户")
def set_admin_status(
    body: AdminStatusBody,
    request: Request,
    admin_id: AdminId,
    admin: Admin = Depends(require_permission("security.admins.status")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.set_admin_status(db, admin, admin_id, body.is_active)
    request.state.audit_summary = summary
    return ok(item)


@router.post("/{admin_id}/reset-password", summary="重置用户密码")
def reset_password(
    body: AdminResetPasswordBody,
    request: Request,
    admin_id: AdminId,
    admin: Admin = Depends(require_permission("security.admins.reset_password")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.reset_admin_password(db, admin, admin_id, body.password)
    request.state.audit_summary = summary
    return ok(item)
