"""``python -m app.worker``：AI 任务执行与周期任务主循环（docs/01 §5.1、§5.3、§4.6；docs/02 §2.1 ``app/worker.py``）。

主循环线程只做领取（``LPOP`` + 非阻塞取信号量 + ``claim``）、到期查询（短 SQL）、周期任务的**提交**与心跳；任何网络 I/O
（上游 HTTP、下载、探测、对账、目录同步）与长事务都在进程内线程池执行，单轮阻塞 ≤ 2s，心跳 ``at`` 持续刷新。
并发限流用进程内 ``threading.BoundedSemaphore``（按模态，``AI_MAX_CONCURRENCY_TEXT/IMAGE/VIDEO``），不走 Redis；
多副本部署时每副本独立限流，任务互斥依赖 DB ``claim`` 与 ``lock:*``。

主循环辅助类 ``TrackedThreadPool`` / ``PeriodicTimers`` 与 ``heartbeat(name)`` / ``bootstrap(name)`` 定义在本文件顶部，
``app.monitor_worker``（第 5 步）从这里导入复用（两进程同构）。

``poll_media_tasks`` / ``transfer_media`` / ``cleanup_media`` 由第 4 步提供：模块存在即自动接入主循环，不存在时跳过。

信号：首次 SIGTERM / SIGINT → 停止领取新任务，等待执行中的任务完成后退出（已领取但未开始的根任务退回 ``queued``）；
再次收到信号 → 立即退出（未完成的根任务由其它副本的 ``recover_stale_tasks`` 按心跳超时回收）。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import redis

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.locks import LockTimeout, with_lock
from app.core.redis import redis_client
from app.services import (
    admin_rbac_service,
    ai_gateway_service,
    settings_service,
    stats_service,
)
from app.tasks import health_probe, reconcile_usage, recover_stale_tasks, run_ai_tasks, sync_models

logger = logging.getLogger("app.worker")

WORKER_NAME = "worker"
HEARTBEAT_PREFIX = "worker:heartbeat:"
HEARTBEAT_TTL_SECONDS = 900
BOOTSTRAP_LOCK_KEY = "lock:bootstrap"
BOOTSTRAP_LOCK_TTL = 60
BOOTSTRAP_LOCK_WAIT = 30
RECOVER_INTERVAL_SECONDS = 60
TRANSFER_RETRY_INTERVAL_SECONDS = 60
CLEANUP_DAILY_AT = "03:00"
DRAIN_LIMIT = 10
POLL_LIMIT = 20


# =====================================================================
# 进程标识 / 心跳 / 启动引导
# =====================================================================


def worker_id() -> str:
    """``ai_tasks.locked_by`` = ``{hostname}:{pid}``。"""
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def heartbeat_key(name: str) -> str:
    return f"{HEARTBEAT_PREFIX}{name}:{socket.gethostname()}:{os.getpid()}"


def heartbeat(name: str = WORKER_NAME) -> bool:
    """``SET worker:heartbeat:{name}:{hostname}:{pid} {hostname, pid, at} EX 900``（每副本一键，主循环每轮写入）。"""
    at = datetime.now(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
    payload = json.dumps({"hostname": socket.gethostname(), "pid": os.getpid(), "at": at}, separators=(",", ":"))
    try:
        return bool(redis_client.set(heartbeat_key(name), payload, ex=HEARTBEAT_TTL_SECONDS))
    except redis.RedisError as exc:
        logger.warning("写入心跳失败：%s", exc)
        return False


def clear_heartbeat(name: str = WORKER_NAME) -> None:
    """正常退出时删除本副本心跳键（其它副本与监控据此判断存活副本数）。"""
    try:
        redis_client.delete(heartbeat_key(name))
    except redis.RedisError:
        pass


def run_ensure_steps() -> None:
    """``ensure_rbac_seed`` → ``ensure_default_settings`` → ``ensure_default_routes``（全部幂等，与 ``main.py`` 相同）。"""
    with SessionLocal() as db:
        admin_rbac_service.ensure_rbac_seed(db)
        settings_service.ensure_default_settings(db)
        ai_gateway_service.ensure_default_routes(db)


def bootstrap(name: str = WORKER_NAME) -> None:
    """``lock:bootstrap``（60s，最多等待 30s）内执行 ensure_*；等锁超时直接继续（持锁进程已完成同样的幂等写入，docs/01 §4.6）。"""
    try:
        with with_lock(BOOTSTRAP_LOCK_KEY, BOOTSTRAP_LOCK_TTL, wait_seconds=BOOTSTRAP_LOCK_WAIT):
            run_ensure_steps()
    except LockTimeout:
        logger.warning("%s 等待 %ss 仍未获取 %s，跳过启动引导（由持锁进程完成）", name, BOOTSTRAP_LOCK_WAIT, BOOTSTRAP_LOCK_KEY)
    except redis.RedisError as exc:
        logger.warning("Redis 不可用，无锁执行启动引导：%s", exc)
        run_ensure_steps()


# =====================================================================
# 线程池与周期任务
# =====================================================================


class TrackedThreadPool(ThreadPoolExecutor):
    """``ThreadPoolExecutor`` 子类：自维护 ``in_flight`` 计数（submit +1、完成回调 −1）与 ``named_submit(name, fn)``
    （同名任务同时最多一个在飞）；「线程池已满」= ``in_flight >= max_workers``。"""

    def __init__(self, max_workers: int, thread_name_prefix: str = "worker") -> None:
        super().__init__(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self.max_workers = max_workers
        self._count_lock = threading.Lock()
        self._in_flight = 0
        self._named: dict[str, Future] = {}

    @property
    def in_flight(self) -> int:
        with self._count_lock:
            return self._in_flight

    def is_full(self) -> bool:
        return self.in_flight >= self.max_workers

    def _done(self, _future: Future) -> None:
        with self._count_lock:
            self._in_flight = max(0, self._in_flight - 1)

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:  # type: ignore[override]
        with self._count_lock:
            self._in_flight += 1
        try:
            future = super().submit(fn, *args, **kwargs)
        except BaseException:
            with self._count_lock:
                self._in_flight = max(0, self._in_flight - 1)
            raise
        future.add_done_callback(self._done)
        return future

    def named_submit(self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future | None:
        """同名任务仍在飞（排队或执行中）时不重复提交，返回 ``None``；异常在完成回调中记日志。"""
        with self._count_lock:
            running = self._named.get(name)
            if running is not None and not running.done():
                return None
        future = self.submit(_logged(name, fn), *args, **kwargs)
        with self._count_lock:
            self._named[name] = future
        return future

    def running(self, name: str) -> bool:
        with self._count_lock:
            future = self._named.get(name)
        return future is not None and not future.done()


def _logged(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    def _run(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - 周期任务异常只记日志，不影响主循环
            logger.exception("周期任务 %s 执行失败", name)
            return None
        finally:
            elapsed = time.monotonic() - started
            if elapsed > 60:
                logger.info("周期任务 %s 耗时 %.1fs", name, elapsed)

    return _run


class PeriodicTimers:
    """记录各周期任务上次提交时间（``time.monotonic``），回调一律 ``pool.named_submit``。

    - ``run_due(name, interval_seconds, fn)``：``interval_seconds <= 0`` 表示关闭；首次计时从进程启动起算（启动即执行的任务
      由调用方显式提交后 ``touch``）；同名任务仍在飞时不重复提交（下一轮再判断）；
    - ``run_daily(name, "HH:MM", fn, tz)``：按 ``tz``（``stats_config.timezone``）判定当日是否已执行，当地时间到点后执行一次。
    """

    def __init__(self, pool: TrackedThreadPool, *, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] | None = None) -> None:
        self.pool = pool
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._started = clock()
        self._last: dict[str, float] = {}
        self._daily: dict[str, str] = {}

    def touch(self, name: str) -> None:
        self._last[name] = self._clock()

    def last_run(self, name: str) -> float | None:
        return self._last.get(name)

    def run_due(self, name: str, interval_seconds: float | int | None, fn: Callable[[], Any]) -> bool:
        try:
            interval = float(interval_seconds or 0)
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            return False
        now = self._clock()
        if now - self._last.get(name, self._started) < interval:
            return False
        if self.pool.named_submit(name, fn) is None:
            return False
        self._last[name] = now
        return True

    def run_daily(self, name: str, hh_mm: str, fn: Callable[[], Any], tz: tzinfo | str | None = None) -> bool:
        zone = _zone(tz)
        local = self._now().astimezone(zone)
        try:
            hour, minute = (int(part) for part in hh_mm.split(":", 1))
        except ValueError:
            logger.warning("无效的每日时间 %r（任务 %s）", hh_mm, name)
            return False
        today = local.date().isoformat()
        if self._daily.get(name) == today or (local.hour, local.minute) < (hour, minute):
            return False
        if self.pool.named_submit(name, fn) is None:
            return False
        self._daily[name] = today
        return True


def _zone(tz: tzinfo | str | None) -> tzinfo:
    if isinstance(tz, tzinfo):
        return tz
    try:
        return ZoneInfo(str(tz or settings.app_timezone))
    except Exception:  # noqa: BLE001
        return UTC


def optional_task(module: str, attr: str) -> Callable[..., Any] | None:
    """第 4 步的任务模块（``poll_media_tasks`` / ``transfer_media`` / ``cleanup_media``）：存在即返回入口函数，否则 ``None``。"""
    name = f"app.tasks.{module}"
    if importlib.util.find_spec(name) is None:
        return None
    fn = getattr(importlib.import_module(name), attr, None)
    return fn if callable(fn) else None


# =====================================================================
# 主循环
# =====================================================================


class Worker:
    """``app.worker`` 主循环：``tick()`` 执行一轮（便于测试），``run()`` 循环至收到停止信号。"""

    def __init__(self, *, max_workers: int | None = None, sems: dict[str, threading.BoundedSemaphore] | None = None,
                 poll_interval: float | None = None) -> None:
        self.worker_id = worker_id()
        self.stop_event = threading.Event()
        workers = max_workers or (settings.ai_max_concurrency_text + settings.ai_max_concurrency_image + settings.ai_max_concurrency_video + 2)
        self.pool = TrackedThreadPool(max_workers=max(1, int(workers)), thread_name_prefix="worker")
        self.sems = sems or {
            "text": threading.BoundedSemaphore(max(1, settings.ai_max_concurrency_text)),
            "image": threading.BoundedSemaphore(max(1, settings.ai_max_concurrency_image)),
            "video": threading.BoundedSemaphore(max(1, settings.ai_max_concurrency_video)),
        }
        self.poll_interval = float(poll_interval if poll_interval is not None else settings.worker_poll_interval_seconds)
        self.timers = PeriodicTimers(self.pool)
        self.poll_due = optional_task("poll_media_tasks", "poll_due")
        self.transfer_retry_due = optional_task("transfer_media", "retry_due")
        self.cleanup = optional_task("cleanup_media", "cleanup")

    # ------------------------------------------------------------ 启动

    def start(self) -> None:
        """启动引导 + 启动即执行一次的 ``recover_stale_tasks.recover`` 与 ``sync_models``（docs/01 §4.6 第 4 条）。"""
        bootstrap(WORKER_NAME)
        heartbeat(WORKER_NAME)
        self.pool.named_submit("recover", recover_stale_tasks.recover)
        self.timers.touch("recover")
        self.pool.named_submit("sync_models", sync_models.sync_models)
        self.timers.touch("sync_models")

    # ------------------------------------------------------------ 每轮

    def tick(self) -> int:
        """一轮：心跳 → 读配置 → 领取与到期提交 → 周期任务提交。返回本轮提交的任务数（> 0 时不休眠）。"""
        heartbeat(WORKER_NAME)
        with SessionLocal() as db:
            cfg = settings_service.get_config(db, "ai_routing_config")
            reconcile_interval = reconcile_usage.interval_seconds(db)
            tz = stats_service.get_tz(db)
        submitted = run_ai_tasks.drain(self.pool, self.sems, limit=DRAIN_LIMIT, worker_id=self.worker_id)
        if self.poll_due is not None:
            submitted += int(self.poll_due(self.pool, limit=POLL_LIMIT) or 0)
        timers = self.timers
        timers.run_due("reconcile", reconcile_interval, reconcile_usage.reconcile)
        timers.run_due("sync_models", (cfg.get("catalog") or {}).get("sync_interval_seconds"), sync_models.sync_models)
        timers.run_due("health", (cfg.get("health") or {}).get("probe_interval_seconds"), lambda: health_probe.probe_routes(self.pool))
        timers.run_due("recover", RECOVER_INTERVAL_SECONDS, recover_stale_tasks.recover)
        if self.transfer_retry_due is not None:
            retry_due = self.transfer_retry_due
            timers.run_due("transfer_retry", TRANSFER_RETRY_INTERVAL_SECONDS, lambda: retry_due(self.pool))
        if self.cleanup is not None:
            timers.run_daily("cleanup", CLEANUP_DAILY_AT, self.cleanup, tz=tz)
        return submitted

    def run(self) -> None:
        logger.info("Worker 启动 worker_id=%s 线程池=%s 并发=%s", self.worker_id, self.pool.max_workers,
                    {k: v._initial_value for k, v in self.sems.items()})  # noqa: SLF001
        self.start()
        try:
            while not self.stop_event.is_set():
                try:
                    if self.tick():
                        continue  # 有任务时不休眠
                except Exception as exc:  # noqa: BLE001 - 主循环不因单轮异常退出
                    logger.exception("Worker 循环异常: %s", exc)
                self.stop_event.wait(self.poll_interval)
        finally:
            self.shutdown()

    # ------------------------------------------------------------ 停止

    def request_stop(self) -> None:
        self.stop_event.set()
        run_ai_tasks.request_stop()

    def shutdown(self) -> None:
        """停止领取 → 取消排队中的周期任务 → 等待执行中的任务完成 → 已领取未开始的根任务退回 ``queued`` → 删除心跳键。"""
        self.request_stop()
        logger.info("Worker 停止中：等待 %s 个执行中的任务完成", self.pool.in_flight)
        self.pool.shutdown(wait=True, cancel_futures=True)
        released = run_ai_tasks.release_unstarted(self.worker_id)
        if released:
            logger.info("已领取未开始的 %s 个根任务退回 queued", released)
        clear_heartbeat(WORKER_NAME)
        logger.info("Worker 已退出")


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] [%(threadName)s] %(message)s")
    logging.getLogger("app").setLevel(settings.log_level)


def install_signal_handlers(worker: Worker) -> None:
    def _handle(signum: int, _frame: Any) -> None:
        if worker.stop_event.is_set():
            logger.warning("再次收到信号 %s，立即退出", signum)
            os._exit(1)
        logger.info("收到信号 %s，停止领取新任务，等待执行中的任务完成（再次发送信号立即退出）", signum)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main() -> None:
    _configure_logging()
    logger.info("Worker 启动（zhiqi_mode=%s）", "mock" if settings.zhiqi_mock_mode else "live")
    worker = Worker()
    install_signal_handlers(worker)
    worker.run()


if __name__ == "__main__":
    main()
