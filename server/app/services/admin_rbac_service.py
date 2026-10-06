"""后台 RBAC：权限计算、启动 seed、用户 / 用户组管理、登录流程与审计（docs/07-admin-rbac.md）。

- 权限以数据库用户组授权为准（``super_admin`` 短路为 ``PERMISSION_CODES`` 全集），每次请求读库，不缓存；
- 所有安全规则拒绝一律 403（``data=None``），参数类错误 400（``data`` 为校验错误列表），见 07 §6.5、§10；
- ``token_version`` 在登出、本人改密、重置他人密码、启用 / 禁用、更换用户组时于同一事务递增（07 §7.4）；
- 审计 ``write_audit`` 写 ``admin_operation_logs``；审计失败只记错误日志，不回滚业务（07 §10 第 10 条）。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import admin_permissions as ap
from app.core.config import settings
from app.core.exceptions import (
    CODE_BAD_REQUEST,
    CODE_CONFLICT,
    CODE_FORBIDDEN,
    CODE_NOT_FOUND,
    CODE_UNAUTHORIZED,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.ratelimit import check_admin_login_allowed, clear_admin_login_failures, record_admin_login_failure
from app.core.security import DUMMY_PASSWORD_HASH, create_token, hash_password, verify_password
from app.models import Admin, AdminGroup, AdminGroupPermission, AdminOperationLog, AdminPermission, utcnow
from app.schemas.admin_rbac import AdminCreate, AdminUpdate, GroupCreate, GroupUpdate
from app.schemas.common import iso_utc, to_utc_naive
from app.services.data_scope_service import DataScope, effective_data_scope, scope_operation_logs

logger = logging.getLogger(__name__)

# 模块中文名（权威常量在 app.core.admin_permissions，纯数据）
MODULE_NAMES: dict[str, str] = ap.MODULE_NAMES

# 无权限码动作的审计标签（07 §3.5）
AUDIT_LABEL_LOGIN = "auth.login"
AUDIT_LABEL_LOGOUT = "auth.logout"
AUDIT_LABEL_CHANGE_PASSWORD = "auth.change_password"

_ASCII_NAME = re.compile(r"^[A-Za-z0-9 ]+$")


# =====================================================================
# 权限计算（07 §7.3）
# =====================================================================


def permission_codes(db: Session, admin: Admin) -> set[str]:
    group = db.get(AdminGroup, admin.group_id)
    if not group or not group.is_active:
        return set()
    if group.code == ap.SUPER_ADMIN_GROUP_CODE:
        return set(ap.PERMISSION_CODES)              # 短路：新增权限码无需等待 seed
    rows = db.scalars(
        select(AdminPermission.code)
        .join(AdminGroupPermission, AdminGroupPermission.permission_id == AdminPermission.id)
        .where(AdminGroupPermission.group_id == group.id)
    ).all()
    return set(rows) & ap.PERMISSION_CODES          # 数据库中的历史旧码不生效


def has_permission(db: Session, admin: Admin, code: str) -> bool:
    return code in permission_codes(db, admin)


def _spec_order() -> dict[str, tuple[int, str]]:
    return {spec.code: (spec.sort, spec.code) for spec in ap.PERMISSIONS}


def ordered_codes(codes: Iterable[str]) -> list[str]:
    """按权限表 ``sort`` 顺序排列（``login`` / ``me`` 返回的 ``permissions[]``）。"""
    order = _spec_order()
    return sorted(set(codes), key=lambda code: order.get(code, (10**9, code)))


def group_permission_codes(db: Session, group: AdminGroup) -> list[str]:
    """用户组的权限码（字母序）；``super_admin`` 固定为 ``PERMISSION_CODES`` 全集。"""
    if group.code == ap.SUPER_ADMIN_GROUP_CODE:
        return sorted(ap.PERMISSION_CODES)
    rows = db.scalars(
        select(AdminPermission.code)
        .join(AdminGroupPermission, AdminGroupPermission.permission_id == AdminPermission.id)
        .where(AdminGroupPermission.group_id == group.id)
    ).all()
    return sorted(set(rows) & ap.PERMISSION_CODES)


def permission_list() -> list[dict[str, Any]]:
    return ap.permission_list()


def permission_tree() -> list[dict[str, Any]]:
    return ap.permission_tree()


# =====================================================================
# 启动自愈 seed（07 §3.6）
# =====================================================================


def _ensure_rbac_seed_once(db: Session) -> dict[str, int]:
    stats = {"permissions_inserted": 0, "permissions_updated": 0, "groups_inserted": 0, "links_inserted": 0}

    # 1. 权限码：按 code upsert（多余旧码不删除）
    existing = {p.code: p for p in db.scalars(select(AdminPermission)).all()}
    for spec in ap.PERMISSIONS:
        row = existing.get(spec.code)
        if row is None:
            row = AdminPermission(
                code=spec.code, module=spec.module, name=spec.name, type=spec.type, parent_code=spec.parent_code, sort=spec.sort
            )
            db.add(row)
            existing[spec.code] = row
            stats["permissions_inserted"] += 1
        elif (row.module, row.name, row.type, row.parent_code, row.sort) != (
            spec.module, spec.name, spec.type, spec.parent_code, spec.sort
        ):
            row.module, row.name, row.type, row.parent_code, row.sort = (
                spec.module, spec.name, spec.type, spec.parent_code, spec.sort
            )
            stats["permissions_updated"] += 1
    db.flush()
    perm_ids = {code: row.id for code, row in existing.items()}

    # 2. 系统用户组：缺失则插入（含默认 data_scope）；super_admin 每次写回 all
    groups = {g.code: g for g in db.scalars(select(AdminGroup).where(AdminGroup.code.in_(ap.SYSTEM_GROUP_CODES))).all()}
    created: set[str] = set()
    for spec in ap.SYSTEM_GROUPS:
        group = groups.get(spec["code"])
        if group is None:
            group = AdminGroup(
                code=spec["code"], name=spec["name"], name_en=spec["name_en"], description=spec["description"],
                is_system=bool(spec["is_system"]), is_active=True, data_scope=spec["data_scope"],
            )
            db.add(group)
            groups[spec["code"]] = group
            created.add(spec["code"])
            stats["groups_inserted"] += 1
        elif spec["code"] == ap.SUPER_ADMIN_GROUP_CODE and group.data_scope != "all":
            group.data_scope = "all"
    db.flush()

    # 3. 权限关联：新建或无任何关联的系统组写默认值；super_admin 每次补齐全集
    for spec in ap.SYSTEM_GROUPS:
        code = spec["code"]
        group = groups[code]
        linked = set(
            db.scalars(select(AdminGroupPermission.permission_id).where(AdminGroupPermission.group_id == group.id)).all()
        )
        if code == ap.SUPER_ADMIN_GROUP_CODE:
            wanted = set(ap.PERMISSION_CODES)
        elif code in created or not linked:
            wanted = set(ap.DEFAULT_GROUP_PERMISSIONS.get(code, set()))
        else:
            continue                                   # 管理员的手工调整优先
        for perm_code in sorted(wanted):
            perm_id = perm_ids.get(perm_code)
            if perm_id is not None and perm_id not in linked:
                db.add(AdminGroupPermission(group_id=group.id, permission_id=perm_id))
                linked.add(perm_id)
                stats["links_inserted"] += 1
    db.commit()
    return stats


def ensure_rbac_seed(db: Session) -> dict[str, int]:
    """补齐权限码、缺失的系统组与 ``super_admin`` 缺失的权限行；幂等，并发插入冲突回滚后重试。"""
    for attempt in range(3):
        try:
            stats = _ensure_rbac_seed_once(db)
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
            continue
        if any(stats.values()):
            logger.info("RBAC seed：%s", stats)
        return stats
    return {}  # pragma: no cover


# =====================================================================
# 工具：name_en、IP 脱敏、审计
# =====================================================================


def generate_name_en(name: str, code: str) -> str:
    """``name`` 全为 ASCII 字母 / 数字 / 空格时取其 Title Case；否则 ``"Custom Group " + code[-6:]``。"""
    name = (name or "").strip()
    if name and _ASCII_NAME.match(name):
        return " ".join(word[:1].upper() + word[1:] for word in name.split())[:80]
    return f"Custom Group {code[-6:]}"


def mask_ip(value: str | None) -> str | None:
    """IPv4 末段置 0（``203.0.113.42`` → ``203.0.113.0``）；IPv6 保留前 4 组后接 ``::``；空值或非法值返回 ``None``。"""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.startswith("[") and "]" in raw:            # [IPv6]:port
        raw = raw[1 : raw.index("]")]
    elif raw.count(":") == 1:                          # IPv4:port
        raw = raw.split(":", 1)[0]
    raw = raw.split("%", 1)[0]                         # IPv6 zone id
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address):
        parts = str(ip).split(".")
        return ".".join(parts[:3] + ["0"])
    groups = ip.exploded.split(":")[:4]
    return ":".join(format(int(g, 16), "x") for g in groups) + "::"


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "")


def write_audit(db: Session, request: Request, admin: Admin, permission_code: str, action: str,
                target_type: str, target_id: str | int | None, summary: str) -> None:
    ip = mask_ip(client_ip(request))
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    db.add(AdminOperationLog(admin_id=admin.id, permission_code=permission_code, action=action, target_type=target_type,
                             target_id=str(target_id)[:64] if target_id is not None else None, summary=(summary or "")[:255],
                             request_id=request_id[:64], ip=ip or "",
                             user_agent=(request.headers.get("user-agent") or "")[:255] or None))
    db.commit()
    request.state.audit_written = True


def safe_write_audit(db: Session, request: Request, admin: Admin, permission_code: str, action: str,
                     target_type: str, target_id: str | int | None, summary: str) -> None:
    """处理函数自行写审计（登录 / 登出 / 本人改密）：失败只记错误日志，不影响业务响应。"""
    try:
        write_audit(db, request, admin, permission_code, action, target_type, target_id, summary)
    except Exception:  # noqa: BLE001
        db.rollback()
        request.state.audit_written = True
        logger.exception("后台操作审计日志写入失败")


# =====================================================================
# 序列化
# =====================================================================


def group_ref(group: AdminGroup | None) -> dict[str, Any] | None:
    if group is None:
        return None
    return {"id": group.id, "code": group.code, "name": group.name}


def admin_profile(db: Session, admin: Admin, group: AdminGroup | None = None) -> dict[str, Any]:
    """登录响应 / ``me`` 的 ``admin`` 对象：用户 + 用户组 + 最新权限码 + 数据范围。"""
    group = group or db.get(AdminGroup, admin.group_id)
    return {
        "id": admin.id,
        "username": admin.username,
        "display_name": admin.display_name,
        "group": group_ref(group),
        "permissions": ordered_codes(permission_codes(db, admin)),
        "data_scope": effective_data_scope(group),
    }


def admin_item(admin: Admin, group: AdminGroup | None) -> dict[str, Any]:
    return {
        "id": admin.id,
        "username": admin.username,
        "display_name": admin.display_name,
        "group": group_ref(group),
        "data_scope": effective_data_scope(group),
        "is_active": bool(admin.is_active),
        "last_login_at": iso_utc(admin.last_login_at),
        "created_by": admin.created_by,
        "created_at": iso_utc(admin.created_at),
    }


def _admin_count(db: Session, group_id: int, *, active_only: bool = False) -> int:
    stmt = select(func.count(Admin.id)).where(Admin.group_id == group_id)
    if active_only:
        stmt = stmt.where(Admin.is_active == True)  # noqa: E712
    return int(db.scalar(stmt) or 0)


def group_item(group: AdminGroup, admin_count: int) -> dict[str, Any]:
    return {
        "id": group.id,
        "code": group.code,
        "name": group.name,
        "name_en": group.name_en,
        "description": group.description,
        "is_system": bool(group.is_system),
        "is_active": bool(group.is_active),
        "data_scope": effective_data_scope(group),
        "admin_count": int(admin_count),
        "created_at": iso_utc(group.created_at),
    }


# =====================================================================
# 公共校验
# =====================================================================


def _forbidden(message: str) -> BusinessError:
    return BusinessError(message, code=CODE_FORBIDDEN, http_status=403)


def _get_admin(db: Session, admin_id: int) -> Admin:
    admin = db.get(Admin, admin_id)
    if admin is None:
        raise BusinessError("用户不存在", code=CODE_NOT_FOUND, http_status=404)
    return admin


def _get_group(db: Session, group_id: int) -> AdminGroup:
    group = db.get(AdminGroup, group_id)
    if group is None:
        raise BusinessError("用户组不存在", code=CODE_NOT_FOUND, http_status=404)
    return group


def _available_group(db: Session, group_id: int) -> AdminGroup:
    """分配目标组：须存在且 ``is_active=1``，否则 400 ``group_unavailable``。"""
    group = db.get(AdminGroup, group_id)
    if group is None or not group.is_active:
        raise invalid_params(field_error(["body", "group_id"], "用户组不存在或已停用", "group_unavailable", group_id))
    return group


def _super_group(db: Session) -> AdminGroup | None:
    return db.scalar(select(AdminGroup).where(AdminGroup.code == ap.SUPER_ADMIN_GROUP_CODE))


def _is_last_active_super_admin(db: Session, admin: Admin) -> bool:
    """目标是 ``super_admin`` 组内 ``is_active=1`` 的唯一管理员（同一事务 ``SELECT … FOR UPDATE`` 计数）。"""
    group = _super_group(db)
    if group is None or admin.group_id != group.id or not admin.is_active:
        return False
    ids = db.scalars(
        select(Admin.id).where(Admin.group_id == group.id, Admin.is_active == True).with_for_update()  # noqa: E712
    ).all()
    return len(ids) <= 1 and (not ids or ids[0] == admin.id)


def _find_admin_by_username(db: Session, username: str) -> Admin | None:
    # 与 utf8mb4_unicode_ci 下大小写不敏感的账号查找一致（SQLite 测试会话同样生效）
    return db.scalar(select(Admin).where(func.lower(Admin.username) == username.strip().lower()))


def _find_group_by_name(db: Session, name: str) -> AdminGroup | None:
    return db.scalar(select(AdminGroup).where(func.lower(AdminGroup.name) == name.strip().lower()))


def _commit(db: Session) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


# =====================================================================
# 用户管理（/admin/admins，07 §6.2）
# =====================================================================


def list_admins(
    db: Session,
    *,
    keyword: str | None = None,
    group_id: int | None = None,
    is_active: bool | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(Admin, AdminGroup).outerjoin(AdminGroup, AdminGroup.id == Admin.group_id)
    count_stmt = select(func.count(Admin.id))
    conditions = []
    if keyword and keyword.strip():
        kw = keyword.strip().lower()
        conditions.append(
            func.lower(Admin.username).contains(kw, autoescape=True)
            | func.lower(func.coalesce(Admin.display_name, "")).contains(kw, autoescape=True)
        )
    if group_id is not None:
        conditions.append(Admin.group_id == group_id)
    if is_active is not None:
        conditions.append(Admin.is_active == is_active)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.execute(stmt.order_by(Admin.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return [admin_item(admin, group) for admin, group in rows], total


def get_admin_detail(db: Session, admin_id: int) -> dict[str, Any]:
    admin = _get_admin(db, admin_id)
    group = db.get(AdminGroup, admin.group_id)
    item = admin_item(admin, group)
    item["permissions"] = ordered_codes(permission_codes(db, admin))
    return item


def create_admin(db: Session, actor: Admin, body: AdminCreate) -> tuple[dict[str, Any], str]:
    existing = _find_admin_by_username(db, body.username)
    if existing is not None:
        raise BusinessError("用户名已存在", code=CODE_CONFLICT, http_status=409, data={"existing_id": existing.id})
    group = _available_group(db, body.group_id)
    admin = Admin(
        username=body.username, password_hash=hash_password(body.password), display_name=body.display_name,
        group_id=group.id, is_active=body.is_active, token_version=1, created_by=actor.id,
    )
    db.add(admin)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_admin_by_username(db, body.username)
        raise BusinessError("用户名已存在", code=CODE_CONFLICT, http_status=409,
                            data={"existing_id": existing.id if existing else None}) from None
    return admin_item(admin, group), f"新增用户 {admin.username}"


def update_admin(db: Session, actor: Admin, admin_id: int, body: AdminUpdate) -> tuple[dict[str, Any], str]:
    """编辑显示名 / 更换用户组；换组递增 ``token_version``，安全规则在递增前校验（拒绝时不递增）。"""
    admin = _get_admin(db, admin_id)
    group = db.get(AdminGroup, admin.group_id)
    changes: list[str] = []
    if "group_id" in body.model_fields_set and body.group_id is not None and body.group_id != admin.group_id:
        if admin.id == actor.id:
            raise _forbidden("不能修改自己的用户组")
        new_group = _available_group(db, body.group_id)
        if group is not None and group.code == ap.SUPER_ADMIN_GROUP_CODE and _is_last_active_super_admin(db, admin):
            db.rollback()
            raise _forbidden("不能把最后一个有效的超级管理员移出超级管理员组")
        changes.append(f"用户组 {group.name if group else admin.group_id} → {new_group.name}")
        admin.group_id = new_group.id
        admin.token_version += 1
        group = new_group
    if "display_name" in body.model_fields_set and body.display_name != admin.display_name:
        changes.append("显示名称")
        admin.display_name = body.display_name
    _commit(db)
    summary = f"编辑用户 {admin.username}" + (f"：{'，'.join(changes)}" if changes else "")
    return admin_item(admin, group), summary


def set_admin_status(db: Session, actor: Admin, admin_id: int, is_active: bool) -> tuple[dict[str, Any], str]:
    """启用 / 禁用：不能禁用自己 / 最后一个有效超管；状态变化时 ``token_version += 1``。"""
    admin = _get_admin(db, admin_id)
    group = db.get(AdminGroup, admin.group_id)
    if not is_active:
        if admin.id == actor.id:
            raise _forbidden("不能禁用自己")
        if _is_last_active_super_admin(db, admin):
            db.rollback()
            raise _forbidden("不能禁用最后一个有效的超级管理员")
    if bool(admin.is_active) != is_active:
        admin.is_active = is_active
        admin.token_version += 1
    _commit(db)
    return admin_item(admin, group), f"{'启用' if is_active else '禁用'}用户 {admin.username}"


def reset_admin_password(db: Session, actor: Admin, admin_id: int, password: str) -> tuple[dict[str, Any], str]:
    """重置他人密码：``token_version += 1``；摘要只写「重置用户 xxx 的密码」，不含密码。"""
    admin = _get_admin(db, admin_id)
    group = db.get(AdminGroup, admin.group_id)
    admin.password_hash = hash_password(password)
    admin.token_version += 1
    _commit(db)
    return admin_item(admin, group), f"重置用户 {admin.username} 的密码"


# =====================================================================
# 登录态（/admin/auth，07 §6.1）
# =====================================================================


def login(db: Session, request: Request, username: str, password: str) -> dict[str, Any]:
    """登录处理顺序：① 锁定检查（429）→ ② 查找 + 校验密码（401，计数 +1）→ ③ 账号 / 用户组状态（403，不写审计）→ ④ 成功。"""
    username = (username or "").strip()
    check_admin_login_allowed(username)
    admin = _find_admin_by_username(db, username) if username else None
    if admin is None:
        verify_password(password, DUMMY_PASSWORD_HASH)    # 使两种失败耗时一致，防止用户名枚举
        valid = False
    else:
        valid = verify_password(password, admin.password_hash)
    if not valid or admin is None:
        record_admin_login_failure(username)
        raise BusinessError("用户名或密码错误", code=CODE_UNAUTHORIZED, http_status=401)
    if not admin.is_active:
        raise _forbidden("账号已禁用")
    group = db.get(AdminGroup, admin.group_id)
    if group is None or not group.is_active:
        raise _forbidden("用户组已停用")
    clear_admin_login_failures(username)
    admin.last_login_at = utcnow()
    _commit(db)
    token = create_token(admin.id, admin.token_version)
    request.state.admin_id = admin.id
    safe_write_audit(db, request, admin, AUDIT_LABEL_LOGIN, "login", "admin", admin.id, f"登录 {admin.username}")
    return {"token": token, "expires_in": settings.admin_jwt_expire_seconds, "admin": admin_profile(db, admin, group)}


def me(db: Session, admin: Admin) -> dict[str, Any]:
    data = admin_profile(db, admin)
    data["last_login_at"] = iso_utc(admin.last_login_at)
    data["zhiqi_mode"] = "mock" if settings.zhiqi_mock_mode else "live"
    return data


def logout(db: Session, request: Request, admin: Admin) -> None:
    """登出：``token_version += 1``（服务端真正失效），写审计 ``logout``。"""
    admin.token_version += 1
    _commit(db)
    safe_write_audit(db, request, admin, AUDIT_LABEL_LOGOUT, "logout", "admin", admin.id, f"登出 {admin.username}")


def change_password(db: Session, request: Request, admin: Admin, old_password: str, new_password: str) -> None:
    """本人改密：原密码错误 400 ``wrong_password``；新旧相同 400 ``same_as_old``；成功 ``token_version += 1``。"""
    if not verify_password(old_password, admin.password_hash):
        raise BusinessError("原密码错误", code=CODE_BAD_REQUEST, http_status=400,
                            data=[field_error(["body", "old_password"], "原密码错误", "wrong_password")])
    if old_password == new_password or verify_password(new_password, admin.password_hash):
        raise BusinessError("新密码不能与原密码相同", code=CODE_BAD_REQUEST, http_status=400,
                            data=[field_error(["body", "new_password"], "新密码不能与原密码相同", "same_as_old")])
    admin.password_hash = hash_password(new_password)
    admin.token_version += 1
    _commit(db)
    safe_write_audit(db, request, admin, AUDIT_LABEL_CHANGE_PASSWORD, "reset_password", "admin", admin.id,
                     f"修改本人密码 {admin.username}")


# =====================================================================
# 用户组（/admin/admin-groups，07 §6.3）
# =====================================================================


def list_groups(db: Session, *, is_active: bool | None = None) -> list[dict[str, Any]]:
    """不分页；系统组在前、``id`` 升序；每组含 ``admin_count``（含已禁用）与 ``data_scope``。"""
    stmt = select(AdminGroup)
    if is_active is not None:
        stmt = stmt.where(AdminGroup.is_active == is_active)
    groups = db.scalars(stmt.order_by(AdminGroup.is_system.desc(), AdminGroup.id.asc())).all()
    counts = dict(db.execute(select(Admin.group_id, func.count(Admin.id)).group_by(Admin.group_id)).all())
    return [group_item(g, counts.get(g.id, 0)) for g in groups]


def get_group_detail(db: Session, group_id: int) -> dict[str, Any]:
    group = _get_group(db, group_id)
    item = group_item(group, _admin_count(db, group.id))
    item["permission_codes"] = group_permission_codes(db, group)
    return item


def create_group(db: Session, actor: Admin, body: GroupCreate) -> tuple[dict[str, Any], str]:
    existing = _find_group_by_name(db, body.name)
    if existing is not None:
        raise BusinessError("用户组名称已存在", code=CODE_CONFLICT, http_status=409, data={"existing_id": existing.id})
    code = f"custom_{uuid.uuid4().hex[:12]}"
    group = AdminGroup(
        code=code, name=body.name, name_en=generate_name_en(body.name, code), description=body.description,
        is_system=False, is_active=body.is_active, data_scope=body.data_scope, created_by=actor.id,
    )
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_group_by_name(db, body.name)
        raise BusinessError("用户组名称已存在", code=CODE_CONFLICT, http_status=409,
                            data={"existing_id": existing.id if existing else None}) from None
    item = group_item(group, 0)
    item["permission_codes"] = []
    return item, f"新增用户组 {group.name}（数据范围 {group.data_scope}）"


def update_group(db: Session, actor: Admin, group_id: int, body: GroupUpdate) -> tuple[dict[str, Any], str]:
    """改名称 / 说明 / 状态 / 数据范围；``data_scope`` 修改立即生效、不递增成员 ``token_version``，摘要记录前后值。"""
    group = _get_group(db, group_id)
    fields = body.model_fields_set
    old_name = group.name
    changes: list[str] = []
    scope_change: str | None = None

    if "name" in fields and body.name is not None and body.name != group.name:
        existing = _find_group_by_name(db, body.name)
        if existing is not None and existing.id != group.id:
            raise BusinessError("用户组名称已存在", code=CODE_CONFLICT, http_status=409, data={"existing_id": existing.id})
    if "is_active" in fields and body.is_active is False and group.is_active:
        if group.is_system:
            raise _forbidden("系统用户组不可停用")
        if _admin_count(db, group.id, active_only=True) > 0:
            raise _forbidden("用户组内仍有启用中的用户，不能停用")
    if "data_scope" in fields and body.data_scope is not None:
        if group.code == ap.SUPER_ADMIN_GROUP_CODE and body.data_scope != "all":
            raise _forbidden("超级管理员组的数据范围固定为全部数据，不可修改")
        if body.data_scope != group.data_scope:
            scope_change = f"数据范围 {group.data_scope} → {body.data_scope}"
            group.data_scope = body.data_scope

    if "name" in fields and body.name is not None and body.name != group.name:
        changes.append(f"名称 {group.name} → {body.name}")
        group.name = body.name
    if "description" in fields and body.description != group.description:
        changes.append("说明")
        group.description = body.description
    if "is_active" in fields and body.is_active is not None and body.is_active != bool(group.is_active):
        changes.append("启用" if body.is_active else "停用")
        group.is_active = body.is_active
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_group_by_name(db, body.name or "")
        raise BusinessError("用户组名称已存在", code=CODE_CONFLICT, http_status=409,
                            data={"existing_id": existing.id if existing else None}) from None

    if scope_change:
        summary = f"用户组 {old_name} {scope_change}" + (f"；{'，'.join(changes)}" if changes else "")
    else:
        summary = f"编辑用户组 {old_name}" + (f"：{'，'.join(changes)}" if changes else "")
    item = group_item(group, _admin_count(db, group.id))
    item["permission_codes"] = group_permission_codes(db, group)
    return item, summary


def _permission_ids(db: Session, codes: Iterable[str]) -> dict[str, int]:
    """权限码 → ``admin_permissions.id``；缺失的合法码（seed 尚未补齐）就地插入。"""
    codes = set(codes)
    rows = {p.code: p for p in db.scalars(select(AdminPermission).where(AdminPermission.code.in_(codes))).all()}
    for code in sorted(codes - set(rows)):
        spec = ap.PERMISSIONS_BY_CODE.get(code) or next((s for s in ap.PERMISSIONS if s.code == code), None)
        if spec is None:
            continue
        row = AdminPermission(code=spec.code, module=spec.module, name=spec.name, type=spec.type,
                              parent_code=spec.parent_code, sort=spec.sort)
        db.add(row)
        rows[code] = row
    db.flush()
    return {code: row.id for code, row in rows.items()}


def validate_permission_codes(codes: Sequence[str]) -> None:
    """每个无效码一项 400 错误（``loc`` 末位为下标，``type="unknown_permission"``）。"""
    errors = [
        field_error(["body", "permission_codes", index], "未知权限码", "unknown_permission", code)
        for index, code in enumerate(codes)
        if code not in ap.PERMISSION_CODES
    ]
    if errors:
        raise invalid_params(errors)


def expand_codes(codes: Iterable[str]) -> set[str]:
    """同资源补齐 ``view`` + 跨资源 ``PERMISSION_DEPENDENCIES``（07 §4.3）。"""
    result = {code for code in codes if code in ap.PERMISSION_CODES}
    by_code = {spec.code: spec for spec in ap.PERMISSIONS}
    changed = True
    while changed:
        changed = False
        for code in list(result):
            extra: set[str] = set()
            parent = by_code[code].parent_code if code in by_code else None
            if parent:
                extra.add(parent)
            extra |= ap.PERMISSION_DEPENDENCIES.get(code, set())
            new = (extra & ap.PERMISSION_CODES) - result
            if new:
                result |= new
                changed = True
    return result


def save_group_permissions(db: Session, group_id: int, codes: Sequence[str]) -> tuple[dict[str, Any], str]:
    """覆盖保存（先删后插，同一事务）；``super_admin`` 拒绝修改；系统组补齐后不得为空；无效码整组不写入。"""
    group = _get_group(db, group_id)
    if group.code == ap.SUPER_ADMIN_GROUP_CODE:
        raise _forbidden("超级管理员组的权限固定为全部权限，不可修改")
    validate_permission_codes(codes)
    final = expand_codes(codes)
    if group.is_system and not final:
        raise _forbidden("系统用户组的权限不能为空")
    try:
        ids = _permission_ids(db, final)
        db.execute(delete(AdminGroupPermission).where(AdminGroupPermission.group_id == group.id))
        for code in sorted(final):
            db.add(AdminGroupPermission(group_id=group.id, permission_id=ids[code]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    item = group_item(group, _admin_count(db, group.id))
    item["permission_codes"] = sorted(final)
    return item, f"分配用户组 {group.name} 权限（{len(final)} 项）"


def delete_group(db: Session, group_id: int) -> str:
    """非系统且无管理员（含已禁用）才可删除；同一事务先删关联行再删组行。"""
    group = _get_group(db, group_id)
    if group.is_system:
        raise _forbidden("系统用户组不可删除")
    if _admin_count(db, group.id) > 0:
        raise _forbidden("用户组内仍有用户，不能删除")
    name = group.name
    try:
        db.execute(delete(AdminGroupPermission).where(AdminGroupPermission.group_id == group.id))
        db.delete(group)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _forbidden("用户组内仍有用户，不能删除") from None
    return f"删除用户组 {name}"


# =====================================================================
# 操作日志（/admin/admin-operation-logs，07 §6.4）
# =====================================================================


def list_operation_logs(
    db: Session,
    scope: DataScope,
    *,
    admin_id: int | None = None,
    module: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """按 ``id`` 倒序分页；``module`` 按 ``permission_code LIKE '{module}.%'``；``start``/``end`` 闭区间；
    ``own`` 范围只返回本人记录（``admin_id`` 参数被忽略），``owner_id`` 对本接口不生效。"""
    conditions = []
    if scope.scope != "own" and admin_id is not None:
        conditions.append(AdminOperationLog.admin_id == admin_id)
    if module:
        conditions.append(AdminOperationLog.permission_code.startswith(f"{module}.", autoescape=True))
    if action:
        conditions.append(AdminOperationLog.action == action)
    if target_type:
        conditions.append(AdminOperationLog.target_type == target_type)
    if start is not None:
        conditions.append(AdminOperationLog.created_at >= to_utc_naive(start))
    if end is not None:
        conditions.append(AdminOperationLog.created_at <= to_utc_naive(end))

    stmt = scope_operation_logs(select(AdminOperationLog), scope)
    count_stmt = scope_operation_logs(select(func.count(AdminOperationLog.id)), scope)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = int(db.scalar(count_stmt) or 0)
    logs = db.scalars(stmt.order_by(AdminOperationLog.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()

    admin_ids = {log.admin_id for log in logs}
    admins = {a.id: a for a in db.scalars(select(Admin).where(Admin.id.in_(admin_ids))).all()} if admin_ids else {}
    group_ids = {a.group_id for a in admins.values()}
    groups = {g.id: g for g in db.scalars(select(AdminGroup).where(AdminGroup.id.in_(group_ids))).all()} if group_ids else {}

    items = []
    for log in logs:
        admin = admins.get(log.admin_id)
        group = groups.get(admin.group_id) if admin else None
        items.append({
            "id": log.id,
            "admin": {"id": admin.id, "username": admin.username, "display_name": admin.display_name} if admin else None,
            "group_name": group.name if group else None,
            "permission_code": log.permission_code,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "summary": log.summary,
            "request_id": log.request_id or None,
            "ip": log.ip or None,
            "created_at": iso_utc(log.created_at),
        })
    return items, total
