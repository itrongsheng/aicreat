"""AI 网关编排（docs/08-zhiqiapi-integration.md §5.13、§6、§7、§8、§10.2）。

职责：能力路由解析与缓存、候选链与模型覆盖、熔断判定、全局暂停、本地额度预占 / 结算、``ai_tasks`` 根任务与尝试行记录、
告警；业务 service 只经本模块调用上游（``core/zhiqi`` 只做传输与契约，不读库）。

记录规则（§7.4）：每次实际 HTTP 调用先 ``INSERT`` 尝试行（``running``，复制根任务冗余列）并提交，再调上游，拿到结果后立即
``UPDATE`` 并提交；未发起 HTTP 的 ``breaker_open`` / ``model_unrouted`` 也各记一行 ``failed`` 占位尝试行。根任务的合计列由
``finalize_root`` 在单元终态（与业务对象写入同一事务）汇总。Redis 写入（入队、``stats:rt``、额度结算）一律在事务提交之后。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import after_commit
from app.core.exceptions import (
    CODE_BAD_REQUEST,
    CODE_CAPABILITY_UNAVAILABLE,
    CODE_CONFLICT,
    CODE_QUOTA_LIMIT_REACHED,
    CODE_UPSTREAM_ERROR,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.redis import (
    cache_delete_prefix,
    cache_get_json,
    cache_set_json,
    redis_client,
)
from app.core.zhiqi import catalog, get_client, images, text, usage, videos
from app.core.zhiqi.breaker import CircuitBreaker
from app.core.zhiqi.errors import (
    ZhiqiBreakerOpen,
    ZhiqiError,
    is_fallbackable,
    sanitize_error_message,
)
from app.core.zhiqi.types import (
    MODALITY_OF,
    TEXT_CAPABILITIES,
    TEXT_PROTOCOLS,
    Capability,
    ErrorCategory,
    ImageRequest,
    ImageSubmitResult,
    ImageTaskStatus,
    Protocol,
    RetryPolicy,
    TaskStatus,
    TextRequest,
    TextResult,
    VideoRequest,
    VideoSubmitResult,
    VideoTaskStatus,
)
from app.models import AiModel, AiTask, CapabilityRoute, Project, PublishLink, utcnow
from app.schemas.common import iso_utc
from app.services import (
    ai_catalog_service,
    alert_service,
    settings_service,
    stats_service,
)
from app.services.data_scope_service import (
    SYSTEM_SCOPE,
    DataScope,
    get_visible,
    is_visible,
    require_project,
    scope_routes,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ResolvedRoute",
    "TaskAbandoned",
    "allowed_protocols",
    "check_paused",
    "check_quota",
    "clear_paused",
    "complete_text",
    "create_root_task",
    "create_route",
    "delete_route",
    "ensure_can_call",
    "ensure_default_routes",
    "estimate_for",
    "finalize_root",
    "get_breaker",
    "get_retry_policy",
    "get_route",
    "get_route_row",
    "health_key",
    "invalidate_routes_cache",
    "is_sync_root",
    "list_routes",
    "poll_task",
    "preflight",
    "quota_warning",
    "record_breaker_failure",
    "record_failure",
    "release_reservation",
    "reset_breaker",
    "reset_route_breakers",
    "resolve_route",
    "rollback_for_pause",
    "route_item",
    "route_models",
    "settle_quota",
    "start_polling",
    "submit_image",
    "submit_video",
    "update_route",
    "validate_model_override",
]

# =====================================================================
# 常量
# =====================================================================

ROUTE_CACHE_PREFIX = "cache:routes:"
ROUTE_CACHE_TTL = 60
PAUSED_PREFIX = "ai:paused:"
PAUSE_REASONS: tuple[str, ...] = ("quota_exceeded", "auth_failed")
QUEUE_AI_TASKS = "queue:ai_tasks"
QUOTA_DAILY_PREFIX = "quota:daily:"
QUOTA_PROJECT_PREFIX = "quota:project:"
QUOTA_DAILY_TTL = 172800
QUOTA_PROJECT_TTL = 40 * 86400
MAX_PAUSE_COUNT = 3
PAYLOAD_TEXT_LIMIT = 20000
OUTPUT_EXCERPT_LIMIT = 2000
POLL_REQUEST_IDS_KEEP = 20
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})
SYNC_ROOT_OPERATIONS = frozenset({"route_probe", "image_prompt"})
SYNC_ROOT_CAPABILITIES = frozenset({"seo_check", "geo_check"})
SYNC_ROOT_TRIGGERS = frozenset({"worker", "health_probe"})
PAUSE_CATEGORIES = frozenset({ErrorCategory.QUOTA_EXCEEDED, ErrorCategory.AUTH_FAILED})
CAPABILITY_UNAVAILABLE_MESSAGE = "能力暂不可用"
UPSTREAM_ERROR_MESSAGE = "上游服务调用失败"

MOCK_MODELS = {"text": "mock-text", "image": "mock-image", "video": "mock-video"}

# 8 条全局路由的 seed（docs/08 §6.2）：capability → (默认模型环境变量字段, params_json)
DEFAULT_ROUTE_SEEDS: dict[str, tuple[str, dict[str, Any]]] = {
    "keyword": ("zhiqi_text_default_model", {"temperature": 0.7, "max_tokens": 2048}),
    "title": ("zhiqi_text_default_model", {"temperature": 0.8, "max_tokens": 1024}),
    "content": ("zhiqi_text_default_model", {"temperature": 0.7, "max_tokens": 4096}),
    "rewrite": ("zhiqi_text_default_model", {"temperature": 0.7, "max_tokens": 4096}),
    "image": ("zhiqi_image_default_model", {"resolution": "1080p", "aspect_ratio": "16:9"}),
    "video": ("zhiqi_video_default_model", {"resolution": "720p", "duration": 5, "aspect_ratio": "16:9", "generate_audio": False}),
    "geo_check": ("zhiqi_geo_default_model", {"temperature": 0.2, "max_tokens": 1024}),
    "seo_check": ("zhiqi_seo_default_model", {"temperature": 0.2, "max_tokens": 1024}),
}

_UNSET: Any = object()


class TaskAbandoned(Exception):
    """根任务已不再由本执行者持有（被取消 / 回收 / 改派），本轮放弃：不写业务对象、不结算（docs/03「任务幂等」第 2 条）。"""


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
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _cfg(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "ai_routing_config")


def task_input(task: AiTask) -> dict[str, Any]:
    value = _loads(task.input_json, {})
    return value if isinstance(value, dict) else {}


def task_meta(task: AiTask) -> dict[str, Any]:
    value = _loads(task.response_meta_json, {})
    return value if isinstance(value, dict) else {}


def set_task_meta(task: AiTask, meta: Mapping[str, Any]) -> None:
    task.response_meta_json = _dumps(dict(meta)) if meta else None


def update_task_meta(task: AiTask, **values: Any) -> dict[str, Any]:
    meta = task_meta(task)
    meta.update(values)
    set_task_meta(task, meta)
    return meta


def is_sync_root(task: AiTask) -> bool:
    """同步执行的根任务（不经 ``queue:ai_tasks``）：``seo_check`` / ``geo_check`` 检测、健康探测、内嵌 ``image_prompt``。"""
    return (
        task.trigger_type in SYNC_ROOT_TRIGGERS
        or task.capability in SYNC_ROOT_CAPABILITIES
        or task.operation in SYNC_ROOT_OPERATIONS
    )


def _truncate_payload(value: Any) -> Any:
    """脱敏后的请求体：字符串超过 20000 字符截断（prompt），容器递归。"""
    if isinstance(value, str):
        return value[:PAYLOAD_TEXT_LIMIT]
    if isinstance(value, Mapping):
        return {
            str(k): _truncate_payload(v)
            for k, v in value.items()
            if str(k).lower() not in ("authorization", "api_key", "apikey")
        }
    if isinstance(value, (list, tuple)):
        return [_truncate_payload(v) for v in value]
    return value


def _payload_json(payload: Any) -> str | None:
    if payload is None:
        return None
    return _dumps(_truncate_payload(payload))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _pricing(cfg: Mapping[str, Any]) -> dict[str, Any]:
    pricing = dict(cfg.get("pricing") or {})
    return {
        "quota_per_unit": int(pricing.get("quota_per_unit") or settings.zhiqi_quota_per_unit or 500000),
        "usd_cny_rate": float(pricing.get("usd_cny_rate") if pricing.get("usd_cny_rate") is not None else settings.zhiqi_usd_cny_rate),
        "group_ratio": float(pricing.get("group_ratio") if pricing.get("group_ratio") is not None else settings.zhiqi_group_ratio),
    }


def quota_to_cny(cfg: Mapping[str, Any], quota: int) -> Decimal:
    pricing = _pricing(cfg)
    return usage.quota_to_cny(int(quota or 0), quota_per_unit=pricing["quota_per_unit"], usd_cny_rate=pricing["usd_cny_rate"])


# =====================================================================
# 熔断 / 重试 / 暂停
# =====================================================================


def get_breaker(config: Mapping[str, Any]) -> CircuitBreaker:
    """按当前 ``ai_routing_config.breaker`` / ``catalog`` 构造（每次调用）；``permanent_ttl = max(open_seconds, sync_interval) + 60``。"""
    cfg = dict(config or {})
    b = dict(cfg.get("breaker") or {})
    open_seconds = int(b.get("open_seconds") or settings.zhiqi_breaker_open_seconds)
    sync_interval = int((cfg.get("catalog") or {}).get("sync_interval_seconds") or 0)
    return CircuitBreaker(
        redis_client,
        failure_threshold=int(b.get("failure_threshold") or settings.zhiqi_breaker_failure_threshold),
        window_seconds=int(b.get("window_seconds") or settings.zhiqi_breaker_window_seconds),
        open_seconds=open_seconds,
        half_open_max_calls=int(b.get("half_open_max_calls") or 1),
        permanent_ttl_seconds=max(open_seconds, sync_interval) + 60,
    )


def get_retry_policy(config: Mapping[str, Any]) -> RetryPolicy:
    """按 ``ai_routing_config.retry`` 构造客户端 HTTP 幂等重试策略。"""
    return RetryPolicy.from_config(dict((config or {}).get("retry") or {}))


def check_paused() -> str | None:
    """返回 ``ai:paused:*`` 的 reason（``quota_exceeded`` / ``auth_failed``）或 ``None``；Redis 不可用时按未暂停处理。"""
    try:
        for reason in PAUSE_REASONS:
            if redis_client.exists(f"{PAUSED_PREFIX}{reason}"):
                return reason
    except redis.RedisError as exc:
        logger.warning("读取 ai:paused:* 失败: %s", exc)
    return None


def set_paused(reason: str, pause_seconds: int) -> None:
    """``SET ai:paused:{reason} <ISO 时间> EX pause_seconds``。"""
    redis_client.set(f"{PAUSED_PREFIX}{reason}", utcnow().isoformat() + "Z", ex=max(1, int(pause_seconds)))


def clear_paused() -> bool:
    """``DEL ai:paused:*``（``reset-breaker``、探测成功）；返回是否删除了任一键。"""
    return bool(redis_client.delete(*(f"{PAUSED_PREFIX}{r}" for r in PAUSE_REASONS)))


def reset_breaker(db: Session, capability: str, model: str, *, config: Mapping[str, Any] | None = None) -> bool:
    """重置 ``(capability, model)`` 熔断器；原为 open / half_open 时自动解决 ``ai_breaker_open``（只 ``flush``）。"""
    breaker = get_breaker(config or _cfg(db))
    was_open = breaker.reset(capability, model)
    if was_open:
        alert_service.resolve_alert(db, SYSTEM_SCOPE, "ai_breaker_open", "ai_model", f"{capability}:{model}")
    return was_open


# =====================================================================
# 能力路由（§6.2、§6.3、§6.4）
# =====================================================================


@dataclass
class ResolvedRoute:
    route_id: int; capability: Capability; protocol: Protocol
    candidates: list[str]                  # [primary, *fallbacks]，已剔除 ai_models.is_available=0 的模型；有 model_override 时固定为 [model_override]
    unavailable_models: list[str]          # 被剔除的模型（写入 5031 的 data.unavailable_models）
    params: dict[str, Any]; timeout_seconds: int; max_attempts: int
    model_override: str | None = None      # 请求级/引擎级指定模型
    route_project_id: int = 0              # 命中行的 project_id（0 = 全局行）
    fallback_models: list[str] = field(default_factory=list)


def invalidate_routes_cache() -> int:
    """写路由后 ``cache_delete_prefix("cache:routes:")``。"""
    return cache_delete_prefix(ROUTE_CACHE_PREFIX)


def _route_row_data(row: CapabilityRoute) -> dict[str, Any]:
    fallbacks = _loads(row.fallback_models_json, []) or []
    params = _loads(row.params_json, {}) or {}
    return {
        "id": row.id,
        "capability": row.capability,
        "project_id": int(row.project_id or 0),
        "protocol": row.protocol,
        "primary_model": row.primary_model or "",
        "fallback_models": [str(m) for m in fallbacks if isinstance(m, str) and m.strip()],
        "params": params if isinstance(params, dict) else {},
        "timeout_seconds": row.timeout_seconds,
        "max_attempts": int(row.max_attempts or 3),
        "is_enabled": bool(row.is_enabled),
    }


def load_route(db: Session, capability: str, project_id: int | None) -> dict[str, Any] | None:
    """命中行（项目覆盖优先，其次全局）及其生效超时（项目行非空 > 全局行非空），缓存 ``cache:routes:{capability}:{project_id}`` 60s。"""
    pid = int(project_id or 0)
    key = f"{ROUTE_CACHE_PREFIX}{capability}:{pid}"
    cached = cache_get_json(key)
    if isinstance(cached, dict) and cached.get("id"):
        return cached
    global_row = db.scalar(select(CapabilityRoute).where(CapabilityRoute.capability == capability, CapabilityRoute.project_id == 0))
    project_row = None
    if pid > 0:
        project_row = db.scalar(select(CapabilityRoute).where(CapabilityRoute.capability == capability, CapabilityRoute.project_id == pid))
    hit = project_row or global_row
    if hit is None:
        return None
    data = _route_row_data(hit)
    timeout = hit.timeout_seconds
    if timeout is None and global_row is not None:
        timeout = global_row.timeout_seconds
    data["route_timeout"] = timeout
    cache_set_json(key, data, ROUTE_CACHE_TTL)
    return data


def _unavailable_error(
    capability: str,
    *,
    breaker_open: Iterable[str] = (),
    unavailable_models: Iterable[str] = (),
    paused_reason: str | None = None,
    hint: str | None = None,
    error_category: str = "model_unrouted",
    error_message: str | None = None,
) -> BusinessError:
    data: dict[str, Any] = {
        "capability": capability,
        "breaker_open": list(breaker_open),
        "unavailable_models": list(unavailable_models),
        "paused_reason": paused_reason,
    }
    if hint:
        data["hint"] = hint
    err = BusinessError(CAPABILITY_UNAVAILABLE_MESSAGE, code=CODE_CAPABILITY_UNAVAILABLE, data=data)
    err.error_category = error_category  # type: ignore[attr-defined]  # worker 写根任务 error_category 用
    err.error_message = error_message or CAPABILITY_UNAVAILABLE_MESSAGE  # type: ignore[attr-defined]
    return err


def _upstream_error(err: ZhiqiError, *, model: str | None, hint: str | None) -> BusinessError:
    exc = BusinessError(
        UPSTREAM_ERROR_MESSAGE,
        code=CODE_UPSTREAM_ERROR,
        data={"error_category": err.category.value, "request_id": err.request_id, "model": model, "hint": hint},
    )
    exc.error_category = err.category.value  # type: ignore[attr-defined]
    exc.error_message = sanitize_error_message(err.message)  # type: ignore[attr-defined]
    exc.request_id = err.request_id  # type: ignore[attr-defined]
    return exc


def resolve_route(
    db: Session,
    capability: Capability,
    project_id: int | None,
    *,
    model_override: str | None = None,
    protocol_override: Protocol | None = None,
) -> ResolvedRoute:
    """项目覆盖优先，其次全局；``is_enabled=0`` → 5031；候选全部 ``is_available=0`` → 5031（``data.unavailable_models``）；
    候选在 ``ai_models`` 中不存在（尚未首次同步）时不剔除；``model_override`` 非空 → ``candidates=[model_override]``。"""
    capability = Capability(capability)
    data = load_route(db, capability.value, project_id)
    if data is None:
        raise _unavailable_error(capability.value, error_message="能力路由不存在")
    if not data.get("is_enabled"):
        raise _unavailable_error(capability.value, error_message="能力路由已禁用")
    override = (model_override or "").strip() or None
    if override:
        models = [override]
    else:
        models = list(dict.fromkeys(m for m in [data.get("primary_model") or "", *data.get("fallback_models", [])] if m))
    if not models:
        raise _unavailable_error(capability.value, error_message="能力路由未配置模型")
    availability = dict(db.execute(select(AiModel.model_id, AiModel.is_available).where(AiModel.model_id.in_(models))).all())
    candidates = [m for m in models if availability.get(m, True)]
    unavailable = [m for m in models if availability.get(m) is False or availability.get(m) == 0]
    if not candidates:
        raise _unavailable_error(
            capability.value,
            unavailable_models=unavailable,
            hint="model_override" if override else None,
            error_message="候选模型均不可用",
        )
    cfg = _cfg(db)
    timeouts = cfg.get("timeouts") or {}
    timeout = data.get("route_timeout")
    if timeout is None:
        key = "text_seconds" if capability in TEXT_CAPABILITIES else "submit_seconds"
        timeout = timeouts.get(key) or (settings.zhiqi_timeout_text_seconds if key == "text_seconds" else settings.zhiqi_timeout_submit_seconds)
    protocol = Protocol(protocol_override) if protocol_override else Protocol(data["protocol"])
    return ResolvedRoute(
        route_id=int(data["id"]),
        capability=capability,
        protocol=protocol,
        candidates=candidates,
        unavailable_models=unavailable,
        params=dict(data.get("params") or {}),
        timeout_seconds=int(timeout),
        max_attempts=max(1, int(data.get("max_attempts") or 3)),
        model_override=override,
        route_project_id=int(data.get("project_id") or 0),
        fallback_models=list(data.get("fallback_models") or []),
    )


def validate_model_override(db: Session, capability: Capability | str, model: str | None) -> None:
    """请求级 ``model?`` 校验：须存在于 ``ai_models`` 且 ``is_available=1`` 且模态含该能力的模态，否则 400 ``data={"model":…}``。"""
    if model is None or not str(model).strip():
        return
    row = ai_catalog_service.get_model_by_id(db, str(model).strip())
    modality = MODALITY_OF[Capability(capability)]
    if row is None or not row.is_available or not ai_catalog_service.model_supports(row, modality):
        raise BusinessError("模型不存在、不可用或模态不匹配", code=CODE_BAD_REQUEST, http_status=400, data={"model": model})


def preflight(db: Session, capability: Capability | str, project_id: int | None, *, model_override: str | None = None) -> ResolvedRoute:
    """生成类接口创建任务前的本地校验（docs/04 §5.2）：全局暂停 → 5031 ``paused_reason``；``resolve_route``；
    候选链各模型熔断状态快照（``half_open`` 视为可用），全部打开 → 5031 ``data.breaker_open``（覆盖模型另附 ``hint``）。"""
    capability = Capability(capability)
    reason = check_paused()
    if reason:
        raise _unavailable_error(capability.value, paused_reason=reason, error_category=reason, error_message="AI 调用已暂停")
    route = resolve_route(db, capability, project_id, model_override=model_override)
    breaker = get_breaker(_cfg(db))
    open_models = [m for m in route.candidates if breaker.state(capability.value, m) == "open"]
    if len(open_models) == len(route.candidates):
        raise _unavailable_error(
            capability.value,
            breaker_open=open_models,
            unavailable_models=route.unavailable_models,
            hint="model_override" if route.model_override else None,
            error_category="breaker_open",
            error_message="候选模型均已熔断",
        )
    return route


# =====================================================================
# 默认路由 seed（§6.2、§13.6）
# =====================================================================


def _default_protocol(capability: str) -> str:
    if capability == "image":
        return Protocol.IMAGE_ASYNC.value
    if capability == "video":
        return Protocol.VIDEO.value
    configured = (settings.zhiqi_text_default_protocol or "").strip().lower()
    return configured if configured in {p.value for p in TEXT_PROTOCOLS} else Protocol.OPENAI_CHAT.value


def _default_model(capability: str, *, mock: bool) -> str:
    if mock:
        return MOCK_MODELS[MODALITY_OF[Capability(capability)]]
    return str(getattr(settings, DEFAULT_ROUTE_SEEDS[capability][0], "") or "").strip()


def _insert_route_ignore(db: Session, values: dict[str, Any]) -> bool:
    """``INSERT … ON DUPLICATE KEY UPDATE id=id``（MySQL）/ ``ON CONFLICT DO NOTHING``（SQLite），返回是否新插入。"""
    table = CapabilityRoute.__table__
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        stmt = mysql_insert(table).values(**values)
        stmt = stmt.on_duplicate_key_update(id=table.c.id)
        return bool(db.execute(stmt).rowcount == 1)
    if dialect == "sqlite":
        stmt = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=["capability", "project_id"])
        return bool(db.execute(stmt).rowcount)
    exists = db.scalar(
        select(CapabilityRoute.id).where(CapabilityRoute.capability == values["capability"], CapabilityRoute.project_id == values["project_id"])
    )
    if exists:
        return False
    db.execute(table.insert().values(**values))
    return True


def ensure_default_routes(db: Session) -> None:
    """8 条全局路由不存在则按当前模式 seed（幂等）；非 Mock 且 ``primary_model`` 以 ``mock-`` 开头时用环境变量替换，
    环境变量为空则 ``is_enabled=0`` + 启动告警日志。调用方持 ``lock:bootstrap``。"""
    mock = settings.zhiqi_mock_mode
    changed = False
    now = utcnow()
    for capability, (_env_field, params) in DEFAULT_ROUTE_SEEDS.items():
        model = _default_model(capability, mock=mock)
        values = {
            "capability": capability,
            "project_id": 0,
            "protocol": _default_protocol(capability),
            "primary_model": model,
            "fallback_models_json": "[]",
            "params_json": _dumps(params),
            "timeout_seconds": None,
            "max_attempts": 3,
            "is_enabled": bool(model),
            "created_at": now,
            "updated_at": now,
        }
        if _insert_route_ignore(db, values):
            changed = True
            if not model:
                logger.warning("能力 %s 的默认模型环境变量为空，全局路由已禁用（is_enabled=0），该能力的生成接口将返回 5031", capability)
    if not mock:
        rows = db.scalars(select(CapabilityRoute).where(CapabilityRoute.project_id == 0)).all()
        for row in rows:
            if not (row.primary_model or "").startswith("mock-") or row.capability not in DEFAULT_ROUTE_SEEDS:
                continue
            model = _default_model(row.capability, mock=False)
            if model:
                logger.info("真实模式：能力 %s 的全局路由主模型 %s 替换为 %s", row.capability, row.primary_model, model)
                row.primary_model = model
            else:
                logger.warning("真实模式：能力 %s 的全局路由主模型为 %s 且默认模型环境变量为空，路由已禁用", row.capability, row.primary_model)
                row.is_enabled = False
            changed = True
    if changed:
        after_commit(db, invalidate_routes_cache)
    db.commit()


# =====================================================================
# 路由管理（/admin/ai/routes*，docs/04 §6.15、docs/08 §6.7、§11.4、§12.5；docs/13 §6.3）
# =====================================================================

HEALTH_KEY_PREFIX = "ai:health:"
ROUTE_MODEL_UNAVAILABLE_MSG = "模型当前不可用，运行期将被跳过"
ROUTE_INVALID_MODEL_MSG = "模型不存在或模态不匹配"
ROUTE_WRITABLE_FIELDS = ("protocol", "primary_model", "fallback_models", "params", "timeout_seconds", "max_attempts", "is_enabled", "note")


def health_key(capability: str, model: str) -> str:
    """``ai:health:{capability}:{model}``（§12.4 探测快照）。"""
    return f"{HEALTH_KEY_PREFIX}{capability}:{model}"


def allowed_protocols(capability: Capability | str) -> frozenset[str]:
    """能力可用的路由协议：文本三协议；``image`` → ``image_async``；``video`` → ``video``（docs/03 B.17）。"""
    capability = Capability(capability)
    if capability == Capability.IMAGE:
        return frozenset({Protocol.IMAGE_ASYNC.value})
    if capability == Capability.VIDEO:
        return frozenset({Protocol.VIDEO.value})
    return frozenset(p.value for p in TEXT_PROTOCOLS)


def route_models(row: CapabilityRoute) -> list[str]:
    """``[primary_model, *fallback_models]``（去空、去重保序）。"""
    data = _route_row_data(row)
    return list(dict.fromkeys(m for m in [data["primary_model"], *data["fallback_models"]] if m))


def route_item(db: Session, row: CapabilityRoute, *, with_status: bool = True, breaker: CircuitBreaker | None = None) -> dict[str, Any]:
    """路由对象（``GET /admin/ai/routes/{id}`` 结构）；``with_status`` 时附主模型 ``breaker_state`` / ``breaker_reason`` 与
    ``models[]``（按候选链顺序的 ``{model, candidate_index, health, is_available, breaker_state, breaker_reason}``）。"""
    data = _route_row_data(row)
    item: dict[str, Any] = {
        "id": row.id,
        "capability": row.capability,
        "project_id": int(row.project_id or 0),
        "protocol": row.protocol,
        "primary_model": data["primary_model"],
        "fallback_models": data["fallback_models"],
        "params": data["params"],
        "timeout_seconds": row.timeout_seconds,
        "max_attempts": int(row.max_attempts or 3),
        "is_enabled": bool(row.is_enabled),
        "note": row.note,
        "updated_by": row.updated_by,
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }
    if not with_status:
        return item
    breaker = breaker or get_breaker(_cfg(db))
    models = [data["primary_model"], *data["fallback_models"]]
    names = [m for m in models if m]
    rows = {r.model_id: r for r in db.scalars(select(AiModel).where(AiModel.model_id.in_(names))).all()} if names else {}
    statuses: list[dict[str, Any]] = []
    for index, model in enumerate(models):
        if not model:
            continue
        state = breaker.state(row.capability, model)
        snapshot = cache_get_json(health_key(row.capability, model))
        model_row = rows.get(model)
        health = (snapshot or {}).get("status") if isinstance(snapshot, dict) else None
        if not health and model_row is not None:
            health = model_row.last_health_status
        statuses.append(
            {
                "model": model,
                "candidate_index": index,
                "health": health or "unknown",
                "is_available": bool(model_row.is_available) if model_row is not None else None,
                "breaker_state": state,
                "breaker_reason": breaker.reason(row.capability, model) if state != "closed" else None,
            }
        )
    primary = statuses[0] if statuses and statuses[0]["candidate_index"] == 0 else None
    item["breaker_state"] = primary["breaker_state"] if primary else "closed"
    item["breaker_reason"] = primary["breaker_reason"] if primary else None
    item["models"] = statuses
    return item


def list_routes(db: Session, scope: DataScope, *, project_id: int | None = None) -> list[dict[str, Any]]:
    """``GET /admin/ai/routes``（不分页）：缺省返回全部全局路由与可见项目的覆盖行；``project_id`` 指定时返回全局行 +
    该项目的覆盖行（项目不可见时只返回全局行，docs/13 §6.3）。按能力枚举顺序、全局行在前排序。"""
    stmt = select(CapabilityRoute)
    if project_id:
        project = db.get(Project, project_id)
        if project is not None and is_visible(db, scope, project):
            stmt = stmt.where(CapabilityRoute.project_id.in_((0, int(project_id))))
        else:
            stmt = stmt.where(CapabilityRoute.project_id == 0)
    else:
        stmt = scope_routes(stmt, scope)
    rows = list(db.scalars(stmt).all())
    order = {c.value: i for i, c in enumerate(Capability)}
    rows.sort(key=lambda r: (int(r.project_id or 0) != 0, int(r.project_id or 0), order.get(r.capability, 99), r.id))
    breaker = get_breaker(_cfg(db))
    return [route_item(db, row, breaker=breaker) for row in rows]


def get_route_row(db: Session, scope: DataScope, route_id: int) -> CapabilityRoute:
    """读取路由；项目覆盖行须属于可见项目（否则与不存在相同，404）。"""
    return get_visible(db, scope, CapabilityRoute, route_id)


def get_route(db: Session, scope: DataScope, route_id: int) -> dict[str, Any]:
    return route_item(db, get_route_row(db, scope, route_id))


def _validate_route(db: Session, capability: str, values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """校验协议与主 / 备模型（docs/08 §11.4 第 4 条）：模型须存在于 ``ai_models`` 且 ``modalities_json`` 含该能力的模态，
    否则 400（``type=invalid_model``）；``is_available=0`` 的模型允许保存，返回 ``warnings[]``（``type=model_unavailable``）。"""
    errors: list[dict[str, Any]] = []
    protocol = values.get("protocol")
    if protocol not in allowed_protocols(capability):
        errors.append(field_error(["body", "protocol"], "协议与能力不匹配", "invalid_protocol", protocol))
    modality = MODALITY_OF[Capability(capability)]
    primary = str(values.get("primary_model") or "").strip()
    fallbacks = [str(m or "").strip() for m in values.get("fallback_models") or []]
    names = [m for m in [primary, *fallbacks] if m]
    rows = {r.model_id: r for r in db.scalars(select(AiModel).where(AiModel.model_id.in_(names))).all()} if names else {}
    warnings: list[dict[str, Any]] = []

    def _check(loc: list[Any], model: str) -> None:
        row = rows.get(model)
        if not model or row is None or not ai_catalog_service.model_supports(row, modality):
            errors.append(field_error(loc, ROUTE_INVALID_MODEL_MSG, "invalid_model", model))
        elif not row.is_available:
            warnings.append(field_error(loc, ROUTE_MODEL_UNAVAILABLE_MSG, "model_unavailable", model))

    _check(["body", "primary_model"], primary)
    seen = {primary} if primary else set()
    for index, model in enumerate(fallbacks):
        loc: list[Any] = ["body", "fallback_models", index]
        if model and model in seen:
            errors.append(field_error(loc, "备选模型与主模型或其它备选模型重复", "duplicate_model", model))
            continue
        seen.add(model)
        _check(loc, model)
    if errors:
        raise invalid_params(errors)
    return warnings


def _apply_route_values(row: CapabilityRoute, values: Mapping[str, Any], *, admin_id: int | None) -> None:
    for key in ROUTE_WRITABLE_FIELDS:
        if key not in values:
            continue
        value = values[key]
        if key == "primary_model":
            row.primary_model = str(value).strip()
        elif key == "fallback_models":
            row.fallback_models_json = _dumps([str(m).strip() for m in value or []])
        elif key == "params":
            row.params_json = _dumps(dict(value or {}))
        elif key == "note":
            row.note = (str(value).strip() or None) if value is not None else None
        else:
            setattr(row, key, value)
    row.updated_by = admin_id


def create_route(db: Session, scope: DataScope, values: Mapping[str, Any], *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/ai/routes``：新建项目覆盖路由（``project_id > 0`` 且项目可见，否则 404）；同能力同项目已存在 → 409
    ``{"existing_id"}``；模型校验见 ``_validate_route``；保存后清 ``cache:routes:*``。返回路由对象 + ``warnings[]``。"""
    capability = Capability(values["capability"]).value
    project_id = int(values["project_id"])
    if project_id <= 0:
        raise invalid_params(field_error(["body", "project_id"], "只能新建项目覆盖路由（project_id > 0）", "greater_than", project_id))
    require_project(db, scope, project_id)
    existing = db.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == capability, CapabilityRoute.project_id == project_id))
    if existing:
        raise BusinessError("该项目已存在此能力的覆盖路由", code=CODE_CONFLICT, http_status=409, data={"existing_id": existing})
    merged = {
        "protocol": values.get("protocol"),
        "primary_model": values.get("primary_model"),
        "fallback_models": values.get("fallback_models") or [],
        "params": values.get("params") or {},
        "timeout_seconds": values.get("timeout_seconds"),
        "max_attempts": values.get("max_attempts") or 3,
        "is_enabled": True if values.get("is_enabled") is None else bool(values.get("is_enabled")),
        "note": values.get("note"),
    }
    warnings = _validate_route(db, capability, merged)
    row = CapabilityRoute(capability=capability, project_id=project_id, protocol=str(merged["protocol"]), primary_model="")
    _apply_route_values(row, merged, admin_id=admin_id)
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == capability, CapabilityRoute.project_id == project_id))
        raise BusinessError("该项目已存在此能力的覆盖路由", code=CODE_CONFLICT, http_status=409, data={"existing_id": existing}) from None
    after_commit(db, invalidate_routes_cache)
    db.commit()
    item = route_item(db, row)
    item["warnings"] = warnings
    return item


def update_route(db: Session, scope: DataScope, route_id: int, values: Mapping[str, Any], *, admin_id: int | None) -> dict[str, Any]:
    """``PUT /admin/ai/routes/{id}``：可改 ``protocol`` / ``primary_model`` / ``fallback_models`` / ``params`` /
    ``timeout_seconds`` / ``max_attempts`` / ``is_enabled`` / ``note``（``capability`` / ``project_id`` 不可改）；
    模型校验与 ``warnings[]`` 同新建；保存后清 ``cache:routes:*``。"""
    row = get_route_row(db, scope, route_id)
    current = _route_row_data(row)
    merged = {
        "protocol": values.get("protocol", row.protocol),
        "primary_model": values.get("primary_model", current["primary_model"]),
        "fallback_models": values.get("fallback_models", current["fallback_models"]),
    }
    warnings = _validate_route(db, row.capability, merged)
    _apply_route_values(row, {k: v for k, v in values.items() if k in ROUTE_WRITABLE_FIELDS}, admin_id=admin_id)
    after_commit(db, invalidate_routes_cache)
    db.commit()
    item = route_item(db, row)
    item["warnings"] = warnings
    return item


def delete_route(db: Session, scope: DataScope, route_id: int) -> None:
    """``DELETE /admin/ai/routes/{id}``：仅项目覆盖行（全局路由 409）；删除后清 ``cache:routes:*``。"""
    row = get_route_row(db, scope, route_id)
    if int(row.project_id or 0) == 0:
        raise BusinessError("全局路由不可删除", code=CODE_CONFLICT, http_status=409, data={"reason": "global_route"})
    db.delete(row)
    after_commit(db, invalidate_routes_cache)
    db.commit()


def reset_route_breakers(db: Session, scope: DataScope, route_id: int) -> dict[str, Any]:
    """``POST /admin/ai/routes/{id}/reset-breaker``：清除该路由主 / 备模型的熔断状态（原为 open / half_open 的自动解决
    ``ai_breaker_open``）并 ``DEL ai:paused:*`` → ``{reset_models[], paused_cleared}``。"""
    row = get_route_row(db, scope, route_id)
    cfg = _cfg(db)
    models = route_models(row)
    for model in models:
        reset_breaker(db, row.capability, model, config=cfg)
    paused_cleared = clear_paused()
    db.commit()
    return {"reset_models": models, "paused_cleared": paused_cleared}


# =====================================================================
# 额度（§7.6、§8.6）
# =====================================================================

_QUOTA_SETTLE_SCRIPT = redis_client.register_script(
    """
local delta = tonumber(ARGV[1])
for i, key in ipairs(KEYS) do
    if delta > 0 or redis.call('EXISTS', key) == 1 then
        redis.call('INCRBY', key, delta)
        redis.call('EXPIRE', key, tonumber(ARGV[i + 1]))
    end
end
return 1
"""
)


def _quota_keys(db: Session, project_id: int | None, at: Any = None) -> list[tuple[str, int]]:
    day = stats_service.local_date(at, db) if at is not None else stats_service.today_date(db)
    keys = [(f"{QUOTA_DAILY_PREFIX}{day.isoformat()}", QUOTA_DAILY_TTL)]
    if project_id:
        keys.append((f"{QUOTA_PROJECT_PREFIX}{int(project_id)}:{day.strftime('%Y-%m')}", QUOTA_PROJECT_TTL))
    return keys


def _quota_limits(db: Session) -> dict[str, int]:
    quota = (settings_service.get_config(db, "generation_config").get("quota") or {})
    return {
        "daily_limit": int(quota.get("daily_limit") or 0),
        "project_monthly_limit": int(quota.get("project_monthly_limit") or 0),
        "warn_percent": int(quota.get("warn_percent") or 80),
    }


def check_quota(db: Session, *, project_id: int | None, estimated_quota: int, task: AiTask) -> None:
    """日 / 项目月上限 → 4291 ``data={"scope","limit","used"}``（``used`` 含本次预占）；通过则 ``INCRBY quota:daily`` /
    ``quota:project`` 并累加 ``task.quota_reserved``。上限为 0 不限。预占立即生效（调用方事务失败时用 ``release_reservation`` 回滚）。"""
    estimated = max(0, int(estimated_quota or 0))
    if estimated <= 0:
        return
    limits = _quota_limits(db)
    keys = _quota_keys(db, project_id)
    daily_key, daily_ttl = keys[0]
    pipe = redis_client.pipeline()
    pipe.incrby(daily_key, estimated)
    pipe.expire(daily_key, daily_ttl)
    used_daily = int(pipe.execute()[0])
    if limits["daily_limit"] > 0 and used_daily > limits["daily_limit"]:
        redis_client.decrby(daily_key, estimated)
        raise BusinessError(
            "今日 AI 额度已达上限", code=CODE_QUOTA_LIMIT_REACHED,
            data={"scope": "daily", "limit": limits["daily_limit"], "used": used_daily},
        )
    if len(keys) > 1:
        project_key, project_ttl = keys[1]
        pipe = redis_client.pipeline()
        pipe.incrby(project_key, estimated)
        pipe.expire(project_key, project_ttl)
        used_project = int(pipe.execute()[0])
        if limits["project_monthly_limit"] > 0 and used_project > limits["project_monthly_limit"]:
            redis_client.decrby(project_key, estimated)
            redis_client.decrby(daily_key, estimated)
            raise BusinessError(
                "项目本月 AI 额度已达上限", code=CODE_QUOTA_LIMIT_REACHED,
                data={"scope": "project_monthly", "limit": limits["project_monthly_limit"], "used": used_project},
            )
    task.quota_reserved = int(task.quota_reserved or 0) + estimated


def release_reservation(db: Session, task: AiTask) -> None:
    """立即回滚 ``task`` 的预占（创建根任务的事务失败时由调用方使用；不改 ``quota_reserved`` 列）。"""
    reserved = int(task.quota_reserved or 0)
    if reserved <= 0:
        return
    for key, _ttl in _quota_keys(db, task.project_id):
        try:
            redis_client.decrby(key, reserved)
        except redis.RedisError as exc:
            logger.warning("回滚额度预占失败 key=%s: %s", key, exc)


def settle_quota(db: Session, task: AiTask, *, reserved: int, actual: int) -> None:
    """根任务终态结算（提交后执行）：对预占时的日 / 月键 ``INCRBY (actual − reserved)``；差值为负且键已过期时不写。"""
    delta = int(actual or 0) - int(reserved or 0)
    if delta == 0:
        return
    keys = _quota_keys(db, task.project_id, task.created_at or utcnow())

    def _apply() -> None:
        try:
            _QUOTA_SETTLE_SCRIPT(keys=[k for k, _ in keys], args=[delta, *[ttl for _, ttl in keys]])
        except redis.RedisError as exc:
            logger.warning("额度结算写入失败 task_id=%s delta=%s: %s", task.id, delta, exc)

    after_commit(db, _apply)


def quota_warning(db: Session, *, project_id: int | None) -> dict[str, Any] | None:
    """额度预警（docs/09 §9.6）：在全部根任务预占完成后调用；``used ≥ limit × warn_percent / 100`` 的 scope 中取
    ``percent`` 较高者（相同取 ``daily``）返回 ``{scope, limit, used, percent}``；上限为 0 不预警。"""
    limits = _quota_limits(db)
    keys = _quota_keys(db, project_id)
    try:
        values = redis_client.mget([k for k, _ in keys])
    except redis.RedisError as exc:
        logger.warning("读取额度计数失败: %s", exc)
        return None
    candidates: list[dict[str, Any]] = []
    for (scope, limit), raw in zip(
        (("daily", limits["daily_limit"]), ("project_monthly", limits["project_monthly_limit"])), list(values) + [None], strict=False
    ):
        if limit <= 0 or raw is None:
            continue
        used = int(raw)
        if used * 100 >= limit * limits["warn_percent"]:
            candidates.append({"scope": scope, "limit": limit, "used": used, "percent": used * 100 // limit})
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item["percent"], item["scope"] == "daily"))


def estimate_for(db: Session, model: str, prompt_tokens: int, completion_tokens: int) -> int:
    """查 ``ai_models`` 价格快照 + ``ai_routing_config.pricing`` 估算额度；``quota_type=1`` 走 ``model_price`` 路径；
    模型不在快照中时 ``model_ratio=1``、``completion_ratio=1``、``quota_type=0`` 并记 warning。"""
    cfg = _cfg(db)
    pricing = _pricing(cfg)
    row = ai_catalog_service.get_model_by_id(db, model) if model else None
    if row is None:
        logger.warning("模型 %s 不在价格快照中，按缺省倍率估算额度", model)
        model_ratio, completion_ratio, quota_type, model_price = 1.0, 1.0, 0, None
    else:
        model_ratio = float(row.model_ratio) if row.model_ratio is not None else 1.0
        completion_ratio = float(row.completion_ratio) if row.completion_ratio is not None else 1.0
        quota_type = int(row.quota_type or 0)
        model_price = float(row.model_price) if row.model_price is not None else None
    return usage.estimate_quota(
        prompt_tokens=int(prompt_tokens or 0),
        completion_tokens=int(completion_tokens or 0),
        model_ratio=model_ratio,
        completion_ratio=completion_ratio,
        group_ratio=pricing["group_ratio"],
        quota_type=quota_type,
        model_price=model_price,
        quota_per_unit=pricing["quota_per_unit"],
    )


def estimate_route(db: Session, route: ResolvedRoute, *, prompt: str = "", completion_tokens: int | None = None, calls: int = 1) -> int:
    """``estimate_for(db, model=候选链首个模型, prompt_tokens=estimate_tokens(prompt), completion_tokens=params.max_tokens)`` × ``calls``。"""
    if completion_tokens is None:
        completion_tokens = int(route.params.get("max_tokens") or 0) if route.capability in TEXT_CAPABILITIES else 0
    per_call = estimate_for(db, route.candidates[0], usage.estimate_tokens(prompt or ""), completion_tokens)
    return per_call * max(1, int(calls))


# =====================================================================
# 根任务
# =====================================================================


def default_worker_id() -> str:
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def create_root_task(
    db: Session,
    *,
    capability: Capability | str,
    operation: str,
    project_id: int | None,
    created_by: int | None,
    trigger_type: str = "user",
    target_type: str | None = None,
    target_id: int | None = None,
    batch_id: int | None = None,
    template_id: int | None = None,
    input: Mapping[str, Any] | None = None,  # noqa: A002
    model: str | None = None,
    route: ResolvedRoute | None = None,
    parent_task_id: int | None = None,
    candidate_index: int = 0,
    sync: bool = False,
    worker_id: str | None = None,
    enqueue: bool = True,
) -> AiTask:
    """新建根任务行（只 ``flush``，由调用方提交）。

    - ``model`` 缺省取 ``input.model``（请求级覆盖）或 ``route`` 的首个候选；``route_id`` 取 ``route``；
    - ``sync=False``：``status=queued``，事务提交后 ``RPUSH queue:ai_tasks``（``enqueue=False`` 时不入队）；
    - ``sync=True``（检测、探测、内嵌 ``image_prompt``）：直接以 ``running`` 创建，``locked_by`` = 执行进程、``heartbeat_at = started_at``；
    - ``candidate_index`` 为起始候选（轮询阶段 ``media_storage`` 备选回退从下一候选起）。
    """
    data = dict(input or {})
    chosen = model or data.get("model") or (route.candidates[0] if route and route.candidates else "") or ""
    now = utcnow()
    task = AiTask(
        project_id=project_id,
        capability=Capability(capability).value,
        operation=operation,
        input_json=_dumps(data),
        trigger_type=trigger_type,
        target_type=target_type,
        target_id=target_id,
        batch_id=batch_id,
        parent_task_id=parent_task_id,
        route_id=route.route_id if route else None,
        candidate_index=int(candidate_index or 0),
        attempt=1,
        model=str(chosen)[:120],
        template_id=template_id if template_id is not None else data.get("template_id"),
        status="running" if sync else "queued",
        created_by=created_by,
    )
    if sync:
        task.locked_by = worker_id or default_worker_id()
        task.started_at = now
        task.heartbeat_at = now
    db.add(task)
    db.flush()
    if not sync and enqueue:
        task_id = task.id
        after_commit(db, lambda: enqueue_task(task_id))
    return task


def enqueue_task(task_id: int, *, front: bool = False) -> None:
    """``RPUSH queue:ai_tasks <id>``（``front=True`` 时 ``LPUSH`` 插队）。"""
    try:
        if front:
            redis_client.lpush(QUEUE_AI_TASKS, task_id)
        else:
            redis_client.rpush(QUEUE_AI_TASKS, task_id)
    except redis.RedisError as exc:  # 队列丢失由 recover_stale_tasks ③ 按库补扫
        logger.warning("入队 queue:ai_tasks 失败 task_id=%s: %s", task_id, exc)


def rollback_for_pause(db: Session, root_task: AiTask, *, commit: bool = True) -> bool:
    """全局暂停回滚（§8.5）：``pause_count += 1``；``pause_count < 3`` 且为队列根任务 → ``queued``，清空
    ``locked_by`` / ``heartbeat_at`` / ``started_at``（不结算、不收敛、不入队），返回 ``True``；否则返回 ``False``（按常规 failed）。"""
    root_task.pause_count = int(root_task.pause_count or 0) + 1
    rolled = (
        root_task.root_task_id is None
        and root_task.pause_count < MAX_PAUSE_COUNT
        and not is_sync_root(root_task)
        and root_task.status in ("running", "queued")
    )
    if rolled:
        root_task.status = "queued"
        root_task.locked_by = None
        root_task.heartbeat_at = None
        root_task.started_at = None
    if commit:
        db.commit()
    return rolled


def _guard(db: Session, root_task: AiTask) -> None:
    """每次上游调用前：``check_paused()`` 非空 → 回滚并抛出（``queued`` 时抛 ``ZhiqiBreakerOpen``，否则抛暂停分类的
    ``ZhiqiError`` 由调用方按常规 failed）；根任务由执行者持有时重读 ``status='running' AND locked_by=self``，不满足抛 ``TaskAbandoned``。"""
    reason = check_paused()
    if reason:
        if rollback_for_pause(db, root_task):
            raise ZhiqiBreakerOpen(f"AI 调用已暂停（{reason}），任务已回滚为排队")
        raise ZhiqiError(ErrorCategory(reason), f"AI 调用已暂停（{reason}）")
    if root_task.locked_by:
        row = db.execute(select(AiTask.status, AiTask.locked_by).where(AiTask.id == root_task.id)).first()
        if row is None or row.status != "running" or row.locked_by != root_task.locked_by:
            raise TaskAbandoned(f"根任务 {root_task.id} 已不由本执行者持有")


ensure_can_call = _guard


def finalize_root(
    db: Session,
    root_task: AiTask,
    status: str,
    *,
    error_category: str | None = None,
    error_message: str | None = None,
    output_excerpt: str | None = None,
) -> None:
    """单元终态（只 ``flush``，与业务对象写入同一事务）：汇总尝试行（tokens / 额度 / 成本求和，``model`` / ``protocol`` /
    ``request_id`` / ``candidate_index`` 取最终成功行，全部失败取最后一行），写 ``status`` / ``finished_at`` / ``duration_ms``，
    ``settle_quota``（成功按 Σ 尝试行 ``quota_estimated``，失败 / 取消 / 过期按 0）；提交后写 ``stats:rt``。"""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"不是终态：{status}")
    attempts = db.scalars(select(AiTask).where(AiTask.root_task_id == root_task.id).order_by(AiTask.id)).all()
    now = utcnow()
    root_task.prompt_tokens = sum(int(a.prompt_tokens or 0) for a in attempts)
    root_task.completion_tokens = sum(int(a.completion_tokens or 0) for a in attempts)
    root_task.cache_tokens = sum(int(a.cache_tokens or 0) for a in attempts)
    root_task.quota_estimated = sum(int(a.quota_estimated or 0) for a in attempts)
    actuals = [int(a.quota_actual) for a in attempts if a.quota_actual is not None]
    root_task.quota_actual = sum(actuals) if actuals else None
    costs = [Decimal(a.cost_cny) for a in attempts if a.cost_cny is not None]
    root_task.cost_cny = sum(costs, Decimal(0)) if costs else None
    succeeded = [a for a in attempts if a.status == "succeeded"]
    final = succeeded[-1] if succeeded else (attempts[-1] if attempts else None)
    if final is not None:
        root_task.model = final.model
        root_task.protocol = final.protocol
        root_task.request_id = final.request_id
        root_task.candidate_index = final.candidate_index
        if not root_task.upstream_task_id and final.upstream_task_id:
            root_task.upstream_task_id = final.upstream_task_id
        if output_excerpt is None and status == "succeeded":
            output_excerpt = final.output_excerpt
    root_task.status = status
    root_task.finished_at = now
    started = root_task.started_at or root_task.created_at or now
    root_task.duration_ms = max(0, int((now - started).total_seconds() * 1000))
    if status == "succeeded":
        root_task.progress = 100
        root_task.error_category = None
        root_task.error_message = None
    elif status == "cancelled":
        root_task.error_category = "cancelled"
        root_task.error_message = sanitize_error_message(error_message) if error_message else root_task.error_message
    else:
        category = error_category or root_task.error_category or (final.error_category if final else None) or "unknown"
        message = error_message or root_task.error_message or (final.error_message if final else None)
        root_task.error_category = category
        root_task.error_message = sanitize_error_message(message) if message else None
    if output_excerpt is not None:
        root_task.output_excerpt = output_excerpt[:OUTPUT_EXCERPT_LIMIT]
    db.flush()
    settle_quota(
        db, root_task, reserved=int(root_task.quota_reserved or 0),
        actual=int(root_task.quota_estimated or 0) if status == "succeeded" else 0,
    )
    if root_task.trigger_type != "health_probe" and status in ("succeeded", "failed", "expired"):
        fields = (
            {"tasks_succeeded": 1, "task_duration_ms_sum": root_task.duration_ms or 0}
            if status == "succeeded"
            else {"tasks_failed": 1}
        )
        stats_service.increment_realtime_after_commit(db, root_task.project_id, fields, at=now)


def start_polling(
    db: Session, root_task: AiTask, attempt: AiTask, *, first_interval_seconds: int, budget_seconds: int
) -> None:
    """媒体提交成功后根任务进入 ``polling``（只 ``flush``）：``upstream_task_id``、``next_poll_at = now + intervals[0]``、
    ``deadline_at = now + poll_budget_seconds``，并冗余提交尝试行的 ``model`` / ``protocol`` / ``request_id`` / ``candidate_index``。"""
    now = utcnow()
    root_task.status = "polling"
    root_task.upstream_task_id = attempt.upstream_task_id or root_task.upstream_task_id
    root_task.model = attempt.model
    root_task.protocol = attempt.protocol
    root_task.request_id = attempt.request_id
    root_task.candidate_index = attempt.candidate_index
    root_task.progress = 0
    root_task.next_poll_at = now + timedelta(seconds=max(0, int(first_interval_seconds)))
    root_task.deadline_at = now + timedelta(seconds=max(1, int(budget_seconds)))
    db.flush()


# =====================================================================
# 尝试行
# =====================================================================


def _new_attempt(
    db: Session,
    root_task: AiTask,
    *,
    model: str,
    protocol: str | None,
    candidate_index: int,
    attempt: int,
    route_id: int | None,
    segment_index: int | None = None,
    payload: Any = None,
    meta: Mapping[str, Any] | None = None,
) -> AiTask:
    """插入 ``running`` 尝试行（复制 B.15 列出的冗余列）并提交。"""
    row = AiTask(
        project_id=root_task.project_id,
        capability=root_task.capability,
        operation=root_task.operation,
        trigger_type=root_task.trigger_type,
        target_type=root_task.target_type,
        target_id=root_task.target_id,
        batch_id=root_task.batch_id,
        route_id=root_task.route_id or route_id,
        template_id=root_task.template_id,
        created_by=root_task.created_by,
        root_task_id=root_task.id,
        candidate_index=int(candidate_index),
        attempt=int(attempt),
        segment_index=segment_index,
        model=str(model)[:120],
        protocol=protocol,
        status="running",
        request_payload_json=_payload_json(payload),
        response_meta_json=_dumps(dict(meta)) if meta else None,
        started_at=utcnow(),
    )
    db.add(row)
    db.commit()
    return row


def _attempt_realtime(db: Session, attempt: AiTask) -> None:
    if attempt.trigger_type == "health_probe" or attempt.status not in ("succeeded", "failed"):
        return
    fields: dict[str, Any] = {
        "ai_calls": 1,
        "ai_succeeded" if attempt.status == "succeeded" else "ai_failed": 1,
        "prompt_tokens": attempt.prompt_tokens or 0,
        "completion_tokens": attempt.completion_tokens or 0,
        "quota_estimated": attempt.quota_estimated or 0,
        "cost_cny": float(attempt.cost_cny or 0),
    }
    if attempt.status == "succeeded":
        fields["ai_duration_ms_sum"] = attempt.duration_ms or 0
    stats_service.increment_realtime_after_commit(db, attempt.project_id, fields, at=attempt.finished_at)


def _placeholder_attempt(
    db: Session,
    root_task: AiTask,
    *,
    model: str,
    protocol: str | None,
    candidate_index: int,
    route_id: int | None,
    category: ErrorCategory,
    message: str,
    segment_index: int | None = None,
) -> AiTask:
    """未发起 HTTP 的占位尝试行：``failed(breaker_open | model_unrouted, request_id=NULL)``。"""
    now = utcnow()
    row = AiTask(
        project_id=root_task.project_id,
        capability=root_task.capability,
        operation=root_task.operation,
        trigger_type=root_task.trigger_type,
        target_type=root_task.target_type,
        target_id=root_task.target_id,
        batch_id=root_task.batch_id,
        route_id=root_task.route_id or route_id,
        template_id=root_task.template_id,
        created_by=root_task.created_by,
        root_task_id=root_task.id,
        candidate_index=int(candidate_index),
        attempt=1,
        segment_index=segment_index,
        model=str(model)[:120],
        protocol=protocol,
        status="failed",
        error_category=category.value,
        error_message=message[:500],
        cost_cny=Decimal(0),
        duration_ms=0,
        started_at=now,
        finished_at=now,
    )
    db.add(row)
    db.flush()
    _attempt_realtime(db, row)
    db.commit()
    return row


def _breaker_alert(db: Session, capability: str, model: str, cfg: Mapping[str, Any], err: ZhiqiError) -> None:
    b = cfg.get("breaker") or {}
    target_key = f"{capability}:{model}"
    alert_service.raise_alert(
        db, SYSTEM_SCOPE, "ai_breaker_open",
        target_type="ai_model", target_key=target_key,
        title=f"AI 模型熔断：{target_key}",
        message=(
            f"模型 {model}（能力 {capability}）在 {b.get('window_seconds', 300)} 秒内失败达到 "
            f"{b.get('failure_threshold', 5)} 次，熔断 {b.get('open_seconds', 120)} 秒；最近错误 {err.category.value}"
        ),
        payload={
            "capability": capability, "model": model, "reason": "failures",
            "error_category": err.category.value, "request_id": err.request_id, "http_status": err.http_status,
        },
    )


def _pause_alert(db: Session, reason: str, task: AiTask, err: ZhiqiError, pause_seconds: int) -> None:
    alert_type = "ai_quota_exceeded" if reason == "quota_exceeded" else "ai_auth_failed"
    what = "额度不足" if reason == "quota_exceeded" else "鉴权失败"
    alert_service.raise_alert(
        db, SYSTEM_SCOPE, alert_type,
        target_type="system", target_key="",
        title=f"AI 上游{what}，已暂停调用",
        message=(
            f"zhiqiapi 返回{what}（HTTP {err.http_status if err.http_status is not None else '-'}，模型 {task.model}），"
            f"已暂停 AI 调用 {pause_seconds} 秒；请检查账户额度或密钥后在「AI 网关 → 能力路由」重置"
        ),
        payload={
            "error_category": reason, "request_id": err.request_id, "http_status": err.http_status,
            "capability": task.capability, "model": task.model, "task_id": task.id, "pause_seconds": pause_seconds,
        },
    )


def record_breaker_failure(db: Session, capability: str | None, model: str | None, err: ZhiqiError) -> bool:
    """媒体轮询阶段的失败计入熔断（§8.4、§9.2）：上游异步任务 ``failed(media_storage)`` 与轮询超出预算 / 上游 ``expired``
    （``timeout``）不经尝试行，由 ``media_service`` / ``recover_stale_tasks`` 在根任务终态事务内调用。``breaker.record_failure``
    非 open → open 时 ``raise_alert(ai_breaker_open)``；不提交（调用方提交）。Redis 异常只记日志。返回是否发生 open 转换。"""
    if not capability or not model:
        return False
    cfg = _cfg(db)
    try:
        opened = get_breaker(cfg).record_failure(capability, model, ErrorCategory(err.category))
    except redis.RedisError as exc:
        logger.warning("熔断计数失败 %s:%s: %s", capability, model, exc)
        return False
    if opened:
        _breaker_alert(db, capability, model, cfg, err)
    return opened


def record_failure(db: Session, attempt_task: AiTask, err: ZhiqiError, *, count_breaker: bool = True) -> None:
    """记录一次失败并提交：

    - 尝试行：写 ``status=failed`` 与 ``error_category`` / ``error_message`` / ``http_status`` / ``request_id``；
      ``breaker.record_failure``（``count_breaker=False`` 用于图片异步提交触发同步回退的那次）；非 open → open 时
      ``raise_alert(ai_breaker_open)``；
    - ``quota_exceeded`` / ``auth_failed``：``SET ai:paused:{reason} EX pause_seconds`` + ``ai_quota_exceeded`` /
      ``ai_auth_failed`` 告警；传入的是尝试行时把所属根任务按 §8.5 回滚（``rollback_for_pause``）；
    - 传入根任务行（轮询 GET 的失败）时只续写暂停键与告警，任务保持 ``polling``。
    """
    cfg = _cfg(db)
    category = ErrorCategory(err.category)
    is_attempt = attempt_task.root_task_id is not None
    if is_attempt:
        now = utcnow()
        attempt_task.status = "failed"
        attempt_task.error_category = category.value
        attempt_task.error_message = sanitize_error_message(err.message)
        if err.http_status is not None:
            attempt_task.http_status = err.http_status
        if err.request_id:
            attempt_task.request_id = err.request_id[:64]
        attempt_task.finished_at = now
        if attempt_task.duration_ms is None:
            started = attempt_task.started_at or now
            attempt_task.duration_ms = max(0, int((now - started).total_seconds() * 1000))
        if attempt_task.cost_cny is None:
            attempt_task.cost_cny = quota_to_cny(cfg, int(attempt_task.quota_estimated or 0))
        if len(err.retry_request_ids or []) > 1:
            update_task_meta(attempt_task, retry_request_ids=list(err.retry_request_ids))
        db.flush()
        _attempt_realtime(db, attempt_task)
        if count_breaker:
            breaker = get_breaker(cfg)
            if breaker.record_failure(attempt_task.capability, attempt_task.model, category):
                _breaker_alert(db, attempt_task.capability, attempt_task.model, cfg, err)
    if category in PAUSE_CATEGORIES:
        pause_seconds = int(cfg.get("pause_seconds") or 600)
        try:
            set_paused(category.value, pause_seconds)
        except redis.RedisError as exc:
            logger.warning("写入 ai:paused:%s 失败: %s", category.value, exc)
        _pause_alert(db, category.value, attempt_task, err, pause_seconds)
        if is_attempt:
            root = db.get(AiTask, attempt_task.root_task_id)
            if root is not None:
                rollback_for_pause(db, root, commit=False)
    db.commit()


def _record_success(db: Session, attempt: AiTask, cfg: Mapping[str, Any]) -> None:
    breaker = get_breaker(cfg)
    if breaker.record_success(attempt.capability, attempt.model):
        alert_service.resolve_alert(db, SYSTEM_SCOPE, "ai_breaker_open", "ai_model", f"{attempt.capability}:{attempt.model}")


def _messages_text(messages: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return "\n".join(parts)


def _finish_text_attempt(
    db: Session,
    attempt: AiTask,
    result: TextResult,
    *,
    prompt_text: str,
    cfg: Mapping[str, Any],
    started: float,
    error: ZhiqiError | None = None,
) -> None:
    """文本尝试行终态：tokens（``usage_missing`` 时估算）、``quota_estimated``、``cost_cny``、``request_id``、``response_meta``。"""
    prompt_tokens, completion_tokens = result.prompt_tokens, result.completion_tokens
    if result.usage_missing:
        prompt_tokens = usage.estimate_tokens(prompt_text)
        completion_tokens = usage.estimate_tokens(result.text or "")
    attempt.prompt_tokens = int(prompt_tokens or 0)
    attempt.completion_tokens = int(completion_tokens or 0)
    attempt.cache_tokens = int(result.cache_tokens or 0)
    attempt.quota_estimated = estimate_for(db, attempt.model, attempt.prompt_tokens, attempt.completion_tokens)
    attempt.cost_cny = quota_to_cny(cfg, attempt.quota_estimated)
    attempt.request_id = (result.request_id or None) and result.request_id[:64]
    attempt.http_status = result.http_status
    attempt.upstream_latency_ms = result.latency_ms
    attempt.duration_ms = _elapsed_ms(started)
    attempt.finished_at = utcnow()
    attempt.output_excerpt = (result.text or "")[:OUTPUT_EXCERPT_LIMIT]
    meta = task_meta(attempt)
    meta.update(
        {
            "finish_reason": result.finish_reason,
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cache_tokens": result.cache_tokens,
            },
            "citations": len(result.citations or []),
            "http_status": result.http_status,
            "usage_missing": bool(result.usage_missing),
        }
    )
    retry_ids = list(getattr(result, "retry_request_ids", None) or [])
    if retry_ids:
        meta["retry_request_ids"] = retry_ids
    set_task_meta(attempt, meta)
    if error is None:
        attempt.status = "succeeded"
    else:
        attempt.status = "failed"
        attempt.error_category = error.category.value
        attempt.error_message = sanitize_error_message(error.message)
    db.flush()
    _attempt_realtime(db, attempt)


def _final_error(
    cfg: Mapping[str, Any],
    route: ResolvedRoute,
    last_err: ZhiqiError,
    *,
    last_model: str | None,
    breaker_open: list[str],
    media: bool,
) -> BusinessError:
    """候选链结束后的业务异常：覆盖模型熔断 → 5031 ``hint=model_override``；不可切换（含覆盖模型失败）→ 5021；
    可切换但候选耗尽 → 5031（``data.breaker_open`` / ``unavailable_models``）。``error_category`` 取最后一个候选的分类。"""
    category = ErrorCategory(last_err.category)
    capability = route.capability.value
    if route.model_override:
        if category == ErrorCategory.BREAKER_OPEN:
            return _unavailable_error(
                capability, breaker_open=breaker_open, unavailable_models=route.unavailable_models, paused_reason=check_paused(),
                hint="model_override", error_category=category.value, error_message=sanitize_error_message(last_err.message),
            )
        hint = "prompt_blocked" if category == ErrorCategory.CONTENT_BLOCKED else "model_override"
        return _upstream_error(last_err, model=route.model_override, hint=hint)
    fallbackable = is_fallbackable(category, dict(cfg)) and not (media and category == ErrorCategory.TIMEOUT)
    if not fallbackable:
        hint = "prompt_blocked" if category == ErrorCategory.CONTENT_BLOCKED else None
        return _upstream_error(last_err, model=last_model, hint=hint)
    return _unavailable_error(
        capability, breaker_open=breaker_open, unavailable_models=route.unavailable_models, paused_reason=check_paused(),
        error_category=category.value, error_message=sanitize_error_message(last_err.message),
    )


def _opened_meanwhile(breaker: CircuitBreaker, capability: str, model: str) -> bool:
    """同候选内 service 级再尝试（参数降级 / 重新生成 / ``rate_limited`` / 提交前错误）前复查熔断器：已 ``open`` 时不再对该
    模型发起 HTTP（§8.4「``allow=False`` 时不发起 HTTP」），保留上一次失败分类按候选链切换。``half_open`` 的试探名额已由本候选
    首次 ``allow()`` 占用，视为可继续。"""
    return breaker.state(capability, model) == "open"


def _can_switch(cfg: Mapping[str, Any], route: ResolvedRoute, category: ErrorCategory, *, media: bool) -> bool:
    if route.model_override:
        return False
    if media and category == ErrorCategory.TIMEOUT:
        return False
    return is_fallbackable(category, dict(cfg))


def _target_url(db: Session, root_task: AiTask) -> str | None:
    if root_task.capability in ("seo_check", "geo_check") and root_task.target_type == "publish_link" and root_task.target_id:
        return db.scalar(select(PublishLink.url).where(PublishLink.id == root_task.target_id))
    return None


# =====================================================================
# 文本（§5.13 complete_text、§9.2）
# =====================================================================


def complete_text(
    db: Session,
    *,
    root_task: AiTask,
    messages: list[dict],
    params: dict | None,
    response_format: str,
    extra: dict | None = None,
    segment_index: int | None = None,
    model_override: str | None = None,
    protocol_override: Protocol | None = None,
    timeout_override: float | None = None,
    metadata: Mapping[str, Any] | None = None,
    validator: Callable[[TextResult], Any] | None = None,
) -> tuple[TextResult, AiTask]:
    """文本调用（候选链 + 同模型再尝试 + 尝试行记录），返回 ``(TextResult, 成功尝试行)``。

    - ``model_override`` 缺省取 ``root_task.input_json.model``；GEO / SEO 引擎显式传入 ``model_override`` / ``protocol_override``
      与 ``timeout_override``（引擎自带 ``timeout_seconds``，§6.6）；
    - ``metadata`` 并入 ``TextRequest.metadata``（本地上下文，供 Mock 识别模板，如 ``template_code``），不发送；
    - ``validator(result)``：输出校验（``extract_json`` + ``output_schema``），抛 ``ZhiqiError(INVALID_RESPONSE)`` 时该尝试行记
      ``failed(invalid_response)``（照常记 tokens / 成本）并同模型重新生成 1 次；返回值挂在 ``result.parsed``；
    - 失败：``unsupported_parameter`` 同模型降级重试 1 次；``invalid_response`` 重新生成 1 次；``rate_limited`` / 提交前错误
      同模型再尝试直至 ``max_attempts``；``quota_exceeded`` / ``auth_failed`` 抛出原 ``ZhiqiError``（根任务已按 §8.5 回滚）；
      可切换且无覆盖 → 下一候选；否则 5021 / 5031（``BusinessError``，附 ``error_category`` 属性）；
    - 调用前全局暂停 → 根任务回滚 ``queued`` 并抛 ``ZhiqiBreakerOpen``；根任务不再由本执行者持有 → ``TaskAbandoned``。
    """
    cfg = _cfg(db)
    _guard(db, root_task)
    capability = Capability(root_task.capability)
    override = (model_override or task_input(root_task).get("model") or None)
    route = resolve_route(db, capability, root_task.project_id, model_override=override, protocol_override=protocol_override)
    if root_task.route_id is None:
        root_task.route_id = route.route_id
    effective = {**route.params, **(params or {})}
    max_tokens = int(effective.get("max_tokens") or 2048)
    temperature = float(effective.get("temperature") if effective.get("temperature") is not None else 0.7)
    top_p = float(effective["top_p"]) if effective.get("top_p") is not None else None
    stop_value = effective.get("stop")
    stop = [stop_value] if isinstance(stop_value, str) else (list(stop_value) if stop_value else None)
    timeout = float(timeout_override or route.timeout_seconds)
    policy = get_retry_policy(cfg)
    passthrough = dict(cfg.get("passthrough") or {})
    breaker = get_breaker(cfg)
    client = get_client()
    preferred = Protocol(protocol_override) if protocol_override else route.protocol
    local_meta: dict[str, Any] = {"capability": capability.value, "task_id": root_task.id, "operation": root_task.operation}
    target_url = _target_url(db, root_task)
    if target_url:
        local_meta["target_url"] = target_url
    local_meta.update(dict(metadata or {}))
    prompt_text = _messages_text(messages)

    last_err: ZhiqiError | None = None
    last_model: str | None = None
    breaker_open: list[str] = []
    for index, model in enumerate(route.candidates):
        last_model = model
        protocol = catalog.protocol_for(ai_catalog_service.catalog_entry(db, model), preferred)
        if protocol is None:
            last_err = ZhiqiError(ErrorCategory.MODEL_UNROUTED, f"模型 {model} 没有可用的文本端点")
            _placeholder_attempt(
                db, root_task, model=model, protocol=None, candidate_index=index, route_id=route.route_id,
                category=ErrorCategory.MODEL_UNROUTED, message=last_err.message, segment_index=segment_index,
            )
        elif not breaker.allow(capability.value, model):
            breaker_open.append(model)
            last_err = ZhiqiBreakerOpen(f"模型 {model} 熔断打开，未发起调用")
            _placeholder_attempt(
                db, root_task, model=model, protocol=protocol.value, candidate_index=index, route_id=route.route_id,
                category=ErrorCategory.BREAKER_OPEN, message=last_err.message, segment_index=segment_index,
            )
        else:
            attempt_no, degraded, regenerated = 1, False, False
            while True:
                _guard(db, root_task)
                if attempt_no > 1 and _opened_meanwhile(breaker, capability.value, model):
                    break  # 同模型再尝试前熔断已打开（本次失败计入后打开或并发打开）：不再发起 HTTP，按候选链处理
                req = TextRequest(
                    model=model, protocol=protocol, messages=messages, max_tokens=max_tokens, temperature=temperature,
                    top_p=top_p, response_format=response_format or "text", stop=stop, extra=dict(extra or {}),
                    metadata=dict(local_meta),
                )
                meta: dict[str, Any] = {}
                if degraded:
                    meta["degraded_params"] = text.degraded_param_names(req, passthrough)
                attempt = _new_attempt(
                    db, root_task, model=model, protocol=protocol.value, candidate_index=index, attempt=attempt_no,
                    route_id=route.route_id, segment_index=segment_index,
                    payload=text.build_payload(req, passthrough, degraded=degraded), meta=meta,
                )
                started = time.monotonic()
                try:
                    result = text.complete(client, req, timeout=timeout, retry=policy, passthrough=passthrough, degraded=degraded)
                except ZhiqiError as err:
                    attempt.duration_ms = _elapsed_ms(started)
                    record_failure(db, attempt, err)
                    last_err = err
                    category = ErrorCategory(err.category)
                    if category in PAUSE_CATEGORIES:
                        raise
                    if category == ErrorCategory.UNSUPPORTED_PARAMETER and not degraded and attempt_no < route.max_attempts:
                        degraded = True
                        attempt_no += 1
                        continue
                    if category == ErrorCategory.INVALID_RESPONSE and not regenerated and attempt_no < route.max_attempts:
                        regenerated = True
                        attempt_no += 1
                        continue
                    if (category == ErrorCategory.RATE_LIMITED or err.pre_submit) and attempt_no < route.max_attempts:
                        attempt_no += 1
                        continue
                    break
                parsed: Any = _UNSET
                validation_error: ZhiqiError | None = None
                if validator is not None:
                    try:
                        parsed = validator(result)
                    except (ZhiqiError, ValueError) as verr:
                        validation_error = ZhiqiError(
                            ErrorCategory.INVALID_RESPONSE, getattr(verr, "message", None) or str(verr) or "输出不符合约定格式",
                            http_status=result.http_status, request_id=result.request_id,
                        )
                _finish_text_attempt(db, attempt, result, prompt_text=prompt_text, cfg=cfg, started=started, error=validation_error)
                if validation_error is None:
                    _record_success(db, attempt, cfg)
                    db.commit()
                    if parsed is not _UNSET:
                        result.parsed = parsed  # type: ignore[attr-defined]
                    return result, attempt
                db.commit()
                last_err = validation_error
                if not regenerated and attempt_no < route.max_attempts:
                    regenerated = True
                    attempt_no += 1
                    continue
                break
        assert last_err is not None
        if not _can_switch(cfg, route, ErrorCategory(last_err.category), media=False) or index >= len(route.candidates) - 1:
            break
    assert last_err is not None
    raise _final_error(cfg, route, last_err, last_model=last_model, breaker_open=breaker_open, media=False)


# =====================================================================
# 图片 / 视频提交（§5.13 submit_image / submit_video、docs/10 §4.3）
# =====================================================================


def _media_candidates(route: ResolvedRoute, root_task: AiTask) -> list[tuple[int, str]]:
    """候选 ``(candidate_index, model)``；轮询阶段 ``media_storage`` 备选回退的新根任务从 ``root_task.candidate_index`` 起。"""
    pairs = list(enumerate(route.candidates))
    start = 0 if route.model_override else int(root_task.candidate_index or 0)
    if start > 0:
        pairs = [p for p in pairs if p[0] >= start]
    return pairs


def _finish_media_attempt(
    db: Session,
    attempt: AiTask,
    *,
    request_id: str | None,
    http_status: int,
    latency_ms: int,
    started: float,
    prompt: str,
    cfg: Mapping[str, Any],
    upstream_task_id: str | None = None,
    urls: list[str] | None = None,
    extra_meta: Mapping[str, Any] | None = None,
) -> None:
    attempt.status = "succeeded"
    attempt.request_id = (request_id or None) and request_id[:64]
    attempt.http_status = http_status
    attempt.upstream_latency_ms = latency_ms
    attempt.duration_ms = _elapsed_ms(started)
    attempt.finished_at = utcnow()
    attempt.upstream_task_id = (upstream_task_id or None) and upstream_task_id[:80]
    attempt.quota_estimated = estimate_for(db, attempt.model, usage.estimate_tokens(prompt or ""), 0)
    attempt.cost_cny = quota_to_cny(cfg, attempt.quota_estimated)
    if urls:
        attempt.output_excerpt = urls[0][:OUTPUT_EXCERPT_LIMIT]
    meta = task_meta(attempt)
    meta["http_status"] = http_status
    if urls:
        meta["urls"] = list(urls)
    meta.update(dict(extra_meta or {}))
    set_task_meta(attempt, meta)
    db.flush()
    _attempt_realtime(db, attempt)


def _edit_payload(req: ImageRequest, extra_fields: tuple[str, ...]) -> dict[str, Any]:
    return {"multipart": [[k, v] for k, v in images.build_edit_form(req, extra_fields)]}


def submit_image(
    db: Session, *, root_task: AiTask, req: ImageRequest, model_override: str | None = None
) -> tuple[ImageSubmitResult, AiTask]:
    """图片提交（候选链；默认异步，仅异步返回 ``route_missing`` / ``model_unrouted`` 且 ``sync_fallback`` 时同候选回退同步 / 编辑）。

    返回 ``(提交结果, 成功尝试行)``：``mode=async`` 时调用方以 ``start_polling`` 置根任务 ``polling``；``sync`` / ``edit`` 直接得到 URL。
    提交读超时固定不回退、不切换（5021 ``error_category=timeout``）；其它失败按 ``fallback_on`` 切换备选（覆盖模型不切换）。
    """
    cfg = _cfg(db)
    _guard(db, root_task)
    override = (model_override or task_input(root_task).get("model") or None)
    route = resolve_route(db, Capability.IMAGE, root_task.project_id, model_override=override)
    if root_task.route_id is None:
        root_task.route_id = route.route_id
    image_cfg = (settings_service.get_config(db, "media_config").get("image") or {})
    sync_fallback = bool(image_cfg.get("sync_fallback", True))
    extra_fields = tuple(image_cfg.get("edit_extra_fields") or ())
    timeout = float(route.timeout_seconds)
    policy = get_retry_policy(cfg)
    breaker = get_breaker(cfg)
    client = get_client()
    pairs = _media_candidates(route, root_task)
    if not pairs:
        raise _unavailable_error("image", unavailable_models=route.unavailable_models, error_message="没有剩余的候选模型")

    last_err: ZhiqiError | None = None
    last_model: str | None = None
    breaker_open: list[str] = []
    for position, (index, model) in enumerate(pairs):
        last_model = model
        entry = ai_catalog_service.catalog_entry(db, model)
        protocol = catalog.protocol_for(entry, route.protocol)
        if protocol is None:
            last_err = ZhiqiError(ErrorCategory.MODEL_UNROUTED, f"模型 {model} 没有可用的图片端点")
            _placeholder_attempt(
                db, root_task, model=model, protocol=None, candidate_index=index, route_id=route.route_id,
                category=ErrorCategory.MODEL_UNROUTED, message=last_err.message,
            )
        elif not breaker.allow("image", model):
            breaker_open.append(model)
            last_err = ZhiqiBreakerOpen(f"模型 {model} 熔断打开，未发起调用")
            _placeholder_attempt(
                db, root_task, model=model, protocol=protocol.value, candidate_index=index, route_id=route.route_id,
                category=ErrorCategory.BREAKER_OPEN, message=last_err.message,
            )
        else:
            attempt_no = 1
            req_m = dataclasses.replace(req, model=model)
            while True:
                _guard(db, root_task)
                if attempt_no > 1 and _opened_meanwhile(breaker, "image", model):
                    break
                attempt = _new_attempt(
                    db, root_task, model=model, protocol=Protocol.IMAGE_ASYNC.value, candidate_index=index, attempt=attempt_no,
                    route_id=route.route_id, payload=images.build_async_payload(req_m),
                )
                started = time.monotonic()
                try:
                    result = images.submit_async(client, req_m, timeout=timeout, retry=policy)
                except ZhiqiError as err:
                    attempt.duration_ms = _elapsed_ms(started)
                    category = ErrorCategory(err.category)
                    if category in (ErrorCategory.ROUTE_MISSING, ErrorCategory.MODEL_UNROUTED) and sync_fallback:
                        # 触发回退的异步错误不计熔断；同步 / 编辑也失败时才 record_failure 一次
                        record_failure(db, attempt, err, count_breaker=False)
                        fb_protocol = catalog.sync_fallback_protocol(entry, has_reference_images=bool(req_m.reference_image_urls))
                        attempt_no += 1
                        _guard(db, root_task)
                        is_edit = fb_protocol == Protocol.IMAGE_EDIT
                        fb_attempt = _new_attempt(
                            db, root_task, model=model, protocol=fb_protocol.value, candidate_index=index, attempt=attempt_no,
                            route_id=route.route_id,
                            payload=_edit_payload(req_m, extra_fields) if is_edit else images.build_async_payload(req_m),
                            meta={"fallback_from": attempt.id},
                        )
                        fb_started = time.monotonic()
                        try:
                            if is_edit:
                                fb_result = images.edit_sync(client, req_m, timeout=timeout, extra_fields=extra_fields)
                            else:
                                fb_result = images.generate_sync(client, req_m, timeout=timeout)
                        except ZhiqiError as fb_err:
                            fb_attempt.duration_ms = _elapsed_ms(fb_started)
                            record_failure(db, fb_attempt, fb_err)
                            last_err = fb_err
                            if ErrorCategory(fb_err.category) in PAUSE_CATEGORIES:
                                raise
                            break
                        _finish_media_attempt(
                            db, fb_attempt, request_id=fb_result.request_id, http_status=fb_result.http_status,
                            latency_ms=fb_result.latency_ms, started=fb_started, prompt=req_m.prompt, cfg=cfg, urls=fb_result.urls,
                        )
                        _record_success(db, fb_attempt, cfg)
                        db.commit()
                        return fb_result, fb_attempt
                    record_failure(db, attempt, err)
                    last_err = err
                    if category in PAUSE_CATEGORIES:
                        raise
                    if category == ErrorCategory.TIMEOUT:
                        break
                    if (category == ErrorCategory.RATE_LIMITED or err.pre_submit) and attempt_no < route.max_attempts:
                        attempt_no += 1
                        continue
                    break
                _finish_media_attempt(
                    db, attempt, request_id=result.request_id, http_status=result.http_status, latency_ms=result.latency_ms,
                    started=started, prompt=req_m.prompt, cfg=cfg, upstream_task_id=result.task_id,
                )
                root_task.upstream_task_id = result.task_id
                _record_success(db, attempt, cfg)
                db.commit()
                return result, attempt
        assert last_err is not None
        if not _can_switch(cfg, route, ErrorCategory(last_err.category), media=True) or position >= len(pairs) - 1:
            break
    assert last_err is not None
    raise _final_error(cfg, route, last_err, last_model=last_model, breaker_open=breaker_open, media=True)


def submit_video(
    db: Session, *, root_task: AiTask, req: VideoRequest, model_override: str | None = None
) -> tuple[VideoSubmitResult, AiTask]:
    """视频提交（``POST /v1/videos``，候选链同 ``submit_image``，无同步回退；提交读超时固定不回退、不切换）。"""
    cfg = _cfg(db)
    _guard(db, root_task)
    override = (model_override or task_input(root_task).get("model") or None)
    route = resolve_route(db, Capability.VIDEO, root_task.project_id, model_override=override)
    if root_task.route_id is None:
        root_task.route_id = route.route_id
    timeout = float(route.timeout_seconds)
    policy = get_retry_policy(cfg)
    breaker = get_breaker(cfg)
    client = get_client()
    pairs = _media_candidates(route, root_task)
    if not pairs:
        raise _unavailable_error("video", unavailable_models=route.unavailable_models, error_message="没有剩余的候选模型")

    last_err: ZhiqiError | None = None
    last_model: str | None = None
    breaker_open: list[str] = []
    for position, (index, model) in enumerate(pairs):
        last_model = model
        protocol = catalog.protocol_for(ai_catalog_service.catalog_entry(db, model), route.protocol)
        if protocol is None:
            last_err = ZhiqiError(ErrorCategory.MODEL_UNROUTED, f"模型 {model} 没有可用的视频端点")
            _placeholder_attempt(
                db, root_task, model=model, protocol=None, candidate_index=index, route_id=route.route_id,
                category=ErrorCategory.MODEL_UNROUTED, message=last_err.message,
            )
        elif not breaker.allow("video", model):
            breaker_open.append(model)
            last_err = ZhiqiBreakerOpen(f"模型 {model} 熔断打开，未发起调用")
            _placeholder_attempt(
                db, root_task, model=model, protocol=protocol.value, candidate_index=index, route_id=route.route_id,
                category=ErrorCategory.BREAKER_OPEN, message=last_err.message,
            )
        else:
            attempt_no = 1
            req_m = dataclasses.replace(req, model=model)
            while True:
                _guard(db, root_task)
                if attempt_no > 1 and _opened_meanwhile(breaker, "video", model):
                    break
                attempt = _new_attempt(
                    db, root_task, model=model, protocol=Protocol.VIDEO.value, candidate_index=index, attempt=attempt_no,
                    route_id=route.route_id, payload=videos.build_payload(req_m),
                )
                started = time.monotonic()
                try:
                    result = videos.submit_video(client, req_m, timeout=timeout, retry=policy)
                except ZhiqiError as err:
                    attempt.duration_ms = _elapsed_ms(started)
                    record_failure(db, attempt, err)
                    last_err = err
                    category = ErrorCategory(err.category)
                    if category in PAUSE_CATEGORIES:
                        raise
                    if category == ErrorCategory.TIMEOUT:
                        break
                    if (category == ErrorCategory.RATE_LIMITED or err.pre_submit) and attempt_no < route.max_attempts:
                        attempt_no += 1
                        continue
                    break
                _finish_media_attempt(
                    db, attempt, request_id=result.request_id, http_status=result.http_status, latency_ms=result.latency_ms,
                    started=started, prompt=req_m.prompt, cfg=cfg, upstream_task_id=result.task_id,
                )
                root_task.upstream_task_id = result.task_id
                _record_success(db, attempt, cfg)
                db.commit()
                return result, attempt
        assert last_err is not None
        if not _can_switch(cfg, route, ErrorCategory(last_err.category), media=True) or position >= len(pairs) - 1:
            break
    assert last_err is not None
    raise _final_error(cfg, route, last_err, last_model=last_model, breaker_open=breaker_open, media=True)


# =====================================================================
# 轮询（§5.13 poll_task、§9.4）
# =====================================================================


def poll_task(db: Session, root_task: AiTask) -> ImageTaskStatus | VideoTaskStatus:
    """轮询上游异步任务（不记尝试行）：``GET`` 使用 ``retry=policy``、``timeout=timeouts.poll_seconds``；每次把
    ``request_id`` / ``http_status`` / 错误写入 ``response_meta_json.poll``（``request_ids`` 保留最近 20 个、``consecutive_404``
    遇 404 累加、其它响应清零）并 ``poll_count += 1``、同步 ``progress``，然后提交。

    ``ZhiqiError`` 原样抛出（``auth_failed`` / ``quota_exceeded`` 已 ``record_failure`` 续写暂停键与告警，任务保持 ``polling``）；
    状态流转（``succeeded`` / ``failed`` / ``expired`` / 重排 ``next_poll_at``）由 ``poll_media_tasks`` 处理。
    """
    if not root_task.upstream_task_id:
        raise ValueError(f"根任务 {root_task.id} 没有 upstream_task_id")
    cfg = _cfg(db)
    timeout = float((cfg.get("timeouts") or {}).get("poll_seconds") or settings.zhiqi_timeout_poll_seconds)
    policy = get_retry_policy(cfg)
    client = get_client()
    meta = task_meta(root_task)
    poll = dict(meta.get("poll") or {})
    request_ids: list[str] = list(poll.get("request_ids") or [])
    root_task.poll_count = int(root_task.poll_count or 0) + 1
    try:
        if root_task.capability == Capability.VIDEO.value:
            status: ImageTaskStatus | VideoTaskStatus = videos.get_video(client, root_task.upstream_task_id, timeout=timeout, retry=policy)
        else:
            status = images.get_generation(client, root_task.upstream_task_id, timeout=timeout, retry=policy)
    except ZhiqiError as err:
        ids = [i for i in (err.retry_request_ids or ([err.request_id] if err.request_id else [])) if i]
        request_ids.extend(ids)
        poll.update(
            {
                "last_request_id": err.request_id,
                "last_http_status": err.http_status,
                "error_code": err.category.value,
                "error_message": sanitize_error_message(err.message),
                "request_ids": request_ids[-POLL_REQUEST_IDS_KEEP:],
                "consecutive_404": int(poll.get("consecutive_404") or 0) + 1 if err.category == ErrorCategory.ROUTE_MISSING else 0,
            }
        )
        meta["poll"] = poll
        set_task_meta(root_task, meta)
        if ErrorCategory(err.category) in PAUSE_CATEGORIES:
            record_failure(db, root_task, err)  # 续写暂停键与告警（提交）
        else:
            db.commit()
        raise
    if status.request_id:
        request_ids.append(status.request_id)
    poll.update(
        {
            "last_request_id": status.request_id,
            "last_http_status": 200,
            "error_code": status.error_code,
            "error_message": sanitize_error_message(status.error_message) if status.error_message else None,
            "request_ids": request_ids[-POLL_REQUEST_IDS_KEEP:],
            "consecutive_404": 0,
        }
    )
    meta["poll"] = poll
    set_task_meta(root_task, meta)
    if status.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
        root_task.progress = max(0, min(99, int(status.progress or 0)))
    elif status.status == TaskStatus.SUCCEEDED:
        root_task.progress = 100
    db.commit()
    return status
