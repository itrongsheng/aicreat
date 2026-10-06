"""AI 网关接口的请求 / 响应模型（docs/04 §6.15、§7.15~§7.17；docs/08 §6.7、§11.5、§12.3）。

请求体：``RouteUpsert``（``POST /admin/ai/routes``）/ ``RouteUpdate``（``PUT /admin/ai/routes/{id}``）/ ``RouteTestBody`` /
``ProbeBody``。主 / 备模型「存在于 ``ai_models`` 且模态匹配」与协议 ↔ 能力的匹配由 ``ai_gateway_service`` 校验（需要查库，
错误项同为 ``[{loc, msg, type, input}]``）。

响应模型 ``ModelOut`` / ``RouteOut`` / ``TaskOut`` / ``UsageLogOut`` 等只用于 OpenAPI 文档与前后端对照（service 直接返回 dict），
字段与 ``packages/shared/src/types.ts`` 同名。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.models import (
    AiTaskOperation,
    AiTaskStatus,
    AiTaskTargetType,
    AiTaskTriggerType,
    BreakerReason,
    BreakerState,
    Capability,
    ErrorCategory,
    HealthStatus,
    Modality,
    Protocol,
    ZhiqiMode,
)

ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]

MAX_FALLBACK_MODELS = 10
MAX_ROUTE_TIMEOUT_SECONDS = 3600
MAX_ROUTE_ATTEMPTS = 10

RowKind = Literal["root", "attempt", "all"]
UsageSummaryGroupBy = Literal["model", "capability", "project", "day"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


# =====================================================================
# 能力路由（/admin/ai/routes）
# =====================================================================


class RouteUpsert(_Body):
    """新建项目覆盖路由（``project_id > 0``；同能力同项目已存在 409）。"""

    capability: Capability
    project_id: int = Field(gt=0, description="项目 ID（只能新建项目覆盖行）")
    protocol: Protocol
    primary_model: ModelId
    fallback_models: list[ModelId] = Field(default_factory=list, max_length=MAX_FALLBACK_MODELS)
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(None, ge=1, le=MAX_ROUTE_TIMEOUT_SECONDS, description="NULL 取全局路由 / ai_routing_config.timeouts")
    max_attempts: int = Field(3, ge=1, le=MAX_ROUTE_ATTEMPTS)
    is_enabled: bool = True
    note: Note | None = None

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value: Any) -> Any:
        return _blank_to_none(value)


class RouteUpdate(_Body):
    """编辑路由：只写出现的字段；``capability`` / ``project_id`` 不可改（出现即 400）。"""

    protocol: Protocol | None = None
    primary_model: ModelId | None = None
    fallback_models: list[ModelId] | None = Field(None, max_length=MAX_FALLBACK_MODELS)
    params: dict[str, Any] | None = None
    timeout_seconds: int | None = Field(None, ge=1, le=MAX_ROUTE_TIMEOUT_SECONDS)
    max_attempts: int | None = Field(None, ge=1, le=MAX_ROUTE_ATTEMPTS)
    is_enabled: bool | None = None
    note: Note | None = None

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @model_validator(mode="after")
    def _not_null(self) -> RouteUpdate:
        """只有 ``timeout_seconds`` / ``note`` 可显式置空；其余字段出现时不能为 ``null``。"""
        for name in ("protocol", "primary_model", "fallback_models", "params", "max_attempts", "is_enabled"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} 不能为空")
        return self

    def changes(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class RouteTestBody(_Body):
    """一键测试：``image`` / ``video`` 仅 ``probe_media=true`` 时真实提交（产生计费）。"""

    probe_media: bool = False


class ProbeBody(_Body):
    """单模型探测（GEO / SEO 引擎配置页「测试模型」）。"""

    capability: Capability
    model: ModelId
    protocol: Protocol | None = None
    probe_media: bool = False


class RouteModelStatus(BaseModel):
    model: str
    candidate_index: int
    health: HealthStatus
    is_available: bool | None
    breaker_state: BreakerState
    breaker_reason: BreakerReason | None


class RouteOut(BaseModel):
    id: int
    capability: Capability
    project_id: int
    protocol: Protocol
    primary_model: str
    fallback_models: list[str]
    params: dict[str, Any]
    timeout_seconds: int | None
    max_attempts: int
    is_enabled: bool
    note: str | None
    updated_by: int | None
    created_at: str | None
    updated_at: str | None
    breaker_state: BreakerState | None = None
    breaker_reason: BreakerReason | None = None
    models: list[RouteModelStatus] | None = None
    warnings: list[dict[str, Any]] | None = None


class RouteTestItem(BaseModel):
    model: str
    status: HealthStatus
    latency_ms: int | None
    request_id: str | None
    error_category: ErrorCategory | None


class ResetBreakerOut(BaseModel):
    reset_models: list[str]
    paused_cleared: bool


class HealthResultOut(BaseModel):
    capability: Capability
    model: str
    protocol: Protocol | None
    status: HealthStatus
    latency_ms: int | None
    request_id: str | None
    error_category: ErrorCategory | None
    error_message: str | None
    checked_at: str


class HealthModelOut(BaseModel):
    capability: Capability
    model: str
    status: HealthStatus
    latency_ms: int | None
    checked_at: str | None
    breaker_state: BreakerState
    breaker_reason: BreakerReason | None
    is_available: bool | None


class WorkerHeartbeatOut(BaseModel):
    name: str
    hostname: str
    pid: int | str
    heartbeat_at: str | None
    alive: bool


class AiHealthOut(BaseModel):
    zhiqi_mode: ZhiqiMode
    base_url: str
    paused: dict[str, bool]
    models: list[HealthModelOut]
    workers: list[WorkerHeartbeatOut]


# =====================================================================
# 模型目录（/admin/ai/models）
# =====================================================================


class ModelOut(BaseModel):
    id: int
    model_id: str
    owned_by: str | None
    vendor_id: int | None
    vendor_name: str | None
    description: str | None
    tags: list[Any]
    icon: str | None
    cover_url: str | None
    supported_endpoint_types: list[str]
    modalities: list[Modality]
    quota_type: int
    model_ratio: float | None
    model_price: float | None
    completion_ratio: float | None
    cache_ratio: float | None
    create_cache_ratio: float | None
    enable_groups: list[str]
    billing_mode: str | None
    billing_expr: str | None
    model_price_type: str | None
    sort_order: int
    is_available: bool
    last_seen_at: str | None
    last_health_status: HealthStatus | None
    last_health_at: str | None
    last_health_latency_ms: int | None
    synced_at: str | None
    created_at: str | None
    updated_at: str | None
    raw_pricing: dict[str, Any] | None = None


class ModelOptionOut(BaseModel):
    model_id: str
    vendor_name: str | None
    modalities: list[Modality]
    is_available: bool
    last_health_status: HealthStatus | None
    quota_type: int


class ModelSyncOut(BaseModel):
    total: int
    added: int
    updated: int
    unavailable: int
    synced_at: str
    request_ids: dict[str, str | None]


# =====================================================================
# AI 任务（/admin/ai/tasks）
# =====================================================================


class TaskOut(BaseModel):
    id: int
    project_id: int | None
    capability: Capability
    operation: AiTaskOperation
    protocol: Protocol | None
    trigger_type: AiTaskTriggerType
    target_type: AiTaskTargetType | None
    target_id: int | None
    batch_id: int | None
    root_task_id: int | None
    parent_task_id: int | None
    route_id: int | None
    candidate_index: int
    attempt: int
    segment_index: int | None
    model: str | None
    template_id: int | None
    status: AiTaskStatus
    pause_count: int
    request_id: str | None
    upstream_task_id: str | None
    output_excerpt: str | None
    prompt_tokens: int
    completion_tokens: int
    cache_tokens: int
    quota_reserved: int
    quota_estimated: int
    quota_actual: int | None
    cost_cny: float | None
    reconciled_at: str | None
    usage_log_type: int | None
    error_category: ErrorCategory | None
    error_message: str | None
    http_status: int | None
    duration_ms: int | None
    upstream_latency_ms: int | None
    progress: int
    poll_count: int
    next_poll_at: str | None
    deadline_at: str | None
    locked_by: str | None
    heartbeat_at: str | None
    started_at: str | None
    finished_at: str | None
    created_by: int | None
    created_at: str | None
    updated_at: str | None
    input: dict[str, Any] | None = None
    request_payload: Any = None
    response_meta: dict[str, Any] | None = None
    attempts: list[TaskOut] | None = None


class TaskRetryOut(BaseModel):
    task_id: int


# =====================================================================
# 用量对账（/admin/ai/usage）
# =====================================================================


class UsageLogOut(BaseModel):
    id: int
    entry_hash: str
    upstream_log_id: int | None
    request_id: str | None
    log_type: int
    model_name: str | None
    group_name: str | None
    quota: int
    prompt_tokens: int
    completion_tokens: int
    cache_tokens: int
    group_ratio: float | None
    model_ratio: float | None
    completion_ratio: float | None
    request_path: str | None
    upstream_task_id: str | None
    upstream_created_at: str | None
    ai_task_id: int | None
    matched: bool
    matched_at: str | None
    pulled_at: str | None
    raw: Any = None
    created_at: str | None
    updated_at: str | None


class ReconcileOut(BaseModel):
    pulled: int
    new: int
    matched: int
    unmatched: int
    window_overflow: bool
    request_ids: list[str]


class UsageSummaryRow(BaseModel):
    key: str | None
    calls: int
    prompt_tokens: int
    completion_tokens: int
    quota_estimated: int
    quota_actual: int
    cost_cny: float
    reconciled_rate: float


class UsageLastPullOut(BaseModel):
    pulled_at: str | None
    window_overflow: bool
    pulled: int | None = None
    new: int | None = None
    matched: int | None = None
    unmatched: int | None = None
    request_ids: list[str] | None = None


__all__ = [
    "AiHealthOut",
    "HealthModelOut",
    "HealthResultOut",
    "ModelOptionOut",
    "ModelOut",
    "ModelSyncOut",
    "ProbeBody",
    "ReconcileOut",
    "ResetBreakerOut",
    "RouteModelStatus",
    "RouteOut",
    "RouteTestBody",
    "RouteTestItem",
    "RouteUpdate",
    "RouteUpsert",
    "RowKind",
    "TaskOut",
    "TaskRetryOut",
    "UsageLastPullOut",
    "UsageLogOut",
    "UsageSummaryGroupBy",
    "UsageSummaryRow",
    "WorkerHeartbeatOut",
]
