"""权限定义（``/admin/admin-permissions``，docs/07 §6.3）：平铺列表与 module → menu → action 树（静态字典，不分页）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import require_permission
from app.core.response import ok
from app.models import Admin
from app.services import admin_rbac_service

router = APIRouter()


@router.get("", summary="平铺权限列表")
def list_permissions(_admin: Admin = Depends(require_permission("security.groups.view"))) -> dict[str, Any]:
    return ok(admin_rbac_service.permission_list())


@router.get("/tree", summary="权限树")
def permission_tree(_admin: Admin = Depends(require_permission("security.groups.view"))) -> dict[str, Any]:
    return ok(admin_rbac_service.permission_tree())
