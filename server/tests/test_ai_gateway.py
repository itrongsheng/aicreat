"""AI 网关编排服务测试（docs/08 §16.2）：路由解析、候选链、额度、暂停回滚、尝试行、终态汇总、对账、目录同步、告警、实时计数、
根任务生命周期与管理接口。上游一律为 Mock 客户端或 ``httpx.MockTransport``（不访问网络）；Redis 使用 db 15。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import BusinessError
from app.core.redis import cache_get_json, redis_client
from app.core.zhiqi import mock as zhiqi_mock
from app.core.zhiqi.client import ZhiqiClient
from app.core.zhiqi.errors import ZhiqiBreakerOpen, ZhiqiError
from app.core.zhiqi.types import (
    Capability,
    ErrorCategory,
    ImageRequest,
    TaskStatus,
    Timeouts,
    VideoRequest,
)
from app.models import (
    AiModel,
    AiTask,
    AiUsageLog,
    Alert,
    CapabilityRoute,
    GenerationBatch,
    Project,
    utcnow,
)
from app.services import (
    ai_catalog_service,
    ai_task_service,
    ai_usage_service,
    alert_service,
    settings_service,
    stats_service,
)
from app.services import (
    ai_gateway_service as gw,
)
from app.services.data_scope_service import SYSTEM_SCOPE, DataScope

pytestmark = pytest.mark.usefixtures("redis_required")


# =====================================================================
# 夹具与工具
# =====================================================================


@pytest.fixture
def gdb(db: Session) -> Session:
    """默认配置 + 8 条全局路由（Mock 模式），模型目录未同步（目录缺失时协议取路由默认）。"""
    gw.ensure_default_routes(db)
    return db


@pytest.fixture
def synced(gdb: Session) -> Session:
    ai_catalog_service.sync_models(gdb)
    return gdb


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
    """测试结束后恢复处理器注册表与批次钩子。"""
    saved = dict(ai_task_service.REGISTRY)
    yield
    ai_task_service.REGISTRY.clear()
    ai_task_service.REGISTRY.update(saved)
    ai_task_service.set_batch_finished_hook(None)


def set_route(db: Session, capability: str, primary: str, fallbacks: list[str] | None = None, *, project_id: int = 0, **values: Any) -> CapabilityRoute:
    row = db.scalar(select(CapabilityRoute).where(CapabilityRoute.capability == capability, CapabilityRoute.project_id == project_id))
    if row is None:
        row = CapabilityRoute(capability=capability, project_id=project_id, protocol=values.pop("protocol", "openai_chat"), primary_model=primary)
        db.add(row)
    row.primary_model = primary
    row.fallback_models_json = json.dumps(fallbacks or [])
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    gw.invalidate_routes_cache()
    return row


def add_model(db: Session, model_id: str, *, available: bool = True, types: list[str] | None = None, **values: Any) -> AiModel:
    from app.core.zhiqi import catalog

    types = types if types is not None else ["openai"]
    row = AiModel(
        model_id=model_id, is_available=available, synced_at=utcnow(), last_seen_at=utcnow(),
        supported_endpoint_types_json=json.dumps(types), modalities_json=json.dumps(sorted(catalog.derive_modalities(types))), **values,
    )
    db.add(row)
    db.commit()
    ai_catalog_service.invalidate_model_caches()
    return row


def make_root(db: Session, capability: str = "keyword", operation: str = "keyword_generate", **kw: Any) -> AiTask:
    kw.setdefault("project_id", None)
    kw.setdefault("created_by", None)
    task = gw.create_root_task(db, capability=capability, operation=operation, enqueue=kw.pop("enqueue", False), **kw)
    db.commit()
    return task


def attempts_of(db: Session, root: AiTask) -> list[AiTask]:
    db.expire_all()
    return list(db.scalars(select(AiTask).where(AiTask.root_task_id == root.id).order_by(AiTask.id)))


def update_config(db: Session, key: str, value: dict[str, Any]) -> None:
    settings_service.set_value(db, key, value)


MESSAGES = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "生成关键词"}]


def chat_body(text: str = "hello", *, usage: bool = True) -> dict[str, Any]:
    body: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
    if usage:
        body["usage"] = {"prompt_tokens": 100, "completion_tokens": 50}
    return body


class FakeUpstream:
    """``httpx.MockTransport`` 上游：``routes[(method, path)] = callable(request, model) -> (status, body)``，记录调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.handler: Callable[[httpx.Request, str | None], tuple[int, Any]] = lambda r, m: (200, chat_body())
        self.seq = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seq += 1
        model = None
        if request.headers.get("content-type", "").startswith("application/json") and request.content:
            model = json.loads(request.content).get("model")
        self.calls.append((request.method, request.url.path, model))
        status, body = self.handler(request, model)
        if isinstance(body, Exception):
            raise body
        headers = {"x-oneapi-request-id": f"req-{self.seq}"}
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers=headers)
        return httpx.Response(status, json=body, headers=headers)


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> FakeUpstream:
    fake = FakeUpstream()
    client = ZhiqiClient(
        base_url="https://zhiqi.test/v1", api_key="sk-test", timeouts=Timeouts(), user_agent="test",
        transport=httpx.MockTransport(fake), sleep=lambda _s: None,
    )
    monkeypatch.setattr(gw, "get_client", lambda: client)
    monkeypatch.setattr(ai_task_service, "get_client", lambda: client)
    return fake


# =====================================================================
# 默认路由与路由解析（§6.2、§6.3、§6.4）
# =====================================================================


def test_ensure_default_routes_mock_seed_is_idempotent(db: Session) -> None:
    gw.ensure_default_routes(db)
    gw.ensure_default_routes(db)
    rows = {r.capability: r for r in db.scalars(select(CapabilityRoute).where(CapabilityRoute.project_id == 0))}
    assert set(rows) == {"keyword", "title", "content", "rewrite", "image", "video", "geo_check", "seo_check"}
    assert rows["keyword"].primary_model == "mock-text" and rows["keyword"].protocol == "openai_chat"
    assert rows["image"].primary_model == "mock-image" and rows["image"].protocol == "image_async"
    assert rows["video"].primary_model == "mock-video" and rows["video"].protocol == "video"
    assert json.loads(rows["video"].params_json) == {"resolution": "720p", "duration": 5, "aspect_ratio": "16:9", "generate_audio": False}
    assert json.loads(rows["title"].params_json) == {"temperature": 0.8, "max_tokens": 1024}
    assert all(r.is_enabled and r.max_attempts == 3 and r.timeout_seconds is None for r in rows.values())


def test_ensure_default_routes_live_mode_replaces_mock_models(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    gw.ensure_default_routes(db)
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-live")
    monkeypatch.setattr(settings, "zhiqi_text_default_model", "gpt-live")
    monkeypatch.setattr(settings, "zhiqi_image_default_model", "")
    gw.ensure_default_routes(db)
    rows = {r.capability: r for r in db.scalars(select(CapabilityRoute).where(CapabilityRoute.project_id == 0))}
    assert rows["content"].primary_model == "gpt-live" and rows["content"].is_enabled
    assert rows["image"].primary_model == "mock-image" and not rows["image"].is_enabled


def test_ensure_default_routes_live_mode_empty_env_disables(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-live")
    monkeypatch.setattr(settings, "zhiqi_text_default_model", "")
    gw.ensure_default_routes(db)
    row = db.scalar(select(CapabilityRoute).where(CapabilityRoute.capability == "keyword"))
    assert row.primary_model == "" and not row.is_enabled
    with pytest.raises(BusinessError) as exc:
        gw.resolve_route(db, Capability.KEYWORD, None)
    assert exc.value.code == 5031 and exc.value.http_status == 503
    assert exc.value.data == {"capability": "keyword", "breaker_open": [], "unavailable_models": [], "paused_reason": None}


def test_resolve_route_project_override_timeout_and_cache(gdb: Session, project: Project) -> None:
    set_route(gdb, "content", "g-primary", ["g-fb"], timeout_seconds=90)
    set_route(gdb, "content", "p-primary", ["p-fb"], project_id=project.id, params_json=json.dumps({"max_tokens": 10}))
    route = gw.resolve_route(gdb, Capability.CONTENT, project.id)
    assert route.candidates == ["p-primary", "p-fb"] and route.params == {"max_tokens": 10}
    assert route.timeout_seconds == 90 and route.route_project_id == project.id  # 项目行为空 → 全局行
    assert cache_get_json(f"cache:routes:content:{project.id}")["primary_model"] == "p-primary"
    glob = gw.resolve_route(gdb, Capability.CONTENT, None)
    assert glob.candidates == ["g-primary", "g-fb"] and glob.timeout_seconds == 90
    # 写路由后缓存失效
    set_route(gdb, "content", "g2", [], timeout_seconds=None)
    route = gw.resolve_route(gdb, Capability.CONTENT, None)
    assert route.candidates == ["g2"] and route.timeout_seconds == 180  # ai_routing_config.timeouts.text_seconds
    assert gw.resolve_route(gdb, Capability.IMAGE, None).timeout_seconds == 60  # submit_seconds


def test_resolve_route_skips_unavailable_and_keeps_unknown(gdb: Session) -> None:
    set_route(gdb, "keyword", "gone", ["unsynced", "ok"])
    add_model(gdb, "gone", available=False)
    add_model(gdb, "ok", available=True)
    route = gw.resolve_route(gdb, Capability.KEYWORD, None)
    assert route.candidates == ["unsynced", "ok"] and route.unavailable_models == ["gone"]
    set_route(gdb, "keyword", "gone", [])
    with pytest.raises(BusinessError) as exc:
        gw.resolve_route(gdb, Capability.KEYWORD, None)
    assert exc.value.code == 5031 and exc.value.data["unavailable_models"] == ["gone"]
    assert exc.value.message == "能力暂不可用"


def test_resolve_route_model_override_and_disabled(gdb: Session) -> None:
    set_route(gdb, "keyword", "a", ["b"])
    route = gw.resolve_route(gdb, Capability.KEYWORD, None, model_override="x")
    assert route.candidates == ["x"] and route.model_override == "x"
    set_route(gdb, "keyword", "a", ["b"], is_enabled=False)
    with pytest.raises(BusinessError) as exc:
        gw.resolve_route(gdb, Capability.KEYWORD, None)
    assert exc.value.code == 5031 and exc.value.data["paused_reason"] is None


def test_validate_model_override_and_preflight(synced: Session) -> None:
    gw.validate_model_override(synced, "keyword", "mock-text")
    for bad in ("mock-image", "nope"):
        with pytest.raises(BusinessError) as exc:
            gw.validate_model_override(synced, "keyword", bad)
        assert exc.value.code == 400 and exc.value.data == {"model": bad}
    breaker = gw.get_breaker(gw._cfg(synced))
    breaker.force_open("keyword", "mock-text", reason="manual")
    with pytest.raises(BusinessError) as exc:
        gw.preflight(synced, "keyword", None, model_override="mock-text")
    assert exc.value.code == 5031 and exc.value.data["breaker_open"] == ["mock-text"] and exc.value.data["hint"] == "model_override"
    breaker.reset("keyword", "mock-text")
    gw.set_paused("quota_exceeded", 60)
    with pytest.raises(BusinessError) as exc:
        gw.preflight(synced, "keyword", None)
    assert exc.value.data["paused_reason"] == "quota_exceeded"
    assert gw.check_paused() == "quota_exceeded"
    assert gw.clear_paused() and gw.check_paused() is None


# =====================================================================
# 文本调用与尝试行（§5.13、§7.4、§9.2）
# =====================================================================


def test_complete_text_mock_success_records_attempt(synced: Session, project: Project, owner: Any) -> None:
    root = make_root(synced, project_id=project.id, created_by=owner.id, batch_id=None, target_type="keyword", target_id=7, template_id=3)
    result, attempt = gw.complete_text(synced, root_task=root, messages=MESSAGES, params={"max_tokens": 256}, response_format="json")
    assert result.text and result.request_id.startswith("mock-")
    assert attempt.status == "succeeded" and attempt.root_task_id == root.id
    for col in ("project_id", "capability", "operation", "trigger_type", "target_type", "target_id", "batch_id", "template_id", "created_by"):
        assert getattr(attempt, col) == getattr(root, col), col
    keyword_route = synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "keyword", CapabilityRoute.project_id == 0))
    assert attempt.route_id == root.route_id == keyword_route
    assert attempt.protocol == "openai_chat" and attempt.candidate_index == 0 and attempt.attempt == 1
    assert attempt.request_id == result.request_id and attempt.http_status == 200
    assert attempt.prompt_tokens > 0 and attempt.quota_estimated == attempt.prompt_tokens + attempt.completion_tokens
    assert attempt.cost_cny == (Decimal(attempt.quota_estimated) / Decimal(500000) * Decimal("7.2")).quantize(Decimal("0.000001"))
    payload = json.loads(attempt.request_payload_json)
    assert payload["max_tokens"] == 256 and payload["response_format"] == {"type": "json_object"} and "metadata" not in payload
    meta = json.loads(attempt.response_meta_json)
    assert meta["usage_missing"] is False and meta["finish_reason"]
    # 实时计数：尝试行终态写 stats:rt（项目键与 0 汇总键）
    today = stats_service.today_date(synced)
    for pid in (project.id, 0):
        data = redis_client.hgetall(stats_service.realtime_key(today, pid))
        assert data["ai_calls"] == "1" and data["ai_succeeded"] == "1" and int(data["quota_estimated"]) == attempt.quota_estimated


def test_complete_text_breaker_open_primary_falls_back(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", ["m2"])
    gw.get_breaker(gw._cfg(gdb)).force_open("keyword", "m1", reason="manual")
    root = make_root(gdb)
    _, attempt = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    rows = attempts_of(gdb, root)
    assert [(r.model, r.status, r.error_category, r.candidate_index, r.request_id) for r in rows] == [
        ("m1", "failed", "breaker_open", 0, None),
        ("m2", "succeeded", None, 1, attempt.request_id),
    ]
    assert [c[2] for c in upstream.calls] == ["m2"]


def test_complete_text_unsupported_parameter_degrade_then_fallback(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", ["m2"])
    upstream.handler = lambda r, m: (400, {"error": {"message": "unsupported_parameter: temperature"}}) if m == "m1" else (200, chat_body())
    root = make_root(gdb)
    _, attempt = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params={"temperature": 0.3}, response_format="json")
    rows = attempts_of(gdb, root)
    assert [(r.model, r.attempt, r.status, r.error_category) for r in rows] == [
        ("m1", 1, "failed", "unsupported_parameter"),
        ("m1", 2, "failed", "unsupported_parameter"),
        ("m2", 1, "succeeded", None),
    ]
    degraded_payload = json.loads(rows[1].request_payload_json)
    assert "max_completion_tokens" in degraded_payload and "temperature" not in degraded_payload
    assert set(json.loads(rows[1].response_meta_json)["degraded_params"]) >= {"temperature", "response_format", "max_tokens"}
    assert attempt.id == rows[2].id


def test_complete_text_invalid_response_regenerates_once(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", [])
    calls = {"n": 0}

    def validator(result: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "输出不是 JSON")
        return {"ok": True}

    root = make_root(gdb)
    result, _ = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="json", validator=validator)
    rows = attempts_of(gdb, root)
    assert [(r.attempt, r.status, r.error_category) for r in rows] == [(1, "failed", "invalid_response"), (2, "succeeded", None)]
    assert rows[0].request_id and rows[0].prompt_tokens == 100 and rows[0].quota_estimated > 0  # 已计费照常记成本
    assert result.parsed == {"ok": True}


def test_complete_text_rate_limited_retries_then_all_fail_5031(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", ["m2"], max_attempts=2)
    upstream.handler = lambda r, m: (429, {"error": {"message": "too many requests"}})
    root = make_root(gdb)
    with pytest.raises(BusinessError) as exc:
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.code == 5031
    assert exc.value.data == {"capability": "keyword", "breaker_open": [], "unavailable_models": [], "paused_reason": None}
    assert exc.value.error_category == "rate_limited"
    rows = attempts_of(gdb, root)
    assert [(r.model, r.attempt) for r in rows] == [("m1", 1), ("m1", 2), ("m2", 1), ("m2", 2)]
    assert all(r.error_category == "rate_limited" and r.request_id for r in rows)
    meta = json.loads(rows[0].response_meta_json)
    assert len(meta["retry_request_ids"]) == 3  # 客户端 HTTP 幂等重试 3 次不新建尝试行


def test_complete_text_content_blocked_no_fallback(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", ["m2"])
    upstream.handler = lambda r, m: (400, {"error": {"message": "content_policy violation"}})
    root = make_root(gdb)
    with pytest.raises(BusinessError) as exc:
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.code == 5021 and exc.value.http_status == 502
    assert exc.value.data == {"error_category": "content_blocked", "request_id": "req-1", "model": "m1", "hint": "prompt_blocked"}
    assert len(attempts_of(gdb, root)) == 1


def test_complete_text_model_override_failure_hint(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", ["m2"])
    upstream.handler = lambda r, m: (503, {"error": {"message": "service unavailable"}})
    root = make_root(gdb, input={"model": "ovr"})
    with pytest.raises(BusinessError) as exc:
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.code == 5021 and exc.value.data["hint"] == "model_override" and exc.value.data["model"] == "ovr"
    assert [r.model for r in attempts_of(gdb, root)] == ["ovr"]
    # 覆盖模型熔断打开 → 5031 hint=model_override
    gw.get_breaker(gw._cfg(gdb)).force_open("keyword", "ovr", reason="manual")
    root2 = make_root(gdb, input={"model": "ovr"})
    with pytest.raises(BusinessError) as exc:
        gw.complete_text(gdb, root_task=root2, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.code == 5031 and exc.value.data["hint"] == "model_override" and exc.value.data["breaker_open"] == ["ovr"]


def test_complete_text_no_text_endpoint_records_model_unrouted(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "img-only", ["m2"])
    add_model(gdb, "img-only", types=["image-generation"])
    root = make_root(gdb)
    gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    rows = attempts_of(gdb, root)
    assert (rows[0].model, rows[0].error_category, rows[0].request_id, rows[0].protocol) == ("img-only", "model_unrouted", None, None)
    assert rows[1].status == "succeeded" and [c[2] for c in upstream.calls] == ["m2"]


def test_complete_text_usage_missing_estimates_tokens(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", [])
    upstream.handler = lambda r, m: (200, chat_body("abcdabcd", usage=False))
    root = make_root(gdb)
    _, attempt = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert json.loads(attempt.response_meta_json)["usage_missing"] is True
    assert attempt.completion_tokens == 2 and attempt.prompt_tokens > 0


def test_complete_text_target_url_metadata_not_in_payload(gdb: Session, monkeypatch: pytest.MonkeyPatch, project: Project, owner: Any) -> None:
    from app.models import Content, PublishLink, PublishPlatform

    platform = PublishPlatform(code="zhihu-t", name="知乎", name_en="Zhihu")
    gdb.add(platform)
    gdb.flush()
    content = Content(project_id=project.id, title="t", language="zh-CN", style="news", created_by=owner.id, updated_by=owner.id)
    gdb.add(content)
    gdb.flush()
    link = PublishLink(
        project_id=project.id, content_id=content.id, platform_id=platform.id, url="https://example.com/p/1",
        normalized_url="https://example.com/p/1", url_hash="h" * 64, domain="example.com", published_at=utcnow(), backfilled_by=owner.id,
        title_snapshot="t",
    )
    gdb.add(link)
    gdb.commit()
    seen: dict[str, Any] = {}
    original = zhiqi_mock.MockZhiqiClient.request

    def spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        seen["metadata"] = kw.get("metadata")
        seen["json"] = kw.get("json")
        return original(self, method, path, **kw)

    monkeypatch.setattr(zhiqi_mock.MockZhiqiClient, "request", spy)
    root = make_root(gdb, capability="geo_check", operation="geo_check", project_id=project.id, target_type="publish_link", target_id=link.id, sync=True)
    _, attempt = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text", model_override="mock-text")
    assert seen["metadata"]["target_url"] == "https://example.com/p/1" and seen["metadata"]["capability"] == "geo_check"
    assert "example.com/p/1" not in attempt.request_payload_json


# =====================================================================
# 全局暂停与回滚（§8.5）
# =====================================================================


def test_quota_exceeded_pauses_rolls_back_and_alerts(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", ["m2"])
    upstream.handler = lambda r, m: (402, {"error": {"message": "insufficient quota"}})
    root = make_root(gdb)
    root.status, root.locked_by = "running", "w:1"
    gdb.commit()
    with pytest.raises(ZhiqiError) as exc:
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.category == ErrorCategory.QUOTA_EXCEEDED
    gdb.refresh(root)
    assert (root.status, root.pause_count, root.locked_by, root.started_at) == ("queued", 1, None, None)
    assert redis_client.exists("ai:paused:quota_exceeded") and redis_client.ttl("ai:paused:quota_exceeded") > 500
    alert = gdb.scalar(select(Alert).where(Alert.alert_type == "ai_quota_exceeded"))
    assert alert.severity == "critical" and alert.target_type == "system" and alert.target_key == "" and alert.project_id is None
    assert len(attempts_of(gdb, root)) == 1  # 不切换候选
    # 暂停中：调用前回滚，不发 HTTP
    root.status, root.locked_by = "running", "w:1"
    gdb.commit()
    with pytest.raises(ZhiqiBreakerOpen):
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    gdb.refresh(root)
    assert root.status == "queued" and root.pause_count == 2 and len(upstream.calls) == 1
    # 第 3 次 → 不再回滚，按常规失败
    root.status, root.locked_by = "running", "w:1"
    gdb.commit()
    with pytest.raises(ZhiqiError) as exc:
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    gdb.refresh(root)
    assert exc.value.category == ErrorCategory.QUOTA_EXCEEDED and root.status == "running" and root.pause_count == 3


def test_auth_failed_sync_root_not_rolled_back(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "seo_check", "m1", [])
    upstream.handler = lambda r, m: (401, {"error": {"message": "invalid token"}})
    root = make_root(gdb, capability="seo_check", operation="seo_check", sync=True)
    with pytest.raises(ZhiqiError):
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    gdb.refresh(root)
    assert root.status == "running" and gw.check_paused() == "auth_failed"
    assert gdb.scalar(select(Alert).where(Alert.alert_type == "ai_auth_failed")) is not None


def test_guard_abandons_when_task_taken_over(gdb: Session) -> None:
    root = make_root(gdb)
    root.status, root.locked_by = "running", "w:1"
    gdb.commit()
    gdb.execute(AiTask.__table__.update().where(AiTask.id == root.id).values(locked_by="w:2"))
    gdb.commit()
    root.locked_by = "w:1"
    with pytest.raises(gw.TaskAbandoned):
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")


# =====================================================================
# 熔断与告警（§8.4）
# =====================================================================


def test_breaker_opens_with_alert_and_closes_on_success(gdb: Session, upstream: FakeUpstream) -> None:
    update_config(gdb, "ai_routing_config", {"breaker": {"failure_threshold": 2, "window_seconds": 300, "open_seconds": 1}})
    set_route(gdb, "keyword", "m1", [], max_attempts=1)
    upstream.handler = lambda r, m: (500, {"error": {"message": "boom"}})
    for _ in range(2):
        with pytest.raises(BusinessError):
            gw.complete_text(gdb, root_task=make_root(gdb), messages=MESSAGES, params=None, response_format="text")
    alert = gdb.scalar(select(Alert).where(Alert.alert_type == "ai_breaker_open"))
    assert alert.status == "open" and alert.target_type == "ai_model" and alert.target_key == "keyword:m1" and alert.severity == "warning"
    breaker = gw.get_breaker(gw._cfg(gdb))
    assert breaker.state("keyword", "m1") == "open"
    time.sleep(1.1)  # open_seconds 到期 → half_open
    upstream.handler = lambda r, m: (200, chat_body())
    gw.complete_text(gdb, root_task=make_root(gdb), messages=MESSAGES, params=None, response_format="text")
    gdb.refresh(alert)
    assert breaker.state("keyword", "m1") == "closed"
    assert alert.status == "resolved" and alert.resolution_note == "auto" and alert.resolved_by is None


# =====================================================================
# 额度（§7.6）
# =====================================================================


def test_check_quota_limits_settle_and_warning(gdb: Session, project: Project) -> None:
    update_config(gdb, "generation_config", {"quota": {"daily_limit": 1000, "project_monthly_limit": 800, "warn_percent": 80}})
    today = stats_service.today_date(gdb)
    daily_key = f"quota:daily:{today.isoformat()}"
    project_key = f"quota:project:{project.id}:{today.strftime('%Y-%m')}"
    t1 = make_root(gdb, project_id=project.id)
    gw.check_quota(gdb, project_id=project.id, estimated_quota=600, task=t1)
    assert t1.quota_reserved == 600 and redis_client.get(daily_key) == "600" and redis_client.get(project_key) == "600"
    assert 0 < redis_client.ttl(daily_key) <= 172800 and redis_client.ttl(project_key) > 172800
    assert gw.quota_warning(gdb, project_id=project.id) is None
    t2 = make_root(gdb, project_id=project.id)
    with pytest.raises(BusinessError) as exc:
        gw.check_quota(gdb, project_id=project.id, estimated_quota=300, task=t2)
    assert exc.value.code == 4291 and exc.value.http_status == 429
    assert exc.value.data == {"scope": "project_monthly", "limit": 800, "used": 900}
    assert redis_client.get(daily_key) == "600" and redis_client.get(project_key) == "600" and t2.quota_reserved == 0
    with pytest.raises(BusinessError) as exc:
        gw.check_quota(gdb, project_id=None, estimated_quota=500, task=t2)
    assert exc.value.data == {"scope": "daily", "limit": 1000, "used": 1100}
    gw.check_quota(gdb, project_id=project.id, estimated_quota=100, task=t2)
    assert gw.quota_warning(gdb, project_id=project.id) == {"scope": "project_monthly", "limit": 800, "used": 700, "percent": 87}
    # 结算：失败 actual=0 → 释放预占（提交后）
    gw.finalize_root(gdb, t1, "failed", error_category="timeout")
    assert redis_client.get(daily_key) == "700"
    gdb.commit()
    assert redis_client.get(daily_key) == "100" and redis_client.get(project_key) == "100"
    # 不限（0）不预警
    update_config(gdb, "generation_config", {"quota": {"daily_limit": 0, "project_monthly_limit": 0}})
    assert gw.quota_warning(gdb, project_id=project.id) is None


def test_settle_quota_positive_delta_and_rollback(gdb: Session) -> None:
    task = make_root(gdb)
    gw.check_quota(gdb, project_id=None, estimated_quota=50, task=task)
    gw.settle_quota(gdb, task, reserved=50, actual=80)
    gdb.rollback()  # 事务回滚 → 提交后动作丢弃
    key = f"quota:daily:{stats_service.today_date(gdb).isoformat()}"
    assert redis_client.get(key) == "50"
    gw.settle_quota(gdb, task, reserved=50, actual=80)
    gdb.commit()
    assert redis_client.get(key) == "80"


def test_estimate_for_quota_types(gdb: Session) -> None:
    add_model(gdb, "per-token", model_ratio=Decimal("2.5"), completion_ratio=Decimal(4), quota_type=0)
    add_model(gdb, "per-call", types=["image-generation"], quota_type=1, model_price=Decimal("0.02"))
    assert gw.estimate_for(gdb, "per-token", 1200, 800) == 11000
    assert gw.estimate_for(gdb, "per-call", 999, 0) == 10000
    assert gw.estimate_for(gdb, "unknown-model", 10, 5) == 15


# =====================================================================
# finalize_root（§7.4 第 3 条）
# =====================================================================


def test_finalize_root_sums_attempts_and_realtime(gdb: Session, upstream: FakeUpstream, project: Project) -> None:
    set_route(gdb, "keyword", "m1", ["m2"])
    upstream.handler = lambda r, m: (503, {"error": {"message": "unavailable"}}) if m == "m1" else (200, chat_body())
    root = make_root(gdb, project_id=project.id)
    root.status, root.started_at = "running", utcnow() - timedelta(seconds=2)
    gdb.commit()
    gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text", segment_index=2)
    gw.finalize_root(gdb, root, "succeeded")
    gdb.commit()
    rows = attempts_of(gdb, root)
    succeeded = [r for r in rows if r.status == "succeeded"]
    gdb.refresh(root)
    assert root.status == "succeeded" and root.progress == 100 and root.finished_at and root.duration_ms >= 2000
    assert root.prompt_tokens == sum(r.prompt_tokens for r in rows) == 200
    assert root.quota_estimated == sum(r.quota_estimated for r in rows)
    assert root.cost_cny == sum((r.cost_cny for r in rows), Decimal(0))
    assert (root.model, root.request_id, root.candidate_index, root.protocol) == ("m2", succeeded[-1].request_id, 1, "openai_chat")
    assert root.output_excerpt == "hello" and root.error_category is None
    rt = redis_client.hgetall(stats_service.realtime_key(stats_service.today_date(gdb), project.id))
    assert rt["tasks_succeeded"] == "1" and int(rt["task_duration_ms_sum"]) >= 2000
    assert rt["ai_calls"] == "4" and rt["ai_failed"] == "2" and rt["ai_succeeded"] == "2"


def test_finalize_root_failed_takes_last_attempt(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "keyword", "m1", [], max_attempts=1)
    upstream.handler = lambda r, m: (500, {"error": {"message": "Bearer sk-secret123456 leaked"}})
    root = make_root(gdb)
    with pytest.raises(BusinessError):
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    gw.finalize_root(gdb, root, "failed")
    gdb.commit()
    assert root.error_category == "upstream_unavailable" and root.request_id == "req-1" and "sk-secret" not in root.error_message


# =====================================================================
# 图片 / 视频（§5.13、docs/10 §4.3）
# =====================================================================


def test_submit_image_async_and_poll_mock(synced: Session) -> None:
    root = make_root(synced, capability="image", operation="image_generate")
    result, attempt = gw.submit_image(synced, root_task=root, req=ImageRequest(model="", prompt="一只猫"))
    assert result.mode == "async" and result.task_id.startswith("task_mock_")
    assert attempt.protocol == "image_async" and attempt.upstream_task_id == result.task_id and attempt.status == "succeeded"
    assert root.upstream_task_id == result.task_id
    gw.start_polling(synced, root, attempt, first_interval_seconds=5, budget_seconds=600)
    synced.commit()
    assert root.status == "polling" and root.next_poll_at > utcnow() and root.deadline_at > root.next_poll_at
    statuses = [gw.poll_task(synced, root).status for _ in range(3)]
    assert statuses == [TaskStatus.QUEUED, TaskStatus.IN_PROGRESS, TaskStatus.SUCCEEDED]
    poll = json.loads(root.response_meta_json)["poll"]
    assert root.poll_count == 3 and len(poll["request_ids"]) == 3 and poll["consecutive_404"] == 0 and root.progress == 100
    assert attempts_of(synced, root) == [attempt]  # 轮询不记尝试行


def test_poll_task_404_and_auth_failed(gdb: Session, upstream: FakeUpstream) -> None:
    root = make_root(gdb, capability="image", operation="image_generate")
    root.status, root.upstream_task_id = "polling", "task_x"
    gdb.commit()
    upstream.handler = lambda r, m: (404, {"error": {"message": "task not found"}})
    for expected in (1, 2):
        with pytest.raises(ZhiqiError):
            gw.poll_task(gdb, root)
        assert json.loads(root.response_meta_json)["poll"]["consecutive_404"] == expected
    upstream.handler = lambda r, m: (401, {"error": {"message": "unauthorized"}})
    with pytest.raises(ZhiqiError):
        gw.poll_task(gdb, root)
    gdb.refresh(root)
    assert root.status == "polling" and gw.check_paused() == "auth_failed"
    assert json.loads(root.response_meta_json)["poll"]["consecutive_404"] == 0


def test_submit_image_sync_fallback_not_counted_in_breaker(gdb: Session, upstream: FakeUpstream) -> None:
    update_config(gdb, "ai_routing_config", {"breaker": {"failure_threshold": 1}})
    set_route(gdb, "image", "img1", [], protocol="image_async")

    def handler(request: httpx.Request, model: str | None) -> tuple[int, Any]:
        if request.url.path.endswith("/async"):
            return 404, {"error": {"message": "Invalid URL"}}
        if request.url.path == "/v1/images/edits":
            return 200, {"data": [{"url": "https://cdn.test/edit.png"}]}
        return 200, {"data": [{"url": "https://cdn.test/a.png"}]}

    upstream.handler = handler
    root = make_root(gdb, capability="image", operation="image_generate")
    result, attempt = gw.submit_image(gdb, root_task=root, req=ImageRequest(model="", prompt="p", reference_image_urls=["https://x.test/r.png"]))
    rows = attempts_of(gdb, root)
    assert [(r.protocol, r.attempt, r.status, r.error_category) for r in rows] == [
        ("image_async", 1, "failed", "route_missing"), ("image_edit", 2, "succeeded", None),
    ]
    assert json.loads(rows[1].response_meta_json)["fallback_from"] == rows[0].id
    assert result.mode == "edit" and result.urls == ["https://cdn.test/edit.png"] and attempt.output_excerpt == "https://cdn.test/edit.png"
    assert gw.get_breaker(gw._cfg(gdb)).state("image", "img1") == "closed"


def test_submit_image_without_sync_fallback_counts_and_switches(gdb: Session, upstream: FakeUpstream) -> None:
    update_config(gdb, "media_config", {"image": {"sync_fallback": False}})
    update_config(gdb, "ai_routing_config", {"breaker": {"failure_threshold": 1}})
    set_route(gdb, "image", "img1", ["img2"], protocol="image_async")
    upstream.handler = lambda r, m: (404, {"error": {"message": "x"}}) if m == "img1" else (202, {"id": "task_9", "status": "queued"})
    root = make_root(gdb, capability="image", operation="image_generate")
    result, attempt = gw.submit_image(gdb, root_task=root, req=ImageRequest(model="", prompt="p"))
    assert result.task_id == "task_9" and attempt.candidate_index == 1
    assert gw.get_breaker(gw._cfg(gdb)).state("image", "img1") == "open"


def test_submit_image_timeout_never_falls_back(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "image", "img1", ["img2"], protocol="image_async")
    upstream.handler = lambda r, m: (0, httpx.ReadTimeout("slow"))
    root = make_root(gdb, capability="image", operation="image_generate")
    with pytest.raises(BusinessError) as exc:
        gw.submit_image(gdb, root_task=root, req=ImageRequest(model="", prompt="p"))
    assert exc.value.code == 5021 and exc.value.data["error_category"] == "timeout"
    assert [r.model for r in attempts_of(gdb, root)] == ["img1"]


def test_submit_image_media_storage_fallback_root_starts_from_next_candidate(gdb: Session, upstream: FakeUpstream) -> None:
    set_route(gdb, "image", "img1", ["img2"], protocol="image_async")
    upstream.handler = lambda r, m: (202, {"id": f"task_{m}", "status": "queued"})
    root = make_root(gdb, capability="image", operation="image_generate", candidate_index=1, trigger_type="system")
    result, attempt = gw.submit_image(gdb, root_task=root, req=ImageRequest(model="", prompt="p"))
    assert result.task_id == "task_img2" and attempt.candidate_index == 1


def test_submit_video_mock_and_poll(synced: Session) -> None:
    root = make_root(synced, capability="video", operation="video_generate")
    result, attempt = gw.submit_video(synced, root_task=root, req=VideoRequest(model="", prompt="海浪", duration=5))
    assert result.task_id.startswith("vidtask_mock_") and attempt.protocol == "video"
    statuses = [gw.poll_task(synced, root).status for _ in range(5)]
    assert statuses[-1] == TaskStatus.SUCCEEDED and statuses[0] == TaskStatus.QUEUED


# =====================================================================
# 用量对账（§10）
# =====================================================================


def _push_log(entry: dict[str, Any]) -> None:
    redis_client.lpush(zhiqi_mock.USAGE_LOGS_KEY, json.dumps(entry))


def test_reconcile_matches_and_is_idempotent(synced: Session, project: Project) -> None:
    root = make_root(synced, project_id=project.id)
    _, attempt = gw.complete_text(synced, root_task=root, messages=MESSAGES, params=None, response_format="text")
    gw.finalize_root(synced, root, "succeeded")
    synced.commit()
    result = ai_usage_service.reconcile(synced)
    assert result["pulled"] == 1 and result["new"] == 1 and result["matched"] == 1 and result["unmatched"] == 0
    assert result["window_overflow"] is False and result["request_ids"][0].startswith("mock-")
    synced.refresh(attempt)
    synced.refresh(root)
    assert attempt.quota_actual == attempt.quota_estimated and attempt.reconciled_at and attempt.usage_log_type == 2
    assert root.quota_actual == attempt.quota_actual and root.cost_cny == attempt.cost_cny
    log = synced.scalar(select(AiUsageLog))
    assert log.ai_task_id == attempt.id and log.matched_at and len(log.entry_hash) == 64 and log.request_id == attempt.request_id
    again = ai_usage_service.reconcile(synced)
    assert again["new"] == 0 and again["matched"] == 0 and synced.scalar(select(AiUsageLog.id).where(AiUsageLog.id > log.id)) is None
    last = ai_usage_service.last_pull_view(SYSTEM_SCOPE)
    assert set(last) == {"pulled_at", "pulled", "new", "matched", "unmatched", "window_overflow", "request_ids"}
    own = ai_usage_service.last_pull_view(DataScope(admin_id=5, scope="own", owner_id=5))
    assert set(own) == {"pulled_at", "window_overflow"}


def test_reconcile_refunds_type5_and_duplicates(gdb: Session, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
    set_route(gdb, "image", "img1", [], protocol="image_async")
    upstream.handler = lambda r, m: (202, {"id": "task_refund", "status": "queued"})
    root = make_root(gdb, capability="image", operation="image_generate")
    _, img_attempt = gw.submit_image(gdb, root_task=root, req=ImageRequest(model="", prompt="p"))
    set_route(gdb, "keyword", "m1", [])
    upstream.handler = lambda r, m: (200, chat_body())
    text_root = make_root(gdb)
    _, text_attempt = gw.complete_text(gdb, root_task=text_root, messages=MESSAGES, params=None, response_format="text")
    monkeypatch.setattr(ai_usage_service, "get_client", lambda: zhiqi_mock.MockZhiqiClient())
    _push_log({"id": 1, "request_id": img_attempt.request_id, "type": 2, "quota": 1000, "model_name": "img1", "created_at": 100})
    _push_log({"id": 2, "request_id": "", "type": 6, "quota": -300, "task_id": "task_refund", "created_at": 101})
    _push_log({"id": 3, "request_id": text_attempt.request_id, "type": 5, "quota": 0, "created_at": 102})
    _push_log({"id": 4, "request_id": "unknown-req", "type": 2, "quota": 7, "created_at": 103})
    result = ai_usage_service.reconcile(gdb)
    assert result["new"] == 4 and result["matched"] == 3 and result["unmatched"] == 1
    gdb.refresh(img_attempt)
    gdb.refresh(text_attempt)
    assert img_attempt.quota_actual == 700 and img_attempt.cost_cny == Decimal("0.010080")
    assert text_attempt.quota_actual == 0 and text_attempt.usage_log_type == 5
    # 已对账行：新的 type=6 继续累加，重复的 type=2 不重写
    _push_log({"id": 5, "request_id": img_attempt.request_id, "type": 2, "quota": 999, "created_at": 104})
    _push_log({"id": 6, "request_id": "", "type": 6, "quota": 200, "task_id": "task_refund", "created_at": 105})
    result = ai_usage_service.reconcile(gdb)
    gdb.refresh(img_attempt)
    assert result["new"] == 2 and result["matched"] == 1 and img_attempt.quota_actual == 500 and img_attempt.usage_log_type == 6
    root_sum = gdb.get(AiTask, root.id)
    gdb.refresh(root_sum)
    assert root_sum.quota_actual == 500
    unmatched = gdb.scalars(select(AiUsageLog).where(AiUsageLog.ai_task_id.is_(None))).all()
    assert {u.upstream_log_id for u in unmatched} == {4, 5}
    assert gdb.scalar(select(AiUsageLog).where(AiUsageLog.upstream_log_id == 4)).model_name is None


def test_reconcile_window_overflow_and_retention(gdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_usage_service, "get_client", lambda: zhiqi_mock.MockZhiqiClient())
    old = AiUsageLog(entry_hash="0" * 64, log_type=2, quota=1, pulled_at=utcnow() - timedelta(days=31))
    gdb.add(old)
    gdb.commit()
    pipe = redis_client.pipeline()
    for i in range(1000):
        pipe.lpush(zhiqi_mock.USAGE_LOGS_KEY, json.dumps({"id": i + 10, "request_id": f"r{i}", "type": 2, "quota": 1}))
    pipe.execute()
    result = ai_usage_service.reconcile(gdb)
    assert result["pulled"] == 1000 and result["new"] == 1000 and result["window_overflow"] is True
    assert ai_usage_service.next_interval_seconds(gdb) == 60
    assert gdb.get(AiUsageLog, old.id) is None  # 超过 30 天的未匹配条目被清理


def test_usage_logs_and_summary_scope(synced: Session, project: Project, owner: Any) -> None:
    root = make_root(synced, project_id=project.id)
    gw.complete_text(synced, root_task=root, messages=MESSAGES, params=None, response_format="text")
    probe = ai_task_service.run_route_probe(synced, capability="keyword", model="mock-text")
    assert probe.status == "healthy"
    ai_usage_service.reconcile(synced)
    own = DataScope(admin_id=owner.id, scope="own", owner_id=owner.id)
    other = DataScope(admin_id=999, scope="own", owner_id=999)
    items, total = ai_usage_service.list_usage_logs(synced, SYSTEM_SCOPE)
    assert total == 2
    items, total = ai_usage_service.list_usage_logs(synced, own)
    assert total == 1 and items[0]["matched"] is True
    assert ai_usage_service.list_usage_logs(synced, other)[1] == 0
    rows = ai_usage_service.usage_summary(synced, SYSTEM_SCOPE, group_by="model")
    assert rows[0]["key"] == "mock-text" and rows[0]["calls"] == 1 and rows[0]["reconciled_rate"] == 1.0  # 探测不计
    day_rows = ai_usage_service.usage_summary(synced, own, group_by="day")
    assert day_rows[0]["key"] == stats_service.today_date(synced).isoformat() and day_rows[0]["calls"] == 1
    assert ai_usage_service.usage_summary(synced, other, group_by="project") == []
    with pytest.raises(BusinessError):
        ai_usage_service.usage_summary(synced, SYSTEM_SCOPE, group_by="nope")


# =====================================================================
# 模型目录（§11）
# =====================================================================


def test_sync_models_mock_and_catalog_cache(gdb: Session) -> None:
    result = ai_catalog_service.sync_models(gdb)
    assert (result["total"], result["added"], result["updated"], result["unavailable"]) == (3, 3, 0, 0)
    assert result["request_ids"]["models"].startswith("mock-") and result["request_ids"]["pricing"].startswith("mock-")
    rows = {r.model_id: r for r in gdb.scalars(select(AiModel))}
    assert json.loads(rows["mock-image"].modalities_json) == ["image"] and rows["mock-text"].vendor_name == "mock"
    assert rows["mock-text"].model_ratio == Decimal(1) and rows["mock-text"].is_available and rows["mock-text"].last_seen_at
    ttl = redis_client.ttl(ai_catalog_service.CATALOG_CACHE_KEY)
    assert 4100 < ttl <= 4200
    entry = ai_catalog_service.catalog_entry(gdb, "mock-video")
    assert entry.supported_endpoint_types == ["openai-video"]
    redis_client.delete(ai_catalog_service.CATALOG_CACHE_KEY)
    assert ai_catalog_service.catalog_entry(gdb, "mock-text").id == "mock-text"  # 键缺失 → ai_models 回填
    assert redis_client.exists(ai_catalog_service.CATALOG_CACHE_KEY)
    assert ai_catalog_service.catalog_entry(gdb, "not-there") is None
    options = ai_catalog_service.model_options(gdb, modality="image")
    assert options == [{"model_id": "mock-image", "vendor_name": "mock", "modalities": ["image"], "is_available": True, "last_health_status": "unknown", "quota_type": 0}]
    assert redis_client.exists("cache:ai:models:options:image:1")
    again = ai_catalog_service.sync_models(gdb)
    assert (again["added"], again["updated"]) == (0, 3)
    assert not redis_client.exists("cache:ai:models:options:image:1")


def test_sync_models_unavailable_force_open_and_restore(gdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    ai_catalog_service.sync_models(gdb)
    monkeypatch.setattr(zhiqi_mock, "MOCK_MODELS", tuple(m for m in zhiqi_mock.MOCK_MODELS if m[0] != "mock-image"))
    result = ai_catalog_service.sync_models(gdb)
    assert result["unavailable"] == 1
    row = gdb.scalar(select(AiModel).where(AiModel.model_id == "mock-image"))
    assert not row.is_available and row.last_seen_at is not None
    breaker = gw.get_breaker(gw._cfg(gdb))
    assert breaker.state("image", "mock-image") == "open" and breaker.reason("image", "mock-image") == "model_unavailable"
    assert redis_client.ttl(breaker.key("image", "mock-image")) > 3600
    alert = gdb.scalar(select(Alert).where(Alert.alert_type == "ai_breaker_open", Alert.target_key == "image:mock-image"))
    assert alert.status == "open"
    with pytest.raises(BusinessError) as exc:
        gw.resolve_route(gdb, Capability.IMAGE, None)
    assert exc.value.data["unavailable_models"] == ["mock-image"]
    items, _ = ai_catalog_service.list_models(gdb, include_hidden=False)
    assert "mock-image" in {i["model_id"] for i in items}
    row.last_seen_at = utcnow() - timedelta(days=8)
    gdb.commit()
    assert "mock-image" not in {i["model_id"] for i in ai_catalog_service.list_models(gdb)[0]}
    assert "mock-image" in {i["model_id"] for i in ai_catalog_service.list_models(gdb, include_hidden=True)[0]}
    monkeypatch.undo()
    from app.core.zhiqi.client import reset_client

    reset_client()
    ai_catalog_service.sync_models(gdb)
    gdb.refresh(alert)
    assert breaker.state("image", "mock-image") == "closed" and alert.status == "resolved"


def test_sync_models_lock_and_upstream_failure(gdb: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    redis_client.set(ai_catalog_service.SYNC_LOCK_KEY, "x", ex=30)
    with pytest.raises(BusinessError) as exc:
        ai_catalog_service.sync_models(gdb)
    assert exc.value.code == 409
    redis_client.delete(ai_catalog_service.SYNC_LOCK_KEY)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise ZhiqiError(ErrorCategory.UPSTREAM_UNAVAILABLE, "down", request_id="rq")

    monkeypatch.setattr(ai_catalog_service.catalog, "list_models", boom)
    with pytest.raises(BusinessError) as exc:
        ai_catalog_service.sync_models(gdb)
    assert exc.value.code == 5021 and exc.value.data["request_id"] == "rq"
    assert not redis_client.exists(ai_catalog_service.SYNC_LOCK_KEY) and gdb.scalar(select(AiModel.id)) is None


# =====================================================================
# 告警（docs/11 §10、docs/03「告警去重」）
# =====================================================================


def test_alert_dedupe_resolve_and_new_row(db: Session) -> None:
    a1 = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key="keyword:m", title="t", message="m", payload={"n": 1})
    db.commit()
    a2 = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key="keyword:m", title="t", message="m", payload={"n": 2})
    db.commit()
    assert a1.id == a2.id and a2.trigger_count == 2 and json.loads(a2.payload_json) == {"n": 2}
    assert a1.dedupe_key == "ai_breaker_open:ai_model:keyword:m" and json.loads(a1.notified_channels_json) == ["in_app"]
    assert alert_service.resolve_alert(db, SYSTEM_SCOPE, "ai_breaker_open", "ai_model", "keyword:m") == 1
    db.commit()
    assert a1.status == "resolved" and a1.resolution_note == "auto"
    a3 = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key="keyword:m", title="t", message="m")
    db.commit()
    assert a3.id != a1.id and a3.trigger_count == 1
    numeric = alert_service.raise_alert(db, SYSTEM_SCOPE, "media_task_failed", target_type="media_asset", target_id=42, project_id=None, title="x" * 300, message="y" * 2000)
    db.commit()
    assert numeric.target_key == "42" and len(numeric.title) == 200 and len(numeric.message) == 1000 and numeric.severity == "info"
    restored = alert_service.raise_alert(db, SYSTEM_SCOPE, "link_restored", target_type="publish_link", target_id=1, title="r", message="r")
    db.commit()
    assert restored.status == "resolved" and restored.resolution_note == "auto"
    rt = redis_client.hgetall(stats_service.realtime_key(stats_service.today_date(db), 0))
    assert rt["alerts_opened"] == "4" and rt["alerts_resolved"] == "2"


def test_alert_rule_disabled_and_rollback(db: Session) -> None:
    update_config(db, "alert_config", {"rules": {"ai_auth_failed": {"enabled": False, "severity": "critical"}}})
    assert alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_auth_failed", target_type="system", title="t", message="m") is None
    alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_quota_exceeded", target_type="system", title="t", message="m")
    db.rollback()
    assert db.scalar(select(Alert.id)) is None and not redis_client.keys("stats:rt:*")


def test_alert_deliver_cooldown_and_channels(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(alert_service.WebhookChannel, "send", lambda self, alert, event: sent.append((event, alert["dedupe_key"])) or True)
    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.test/alert")
    update_config(db, "alert_config", {"channels": {"webhook": {"enabled": True, "url_env": "ALERT_WEBHOOK_URL", "min_severity": "warning"}}, "dedupe_cooldown_minutes": 60})
    for _ in range(2):
        alert = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_quota_exceeded", target_type="system", title="t", message="m")
        db.commit()
    assert sent == [("alert.triggered", "ai_quota_exceeded:system:")]
    db.refresh(alert)
    assert json.loads(alert.notified_channels_json) == ["in_app", "webhook"]
    assert redis_client.ttl("alert:cooldown:ai_quota_exceeded:system:") > 3500
    # info 低于 min_severity 不投递
    alert_service.raise_alert(db, SYSTEM_SCOPE, "media_task_failed", target_type="media_asset", target_id=1, title="t", message="m")
    db.commit()
    assert len(sent) == 1
    alert_service.resolve(db, SYSTEM_SCOPE, alert.id, admin_id=None, note="done")
    assert sent[-1] == ("alert.resolved", "ai_quota_exceeded:system:")


def test_webhook_channel_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200)

    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.test/alert")
    monkeypatch.setattr(settings, "alert_webhook_secret", "s3cret")
    channel = alert_service.WebhookChannel({"url_env": "ALERT_WEBHOOK_URL"}, transport=httpx.MockTransport(handler))
    assert channel.send({"id": 1, "severity": "warning", "title": "t", "dedupe_key": "k"}, "alert.triggered")
    assert captured["headers"]["x-aicreat-event"] == "alert.triggered"
    assert captured["headers"]["x-aicreat-signature"] == alert_service.WebhookChannel.signature("s3cret", captured["body"])
    assert json.loads(captured["body"])["alert"]["id"] == 1
    assert not alert_service.EmailChannel({"to": []}).send({"id": 1}, "alert.triggered")


def test_alert_handling_scope_and_summary(db: Session, project: Project, owner: Any) -> None:
    own = DataScope(admin_id=owner.id, scope="own", owner_id=owner.id)
    biz = alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=9, project_id=project.id, title="删", message="m")
    sysa = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_auth_failed", target_type="system", title="鉴权", message="m")
    db.commit()
    items, total = alert_service.list_alerts(db, own)
    assert total == 1 and items[0]["id"] == biz.id and items[0]["payload"] is None
    assert alert_service.list_alerts(db, SYSTEM_SCOPE)[1] == 2
    with pytest.raises(BusinessError) as exc:
        alert_service.get_alert(db, own, sysa.id)
    assert exc.value.code == 404
    assert alert_service.acknowledge(db, own, biz.id, admin_id=owner.id)["status"] == "acknowledged"
    with pytest.raises(BusinessError) as exc:
        alert_service.acknowledge(db, own, biz.id, admin_id=owner.id)
    assert exc.value.code == 409 and exc.value.data == {"current_status": "acknowledged"}
    summary = alert_service.summary(db, SYSTEM_SCOPE)
    assert summary["open"]["critical"] == 1 and summary["acknowledged"]["warning"] == 1 and summary["today_opened"] == 2
    result = alert_service.batch_resolve(db, own, [biz.id, sysa.id, 999999], admin_id=owner.id, note="批量")
    assert result == {"updated": 1, "skipped": [{"id": sysa.id, "reason": "not_found"}, {"id": 999999, "reason": "not_found"}]}
    assert alert_service.batch_resolve(db, SYSTEM_SCOPE, [biz.id], admin_id=None)["skipped"] == [{"id": biz.id, "reason": "invalid_transition"}]
    assert alert_service.ignore(db, SYSTEM_SCOPE, sysa.id, admin_id=None)["status"] == "ignored"
    assert alert_service.summary(db, own)["today_resolved"] == 1
    assert alert_service.evaluate(db)["placeholder"] is True


# =====================================================================
# 实时计数（docs/12 §4.6）
# =====================================================================


def test_realtime_counters(db: Session, project: Project, owner: Any) -> None:
    assert stats_service.increment_realtime(project.id, {"ai_calls": 2, "cost_cny": Decimal("0.5")}, db=db)
    assert not stats_service.increment_realtime(project.id, {"ai_calls": 1}, at=utcnow() - timedelta(days=3), db=db)
    with pytest.raises(ValueError):
        stats_service.increment_realtime(project.id, {"quota_actual": 1}, db=db)
    today = stats_service.today_date(db)
    key = stats_service.realtime_key(today, project.id)
    assert redis_client.hgetall(key) == {"ai_calls": "2", "cost_cny": "0.5"} and 0 < redis_client.ttl(key) <= 259200
    assert redis_client.hget(stats_service.realtime_key(today, 0), "ai_calls") == "2"
    assert stats_service.realtime_today(SYSTEM_SCOPE, 0, db=db) == {"ai_calls": 2, "cost_cny": 0.5}
    own = DataScope(admin_id=owner.id, scope="own", owner_id=owner.id)
    assert stats_service.realtime_today(own, 0, db=db) == {"ai_calls": 2, "cost_cny": 0.5}
    assert stats_service.realtime_today(DataScope(admin_id=1, scope="own", owner_id=12345), 0, db=db) == {}
    start, end = stats_service.day_bounds(today, db)
    assert end - start == timedelta(days=1) and stats_service.local_date(start, db) == today


# =====================================================================
# 根任务执行生命周期（ai_task_service）
# =====================================================================


def _queued_root(db: Session, **kw: Any) -> AiTask:
    task = gw.create_root_task(db, capability=kw.pop("capability", "keyword"), operation=kw.pop("operation", "keyword_generate"), project_id=kw.pop("project_id", None), created_by=None, **kw)
    db.commit()
    return task


def test_claim_process_success_and_batch_hook(gdb: Session, project: Project, owner: Any, handlers: None) -> None:
    batch = GenerationBatch(project_id=project.id, kind="keyword", template_id=1, template_version=1, task_total=1, created_by=owner.id)
    gdb.add(batch)
    gdb.commit()
    applied: list[int] = []
    finished: list[int] = []

    def handler(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
        result, attempt = ctx.complete_text(messages=MESSAGES, params=None, response_format="json")
        return ai_task_service.TaskOutcome(apply=lambda c: applied.append(attempt.id), output_excerpt=result.text[:20])

    ai_task_service.register_handler("keyword_generate", handler)
    ai_task_service.set_batch_finished_hook(finished.append)
    task = _queued_root(gdb, project_id=project.id, batch_id=batch.id)
    assert redis_client.lrange("queue:ai_tasks", 0, -1) == [str(task.id)]
    assert ai_task_service.claim(gdb, task.id, "w:1")
    assert not ai_task_service.claim(gdb, task.id, "w:2")
    gdb.refresh(batch)
    assert batch.status == "running" and batch.started_at
    assert ai_task_service.process_one(task.id, worker_id="w:1")
    gdb.expire_all()
    task = gdb.get(AiTask, task.id)
    assert task.status == "succeeded" and len(applied) == 1 and finished == [batch.id]
    assert not redis_client.exists(f"lock:ai_task:{task.id}")


def test_process_one_failure_paths(gdb: Session, upstream: FakeUpstream, handlers: None) -> None:
    set_route(gdb, "keyword", "m1", [], max_attempts=1)
    failed_hook: list[tuple[str, str | None]] = []
    ai_task_service.register_handler(
        "keyword_generate",
        lambda ctx: ctx.complete_text(messages=MESSAGES, params=None, response_format="text") and None,
        on_failed=lambda db, root, cat, msg: failed_hook.append((cat, msg)),
    )
    upstream.handler = lambda r, m: (400, {"error": {"message": "moderation blocked"}})
    task = _queued_root(gdb)
    assert ai_task_service.claim(gdb, task.id, "w:1") and ai_task_service.process_one(task.id, worker_id="w:1")
    gdb.expire_all()
    task = gdb.get(AiTask, task.id)
    assert (task.status, task.error_category) == ("failed", "content_blocked") and failed_hook[0][0] == "content_blocked"
    assert "hint" not in (task.error_message or "")
    # 全局暂停 → 回滚 queued，不终态
    upstream.handler = lambda r, m: (402, {"error": {"message": "quota"}})
    task2 = _queued_root(gdb)
    assert ai_task_service.claim(gdb, task2.id, "w:1") and not ai_task_service.process_one(task2.id, worker_id="w:1")
    gdb.expire_all()
    task2 = gdb.get(AiTask, task2.id)
    assert task2.status == "queued" and task2.pause_count == 1 and task2.finished_at is None
    # 未注册的 operation → failed(unknown)
    task3 = _queued_root(gdb, operation="content_outline", capability="content")
    ai_task_service.unregister_handler("content_outline")
    gw.clear_paused()
    assert ai_task_service.claim(gdb, task3.id, "w:1") and ai_task_service.process_one(task3.id, worker_id="w:1")
    gdb.expire_all()
    assert gdb.get(AiTask, task3.id).error_category == "unknown"


def test_process_one_batch_cancelled_marks_cancelled(gdb: Session, project: Project, owner: Any, handlers: None) -> None:
    batch = GenerationBatch(project_id=project.id, kind="keyword", template_id=1, template_version=1, task_total=1, created_by=owner.id)
    gdb.add(batch)
    gdb.commit()
    cancelled: list[int] = []

    def handler(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
        ctx.complete_text(messages=MESSAGES, params=None, response_format="text")
        b = ctx.batch()
        b.status = "cancelled"
        ctx.db.commit()
        return ai_task_service.TaskOutcome(apply=lambda c: pytest.fail("不应写业务对象"))

    ai_task_service.register_handler("keyword_generate", handler, on_cancelled=lambda db, root: cancelled.append(root.id))
    task = _queued_root(gdb, project_id=project.id, batch_id=batch.id)
    ai_task_service.claim(gdb, task.id, "w:1")
    assert ai_task_service.process_one(task.id, worker_id="w:1")
    gdb.expire_all()
    task = gdb.get(AiTask, task.id)
    assert task.status == "cancelled" and task.error_category == "cancelled" and cancelled == [task.id]
    assert attempts_of(gdb, task)[0].status == "succeeded"  # 尝试行按实际结果保留


def test_task_admin_list_detail_cancel_retry_export(gdb: Session, project: Project, owner: Any, handlers: None) -> None:
    own = DataScope(admin_id=owner.id, scope="own", owner_id=owner.id)
    batch = GenerationBatch(project_id=project.id, kind="keyword", template_id=1, template_version=1, task_total=1, task_failed=1, status="failed", created_by=owner.id)
    gdb.add(batch)
    gdb.commit()
    failed = _queued_root(gdb, project_id=project.id, batch_id=batch.id, target_type="generation_batch", target_id=batch.id, input={"count": 5})
    gw.check_quota(gdb, project_id=project.id, estimated_quota=10, task=failed)
    gw.complete_text(gdb, root_task=failed, messages=MESSAGES, params=None, response_format="text")
    gw.finalize_root(gdb, failed, "failed", error_category="invalid_response")
    gdb.commit()
    hidden = _queued_root(gdb)  # 无项目：只对总后台可见
    items, total = ai_task_service.list_tasks(gdb, own)
    assert total == 1 and items[0]["id"] == failed.id and items[0]["input"] == {"count": 5}
    attempts, total = ai_task_service.list_tasks(gdb, SYSTEM_SCOPE, row_kind="attempt")
    assert total == 1 and attempts[0]["input"] is None and attempts[0]["root_task_id"] == failed.id
    detail = ai_task_service.get_task_detail(gdb, own, failed.id)
    assert len(detail["attempts"]) == 1 and detail["attempts"][0]["request_id"].startswith("mock-")
    assert detail["attempts"][0]["cost_cny"] > 0 and detail["response_meta"] is None
    with pytest.raises(BusinessError) as exc:
        ai_task_service.get_task_detail(gdb, own, hidden.id)
    assert exc.value.code == 404
    # 取消 queued → cancelled；再次取消 409
    item = ai_task_service.cancel_task(gdb, SYSTEM_SCOPE, hidden.id, admin_id=None)
    assert item["status"] == "cancelled" and item["error_category"] == "cancelled"
    with pytest.raises(BusinessError) as exc:
        ai_task_service.cancel_task(gdb, SYSTEM_SCOPE, hidden.id, admin_id=None)
    assert exc.value.code == 409 and exc.value.data == {"current_status": "cancelled"}
    # 重试：新根任务 + 批次 task_failed −1 + running；再次重试 409 existing_id
    result = ai_task_service.retry_task(gdb, own, failed.id, admin_id=owner.id)
    new = gdb.get(AiTask, result["task_id"])
    assert new.parent_task_id == failed.id and new.status == "queued" and new.batch_id == batch.id and new.created_by == owner.id
    assert gw.task_input(new) == {"count": 5} and new.quota_reserved == 10
    assert str(new.id) in redis_client.lrange("queue:ai_tasks", 0, -1)
    gdb.refresh(batch)
    assert batch.task_failed == 0 and batch.status == "running"
    with pytest.raises(BusinessError) as exc:
        ai_task_service.retry_task(gdb, own, failed.id, admin_id=owner.id)
    assert exc.value.code == 409 and exc.value.data == {"existing_id": new.id}
    media = _queued_root(gdb, capability="image", operation="image_generate", target_type="media_asset", target_id=77)
    with pytest.raises(BusinessError) as exc:
        ai_task_service.retry_task(gdb, SYSTEM_SCOPE, media.id, admin_id=None)
    assert exc.value.data == {"hint": "POST /admin/media/assets/77/retry"} and exc.value.message == "请通过素材重试接口重试"
    # 导出：缺省 row_kind=attempt，BOM + 中文表头
    filename, content = ai_task_service.export_tasks(gdb, SYSTEM_SCOPE)
    assert filename.startswith("ai-tasks-") and filename.endswith(".csv") and content.startswith("﻿ID,根任务 ID")
    assert len(content.strip().splitlines()) == 2


def test_cancel_running_and_sync_conflicts(gdb: Session) -> None:
    running = _queued_root(gdb)
    ai_task_service.claim(gdb, running.id, "w:1")
    with pytest.raises(BusinessError) as exc:
        ai_task_service.cancel_task(gdb, SYSTEM_SCOPE, running.id, admin_id=None)
    assert exc.value.data == {"current_status": "running"}
    probe = make_root(gdb, capability="seo_check", operation="seo_check", sync=True)
    with pytest.raises(BusinessError) as exc:
        ai_task_service.cancel_task(gdb, SYSTEM_SCOPE, probe.id, admin_id=None)
    assert exc.value.code == 409


def test_run_route_probe_records_rows(synced: Session) -> None:
    result = ai_task_service.run_route_probe(synced, capability="content", model="mock-text", created_by=None)
    assert result.status == "healthy" and result.request_id.startswith("mock-") and result.protocol.value == "openai_chat"
    root = synced.scalar(select(AiTask).where(AiTask.operation == "route_probe", AiTask.root_task_id.is_(None)))
    assert (root.trigger_type, root.target_type, root.project_id, root.status, root.quota_reserved) == ("health_probe", "route_probe", None, "succeeded", 0)
    assert root.target_id == synced.scalar(select(CapabilityRoute.id).where(CapabilityRoute.capability == "content", CapabilityRoute.project_id == 0))
    attempt = attempts_of(synced, root)[0]
    assert attempt.quota_estimated > 0 and attempt.trigger_type == "health_probe"
    assert not redis_client.hgetall(stats_service.realtime_key(stats_service.today_date(synced), 0))  # 探测不计入实时计数
    media = ai_task_service.run_route_probe(synced, capability="image", model="mock-image")
    assert media.status == "healthy" and media.latency_ms == 0
    unknown = ai_task_service.run_route_probe(synced, capability="video", model="not-synced")
    assert unknown.status == "unknown"
    assert synced.scalar(select(AiTask).where(AiTask.model == "not-synced")) is None
    down = ai_task_service.run_route_probe(synced, capability="keyword", model="mock-text", protocol="openai_chat")
    assert down.status == "healthy"


# =====================================================================
# 规则逐条复核（docs/08 §8、§9、§11、§12 中此前未覆盖的分支）
# =====================================================================


def _breaker_rows(db: Session, root: AiTask) -> list[tuple[str, int, str | None, str | None]]:
    return [(r.model, r.attempt, r.error_category, r.request_id) for r in attempts_of(db, root)]


def test_complete_text_pre_submit_error_retries_same_model_then_falls_back(gdb: Session, upstream: FakeUpstream) -> None:
    """§9.2 upstream_unavailable（连接阶段）：① 客户端按 RetryPolicy 重试（POST 仅 pre_submit）、② 网关同模型再尝试至
    max_attempts（每次新尝试行，request_id=NULL）、③ 再切换备选。"""
    set_route(gdb, "keyword", "m1", ["m2"], max_attempts=2)
    upstream.handler = lambda r, m: (0, httpx.ConnectError("refused")) if m == "m1" else (200, chat_body())
    root = make_root(gdb)
    _, attempt = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert attempt.model == "m2" and attempt.candidate_index == 1 and attempt.attempt == 1
    assert _breaker_rows(gdb, root) == [
        ("m1", 1, "upstream_unavailable", None), ("m1", 2, "upstream_unavailable", None), ("m2", 1, None, "req-7"),
    ]
    assert [c[2] for c in upstream.calls].count("m1") == 6  # 2 行尝试 × 客户端 3 次（retry.max_attempts=3）


def test_complete_text_read_timeout_not_resent_and_falls_back(gdb: Session, upstream: FakeUpstream) -> None:
    """§8.2 / §9.2 timeout：非幂等 POST 读超时不重发（客户端与网关均不重试），文本按 fallback_on 切换备选并计入熔断。"""
    set_route(gdb, "keyword", "m1", ["m2"], max_attempts=3)
    upstream.handler = lambda r, m: (0, httpx.ReadTimeout("slow")) if m == "m1" else (200, chat_body())
    root = make_root(gdb)
    gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert [c[2] for c in upstream.calls] == ["m1", "m2"]
    rows = attempts_of(gdb, root)
    assert (rows[0].error_category, rows[0].request_id, rows[0].http_status) == ("timeout", None, None)
    assert redis_client.llen("ai:breaker:failures:keyword:m1") == 1


def test_complete_text_http_5xx_post_not_retried(gdb: Session, upstream: FakeUpstream) -> None:
    """HTTP 500/502/503/504 的 POST 一律不重试（客户端与网关），直接切换备选。"""
    set_route(gdb, "keyword", "m1", ["m2"], max_attempts=3)
    upstream.handler = lambda r, m: (502, {"error": {"message": "bad gateway"}}) if m == "m1" else (200, chat_body())
    root = make_root(gdb)
    gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert [c[2] for c in upstream.calls] == ["m1", "m2"]
    assert _breaker_rows(gdb, root)[0] == ("m1", 1, "upstream_unavailable", "req-1")


def test_fallback_disabled_and_never_fallback_on_precedence(gdb: Session, upstream: FakeUpstream) -> None:
    """§4.2 fallback：enabled=false 一律不切换（5021）；never_fallback_on 优先于 fallback_on。"""
    set_route(gdb, "keyword", "m1", ["m2"], max_attempts=1)
    upstream.handler = lambda r, m: (503, {"error": {"message": "overloaded"}})
    update_config(gdb, "ai_routing_config", {"fallback": {"enabled": False}})
    root = make_root(gdb)
    with pytest.raises(BusinessError) as exc:
        gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.code == 5021 and exc.value.data["model"] == "m1" and exc.value.data["hint"] is None
    assert [r.model for r in attempts_of(gdb, root)] == ["m1"]

    update_config(gdb, "ai_routing_config", {"fallback": {
        "enabled": True, "fallback_on": ["upstream_unavailable", "timeout"], "never_fallback_on": ["timeout"],
    }})
    upstream.handler = lambda r, m: (0, httpx.ReadTimeout("slow"))
    root2 = make_root(gdb)
    with pytest.raises(BusinessError) as exc:
        gw.complete_text(gdb, root_task=root2, messages=MESSAGES, params=None, response_format="text")
    assert exc.value.code == 5021 and exc.value.data["error_category"] == "timeout"
    assert [r.model for r in attempts_of(gdb, root2)] == ["m1"]


def test_half_open_trial_failure_stops_same_model_retries(gdb: Session, upstream: FakeUpstream) -> None:
    """§8.4：half_open 只放行 1 个试探调用；试探失败（rate_limited 计入熔断）重新 open 后，同模型不再发起 HTTP，按候选链切换。"""
    set_route(gdb, "keyword", "m1", ["m2"], max_attempts=3)
    breaker = gw.get_breaker(gw._cfg(gdb))
    breaker.force_open("keyword", "m1", reason="failures")
    redis_client.hset("ai:breaker:keyword:m1", "opened_at", f"{time.time() - 3600:.3f}")
    assert breaker.state("keyword", "m1") == "half_open"
    upstream.handler = lambda r, m: (429, {"error": {"message": "slow down"}}) if m == "m1" else (200, chat_body())
    root = make_root(gdb)
    _, attempt = gw.complete_text(gdb, root_task=root, messages=MESSAGES, params=None, response_format="text")
    assert attempt.model == "m2"
    assert [(r.model, r.attempt, r.error_category) for r in attempts_of(gdb, root)] == [("m1", 1, "rate_limited"), ("m2", 1, None)]
    assert breaker.state("keyword", "m1") == "open" and breaker.reason("keyword", "m1") == "failures"
    assert gdb.scalar(select(Alert).where(Alert.alert_type == "ai_breaker_open", Alert.target_key == "keyword:m1")) is not None


def test_media_submit_rate_limited_retries_and_respects_reopened_breaker(gdb: Session, upstream: FakeUpstream) -> None:
    """§9.2 rate_limited：图片提交同模型再尝试至 max_attempts（新尝试行），不切换到同步端点；之后按 fallback_on 切换备选。"""
    set_route(gdb, "image", "img1", ["img2"], protocol="image_async", max_attempts=2)
    upstream.handler = (
        lambda r, m: (429, {"error": {"message": "busy"}}) if m == "img1" else (202, {"id": "task_ok", "status": "queued"})
    )
    root = make_root(gdb, capability="image", operation="image_generate")
    result, attempt = gw.submit_image(gdb, root_task=root, req=ImageRequest(model="", prompt="一只猫"))
    assert result.task_id == "task_ok" and attempt.model == "img2" and attempt.upstream_task_id == "task_ok"
    assert [(r.model, r.attempt, r.protocol, r.error_category) for r in attempts_of(gdb, root)] == [
        ("img1", 1, "image_async", "rate_limited"), ("img1", 2, "image_async", "rate_limited"), ("img2", 1, "image_async", None),
    ]
    assert all(path == "/v1/images/generations/async" for _, path, _ in upstream.calls)


def test_probe_success_breaker_effects(gdb: Session) -> None:
    """§12.3：探测成功时 probe_down 的 open → reset；half_open → record_success 关闭；failures 的 open 不因探测成功关闭；
    model_unavailable 只由 sync_models / reset-breaker 解除。"""
    ai_catalog_service.sync_models(gdb)
    breaker = gw.get_breaker(gw._cfg(gdb))
    cases = {"keyword": "probe_down", "title": "failures", "content": "model_unavailable"}
    for capability, reason in cases.items():
        breaker.force_open(capability, "mock-text", reason=reason)
    breaker.force_open("rewrite", "mock-text", reason="failures")
    redis_client.hset("ai:breaker:rewrite:mock-text", "opened_at", f"{time.time() - 3600:.3f}")
    for capability in ("keyword", "title", "content", "rewrite"):
        result = ai_task_service.probe_and_record(gdb, capability=capability, model="mock-text", preferred_protocol="openai_chat")
        assert result.status == "healthy"
    assert breaker.state("keyword", "mock-text") == "closed"
    assert (breaker.state("title", "mock-text"), breaker.reason("title", "mock-text")) == ("open", "failures")
    assert (breaker.state("content", "mock-text"), breaker.reason("content", "mock-text")) == ("open", "model_unavailable")
    assert breaker.state("rewrite", "mock-text") == "closed"


def test_health_degraded_latency_without_failures(gdb: Session) -> None:
    """§12.4 ⑤：最近 5 次无失败时取单次结果；成功但延迟 ≥ degraded_latency_ms → degraded（不计 consecutive_failures）。"""
    from app.core.zhiqi import health
    from app.core.zhiqi.types import HealthResult, Protocol

    assert health.status_from(15000, None, 15000) == "degraded" and health.status_from(14999, None, 15000) == "healthy"
    result = HealthResult(
        capability=Capability.TITLE, model="slow-model", protocol=Protocol.OPENAI_CHAT, status=health.status_from(20000, None, 15000),
        latency_ms=20000, request_id="r-1", error_category=None, error_message=None, checked_at=utcnow(),
    )
    snap = ai_task_service.record_health_result(gdb, result, probed_upstream=True)
    assert (snap["status"], snap["consecutive_failures"], snap["latency_ms"]) == ("degraded", 0, 20000)


def test_sync_models_and_reconcile_retry_idempotent_gets(gdb: Session, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch) -> None:
    """§4.2 retry / §8.2：目录、价格与用量日志是幂等 GET，按 ai_routing_config.retry 对 502/503 指数退避重试，request_id 取最后一次。"""
    client = gw.get_client()
    monkeypatch.setattr(ai_catalog_service, "get_client", lambda: client)
    monkeypatch.setattr(ai_usage_service, "get_client", lambda: client)
    seen: dict[str, int] = {}

    def handler(request: httpx.Request, _model: str | None) -> tuple[int, Any]:
        path = request.url.path
        seen[path] = seen.get(path, 0) + 1
        if seen[path] == 1:
            return 503, {"error": {"message": "temporarily unavailable"}}
        if path == "/v1/models":
            return 200, {"data": [{"id": "m-a", "owned_by": "x", "supported_endpoint_types": ["openai"]}]}
        if path == "/api/pricing_new":
            return 200, {"data": [{"model_name": "m-a", "quota_type": 0, "model_ratio": 1}], "vendors": []}
        return 200, {"data": []}

    upstream.handler = handler
    result = ai_catalog_service.sync_models(gdb)
    assert result["total"] == 1 and seen == {"/v1/models": 2, "/api/pricing_new": 2}
    assert result["request_ids"]["models"] == "req-2"
    pulled = ai_usage_service.reconcile(gdb)
    assert pulled["pulled"] == 0 and seen["/api/log/token"] == 2
    # retry.max_attempts=1 时不重试，上游失败 → 5021
    update_config(gdb, "ai_routing_config", {"retry": {"max_attempts": 1}})
    seen.clear()
    with pytest.raises(BusinessError) as exc:
        ai_catalog_service.sync_models(gdb)
    assert exc.value.code == 5021 and seen == {"/v1/models": 1}


def test_pause_count_third_rollback_fails_root_via_worker(gdb: Session, upstream: FakeUpstream, handlers: None) -> None:
    """§8.5：pause_count 达到 3 后不再回滚，根任务经 worker 按常规 failed(quota_exceeded) 并结算（释放预占）。"""
    set_route(gdb, "keyword", "m1", [], max_attempts=1)
    ai_task_service.register_handler(
        "keyword_generate", lambda ctx: ctx.complete_text(messages=MESSAGES, params=None, response_format="text") and None,
    )
    upstream.handler = lambda r, m: (402, {"error": {"message": "insufficient balance"}})
    task = _queued_root(gdb)
    task.pause_count = 2
    gdb.commit()
    assert ai_task_service.claim(gdb, task.id, "w:1") and ai_task_service.process_one(task.id, worker_id="w:1")
    gdb.expire_all()
    task = gdb.get(AiTask, task.id)
    assert (task.status, task.error_category, task.pause_count) == ("failed", "quota_exceeded", 3)
    assert task.finished_at is not None and gw.check_paused() == "quota_exceeded"
