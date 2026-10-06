"""Prompt 模板接口的请求 / 响应模型（docs/04 §6.8；docs/09 §5；docs/03 B.8）。

请求体：``TemplateCreate``（``POST /admin/prompt-templates``）/ ``TemplateUpdate``（``PUT /{id}``，``code`` / ``kind`` 不可改）/
``TemplateDuplicate``（``POST /{id}/duplicate``）/ ``TemplatePreview``（``POST /{id}/preview``）。需要查库的规则（``code``
全局唯一与 ``owned_by_other``、``project_id`` 可见性、发布前的变量引用校验）由 ``prompt_template_service`` 实现，错误项同为
``[{loc, msg, type, input}]``。``TemplateOut`` 只用于 OpenAPI 文档与前后端对照（service 直接返回 dict）。
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import Capability, PromptKind, PromptOutputFormat, PromptStatus

CODE_PATTERN = r"^[a-z][a-z0-9_]{2,79}$"
VARIABLE_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,49}$"
LANGUAGE_PATTERN = r"^[a-z]{2,3}(-[A-Za-z0-9]{2,4})?$"
MAX_PROMPT_CHARS = 20000
MAX_VARIABLES = 50

TemplateCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=CODE_PATTERN)]
TemplateName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Language = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10, pattern=LANGUAGE_PATTERN)]
PromptText = Annotated[str, StringConstraints(max_length=MAX_PROMPT_CHARS)]
UserPrompt = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PROMPT_CHARS)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PromptVariable(_Body):
    """变量声明 ``{name, label, required, default}``：内置变量可不声明；自定义变量须声明且给 ``default``
    （首版生成接口不接收自定义变量值，无默认的必填自定义变量在生成时 4221）。"""

    name: Annotated[str, StringConstraints(strip_whitespace=True, pattern=VARIABLE_NAME_PATTERN)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    required: bool = False
    default: Annotated[str, StringConstraints(max_length=4000)] | None = None


class ModelParams(_Body):
    """覆盖路由 ``params_json`` 的调用参数（§5.1）：``temperature`` / ``max_tokens`` / ``top_p`` / ``stop``。"""

    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1, le=200000)
    top_p: float | None = Field(None, gt=0, le=1)
    stop: str | list[Annotated[str, StringConstraints(min_length=1, max_length=50)]] | None = Field(None)

    @field_validator("stop")
    @classmethod
    def _stop(cls, value: Any) -> Any:
        if isinstance(value, list) and len(value) > 4:
            raise ValueError("stop 最多 4 项")
        return value


def _not_sys(code: str) -> str:
    if code.startswith("sys_"):
        raise ValueError("自定义模板代码不能以 sys_ 开头")
    return code


class TemplateCreate(_Body):
    """新建 ``draft``（``version=1``）；``capability`` 由 ``kind`` 推导，不可手填。"""

    code: TemplateCode
    kind: PromptKind
    name: TemplateName
    description: Description | None = None
    language: Language = "zh-CN"
    project_id: int = Field(0, ge=0, description="0 = 全局；>0 = 项目专属（须为可见项目）")
    system_prompt: PromptText | None = None
    user_prompt: UserPrompt
    variables: list[PromptVariable] = Field(default_factory=list, max_length=MAX_VARIABLES)
    output_format: PromptOutputFormat = "json"
    output_schema: dict[str, Any] | None = None
    model_params: ModelParams | None = None

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return _not_sys(value)

    def values(self) -> dict[str, Any]:
        data = self.model_dump()
        data["variables"] = [v.model_dump() for v in self.variables]
        data["model_params"] = self.model_params.model_dump(exclude_none=True) if self.model_params else None
        return data


class TemplateUpdate(_Body):
    """编辑（只写出现的字段）：``draft`` 原地修改；``published`` 复制为新版本草稿；``code`` / ``kind`` 创建后不可修改
    （出现即 400 ``extra_forbidden``）。``description`` / ``system_prompt`` / ``output_schema`` / ``model_params`` 可传
    ``null`` 清空。"""

    name: TemplateName | None = None
    description: Description | None = None
    language: Language | None = None
    project_id: int | None = Field(None, ge=0)
    system_prompt: PromptText | None = None
    user_prompt: UserPrompt | None = None
    variables: list[PromptVariable] | None = Field(None, max_length=MAX_VARIABLES)
    output_format: PromptOutputFormat | None = None
    output_schema: dict[str, Any] | None = None
    model_params: ModelParams | None = None

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True)
        for key in ("name", "language", "project_id", "user_prompt", "variables", "output_format"):
            if key in data and data[key] is None:      # 非空列：null 视为未修改
                data.pop(key)
        if "variables" in data:
            data["variables"] = [v.model_dump() for v in self.variables or []]
        if "model_params" in data:
            data["model_params"] = self.model_params.model_dump(exclude_none=True) if self.model_params else None
        return data


class TemplateDuplicate(_Body):
    """复制为新 code 的草稿；``project_id`` 缺省沿用源模板（从系统模板派生项目模板时指定）。"""

    code: TemplateCode
    name: TemplateName
    project_id: int | None = Field(None, ge=0)

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        return _not_sys(value)


class TemplatePreview(_Body):
    """``{variables}`` 覆盖内置变量的示例值后渲染（不调用模型）。"""

    variables: dict[str, Any] = Field(default_factory=dict)


# =====================================================================
# 响应（文档用）
# =====================================================================


class TemplateOut(BaseModel):
    id: int
    code: str
    version: int
    kind: PromptKind
    capability: Capability
    name: str
    description: str | None = None
    language: str
    project_id: int
    system_prompt: str | None = None
    user_prompt: str
    variables: list[dict[str, Any]]
    output_format: PromptOutputFormat
    output_schema: dict[str, Any] | None = None
    model_params: dict[str, Any] | None = None
    status: PromptStatus
    is_system: bool
    published_at: str | None = None
    created_by: int | None = None
    updated_by: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class PreviewOut(BaseModel):
    system_prompt: str | None = None
    user_prompt: str
