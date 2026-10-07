"""AI 根任务执行与管理（docs/08 §7.4、§7.5、§7.7；docs/09 §9.4、§9.7；docs/03「任务幂等与状态收敛」；docs/13 §6.3）。

执行（``app.worker`` 的 ``run_ai_tasks`` 调用）：

- ``claim(db, task_id, worker_id)``：``SET lock:ai_task:{id} NX EX 1800`` + ``UPDATE … SET status='running' WHERE status='queued'``
  （rowcount=0 放弃）；首个根任务被领取时批次 ``queued → running``；
- ``process_one(task_id, worker_id)``：按 ``operation`` 从 ``REGISTRY`` 取处理器执行（``register_handler``，第 3/4/5 步注册
  ``keyword_generate`` / ``title_generate`` / ``content_*`` / ``image_*`` / ``video_generate`` 等；``route_probe`` 在本模块注册），
  执行线程每 30s 心跳；上游调用前的状态 / 暂停复查由 ``ai_gateway_service`` 完成；终态：先复查所属批次（已取消 → ``cancelled``，
  不写业务对象）→ 业务对象写入与 ``finalize_root`` 同一事务 → 提交后调用一次批次收敛钩子 ``on_task_finished(batch_id)``；
  ``quota_exceeded`` / ``auth_failed`` 回滚为 ``queued`` 的根任务不是终态。

管理（``/admin/ai/tasks*``，按数据范围过滤）：``list_tasks`` / ``get_task_detail`` / ``retry_task`` / ``cancel_task`` / ``export_tasks``。
健康探测：``run_route_probe`` 记同步执行的 ``route_probe`` 根任务 + 尝试行（``trigger_type=health_probe``，不预占额度）。
"""

from __future__ import annotations

import csv
import importlib
import importlib.util
import io
import logging
import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import (
    CODE_BAD_REQUEST,
    CODE_CAPABILITY_UNAVAILABLE,
    CODE_CONFLICT,
    CODE_QUOTA_LIMIT_REACHED,
    CODE_UPSTREAM_ERROR,
    BusinessError,
    field_error,
)
from app.core.locks import acquire_lock, extend_lock, release_lock
from app.core.redis import cache_get_json, cache_set_json, redis_client
from app.core.zhiqi import catalog, get_client, health, images, text, videos
from app.core.zhiqi.errors import ZhiqiError, sanitize_error_message
from app.core.zhiqi.types import (
    TEXT_CAPABILITIES,
    TEXT_PROTOCOLS,
    Capability,
    ErrorCategory,
    HealthResult,
    ImageRequest,
    Protocol,
    TextRequest,
    VideoRequest,
)
from app.core.zhiqi.types import (
    utcnow as zhiqi_utcnow,
)
from app.models import (
    AiModel,
    AiTask,
    CapabilityRoute,
    GenerationBatch,
    MediaAsset,
    utcnow,
)
from app.schemas.common import EXPORT_MAX_ROWS, iso_utc, to_utc_naive
from app.services import (
    ai_catalog_service,
    alert_service,
    settings_service,
    stats_service,
)
from app.services import ai_gateway_service as gateway
from app.services.ai_gateway_service import TaskAbandoned
from app.services.data_scope_service import (
    SYSTEM_SCOPE,
    DataScope,
    get_visible,
    scope_by_project,
)

logger = logging.getLogger(__name__)

TASK_LOCK_PREFIX = "lock:ai_task:"
TASK_LOCK_TTL = 1800
HEARTBEAT_INTERVAL_SECONDS = 30
RETRYABLE_TARGET_TYPES = frozenset({"generation_batch", "keyword", "content"})
NON_RETRYABLE_OPERATIONS = frozenset({"seo_check", "geo_check", "route_probe", "image_prompt"})
MEDIA_CAPABILITIES = frozenset({"image", "video"})
ROW_KINDS = ("root", "attempt", "all")


# =====================================================================
# 处理器注册表
# =====================================================================


@dataclass
class TaskOutcome:
    """处理器返回值。

    - ``succeeded``：``apply(ctx)`` 在终态事务内写业务对象，随后 ``finalize_root(succeeded)``；
    - ``failed``：调用 ``on_failed`` 钩子后 ``finalize_root(failed, error_category, error_message)``；
    - ``polling``：媒体提交成功，处理器已用 ``ai_gateway_service.start_polling`` 置根任务 ``polling``（非终态，只提交）。
    """

    status: Literal["succeeded", "failed", "polling"] = "succeeded"
    apply: Callable[[TaskContext], None] | None = None
    output_excerpt: str | None = None
    error_category: str | None = None
    error_message: str | None = None


TaskHandler = Callable[["TaskContext"], "TaskOutcome | None"]
FailedHook = Callable[[Session, AiTask, str, "str | None"], None]
CancelledHook = Callable[[Session, AiTask], None]
RetryHook = Callable[[Session, AiTask, AiTask], None]
BatchFinishedHook = Callable[[int], Any]


@dataclass
class HandlerSpec:
    execute: TaskHandler
    on_failed: FailedHook | None = None          # 根任务失败（终态事务内）：内容恢复 prev_status、资产 failed + 告警…
    on_cancelled: CancelledHook | None = None    # 根任务取消（worker 复查批次已取消 / 单任务取消，终态事务内）
    before_retry: RetryHook | None = None        # 单任务 retry 新建根任务时（同一事务）：业务状态校验、contents.ai_task_id 改指


REGISTRY: dict[str, HandlerSpec] = {}
_batch_finished_hook: BatchFinishedHook | None = None


def register_handler(
    operation: str,
    fn: TaskHandler,
    *,
    on_failed: FailedHook | None = None,
    on_cancelled: CancelledHook | None = None,
    before_retry: RetryHook | None = None,
) -> None:
    """注册 ``operation`` 的处理器（重复注册覆盖）。处理器签名 ``fn(ctx: TaskContext) -> TaskOutcome | None``（``None`` = 成功）。"""
    REGISTRY[operation] = HandlerSpec(execute=fn, on_failed=on_failed, on_cancelled=on_cancelled, before_retry=before_retry)


def unregister_handler(operation: str) -> None:
    REGISTRY.pop(operation, None)


def get_handler(operation: str) -> HandlerSpec | None:
    spec = REGISTRY.get(operation)
    if spec is None:
        load_handlers()
        spec = REGISTRY.get(operation)
    return spec


# 在模块导入时调用 register_handler 的业务模块（worker 进程不经路由导入它们，按需加载；不存在的模块跳过）
HANDLER_MODULES: tuple[str, ...] = (
    "app.services.generation_service",
    "app.services.keyword_service",
    "app.services.title_service",
    "app.services.content_service",
    "app.services.media_service",
)
_handlers_loaded = False


def load_handlers() -> None:
    """导入 ``HANDLER_MODULES`` 中已存在的模块以完成处理器注册（幂等；单个模块导入失败只记日志）。"""
    global _handlers_loaded
    if _handlers_loaded:
        return
    _handlers_loaded = True
    for name in HANDLER_MODULES:
        try:
            if importlib.util.find_spec(name) is not None:
                importlib.import_module(name)
        except Exception:  # noqa: BLE001
            logger.exception("加载任务处理器模块失败：%s", name)


def set_batch_finished_hook(fn: BatchFinishedHook | None) -> None:
    """批次收敛钩子（第 3 步 ``generation_service.on_task_finished(batch_id)``）；未设置时按需从 ``generation_service`` 惰性加载。"""
    global _batch_finished_hook
    _batch_finished_hook = fn


def _resolve_batch_hook() -> BatchFinishedHook | None:
    if _batch_finished_hook is not None:
        return _batch_finished_hook
    if importlib.util.find_spec("app.services.generation_service") is None:
        return None
    module = importlib.import_module("app.services.generation_service")
    hook = getattr(module, "on_task_finished", None)
    return hook if callable(hook) else None


def notify_batch_finished(batch_id: int | None) -> None:
    """根任务终态提交后调用一次 ``on_task_finished(batch_id)``（仅 ``batch_id`` 非空）；异常只记日志（由 recover ④ 兜底）。"""
    if not batch_id:
        return
    hook = _resolve_batch_hook()
    if hook is None:
        return
    try:
        hook(batch_id)
    except Exception:  # noqa: BLE001
        logger.exception("批次收敛失败 batch_id=%s（由 recover_stale_tasks ④ 兜底）", batch_id)


# =====================================================================
# 执行上下文与心跳
# =====================================================================


class TaskContext:
    """处理器执行上下文：会话、根任务、执行者标识、业务参数与网关调用的便捷包装（worker 内使用 ``SYSTEM_SCOPE``）。"""

    def __init__(self, db: Session, task: AiTask, worker_id: str) -> None:
        self.db = db
        self.task = task
        self.worker_id = worker_id
        self.input: dict[str, Any] = gateway.task_input(task)
        self.scope: DataScope = SYSTEM_SCOPE

    @property
    def model_override(self) -> str | None:
        value = self.input.get("model")
        return str(value) if value else None

    def ensure_active(self) -> None:
        """重读 ``status='running' AND locked_by=self`` 与 ``check_paused()``（不满足抛 ``TaskAbandoned`` / 回滚后抛出）。"""
        gateway.ensure_can_call(self.db, self.task)

    def heartbeat(self) -> None:
        beat(self.task.id, self.worker_id)

    def batch(self) -> GenerationBatch | None:
        return self.db.get(GenerationBatch, self.task.batch_id) if self.task.batch_id else None

    def batch_cancelled(self) -> bool:
        return _batch_cancelled(self.db, self.task)

    def complete_text(self, **kwargs: Any) -> Any:
        return gateway.complete_text(self.db, root_task=self.task, **kwargs)

    def submit_image(self, req: ImageRequest, **kwargs: Any) -> Any:
        return gateway.submit_image(self.db, root_task=self.task, req=req, **kwargs)

    def submit_video(self, req: VideoRequest, **kwargs: Any) -> Any:
        return gateway.submit_video(self.db, root_task=self.task, req=req, **kwargs)


def beat(task_id: int, worker_id: str) -> bool:
    """心跳：``UPDATE ai_tasks SET heartbeat_at=now WHERE id AND status='running' AND locked_by=self`` 并续期
    ``lock:ai_task:{id}``（1800s）。独立会话，可在计时线程中调用。返回是否仍持有。"""
    with SessionLocal() as session:
        result = session.execute(
            update(AiTask)
            .where(AiTask.id == task_id, AiTask.status == "running", AiTask.locked_by == worker_id)
            .values(heartbeat_at=utcnow())
        )
        session.commit()
        held = bool(result.rowcount)
    try:
        extend_lock(f"{TASK_LOCK_PREFIX}{task_id}", TASK_LOCK_TTL, token=worker_id)
    except Exception:  # noqa: BLE001
        logger.warning("续期 lock:ai_task:%s 失败", task_id, exc_info=True)
    return held


class Heartbeat:
    """执行线程的计时心跳（每 ``HEARTBEAT_INTERVAL_SECONDS`` 秒，文本调用期间由该线程完成）。"""

    def __init__(self, task_id: int, worker_id: str, interval: float | None = None) -> None:
        self.task_id = task_id
        self.worker_id = worker_id
        self.interval = float(interval if interval is not None else HEARTBEAT_INTERVAL_SECONDS)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                beat(self.task_id, self.worker_id)
            except Exception:  # noqa: BLE001
                logger.warning("根任务心跳失败 task_id=%s", self.task_id, exc_info=True)

    def start(self) -> Heartbeat:
        if self.interval > 0:
            self._thread = threading.Thread(target=self._run, name=f"ai-task-heartbeat-{self.task_id}", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


# =====================================================================
# 领取与执行
# =====================================================================


def claim(db: Session, task_id: int, worker_id: str | None = None) -> bool:
    """领取根任务：``lock:ai_task:{id}`` + 数据库条件更新双保险；rowcount=0（已被领取 / 取消）放弃并释放锁。"""
    worker_id = worker_id or gateway.default_worker_id()
    lock_key = f"{TASK_LOCK_PREFIX}{task_id}"
    if not acquire_lock(lock_key, TASK_LOCK_TTL, token=worker_id):
        return False
    try:
        now = utcnow()
        result = db.execute(
            update(AiTask)
            .where(AiTask.id == task_id, AiTask.status == "queued", AiTask.root_task_id.is_(None))
            .values(status="running", locked_by=worker_id, started_at=now, heartbeat_at=now)
        )
        if not result.rowcount:
            db.rollback()
            release_lock(lock_key, worker_id)
            return False
        batch_id = db.scalar(select(AiTask.batch_id).where(AiTask.id == task_id))
        if batch_id:
            db.execute(
                update(GenerationBatch)
                .where(GenerationBatch.id == batch_id, GenerationBatch.status == "queued")
                .values(status="running", started_at=now, heartbeat_at=now)
            )
        db.commit()
        return True
    except Exception:
        db.rollback()
        release_lock(lock_key, worker_id)
        raise


def dispatch(ctx: TaskContext) -> TaskOutcome:
    """按根任务 ``operation`` 分派到已注册的处理器；未注册 → ``failed(unknown)``。"""
    spec = get_handler(ctx.task.operation)
    if spec is None:
        return TaskOutcome(status="failed", error_category="unknown", error_message=f"未注册的任务类型：{ctx.task.operation}")
    return spec.execute(ctx) or TaskOutcome()


def _category_for_business(exc: BusinessError) -> str:
    category = getattr(exc, "error_category", None)
    if category:
        return str(category)
    if exc.code == CODE_CAPABILITY_UNAVAILABLE:
        return "model_unrouted"
    if exc.code == CODE_UPSTREAM_ERROR:
        data = exc.data if isinstance(exc.data, dict) else {}
        return str(data.get("error_category") or "unknown")
    if exc.code == CODE_QUOTA_LIMIT_REACHED:
        return "quota_exceeded"
    return "unknown"


def _batch_cancelled(db: Session, task: AiTask) -> bool:
    if not task.batch_id:
        return False
    return db.scalar(select(GenerationBatch.status).where(GenerationBatch.id == task.batch_id)) == "cancelled"


def process_one(task_id: int, *, worker_id: str | None = None) -> bool:
    """执行一个已 ``claim`` 的根任务（线程池线程内调用，自建会话）；返回是否由本执行者写入了结果。"""
    worker_id = worker_id or gateway.default_worker_id()
    try:
        with SessionLocal() as db:
            return _process(db, task_id, worker_id)
    finally:
        try:
            release_lock(f"{TASK_LOCK_PREFIX}{task_id}", worker_id)
        except Exception:  # noqa: BLE001
            logger.warning("释放 lock:ai_task:%s 失败", task_id, exc_info=True)


def _owned(db: Session, task: AiTask, worker_id: str, statuses: tuple[str, ...] = ("running",)) -> bool:
    row = db.execute(select(AiTask.status, AiTask.locked_by).where(AiTask.id == task.id)).first()
    return row is not None and row.status in statuses and row.locked_by == worker_id


def _process(db: Session, task_id: int, worker_id: str) -> bool:
    root = db.get(AiTask, task_id)
    if root is None or root.root_task_id is not None or root.status != "running" or root.locked_by != worker_id:
        return False
    spec = get_handler(root.operation)
    ctx = TaskContext(db, root, worker_id)
    heartbeat = Heartbeat(task_id, worker_id).start()
    raised = False
    try:
        outcome = dispatch(ctx)
    except TaskAbandoned as exc:
        db.rollback()
        logger.info("放弃根任务 %s：%s", task_id, exc)
        return False
    except ZhiqiError as err:
        raised = True
        db.rollback()
        outcome = TaskOutcome(status="failed", error_category=err.category.value, error_message=sanitize_error_message(err.message))
    except BusinessError as exc:
        raised = True
        db.rollback()
        outcome = TaskOutcome(
            status="failed", error_category=_category_for_business(exc), error_message=getattr(exc, "error_message", None) or exc.message,
        )
    except Exception as exc:  # noqa: BLE001
        raised = True
        db.rollback()
        logger.exception("根任务 %s 执行异常", task_id)
        outcome = TaskOutcome(status="failed", error_category="unknown", error_message=f"{type(exc).__name__}: {exc}")
    finally:
        heartbeat.stop()
    if raised:
        db.refresh(root)
    return _finish(ctx, spec, outcome)


def _finish(ctx: TaskContext, spec: HandlerSpec | None, outcome: TaskOutcome) -> bool:
    db, root = ctx.db, ctx.task
    if outcome.status == "polling":
        if not _owned(db, root, ctx.worker_id, ("polling", "running")):
            db.rollback()
            return False
        db.commit()
        return True
    if not _owned(db, root, ctx.worker_id):
        # 已被回滚为 queued（全局暂停）、取消或回收：不写业务对象、不结算
        db.rollback()
        return False
    batch_id = root.batch_id
    try:
        _write_terminal(db, root, spec, outcome, ctx)
        db.commit()
    except Exception:  # noqa: BLE001 - 业务对象写入失败：整体回滚后按 failed(unknown) 收尾
        db.rollback()
        logger.exception("根任务 %s 终态写入失败", root.id)
        db.refresh(root)
        if not _owned(db, root, ctx.worker_id):
            return False
        failure = TaskOutcome(status="failed", error_category="unknown", error_message="终态写入失败")
        _write_terminal(db, root, spec, failure, ctx)
        db.commit()
    notify_batch_finished(batch_id)
    return True


def _write_terminal(db: Session, root: AiTask, spec: HandlerSpec | None, outcome: TaskOutcome, ctx: TaskContext) -> None:
    if _batch_cancelled(db, root):
        if spec and spec.on_cancelled:
            spec.on_cancelled(db, root)
        gateway.finalize_root(db, root, "cancelled")
        return
    if outcome.status == "succeeded":
        if outcome.apply is not None:
            outcome.apply(ctx)
        gateway.finalize_root(db, root, "succeeded", output_excerpt=outcome.output_excerpt)
        return
    category = outcome.error_category or "unknown"
    if spec and spec.on_failed:
        spec.on_failed(db, root, category, outcome.error_message)
    gateway.finalize_root(db, root, "failed", error_category=category, error_message=outcome.error_message)


# =====================================================================
# 健康探测任务（route_probe，§12.1）
# =====================================================================


def _fail_probe_attempt(db: Session, attempt: AiTask, err: ZhiqiError, started: float) -> None:
    """探测尝试行失败：只记字段，不计熔断、不写暂停键、不告警（探测不经 ``complete_text``，§8.5）。"""
    attempt.status = "failed"
    attempt.error_category = err.category.value
    attempt.error_message = sanitize_error_message(err.message)
    attempt.http_status = err.http_status
    attempt.request_id = (err.request_id or None) and err.request_id[:64]
    attempt.duration_ms = max(0, int((time.monotonic() - started) * 1000))
    attempt.finished_at = utcnow()
    attempt.cost_cny = gateway.quota_to_cny({}, 0)


def _execute_probe(
    db: Session, root: AiTask, *, capability: Capability, model: str, protocol: Protocol, probe_media: bool,
    model_available: bool | None, cfg: dict[str, Any],
) -> HealthResult:
    """在已有的 ``route_probe`` 根任务下执行一次探测并写尝试行（不写根任务终态）。"""
    health_cfg = cfg.get("health") or {}
    degraded_ms = int(health_cfg.get("degraded_latency_ms") or health.DEFAULT_DEGRADED_LATENCY_MS)
    prompt = str(health_cfg.get("probe_text_prompt") or "ping")
    max_tokens = int(health_cfg.get("probe_max_tokens") or 8)
    checked_at = zhiqi_utcnow()
    client = get_client()
    payload: Any = None
    is_text = capability in TEXT_CAPABILITIES
    text_req = None
    image_req = video_req = None
    if is_text:
        text_req = TextRequest(
            model=model, protocol=protocol, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
            metadata={"capability": capability.value, "operation": "route_probe", "task_id": root.id},
        )
        payload = text.build_payload(text_req, {})
    elif probe_media and capability == Capability.IMAGE:
        image_req = ImageRequest(model=model, prompt=health.PROBE_IMAGE_PROMPT, resolution="1080p", aspect_ratio="1:1")
        payload = images.build_async_payload(image_req)
    elif probe_media:
        video_req = VideoRequest(model=model, prompt=health.PROBE_VIDEO_PROMPT)
        payload = videos.build_payload(video_req)
    attempt = gateway._new_attempt(  # noqa: SLF001 - 同包内复用尝试行记录
        db, root, model=model, protocol=protocol.value, candidate_index=0, attempt=1, route_id=root.route_id, payload=payload,
    )
    started = time.monotonic()
    error: ZhiqiError | None = None
    request_id: str | None = None
    latency_ms = 0
    try:
        if text_req is not None:
            result = text.complete(client, text_req, timeout=30, retry=None, passthrough={})
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            gateway._finish_text_attempt(db, attempt, result, prompt_text=prompt, cfg=cfg, started=started)  # noqa: SLF001
            request_id = result.request_id
        elif image_req is not None:
            submitted = images.submit_async(client, image_req, timeout=30)
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            gateway._finish_media_attempt(  # noqa: SLF001
                db, attempt, request_id=submitted.request_id, http_status=submitted.http_status, latency_ms=submitted.latency_ms,
                started=started, prompt=image_req.prompt, cfg=cfg, upstream_task_id=submitted.task_id,
            )
            request_id = submitted.request_id
        elif video_req is not None:
            submitted_video = videos.submit_video(client, video_req, timeout=30)
            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            gateway._finish_media_attempt(  # noqa: SLF001
                db, attempt, request_id=submitted_video.request_id, http_status=submitted_video.http_status,
                latency_ms=submitted_video.latency_ms, started=started, prompt=video_req.prompt, cfg=cfg,
                upstream_task_id=submitted_video.task_id,
            )
            request_id = submitted_video.request_id
        else:
            # image / video 且 probe_media=False：不发 HTTP，按 ai_models.is_available 判定
            if model_available:
                attempt.status = "succeeded"
                attempt.duration_ms = 0
                attempt.finished_at = utcnow()
                attempt.cost_cny = gateway.quota_to_cny(cfg, 0)
            else:
                raise ZhiqiError(ErrorCategory.MODEL_UNROUTED, "模型不在 /v1/models 目录")
    except ZhiqiError as err:
        error = err
        request_id = err.request_id
        latency_ms = max(0, int((time.monotonic() - started) * 1000)) if (text_req or image_req or video_req) else 0
        _fail_probe_attempt(db, attempt, err, started)
    except Exception as exc:  # noqa: BLE001 - 探测不向上抛
        error = ZhiqiError(ErrorCategory.UNKNOWN, f"{type(exc).__name__}: {exc}")
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        _fail_probe_attempt(db, attempt, error, started)
    db.commit()
    return HealthResult(
        capability=capability, model=model, protocol=protocol,
        status=health.status_from(latency_ms, error, degraded_ms),
        latency_ms=latency_ms, request_id=request_id,
        error_category=error.category if error else None,
        error_message=sanitize_error_message(error.message) if error else None,
        checked_at=checked_at,
    )


def _probe_protocol(db: Session, capability: Capability, model: str, protocol: Protocol | str | None) -> tuple[Protocol, int | None]:
    route = gateway.load_route(db, capability.value, 0)
    route_id = int(route["id"]) if route else None
    if protocol:
        return Protocol(protocol), route_id
    if route:
        preferred = Protocol(route["protocol"])
    elif capability == Capability.IMAGE:
        preferred = Protocol.IMAGE_ASYNC
    elif capability == Capability.VIDEO:
        preferred = Protocol.VIDEO
    else:
        preferred = Protocol.OPENAI_CHAT
    resolved = catalog.protocol_for(ai_catalog_service.catalog_entry(db, model), preferred)
    return resolved or preferred, route_id


def run_route_probe(
    db: Session,
    *,
    capability: Capability | str,
    model: str,
    protocol: Protocol | str | None = None,
    route_id: int | None = None,
    created_by: int | None = None,
    probe_media: bool = False,
    worker_id: str | None = None,
) -> HealthResult:
    """单模型探测并记同步执行的 ``route_probe`` 根任务 + 尝试行（``trigger_type=health_probe``、``target_type=route_probe``、
    ``target_id=route_id``、``project_id=NULL``、不预占额度）；``protocol`` 缺省取 ``protocol_for(目录项, 全局路由.protocol)``，
    ``route_id`` 缺省取该能力的全局路由。image / video 且 ``probe_media=False`` 时按 ``ai_models.is_available`` 判定，
    ``ai_models`` 无该行 → ``unknown``（不发 HTTP、不记任务行）。窗口规则与 ``ai:health:*`` 写入由调用方完成（§12.4）。"""
    cfg = gateway._cfg(db)  # noqa: SLF001
    capability = Capability(capability)
    resolved_protocol, global_route_id = _probe_protocol(db, capability, model, protocol)
    route_id = route_id if route_id is not None else global_route_id
    row = ai_catalog_service.get_model_by_id(db, model)
    model_available = None if row is None else bool(row.is_available)
    if capability not in TEXT_CAPABILITIES and not probe_media and model_available is None:
        return health.probe(
            get_client(), capability=capability, model=model, protocol=resolved_protocol, probe_media=False, model_available=None,
        )
    root = gateway.create_root_task(
        db, capability=capability, operation="route_probe", project_id=None, created_by=created_by, trigger_type="health_probe",
        target_type="route_probe", target_id=route_id, model=model, sync=True, worker_id=worker_id,
        input={"model": model, "protocol": resolved_protocol.value, "probe_media": bool(probe_media)},
    )
    root.route_id = route_id
    root.protocol = resolved_protocol.value
    db.commit()
    result = _execute_probe(
        db, root, capability=capability, model=model, protocol=resolved_protocol, probe_media=probe_media,
        model_available=model_available, cfg=cfg,
    )
    gateway.finalize_root(
        db, root, "succeeded" if result.error_category is None else "failed",
        error_category=result.error_category.value if result.error_category else None, error_message=result.error_message,
    )
    db.commit()
    return result


def _route_probe_handler(ctx: TaskContext) -> TaskOutcome:
    """经队列执行的 ``route_probe`` 根任务（正常路径为 ``run_route_probe`` 同步执行）：按 ``input_json`` 探测一次。"""
    model = str(ctx.input.get("model") or ctx.task.model or "")
    capability = Capability(ctx.task.capability)
    protocol, _ = _probe_protocol(ctx.db, capability, model, ctx.input.get("protocol"))
    row = ai_catalog_service.get_model_by_id(ctx.db, model)
    result = _execute_probe(
        ctx.db, ctx.task, capability=capability, model=model, protocol=protocol, probe_media=bool(ctx.input.get("probe_media")),
        model_available=None if row is None else bool(row.is_available), cfg=gateway._cfg(ctx.db),  # noqa: SLF001
    )
    if result.error_category is None:
        return TaskOutcome(status="succeeded")
    return TaskOutcome(status="failed", error_category=result.error_category.value, error_message=result.error_message)


register_handler("route_probe", _route_probe_handler)


# =====================================================================
# 管理接口（/admin/ai/tasks*）
# =====================================================================


def _num(value: Any) -> float | None:
    return round(float(value), 6) if value is not None else None


def _output_excerpt(task: AiTask) -> str | None:
    """媒体（``image`` / ``video``）根任务与尝试行的 ``output_excerpt`` 是上游临时 URL（``data[0].url``）：库中保留供审计
    （docs/03），接口一律返回 ``null``——上游 URL 不出现在任何前端展示中（docs/10 §2.2、§13 第 1 条）。"""
    return None if task.capability in MEDIA_CAPABILITIES else task.output_excerpt


def _response_meta(task: AiTask) -> Any:
    """详情的 ``response_meta``：媒体尝试行（同步端点 / 回退）记录的 ``urls[]`` 为上游临时 URL，不对外返回（同 ``_output_excerpt``）。"""
    meta = gateway._loads(task.response_meta_json, None)  # noqa: SLF001
    if isinstance(meta, dict) and task.capability in MEDIA_CAPABILITIES and "urls" in meta:
        meta = {k: v for k, v in meta.items() if k != "urls"}
    return meta


def task_item(task: AiTask) -> dict[str, Any]:
    """任务行对象（docs/04 §7.17）；尝试行的 ``input`` 为 ``null``；媒体任务的 ``output_excerpt`` 为 ``null``（见 ``_output_excerpt``）。"""
    return {
        "id": task.id,
        "project_id": task.project_id,
        "capability": task.capability,
        "operation": task.operation,
        "protocol": task.protocol,
        "trigger_type": task.trigger_type,
        "target_type": task.target_type,
        "target_id": task.target_id,
        "batch_id": task.batch_id,
        "root_task_id": task.root_task_id,
        "parent_task_id": task.parent_task_id,
        "route_id": task.route_id,
        "candidate_index": task.candidate_index,
        "attempt": task.attempt,
        "segment_index": task.segment_index,
        "model": task.model,
        "template_id": task.template_id,
        "status": task.status,
        "pause_count": task.pause_count,
        "request_id": task.request_id,
        "upstream_task_id": task.upstream_task_id,
        "output_excerpt": _output_excerpt(task),
        "prompt_tokens": task.prompt_tokens,
        "completion_tokens": task.completion_tokens,
        "cache_tokens": task.cache_tokens,
        "quota_reserved": task.quota_reserved,
        "quota_estimated": task.quota_estimated,
        "quota_actual": task.quota_actual,
        "cost_cny": _num(task.cost_cny),
        "reconciled_at": iso_utc(task.reconciled_at),
        "usage_log_type": task.usage_log_type,
        "error_category": task.error_category,
        "error_message": task.error_message,
        "http_status": task.http_status,
        "duration_ms": task.duration_ms,
        "upstream_latency_ms": task.upstream_latency_ms,
        "progress": task.progress,
        "poll_count": task.poll_count,
        "next_poll_at": iso_utc(task.next_poll_at),
        "deadline_at": iso_utc(task.deadline_at),
        "locked_by": task.locked_by,
        "heartbeat_at": iso_utc(task.heartbeat_at),
        "started_at": iso_utc(task.started_at),
        "finished_at": iso_utc(task.finished_at),
        "created_by": task.created_by,
        "created_at": iso_utc(task.created_at),
        "updated_at": iso_utc(task.updated_at),
        "input": gateway.task_input(task) if task.root_task_id is None and task.input_json else None,
    }


def _task_conditions(
    *,
    row_kind: str = "root",
    project_id: int | None = None,
    capability: str | None = None,
    operation: str | None = None,
    model: str | None = None,
    status: str | None = None,
    error_category: str | None = None,
    trigger_type: str | None = None,
    batch_id: int | None = None,
    root_task_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    request_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Any]:
    if row_kind not in ROW_KINDS:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["query", "row_kind"], "取值必须是 root、attempt、all 之一", "enum", row_kind)])
    conditions: list[Any] = []
    if row_kind == "root":
        conditions.append(AiTask.root_task_id.is_(None))
    elif row_kind == "attempt":
        conditions.append(AiTask.root_task_id.is_not(None))
    for column, value in (
        (AiTask.project_id, project_id), (AiTask.capability, capability), (AiTask.operation, operation), (AiTask.model, model),
        (AiTask.status, status), (AiTask.error_category, error_category), (AiTask.trigger_type, trigger_type),
        (AiTask.batch_id, batch_id), (AiTask.root_task_id, root_task_id), (AiTask.target_type, target_type),
        (AiTask.target_id, target_id), (AiTask.request_id, request_id),
    ):
        if value is not None and value != "":
            conditions.append(column == value)
    if start is not None:
        conditions.append(AiTask.created_at >= to_utc_naive(start))
    if end is not None:
        conditions.append(AiTask.created_at < to_utc_naive(end))  # 左闭右开（docs/04 §3）
    return conditions


def list_tasks(db: Session, scope: DataScope, *, page: int = 1, page_size: int = 20, **filters: Any) -> tuple[list[dict[str, Any]], int]:
    """分页（``created_at DESC, id DESC``）；筛选见 docs/04 §6.15，``row_kind`` 缺省 ``root``；按 ``project_id IN P`` 过滤
    （无项目的探测任务只对总后台可见）。"""
    filters.setdefault("row_kind", "root")
    conditions = _task_conditions(**filters)
    stmt = scope_by_project(select(AiTask), AiTask.project_id, scope).where(*conditions)
    count_stmt = scope_by_project(select(func.count(AiTask.id)), AiTask.project_id, scope).where(*conditions)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(stmt.order_by(AiTask.created_at.desc(), AiTask.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return [task_item(r) for r in rows], total


def get_task_detail(db: Session, scope: DataScope, task_id: int) -> dict[str, Any]:
    """详情：任务字段 + ``input`` + 脱敏 ``request_payload`` + ``response_meta``；根任务附 ``attempts[]``
    （按 ``candidate_index, attempt, segment_index`` 排序）。"""
    task = get_visible(db, scope, AiTask, task_id)
    item = task_item(task)
    item["request_payload"] = gateway._loads(task.request_payload_json, None)  # noqa: SLF001
    item["response_meta"] = _response_meta(task)
    if task.root_task_id is None:
        attempts = db.scalars(select(AiTask).where(AiTask.root_task_id == task.id)).all()
        attempts = sorted(attempts, key=lambda a: (a.candidate_index, a.attempt, a.segment_index or 0, a.id))
        item["attempts"] = [task_item(a) for a in attempts]
    return item


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _media_asset_id(db: Session, task: AiTask) -> int | None:
    if task.target_type == "media_asset" and task.target_id:
        return task.target_id
    return db.scalar(select(MediaAsset.id).where(MediaAsset.ai_task_id == task.id).limit(1))


def retry_task(db: Session, scope: DataScope, task_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/ai/tasks/{id}/retry``：仅 ``failed`` / ``expired`` 的文本根任务（``target_type ∈ {generation_batch, keyword,
    content}``、非探测、``operation ∉ {seo_check, geo_check, route_probe, image_prompt}``）→ 新建根任务（``parent_task_id``，
    复制 ``project_id`` / ``capability`` / ``operation`` / ``target_type`` / ``target_id`` / ``batch_id`` / ``input_json``）重新入队；
    有批次时同事务 ``task_failed −1`` 并把批次置回 ``running``。返回 ``{task_id}``。"""
    task = get_visible(db, scope, AiTask, task_id)
    if task.root_task_id is not None:
        raise _conflict("只有根任务可以重试", {"current_status": task.status})
    if task.capability in MEDIA_CAPABILITIES:
        raise _conflict("请通过素材重试接口重试", {"hint": f"POST /admin/media/assets/{_media_asset_id(db, task)}/retry"})
    if task.capability in ("seo_check", "geo_check") or task.operation in ("seo_check", "geo_check"):
        raise _conflict("请通过收录检测接口重试", {"hint": f"POST /admin/links/{task.target_id}/index-check"})
    if task.trigger_type == "health_probe" or task.operation in NON_RETRYABLE_OPERATIONS:
        raise _conflict("该任务不可重试", None)
    if task.status not in ("failed", "expired"):
        raise _conflict("只有失败或过期的任务可以重试", {"current_status": task.status})
    if task.capability not in {c.value for c in TEXT_CAPABILITIES} or task.target_type not in RETRYABLE_TARGET_TYPES:
        raise _conflict("该任务不可重试", {"current_status": task.status})
    existing = db.scalar(select(AiTask.id).where(AiTask.parent_task_id == task.id, AiTask.root_task_id.is_(None)).limit(1))
    if existing:
        raise _conflict("该任务已有重试任务", {"existing_id": existing})
    input_data = gateway.task_input(task)
    route = gateway.preflight(db, task.capability, task.project_id, model_override=input_data.get("model") or None)
    new_task = gateway.create_root_task(
        db, capability=task.capability, operation=task.operation, project_id=task.project_id, created_by=admin_id,
        trigger_type="user", target_type=task.target_type, target_id=task.target_id, batch_id=task.batch_id,
        template_id=task.template_id, input=input_data, route=route, parent_task_id=task.id,
    )
    reserved = False
    try:
        if int(task.quota_reserved or 0) > 0:
            gateway.check_quota(db, project_id=task.project_id, estimated_quota=int(task.quota_reserved), task=new_task)
            reserved = True
        if task.batch_id:
            batch = db.get(GenerationBatch, task.batch_id)
            if batch is not None:
                batch.task_failed = max(0, int(batch.task_failed or 0) - 1)
                batch.status = "running"
                batch.finished_at = None
                batch.heartbeat_at = utcnow()
        spec = get_handler(task.operation)
        if spec and spec.before_retry:
            spec.before_retry(db, task, new_task)
        db.commit()
    except Exception:
        db.rollback()
        if reserved:
            gateway.release_reservation(db, new_task)
        raise
    return {"task_id": new_task.id}


def cancel_task(db: Session, scope: DataScope, task_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/ai/tasks/{id}/cancel``：根任务 ``queued`` / ``polling → cancelled``（``error_category=cancelled``，
    ``settle_quota`` 释放预占；``polling`` 不调用上游取消）；``running`` 及其它状态 409 ``current_status``；同步执行的根任务 409。"""
    del admin_id
    task = get_visible(db, scope, AiTask, task_id)
    if task.root_task_id is not None:
        raise _conflict("只有根任务可以取消", {"current_status": task.status})
    if gateway.is_sync_root(task):
        raise _conflict("同步执行的任务不可取消", {"current_status": task.status})
    if task.status not in ("queued", "polling"):
        raise _conflict("当前状态不可取消", {"current_status": task.status})
    # 行锁重读最新状态（worker 可能刚领取，或媒体轮询正把根任务置终态；poll_media_tasks 同样 FOR UPDATE 后再写）
    db.refresh(task, with_for_update=True)
    current = task.status
    if current not in ("queued", "polling"):
        db.rollback()
        raise _conflict("当前状态不可取消", {"current_status": current})
    spec = get_handler(task.operation)
    try:
        if spec and spec.on_cancelled:
            spec.on_cancelled(db, task)
        gateway.finalize_root(db, task, "cancelled")
        db.commit()
    except Exception:
        db.rollback()
        raise
    notify_batch_finished(task.batch_id)
    return task_item(task)


# ---------------------------------------------------------------- CSV 导出

AI_TASK_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("root_task_id", "根任务 ID"),
    ("project_id", "项目 ID"),
    ("capability", "能力"),
    ("operation", "操作"),
    ("trigger_type", "触发方式"),
    ("model", "模型"),
    ("protocol", "协议"),
    ("candidate_index", "候选序号"),
    ("attempt", "尝试序号"),
    ("status", "状态"),
    ("error_category", "错误分类"),
    ("request_id", "请求号"),
    ("http_status", "HTTP 状态"),
    ("prompt_tokens", "输入 tokens"),
    ("completion_tokens", "输出 tokens"),
    ("quota_estimated", "估算额度"),
    ("quota_actual", "实扣额度"),
    ("cost_cny", "成本（元）"),
    ("duration_ms", "耗时（ms）"),
    ("request_summary", "请求摘要"),
    ("created_at", "创建时间"),
)
REQUEST_SUMMARY_CHARS = 200


def _request_summary(task: AiTask) -> str:
    """脱敏请求摘要：实际请求体（已脱敏、截断）的紧凑 JSON 前 200 字符。"""
    raw = task.request_payload_json or ""
    return raw[:REQUEST_SUMMARY_CHARS]


def export_tasks(db: Session, scope: DataScope, **filters: Any) -> tuple[str, str]:
    """``GET /admin/ai/tasks/export``：同列表筛选，缺省 ``row_kind=attempt``；UTF-8 BOM CSV，最多 50,000 行，超出 400。
    返回 ``(文件名, CSV 文本)``，文件名 ``ai-tasks-<YYYYMMDD>.csv``（统计时区日期）。"""
    filters.setdefault("row_kind", "attempt")
    conditions = _task_conditions(**filters)
    total = int(db.scalar(scope_by_project(select(func.count(AiTask.id)), AiTask.project_id, scope).where(*conditions)) or 0)
    if total > EXPORT_MAX_ROWS:
        raise BusinessError(
            "参数错误", code=CODE_BAD_REQUEST,
            data=[field_error(["query"], f"导出行数 {total} 超过上限 {EXPORT_MAX_ROWS}，请缩小筛选范围", "too_many_rows", total)],
        )
    stmt = scope_by_project(select(AiTask), AiTask.project_id, scope).where(*conditions).order_by(AiTask.created_at.desc(), AiTask.id.desc())
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow([header for _, header in AI_TASK_EXPORT_COLUMNS])
    for task in db.scalars(stmt).yield_per(1000):
        values = {
            **task_item(task),
            "request_summary": _request_summary(task),
        }
        writer.writerow(["" if values.get(key) is None else values.get(key) for key, _ in AI_TASK_EXPORT_COLUMNS])
    filename = f"ai-tasks-{stats_service.today_date(db).strftime('%Y%m%d')}.csv"
    return filename, buffer.getvalue()


# =====================================================================
# 详情页指标：ai_p95_duration_ms / ai_failures_by_category（docs/12 §3.2、§10.2；docs/08 §15）
# =====================================================================

ATTEMPT_STATS_DEFAULT_DAYS = 7          # 未传 start/end 时取最近 7 天
ATTEMPT_STATS_MAX_DAYS = 366
ATTEMPT_STATS_P95_SAMPLE = 10000        # 超过 1 万条成功尝试行按最近 1 万条近似（同 time_to_index_hours_p50）


def percentile_95(values: list[int]) -> int | None:
    """``PERCENTILE_95``：最近秩法（nearest-rank，``ceil(0.95·n)``-th 最小值）；空集 ``None``。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return int(ordered[rank - 1])


def attempt_stats(
    db: Session,
    scope: DataScope,
    *,
    project_id: int | None = None,
    capability: str | None = None,
    model: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """``GET /admin/ai/tasks/stats``：range 内尝试行实时查询（``root_task_id IS NOT NULL``、``status IN
    ('succeeded','failed')``、``trigger_type != 'health_probe'``，按 ``project_id IN P`` 过滤）。

    返回 ``p95_duration_ms``（成功尝试行 ``duration_ms`` 的 P95）、``failures_by_category``（失败尝试行按
    ``error_category`` 计数，降序）与 ``by_model``（``capability × model`` 维度的同口径明细）。"""
    end_utc = to_utc_naive(end) if end is not None else utcnow()
    start_utc = to_utc_naive(start) if start is not None else end_utc - timedelta(days=ATTEMPT_STATS_DEFAULT_DAYS)
    if start_utc >= end_utc or end_utc - start_utc > timedelta(days=ATTEMPT_STATS_MAX_DAYS):
        raise BusinessError(
            "参数错误", code=CODE_BAD_REQUEST,
            data=[field_error(["query", "start"], f"start 必须早于 end，且跨度不超过 {ATTEMPT_STATS_MAX_DAYS} 天", "range",
                              iso_utc(start_utc))],
        )
    conditions = [
        # 未传 end 时不设上界（当前秒内新建的行 created_at == now，左闭右开会漏掉）
        *_task_conditions(row_kind="attempt", project_id=project_id, capability=capability, model=model,
                          start=start_utc, end=end_utc if end is not None else None),
        AiTask.status.in_(("succeeded", "failed")),
        AiTask.trigger_type != "health_probe",
    ]

    groups: dict[tuple[str, str], dict[str, Any]] = {}

    def _group(cap: str, mdl: str | None) -> dict[str, Any]:
        key = (cap, mdl or "")
        if key not in groups:
            groups[key] = {"capability": cap, "model": mdl or "", "attempts": 0, "succeeded": 0, "failed": 0,
                           "p95_duration_ms": None, "failures_by_category": {}, "_durations": []}
        return groups[key]

    count_stmt = select(AiTask.capability, AiTask.model, AiTask.status, AiTask.error_category, func.count(AiTask.id)).where(
        *conditions).group_by(AiTask.capability, AiTask.model, AiTask.status, AiTask.error_category)
    failures: dict[str, int] = {}
    succeeded_total = failed_total = 0
    for cap, mdl, status, category, count in db.execute(scope_by_project(count_stmt, AiTask.project_id, scope)).all():
        group = _group(cap, mdl)
        n = int(count or 0)
        group["attempts"] += n
        if status == "succeeded":
            group["succeeded"] += n
            succeeded_total += n
        else:
            group["failed"] += n
            failed_total += n
            cat = category or "unknown"
            group["failures_by_category"][cat] = group["failures_by_category"].get(cat, 0) + n
            failures[cat] = failures.get(cat, 0) + n

    duration_stmt = (
        select(AiTask.capability, AiTask.model, AiTask.duration_ms)
        .where(*conditions, AiTask.status == "succeeded", AiTask.duration_ms.is_not(None))
        .order_by(AiTask.created_at.desc(), AiTask.id.desc())
        .limit(ATTEMPT_STATS_P95_SAMPLE + 1)
    )
    samples = db.execute(scope_by_project(duration_stmt, AiTask.project_id, scope)).all()
    warnings: list[str] = []
    if len(samples) > ATTEMPT_STATS_P95_SAMPLE:
        samples = samples[:ATTEMPT_STATS_P95_SAMPLE]
        warnings.append("p95_sampled")
    all_durations: list[int] = []
    for cap, mdl, duration in samples:
        _group(cap, mdl)["_durations"].append(int(duration))
        all_durations.append(int(duration))

    def _categories(counts: dict[str, int]) -> list[dict[str, Any]]:
        return [{"error_category": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    by_model = []
    for group in sorted(groups.values(), key=lambda g: (-g["attempts"], g["capability"], g["model"])):
        durations = group.pop("_durations")
        group["p95_duration_ms"] = percentile_95(durations)
        group["failures_by_category"] = _categories(group["failures_by_category"])
        by_model.append(group)
    return {
        "start": iso_utc(start_utc),
        "end": iso_utc(end_utc),
        "attempts": succeeded_total + failed_total,
        "succeeded": succeeded_total,
        "failed": failed_total,
        "p95_duration_ms": percentile_95(all_durations),
        "failures_by_category": _categories(failures),
        "by_model": by_model,
        "warnings": warnings,
    }



# =====================================================================
# 健康探测：探测对象、窗口规则、快照（docs/08 §12.1~§12.5）
# =====================================================================

HEALTH_KEY_PREFIX = gateway.HEALTH_KEY_PREFIX
HEALTH_TTL_SECONDS = 86400
HEALTH_WINDOW = 5                      # 最近 5 条 route_probe 尝试行（§12.4 ④）
HEALTH_DOWN_CONSECUTIVE = 2            # consecutive_failures >= 2 → down（§12.4 ③）
PROBE_TIMEOUT_SECONDS = 30             # 单模型探测固定 timeout（§12.1）
PROBE_TOTAL_TIMEOUT_SECONDS = 35       # 一键测试：模型并行、总超时 35s（§12.3）
PROBE_MAX_PARALLEL = 8


@dataclass(frozen=True)
class ProbeTarget:
    """一个探测对象：``(capability, model)``；``preferred_protocol`` 为路由 / 引擎协议（经 ``protocol_for`` 按目录校正），
    ``route_id`` 为空时取该能力的全局路由（``run_route_probe``）。"""

    capability: str
    model: str
    preferred_protocol: str | None = None
    route_id: int | None = None


health_key = gateway.health_key


def read_health(capability: str, model: str) -> dict[str, Any] | None:
    """``ai:health:{capability}:{model}`` 快照 ``{status, latency_ms, checked_at, request_id, error_category, consecutive_failures}``。"""
    data = cache_get_json(health_key(capability, model))
    return data if isinstance(data, dict) else None


_route_models = gateway.route_models


def probe_targets(db: Session) -> list[ProbeTarget]:
    """自动探测 / 健康快照的对象（§12.2）：每条 ``is_enabled=1`` 路由（全局行优先）的主 / 备模型 + 启用且 ``model`` 非空的
    GEO 引擎（``geo_check``，引擎自身协议）+ ``seo_providers.engines.<e>.model`` 非空的覆盖模型（``seo_check``），
    按 ``(capability, model)`` 去重。"""
    order = {c.value: i for i, c in enumerate(Capability)}
    routes = list(db.scalars(select(CapabilityRoute).where(CapabilityRoute.is_enabled == True)).all())  # noqa: E712
    routes.sort(key=lambda r: (int(r.project_id or 0), order.get(r.capability, 99), r.id))
    targets: list[ProbeTarget] = []
    seen: set[tuple[str, str]] = set()

    def _add(target: ProbeTarget) -> None:
        key = (target.capability, target.model)
        if target.model and key not in seen:
            seen.add(key)
            targets.append(target)

    for route in routes:
        for model in _route_models(route):
            _add(ProbeTarget(route.capability, model, route.protocol, route.id))
    geo = settings_service.get_config(db, "geo_engines")
    for engine in geo.get("engines") or []:
        if isinstance(engine, dict) and engine.get("enabled") and str(engine.get("model") or "").strip():
            _add(ProbeTarget(Capability.GEO_CHECK.value, str(engine["model"]).strip(), engine.get("protocol") or None))
    seo = settings_service.get_config(db, "seo_providers")
    seo_protocol = ((seo.get("providers") or {}).get("zhiqi_web_search") or {}).get("protocol") or None
    for engine in (seo.get("engines") or {}).values():
        if isinstance(engine, dict) and str(engine.get("model") or "").strip():
            _add(ProbeTarget(Capability.SEO_CHECK.value, str(engine["model"]).strip(), seo_protocol))
    return targets


def _default_protocol(capability: Capability) -> Protocol:
    if capability == Capability.IMAGE:
        return Protocol.IMAGE_ASYNC
    if capability == Capability.VIDEO:
        return Protocol.VIDEO
    return Protocol.OPENAI_CHAT


def resolve_probe_protocol(db: Session, capability: Capability | str, model: str, preferred: Protocol | str | None) -> Protocol | None:
    """``preferred`` 非空 → ``catalog.protocol_for(catalog_entry(model), preferred)``（无可用端点时保留 ``preferred``，由上游判定）；
    为空返回 ``None``（``run_route_probe`` 按该能力全局路由协议解析）。"""
    if not preferred:
        return None
    capability = Capability(capability)
    try:
        pref = Protocol(preferred)
    except ValueError:
        return None
    if (capability in TEXT_CAPABILITIES) != (pref in TEXT_PROTOCOLS):
        return None
    return catalog.protocol_for(ai_catalog_service.catalog_entry(db, model), pref) or pref


def _probe_rule(db: Session) -> dict[str, Any]:
    rules = (settings_service.get_config(db, "alert_config").get("rules") or {})
    return rules.get("ai_upstream_unavailable") or {}


def record_health_result(db: Session, result: HealthResult, *, probed_upstream: bool) -> dict[str, Any]:
    """§12.4 窗口规则 + 写入（``probe_routes`` 与一键测试共用），提交后返回写入 ``ai:health:*`` 的快照。

    - 单次结果 ``unknown``：只写 ``ai:health:*.status=unknown``，保留原 ``consecutive_failures``；
    - ``consecutive_failures``：单次 ``down`` 则 +1，否则清零；``>= 2`` → ``down``；否则最近 5 条 ``route_probe`` 尝试行
      （含本次）有失败 → ``degraded``；都成功 → 单次结果；最终值写 ``ai:health:*`` 与 ``ai_models.last_health_*``；
    - 连续失败达 ``alert_config.rules.ai_upstream_unavailable.consecutive_probes``（2）→ ``force_open(reason="probe_down")``
      （``model_unavailable`` 的熔断不覆盖）+ ``ai_upstream_unavailable``（critical；熔断由非 open → open 时另发 ``ai_breaker_open``）；
    - 探测成功 → 自动解决 ``ai_upstream_unavailable``；``probe_down`` 的熔断 ``reset()``；实际发起了上游调用时
      ``half_open → record_success()`` 关闭并 ``DEL ai:paused:*``（``failures`` / ``model_unavailable`` 的熔断不因探测成功关闭）。
    """
    capability = result.capability.value
    model = result.model
    previous = read_health(capability, model) or {}
    prev_failures = int(previous.get("consecutive_failures") or 0)
    checked_at = iso_utc(result.checked_at)
    if result.status == "unknown":
        snapshot = {
            "status": "unknown", "latency_ms": result.latency_ms, "checked_at": checked_at, "request_id": None,
            "error_category": None, "consecutive_failures": prev_failures,
        }
        cache_set_json(health_key(capability, model), snapshot, HEALTH_TTL_SECONDS)
        return snapshot

    single = result.status
    failures = prev_failures + 1 if single == "down" else 0
    if failures >= HEALTH_DOWN_CONSECUTIVE:
        final = "down"
    else:
        recent = db.scalars(
            select(AiTask.status)
            .where(
                AiTask.root_task_id.is_not(None), AiTask.operation == "route_probe", AiTask.capability == capability,
                AiTask.model == model, AiTask.status.in_(("succeeded", "failed")),
            )
            .order_by(AiTask.id.desc())
            .limit(HEALTH_WINDOW)
        ).all()
        final = "degraded" if any(s == "failed" for s in recent) else single
    snapshot = {
        "status": final,
        "latency_ms": result.latency_ms,
        "checked_at": checked_at,
        "request_id": result.request_id,
        "error_category": result.error_category.value if result.error_category else None,
        "consecutive_failures": failures,
    }
    cache_set_json(health_key(capability, model), snapshot, HEALTH_TTL_SECONDS)

    row = ai_catalog_service.get_model_by_id(db, model)
    if row is not None:
        row.last_health_status = final
        row.last_health_at = result.checked_at
        row.last_health_latency_ms = result.latency_ms

    cfg = gateway._cfg(db)  # noqa: SLF001
    breaker = gateway.get_breaker(cfg)
    target_key = f"{capability}:{model}"
    if single == "down":
        rule = _probe_rule(db)
        threshold = max(1, int(rule.get("consecutive_probes") or HEALTH_DOWN_CONSECUTIVE))
        if failures >= threshold:
            if breaker.reason(capability, model) != "model_unavailable" and breaker.force_open(capability, model, reason="probe_down"):
                alert_service.raise_alert(
                    db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key=target_key,
                    title=f"AI 模型熔断：{target_key}",
                    message=f"模型 {model}（能力 {capability}）连续 {failures} 次健康探测失败，已熔断；最近错误 {snapshot['error_category']}",
                    payload={"capability": capability, "model": model, "reason": "probe_down", "request_id": result.request_id},
                )
            alert_service.raise_alert(
                db, SYSTEM_SCOPE, "ai_upstream_unavailable", target_type="ai_model", target_key=target_key,
                title=f"AI 上游不可用：{target_key}",
                message=(
                    f"模型 {model}（能力 {capability}）连续 {failures} 次健康探测失败，最近错误 {snapshot['error_category']}："
                    f"{(result.error_message or '')[:300]}"
                ),
                payload={
                    "capability": capability, "model": model, "consecutive_failures": failures,
                    "error_category": snapshot["error_category"], "request_id": result.request_id,
                },
            )
    else:
        alert_service.resolve_alert(db, SYSTEM_SCOPE, "ai_upstream_unavailable", "ai_model", target_key)
        state = breaker.state(capability, model)
        if state == "open" and breaker.reason(capability, model) == "probe_down":
            gateway.reset_breaker(db, capability, model, config=cfg)
        elif state == "half_open" and probed_upstream and breaker.record_success(capability, model):
            alert_service.resolve_alert(db, SYSTEM_SCOPE, "ai_breaker_open", "ai_model", target_key)
        if probed_upstream:
            try:
                gateway.clear_paused()
            except Exception:  # noqa: BLE001
                logger.warning("探测成功后删除 ai:paused:* 失败", exc_info=True)
    db.commit()
    return snapshot


def probe_and_record(
    db: Session,
    *,
    capability: Capability | str,
    model: str,
    preferred_protocol: Protocol | str | None = None,
    route_id: int | None = None,
    created_by: int | None = None,
    probe_media: bool = False,
    worker_id: str | None = None,
) -> HealthResult:
    """单模型探测（记 ``route_probe`` 根任务 + 尝试行）并按 §12.4 写健康快照；探测失败不抛异常（``status="down"``）。"""
    capability = Capability(capability)
    protocol = resolve_probe_protocol(db, capability, model, preferred_protocol)
    result = run_route_probe(
        db, capability=capability, model=model, protocol=protocol, route_id=route_id, created_by=created_by,
        probe_media=probe_media, worker_id=worker_id,
    )
    record_health_result(db, result, probed_upstream=capability in TEXT_CAPABILITIES or bool(probe_media))
    return result


def failed_probe_result(target: ProbeTarget, category: ErrorCategory, message: str) -> HealthResult:
    capability = Capability(target.capability)
    try:
        protocol = Protocol(target.preferred_protocol) if target.preferred_protocol else _default_protocol(capability)
    except ValueError:
        protocol = _default_protocol(capability)
    return HealthResult(
        capability=capability, model=target.model, protocol=protocol, status="down", latency_ms=None,  # type: ignore[arg-type]
        request_id=None, error_category=category, error_message=message, checked_at=zhiqi_utcnow(),
    )


def _probe_in_session(target: ProbeTarget, *, created_by: int | None, probe_media: bool, worker_id: str | None) -> HealthResult:
    with SessionLocal() as session:
        try:
            return probe_and_record(
                session, capability=target.capability, model=target.model, preferred_protocol=target.preferred_protocol,
                route_id=target.route_id, created_by=created_by, probe_media=probe_media, worker_id=worker_id,
            )
        except Exception as exc:  # noqa: BLE001 - 单个模型探测异常不影响其它模型
            session.rollback()
            logger.exception("健康探测异常 capability=%s model=%s", target.capability, target.model)
            return failed_probe_result(target, ErrorCategory.UNKNOWN, f"{type(exc).__name__}: {exc}"[:500])


def _sqlite_bound() -> bool:
    bind = SessionLocal.kw.get("bind")
    return bind is not None and getattr(bind.dialect, "name", "") == "sqlite"


def probe_many(
    targets: list[ProbeTarget],
    *,
    created_by: int | None = None,
    probe_media: bool = False,
    timeout: float = PROBE_TOTAL_TIMEOUT_SECONDS,
    worker_id: str | None = None,
) -> list[HealthResult]:
    """多模型并行探测（每个模型独立会话，§12.3「模型并行、总超时 35s」），按 ``targets`` 顺序返回；超时未完成的模型
    以 ``down(timeout)`` 返回（后台线程完成后照常写记录）。SQLite（测试会话共享单连接）下串行执行。"""
    if not targets:
        return []
    workers = 1 if _sqlite_bound() else max(1, min(len(targets), PROBE_MAX_PARALLEL))
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ai-probe")
    try:
        futures = [
            executor.submit(_probe_in_session, t, created_by=created_by, probe_media=probe_media, worker_id=worker_id)
            for t in targets
        ]
        done, _pending = futures_wait(futures, timeout=timeout)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    results: list[HealthResult] = []
    for target, future in zip(targets, futures, strict=True):
        if future in done and not future.cancelled():
            results.append(future.result())
        else:
            results.append(failed_probe_result(target, ErrorCategory.TIMEOUT, "探测超时"))
    return results


def probe_result_item(result: HealthResult) -> dict[str, Any]:
    """``POST /admin/ai/health/probe`` 的 ``HealthResult``（``checked_at`` 为 ISO 8601 UTC）。"""
    data = result.to_dict()
    data["checked_at"] = iso_utc(result.checked_at)
    return data


def route_test_item(result: HealthResult) -> dict[str, Any]:
    """``POST /admin/ai/routes/{id}/test`` 的结果项 ``{model, status, latency_ms, request_id, error_category}``。"""
    return {
        "model": result.model,
        "status": result.status,
        "latency_ms": result.latency_ms,
        "request_id": result.request_id,
        "error_category": result.error_category.value if result.error_category else None,
    }


def test_route(db: Session, scope: DataScope, route_id: int, *, probe_media: bool = False, admin_id: int | None = None) -> list[dict[str, Any]]:
    """一键测试（§12.3）：对路由主模型与每个备选模型各探测一次（API 进程内同步执行，模型并行、总超时 35s），
    结果写 ``ai:health:*`` 与 ``ai_models.last_health_*`` 并记 ``route_probe`` 根任务（``created_by`` = 操作人）。"""
    route = get_visible(db, scope, CapabilityRoute, route_id)
    targets = [ProbeTarget(route.capability, model, route.protocol, route.id) for model in _route_models(route)]
    db.rollback()  # 结束读事务：探测在各自会话内执行
    return [route_test_item(r) for r in probe_many(targets, created_by=admin_id, probe_media=probe_media)]


test_route.__test__ = False  # type: ignore[attr-defined]  # 防止 pytest 把本函数当作测试收集


def probe_model(
    db: Session, *, capability: Capability | str, model: str, protocol: Protocol | str | None = None,
    probe_media: bool = False, admin_id: int | None = None,
) -> dict[str, Any]:
    """``POST /admin/ai/health/probe``（§12.3）：``model`` 须存在于 ``ai_models``，否则 400（``invalid_model``）；
    ``protocol`` 缺省取 ``protocol_for(目录项, 该能力全局路由.protocol)``；``route_id`` 取该能力的全局路由。"""
    capability = Capability(capability)
    model = (model or "").strip()
    if not model or ai_catalog_service.get_model_by_id(db, model) is None:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["body", "model"], "模型不存在", "invalid_model", model)])
    if protocol:
        pref = Protocol(protocol)
        if (capability in TEXT_CAPABILITIES) != (pref in TEXT_PROTOCOLS):
            raise BusinessError(
                "参数错误", code=CODE_BAD_REQUEST,
                data=[field_error(["body", "protocol"], "协议与能力不匹配", "invalid_protocol", pref.value)],
            )
    else:
        route = gateway.load_route(db, capability.value, 0)
        pref = Protocol(route["protocol"]) if route else _default_protocol(capability)
    db.rollback()
    target = ProbeTarget(capability.value, model, pref.value, None)
    result = probe_many([target], created_by=admin_id, probe_media=probe_media)[0]
    return probe_result_item(result)


def health_snapshot(db: Session) -> dict[str, Any]:
    """``GET /admin/ai/health`` 的 ``{zhiqi_mode, base_url, paused, models[]}``（``workers[]`` 由路由层按心跳键补充）：
    ``models[]`` 与自动探测对象相同（``(capability, model)`` 去重，含 GEO/SEO 引擎覆盖模型），带最近探测快照与熔断状态。"""
    try:
        paused = {reason: bool(redis_client.exists(f"{gateway.PAUSED_PREFIX}{reason}")) for reason in gateway.PAUSE_REASONS}
    except Exception:  # noqa: BLE001
        logger.warning("读取 ai:paused:* 失败", exc_info=True)
        paused = dict.fromkeys(gateway.PAUSE_REASONS, False)
    targets = probe_targets(db)
    names = list({t.model for t in targets})
    rows = {r.model_id: r for r in db.scalars(select(AiModel).where(AiModel.model_id.in_(names))).all()} if names else {}
    breaker = gateway.get_breaker(gateway._cfg(db))  # noqa: SLF001
    models = []
    for target in targets:
        snap = read_health(target.capability, target.model) or {}
        row = rows.get(target.model)
        state = breaker.state(target.capability, target.model)
        models.append(
            {
                "capability": target.capability,
                "model": target.model,
                "status": snap.get("status") or (row.last_health_status if row is not None else None) or "unknown",
                "latency_ms": snap.get("latency_ms") if snap else (row.last_health_latency_ms if row is not None else None),
                "checked_at": snap.get("checked_at") if snap else (iso_utc(row.last_health_at) if row is not None else None),
                "breaker_state": state,
                "breaker_reason": breaker.reason(target.capability, target.model) if state != "closed" else None,
                "is_available": bool(row.is_available) if row is not None else None,
            }
        )
    return {
        "zhiqi_mode": "mock" if settings.zhiqi_mock_mode else "live",
        "base_url": settings.zhiqi_base_url,
        "paused": paused,
        "models": models,
    }

__all__ = [
    "AI_TASK_EXPORT_COLUMNS",
    "HANDLER_MODULES",
    "REGISTRY",
    "HandlerSpec",
    "Heartbeat",
    "ProbeTarget",
    "TaskAbandoned",
    "TaskContext",
    "TaskOutcome",
    "beat",
    "cancel_task",
    "claim",
    "dispatch",
    "export_tasks",
    "failed_probe_result",
    "get_handler",
    "load_handlers",
    "get_task_detail",
    "health_snapshot",
    "list_tasks",
    "notify_batch_finished",
    "probe_and_record",
    "probe_many",
    "probe_model",
    "probe_targets",
    "process_one",
    "read_health",
    "record_health_result",
    "register_handler",
    "resolve_probe_protocol",
    "retry_task",
    "run_route_probe",
    "set_batch_finished_hook",
    "task_item",
    "test_route",
    "unregister_handler",
]
