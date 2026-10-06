"""删除检测到期扫描（docs/11 §6.6、§6.7；docs/01 §5.2 第 1 行；docs/03 B.20）。

``enqueue_due(limit=200) -> int`` 每 ``monitoring_config.link_check.scan_interval_seconds``（60s）在 ``app.monitor_worker`` 主循环线程
执行（短 SQL + Redis 读写，无网络 I/O），持单例锁 ``lock:monitor:schedule:link_checks``（60s）：

1. ``link_check.enabled=false`` → 不扫描（到期链接积压，恢复后按 ``next_check_at`` 顺序逐批补检，不追补历史轮次）；
2. ``GET limit:link_checks:{date}``：已达 ``daily_limit`` 时本轮不入队、``next_check_at`` 不变（次日计数键切换后自然恢复）；
   未达时本轮入队数不超过剩余额度（``daily_limit − 已用``）；
3. ``SELECT … WHERE is_monitoring=1 AND next_check_at <= now ORDER BY next_check_at LIMIT …``，逐条选择 ``check_type``：
   ``check_count=0`` → ``baseline``（回填后基线入队失败的补检，享受基线 404 宽限）；计数器 > 0 → ``retry``；其余 ``scheduled``；
4. ``link_service.enqueue_check``（``SET NX queued:link_check:{id}`` → ``RPUSH`` → ``next_check_at = now + 1h``），最后统一提交。
"""

from __future__ import annotations

import logging

import redis
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.locks import acquire_lock, release_lock
from app.models import PublishLink, utcnow
from app.services import link_check_service, link_service

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:monitor:schedule:link_checks"
LOCK_TTL_SECONDS = 60
DEFAULT_LIMIT = 200


def check_type_for(link: PublishLink) -> str:
    """``check_count=0`` → ``baseline``；``consecutive_unknown > 0 or consecutive_suspected > 0`` → ``retry``；其余 ``scheduled``。"""
    if int(link.check_count or 0) == 0:
        return "baseline"
    if int(link.consecutive_unknown or 0) > 0 or int(link.consecutive_suspected or 0) > 0:
        return "retry"
    return "scheduled"


def enqueue_due(limit: int = DEFAULT_LIMIT) -> int:
    """扫描到期链接入队，返回本轮入队数。锁被占用、功能关闭或已达日上限时返回 0。"""
    try:
        token = acquire_lock(LOCK_KEY, LOCK_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.warning("schedule_link_checks 获取锁失败（Redis 不可用）: %s", exc)
        return 0
    if not token:
        return 0
    try:
        with SessionLocal() as db:
            cfg = link_check_service.link_check_config(db)
            if not cfg.get("enabled", True):
                return 0
            daily_limit = int(cfg.get("daily_limit") or 0)
            remaining = max(0, daily_limit - link_check_service.used_today(db)) if daily_limit > 0 else limit
            if remaining <= 0:
                logger.info("删除检测已达日上限 %s，本轮不入队", daily_limit)
                return 0
            now = utcnow()
            rows = list(db.scalars(
                select(PublishLink)
                .where(PublishLink.is_monitoring == True, PublishLink.next_check_at <= now)  # noqa: E712
                .order_by(PublishLink.next_check_at, PublishLink.id)
                .limit(max(1, min(int(limit), remaining)))
            ).all())
            enqueued = 0
            try:
                for link in rows:
                    if link_service.enqueue_check(link, check_type_for(link), None):
                        enqueued += 1
            finally:
                db.commit()                      # 已入队链接的 next_check_at = now + 1h（防重复入队兜底）
            if enqueued:
                logger.info("删除检测入队 %s 条（到期 %s 条）", enqueued, len(rows))
            return enqueued
    finally:
        release_lock(LOCK_KEY, token)
