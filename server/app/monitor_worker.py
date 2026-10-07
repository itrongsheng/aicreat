"""``python -m app.monitor_worker``：链接删除检测、SEO/GEO 收录检测、每日统计聚合与告警评估主循环
（docs/01 §5.2、§5.3、§4.6；docs/02 §2.1 ``app/monitor_worker.py``；docs/11 §2.6、§6.7、§9、§10.5）。

与 ``app.worker`` 同构（``TrackedThreadPool`` / ``PeriodicTimers`` / ``heartbeat`` / ``bootstrap`` 从 ``app.worker`` 导入复用）：

- 启动：``lock:bootstrap`` 内执行 ensure_* → 心跳 → ``named_submit("catch_up", aggregate_daily_stats.catch_up)``（补算最近 3 天）；
- 线程池 ``TrackedThreadPool(link_check.global_concurrency + index_check.concurrency + 2)``，信号量
  ``sems = {"link_check": BoundedSemaphore(global_concurrency), "index_check": BoundedSemaphore(index_check.concurrency)}``
  （启动时读取 ``monitoring_config``，修改后需重启进程）；
- 每轮：心跳 ``worker:heartbeat:monitor_worker:{hostname}:{pid}`` → ``schedule_link_checks.enqueue_due`` /
  ``schedule_index_checks.enqueue_due``（主循环线程，按各自 ``scan_interval_seconds``，``enabled=false`` 时不扫描）→
  ``run_link_checks.drain`` / ``run_index_checks.drain``（提交线程池）→ ``drain_recompute(pool)``（``recompute`` 不在飞时
  ``LPOP queue:stats_recompute`` 一条并 ``named_submit("recompute", …)``）→ 每日聚合 ``run_daily("daily_stats", daily_at,
  daily_job)``（聚合昨天、重算前天并按保留期清理）、今日刷新 ``run_due("stats_today", intraday_refresh_seconds)``、
  ``evaluate_alerts``（300s）；
- 有任务提交时不休眠，否则 ``MONITOR_POLL_INTERVAL_SECONDS``。

收录检测（``schedule_index_checks`` / ``run_index_checks``）、统计聚合（``aggregate_daily_stats``）与告警评估（``evaluate_alerts``）
由后续步骤提供：模块存在即自动接入主循环，不存在时跳过（``optional_task``）。

信号：首次 SIGTERM / SIGINT → 停止领取新元素，等待执行中的检测完成后退出（排队中的元素留在 Redis 队列，``queued:*`` 标记 1h 自过期，
MySQL 的 ``next_*_at`` 为排程权威）；再次收到信号 → 立即退出。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from app.core.config import settings
from app.core.database import SessionLocal
from app.services import settings_service, stats_service
from app.tasks import run_link_checks, schedule_link_checks
from app.worker import (
    PeriodicTimers,
    TrackedThreadPool,
    _configure_logging,
    bootstrap,
    clear_heartbeat,
    heartbeat,
    optional_task,
    worker_id,
)

logger = logging.getLogger("app.monitor_worker")

WORKER_NAME = "monitor_worker"
LINK_DRAIN_LIMIT = 20
INDEX_DRAIN_LIMIT = 10
EVALUATE_ALERTS_INTERVAL_SECONDS = 300
CATCH_UP_DAYS = 3
DEFAULT_DAILY_AT = "00:30"


def _int(value: Any, default: int, low: int = 1) -> int:
    try:
        return max(low, int(value))
    except (TypeError, ValueError):
        return default


class InlineTimers:
    """主循环线程内直接执行的周期任务（``schedule_*``：短 SQL + Redis，无网络 I/O）；``interval <= 0`` 表示关闭。

    首次在启动后立即执行（到期扫描需要尽快补齐积压）；异常只记日志。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: dict[str, float] = {}

    def run_due(self, name: str, interval_seconds: float | int | None, fn: Callable[[], Any]) -> Any:
        try:
            interval = float(interval_seconds or 0)
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            return None
        now = self._clock()
        last = self._last.get(name)
        if last is not None and now - last < interval:
            return None
        self._last[name] = now
        try:
            return fn()
        except Exception:  # noqa: BLE001 - 单个周期任务失败不影响主循环
            logger.exception("周期任务 %s 执行失败", name)
            return None


class MonitorWorker:
    """``app.monitor_worker`` 主循环：``tick()`` 执行一轮（便于测试），``run()`` 循环至收到停止信号。"""

    def __init__(self, *, poll_interval: float | None = None, link_concurrency: int | None = None,
                 index_concurrency: int | None = None) -> None:
        self.worker_id = worker_id()
        self.stop_event = threading.Event()
        if link_concurrency is None or index_concurrency is None:
            with SessionLocal() as db:
                cfg = settings_service.get_config(db, "monitoring_config")
            link_concurrency = link_concurrency or _int((cfg.get("link_check") or {}).get("global_concurrency"), 4)
            index_concurrency = index_concurrency or _int((cfg.get("index_check") or {}).get("concurrency"), 2)
        self.link_concurrency = int(link_concurrency)
        self.index_concurrency = int(index_concurrency)
        self.pool = TrackedThreadPool(max_workers=self.link_concurrency + self.index_concurrency + 2,
                                      thread_name_prefix="monitor")
        self.sems: dict[str, threading.BoundedSemaphore] = {
            "link_check": threading.BoundedSemaphore(self.link_concurrency),
            "index_check": threading.BoundedSemaphore(self.index_concurrency),
        }
        self.poll_interval = float(poll_interval if poll_interval is not None else settings.monitor_poll_interval_seconds)
        self.timers = PeriodicTimers(self.pool)
        self.inline = InlineTimers()
        # 后续步骤提供的任务：模块存在即接入，否则跳过
        self.schedule_index_checks = optional_task("schedule_index_checks", "enqueue_due")
        self.run_index_checks_drain = optional_task("run_index_checks", "drain")
        self.catch_up = optional_task("aggregate_daily_stats", "catch_up")
        self.aggregate = optional_task("aggregate_daily_stats", "aggregate")
        self.aggregate_today = optional_task("aggregate_daily_stats", "aggregate_today")
        self.daily_job = optional_task("aggregate_daily_stats", "daily_job")
        self.drain_recompute = optional_task("aggregate_daily_stats", "drain_recompute")
        self.evaluate_alerts = optional_task("evaluate_alerts", "evaluate")

    # ------------------------------------------------------------ 启动

    def start(self) -> None:
        bootstrap(WORKER_NAME)
        heartbeat(WORKER_NAME)
        if self.catch_up is not None:
            catch_up = self.catch_up
            self.pool.named_submit("catch_up", lambda: catch_up(days=CATCH_UP_DAYS))

    # ------------------------------------------------------------ 每轮

    def _daily_aggregate(self) -> None:
        """每日 ``stats_config.daily_at``：聚合昨天并重算前天（各自持 ``lock:monitor:daily_stats:{date}``，由聚合函数内部获取），
        随后按 ``retention_days`` 清理（``aggregate_daily_stats.daily_job``）。"""
        if self.daily_job is not None:
            self.daily_job()
            return
        if self.aggregate is None:
            return
        with SessionLocal() as db:
            today = stats_service.today_date(db)
        for days in (1, 2):
            try:
                self.aggregate(today - timedelta(days=days))
            except Exception:  # noqa: BLE001
                logger.exception("每日聚合失败 stat_date=%s", today - timedelta(days=days))

    def tick(self) -> int:
        """一轮：心跳 → 读配置 → 到期扫描（主循环线程）→ 消费队列（线程池）→ 周期任务提交。返回本轮提交的检测数。"""
        heartbeat(WORKER_NAME)
        with SessionLocal() as db:
            cfg = settings_service.get_config(db, "monitoring_config")
            stats_cfg = settings_service.get_config(db, "stats_config")
            tz = stats_service.get_tz(db)
        link_cfg = cfg.get("link_check") or {}
        index_cfg = cfg.get("index_check") or {}
        inline = self.inline
        inline.run_due("schedule_link_checks",
                       link_cfg.get("scan_interval_seconds") if link_cfg.get("enabled", True) else 0,
                       schedule_link_checks.enqueue_due)
        if self.schedule_index_checks is not None:
            inline.run_due("schedule_index_checks",
                           index_cfg.get("scan_interval_seconds") if index_cfg.get("enabled", True) else 0,
                           self.schedule_index_checks)
        submitted = run_link_checks.drain(self.pool, limit=LINK_DRAIN_LIMIT, sems=self.sems)
        if self.run_index_checks_drain is not None:
            submitted += int(self.run_index_checks_drain(self.pool, limit=INDEX_DRAIN_LIMIT, sems=self.sems) or 0)
        timers = self.timers
        if self.drain_recompute is not None:
            # recompute 在飞时不出队（元素留在队列等待下一轮）；出队后 named_submit("recompute", …)，主循环不等待
            try:
                self.drain_recompute(self.pool)
            except Exception:  # noqa: BLE001 - Redis 暂不可用等，下一轮重试
                logger.exception("drain_recompute 失败")
        if self.daily_job is not None or self.aggregate is not None:
            timers.run_daily("daily_stats", str(stats_cfg.get("daily_at") or DEFAULT_DAILY_AT), self._daily_aggregate, tz=tz)
        if self.aggregate_today is not None:
            timers.run_due("stats_today", stats_cfg.get("intraday_refresh_seconds"), self.aggregate_today)
        if self.evaluate_alerts is not None:
            timers.run_due("evaluate_alerts", EVALUATE_ALERTS_INTERVAL_SECONDS, self.evaluate_alerts)
        return submitted

    def run(self) -> None:
        logger.info("MonitorWorker 启动 worker_id=%s 线程池=%s 并发 link_check=%s index_check=%s", self.worker_id,
                    self.pool.max_workers, self.link_concurrency, self.index_concurrency)
        self.start()
        try:
            while not self.stop_event.is_set():
                try:
                    if self.tick():
                        continue  # 有任务时不休眠
                except Exception as exc:  # noqa: BLE001 - 主循环不因单轮异常退出（Redis 不可用时下轮重试）
                    logger.exception("MonitorWorker 循环异常: %s", exc)
                self.stop_event.wait(self.poll_interval)
        finally:
            self.shutdown()

    # ------------------------------------------------------------ 停止

    def request_stop(self) -> None:
        self.stop_event.set()

    def shutdown(self) -> None:
        """停止领取 → 取消排队中的周期任务 → 等待执行中的检测完成 → 删除心跳键。"""
        self.request_stop()
        logger.info("MonitorWorker 停止中：等待 %s 个执行中的任务完成", self.pool.in_flight)
        self.pool.shutdown(wait=True, cancel_futures=True)
        clear_heartbeat(WORKER_NAME)
        logger.info("MonitorWorker 已退出")


def install_signal_handlers(worker: MonitorWorker) -> None:
    def _handle(signum: int, _frame: Any) -> None:
        if worker.stop_event.is_set():
            logger.warning("再次收到信号 %s，立即退出", signum)
            os._exit(1)
        logger.info("收到信号 %s，停止领取新任务，等待执行中的检测完成（再次发送信号立即退出）", signum)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> None:
    _configure_logging()
    logger.info("MonitorWorker 启动（zhiqi_mode=%s）", "mock" if settings.zhiqi_mock_mode else "live")
    worker = MonitorWorker()
    install_signal_handlers(worker)
    worker.run()


if __name__ == "__main__":
    main()
