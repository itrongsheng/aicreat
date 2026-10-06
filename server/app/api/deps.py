"""路由依赖：会话、语言、管理员鉴权、权限码、数据范围与分页（docs/07 §7.2、docs/13 §9.1、docs/04 §3）。

依赖链：``get_db`` → ``get_current_admin``（JWT + ``ver`` + 账号 / 用户组状态，401）→ ``require_permission(code)``
（数据库用户组授权，403）→ ``get_data_scope``（受数据范围约束的路由）。FastAPI 在同一请求内缓存依赖结果，
``require_permission`` 与 ``get_data_scope`` 共用同一个 ``get_current_admin`` 结果。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from app.core import admin_permissions as ap
from app.core.database import get_db
from app.core.exceptions import BusinessError
from app.core.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.security import decode_token
from app.models import Admin, AdminGroup, Locale
from app.schemas.common import PageParams
from app.services import admin_rbac_service
from app.services.data_scope_service import DataScope, build_scope
from app.services.i18n import pick_locale

__all__ = [
    "get_db",
    "get_locale",
    "get_current_admin",
    "require_permission",
    "get_data_scope",
    "get_pagination",
]


def get_locale(
    request: Request,
    lang: str | None = Query(None, max_length=35, description="界面语言 zh-CN / en-US（优先于 Accept-Language）"),
) -> Locale:
    """``lang`` 参数 > ``Accept-Language`` > ``zh-CN``（经 ``services/i18n.normalize_locale``）。"""
    return pick_locale(lang, request.headers.get("accept-language"))


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else None


def get_current_admin(request: Request, db: Session = Depends(get_db)) -> Admin:
    token = _bearer(request)
    payload = decode_token(token) if token else None
    if not payload:
        raise BusinessError("未登录", code=401, http_status=401)
    try:
        admin_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise BusinessError("未登录", code=401, http_status=401) from None
    admin = db.get(Admin, admin_id)
    if not admin or not admin.is_active:
        raise BusinessError("管理员不存在或已禁用", code=401, http_status=401)
    try:
        version = int(payload.get("ver", -1))
    except (TypeError, ValueError):
        version = -1
    if version != admin.token_version:
        raise BusinessError("登录状态已失效，请重新登录", code=401, http_status=401)
    group = db.get(AdminGroup, admin.group_id)
    if not group or not group.is_active:
        raise BusinessError("管理员用户组已停用", code=401, http_status=401)
    request.state.admin_id = admin.id
    return admin


def require_permission(code: str) -> Callable[..., Admin]:
    """生成后台权限依赖；权限以数据库中的用户组授权为准。code 必须属于 PERMISSION_CODES。"""
    if code not in ap.PERMISSION_CODES:
        raise ValueError(f"未知权限码: {code}")           # 路由模块导入时即失败

    def _checker(request: Request, admin: Admin = Depends(get_current_admin), db: Session = Depends(get_db)) -> Admin:
        if not admin_rbac_service.has_permission(db, admin, code):
            raise BusinessError("无权执行此操作", code=403, http_status=403, data={"permission": code})
        request.state.permission_code = code      # 供审计中间件使用
        return admin

    _checker.permission_code = code  # type: ignore[attr-defined]
    _checker.__name__ = f"require_permission[{code}]"
    return _checker


def get_data_scope(
    admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
    owner_id: int | None = Query(None, gt=0, description="总后台按用户筛选（own 范围忽略）"),
) -> DataScope:
    group = db.get(AdminGroup, admin.group_id)        # get_current_admin 已加载，身份映射命中，不再查询
    return build_scope(admin, group, owner_id)        # super_admin 短路为 all；own 范围忽略 owner_id


def get_pagination(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, description=f"每页条数，默认 {DEFAULT_PAGE_SIZE}，超过 {MAX_PAGE_SIZE} 按 {MAX_PAGE_SIZE} 处理"),
) -> PageParams:
    return PageParams(page=page, page_size=min(page_size, MAX_PAGE_SIZE))
