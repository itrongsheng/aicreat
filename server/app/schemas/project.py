"""项目接口的请求 / 响应模型（docs/04 §6.7、§7.3；docs/09 §4；docs/13 §7.1、§7.2）。

请求体：``ProjectCreate``（``POST /admin/projects``）/ ``ProjectUpdate``（``PUT /admin/projects/{id}``，``owner_id`` 变化即转移
负责人）/ ``ProjectRoutesBody``（``PUT /admin/projects/{id}/routes``）。需要查库的规则（负责人范围、同负责人下 ``name`` /
``slug`` 唯一、``default_templates`` 引用、``default_platform_ids`` 启用状态、路由模型存在 / 模态 / 可用）由
``project_service`` 实现，错误项同为 ``[{loc, msg, type, input}]``。``ProjectOut`` 只用于 OpenAPI 文档与前后端对照。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import Capability, ContentFormat, ContentStyle, ProjectStatus, PromptKind, Protocol

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,79}$"
LANGUAGE_PATTERN = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,4})?$"
MAX_PLATFORM_IDS = 50
MAX_FALLBACK_MODELS = 10

ProjectName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Slug = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, pattern=SLUG_PATTERN)]
ShortText80 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
ShortText100 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
Audience = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
BrandInfo = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Language = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10, pattern=LANGUAGE_PATTERN)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
PositiveId = Annotated[int, Field(gt=0)]

# 可空文本字段：空串按 NULL 存储
NULLABLE_TEXT_FIELDS = ("industry", "audience", "brand_name", "brand_info", "description")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class _ProjectFields(_Body):
    @field_validator(*NULLABLE_TEXT_FIELDS, mode="before", check_fields=False)
    @classmethod
    def _blank(cls, value: Any) -> Any:
        return _blank_to_none(value)


class ProjectCreate(_ProjectFields):
    """创建项目（§7.3）：``owner_id`` 省略为当前用户；``own`` 范围只能是本人（400 ``owner_forbidden``），``all`` 范围须为
    启用用户（400 ``owner_unavailable``）；``language`` / ``default_style`` / ``default_format`` 省略时取
    ``generation_config`` 的默认值。"""

    name: ProjectName
    slug: Slug
    industry: ShortText80 | None = None
    audience: Audience | None = None
    brand_name: ShortText100 | None = None
    brand_info: BrandInfo | None = None
    description: Description | None = None
    language: Language | None = None
    default_style: ContentStyle | None = None
    default_format: ContentFormat | None = None
    default_templates: dict[PromptKind, PositiveId] = Field(default_factory=dict)
    default_platform_ids: list[PositiveId] = Field(default_factory=list, max_length=MAX_PLATFORM_IDS)
    owner_id: PositiveId | None = None


class ProjectUpdate(_ProjectFields):
    """编辑（只写出现的字段；非空列传 ``null`` 视为未修改，可空文本传 ``null`` / 空串清空）；``default_templates`` /
    ``default_platform_ids`` 整体替换；``owner_id`` 变化即转移负责人（仅 ``all`` 范围）。"""

    name: ProjectName | None = None
    slug: Slug | None = None
    industry: ShortText80 | None = None
    audience: Audience | None = None
    brand_name: ShortText100 | None = None
    brand_info: BrandInfo | None = None
    description: Description | None = None
    language: Language | None = None
    default_style: ContentStyle | None = None
    default_format: ContentFormat | None = None
    default_templates: dict[PromptKind, PositiveId] | None = None
    default_platform_ids: list[PositiveId] | None = Field(None, max_length=MAX_PLATFORM_IDS)
    owner_id: PositiveId | None = None

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True)
        for key in ("name", "slug", "language", "default_style", "default_format", "owner_id"):
            if key in data and data[key] is None:
                data.pop(key)
        return data


class ProjectRouteItem(_Body):
    """项目覆盖路由一项：只写 ``protocol`` / ``primary_model`` / ``fallback_models`` / ``params``（docs/08 §6.7）。"""

    capability: Capability
    primary_model: ModelId
    fallback_models: list[ModelId] = Field(default_factory=list, max_length=MAX_FALLBACK_MODELS)
    params: dict[str, Any] | None = None
    protocol: Protocol | None = None


class ProjectRoutesBody(_Body):
    """``{routes:[…]}``：对 ``capability_routes(project_id=id)`` upsert；未出现的能力删除覆盖行，空数组删除全部。"""

    routes: list[ProjectRouteItem] = Field(default_factory=list, max_length=8)


# =====================================================================
# 响应（文档用）
# =====================================================================


class OwnerBrief(BaseModel):
    id: int
    username: str
    display_name: str | None = None


class ProjectCountsOut(BaseModel):
    keywords: int
    contents: int
    links: int


class ProjectOut(BaseModel):
    id: int
    name: str
    slug: str
    industry: str | None = None
    audience: str | None = None
    brand_name: str | None = None
    brand_info: str | None = None
    description: str | None = None
    language: str
    default_style: ContentStyle
    default_format: ContentFormat
    default_templates: dict[str, int]
    default_platform_ids: list[int]
    status: ProjectStatus
    owner_id: int
    created_by: int
    created_at: str | None = None
    updated_at: str | None = None
    owner: OwnerBrief | None = None
    counts: ProjectCountsOut | None = None
    routes: list[dict[str, Any]] | None = None
