"""项目（docs/03 B.7、「数据归属与负责人转移」「删除规则」；docs/04 §6.7、§7.3；docs/09 §4；docs/13 §6.3、§7.1、§7.2）。

- CRUD：名称与 slug 在同一负责人下唯一（409 ``existing_id``）；负责人规则（创建缺省本人；``own`` 范围只能是本人
  ``owner_forbidden``；``all`` 范围指定 / 转移须为启用用户 ``owner_unavailable``）；创建、删除、转移提交后清 ``cache:stats:*``；
- ``default_templates`` 只能引用可见的、该 kind 的 ``published`` 模板且 ``project_id ∈ {0, 该项目}``；
  ``default_platform_ids`` 须为启用平台；
- 归档 / 恢复：``active ⇄ archived``（非法流转 409 ``current_status``）；
- 删除：仅 ``archived`` 且无关键词 / 标题 / 内容 / 链接 / 素材 / 批次；同一事务删除项目专属模板与路由覆盖、``ai_tasks`` /
  ``alerts`` 的 ``project_id`` 置 NULL（``daily_stats`` 项目行保留），提交后清 ``cache:routes:*`` 与 ``cache:stats:*``；
- ``save_project_routes``：运营侧项目覆盖路由（docs/03 B.17、docs/08 §6.7、§11.4 第 4 条）；
- ``overview``：委托 ``stats_service.overview``（第 6 步实现）；缺失时返回按库实时计算的最小 KPI 结构（``meta.placeholder``）。

所有读写函数以 ``scope`` 为必填参数（docs/13 §9.3），目标项目不可见与不存在一律 404。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import after_commit
from app.core.exceptions import CODE_CONFLICT, BusinessError, field_error, invalid_params
from app.core.redis import cache_delete_prefix
from app.core.zhiqi.types import MODALITY_OF
from app.core.zhiqi.types import Capability as CapabilityEnum
from app.models import (
    Admin,
    AiModel,
    AiTask,
    Alert,
    CapabilityRoute,
    Content,
    GenerationBatch,
    Keyword,
    MediaAsset,
    Project,
    PromptTemplate,
    PublishLink,
    PublishPlatform,
    Title,
)
from app.schemas.common import iso_utc
from app.services import ai_catalog_service, ai_gateway_service, prompt_template_service, settings_service, stats_service
from app.services.data_scope_service import DataScope, get_visible, scope_projects

logger = logging.getLogger(__name__)

__all__ = [
    "STATS_CACHE_PREFIX",
    "archive_project",
    "create_project",
    "delete_project",
    "get_project",
    "get_project_row",
    "list_projects",
    "overview",
    "project_counts",
    "project_item",
    "project_routes",
    "save_project_routes",
    "unarchive_project",
    "update_project",
]

STATS_CACHE_PREFIX = "cache:stats:"
PROJECT_NOT_FOUND = "项目不存在"
MSG_OWNER_FORBIDDEN = "只能创建或保留自己负责的项目"
MSG_OWNER_UNAVAILABLE = "负责人不存在或已禁用"
MSG_NAME_EXISTS = "该负责人下已存在同名项目"
MSG_SLUG_EXISTS = "该负责人下已存在相同 slug 的项目"
MSG_PLATFORM_UNAVAILABLE = "平台不存在或已停用"
MSG_INVALID_MODEL = "模型不存在或模态不匹配"
MSG_MODEL_UNAVAILABLE = "模型当前不可用"

# 删除前检查的子对象（docs/03「删除规则」）
_CHILD_MODELS = (Keyword, Title, Content, PublishLink, MediaAsset, GenerationBatch)
_UPDATABLE_FIELDS = (
    "name", "slug", "industry", "audience", "brand_name", "brand_info", "description", "language", "default_style", "default_format",
)
OVERVIEW_RANGES = ("today", "7d", "30d")


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


def _clear_stats_cache() -> int:
    return cache_delete_prefix(STATS_CACHE_PREFIX)


def _admin_label(admin: Admin | None, fallback_id: int | None = None) -> str:
    if admin is None:
        return f"#{fallback_id}" if fallback_id is not None else "-"
    return admin.display_name or admin.username


def _owner_brief(admin: Admin | None) -> dict[str, Any] | None:
    if admin is None:
        return None
    return {"id": admin.id, "username": admin.username, "display_name": admin.display_name}


def project_item(project: Project) -> dict[str, Any]:
    """项目对象（字段与 ``packages/shared`` 的 ``Project`` 同名；列表另附 ``owner`` / ``counts``，详情另附 ``routes``）。"""
    templates = prompt_template_service.project_default_templates(project)
    platforms = _loads(project.default_platform_ids_json, []) or []
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "industry": project.industry,
        "audience": project.audience,
        "brand_name": project.brand_name,
        "brand_info": project.brand_info,
        "description": project.description,
        "language": project.language,
        "default_style": project.default_style,
        "default_format": project.default_format,
        "default_templates": templates,
        "default_platform_ids": [int(p) for p in platforms if isinstance(p, int) and not isinstance(p, bool)],
        "status": project.status,
        "owner_id": project.owner_id,
        "created_by": project.created_by,
        "created_at": iso_utc(project.created_at),
        "updated_at": iso_utc(project.updated_at),
    }


def project_counts(db: Session, project_ids: Sequence[int]) -> dict[int, dict[str, int]]:
    """``{project_id: {keywords, contents, links}}``（列表 ``counts{}``）。"""
    ids = [int(i) for i in project_ids]
    result = {pid: {"keywords": 0, "contents": 0, "links": 0} for pid in ids}
    if not ids:
        return result
    for key, model in (("keywords", Keyword), ("contents", Content), ("links", PublishLink)):
        rows = db.execute(select(model.project_id, func.count(model.id)).where(model.project_id.in_(ids)).group_by(model.project_id)).all()
        for pid, count in rows:
            result[int(pid)][key] = int(count)
    return result


def project_routes(db: Session, project_id: int) -> list[dict[str, Any]]:
    """项目级 ``capability_routes`` 覆盖行（按能力枚举顺序），每项 ``{id,capability,project_id,protocol,primary_model,
    fallback_models,params,timeout_seconds,max_attempts,is_enabled,note,updated_by,created_at,updated_at}``。"""
    rows = list(db.scalars(select(CapabilityRoute).where(CapabilityRoute.project_id == project_id)).all())
    order = {c.value: i for i, c in enumerate(CapabilityEnum)}
    rows.sort(key=lambda r: (order.get(r.capability, 99), r.id))
    return [ai_gateway_service.route_item(db, row, with_status=False) for row in rows]


def _detail(db: Session, project: Project) -> dict[str, Any]:
    item = project_item(project)
    item["owner"] = _owner_brief(db.get(Admin, project.owner_id))
    item["routes"] = project_routes(db, project.id)
    return item


def get_project_row(db: Session, scope: DataScope, project_id: int) -> Project:
    """目标项目须可见（``own`` 范围即本人负责），否则 404。"""
    return get_visible(db, scope, Project, project_id, message=PROJECT_NOT_FOUND)


# =====================================================================
# 查询
# =====================================================================


def list_projects(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
    status: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """分页（``created_at DESC, id DESC``）：只含可见项目（总后台带 ``owner_id`` 时为该用户的项目）；``keyword`` 匹配名称 /
    slug / 行业 / 品牌名；每项附 ``counts{keywords,contents,links}`` 与负责人摘要 ``owner{id,username,display_name}``。"""
    conditions: list[Any] = []
    if status:
        conditions.append(Project.status == status)
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        conditions.append(or_(Project.name.like(like), Project.slug.like(like), Project.industry.like(like), Project.brand_name.like(like)))
    stmt = scope_projects(select(Project), scope).where(*conditions)
    total = int(db.scalar(scope_projects(select(func.count(Project.id)), scope).where(*conditions)) or 0)
    rows = db.scalars(stmt.order_by(Project.created_at.desc(), Project.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    counts = project_counts(db, [r.id for r in rows])
    owner_ids = {r.owner_id for r in rows}
    owners = {a.id: a for a in db.scalars(select(Admin).where(Admin.id.in_(owner_ids))).all()} if owner_ids else {}
    items: list[dict[str, Any]] = []
    for row in rows:
        item = project_item(row)
        item["owner"] = _owner_brief(owners.get(row.owner_id))
        item["counts"] = counts[row.id]
        items.append(item)
    return items, total


def get_project(db: Session, scope: DataScope, project_id: int) -> dict[str, Any]:
    """详情 + ``owner`` + ``routes[]``（项目级覆盖行）。"""
    return _detail(db, get_project_row(db, scope, project_id))


# =====================================================================
# 校验
# =====================================================================


def _resolve_owner(db: Session, scope: DataScope, owner_id: int | None, *, current_owner: int | None = None) -> int:
    """负责人规则（docs/13 §7.2）：省略 → 当前用户（编辑时为原负责人）；``own`` 范围只能是本人；``all`` 范围变更时目标须为
    存在且启用的用户。"""
    admin_id = scope.admin_id
    if owner_id is None:
        if current_owner is not None:
            return current_owner
        if admin_id is None:
            raise invalid_params(field_error(["body", "owner_id"], MSG_OWNER_UNAVAILABLE, "owner_unavailable", None))
        return admin_id
    if owner_id == current_owner:
        return owner_id
    if not scope.is_all:
        if owner_id != admin_id:
            raise invalid_params(field_error(["body", "owner_id"], MSG_OWNER_FORBIDDEN, "owner_forbidden", owner_id))
        return owner_id
    target = db.get(Admin, owner_id)
    if target is None or not target.is_active:
        raise invalid_params(field_error(["body", "owner_id"], MSG_OWNER_UNAVAILABLE, "owner_unavailable", owner_id))
    return owner_id


def _ensure_unique(db: Session, owner_id: int, name: str, slug: str, *, exclude_id: int | None = None) -> None:
    """同一负责人下 ``name`` / ``slug`` 唯一，冲突 409 ``{"existing_id": …}``（冲突对象必属于目标负责人）。"""
    for column, value, message in ((Project.name, name, MSG_NAME_EXISTS), (Project.slug, slug, MSG_SLUG_EXISTS)):
        stmt = select(Project.id).where(Project.owner_id == owner_id, column == value)
        if exclude_id is not None:
            stmt = stmt.where(Project.id != exclude_id)
        existing = db.scalar(stmt.limit(1))
        if existing:
            raise _conflict(message, {"existing_id": int(existing)})


def _platform_errors(db: Session, platform_ids: Sequence[int]) -> list[dict[str, Any]]:
    ids = [int(p) for p in platform_ids]
    if not ids:
        return []
    active = set(db.scalars(select(PublishPlatform.id).where(PublishPlatform.id.in_(ids), PublishPlatform.is_active == True)).all())  # noqa: E712
    return [
        field_error(["body", "default_platform_ids", index], MSG_PLATFORM_UNAVAILABLE, "platform_unavailable", pid)
        for index, pid in enumerate(ids)
        if pid not in active
    ]


def _validate_refs(
    db: Session, scope: DataScope, values: Mapping[str, Any], project_id: int | None
) -> None:
    errors: list[dict[str, Any]] = []
    if "default_templates" in values:
        errors.extend(prompt_template_service.default_templates_errors(db, scope, values.get("default_templates") or {}, project_id))
    if "default_platform_ids" in values:
        errors.extend(_platform_errors(db, values.get("default_platform_ids") or []))
    if errors:
        raise invalid_params(errors)


def _apply(project: Project, values: Mapping[str, Any]) -> None:
    for key in _UPDATABLE_FIELDS:
        if key in values:
            setattr(project, key, values[key])
    if "default_templates" in values:
        templates = {str(k): int(v) for k, v in (values.get("default_templates") or {}).items()}
        project.default_templates_json = _dumps(templates) if templates else None
    if "default_platform_ids" in values:
        platforms = list(dict.fromkeys(int(p) for p in values.get("default_platform_ids") or []))
        project.default_platform_ids_json = _dumps(platforms) if platforms else None


def _flush_or_conflict(db: Session, project: Project) -> None:
    """唯一索引兜底（并发创建 / 改名）：回滚后按负责人重新定位冲突对象，409 ``existing_id``。"""
    owner_id, name, slug = project.owner_id, project.name, project.slug
    exclude_id = project.id
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        _ensure_unique(db, owner_id, name, slug, exclude_id=exclude_id)
        raise _conflict(MSG_NAME_EXISTS, {"existing_id": None}) from None


# =====================================================================
# 写入
# =====================================================================


def create_project(db: Session, scope: DataScope, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/projects``（§7.3）：负责人规则 → 同负责人下唯一 → ``default_templates``（新建项目只能引用全局模板）/
    ``default_platform_ids`` 校验 → 写入；``language`` / ``default_style`` / ``default_format`` 缺省取 ``generation_config``。
    提交后清 ``cache:stats:*``。"""
    owner_id = _resolve_owner(db, scope, values.get("owner_id"))
    _validate_refs(db, scope, values, None)
    _ensure_unique(db, owner_id, values["name"], values["slug"])
    cfg = settings_service.get_config(db, "generation_config")
    project = Project(
        name=values["name"],
        slug=values["slug"],
        language=values.get("language") or cfg.get("default_language") or "zh-CN",
        default_style=values.get("default_style") or settings_service.get_path(cfg, "title.default_style") or "news",
        default_format=values.get("default_format") or settings_service.get_path(cfg, "content.default_format") or "markdown",
        status="active",
        owner_id=owner_id,
        created_by=admin_id,
    )
    _apply(project, {k: v for k, v in values.items() if k not in ("language", "default_style", "default_format")})
    db.add(project)
    _flush_or_conflict(db, project)
    after_commit(db, _clear_stats_cache)
    db.commit()
    return _detail(db, project)


def update_project(
    db: Session, scope: DataScope, project_id: int, values: Mapping[str, Any], *, admin_id: int
) -> tuple[dict[str, Any], str | None]:
    """``PUT /admin/projects/{id}``：只写出现的字段；``owner_id`` 变化即转移负责人（仅 ``all`` 范围，目标须启用，目标用户
    下 ``name`` / ``slug`` 冲突 409），同一事务只改 ``owner_id``，提交后清 ``cache:stats:*``。

    返回 ``(详情, 审计摘要)``：转移时摘要为「转移项目 {name} 负责人：{旧} → {新}」，否则 ``None``。"""
    project = get_project_row(db, scope, project_id)
    old_owner = project.owner_id
    new_owner = _resolve_owner(db, scope, values.get("owner_id"), current_owner=old_owner)
    _validate_refs(db, scope, values, project.id)
    name = values.get("name", project.name)
    slug = values.get("slug", project.slug)
    if new_owner != old_owner or name != project.name or slug != project.slug:
        _ensure_unique(db, new_owner, name, slug, exclude_id=project.id)
    _apply(project, values)
    project.owner_id = new_owner
    _flush_or_conflict(db, project)
    summary: str | None = None
    if new_owner != old_owner:
        summary = (
            f"转移项目 {project.name} 负责人：{_admin_label(db.get(Admin, old_owner), old_owner)} → "
            f"{_admin_label(db.get(Admin, new_owner), new_owner)}"
        )
        after_commit(db, _clear_stats_cache)
    db.commit()
    return _detail(db, project), summary


def archive_project(db: Session, scope: DataScope, project_id: int) -> dict[str, Any]:
    """``active → archived``；非 ``active`` 时 409 ``current_status``。"""
    project = get_project_row(db, scope, project_id)
    if project.status != "active":
        raise _conflict("项目已归档", {"current_status": project.status})
    project.status = "archived"
    db.commit()
    return _detail(db, project)


def unarchive_project(db: Session, scope: DataScope, project_id: int) -> dict[str, Any]:
    """``archived → active``；非 ``archived`` 时 409 ``current_status``。"""
    project = get_project_row(db, scope, project_id)
    if project.status != "archived":
        raise _conflict("项目未归档", {"current_status": project.status})
    project.status = "active"
    db.commit()
    return _detail(db, project)


def delete_project(db: Session, scope: DataScope, project_id: int) -> None:
    """``DELETE /admin/projects/{id}``：仅 ``archived``（否则 409 ``current_status``）且无关键词 / 标题 / 内容 / 链接 / 素材 /
    批次（否则 409 ``reason=in_use``）；同一事务删除 ``prompt_templates(project_id=id)`` 与 ``capability_routes(project_id=id)``、
    ``ai_tasks`` / ``alerts`` 的 ``project_id`` 置 NULL；提交后清 ``cache:routes:*`` 与 ``cache:stats:*``。"""
    project = get_project_row(db, scope, project_id)
    if project.status != "archived":
        raise _conflict("只能删除已归档的项目", {"current_status": project.status})
    for model in _CHILD_MODELS:
        if db.scalar(select(model.id).where(model.project_id == project.id).limit(1)) is not None:
            raise _conflict("项目下仍有数据，不能删除", {"reason": "in_use"})
    db.execute(delete(PromptTemplate).where(PromptTemplate.project_id == project.id))
    db.execute(delete(CapabilityRoute).where(CapabilityRoute.project_id == project.id))
    db.execute(update(AiTask).where(AiTask.project_id == project.id).values(project_id=None))
    db.execute(update(Alert).where(Alert.project_id == project.id).values(project_id=None))
    db.delete(project)
    after_commit(db, ai_gateway_service.invalidate_routes_cache)
    after_commit(db, _clear_stats_cache)
    db.commit()


# =====================================================================
# 项目覆盖路由（运营侧）
# =====================================================================


def _route_errors(db: Session, routes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """主 / 备模型须存在于 ``ai_models``、``modalities_json`` 含该能力的模态（否则 ``invalid_model``）且 ``is_available=1``
    （否则 ``model_unavailable``，运营侧不允许保存不可用模型）；协议须与能力匹配；能力不得重复。"""
    names = {str(m) for r in routes for m in [r.get("primary_model"), *(r.get("fallback_models") or [])] if m}
    rows = {r.model_id: r for r in db.scalars(select(AiModel).where(AiModel.model_id.in_(names))).all()} if names else {}
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, route in enumerate(routes):
        base: list[Any] = ["body", "routes", index]
        capability = str(route["capability"])
        if capability in seen:
            errors.append(field_error([*base, "capability"], "能力重复", "duplicate_capability", capability))
            continue
        seen.add(capability)
        protocol = route.get("protocol")
        if protocol is not None and protocol not in ai_gateway_service.allowed_protocols(capability):
            errors.append(field_error([*base, "protocol"], "协议与能力不匹配", "invalid_protocol", protocol))
        modality = MODALITY_OF[CapabilityEnum(capability)]
        primary = str(route.get("primary_model") or "")
        used = {primary}

        def _check(loc: list[Any], model: str) -> None:
            row = rows.get(model)
            if row is None or not ai_catalog_service.model_supports(row, modality):
                errors.append(field_error(loc, MSG_INVALID_MODEL, "invalid_model", model))
            elif not row.is_available:
                errors.append(field_error(loc, MSG_MODEL_UNAVAILABLE, "model_unavailable", model))

        _check([*base, "primary_model"], primary)
        for f_index, model in enumerate(route.get("fallback_models") or []):
            loc = [*base, "fallback_models", f_index]
            if model in used:
                errors.append(field_error(loc, "备选模型与主模型或其它备选模型重复", "duplicate_model", model))
                continue
            used.add(model)
            _check(loc, str(model))
    return errors


def save_project_routes(
    db: Session, scope: DataScope, project_id: int, routes: Sequence[Mapping[str, Any]], *, admin_id: int
) -> list[dict[str, Any]]:
    """``PUT /admin/projects/{id}/routes``（docs/03 B.17、docs/08 §6.7）：对 ``capability_routes(project_id=id)`` upsert。

    - 只写 ``protocol`` / ``primary_model`` / ``fallback_models_json`` / ``params_json`` / ``updated_by``；
    - 更新既有行：``timeout_seconds`` / ``max_attempts`` / ``is_enabled`` / ``note`` 原样保留；未给出的 ``protocol`` /
      ``params`` 保持原值；
    - 插入新行：``timeout_seconds`` / ``max_attempts`` / ``is_enabled`` 从同能力全局行复制；未给出的 ``protocol`` /
      ``params`` 取全局行的值（全局行缺失时取能力默认协议与空参数）；
    - 列表中未出现的能力删除其覆盖行（含管理员经 ``/admin/ai/routes`` 创建的行），空数组删除全部；
    - 保存后清 ``cache:routes:*``。返回更新后的 ``routes[]``。
    """
    project = get_project_row(db, scope, project_id)
    routes = [dict(r) for r in routes]
    errors = _route_errors(db, routes)
    if errors:
        raise invalid_params(errors)
    existing = {r.capability: r for r in db.scalars(select(CapabilityRoute).where(CapabilityRoute.project_id == project.id)).all()}
    globals_ = {r.capability: r for r in db.scalars(select(CapabilityRoute).where(CapabilityRoute.project_id == 0)).all()}
    wanted = {str(r["capability"]) for r in routes}
    for capability, row in existing.items():
        if capability not in wanted:
            db.delete(row)
    for route in routes:
        capability = str(route["capability"])
        row = existing.get(capability)
        fallbacks = [str(m) for m in route.get("fallback_models") or []]
        if row is None:
            global_row = globals_.get(capability)
            protocol = route.get("protocol") or (global_row.protocol if global_row is not None else ai_gateway_service._default_protocol(capability))  # noqa: SLF001
            params = route.get("params")
            if params is None:
                params = _loads(global_row.params_json, {}) if global_row is not None else {}
            row = CapabilityRoute(
                capability=capability,
                project_id=project.id,
                protocol=protocol,
                primary_model=str(route["primary_model"]),
                fallback_models_json=_dumps(fallbacks),
                params_json=_dumps(params or {}),
                timeout_seconds=global_row.timeout_seconds if global_row is not None else None,
                max_attempts=int(global_row.max_attempts) if global_row is not None else 3,
                is_enabled=bool(global_row.is_enabled) if global_row is not None else True,
                updated_by=admin_id,
            )
            db.add(row)
        else:
            if route.get("protocol"):
                row.protocol = str(route["protocol"])
            row.primary_model = str(route["primary_model"])
            row.fallback_models_json = _dumps(fallbacks)
            if route.get("params") is not None:
                row.params_json = _dumps(route["params"])
            row.updated_by = admin_id
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise _conflict("项目覆盖路由已被并发修改，请重试", {"existing_id": None}) from None
    after_commit(db, ai_gateway_service.invalidate_routes_cache)
    db.commit()
    return project_routes(db, project.id)


# =====================================================================
# 概览
# =====================================================================


def _placeholder_overview(db: Session, scope: DataScope, project: Project, range_: str) -> dict[str, Any]:
    """第 6 步 ``stats_service.overview`` 实现之前的最小 KPI 结构：只含按库实时统计的当前值（总量与状态分布），流量类、
    比率类与趋势留空；``meta.placeholder = true``、``meta.warnings`` 含 ``stats_overview_unavailable``。"""

    def _by_status(model: Any) -> dict[str, int]:
        rows = db.execute(select(model.status, func.count(model.id)).where(model.project_id == project.id).group_by(model.status)).all()
        return {str(status): int(count) for status, count in rows}

    keywords = _by_status(Keyword)
    titles = _by_status(Title)
    contents = _by_status(Content)
    links_rows = db.execute(
        select(PublishLink.alive_status, func.count(PublishLink.id)).where(PublishLink.project_id == project.id).group_by(PublishLink.alive_status)
    ).all()
    links = {str(status): int(count) for status, count in links_rows}
    return {
        "meta": {
            "range": range_,
            "project_id": project.id,
            "scope": "owner" if scope.restricted else "all",
            "owner_id": scope.owner_id if scope.restricted else None,
            "placeholder": True,
            "warnings": ["stats_overview_unavailable"],
        },
        "kpis": {
            "keywords_total": sum(keywords.values()),
            "keywords_adopted": keywords.get("adopted", 0),
            "titles_total": sum(titles.values()),
            "titles_adopted": titles.get("adopted", 0),
            "contents_total": sum(contents.values()),
            "contents_approved": contents.get("approved", 0),
            "contents_published": contents.get("published", 0),
            "links_total": sum(links.values()),
            "links_alive": links.get("alive", 0),
            "links_deleted": links.get("deleted", 0),
        },
        "compare": {},
        "breakdowns": {
            "keywords_by_status": keywords,
            "titles_by_status": titles,
            "contents_by_status": contents,
            "links_by_status": links,
        },
        "series": {"dates": []},
    }


def overview(db: Session, scope: DataScope, project_id: int, *, range_: str = "7d") -> dict[str, Any]:
    """``GET /admin/projects/{id}/overview``：返回结构 = ``GET /admin/stats/overview?project_id={id}``（docs/12 §9.1）。"""
    project = get_project_row(db, scope, project_id)
    stats_overview = getattr(stats_service, "overview", None)
    if callable(stats_overview):
        return stats_overview(db, scope, project_id=project.id, range=range_)
    return _placeholder_overview(db, scope, project, range_)
