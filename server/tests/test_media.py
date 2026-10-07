"""图片 / 视频生成测试（docs/10 §12.1、§12.2；Mock 模式全生命周期）。

- 创建：参数映射与默认值、``params_json`` / ``input_json`` 快照、``reference_asset_ids_json`` 反解、校验链（400 / 404 / 409 / 5031 /
  429 / 4291 / 4222）、日上限与额度回滚、``quota_warning``；
- 执行与轮询：Mock 图片第 3 次轮询成功、视频第 5 次轮询成功（``progress=99`` 无特殊处理）、轮询间隔封顶、``deadline_at``、内嵌
  ``image_prompt`` 根任务（含暂停回滚）、同步回退、``content_blocked`` / ``unknown`` 失败、``media_storage`` 备选回退复用资产行、
  过期（本地预算 / 上游 ``expired``）、连续 3 次 404；
- 转存：Mock 复制占位文件、``url`` 位于 ``/media/`` 下、宽高 / 哈希 / 下载记录 / ``stats:rt``、重试节奏 30s / 120s 与第 3 次
  ``failed(transfer_failed)``（告警附 ``download_request_id``）、人工 ``transfer``、视频 ``/content`` 回退、封面联动；
- 人工入口：``retry`` 三分支（复查成功 → 转存、进行中 → ``polling``、404 / 非 ``timeout`` → 重新提交）与 5021、取消、删除 409 规则
  （含 ``in_use``）；列表 / 详情 / 任务摘要与数据范围过滤；
- ``recover_stale_tasks`` 媒体分支（① 续轮询 / 失败、② 过期、⑥ 转存重排）与 ``cleanup_media`` 两条规则。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BusinessError
from app.core.redis import redis_client
from app.core.storage import build_storage_key, get_storage, public_url_for
from app.core.zhiqi import mock as zhiqi_mock
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.mock import MockHTTPError, MockZhiqiClient
from app.core.zhiqi.types import ErrorCategory
from app.models import AdminOperationLog, AiTask, Alert, CapabilityRoute, Content, MediaAsset, Project, utcnow
from app.schemas.media import ImageGenerateBody, VideoGenerateBody
from app.services import ai_catalog_service, ai_task_service, media_service, settings_service, stats_service
from app.services import ai_gateway_service as gw
from app.services.data_scope_service import SYSTEM_SCOPE, DataScope
from app.tasks import cleanup_media, poll_media_tasks, recover_stale_tasks, transfer_media
from tests.conftest import ADMIN_API, User, UserFactory, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")

PLACEHOLDER_PNG = zhiqi_mock.MOCK_ASSETS_DIR / zhiqi_mock.PLACEHOLDER_PNG
PLACEHOLDER_MP4 = zhiqi_mock.MOCK_ASSETS_DIR / zhiqi_mock.PLACEHOLDER_MP4


# =====================================================================
# 夹具与助手
# =====================================================================


class SyncPool:
    """同步执行的线程池替身（记录提交的函数名）；``transfer=False`` 时丢弃转存提交（资产停在 ``downloading``）。"""

    def __init__(self, *, transfer: bool = True) -> None:
        self.calls: list[str] = []
        self.transfer = transfer

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        name = getattr(fn, "__name__", repr(fn))
        self.calls.append(name)
        if name == "transfer_asset" and not self.transfer:
            return None
        return fn(*args, **kwargs)


@pytest.fixture
def env(client: TestClient, db: Session, users: UserFactory, system_templates: dict[str, Any]) -> SimpleNamespace:
    """应用已启动（默认路由 + Mock 占位文件）、Mock 模型目录已同步、``sys_image_prompt`` 已 seed；一个运营用户的项目与内容。"""
    ai_catalog_service.sync_models(db)
    media_service.register_handlers()
    owner = users.create("operator", username="media_owner")
    project = Project(name="媒体项目", slug="media", owner_id=owner.id, created_by=owner.id)
    db.add(project)
    db.commit()
    content = Content(
        project_id=project.id, title="智能门锁怎么选", format="markdown", language="zh-CN", style="tutorial", status="draft",
        summary="选购智能门锁的五个要点", body="## 引言\n\n智能门锁正文", created_by=owner.id, updated_by=owner.id,
    )
    db.add(content)
    db.commit()
    return SimpleNamespace(owner=owner, scope=DataScope(owner.id, "own", owner.id), project=project, content=content)


def run_queue(db: Session, worker_id: str = "w:1") -> list[int]:
    """模拟 worker：``LPOP queue:ai_tasks`` → ``claim`` → ``process_one``，直到队列为空。"""
    processed: list[int] = []
    while True:
        raw = redis_client.lpop(gw.QUEUE_AI_TASKS)
        if raw is None:
            break
        task_id = int(raw)
        if ai_task_service.claim(db, task_id, worker_id):
            ai_task_service.process_one(task_id, worker_id=worker_id)
            processed.append(task_id)
    db.expire_all()
    return processed


def poll(db: Session, task_id: int, pool: SyncPool | None = None) -> int:
    """把根任务置为到期后执行一轮 ``poll_due``（同步线程池；缺省不立即转存），返回抢占数。"""
    db.execute(update(AiTask).where(AiTask.id == task_id).values(next_poll_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    count = poll_media_tasks.poll_due(pool or SyncPool(transfer=False))
    db.expire_all()
    return count


def gen_image(db: Session, env: SimpleNamespace, **values: Any) -> dict[str, Any]:
    body = {"project_id": env.project.id, "usage_type": "standalone", "prompt": "A cozy home office, soft light", **values}
    return media_service.generate_images(db, env.scope, ImageGenerateBody(**body), admin_id=env.owner.id)


def gen_video(db: Session, env: SimpleNamespace, **values: Any) -> dict[str, Any]:
    body = {"project_id": env.project.id, "usage_type": "standalone", "prompt": "A slow dolly shot across a desk", **values}
    return media_service.generate_video(db, env.scope, VideoGenerateBody(**body), admin_id=env.owner.id)


def asset_of(db: Session, asset_id: int) -> MediaAsset:
    db.expire_all()
    asset = db.get(MediaAsset, asset_id)
    assert asset is not None
    return asset


def task_of(db: Session, task_id: int) -> AiTask:
    db.expire_all()
    task = db.get(AiTask, task_id)
    assert task is not None
    return task


def media_alerts(db: Session) -> list[Alert]:
    db.expire_all()
    return list(db.scalars(select(Alert).where(Alert.alert_type == "media_task_failed").order_by(Alert.id)).all())


def rt(db: Session, project_id: int, field: str) -> int:
    value = redis_client.hget(stats_service.realtime_key(stats_service.today_date(db), project_id), field)
    return int(value or 0)


def daily_used(db: Session, kind: str = "image") -> int:
    return int(redis_client.get(media_service._daily_key(db, kind)) or 0)  # noqa: SLF001


def seconds_until(value: Any) -> float:
    return (value - utcnow()).total_seconds()


def ready_image(db: Session, env: SimpleNamespace, **values: Any) -> MediaAsset:
    """生成并跑完整条链路（3 次轮询 + 转存）的图片资产。"""
    data = gen_image(db, env, **values)
    run_queue(db)
    for task_id in data["task_ids"]:
        for _ in range(3):
            poll(db, task_id)
    for asset_id in data["asset_ids"]:
        if asset_of(db, asset_id).status == "downloading":
            assert media_service.transfer_asset(asset_id)
    return asset_of(db, data["asset_ids"][-1])


def make_upload(db: Session, owner: User, *, created_days_ago: int = 0, kind: str = "image") -> MediaAsset:
    """模拟 ``POST /admin/uploads/image`` 落库的参考素材（文件写入存储）。"""
    ext = "png" if kind == "image" else "mp4"
    key = build_storage_key("uploads", ext)
    source = PLACEHOLDER_PNG if kind == "image" else PLACEHOLDER_MP4
    get_storage().save(key, source.read_bytes(), content_type="image/png" if kind == "image" else "video/mp4")
    created = utcnow() - timedelta(days=created_days_ago)
    asset = MediaAsset(
        project_id=None, kind=kind, source="uploaded", usage_type="reference", status="ready", storage_key=key,
        url=public_url_for(key), thumbnail_key=key if kind == "image" else None, mime_type="image/png" if kind == "image" else "video/mp4",
        ready_at=created, created_by=owner.id, created_at=created,
    )
    db.add(asset)
    db.commit()
    return asset


def expire_root(db: Session, task_id: int) -> None:
    db.execute(update(AiTask).where(AiTask.id == task_id).values(deadline_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert recover_stale_tasks.expire_polling(db, utcnow()) == {"expired": 1}
    db.expire_all()


# =====================================================================
# 1. 图片全链路（Mock：第 3 次轮询成功）
# =====================================================================


def test_image_lifecycle_mock(env: SimpleNamespace, db: Session, client: TestClient) -> None:
    data = gen_image(db, env)
    assert set(data) == {"asset_ids", "task_ids"}
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    asset, root = asset_of(db, asset_id), task_of(db, task_id)
    assert (asset.status, asset.kind, asset.source, asset.usage_type, asset.ai_task_id) == ("pending", "image", "generated", "standalone", task_id)
    assert asset.model == "mock-image" and asset.created_by == env.owner.id and asset.content_id is None
    assert json.loads(asset.params_json) == {"resolution": "1080p", "aspect_ratio": "16:9", "reference_image_urls": []}
    assert (root.status, root.capability, root.operation, root.trigger_type) == ("queued", "image", "image_generate", "user")
    assert (root.target_type, root.target_id, root.project_id, root.created_by, root.batch_id) == ("media_asset", asset_id, env.project.id, env.owner.id, None)
    assert gw.task_input(root) == {
        "prompt": "A cozy home office, soft light", "from_content_prompt": False, "content_id": None, "usage_type": "standalone",
        "resolution": "1080p", "aspect_ratio": "16:9", "reference_image_urls": [],
    }
    assert root.quota_reserved > 0
    assert daily_used(db) == 1 and redis_client.zcard(f"rate:media:{env.owner.id}") == 1
    assert str(task_id) in redis_client.lrange(gw.QUEUE_AI_TASKS, 0, -1)

    # 提交：202 queued → 根任务 polling、资产 submitted；首个间隔 5s，预算 600s
    assert run_queue(db) == [task_id]
    root, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert root.status == "polling" and root.upstream_task_id.startswith("task_mock_") and root.protocol == "image_async"
    assert 3 <= seconds_until(root.next_poll_at) <= 6 and 595 <= seconds_until(root.deadline_at) <= 601
    assert asset.status == "submitted" and asset.upstream_task_id == root.upstream_task_id
    attempts = db.scalars(select(AiTask).where(AiTask.root_task_id == task_id)).all()
    assert len(attempts) == 1 and attempts[0].status == "succeeded" and attempts[0].request_id.startswith("mock-")

    pool = SyncPool()
    assert poll(db, task_id, pool) == 1                                         # 第 1 次：queued
    root, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert (root.status, root.poll_count, asset.status) == ("polling", 1, "submitted")
    assert 8 <= seconds_until(root.next_poll_at) <= 11                          # intervals[1] = 10
    assert gw.task_meta(root)["poll"]["last_http_status"] == 200
    poll(db, task_id, pool)                                                     # 第 2 次：in_progress 50
    root, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert (asset.status, asset.progress, root.progress, root.poll_count) == ("generating", 50, 50, 2)
    assert 13 <= seconds_until(root.next_poll_at) <= 16                         # intervals[2] = 15
    poll(db, task_id, pool)                                                     # 第 3 次：succeeded → 立即转存
    assert pool.calls[-1] == "transfer_asset"
    root, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert root.status == "succeeded" and root.finished_at is not None
    assert asset.status == "ready" and asset.upstream_url.endswith("/media/mock/placeholder.png")
    assert asset.url == f"{settings.public_base_url.rstrip('/')}/media/{asset.storage_key}"
    assert asset.url.startswith(f"{settings.public_base_url.rstrip('/')}/media/media/images/") and asset.storage_key.endswith(".png")
    raw = PLACEHOLDER_PNG.read_bytes()
    stored = settings.local_storage_path / asset.storage_key
    assert stored.read_bytes() == raw
    assert (asset.width, asset.height, asset.mime_type, asset.size_bytes) == (64, 36, "image/png", len(raw))
    assert asset.file_hash == hashlib.sha256(raw).hexdigest()
    assert (asset.thumbnail_key, asset.thumbnail_url) == (asset.storage_key, asset.url)
    assert asset.progress == 100 and asset.ready_at is not None and asset.failed_at is None and asset.next_transfer_at is None
    download = gw.task_meta(root)["download"]
    assert download["source"] == "mock" and download["request_id"].startswith("mock-") and download["http_status"] == 200
    assert download["request_ids"] == [download["request_id"]]
    assert rt(db, env.project.id, "images_generated") == 1 and rt(db, 0, "images_generated") == 1
    assert not list((settings.local_storage_path / "tmp").glob("*.part"))
    # 转存后的文件经 GET /media/{key} 可访问
    response = client.get(f"/media/{asset.storage_key}")
    assert response.status_code == 200 and response.content == raw
    # 任务摘要与详情
    summary = media_service.get_asset_task(db, env.scope, asset_id)
    assert summary == {
        "task_id": task_id, "operation": "image_generate", "status": "succeeded", "progress": 100, "error_category": None,
        "error_message": None, "model_override": None, "finished_at": summary["finished_at"],
    }
    detail = media_service.get_asset_detail(db, env.scope, asset_id)
    assert detail["task"]["status"] == "succeeded" and detail["references"] == {
        "cover_of": None, "bound_content_id": None, "referenced_by_asset_ids": [], "count": 0,
    }


def test_poll_intervals_and_budget_helpers() -> None:
    image = {"image": {"poll_intervals_seconds": [5, 10, 15, 30], "poll_budget_seconds": 600}}
    assert [media_service.next_poll_delay(image, "image", k) for k in range(6)] == [5, 10, 15, 30, 30, 30]
    video = {"video": {"poll_intervals_seconds": [15, 30, 60], "poll_budget_seconds": 1200}}
    assert [media_service.next_poll_delay(video, "video", k) for k in range(5)] == [15, 30, 60, 60, 60]
    assert media_service.poll_budget(video, "video") == 1200 and media_service.poll_budget(image, "image") == 600


# =====================================================================
# 2. 视频全链路（Mock：第 5 次轮询成功，progress=99 不做特殊处理）
# =====================================================================


def test_video_lifecycle_mock(env: SimpleNamespace, db: Session) -> None:
    data = gen_video(db, env, negative_prompt="模糊、水印")
    assert set(data) == {"asset_id", "task_id"}
    asset_id, task_id = data["asset_id"], data["task_id"]
    asset = asset_of(db, asset_id)
    assert json.loads(asset.params_json) == {
        "duration": 5, "resolution": "720p", "aspect_ratio": "16:9", "size": None, "input_reference": None,
        "reference_image_urls": [], "reference_video_urls": [], "reference_audio_urls": [], "first_frame_image_url": None,
        "last_frame_image_url": None, "generate_audio": False,
    }
    assert asset.negative_prompt == "模糊、水印" and daily_used(db, "video") == 1
    run_queue(db)
    root = task_of(db, task_id)
    assert root.status == "polling" and root.upstream_task_id.startswith("vidtask_mock_") and root.protocol == "video"
    assert 13 <= seconds_until(root.next_poll_at) <= 16 and 1195 <= seconds_until(root.deadline_at) <= 1201
    payload = json.loads(db.scalars(select(AiTask).where(AiTask.root_task_id == task_id)).one().request_payload_json)
    assert payload["negative_prompt"] == "模糊、水印" and "size" not in payload and payload["n"] == 1

    pool = SyncPool()
    expected = [("submitted", 0), ("generating", 25), ("generating", 60), ("generating", 99)]
    delays = [30, 60, 60, 60]
    for (status, progress), delay in zip(expected, delays, strict=True):
        poll(db, task_id, pool)
        asset, root = asset_of(db, asset_id), task_of(db, task_id)
        assert (asset.status, asset.progress, root.status) == (status, progress, "polling")
        assert delay - 2 <= seconds_until(root.next_poll_at) <= delay + 1
    poll(db, task_id, pool)                                                     # 第 5 次：succeeded
    asset, root = asset_of(db, asset_id), task_of(db, task_id)
    assert root.status == "succeeded" and root.poll_count == 5
    assert asset.status == "ready" and asset.storage_key.startswith("media/videos/") and asset.storage_key.endswith(".mp4")
    assert asset.mime_type == "video/mp4" and asset.size_bytes == PLACEHOLDER_MP4.stat().st_size
    assert (asset.width, asset.height, asset.duration_seconds, asset.thumbnail_key, asset.thumbnail_url) == (None, None, None, None, None)
    assert (settings.local_storage_path / asset.storage_key).read_bytes() == PLACEHOLDER_MP4.read_bytes()
    assert rt(db, env.project.id, "videos_generated") == 1


def test_video_content_fallback_within_one_attempt(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_video(db, env)
    run_queue(db)
    for _ in range(5):
        poll(db, data["task_id"])
    assert asset_of(db, data["asset_id"]).status == "downloading"
    original = MockZhiqiClient.stream_download

    def flaky(self: MockZhiqiClient, url_or_path: str, dest: Any, **kw: Any) -> Any:
        if "/media/mock/" in url_or_path:
            err = ZhiqiError(ErrorCategory.TRANSFER_FAILED, "下载失败：HTTP 403", http_status=403, request_id="origin-req-1")
            err.retry_request_ids = ["origin-req-1"]
            raise err
        return original(self, url_or_path, dest, **kw)

    monkeypatch.setattr(MockZhiqiClient, "stream_download", flaky)
    assert media_service.transfer_asset(data["asset_id"])                       # upstream_url 失败 → 同一次尝试内回退 /content
    asset, root = asset_of(db, data["asset_id"]), task_of(db, data["task_id"])
    assert asset.status == "ready" and asset.transfer_attempts == 0
    download = gw.task_meta(root)["download"]
    assert download["source"] == "mock" and download["http_status"] == 200      # Mock 模式下 /content 同样复制占位文件
    assert download["request_ids"][0] == "origin-req-1" and download["request_ids"][-1] == download["request_id"]


# =====================================================================
# 3. 封面联动与内容绑定
# =====================================================================


def test_cover_binding_and_sort(env: SimpleNamespace, db: Session) -> None:
    content = env.content
    inline = gen_image(db, env, usage_type="inline", content_id=content.id)
    data = gen_image(db, env, usage_type="cover", content_id=content.id, count=2)
    first, second = data["asset_ids"]
    assert [asset_of(db, a).sort for a in (inline["asset_ids"][0], first, second)] == [1, 2, 3]
    run_queue(db)
    for task_id in data["task_ids"]:
        for _ in range(3):
            poll(db, task_id)
    assert media_service.transfer_asset(first)
    db.expire_all()
    assert db.get(Content, content.id).cover_asset_id == first
    assert media_service.transfer_asset(second)
    db.expire_all()
    assert db.get(Content, content.id).cover_asset_id == second                 # 最后 ready 的成为封面
    assert asset_of(db, first).usage_type == "inline" and asset_of(db, first).content_id == content.id
    assert asset_of(db, second).usage_type == "cover"
    refs = media_service.get_asset_detail(db, env.scope, second)["references"]
    assert refs == {"cover_of": content.id, "bound_content_id": content.id, "referenced_by_asset_ids": [], "count": 2}


def test_on_asset_ready_only_images_set_cover(env: SimpleNamespace, db: Session) -> None:
    video = MediaAsset(project_id=env.project.id, content_id=env.content.id, kind="video", usage_type="cover", status="ready",
                       created_by=env.owner.id)
    db.add(video)
    db.commit()
    media_service.on_asset_ready(db, video)
    db.commit()
    db.expire_all()
    assert db.get(Content, env.content.id).cover_asset_id is None


# =====================================================================
# 4. 内嵌 image_prompt（from_content_prompt）
# =====================================================================


def test_from_content_prompt_runs_embedded_image_prompt(env: SimpleNamespace, db: Session) -> None:
    data = gen_image(db, env, usage_type="cover", content_id=env.content.id, from_content_prompt=True, prompt=None)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    assert asset_of(db, asset_id).prompt is None
    assert gw.task_input(task_of(db, task_id))["from_content_prompt"] is True
    run_queue(db)
    asset = asset_of(db, asset_id)
    assert asset.prompt == zhiqi_mock.IMAGE_PROMPT_TEXT and "\n" not in asset.prompt
    assert asset.status == "submitted" and task_of(db, task_id).status == "polling"
    prompt_root = db.scalars(select(AiTask).where(AiTask.operation == "image_prompt", AiTask.root_task_id.is_(None))).one()
    assert (prompt_root.status, prompt_root.capability, prompt_root.trigger_type) == ("succeeded", "content", "system")
    assert (prompt_root.target_type, prompt_root.target_id, prompt_root.project_id) == ("content", env.content.id, env.project.id)
    assert prompt_root.created_by == env.owner.id and prompt_root.output_excerpt == asset.prompt
    assert gw.task_input(prompt_root) == {"asset_id": asset_id, "usage_type": "cover"}
    assert prompt_root.template_id is not None and prompt_root.quota_reserved == 0
    text_attempt = db.scalars(select(AiTask).where(AiTask.root_task_id == prompt_root.id)).one()
    assert text_attempt.capability == "content" and text_attempt.status == "succeeded"
    submit = json.loads(db.scalars(select(AiTask).where(AiTask.root_task_id == task_id)).one().request_payload_json)
    assert submit["prompt"] == asset.prompt


def test_image_prompt_pause_rolls_back_host(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def quota_exceeded(*_args: Any, **_kw: Any) -> Any:
        raise ZhiqiError(ErrorCategory.QUOTA_EXCEEDED, "insufficient quota", http_status=402, request_id="req-402")

    monkeypatch.setattr(gw.text, "complete", quota_exceeded)
    data = gen_image(db, env, usage_type="inline", content_id=env.content.id, from_content_prompt=True)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    run_queue(db)
    host, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert (host.status, host.pause_count, host.locked_by, host.started_at) == ("queued", 1, None, None)
    assert asset.status == "pending" and asset.prompt is None
    prompt_root = db.scalars(select(AiTask).where(AiTask.operation == "image_prompt", AiTask.root_task_id.is_(None))).one()
    assert (prompt_root.status, prompt_root.error_category) == ("failed", "quota_exceeded")
    assert redis_client.exists("ai:paused:quota_exceeded") and media_alerts(db) == []
    # 暂停解除后再次执行：新建 image_prompt 根任务；pause_count 达 3 才按常规 failed
    redis_client.delete("ai:paused:quota_exceeded")
    db.execute(update(AiTask).where(AiTask.id == task_id).values(pause_count=2))
    db.commit()
    gw.enqueue_task(task_id)
    run_queue(db)
    host, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert (host.status, host.error_category, host.pause_count) == ("failed", "quota_exceeded", 3)
    assert (asset.status, asset.error_category) == ("failed", "quota_exceeded") and asset.failed_at is not None
    latest = db.scalar(select(AiTask.id).where(AiTask.operation == "image_prompt", AiTask.root_task_id.is_(None)).order_by(AiTask.id.desc()).limit(1))
    assert latest != prompt_root.id
    alerts = media_alerts(db)
    assert len(alerts) == 1 and alerts[0].project_id == env.project.id and alerts[0].target_id == asset_id


def test_image_prompt_banned_words_blocks_image(env: SimpleNamespace, db: Session) -> None:
    data = gen_image(db, env, usage_type="inline", content_id=env.content.id, from_content_prompt=True)
    settings_service.set_value(db, "generation_config", {"quality": {"banned_words": ["Editorial"]}})
    run_queue(db)
    asset, host = asset_of(db, data["asset_ids"][0]), task_of(db, data["task_ids"][0])
    assert (host.status, host.error_category) == ("failed", "content_blocked")
    assert host.error_message == "命中敏感词：Editorial"
    assert (asset.status, asset.error_category) == ("failed", "content_blocked")
    prompt_root = db.scalars(select(AiTask).where(AiTask.operation == "image_prompt", AiTask.root_task_id.is_(None))).one()
    assert prompt_root.status == "succeeded"                                    # 文本已产出、正常计费


# =====================================================================
# 5. 同步回退
# =====================================================================


def test_sync_fallback_on_route_missing(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_args: Any, **_kw: Any) -> Any:
        raise ZhiqiError(ErrorCategory.ROUTE_MISSING, "Invalid URL", http_status=404, request_id="req-404")

    monkeypatch.setattr(gw.images, "submit_async", missing)
    data = gen_image(db, env)
    run_queue(db)
    root, asset = task_of(db, data["task_ids"][0]), asset_of(db, data["asset_ids"][0])
    assert root.status == "succeeded" and root.protocol == "image_sync"
    assert asset.status == "downloading" and asset.upstream_url.endswith("/media/mock/placeholder.png")
    assert asset.next_transfer_at is not None and asset.upstream_task_id is None
    attempts = db.scalars(select(AiTask).where(AiTask.root_task_id == root.id).order_by(AiTask.id)).all()
    assert [(a.protocol, a.status) for a in attempts] == [("image_async", "failed"), ("image_sync", "succeeded")]
    pool = SyncPool()
    assert transfer_media.retry_due(pool) == 1 and pool.calls == ["transfer_asset"]
    assert asset_of(db, asset.id).status == "ready"


CDN_URL = "https://cdn.upstream.example/out/5f1c2a.png"


def test_upstream_url_not_exposed_via_ai_tasks(
    env: SimpleNamespace, db: Session, client: TestClient, super_admin: User, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/10 §2.2、§13 第 1 条：上游临时 URL 不出现在任何前端展示中——AI 任务列表 / 详情（含尝试行与 ``response_meta``）
    对媒体任务返回 ``output_excerpt=null`` 且不含 ``urls``；库中 ``output_excerpt`` 仍保留供审计（docs/03）。"""
    real_status = zhiqi_mock.mock_image_status

    def cdn_status(task_id: str) -> dict[str, Any]:
        body = real_status(task_id)
        if body.get("status") == "succeeded":
            body["data"] = [{"url": CDN_URL}]
        return body

    monkeypatch.setattr(zhiqi_mock, "mock_image_status", cdn_status)
    data = gen_image(db, env)
    run_queue(db)
    root_id = data["task_ids"][0]
    for _ in range(3):
        poll(db, root_id)
    root, asset = task_of(db, root_id), asset_of(db, data["asset_ids"][0])
    assert root.status == "succeeded" and asset.upstream_url == CDN_URL and root.output_excerpt == CDN_URL
    headers = super_admin.headers
    detail = ok_data(client.get(f"{ADMIN_API}/ai/tasks/{root_id}", headers=headers))
    assert detail["output_excerpt"] is None and all(a["output_excerpt"] is None for a in detail["attempts"])
    rows = ok_data(client.get(f"{ADMIN_API}/ai/tasks", params={"capability": "image", "row_kind": "all"}, headers=headers))["items"]
    assert rows and all(r["output_excerpt"] is None for r in rows)
    assert CDN_URL not in json.dumps([detail, rows], ensure_ascii=False)

    # 同步回退：成功尝试行记录的 urls[] / output_excerpt 同样不对外返回
    def missing(*_args: Any, **_kw: Any) -> Any:
        raise ZhiqiError(ErrorCategory.ROUTE_MISSING, "Invalid URL", http_status=404, request_id="req-404")

    monkeypatch.setattr(gw.images, "submit_async", missing)
    sync = gen_image(db, env)
    run_queue(db)
    sync_root = task_of(db, sync["task_ids"][0])
    attempt = db.scalar(select(AiTask).where(AiTask.root_task_id == sync_root.id, AiTask.status == "succeeded"))
    upstream = asset_of(db, sync["asset_ids"][0]).upstream_url
    assert attempt is not None and attempt.output_excerpt == upstream and "urls" in gw.task_meta(attempt)
    detail = ok_data(client.get(f"{ADMIN_API}/ai/tasks/{sync_root.id}", headers=headers))
    attempt_detail = ok_data(client.get(f"{ADMIN_API}/ai/tasks/{attempt.id}", headers=headers))
    assert attempt_detail["output_excerpt"] is None and "urls" not in (attempt_detail["response_meta"] or {})
    assert attempt_detail["response_meta"]["http_status"] == 200
    assert upstream not in json.dumps([detail, attempt_detail], ensure_ascii=False)
    # 文本任务的 output_excerpt 不受影响（内嵌 image_prompt 等非媒体能力照常返回）
    text_rows = ok_data(client.get(f"{ADMIN_API}/ai/tasks", params={"capability": "content", "row_kind": "all"}, headers=headers))["items"]
    assert all(r["output_excerpt"] for r in text_rows if r["status"] == "succeeded")


# =====================================================================
# 6. 轮询阶段失败、备选回退、过期、404
# =====================================================================


def _failing_status(error_code: str, message: str) -> Any:
    def status(task_id: str) -> dict[str, Any]:
        return {"id": task_id, "status": "failed", "error_code": error_code, "error_message": message}

    return status


def test_poll_failed_content_blocked_and_unknown(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_image(db, env, count=2)
    run_queue(db)
    monkeypatch.setattr(zhiqi_mock, "mock_image_status", _failing_status("content_policy_violation", "blocked by moderation"))
    poll(db, data["task_ids"][0])
    monkeypatch.setattr(zhiqi_mock, "mock_image_status", _failing_status("internal", "something odd"))
    poll(db, data["task_ids"][1])
    blocked, unknown = (asset_of(db, a) for a in data["asset_ids"])
    assert (blocked.status, blocked.error_category, task_of(db, data["task_ids"][0]).status) == ("failed", "content_blocked", "failed")
    assert (unknown.status, unknown.error_category) == ("failed", "unknown") and unknown.failed_at is not None
    alerts = media_alerts(db)
    assert [a.target_id for a in alerts] == data["asset_ids"]
    assert all(a.project_id == env.project.id and a.target_type == "media_asset" and a.severity == "info" for a in alerts)
    payload = json.loads(alerts[0].payload_json)
    assert payload["error_category"] == "content_blocked" and payload["model"] == "mock-image"
    assert payload["upstream_task_id"].startswith("task_mock_") and payload["request_id"].startswith("mock-")
    assert rt(db, env.project.id, "media_failed") == 2


def test_media_storage_fallback_reuses_asset(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    route = db.scalar(select(CapabilityRoute).where(CapabilityRoute.capability == "image", CapabilityRoute.project_id == 0))
    route.fallback_models_json = json.dumps(["mock-image-b"])
    db.commit()
    gw.invalidate_routes_cache()
    data = gen_image(db, env)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    run_queue(db)
    redis_client.delete(gw.QUEUE_AI_TASKS)
    monkeypatch.setattr(zhiqi_mock, "mock_image_status", _failing_status("media_storage_upload_failed", "upload failed"))
    poll(db, task_id)
    old, asset = task_of(db, task_id), asset_of(db, asset_id)
    assert (old.status, old.error_category) == ("failed", "media_storage")
    assert asset.ai_task_id != task_id and asset.status == "pending" and asset.progress == 0 and asset.transfer_attempts == 0
    assert asset.error_category is None and asset.failed_at is None and asset.upstream_task_id is None
    new = task_of(db, asset.ai_task_id)
    assert (new.status, new.trigger_type, new.parent_task_id, new.candidate_index) == ("queued", "system", task_id, 1)
    assert (new.target_type, new.target_id, new.project_id, new.model) == ("media_asset", asset_id, env.project.id, "mock-image-b")
    assert gw.task_input(new) == gw.task_input(old)
    assert redis_client.lrange(gw.QUEUE_AI_TASKS, 0, -1) == [str(new.id)]
    assert daily_used(db) == 1 and media_alerts(db) == []                       # 不重复计日上限、不告警
    assert media_service.get_asset_task(db, env.scope, asset_id)["task_id"] == new.id
    # 新根任务从下一候选提交；再次 media_storage 时已无备选 → failed(media_storage) + 告警
    run_queue(db)
    new = task_of(db, new.id)
    assert new.status == "polling" and new.model == "mock-image-b"
    poll(db, new.id)
    asset = asset_of(db, asset_id)
    assert (asset.status, asset.error_category, asset.ai_task_id) == ("failed", "media_storage", new.id)
    assert len(media_alerts(db)) == 1


def test_expired_by_deadline_and_upstream(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_video(db, env)
    image = gen_image(db, env)
    run_queue(db)
    # 本地预算耗尽：当次轮询仍为 in_progress 时判定 expired
    db.execute(update(AiTask).where(AiTask.id == data["task_id"]).values(deadline_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    poll(db, data["task_id"])
    video = asset_of(db, data["asset_id"])
    assert task_of(db, data["task_id"]).status == "expired"
    assert (video.status, video.error_category) == ("expired", "timeout") and video.failed_at is not None
    # 上游 expired
    monkeypatch.setattr(zhiqi_mock, "mock_image_status", lambda task_id: {"id": task_id, "status": "expired"})
    poll(db, image["task_ids"][0])
    assert task_of(db, image["task_ids"][0]).status == "expired" and asset_of(db, image["asset_ids"][0]).status == "expired"
    assert len(media_alerts(db)) == 2 and rt(db, env.project.id, "media_failed") == 2


def _breaker_alerts(db: Session) -> list[Alert]:
    db.expire_all()
    return list(db.scalars(select(Alert).where(Alert.alert_type == "ai_breaker_open", Alert.status == "open")).all())


def test_media_storage_counts_in_breaker(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """docs/08 §8.4 / §9.2：轮询 failed(media_storage) 计入 (capability, model) 熔断，达阈值 open + ai_breaker_open 告警。"""
    settings_service.set_value(db, "ai_routing_config", {"breaker": {"failure_threshold": 1}})
    data = gen_image(db, env)
    run_queue(db)
    monkeypatch.setattr(zhiqi_mock, "mock_image_status", _failing_status("media_storage_upload_failed", "upload failed"))
    poll(db, data["task_ids"][0])
    assert task_of(db, data["task_ids"][0]).error_category == "media_storage"
    cfg = gw._cfg(db)  # noqa: SLF001
    assert gw.get_breaker(cfg).state("image", "mock-image") == "open"
    state = redis_client.hgetall("ai:breaker:image:mock-image")                 # open 时失败窗口清空，计数留在 Hash
    assert (state["state"], state["reason"], int(state["failures"])) == ("open", "failures", 1)
    alerts = _breaker_alerts(db)
    assert [(a.target_type, a.target_key) for a in alerts] == [("ai_model", "image:mock-image")]
    assert json.loads(alerts[0].payload_json)["error_category"] == "media_storage"


def test_unknown_poll_failure_not_counted_in_breaker(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_service.set_value(db, "ai_routing_config", {"breaker": {"failure_threshold": 1}})
    data = gen_image(db, env)
    run_queue(db)
    monkeypatch.setattr(zhiqi_mock, "mock_image_status", _failing_status("internal_error", "something broke"))
    poll(db, data["task_ids"][0])
    assert task_of(db, data["task_ids"][0]).error_category == "unknown"
    assert gw.get_breaker(gw._cfg(db)).state("image", "mock-image") == "closed"  # noqa: SLF001
    assert _breaker_alerts(db) == []


def test_poll_budget_expiry_counts_timeout_in_breaker(env: SimpleNamespace, db: Session) -> None:
    """docs/08 §9.2 timeout 行「或轮询超出预算」计入熔断：轮询判定与 recover_stale_tasks ② 两条过期路径都计一次。"""
    settings_service.set_value(db, "ai_routing_config", {"breaker": {"failure_threshold": 2}})
    first, second = gen_video(db, env), gen_video(db, env)
    run_queue(db)
    db.execute(update(AiTask).where(AiTask.id == first["task_id"]).values(deadline_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    poll(db, first["task_id"])
    assert task_of(db, first["task_id"]).status == "expired"
    breaker = gw.get_breaker(gw._cfg(db))  # noqa: SLF001
    assert redis_client.llen("ai:breaker:failures:video:mock-video") == 1 and breaker.state("video", "mock-video") == "closed"
    expire_root(db, second["task_id"])
    assert breaker.state("video", "mock-video") == "open"
    alerts = _breaker_alerts(db)
    assert [a.target_key for a in alerts] == ["video:mock-video"]
    assert json.loads(alerts[0].payload_json)["error_category"] == "timeout"


def test_three_consecutive_404_fail_route_missing(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_image(db, env)
    run_queue(db)
    task_id = data["task_ids"][0]

    def gone(task_id: str) -> dict[str, Any]:
        raise MockHTTPError(404, {"error": {"message": f"task {task_id} not found", "code": "task_not_found"}})

    monkeypatch.setattr(zhiqi_mock, "mock_image_status", gone)
    for expected in (1, 2):
        poll(db, task_id)
        root = task_of(db, task_id)
        assert root.status == "polling" and gw.task_meta(root)["poll"]["consecutive_404"] == expected
    poll(db, task_id)
    root, asset = task_of(db, task_id), asset_of(db, data["asset_ids"][0])
    assert (root.status, root.error_category) == ("failed", "route_missing")
    assert (asset.status, asset.error_category) == ("failed", "route_missing")


def test_poll_auth_failed_keeps_polling(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_image(db, env)
    run_queue(db)

    def unauthorized(task_id: str) -> dict[str, Any]:
        raise MockHTTPError(401, {"error": {"message": "invalid api key", "code": "invalid_api_key"}})

    monkeypatch.setattr(zhiqi_mock, "mock_image_status", unauthorized)
    poll(db, data["task_ids"][0])
    root = task_of(db, data["task_ids"][0])
    assert root.status == "polling" and redis_client.exists("ai:paused:auth_failed")
    assert 8 <= seconds_until(root.next_poll_at) <= 11 and asset_of(db, data["asset_ids"][0]).status == "submitted"


def test_poll_due_claims_once(env: SimpleNamespace, db: Session) -> None:
    data = gen_image(db, env)
    run_queue(db)
    task_id = data["task_ids"][0]
    db.execute(update(AiTask).where(AiTask.id == task_id).values(next_poll_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert media_service.claim_due_polls(db) == [task_id]
    assert media_service.claim_due_polls(db) == []                              # 已被抢占（next_poll_at = now + 60s）
    assert 55 <= seconds_until(task_of(db, task_id).next_poll_at) <= 61


# =====================================================================
# 7. 转存失败重试与人工转存
# =====================================================================


def _downloading_image(env: SimpleNamespace, db: Session) -> tuple[int, int]:
    data = gen_image(db, env)
    run_queue(db)
    for _ in range(3):
        poll(db, data["task_ids"][0])
    assert asset_of(db, data["asset_ids"][0]).status == "downloading"
    return data["asset_ids"][0], data["task_ids"][0]


def test_transfer_retries_then_failed_and_manual_transfer(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id, task_id = _downloading_image(env, db)
    original = MockZhiqiClient.stream_download
    calls = {"n": 0}

    def broken(self: MockZhiqiClient, url_or_path: str, dest: Any, **kw: Any) -> Any:
        calls["n"] += 1
        err = ZhiqiError(ErrorCategory.TRANSFER_FAILED, "下载失败：HTTP 500", http_status=500, request_id=f"dl-{calls['n']}")
        err.retry_request_ids = [f"dl-{calls['n']}"]
        raise err

    monkeypatch.setattr(MockZhiqiClient, "stream_download", broken)
    assert not media_service.transfer_asset(asset_id)
    asset = asset_of(db, asset_id)
    assert (asset.status, asset.transfer_attempts) == ("downloading", 1) and 28 <= seconds_until(asset.next_transfer_at) <= 31
    db.execute(update(MediaAsset).where(MediaAsset.id == asset_id).values(next_transfer_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert transfer_media.retry_due(SyncPool()) == 1
    asset = asset_of(db, asset_id)
    assert (asset.status, asset.transfer_attempts) == ("downloading", 2) and 118 <= seconds_until(asset.next_transfer_at) <= 121
    assert not media_service.transfer_asset(asset_id)                           # 第 3 次 → failed(transfer_failed)
    asset, root = asset_of(db, asset_id), task_of(db, task_id)
    assert (asset.status, asset.error_category, asset.transfer_attempts) == ("failed", "transfer_failed", 3)
    assert asset.next_transfer_at is None and asset.failed_at is not None and asset.upstream_url
    assert asset.error_message.endswith("（download_request_id=dl-3）")
    assert root.status == "succeeded" and root.error_category is None          # 根任务保持 succeeded
    download = gw.task_meta(root)["download"]
    assert download == {"source": "mock", "request_id": "dl-3", "http_status": 500, "request_ids": ["dl-1", "dl-2", "dl-3"]}
    alerts = media_alerts(db)
    assert len(alerts) == 1 and json.loads(alerts[0].payload_json)["download_request_id"] == "dl-3"
    assert rt(db, env.project.id, "media_failed") == 1
    assert not list((settings.local_storage_path / "tmp").glob("*.part"))

    # 人工转存：transfer_attempts 清零、failed_at 清空、next_transfer_at=now；不新建根任务
    monkeypatch.setattr(MockZhiqiClient, "stream_download", original)
    result = media_service.request_transfer(db, env.scope, asset_id)
    assert result["asset"]["status"] == "downloading" and result["asset"]["transfer_attempts"] == 0
    asset = asset_of(db, asset_id)
    assert asset.failed_at is None and asset.ai_task_id == task_id and seconds_until(asset.next_transfer_at) <= 1
    assert transfer_media.retry_due(SyncPool()) == 1
    assert asset_of(db, asset_id).status == "ready"
    with pytest.raises(BusinessError) as exc:
        media_service.request_transfer(db, env.scope, asset_id)
    assert exc.value.code == 409 and exc.value.data == {"current_status": "ready"}


def test_transfer_rejects_mismatched_type_and_lock(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    asset_id, _ = _downloading_image(env, db)
    redis_client.set(f"lock:media:transfer:{asset_id}", "other", ex=600)
    assert not media_service.transfer_asset(asset_id)                           # 另一副本在转存
    assert asset_of(db, asset_id).transfer_attempts == 0
    redis_client.delete(f"lock:media:transfer:{asset_id}")
    def video_bytes(self: MockZhiqiClient, url_or_path: str, dest: Any, **kw: Any) -> Any:
        dest.write(PLACEHOLDER_MP4.read_bytes())
        from app.core.zhiqi.types import DownloadResult

        return DownloadResult(source="mock", request_id="mock-x", http_status=200, request_ids=["mock-x"])

    db.execute(update(MediaAsset).where(MediaAsset.id == asset_id).values(status="downloading", transfer_attempts=0))
    db.commit()
    monkeypatch.setattr(MockZhiqiClient, "stream_download", video_bytes)
    assert not media_service.transfer_asset(asset_id)
    asset = asset_of(db, asset_id)
    assert asset.status == "downloading" and asset.transfer_attempts == 1 and "与素材类型 image 不符" in asset.error_message


# =====================================================================
# 8. 人工重试（docs/10 §4.10）
# =====================================================================


def test_retry_expired_rechecks_succeeded_upstream(env: SimpleNamespace, db: Session) -> None:
    data = gen_image(db, env)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    run_queue(db)
    poll(db, task_id)
    poll(db, task_id)
    expire_root(db, task_id)
    assert asset_of(db, asset_id).status == "expired"
    result = media_service.retry_asset(db, env.scope, asset_id, admin_id=env.owner.id)   # 复查：第 3 次 GET → succeeded
    assert result["resumed"] is True and result["asset"]["status"] == "downloading"
    new = task_of(db, result["task_id"])
    assert (new.status, new.parent_task_id, new.trigger_type, new.created_by) == ("succeeded", task_id, "user", env.owner.id)
    assert new.upstream_task_id == task_of(db, task_id).upstream_task_id and new.project_id == env.project.id
    assert gw.task_input(new) == gw.task_input(task_of(db, task_id))
    asset = asset_of(db, asset_id)
    assert asset.ai_task_id == new.id and asset.failed_at is None and asset.error_category is None and asset.transfer_attempts == 0
    assert asset.upstream_url.endswith("/media/mock/placeholder.png") and seconds_until(asset.next_transfer_at) <= 1
    assert daily_used(db) == 1                                                  # 复查分支不计日上限
    assert task_of(db, task_id).status == "expired"                              # 旧根任务保持终态
    assert transfer_media.retry_due(SyncPool()) == 1 and asset_of(db, asset_id).status == "ready"


def test_retry_expired_video_still_in_progress(env: SimpleNamespace, db: Session) -> None:
    data = gen_video(db, env)
    run_queue(db)
    poll(db, data["task_id"])                                                    # queued
    expire_root(db, data["task_id"])
    result = media_service.retry_asset(db, env.scope, data["asset_id"], admin_id=env.owner.id)  # 第 2 次 GET：in_progress 25
    assert result["resumed"] is True
    new, asset = task_of(db, result["task_id"]), asset_of(db, data["asset_id"])
    assert new.status == "polling" and new.upstream_task_id == asset.upstream_task_id and new.progress == 25
    assert 1195 <= seconds_until(new.deadline_at) <= 1201 and seconds_until(new.next_poll_at) <= 1
    assert (asset.status, asset.progress, asset.ai_task_id) == ("generating", 25, new.id)
    assert daily_used(db, "video") == 1


def test_retry_resubmits_on_404_and_non_timeout(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_image(db, env)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    run_queue(db)
    expire_root(db, task_id)
    redis_client.delete(zhiqi_mock.TASK_KEY_PREFIX + task_of(db, task_id).upstream_task_id)  # 上游任务已失效 → 404
    redis_client.delete(gw.QUEUE_AI_TASKS)
    result = media_service.retry_asset(db, env.scope, asset_id, admin_id=env.owner.id)
    assert result["resumed"] is False and result["asset"]["status"] == "pending"
    new, asset = task_of(db, result["task_id"]), asset_of(db, asset_id)
    assert (new.status, new.parent_task_id, new.trigger_type) == ("queued", task_id, "user") and new.quota_reserved > 0
    assert asset.upstream_task_id is None and asset.upstream_url is None and asset.ai_task_id == new.id
    assert redis_client.lrange(gw.QUEUE_AI_TASKS, 0, -1) == [str(new.id)] and daily_used(db) == 2
    # 非 failed / expired → 409
    with pytest.raises(BusinessError) as exc:
        media_service.retry_asset(db, env.scope, asset_id, admin_id=env.owner.id)
    assert exc.value.code == 409 and exc.value.data == {"current_status": "pending"}
    # failed(transfer_failed)：一律重新提交（即使有 upstream_url）
    asset.status, asset.error_category, asset.upstream_url = "failed", "transfer_failed", "https://cdn.test/x.png"
    asset.upstream_task_id = "task_old"
    db.commit()
    result = media_service.retry_asset(db, env.scope, asset_id, admin_id=env.owner.id)
    assert result["resumed"] is False and daily_used(db) == 3


def test_retry_recheck_error_returns_5021(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    data = gen_image(db, env)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    run_queue(db)
    expire_root(db, task_id)

    def unavailable(*_args: Any, **_kw: Any) -> Any:
        raise ZhiqiError(ErrorCategory.UPSTREAM_UNAVAILABLE, "bad gateway", http_status=502, request_id="req-502")

    monkeypatch.setattr(gw.images, "get_generation", unavailable)
    with pytest.raises(BusinessError) as exc:
        media_service.retry_asset(db, env.scope, asset_id, admin_id=env.owner.id)
    assert exc.value.code == 5021 and exc.value.data == {"error_category": "upstream_unavailable", "request_id": "req-502"}
    asset = asset_of(db, asset_id)
    assert asset.status == "expired" and asset.ai_task_id == task_id
    assert db.scalar(select(AiTask.id).where(AiTask.parent_task_id == task_id)) is None


def test_retry_daily_limit_and_scope(env: SimpleNamespace, db: Session, users: UserFactory) -> None:
    data = gen_image(db, env)
    asset_id = data["asset_ids"][0]
    asset = asset_of(db, asset_id)
    asset.status, asset.error_category = "failed", "cancelled"
    db.commit()
    other = users.create("operator", username="media_other")
    with pytest.raises(BusinessError) as exc:
        media_service.retry_asset(db, DataScope(other.id, "own", other.id), asset_id, admin_id=other.id)
    assert exc.value.code == 404
    settings_service.set_value(db, "media_config", {"daily_limits": {"images": 1}})
    with pytest.raises(BusinessError) as exc:
        media_service.retry_asset(db, env.scope, asset_id, admin_id=env.owner.id)
    assert exc.value.code == 4291 and exc.value.data == {"scope": "daily_images", "limit": 1, "used": 1}
    assert daily_used(db) == 1 and asset_of(db, asset_id).status == "failed"


# =====================================================================
# 9. 取消
# =====================================================================


def test_cancel_polling_marks_asset_cancelled(env: SimpleNamespace, db: Session) -> None:
    data = gen_image(db, env, count=2)
    run_queue(db)
    for task_id in data["task_ids"]:
        ai_task_service.cancel_task(db, env.scope, task_id, admin_id=env.owner.id)
    for asset_id in data["asset_ids"]:
        asset = asset_of(db, asset_id)
        assert (asset.status, asset.error_category) == ("failed", "cancelled") and asset.failed_at is not None
    assert media_alerts(db) == [] and rt(db, env.project.id, "media_failed") == 0
    assert poll(db, data["task_ids"][0]) == 0                                    # 已取消的根任务不再轮询
    with pytest.raises(BusinessError) as exc:
        ai_task_service.retry_task(db, env.scope, data["task_ids"][0], admin_id=env.owner.id)
    assert exc.value.data == {"hint": f"POST /admin/media/assets/{data['asset_ids'][0]}/retry"}


# =====================================================================
# 10. 删除
# =====================================================================


def test_delete_rules(env: SimpleNamespace, db: Session) -> None:
    pending = gen_image(db, env)["asset_ids"][0]
    with pytest.raises(BusinessError) as exc:
        media_service.delete_asset(db, env.scope, pending)
    assert exc.value.code == 409 and exc.value.data == {"current_status": "pending"}
    redis_client.delete(gw.QUEUE_AI_TASKS)
    cover = ready_image(db, env, usage_type="cover", content_id=env.content.id)
    db.expire_all()
    assert db.get(Content, env.content.id).cover_asset_id == cover.id
    path = settings.local_storage_path / cover.storage_key
    assert path.exists()
    media_service.delete_asset(db, env.scope, cover.id)
    asset = asset_of(db, cover.id)
    assert asset.status == "deleted" and asset.content_id is None and asset.url and asset.storage_key and asset.ready_at
    assert db.get(Content, env.content.id).cover_asset_id is None and not path.exists()
    with pytest.raises(BusinessError) as exc:
        media_service.delete_asset(db, env.scope, cover.id)
    assert exc.value.data == {"current_status": "deleted"}


def test_delete_reference_in_use(env: SimpleNamespace, db: Session) -> None:
    upload = make_upload(db, env.owner)
    data = gen_image(db, env, reference_image_urls=[upload.url, "https://cdn.example.com/x.png"])
    generated = asset_of(db, data["asset_ids"][0])
    assert json.loads(generated.reference_asset_ids_json) == [upload.id]
    with pytest.raises(BusinessError) as exc:
        media_service.delete_asset(db, env.scope, upload.id)
    assert exc.value.code == 409 and exc.value.data == {"reason": "in_use"}
    run_queue(db)                                                               # submitted 仍受保护
    with pytest.raises(BusinessError):
        media_service.delete_asset(db, env.scope, upload.id)
    poll(db, data["task_ids"][0])
    poll(db, data["task_ids"][0])                                               # generating：不再受保护
    media_service.delete_asset(db, env.scope, upload.id)
    assert asset_of(db, upload.id).status == "deleted"


# =====================================================================
# 11. 列表 / 详情 / 引用与数据范围
# =====================================================================


def test_list_detail_and_reference_scope(env: SimpleNamespace, db: Session, users: UserFactory) -> None:
    upload = make_upload(db, env.owner)
    data = gen_image(db, env, reference_image_urls=[upload.url], model="mock-image")
    generated_id = data["asset_ids"][0]
    other = users.create("operator", username="media_other")
    other_project = Project(name="别人的项目", slug="other", owner_id=other.id, created_by=other.id)
    db.add(other_project)
    db.commit()
    other_scope = DataScope(other.id, "own", other.id)
    # 别人的资产引用了本人的上传素材：本人看不到该引用方
    foreign = MediaAsset(project_id=other_project.id, kind="image", status="pending", created_by=other.id,
                         reference_asset_ids_json=json.dumps([upload.id]))
    db.add(foreign)
    db.commit()
    detail = media_service.get_asset_detail(db, env.scope, upload.id)
    assert detail["references"] == {
        "cover_of": None, "bound_content_id": None, "referenced_by_asset_ids": [{"id": generated_id, "status": "pending"}], "count": 1,
    }
    full = media_service.get_asset_detail(db, SYSTEM_SCOPE, upload.id)["references"]
    assert full["count"] == 2 and [r["id"] for r in full["referenced_by_asset_ids"]] == [generated_id, foreign.id]
    gen_detail = media_service.get_asset_detail(db, env.scope, generated_id)
    assert gen_detail["reference_asset_ids"] == [upload.id] and gen_detail["task"]["model_override"] == "mock-image"
    assert media_service.get_asset_detail(db, other_scope, foreign.id)["reference_asset_ids"] == []   # 上传素材对他人不可见
    with pytest.raises(BusinessError) as exc:
        media_service.get_asset_detail(db, other_scope, upload.id)
    assert exc.value.code == 404
    items, total = media_service.list_assets(db, env.scope)
    assert total == 2 and [i["id"] for i in items] == [generated_id, upload.id]
    items, total = media_service.list_assets(db, env.scope, usage_type="reference")
    assert (total, items[0]["id"]) == (1, upload.id)
    items, total = media_service.list_assets(db, other_scope)
    assert total == 1 and items[0]["id"] == foreign.id
    assert media_service.list_assets(db, SYSTEM_SCOPE, page_size=1)[1] == 3
    assert media_service.get_asset_task(db, env.scope, upload.id) is None


# =====================================================================
# 12. 创建校验链
# =====================================================================


def test_generate_validation_errors(env: SimpleNamespace, db: Session) -> None:
    with pytest.raises(ValidationError):
        ImageGenerateBody(project_id=env.project.id, usage_type="standalone", prompt="x", count=5)
    with pytest.raises(ValidationError):
        ImageGenerateBody(project_id=env.project.id, usage_type="reference", prompt="x")
    with pytest.raises(ValidationError):
        VideoGenerateBody(project_id=env.project.id, usage_type="cover", prompt="x")

    def errors(**values: Any) -> list[dict[str, Any]]:
        with pytest.raises(BusinessError) as exc:
            gen_image(db, env, **values)
        assert exc.value.code == 400
        return exc.value.data

    assert errors(usage_type="cover")[0]["loc"] == ["body", "content_id"]
    assert errors(usage_type="standalone", from_content_prompt=True, prompt=None)[0]["loc"] == ["body", "from_content_prompt"]
    assert errors(usage_type="inline", from_content_prompt=True)[0]["loc"] == ["body", "content_id"]
    assert errors(usage_type="standalone", content_id=env.content.id)[0]["loc"] == ["body", "content_id"]
    assert errors(prompt=None)[0]["loc"] == ["body", "prompt"]
    assert errors(resolution="8k")[0]["loc"] == ["body", "resolution"]
    settings_service.set_value(db, "generation_config", {"quality": {"banned_words": ["违禁"]}})
    item = errors(prompt="这是违禁内容" + "x" * 300)[0]
    assert item["loc"] == ["body", "prompt"] and item["type"] == "banned_words" and item["msg"] == "命中敏感词：违禁"
    assert len(item["input"]) <= 203
    settings_service.set_value(db, "media_config", {"image": {"max_count_per_request": 2}})
    assert errors(count=3)[0]["loc"] == ["body", "count"]
    with pytest.raises(BusinessError) as exc:
        gen_video(db, env, duration=16)
    assert exc.value.code == 400 and exc.value.data[0]["loc"] == ["body", "duration"]
    with pytest.raises(BusinessError) as exc:
        gen_video(db, env, input_reference="https://a.example/x.png", first_frame_image_url="https://a.example/y.png")
    assert exc.value.code == 400
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env, model="no-such-model")
    assert exc.value.code == 400 and exc.value.data == {"model": "no-such-model"}
    assert db.scalar(select(MediaAsset.id)) is None and daily_used(db) == 0


def test_generate_scope_project_and_limits(env: SimpleNamespace, db: Session, users: UserFactory) -> None:
    other = users.create("operator", username="media_other")
    with pytest.raises(BusinessError) as exc:                                    # 项目不可见 → 404
        media_service.generate_images(db, DataScope(other.id, "own", other.id),
                                      ImageGenerateBody(project_id=env.project.id, usage_type="standalone", prompt="x"), admin_id=other.id)
    assert exc.value.code == 404
    # 日上限 4291：INCRBY 超限回滚，不部分创建
    settings_service.set_value(db, "media_config", {"daily_limits": {"images": 3}})
    gen_image(db, env, count=2)
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env, count=2)
    assert exc.value.code == 4291 and exc.value.message == "今日图片生成已达上限"
    assert exc.value.data == {"scope": "daily_images", "limit": 3, "used": 2} and daily_used(db) == 2
    assert len(db.scalars(select(MediaAsset.id)).all()) == 2
    # 本地额度上限 4291：已预占的根任务回滚，日上限计数回滚
    settings_service.set_value(db, "media_config", {"daily_limits": {"images": 0}})
    settings_service.set_value(db, "generation_config", {"quota": {"daily_limit": 1}})
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env, count=2)
    assert exc.value.code == 4291 and exc.value.data["scope"] == "daily" and daily_used(db) == 2
    assert len(db.scalars(select(MediaAsset.id)).all()) == 2
    # 额度预警：达到 80% 附 quota_warning
    settings_service.set_value(db, "generation_config", {"quota": {"daily_limit": 1000000}})
    redis_client.set(f"quota:daily:{stats_service.today_date(db).isoformat()}", 900000)
    data = gen_image(db, env)
    assert data["quota_warning"]["scope"] == "daily" and data["quota_warning"]["percent"] >= 90
    # 频控 429
    settings_service.set_value(db, "generation_config", {"rate_limits": {"media_per_admin": "5/hour"}})
    gen_image(db, env)
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env)
    assert exc.value.code == 429 and exc.value.data["retry_after"] > 0
    # 全局暂停 5031（频控之前）
    redis_client.set("ai:paused:auth_failed", "x", ex=60)
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env)
    assert exc.value.code == 5031 and exc.value.data["paused_reason"] == "auth_failed"
    redis_client.delete("ai:paused:auth_failed")
    # 项目归档 → 409
    env.project.status = "archived"
    db.commit()
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env)
    assert exc.value.code == 409 and exc.value.data == {"current_status": "archived"}


def test_generate_live_mode_requires_public_urls(env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-test-0123456789")
    with pytest.raises(BusinessError) as exc:
        gen_image(db, env, reference_image_urls=["http://127.0.0.1:8100/media/media/uploads/2026/10/a.png", "https://8.8.8.8/b.png"])
    assert exc.value.code == 4222 and exc.value.message == "参考素材 URL 必须是公网可访问地址"
    assert exc.value.data == {"urls": ["http://127.0.0.1:8100/media/media/uploads/2026/10/a.png"]}
    with pytest.raises(BusinessError) as exc:
        gen_video(db, env, input_reference="http://10.0.0.1/a.png")
    assert exc.value.code == 4222 and exc.value.data == {"urls": ["http://10.0.0.1/a.png"]}
    assert daily_used(db) == 0 and daily_used(db, "video") == 0 and db.scalar(select(MediaAsset.id)) is None


def test_param_priority_route_params(env: SimpleNamespace, db: Session) -> None:
    project_route = CapabilityRoute(capability="image", project_id=env.project.id, protocol="image_async", primary_model="mock-image",
                                    params_json=json.dumps({"aspect_ratio": "1:1"}), is_enabled=True)
    db.add(project_route)
    db.commit()
    gw.invalidate_routes_cache()
    asset = asset_of(db, gen_image(db, env)["asset_ids"][0])
    assert json.loads(asset.params_json) == {"resolution": "1080p", "aspect_ratio": "1:1", "reference_image_urls": []}
    asset = asset_of(db, gen_image(db, env, aspect_ratio="9:16", resolution="2k")["asset_ids"][0])
    assert json.loads(asset.params_json)["aspect_ratio"] == "9:16"
    video = asset_of(db, gen_video(db, env, size="1280x720", generate_audio=True, duration=8)["asset_id"])
    params = json.loads(video.params_json)
    assert (params["aspect_ratio"], params["size"], params["generate_audio"], params["duration"]) == (None, "1280x720", True, 8)
    both = asset_of(db, gen_video(db, env, size="1280x720", aspect_ratio="9:16")["asset_id"])
    assert (json.loads(both.params_json)["aspect_ratio"], json.loads(both.params_json)["size"]) == ("9:16", None)


# =====================================================================
# 13. recover_stale_tasks 媒体分支（钩子）
# =====================================================================


def test_recover_stale_running_resumes_polling(env: SimpleNamespace, db: Session) -> None:
    data = gen_image(db, env)
    task_id = data["task_ids"][0]
    stale = utcnow() - timedelta(minutes=30)
    db.execute(update(AiTask).where(AiTask.id == task_id).values(
        status="running", locked_by="dead:1", started_at=stale, heartbeat_at=stale, upstream_task_id="task_mock_lost",
    ))
    db.commit()
    result = recover_stale_tasks.recover_running(db, utcnow())
    assert result["stale_polling"] == 1
    root, asset = task_of(db, task_id), asset_of(db, data["asset_ids"][0])
    assert root.status == "polling" and root.deadline_at is not None and asset.status == "submitted"
    assert asset.upstream_task_id == "task_mock_lost"
    # 未发出请求的媒体根任务 → failed(timeout) + 资产 failed + 告警
    other = gen_image(db, env)
    db.execute(update(AiTask).where(AiTask.id == other["task_ids"][0]).values(
        status="running", locked_by="dead:1", started_at=stale, heartbeat_at=stale,
    ))
    db.commit()
    recover_stale_tasks.recover_running(db, utcnow())
    asset = asset_of(db, other["asset_ids"][0])
    assert (asset.status, asset.error_category) == ("failed", "timeout") and len(media_alerts(db)) == 1


def test_recover_stale_transfer_uses_hook(env: SimpleNamespace, db: Session) -> None:
    asset_id, _ = _downloading_image(env, db)
    old = utcnow() - timedelta(minutes=20)
    db.execute(update(MediaAsset).where(MediaAsset.id == asset_id).values(updated_at=old, transfer_attempts=2))
    db.commit()
    assert recover_stale_tasks.recover_transfers(db, utcnow()) == {"transfers_rescheduled": 0, "transfers_failed": 1}
    asset = asset_of(db, asset_id)
    assert (asset.status, asset.error_category, asset.transfer_attempts) == ("failed", "transfer_failed", 3)
    assert "download_request_id=null" in asset.error_message
    assert json.loads(media_alerts(db)[0].payload_json)["download_request_id"] is None


# =====================================================================
# 14. cleanup_media
# =====================================================================


def test_cleanup_media(env: SimpleNamespace, db: Session) -> None:
    orphan = make_upload(db, env.owner, created_days_ago=8)
    referenced = make_upload(db, env.owner, created_days_ago=8)
    fresh = make_upload(db, env.owner, created_days_ago=1)
    gen_image(db, env, reference_image_urls=[referenced.url])
    # 失败残留：storage_key 无 url 的对象与 tmp/{id}.part
    key = build_storage_key("images", "png")
    get_storage().save(key, PLACEHOLDER_PNG.read_bytes())
    failed = MediaAsset(project_id=env.project.id, kind="image", status="failed", error_category="unknown", storage_key=key,
                        failed_at=utcnow() - timedelta(days=31), created_by=env.owner.id)
    recent = MediaAsset(project_id=env.project.id, kind="image", status="failed", error_category="unknown",
                        failed_at=utcnow() - timedelta(days=2), created_by=env.owner.id)
    db.add_all([failed, recent])
    db.commit()
    tmp = settings.local_storage_path / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / f"{failed.id}.part").write_bytes(b"partial")
    stale_part = tmp / "999999.part"
    stale_part.write_bytes(b"x")
    old_time = time.time() - 2 * 86400
    os.utime(stale_part, (old_time, old_time))
    keep_part = tmp / f"{recent.id}.part"
    keep_part.write_bytes(b"y")

    result = cleanup_media.cleanup()
    assert result == {"failed_cleaned": 2, "orphans_deleted": 1}
    assert asset_of(db, orphan.id).status == "deleted" and not (settings.local_storage_path / orphan.storage_key).exists()
    assert asset_of(db, orphan.id).url and asset_of(db, orphan.id).storage_key
    assert asset_of(db, referenced.id).status == "ready" and asset_of(db, fresh.id).status == "ready"
    assert not (settings.local_storage_path / key).exists() and asset_of(db, failed.id).status == "failed"
    assert not (tmp / f"{failed.id}.part").exists() and not stale_part.exists() and keep_part.exists()
    assert cleanup_media.cleanup() == {"failed_cleaned": 0, "orphans_deleted": 0}


# =====================================================================
# 15. HTTP 接口：/admin/media、/admin/uploads、内容素材绑定（docs/10 §12.3；docs/04 §6.13、§6.14；docs/13 §7.4）
# =====================================================================

MEDIA_API = f"{ADMIN_API}/media"
UPLOAD_API = f"{ADMIN_API}/uploads"


def api_upload(client: TestClient, user: User, kind: str = "image", *, name: str | None = None, data: bytes | None = None) -> Any:
    name = name or ("ref.png" if kind == "image" else "ref.mp4")
    payload = data if data is not None else (PLACEHOLDER_PNG if kind == "image" else PLACEHOLDER_MP4).read_bytes()
    return client.post(f"{UPLOAD_API}/{kind}", headers=user.headers, files={"file": (name, payload, "application/octet-stream")})


def api_gen_image(client: TestClient, env: SimpleNamespace, user: User | None = None, **values: Any) -> Any:
    body = {"project_id": env.project.id, "usage_type": "standalone", "prompt": "A cozy home office, soft light", **values}
    return client.post(f"{MEDIA_API}/images/generate", headers=(user or env.owner).headers, json=body)


def api_gen_video(client: TestClient, env: SimpleNamespace, user: User | None = None, **values: Any) -> Any:
    body = {"project_id": env.project.id, "usage_type": "standalone", "prompt": "A slow dolly shot across a desk", **values}
    return client.post(f"{MEDIA_API}/videos/generate", headers=(user or env.owner).headers, json=body)


def err(response: Any, http_status: int, code: int | None = None) -> Any:
    assert response.status_code == http_status, response.text
    body = response.json()
    assert body["code"] == (code if code is not None else http_status), body
    return body


def asset_ids_of(response: Any) -> list[int]:
    return [item["id"] for item in ok_data(response)["items"]]


def test_api_upload_image_and_video(env: SimpleNamespace, db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    data = ok_data(api_upload(client, env.owner))
    assert set(data) == {"asset_id", "url", "public"} and data["public"] is True              # Mock 模式恒为 true
    asset = asset_of(db, data["asset_id"])
    raw = PLACEHOLDER_PNG.read_bytes()
    assert (asset.kind, asset.source, asset.usage_type, asset.status, asset.project_id) == ("image", "uploaded", "reference", "ready", None)
    assert asset.created_by == env.owner.id and asset.ai_task_id is None and asset.ready_at is not None
    assert asset.storage_key.startswith("media/uploads/") and asset.storage_key.endswith(".png") and "ref" not in asset.storage_key
    assert asset.url == data["url"] == public_url_for(asset.storage_key)
    assert (asset.mime_type, asset.size_bytes, asset.width, asset.height) == ("image/png", len(raw), 64, 36)
    assert asset.file_hash == hashlib.sha256(raw).hexdigest() and (asset.thumbnail_key, asset.thumbnail_url) == (asset.storage_key, asset.url)
    assert (settings.local_storage_path / asset.storage_key).read_bytes() == raw
    assert client.get(f"/media/{asset.storage_key}").content == raw
    assert rt(db, 0, "images_generated") == 0                                                   # 上传不计生成数
    assert ok_data(client.get(f"{MEDIA_API}/assets/{asset.id}/task", headers=env.owner.headers)) is None
    # 同一文件再次上传不去重
    assert ok_data(api_upload(client, env.owner))["asset_id"] != asset.id

    video = ok_data(api_upload(client, env.owner, "video", name="clip.MOV"))
    row = asset_of(db, video["asset_id"])
    assert (row.kind, row.mime_type, row.thumbnail_key, row.width) == ("video", "video/mp4", None, None)
    assert row.storage_key.endswith(".mp4") and row.size_bytes == len(PLACEHOLDER_MP4.read_bytes())

    # 扩展名 / 魔数 / 大小 / 空文件 → 400 loc=["body","file"]
    for kind, name, payload, error_type in (
        ("image", "ref.bmp", raw, "file_type"),
        ("image", "noext", raw, "file_type"),
        ("image", "ref.jpg", raw, "file_type"),                                             # PNG 内容 + jpg 扩展名
        ("image", "ref.png", PLACEHOLDER_MP4.read_bytes(), "file_type"),
        ("video", "clip.mp4", raw, "file_type"),
        ("video", "clip.avi", PLACEHOLDER_MP4.read_bytes(), "file_type"),
        ("image", "empty.png", b"", "file_empty"),
    ):
        body = err(api_upload(client, env.owner, kind, name=name, data=payload), 400)
        assert body["data"][0]["loc"] == ["body", "file"] and body["data"][0]["type"] == error_type, (name, body)
    monkeypatch.setattr(settings, "max_image_size_mb", 1)
    body = err(api_upload(client, env.owner, name="big.png", data=raw + b"\0" * (1024 * 1024)), 400)
    assert body["data"][0]["type"] == "file_too_large" and body["data"][0]["msg"] == "文件大小不能超过 1 MB"
    assert len(db.scalars(select(MediaAsset.id)).all()) == 3
    assert not list((settings.local_storage_path / "tmp").glob("*.part"))
    assert len(list((settings.local_storage_path / "media" / "uploads").rglob("*.*"))) == 3   # 失败的上传不残留文件

    # 真实模式：public 按公网校验（127.0.0.1 → false；公网 https 地址 → true）
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-test-0123456789")
    assert ok_data(api_upload(client, env.owner))["public"] is False
    monkeypatch.setattr(settings, "public_base_url", "https://8.8.8.8")
    public = ok_data(api_upload(client, env.owner))
    assert public["public"] is True and public["url"].startswith("https://8.8.8.8/media/media/uploads/")

    # 审计：上传记 create，target_type=media_asset，target_id=asset_id
    db.expire_all()
    log = db.scalar(select(AdminOperationLog).where(AdminOperationLog.target_id == str(asset.id)))
    assert log is not None and (log.action, log.target_type, log.permission_code) == ("create", "media_asset", "system.upload.create")


def test_api_permissions(
    env: SimpleNamespace, db: Session, client: TestClient, users: UserFactory, custom_user: User, super_admin: User,
) -> None:
    reviewer = users.create("reviewer", username="media_reviewer")
    read_only = users.create("read_only", username="media_readonly")
    upload_id = ok_data(api_upload(client, env.owner))["asset_id"]
    # 未登录 401；无 system.upload.create 403
    assert client.get(f"{MEDIA_API}/assets").status_code == 401
    body = err(api_upload(client, reviewer), 403)
    assert body["data"] == {"permission": "system.upload.create"}
    # read_only：可看不可删 / 不可生成 / 不可重试
    assert upload_id in asset_ids_of(client.get(f"{MEDIA_API}/assets", headers=read_only.headers))
    assert err(client.delete(f"{MEDIA_API}/assets/{upload_id}", headers=read_only.headers), 403)["data"] == {"permission": "media.assets.delete"}
    assert err(client.post(f"{MEDIA_API}/assets/{upload_id}/retry", headers=read_only.headers), 403)["data"] == {"permission": "media.assets.retry"}
    assert err(client.post(f"{MEDIA_API}/assets/{upload_id}/transfer", headers=read_only.headers), 403)["data"] == {"permission": "media.assets.retry"}
    assert err(api_gen_image(client, env, read_only), 403)["data"] == {"permission": "media.images.generate"}
    assert err(api_gen_video(client, env, read_only), 403)["data"] == {"permission": "media.videos.generate"}
    # 自定义组只授予 media.images.generate：自动补齐 media.assets.view；视频生成 403
    assert ok_data(client.get(f"{MEDIA_API}/assets", headers=custom_user.headers))["total"] == 0   # own 范围：看不到他人素材
    assert err(api_gen_video(client, env, custom_user), 403)["data"] == {"permission": "media.videos.generate"}
    assert err(api_gen_image(client, env, custom_user), 404)["data"] is None                      # 项目不可见 → 404
    # 路径参数非正整数 → 400；静态子路径不被 {id} 捕获
    assert err(client.get(f"{MEDIA_API}/assets/0", headers=env.owner.headers), 400)
    assert err(client.get(f"{MEDIA_API}/assets/abc", headers=env.owner.headers), 400)
    # 列表筛选参数枚举校验
    assert err(client.get(f"{MEDIA_API}/assets?kind=audio", headers=env.owner.headers), 400)["data"][0]["loc"] == ["query", "kind"]
    # media_config 的 poll_budget_seconds 越界 → 400（超管保存配置）
    body = err(client.put(f"{ADMIN_API}/settings/media_config", headers=super_admin.headers, json={"value": {"image": {"poll_budget_seconds": 30}}}), 400)
    assert body["data"][0]["loc"][-1] == "poll_budget_seconds"


def test_api_data_scope(env: SimpleNamespace, db: Session, client: TestClient, users: UserFactory, super_admin: User) -> None:
    owner = env.owner
    other = users.create("operator", username="scope_other")
    reviewer = users.create("reviewer", username="scope_reviewer")
    other_project = Project(name="B 的项目", slug="b-media", owner_id=other.id, created_by=other.id)
    db.add(other_project)
    db.commit()
    other_content = Content(project_id=other_project.id, title="B 的内容", format="markdown", language="zh-CN", style="tutorial",
                            status="draft", body="正文", created_by=other.id, updated_by=other.id)
    db.add(other_content)
    db.commit()

    upload_id = ok_data(api_upload(client, owner))["asset_id"]
    generated_id = ok_data(api_gen_image(client, env))["asset_ids"][0]
    other_upload = ok_data(api_upload(client, other))["asset_id"]

    # 上传素材只对上传人与 all 范围可见；AssetPicker 查询 usage_type=reference&status=ready、不带 project_id
    picker = f"{MEDIA_API}/assets?usage_type=reference&status=ready"
    assert asset_ids_of(client.get(picker, headers=owner.headers)) == [upload_id]
    assert asset_ids_of(client.get(picker, headers=other.headers)) == [other_upload]
    assert asset_ids_of(client.get(picker, headers=reviewer.headers)) == [other_upload, upload_id]
    assert asset_ids_of(client.get(picker, headers=super_admin.headers)) == [other_upload, upload_id]
    assert asset_ids_of(client.get(f"{picker}&owner_id={owner.id}", headers=super_admin.headers)) == [upload_id]
    assert asset_ids_of(client.get(f"{picker}&owner_id={other.id}", headers=owner.headers)) == [upload_id]   # own 忽略 owner_id
    assert asset_ids_of(client.get(f"{MEDIA_API}/assets", headers=owner.headers)) == [generated_id, upload_id]
    assert asset_ids_of(client.get(f"{MEDIA_API}/assets?project_id={other_project.id}", headers=owner.headers)) == []
    assert ok_data(client.get(f"{MEDIA_API}/assets/{upload_id}", headers=reviewer.headers))["id"] == upload_id

    # 不可见对象一律 404，响应与不存在的 ID 完全相同
    missing = 999999
    for method, suffix in (("get", ""), ("get", "/task"), ("post", "/retry"), ("post", "/transfer"), ("delete", "")):
        for target in (upload_id, generated_id):
            response = getattr(client, method)(f"{MEDIA_API}/assets/{target}{suffix}", headers=other.headers)
            nothing = getattr(client, method)(f"{MEDIA_API}/assets/{missing}{suffix}", headers=other.headers)
            assert response.status_code == 404 and response.json() == nothing.json(), (method, suffix, target)
    # 生成：project_id / content_id 不可见 → 404
    err(api_gen_image(client, env, other), 404)
    err(api_gen_video(client, SimpleNamespace(project=other_project), other, usage_type="inline", content_id=env.content.id), 404)
    # attach 不可见素材 → 404
    err(client.post(f"{ADMIN_API}/contents/{other_content.id}/assets/{upload_id}/attach", headers=other.headers,
                    json={"usage_type": "inline", "sort": 1}), 404)
    # 绑定到本人内容后随项目归属：仍只对项目负责人与 all 可见
    ok_data(client.post(f"{ADMIN_API}/contents/{env.content.id}/assets/{upload_id}/attach", headers=owner.headers,
                        json={"usage_type": "inline", "sort": 1}))
    assert asset_of(db, upload_id).project_id == env.project.id
    err(client.get(f"{MEDIA_API}/assets/{upload_id}", headers=other.headers), 404)
    assert upload_id in asset_ids_of(client.get(f"{MEDIA_API}/assets?project_id={env.project.id}", headers=reviewer.headers))
    # 他人引用了本人的上传素材：详情 references 只列可见引用方，删除保护仍生效
    third = ok_data(api_upload(client, owner))
    foreign = MediaAsset(project_id=other_project.id, kind="image", status="pending", created_by=other.id,
                         reference_asset_ids_json=json.dumps([third["asset_id"]]))
    db.add(foreign)
    db.commit()
    detail = ok_data(client.get(f"{MEDIA_API}/assets/{third['asset_id']}", headers=owner.headers))
    assert detail["references"] == {"cover_of": None, "bound_content_id": None, "referenced_by_asset_ids": [], "count": 0}
    full = ok_data(client.get(f"{MEDIA_API}/assets/{third['asset_id']}", headers=super_admin.headers))["references"]
    assert full["referenced_by_asset_ids"] == [{"id": foreign.id, "status": "pending"}] and full["count"] == 1
    body = err(client.delete(f"{MEDIA_API}/assets/{third['asset_id']}", headers=owner.headers), 409)
    assert body["data"] == {"reason": "in_use"} and body["message"] == "素材正被待提交的生成任务引用"


def test_api_generate_errors(env: SimpleNamespace, db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # 400：参数组合（Pydantic 与业务校验同结构）
    for values, loc in (
        ({"usage_type": "cover"}, ["body", "content_id"]),
        ({"usage_type": "reference"}, ["body", "usage_type"]),
        ({"count": 5}, ["body", "count"]),
        ({"usage_type": "standalone", "content_id": env.content.id}, ["body", "content_id"]),
        ({"from_content_prompt": True, "prompt": None, "usage_type": "inline"}, ["body", "content_id"]),
        ({"prompt": None}, ["body", "prompt"]),
        ({"resolution": "8k"}, ["body", "resolution"]),
    ):
        body = err(api_gen_image(client, env, **values), 400)
        assert body["data"][0]["loc"] == loc, (values, body)
    for values, loc in (
        ({"usage_type": "cover", "content_id": env.content.id}, ["body", "usage_type"]),
        ({"usage_type": "reference"}, ["body", "usage_type"]),
        ({"usage_type": "inline"}, ["body", "content_id"]),
        ({"duration": 16}, ["body", "duration"]),
    ):
        body = err(api_gen_video(client, env, **values), 400)
        assert body["data"][0]["loc"] == loc, (values, body)
    body = err(api_gen_image(client, env, model="no-such-model"), 400)
    assert body["data"] == {"model": "no-such-model"}
    settings_service.set_value(db, "generation_config", {"quality": {"banned_words": ["违禁"]}})
    body = err(api_gen_video(client, env, negative_prompt="违禁画面"), 400)
    assert body["data"] == [{"loc": ["body", "negative_prompt"], "msg": "命中敏感词：违禁", "type": "banned_words", "input": "违禁画面"}]
    settings_service.set_value(db, "generation_config", {"quality": {"banned_words": []}})
    assert db.scalar(select(MediaAsset.id)) is None

    # 4222：真实模式参考 URL 非公网（DEV_MODE=false 时 http 同样 4222）
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-test-0123456789")
    monkeypatch.setattr(settings, "dev_mode", False)
    body = err(api_gen_image(client, env, reference_image_urls=["http://8.8.8.8/a.png", "https://8.8.8.8/b.png"]), 422, 4222)
    assert body["message"] == "参考素材 URL 必须是公网可访问地址" and body["data"] == {"urls": ["http://8.8.8.8/a.png"]}
    monkeypatch.setattr(settings, "zhiqi_api_key", "")
    monkeypatch.setattr(settings, "dev_mode", True)

    # 4291：视频日上限
    settings_service.set_value(db, "media_config", {"daily_limits": {"videos": 1}})
    first = ok_data(api_gen_video(client, env))
    assert set(first) == {"asset_id", "task_id"}
    body = err(api_gen_video(client, env), 429, 4291)
    assert body["message"] == "今日视频生成已达上限" and body["data"] == {"scope": "daily_videos", "limit": 1, "used": 1}
    # 4291：本地额度上限（scope=daily），日上限计数回滚
    settings_service.set_value(db, "generation_config", {"quota": {"daily_limit": 1}})
    body = err(api_gen_image(client, env, count=2), 429, 4291)
    assert body["data"]["scope"] == "daily" and daily_used(db) == 0
    settings_service.set_value(db, "generation_config", {"quota": {"daily_limit": 0}})
    # 5031：全局暂停
    redis_client.set("ai:paused:quota_exceeded", "x", ex=60)
    body = err(api_gen_image(client, env), 503, 5031)
    assert body["data"]["paused_reason"] == "quota_exceeded"
    redis_client.delete("ai:paused:quota_exceeded")
    # 429：频控 rate:media:{admin_id}（之前的请求已计数）
    settings_service.set_value(db, "generation_config", {"rate_limits": {"media_per_admin": f"{redis_client.zcard(f'rate:media:{env.owner.id}') + 1}/hour"}})
    ok_data(api_gen_image(client, env))
    body = err(api_gen_image(client, env), 429)
    assert body["data"]["retry_after"] > 0
    assert len(db.scalars(select(MediaAsset.id)).all()) == 2
    # 审计：生成记 execute，target_type=media_asset
    db.expire_all()
    log = db.scalar(select(AdminOperationLog).where(AdminOperationLog.permission_code == "media.videos.generate"))
    assert log is not None and (log.action, log.target_type, log.target_id) == ("execute", "media_asset", str(first["asset_id"]))


def test_api_state_conflicts(env: SimpleNamespace, db: Session, client: TestClient) -> None:
    headers = env.owner.headers
    data = ok_data(api_gen_image(client, env))
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    for path in ("retry", "transfer"):
        body = err(client.post(f"{MEDIA_API}/assets/{asset_id}/{path}", headers=headers), 409)
        assert body["data"] == {"current_status": "pending"}
    assert err(client.delete(f"{MEDIA_API}/assets/{asset_id}", headers=headers), 409)["data"] == {"current_status": "pending"}
    # /admin/ai/tasks/{id}/retry 对媒体根任务 409 hint；running 时 cancel 409
    body = err(client.post(f"{ADMIN_API}/ai/tasks/{task_id}/retry", headers=headers), 409)
    assert body["data"] == {"hint": f"POST /admin/media/assets/{asset_id}/retry"}
    db.execute(update(AiTask).where(AiTask.id == task_id).values(status="running"))
    db.commit()
    assert err(client.post(f"{ADMIN_API}/ai/tasks/{task_id}/cancel", headers=headers), 409)["data"] == {"current_status": "running"}
    db.execute(update(AiTask).where(AiTask.id == task_id).values(status="queued"))
    db.commit()
    ok_data(client.post(f"{ADMIN_API}/ai/tasks/{task_id}/cancel", headers=headers))
    asset = asset_of(db, asset_id)
    assert (asset.status, asset.error_category) == ("failed", "cancelled")
    # failed(cancelled)：transfer 409（非 transfer_failed/timeout），retry 走重新提交（新根任务 queued、计入日上限）
    assert err(client.post(f"{MEDIA_API}/assets/{asset_id}/transfer", headers=headers), 409)["data"] == {"current_status": "failed"}
    used = daily_used(db)
    retried = ok_data(client.post(f"{MEDIA_API}/assets/{asset_id}/retry", headers=headers))
    assert retried["resumed"] is False and retried["asset"]["status"] == "pending" and retried["task_id"] != task_id
    new_root = task_of(db, retried["task_id"])
    assert (new_root.status, new_root.parent_task_id, new_root.trigger_type) == ("queued", task_id, "user") and daily_used(db) == used + 1
    task = ok_data(client.get(f"{MEDIA_API}/assets/{asset_id}/task", headers=headers))
    assert task["task_id"] == retried["task_id"] and task["status"] == "queued" and task["model_override"] is None
    # downloading：删除 409；transfer_failed → 人工 transfer 只写 next_transfer_at，由 worker 转存
    redis_client.delete(gw.QUEUE_AI_TASKS)
    downloading_id, _ = _downloading_image(env, db)
    assert err(client.delete(f"{MEDIA_API}/assets/{downloading_id}", headers=headers), 409)["data"] == {"current_status": "downloading"}
    db.execute(update(MediaAsset).where(MediaAsset.id == downloading_id).values(
        status="failed", error_category="transfer_failed", failed_at=utcnow(), transfer_attempts=3, next_transfer_at=None))
    db.commit()
    result = ok_data(client.post(f"{MEDIA_API}/assets/{downloading_id}/transfer", headers=headers))
    assert set(result) == {"asset"} and result["asset"]["status"] == "downloading" and result["asset"]["transfer_attempts"] == 0
    asset = asset_of(db, downloading_id)
    assert asset.failed_at is None and asset.storage_key is None and seconds_until(asset.next_transfer_at) <= 1
    assert transfer_media.retry_due(SyncPool()) == 1
    ready = ok_data(client.get(f"{MEDIA_API}/assets/{downloading_id}", headers=headers))
    assert ready["status"] == "ready" and ready["url"]
    assert err(client.post(f"{MEDIA_API}/assets/{downloading_id}/retry", headers=headers), 409)["data"] == {"current_status": "ready"}
    # 删除：ready → deleted；重复删除 409 current_status=deleted
    assert ok_data(client.delete(f"{MEDIA_API}/assets/{downloading_id}", headers=headers)) is None
    assert err(client.delete(f"{MEDIA_API}/assets/{downloading_id}", headers=headers), 409)["data"] == {"current_status": "deleted"}
    db.expire_all()
    actions = {(log.action, log.target_id) for log in db.scalars(select(AdminOperationLog).where(AdminOperationLog.target_type == "media_asset"))}
    assert ("delete", str(downloading_id)) in actions and ("execute", str(asset_id)) in actions


def test_api_end_to_end_with_content(env: SimpleNamespace, db: Session, client: TestClient) -> None:
    """generate（cover + from_content_prompt / inline 视频）→ worker process_one → poll_due → transfer → ready → 自动绑定。"""
    headers = env.owner.headers
    content_id = env.content.id
    data = ok_data(api_gen_image(client, env, usage_type="cover", content_id=content_id, from_content_prompt=True, prompt=None))
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    detail = ok_data(client.get(f"{MEDIA_API}/assets/{asset_id}", headers=headers))
    assert (detail["status"], detail["prompt"], detail["content_id"], detail["usage_type"], detail["sort"]) == ("pending", None, content_id, "cover", 1)
    video = ok_data(api_gen_video(client, env, usage_type="inline", content_id=content_id, duration=5, resolution="720p"))

    assert sorted(run_queue(db)) == sorted([task_id, video["task_id"]])                           # 提交（含内嵌 image_prompt）
    task = ok_data(client.get(f"{MEDIA_API}/assets/{asset_id}/task", headers=headers))
    assert (task["task_id"], task["operation"], task["status"]) == (task_id, "image_generate", "polling")
    assert ok_data(client.get(f"{MEDIA_API}/assets/{asset_id}", headers=headers))["prompt"]           # worker 写回提示词
    pool = SyncPool()
    for _ in range(3):
        poll(db, task_id, pool)
    assert "transfer_asset" in pool.calls
    detail = ok_data(client.get(f"{MEDIA_API}/assets/{asset_id}", headers=headers))
    assert detail["status"] == "ready" and detail["url"].startswith(f"{settings.public_base_url.rstrip('/')}/media/media/images/")
    assert detail["task"]["status"] == "succeeded" and detail["references"] == {
        "cover_of": content_id, "bound_content_id": content_id, "referenced_by_asset_ids": [], "count": 2,
    }
    response = client.get(f"/media/{detail['storage_key']}")
    assert response.status_code == 200 and response.headers["content-type"].startswith("image/png")
    assert ok_data(client.get(f"{ADMIN_API}/contents/{content_id}", headers=headers))["cover_asset_id"] == asset_id

    # 视频：第 5 次轮询成功，转存交给 retry_due
    for _ in range(5):
        poll(db, video["task_id"])
    assert asset_of(db, video["asset_id"]).status == "downloading"
    assert transfer_media.retry_due(SyncPool()) == 1
    vdetail = ok_data(client.get(f"{MEDIA_API}/assets/{video['asset_id']}", headers=headers))
    assert (vdetail["status"], vdetail["kind"], vdetail["usage_type"], vdetail["sort"]) == ("ready", "video", "inline", 2)
    bound = ok_data(client.get(f"{ADMIN_API}/contents/{content_id}/assets", headers=headers))
    assert [a["id"] for a in bound] == [asset_id, video["asset_id"]]
    assert ok_data(client.get(f"{ADMIN_API}/contents/{content_id}", headers=headers))["cover_asset_id"] == asset_id

    # 事后绑定：视频不能作封面（400）；上传素材（project_id=NULL）attach 为封面 → 写入内容项目、替换旧封面
    body = err(client.post(f"{ADMIN_API}/contents/{content_id}/assets/{video['asset_id']}/attach", headers=headers,
                           json={"usage_type": "cover", "sort": 1}), 400)
    assert body["data"][0]["loc"] == ["body", "usage_type"]
    upload_id = ok_data(api_upload(client, env.owner))["asset_id"]
    attached = ok_data(client.post(f"{ADMIN_API}/contents/{content_id}/assets/{upload_id}/attach", headers=headers,
                                   json={"usage_type": "cover", "sort": 0}))
    assert (attached["project_id"], attached["content_id"], attached["usage_type"]) == (env.project.id, content_id, "cover")
    assert ok_data(client.get(f"{ADMIN_API}/contents/{content_id}", headers=headers))["cover_asset_id"] == upload_id
    assert asset_of(db, asset_id).usage_type == "inline" and asset_of(db, asset_id).content_id == content_id
    # 进行中资产不可 attach（409 current_status）
    pending = ok_data(api_gen_image(client, env))["asset_ids"][0]
    body = err(client.post(f"{ADMIN_API}/contents/{content_id}/assets/{pending}/attach", headers=headers, json={"usage_type": "inline", "sort": 3}), 409)
    assert body["data"] == {"current_status": "pending"}
    # 其它项目的素材不能 attach（project_mismatch）
    other_project = Project(name="同用户另一项目", slug="media-2", owner_id=env.owner.id, created_by=env.owner.id)
    db.add(other_project)
    db.commit()
    other_content = Content(project_id=other_project.id, title="另一篇", format="markdown", language="zh-CN", style="tutorial",
                            status="draft", body="正文", created_by=env.owner.id, updated_by=env.owner.id)
    db.add(other_content)
    db.commit()
    body = err(client.post(f"{ADMIN_API}/contents/{other_content.id}/assets/{asset_id}/attach", headers=headers,
                           json={"usage_type": "inline", "sort": 1}), 400)
    assert body["data"][0]["type"] == "project_mismatch"
    # 解绑：content_id=NULL、usage_type=standalone、sort=0
    detached = ok_data(client.post(f"{ADMIN_API}/contents/{content_id}/assets/{video['asset_id']}/detach", headers=headers))
    assert (detached["content_id"], detached["usage_type"], detached["sort"]) == (None, "standalone", 0)
    # 删除封面素材：清空 contents.cover_asset_id，文件删除，url / storage_key 保留
    path = settings.local_storage_path / attached["storage_key"]
    assert path.exists()
    assert ok_data(client.delete(f"{MEDIA_API}/assets/{upload_id}", headers=headers)) is None
    assert ok_data(client.get(f"{ADMIN_API}/contents/{content_id}", headers=headers))["cover_asset_id"] is None
    gone = asset_of(db, upload_id)
    assert gone.status == "deleted" and gone.content_id is None and gone.url and gone.storage_key and not path.exists()
    assert [a["id"] for a in ok_data(client.get(f"{ADMIN_API}/contents/{content_id}/assets", headers=headers))] == [asset_id]
    assert rt(db, env.project.id, "images_generated") == 1 and rt(db, env.project.id, "videos_generated") == 1


# =====================================================================
# 18. 阶段 4 一致性补充：暂停不影响轮询 / 转存、取消与轮询互斥、/media 的 Content-Type
# =====================================================================


def test_paused_does_not_block_polling_and_transfer(env: SimpleNamespace, db: Session) -> None:
    """``ai:paused:*`` 只阻止新提交（领取），已提交任务的轮询与转存照常进行（docs/10 §4.7、§12.2）。"""
    data = gen_image(db, env)
    asset_id, task_id = data["asset_ids"][0], data["task_ids"][0]
    run_queue(db)
    redis_client.set("ai:paused:quota_exceeded", "x", ex=120)
    try:
        pool = SyncPool()
        for _ in range(3):
            assert poll(db, task_id, pool) == 1
        assert pool.calls[-1] == "transfer_asset"
        assert task_of(db, task_id).status == "succeeded" and asset_of(db, asset_id).status == "ready"
    finally:
        redis_client.delete("ai:paused:quota_exceeded")


def test_cancel_during_poll_wins_and_poll_after_terminal_is_noop(
    env: SimpleNamespace, db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """轮询的网络调用期间根任务被取消：``poll_root`` 加行锁重读到 ``cancelled`` 后不再改写根任务与资产；反之根任务已被轮询置为
    终态后再取消返回 409 ``current_status``。"""
    data = gen_image(db, env, count=2)
    run_queue(db)
    cancel_id, done_id = data["task_ids"]
    original = gw.poll_task

    def cancel_then_poll(session: Session, root: AiTask) -> Any:
        status = original(session, root)
        from app.core.database import SessionLocal

        with SessionLocal() as other:                    # 另一个会话（API 进程）在轮询结果落库前提交取消
            ai_task_service.cancel_task(other, SYSTEM_SCOPE, root.id, admin_id=env.owner.id)
        return status

    for _ in range(2):                                    # queued、in_progress
        poll(db, cancel_id)
    monkeypatch.setattr(gw, "poll_task", cancel_then_poll)
    db.execute(update(AiTask).where(AiTask.id == cancel_id).values(next_poll_at=utcnow() - timedelta(seconds=1)))
    db.commit()
    assert media_service.poll_root(db, task_of(db, cancel_id)) is None   # 第 3 次上游已 succeeded，仍以取消为准
    root, asset = task_of(db, cancel_id), asset_of(db, data["asset_ids"][0])
    assert root.status == "cancelled" and (asset.status, asset.error_category) == ("failed", "cancelled")
    assert asset.upstream_url is None and media_alerts(db) == []
    monkeypatch.setattr(gw, "poll_task", original)

    for _ in range(3):
        poll(db, done_id, SyncPool(transfer=False))
    assert task_of(db, done_id).status == "succeeded" and asset_of(db, data["asset_ids"][1]).status == "downloading"
    with pytest.raises(BusinessError) as exc:
        ai_task_service.cancel_task(db, env.scope, done_id, admin_id=env.owner.id)
    assert exc.value.http_status == 409 and exc.value.data == {"current_status": "succeeded"}


def test_media_file_content_type_from_stored_mime(env: SimpleNamespace, db: Session, client: TestClient) -> None:
    """``GET /media/{key}`` 的 ``Content-Type`` 取库内 ``mime_type``（附 ``nosniff``）；无对应素材行（Mock 占位文件）按扩展名推断；
    路径穿越 400，不存在 404（docs/10 §11.1）。"""
    key = build_storage_key("images", "png")
    get_storage().save(key, PLACEHOLDER_PNG.read_bytes(), content_type="image/png")
    asset = MediaAsset(
        project_id=env.project.id, kind="image", source="generated", usage_type="standalone", status="ready", storage_key=key,
        url=public_url_for(key), thumbnail_key=key, mime_type="image/webp", created_by=env.owner.id,
    )
    db.add(asset)
    db.commit()
    response = client.get(f"/media/{key}")
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert response.headers["x-content-type-options"] == "nosniff"
    mock = client.get("/media/mock/placeholder.png")
    assert mock.status_code == 200 and mock.headers["content-type"] == "image/png"
    assert client.get("/media/media%2F..%2F..%2Fsecret.png").status_code == 400
    assert client.get("/media/media/images/2026/10/missing.png").status_code == 404
