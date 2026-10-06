"""``/admin/ai/*`` 接口测试（docs/04 §6.15、§7.15~§7.17；docs/08 §6.7、§7.7、§10.7、§11.5、§12.3；docs/13 §6.3）。

权限码、数据范围过滤（任务 / 用量 / 项目覆盖路由）、Mock 模式下的目录同步 / 一键测试 / 单模型探测 / 对账、
重试与取消规则（409 ``existing_id`` / ``hint``）、CSV 导出。上游为 Mock 客户端或 ``httpx.MockTransport``。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redis import redis_client
from app.core.zhiqi.client import ZhiqiClient
from app.core.zhiqi.types import Timeouts
from app.models import AiModel, AiTask, AiUsageLog, Alert, CapabilityRoute, GenerationBatch, Project, utcnow
from app.services import ai_catalog_service, ai_task_service
from app.services import ai_gateway_service as gw
from tests.conftest import ADMIN_API, User, UserFactory, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")

AI = f"{ADMIN_API}/ai"


# =====================================================================
# 夹具
# =====================================================================


@pytest.fixture
def synced(client: TestClient, db: Session) -> Session:
    """应用已启动（8 条全局路由）+ Mock 模型目录已同步（mock-text / mock-image / mock-video）。"""
    ai_catalog_service.sync_models(db)
    return db


@pytest.fixture
def owner_a(users: UserFactory) -> User:
    return users.create("operator", username="owner_a")


@pytest.fixture
def owner_b(users: UserFactory) -> User:
    return users.create("operator", username="owner_b")


@pytest.fixture
def router_user(users: UserFactory) -> User:
    """自定义组（own）：AI 网关能力路由全部权限 + 任务查看。"""
    group = users.custom_group(
        "路由管理", ["ai.routes.view", "ai.routes.create", "ai.routes.update", "ai.routes.delete", "ai.routes.test",
                 "ai.routes.reset_breaker", "ai.models.view", "ai.tasks.view"],
    )
    return users.create(group, username="router01")


def _project(db: Session, owner: User, name: str) -> Project:
    project = Project(name=name, slug=name.lower(), owner_id=owner.id, created_by=owner.id)
    db.add(project)
    db.commit()
    return project


@pytest.fixture
def pa(db: Session, owner_a: User) -> Project:
    return _project(db, owner_a, "PA")


@pytest.fixture
def pb(db: Session, owner_b: User) -> Project:
    return _project(db, owner_b, "PB")


def add_model(db: Session, model_id: str, *, available: bool = True, types: list[str] | None = None) -> AiModel:
    from app.core.zhiqi import catalog

    types = types if types is not None else ["openai"]
    row = AiModel(
        model_id=model_id, is_available=available, synced_at=utcnow(), last_seen_at=utcnow(),
        supported_endpoint_types_json=json.dumps(types), modalities_json=json.dumps(sorted(catalog.derive_modalities(types))),
    )
    db.add(row)
    db.commit()
    ai_catalog_service.invalidate_model_caches()
    return row


def root_task(db: Session, project: Project | None, *, status: str = "failed", capability: str = "keyword",
              operation: str = "keyword_generate", **kw: Any) -> AiTask:
    task = gw.create_root_task(
        db, capability=capability, operation=operation, project_id=project.id if project else None,
        created_by=project.owner_id if project else None, enqueue=False, **kw,
    )
    task.status = status
    if status == "failed":
        task.error_category = kw.get("error_category_value", "timeout")
    db.commit()
    return task


def attempt_row(db: Session, root: AiTask, *, request_id: str, status: str = "succeeded") -> AiTask:
    row = AiTask(
        project_id=root.project_id, capability=root.capability, operation=root.operation, trigger_type=root.trigger_type,
        target_type=root.target_type, target_id=root.target_id, batch_id=root.batch_id, root_task_id=root.id,
        candidate_index=0, attempt=1, model="mock-text", protocol="openai_chat", status=status, request_id=request_id,
        prompt_tokens=10, completion_tokens=5, quota_estimated=100, finished_at=utcnow(), started_at=utcnow(),
    )
    db.add(row)
    db.commit()
    return row


class FakeUpstream:
    """``httpx.MockTransport`` 上游：``handler(request) -> (status, body)``。"""

    def __init__(self) -> None:
        self.seq = 0
        self.handler: Callable[[httpx.Request], tuple[int, Any]] = lambda r: (
            200, {"choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seq += 1
        status, body = self.handler(request)
        return httpx.Response(status, json=body, headers={"x-oneapi-request-id": f"req-{self.seq}"})


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> FakeUpstream:
    fake = FakeUpstream()
    zclient = ZhiqiClient(
        base_url="https://zhiqi.test/v1", api_key="sk-test", timeouts=Timeouts(), user_agent="test",
        transport=httpx.MockTransport(fake), sleep=lambda _s: None,
    )
    monkeypatch.setattr(ai_task_service, "get_client", lambda: zclient)
    monkeypatch.setattr(gw, "get_client", lambda: zclient)
    return fake


def err(response: Any, http_status: int, code: int | None = None) -> dict[str, Any]:
    assert response.status_code == http_status, response.text
    body = response.json()
    assert body["code"] == (code if code is not None else http_status), body
    return body


# =====================================================================
# 模型目录
# =====================================================================


def test_models_sync_list_options_detail(client: TestClient, db: Session, super_admin: User, operator: User, reviewer: User) -> None:
    data = ok_data(client.post(f"{AI}/models/sync", headers=super_admin.headers))
    assert data["total"] == 3 and data["added"] == 3 and data["request_ids"]["models"].startswith("mock-")
    page = ok_data(client.get(f"{AI}/models", params={"modality": "text"}, headers=operator.headers))
    assert page["total"] == 1 and page["items"][0]["model_id"] == "mock-text" and "raw_pricing" not in page["items"][0]
    options = ok_data(client.get(f"{AI}/models/options", params={"modality": "image"}, headers=operator.headers))
    assert [o["model_id"] for o in options] == ["mock-image"] and set(options[0]) == {
        "model_id", "vendor_name", "modalities", "is_available", "last_health_status", "quota_type",
    }
    detail = ok_data(client.get(f"{AI}/models/{page['items'][0]['id']}", headers=operator.headers))
    assert "raw_pricing" in detail
    err(client.get(f"{AI}/models/999999", headers=operator.headers), 404)
    # 权限：operator 无 ai.models.sync；reviewer 无 ai.models.view
    body = err(client.post(f"{AI}/models/sync", headers=operator.headers), 403)
    assert body["data"] == {"permission": "ai.models.sync"}
    err(client.get(f"{AI}/models", headers=reviewer.headers), 403)
    # 同步进行中 409
    redis_client.set("lock:ai:models_sync", "x", ex=60)
    err(client.post(f"{AI}/models/sync", headers=super_admin.headers), 409)


def test_models_hidden_unavailable(client: TestClient, db: Session, super_admin: User) -> None:
    from datetime import timedelta

    row = add_model(db, "old-model", available=False)
    row.last_seen_at = utcnow() - timedelta(days=30)
    db.commit()
    page = ok_data(client.get(f"{AI}/models", headers=super_admin.headers))
    assert "old-model" not in [i["model_id"] for i in page["items"]]
    page = ok_data(client.get(f"{AI}/models", params={"include_hidden": 1}, headers=super_admin.headers))
    assert "old-model" in [i["model_id"] for i in page["items"]]


# =====================================================================
# 能力路由
# =====================================================================


def test_routes_list_scope_and_status(client: TestClient, synced: Session, super_admin: User, owner_a: User, pa: Project, pb: Project) -> None:
    for project in (pa, pb):
        synced.add(CapabilityRoute(capability="content", project_id=project.id, protocol="openai_chat", primary_model="mock-text",
                                   fallback_models_json="[]", params_json="{}", max_attempts=3, is_enabled=True))
    synced.commit()
    all_rows = ok_data(client.get(f"{AI}/routes", headers=super_admin.headers))
    assert len(all_rows) == 10 and [r["project_id"] for r in all_rows[:8]] == [0] * 8
    assert all_rows[0]["capability"] == "keyword" and all_rows[0]["breaker_state"] == "closed"
    assert all_rows[0]["models"] == [{"model": "mock-text", "candidate_index": 0, "health": "unknown", "is_available": True,
                                      "breaker_state": "closed", "breaker_reason": None}]
    a_rows = ok_data(client.get(f"{AI}/routes", headers=owner_a.headers))
    assert {r["project_id"] for r in a_rows} == {0, pa.id}
    # ?project_id 指向不可见项目 → 只返回全局行
    assert {r["project_id"] for r in ok_data(client.get(f"{AI}/routes", params={"project_id": pb.id}, headers=owner_a.headers))} == {0}
    assert {r["project_id"] for r in ok_data(client.get(f"{AI}/routes", params={"project_id": pb.id}, headers=super_admin.headers))} == {0, pb.id}
    pb_route = next(r for r in all_rows if r["project_id"] == pb.id)
    first = err(client.get(f"{AI}/routes/{pb_route['id']}", headers=owner_a.headers), 404)
    second = err(client.get(f"{AI}/routes/999999", headers=owner_a.headers), 404)
    assert first == second
    # 熔断状态
    gw.get_breaker(gw._cfg(synced)).force_open("keyword", "mock-text", reason="manual")
    keyword = ok_data(client.get(f"{AI}/routes/{all_rows[0]['id']}", headers=super_admin.headers))
    assert keyword["breaker_state"] == "open" and keyword["breaker_reason"] == "manual"


def test_route_create_update_delete(client: TestClient, synced: Session, super_admin: User, router_user: User, owner_b: User) -> None:
    mine = _project(synced, router_user, "Mine")
    other = _project(synced, owner_b, "Other")
    add_model(synced, "text-off", available=False)
    add_model(synced, "img-only", types=["image-generation"])
    body = {"capability": "content", "project_id": mine.id, "protocol": "openai_chat", "primary_model": "mock-text",
            "fallback_models": ["text-off"], "params": {"temperature": 0.5}, "timeout_seconds": 90, "max_attempts": 2, "note": "  "}
    created = ok_data(client.post(f"{AI}/routes", json=body, headers=router_user.headers))
    assert created["project_id"] == mine.id and created["fallback_models"] == ["text-off"] and created["note"] is None
    assert created["updated_by"] == router_user.id and created["timeout_seconds"] == 90
    assert created["warnings"] == [{"loc": ["body", "fallback_models", 0], "msg": "模型当前不可用，运行期将被跳过",
                                    "type": "model_unavailable", "input": "text-off"}]
    assert [m["is_available"] for m in created["models"]] == [True, False]
    # 路由缓存已清除：resolve_route 使用项目覆盖
    route = gw.resolve_route(synced, "content", mine.id)
    assert route.route_id == created["id"] and route.unavailable_models == ["text-off"] and route.timeout_seconds == 90
    # 重复 → 409 existing_id；不可见项目 → 404；全局 project_id=0 → 400
    assert err(client.post(f"{AI}/routes", json=body, headers=router_user.headers), 409)["data"] == {"existing_id": created["id"]}
    err(client.post(f"{AI}/routes", json={**body, "project_id": other.id}, headers=router_user.headers), 404)
    err(client.post(f"{AI}/routes", json={**body, "project_id": 0}, headers=router_user.headers), 400)
    # 模型 / 协议校验
    bad = err(client.post(f"{AI}/routes", json={**body, "capability": "title", "primary_model": "img-only", "fallback_models": ["nope", "nope"],
                                                "protocol": "video"}, headers=router_user.headers), 400)
    assert {(tuple(e["loc"]), e["type"]) for e in bad["data"]} == {
        (("body", "protocol"), "invalid_protocol"), (("body", "primary_model"), "invalid_model"),
        (("body", "fallback_models", 0), "invalid_model"), (("body", "fallback_models", 1), "duplicate_model"),
    }
    # 编辑：capability 不可改；primary 改为可用模型；warnings 为空
    err(client.put(f"{AI}/routes/{created['id']}", json={"capability": "title"}, headers=router_user.headers), 400)
    err(client.put(f"{AI}/routes/{created['id']}", json={"primary_model": None}, headers=router_user.headers), 400)
    updated = ok_data(client.put(f"{AI}/routes/{created['id']}", json={"fallback_models": [], "is_enabled": False, "timeout_seconds": None},
                                 headers=router_user.headers))
    assert updated["warnings"] == [] and updated["is_enabled"] is False and updated["timeout_seconds"] is None
    assert updated["params"] == {"temperature": 0.5}
    # 全局路由可编辑不可删
    global_id = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "keyword", CapabilityRoute.project_id == 0))
    ok_data(client.put(f"{AI}/routes/{global_id}", json={"max_attempts": 5}, headers=router_user.headers))
    assert err(client.delete(f"{AI}/routes/{global_id}", headers=router_user.headers), 409)["data"] == {"reason": "global_route"}
    ok_data(client.delete(f"{AI}/routes/{created['id']}", headers=router_user.headers))
    err(client.get(f"{AI}/routes/{created['id']}", headers=router_user.headers), 404)
    # 审计：新增 / 编辑 / 删除都记 capability_route
    logs = ok_data(client.get(f"{ADMIN_API}/admin-operation-logs", params={"target_type": "capability_route"}, headers=super_admin.headers))
    assert {i["action"] for i in logs["items"]} >= {"create", "update", "delete"}


def test_route_permissions(client: TestClient, synced: Session, operator: User) -> None:
    global_id = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "keyword", CapabilityRoute.project_id == 0))
    ok_data(client.get(f"{AI}/routes", headers=operator.headers))
    assert err(client.put(f"{AI}/routes/{global_id}", json={"max_attempts": 2}, headers=operator.headers), 403)["data"] == {"permission": "ai.routes.update"}
    err(client.post(f"{AI}/routes/{global_id}/test", json={}, headers=operator.headers), 403)
    err(client.post(f"{AI}/routes/{global_id}/reset-breaker", headers=operator.headers), 403)
    err(client.post(f"{AI}/health/probe", json={"capability": "keyword", "model": "mock-text"}, headers=operator.headers), 403)


def test_route_test_mock_records_health(client: TestClient, synced: Session, super_admin: User) -> None:
    image_id = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "image", CapabilityRoute.project_id == 0))
    content_id = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "content", CapabilityRoute.project_id == 0))
    result = ok_data(client.post(f"{AI}/routes/{content_id}/test", json={"probe_media": False}, headers=super_admin.headers))
    assert len(result) == 1 and result[0]["model"] == "mock-text" and result[0]["status"] == "healthy"
    assert result[0]["request_id"].startswith("mock-") and result[0]["error_category"] is None
    snap = json.loads(redis_client.get("ai:health:content:mock-text"))
    assert snap["status"] == "healthy" and snap["consecutive_failures"] == 0
    assert 0 < redis_client.ttl("ai:health:content:mock-text") <= 86400
    synced.expire_all()
    model = ai_catalog_service.get_model_by_id(synced, "mock-text")
    assert model.last_health_status == "healthy" and model.last_health_at is not None
    probe_root = synced.scalar(select(AiTask).where(AiTask.operation == "route_probe", AiTask.root_task_id.is_(None)))
    assert (probe_root.created_by, probe_root.target_id, probe_root.trigger_type) == (super_admin.id, content_id, "health_probe")
    # 图片 probe_media=false：不发 HTTP，按 is_available 判定；body 可省略
    image = ok_data(client.post(f"{AI}/routes/{image_id}/test", headers=super_admin.headers))
    assert image[0]["status"] == "healthy" and image[0]["latency_ms"] == 0 and image[0]["request_id"] is None
    image = ok_data(client.post(f"{AI}/routes/{image_id}/test", json={"probe_media": True}, headers=super_admin.headers))
    assert image[0]["status"] == "healthy" and image[0]["request_id"].startswith("mock-")
    # 健康快照
    snapshot = ok_data(client.get(f"{AI}/health", headers=super_admin.headers))
    assert snapshot["zhiqi_mode"] == "mock" and snapshot["paused"] == {"quota_exceeded": False, "auth_failed": False}
    content = next(m for m in snapshot["models"] if m["capability"] == "content")
    assert content["status"] == "healthy" and content["breaker_state"] == "closed" and content["is_available"] is True
    assert {(m["capability"], m["model"]) for m in snapshot["models"]} >= {("keyword", "mock-text"), ("image", "mock-image"), ("video", "mock-video")}
    assert isinstance(snapshot["workers"], list)


def test_route_test_failures_window_and_breaker(client: TestClient, synced: Session, super_admin: User, upstream: FakeUpstream) -> None:
    content_id = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "content", CapabilityRoute.project_id == 0))
    upstream.handler = lambda r: (503, {"error": {"message": "server overloaded"}})
    first = ok_data(client.post(f"{AI}/routes/{content_id}/test", json={}, headers=super_admin.headers))
    assert first[0]["status"] == "down" and first[0]["error_category"] == "upstream_unavailable"
    snap = json.loads(redis_client.get("ai:health:content:mock-text"))
    assert snap["status"] == "degraded" and snap["consecutive_failures"] == 1   # 单次失败、连续未达 2 → degraded
    ok_data(client.post(f"{AI}/routes/{content_id}/test", json={}, headers=super_admin.headers))
    snap = json.loads(redis_client.get("ai:health:content:mock-text"))
    assert snap["status"] == "down" and snap["consecutive_failures"] == 2
    breaker = gw.get_breaker(gw._cfg(synced))
    assert breaker.state("content", "mock-text") == "open" and breaker.reason("content", "mock-text") == "probe_down"
    synced.expire_all()
    types = {a.alert_type for a in synced.scalars(select(Alert).where(Alert.status == "open"))}
    assert {"ai_upstream_unavailable", "ai_breaker_open"} <= types
    # 恢复：probe_down 熔断 reset、告警解决、ai:paused:* 删除；最近 5 条仍有失败 → degraded
    gw.set_paused("auth_failed", 600)
    upstream.handler = FakeUpstream().handler
    ok = ok_data(client.post(f"{AI}/routes/{content_id}/test", json={}, headers=super_admin.headers))
    assert ok[0]["status"] == "healthy"
    snap = json.loads(redis_client.get("ai:health:content:mock-text"))
    assert snap["status"] == "degraded" and snap["consecutive_failures"] == 0
    assert breaker.state("content", "mock-text") == "closed" and not redis_client.exists("ai:paused:auth_failed")
    synced.expire_all()
    assert not synced.scalars(select(Alert).where(Alert.status == "open", Alert.alert_type.in_(("ai_upstream_unavailable", "ai_breaker_open")))).all()


def test_reset_breaker_and_scope(client: TestClient, synced: Session, router_user: User, owner_b: User) -> None:
    other = _project(synced, owner_b, "Other")
    synced.add(CapabilityRoute(capability="title", project_id=other.id, protocol="openai_chat", primary_model="mock-text", fallback_models_json="[]"))
    synced.commit()
    other_route = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.project_id == other.id))
    err(client.post(f"{AI}/routes/{other_route}/reset-breaker", headers=router_user.headers), 404)
    err(client.post(f"{AI}/routes/{other_route}/test", json={}, headers=router_user.headers), 404)
    global_id = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "keyword", CapabilityRoute.project_id == 0))
    breaker = gw.get_breaker(gw._cfg(synced))
    breaker.force_open("keyword", "mock-text", reason="failures")
    gw.set_paused("quota_exceeded", 600)
    data = ok_data(client.post(f"{AI}/routes/{global_id}/reset-breaker", headers=router_user.headers))
    assert data == {"reset_models": ["mock-text"], "paused_cleared": True}
    assert breaker.state("keyword", "mock-text") == "closed" and gw.check_paused() is None


def test_health_probe_endpoint(client: TestClient, synced: Session, super_admin: User) -> None:
    body = err(client.post(f"{AI}/health/probe", json={"capability": "geo_check", "model": "nope"}, headers=super_admin.headers), 400)
    assert body["data"] == [{"loc": ["body", "model"], "msg": "模型不存在", "type": "invalid_model", "input": "nope"}]
    err(client.post(f"{AI}/health/probe", json={"capability": "geo_check", "model": "mock-text", "protocol": "video"}, headers=super_admin.headers), 400)
    data = ok_data(client.post(f"{AI}/health/probe", json={"capability": "geo_check", "model": "mock-text"}, headers=super_admin.headers))
    assert data["capability"] == "geo_check" and data["protocol"] == "openai_chat" and data["status"] == "healthy"
    assert data["error_category"] is None and data["checked_at"].endswith("Z")
    root = synced.scalar(select(AiTask).where(AiTask.capability == "geo_check", AiTask.root_task_id.is_(None)))
    global_geo = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "geo_check", CapabilityRoute.project_id == 0))
    assert root.target_id == global_geo and root.created_by == super_admin.id
    video = ok_data(client.post(f"{AI}/health/probe", json={"capability": "video", "model": "mock-video"}, headers=super_admin.headers))
    assert video["status"] == "healthy" and video["protocol"] == "video"


# =====================================================================
# AI 任务
# =====================================================================


def test_tasks_scope_detail_and_filters(client: TestClient, db: Session, super_admin: User, owner_a: User, pa: Project, pb: Project) -> None:
    ta = root_task(db, pa, target_type="keyword", target_id=1)
    attempt_row(db, ta, request_id="req-a")
    tb = root_task(db, pb, target_type="keyword", target_id=2)
    probe = root_task(db, None, status="succeeded", operation="route_probe", trigger_type="health_probe", target_type="route_probe")
    a_page = ok_data(client.get(f"{AI}/tasks", headers=owner_a.headers))
    assert [i["id"] for i in a_page["items"]] == [ta.id] and a_page["total"] == 1
    s_page = ok_data(client.get(f"{AI}/tasks", headers=super_admin.headers))
    assert {i["id"] for i in s_page["items"]} == {ta.id, tb.id, probe.id}
    # 总后台按用户筛选 = 该用户本人所见
    assert [i["id"] for i in ok_data(client.get(f"{AI}/tasks", params={"owner_id": owner_a.id}, headers=super_admin.headers))["items"]] == [ta.id]
    attempts = ok_data(client.get(f"{AI}/tasks", params={"row_kind": "attempt"}, headers=owner_a.headers))
    assert attempts["total"] == 1 and attempts["items"][0]["root_task_id"] == ta.id and attempts["items"][0]["input"] is None
    assert ok_data(client.get(f"{AI}/tasks", params={"row_kind": "all", "request_id": "req-a"}, headers=super_admin.headers))["total"] == 1
    assert ok_data(client.get(f"{AI}/tasks", params={"trigger_type": "health_probe"}, headers=super_admin.headers))["total"] == 1
    err(client.get(f"{AI}/tasks", params={"row_kind": "bogus"}, headers=super_admin.headers), 400)
    err(client.get(f"{AI}/tasks", params={"status": "bogus"}, headers=super_admin.headers), 400)
    detail = ok_data(client.get(f"{AI}/tasks/{ta.id}", headers=owner_a.headers))
    assert [a["request_id"] for a in detail["attempts"]] == ["req-a"] and "request_payload" in detail and "response_meta" in detail
    assert err(client.get(f"{AI}/tasks/{tb.id}", headers=owner_a.headers), 404) == err(client.get(f"{AI}/tasks/999999", headers=owner_a.headers), 404)
    err(client.get(f"{AI}/tasks/{probe.id}", headers=owner_a.headers), 404)


def test_tasks_retry_rules(client: TestClient, synced: Session, owner_a: User, pa: Project, pb: Project) -> None:
    batch = GenerationBatch(project_id=pa.id, kind="keyword", template_id=1, template_version=1, task_total=1, task_failed=1,
                            status="failed", created_by=owner_a.id)
    synced.add(batch)
    synced.commit()
    failed = root_task(synced, pa, batch_id=batch.id, target_type="generation_batch", target_id=batch.id, input={"count": 3})
    data = ok_data(client.post(f"{AI}/tasks/{failed.id}/retry", headers=owner_a.headers))
    new = synced.get(AiTask, data["task_id"])
    assert new.parent_task_id == failed.id and new.status == "queued" and new.project_id == pa.id and new.created_by == owner_a.id
    assert str(new.id) in redis_client.lrange("queue:ai_tasks", 0, -1)
    synced.refresh(batch)
    assert (batch.task_failed, batch.status) == (0, "running")
    assert err(client.post(f"{AI}/tasks/{failed.id}/retry", headers=owner_a.headers), 409)["data"] == {"existing_id": new.id}
    # 媒体 / 收录检测 / 探测 / 非失败状态
    media = root_task(synced, pa, capability="image", operation="image_generate", target_type="media_asset", target_id=55)
    body = err(client.post(f"{AI}/tasks/{media.id}/retry", headers=owner_a.headers), 409)
    assert body["message"] == "请通过素材重试接口重试" and body["data"] == {"hint": "POST /admin/media/assets/55/retry"}
    seo = root_task(synced, pa, capability="seo_check", operation="seo_check", target_type="publish_link", target_id=9, trigger_type="worker")
    assert err(client.post(f"{AI}/tasks/{seo.id}/retry", headers=owner_a.headers), 409)["data"] == {"hint": "POST /admin/links/9/index-check"}
    ok_task = root_task(synced, pa, status="succeeded", target_type="keyword", target_id=1)
    assert err(client.post(f"{AI}/tasks/{ok_task.id}/retry", headers=owner_a.headers), 409)["data"] == {"current_status": "succeeded"}
    # 不可见 → 404
    other = root_task(synced, pb, target_type="keyword", target_id=1)
    err(client.post(f"{AI}/tasks/{other.id}/retry", headers=owner_a.headers), 404)


def test_tasks_cancel_rules(client: TestClient, db: Session, owner_a: User, read_only: User, pa: Project) -> None:
    queued = root_task(db, pa, status="queued", target_type="keyword", target_id=1)
    item = ok_data(client.post(f"{AI}/tasks/{queued.id}/cancel", headers=owner_a.headers))
    assert item["status"] == "cancelled" and item["error_category"] == "cancelled"
    running = root_task(db, pa, status="running", target_type="keyword", target_id=1)
    assert err(client.post(f"{AI}/tasks/{running.id}/cancel", headers=owner_a.headers), 409)["data"] == {"current_status": "running"}
    sync_root = root_task(db, pa, status="running", capability="geo_check", operation="geo_check", trigger_type="worker")
    err(client.post(f"{AI}/tasks/{sync_root.id}/cancel", headers=owner_a.headers), 409)
    body = err(client.post(f"{AI}/tasks/{queued.id}/cancel", headers=read_only.headers), 403)
    assert body["data"] == {"permission": "ai.tasks.cancel"}


def test_tasks_export_csv(client: TestClient, db: Session, super_admin: User, owner_a: User, pa: Project, pb: Project) -> None:
    ta = root_task(db, pa, target_type="keyword", target_id=1)
    attempt_row(db, ta, request_id="req-a")
    tb = root_task(db, pb, target_type="keyword", target_id=1)
    attempt_row(db, tb, request_id="req-b")
    response = client.get(f"{AI}/tasks/export", headers=owner_a.headers)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"] and "ai-tasks-" in response.headers["content-disposition"]
    text = response.content.decode("utf-8")
    assert text.startswith("﻿ID,根任务 ID") and "req-a" in text and "req-b" not in text
    full = client.get(f"{AI}/tasks/export", params={"row_kind": "root"}, headers=super_admin.headers).content.decode("utf-8")
    assert len(full.strip().splitlines()) == 3


# =====================================================================
# 用量对账
# =====================================================================


def test_usage_reconcile_logs_summary_last_pull(client: TestClient, synced: Session, super_admin: User, owner_a: User, operator: User,
                                                pa: Project) -> None:
    assert ok_data(client.get(f"{AI}/usage/last-pull", headers=super_admin.headers)) is None
    root = root_task(synced, pa, status="succeeded", target_type="keyword", target_id=1)
    mine = attempt_row(synced, root, request_id="mock-req-a")
    redis_client.lpush("mock:usage_logs", json.dumps({
        "id": 1, "type": 2, "request_id": "mock-req-a", "model_name": "mock-text", "quota": 120, "prompt_tokens": 10,
        "completion_tokens": 5, "created_at": int(utcnow().timestamp()),
    }))
    redis_client.lpush("mock:usage_logs", json.dumps({"id": 2, "type": 2, "request_id": "unknown-req", "model_name": "mock-text", "quota": 7}))
    result = ok_data(client.post(f"{AI}/usage/reconcile", headers=super_admin.headers))
    assert result["pulled"] == 2 and result["new"] == 2 and result["matched"] == 1 and result["unmatched"] == 1
    synced.expire_all()
    assert synced.get(AiTask, mine.id).quota_actual == 120
    # 日志范围：未匹配条目只对总后台可见
    s_logs = ok_data(client.get(f"{AI}/usage/logs", headers=super_admin.headers))
    a_logs = ok_data(client.get(f"{AI}/usage/logs", headers=owner_a.headers))
    assert s_logs["total"] == 2 and a_logs["total"] == 1 and a_logs["items"][0]["request_id"] == "mock-req-a"
    assert a_logs["items"][0]["raw"]["request_id"] == "mock-req-a" and a_logs["items"][0]["matched"] is True
    assert ok_data(client.get(f"{AI}/usage/logs", params={"matched": False}, headers=super_admin.headers))["total"] == 1
    assert ok_data(client.get(f"{AI}/usage/logs", params={"log_type": 6}, headers=super_admin.headers))["total"] == 0
    err(client.get(f"{AI}/usage/logs", params={"log_type": 3}, headers=super_admin.headers), 400)
    # last-pull：all 完整、own 只有 pulled_at / window_overflow、带 owner_id 同 own
    full = ok_data(client.get(f"{AI}/usage/last-pull", headers=super_admin.headers))
    assert full["pulled"] == 2 and full["matched"] == 1 and isinstance(full["request_ids"], list)
    assert set(ok_data(client.get(f"{AI}/usage/last-pull", headers=owner_a.headers))) == {"pulled_at", "window_overflow"}
    assert set(ok_data(client.get(f"{AI}/usage/last-pull", params={"owner_id": owner_a.id}, headers=super_admin.headers))) == {"pulled_at", "window_overflow"}
    # 汇总按可见尝试行
    s_sum = ok_data(client.get(f"{AI}/usage/summary", params={"group_by": "project"}, headers=super_admin.headers))
    assert s_sum == [{"key": str(pa.id), "calls": 1, "prompt_tokens": 10, "completion_tokens": 5, "quota_estimated": 100,
                      "quota_actual": 120, "cost_cny": s_sum[0]["cost_cny"], "reconciled_rate": 1.0}]
    assert ok_data(client.get(f"{AI}/usage/summary", params={"group_by": "project"}, headers=operator.headers)) == []
    err(client.get(f"{AI}/usage/summary", params={"group_by": "owner"}, headers=super_admin.headers), 400)
    err(client.get(f"{AI}/usage/summary", params={"start": "2026-01-01", "end": "2027-06-01"}, headers=super_admin.headers), 400)
    # 权限：operator 无 ai.usage.reconcile；对账进行中 409
    assert err(client.post(f"{AI}/usage/reconcile", headers=operator.headers), 403)["data"] == {"permission": "ai.usage.reconcile"}
    redis_client.set("lock:worker:reconcile", "x", ex=60)
    err(client.post(f"{AI}/usage/reconcile", headers=super_admin.headers), 409)
    assert synced.scalar(select(AiUsageLog.id).where(AiUsageLog.request_id == "unknown-req")) is not None


def test_audit_target_types_for_ai_routes(client: TestClient, synced: Session, super_admin: User) -> None:
    """``/admin/ai/health/probe`` 与 ``/admin/ai/usage/reconcile`` 的审计 ``target_id`` 固定为 NULL。"""
    ok_data(client.post(f"{AI}/health/probe", json={"capability": "keyword", "model": "mock-text"}, headers=super_admin.headers))
    ok_data(client.post(f"{AI}/usage/reconcile", headers=super_admin.headers))
    logs = ok_data(client.get(f"{ADMIN_API}/admin-operation-logs", headers=super_admin.headers))["items"]
    by_type = {i["target_type"]: i for i in logs}
    assert by_type["ai_model"]["target_id"] is None and by_type["ai_usage_log"]["target_id"] is None
    assert by_type["ai_model"]["action"] == "execute"

