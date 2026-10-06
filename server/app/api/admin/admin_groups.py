"""用户组（``/admin/admin-groups``，docs/07 §6.3）：列表（不分页）、新增、详情、编辑（含数据范围）、权限覆盖保存、删除。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.core.response import ok
from app.models import Admin
from app.schemas.admin_rbac import GroupCreate, GroupUpdate, PermissionCodesBody
from app.services import admin_rbac_service

router = APIRouter()

GroupId = Annotated[int, Path(gt=0, description="用户组 ID")]


@router.get("", summary="用户组列表（不分页）")
def list_groups(
    is_active: bool | None = Query(None),
    _admin: Admin = Depends(require_permission("security.groups.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_rbac_service.list_groups(db, is_active=is_active))


@router.post("", summary="新增用户组")
def create_group(
    body: GroupCreate,
    request: Request,
    admin: Admin = Depends(require_permission("security.groups.create")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.create_group(db, admin, body)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = summary
    return ok(item)


@router.get("/{group_id}", summary="用户组详情")
def get_group(
    group_id: GroupId,
    _admin: Admin = Depends(require_permission("security.groups.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(admin_rbac_service.get_group_detail(db, group_id))


@router.put("/{group_id}", summary="编辑用户组（名称 / 说明 / 状态 / 数据范围）")
def update_group(
    body: GroupUpdate,
    request: Request,
    group_id: GroupId,
    admin: Admin = Depends(require_permission("security.groups.update")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.update_group(db, admin, group_id, body)
    request.state.audit_summary = summary
    return ok(item)


@router.put("/{group_id}/permissions", summary="覆盖保存用户组权限")
def save_permissions(
    body: PermissionCodesBody,
    request: Request,
    group_id: GroupId,
    _admin: Admin = Depends(require_permission("security.groups.assign")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item, summary = admin_rbac_service.save_group_permissions(db, group_id, body.permission_codes)
    request.state.audit_summary = summary
    return ok(item)


@router.delete("/{group_id}", summary="删除用户组")
def delete_group(
    request: Request,
    group_id: GroupId,
    _admin: Admin = Depends(require_permission("security.groups.delete")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    request.state.audit_summary = admin_rbac_service.delete_group(db, group_id)
    return ok(None)
