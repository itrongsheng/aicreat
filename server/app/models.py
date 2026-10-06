"""全部 24 张表的 SQLAlchemy ORM 模型（权威定义：docs/03-data-model.md）与状态枚举 ``Literal`` 常量。

类型约定（MySQL 8 与 SQLite 测试会话共用同一份定义，见 docs/03「数据库约定」「测试会话」）：

- 主键 ``BIGINT AUTO_INCREMENT``：``BigInteger`` + SQLite 变体 ``Integer``（SQLite 只有 ``INTEGER PRIMARY KEY`` 自增）。
- 布尔（``TINYINT(1)`` 0/1）：一律 ``Boolean``（MySQL 渲染为 ``BOOL`` = ``TINYINT(1)``，不建 CHECK 约束）；
  非布尔的小整数 ``TINYINT``（难度、进度、计数等）：``SmallInteger`` + MySQL 变体 ``TINYINT``，ORM 中为 ``int``。
- ``MEDIUMTEXT``：``Text`` + MySQL 变体 ``MEDIUMTEXT``；金额/比率：``Numeric(p, s)``（MySQL ``DECIMAL``）。
- 枚举一律 ``VARCHAR`` 存小写 ``snake_case``，不用 MySQL ENUM；取值集合见下方 ``Literal``（与 ``packages/shared/src/enums.ts`` 同名同值）。
- JSON 列（``*_json``）一律可空的 ``TEXT``/``MEDIUMTEXT``，由 service 编解码。
- ``created_at``/``updated_at``：数据库默认 ``CURRENT_TIMESTAMP``，MySQL 下 ``updated_at`` 另带 ``ON UPDATE CURRENT_TIMESTAMP``；
  ORM 同时以 ``utcnow()`` 作为 Python 侧 insert/update 默认值，使 SQLite 会话与 Core ``update()`` 下行为一致。
- 默认值同时声明 Python 侧 ``default`` 与数据库 ``server_default``：新建对象 flush 后无需回查即可读取默认值。
- 外键：只有 docs/03 列出的 6 处真实外键（``fk_<表>_<列>``），其余 ``FK→表`` 为逻辑外键，只建索引。
- 索引命名：普通 ``ix_<表>_<列…>``、唯一 ``uq_<表>_<列…>``；``alerts.dedupe_key`` 在 MySQL 下为前缀索引（191）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.expression import ColumnElement

from app.core.admin_permissions import PermissionType
from app.core.database import Base

# =====================================================================
# 状态枚举（取值集合权威：docs/00-overview.md §8；data_scope 见 docs/13 §3.1）
# =====================================================================

# ---- 8.1 内容生产 ----
ProjectStatus = Literal["active", "archived"]
ContentStyle = Literal["news", "tutorial", "review", "qa", "recommend", "listicle", "story"]
ContentFormat = Literal["markdown", "html"]
PromptKind = Literal[
    "keyword", "title", "outline", "content", "section", "rewrite", "expand", "shorten", "restyle",
    "seo_meta", "faq", "image_prompt", "geo_query", "seo_query",
]
PromptStatus = Literal["draft", "published", "archived"]
PromptOutputFormat = Literal["json", "markdown", "text"]
BatchKind = Literal["keyword", "title", "content"]
BatchStatus = Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]
KeywordStatus = Literal["candidate", "adopted", "discarded"]
KeywordIntent = Literal["informational", "navigational", "transactional", "commercial", "unknown"]
KeywordType = Literal["core", "long_tail", "question", "brand", "competitor"]
KeywordSource = Literal["generated", "imported", "manual"]
TitleSource = Literal["generated", "manual"]
TitleStatus = Literal["candidate", "adopted", "discarded"]
ContentStatus = Literal["draft", "generating", "ready", "reviewing", "approved", "rejected", "published", "archived"]
ReviewResult = Literal["approved", "rejected"]
VersionSource = Literal["generate", "rewrite", "expand", "shorten", "restyle", "manual", "restore"]
RewriteMode = Literal["rewrite", "expand", "shorten", "restyle"]
RewriteScope = Literal["full", "section"]

# ---- 8.2 媒体 ----
MediaKind = Literal["image", "video"]
MediaUsageType = Literal["cover", "inline", "standalone", "reference"]
MediaSource = Literal["generated", "uploaded"]
MediaStatus = Literal["pending", "submitted", "generating", "downloading", "ready", "failed", "expired", "deleted"]
ImageResolution = Literal["1080p", "2k", "4k"]
ImageAspectRatio = Literal["1:1", "4:3", "3:4", "16:9", "9:16"]
VideoResolution = Literal["480p", "720p", "1080p", "4k"]
UpstreamTaskStatus = Literal["queued", "in_progress", "succeeded", "failed", "expired"]

# ---- 8.3 AI 网关 ----
Capability = Literal["keyword", "title", "content", "rewrite", "image", "video", "geo_check", "seo_check"]
Modality = Literal["text", "image", "video"]
Protocol = Literal["openai_chat", "openai_responses", "anthropic_messages", "image_async", "image_sync", "image_edit", "video"]
AiTaskStatus = Literal["queued", "running", "polling", "succeeded", "failed", "cancelled", "expired"]
AiTaskOperation = Literal[
    "keyword_generate", "title_generate", "content_generate", "content_outline", "content_body", "content_seo",
    "content_rewrite", "image_prompt", "image_generate", "video_generate", "seo_check", "geo_check", "route_probe",
]
AiTaskTriggerType = Literal["user", "worker", "health_probe", "system"]
AiTaskTargetType = Literal["generation_batch", "keyword", "content", "media_asset", "publish_link", "route_probe"]
ErrorCategory = Literal[
    "unsupported_parameter", "route_missing", "model_unrouted", "upstream_unavailable", "rate_limited", "timeout",
    "quota_exceeded", "auth_failed", "content_blocked", "media_storage", "transfer_failed", "invalid_response",
    "breaker_open", "cancelled", "unknown",
]
HealthStatus = Literal["healthy", "degraded", "down", "unknown"]
BreakerState = Literal["closed", "open", "half_open"]
BreakerReason = Literal["failures", "model_unavailable", "probe_down", "manual"]
PausedReason = Literal["quota_exceeded", "auth_failed"]
ZhiqiMode = Literal["mock", "live"]
UsageLogType = Literal[2, 5, 6]
QuotaType = Literal[0, 1]

# ---- 8.4 发布与监控 ----
PlatformCode = Literal["zhihu", "wechat_mp", "xiaohongshu", "csdn", "toutiao", "baijiahao", "website", "other"]  # seed 平台，可新增
LinkAliveStatus = Literal["pending", "alive", "changed", "suspected_deleted", "deleted", "unknown"]
CheckType = Literal["baseline", "scheduled", "manual", "retry"]
IndexCheckType = Literal["scheduled", "manual"]
LinkCheckResult = Literal["alive", "changed", "suspected_deleted", "deleted", "unknown"]
LinkCheckRule = Literal[
    "http_404", "http_410", "http_451", "redirect_home", "redirect_login", "title_changed", "body_changed",
    "network_error", "blocked_by_robots", "ssrf_blocked", "ok",
]  # 另有动态取值 "marker:<文案>"（前缀 LINK_CHECK_RULE_MARKER_PREFIX）
LINK_CHECK_RULE_MARKER_PREFIX = "marker:"
IndexKind = Literal["seo", "geo"]
SeoEngine = Literal["baidu", "bing", "google"]
GeoEngine = Literal["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"]  # 可在 settings.geo_engines 扩展
SeoProvider = Literal["zhiqi_web_search", "baidu_ai_search", "bing_webmaster", "google_search_console", "manual"]
GeoProvider = Literal["zhiqi_model", "manual"]
SeoIndexStatus = Literal["indexed", "not_indexed", "unknown"]
GeoCiteStatus = Literal["cited", "not_cited", "unknown"]
IndexMatchMode = Literal["url", "domain", "title", "none", "manual"]
AlertType = Literal[
    "link_deleted", "link_restored", "link_changed", "index_overdue", "ai_task_failures", "ai_breaker_open",
    "ai_quota_exceeded", "ai_auth_failed", "ai_upstream_unavailable", "media_task_failed", "worker_stale",
]
AlertSeverity = Literal["info", "warning", "critical"]
AlertStatus = Literal["open", "acknowledged", "resolved", "ignored"]
AlertTargetType = Literal["publish_link", "content", "ai_task", "ai_model", "capability_route", "media_asset", "worker", "system"]
AlertChannel = Literal["in_app", "webhook", "email"]

# ---- 8.5 报表、系统与安全 ----
StatsDimension = Literal["total", "platform", "capability", "model", "admin", "seo_engine", "geo_engine"]
StatsGranularity = Literal["day", "week", "month"]
StatsRange = Literal["today", "7d", "30d"]
RankingType = Literal["fastest_indexed", "most_deleted_platforms", "top_cost_models", "top_cost_projects", "top_failed_models"]
AdminGroupCode = Literal["super_admin", "operator", "reviewer", "read_only"]  # 系统组；自定义组为 custom_*
DataScopeValue = Literal["all", "own"]
OperationAction = Literal["create", "update", "update_status", "delete", "execute", "login", "logout", "reset_password"]
Locale = Literal["zh-CN", "en-US"]
SettingsLocale = Literal["zh-CN", "en-US", "*"]
StorageMode = Literal["local", "oss"]
HealthCheckStatus = Literal["ok", "degraded"]

# PermissionType（admin_permissions.type：menu / action）定义在 app.core.admin_permissions，经本模块再导出


def enum_values(literal_type: Any) -> tuple[Any, ...]:
    """``Literal`` 的取值元组，如 ``enum_values(ContentStatus)``（供 schema 校验与筛选参数复用）。"""
    return get_args(literal_type)


# =====================================================================
# 通用类型与列
# =====================================================================

BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
TinyInt = SmallInteger().with_variant(mysql.TINYINT(), "mysql")
MediumText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

MYSQL_TABLE_KWARGS: dict[str, Any] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def utcnow() -> datetime:
    """当前 UTC 时间（naive，秒精度），与库内 ``DATETIME`` 一律存 UTC 的约定一致。"""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


class CurrentTimestampOnUpdate(ColumnElement[datetime]):
    """``updated_at`` 的数据库默认值：MySQL 渲染 ``CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP``，其它方言 ``CURRENT_TIMESTAMP``。"""

    inherit_cache = True
    type = DateTime()


@compiles(CurrentTimestampOnUpdate)
def _compile_cts_default(element: CurrentTimestampOnUpdate, compiler: Any, **kw: Any) -> str:
    return "CURRENT_TIMESTAMP"


@compiles(CurrentTimestampOnUpdate, "mysql")
def _compile_cts_mysql(element: CurrentTimestampOnUpdate, compiler: Any, **kw: Any) -> str:
    return "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"


def _sd(value: int | bool | str) -> Any:
    """数据库默认值：数字/布尔渲染为裸字面量，字符串为带引号的字面量。"""
    if isinstance(value, bool):
        return text("1" if value else "0")
    if isinstance(value, int):
        return text(str(value))
    return value


def col_default(value: int | bool | str | Decimal) -> dict[str, Any]:
    """同时声明 Python 侧 ``default`` 与数据库 ``server_default`` 的关键字参数。"""
    if isinstance(value, Decimal):
        return {"default": value, "server_default": text(str(value))}
    return {"default": value, "server_default": _sd(value)}


class TimestampMixin:
    """所有表的 ``created_at``/``updated_at``（数据库维护，ORM 同步写入 UTC）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"), sort_order=1000
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=CurrentTimestampOnUpdate(),
        sort_order=1001,
    )


def _pk() -> Mapped[int]:
    return mapped_column(BigIntPK, primary_key=True, autoincrement=True)


# =====================================================================
# RBAC（B.1 ~ B.5）
# =====================================================================


class Admin(TimestampMixin, Base):
    """B.1 管理员账号（界面称「用户」）。"""

    __tablename__ = "admins"
    __table_args__ = (
        UniqueConstraint("username", name="uq_admins_username"),
        Index("ix_admins_group_id", "group_id"),
        Index("ix_admins_is_active", "is_active"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(50))
    group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admin_groups.id", name="fk_admins_group_id", ondelete="RESTRICT"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(True))
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(1))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class AdminGroup(TimestampMixin, Base):
    """B.2 管理员用户组（带数据范围 ``data_scope``）。"""

    __tablename__ = "admin_groups"
    __table_args__ = (
        UniqueConstraint("code", name="uq_admin_groups_code"),
        UniqueConstraint("name", name="uq_admin_groups_name"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    name_en: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(False))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(True))
    data_scope: Mapped[DataScopeValue] = mapped_column(String(16), nullable=False, **col_default("own"))
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class AdminPermission(TimestampMixin, Base):
    """B.3 权限定义（迁移 seed + 启动 ``ensure_rbac_seed`` 维护）。"""

    __tablename__ = "admin_permissions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_admin_permissions_code"),
        Index("ix_admin_permissions_module_sort", "module", "sort"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    module: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    type: Mapped[PermissionType] = mapped_column(String(16), nullable=False)
    parent_code: Mapped[str | None] = mapped_column(String(100))
    sort: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))


class AdminGroupPermission(TimestampMixin, Base):
    """B.4 用户组 × 权限（复合主键）。"""

    __tablename__ = "admin_group_permissions"
    __table_args__ = (
        Index("ix_admin_group_permissions_permission_id", "permission_id"),
        MYSQL_TABLE_KWARGS,
    )

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("admin_groups.id", name="fk_admin_group_permissions_group_id", ondelete="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )
    permission_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("admin_permissions.id", name="fk_admin_group_permissions_permission_id", ondelete="RESTRICT"),
        primary_key=True,
        autoincrement=False,
    )


class AdminOperationLog(TimestampMixin, Base):
    """B.5 管理员操作日志（不可变）。"""

    __tablename__ = "admin_operation_logs"
    __table_args__ = (
        Index("ix_admin_operation_logs_admin_id_created_at", "admin_id", "created_at"),
        Index("ix_admin_operation_logs_permission_code", "permission_code"),
        Index("ix_admin_operation_logs_target_type_target_id", "target_type", "target_id"),
        Index("ix_admin_operation_logs_created_at", "created_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    permission_code: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[OperationAction] = mapped_column(String(50), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ip: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))


# =====================================================================
# 配置（B.6）
# =====================================================================


class Setting(TimestampMixin, Base):
    """B.6 系统配置（复合主键 ``(key, locale)``；``key`` 为 MySQL 保留字，SQLAlchemy 自动加反引号）。"""

    __tablename__ = "settings"
    __table_args__ = (MYSQL_TABLE_KWARGS,)

    key: Mapped[str] = mapped_column("key", String(80), primary_key=True, autoincrement=False)
    locale: Mapped[SettingsLocale] = mapped_column(String(10), primary_key=True, autoincrement=False)
    value: Mapped[str] = mapped_column(MediumText, nullable=False)


# =====================================================================
# 生成域（B.7 ~ B.14）
# =====================================================================


class Project(TimestampMixin, Base):
    """B.7 项目/专题；``owner_id`` 为数据归属键（docs/13）。"""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_projects_owner_id_name"),
        UniqueConstraint("owner_id", "slug", name="uq_projects_owner_id_slug"),
        Index("ix_projects_status", "status"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(80))
    audience: Mapped[str | None] = mapped_column(String(255))
    brand_name: Mapped[str | None] = mapped_column(String(100))
    brand_info: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str] = mapped_column(String(10), nullable=False, **col_default("zh-CN"))
    default_style: Mapped[ContentStyle] = mapped_column(String(32), nullable=False, **col_default("news"))
    default_format: Mapped[ContentFormat] = mapped_column(String(10), nullable=False, **col_default("markdown"))
    default_templates_json: Mapped[str | None] = mapped_column(Text)
    default_platform_ids_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(String(16), nullable=False, **col_default("active"))
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PromptTemplate(TimestampMixin, Base):
    """B.8 Prompt 模板（``code`` + ``version``）。"""

    __tablename__ = "prompt_templates"
    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_prompt_templates_code_version"),
        Index("ix_prompt_templates_kind_status_project_id", "kind", "status", "project_id"),
        Index("ix_prompt_templates_project_id", "project_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(1))
    kind: Mapped[PromptKind] = mapped_column(String(32), nullable=False)
    capability: Mapped[Capability] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str] = mapped_column(String(10), nullable=False, **col_default("zh-CN"))
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    system_prompt: Mapped[str | None] = mapped_column(Text)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    variables_json: Mapped[str | None] = mapped_column(Text)
    output_format: Mapped[PromptOutputFormat] = mapped_column(String(16), nullable=False, **col_default("json"))
    output_schema_json: Mapped[str | None] = mapped_column(Text)
    model_params_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[PromptStatus] = mapped_column(String(16), nullable=False, **col_default("draft"))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(False))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GenerationBatch(TimestampMixin, Base):
    """B.9 生成批次（关键词/标题/内容）。"""

    __tablename__ = "generation_batches"
    __table_args__ = (
        Index("ix_generation_batches_project_id_kind_status", "project_id", "kind", "status"),
        Index("ix_generation_batches_status_created_at", "status", "created_at"),
        Index("ix_generation_batches_created_by_created_at", "created_by", "created_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[BatchKind] = mapped_column(String(16), nullable=False)
    status: Mapped[BatchStatus] = mapped_column(String(16), nullable=False, **col_default("queued"))
    input_json: Mapped[str | None] = mapped_column(Text)
    template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    produced_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    task_total: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    task_done: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    task_failed: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    error_summary: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Keyword(TimestampMixin, Base):
    """B.10 关键词（项目内按 ``normalized_keyword`` 唯一）。"""

    __tablename__ = "keywords"
    __table_args__ = (
        UniqueConstraint("project_id", "normalized_keyword", name="uq_keywords_project_id_normalized_keyword"),
        Index("ix_keywords_project_id_status_score", "project_id", "status", "score"),
        Index("ix_keywords_batch_id", "batch_id"),
        Index("ix_keywords_ai_task_id", "ai_task_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    keyword: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_keyword: Mapped[str] = mapped_column(String(160), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    intent: Mapped[KeywordIntent] = mapped_column(String(16), nullable=False, **col_default("unknown"))
    keyword_type: Mapped[KeywordType] = mapped_column(String(16), nullable=False, **col_default("core"))
    difficulty: Mapped[int | None] = mapped_column(TinyInt)
    heat: Mapped[int | None] = mapped_column(TinyInt)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    seed: Mapped[str | None] = mapped_column(String(120))
    source: Mapped[KeywordSource] = mapped_column(String(16), nullable=False, **col_default("generated"))
    batch_id: Mapped[int | None] = mapped_column(BigInteger)
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(500))
    tags_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[KeywordStatus] = mapped_column(String(16), nullable=False, **col_default("candidate"))
    adopted_by: Mapped[int | None] = mapped_column(BigInteger)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime)
    title_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    content_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Title(TimestampMixin, Base):
    """B.11 标题。"""

    __tablename__ = "titles"
    __table_args__ = (
        Index("ix_titles_keyword_id_status", "keyword_id", "status"),
        Index("ix_titles_project_id_status_created_at", "project_id", "status", "created_at"),
        Index("ix_titles_batch_id", "batch_id"),
        Index("ix_titles_ai_task_id", "ai_task_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    keyword_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    original_title: Mapped[str | None] = mapped_column(String(200))
    style: Mapped[ContentStyle] = mapped_column(String(32), nullable=False)
    ai_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    manual_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(False))
    source: Mapped[TitleSource] = mapped_column(String(16), nullable=False, **col_default("generated"))
    batch_id: Mapped[int | None] = mapped_column(BigInteger)
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[TitleStatus] = mapped_column(String(16), nullable=False, **col_default("candidate"))
    adopted_by: Mapped[int | None] = mapped_column(BigInteger)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime)
    content_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Content(TimestampMixin, Base):
    """B.12 内容（文章）；版本化字段经 ``content_service`` 与 ``content_versions`` 同事务写入。"""

    __tablename__ = "contents"
    __table_args__ = (
        Index("ix_contents_project_id_status_updated_at", "project_id", "status", "updated_at"),
        Index("ix_contents_title_id", "title_id"),
        Index("ix_contents_keyword_id", "keyword_id"),
        Index("ix_contents_status_created_at", "status", "created_at"),
        Index("ix_contents_batch_id", "batch_id"),
        Index("ix_contents_created_by", "created_by"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title_id: Mapped[int | None] = mapped_column(BigInteger)
    keyword_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    format: Mapped[ContentFormat] = mapped_column(String(10), nullable=False, **col_default("markdown"))
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    style: Mapped[ContentStyle] = mapped_column(String(32), nullable=False)
    status: Mapped[ContentStatus] = mapped_column(String(16), nullable=False, **col_default("draft"))
    prev_status: Mapped[ContentStatus | None] = mapped_column(String(16))
    review_result: Mapped[ReviewResult | None] = mapped_column(String(10))
    current_version_id: Mapped[int | None] = mapped_column(BigInteger)
    version_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    outline_json: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(MediumText)
    summary: Mapped[str | None] = mapped_column(String(500))
    seo_title: Mapped[str | None] = mapped_column(String(200))
    seo_description: Mapped[str | None] = mapped_column(String(500))
    seo_keywords_json: Mapped[str | None] = mapped_column(Text)
    faq_json: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    cover_asset_id: Mapped[int | None] = mapped_column(BigInteger)
    template_id: Mapped[int | None] = mapped_column(BigInteger)
    generation_params_json: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[int | None] = mapped_column(BigInteger)
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    quality_score: Mapped[int | None] = mapped_column(TinyInt)
    risk_flags_json: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str | None] = mapped_column(String(500))
    link_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


# 版本化字段（docs/03 B.12：固定 8 个）
CONTENT_VERSIONED_FIELDS: tuple[str, ...] = (
    "title", "body", "outline_json", "summary", "seo_title", "seo_description", "seo_keywords_json", "faq_json",
)


class ContentVersion(TimestampMixin, Base):
    """B.13 内容版本快照（不可变；删除内容时级联删除）。"""

    __tablename__ = "content_versions"
    __table_args__ = (
        UniqueConstraint("content_id", "version_no", name="uq_content_versions_content_id_version_no"),
        Index("ix_content_versions_ai_task_id", "ai_task_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    content_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contents.id", name="fk_content_versions_content_id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[VersionSource] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(MediumText, nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    outline_json: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(String(500))
    seo_title: Mapped[str | None] = mapped_column(String(200))
    seo_description: Mapped[str | None] = mapped_column(String(500))
    seo_keywords_json: Mapped[str | None] = mapped_column(Text)
    faq_json: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    template_id: Mapped[int | None] = mapped_column(BigInteger)
    model: Mapped[str | None] = mapped_column(String(120))
    change_summary: Mapped[str | None] = mapped_column(String(300))
    restored_from_version_id: Mapped[int | None] = mapped_column(BigInteger)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


class MediaAsset(TimestampMixin, Base):
    """B.14 媒体素材（图片/视频）。"""

    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_media_assets_project_id_kind_status_created_at", "project_id", "kind", "status", "created_at"),
        Index("ix_media_assets_content_id_sort", "content_id", "sort"),
        Index("ix_media_assets_ai_task_id", "ai_task_id"),
        Index("ix_media_assets_status_updated_at", "status", "updated_at"),
        Index("ix_media_assets_status_next_transfer_at", "status", "next_transfer_at"),
        Index("ix_media_assets_upstream_task_id", "upstream_task_id"),
        Index("ix_media_assets_kind_ready_at", "kind", "ready_at"),
        Index("ix_media_assets_kind_failed_at", "kind", "failed_at"),
        Index("ix_media_assets_created_by_created_at", "created_by", "created_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int | None] = mapped_column(BigInteger)
    content_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[MediaKind] = mapped_column(String(10), nullable=False)
    usage_type: Mapped[MediaUsageType] = mapped_column(String(16), nullable=False, **col_default("standalone"))
    source: Mapped[MediaSource] = mapped_column(String(16), nullable=False, **col_default("generated"))
    status: Mapped[MediaStatus] = mapped_column(String(16), nullable=False, **col_default("pending"))
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    prompt: Mapped[str | None] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    params_json: Mapped[str | None] = mapped_column(Text)
    reference_asset_ids_json: Mapped[str | None] = mapped_column(Text)
    upstream_task_id: Mapped[str | None] = mapped_column(String(80))
    upstream_url: Mapped[str | None] = mapped_column(String(1000))
    storage_key: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1000))
    thumbnail_key: Mapped[str | None] = mapped_column(String(255))
    thumbnail_url: Mapped[str | None] = mapped_column(String(1000))
    mime_type: Mapped[str | None] = mapped_column(String(80))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    file_hash: Mapped[str | None] = mapped_column(CHAR(64))
    progress: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    error_category: Mapped[ErrorCategory | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(500))
    transfer_attempts: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    next_transfer_at: Mapped[datetime | None] = mapped_column(DateTime)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)


# =====================================================================
# AI 网关域（B.15 ~ B.18）
# =====================================================================


class AiTask(TimestampMixin, Base):
    """B.15 统一 AI 调用/任务记录：根任务行（``root_task_id IS NULL``）+ 尝试行。"""

    __tablename__ = "ai_tasks"
    __table_args__ = (
        Index("ix_ai_tasks_status_next_poll_at", "status", "next_poll_at"),
        Index("ix_ai_tasks_status_heartbeat_at", "status", "heartbeat_at"),
        Index("ix_ai_tasks_root_task_id", "root_task_id"),
        Index("ix_ai_tasks_request_id", "request_id"),
        Index("ix_ai_tasks_upstream_task_id", "upstream_task_id"),
        Index("ix_ai_tasks_batch_id", "batch_id"),
        Index("ix_ai_tasks_target_type_target_id_operation_status", "target_type", "target_id", "operation", "status"),
        Index("ix_ai_tasks_project_id_capability_created_at", "project_id", "capability", "created_at"),
        Index("ix_ai_tasks_capability_model_created_at", "capability", "model", "created_at"),
        Index("ix_ai_tasks_created_at", "created_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int | None] = mapped_column(BigInteger)
    capability: Mapped[Capability] = mapped_column(String(20), nullable=False)
    operation: Mapped[AiTaskOperation] = mapped_column(String(32), nullable=False)
    input_json: Mapped[str | None] = mapped_column(Text)
    protocol: Mapped[Protocol | None] = mapped_column(String(24))
    trigger_type: Mapped[AiTaskTriggerType] = mapped_column(String(16), nullable=False, **col_default("user"))
    target_type: Mapped[AiTaskTargetType | None] = mapped_column(String(32))
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    batch_id: Mapped[int | None] = mapped_column(BigInteger)
    root_task_id: Mapped[int | None] = mapped_column(BigInteger)
    parent_task_id: Mapped[int | None] = mapped_column(BigInteger)
    route_id: Mapped[int | None] = mapped_column(BigInteger)
    candidate_index: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    attempt: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(1))
    segment_index: Mapped[int | None] = mapped_column(TinyInt)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    template_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[AiTaskStatus] = mapped_column(String(16), nullable=False, **col_default("queued"))
    pause_count: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    request_id: Mapped[str | None] = mapped_column(String(64))
    upstream_task_id: Mapped[str | None] = mapped_column(String(80))
    request_payload_json: Mapped[str | None] = mapped_column(MediumText)
    response_meta_json: Mapped[str | None] = mapped_column(Text)
    output_excerpt: Mapped[str | None] = mapped_column(String(2000))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    cache_tokens: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    quota_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    quota_estimated: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    quota_actual: Mapped[int | None] = mapped_column(BigInteger)
    cost_cny: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime)
    usage_log_type: Mapped[int | None] = mapped_column(TinyInt)
    error_category: Mapped[ErrorCategory | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(500))
    http_status: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    upstream_latency_ms: Mapped[int | None] = mapped_column(Integer)
    progress: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    poll_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class AiModel(TimestampMixin, Base):
    """B.16 zhiqiapi 模型目录与价格快照。"""

    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("model_id", name="uq_ai_models_model_id"),
        Index("ix_ai_models_is_available_sort_order", "is_available", "sort_order"),
        Index("ix_ai_models_vendor_id", "vendor_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    owned_by: Mapped[str | None] = mapped_column(String(80))
    vendor_id: Mapped[int | None] = mapped_column(Integer)
    vendor_name: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(Text)
    tags_json: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(500))
    cover_url: Mapped[str | None] = mapped_column(String(500))
    supported_endpoint_types_json: Mapped[str | None] = mapped_column(Text)
    modalities_json: Mapped[str | None] = mapped_column(Text)
    quota_type: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    model_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    model_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    completion_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cache_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    create_cache_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    enable_groups_json: Mapped[str | None] = mapped_column(Text)
    billing_mode: Mapped[str | None] = mapped_column(String(32))
    billing_expr: Mapped[str | None] = mapped_column(String(255))
    model_price_type: Mapped[str | None] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_health_status: Mapped[HealthStatus] = mapped_column(String(16), nullable=False, **col_default("unknown"))
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_health_latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_pricing_json: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class CapabilityRoute(TimestampMixin, Base):
    """B.17 能力路由：全局行（``project_id=0``）+ 项目覆盖行。"""

    __tablename__ = "capability_routes"
    __table_args__ = (
        UniqueConstraint("capability", "project_id", name="uq_capability_routes_capability_project_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    capability: Mapped[Capability] = mapped_column(String(20), nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    protocol: Mapped[Protocol] = mapped_column(String(24), nullable=False)
    primary_model: Mapped[str] = mapped_column(String(120), nullable=False)
    # TEXT 列不设数据库字面量默认值（MySQL 限制），默认 "[]" 由 ORM 写入
    fallback_models_json: Mapped[str | None] = mapped_column(Text, default="[]")
    params_json: Mapped[str | None] = mapped_column(Text)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(3))
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(True))
    note: Mapped[str | None] = mapped_column(String(255))
    updated_by: Mapped[int | None] = mapped_column(BigInteger)


class AiUsageLog(TimestampMixin, Base):
    """B.18 ``/api/log/token`` 对账日志（不可变，``entry_hash`` 幂等）。"""

    __tablename__ = "ai_usage_logs"
    __table_args__ = (
        UniqueConstraint("entry_hash", name="uq_ai_usage_logs_entry_hash"),
        Index("ix_ai_usage_logs_request_id_log_type", "request_id", "log_type"),
        Index("ix_ai_usage_logs_upstream_task_id", "upstream_task_id"),
        Index("ix_ai_usage_logs_ai_task_id", "ai_task_id"),
        Index("ix_ai_usage_logs_model_name_pulled_at", "model_name", "pulled_at"),
        Index("ix_ai_usage_logs_matched_at", "matched_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    entry_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    upstream_log_id: Mapped[int | None] = mapped_column(BigInteger)
    request_id: Mapped[str | None] = mapped_column(String(64))
    log_type: Mapped[int] = mapped_column(TinyInt, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(120))
    group_name: Mapped[str | None] = mapped_column(String(50))
    quota: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    cache_tokens: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    group_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    model_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    completion_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    request_path: Mapped[str | None] = mapped_column(String(255))
    upstream_task_id: Mapped[str | None] = mapped_column(String(80))
    upstream_created_at: Mapped[datetime | None] = mapped_column(DateTime)
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime)
    pulled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_json: Mapped[str | None] = mapped_column(Text)


# =====================================================================
# 发布监控与报表域（B.19 ~ B.24）
# =====================================================================


class PublishPlatform(TimestampMixin, Base):
    """B.19 发布平台与删除特征规则。"""

    __tablename__ = "publish_platforms"
    __table_args__ = (
        UniqueConstraint("code", name="uq_publish_platforms_code"),
        Index("ix_publish_platforms_is_active_sort", "is_active", "sort"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    name_en: Mapped[str] = mapped_column(String(80), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(500))
    home_url: Mapped[str | None] = mapped_column(String(255))
    url_patterns_json: Mapped[str | None] = mapped_column(Text)
    deleted_markers_json: Mapped[str | None] = mapped_column(Text)
    redirect_markers_json: Mapped[str | None] = mapped_column(Text)
    fetch_config_json: Mapped[str | None] = mapped_column(Text)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(False))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(True))
    sort: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))


class PublishLink(TimestampMixin, Base):
    """B.20 回填发布链接（存活状态 + 按引擎收录状态）。"""

    __tablename__ = "publish_links"
    __table_args__ = (
        UniqueConstraint("url_hash", name="uq_publish_links_url_hash"),
        Index("ix_publish_links_content_id", "content_id"),
        Index("ix_publish_links_project_id_platform_id_published_at", "project_id", "platform_id", "published_at"),
        Index("ix_publish_links_platform_id_alive_status", "platform_id", "alive_status"),
        Index("ix_publish_links_is_monitoring_next_check_at", "is_monitoring", "next_check_at"),
        Index("ix_publish_links_is_monitoring_next_index_check_at", "is_monitoring", "next_index_check_at"),
        Index("ix_publish_links_alive_status", "alive_status"),
        Index("ix_publish_links_published_at", "published_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    platform_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    url_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    publish_account: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    backfilled_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title_snapshot: Mapped[str] = mapped_column(String(300), nullable=False)
    alive_status: Mapped[LinkAliveStatus] = mapped_column(String(20), nullable=False, **col_default("pending"))
    alive_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    consecutive_unknown: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    consecutive_suspected: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    baseline_title: Mapped[str | None] = mapped_column(String(300))
    baseline_simhash: Mapped[int | None] = mapped_column(BigInteger)
    baseline_excerpt: Mapped[str | None] = mapped_column(String(1000))
    baseline_captured_at: Mapped[datetime | None] = mapped_column(DateTime)
    seo_status_json: Mapped[str | None] = mapped_column(Text)
    geo_status_json: Mapped[str | None] = mapped_column(Text)
    seo_indexed_any: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(False))
    geo_cited_any: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(False))
    first_indexed_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_cited_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_index_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_index_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    index_check_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    index_checks_done: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    is_monitoring: Mapped[bool] = mapped_column(Boolean, nullable=False, **col_default(True))
    note: Mapped[str | None] = mapped_column(String(500))


class LinkCheck(TimestampMixin, Base):
    """B.21 删除检测记录（不可变；随链接级联删除）。"""

    __tablename__ = "link_checks"
    __table_args__ = (
        Index("ix_link_checks_link_id_checked_at", "link_id", "checked_at"),
        Index("ix_link_checks_checked_at", "checked_at"),
        Index("ix_link_checks_result_status_checked_at", "result_status", "checked_at"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_links.id", name="fk_link_checks_link_id", ondelete="CASCADE"), nullable=False
    )
    check_type: Mapped[CheckType] = mapped_column(String(16), nullable=False)
    result_status: Mapped[LinkCheckResult] = mapped_column(String(20), nullable=False)
    previous_status: Mapped[LinkAliveStatus] = mapped_column(String(20), nullable=False)
    applied_status: Mapped[LinkAliveStatus] = mapped_column(String(20), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    final_url: Mapped[str | None] = mapped_column(String(1000))
    redirect_count: Mapped[int] = mapped_column(TinyInt, nullable=False, **col_default(0))
    matched_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str | None] = mapped_column(String(300))
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    hamming_distance: Mapped[int | None] = mapped_column(TinyInt)
    response_bytes: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(500))
    evidence_json: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    triggered_by: Mapped[int | None] = mapped_column(BigInteger)


class IndexCheck(TimestampMixin, Base):
    """B.22 SEO/GEO 收录检测记录（不可变；随链接级联删除）。"""

    __tablename__ = "index_checks"
    __table_args__ = (
        Index("ix_index_checks_link_id_kind_engine_checked_at", "link_id", "kind", "engine", "checked_at"),
        Index("ix_index_checks_checked_at", "checked_at"),
        Index("ix_index_checks_kind_result_status_checked_at", "kind", "result_status", "checked_at"),
        Index("ix_index_checks_ai_task_id", "ai_task_id"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    link_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("publish_links.id", name="fk_index_checks_link_id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[IndexKind] = mapped_column(String(8), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    check_type: Mapped[IndexCheckType] = mapped_column(String(16), nullable=False)
    result_status: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    match_mode: Mapped[IndexMatchMode] = mapped_column(String(16), nullable=False)
    query_text: Mapped[str | None] = mapped_column(String(500))
    ai_task_id: Mapped[int | None] = mapped_column(BigInteger)
    request_id: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(120))
    evidence_title: Mapped[str | None] = mapped_column(String(300))
    evidence_snippet: Mapped[str | None] = mapped_column(String(1000))
    evidence_url: Mapped[str | None] = mapped_column(String(1000))
    evidence_json: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    error_category: Mapped[ErrorCategory | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(String(500))
    checked_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    triggered_by: Mapped[int | None] = mapped_column(BigInteger)


class Alert(TimestampMixin, Base):
    """B.23 告警（多态目标；``dedupe_key`` 去重）。"""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_status_severity_last_triggered_at", "status", "severity", "last_triggered_at"),
        Index("ix_alerts_dedupe_key_status", "dedupe_key", "status", mysql_length={"dedupe_key": 191}),
        Index("ix_alerts_project_id_status", "project_id", "status"),
        Index("ix_alerts_alert_type_created_at", "alert_type", "created_at"),
        Index("ix_alerts_target_type_target_key", "target_type", "target_key"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    alert_type: Mapped[AlertType] = mapped_column(String(40), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(String(10), nullable=False)
    status: Mapped[AlertStatus] = mapped_column(String(16), nullable=False, **col_default("open"))
    project_id: Mapped[int | None] = mapped_column(BigInteger)
    target_type: Mapped[AlertTargetType | None] = mapped_column(String(32))
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    target_key: Mapped[str] = mapped_column(String(160), nullable=False, **col_default(""))
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text)
    first_triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    trigger_count: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(1))
    acknowledged_by: Mapped[int | None] = mapped_column(BigInteger)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolution_note: Mapped[str | None] = mapped_column(String(500))
    notified_channels_json: Mapped[str | None] = mapped_column(Text)


class DailyStat(TimestampMixin, Base):
    """B.24 每日预聚合（每个 ``(stat_date, project_id, dimension, dimension_key)`` 一行，全量 upsert）。"""

    __tablename__ = "daily_stats"
    __table_args__ = (
        UniqueConstraint(
            "stat_date", "project_id", "dimension", "dimension_key",
            name="uq_daily_stats_stat_date_project_id_dimension_dimension_key",
        ),
        Index("ix_daily_stats_stat_date_dimension", "stat_date", "dimension"),
        Index("ix_daily_stats_project_id_stat_date", "project_id", "stat_date"),
        MYSQL_TABLE_KWARGS,
    )

    id: Mapped[int] = _pk()
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    dimension: Mapped[StatsDimension] = mapped_column(String(16), nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(120), nullable=False, **col_default(""))
    keywords_created: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    keywords_adopted: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    titles_created: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    titles_adopted: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    contents_created: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    contents_approved: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    contents_published: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_backfilled: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_checked: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_deleted: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_changed: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_restored: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_alive_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    links_total_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    seo_checks: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    seo_newly_indexed: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    seo_indexed_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    geo_checks: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    geo_newly_cited: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    geo_cited_snapshot: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    index_hours_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    index_hours_links: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    ai_calls: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    ai_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    ai_failed: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    ai_duration_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    tasks_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    tasks_failed: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    task_duration_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    completion_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    quota_estimated: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    quota_actual: Mapped[int] = mapped_column(BigInteger, nullable=False, **col_default(0))
    quota_reconciled_calls: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    cost_cny: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, **col_default(Decimal("0")))
    images_generated: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    videos_generated: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    media_failed: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    alerts_opened: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    alerts_resolved: Mapped[int] = mapped_column(Integer, nullable=False, **col_default(0))
    extra_json: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# daily_stats 的数值指标列（聚合 / 实时计数 / 维度矩阵共用）
DAILY_STATS_METRIC_COLUMNS: tuple[str, ...] = tuple(
    c.name
    for c in DailyStat.__table__.columns
    if c.name not in {"id", "stat_date", "project_id", "dimension", "dimension_key", "extra_json", "computed_at", "created_at", "updated_at"}
)

ALL_MODELS: tuple[type[Base], ...] = (
    Admin, AdminGroup, AdminPermission, AdminGroupPermission, AdminOperationLog, Setting,
    Project, PromptTemplate, GenerationBatch, Keyword, Title, Content, ContentVersion, MediaAsset,
    AiTask, AiModel, CapabilityRoute, AiUsageLog,
    PublishPlatform, PublishLink, LinkCheck, IndexCheck, Alert, DailyStat,
)
