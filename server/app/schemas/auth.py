"""登录、当前用户与改密的请求 / 响应模型（docs/07-admin-rbac.md §6.1、docs/04-api-spec.md §7.1）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.models import DataScopeValue
from app.schemas.admin_rbac import AdminGroupRef, validate_password
from app.schemas.common import UtcDatetime


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ChangePasswordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    old_password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _new_password(cls, v: str) -> str:
        return validate_password(v)


class AdminProfile(BaseModel):
    """登录响应中的 ``admin`` 对象。"""

    id: int
    username: str
    display_name: str | None
    group: AdminGroupRef | None
    permissions: list[str]
    data_scope: DataScopeValue


class LoginOut(BaseModel):
    token: str
    expires_in: int
    admin: AdminProfile


class MeOut(AdminProfile):
    """``GET /admin/auth/me``：``admin`` 对象 + ``last_login_at`` + ``zhiqi_mode``。"""

    last_login_at: UtcDatetime | None = None
    zhiqi_mode: Literal["mock", "live"]


class SiteInfoOut(BaseModel):
    site_name: str
    logo_url: str
    footer: str
    support_contact: str
