"""每日统计聚合（docs/12 §4.4；运行在 ``app.monitor_worker``，计算均在线程池内执行）。

- ``aggregate(stat_date) -> int``：持 ``lock:monitor:daily_stats:{date}``（``SET NX EX 3600``），获取失败返回 0 并记 INFO；
  调用 ``stats_service.aggregate_daily`` 在单事务内 upsert + 清理旧行，提交后 ``cache_delete_prefix("cache:stats:")``，
  ``finally`` 释放锁；返回 upsert 行数。``aggregate_locked`` 同上但锁被占用时返回 ``None``（同步重算据此写 ``skipped[]``）；
- ``aggregate_today()``：``PeriodicTimers.run_due("stats_today", intraday_refresh_seconds)``；
- ``catch_up(days=3)``：启动时补算 ``today − (days − 1) … today``；
- ``daily_job()``：每日 ``stats_config.daily_at``（``run_daily("daily_stats", …)``）聚合昨天、重算前天，随后按保留期分批清理；
- ``drain_recompute(pool=None)``：主循环每轮调用；``recompute`` 任务在飞时不出队，否则 ``LPOP queue:stats_recompute`` 一条
  并 ``pool.named_submit("recompute", …)``（不给 ``pool`` 时在当前线程执行）。

其它入口：``reconcile_usage``（对账涉及的历史日期）与 ``POST /admin/stats/recompute``（≤ 7 天同步）共用同一把锁。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from typing import Any

import redis

from app.core.database import SessionLocal
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.services import stats_service

logger = logging.getLogger(__name__)

LOCK_PREFIX = "lock:monitor:daily_stats:"
LOCK_TTL_SECONDS = 3600
RECOMPUTE_QUEUE = stats_service.RECOMPUTE_QUEUE
RECOMPUTE_TASK_NAME = "recompute"
DEFAULT_CATCH_UP_DAYS = 3


def lock_key(stat_date: date) -> str:
    return f"{LOCK_PREFIX}{stat_date.isoformat()}"


def _as_date(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def aggregate_locked(stat_date: date | str) -> int | None:
    """持锁聚合一天；锁被占用（或 Redis 不可用）返回 ``None``，成功返回 upsert 行数。计算异常向上抛出。"""
    day = _as_date(stat_date)
    key = lock_key(day)
    try:
        token = acquire_lock(key, LOCK_TTL_SECONDS)
    except redis.RedisError as exc:
        logger.error("聚合锁不可用，跳过 stat_date=%s: %s", day, exc)
        return None
    if not token:
        logger.info("%s 已被占用，本轮跳过 stat_date=%s", key, day)
        return None
    try:
        started = time.monotonic()
        with SessionLocal() as db:
            rows = stats_service.aggregate_daily(db, day)
        stats_service.clear_cache()
        logger.info("daily_stats 聚合完成 stat_date=%s rows=%s 耗时 %.2fs", day, rows, time.monotonic() - started)
        return rows
    finally:
        try:
            release_lock(key, token)
        except redis.RedisError:  # 释放失败只会让锁自然过期
            logger.warning("释放 %s 失败", key)


def aggregate(stat_date: date | str) -> int:
    """``aggregate(stat_date) -> int``：锁被占用返回 0（不报错）。"""
    return aggregate_locked(stat_date) or 0


def _today() -> date:
    with SessionLocal() as db:
        return stats_service.today_date(db)


def aggregate_today() -> int:
    """今日增量刷新（总览「今日」最多滞后 ``intraday_refresh_seconds``）。"""
    return aggregate(_today())


def catch_up(days: int = DEFAULT_CATCH_UP_DAYS) -> int:
    """对 ``today − (days − 1) … today`` 逐日聚合（含今日），补齐停机期间缺失的行；返回 upsert 行数合计。"""
    today = _today()
    total = 0
    for offset in range(max(1, int(days)) - 1, -1, -1):
        day = today - timedelta(days=offset)
        try:
            total += aggregate(day)
        except Exception:  # noqa: BLE001 - 单日失败不影响其它日期
            logger.exception("catch_up 聚合失败 stat_date=%s", day)
    return total


def daily_job() -> int:
    """每日聚合：``aggregate(昨天)``、``aggregate(前天)``（吸收跨日对账回填与晚到的检测写回），随后删除保留期之外的行。"""
    today = _today()
    total = 0
    for offset in (1, 2):
        day = today - timedelta(days=offset)
        try:
            total += aggregate(day)
        except Exception:  # noqa: BLE001
            logger.exception("每日聚合失败 stat_date=%s", day)
    try:
        with SessionLocal() as db:
            stats_service.cleanup_retention(db, today=today)
    except Exception:  # noqa: BLE001
        logger.exception("daily_stats 保留期清理失败")
    return total


def parse_recompute(raw: Any) -> dict[str, Any] | None:
    """队列元素 ``{"start_date","end_date","requested_by"}`` → 规范化字典；非法返回 ``None``。"""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        start = date.fromisoformat(str(data["start_date"]))
        end = date.fromisoformat(str(data["end_date"]))
    except (TypeError, ValueError, KeyError):
        return None
    if start > end:
        return None
    requested_by = data.get("requested_by") if isinstance(data, dict) else None
    return {"start_date": start, "end_date": end, "requested_by": requested_by}


def run_recompute(payload: dict[str, Any]) -> int:
    """按日期升序逐日 ``aggregate``；获取锁失败的日期跳过并记日志；返回处理（成功聚合）的天数。"""
    start, end = payload["start_date"], payload["end_date"]
    skipped: list[str] = []
    done = 0
    day = start
    while day <= end:
        try:
            if aggregate_locked(day) is None:
                skipped.append(day.isoformat())
            else:
                done += 1
        except Exception:  # noqa: BLE001
            logger.exception("重算失败 stat_date=%s", day)
            skipped.append(day.isoformat())
        day += timedelta(days=1)
    logger.info("recompute done start=%s end=%s days=%s skipped=%s requested_by=%s", start.isoformat(), end.isoformat(),
                (end - start).days + 1, skipped, payload.get("requested_by"))
    return done


def drain_recompute(pool: Any = None) -> int:
    """消费 ``queue:stats_recompute`` 一条。

    给出 ``pool``（``TrackedThreadPool``）时：``recompute`` 任务仍在飞则不出队（元素留在队列等待下一轮，先出队再跳过会丢失请求），
    否则 ``LPOP`` 一条并 ``pool.named_submit("recompute", run_recompute, payload)``，主循环不等待；返回提交数（0 / 1）。
    不给 ``pool`` 时在当前线程执行，返回成功聚合的天数。"""
    if pool is not None and pool.running(RECOMPUTE_TASK_NAME):
        return 0
    try:
        raw = redis_client.lpop(RECOMPUTE_QUEUE)
    except redis.RedisError as exc:
        logger.warning("读取 %s 失败: %s", RECOMPUTE_QUEUE, exc)
        return 0
    if raw is None:
        return 0
    payload = parse_recompute(raw)
    if payload is None:
        logger.warning("丢弃非法的重算请求: %r", raw)
        return 0
    if pool is None:
        return run_recompute(payload)
    return 1 if pool.named_submit(RECOMPUTE_TASK_NAME, run_recompute, payload) is not None else 0
