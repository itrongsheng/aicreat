"""worker 进程与周期任务测试（docs/01 §5.1、§5.3；docs/03「任务幂等与状态收敛」第 8 条；docs/08 §10.3、§11.1、§12.2、§12.4）。

主循环辅助类（``TrackedThreadPool`` / ``PeriodicTimers`` / 心跳）、``run_ai_tasks.drain`` + ``process_one``（测试处理器）、
停止时退回未开始的已领取任务、``Worker.start/tick/shutdown``、``recover_stale_tasks`` ①~⑥ 各分支、``sync_models`` /
``reconcile_usage`` / ``health_probe`` 入口函数。上游一律为 Mock 客户端或 ``httpx.MockTransport``；Redis 使用 db 15。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import worker as worker_module
from app.core.redis import redis_client
from app.core.zhiqi.client import ZhiqiClient
from app.core.zhiqi.types import Timeouts
from app.models import (
    AiTask,
    Alert,
    CapabilityRoute,
    GenerationBatch,
    Keyword,
    MediaAsset,
    Project,
    utcnow,
)
from app.services import ai_catalog_service, ai_task_service
from app.services import ai_gateway_service as gw
from app.tasks import health_probe, reconcile_usage, recover_stale_tasks, run_ai_tasks, sync_models
from app.worker import PeriodicTimers, TrackedThreadPool, Worker, heartbeat
from tests.conftest import API

pytestmark = pytest.mark.usefixtures("redis_required")

QUEUE = "queue:ai_tasks"
MESSAGES = [{"role": "user", "content": "生成关键词"}]


# =====================================================================
# 夹具与工具
# =====================================================================


@pytest.fixture
def gdb(db: Session) -> Session:
    gw.ensure_default_routes(db)
    return db


@pytest.fixture
def owner(users: Any) -> Any:
    return users.create("operator", username="owner01")


@pytest.fixture
def project(gdb: Session, owner: Any) -> Project:
    p = Project(name="项目 A", slug="proj-a", owner_id=owner.id, created_by=owner.id)
    gdb.add(p)
    gdb.commit()
    return p


@pytest.fixture
def handlers() -> Iterator[None]:
    saved = dict(ai_task_service.REGISTRY)
    run_ai_tasks.reset_stop()
    yield
    ai_task_service.REGISTRY.clear()
    ai_task_service.REGISTRY.update(saved)
    ai_task_service.set_batch_finished_hook(None)
    run_ai_tasks.reset_stop()


@pytest.fixture
def pool() -> Iterator[TrackedThreadPool]:
    executor = TrackedThreadPool(max_workers=4, thread_name_prefix="test-worker")
    yield executor
    executor.shutdown(wait=True, cancel_futures=True)


def sems(text: int = 2, image: int = 1, video: int = 1) -> dict[str, threading.BoundedSemaphore]:
    return {"text": threading.BoundedSemaphore(text), "image": threading.BoundedSemaphore(image), "video": threading.BoundedSemaphore(video)}


def wait_idle(executor: TrackedThreadPool, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while executor.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert executor.in_flight == 0


def queued(db: Session, *, capability: str = "keyword", operation: str = "keyword_generate", enqueue: bool = True, **kw: Any) -> AiTask:
    task = gw.create_root_task(db, capability=capability, operation=operation, project_id=kw.pop("project_id", None), created_by=None,
                               enqueue=enqueue, **kw)
    db.commit()
    return task


def running_root(db: Session, *, minutes_ago: int = 20, capability: str = "keyword", operation: str = "keyword_generate", **kw: Any) -> AiTask:
    sync = kw.pop("sync", False)
    task = gw.create_root_task(db, capability=capability, operation=operation, project_id=kw.pop("project_id", None),
                               created_by=kw.pop("created_by", None), enqueue=False, sync=sync, **kw)
    stale = utcnow() - timedelta(minutes=minutes_ago)
    task.status = "running"
    task.locked_by = "dead-host:1"
    task.started_at = stale
    task.heartbeat_at = stale
    db.commit()
    return task


def add_attempt(db: Session, root: AiTask, *, request_id: str | None, status: str = "running", upstream_task_id: str | None = None) -> AiTask:
    row = AiTask(
        project_id=root.project_id, capability=root.capability, operation=root.operation, trigger_type=root.trigger_type,
        target_type=root.target_type, target_id=root.target_id, batch_id=root.batch_id, root_task_id=root.id, candidate_index=0,
        attempt=1, model="mock-text", protocol="openai_chat", status=status, request_id=request_id, upstream_task_id=upstream_task_id,
        started_at=utcnow(),
    )
    db.add(row)
    db.commit()
    return row


def make_batch(db: Session, project: Project, owner: Any, **kw: Any) -> GenerationBatch:
    batch = GenerationBatch(project_id=project.id, kind=kw.pop("kind", "keyword"), template_id=1, template_version=1,
                            task_total=kw.pop("task_total", 1), status=kw.pop("status", "running"), created_by=owner.id, **kw)
    db.add(batch)
    db.commit()
    return batch


def make_asset(db: Session, project: Project | None, root: AiTask | None, *, status: str = "pending", kind: str = "image", **kw: Any) -> MediaAsset:
    asset = MediaAsset(project_id=project.id if project else None, kind=kind, status=status, ai_task_id=root.id if root else None,
                       prompt="一只猫", created_by=kw.pop("created_by", 1), **kw)
    db.add(asset)
    db.commit()
    return asset


def alerts(db: Session, alert_type: str, status: str = "open") -> list[Alert]:
    db.expire_all()
    return list(db.scalars(select(Alert).where(Alert.alert_type == alert_type, Alert.status == status)).all())


# =====================================================================
# 主循环辅助类
# =====================================================================


def test_tracked_pool_in_flight_and_named_submit(pool: TrackedThreadPool) -> None:
    gate = threading.Event()
    calls: list[str] = []

    def slow(tag: str) -> str:
        gate.wait(5)
        calls.append(tag)
        return tag

    first = pool.named_submit("job", slow, "a")
    assert first is not None and pool.named_submit("job", slow, "b") is None and pool.running("job")
    pool.submit(slow, "c")
    assert pool.in_flight == 2 and not pool.is_full()
    pool.submit(slow, "d")
    pool.submit(slow, "e")
    assert pool.is_full()
    gate.set()
    wait_idle(pool)
    assert sorted(calls) == ["a", "c", "d", "e"] and not pool.running("job")
    # 周期任务异常只记日志
    failing = pool.named_submit("boom", lambda: 1 / 0)
    assert failing is not None and failing.result(timeout=5) is None


def test_periodic_timers_run_due_and_daily(pool: TrackedThreadPool) -> None:
    clock = {"t": 1000.0}
    now = {"v": datetime(2026, 10, 6, 18, 0, tzinfo=UTC)}  # Asia/Shanghai 02:00
    timers = PeriodicTimers(pool, clock=lambda: clock["t"], now=lambda: now["v"])
    hits: list[str] = []
    assert not timers.run_due("a", 60, lambda: hits.append("a"))      # 首次计时从启动起算
    clock["t"] += 61
    assert timers.run_due("a", 60, lambda: hits.append("a"))
    assert not timers.run_due("a", 60, lambda: hits.append("a"))
    assert not timers.run_due("off", 0, lambda: hits.append("off"))  # 0 = 关闭
    assert not timers.run_due("off", None, lambda: hits.append("off"))
    timers.touch("b")
    clock["t"] += 30
    assert not timers.run_due("b", 60, lambda: hits.append("b"))
    tz = ZoneInfo("Asia/Shanghai")
    assert not timers.run_daily("cleanup", "03:00", lambda: hits.append("cleanup"), tz=tz)
    now["v"] += timedelta(hours=1, minutes=5)                          # 03:05
    assert timers.run_daily("cleanup", "03:00", lambda: hits.append("cleanup"), tz=tz)
    assert not timers.run_daily("cleanup", "03:00", lambda: hits.append("cleanup"), tz=tz)
    now["v"] += timedelta(days=1)
    wait_idle(pool)
    assert timers.run_daily("cleanup", "03:00", lambda: hits.append("cleanup"), tz="Asia/Shanghai")
    wait_idle(pool)
    assert hits.count("a") == 1 and hits.count("cleanup") == 2 and "off" not in hits and "b" not in hits


def test_heartbeat_key_and_public_health(client: TestClient, db: Session) -> None:
    assert heartbeat("worker")
    key = worker_module.heartbeat_key("worker")
    data = json.loads(redis_client.get(key))
    assert set(data) == {"hostname", "pid", "at"} and data["at"].endswith("Z")
    assert 0 < redis_client.ttl(key) <= 900
    health = client.get(f"{API}/health").json()["data"]
    assert health["zhiqi_mode"] == "mock" and health["workers"]["worker"]["alive"] is True
    assert health["workers"]["worker"]["replicas"] == 1
    worker_module.clear_heartbeat("worker")
    assert not redis_client.exists(key)


# =====================================================================
# run_ai_tasks
# =====================================================================


def test_drain_process_one_with_handler(gdb: Session, pool: TrackedThreadPool, handlers: None) -> None:
    seen: list[tuple[int, str]] = []

    def handler(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
        result, attempt = ctx.complete_text(messages=MESSAGES, params=None, response_format="text")
        seen.append((ctx.task.id, threading.current_thread().name))
        return ai_task_service.TaskOutcome(output_excerpt=result.text[:10])

    ai_task_service.register_handler("keyword_generate", handler)
    tasks = [queued(gdb) for _ in range(3)]
    redis_client.rpush(QUEUE, "garbage", 999999)
    # 文本信号量 1（SQLite 测试会话共享单连接，避免两个执行线程同时写库）：第 2 个元素放回队头
    limits = sems(text=1)
    assert run_ai_tasks.drain(pool, limits, limit=10, worker_id="w:1") == 1
    assert redis_client.lrange(QUEUE, 0, -1)[0] == str(tasks[1].id)
    wait_idle(pool)
    assert run_ai_tasks.drain(pool, limits, limit=10, worker_id="w:1") == 1
    wait_idle(pool)
    assert run_ai_tasks.drain(pool, limits, limit=10, worker_id="w:1") == 1
    wait_idle(pool)
    gdb.expire_all()
    assert all(gdb.get(AiTask, t.id).status == "succeeded" for t in tasks)
    assert {t for t, _ in seen} == {t.id for t in tasks} and all(name.startswith("test-worker") for _, name in seen)
    assert redis_client.llen(QUEUE) == 0                # 非法 / 不存在的元素被丢弃
    assert limits["text"].acquire(blocking=False)       # 信号量已释放
    assert run_ai_tasks.unstarted_count() == 0


def test_drain_paused_full_pool_and_modality(gdb: Session, handlers: None) -> None:
    ai_task_service.register_handler("image_generate", lambda ctx: ai_task_service.TaskOutcome(status="failed", error_category="unknown"))
    image = queued(gdb, capability="image", operation="image_generate")
    gw.set_paused("quota_exceeded", 60)
    executor = TrackedThreadPool(max_workers=1)
    try:
        assert run_ai_tasks.drain(executor, sems(), worker_id="w:1") == 0 and redis_client.llen(QUEUE) == 1
        gw.clear_paused()
        limits = sems(image=1)
        assert limits["image"].acquire(blocking=False)
        assert run_ai_tasks.drain(executor, limits, worker_id="w:1") == 0      # image 信号量耗尽 → 放回
        assert redis_client.lrange(QUEUE, 0, -1) == [str(image.id)]
        limits["image"].release()
        gate = threading.Event()
        executor.submit(gate.wait, 5)                                         # 线程池已满 → 不 LPOP
        assert run_ai_tasks.drain(executor, limits, worker_id="w:1") == 0 and redis_client.llen(QUEUE) == 1
        gate.set()
        wait_idle(executor)
        assert run_ai_tasks.drain(executor, limits, worker_id="w:1") == 1
        wait_idle(executor)
    finally:
        executor.shutdown(wait=True)
    gdb.expire_all()
    assert gdb.get(AiTask, image.id).status == "failed"
    # 非 queued 元素直接丢弃
    done = queued(gdb)
    done.status = "cancelled"
    gdb.commit()
    executor = TrackedThreadPool(max_workers=1)
    try:
        assert run_ai_tasks.drain(executor, sems(), worker_id="w:1") == 0 and redis_client.llen(QUEUE) == 0
    finally:
        executor.shutdown(wait=True)


def test_stop_releases_unstarted_claims(gdb: Session, handlers: None) -> None:
    ai_task_service.register_handler("keyword_generate", lambda ctx: pytest.fail("停止后不应执行"))
    task = queued(gdb)
    redis_client.delete(QUEUE)
    assert ai_task_service.claim(gdb, task.id, "w:1")
    run_ai_tasks.request_stop()
    assert run_ai_tasks.process_one(task.id, worker_id="w:1") is False
    gdb.expire_all()
    task = gdb.get(AiTask, task.id)
    assert task.status == "queued" and task.locked_by is None and task.started_at is None
    assert redis_client.lrange(QUEUE, 0, -1) == [str(task.id)] and not redis_client.exists(f"lock:ai_task:{task.id}")
    # release_unstarted：领取后从未开始执行
    run_ai_tasks.reset_stop()
    redis_client.delete(QUEUE)
    assert ai_task_service.claim(gdb, task.id, "w:1")
    run_ai_tasks._unstarted.add(task.id)  # noqa: SLF001 - 模拟 drain 已提交、线程池关闭时被取消
    assert run_ai_tasks.release_unstarted("w:1") == 1
    gdb.expire_all()
    assert gdb.get(AiTask, task.id).status == "queued"


def test_worker_start_tick_shutdown(gdb: Session, handlers: None, monkeypatch: pytest.MonkeyPatch) -> None:
    ai_task_service.register_handler("keyword_generate", lambda ctx: ai_task_service.TaskOutcome())
    calls: list[str] = []
    monkeypatch.setattr(recover_stale_tasks, "recover", lambda: calls.append("recover") or {})
    worker = Worker(max_workers=3, poll_interval=0.01)
    worker.cleanup = None  # 每日 cleanup_media 到点即提交，SQLite 单连接下与本测试的根任务并发写互相干扰（与本测试无关）
    try:
        worker.start()
        wait_idle(worker.pool)                         # 启动即执行的 recover / sync_models 完成后再领取（SQLite 单连接）
        task = queued(gdb)
        assert worker.tick() == 1
        assert redis_client.exists(worker_module.heartbeat_key("worker"))
        wait_idle(worker.pool)
        assert worker.tick() == 0
    finally:
        worker.shutdown()
    gdb.expire_all()
    assert gdb.get(AiTask, task.id).status == "succeeded"
    assert calls == ["recover"]                                                       # 启动即一次
    assert ai_catalog_service.get_model_by_id(gdb, "mock-text") is not None           # 启动即同步目录
    assert not redis_client.exists(worker_module.heartbeat_key("worker"))


def test_worker_run_loop_exits_on_stop(gdb: Session, handlers: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recover_stale_tasks, "recover", lambda: {})
    monkeypatch.setattr(sync_models, "sync_models", lambda: {})
    worker = Worker(max_workers=2, poll_interval=0.01)
    worker.cleanup = None
    ticks = {"n": 0}
    original = worker.tick

    def tick() -> int:
        ticks["n"] += 1
        if ticks["n"] == 2:
            raise RuntimeError("单轮异常不退出")
        if ticks["n"] >= 4:
            worker.request_stop()
        return original()

    worker.tick = tick  # type: ignore[method-assign]
    thread = threading.Thread(target=worker.run)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and ticks["n"] >= 4


# =====================================================================
# recover_stale_tasks ①
# =====================================================================


def test_recover_sync_root_fails_without_retry(gdb: Session) -> None:
    probe = running_root(gdb, capability="content", operation="route_probe", trigger_type="health_probe", sync=True, target_type="route_probe")
    fresh = running_root(gdb, minutes_ago=1)
    result = recover_stale_tasks.recover()
    assert result["stale_failed"] == 1 and result["auto_retried"] == 0
    gdb.expire_all()
    probe = gdb.get(AiTask, probe.id)
    assert (probe.status, probe.error_category) == ("failed", "timeout") and probe.finished_at is not None
    assert gdb.get(AiTask, fresh.id).status == "running"
    assert gdb.scalar(select(AiTask.id).where(AiTask.parent_task_id == probe.id)) is None
    assert recover_stale_tasks.recover()["stale_failed"] == 0                     # 幂等


def test_recover_text_auto_retry_with_batch(gdb: Session, project: Project, owner: Any, handlers: None) -> None:
    finished: list[int] = []
    ai_task_service.set_batch_finished_hook(finished.append)
    batch = make_batch(gdb, project, owner, task_total=1)
    root = running_root(gdb, project_id=project.id, batch_id=batch.id, target_type="generation_batch", target_id=batch.id,
                        input={"count": 5}, created_by=owner.id)
    gw.check_quota(gdb, project_id=project.id, estimated_quota=50, task=root)
    gdb.commit()
    add_attempt(gdb, root, request_id=None)                                          # 未拿到 request_id 的尝试行
    redis_client.delete(QUEUE)
    result = recover_stale_tasks.recover_running(gdb, utcnow())
    assert result == {"stale_failed": 1, "stale_polling": 0, "stale_after_submit": 0, "auto_retried": 1}
    gdb.expire_all()
    old = gdb.get(AiTask, root.id)
    assert (old.status, old.error_category) == ("failed", "timeout")
    assert all(a.status == "failed" for a in gdb.scalars(select(AiTask).where(AiTask.root_task_id == root.id)))
    new = gdb.scalar(select(AiTask).where(AiTask.parent_task_id == root.id))
    assert (new.trigger_type, new.status, new.batch_id, new.project_id, new.created_by) == ("system", "queued", batch.id, project.id, owner.id)
    assert gw.task_input(new) == {"count": 5} and new.quota_reserved == 50
    assert redis_client.lrange(QUEUE, 0, -1) == [str(new.id)] and finished == [batch.id]
    batch = gdb.get(GenerationBatch, batch.id)
    assert batch.status == "running" and batch.task_failed == 0                     # −1 抵消 on_task_finished 的 +1（钩子为测试桩）


def test_recover_text_no_retry_when_batch_cancelled_or_child_exists(gdb: Session, project: Project, owner: Any) -> None:
    batch = make_batch(gdb, project, owner, status="cancelled")
    root = running_root(gdb, project_id=project.id, batch_id=batch.id)
    other = running_root(gdb, project_id=project.id)
    queued(gdb, project_id=project.id, parent_task_id=other.id, enqueue=False)
    result = recover_stale_tasks.recover_running(gdb, utcnow())
    assert result["stale_failed"] == 2 and result["auto_retried"] == 0
    gdb.expire_all()
    assert gdb.scalar(select(AiTask.id).where(AiTask.parent_task_id == root.id)) is None
    assert len(gdb.scalars(select(AiTask).where(AiTask.parent_task_id == other.id)).all()) == 1


def test_recover_stale_after_submit(gdb: Session, project: Project, owner: Any, handlers: None) -> None:
    failed_hook: list[tuple[int, str]] = []
    ai_task_service.register_handler("content_generate", lambda ctx: None, on_failed=lambda db, root, cat, msg: failed_hook.append((root.id, cat)))
    root = running_root(gdb, capability="content", operation="content_generate", project_id=project.id, target_type="content", target_id=1)
    add_attempt(gdb, root, request_id="req-billed", status="running")
    result = recover_stale_tasks.recover_running(gdb, utcnow())
    assert result["stale_after_submit"] == 1 and result["auto_retried"] == 0
    gdb.expire_all()
    root = gdb.get(AiTask, root.id)
    assert (root.status, root.error_category) == ("failed", "timeout") and gw.task_meta(root)["stale_after_submit"] is True
    assert failed_hook == [(root.id, "timeout")]
    assert gdb.scalar(select(AiTask.id).where(AiTask.parent_task_id == root.id)) is None


def test_recover_media_resume_polling_and_fail(gdb: Session, project: Project) -> None:
    submitted = running_root(gdb, capability="video", operation="video_generate", project_id=project.id, target_type="media_asset")
    add_attempt(gdb, submitted, request_id="req-v", status="succeeded", upstream_task_id="video_123")
    asset = make_asset(gdb, project, submitted, kind="video")
    lost = running_root(gdb, capability="image", operation="image_generate", project_id=project.id, target_type="media_asset")
    lost_asset = make_asset(gdb, project, lost)
    result = recover_stale_tasks.recover_running(gdb, utcnow())
    assert result["stale_polling"] == 1 and result["stale_failed"] == 1
    gdb.expire_all()
    submitted = gdb.get(AiTask, submitted.id)
    assert (submitted.status, submitted.upstream_task_id, submitted.locked_by) == ("polling", "video_123", None)
    assert submitted.next_poll_at is not None and submitted.deadline_at is not None
    assert 1100 <= (submitted.deadline_at - submitted.next_poll_at).total_seconds() <= 1300      # video.poll_budget_seconds=1200
    assert gdb.get(MediaAsset, asset.id).status == "submitted"
    lost = gdb.get(AiTask, lost.id)
    assert (lost.status, lost.error_category) == ("failed", "timeout")
    lost_asset = gdb.get(MediaAsset, lost_asset.id)
    assert (lost_asset.status, lost_asset.error_category) == ("failed", "timeout") and lost_asset.failed_at is not None
    media_alerts = alerts(gdb, "media_task_failed")
    assert [a.target_id for a in media_alerts] == [lost_asset.id] and media_alerts[0].project_id == project.id


# =====================================================================
# ② ~ ⑥
# =====================================================================


def test_expire_polling(gdb: Session, project: Project) -> None:
    root = queued(gdb, capability="image", operation="image_generate", project_id=project.id, enqueue=False)
    root.status = "polling"
    root.upstream_task_id = "img_1"
    root.deadline_at = utcnow() - timedelta(seconds=5)
    gdb.commit()
    asset = make_asset(gdb, project, root, status="generating")
    future = queued(gdb, capability="image", operation="image_generate", project_id=project.id, enqueue=False)
    future.status = "polling"
    future.deadline_at = utcnow() + timedelta(minutes=5)
    gdb.commit()
    assert recover_stale_tasks.expire_polling(gdb, utcnow()) == {"expired": 1}
    gdb.expire_all()
    assert gdb.get(AiTask, root.id).status == "expired" and gdb.get(AiTask, future.id).status == "polling"
    asset = gdb.get(MediaAsset, asset.id)
    assert asset.status == "expired" and asset.failed_at is not None
    assert len(alerts(gdb, "media_task_failed")) == 1


def test_requeue_queued(gdb: Session) -> None:
    lost = queued(gdb, enqueue=False)
    present = queued(gdb, enqueue=False)
    redis_client.rpush(QUEUE, present.id)
    later = utcnow() + timedelta(minutes=11)
    gw.set_paused("auth_failed", 60)
    assert recover_stale_tasks.requeue_queued(gdb, later) == {"requeued": 0}
    gw.clear_paused()
    assert recover_stale_tasks.requeue_queued(gdb, utcnow()) == {"requeued": 0}       # 未超过 10 分钟
    assert recover_stale_tasks.requeue_queued(gdb, later) == {"requeued": 1}
    assert redis_client.lrange(QUEUE, 0, -1) == [str(present.id), str(lost.id)]


def test_converge_batches_fallback(gdb: Session, project: Project, owner: Any) -> None:
    batch = make_batch(gdb, project, owner, task_total=3)
    ok = queued(gdb, project_id=project.id, batch_id=batch.id, enqueue=False)
    attempt = add_attempt(gdb, ok, request_id="r1", status="succeeded")
    gw.update_task_meta(ok, apply_counts={"duplicates": 2, "invalid": 0, "intent_missing": 0, "empty_output": 0, "too_long": 0})
    ok.status = "succeeded"
    blocked = queued(gdb, project_id=project.id, batch_id=batch.id, enqueue=False)
    blocked.status, blocked.error_category = "failed", "content_blocked"
    stale = queued(gdb, project_id=project.id, batch_id=batch.id, enqueue=False)
    stale.status, stale.error_category = "failed", "timeout"
    gw.update_task_meta(stale, stale_after_submit=True)
    retried = queued(gdb, project_id=project.id, batch_id=batch.id, enqueue=False)
    retried.status, retried.error_category = "failed", "timeout"
    child = queued(gdb, project_id=project.id, batch_id=batch.id, parent_task_id=retried.id, enqueue=False)
    child.status = "cancelled"
    gdb.commit()
    for word in ("a", "b"):
        gdb.add(Keyword(project_id=project.id, keyword=word, normalized_keyword=word, language="zh-CN", batch_id=batch.id,
                        ai_task_id=attempt.id, created_by=owner.id))
    gdb.commit()
    idle = make_batch(gdb, project, owner, heartbeat_at=utcnow() - timedelta(minutes=40))
    busy = make_batch(gdb, project, owner)
    queued(gdb, project_id=project.id, batch_id=busy.id, enqueue=False)
    result = recover_stale_tasks.converge_batches(gdb, utcnow())
    assert result == {"batches_converged": 1, "batches_idle_partial": 1}
    gdb.expire_all()
    batch = gdb.get(GenerationBatch, batch.id)
    assert (batch.status, batch.task_done, batch.task_failed, batch.produced_count) == ("partial", 1, 3, 2)
    assert batch.error_summary == "duplicates=2;cancelled=1;content_blocked=1;stale_after_submit=1" and batch.finished_at
    assert gdb.get(GenerationBatch, idle.id).status == "partial" and gdb.get(GenerationBatch, busy.id).status == "running"


def test_converge_batches_uses_generation_hook(gdb: Session, project: Project, owner: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    batch = make_batch(gdb, project, owner)
    root = queued(gdb, project_id=project.id, batch_id=batch.id, enqueue=False)
    root.status = "succeeded"
    gdb.commit()
    called: list[int] = []

    def hook(db: Session, batch_id: int) -> str:
        assert redis_client.exists(f"lock:generation_batch:{batch_id}")
        called.append(batch_id)
        db.get(GenerationBatch, batch_id).status = "succeeded"
        return "succeeded"

    monkeypatch.setattr(recover_stale_tasks, "batch_hook", lambda: hook)
    assert recover_stale_tasks.converge_batches(gdb, utcnow())["batches_converged"] == 1
    gdb.expire_all()
    assert called == [batch.id] and gdb.get(GenerationBatch, batch.id).status == "succeeded"
    assert not redis_client.exists(f"lock:generation_batch:{batch.id}")


def test_worker_stale_monitor_worker(gdb: Session) -> None:
    now = utcnow()
    result = recover_stale_tasks.check_worker_stale(gdb, "monitor_worker", now)
    assert result["worker_stale_raised"] == 1
    assert [a.target_key for a in alerts(gdb, "worker_stale")] == ["monitor_worker"]     # 无副本 → 进程级
    old = (now - timedelta(minutes=8)).isoformat() + "Z"
    redis_client.set("worker:heartbeat:monitor_worker:h1:11", json.dumps({"hostname": "h1", "pid": 11, "at": old}), ex=900)
    recover_stale_tasks.check_worker_stale(gdb, "monitor_worker", now)
    assert sorted(a.target_key for a in alerts(gdb, "worker_stale")) == ["monitor_worker", "monitor_worker:h1:11"]
    fresh = now.isoformat() + "Z"
    redis_client.set("worker:heartbeat:monitor_worker:h2:22", json.dumps({"hostname": "h2", "pid": 22, "at": fresh}), ex=900)
    recover_stale_tasks.check_worker_stale(gdb, "monitor_worker", now)
    assert [a.target_key for a in alerts(gdb, "worker_stale")] == ["monitor_worker:h1:11"]   # 进程级解决、副本级保留
    redis_client.delete("worker:heartbeat:monitor_worker:h1:11")                              # 副本键过期 → 自动解决
    recover_stale_tasks.check_worker_stale(gdb, "monitor_worker", now)
    assert alerts(gdb, "worker_stale") == [] and len(alerts(gdb, "worker_stale", "resolved")) == 2


def test_recover_stale_transfers(gdb: Session, project: Project) -> None:
    old = utcnow() - timedelta(minutes=20)
    retry = make_asset(gdb, project, None, status="downloading", upstream_url="https://cdn.test/a.png", updated_at=old)
    last = make_asset(gdb, project, None, status="downloading", transfer_attempts=2, updated_at=old)
    locked = make_asset(gdb, project, None, status="downloading", updated_at=old)
    fresh = make_asset(gdb, project, None, status="downloading")
    redis_client.set(f"lock:media:transfer:{locked.id}", "x", ex=600)
    result = recover_stale_tasks.recover_transfers(gdb, utcnow())
    assert result == {"transfers_rescheduled": 1, "transfers_failed": 1}
    gdb.expire_all()
    retry = gdb.get(MediaAsset, retry.id)
    assert retry.status == "downloading" and retry.transfer_attempts == 1 and retry.next_transfer_at is not None
    assert 20 <= (retry.next_transfer_at - utcnow()).total_seconds() <= 31                   # retry_seconds[0]=30
    last = gdb.get(MediaAsset, last.id)
    assert (last.status, last.error_category, last.transfer_attempts) == ("failed", "transfer_failed", 3) and last.failed_at
    assert [a.target_id for a in alerts(gdb, "media_task_failed")] == [last.id]
    assert gdb.get(MediaAsset, locked.id).transfer_attempts == 0 and gdb.get(MediaAsset, fresh.id).transfer_attempts == 0


def test_recover_lock_and_step_isolation(gdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    redis_client.set("lock:worker:recover", "x", ex=60)
    assert recover_stale_tasks.recover() == {"skipped": True}
    redis_client.delete("lock:worker:recover")

    def boom(db: Session, now: datetime) -> dict[str, int]:
        raise RuntimeError("step failed")

    monkeypatch.setattr(recover_stale_tasks, "expire_polling", boom)
    result = recover_stale_tasks.recover()
    assert result["error_②"] is True and "requeued" in result and "worker_stale_raised" in result
    assert not redis_client.exists("lock:worker:recover")


# =====================================================================
# sync_models / reconcile_usage / health_probe
# =====================================================================


def test_sync_models_task(gdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    result = sync_models.sync_models()
    assert result["total"] == 3 and result["request_ids"]["models"].startswith("mock-")
    redis_client.set("lock:ai:models_sync", "x", ex=60)
    assert sync_models.sync_models() == {"skipped": True, "reason": "locked"}
    redis_client.delete("lock:ai:models_sync")
    failing = ZhiqiClient(base_url="https://zhiqi.test/v1", api_key="sk", timeouts=Timeouts(), user_agent="t",
                          transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "invalid token"}})),
                          sleep=lambda _s: None)
    monkeypatch.setattr(ai_catalog_service, "get_client", lambda: failing)
    assert sync_models.sync_models()["error"]["error_category"] == "auth_failed"


def test_reconcile_usage_task_and_interval(gdb: Session) -> None:
    result = reconcile_usage.reconcile()
    assert set(result) == {"pulled", "new", "matched", "unmatched", "window_overflow", "request_ids"}
    assert reconcile_usage.interval_seconds(gdb) == 300
    redis_client.set("ai:usage:last_pull", json.dumps({"window_overflow": True}))
    assert reconcile_usage.interval_seconds(gdb) == 60
    redis_client.set("lock:worker:reconcile", "x", ex=60)
    assert reconcile_usage.reconcile() == {"skipped": True, "reason": "locked"}


def test_health_probe_routes(gdb: Session, pool: TrackedThreadPool) -> None:
    ai_catalog_service.sync_models(gdb)
    results = health_probe.probe_routes(pool)
    pairs = {(r.capability.value, r.model) for r in results}
    assert pairs == {(c, "mock-text") for c in ("keyword", "title", "content", "rewrite", "geo_check", "seo_check")} | {
        ("image", "mock-image"), ("video", "mock-video"),
    }
    assert all(r.status == "healthy" for r in results)
    for capability, model in pairs:
        snap = json.loads(redis_client.get(f"ai:health:{capability}:{model}"))
        assert snap["status"] == "healthy" and snap["consecutive_failures"] == 0
    gdb.expire_all()
    probes = gdb.scalars(select(AiTask).where(AiTask.operation == "route_probe", AiTask.root_task_id.is_(None))).all()
    assert len(probes) == 8 and all(p.trigger_type == "health_probe" and p.created_by is None for p in probes)
    assert not redis_client.exists("lock:ai:health_probe")
    redis_client.set("lock:ai:health_probe", "x", ex=60)
    assert health_probe.probe_routes(None) == []


def test_health_probe_disabled_routes_and_engine_overrides(gdb: Session) -> None:
    from app.services import settings_service

    ai_catalog_service.sync_models(gdb)
    for row in gdb.scalars(select(CapabilityRoute)).all():
        row.is_enabled = row.capability == "seo_check"
    gdb.commit()
    gw.invalidate_routes_cache()
    seo = settings_service.get_config(gdb, "seo_providers")
    seo["engines"]["baidu"]["model"] = "mock-text"
    settings_service.set_value(gdb, "seo_providers", seo)
    geo = settings_service.get_config(gdb, "geo_engines")
    geo["engines"][0]["model"] = "mock-text"
    settings_service.set_value(gdb, "geo_engines", geo)
    results = health_probe.probe_routes(None)
    assert {(r.capability.value, r.model) for r in results} == {("seo_check", "mock-text"), ("geo_check", "mock-text")}


def test_health_probe_unknown_media_keeps_failures(gdb: Session) -> None:
    """``ai_models`` 无该行的 image 模型（未同步）→ ``unknown``：不记任务行、保留 ``consecutive_failures``。"""
    redis_client.set("ai:health:image:mock-image", json.dumps({"status": "degraded", "consecutive_failures": 1}))
    result = ai_task_service.probe_and_record(gdb, capability="image", model="mock-image")
    assert result.status == "unknown"
    snap = json.loads(redis_client.get("ai:health:image:mock-image"))
    assert snap["status"] == "unknown" and snap["consecutive_failures"] == 1
    assert gdb.scalar(select(AiTask.id).where(AiTask.model == "mock-image")) is None


def test_worker_bootstrap_lock_timeout(gdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "BOOTSTRAP_LOCK_WAIT", 0.2)
    redis_client.set("lock:bootstrap", "other", ex=60)
    worker_module.bootstrap()       # 等锁超时直接继续，不抛异常
    redis_client.delete("lock:bootstrap")
    worker_module.bootstrap()
    assert gdb.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "video", CapabilityRoute.project_id == 0))

