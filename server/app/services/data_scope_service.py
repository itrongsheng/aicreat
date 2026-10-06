"""用户数据范围（docs/13-user-data-scope.md §3、§4.2、§6.4、§8、§9）。

- ``DataScope``：一次请求的数据范围；``own`` 恒按本人过滤，``all`` 可用查询参数 ``owner_id`` 收窄到某一用户；
- 谓词函数以子查询附加到 ``Select`` 上（项目负责人 ``projects.owner_id`` 是唯一依据）；
- ``get_visible`` / ``require_project`` / ``is_visible``：不可见与不存在一律 404，从不以 403 暴露存在性；
- worker、启动引导与 seed 使用 ``SYSTEM_SCOPE``（不受约束）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.admin_permissions import SUPER_ADMIN_GROUP_CODE
from app.core.exceptions import CODE_CONFLICT, CODE_NOT_FOUND, BusinessError
from app.models import (
    Admin,
    AdminGroup,
    AdminOperationLog,
    AiTask,
    AiUsageLog,
    Alert,
    CapabilityRoute,
    Content,
    ContentVersion,
    DailyStat,
    DataScopeValue,
    GenerationBatch,
    IndexCheck,
    Keyword,
    LinkCheck,
    MediaAsset,
    Project,
    PromptTemplate,
    PublishLink,
    Title,
)

__all__ = [
    "MAX_BIGINT",
    "DataScope",
    "DataScopeValue",
    "SYSTEM_SCOPE",
    "SCOPED_ROUTE_PREFIXES",
    "NOT_FOUND_MESSAGE",
    "effective_data_scope",
    "build_scope",
    "visible_project_ids",
    "scope_by_project",
    "scope_projects",
    "scope_media",
    "scope_templates",
    "scope_routes",
    "scope_link_children",
    "scope_usage_logs",
    "scope_operation_logs",
    "get_visible",
    "require_project",
    "is_visible",
    "owner_options",
]

T = TypeVar("T")

NOT_FOUND_MESSAGE = "对象不存在"

# 路径前缀属于此列表的路由都声明 get_data_scope（docs/13 §9.3；04 §6.0 同名列表）
SCOPED_ROUTE_PREFIXES: tuple[str, ...] = (
    "/admin/projects",
    "/admin/prompt-templates",
    "/admin/keywords",
    "/admin/titles",
    "/admin/contents",
    "/admin/generation-batches",
    "/admin/media",
    "/admin/uploads",
    "/admin/ai/routes",
    "/admin/ai/tasks",
    "/admin/ai/usage",
    "/admin/platforms",
    "/admin/links",
    "/admin/monitoring",
    "/admin/alerts",
    "/admin/stats",
    "/admin/admin-operation-logs",
)


@dataclass(frozen=True)
class DataScope:
    admin_id: int | None        # 发起请求的用户；worker、启动引导、seed 为 None
    scope: DataScopeValue       # 所属用户组的 data_scope
    owner_id: int | None        # 生效的负责人筛选：own 恒为 admin_id；all 为查询参数 owner_id（None = 不筛选）

    @property
    def is_all(self) -> bool:
        return self.scope == "all"

    @property
    def restricted(self) -> bool:
        return self.owner_id is not None

    @property
    def cache_key(self) -> str:                      # 统计缓存键的范围段（§10.4）
        return f"owner:{self.owner_id}" if self.restricted else "all"


SYSTEM_SCOPE = DataScope(admin_id=None, scope="all", owner_id=None)


def effective_data_scope(group: AdminGroup | None) -> DataScopeValue:
    """用户组的生效数据范围：``super_admin`` 短路为 ``all``（与 ``permission_codes`` 一致），其余取 ``data_scope``。"""
    if group is None:
        return "own"
    if group.code == SUPER_ADMIN_GROUP_CODE or group.data_scope == "all":
        return "all"
    return "own"


def build_scope(admin: Admin, group: AdminGroup | None, owner_id: int | None = None) -> DataScope:
    """``get_data_scope`` 的计算：``all`` 范围采用 ``owner_id`` 查询参数，``own`` 范围忽略它（恒为本人）。"""
    if effective_data_scope(group) == "all":
        return DataScope(admin.id, "all", owner_id)
    return DataScope(admin.id, "own", admin.id)


# =====================================================================
# 范围谓词（§4.2 归属矩阵）
# =====================================================================


def visible_project_ids(scope: DataScope) -> Select | None:
    """可见项目集 P：``scope.restricted`` 时为 ``SELECT id FROM projects WHERE owner_id = :owner``，否则 ``None``（不加条件）。"""
    if not scope.restricted:
        return None
    return select(Project.id).where(Project.owner_id == scope.owner_id)


def scope_projects(stmt: Select, scope: DataScope) -> Select:
    """``projects``：``owner_id = :me``。"""
    if not scope.restricted:
        return stmt
    return stmt.where(Project.owner_id == scope.owner_id)


def scope_by_project(stmt: Select, column: Any, scope: DataScope) -> Select:
    """``column IN P``：``keywords`` / ``titles`` / ``contents`` / ``generation_batches`` / ``publish_links`` /
    ``ai_tasks`` / ``alerts`` / ``daily_stats``（``project_id`` 为 NULL 的行在受限范围内天然不可见）。"""
    projects = visible_project_ids(scope)
    if projects is None:
        return stmt
    return stmt.where(column.in_(projects))


def scope_media(stmt: Select, scope: DataScope) -> Select:
    """``media_assets``：``project_id IN P OR (project_id IS NULL AND created_by = :me)``。"""
    projects = visible_project_ids(scope)
    if projects is None:
        return stmt
    return stmt.where(
        or_(
            MediaAsset.project_id.in_(projects),
            and_(MediaAsset.project_id.is_(None), MediaAsset.created_by == scope.owner_id),
        )
    )


def scope_templates(stmt: Select, scope: DataScope) -> Select:
    """``prompt_templates``：``project_id IN P``，或 ``project_id = 0 AND (status <> 'draft' OR created_by = :me)``。"""
    projects = visible_project_ids(scope)
    if projects is None:
        return stmt
    return stmt.where(
        or_(
            PromptTemplate.project_id.in_(projects),
            and_(
                PromptTemplate.project_id == 0,
                or_(PromptTemplate.status != "draft", PromptTemplate.created_by == scope.owner_id),
            ),
        )
    )


def scope_routes(stmt: Select, scope: DataScope) -> Select:
    """``capability_routes``：``project_id = 0 OR project_id IN P``。"""
    projects = visible_project_ids(scope)
    if projects is None:
        return stmt
    return stmt.where(or_(CapabilityRoute.project_id == 0, CapabilityRoute.project_id.in_(projects)))


def scope_link_children(stmt: Select, link_column: Any, scope: DataScope) -> Select:
    """``link_checks`` / ``index_checks``：``link_id IN (SELECT id FROM publish_links WHERE project_id IN P)``。"""
    projects = visible_project_ids(scope)
    if projects is None:
        return stmt
    return stmt.where(link_column.in_(select(PublishLink.id).where(PublishLink.project_id.in_(projects))))


def scope_usage_logs(stmt: Select, scope: DataScope) -> Select:
    """``ai_usage_logs``：``ai_task_id IN (可见的尝试行)``；未匹配条目（``ai_task_id IS NULL``）只对总后台可见。

    尝试行复制根任务的 ``project_id``（docs/03 B.15），因此按 ``ai_tasks.project_id IN P`` 判定。
    """
    projects = visible_project_ids(scope)
    if projects is None:
        return stmt
    return stmt.where(AiUsageLog.ai_task_id.in_(select(AiTask.id).where(AiTask.project_id.in_(projects))))


def scope_operation_logs(stmt: Select, scope: DataScope) -> Select:
    """``admin_operation_logs``：``own`` 只看本人记录；``all``（无论是否带 ``owner_id``）不加条件。"""
    if scope.scope == "own":
        return stmt.where(AdminOperationLog.admin_id == scope.admin_id)
    return stmt


# =====================================================================
# 单对象可见性
# =====================================================================

# 直接以 project_id 归属的表
_PROJECT_OWNED = (Keyword, Title, Content, GenerationBatch, PublishLink, AiTask, Alert, DailyStat)


def _project_visible(db: Session, scope: DataScope, project_id: int | None) -> bool:
    if not scope.restricted:
        return True
    if not project_id:
        return False
    project = db.get(Project, project_id)
    return project is not None and project.owner_id == scope.owner_id


def is_visible(db: Session, scope: DataScope, obj: Any) -> bool:
    """对已加载的对象判断可见性（批量接口逐条使用）；不受约束的表（平台、模型、配置、用户等）恒为 ``True``。"""
    if obj is None:
        return False
    if isinstance(obj, AdminOperationLog):
        return scope.scope != "own" or obj.admin_id == scope.admin_id
    if not scope.restricted:
        return True
    if isinstance(obj, Project):
        return obj.owner_id == scope.owner_id
    if isinstance(obj, _PROJECT_OWNED):
        return _project_visible(db, scope, obj.project_id)
    if isinstance(obj, ContentVersion):
        return is_visible(db, scope, db.get(Content, obj.content_id))
    if isinstance(obj, (LinkCheck, IndexCheck)):
        return is_visible(db, scope, db.get(PublishLink, obj.link_id))
    if isinstance(obj, MediaAsset):
        if obj.project_id is None:
            return obj.created_by == scope.owner_id
        return _project_visible(db, scope, obj.project_id)
    if isinstance(obj, PromptTemplate):
        if obj.project_id == 0:
            return obj.status != "draft" or obj.created_by == scope.owner_id
        return _project_visible(db, scope, obj.project_id)
    if isinstance(obj, CapabilityRoute):
        return obj.project_id == 0 or _project_visible(db, scope, obj.project_id)
    if isinstance(obj, AiUsageLog):
        if obj.ai_task_id is None:
            return False
        return is_visible(db, scope, db.get(AiTask, obj.ai_task_id))
    return True


MAX_BIGINT = 2**63 - 1


def _id_in_range(obj_id: Any) -> bool:
    """超出 BIGINT 范围的整数 ID 不可能存在（直接按不存在处理，避免 SQLite 绑定参数时 ``OverflowError``）。"""
    return not isinstance(obj_id, int) or -MAX_BIGINT - 1 <= obj_id <= MAX_BIGINT


def get_visible(db: Session, scope: DataScope, model: type[T], obj_id: Any, *, message: str = NOT_FOUND_MESSAGE) -> T:
    """读取对象并判断可见性；不存在或不可见都抛 ``BusinessError("对象不存在", code=404, http_status=404)``。"""
    obj = db.get(model, obj_id) if obj_id is not None and _id_in_range(obj_id) else None
    if obj is None or not is_visible(db, scope, obj):
        raise BusinessError(message, code=CODE_NOT_FOUND, http_status=404)
    return obj


def require_project(db: Session, scope: DataScope, project_id: int, *, active: bool = False) -> Project:
    """写入口：项目须可见（否则 404）；``active=True`` 时 ``archived`` 项目返回 409 ``current_status``。"""
    project = get_visible(db, scope, Project, project_id, message="项目不存在")
    if active and project.status != "active":
        raise BusinessError("项目已归档", code=CODE_CONFLICT, http_status=409, data={"current_status": project.status})
    return project


# =====================================================================
# 负责人候选（§6.4）
# =====================================================================


def owner_options(db: Session, scope: DataScope) -> list[dict[str, Any]]:
    """``GET /admin/projects/owner-options``：按 ``scope.is_all`` 判定，忽略 ``scope.owner_id``（用户视角下仍返回全部候选）。

    - ``all``：全部 ``is_active=1`` 的用户 + 仍负责至少一个项目的已禁用用户，按 ``display_name``（空则 ``username``）排序；
    - ``own``：只返回本人一项。
    """
    counts = dict(db.execute(select(Project.owner_id, func.count(Project.id)).group_by(Project.owner_id)).all())
    stmt = select(Admin, AdminGroup).join(AdminGroup, AdminGroup.id == Admin.group_id)
    if scope.is_all:
        owners = select(Project.owner_id).distinct()
        stmt = stmt.where(or_(Admin.is_active == True, Admin.id.in_(owners)))  # noqa: E712
    else:
        stmt = stmt.where(Admin.id == scope.admin_id)
    items = [
        {
            "id": admin.id,
            "username": admin.username,
            "display_name": admin.display_name,
            "is_active": bool(admin.is_active),
            "data_scope": effective_data_scope(group),
            "project_count": int(counts.get(admin.id, 0)),
        }
        for admin, group in db.execute(stmt).all()
    ]
    items.sort(key=lambda item: ((item["display_name"] or item["username"]).lower(), item["id"]))
    return items
