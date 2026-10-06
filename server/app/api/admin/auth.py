"""当前用户（``/admin/auth``，docs/07 §6.1、docs/04 §6.1、§7.1、§7.2）。

``POST /login``、``GET /site-info`` 公开；``me`` / ``logout`` / ``change-password`` 仅需登录（``get_current_admin``）。
登录、登出、本人改密没有权限码，由处理函数自行写审计（审计标签 ``auth.login`` / ``auth.logout`` / ``auth.change_password``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin, get_db
from app.core.response import ok
from app.models import Admin
from app.schemas.auth import ChangePasswordBody, LoginBody
from app.services import admin_rbac_service, settings_service
from app.services.i18n import normalize_locale

router = APIRouter()


@router.post("/login", summary="登录（公开）")
def login(body: LoginBody, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    return ok(admin_rbac_service.login(db, request, body.username, body.password))


@router.get("/site-info", summary="站点信息（公开）")
def site_info(
    locale: str | None = Query(None, max_length=35, description="zh-CN / en-US，缺省与非法值回退 zh-CN"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    value = settings_service.get_config(db, "system_info", normalize_locale(locale))
    return ok({k: value.get(k, "") for k in ("site_name", "logo_url", "footer", "support_contact")})


@router.get("/me", summary="当前用户")
def me(admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    return ok(admin_rbac_service.me(db, admin))


@router.post("/logout", summary="登出")
def logout(request: Request, admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)) -> dict[str, Any]:
    admin_rbac_service.logout(db, request, admin)
    return ok(None)


@router.post("/change-password", summary="修改本人密码")
def change_password(
    body: ChangePasswordBody,
    request: Request,
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    admin_rbac_service.change_password(db, request, admin, body.old_password, body.new_password)
    return ok(None, message="密码已修改，请重新登录")
