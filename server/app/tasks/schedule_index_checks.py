"""收录检测到期扫描（docs/11 §7.5、§9；docs/01 §5.2 第 3 行；docs/03 B.20）。

``enqueue_due(limit=100) -> int`` 每 ``monitoring_config.index_check.scan_interval_seconds``（300s）在 ``app.monitor_worker`` 主循环
线程执行（短 SQL + Redis 读写，无网络 I/O），持单例锁 ``lock:monitor:schedule:index_checks``（300s）：

1. ``index_check.enabled=false`` → 不扫描（``next_index_check_at`` 保留计算值，恢复后按到期顺序补检）；
2. ``SELECT … WHERE is_monitoring=1 AND next_index_check_at <= now AND alive_status NOT IN ('deleted')
   ORDER BY next_index_check_at LIMIT 100``；
3. 逐链接按引擎计算 ``due(e)``，只把已到期的引擎写入 payload ``engines``（``kinds`` 由引擎推导）；没有到期引擎（如配置变更）
   → 按 ``compute_next_index_check_at`` 重算；
4. ``index_check_service.enqueue``：``INCRBY limit:index_checks:{date} len(engines)`` 预扣（超 ``daily_limit`` → 不入队，
   ``next_index_check_at`` = 次日 00:00（``stats_config.timezone``））→ ``SET NX queued:index_check:{id}:{kind}`` →
   ``RPUSH queue:index_checks`` → ``next_index_check_at = now + 1h``；已在队列 → 同样推后 1h（标记 1h 自过期）；最后统一提交。
"""

from __future__ import annotations

import logging

import redis
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.locks import acquire_lock, release_lock
from app.models import PublishLink, utcnow
from app.services import index_check_service, link_service

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:monitor:schedule:index_checks"
LOCK_TTL_SECONDS = 300
DEFAULT_LIMIT = 100


def enqueue_due(limit: int = DEFAULT_LIMIT) -> int:
    """扫描到期链接并入队，返回本轮入队数。锁被占用或功能关闭时返回 0。"""
    try:
        token = acquire_lock(LOCK_KEY, LOCK_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("schedule_index_checks 获取锁失败（Redis 不可用）: %s", exc)
        return 0
    if not token:
        return 0
    try:
        with SessionLocal() as db:
            cfg = index_check_service.index_check_config(db)
            if not cfg.get("enabled", True):
                return 0
            now = utcnow()
            engines = index_check_service.schedulable_engines(db)
            rows = list(db.scalars(
                select(PublishLink)
                .where(
                    PublishLink.is_monitoring == True,  # noqa: E712
                    PublishLink.next_index_check_at <= now,
                    PublishLink.alive_status != "deleted",
                )
                .order_by(PublishLink.next_index_check_at, PublishLink.id)
                .limit(max(1, int(limit)))
            ).all())
            enqueued = limited = 0
            tomorrow = index_check_service.next_day_start(db)
            try:
                for link in rows:
                    pairs = index_check_service.due_pairs(link, engines, now=now, cfg=cfg)
                    if not pairs:
                        link.next_index_check_at = link_service.compute_next_index_check_at(link, now=now, cfg=cfg, engines=engines)
                        continue
                    queued, reason = index_check_service.enqueue(db, link, pairs, "scheduled", None, cfg=cfg)
                    if queued:
                        enqueued += 1
                    elif reason == "daily_limit":
                        link.next_index_check_at = tomorrow
                        limited += 1
                    else:
                        link.next_index_check_at = now + index_check_service.ENQUEUE_DEFER
            finally:
                db.commit()
            if enqueued or limited:
                logger.info("收录检测入队 %s 条（到期 %s 条，超日上限顺延 %s 条）", enqueued, len(rows), limited)
            return enqueued
    finally:
        release_lock(LOCK_KEY, token)


__all__ = ["DEFAULT_LIMIT", "LOCK_KEY", "enqueue_due"]
