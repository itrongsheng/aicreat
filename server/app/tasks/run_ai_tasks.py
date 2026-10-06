"""消费 ``queue:ai_tasks``（docs/01 §5.1 第 1 行、§5.3；docs/09 §9.4；docs/03「任务幂等与状态收敛」第 2 条）。

``drain(pool, sems, limit=10) -> int`` 在 ``app.worker`` 主循环线程执行（只做 ``LPOP`` + 短 SQL + 非阻塞取信号量 + ``claim`` +
提交线程池，不做网络 I/O）：

1. ``ai:paused:*`` 存在或线程池已满（``in_flight >= max_workers``）时不 ``LPOP``；
2. ``LPOP queue:ai_tasks`` → 读根任务 ``capability``，按 ``MODALITY_OF`` 取模态（文本能力 → ``text``）→
   ``sems[text / image / video]`` 非阻塞获取，失败则 ``LPUSH`` 回队头并结束本轮；
3. ``ai_task_service.claim``（``lock:ai_task:{id}`` + ``UPDATE … WHERE status='queued'``），rowcount=0 则释放信号量跳过；
4. ``pool.submit(process_one)``；信号量在执行线程 ``finally`` 释放。

队列元素只是触发器：``ai_tasks.status='queued'`` 为权威，非 ``queued`` / 非根任务 / 不存在的元素直接丢弃，丢失的元素由
``recover_stale_tasks`` ③ 按库补扫。进程停止（SIGTERM）时尚未开始执行的已领取任务由 ``release_unstarted`` 退回 ``queued``。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

import redis
from sqlalchemy import select, update

from app.core.database import SessionLocal
from app.core.locks import release_lock
from app.core.redis import redis_client
from app.core.zhiqi.types import MODALITY_OF, Capability
from app.models import AiTask
from app.services import ai_gateway_service as gateway
from app.services import ai_task_service

logger = logging.getLogger(__name__)

QUEUE_KEY = gateway.QUEUE_AI_TASKS
SCAN_FACTOR = 5          # 单轮最多查看 limit × 5 个元素（丢弃的无效元素不计入 limit）

_stop = threading.Event()
_unstarted: set[int] = set()
_unstarted_lock = threading.Lock()


def request_stop() -> None:
    """进程收到 SIGTERM / SIGINT：尚未开始执行的已领取根任务在 ``process_one`` 入口退回 ``queued``。"""
    _stop.set()


def reset_stop() -> None:
    """测试与进程内重启用：清除停止标记。"""
    _stop.clear()


def modality_of(capability: str | None) -> str:
    try:
        return MODALITY_OF[Capability(capability)]
    except (ValueError, KeyError):
        return "text"


def _pool_full(pool: Any) -> bool:
    checker = getattr(pool, "is_full", None)
    if callable(checker):
        return bool(checker())
    return False


def _parse_id(raw: Any) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _push_front(task_id: int) -> None:
    try:
        redis_client.lpush(QUEUE_KEY, task_id)
    except redis.RedisError as exc:  # 丢失由 recover_stale_tasks ③ 补扫
        logger.warning("LPUSH 回队头失败 task_id=%s: %s", task_id, exc)


def drain(pool: Any, sems: Mapping[str, threading.BoundedSemaphore], limit: int = 10, *, worker_id: str | None = None) -> int:
    """从 ``queue:ai_tasks`` 领取至多 ``limit`` 个根任务并提交线程池，返回提交数。"""
    worker_id = worker_id or gateway.default_worker_id()
    if _stop.is_set() or gateway.check_paused():
        return 0
    submitted = 0
    scanned = 0
    with SessionLocal() as db:
        while submitted < limit and scanned < limit * SCAN_FACTOR:
            if _pool_full(pool):
                break
            raw = redis_client.lpop(QUEUE_KEY)
            if raw is None:
                break
            scanned += 1
            task_id = _parse_id(raw)
            if task_id is None:
                logger.warning("queue:ai_tasks 中的非法元素已丢弃：%r", raw)
                continue
            row = db.execute(select(AiTask.capability, AiTask.status, AiTask.root_task_id).where(AiTask.id == task_id)).first()
            db.rollback()  # 结束只读事务，claim 读到最新状态
            if row is None or row.root_task_id is not None or row.status != "queued":
                logger.info("丢弃队列元素 task_id=%s（不存在 / 非根任务 / 状态 %s）", task_id, getattr(row, "status", None))
                continue
            sem = sems.get(modality_of(row.capability)) or sems.get("text")
            if sem is None or not sem.acquire(blocking=False):
                _push_front(task_id)
                break
            try:
                claimed = ai_task_service.claim(db, task_id, worker_id)
            except Exception:  # noqa: BLE001 - 领取异常：放回队头，下一轮再试
                sem.release()
                _push_front(task_id)
                logger.exception("领取根任务失败 task_id=%s", task_id)
                break
            if not claimed:
                sem.release()
                continue
            with _unstarted_lock:
                _unstarted.add(task_id)
            try:
                pool.submit(_execute, task_id, sem, worker_id)
            except RuntimeError:  # 线程池已关闭（进程退出中）
                sem.release()
                with _unstarted_lock:
                    _unstarted.discard(task_id)
                release_claim(task_id, worker_id)
                break
            submitted += 1
    return submitted


def _execute(task_id: int, sem: threading.BoundedSemaphore, worker_id: str) -> bool:
    try:
        return process_one(task_id, worker_id=worker_id)
    finally:
        sem.release()


def process_one(task_id: int, *, worker_id: str | None = None) -> bool:
    """执行一个已领取的根任务（线程池线程内）：委托 ``ai_task_service.process_one``；进程停止中且尚未开始的任务退回 ``queued``。"""
    worker_id = worker_id or gateway.default_worker_id()
    with _unstarted_lock:
        _unstarted.discard(task_id)
    if _stop.is_set():
        release_claim(task_id, worker_id)
        return False
    try:
        return ai_task_service.process_one(task_id, worker_id=worker_id)
    except Exception:  # noqa: BLE001 - 不让单个任务的异常冒到线程池（由 recover_stale_tasks 兜底）
        logger.exception("执行根任务异常 task_id=%s", task_id)
        return False


def release_claim(task_id: int, worker_id: str) -> bool:
    """把本执行者已领取但未开始执行的根任务退回 ``queued``（清空 ``locked_by`` / ``started_at`` / ``heartbeat_at``），
    释放 ``lock:ai_task:{id}`` 并 ``LPUSH`` 回队头。"""
    with SessionLocal() as db:
        result = db.execute(
            update(AiTask)
            .where(AiTask.id == task_id, AiTask.status == "running", AiTask.locked_by == worker_id)
            .values(status="queued", locked_by=None, started_at=None, heartbeat_at=None)
        )
        db.commit()
        released = bool(result.rowcount)
    try:
        release_lock(f"{ai_task_service.TASK_LOCK_PREFIX}{task_id}", worker_id)
    except redis.RedisError:
        logger.warning("释放 lock:ai_task:%s 失败", task_id, exc_info=True)
    if released:
        _push_front(task_id)
    return released


def release_unstarted(worker_id: str | None = None) -> int:
    """进程退出时调用（线程池关闭后）：已领取但从未开始执行的根任务退回 ``queued``，返回条数。"""
    worker_id = worker_id or gateway.default_worker_id()
    with _unstarted_lock:
        pending = sorted(_unstarted)
        _unstarted.clear()
    return sum(1 for task_id in pending if release_claim(task_id, worker_id))


def unstarted_count() -> int:
    with _unstarted_lock:
        return len(_unstarted)
