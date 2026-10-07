"""统计报表（docs/12-dashboard-reports.md §14 后端范围；数据范围 docs/13 §16「统计」）。

- 时区边界、``aggregate_daily`` 每列的归属时间与条件（尝试行 / 根任务行口径、admin 行、媒体、快照回放、引擎行）、
  幂等与清理、``extra_json``；
- ``overview`` / ``trends`` / ``breakdown`` / ``rankings`` / ``export`` / ``recompute`` 的语义、校验错误、缓存与权限；
- 数据范围：普通用户只含本人项目，``owner_id`` 视角与本人一致且共用缓存键，``dimension=owner``，项目转移；
- 实时计数写入点与 ``realtime_today``；monitor_worker 的 ``drain_recompute`` 接线。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.models import DailyStat
from app.services import data_scope_service as ds
from app.services import index_check_service, settings_service, stats_service
from app.services.data_scope_service import SYSTEM_SCOPE, DataScope
from app.tasks import aggregate_daily_stats as agg
from conftest import ADMIN_API, User, UserFactory, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")

STATS = f"{ADMIN_API}/stats"
SH = ZoneInfo("Asia/Shanghai")
D1 = date(2026, 9, 1)
D2 = date(2026, 9, 2)
E1 = date(2026, 8, 1)
E2 = date(2026, 8, 2)


def at(day: date, hh: int = 10, mm: int = 0, tz: ZoneInfo = SH) -> datetime:
    """统计时区下 ``day hh:mm`` 对应的 naive UTC。"""
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).astimezone(UTC).replace(tzinfo=None)


def iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def add(db: Session, obj: Any) -> Any:
    db.add(obj)
    db.flush()
    return obj


def err(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    return response.json()


def row(db: Session, day: date, pid: int, dimension: str = "total", key: str = "") -> dict[str, Any] | None:
    item = db.scalar(select(DailyStat).where(DailyStat.stat_date == day, DailyStat.project_id == pid,
                                             DailyStat.dimension == dimension, DailyStat.dimension_key == key))
    if item is None:
        return None
    db.refresh(item)
    data = {c: getattr(item, c) for c in stats_service.METRIC_COLUMNS}
    data["extra"] = json.loads(item.extra_json) if item.extra_json else None
    return data


def row_keys(db: Session, day: date) -> set[tuple[int, str, str]]:
    db.expire_all()
    return {(r.project_id, r.dimension, r.dimension_key) for r in db.scalars(select(DailyStat).where(DailyStat.stat_date == day))}


def put_stat(db: Session, day: date, pid: int, dimension: str = "total", key: str = "", *, extra: int | None = None,
             computed_at: datetime | None = None, **cols: Any) -> DailyStat:
    """直接写一行 daily_stats（查询类用例的夹具）。"""
    item = DailyStat(stat_date=day, project_id=pid, dimension=dimension, dimension_key=key,
                     computed_at=computed_at or datetime(2026, 1, 1), **cols)
    if extra is not None:
        item.extra_json = json.dumps({"quota_estimated_reconciled": extra})
    return add(db, item)


# =====================================================================
# 夹具
# =====================================================================


@dataclass
class World:
    s: User
    a: User
    b: User
    r: User
    ro: User
    pa: m.Project
    pb: m.Project
    zhihu: m.PublishPlatform
    csdn: m.PublishPlatform


@pytest.fixture
def world(db: Session, users: UserFactory, super_admin: User) -> World:
    from seeds.seed import seed_publish_platforms

    seed_publish_platforms(db)
    a = users.create("operator", username="stat_a", display_name="用户甲")
    b = users.create("operator", username="stat_b", display_name="用户乙")
    r = users.create("reviewer", username="stat_r", display_name="审核员")
    ro = users.create("read_only", username="stat_ro", display_name="只读")
    pa = add(db, m.Project(name="项目A", slug="spa", owner_id=a.id, created_by=a.id))
    pb = add(db, m.Project(name="项目B", slug="spb", owner_id=b.id, created_by=b.id))
    zhihu = db.scalar(select(m.PublishPlatform).where(m.PublishPlatform.code == "zhihu"))
    csdn = db.scalar(select(m.PublishPlatform).where(m.PublishPlatform.code == "csdn"))
    db.commit()
    return World(s=super_admin, a=a, b=b, r=r, ro=ro, pa=pa, pb=pb, zhihu=zhihu, csdn=csdn)


def scope_of(db: Session, user: User, owner_id: int | None = None) -> DataScope:
    return ds.build_scope(user.admin, db.get(m.AdminGroup, user.admin.group_id), owner_id)


# ---- 源数据工厂 ----


def keyword(db: Session, project: m.Project, created_at: datetime, by: int, *, adopted_at: datetime | None = None,
            adopted_by: int | None = None, name: str | None = None) -> m.Keyword:
    text = name or f"kw-{created_at.isoformat()}-{adopted_at}"
    return add(db, m.Keyword(project_id=project.id, keyword=text[:120], normalized_keyword=text[:160], language="zh-CN",
                             created_by=by, created_at=created_at, adopted_at=adopted_at, adopted_by=adopted_by,
                             status="adopted" if adopted_at else "candidate"))


def content(db: Session, project: m.Project, created_at: datetime, by: int, **kw: Any) -> m.Content:
    return add(db, m.Content(project_id=project.id, title=kw.pop("title", "文章"), language="zh-CN", style="news", created_by=by,
                             updated_by=by, created_at=created_at, **kw))


_seq = iter(range(1, 10**6))


def link(db: Session, project: m.Project, cont: m.Content, platform: m.PublishPlatform, *, created_at: datetime,
         published_at: datetime, by: int, **kw: Any) -> m.PublishLink:
    n = next(_seq)
    url = f"https://example.com/p/{n}"
    for key in ("seo_status_json", "geo_status_json"):
        if isinstance(kw.get(key), dict):
            kw[key] = json.dumps(kw[key])
    return add(db, m.PublishLink(project_id=project.id, content_id=cont.id, platform_id=platform.id, url=url, normalized_url=url,
                                 url_hash=f"{n:064d}", domain="example.com", published_at=published_at, backfilled_by=by,
                                 title_snapshot=cont.title, created_at=created_at, **kw))


def link_check(db: Session, lk: m.PublishLink, checked_at: datetime, previous: str, applied: str, result: str | None = None) -> None:
    add(db, m.LinkCheck(link_id=lk.id, check_type="scheduled", result_status=result or (applied if applied != "pending" else "alive"),
                        previous_status=previous, applied_status=applied, matched_rule="ok", duration_ms=10, checked_at=checked_at))


def index_check(db: Session, lk: m.PublishLink, kind: str, engine: str, result: str, checked_at: datetime, provider: str = "zhiqi_web_search") -> None:
    add(db, m.IndexCheck(link_id=lk.id, kind=kind, engine=engine, provider=provider, check_type="scheduled", result_status=result,
                         previous_status="unknown", match_mode="url", checked_at=checked_at))


def root_task(db: Session, project: m.Project | None, capability: str, status: str, *, finished_at: datetime | None,
              created_at: datetime | None = None, by: int | None = None, duration_ms: int | None = None, model: str = "mock-text",
              trigger_type: str = "user", **kw: Any) -> m.AiTask:
    return add(db, m.AiTask(project_id=project.id if project else None, capability=capability, operation="content_generate",
                            model=model, status=status, finished_at=finished_at, created_at=created_at or finished_at or at(D1),
                            created_by=by, duration_ms=duration_ms, trigger_type=trigger_type, **kw))


def attempt(db: Session, root: m.AiTask, status: str, finished_at: datetime, **kw: Any) -> m.AiTask:
    kw.setdefault("created_by", root.created_by)
    kw.setdefault("model", root.model)
    kw.setdefault("created_at", finished_at - timedelta(minutes=1))
    return add(db, m.AiTask(project_id=root.project_id, capability=root.capability, operation=root.operation, status=status,
                            root_task_id=root.id, trigger_type=root.trigger_type, finished_at=finished_at, **kw))


def media(db: Session, project: m.Project, kind: str, by: int, **kw: Any) -> m.MediaAsset:
    return add(db, m.MediaAsset(project_id=project.id, kind=kind, created_by=by, **kw))


def alert(db: Session, project: m.Project | None, first: datetime, *, resolved_at: datetime | None = None, n: int = 0,
          severity: str = "warning", status: str | None = None) -> m.Alert:
    return add(db, m.Alert(alert_type="link_deleted" if project else "worker_stale", severity=severity,
                           status=status or ("resolved" if resolved_at else "open"), project_id=project.id if project else None,
                           target_type="publish_link" if project else "worker", target_key=f"k{n}-{first}", dedupe_key=f"d{n}-{first}",
                           title="t", message="m", first_triggered_at=first, last_triggered_at=first, resolved_at=resolved_at))


# =====================================================================
# 1. 时区
# =====================================================================


def test_day_bounds_and_timezone_switch(db: Session, world: World) -> None:
    assert stats_service.day_bounds(D1, db) == (datetime(2026, 8, 31, 16, 0), datetime(2026, 9, 1, 16, 0))
    keyword(db, world.pa, at(D1, 23, 59), world.a.id, name="late")
    keyword(db, world.pa, at(D2, 0, 0), world.a.id, name="next")
    db.commit()
    stats_service.aggregate_daily(db, D1)
    stats_service.aggregate_daily(db, D2)
    assert row(db, D1, world.pa.id)["keywords_created"] == 1
    assert row(db, D2, world.pa.id)["keywords_created"] == 1
    settings_service.set_value(db, "stats_config", {"timezone": "UTC"})
    assert stats_service.get_tz(db).key == "UTC"
    assert stats_service.day_bounds(D1, db) == (datetime(2026, 9, 1), datetime(2026, 9, 2))
    stats_service.aggregate_daily(db, D1)          # UTC 下两条都落在 9-01（UTC 15:59 与 16:00）
    assert row(db, D1, world.pa.id)["keywords_created"] == 2


# =====================================================================
# 2. 聚合正确性（夹具跨两个统计日）
# =====================================================================


@pytest.fixture
def scenario(db: Session, world: World) -> dict[str, Any]:
    a, b, s = world.a.id, world.b.id, world.s.id
    pa, pb = world.pa, world.pb
    # ---- 关键词 / 标题 / 内容 ----
    keyword(db, pa, at(D1), a, adopted_at=at(D1, 11), adopted_by=s, name="k1")
    keyword(db, pa, at(D1), a, name="k2")
    keyword(db, pa, at(D1, 23, 59), a, name="k4")
    keyword(db, pa, at(D2, 0, 0), a, name="k5")
    keyword(db, pb, at(D1), b, name="kb1")
    add(db, m.Title(project_id=pa.id, keyword_id=1, title="t1", style="news", created_by=a, created_at=at(D1),
                    adopted_at=at(D1, 12), adopted_by=s, status="adopted"))
    c1 = content(db, pa, at(D1), a, reviewed_at=at(D1, 12), reviewed_by=s, review_result="approved", status="published",
                 first_published_at=at(D1, 8))
    content(db, pa, at(D1), a, reviewed_at=at(D1, 13), reviewed_by=a, review_result="approved", review_note="auto", status="approved")
    content(db, pa, at(D1), a, reviewed_at=at(D1, 14), reviewed_by=s, review_result="rejected", status="rejected")
    cb = content(db, pb, at(D1 - timedelta(days=3)), b)
    # ---- 链接 ----
    l1 = link(db, pa, c1, world.zhihu, created_at=at(D1, 10), published_at=at(D1, 8), by=a,
              first_indexed_at=at(D1, 20), first_cited_at=at(D1, 21),
              seo_status_json={"baidu": {"status": "indexed", "first_indexed_at": iso(at(D1, 20))},
                               "bing": {"status": "indexed", "first_indexed_at": iso(at(D2, 12))}},
              geo_status_json={"doubao": {"status": "cited", "first_cited_at": iso(at(D1, 21))}})
    l2 = link(db, pa, c1, world.csdn, created_at=at(D1, 11), published_at=at(D1, 11) - timedelta(days=10), by=a,
              first_indexed_at=at(D1, 22), seo_status_json={"baidu": {"status": "indexed", "first_indexed_at": iso(at(D1, 22))}})
    l3 = link(db, pb, cb, world.zhihu, created_at=at(D1, 9), published_at=at(D1, 9), by=b)
    link_check(db, l1, at(D1, 12), "pending", "alive")
    link_check(db, l1, at(D1, 18), "alive", "deleted")
    link_check(db, l1, at(D1, 23), "deleted", "alive")
    link_check(db, l1, at(D2, 9), "alive", "changed")
    link_check(db, l2, at(D1, 12), "pending", "changed")
    link_check(db, l3, at(D1, 13), "pending", "alive", result="deleted")       # 未达确认阈值：applied 不变
    link_check(db, l3, at(D1, 14), "alive", "deleted")
    index_check(db, l1, "seo", "baidu", "indexed", at(D1, 20))
    index_check(db, l1, "seo", "baidu", "unknown", at(D1, 22))
    index_check(db, l1, "seo", "bing", "not_indexed", at(D1, 21))
    index_check(db, l1, "seo", "bing", "indexed", at(D2, 12))
    index_check(db, l1, "geo", "doubao", "cited", at(D1, 21))
    index_check(db, l2, "seo", "baidu", "indexed", at(D1, 22))
    index_check(db, l3, "seo", "google", "unknown", at(D1, 15))
    index_check(db, l3, "geo", "kimi", "not_cited", at(D1, 16))
    # ---- AI：尝试行 + 根任务行 ----
    r1 = root_task(db, pa, "content", "succeeded", finished_at=at(D1, 10, 5), by=a, duration_ms=5000)
    attempt(db, r1, "failed", at(D1, 10, 1), duration_ms=100, error_category="timeout")
    attempt(db, r1, "succeeded", at(D1, 10, 4), duration_ms=2000, prompt_tokens=100, completion_tokens=50, quota_estimated=1000,
            quota_actual=900, reconciled_at=at(D1, 11), cost_cny=Decimal("0.5"), request_id="req-1")
    r2 = root_task(db, pa, "keyword", "failed", finished_at=at(D1, 11), by=a)
    attempt(db, r2, "failed", at(D1, 11), error_category="model_unrouted", request_id=None)
    r3 = root_task(db, pa, "image", "expired", finished_at=at(D1, 12), by=a, model="mock-image")
    attempt(db, r3, "succeeded", at(D1, 11, 30), duration_ms=300, quota_estimated=500, cost_cny=Decimal("0.25"))
    root_task(db, pa, "content", "cancelled", finished_at=at(D1, 12), by=a)
    root_task(db, pa, "content", "queued", finished_at=None, by=a)
    probe = root_task(db, None, "content", "succeeded", finished_at=at(D1, 9), duration_ms=10, trigger_type="health_probe")
    attempt(db, probe, "succeeded", at(D1, 9), duration_ms=10, quota_estimated=5)
    r6 = root_task(db, pa, "seo_check", "succeeded", finished_at=at(D1, 13), by=None, duration_ms=1000, trigger_type="system")
    attempt(db, r6, "succeeded", at(D1, 13), duration_ms=800, prompt_tokens=10, completion_tokens=5, quota_estimated=50,
            cost_cny=Decimal("0.01"))
    r7 = root_task(db, pb, "video", "succeeded", finished_at=at(D1, 14), created_at=at(D1 - timedelta(days=3)), by=b,
                   duration_ms=600000, model="mock-video")
    attempt(db, r7, "succeeded", at(D1 - timedelta(days=3), 10, 5), duration_ms=900)
    root_task(db, pa, "keyword", "succeeded", finished_at=at(D1, 15), by=a, duration_ms=700, parent_task_id=r2.id)
    late = root_task(db, pa, "content", "succeeded", finished_at=at(D2, 10), by=a, duration_ms=1)
    attempt(db, late, "succeeded", at(D2, 10), duration_ms=1)
    # ---- 媒体 ----
    media(db, pa, "image", a, source="generated", status="deleted", model="mock-image", ready_at=at(D1, 12))
    media(db, pa, "video", a, source="generated", status="ready", model="mock-video", ready_at=at(D1, 13))
    media(db, pa, "image", a, source="generated", status="expired", model="mock-image", failed_at=at(D1, 14), error_category="timeout")
    media(db, pa, "image", a, source="generated", status="failed", model="mock-image", failed_at=at(D1, 15), error_category="cancelled")
    media(db, pa, "image", a, source="uploaded", status="ready", ready_at=at(D1, 15))
    # ---- 告警 ----
    alert(db, pa, at(D1, 18), n=1)
    alert(db, None, at(D1, 9), resolved_at=at(D1, 10), n=2)
    alert(db, pb, at(D1 - timedelta(days=5)), resolved_at=at(D1, 16), n=3)
    db.commit()
    return {"l1": l1, "l2": l2, "l3": l3, "r1": r1}


def test_aggregate_total_rows(db: Session, world: World, scenario: dict[str, Any]) -> None:
    rows = stats_service.aggregate_daily(db, D1)
    pa = row(db, D1, world.pa.id)
    assert pa["keywords_created"] == 3 and pa["keywords_adopted"] == 1
    assert pa["titles_created"] == 1 and pa["titles_adopted"] == 1
    assert pa["contents_created"] == 3 and pa["contents_approved"] == 2 and pa["contents_published"] == 1
    assert pa["links_backfilled"] == 2 and pa["links_checked"] == 4
    assert (pa["links_deleted"], pa["links_changed"], pa["links_restored"]) == (1, 1, 1)
    assert (pa["links_total_snapshot"], pa["links_alive_snapshot"]) == (2, 2)
    assert pa["seo_checks"] == 4 and pa["geo_checks"] == 1
    assert pa["seo_newly_indexed"] == 2 and pa["geo_newly_cited"] == 1
    assert (pa["index_hours_sum"], pa["index_hours_links"]) == (12, 1)          # l2 为历史补录：计新收录、不计耗时
    assert pa["seo_indexed_snapshot"] == 2 and pa["geo_cited_snapshot"] == 1
    # 尝试行：a1 失败、a2 成功（已对账）、a3 model_unrouted 失败、a4 成功（根任务 expired 不影响）、a6 系统任务成功
    assert (pa["ai_calls"], pa["ai_succeeded"], pa["ai_failed"]) == (5, 3, 2)
    assert pa["ai_duration_ms_sum"] == 3100
    assert (pa["prompt_tokens"], pa["completion_tokens"], pa["quota_estimated"]) == (110, 55, 1550)
    assert (pa["quota_actual"], pa["quota_reconciled_calls"]) == (900, 1)
    assert pa["cost_cny"] == Decimal("0.760000")
    assert pa["extra"] == {"quota_estimated_reconciled": 1000}
    # 根任务：r1 / r6 / 重试根任务成功，r2 失败 + r3 expired；cancelled / queued 不计
    assert (pa["tasks_succeeded"], pa["tasks_failed"], pa["task_duration_ms_sum"]) == (3, 2, 6700)
    assert (pa["images_generated"], pa["videos_generated"], pa["media_failed"]) == (1, 1, 1)
    assert (pa["alerts_opened"], pa["alerts_resolved"]) == (1, 0)
    pb = row(db, D1, world.pb.id)
    assert (pb["links_deleted"], pb["links_total_snapshot"], pb["links_alive_snapshot"]) == (1, 1, 0)
    assert (pb["tasks_succeeded"], pb["task_duration_ms_sum"], pb["ai_calls"]) == (1, 600000, 0)   # 跨多日的根任务按 finished_at 归属
    assert (pb["alerts_opened"], pb["alerts_resolved"]) == (0, 1)
    total = row(db, D1, 0)
    for col in stats_service.METRIC_COLUMNS:
        if col.startswith("alerts_"):
            continue
        assert total[col] == pa[col] + pb[col], col                          # 无已删除项目：汇总行 = 各项目行之和
    assert (total["alerts_opened"], total["alerts_resolved"]) == (2, 2)      # 系统告警只进 project_id=0 行
    assert rows == len(row_keys(db, D1))


def test_aggregate_dimension_rows(db: Session, world: World, scenario: dict[str, Any]) -> None:
    stats_service.aggregate_daily(db, D1)
    pa, a, b, s = world.pa.id, str(world.a.id), str(world.b.id), str(world.s.id)
    zhihu = row(db, D1, pa, "platform", "zhihu")
    assert (zhihu["links_backfilled"], zhihu["links_checked"], zhihu["links_deleted"], zhihu["links_restored"]) == (1, 3, 1, 1)
    assert (zhihu["links_total_snapshot"], zhihu["seo_checks"], zhihu["index_hours_sum"], zhihu["geo_cited_snapshot"]) == (1, 3, 12, 1)
    assert zhihu["ai_calls"] == 0 and zhihu["keywords_created"] == 0 and zhihu["extra"] is None   # 矩阵外的列保持 0
    csdn = row(db, D1, pa, "platform", "csdn")
    assert (csdn["links_changed"], csdn["seo_newly_indexed"], csdn["index_hours_links"]) == (1, 1, 0)
    assert row(db, D1, 0, "platform", "zhihu")["links_deleted"] == 2
    # capability / model（尝试行按尝试行、任务列按根任务行、媒体按 kind / model）
    cap = row(db, D1, pa, "capability", "content")
    assert (cap["ai_calls"], cap["ai_succeeded"], cap["ai_duration_ms_sum"], cap["quota_reconciled_calls"]) == (2, 1, 2000, 1)
    assert (cap["tasks_succeeded"], cap["task_duration_ms_sum"], cap["extra"]) == (1, 5000, {"quota_estimated_reconciled": 1000})
    kw = row(db, D1, pa, "capability", "keyword")
    assert (kw["ai_calls"], kw["ai_failed"], kw["tasks_succeeded"], kw["tasks_failed"]) == (1, 1, 1, 1)   # 重试根任务单独计数
    img = row(db, D1, pa, "capability", "image")
    assert (img["ai_calls"], img["tasks_failed"], img["images_generated"], img["media_failed"]) == (1, 1, 1, 1)
    assert img["keywords_created"] == 0 and img["links_backfilled"] == 0
    assert row(db, D1, pa, "capability", "video")["videos_generated"] == 1
    assert row(db, D1, world.pb.id, "capability", "video")["tasks_succeeded"] == 1
    model_img = row(db, D1, pa, "model", "mock-image")
    assert (model_img["ai_calls"], model_img["tasks_failed"], model_img["images_generated"], model_img["media_failed"]) == (1, 1, 1, 1)
    assert row(db, D1, pa, "model", "mock-text")["ai_calls"] == 4
    # admin 行：created_by IS NULL 的系统任务不进；审核按 reviewed_by 且排除自动通过；不填 ai_duration_ms_sum / quota_reconciled_calls
    adm_a = row(db, D1, pa, "admin", a)
    assert (adm_a["keywords_created"], adm_a["contents_created"], adm_a["contents_approved"], adm_a["links_backfilled"]) == (3, 3, 0, 2)
    assert (adm_a["ai_calls"], adm_a["ai_succeeded"], adm_a["ai_failed"], adm_a["quota_actual"]) == (4, 2, 2, 900)
    assert (adm_a["ai_duration_ms_sum"], adm_a["quota_reconciled_calls"], adm_a["extra"]) == (0, 0, {"quota_estimated_reconciled": 1000})
    assert (adm_a["tasks_succeeded"], adm_a["tasks_failed"], adm_a["task_duration_ms_sum"]) == (2, 2, 5700)
    assert (adm_a["images_generated"], adm_a["videos_generated"], adm_a["media_failed"]) == (1, 1, 1)
    adm_s = row(db, D1, pa, "admin", s)
    assert (adm_s["keywords_adopted"], adm_s["titles_adopted"], adm_s["contents_approved"], adm_s["ai_calls"]) == (1, 1, 1, 0)
    assert row(db, D1, world.pb.id, "admin", b)["tasks_succeeded"] == 1
    assert not any(key[1] == "admin" and key[2] in ("", "None") for key in row_keys(db, D1))
    # 引擎行
    baidu = row(db, D1, pa, "seo_engine", "baidu")
    assert (baidu["seo_checks"], baidu["seo_newly_indexed"], baidu["seo_indexed_snapshot"]) == (3, 2, 2)
    assert (baidu["index_hours_sum"], baidu["index_hours_links"], baidu["geo_checks"]) == (12, 1, 0)
    bing = row(db, D1, pa, "seo_engine", "bing")
    assert (bing["seo_checks"], bing["seo_newly_indexed"], bing["seo_indexed_snapshot"]) == (1, 0, 0)
    doubao = row(db, D1, pa, "geo_engine", "doubao")
    assert (doubao["geo_checks"], doubao["geo_newly_cited"], doubao["geo_cited_snapshot"]) == (1, 1, 1)
    assert row(db, D1, world.pb.id, "seo_engine", "google")["seo_checks"] == 1     # 已停用引擎当日有检测同样生成行
    assert row(db, D1, 0, "seo_engine", "google") is not None
    for engine in index_check_service.enabled_engines("seo", db):
        assert row(db, D1, 0, "seo_engine", engine) is not None
    for engine in index_check_service.enabled_engines("geo", db):
        assert row(db, D1, 0, "geo_engine", engine) is not None                # 启用引擎即使无检测也有一行


def test_snapshot_replay(db: Session, world: World, scenario: dict[str, Any]) -> None:
    """日终之后的检测不影响该日快照；unknown 不覆盖此前结论；只有 unknown 的链接 × 引擎不计；链接物理删除后不再出现。"""
    stats_service.aggregate_daily(db, D1)
    stats_service.aggregate_daily(db, D2)
    d2 = row(db, D2, world.pa.id)
    assert d2["links_alive_snapshot"] == 2                                     # l1 在 D2 变为 changed 仍算存活
    assert row(db, D2, world.pa.id, "seo_engine", "bing")["seo_indexed_snapshot"] == 1
    assert row(db, D1, world.pa.id, "seo_engine", "bing") is not None
    assert row(db, D1, world.pa.id, "seo_engine", "bing")["seo_indexed_snapshot"] == 0
    assert row(db, D1, world.pb.id)["seo_indexed_snapshot"] == 0              # 只有 unknown 记录
    # 物理删除 l2（检测记录级联删除）后重算 D1：快照与平台行随之变化
    db.delete(db.get(m.PublishLink, scenario["l2"].id))
    db.commit()
    stats_service.aggregate_daily(db, D1)
    pa = row(db, D1, world.pa.id)
    assert (pa["links_total_snapshot"], pa["links_alive_snapshot"], pa["seo_indexed_snapshot"]) == (1, 1, 1)
    assert row(db, D1, world.pa.id, "platform", "csdn") is None               # 平台的全部链接删除后平台行被清理
    assert row(db, D1, 0, "platform", "csdn") is None


def test_aggregate_idempotent_and_cleanup(db: Session, world: World, scenario: dict[str, Any]) -> None:
    first = stats_service.aggregate_daily(db, D1)
    keys = row_keys(db, D1)
    snapshot = {k: row(db, D1, *k) for k in keys}
    assert stats_service.aggregate_daily(db, D1) == first
    assert row_keys(db, D1) == keys and {k: row(db, D1, *k) for k in keys} == snapshot
    # 源数据变化后重算：不再出现的维度行被删除
    for asset in db.scalars(select(m.MediaAsset).where(m.MediaAsset.kind == "video")).all():
        db.delete(asset)
    db.commit()
    stats_service.aggregate_daily(db, D1)
    assert row(db, D1, world.pa.id, "model", "mock-video") is None
    # 无任何事件的日期：只有 project_id=0 的 total 行（全 0）与启用引擎行
    empty = date(2026, 1, 1)
    stats_service.aggregate_daily(db, empty)
    expected = {(0, "total", "")} | {(0, "seo_engine", e) for e in index_check_service.enabled_engines("seo", db)} \
        | {(0, "geo_engine", e) for e in index_check_service.enabled_engines("geo", db)}
    assert row_keys(db, empty) == expected
    assert not [v for k, v in row(db, empty, 0).items() if k != "extra" and v]


def test_deleted_project_rows_preserved(db: Session, world: World) -> None:
    keyword(db, world.pb, at(D1), world.b.id, name="pb-kw")
    keyword(db, world.pa, at(D1), world.a.id, name="pa-kw")
    db.commit()
    stats_service.aggregate_daily(db, D1)
    pb_rows = {k for k in row_keys(db, D1) if k[0] == world.pb.id}
    assert pb_rows and row(db, D1, world.pb.id)["keywords_created"] == 1
    # 删除项目（其关键词等先清空）后重算：项目行保持不变，project_id=0 行按现存源数据计算
    for kw in db.scalars(select(m.Keyword).where(m.Keyword.project_id == world.pb.id)).all():
        db.delete(kw)
    db.delete(db.get(m.Project, world.pb.id))
    db.commit()
    stats_service.aggregate_daily(db, D1)
    assert {k for k in row_keys(db, D1) if k[0] == world.pb.id} == pb_rows
    assert row(db, D1, world.pb.id)["keywords_created"] == 1
    assert row(db, D1, 0)["keywords_created"] == 1


def test_extra_json_and_reconcile_update(db: Session, world: World) -> None:
    root = root_task(db, world.pa, "content", "succeeded", finished_at=at(D1, 10), by=world.a.id, duration_ms=10)
    a1 = attempt(db, root, "succeeded", at(D1, 10), quota_estimated=300, request_id="r1")
    attempt(db, root, "succeeded", at(D1, 10), quota_estimated=200, quota_actual=150, reconciled_at=at(D1, 11), request_id="r2")
    db.commit()
    stats_service.aggregate_daily(db, D1)
    total = row(db, D1, world.pa.id)
    assert (total["quota_actual"], total["quota_reconciled_calls"], total["extra"]) == (150, 1, {"quota_estimated_reconciled": 200})
    # 对账回填后重算
    a1.quota_actual, a1.reconciled_at = 280, at(D2, 1)
    db.commit()
    stats_service.aggregate_daily(db, D1)
    total = row(db, D1, world.pa.id)
    assert (total["quota_actual"], total["quota_reconciled_calls"], total["extra"]) == (430, 2, {"quota_estimated_reconciled": 500})
    assert row(db, D1, world.pa.id, "model", "mock-text")["extra"] == {"quota_estimated_reconciled": 500}
    assert row(db, D1, world.pa.id, "admin", str(world.a.id))["extra"] == {"quota_estimated_reconciled": 500}


def test_compute_metric_ratios() -> None:
    cm = stats_service.compute_metric
    assert cm("ai_success_rate", {"ai_calls": 0, "ai_succeeded": 0}) is None
    assert cm("ai_success_rate", {"ai_calls": 3, "ai_succeeded": 2}) == 0.6667
    assert cm("task_success_rate", {"tasks_succeeded": 3, "tasks_failed": 1}) == 0.75
    assert cm("task_avg_duration_ms", {"task_duration_ms_sum": 1000, "tasks_succeeded": 3}) == 333
    assert cm("time_to_index_hours_avg", {"index_hours_sum": 25, "index_hours_links": 2, "seo_newly_indexed": 5}) == 12.5
    assert cm("quota_diff", {"quota_actual": 90, "quota_estimated_reconciled": 100}) == -10
    assert cm("quota_diff_rate", {"quota_actual": 90, "quota_estimated_reconciled": 100}) == -0.1
    assert cm("quota_diff_rate", {"quota_actual": 90}) is None
    assert cm("cost_cny_per_content", {"_content_cost": 1.0, "contents_created": 3}) == 0.333333
    assert cm("media_success_rate", {"images_generated": 1, "videos_generated": 1, "media_failed": 2}) == 0.5
    assert cm("tokens_total", {"prompt_tokens": 3, "completion_tokens": 4}) == 7
    # 规格表覆盖 §3.2 的每个指标、daily_stats 每列都可作 metric
    for key in ("keywords_total", "seo_index_rate", "geo_cite_rate_by_engine", "time_to_index_hours_p50", "ai_p95_duration_ms",
                "ai_failures_by_category", "quota_diff_rate", "alerts_open", "fastest_indexed", "top_failed_models"):
        assert key in stats_service.METRIC_SPECS
    assert set(stats_service.METRIC_COLUMNS) <= set(stats_service.QUERYABLE_METRICS)
    assert stats_service.METRIC_SPECS["cost_cny"].label == "费用（元）"
    assert stats_service.METRIC_SPECS["ai_calls"].label == "AI 调用"


# =====================================================================
# 3. 总览
# =====================================================================


class _BrokenRedis:
    """模拟 Redis 不可用：任何调用都抛 ``ConnectionError``。"""

    def __getattr__(self, name: str) -> Any:
        def _fail(*_a: Any, **_k: Any) -> Any:
            raise redis.ConnectionError("down")
        return _fail


def _today(db: Session) -> date:
    return stats_service.today_date(db)


def test_overview_today_sources(db: Session, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    t = _today(db)
    put_stat(db, t - timedelta(days=1), 0, ai_calls=4)
    db.commit()
    stats_service.increment_realtime(world.pa.id, {"ai_calls": 3, "cost_cny": 1.25})
    data = stats_service.overview(db, SYSTEM_SCOPE, range="7d")
    assert data["meta"]["today_source"] == "realtime" and data["kpis"]["ai_calls"] == 7 and data["kpis"]["cost_cny"] == 1.25
    # 今日 project_id=0 的 total 行存在：只用 daily_stats，不叠加 Redis
    put_stat(db, t, 0, ai_calls=10)
    db.commit()
    stats_service.clear_cache()
    data = stats_service.overview(db, SYSTEM_SCOPE, range="7d")
    assert data["meta"]["today_source"] == "daily_stats" and data["kpis"]["ai_calls"] == 14
    # Redis 不可用：今日按 0（无行时）、today_source=none，缓存降级
    db.delete(db.scalar(select(DailyStat).where(DailyStat.stat_date == t)))
    db.commit()
    monkeypatch.setattr(stats_service, "redis_client", _BrokenRedis())
    data = stats_service.overview(db, SYSTEM_SCOPE, range="today")
    assert data["meta"]["today_source"] == "none" and data["kpis"]["ai_calls"] == 0
    assert set(data["meta"]["warnings"]) >= {"realtime_unavailable", "cache_unavailable"}


def test_overview_kpis_compare_series_breakdowns(client: TestClient, db: Session, world: World) -> None:
    t = _today(db)
    put_stat(db, t, 0, ai_calls=5, ai_succeeded=4, cost_cny=Decimal("1.5"), links_backfilled=2, contents_created=2,
             prompt_tokens=7, completion_tokens=3, quota_reconciled_calls=5, tasks_succeeded=3, tasks_failed=1,
             computed_at=datetime(2026, 10, 6, 2, 30, 12))
    put_stat(db, t - timedelta(days=3), 0, ai_calls=5, ai_succeeded=5, cost_cny=Decimal("1"))
    put_stat(db, t - timedelta(days=8), 0, ai_calls=4, ai_succeeded=4, cost_cny=Decimal("2"))
    put_stat(db, t, 0, "capability", "content", ai_calls=3, ai_succeeded=2, cost_cny=Decimal("1.2"), quota_estimated=100,
             quota_actual=90, prompt_tokens=10, tasks_succeeded=1, task_duration_ms_sum=4000)
    put_stat(db, t, 0, "capability", "image", ai_calls=2, ai_succeeded=2, cost_cny=Decimal("0.3"), tasks_succeeded=2,
             task_duration_ms_sum=10000)
    put_stat(db, t, 0, "model", "mock-text", ai_calls=5, ai_succeeded=4, cost_cny=Decimal("1.5"))
    put_stat(db, t, 0, "seo_engine", "baidu", seo_indexed_snapshot=3)
    db.query(DailyStat).filter(DailyStat.stat_date == t, DailyStat.dimension == "total").update({"links_total_snapshot": 6})
    db.commit()
    data = ok_data(client.get(f"{STATS}/overview", params={"range": "7d"}, headers=world.s.headers))
    meta = data["meta"]
    assert meta["range"] == "7d" and meta["end_date"] == t.isoformat() and meta["start_date"] == (t - timedelta(days=6)).isoformat()
    assert (meta["scope"], meta["owner_id"], meta["project_id"], meta["timezone"]) == ("all", None, 0, "Asia/Shanghai")
    assert meta["snapshot_date"] == t.isoformat() and meta["computed_at"] == "2026-10-06T02:30:12Z" and meta["cached"] is False
    kpis = data["kpis"]
    assert list(kpis) == list(stats_service.KPI_KEYS)
    assert (kpis["ai_calls"], kpis["ai_success_rate"], kpis["cost_cny"], kpis["tokens_total"]) == (10, 0.9, 2.5, 10)
    assert kpis["cost_cny_per_content"] == 0.6 and kpis["task_success_rate"] == 0.75 and kpis["quota_reconciled_rate"] == 0.5
    assert kpis["keyword_adopt_rate"] is None and kpis["media_success_rate"] is None
    cmp_ = data["compare"]
    assert cmp_["ai_calls"] == {"previous": 4, "delta": 6, "delta_rate": 1.5}
    assert cmp_["cost_cny"] == {"previous": 2.0, "delta": 0.5, "delta_rate": 0.25}
    assert cmp_["links_backfilled"] == {"previous": 0, "delta": 2, "delta_rate": None}
    assert "keywords_total" not in cmp_ and "link_alive_rate" not in cmp_
    series = data["series"]
    assert len(series["dates"]) == 7 and series["dates"][-1] == t.isoformat()
    assert series["ai_calls"] == [0, 0, 0, 5, 0, 0, 5] and series["cost_cny"][-1] == 1.5
    caps = data["breakdowns"]["cost_by_capability"]
    assert [c["capability"] for c in caps] == ["content", "image"]
    assert caps[0] == {"capability": "content", "ai_calls": 3, "ai_success_rate": 0.6667, "tokens_total": 10, "quota_estimated": 100,
                       "quota_actual": 90, "cost_cny": 1.2, "share": 0.8, "task_success_rate": 1.0, "task_avg_duration_ms": 4000}
    assert caps[1]["task_avg_duration_ms"] == 5000
    assert data["breakdowns"]["cost_by_model"][0]["model"] == "mock-text" and "task_success_rate" not in data["breakdowns"]["cost_by_model"][0]
    seo = data["breakdowns"]["seo_index_rate_by_engine"]
    assert seo["baidu"] == {"rate": 0.5, "hit": 3, "total": 6}
    assert seo["google"] == {"rate": None, "hit": None, "total": 6}
    assert set(data["breakdowns"]["links_by_status"]) == {"pending", "alive", "changed", "suspected_deleted", "deleted", "unknown"}
    assert data["breakdowns"]["alerts_open"] == {"info": 0, "warning": 0, "critical": 0}
    # 30d / today：序列天数 = range 天数且最少 7 天；today 对比昨天
    data30 = ok_data(client.get(f"{STATS}/overview", params={"range": "30d"}, headers=world.s.headers))
    assert len(data30["series"]["dates"]) == 30 and data30["compare"]["ai_calls"]["previous"] == 0
    today = ok_data(client.get(f"{STATS}/overview", params={"range": "today"}, headers=world.s.headers))
    assert len(today["series"]["dates"]) == 7 and today["kpis"]["ai_calls"] == 5 and today["compare"]["ai_calls"]["previous"] == 0
    # 缓存：命中时 meta.cached=true；aggregate 完成后被清除
    again = ok_data(client.get(f"{STATS}/overview", params={"range": "7d"}, headers=world.s.headers))
    assert again["meta"]["cached"] is True and redis_client.exists("cache:stats:overview:all:0:7d")
    agg.aggregate(t - timedelta(days=20))
    assert not redis_client.exists("cache:stats:overview:all:0:7d")
    assert ok_data(client.get(f"{STATS}/overview", params={"range": "7d"}, headers=world.s.headers))["meta"]["cached"] is False
    body = err(client.get(f"{STATS}/overview", params={"range": "1y"}, headers=world.s.headers), 400)
    assert body["data"][0]["loc"] == ["query", "range"]


def test_overview_current_metrics(db: Session, world: World) -> None:
    now = m.utcnow()
    keyword(db, world.pa, now, world.a.id, name="a1")
    keyword(db, world.pa, now, world.a.id, adopted_at=now, adopted_by=world.a.id, name="a2")
    keyword(db, world.pb, now, world.b.id, name="b1")
    ca = content(db, world.pa, now, world.a.id, status="published")
    cb = content(db, world.pb, now, world.b.id)
    old = now - timedelta(days=3)
    link(db, world.pa, ca, world.zhihu, created_at=old, published_at=old, by=world.a.id, alive_status="alive",
         index_checks_done=1, seo_indexed_any=True, geo_cited_any=True, first_indexed_at=old + timedelta(hours=10))
    link(db, world.pa, ca, world.zhihu, created_at=old, published_at=old, by=world.a.id, alive_status="deleted",
         index_checks_done=2, first_indexed_at=old + timedelta(hours=20))
    link(db, world.pa, ca, world.csdn, created_at=now, published_at=now, by=world.a.id, alive_status="changed",
         index_checks_done=1, seo_indexed_any=True)                                     # 发布不满 1 天：不进 SEO 分母
    link(db, world.pa, ca, world.csdn, created_at=old, published_at=old - timedelta(days=10), by=world.a.id,
         alive_status="pending", first_indexed_at=old + timedelta(hours=1))             # 历史补录：不计收录耗时
    link(db, world.pb, cb, world.zhihu, created_at=old, published_at=old, by=world.b.id, alive_status="alive")
    alert(db, world.pa, now, n=10, severity="critical")
    alert(db, None, now, n=11, severity="info")
    db.commit()
    data = stats_service.overview(db, SYSTEM_SCOPE, project_id=world.pa.id, range="7d")
    k = data["kpis"]
    assert (k["keywords_total"], k["contents_total"], k["links_total"]) == (2, 1, 4)
    assert (k["links_alive"], k["links_deleted"]) == (2, 1)
    assert (k["link_alive_rate"], k["link_deleted_rate"]) == (0.6667, 0.3333)
    assert k["seo_index_rate"] == 0.5 and k["geo_cite_rate"] == 0.3333
    assert (k["time_to_index_hours_avg"], k["time_to_index_hours_p50"]) == (15.0, 15.0)
    assert data["breakdowns"]["keywords_by_status"] == {"candidate": 1, "adopted": 1, "discarded": 0}
    assert data["breakdowns"]["alerts_open"] == {"info": 0, "warning": 0, "critical": 1}
    everything = stats_service.overview(db, SYSTEM_SCOPE, range="7d")
    assert everything["kpis"]["keywords_total"] == 3 and everything["breakdowns"]["alerts_open"]["info"] == 1


# =====================================================================
# 4. 趋势
# =====================================================================


def test_trends_granularity_and_ratios(client: TestClient, db: Session, world: World) -> None:
    put_stat(db, date(2026, 8, 31), 0, ai_calls=2, ai_succeeded=1, links_total_snapshot=5, cost_cny=Decimal("0.5"))
    put_stat(db, date(2026, 9, 2), 0, ai_calls=3, ai_succeeded=3, links_total_snapshot=7)
    put_stat(db, date(2026, 9, 8), 0, ai_calls=1, ai_succeeded=0, links_total_snapshot=8)
    db.commit()
    params = {"metrics": "ai_calls,ai_success_rate,links_total_snapshot,cost_cny", "granularity": "week",
              "start": "2026-08-31", "end": "2026-09-20"}
    data = ok_data(client.get(f"{STATS}/trends", params=params, headers=world.s.headers))
    assert data == [
        {"date": "2026-W36", "ai_calls": 5, "ai_success_rate": 0.8, "links_total_snapshot": 7, "cost_cny": 0.5, "ai_succeeded": 4},
        {"date": "2026-W37", "ai_calls": 1, "ai_success_rate": 0.0, "links_total_snapshot": 8, "cost_cny": 0.0, "ai_succeeded": 0},
        {"date": "2026-W38", "ai_calls": 0, "ai_success_rate": None, "links_total_snapshot": 8, "cost_cny": 0.0, "ai_succeeded": 0},
    ]
    month = ok_data(client.get(f"{STATS}/trends", params={**params, "granularity": "month", "start": "2026-08-30", "end": "2026-09-08"},
                               headers=world.s.headers))
    assert [(r["date"], r["ai_calls"], r["links_total_snapshot"]) for r in month] == [("2026-08", 2, 5), ("2026-09", 4, 8)]
    day = ok_data(client.get(f"{STATS}/trends", params={"metrics": "links_total_snapshot", "start": "2026-09-01", "end": "2026-09-03"},
                             headers=world.s.headers))
    assert [(r["date"], r["links_total_snapshot"]) for r in day] == [("2026-09-01", 5), ("2026-09-02", 7), ("2026-09-03", 7)]
    assert list(redis_client.scan_iter("cache:stats:trends:*"))


def test_trends_dimensions(client: TestClient, db: Session, world: World) -> None:
    put_stat(db, D1, 0, "capability", "image", tasks_succeeded=1, tasks_failed=1, task_duration_ms_sum=9000, ai_calls=2,
             ai_succeeded=2, ai_duration_ms_sum=600, extra=40, quota_actual=50)
    put_stat(db, D1, 0, "admin", str(world.a.id), tasks_succeeded=2, task_duration_ms_sum=10)
    put_stat(db, D1, 0, "seo_engine", "baidu", seo_indexed_snapshot=2, index_hours_sum=30, index_hours_links=2, seo_newly_indexed=5)
    put_stat(db, D1, 0, links_total_snapshot=8)
    db.commit()
    base = {"start": D1.isoformat(), "end": D1.isoformat()}
    cap = ok_data(client.get(f"{STATS}/trends", params={**base, "metrics": "task_success_rate,task_avg_duration_ms,ai_avg_duration_ms,quota_diff_rate",
                                                        "dimension": "capability", "dimension_key": "image"}, headers=world.s.headers))
    assert cap[0]["task_success_rate"] == 0.5 and cap[0]["task_avg_duration_ms"] == 9000 and cap[0]["ai_avg_duration_ms"] == 300
    assert cap[0]["quota_diff_rate"] == 0.25 and cap[0]["quota_estimated_reconciled"] == 40 and cap[0]["tasks_failed"] == 1
    adm = ok_data(client.get(f"{STATS}/trends", params={**base, "metrics": "task_success_rate", "dimension": "admin",
                                                        "dimension_key": str(world.a.id)}, headers=world.s.headers))
    assert adm[0]["task_success_rate"] == 1.0
    eng = ok_data(client.get(f"{STATS}/trends", params={**base, "metrics": "seo_index_rate_by_engine,time_to_index_hours_avg",
                                                        "dimension": "seo_engine", "dimension_key": "baidu"}, headers=world.s.headers))
    assert eng[0]["seo_index_rate_by_engine"] == 0.25 and eng[0]["links_total_snapshot"] == 8
    assert eng[0]["time_to_index_hours_avg"] == 15.0 and eng[0]["index_hours_links"] == 2     # 分母为 index_hours_links，不是新收录数


def test_trends_validation(client: TestClient, world: World) -> None:
    base = {"start": "2026-09-01", "end": "2026-09-07"}
    h = world.s.headers
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "ai_calls,foo,bar"}, headers=h), 400)
    assert body["data"] == [{"loc": ["query", "metrics"], "msg": "不支持的指标", "type": "unsupported_metric", "input": ["foo", "bar"]}]
    nine = ",".join(stats_service.QUERYABLE_METRICS[:9])
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": nine}, headers=h), 400)
    assert body["data"][0]["type"] == "value_error" and body["data"][0]["loc"] == ["query", "metrics"] and len(body["data"][0]["input"]) == 9
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "ai_calls,quota_reconciled_rate,ai_avg_duration_ms",
                                                    "dimension": "admin", "dimension_key": "1"}, headers=h), 400)
    assert body["message"] == "该指标不支持此维度：quota_reconciled_rate（admin）、ai_avg_duration_ms（admin）"
    assert body["data"] == [
        {"loc": ["query", "dimension"], "msg": "该指标不支持此维度", "type": "unsupported_dimension",
         "input": {"metric": "quota_reconciled_rate", "dimension": "admin"}},
        {"loc": ["query", "dimension"], "msg": "该指标不支持此维度", "type": "unsupported_dimension",
         "input": {"metric": "ai_avg_duration_ms", "dimension": "admin"}},
    ]
    body = err(client.get(f"{STATS}/trends", params={"metrics": "ai_calls", "start": "2024-01-01", "end": "2026-01-02"}, headers=h), 400)
    assert body["data"][0]["loc"] == ["query", "end"] and body["data"][0]["type"] == "value_error"
    body = err(client.get(f"{STATS}/trends", params={"metrics": "ai_calls", "start": "2026-09-02", "end": "2026-09-01"}, headers=h), 400)
    assert body["data"][0]["loc"] == ["query", "end"]
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "ai_calls", "dimension": "model"}, headers=h), 400)
    assert body["data"][0]["loc"] == ["query", "dimension_key"] and body["data"][0]["type"] == "value_error"
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "ai_calls", "granularity": "year"}, headers=h), 400)
    assert body["data"][0]["loc"] == ["query", "granularity"]
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "ai_calls", "dimension": "project"}, headers=h), 400)
    assert body["data"][0]["loc"] == ["query", "dimension"]
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "keywords_total"}, headers=h), 400)
    assert body["data"][0]["type"] == "unsupported_metric"           # 当前值指标不经 daily_stats
    body = err(client.get(f"{STATS}/trends", params={**base, "metrics": "seo_index_rate_by_engine"}, headers=h), 400)
    assert body["data"][0]["type"] == "unsupported_dimension"
    body = err(client.get(f"{STATS}/trends", params={"metrics": "ai_calls", "start": "2026-13-01", "end": "2026-09-01"}, headers=h), 400)
    assert body["data"][0]["loc"] == ["query", "start"]


# =====================================================================
# 5. 分解
# =====================================================================


def _breakdown_world(db: Session, world: World) -> None:
    put_stat(db, D1, world.pa.id, contents_created=4, ai_calls=4, ai_succeeded=2, cost_cny=Decimal("2"))
    put_stat(db, D1, world.pb.id, contents_created=1)
    put_stat(db, D1, 9999, contents_created=2, cost_cny=Decimal("3"))          # 已删除项目
    put_stat(db, D1, 0, contents_created=7, ai_calls=4, ai_succeeded=2, cost_cny=Decimal("5"))
    put_stat(db, D1, world.pa.id, "capability", "content", cost_cny=Decimal("1"), ai_calls=4, ai_succeeded=2)
    for pid in (0, world.pa.id):
        put_stat(db, D1, pid, "platform", "zhihu", links_deleted=3, links_backfilled=1)
        put_stat(db, D1, pid, "platform", "csdn", links_backfilled=2)
    db.commit()


def test_breakdown_project_and_platform(client: TestClient, db: Session, world: World) -> None:
    _breakdown_world(db, world)
    base = {"start": D1.isoformat(), "end": D1.isoformat()}
    data = ok_data(client.get(f"{STATS}/breakdown", params={**base, "dimension": "project",
                                                           "metric": "contents_created,ai_success_rate,cost_cny_per_content"},
                              headers=world.s.headers))
    assert [(r["key"], r["label"], r["value"], r["share"]) for r in data] == [
        (str(world.pa.id), "项目A", 4, 0.5714), ("9999", "#9999", 2, 0.2857), (str(world.pb.id), "项目B", 1, 0.1429)]
    assert data[0]["values"] == {"contents_created": 4, "ai_success_rate": 0.5, "cost_cny_per_content": 0.25}
    assert data[2]["values"]["ai_success_rate"] is None
    mine = ok_data(client.get(f"{STATS}/breakdown", params={**base, "dimension": "project", "metric": "contents_created"},
                              headers=world.a.headers))
    assert [r["key"] for r in mine] == [str(world.pa.id)]                     # owner 范围：已删除项目与他人项目不出现
    ratio = ok_data(client.get(f"{STATS}/breakdown", params={**base, "dimension": "project", "metric": "ai_success_rate"},
                               headers=world.s.headers))
    assert all(r["share"] is None for r in ratio)
    plat = ok_data(client.get(f"{STATS}/breakdown", params={**base, "dimension": "platform", "metric": "links_deleted,links_backfilled"},
                              headers=world.s.headers))
    assert [(r["key"], r["label"], r["value"], r["share"]) for r in plat] == [("zhihu", "知乎", 3, 1.0), ("csdn", "CSDN", 0, 0.0)]
    assert plat[1]["values"] == {"links_deleted": 0, "links_backfilled": 2}    # value=0 的键仍返回
    en = ok_data(client.get(f"{STATS}/breakdown", params={**base, "dimension": "platform", "metric": "links_deleted", "lang": "en-US"},
                            headers=world.s.headers))
    assert en[0]["label"] == "Zhihu"
    body = err(client.get(f"{STATS}/breakdown", params={**base, "dimension": "admin", "metric": "foo"}, headers=world.s.headers), 400)
    assert body["data"][0]["loc"] == ["query", "metric"] and body["data"][0]["type"] == "unsupported_metric"
    body = err(client.get(f"{STATS}/breakdown", params={**base, "dimension": "platform", "metric": "ai_calls"}, headers=world.s.headers), 400)
    assert body["data"][0]["type"] == "unsupported_dimension"


def test_breakdown_same_keys_for_split_requests(client: TestClient, db: Session, world: World) -> None:
    put_stat(db, D1, 0, "capability", "content", ai_calls=3, ai_succeeded=3, cost_cny=Decimal("1"), tasks_succeeded=1)
    put_stat(db, D1, 0, "capability", "image", ai_calls=1, ai_succeeded=0, tasks_failed=1)
    put_stat(db, D1, 0, "capability", "video")
    db.commit()
    base = {"start": D1.isoformat(), "end": D1.isoformat(), "dimension": "capability"}
    first = ok_data(client.get(f"{STATS}/breakdown", params={**base, "metric": "ai_calls,ai_success_rate,tokens_total,quota_estimated,"
                                                                            "quota_estimated_reconciled,quota_actual,quota_reconciled_rate,cost_cny"},
                               headers=world.s.headers))
    second = ok_data(client.get(f"{STATS}/breakdown", params={**base, "metric": "task_success_rate,task_avg_duration_ms"},
                                headers=world.s.headers))
    assert {r["key"] for r in first} == {r["key"] for r in second} == {"content", "image", "video"}
    assert first[0]["label"] == "内容"


def test_breakdown_owner_dimension(client: TestClient, db: Session, world: World) -> None:
    _breakdown_world(db, world)
    base = {"start": D1.isoformat(), "end": D1.isoformat(), "dimension": "owner", "metric": "contents_created"}
    data = ok_data(client.get(f"{STATS}/breakdown", params=base, headers=world.s.headers))
    assert [(r["key"], r["label"], r["value"]) for r in data] == [(str(world.a.id), "用户甲", 4), ("0", "已删除项目", 2),
                                                                  (str(world.b.id), "用户乙", 1)]
    assert [r["key"] for r in ok_data(client.get(f"{STATS}/breakdown", params=base, headers=world.a.headers))] == [str(world.a.id)]
    as_a = ok_data(client.get(f"{STATS}/breakdown", params={**base, "owner_id": world.a.id}, headers=world.s.headers))
    assert [r["key"] for r in as_a] == [str(world.a.id)]
    world.b.admin.is_active = False
    db.commit()
    stats_service.clear_cache()
    data = ok_data(client.get(f"{STATS}/breakdown", params=base, headers=world.s.headers))
    assert data[2]["label"] == "用户乙（已禁用）"


# =====================================================================
# 6. 榜单
# =====================================================================


def test_rankings_types(client: TestClient, db: Session, world: World) -> None:
    _breakdown_world(db, world)
    put_stat(db, D1, 0, "model", "mock-text", cost_cny=Decimal("4"), ai_calls=10, ai_succeeded=8, ai_failed=2)
    put_stat(db, D1, 0, "model", "mock-image", cost_cny=Decimal("1"), ai_calls=4, ai_succeeded=1, ai_failed=3)
    cb = content(db, world.pa, at(D1), world.a.id)
    for _ in range(2):
        link(db, world.pa, cb, world.zhihu, created_at=at(D1), published_at=at(D1), by=world.a.id)
    db.commit()
    base = {"start": D1.isoformat(), "end": D1.isoformat()}
    h = world.s.headers
    plats = ok_data(client.get(f"{STATS}/rankings", params={**base, "type": "most_deleted_platforms"}, headers=h))
    assert plats[0] == {"rank": 1, "key": "zhihu", "label": "知乎", "value": 3, "extra": {"links_total": 2}}
    cost = ok_data(client.get(f"{STATS}/rankings", params={**base, "type": "top_cost_models"}, headers=h))
    assert [(r["key"], r["value"], r["extra"]) for r in cost] == [("mock-text", 4.0, {"ai_calls": 10, "ai_success_rate": 0.8}),
                                                                  ("mock-image", 1.0, {"ai_calls": 4, "ai_success_rate": 0.25})]
    failed = ok_data(client.get(f"{STATS}/rankings", params={**base, "type": "top_failed_models", "limit": 1}, headers=h))
    assert [(r["rank"], r["key"], r["value"]) for r in failed] == [(1, "mock-image", 3)]
    projects = ok_data(client.get(f"{STATS}/rankings", params={**base, "type": "top_cost_projects"}, headers=h))
    assert [(r["key"], r["label"], r["value"]) for r in projects][:2] == [("9999", "#9999", 3.0), (str(world.pa.id), "项目A", 2.0)]
    assert projects[1]["extra"] == {"contents_created": 4, "cost_cny_per_content": 0.25}
    mine = ok_data(client.get(f"{STATS}/rankings", params={**base, "type": "top_cost_projects"}, headers=world.a.headers))
    assert [r["key"] for r in mine] == [str(world.pa.id)]
    assert err(client.get(f"{STATS}/rankings", params={**base, "type": "top_cost_models", "limit": 101}, headers=h), 400)
    assert err(client.get(f"{STATS}/rankings", params={**base, "type": "nope"}, headers=h), 400)["data"][0]["loc"] == ["query", "type"]
    settings_service.set_value(db, "stats_config", {"rankings_limit": 1})
    assert len(ok_data(client.get(f"{STATS}/rankings", params={**base, "type": "top_cost_models", "project_id": 0, "lang": "en-US"},
                                  headers=h))) == 1


def test_rankings_fastest_indexed(db: Session, world: World) -> None:
    c = content(db, world.pa, at(D1), world.a.id, title="最快文章")
    pub = at(D1, 1)
    fast = link(db, world.pa, c, world.zhihu, created_at=pub, published_at=pub, by=world.a.id, first_indexed_at=pub + timedelta(hours=5))
    edge = link(db, world.pa, c, world.csdn, created_at=pub + timedelta(hours=72), published_at=pub, by=world.a.id,
                first_indexed_at=pub + timedelta(hours=20))                                   # 补录延迟恰为 72 小时：计入
    link(db, world.pa, c, world.csdn, created_at=pub + timedelta(hours=72, seconds=1), published_at=pub, by=world.a.id,
         first_indexed_at=pub + timedelta(hours=2))                                           # 历史补录：不参与排名
    link(db, world.pa, c, world.zhihu, created_at=pub, published_at=pub, by=world.a.id, first_indexed_at=at(D2, 12))   # 范围外
    other = content(db, world.pb, at(D1), world.b.id)
    link(db, world.pb, other, world.zhihu, created_at=pub, published_at=pub, by=world.b.id, first_indexed_at=pub + timedelta(hours=1))
    db.commit()
    data = stats_service.rankings(db, SYSTEM_SCOPE, ranking_type="fastest_indexed", start=D1, end=D1, project_id=world.pa.id)
    assert [(r["rank"], r["link_id"], r["hours"]) for r in data] == [(1, fast.id, 5), (2, edge.id, 20)]
    assert data[0] == {"rank": 1, "content_id": c.id, "title": "最快文章", "link_id": fast.id, "url": fast.url, "platform_code": "zhihu",
                       "platform_name": "知乎", "project_id": world.pa.id, "project_name": "项目A", "published_at": iso(pub),
                       "first_indexed_at": iso(pub + timedelta(hours=5)), "hours": 5}
    everyone = stats_service.rankings(db, SYSTEM_SCOPE, ranking_type="fastest_indexed", start=D1, end=D1, limit=10)
    assert len(everyone) == 3 and everyone[0]["project_id"] == world.pb.id
    mine = stats_service.rankings(db, scope_of(db, world.a), ranking_type="fastest_indexed", start=D1, end=D1)
    assert {r["project_id"] for r in mine} == {world.pa.id}


# =====================================================================
# 7. 导出
# =====================================================================


def _csv(response: Any) -> tuple[str, list[list[str]]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    raw = response.content.decode("utf-8")
    assert raw.startswith("﻿")
    return raw, list(csv.reader(io.StringIO(raw[1:], newline="")))


def test_export_trends_breakdown_rankings(client: TestClient, db: Session, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    put_stat(db, E1, 0, ai_calls=70, cost_cny=Decimal("17.2"))
    put_stat(db, E2, 0, ai_calls=82, cost_cny=Decimal("19.9"))
    db.commit()
    stamp = _today(db).strftime("%Y%m%d")
    res = client.get(f"{STATS}/export", params={"report": "trends", "metrics": "ai_calls,cost_cny,keyword_adopt_rate", "granularity": "day",
                                                "start": E1.isoformat(), "end": E2.isoformat(), "project_id": 0},
                     headers=world.ro.headers)
    raw, rows = _csv(res)
    assert f'filename="stats-trends-{stamp}.csv"' in res.headers["content-disposition"]
    assert "\r\n" in raw and raw.count("\r\n") == 3
    assert rows == [["日期", "AI 调用", "费用（元）", "关键词采用率"], ["2026-08-01", "70", "17.200000", ""], ["2026-08-02", "82", "19.900000", ""]]
    _, rows = _csv(client.get(f"{STATS}/export", params={"report": "trends", "metrics": "ai_calls", "start": D1.isoformat(),
                                                         "end": D1.isoformat(), "dimension": "model", "dimension_key": "mock-text"},
                              headers=world.s.headers))
    assert rows == [["维度", "维度键", "日期", "AI 调用"], ["model", "mock-text", "2026-09-01", "0"]]
    _breakdown_world(db, world)
    stats_service.clear_cache()
    _, rows = _csv(client.get(f"{STATS}/export", params={"report": "breakdown", "dimension": "project",
                                                         "metric": "contents_created,ai_success_rate", "start": D1.isoformat(),
                                                         "end": D1.isoformat()}, headers=world.s.headers))
    assert rows[0] == ["维度", "键", "名称", "数值", "占比", "内容新增", "尝试成功率"]
    assert rows[1] == ["project", str(world.pa.id), "项目A", "4", "0.5714", "4", "0.5000"]
    _, rows = _csv(client.get(f"{STATS}/export", params={"report": "rankings", "type": "fastest_indexed", "start": D1.isoformat(),
                                                         "end": D1.isoformat()}, headers=world.s.headers))
    assert rows == [["名次", "内容 ID", "标题", "链接 ID", "链接", "平台代码", "平台", "项目 ID", "项目", "发布时间", "首次收录时间",
                     "收录耗时（小时）"]]
    _, rows = _csv(client.get(f"{STATS}/export", params={"report": "rankings", "type": "most_deleted_platforms",
                                                         "start": D1.isoformat(), "end": D1.isoformat()}, headers=world.s.headers))
    assert rows[0] == ["名次", "键", "名称", "数值", "链接总数"] and rows[1][:4] == ["1", "zhihu", "知乎", "3"]
    # 必填项按 report 校验；超过上限 400；无导出权限 403
    body = err(client.get(f"{STATS}/export", params={"report": "breakdown", "start": D1.isoformat(), "end": D1.isoformat()},
                          headers=world.s.headers), 400)
    assert [e["loc"] for e in body["data"]] == [["query", "dimension"], ["query", "metric"]]
    monkeypatch.setattr(stats_service, "EXPORT_MAX_ROWS", 1)
    body = err(client.get(f"{STATS}/export", params={"report": "trends", "metrics": "ai_calls", "start": D1.isoformat(),
                                                     "end": D2.isoformat()}, headers=world.s.headers), 400)
    assert body["message"] == "导出行数超过 1，请收窄筛选范围"
    assert body["data"] == [{"loc": ["query"], "msg": "导出行数超过 1，请收窄筛选范围", "type": "value_error", "input": 2}]
    err(client.get(f"{STATS}/export", params={"report": "trends", "metrics": "ai_calls", "start": D1.isoformat(), "end": D1.isoformat()},
                   headers=world.r.headers), 403)
    assert err(client.get(f"{STATS}/export", params={"report": "nope"}, headers=world.s.headers), 400)["data"][0]["loc"] == ["query", "report"]


# =====================================================================
# 8. 重算与 monitor_worker 接线
# =====================================================================


def test_recompute_sync_async_and_validation(client: TestClient, db: Session, world: World) -> None:
    t = _today(db)
    token = acquire_lock(agg.lock_key(t - timedelta(days=1)), 60)
    try:
        data = ok_data(client.post(f"{STATS}/recompute", json={"start_date": (t - timedelta(days=2)).isoformat(), "end_date": t.isoformat()},
                                   headers=world.s.headers))
    finally:
        release_lock(agg.lock_key(t - timedelta(days=1)), token)
    assert data["days"] == 3 and data["skipped"] == [(t - timedelta(days=1)).isoformat()] and data["rows_upserted"] >= 2
    assert isinstance(data["duration_ms"], int)
    db.expire_all()
    assert row(db, t, 0) is not None and row(db, t - timedelta(days=1), 0) is None
    log = db.scalars(select(m.AdminOperationLog).where(m.AdminOperationLog.admin_id == world.s.id)
                     .order_by(m.AdminOperationLog.id.desc())).first()
    assert (log.action, log.target_type, log.target_id, log.permission_code) == ("execute", "daily_stats", None, "stats.reports.recompute")
    assert log.summary == f"重算统计 {(t - timedelta(days=2)).isoformat()}~{t.isoformat()}"
    res = client.post(f"{STATS}/recompute", json={"start_date": (t - timedelta(days=9)).isoformat(), "end_date": t.isoformat()},
                      headers=world.s.headers)
    assert res.status_code == 202 and res.json()["data"] == {"queued": True, "days": 10}
    assert json.loads(redis_client.lindex("queue:stats_recompute", 0)) == {
        "start_date": (t - timedelta(days=9)).isoformat(), "end_date": t.isoformat(), "requested_by": world.s.id}
    for body in ({"start_date": (t - timedelta(days=31)).isoformat(), "end_date": t.isoformat()},
                 {"start_date": t.isoformat(), "end_date": (t + timedelta(days=1)).isoformat()},
                 {"start_date": t.isoformat(), "end_date": (t - timedelta(days=1)).isoformat()}):
        res = err(client.post(f"{STATS}/recompute", json=body, headers=world.s.headers), 400)
        assert res["data"][0]["loc"] == ["body", "end_date"] and res["data"][0]["type"] == "value_error"
    err(client.post(f"{STATS}/recompute", json={"start_date": t.isoformat(), "end_date": t.isoformat()}, headers=world.ro.headers), 403)


class _FakePool:
    def __init__(self, running: bool) -> None:
        self._running = running
        self.submitted: list[tuple[str, Any]] = []

    def running(self, name: str) -> bool:
        return self._running and name == "recompute"

    def named_submit(self, name: str, fn: Any, *args: Any) -> object:
        self.submitted.append((name, args))
        return object()


def test_drain_recompute_waits_for_inflight(db: Session, world: World) -> None:
    t = _today(db)
    payload = json.dumps({"start_date": (t - timedelta(days=1)).isoformat(), "end_date": t.isoformat(), "requested_by": 1})
    redis_client.rpush("queue:stats_recompute", payload)
    busy = _FakePool(running=True)
    assert agg.drain_recompute(busy) == 0 and busy.submitted == []
    assert redis_client.llen("queue:stats_recompute") == 1                     # 在飞时不出队，元素不丢失
    idle = _FakePool(running=False)
    assert agg.drain_recompute(idle) == 1 and redis_client.llen("queue:stats_recompute") == 0
    name, (parsed,) = idle.submitted[0]
    assert name == "recompute" and parsed["start_date"] == t - timedelta(days=1)
    assert agg.run_recompute(parsed) == 2
    assert row(db, t, 0) is not None and row(db, t - timedelta(days=1), 0) is not None
    redis_client.rpush("queue:stats_recompute", "not json")
    assert agg.drain_recompute(idle) == 0 and redis_client.llen("queue:stats_recompute") == 0


def test_monitor_worker_wiring(db: Session, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from app import monitor_worker

    real = monitor_worker.optional_task
    monkeypatch.setattr(monitor_worker, "optional_task",
                        lambda module, attr: real(module, attr) if module == "aggregate_daily_stats" else None)
    worker = monitor_worker.MonitorWorker(poll_interval=0, link_concurrency=1, index_concurrency=1)
    try:
        assert worker.drain_recompute is agg.drain_recompute and worker.daily_job is agg.daily_job
        assert worker.catch_up is agg.catch_up and worker.aggregate_today is agg.aggregate_today
        worker.daily_job = None
        worker.aggregate = None                                                # 本用例不触发每日聚合
        gate = threading.Event()
        worker.pool.named_submit("recompute", gate.wait, 5)
        redis_client.rpush("queue:stats_recompute", json.dumps({"start_date": D1.isoformat(), "end_date": D1.isoformat()}))
        worker.tick()
        assert redis_client.llen("queue:stats_recompute") == 1
        gate.set()
        for _ in range(100):
            if not worker.pool.running("recompute"):
                break
            threading.Event().wait(0.02)
        worker.tick()
        assert redis_client.llen("queue:stats_recompute") == 0
    finally:
        worker.pool.shutdown(wait=True)
    db.expire_all()
    assert row(db, D1, 0) is not None


def test_catch_up_and_daily_job(db: Session, world: World) -> None:
    t = _today(db)
    put_stat(db, t - timedelta(days=800), 0, ai_calls=1)
    put_stat(db, t - timedelta(days=5), 0, ai_calls=1)
    db.commit()
    assert agg.catch_up(days=3) >= 3
    assert all(row(db, t - timedelta(days=i), 0) is not None for i in range(3))
    assert agg.daily_job() >= 2
    db.expire_all()
    assert row(db, t - timedelta(days=800), 0) is None and row(db, t - timedelta(days=5), 0) is not None   # 保留期 730 天
    token = acquire_lock(agg.lock_key(t), 60)
    try:
        assert agg.aggregate(t) == 0 and agg.aggregate_locked(t) is None
    finally:
        release_lock(agg.lock_key(t), token)


# =====================================================================
# 9. 权限
# =====================================================================


def test_permissions(client: TestClient, db: Session, world: World, users: UserFactory) -> None:
    t = _today(db).isoformat()
    q = {"start": t, "end": t}
    nobody = users.create(users.custom_group("无报表", ["content.keywords.view"]), username="nostats")
    for path, params in (("overview", {}), ("trends", {**q, "metrics": "ai_calls"}),
                         ("breakdown", {**q, "dimension": "capability", "metric": "ai_calls"}),
                         ("rankings", {**q, "type": "top_cost_models"}), ("export", {**q, "report": "trends", "metrics": "ai_calls"})):
        err(client.get(f"{STATS}/{path}", params=params, headers=nobody.headers), 403)
    dash_only = users.create(users.custom_group("只看控制台", ["dashboard.view"]), username="dashonly")
    ok_data(client.get(f"{STATS}/overview", headers=dash_only.headers))
    err(client.get(f"{STATS}/trends", params={**q, "metrics": "ai_calls"}, headers=dash_only.headers), 403)
    for user in (world.r, world.ro, world.a):
        ok_data(client.get(f"{STATS}/trends", params={**q, "metrics": "ai_calls"}, headers=user.headers))
    ok_data(client.get(f"{STATS}/breakdown", params={**q, "dimension": "capability", "metric": "ai_calls"}, headers=world.r.headers))
    assert client.get(f"{STATS}/export", params={**q, "report": "trends", "metrics": "ai_calls"}, headers=world.ro.headers).status_code == 200
    body = {"start_date": t, "end_date": t}
    for user in (world.a, world.r, world.ro):
        err(client.post(f"{STATS}/recompute", json=body, headers=user.headers), 403)
    ok_data(client.post(f"{STATS}/recompute", json=body, headers=world.s.headers))


# =====================================================================
# 10. 数据范围（docs/13 §10、§16「统计」）
# =====================================================================


def _scope_world(db: Session, world: World, t: date) -> None:
    for day in (t - timedelta(days=1), t):
        put_stat(db, day, world.pa.id, ai_calls=2, cost_cny=Decimal("1"), links_total_snapshot=4, contents_created=1)
        put_stat(db, day, world.pb.id, ai_calls=5, cost_cny=Decimal("2"), links_total_snapshot=6, contents_created=3)
        put_stat(db, day, 0, ai_calls=8, cost_cny=Decimal("3.5"), links_total_snapshot=10, contents_created=4)
    put_stat(db, t, 0, "seo_engine", "baidu", seo_indexed_snapshot=5)
    put_stat(db, t, 0, "seo_engine", "bing", seo_indexed_snapshot=0)
    put_stat(db, t, world.pa.id, "seo_engine", "baidu", seo_indexed_snapshot=2)
    put_stat(db, t, world.pb.id, "seo_engine", "baidu", seo_indexed_snapshot=3)
    put_stat(db, t, world.pa.id, "capability", "content", ai_calls=2, cost_cny=Decimal("1"))
    put_stat(db, t, 0, "capability", "content", ai_calls=8, cost_cny=Decimal("3.5"))
    db.commit()


def test_owner_scope_overview(client: TestClient, db: Session, world: World) -> None:
    t = _today(db)
    _scope_world(db, world, t)
    mine = ok_data(client.get(f"{STATS}/overview", headers=world.a.headers))
    assert (mine["meta"]["scope"], mine["meta"]["owner_id"], mine["meta"]["today_source"]) == ("owner", world.a.id, "daily_stats")
    assert (mine["kpis"]["ai_calls"], mine["kpis"]["cost_cny"], mine["kpis"]["contents_created"]) == (4, 2.0, 2)   # = PA 各行之和
    seo = mine["breakdowns"]["seo_index_rate_by_engine"]
    assert seo["baidu"] == {"rate": 0.5, "hit": 2, "total": 4} and seo["bing"] == {"rate": 0.0, "hit": 0, "total": 4}
    assert seo["google"]["hit"] is None
    assert mine["breakdowns"]["cost_by_capability"][0]["ai_calls"] == 2
    assert redis_client.exists(f"cache:stats:overview:owner:{world.a.id}:0:7d")
    as_a = ok_data(client.get(f"{STATS}/overview", params={"owner_id": world.a.id}, headers=world.s.headers))
    assert as_a["meta"]["cached"] is True                                       # 与本人共用缓存键
    as_a["meta"]["cached"] = False
    assert as_a == mine
    everyone = ok_data(client.get(f"{STATS}/overview", headers=world.s.headers))
    assert everyone["meta"]["scope"] == "all" and everyone["kpis"]["ai_calls"] == 16 and everyone["meta"]["cached"] is False
    assert everyone["breakdowns"]["seo_index_rate_by_engine"]["baidu"] == {"rate": 0.5, "hit": 5, "total": 10}
    assert redis_client.exists("cache:stats:overview:all:0:7d")
    # 他人项目 404；reviewer（all）可查任意项目，已删除项目仍可查保留行
    for path, params in (("overview", {"project_id": world.pb.id}),
                         ("trends", {"metrics": "ai_calls", "start": t.isoformat(), "end": t.isoformat(), "project_id": world.pb.id}),
                         ("breakdown", {"dimension": "capability", "metric": "ai_calls", "start": t.isoformat(), "end": t.isoformat(),
                                        "project_id": world.pb.id}),
                         ("rankings", {"type": "top_cost_models", "start": t.isoformat(), "end": t.isoformat(), "project_id": world.pb.id})):
        err(client.get(f"{STATS}/{path}", params=params, headers=world.a.headers), 404)
    ok_data(client.get(f"{STATS}/overview", params={"project_id": world.pb.id}, headers=world.r.headers))
    ok_data(client.get(f"{STATS}/overview", params={"project_id": 9999}, headers=world.r.headers))
    trend = ok_data(client.get(f"{STATS}/trends", params={"metrics": "ai_calls,links_total_snapshot", "start": t.isoformat(),
                                                         "end": t.isoformat()}, headers=world.a.headers))
    assert trend == [{"date": t.isoformat(), "ai_calls": 2, "links_total_snapshot": 4}]
    # 项目详情页 KPI 复用总览
    proj = ok_data(client.get(f"{ADMIN_API}/projects/{world.pa.id}/overview", params={"range": "today"}, headers=world.a.headers))
    assert proj["meta"]["project_id"] == world.pa.id and proj["kpis"]["ai_calls"] == 2 and "placeholder" not in proj["meta"]


def test_owner_scope_realtime_and_transfer(client: TestClient, db: Session, world: World) -> None:
    stats_service.increment_realtime(world.pa.id, {"ai_calls": 2})
    stats_service.increment_realtime(world.pb.id, {"ai_calls": 3})
    mine = ok_data(client.get(f"{STATS}/overview", params={"range": "today"}, headers=world.a.headers))
    assert mine["meta"]["today_source"] == "realtime" and mine["kpis"]["ai_calls"] == 2
    assert stats_service.realtime_today(scope_of(db, world.a), 0, db=db) == {"ai_calls": 2}
    everyone = ok_data(client.get(f"{STATS}/overview", params={"range": "today"}, headers=world.s.headers))
    assert everyone["kpis"]["ai_calls"] == 5
    # 转移负责人：统计随之归属并清除 cache:stats:*
    t = _today(db)
    put_stat(db, t - timedelta(days=1), world.pa.id, contents_created=2)
    put_stat(db, t - timedelta(days=1), world.pb.id, contents_created=1)
    db.commit()
    q = {"dimension": "owner", "metric": "contents_created", "start": (t - timedelta(days=1)).isoformat(), "end": t.isoformat()}
    before = ok_data(client.get(f"{STATS}/breakdown", params=q, headers=world.s.headers))
    assert {r["key"]: r["value"] for r in before} == {str(world.a.id): 2, str(world.b.id): 1}
    assert list(redis_client.scan_iter("cache:stats:*"))
    ok_data(client.put(f"{ADMIN_API}/projects/{world.pa.id}", json={"owner_id": world.b.id}, headers=world.s.headers))
    assert not list(redis_client.scan_iter("cache:stats:*"))
    after = ok_data(client.get(f"{STATS}/breakdown", params=q, headers=world.s.headers))
    assert {r["key"]: r["value"] for r in after} == {str(world.b.id): 3}
    assert ok_data(client.get(f"{STATS}/overview", params={"range": "today"}, headers=world.a.headers))["kpis"]["ai_calls"] == 0


def test_cache_keys_include_scope(db: Session, world: World) -> None:
    params = {"metrics": ["ai_calls"], "start": D1, "end": D1, "project_id": 0, "locale": "zh-CN"}
    all_key = stats_service.query_cache_key("trends", SYSTEM_SCOPE, params)
    a_key = stats_service.query_cache_key("trends", scope_of(db, world.a), params)
    s_as_a = stats_service.query_cache_key("trends", scope_of(db, world.s, world.a.id), params)
    assert all_key != a_key and a_key == s_as_a and a_key.startswith("cache:stats:trends:")
    assert stats_service.query_cache_key("trends", SYSTEM_SCOPE, {**params, "locale": "en-US"}) != all_key
    assert stats_service.overview_cache_key(scope_of(db, world.a), 0, "7d") == f"cache:stats:overview:owner:{world.a.id}:0:7d"


# =====================================================================
# 11. 实时计数
# =====================================================================


def test_realtime_event_points(db: Session, world: World) -> None:
    from app.services import alert_service, keyword_service

    t = _today(db)
    scope_a = scope_of(db, world.a)
    kw = keyword_service.create_keyword(db, scope_a, {"project_id": world.pa.id, "keyword": "实时关键词"}, admin_id=world.a.id)
    keyword_service.adopt_keyword(db, scope_a, kw["id"], admin_id=world.a.id)
    alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=1, target_key="1",
                              title="t", message="m", project_id=world.pa.id)
    db.commit()
    data = redis_client.hgetall(stats_service.realtime_key(t, world.pa.id))
    assert (data["keywords_created"], data["keywords_adopted"], data["alerts_opened"]) == ("1", "1", "1")
    assert redis_client.hgetall(stats_service.realtime_key(t, 0))["keywords_created"] == "1"
    assert 259000 < redis_client.ttl(stats_service.realtime_key(t, world.pa.id)) <= 259200
    # 归属到非今日的事件不写
    assert stats_service.increment_realtime(world.pa.id, {"ai_calls": 1}, at=m.utcnow() - timedelta(days=2)) is False
    assert "ai_calls" not in redis_client.hgetall(stats_service.realtime_key(t, world.pa.id))
    with pytest.raises(ValueError):
        stats_service.increment_realtime(world.pa.id, {"links_alive_snapshot": 1})


def test_realtime_index_hours(db: Session, world: World) -> None:
    from app.services import index_check_service as ics

    t = _today(db)
    now = m.utcnow()
    c = content(db, world.pa, now, world.a.id)
    fresh = link(db, world.pa, c, world.zhihu, created_at=now, published_at=now - timedelta(hours=5), by=world.a.id)
    old = link(db, world.pa, c, world.csdn, created_at=now, published_at=now - timedelta(days=10), by=world.a.id)
    db.commit()
    scope_a = scope_of(db, world.a)
    ics.mark_index(db, scope_a, fresh.id, {"kind": "seo", "engine": "baidu", "status": "indexed"}, admin_id=world.a.id)
    ics.mark_index(db, scope_a, old.id, {"kind": "seo", "engine": "baidu", "status": "indexed"}, admin_id=world.a.id)
    data = redis_client.hgetall(stats_service.realtime_key(t, world.pa.id))
    assert (data["seo_checks"], data["seo_newly_indexed"], data["index_hours_sum"], data["index_hours_links"]) == ("2", "2", "5", "1")
