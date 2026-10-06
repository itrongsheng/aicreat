"""消费 ``queue:link_checks``（docs/11 §6.7；docs/01 §5.2 第 2 行、§6.1）。

``drain(pool, limit=20, sems=None) -> int`` 在 ``app.monitor_worker`` 主循环线程执行：线程池已满或 ``sems["link_check"]``
（``global_concurrency``）非阻塞获取失败时不 ``LPOP``；``LPOP`` 到的元素连同已获取的信号量提交线程池 ``process_one``。

``process_one(payload) -> bool``：

1. ``SET lock:monitor:link_check:{link_id} NX EX 300`` 失败 → 丢弃该元素、**不**删除 ``queued:link_check`` 标记（由持锁者完成时删除）、记 INFO；
2. 读取链接；不存在或 ``is_monitoring=0``（``manual`` 除外）→ 删除标记后结束；
3. ``link_check_service.check_link(db, SYSTEM_SCOPE, link, check_type, triggered_by)``（同域名间隔、计数、抓取、判定、写回）；
4. 任何异常：以 ``unknown`` / ``network_error`` / 脱敏 ``error_message`` 落一条记录（``record_failure``）；
5. ``finally``：``DEL queued:link_check:{link_id}``、释放锁与信号量。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

import redis

from app.core.database import SessionLocal
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.models import PublishLink
from app.services import link_check_service, link_service
from app.services.data_scope_service import SYSTEM_SCOPE

logger = logging.getLogger(__name__)

QUEUE_KEY = link_service.QUEUE_LINK_CHECKS
LOCK_PREFIX = "lock:monitor:link_check:"
LOCK_TTL_SECONDS = 300
DEFAULT_LIMIT = 20
CHECK_TYPES = frozenset({"baseline", "scheduled", "manual", "retry"})


def lock_key(link_id: int) -> str:
    return f"{LOCK_PREFIX}{link_id}"


def parse_payload(raw: Any) -> dict[str, Any] | None:
    """``{"link_id": 1, "check_type": "scheduled", "triggered_by": null}``；非法元素返回 ``None``（丢弃）。"""
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
    check_type = data.get("check_type") if data.get("check_type") in CHECK_TYPES else "scheduled"
    triggered_by = data.get("triggered_by")
    try:
        triggered_by = int(triggered_by) if triggered_by is not None else None
    except (TypeError, ValueError):
        triggered_by = None
    return {"link_id": link_id, "check_type": check_type, "triggered_by": triggered_by}


def _clear_marker(link_id: int) -> None:
    try:
        redis_client.delete(link_service.queued_key(link_id))
    except redis.RedisError as exc:  # 标记 1h 自过期
        logger.warning("删除 queued:link_check 标记失败 link_id=%s: %s", link_id, exc)


def process_one(payload: Any, *, semaphore: threading.Semaphore | None = None) -> bool:
    """执行一次删除检测；``semaphore`` 为调用方已获取、需在结束时释放的并发信号量。返回是否完成检测写回。"""
    try:
        data = parse_payload(payload)
        if data is None:
            logger.warning("丢弃非法的 queue:link_checks 元素：%r", payload)
            return False
        link_id = data["link_id"]
        check_type = data["check_type"]
        triggered_by = data["triggered_by"]
        try:
            token = acquire_lock(lock_key(link_id), LOCK_TTL_SECONDS)
        except redis.RedisError as exc:
            logger.warning("获取删除检测锁失败（Redis 不可用）link_id=%s: %s", link_id, exc)
            return False
        if not token:
            logger.info("链接 %s 正在检测中（锁被占用），丢弃本次 %s 检测元素", link_id, check_type)
            return False
        try:
            with SessionLocal() as db:
                link = db.get(PublishLink, link_id)
                if link is None:
                    logger.info("链接 %s 不存在，丢弃检测元素", link_id)
                    return False
                if not link.is_monitoring and check_type != "manual":
                    logger.info("链接 %s 已暂停监控，丢弃 %s 检测元素", link_id, check_type)
                    return False
                try:
                    link_check_service.check_link(db, SYSTEM_SCOPE, link, check_type, triggered_by)
                    return True
                except Exception as exc:  # noqa: BLE001 - 检测流程异常同样落一条 unknown 记录
                    logger.exception("删除检测执行异常 link_id=%s", link_id)
                    try:
                        link_check_service.record_failure(db, SYSTEM_SCOPE, link_id, check_type, triggered_by, exc)
                    except Exception:  # noqa: BLE001
                        db.rollback()
                        logger.exception("删除检测异常记录写入失败 link_id=%s", link_id)
                    return False
        finally:
            _clear_marker(link_id)
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
    """``LPOP queue:link_checks`` 并提交线程池，返回本轮提交数。"""
    semaphore = (sems or {}).get("link_check")
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
            logger.warning("LPOP queue:link_checks 失败: %s", exc)
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
                    _clear_marker(data["link_id"])
            raise
        submitted += 1
    return submitted
