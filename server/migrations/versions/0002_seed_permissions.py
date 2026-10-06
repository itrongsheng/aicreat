"""seed permissions: 权限码、4 个系统用户组（含默认 data_scope）与系统组默认权限关联

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06 00:00:00+00:00

数据来源 app/core/admin_permissions.py（docs/07-admin-rbac.md §3.6、§4、§5；docs/13-user-data-scope.md §3.2）：
- admin_permissions ← PERMISSIONS（按 code 幂等；MySQL 为 INSERT … ON DUPLICATE KEY UPDATE）；
- admin_groups ← SYSTEM_GROUPS（is_active=1，data_scope 取各组默认值；已存在的组不改写）；
- admin_group_permissions ← super_admin 写入 PERMISSION_CODES 全集，其余系统组写入 DEFAULT_GROUP_PERMISSIONS[code]。
之后新增的权限码由启动时 admin_rbac_service.ensure_rbac_seed 补齐。全部语句不依赖读取结果，可离线（--sql）生成。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql, postgresql, sqlite

from app.core.admin_permissions import (
    DEFAULT_GROUP_PERMISSIONS,
    PERMISSION_CODES,
    PERMISSIONS,
    SUPER_ADMIN_GROUP_CODE,
    SYSTEM_GROUPS,
)

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

admin_permissions = sa.table(
    "admin_permissions",
    sa.column("id", sa.BigInteger),
    sa.column("code", sa.String),
    sa.column("module", sa.String),
    sa.column("name", sa.String),
    sa.column("type", sa.String),
    sa.column("parent_code", sa.String),
    sa.column("sort", sa.Integer),
)

admin_groups = sa.table(
    "admin_groups",
    sa.column("id", sa.BigInteger),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("name_en", sa.String),
    sa.column("description", sa.String),
    sa.column("is_system", sa.Boolean),
    sa.column("is_active", sa.Boolean),
    sa.column("data_scope", sa.String),
)

admin_group_permissions = sa.table(
    "admin_group_permissions",
    sa.column("group_id", sa.BigInteger),
    sa.column("permission_id", sa.BigInteger),
)

admins = sa.table("admins", sa.column("group_id", sa.BigInteger))

PERMISSION_UPDATE_COLUMNS = ("module", "name", "type", "parent_code", "sort")


def _dialect() -> str:
    return op.get_context().dialect.name


def _upsert(table: sa.TableClause, rows: list[dict[str, Any]], key: str, update_columns: Sequence[str]) -> Any:
    """按唯一键 ``key`` 幂等插入：``update_columns`` 为空时冲突行保持不变。"""
    dialect = _dialect()
    if dialect == "mysql":
        stmt = mysql.insert(table).values(rows)
        if update_columns:
            return stmt.on_duplicate_key_update({col: stmt.inserted[col] for col in update_columns})
        return stmt.on_duplicate_key_update({key: table.c[key]})
    if dialect in ("sqlite", "postgresql"):
        module = sqlite if dialect == "sqlite" else postgresql
        stmt = module.insert(table).values(rows)
        if update_columns:
            return stmt.on_conflict_do_update(
                index_elements=[key], set_={col: stmt.excluded[col] for col in update_columns}
            )
        return stmt.on_conflict_do_nothing()
    return sa.insert(table).values(rows)


def _permission_rows() -> list[dict[str, Any]]:
    return [
        {
            "code": spec.code,
            "module": spec.module,
            "name": spec.name,
            "type": spec.type,
            "parent_code": spec.parent_code,
            "sort": spec.sort,
        }
        for spec in sorted(PERMISSIONS, key=lambda item: (item.sort, item.code))
    ]


def _group_rows() -> list[dict[str, Any]]:
    return [
        {
            "code": group["code"],
            "name": group["name"],
            "name_en": group["name_en"],
            "description": group["description"],
            "is_system": bool(group["is_system"]),
            "is_active": True,
            "data_scope": group["data_scope"],
        }
        for group in SYSTEM_GROUPS
    ]


def _group_permission_codes() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for group in SYSTEM_GROUPS:
        code = group["code"]
        codes = PERMISSION_CODES if code == SUPER_ADMIN_GROUP_CODE else DEFAULT_GROUP_PERMISSIONS.get(code, set())
        result[code] = sorted(codes)
    return result


def _link_statement(group_code: str, permission_codes: list[str]) -> Any:
    """INSERT … SELECT：按 code 关联组与权限 ID，已存在的关联行跳过（NOT EXISTS，方言无关）。"""
    g = admin_groups.alias("g")
    p = admin_permissions.alias("p")
    existing = admin_group_permissions.alias("agp")
    select = (
        sa.select(g.c.id.label("group_id"), p.c.id.label("permission_id"))
        .select_from(g.join(p, p.c.code.in_(permission_codes)))
        .where(g.c.code == group_code)
        .where(
            ~sa.exists()
            .where(existing.c.group_id == g.c.id)
            .where(existing.c.permission_id == p.c.id)
        )
    )
    return sa.insert(admin_group_permissions).from_select(["group_id", "permission_id"], select)


def upgrade() -> None:
    op.execute(_upsert(admin_permissions, _permission_rows(), "code", PERMISSION_UPDATE_COLUMNS))
    op.execute(_upsert(admin_groups, _group_rows(), "code", ()))
    for group_code, permission_codes in _group_permission_codes().items():
        if permission_codes:
            op.execute(_link_statement(group_code, permission_codes))


def downgrade() -> None:
    group_codes = sorted(group["code"] for group in SYSTEM_GROUPS)
    permission_codes = sorted(PERMISSION_CODES)
    group_ids = sa.select(admin_groups.c.id).where(admin_groups.c.code.in_(group_codes))
    permission_ids = sa.select(admin_permissions.c.id).where(admin_permissions.c.code.in_(permission_codes))
    # 外键为 RESTRICT：先删关联行（含自定义组对这些权限的授权），再删权限与系统组
    op.execute(
        sa.delete(admin_group_permissions).where(
            sa.or_(
                admin_group_permissions.c.group_id.in_(group_ids),
                admin_group_permissions.c.permission_id.in_(permission_ids),
            )
        )
    )
    op.execute(sa.delete(admin_permissions).where(admin_permissions.c.code.in_(permission_codes)))
    # 仍有管理员归属的系统组保留（admins.group_id 为 RESTRICT），随 0001 downgrade 删表一并移除
    op.execute(
        sa.delete(admin_groups)
        .where(admin_groups.c.code.in_(group_codes))
        .where(~sa.exists().where(admins.c.group_id == admin_groups.c.id))
    )
