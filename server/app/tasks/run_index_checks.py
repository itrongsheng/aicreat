"""消费 ``queue:index_checks``（docs/11 §9；docs/01 §5.2 第 4 行、§6.1）。

``drain(pool, limit=10, sems=None) -> int`` 在 ``app.monitor_worker`` 主循环线程执行：线程池已满或 ``sems["index_check"]``
（``index_check.concurrency``）非阻塞获取失败时不 ``LPOP``；``LPOP`` 到的元素连同已获取的信号量提交线程池 ``process_one``。

``process_one(payload) -> bool``：

1. ``SET lock:monitor:index_check:{link_id} NX EX 900`` 失败 → **不重入队**：``DEL`` 本 payload 涉及的
   ``queued:index_check:{link_id}:{kind}``、``next_index_check_at = now + 300s``（下轮调度重新入队）、记 INFO；
2. ``index_check_service.run(db, SYSTEM_SCOPE, link, kinds, engines, check_type, triggered_by)``：链接不存在、已删除或
   （非手动且）暂停监控 → 直接结束；``ai:paused:*`` → 整条链接延后；逐引擎独立事务写回，每引擎完成后续期锁；
3. ``finally``：``DEL queued:index_check:{link_id}:{kind}``、释放锁与信号量。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

import redis
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.models import PublishLink, utcnow
from app.services import index_check_service
from app.services.data_scope_service import SYSTEM_SCOPE

logger = logging.getLogger(__name__)

QUEUE_KEY = index_check_service.QUEUE_INDEX_CHECKS
LOCK_PREFIX = index_check_service.LOCK_PREFIX
LOCK_TTL_SECONDS = index_check_service.LOCK_TTL_SECONDS
DEFAULT_LIMIT = 10
CHECK_TYPES = frozenset({"scheduled", "manual"})
KINDS = ("seo", "geo")


def lock_key(link_id: int) -> str:
    return f"{LOCK_PREFIX}{link_id}"


def parse_payload(raw: Any) -> dict[str, Any] | None:
    """``{"link_id": 1, "kinds": ["seo","geo"], "engines": ["baidu","doubao"], "check_type": "scheduled", "triggered_by": null}``；
    非法元素返回 ``None``（丢弃）。"""
    if isinstance(raw, Mapping):
        data = dict(raw)
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
    try:
        link_id = int(data.get("link_id"))
    except (TypeError, ValueError):
        return None
    if link_id <= 0:
        return None
    kinds = [k for k in KINDS if k in (data.get("kinds") or [])]
    engines = [str(e) for e in data.get("engines") or [] if isinstance(e, str) and e]
    if not kinds or not engines:
        return None
    check_type = data.get("check_type") if data.get("check_type") in CHECK_TYPES else "scheduled"
    triggered_by = data.get("triggered_by")
    try:
        triggered_by = int(triggered_by) if triggered_by is not None else None
    except (TypeError, ValueError):
        triggered_by = None
    return {"link_id": link_id, "kinds": kinds, "engines": list(dict.fromkeys(engines)), "check_type": check_type,
            "triggered_by": triggered_by}


def _defer_busy(link_id: int) -> None:
    """锁被占用：``next_index_check_at = now + 300s``（链接仍在监控且未删除时）。"""
    try:
        with SessionLocal() as db:
            link = db.scalars(select(PublishLink).where(PublishLink.id == link_id).with_for_update()).first()
            if link is not None and link.is_monitoring and link.alive_status != "deleted":
                link.next_index_check_at = utcnow() + index_check_service.LOCK_BUSY_DEFER
            db.commit()
    except Exception:  # noqa: BLE001 - 排程权威在 MySQL，下轮扫描兜底
        logger.exception("锁竞争后延后 next_index_check_at 失败 link_id=%s", link_id)


def process_one(payload: Any, *, semaphore: threading.Semaphore | None = None) -> bool:
    """执行一个收录检测元素；``semaphore`` 为调用方已获取、需在结束时释放的并发信号量。返回是否执行了检测。"""
    try:
        data = parse_payload(payload)
        if data is None:
            logger.warning("丢弃非法的 queue:index_checks 元素：%r", payload)
            return False
        link_id = data["link_id"]
        kinds = data["kinds"]
        try:
            token = acquire_lock(lock_key(link_id), LOCK_TTL_SECONDS)
        except redis.RedisError as exc:
            logger.warning("获取收录检测锁失败（Redis 不可用）link_id=%s: %s", link_id, exc)
            return False
        if not token:
            logger.info("链接 %s 正在收录检测中（锁被占用），本次 %s 元素不重入队，next_index_check_at 延后 300s",
                        link_id, data["check_type"])
            index_check_service.release_markers(link_id, kinds)
            _defer_busy(link_id)
            return False
        try:
            with SessionLocal() as db:
                link = db.get(PublishLink, link_id)
                if link is None:
                    logger.info("链接 %s 不存在，丢弃收录检测元素", link_id)
                    return False
                records = index_check_service.run(
                    db, SYSTEM_SCOPE, link, kinds, data["engines"], data["check_type"], data["triggered_by"],
                    lock_key=lock_key(link_id), lock_token=token,
                )
                return bool(records)
        except Exception:  # noqa: BLE001 - 线程池任务不向外抛出
            logger.exception("收录检测执行异常 link_id=%s", link_id)
            return False
        finally:
            index_check_service.release_markers(link_id, kinds)
            try:
                release_lock(lock_key(link_id), token)
            except redis.RedisError:
                pass
    finally:
        if semaphore is not None:
            semaphore.release()


def _pool_full(pool: Any) -> bool:
    checker = getattr(pool, "is_full", None)
    return bool(checker()) if callable(checker) else False


def drain(pool: Any, limit: int = DEFAULT_LIMIT, sems: Mapping[str, threading.Semaphore] | None = None) -> int:
    """``LPOP queue:index_checks`` 并提交线程池，返回本轮提交数。"""
    semaphore = (sems or {}).get("index_check")
    submitted = 0
    while submitted < limit:
        if _pool_full(pool):
            break
        if semaphore is not None and not semaphore.acquire(blocking=False):
            break
        try:
            raw = redis_client.lpop(QUEUE_KEY)
        except redis.RedisError as exc:
            if semaphore is not None:
                semaphore.release()
            logger.warning("LPOP queue:index_checks 失败: %s", exc)
            break
        if raw is None:
            if semaphore is not None:
                semaphore.release()
            break
        try:
            pool.submit(process_one, raw, semaphore=semaphore)
        except Exception:
            if semaphore is not None:
                semaphore.release()
            data = parse_payload(raw)
            try:
                redis_client.lpush(QUEUE_KEY, raw)      # 提交失败（线程池已关闭）：放回队头
            except redis.RedisError:
                if data is not None:
                    index_check_service.release_markers(data["link_id"], data["kinds"])
            raise
        submitted += 1
    return submitted


__all__ = ["DEFAULT_LIMIT", "QUEUE_KEY", "drain", "lock_key", "parse_payload", "process_one"]
