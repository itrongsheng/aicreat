"""用户数据范围（docs/13-user-data-scope.md §16）：范围计算、各表范围谓词、不可见即 404、owner-options、
操作日志范围、用户组数据范围的读写规则，以及受约束路由的依赖覆盖测试。

业务资源接口（关键词、内容、链接等）的列表 / 详情隔离随各自阶段补充；本文件在 service 层直接验证
``data_scope_service`` 的谓词，保证后续阶段只需调用即可获得一致的过滤结果。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app import models as m
from app.api.deps import get_data_scope
from app.core.exceptions import BusinessError
from app.services import admin_rbac_service as rbac
from app.services import data_scope_service as ds
from app.services.data_scope_service import SYSTEM_SCOPE, DataScope
from conftest import ADMIN_API, User, UserFactory, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")

NOW = datetime(2026, 10, 6, 8, 0, 0)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# =====================================================================
# 夹具：A、B（operator，own）、R（reviewer，all）、S（super_admin）；项目 PA / PB 及其下数据
# =====================================================================


@dataclass
class World:
    a: User
    b: User
    r: User
    s: User
    pa: m.Project
    pb: m.Project
    ids: dict[str, dict[str, Any]] = field(default_factory=dict)   # 表 → {"A": [...], "B": [...], "system": [...]}


def _add(db: Session, obj: Any) -> Any:
    db.add(obj)
    db.flush()
    return obj


def _project_data(db: Session, world: World, label: str, project: m.Project, owner: User) -> None:
    def rec(table: str, obj: Any) -> Any:
        world.ids.setdefault(table, {}).setdefault(label, []).append(obj.id)
        return obj

    pid, uid = project.id, owner.id
    kw = rec("keywords", _add(db, m.Keyword(project_id=pid, keyword=f"{label} 关键词", normalized_keyword=f"{label.lower()}关键词",
                                          language="zh-CN", created_by=uid)))
    rec("titles", _add(db, m.Title(project_id=pid, keyword_id=kw.id, title=f"{label} 标题", style="news", created_by=uid)))
    content = rec("contents", _add(db, m.Content(project_id=pid, title=f"{label} 文章", language="zh-CN", style="news",
                                                 created_by=uid, updated_by=uid)))
    rec("content_versions", _add(db, m.ContentVersion(content_id=content.id, version_no=1, source="manual", title=content.title,
                                                      body="正文", content_hash=_hash(label), created_by=uid)))
    rec("generation_batches", _add(db, m.GenerationBatch(project_id=pid, kind="keyword", template_id=1, template_version=1, created_by=uid)))
    rec("media_assets", _add(db, m.MediaAsset(project_id=pid, kind="image", created_by=uid)))
    root = rec("ai_tasks", _add(db, m.AiTask(project_id=pid, capability="keyword", operation="keyword_generate", model="mock-text", created_by=uid)))
    attempt = rec("ai_tasks", _add(db, m.AiTask(project_id=pid, capability="keyword", operation="keyword_generate", model="mock-text",
                                                root_task_id=root.id, created_by=uid)))
    rec("ai_usage_logs", _add(db, m.AiUsageLog(entry_hash=_hash(f"usage-{label}"), log_type=2, pulled_at=NOW, ai_task_id=attempt.id)))
    rec("prompt_templates", _add(db, m.PromptTemplate(code=f"proj_{label.lower()}", kind="keyword", capability="keyword", name="项目模板",
                                                      project_id=pid, user_prompt="x", status="published", created_by=uid, updated_by=uid)))
    rec("capability_routes", _add(db, m.CapabilityRoute(capability="keyword", project_id=pid, protocol="openai_chat", primary_model="mock-text")))
    platform = db.scalar(select(m.PublishPlatform).where(m.PublishPlatform.code == "zhihu")) or _add(
        db, m.PublishPlatform(code="zhihu", name="知乎", name_en="Zhihu"))
    url = f"https://zhuanlan.zhihu.com/p/{label}"
    link = rec("publish_links", _add(db, m.PublishLink(project_id=pid, content_id=content.id, platform_id=platform.id, url=url,
                                                       normalized_url=url, url_hash=_hash(url), domain="zhuanlan.zhihu.com",
                                                       published_at=NOW, backfilled_by=uid, title_snapshot=content.title)))
    rec("link_checks", _add(db, m.LinkCheck(link_id=link.id, check_type="baseline", result_status="alive", previous_status="pending",
                                            applied_status="alive", matched_rule="ok", duration_ms=10, checked_at=NOW)))
    rec("index_checks", _add(db, m.IndexCheck(link_id=link.id, kind="seo", engine="baidu", provider="zhiqi_web_search", check_type="scheduled",
                                              result_status="indexed", previous_status="unknown", match_mode="url", checked_at=NOW)))
    rec("alerts", _add(db, m.Alert(alert_type="link_deleted", severity="critical", project_id=pid, target_type="publish_link",
                                   target_id=link.id, target_key=str(link.id), dedupe_key=f"link_deleted:publish_link:{link.id}",
                                   title="链接被删除", message="x", first_triggered_at=NOW, last_triggered_at=NOW)))
    rec("daily_stats", _add(db, m.DailyStat(stat_date=date(2026, 10, 5), project_id=pid, dimension="total", computed_at=NOW)))


@pytest.fixture
def world(db: Session, users: UserFactory, super_admin: User) -> World:
    a = users.create("operator", username="user_a", display_name="用户甲")
    b = users.create("operator", username="user_b", display_name="用户乙")
    r = users.create("reviewer", username="reviewer_r", display_name="审核员")
    pa = _add(db, m.Project(name="项目A", slug="pa", owner_id=a.id, created_by=a.id))
    pb = _add(db, m.Project(name="项目B", slug="pb", owner_id=b.id, created_by=b.id))
    w = World(a=a, b=b, r=r, s=super_admin, pa=pa, pb=pb)
    w.ids["projects"] = {"A": [pa.id], "B": [pb.id]}
    _project_data(db, w, "A", pa, a)
    _project_data(db, w, "B", pb, b)

    def sys(table: str, obj: Any) -> None:
        w.ids.setdefault(table, {}).setdefault("system", []).append(obj.id)

    # 不归属项目的系统数据：系统告警、探测任务、未匹配用量日志、project_id=0 的汇总统计行
    sys("alerts", _add(db, m.Alert(alert_type="worker_stale", severity="warning", project_id=None, target_type="worker",
                                   target_key="worker", dedupe_key="worker_stale:worker:worker", title="worker 心跳超时",
                                   message="x", first_triggered_at=NOW, last_triggered_at=NOW)))
    sys("ai_tasks", _add(db, m.AiTask(project_id=None, capability="content", operation="route_probe", model="mock-text",
                                      trigger_type="health_probe")))
    sys("ai_usage_logs", _add(db, m.AiUsageLog(entry_hash=_hash("unmatched"), log_type=2, pulled_at=NOW, ai_task_id=None)))
    sys("daily_stats", _add(db, m.DailyStat(stat_date=date(2026, 10, 5), project_id=0, dimension="total", computed_at=NOW)))
    # 上传的参考素材（project_id 为空，按上传人归属）
    for label, user in (("A", a), ("B", b)):
        asset = _add(db, m.MediaAsset(project_id=None, kind="image", source="uploaded", usage_type="reference", created_by=user.id))
        w.ids["media_assets"][label].append(asset.id)
    # 模板：全局已发布（所有人）、A 的全局草稿（A 与总后台）、全局已归档（所有人）
    pub = _add(db, m.PromptTemplate(code="sys_keyword", kind="keyword", capability="keyword", name="系统模板", project_id=0,
                                    user_prompt="x", status="published", is_system=True, created_by=s_id(super_admin), updated_by=s_id(super_admin)))
    arch = _add(db, m.PromptTemplate(code="old_keyword", kind="keyword", capability="keyword", name="旧模板", project_id=0,
                                     user_prompt="x", status="archived", created_by=b.id, updated_by=b.id))
    draft_a = _add(db, m.PromptTemplate(code="a_draft", kind="keyword", capability="keyword", name="A 草稿", project_id=0,
                                        user_prompt="x", status="draft", created_by=a.id, updated_by=a.id))
    w.ids["prompt_templates"]["global"] = [pub.id, arch.id]
    w.ids["prompt_templates"]["A"].append(draft_a.id)
    # 全局路由
    glob = _add(db, m.CapabilityRoute(capability="content", project_id=0, protocol="openai_chat", primary_model="mock-text"))
    w.ids["capability_routes"]["global"] = [glob.id]
    db.commit()
    return w


def s_id(user: User) -> int:
    return user.id


def scope_of(user: User, db: Session, owner_id: int | None = None) -> DataScope:
    return ds.build_scope(user.admin, db.get(m.AdminGroup, user.admin.group_id), owner_id)


def _ids(db: Session, stmt: Select) -> set[int]:
    return set(db.scalars(stmt).all())


def _expected(world: World, table: str, *labels: str) -> set[int]:
    groups = world.ids.get(table, {})
    return {i for label in labels for i in groups.get(label, [])}


# =====================================================================
# 1. 范围计算
# =====================================================================


def test_build_scope_per_group(db: Session, world: World) -> None:
    sa = scope_of(world.a, db, owner_id=world.b.id)
    assert sa == DataScope(world.a.id, "own", world.a.id)              # own 范围忽略 owner_id
    assert sa.restricted and not sa.is_all and sa.cache_key == f"owner:{world.a.id}"
    sr = scope_of(world.r, db)
    assert sr == DataScope(world.r.id, "all", None) and sr.is_all and not sr.restricted and sr.cache_key == "all"
    ss = scope_of(world.s, db, owner_id=world.a.id)
    assert ss == DataScope(world.s.id, "all", world.a.id) and ss.restricted and ss.cache_key == f"owner:{world.a.id}"
    assert SYSTEM_SCOPE == DataScope(None, "all", None) and SYSTEM_SCOPE.cache_key == "all"
    # super_admin 短路：即使库里被篡改为 own 仍为 all
    group = db.get(m.AdminGroup, world.s.admin.group_id)
    group.data_scope = "own"
    db.commit()
    assert scope_of(world.s, db).scope == "all"
    assert ds.effective_data_scope(group) == "all"


@pytest.fixture
def scope_app(app: FastAPI) -> FastAPI:
    router = APIRouter()

    @router.get("/api/v1/admin/_scope_probe")
    def probe(scope: DataScope = Depends(get_data_scope)) -> dict:
        return {"code": 0, "message": "ok", "data": {"admin_id": scope.admin_id, "scope": scope.scope, "owner_id": scope.owner_id,
                                                     "cache_key": scope.cache_key}}

    app.include_router(router)
    return app


def test_get_data_scope_dependency(scope_app: FastAPI, client: TestClient, world: World) -> None:
    url = f"{ADMIN_API}/_scope_probe"
    a = ok_data(client.get(url, headers=world.a.headers, params={"owner_id": world.b.id}))
    assert a == {"admin_id": world.a.id, "scope": "own", "owner_id": world.a.id, "cache_key": f"owner:{world.a.id}"}
    s = ok_data(client.get(url, headers=world.s.headers))
    assert s == {"admin_id": world.s.id, "scope": "all", "owner_id": None, "cache_key": "all"}
    s = ok_data(client.get(url, headers=world.s.headers, params={"owner_id": world.a.id}))
    assert s["owner_id"] == world.a.id and s["cache_key"] == f"owner:{world.a.id}"
    r = ok_data(client.get(url, headers=world.r.headers, params={"owner_id": 99999}))     # 不校验用户是否存在
    assert r["owner_id"] == 99999
    response = client.get(url, headers=world.s.headers, params={"owner_id": 0})
    assert response.status_code == 400 and response.json()["data"][0]["loc"] == ["query", "owner_id"]
    assert client.get(url).status_code == 401


# =====================================================================
# 2. 范围谓词（§4.2 归属矩阵）
# =====================================================================

PROJECT_TABLES = [
    ("keywords", m.Keyword), ("titles", m.Title), ("contents", m.Content), ("generation_batches", m.GenerationBatch),
    ("publish_links", m.PublishLink), ("ai_tasks", m.AiTask), ("alerts", m.Alert), ("daily_stats", m.DailyStat),
]


@pytest.mark.parametrize(("table", "model"), PROJECT_TABLES)
def test_scope_by_project(db: Session, world: World, table: str, model: Any) -> None:
    stmt = select(model.id)
    a, b = scope_of(world.a, db), scope_of(world.b, db)
    assert _ids(db, ds.scope_by_project(stmt, model.project_id, a)) == _expected(world, table, "A")
    assert _ids(db, ds.scope_by_project(stmt, model.project_id, b)) == _expected(world, table, "B")
    everything = _expected(world, table, "A", "B", "system")
    assert _ids(db, ds.scope_by_project(stmt, model.project_id, scope_of(world.r, db))) == everything
    assert _ids(db, ds.scope_by_project(stmt, model.project_id, SYSTEM_SCOPE)) == everything
    # 总后台的用户视角与该用户本人结果逐 ID 相同（系统数据不出现）
    assert _ids(db, ds.scope_by_project(stmt, model.project_id, scope_of(world.s, db, owner_id=world.a.id))) == _expected(world, table, "A")


def test_scope_projects_and_visible_ids(db: Session, world: World) -> None:
    stmt = select(m.Project.id)
    assert _ids(db, ds.scope_projects(stmt, scope_of(world.a, db))) == {world.pa.id}
    assert _ids(db, ds.scope_projects(stmt, scope_of(world.s, db))) == {world.pa.id, world.pb.id}
    assert ds.visible_project_ids(scope_of(world.r, db)) is None
    assert _ids(db, ds.visible_project_ids(scope_of(world.b, db))) == {world.pb.id}


def test_scope_media(db: Session, world: World) -> None:
    stmt = select(m.MediaAsset.id)
    assert _ids(db, ds.scope_media(stmt, scope_of(world.a, db))) == _expected(world, "media_assets", "A")
    assert _ids(db, ds.scope_media(stmt, scope_of(world.b, db))) == _expected(world, "media_assets", "B")
    assert _ids(db, ds.scope_media(stmt, scope_of(world.s, db))) == _expected(world, "media_assets", "A", "B")
    assert _ids(db, ds.scope_media(stmt, scope_of(world.s, db, owner_id=world.a.id))) == _expected(world, "media_assets", "A")
    # A 上传的参考素材绑定到 PA 后随项目归属
    uploaded = db.get(m.MediaAsset, world.ids["media_assets"]["A"][-1])
    uploaded.project_id = world.pa.id
    db.commit()
    assert uploaded.id in _ids(db, ds.scope_media(stmt, scope_of(world.a, db)))


def test_scope_templates(db: Session, world: World) -> None:
    stmt = select(m.PromptTemplate.id)
    glob = _expected(world, "prompt_templates", "global")
    a_ids = _ids(db, ds.scope_templates(stmt, scope_of(world.a, db)))
    b_ids = _ids(db, ds.scope_templates(stmt, scope_of(world.b, db)))
    assert a_ids == glob | _expected(world, "prompt_templates", "A")          # 含 A 的全局草稿
    assert b_ids == glob | _expected(world, "prompt_templates", "B")          # 看不到 A 的全局草稿与 PA 模板
    assert _ids(db, ds.scope_templates(stmt, scope_of(world.s, db))) == glob | _expected(world, "prompt_templates", "A", "B")
    assert _ids(db, ds.scope_templates(stmt, scope_of(world.s, db, owner_id=world.a.id))) == a_ids


def test_scope_routes_link_children_usage_and_versions(db: Session, world: World) -> None:
    a, b, s = scope_of(world.a, db), scope_of(world.b, db), scope_of(world.s, db)
    routes = select(m.CapabilityRoute.id)
    assert _ids(db, ds.scope_routes(routes, a)) == _expected(world, "capability_routes", "global", "A")
    assert _ids(db, ds.scope_routes(routes, s)) == _expected(world, "capability_routes", "global", "A", "B")
    for table, model in (("link_checks", m.LinkCheck), ("index_checks", m.IndexCheck)):
        assert _ids(db, ds.scope_link_children(select(model.id), model.link_id, a)) == _expected(world, table, "A")
        assert _ids(db, ds.scope_link_children(select(model.id), model.link_id, b)) == _expected(world, table, "B")
        assert _ids(db, ds.scope_link_children(select(model.id), model.link_id, s)) == _expected(world, table, "A", "B")
    usage = select(m.AiUsageLog.id)
    assert _ids(db, ds.scope_usage_logs(usage, a)) == _expected(world, "ai_usage_logs", "A")
    assert _ids(db, ds.scope_usage_logs(usage, s)) == _expected(world, "ai_usage_logs", "A", "B", "system")
    assert _ids(db, ds.scope_usage_logs(usage, scope_of(world.s, db, owner_id=world.b.id))) == _expected(world, "ai_usage_logs", "B")
    versions = select(m.ContentVersion.id).join(m.Content, m.Content.id == m.ContentVersion.content_id)
    assert _ids(db, ds.scope_by_project(versions, m.Content.project_id, a)) == _expected(world, "content_versions", "A")


def test_scope_operation_logs(db: Session, world: World) -> None:
    for user in (world.a, world.b, world.s):
        db.add(m.AdminOperationLog(admin_id=user.id, permission_code="content.keywords.create", action="create",
                                   target_type="keyword", summary="x", request_id="r", ip=""))
    db.commit()
    stmt = select(m.AdminOperationLog.admin_id)
    assert _ids(db, ds.scope_operation_logs(stmt, scope_of(world.a, db))) == {world.a.id}
    assert _ids(db, ds.scope_operation_logs(stmt, scope_of(world.s, db, owner_id=world.a.id))) == {world.a.id, world.b.id, world.s.id}
    assert _ids(db, ds.scope_operation_logs(stmt, SYSTEM_SCOPE)) == {world.a.id, world.b.id, world.s.id}


# =====================================================================
# 3. 单对象可见性：不可见即 404
# =====================================================================

MODELS_BY_TABLE = {
    "keywords": m.Keyword, "titles": m.Title, "contents": m.Content, "content_versions": m.ContentVersion,
    "generation_batches": m.GenerationBatch, "media_assets": m.MediaAsset, "ai_tasks": m.AiTask, "ai_usage_logs": m.AiUsageLog,
    "prompt_templates": m.PromptTemplate, "capability_routes": m.CapabilityRoute, "publish_links": m.PublishLink,
    "link_checks": m.LinkCheck, "index_checks": m.IndexCheck, "alerts": m.Alert, "daily_stats": m.DailyStat, "projects": m.Project,
}


def test_is_visible_matches_predicates(db: Session, world: World) -> None:
    a, s_as_a = scope_of(world.a, db), scope_of(world.s, db, owner_id=world.a.id)
    for table, model in MODELS_BY_TABLE.items():
        for label, ids in world.ids.get(table, {}).items():
            for obj_id in ids:
                obj = db.get(model, obj_id)
                expected_a = label == "A" or label == "global"
                assert ds.is_visible(db, a, obj) is expected_a, (table, label, obj_id)
                assert ds.is_visible(db, s_as_a, obj) is expected_a, (table, label, obj_id)
                assert ds.is_visible(db, scope_of(world.r, db), obj) is True
                assert ds.is_visible(db, SYSTEM_SCOPE, obj) is True
    assert ds.is_visible(db, a, None) is False
    platform = db.scalar(select(m.PublishPlatform))
    assert ds.is_visible(db, a, platform) is True                       # 不受数据范围约束的平台级数据


def test_get_visible_404_is_indistinguishable(db: Session, world: World) -> None:
    a = scope_of(world.a, db)
    kb = world.ids["keywords"]["B"][0]
    with pytest.raises(BusinessError) as invisible:
        ds.get_visible(db, a, m.Keyword, kb)
    with pytest.raises(BusinessError) as missing:
        ds.get_visible(db, a, m.Keyword, 987654)
    for exc in (invisible.value, missing.value):
        assert (exc.code, exc.http_status, exc.message, exc.data) == (404, 404, "对象不存在", None)
    assert ds.get_visible(db, a, m.Keyword, world.ids["keywords"]["A"][0]).project_id == world.pa.id
    assert ds.get_visible(db, scope_of(world.r, db), m.Keyword, kb).id == kb


def test_require_project(db: Session, world: World) -> None:
    a = scope_of(world.a, db)
    assert ds.require_project(db, a, world.pa.id, active=True).id == world.pa.id
    with pytest.raises(BusinessError) as exc:
        ds.require_project(db, a, world.pb.id)
    assert exc.value.http_status == 404
    world.pa.status = "archived"
    db.commit()
    assert ds.require_project(db, a, world.pa.id).id == world.pa.id
    with pytest.raises(BusinessError) as exc:
        ds.require_project(db, a, world.pa.id, active=True)
    assert (exc.value.code, exc.value.http_status, exc.value.data) == (409, 409, {"current_status": "archived"})


def test_project_transfer_moves_visibility(db: Session, world: World) -> None:
    """转移负责人只改 projects.owner_id：子表无需迁移，原负责人立即不可见、新负责人立即可见。"""
    world.pa.owner_id = world.b.id
    db.commit()
    stmt = select(m.Keyword.id)
    assert _ids(db, ds.scope_by_project(stmt, m.Keyword.project_id, scope_of(world.a, db))) == set()
    assert _ids(db, ds.scope_by_project(stmt, m.Keyword.project_id, scope_of(world.b, db))) == _expected(world, "keywords", "A", "B")
    assert _ids(db, ds.scope_by_project(select(m.Alert.id), m.Alert.project_id, scope_of(world.b, db))) == _expected(world, "alerts", "A", "B")


# =====================================================================
# 4. owner-options（§6.4）
# =====================================================================


def test_owner_options(client: TestClient, db: Session, world: World, users: UserFactory) -> None:
    disabled_owner = users.create("operator", username="user_c", display_name="已离职", is_active=False)
    users.create("operator", username="user_d", is_active=False)                     # 已禁用且无项目：不出现
    db.add(m.Project(name="项目C", slug="pc", owner_id=disabled_owner.id, created_by=disabled_owner.id))
    db.add(m.Project(name="项目A2", slug="pa2", owner_id=world.a.id, created_by=world.a.id, status="archived"))
    db.commit()
    url = f"{ADMIN_API}/projects/owner-options"

    mine = ok_data(client.get(url, headers=world.a.headers, params={"owner_id": world.b.id}))
    assert mine == [{"id": world.a.id, "username": "user_a", "display_name": "用户甲", "is_active": True, "data_scope": "own", "project_count": 2}]

    everyone = ok_data(client.get(url, headers=world.s.headers))
    by_name = {o["username"]: o for o in everyone}
    assert set(by_name) == {"admin", "user_a", "user_b", "reviewer_r", "user_c"}
    assert by_name["user_c"] == {"id": disabled_owner.id, "username": "user_c", "display_name": "已离职", "is_active": False,
                                 "data_scope": "own", "project_count": 1}
    assert by_name["admin"]["data_scope"] == "all" and by_name["reviewer_r"]["data_scope"] == "all"
    assert by_name["user_a"]["project_count"] == 2 and by_name["user_b"]["project_count"] == 1
    labels = [o["display_name"] or o["username"] for o in everyone]
    assert labels == sorted(labels, key=str.lower)
    assert ok_data(client.get(url, headers=world.s.headers, params={"owner_id": world.a.id})) == everyone   # 用户视角下仍返回全部
    assert ok_data(client.get(url, headers=world.r.headers)) == everyone
    assert client.get(url, headers=users.create(users.custom_group("无项目权限", ["dashboard.view"])).headers).status_code == 403


def test_owner_options_service_ignores_owner_filter(db: Session, world: World) -> None:
    all_scope = scope_of(world.s, db, owner_id=world.a.id)
    assert {o["id"] for o in ds.owner_options(db, all_scope)} == {world.a.id, world.b.id, world.r.id, world.s.id}
    assert [o["id"] for o in ds.owner_options(db, scope_of(world.b, db))] == [world.b.id]


# =====================================================================
# 5. 操作日志范围（§6.2、§6.3）
# =====================================================================


def test_operation_logs_scope(client: TestClient, db: Session, world: World, users: UserFactory) -> None:
    auditor_group = users.custom_group("运营审计", ["security.audit.view"], data_scope="own")
    auditor = users.create(auditor_group, username="auditor_own")
    for user, code, action in ((world.a, "content.keywords.create", "create"), (world.b, "content.keywords.update", "update"),
                               (world.s, "security.admins.create", "create"), (auditor, "auth.login", "login")):
        db.add(m.AdminOperationLog(admin_id=user.id, permission_code=code, action=action, target_type="keyword",
                                   target_id="1", summary="x", request_id="rid", ip="203.0.113.0"))
    db.commit()
    url = f"{ADMIN_API}/admin-operation-logs"

    own = ok_data(client.get(url, headers=auditor.headers, params={"admin_id": world.s.id, "owner_id": world.a.id}))
    assert own["total"] == 1 and own["items"][0]["admin"]["id"] == auditor.id                   # admin_id / owner_id 被忽略

    every = ok_data(client.get(url, headers=world.s.headers))
    assert every["total"] == 4
    assert ok_data(client.get(url, headers=world.s.headers, params={"owner_id": world.a.id}))["total"] == 4   # owner_id 不适用
    only_a = ok_data(client.get(url, headers=world.s.headers, params={"admin_id": world.a.id}))
    assert only_a["total"] == 1 and only_a["items"][0]["admin"]["username"] == "user_a"
    assert only_a["items"][0]["group_name"] == "运营人员" and only_a["items"][0]["ip"] == "203.0.113.0"
    assert ok_data(client.get(url, headers=world.s.headers, params={"module": "content"}))["total"] == 2
    assert ok_data(client.get(url, headers=world.s.headers, params={"module": "auth"}))["total"] == 1
    assert ok_data(client.get(url, headers=world.s.headers, params={"action": "update"}))["total"] == 1
    assert ok_data(client.get(url, headers=world.s.headers, params={"target_type": "admin"}))["total"] == 0
    assert client.get(url, headers=world.s.headers, params={"action": "frob"}).status_code == 400
    assert client.get(url, headers=world.s.headers, params={"module": "con%"}).status_code == 400
    future = ok_data(client.get(url, headers=world.s.headers, params={"start": "2100-01-01T00:00:00Z"}))
    assert future["total"] == 0
    window = ok_data(client.get(url, headers=world.s.headers, params={"start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00+08:00"}))
    assert window["total"] == 4
    assert [i["id"] for i in every["items"]] == sorted((i["id"] for i in every["items"]), reverse=True)
    assert client.get(url, headers=world.a.headers).status_code == 403                         # operator 无 security.audit.view


# =====================================================================
# 6. 用户组数据范围的读写（§3.3、§13）
# =====================================================================


def test_group_data_scope_changes_apply_immediately(client: TestClient, db: Session, world: World) -> None:
    url = f"{ADMIN_API}/projects/owner-options"
    assert [o["id"] for o in ok_data(client.get(url, headers=world.a.headers))] == [world.a.id]
    operator_group_id = world.a.admin.group_id
    item = ok_data(client.put(f"{ADMIN_API}/admin-groups/{operator_group_id}", headers=world.s.headers, json={"data_scope": "all"}))
    assert item["data_scope"] == "all"
    # 旧令牌仍有效，下一次请求按新范围过滤
    assert len(ok_data(client.get(url, headers=world.a.headers))) == 4
    me = ok_data(client.get(f"{ADMIN_API}/auth/me", headers=world.a.headers))
    assert me["data_scope"] == "all"
    db.expire_all()
    assert db.get(m.Admin, world.a.id).token_version == 1
    log = db.scalars(select(m.AdminOperationLog).where(m.AdminOperationLog.permission_code == "security.groups.update")).all()[-1]
    assert log.target_type == "admin_group" and log.target_id == str(operator_group_id)
    assert log.summary == "用户组 运营人员 数据范围 own → all"
    # ensure_rbac_seed 不改写已调整的系统组
    rbac.ensure_rbac_seed(db)
    db.expire_all()
    assert db.get(m.AdminGroup, operator_group_id).data_scope == "all"


def test_super_admin_scope_fixed_and_custom_default(client: TestClient, db: Session, world: World) -> None:
    super_group_id = world.s.admin.group_id
    response = client.put(f"{ADMIN_API}/admin-groups/{super_group_id}", headers=world.s.headers, json={"data_scope": "own"})
    assert response.status_code == 403 and response.json()["data"] is None
    created = ok_data(client.post(f"{ADMIN_API}/admin-groups", headers=world.s.headers, json={"name": "新组"}))
    assert created["data_scope"] == "own"
    groups = {g["code"]: g["data_scope"] for g in ok_data(client.get(f"{ADMIN_API}/admin-groups", headers=world.s.headers))}
    assert groups["super_admin"] == "all" and groups["operator"] == "own" and groups["reviewer"] == "all" and groups["read_only"] == "all"
    login_data = ok_data(client.post(f"{ADMIN_API}/auth/login", json={"username": "user_a", "password": world.a.password}))
    assert login_data["admin"]["data_scope"] == "own"


# =====================================================================
# 7. 路由覆盖（§9.3）
# =====================================================================


def _depends_on(route: Any, target: Any) -> bool:
    def walk(dependant: Any) -> bool:
        return any(dep.call is target or walk(dep) for dep in dependant.dependencies)

    return walk(route.dependant)


def test_scoped_routes_declare_data_scope(app: FastAPI) -> None:
    from app.main import iter_api_routes

    scoped = 0
    for path, _methods, route in iter_api_routes(app):
        relative = path[len("/api/v1"):] if path.startswith("/api/v1/") else path
        if any(relative == p or relative.startswith(p + "/") for p in ds.SCOPED_ROUTE_PREFIXES):
            assert _depends_on(route, get_data_scope), path
            scoped += 1
    assert scoped >= 2                     # 当前已挂载：/admin/projects/owner-options、/admin/admin-operation-logs
