"""用户（管理员）、用户组、权限与操作日志的请求 / 响应模型（docs/07-admin-rbac.md §6）。"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.models import DataScopeValue, OperationAction
from app.schemas.common import UtcDatetime

USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,50}$"
PASSWORD_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).{8,}$", re.DOTALL)
PASSWORD_MAX_BYTES = 72  # bcrypt 上限
PASSWORD_POLICY_MESSAGE = "密码至少 8 位且须同时包含字母与数字"


def validate_password(value: str) -> str:
    """密码策略（创建、重置、本人修改共用）：≥ 8 位且同时包含字母与数字、UTF-8 编码 ≤ 72 字节。

    不满足抛 ``ValueError(PASSWORD_POLICY_MESSAGE)``；在 Pydantic 校验器中即 ``type="value_error"``，
    错误项经 ``register_exception_handlers`` 转换时不带 ``input``（不回显密码）。
    """
    if not isinstance(value, str) or not PASSWORD_PATTERN.match(value) or len(value.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    return value


Username = Annotated[str, StringConstraints(strip_whitespace=True, pattern=USERNAME_PATTERN)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
GroupName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
PermissionCode = Annotated[str, StringConstraints(max_length=100)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


# =====================================================================
# 用户（/admin/admins）
# =====================================================================


class AdminCreate(_Body):
    username: Username
    display_name: DisplayName | None = None
    password: str
    group_id: int = Field(gt=0)
    is_active: bool = True

    @field_validator("display_name", mode="before")
    @classmethod
    def _display_name(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return validate_password(v)


class AdminUpdate(_Body):
    """``{display_name?, group_id?}``：两者均可选，至少一个（``display_name`` 传空串 / null 表示清空）。"""

    display_name: DisplayName | None = None
    group_id: int | None = Field(default=None, gt=0)

    @field_validator("display_name", mode="before")
    @classmethod
    def _display_name(cls, v: Any) -> Any:
        return _blank_to_none(v)

    @model_validator(mode="after")
    def _at_least_one(self) -> AdminUpdate:
        if not ({"display_name", "group_id"} & self.model_fields_set) or (
            "group_id" in self.model_fields_set and self.group_id is None and "display_name" not in self.model_fields_set
        ):
            raise ValueError("display_name 与 group_id 至少提供一个")
        return self


class AdminStatusBody(_Body):
    is_active: bool


class AdminResetPasswordBody(_Body):
    password: str

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return validate_password(v)


class AdminGroupRef(BaseModel):
    id: int
    code: str
    name: str


class AdminOut(BaseModel):
    """用户列表项 / 详情（详情另含 ``permissions``）。"""

    id: int
    username: str
    display_name: str | None
    group: AdminGroupRef | None
    data_scope: DataScopeValue
    is_active: bool
    last_login_at: UtcDatetime | None = None
    created_by: int | None = None
    created_at: UtcDatetime
    permissions: list[str] | None = None


# =====================================================================
# 用户组（/admin/admin-groups）
# =====================================================================


class GroupCreate(_Body):
    name: GroupName
    description: Description | None = None
    is_active: bool = True
    data_scope: DataScopeValue = "own"

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, v: Any) -> Any:
        return _blank_to_none(v)


class GroupUpdate(_Body):
    """``{name?, description?, is_active?, data_scope?}``；``code`` / ``name_en`` 不可修改。"""

    name: GroupName | None = None
    description: Description | None = None
    is_active: bool | None = None
    data_scope: DataScopeValue | None = None

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, v: Any) -> Any:
        return _blank_to_none(v)


class PermissionCodesBody(_Body):
    """``{permission_codes: [...]}`` 覆盖保存；未知码由 service 逐项校验（``type="unknown_permission"``）。"""

    permission_codes: list[PermissionCode] = Field(max_length=500)


class GroupOut(BaseModel):
    id: int
    code: str
    name: str
    name_en: str
    description: str | None
    is_system: bool
    is_active: bool
    data_scope: DataScopeValue
    admin_count: int
    created_at: UtcDatetime
    permission_codes: list[str] | None = None


class PermissionOut(BaseModel):
    code: str
    module: str
    name: str
    type: Literal["menu", "action"]
    parent_code: str | None
    sort: int


# =====================================================================
# 操作日志（/admin/admin-operation-logs）
# =====================================================================


class OperationLogAdmin(BaseModel):
    id: int
    username: str
    display_name: str | None


class OperationLogOut(BaseModel):
    id: int
    admin: OperationLogAdmin | None
    group_name: str | None
    permission_code: str
    action: OperationAction
    target_type: str
    target_id: str | None
    summary: str
    request_id: str | None
    ip: str | None
    created_at: UtcDatetime


class OwnerOption(BaseModel):
    """``GET /admin/projects/owner-options`` 的一项（docs/13 §6.4）。"""

    id: int
    username: str
    display_name: str | None
    is_active: bool
    data_scope: DataScopeValue
    project_count: int
