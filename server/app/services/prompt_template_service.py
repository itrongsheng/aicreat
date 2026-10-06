"""Prompt 模板（docs/09-generation-pipeline.md §4.2、§5；docs/03 B.8；docs/04 §6.8；docs/13 §4.2、§7.1、§7.3）。

职责：

- 模板 CRUD 与版本：``published`` 不原地修改（``PUT`` 复制为同 code 新 ``draft``，``version = MAX(version)+1``）；
  ``publish`` 时同 code 旧 ``published → archived``；``archive`` 的最后一个已发布版本保护（系统模板或被项目默认引用，
  409 ``reason=last_published``）；``duplicate`` 复制为新 code 草稿；仅 ``draft`` 且非系统模板可删除；
- 可见性（docs/13 §4.2）：项目模板随项目负责人可见；全局模板的 ``published`` / ``archived`` 对所有人只读可见，``draft``
  只对创建人与总后台可见；``code`` 全局唯一，命中不可见模板时 409 ``{"existing_id":null,"reason":"owned_by_other"}``；
- 渲染 ``render(template, variables)``：``{{name}}`` 占位（允许 ``{{ name }}``）、值清洗（控制字符、``{{``/``}}`` 转义、
  ``</data>`` / ``</text>`` 闭合标签替换、按变量截断）、必填自定义变量缺失 4221；
- ``validate_output(schema, data)``：``output_schema_json`` 的 JSON Schema 固定子集校验，失败抛 ``ZhiqiError(INVALID_RESPONSE)``；
- ``resolve_template(db, kind, project_id, language)``：项目默认模板 → 该 kind 的缺省 code（全局版本），每步先匹配语言再回退
  ``zh-CN``，全部失败 404 ``data={"kind":…}``；
- 生成接口的 ``template_id?`` 校验 ``template_for_generation``（不存在 / 不可见 404 ``data=null``；可见但不合规 400
  ``kind_mismatch`` / ``not_published`` / ``project_mismatch``）与项目 ``default_templates`` 校验 ``default_templates_errors``。

``is_system`` 的继承：系统模板（seed 写入）是该 code 的一条「系统血统」。``PUT`` 复制出的新草稿 ``is_system=0``（未发布的
草稿可以删除）；草稿发布时若同 code 存在 ``is_system=1`` 的版本，新发布版本继承 ``is_system=1``，使「系统模板的最后一个
``published`` 版本禁止归档」对后续版本同样生效（``resolve_template`` 的缺省 code 回退链因此不会为空）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    CODE_TEMPLATE_VARIABLE_MISSING,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.zhiqi import text as zhiqi_text
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import ErrorCategory, TextResult
from app.models import Project, PromptKind, PromptTemplate, enum_values, utcnow
from app.schemas.common import iso_utc
from app.services import settings_service
from app.services.data_scope_service import (
    DataScope,
    get_visible,
    is_visible,
    require_project,
    scope_templates,
)

__all__ = [
    "BUILTIN_VARIABLES",
    "BUILTIN_VARIABLES_BY_KIND",
    "FALLBACK_LANGUAGE",
    "KIND_CAPABILITY",
    "PROMPT_KINDS",
    "SAMPLE_VARIABLES",
    "SYSTEM_CODE_PREFIX",
    "archive_template",
    "builtin_variables",
    "call_metadata",
    "call_params",
    "capability_for",
    "check_required_variables",
    "create_template",
    "default_code_for",
    "default_templates_errors",
    "delete_template",
    "duplicate_template",
    "get_template",
    "list_templates",
    "list_versions",
    "messages_for",
    "output_validator",
    "parse_variables",
    "preview_template",
    "project_default_templates",
    "publish_errors",
    "publish_template",
    "referenced_template_ids",
    "render",
    "referenced_variables",
    "resolve_template",
    "sanitize_value",
    "response_format_for",
    "template_for_generation",
    "template_item",
    "template_variable_errors",
    "unknown_variables",
    "update_template",
    "upsert_system_templates",
    "validate_output",
]

# =====================================================================
# 常量
# =====================================================================

PROMPT_KINDS: tuple[str, ...] = enum_values(PromptKind)
SYSTEM_CODE_PREFIX = "sys_"
FALLBACK_LANGUAGE = "zh-CN"
CUSTOM_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# kind → capability（docs/03 B.8；docs/09 §5.1）
KIND_CAPABILITY: dict[str, str] = {
    "keyword": "keyword",
    "title": "title",
    "outline": "content",
    "content": "content",
    "section": "content",
    "seo_meta": "content",
    "faq": "content",
    "image_prompt": "content",
    "rewrite": "rewrite",
    "expand": "rewrite",
    "shorten": "rewrite",
    "restyle": "rewrite",
    "geo_query": "geo_check",
    "seo_query": "seo_check",
}

_REWRITE_KINDS = ("rewrite", "expand", "shorten", "restyle")
_COMMON_VARIABLES = ("language", "industry", "audience", "brand_info")
_PARAM_VARIABLES = ("target_word_count", "section_word_count", "max_sections", "faq_count", "format")

# 内置变量（docs/09 §5.3，按适用 kind；§5.7 系统模板清单中的变量同样计入，如 rewrite 系与 image_prompt 的 style）
BUILTIN_VARIABLES_BY_KIND: dict[str, tuple[str, ...]] = {
    "keyword": (*_COMMON_VARIABLES, "seeds", "competitors", "count"),
    "title": (*_COMMON_VARIABLES, "count", "keyword", "intent", "style"),
    "outline": (*_COMMON_VARIABLES, "keyword", "intent", "style", "title", *_PARAM_VARIABLES),
    "content": (*_COMMON_VARIABLES, "keyword", "intent", "style", "title", "outline", *_PARAM_VARIABLES),
    "section": (*_COMMON_VARIABLES, "keyword", "intent", "style", "title", "outline", "section", "previous_text", *_PARAM_VARIABLES),
    **{k: (*_COMMON_VARIABLES, "keyword", "intent", "style", "title", "text", "instruction", *_PARAM_VARIABLES) for k in _REWRITE_KINDS},
    "seo_meta": (*_COMMON_VARIABLES, "keyword", "intent", "title", "body"),
    "faq": (*_COMMON_VARIABLES, "keyword", "intent", "title", "body", *_PARAM_VARIABLES),
    "image_prompt": (*_COMMON_VARIABLES, "title", "summary", "style", "usage_type"),
    "geo_query": (*_COMMON_VARIABLES, "keyword", "title", "url", "domain", "engine_name"),
    "seo_query": (*_COMMON_VARIABLES, "title", "url", "domain", "engine_name"),
}
BUILTIN_VARIABLES: frozenset[str] = frozenset(v for names in BUILTIN_VARIABLES_BY_KIND.values() for v in names)

# 空值渲染文本（§5.3）
EMPTY_PLACEHOLDERS: dict[str, str] = {
    "industry": "（未提供）",
    "audience": "（未提供）",
    "brand_info": "（未提供）",
    "instruction": "（无额外要求）",
    "outline": "（无大纲）",
}
# 值截断上限（§5.3 第 2 步）
VALUE_LIMITS: dict[str, int] = {"text": 60000, "body": 20000}
DEFAULT_VALUE_LIMIT = 4000
LIST_JOINER = "、"

# 预览的内置变量示例值（POST /{id}/preview 以请求 variables 覆盖）
SAMPLE_VARIABLES: dict[str, Any] = {
    "language": "zh-CN",
    "industry": "智能家居",
    "audience": "25~40 岁首次装修的城市家庭",
    "brand_info": "品牌：示例品牌。语气专业友好；不贬低竞品；避免绝对化用语",
    "seeds": ["智能门锁", "指纹锁"],
    "competitors": ["品牌A"],
    "count": 5,
    "keyword": "智能门锁选购",
    "intent": "commercial",
    "style": "教程：步骤清晰、可操作，使用编号列表与前置条件说明",
    "title": "智能门锁怎么选？新手必看的 6 个关键指标",
    "outline": "## 引言\n- 为什么越来越多家庭选择智能门锁\n## 核心选购指标\n- 开锁方式\n- 锁芯等级\n## 总结\n- 按预算给出建议",
    "section": "## 核心选购指标\n- 开锁方式\n- 锁芯等级\n### 锁芯等级\n- C 级锁芯的含义",
    "previous_text": "……越来越多的家庭开始关注入户安全与便利性。",
    "text": "## 核心选购指标\n智能门锁的开锁方式包括指纹、密码、刷卡与机械钥匙……",
    "instruction": "补充两个真实使用场景",
    "target_word_count": 1500,
    "section_word_count": 300,
    "max_sections": 8,
    "faq_count": 3,
    "format": "markdown",
    "body": "## 引言\n智能门锁正在成为新装修家庭的标配……\n## 核心选购指标\n……\n## 总结\n……",
    "summary": "本文从开锁方式、锁芯等级、售后等维度介绍智能门锁的选购要点。",
    "usage_type": "cover",
    "url": "https://www.example.com/articles/smart-lock-guide",
    "domain": "www.example.com",
    "engine_name": "百度",
}

TEMPLATE_NOT_FOUND = "模板不存在"
MSG_NOT_PUBLISHED = "模板不存在或未发布"
MSG_KIND_MISMATCH = "模板 kind 不匹配"
MSG_PROJECT_MISMATCH = "模板不属于该项目"
MSG_CODE_EXISTS = "模板代码已存在"
MSG_CODE_OWNED_BY_OTHER = "模板代码已被其他用户使用"
MSG_VARIABLE_MISSING = "模板必填变量缺失"
MSG_NO_TEMPLATE = "未找到可用的已发布模板"

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CLOSING_TAG_RE = re.compile(r"<\s*/\s*(data|text)\s*>", re.IGNORECASE)
_TEXT_FIELDS = ("name", "description", "language", "system_prompt", "user_prompt", "output_format")

# =====================================================================
# 工具
# =====================================================================


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _not_found(message: str = TEMPLATE_NOT_FOUND, data: Any = None) -> BusinessError:
    return BusinessError(message, code=CODE_NOT_FOUND, http_status=404, data=data)


def capability_for(kind: str) -> str:
    """``kind`` → ``capability`` 固定映射（不可手填）。"""
    try:
        return KIND_CAPABILITY[kind]
    except KeyError:
        raise ValueError(f"未知 prompt_kind: {kind}") from None


def builtin_variables(kind: str | None = None) -> tuple[str, ...]:
    """某 kind 适用的内置变量（编辑器只读 chip）；``kind=None`` 返回全部内置变量。"""
    if kind is None:
        return tuple(sorted(BUILTIN_VARIABLES))
    return BUILTIN_VARIABLES_BY_KIND.get(kind, _COMMON_VARIABLES)


def parse_variables(raw: str | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """``variables_json`` → ``[{"name","label","required","default"}]``（忽略无名项）。"""
    items = _loads(raw, []) if isinstance(raw, str) or raw is None else list(raw)
    result: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        default = item.get("default")
        result.append(
            {
                "name": name,
                "label": item.get("label") if item.get("label") is not None else name,
                "required": bool(item.get("required", False)),
                "default": None if default is None else str(default),
            }
        )
    return result


def template_item(tpl: PromptTemplate) -> dict[str, Any]:
    """模板对象（字段与 ``packages/shared`` 的 ``PromptTemplate`` 同名）。"""
    return {
        "id": tpl.id,
        "code": tpl.code,
        "version": int(tpl.version),
        "kind": tpl.kind,
        "capability": tpl.capability,
        "name": tpl.name,
        "description": tpl.description,
        "language": tpl.language,
        "project_id": int(tpl.project_id or 0),
        "system_prompt": tpl.system_prompt,
        "user_prompt": tpl.user_prompt,
        "variables": parse_variables(tpl.variables_json),
        "output_format": tpl.output_format,
        "output_schema": _loads(tpl.output_schema_json, None),
        "model_params": _loads(tpl.model_params_json, None),
        "status": tpl.status,
        "is_system": bool(tpl.is_system),
        "published_at": iso_utc(tpl.published_at),
        "created_by": tpl.created_by,
        "updated_by": tpl.updated_by,
        "created_at": iso_utc(tpl.created_at),
        "updated_at": iso_utc(tpl.updated_at),
    }


# =====================================================================
# 渲染（§5.3）
# =====================================================================


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset)):
        return not any(not _is_blank(v) for v in value)
    return False


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return LIST_JOINER.join(_to_text(v).strip() for v in value if not _is_blank(v))
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def sanitize_value(name: str, value: Any) -> str:
    """变量值清洗（§5.3 第 2 步）：去控制字符（保留换行 / 制表）→ ``{{``/``}}`` 转为全角 → ``</data>``/``</text>`` 闭合标签替换
    为 ``＜/data＞``/``＜/text＞`` → 按变量上限截断（``text`` 60000、``body`` 20000、其余 4000 字符）。空值按 §5.3 渲染占位文本。"""
    text = _to_text(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    if not text.strip() and name in EMPTY_PLACEHOLDERS:
        return EMPTY_PLACEHOLDERS[name]
    text = text.replace("{{", "｛｛").replace("}}", "｝｝")
    text = _CLOSING_TAG_RE.sub(lambda m: f"＜/{m.group(1)}＞", text)
    return text[: VALUE_LIMITS.get(name, DEFAULT_VALUE_LIMIT)]


def referenced_variables(text: str | None) -> list[str]:
    """模板文本中引用的变量名（去重保序）。"""
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text or "")))


def _missing_required(declared: Iterable[Mapping[str, Any]], provided: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    for var in declared:
        name = var["name"]
        if not var.get("required") or name in BUILTIN_VARIABLES:
            continue
        if not _is_blank(provided.get(name)):
            continue
        if var.get("default") not in (None, ""):
            continue
        missing.append(name)
    return missing


def check_required_variables(template: PromptTemplate, variables: Mapping[str, Any] | None = None) -> None:
    """API 校验阶段的必填变量检查（§5.3 第 1 步）：声明为 ``required=true``、无默认且不在内置集合的变量 → 4221
    ``data={"missing":[…]}``。生成接口首版不接收自定义变量值，调用时 ``variables`` 只含内置变量。"""
    missing = _missing_required(parse_variables(template.variables_json), dict(variables or {}))
    if missing:
        raise BusinessError(MSG_VARIABLE_MISSING, code=CODE_TEMPLATE_VARIABLE_MISSING, http_status=422, data={"missing": missing})


def render(template: PromptTemplate, variables: Mapping[str, Any] | None = None) -> tuple[str | None, str]:
    """渲染 ``(system_prompt, user_prompt)``（§5.3）。

    1. 收集变量：调用方给出的内置变量 ∪ ``variables_json`` 声明的默认值（给出的空值不覆盖声明的默认值）；
       必填自定义变量缺失 → 4221 ``data={"missing":[…]}``；
    2. 值清洗见 ``sanitize_value``；
    3. 对 ``system_prompt`` 与 ``user_prompt`` 一次性替换（替换结果不再参与匹配）；未给出值的变量渲染为空串
       （``industry`` / ``audience`` / ``brand_info`` / ``instruction`` / ``outline`` 渲染为占位文本）。
    """
    provided = dict(variables or {})
    declared = parse_variables(template.variables_json)
    check_required_variables(template, provided)
    values: dict[str, Any] = {}
    for var in declared:
        if var.get("default") not in (None, ""):
            values[var["name"]] = var["default"]
    for name, value in provided.items():
        if _is_blank(value) and name in values:
            continue
        values[name] = value
    cleaned = {name: sanitize_value(name, value) for name, value in values.items()}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in cleaned:
            return cleaned[name]
        return EMPTY_PLACEHOLDERS.get(name, "")

    system = PLACEHOLDER_RE.sub(_sub, template.system_prompt) if template.system_prompt else None
    user = PLACEHOLDER_RE.sub(_sub, template.user_prompt or "")
    return system, user


def unknown_variables(template_text: str | None, declared_names: Iterable[str]) -> list[str]:
    """文本中引用了未声明且非内置的变量名。"""
    known = BUILTIN_VARIABLES | set(declared_names)
    return [name for name in referenced_variables(template_text) if name not in known]


def template_variable_errors(
    system_prompt: str | None, user_prompt: str | None, variables: Iterable[Mapping[str, Any]] | str | None
) -> list[dict[str, Any]]:
    """未知变量的校验错误列表（每个未知变量一项，``loc`` 指向所在字段）：``publish`` 时 400，草稿保存时作为 ``warnings``。"""
    declared = [v["name"] for v in parse_variables(variables)]
    errors: list[dict[str, Any]] = []
    for field, text in (("system_prompt", system_prompt), ("user_prompt", user_prompt)):
        for name in unknown_variables(text, declared):
            errors.append(field_error(["body", field], f"未声明变量 {name}", "unknown_variable", name))
    return errors


# =====================================================================
# 输出校验（§5.5）
# =====================================================================

_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_TYPE_LABELS = {"object": "对象", "array": "数组", "string": "字符串", "integer": "整数", "number": "数字", "boolean": "布尔值", "null": "null"}


def _invalid(path: str, message: str) -> ZhiqiError:
    return ZhiqiError(ErrorCategory.INVALID_RESPONSE, f"模型输出不符合约定：{path} {message}")


def validate_output(schema: Mapping[str, Any] | None, data: Any, path: str = "$") -> None:
    """按 ``output_schema_json`` 的固定子集严格校验（无第三方依赖）：``type`` / ``properties`` / ``required`` / ``items`` /
    ``enum`` / ``minimum`` / ``maximum`` / ``maxLength`` / ``minItems`` / ``maxItems``；任一约束不满足抛
    ``ZhiqiError(INVALID_RESPONSE)``。未列出的关键字忽略。"""
    if not isinstance(schema, Mapping) or not schema:
        return
    expected = schema.get("type")
    if expected is not None:
        types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(data) for t in types):
            label = "/".join(_TYPE_LABELS.get(t, str(t)) for t in types)
            raise _invalid(path, f"应为{label}")
    if "enum" in schema and isinstance(schema["enum"], list) and data not in schema["enum"]:
        raise _invalid(path, "取值不在枚举范围内")
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if schema.get("minimum") is not None and data < schema["minimum"]:
            raise _invalid(path, f"不能小于 {schema['minimum']}")
        if schema.get("maximum") is not None and data > schema["maximum"]:
            raise _invalid(path, f"不能大于 {schema['maximum']}")
    if isinstance(data, str) and schema.get("maxLength") is not None and len(data) > int(schema["maxLength"]):
        raise _invalid(path, f"长度不能超过 {schema['maxLength']}")
    if isinstance(data, list):
        if schema.get("minItems") is not None and len(data) < int(schema["minItems"]):
            raise _invalid(path, f"至少需要 {schema['minItems']} 项")
        if schema.get("maxItems") is not None and len(data) > int(schema["maxItems"]):
            raise _invalid(path, f"最多允许 {schema['maxItems']} 项")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(data):
                validate_output(items, item, f"{path}[{index}]")
    if isinstance(data, dict):
        for key in schema.get("required") or []:
            if key not in data:
                raise _invalid(f"{path}.{key}", "缺少必填字段")
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            for key, sub in properties.items():
                if key in data:
                    validate_output(sub, data[key], f"{path}.{key}")


def output_validator(template: PromptTemplate) -> Callable[[TextResult], Any] | None:
    """``complete_text(validator=…)``：``output_format=json`` 时 ``extract_json`` + ``validate_output``，返回解析结果；
    其它输出格式返回 ``None``（不校验）。"""
    if template.output_format != "json":
        return None
    schema = _loads(template.output_schema_json, None)

    def _validate(result: TextResult) -> Any:
        data = zhiqi_text.extract_json(result.text)
        if isinstance(schema, Mapping):
            validate_output(schema, data)
        return data

    return _validate


def response_format_for(template: PromptTemplate) -> str:
    """``output_format=json`` → ``"json"``，否则 ``"text"``（三协议映射见 08）。"""
    return "json" if template.output_format == "json" else "text"


def call_params(template: PromptTemplate, dynamic: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """一次文本调用中模板层的 ``params``（§5.8；路由 ``params_json`` 由网关在下层合并）：模板 ``model_params_json``
    （``temperature`` / ``max_tokens`` / ``top_p`` / ``stop``）+ 场景动态值 ``dynamic``（如正文 ``max_tokens``，§8.3）。
    模板已设置的键优先于动态值（§8.3「模板 ``model_params_json.max_tokens`` 非空时优先于上表动态值」）。"""
    params = _loads(template.model_params_json, {}) or {}
    if not isinstance(params, dict):
        params = {}
    params = {k: v for k, v in params.items() if k in ("temperature", "max_tokens", "top_p", "stop") and v is not None}
    for key, value in (dynamic or {}).items():
        if value is not None and key not in params:
            params[key] = value
    return params


def messages_for(system_prompt: str | None, user_prompt: str) -> list[dict[str, str]]:
    """渲染结果 → ``messages``（system 可空）。"""
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})
    return messages


def call_metadata(template: PromptTemplate, operation: str) -> dict[str, Any]:
    """``complete_text(metadata=…)``：供 Mock 识别模板种类（不发送上游）。"""
    return {"operation": operation, "template_code": template.code, "template_kind": template.kind, "template_id": template.id}


# =====================================================================
# 模板解析（§4.2、§5.4）
# =====================================================================


def default_code_for(db: Session, kind: str) -> str | None:
    """kind 唯一的缺省 code（不跨 kind 回退）：

    ``keyword`` / ``title`` → ``generation_config.<kind>.default_template_code``；``outline`` / ``content`` / ``section`` /
    ``seo_meta`` / ``faq`` → ``generation_config.content.template_codes.<kind>``；``rewrite`` 系 →
    ``generation_config.rewrite.template_codes.<kind>``；``image_prompt`` → ``media_config.image.image_prompt_template_code``；
    ``geo_query`` / ``seo_query`` → ``sys_geo_query`` / ``sys_seo_query``。
    """
    if kind in ("geo_query", "seo_query"):
        return f"{SYSTEM_CODE_PREFIX}{kind}"
    if kind == "image_prompt":
        cfg = settings_service.get_config(db, "media_config")
        return settings_service.get_path(cfg, "image.image_prompt_template_code") or None
    cfg = settings_service.get_config(db, "generation_config")
    if kind in ("keyword", "title"):
        path = f"{kind}.default_template_code"
    elif kind in ("outline", "content", "section", "seo_meta", "faq"):
        path = f"content.template_codes.{kind}"
    elif kind in _REWRITE_KINDS:
        path = f"rewrite.template_codes.{kind}"
    else:
        return None
    return settings_service.get_path(cfg, path) or None


def _language_ok(tpl: PromptTemplate, language: str) -> int | None:
    """语言匹配优先级：同语言 0、回退 ``zh-CN`` 1、不匹配 ``None``。"""
    if tpl.language == language:
        return 0
    if tpl.language == FALLBACK_LANGUAGE:
        return 1
    return None


def _published_of_code(db: Session, code: str, *, kind: str, project_ids: Sequence[int], language: str) -> PromptTemplate | None:
    """该 code 当前 ``published`` 版本（``kind`` 一致、``project_id`` 在给定集合内），按语言优先级挑选（同语言 → ``zh-CN``）。"""
    rows = db.scalars(
        select(PromptTemplate).where(
            PromptTemplate.code == code,
            PromptTemplate.status == "published",
            PromptTemplate.kind == kind,
            PromptTemplate.project_id.in_(list(project_ids)),
        )
    ).all()
    ranked = [(rank, -int(r.version), r) for r in rows if (rank := _language_ok(r, language)) is not None]
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1]))
    return ranked[0][2]


def project_default_templates(project: Project | None) -> dict[str, int]:
    """``projects.default_templates_json`` → ``{kind: template_id}``（忽略非法项）。"""
    if project is None:
        return {}
    raw = _loads(project.default_templates_json, {})
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw.items():
        if key in KIND_CAPABILITY and isinstance(value, int) and not isinstance(value, bool) and value > 0:
            result[key] = value
    return result


def resolve_template(db: Session, kind: str, project_id: int | None, language: str | None = None) -> PromptTemplate:
    """三步解析（§4.2、§5.4、docs/03 B.7），不受请求者数据范围影响（docs/13 §7.3）：

    ① ``projects.default_templates_json[kind]``：该 ID 仍为 ``published`` 则使用；因同 code 发布新版本而 ``archived`` 时改取该
       code 当前的 ``published`` 版本（``project_id ∈ {0, 该项目}``）；
    ② ① 未配置或该 code 已无 ``published`` 版本：取该 kind 的缺省 code（``default_code_for``）的全局（``project_id=0``）
       ``published`` 版本；
    两步都先匹配 ``language``（缺省 ``projects.language``），找不到同语言的再回退 ``zh-CN``；仍为空 → 404
    ``CODE_NOT_FOUND``，``data={"kind": kind}``。
    """
    if kind not in KIND_CAPABILITY:
        raise ValueError(f"未知 prompt_kind: {kind}")
    project = db.get(Project, project_id) if project_id else None
    language = language or (project.language if project is not None else None) or FALLBACK_LANGUAGE
    if project is not None:
        template_id = project_default_templates(project).get(kind)
        configured = db.get(PromptTemplate, template_id) if template_id else None
        if configured is not None and configured.kind == kind and int(configured.project_id or 0) in (0, project.id):
            if configured.status == "published" and _language_ok(configured, language) is not None:
                return configured
            found = _published_of_code(db, configured.code, kind=kind, project_ids=(0, project.id), language=language)
            if found is not None:
                return found
    code = default_code_for(db, kind)
    if code:
        found = _published_of_code(db, code, kind=kind, project_ids=(0,), language=language)
        if found is not None:
            return found
    raise _not_found(MSG_NO_TEMPLATE, {"kind": kind})


def _template_ref_error(tpl: PromptTemplate, kind: str, project_id: int | None, loc: Sequence[str | int], input_value: Any) -> dict[str, Any] | None:
    if tpl.status != "published":
        return field_error(loc, MSG_NOT_PUBLISHED, "not_published", input_value)
    if tpl.kind != kind:
        return field_error(loc, MSG_KIND_MISMATCH, "kind_mismatch", input_value)
    if int(tpl.project_id or 0) not in (0, int(project_id or 0)):
        return field_error(loc, MSG_PROJECT_MISMATCH, "project_mismatch", input_value)
    return None


def template_for_generation(
    db: Session,
    scope: DataScope,
    *,
    kind: str,
    project: Project,
    template_id: int | None = None,
    loc: Sequence[str | int] = ("body", "template_id"),
) -> PromptTemplate:
    """生成接口取模板：``template_id`` 显式指定时跳过解析——不存在或不可见 → 404 ``data=null``（docs/13 §8）；可见但
    ``status≠published`` / ``kind`` 不一致 / ``project_id ∉ {0, 当前项目}`` → 400（``not_published`` / ``kind_mismatch`` /
    ``project_mismatch``）；未指定时 ``resolve_template(kind, project.id, project.language)``。"""
    if template_id is None:
        return resolve_template(db, kind, project.id, project.language)
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    error = _template_ref_error(tpl, kind, project.id, list(loc), template_id)
    if error:
        raise invalid_params(error)
    return tpl


def default_templates_errors(
    db: Session, scope: DataScope, mapping: Mapping[str, Any] | None, project_id: int | None
) -> list[dict[str, Any]]:
    """项目 ``default_templates`` 校验（docs/04 §7.3、docs/13 §7.1）：值须为该 kind 的可见 ``published`` 模板且
    ``project_id ∈ {0, 该项目}``（新建项目时只能引用全局模板）；模板不存在、不可见或未发布均为 ``not_published``。"""
    errors: list[dict[str, Any]] = []
    for kind, template_id in (mapping or {}).items():
        loc = ["body", "default_templates", kind]
        tpl = db.get(PromptTemplate, template_id) if template_id else None
        if tpl is None or not is_visible(db, scope, tpl):
            errors.append(field_error(loc, MSG_NOT_PUBLISHED, "not_published", template_id))
            continue
        error = _template_ref_error(tpl, kind, project_id, loc, template_id)
        if error:
            errors.append(error)
    return errors


def referenced_template_ids(db: Session) -> set[int]:
    """全部项目 ``default_templates_json`` 引用的模板 ID。"""
    ids: set[int] = set()
    for raw in db.scalars(select(Project.default_templates_json).where(Project.default_templates_json.is_not(None))).all():
        data = _loads(raw, {})
        if isinstance(data, dict):
            ids.update(v for v in data.values() if isinstance(v, int) and not isinstance(v, bool))
    return ids


# =====================================================================
# 查询
# =====================================================================


def _filter_conditions(
    *, kind: str | None, status: str | None, project_id: int | None, include_global: bool, keyword: str | None
) -> list[Any]:
    conditions: list[Any] = []
    if kind:
        conditions.append(PromptTemplate.kind == kind)
    if status:
        conditions.append(PromptTemplate.status == status)
    if project_id is not None:
        if project_id > 0 and include_global:
            conditions.append(PromptTemplate.project_id.in_((0, project_id)))
        else:
            conditions.append(PromptTemplate.project_id == project_id)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        conditions.append(or_(PromptTemplate.code.like(like), PromptTemplate.name.like(like)))
    return conditions


def list_templates(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    kind: str | None = None,
    status: str | None = None,
    project_id: int | None = None,
    include_global: bool = False,
    keyword: str | None = None,
    all_versions: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """分页（``created_at DESC, id DESC``）：只含可见版本（docs/13 §4.2）；默认每个 code 只返回满足筛选条件的最新可见版本，
    ``all_versions`` 返回全部可见版本。``project_id=0`` 只看全局模板，``>0`` 看该项目模板（``include_global`` 时含全局）。"""
    conditions = _filter_conditions(kind=kind, status=status, project_id=project_id, include_global=include_global, keyword=keyword)
    base = scope_templates(select(PromptTemplate), scope).where(*conditions)
    if all_versions:
        stmt = base
        total = int(db.scalar(scope_templates(select(func.count(PromptTemplate.id)), scope).where(*conditions)) or 0)
    else:
        latest = (
            scope_templates(select(PromptTemplate.code, func.max(PromptTemplate.version).label("max_version")), scope)
            .where(*conditions)
            .group_by(PromptTemplate.code)
            .subquery()
        )
        stmt = select(PromptTemplate).join(
            latest, and_(PromptTemplate.code == latest.c.code, PromptTemplate.version == latest.c.max_version)
        )
        total = int(db.scalar(select(func.count()).select_from(latest)) or 0)
    rows = db.scalars(
        stmt.order_by(PromptTemplate.created_at.desc(), PromptTemplate.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return [template_item(r) for r in rows], total


def get_template(db: Session, scope: DataScope, template_id: int) -> dict[str, Any]:
    return template_item(get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND))


def list_versions(db: Session, scope: DataScope, template_id: int) -> list[dict[str, Any]]:
    """同 code 的全部可见版本（``version DESC``；他人的全局草稿不列出）。"""
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    stmt = scope_templates(select(PromptTemplate), scope).where(PromptTemplate.code == tpl.code)
    rows = db.scalars(stmt.order_by(PromptTemplate.version.desc())).all()
    return [template_item(r) for r in rows]


# =====================================================================
# 写入
# =====================================================================


def _ensure_code_available(db: Session, scope: DataScope, code: str) -> None:
    """``code`` 全局唯一：命中可见模板 → 409 ``{"existing_id": 最新可见版本}``；只命中不可见模板 → 409
    ``{"existing_id": null, "reason": "owned_by_other"}``（不暴露对象 ID，docs/13 §8）。"""
    rows = db.scalars(select(PromptTemplate).where(PromptTemplate.code == code)).all()
    if not rows:
        return
    visible = [r for r in rows if is_visible(db, scope, r)]
    if visible:
        latest = max(visible, key=lambda r: int(r.version))
        raise _conflict(MSG_CODE_EXISTS, {"existing_id": latest.id})
    raise _conflict(MSG_CODE_OWNED_BY_OTHER, {"existing_id": None, "reason": "owned_by_other"})


def _check_project_ref(db: Session, scope: DataScope, project_id: int) -> None:
    """``project_id`` 须为 ``0`` 或可见项目（不可见与不存在相同，404）。"""
    if int(project_id or 0) > 0:
        require_project(db, scope, int(project_id))


def _normalized_variables(variables: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for var in variables or []:
        name = str(var.get("name") or "").strip()
        default = var.get("default")
        result.append(
            {
                "name": name,
                "label": (str(var.get("label")).strip() if var.get("label") is not None else "") or name,
                "required": bool(var.get("required", False)),
                "default": None if default is None else str(default),
            }
        )
    return result


def _variable_errors(variables: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, var in enumerate(variables):
        name = var["name"]
        if name in seen:
            errors.append(field_error(["body", "variables", index, "name"], f"变量 {name} 重复声明", "duplicate_variable", name))
        seen.add(name)
    return errors


def _apply_values(tpl: PromptTemplate, values: Mapping[str, Any]) -> None:
    for key in _TEXT_FIELDS:
        if key in values:
            value = values[key]
            if key in ("description", "system_prompt") and isinstance(value, str) and not value.strip():
                value = None
            setattr(tpl, key, value)
    if "project_id" in values:
        tpl.project_id = int(values["project_id"] or 0)
    if "variables" in values:
        tpl.variables_json = _dumps(_normalized_variables(values["variables"]))
    if "output_schema" in values:
        schema = values["output_schema"]
        tpl.output_schema_json = _dumps(schema) if schema not in (None, {}) else None
    if "model_params" in values:
        params = values["model_params"]
        params = {k: v for k, v in (params or {}).items() if v is not None}
        tpl.model_params_json = _dumps(params) if params else None


def _warnings(tpl: PromptTemplate) -> list[dict[str, Any]]:
    return template_variable_errors(tpl.system_prompt, tpl.user_prompt, tpl.variables_json)


def _flush_new(db: Session, tpl: PromptTemplate) -> None:
    db.add(tpl)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _conflict(MSG_CODE_EXISTS, {"existing_id": None}) from None


def create_template(db: Session, scope: DataScope, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/prompt-templates``：新建 ``draft``（``version=1``，``capability`` 由 ``kind`` 推导）；``project_id`` 须为
    ``0`` 或可见项目；``code`` 冲突 409（被不可见模板占用时 ``owned_by_other``）。未知变量只作为 ``warnings`` 返回。"""
    code = str(values["code"]).strip()
    kind = str(values["kind"])
    variables = _normalized_variables(values.get("variables"))
    errors = _variable_errors(variables)
    if errors:
        raise invalid_params(errors)
    _check_project_ref(db, scope, int(values.get("project_id") or 0))
    _ensure_code_available(db, scope, code)
    tpl = PromptTemplate(
        code=code,
        version=1,
        kind=kind,
        capability=capability_for(kind),
        name="",
        language=FALLBACK_LANGUAGE,
        project_id=0,
        user_prompt="",
        output_format="json",
        status="draft",
        is_system=False,
        created_by=admin_id,
        updated_by=admin_id,
    )
    _apply_values(tpl, {**values, "variables": variables})
    _flush_new(db, tpl)
    db.commit()
    item = template_item(tpl)
    item["warnings"] = _warnings(tpl)
    return item


def _next_version(db: Session, code: str) -> int:
    return int(db.scalar(select(func.max(PromptTemplate.version)).where(PromptTemplate.code == code)) or 0) + 1


def update_template(db: Session, scope: DataScope, template_id: int, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``PUT /admin/prompt-templates/{id}``：``draft`` 原地修改；``published`` 不原地修改，复制为同 code 新 ``draft``
    （``version = MAX(version)+1``，``created_by`` 为本人）并返回新对象（``id`` 不同）；``archived`` → 409 ``current_status``。
    ``code`` / ``kind`` 创建后不可修改。"""
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    if tpl.status == "archived":
        raise _conflict("已归档的模板不可修改", {"current_status": tpl.status})
    if "variables" in values:
        variables = _normalized_variables(values.get("variables"))
        errors = _variable_errors(variables)
        if errors:
            raise invalid_params(errors)
        values = {**values, "variables": variables}
    if "project_id" in values and int(values.get("project_id") or 0) != int(tpl.project_id or 0):
        _check_project_ref(db, scope, int(values.get("project_id") or 0))
    if tpl.status == "draft":
        _apply_values(tpl, values)
        tpl.updated_by = admin_id
        db.commit()
        item = template_item(tpl)
        item["warnings"] = _warnings(tpl)
        return item
    # published：复制为新版本草稿
    draft = PromptTemplate(
        code=tpl.code,
        version=_next_version(db, tpl.code),
        kind=tpl.kind,
        capability=tpl.capability,
        name=tpl.name,
        description=tpl.description,
        language=tpl.language,
        project_id=int(tpl.project_id or 0),
        system_prompt=tpl.system_prompt,
        user_prompt=tpl.user_prompt,
        variables_json=tpl.variables_json,
        output_format=tpl.output_format,
        output_schema_json=tpl.output_schema_json,
        model_params_json=tpl.model_params_json,
        status="draft",
        is_system=False,
        created_by=admin_id,
        updated_by=admin_id,
    )
    _apply_values(draft, values)
    _flush_new(db, draft)
    db.commit()
    item = template_item(draft)
    item["warnings"] = _warnings(draft)
    item["source_id"] = tpl.id
    return item


def publish_errors(tpl: PromptTemplate) -> list[dict[str, Any]]:
    """``publish`` 前校验（§5.6）：``user_prompt`` 非空、变量引用合法、``output_format=json`` 时 ``output_schema_json``
    可解析为 JSON 对象。``loc`` 以 ``body`` 开头，字段名与新建 / 编辑请求体一致。"""
    errors: list[dict[str, Any]] = []
    if not (tpl.user_prompt or "").strip():
        errors.append(field_error(["body", "user_prompt"], "user_prompt 不能为空", "missing", None))
    errors.extend(template_variable_errors(tpl.system_prompt, tpl.user_prompt, tpl.variables_json))
    if tpl.output_format == "json" and tpl.output_schema_json:
        try:
            schema = json.loads(tpl.output_schema_json)
        except (TypeError, ValueError):
            schema = None
        if not isinstance(schema, dict):
            errors.append(field_error(["body", "output_schema"], "output_schema 不是合法的 JSON 对象", "invalid_schema", None))
    return errors


def publish_template(db: Session, scope: DataScope, template_id: int, *, admin_id: int) -> dict[str, Any]:
    """``draft → published``；同 code 旧 ``published → archived``（同一事务，保证同 code 只有一个 ``published``）。
    同 code 存在系统版本时新发布版本继承 ``is_system=1``（见模块说明）。"""
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    if tpl.status != "draft":
        raise _conflict("只有草稿可以发布", {"current_status": tpl.status})
    errors = publish_errors(tpl)
    if errors:
        raise invalid_params(errors)
    siblings = db.scalars(
        select(PromptTemplate).where(PromptTemplate.code == tpl.code, PromptTemplate.id != tpl.id).with_for_update()
    ).all()
    now = utcnow()
    for other in siblings:
        if other.status == "published":
            other.status = "archived"
            other.updated_by = admin_id
    if any(other.is_system for other in siblings):
        tpl.is_system = True
    tpl.status = "published"
    tpl.published_at = now
    tpl.updated_by = admin_id
    db.commit()
    return template_item(tpl)


def archive_template(db: Session, scope: DataScope, template_id: int, *, admin_id: int) -> dict[str, Any]:
    """``published → archived``；同 code 无其它 ``published`` 且（系统模板或该 code 任一版本被项目 ``default_templates``
    引用）→ 409 ``{"current_status":"published","reason":"last_published"}``。"""
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    if tpl.status != "published":
        raise _conflict("只有已发布的模板可以归档", {"current_status": tpl.status})
    other_published = db.scalar(
        select(func.count(PromptTemplate.id)).where(
            PromptTemplate.code == tpl.code, PromptTemplate.status == "published", PromptTemplate.id != tpl.id
        )
    )
    if not other_published:
        code_ids = set(db.scalars(select(PromptTemplate.id).where(PromptTemplate.code == tpl.code)).all())
        if tpl.is_system or code_ids & referenced_template_ids(db):
            raise _conflict("最后一个已发布版本不可归档", {"current_status": "published", "reason": "last_published"})
    tpl.status = "archived"
    tpl.updated_by = admin_id
    db.commit()
    return template_item(tpl)


def duplicate_template(db: Session, scope: DataScope, template_id: int, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /{id}/duplicate``：``{code, name, project_id?}`` 复制为新 code 的 ``draft``（``version=1``、非系统）；
    ``project_id`` 缺省沿用源模板（从系统模板派生项目模板时指定，须为 ``0`` 或可见项目）。"""
    source = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    code = str(values["code"]).strip()
    project_id = int(values["project_id"]) if values.get("project_id") is not None else int(source.project_id or 0)
    _check_project_ref(db, scope, project_id)
    _ensure_code_available(db, scope, code)
    tpl = PromptTemplate(
        code=code,
        version=1,
        kind=source.kind,
        capability=source.capability,
        name=str(values["name"]).strip(),
        description=source.description,
        language=source.language,
        project_id=project_id,
        system_prompt=source.system_prompt,
        user_prompt=source.user_prompt,
        variables_json=source.variables_json,
        output_format=source.output_format,
        output_schema_json=source.output_schema_json,
        model_params_json=source.model_params_json,
        status="draft",
        is_system=False,
        created_by=admin_id,
        updated_by=admin_id,
    )
    _flush_new(db, tpl)
    db.commit()
    item = template_item(tpl)
    item["warnings"] = _warnings(tpl)
    return item


def preview_template(db: Session, scope: DataScope, template_id: int, variables: Mapping[str, Any] | None) -> dict[str, Any]:
    """``POST /{id}/preview``：内置变量取示例值（``SAMPLE_VARIABLES``），以请求 ``variables`` 覆盖后渲染，不调用模型；
    必填变量缺失 4221。"""
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    merged = {name: SAMPLE_VARIABLES[name] for name in builtin_variables(tpl.kind) if name in SAMPLE_VARIABLES}
    merged.update(dict(variables or {}))
    system, user = render(tpl, merged)
    return {"system_prompt": system, "user_prompt": user}


def delete_template(db: Session, scope: DataScope, template_id: int) -> None:
    """仅 ``draft`` 且非系统模板可物理删除，否则 409（``draft`` 不会被项目默认引用，无需清理）。"""
    tpl = get_visible(db, scope, PromptTemplate, template_id, message=TEMPLATE_NOT_FOUND)
    if tpl.status != "draft":
        raise _conflict("只有草稿可以删除", {"current_status": tpl.status})
    if tpl.is_system:
        raise _conflict("系统模板不可删除", {"current_status": tpl.status, "reason": "system_template"})
    db.delete(tpl)
    db.commit()


# =====================================================================
# 系统模板 seed（seeds/seed.py 调用）
# =====================================================================

_SYSTEM_FIELDS = ("kind", "name", "description", "system_prompt", "user_prompt", "output_format")


def upsert_system_templates(db: Session, specs: Iterable[Mapping[str, Any]], *, admin_id: int) -> dict[str, int]:
    """幂等 upsert 系统模板（``project_id=0``、``is_system=1``、``language=zh-CN``、``version=1``）。

    - ``(code, version=1)`` 不存在 → 插入；同 code 已有其它 ``published`` 版本（运营发布过新版本）时以 ``archived`` 插入，
      否则 ``published``（写 ``published_at``）；
    - 已存在 → 以 seed 原文覆盖内容字段（``kind``/``capability``/名称/提示词/变量/输出格式/schema/参数），``project_id=0``、
      ``is_system=1``、``language=zh-CN``；**不改** ``status``（已被新版本取代而归档的 v1 保持归档），只在该 code 没有任何
      ``published`` 版本且 v1 非草稿时把 v1 恢复为 ``published``，保证缺省解析链不为空。
    返回 ``{"created": n, "updated": m}``。
    """
    created = updated = 0
    now = utcnow()
    for spec in specs:
        code = str(spec["code"])
        kind = str(spec["kind"])
        values = {
            "kind": kind,
            "capability": capability_for(kind),
            "name": spec["name"],
            "description": spec.get("description"),
            "system_prompt": spec.get("system_prompt"),
            "user_prompt": spec["user_prompt"],
            "variables_json": _dumps(_normalized_variables(spec.get("variables") or [])),
            "output_format": spec.get("output_format", "text"),
            "output_schema_json": _dumps(spec["output_schema"]) if spec.get("output_schema") else None,
            "model_params_json": _dumps(spec["model_params"]) if spec.get("model_params") else None,
            "project_id": 0,
            "is_system": True,
            "language": FALLBACK_LANGUAGE,
        }
        row = db.scalar(select(PromptTemplate).where(PromptTemplate.code == code, PromptTemplate.version == 1))
        published = db.scalar(
            select(PromptTemplate.id).where(PromptTemplate.code == code, PromptTemplate.status == "published").limit(1)
        )
        if row is None:
            status = "archived" if published else "published"
            row = PromptTemplate(
                code=code, version=1, status=status, published_at=now, created_by=admin_id, updated_by=admin_id, **values
            )
            db.add(row)
            created += 1
        else:
            changed = False
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    changed = True
            if not published and row.status != "draft" and row.status != "published":
                row.status = "published"
                row.published_at = row.published_at or now
                changed = True
            if changed:
                row.updated_by = admin_id
                updated += 1
    db.commit()
    return {"created": created, "updated": updated}
