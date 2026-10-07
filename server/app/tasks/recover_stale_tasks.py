"""僵死回收（docs/03「任务幂等与状态收敛」第 8 条 ①~⑥；docs/01 §5.1 第 7 行；docs/08 §7.5；docs/09 §9.3、§9.7；docs/10 §7）。

``recover() -> dict``：每 60s 且 ``app.worker`` 启动时一次，持 ``lock:worker:recover``（120s）。六步各自独立会话与事务，
任一步异常只记日志、不影响其余步骤：

① ``running`` 根任务 ``COALESCE(heartbeat_at, started_at) < now − WORKER_STALE_TASK_MINUTES``（逐条独立事务）：
   - 同步执行的根任务（``trigger_type ∈ worker / health_probe``、``capability ∈ seo_check / geo_check``、
     ``operation ∈ route_probe / image_prompt``）→ 只置 ``failed(timeout)``，不重试、不入队；
   - ``upstream_task_id`` 非空（媒体已受理）→ 改置 ``polling``（``next_poll_at = now``，``poll_count`` 不变，``deadline_at`` 为空时补
     ``now + media_config.<image|video>.poll_budget_seconds``），资产 ``pending → submitted``；
   - 任一尝试行 ``request_id`` 非空（可能已计费）→ ``failed(timeout)`` 并同事务写 ``response_meta_json.stale_after_submit = true``，
     不自动重试；
   - 文本能力且全部尝试行 ``request_id IS NULL`` → ``failed(timeout)`` + 自动新建重试根任务（``trigger_type=system``、
     ``parent_task_id``，复制 ``project_id`` / ``capability`` / ``operation`` / ``target_type`` / ``target_id`` / ``batch_id`` /
     ``input_json``，提交后 ``RPUSH``；有批次时同事务 ``task_failed −1``、批次保持 ``running``）；所属批次已 ``cancelled`` 或
     已存在重试根任务时不自动重试；
   - ``image`` / ``video`` 且 ``request_id IS NULL`` → ``failed(timeout)`` + 资产 ``failed`` + ``media_task_failed``，交人工 ``retry``；
   以上失败分支都经 ``finalize_root``（``settle_quota``、``stats:rt``），提交后调用一次 ``on_task_finished(batch_id)``。
② ``polling`` 且 ``deadline_at < now`` → ``expired``，资产 ``expired`` + ``media_task_failed``。
③ ``queued`` 且 ``updated_at`` 超过 10 分钟且 ``LPOS queue:ai_tasks`` 不存在 → ``RPUSH``（``ai:paused:*`` 存在时整步跳过）。
④ 批次兜底：``running`` 批次的根任务全部终态 → 在 ``lock:generation_batch:{id}`` 内按库重算 ``task_done`` / ``task_failed`` /
   ``produced_count`` / ``error_summary`` 并收敛；无活动根任务且 ``heartbeat_at`` 超 30 分钟 → ``partial``。
⑤ ``SCAN worker:heartbeat:monitor_worker:*`` → 副本级 / 进程级 ``worker_stale``，恢复或副本键过期时自动解决。
⑥ 资产 ``downloading`` 且 ``updated_at`` 超 15 分钟且 ``lock:media:transfer:{id}`` 不存在 → 视为一次转存失败重排。

媒体相关分支优先调用业务钩子（第 4 步 ``media_service`` 提供，存在即自动启用；否则使用本文件内按文档实现的兜底写法）：

- ``ai_task_service`` 注册表中该 ``operation`` 的 ``on_failed(db, root, category, message)``（资产 ``failed`` + 告警）；
- ``media_service.on_task_failed(db, root, category, message)``（无注册表钩子时）；
- ``media_service.on_stale_polling(db, root)``：① 改回轮询时资产 ``pending → submitted``；
- ``media_service.on_task_expired(db, root)``：② 资产 ``expired`` + 告警；
- ``media_service.record_transfer_failure(db, asset, error_message)``：⑥ 一次转存失败（重排或 ``failed(transfer_failed)``）。

批次收敛优先调用 ``generation_service.recompute_batch(db, batch_id) -> str | None``（第 3 步提供；调用时本任务已持
``lock:generation_batch:{id}``，钩子只写库、不提交、不取锁，返回收敛后的批次状态），否则使用本文件的按库重算兜底。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.locks import acquire_lock, is_locked, release_lock
from app.core.redis import redis_client
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import TEXT_CAPABILITIES, ErrorCategory
from app.models import (
    AiTask,
    Alert,
    GenerationBatch,
    Keyword,
    MediaAsset,
    Title,
    utcnow,
)
from app.services import ai_gateway_service as gateway
from app.services import ai_task_service, alert_service, settings_service, stats_service
from app.services.data_scope_service import SYSTEM_SCOPE

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:worker:recover"
LOCK_TTL = 120
QUEUED_STALE_MINUTES = 10
BATCH_IDLE_MINUTES = 30
TRANSFER_STALE_MINUTES = 15
TRANSFER_LOCK_PREFIX = "lock:media:transfer:"
BATCH_LOCK_PREFIX = "lock:generation_batch:"
BATCH_LOCK_TTL = 60
HEARTBEAT_PREFIX = "worker:heartbeat:"
HEARTBEAT_ALIVE_SECONDS = 90
SCAN_LIMIT = 500
STALE_MESSAGE = "执行进程心跳超时，任务已回收"
EXPIRED_MESSAGE = "轮询超过截止时间（deadline_at）"
TRANSFER_STALE_MESSAGE = "转存进程失联（锁已过期），按一次转存失败处理"
MEDIA_CAPABILITIES = frozenset({"image", "video"})
TEXT_CAPABILITY_VALUES = frozenset(c.value for c in TEXT_CAPABILITIES)
ACTIVE_ROOT_STATUSES = ("queued", "running", "polling")
FAILED_ROOT_STATUSES = ("failed", "cancelled", "expired")
MEDIA_TERMINAL_STATUSES = ("ready", "failed", "expired", "deleted")
APPLY_COUNT_KEYS = ("duplicates", "invalid", "intent_missing", "empty_output", "too_long")
ERROR_SUMMARY_MAX = 1000


# =====================================================================
# 业务钩子（第 3 / 4 步提供，存在即启用）
# =====================================================================


def _service_hook(module_name: str, attr: str) -> Callable[..., Any] | None:
    if importlib.util.find_spec(module_name) is None:
        return None
    module = importlib.import_module(module_name)
    fn = getattr(module, attr, None)
    return fn if callable(fn) else None


def media_hook(name: str) -> Callable[..., Any] | None:
    """``app.services.media_service.<name>``（不存在时 ``None``，走兜底写法）。"""
    return _service_hook("app.services.media_service", name)


def batch_hook() -> Callable[..., Any] | None:
    """``app.services.generation_service.recompute_batch``（不存在时 ``None``，走兜底写法）。"""
    return _service_hook("app.services.generation_service", "recompute_batch")


# =====================================================================
# 入口
# =====================================================================


def recover(now: datetime | None = None) -> dict[str, Any]:
    """执行 ①~⑥，返回各步计数；锁被占用时返回 ``{"skipped": True}``。"""
    token = acquire_lock(LOCK_KEY, LOCK_TTL)
    if not token:
        logger.info("%s 被占用，本轮回收跳过", LOCK_KEY)
        return {"skipped": True}
    now = now or utcnow()
    result: dict[str, Any] = {}
    steps: tuple[tuple[str, Callable[[Session, datetime], dict[str, int]]], ...] = (
        ("①", recover_running),
        ("②", expire_polling),
        ("③", requeue_queued),
        ("④", converge_batches),
        ("⑤", lambda db, at: check_worker_stale(db, "monitor_worker", at)),
        ("⑥", recover_transfers),
    )
    try:
        for label, step in steps:
            try:
                with SessionLocal() as db:
                    result.update(step(db, now))
            except Exception:  # noqa: BLE001 - 单步失败不影响其余步骤
                logger.exception("僵死回收 %s 执行失败", label)
                result[f"error_{label}"] = True
    finally:
        release_lock(LOCK_KEY, token)
    if any(v for k, v in result.items() if isinstance(v, int) and not isinstance(v, bool) and v > 0):
        logger.info("僵死回收完成：%s", result)
    return result


# =====================================================================
# ① running 根任务心跳超时
# =====================================================================


def recover_running(db: Session, now: datetime) -> dict[str, int]:
    counts = Counter({"stale_failed": 0, "stale_polling": 0, "stale_after_submit": 0, "auto_retried": 0})
    cutoff = now - timedelta(minutes=max(1, int(settings.worker_stale_task_minutes)))
    last_alive = func.coalesce(AiTask.heartbeat_at, AiTask.started_at, AiTask.created_at)
    ids = db.scalars(
        select(AiTask.id)
        .where(AiTask.root_task_id.is_(None), AiTask.status == "running", last_alive < cutoff)
        .order_by(AiTask.id)
        .limit(SCAN_LIMIT)
    ).all()
    db.rollback()
    for task_id in ids:
        try:
            outcome = _recover_one(db, task_id, cutoff, now)
        except Exception:  # noqa: BLE001 - 单条失败不影响其余
            db.rollback()
            logger.exception("回收僵死根任务失败 task_id=%s", task_id)
            continue
        for key in outcome:
            counts[key] += 1
    return dict(counts)


def _attempts(db: Session, root: AiTask) -> list[AiTask]:
    return list(db.scalars(select(AiTask).where(AiTask.root_task_id == root.id).order_by(AiTask.id)).all())


def _fail_running_attempts(attempts: list[AiTask], now: datetime) -> None:
    """失联执行者留下的 ``running`` 尝试行置 ``failed(timeout)``（尝试行按实际结果保留，未完成的视为超时）。"""
    for attempt in attempts:
        if attempt.status != "running":
            continue
        attempt.status = "failed"
        attempt.error_category = "timeout"
        attempt.error_message = STALE_MESSAGE
        attempt.finished_at = now
        if attempt.duration_ms is None:
            started = attempt.started_at or attempt.created_at or now
            attempt.duration_ms = max(0, int((now - started).total_seconds() * 1000))
        if attempt.cost_cny is None:
            attempt.cost_cny = Decimal(0)


def _poll_budget(db: Session, capability: str) -> int:
    media = settings_service.get_config(db, "media_config")
    section = media.get("video" if capability == "video" else "image") or {}
    default = settings.zhiqi_video_poll_budget_seconds if capability == "video" else settings.zhiqi_image_poll_budget_seconds
    return max(1, int(section.get("poll_budget_seconds") or default))


def _release_task_lock(task_id: int) -> None:
    try:
        redis_client.delete(f"{ai_task_service.TASK_LOCK_PREFIX}{task_id}")
    except redis.RedisError:
        logger.warning("删除 lock:ai_task:%s 失败", task_id, exc_info=True)


def _recover_one(db: Session, task_id: int, cutoff: datetime, now: datetime) -> list[str]:
    root = db.get(AiTask, task_id)
    if root is None or root.root_task_id is not None or root.status != "running":
        return []
    last = root.heartbeat_at or root.started_at or root.created_at
    if last is not None and last >= cutoff:
        return []
    attempts = _attempts(db, root)
    spec = ai_task_service.get_handler(root.operation)
    batch_id = root.batch_id

    # 同步执行的根任务：只置 failed(timeout)
    if gateway.is_sync_root(root):
        _fail_running_attempts(attempts, now)
        _fail_root(db, root, spec, "timeout", STALE_MESSAGE)
        db.commit()
        _after_terminal(root.id, batch_id)
        return ["stale_failed"]

    # 媒体已受理：继续轮询
    upstream = root.upstream_task_id or next(
        (a.upstream_task_id for a in reversed(attempts) if a.upstream_task_id and a.status == "succeeded"), None
    )
    if upstream and root.capability in MEDIA_CAPABILITIES:
        _resume_polling(db, root, upstream, now)
        db.commit()
        _release_task_lock(root.id)
        return ["stale_polling"]

    _fail_running_attempts(attempts, now)

    # 已发出请求（可能已计费）：failed(timeout) + stale_after_submit，不自动重试
    if any(a.request_id for a in attempts):
        gateway.update_task_meta(root, stale_after_submit=True)
        _fail_root(db, root, spec, "timeout", STALE_MESSAGE)
        db.commit()
        _after_terminal(root.id, batch_id)
        return ["stale_failed", "stale_after_submit"]

    # 文本且从未发出请求：failed(timeout) + 自动重试
    if root.capability in TEXT_CAPABILITY_VALUES:
        retried = False
        if _can_auto_retry(db, root):
            try:
                _fail_root(db, root, spec, "timeout", STALE_MESSAGE)
                _create_auto_retry(db, root, spec, now)
                db.commit()
                retried = True
            except Exception:  # noqa: BLE001 - 重试根任务创建失败：只置 failed(timeout)
                db.rollback()
                logger.exception("自动重试根任务创建失败 task_id=%s，只置 failed(timeout)", root.id)
                root = db.get(AiTask, task_id)
                if root is None or root.status != "running":
                    return []
                _fail_running_attempts(_attempts(db, root), now)
        if not retried:
            _fail_root(db, root, spec, "timeout", STALE_MESSAGE)
            db.commit()
        _after_terminal(root.id, batch_id)
        return ["stale_failed", "auto_retried"] if retried else ["stale_failed"]

    # 媒体且从未发出请求：failed(timeout) + 资产 failed + 告警，交人工 retry
    _fail_root(db, root, spec, "timeout", STALE_MESSAGE)
    db.commit()
    _after_terminal(root.id, batch_id)
    return ["stale_failed"]


def _after_terminal(task_id: int, batch_id: int | None) -> None:
    _release_task_lock(task_id)
    ai_task_service.notify_batch_finished(batch_id)


def _fail_root(db: Session, root: AiTask, spec: Any, category: str, message: str) -> None:
    """根任务失败的业务收尾（注册表 ``on_failed`` / 媒体钩子 / 兜底）后 ``finalize_root(failed)``（只 flush）。"""
    _business_failed(db, root, spec, category, message)
    gateway.finalize_root(db, root, "failed", error_category=category, error_message=message)


def _business_failed(db: Session, root: AiTask, spec: Any, category: str, message: str) -> None:
    if spec is not None and spec.on_failed is not None:
        spec.on_failed(db, root, category, message)
        return
    if root.capability not in MEDIA_CAPABILITIES:
        return
    hook = media_hook("on_task_failed")
    if hook is not None:
        hook(db, root, category, message)
        return
    fail_media_assets(db, root, status="failed", category=category, message=message)


def _resume_polling(db: Session, root: AiTask, upstream: str, now: datetime) -> None:
    root.upstream_task_id = upstream
    root.status = "polling"
    root.next_poll_at = now
    if root.deadline_at is None:
        root.deadline_at = now + timedelta(seconds=_poll_budget(db, root.capability))
    root.locked_by = None
    root.heartbeat_at = None
    hook = media_hook("on_stale_polling")
    if hook is not None:
        hook(db, root)
        return
    for asset in _task_assets(db, root):
        if asset.status == "pending":
            asset.status = "submitted"
            asset.upstream_task_id = asset.upstream_task_id or upstream


def _can_auto_retry(db: Session, root: AiTask) -> bool:
    if root.batch_id:
        status = db.scalar(select(GenerationBatch.status).where(GenerationBatch.id == root.batch_id))
        if status == "cancelled":
            return False
    existing = db.scalar(select(AiTask.id).where(AiTask.parent_task_id == root.id, AiTask.root_task_id.is_(None)).limit(1))
    return existing is None


def _create_auto_retry(db: Session, root: AiTask, spec: Any, now: datetime) -> AiTask:
    """自动重试根任务（``trigger_type=system``、``parent_task_id``，复制归属与 ``input_json``，提交后 ``RPUSH``）；
    有批次时同事务 ``task_failed −1``、批次保持 ``running``（抵消旧根任务提交后 ``on_task_finished`` 的 +1）。"""
    input_data = gateway.task_input(root)
    route = None
    try:
        route = gateway.resolve_route(db, root.capability, root.project_id, model_override=input_data.get("model") or None)
    except Exception:  # noqa: BLE001 - 路由暂不可用：照常建任务，由 worker 执行时按常规失败
        logger.info("自动重试：能力 %s 路由暂不可用，按缺省建任务", root.capability)
    new_task = gateway.create_root_task(
        db, capability=root.capability, operation=root.operation, project_id=root.project_id, created_by=root.created_by,
        trigger_type="system", target_type=root.target_type, target_id=root.target_id, batch_id=root.batch_id,
        template_id=root.template_id, input=input_data, route=route, parent_task_id=root.id,
    )
    reserved = int(root.quota_reserved or 0)
    if reserved > 0:
        gateway.check_quota(db, project_id=root.project_id, estimated_quota=reserved, task=new_task)
    try:
        if root.batch_id:
            batch = db.get(GenerationBatch, root.batch_id)
            if batch is not None:
                batch.task_failed = max(0, int(batch.task_failed or 0) - 1)
                batch.heartbeat_at = now
        if spec is not None and spec.before_retry is not None:
            spec.before_retry(db, root, new_task)
        db.flush()
    except Exception:
        gateway.release_reservation(db, new_task)
        raise
    return new_task


# =====================================================================
# 媒体兜底写法（media_service 未提供钩子时）
# =====================================================================


def _task_assets(db: Session, root: AiTask) -> list[MediaAsset]:
    stmt = select(MediaAsset).where(MediaAsset.ai_task_id == root.id)
    assets = {a.id: a for a in db.scalars(stmt).all()}
    if root.target_type == "media_asset" and root.target_id and root.target_id not in assets:
        asset = db.get(MediaAsset, root.target_id)
        if asset is not None and asset.ai_task_id in (None, root.id):
            assets[asset.id] = asset
    return list(assets.values())


def _media_alert(db: Session, asset: MediaAsset, *, status: str, category: str, message: str) -> None:
    kind = "视频" if asset.kind == "video" else "图片"
    action = "已过期" if status == "expired" else "失败"
    alert_service.raise_alert(
        db, SYSTEM_SCOPE, "media_task_failed",
        target_type="media_asset", target_id=asset.id, project_id=asset.project_id,
        title=f"{kind}任务{action}：素材 #{asset.id}",
        message=f"{kind}素材 #{asset.id} {action}（{category}）：{message}"[:1000],
        payload={"asset_id": asset.id, "kind": asset.kind, "status": status, "error_category": category, "ai_task_id": asset.ai_task_id},
    )
    stats_service.increment_realtime_after_commit(db, asset.project_id, {"media_failed": 1})


def fail_media_assets(db: Session, root: AiTask, *, status: str, category: str, message: str) -> int:
    """资产置 ``failed`` / ``expired``（写 ``error_*`` / ``failed_at``）+ ``media_task_failed`` 告警 + ``stats:rt.media_failed``。"""
    now = utcnow()
    count = 0
    for asset in _task_assets(db, root):
        if asset.status in MEDIA_TERMINAL_STATUSES:
            continue
        asset.status = status
        asset.error_category = category
        asset.error_message = message[:500]
        asset.failed_at = now
        asset.next_transfer_at = None
        _media_alert(db, asset, status=status, category=category, message=message)
        count += 1
    db.flush()
    return count


# =====================================================================
# ② polling 超过 deadline_at
# =====================================================================


def expire_polling(db: Session, now: datetime) -> dict[str, int]:
    ids = db.scalars(
        select(AiTask.id)
        .where(AiTask.root_task_id.is_(None), AiTask.status == "polling", AiTask.deadline_at.is_not(None), AiTask.deadline_at < now)
        .order_by(AiTask.id)
        .limit(SCAN_LIMIT)
    ).all()
    db.rollback()
    expired = 0
    for task_id in ids:
        try:
            root = db.get(AiTask, task_id)
            if root is None or root.status != "polling" or root.deadline_at is None or root.deadline_at >= now:
                continue
            hook = media_hook("on_task_expired")
            if hook is not None:
                hook(db, root)
            else:
                fail_media_assets(db, root, status="expired", category="timeout", message=EXPIRED_MESSAGE)
            # docs/08 §9.2 timeout 行：轮询超出预算计入熔断（与 media_service 轮询判定的过期一致）
            gateway.record_breaker_failure(db, root.capability, root.model, ZhiqiError(ErrorCategory.TIMEOUT, EXPIRED_MESSAGE))
            gateway.finalize_root(db, root, "expired", error_category="timeout", error_message=EXPIRED_MESSAGE)
            db.commit()
            ai_task_service.notify_batch_finished(root.batch_id)
            expired += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("过期处理失败 task_id=%s", task_id)
    return {"expired": expired}


# =====================================================================
# ③ queued 补入队
# =====================================================================


def requeue_queued(db: Session, now: datetime) -> dict[str, int]:
    if gateway.check_paused():
        return {"requeued": 0}
    cutoff = now - timedelta(minutes=QUEUED_STALE_MINUTES)
    ids = db.scalars(
        select(AiTask.id)
        .where(AiTask.root_task_id.is_(None), AiTask.status == "queued", AiTask.updated_at < cutoff)
        .order_by(AiTask.id)
        .limit(SCAN_LIMIT)
    ).all()
    requeued = 0
    for task_id in ids:
        if redis_client.lpos(gateway.QUEUE_AI_TASKS, str(task_id)) is None:
            redis_client.rpush(gateway.QUEUE_AI_TASKS, task_id)
            requeued += 1
    if requeued:
        logger.info("补入队 %s 个排队超过 %s 分钟且不在队列中的根任务", requeued, QUEUED_STALE_MINUTES)
    return {"requeued": requeued}


# =====================================================================
# ④ 批次兜底收敛
# =====================================================================


def converge_batches(db: Session, now: datetime) -> dict[str, int]:
    batch_ids = db.scalars(select(GenerationBatch.id).where(GenerationBatch.status == "running").order_by(GenerationBatch.id)).all()
    db.rollback()
    converged = idle = 0
    for batch_id in batch_ids:
        active = db.scalar(
            select(func.count(AiTask.id)).where(
                AiTask.batch_id == batch_id, AiTask.root_task_id.is_(None), AiTask.status.in_(ACTIVE_ROOT_STATUSES)
            )
        )
        if active:
            db.rollback()
            continue
        token = acquire_lock(f"{BATCH_LOCK_PREFIX}{batch_id}", BATCH_LOCK_TTL)
        if not token:
            continue
        try:
            outcome = _converge_batch_locked(db, batch_id, now)
            db.commit()
            if outcome == "converged":
                converged += 1
            elif outcome == "idle":
                idle += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("批次兜底收敛失败 batch_id=%s", batch_id)
        finally:
            release_lock(f"{BATCH_LOCK_PREFIX}{batch_id}", token)
    return {"batches_converged": converged, "batches_idle_partial": idle}


def _converge_batch_locked(db: Session, batch_id: int, now: datetime) -> str | None:
    batch = db.get(GenerationBatch, batch_id)
    if batch is None or batch.status != "running":
        return None
    roots = list(db.scalars(select(AiTask).where(AiTask.batch_id == batch_id, AiTask.root_task_id.is_(None))).all())
    if any(r.status in ACTIVE_ROOT_STATUSES for r in roots):
        return None
    if roots:
        hook = batch_hook()
        if hook is not None:
            hook(db, batch_id)
            return "converged"
        recompute_batch_fallback(db, batch, roots, now)
        return "converged"
    last = batch.heartbeat_at or batch.started_at or batch.updated_at
    if last is not None and last < now - timedelta(minutes=BATCH_IDLE_MINUTES):
        batch.status = "partial"
        batch.finished_at = now
        return "idle"
    return None


def _apply_counts(root: AiTask) -> dict[str, int]:
    meta = gateway.task_meta(root)
    counts = meta.get("apply_counts")
    if not isinstance(counts, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in counts.items():
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def recompute_batch_fallback(db: Session, batch: GenerationBatch, roots: list[AiTask], now: datetime) -> str:
    """按库重算并收敛（docs/09 §9.3、docs/03 B.9；与 ``on_task_finished`` 同口径）：

    - ``task_done`` = ``succeeded`` 根任务数；``task_failed`` = ``failed`` / ``cancelled`` / ``expired`` 且不存在重试子任务的根任务数；
    - ``produced_count``：关键词 / 标题批次统计 ``ai_task_id`` 属于 ``succeeded`` 根任务尝试行的 ``keywords`` / ``titles`` 行数；
      内容批次为 ``operation=content_generate`` 的 ``succeeded`` 根任务数；
    - ``error_summary``：各根任务 ``response_meta_json.apply_counts`` 之和 + 失败根任务按 ``error_category`` 计数（排除已有重试子任务的；
      带 ``stale_after_submit`` 标记的计为 ``stale_after_submit``），``;`` 连接、为 0 省略、全 0 为 NULL；
    - 状态：``task_failed = 0`` → ``succeeded``；``task_done = 0`` → ``failed``；否则 ``partial``；写 ``finished_at``。
    """
    root_ids = [r.id for r in roots]
    with_child = set(
        db.scalars(select(AiTask.parent_task_id).where(AiTask.parent_task_id.in_(root_ids), AiTask.root_task_id.is_(None))).all()
    ) if root_ids else set()
    succeeded = [r for r in roots if r.status == "succeeded"]
    failed = [r for r in roots if r.status in FAILED_ROOT_STATUSES and r.id not in with_child]

    if batch.kind in ("keyword", "title") and succeeded:
        model = Keyword if batch.kind == "keyword" else Title
        attempt_ids = select(AiTask.id).where(AiTask.root_task_id.in_([r.id for r in succeeded]))
        produced = int(db.scalar(select(func.count(model.id)).where(model.ai_task_id.in_(attempt_ids))) or 0)
    elif batch.kind == "content":
        produced = sum(1 for r in succeeded if r.operation == "content_generate")
    else:
        produced = 0

    items: Counter[str] = Counter()
    for root in roots:
        for key, value in _apply_counts(root).items():
            items[key] += value
    failures: Counter[str] = Counter()
    for root in failed:
        if gateway.task_meta(root).get("stale_after_submit"):
            failures["stale_after_submit"] += 1
        else:
            failures[root.error_category or ("cancelled" if root.status == "cancelled" else "unknown")] += 1
    ordered = [k for k in APPLY_COUNT_KEYS if items.get(k)] + sorted(k for k in items if k not in APPLY_COUNT_KEYS and items[k])
    parts = [f"{k}={items[k]}" for k in ordered] + [f"{k}={v}" for k, v in sorted(failures.items()) if v]
    summary = ";".join(parts)[:ERROR_SUMMARY_MAX] or None

    batch.task_done = len(succeeded)
    batch.task_failed = len(failed)
    batch.produced_count = produced
    batch.error_summary = summary
    batch.heartbeat_at = now
    if batch.task_failed == 0:
        batch.status = "succeeded"
    elif batch.task_done == 0:
        batch.status = "failed"
    else:
        batch.status = "partial"
    batch.finished_at = now
    db.flush()
    return batch.status


# =====================================================================
# ⑤ worker_stale（monitor_worker 心跳交叉检查）
# =====================================================================


def _parse_at(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC).replace(tzinfo=None)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def read_heartbeats(name: str) -> list[dict[str, Any]]:
    """``SCAN worker:heartbeat:{name}:*`` → ``[{target_key, hostname, pid, at}]``（``at`` 为 naive UTC；无法解析视为 1970-01-01）。"""
    prefix = f"{HEARTBEAT_PREFIX}{name}:"
    keys = sorted(redis_client.scan_iter(match=f"{prefix}*", count=200))
    if not keys:
        return []
    values = redis_client.mget(keys)
    replicas: list[dict[str, Any]] = []
    for key, raw in zip(keys, values, strict=False):
        hostname, _, pid = key[len(prefix):].rpartition(":")
        data: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw)
                data = loaded if isinstance(loaded, dict) else {"at": loaded}
            except (TypeError, ValueError):
                data = {}
        hostname = str(data.get("hostname") or hostname)
        pid = str(data.get("pid") or pid)
        replicas.append(
            {"target_key": f"{name}:{hostname}:{pid}", "hostname": hostname, "pid": pid, "at": _parse_at(data.get("at")) or datetime(1970, 1, 1)}
        )
    return replicas


def check_worker_stale(db: Session, name: str, now: datetime | None = None) -> dict[str, int]:
    """``worker_stale`` 交叉检查（``recover_stale_tasks`` ⑤ 查 ``monitor_worker``；``evaluate_alerts`` ④ 可复用查 ``worker``）：
    副本 ``at`` 落后超过 ``alert_config.rules.worker_stale.minutes``（5）→ 副本级（``target_key={name}:{hostname}:{pid}``）；
    无存活副本（任一副本 ``now − at < 90s`` 即存活；无键视为 1970-01-01）→ 进程级（``target_key={name}``）；
    副本 ``at`` 恢复、副本键过期或任一副本存活时自动解决。"""
    now = now or utcnow()
    rule = (settings_service.get_config(db, "alert_config").get("rules") or {}).get("worker_stale") or {}
    minutes = max(1, int(rule.get("minutes") or 5))
    replicas = read_heartbeats(name)
    raised = resolved = 0
    current: set[str] = set()
    for replica in replicas:
        target_key = replica["target_key"]
        current.add(target_key)
        lag = now - replica["at"]
        if lag > timedelta(minutes=minutes):
            lag_minutes = int(lag.total_seconds() // 60)
            alert_service.raise_alert(
                db, SYSTEM_SCOPE, "worker_stale", target_type="worker", target_key=target_key,
                title=f"worker 心跳超时：{target_key}",
                message=f"进程 {name} 副本 {replica['hostname']}:{replica['pid']} 心跳已 {lag_minutes} 分钟未更新（阈值 {minutes} 分钟）",
                payload={"name": name, "hostname": replica["hostname"], "pid": replica["pid"], "heartbeat_at": replica["at"].isoformat() + "Z"},
            )
            raised += 1
        else:
            resolved += alert_service.resolve_alert(db, SYSTEM_SCOPE, "worker_stale", "worker", target_key)
    expired_conditions = [Alert.alert_type == "worker_stale", Alert.target_type == "worker", Alert.target_key.like(f"{name}:%")]
    if current:
        expired_conditions.append(Alert.target_key.not_in(sorted(current)))
    resolved += alert_service.auto_resolve_where(db, SYSTEM_SCOPE, *expired_conditions)
    alive = any(now - r["at"] < timedelta(seconds=HEARTBEAT_ALIVE_SECONDS) for r in replicas)
    if alive:
        resolved += alert_service.resolve_alert(db, SYSTEM_SCOPE, "worker_stale", "worker", name)
    else:
        latest = max((r["at"] for r in replicas), default=None)
        alert_service.raise_alert(
            db, SYSTEM_SCOPE, "worker_stale", target_type="worker", target_key=name,
            title=f"worker 进程无存活副本：{name}",
            message=(
                f"进程 {name} 没有存活副本（最近心跳 {latest.isoformat() + 'Z' if latest and latest.year > 1970 else '无'}），"
                "请检查进程是否运行"
            ),
            payload={"name": name, "replicas": len(replicas)},
        )
        raised += 1
    db.commit()
    return {"worker_stale_raised": raised, "worker_stale_resolved": resolved}


# =====================================================================
# ⑥ 僵死转存
# =====================================================================


def recover_transfers(db: Session, now: datetime) -> dict[str, int]:
    cutoff = now - timedelta(minutes=TRANSFER_STALE_MINUTES)
    ids = db.scalars(
        select(MediaAsset.id)
        .where(MediaAsset.status == "downloading", MediaAsset.updated_at < cutoff)
        .order_by(MediaAsset.id)
        .limit(SCAN_LIMIT)
    ).all()
    db.rollback()
    rescheduled = failed = 0
    for asset_id in ids:
        if is_locked(f"{TRANSFER_LOCK_PREFIX}{asset_id}"):
            continue
        try:
            asset = db.get(MediaAsset, asset_id)
            if asset is None or asset.status != "downloading" or asset.updated_at is None or asset.updated_at >= cutoff:
                continue
            hook = media_hook("record_transfer_failure")
            if hook is not None:
                hook(db, asset, TRANSFER_STALE_MESSAGE)
            else:
                record_transfer_failure_fallback(db, asset, TRANSFER_STALE_MESSAGE, now)
            db.commit()
            if asset.status == "failed":
                failed += 1
            else:
                rescheduled += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("僵死转存处理失败 asset_id=%s", asset_id)
    return {"transfers_rescheduled": rescheduled, "transfers_failed": failed}


def record_transfer_failure_fallback(db: Session, asset: MediaAsset, message: str, now: datetime) -> None:
    """一次转存失败（docs/03「媒体转存与素材引用」第 2 条）：``transfer_attempts += 1``；未达 ``transfer.max_attempts`` 时保持
    ``downloading``、``next_transfer_at = now + retry_seconds[transfer_attempts-1]``；达到上限 → ``failed(transfer_failed)`` +
    ``media_task_failed`` 告警。"""
    transfer = (settings_service.get_config(db, "media_config").get("transfer") or {})
    max_attempts = max(1, int(transfer.get("max_attempts") or 3))
    retry_seconds = [int(s) for s in (transfer.get("retry_seconds") or [30, 120, 600])] or [30]
    asset.transfer_attempts = int(asset.transfer_attempts or 0) + 1
    asset.error_message = message[:500]
    if asset.transfer_attempts < max_attempts:
        delay = retry_seconds[min(asset.transfer_attempts - 1, len(retry_seconds) - 1)]
        asset.next_transfer_at = now + timedelta(seconds=delay)
        asset.updated_at = now
        return
    asset.status = "failed"
    asset.error_category = "transfer_failed"
    asset.failed_at = now
    asset.next_transfer_at = None
    _media_alert(db, asset, status="failed", category="transfer_failed", message=message)
