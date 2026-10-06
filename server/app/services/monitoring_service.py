"""监控页查询（``/admin/monitoring``，docs/04 §6.18、§7.12；docs/11 §11.6；docs/13 §4.3、§6.3）。

- ``overview(db, scope, workers=…)``：``due`` / ``today`` / ``last_run_at`` 只按调用者可见的链接及其检测记录计算；``queued``
  （``LLEN queue:link_checks`` / ``queue:index_checks``）、``workers``（``monitor_worker`` 副本心跳）与 ``daily_limits``
  （``limit:link_checks:{date}`` / ``limit:index_checks:{date}``）为平台值；``today`` 与 ``used`` 按 ``stats_config.timezone`` 切日；
- 删除检测 / 收录检测全局记录：经所属链接过滤（``scope_link_children``），筛选参数引用不可见对象时为空结果；删除检测每条附
  ``link{id,url,platform_code,content_id}``（收录检测列表不附链接信息）；单条详情的目标不可见 → 404。

``POST …/run`` 的入队逻辑分别在 ``link_service.run_link_checks_batch`` / ``index_check_service.run_index_checks_batch``。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.redis import redis_client
from app.models import IndexCheck, LinkCheck, PublishLink, PublishPlatform, utcnow
from app.schemas.common import iso_utc, to_utc_naive
from app.services import index_check_service, link_check_service, link_service, stats_service
from app.services.data_scope_service import DataScope, get_visible, scope_by_project, scope_link_children

logger = logging.getLogger(__name__)

MSG_CHECK_NOT_FOUND = "检测记录不存在"


def _llen(key: str) -> int:
    try:
        return int(redis_client.llen(key) or 0)
    except redis.RedisError as exc:
        logger.warning("读取队列长度失败 %s: %s", key, exc)
        return 0


def _count(db: Session, stmt: Any) -> int:
    return int(db.scalar(stmt) or 0)


def overview(db: Session, scope: DataScope, *, workers: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    now = utcnow()
    day_start = stats_service.day_bounds(stats_service.today_date(db), db)[0]
    link_due = _count(db, scope_by_project(select(func.count(PublishLink.id)), PublishLink.project_id, scope).where(
        PublishLink.is_monitoring == True, PublishLink.next_check_at <= now))  # noqa: E712
    index_due = _count(db, scope_by_project(select(func.count(PublishLink.id)), PublishLink.project_id, scope).where(
        PublishLink.is_monitoring == True, PublishLink.next_index_check_at <= now))  # noqa: E712
    link_today = _count(db, scope_link_children(select(func.count(LinkCheck.id)), LinkCheck.link_id, scope).where(
        LinkCheck.checked_at >= day_start))
    index_today = _count(db, scope_link_children(select(func.count(IndexCheck.id)), IndexCheck.link_id, scope).where(
        IndexCheck.checked_at >= day_start))
    link_last = db.scalar(scope_link_children(select(func.max(LinkCheck.checked_at)), LinkCheck.link_id, scope))
    index_last = db.scalar(scope_link_children(select(func.max(IndexCheck.checked_at)), IndexCheck.link_id, scope))
    link_cfg = link_check_service.link_check_config(db)
    index_cfg = index_check_service.index_check_config(db)
    return {
        "link_checks": {"due": link_due, "queued": _llen(link_service.QUEUE_LINK_CHECKS), "today": link_today,
                        "last_run_at": iso_utc(link_last)},
        "index_checks": {"due": index_due, "queued": _llen(index_check_service.QUEUE_INDEX_CHECKS), "today": index_today,
                         "last_run_at": iso_utc(index_last)},
        "workers": list(workers),
        "daily_limits": {
            "link_checks": {"limit": int(link_cfg.get("daily_limit") or 0), "used": link_check_service.used_today(db)},
            "index_checks": {"limit": int(index_cfg.get("daily_limit") or 0), "used": index_check_service.used_today(db)},
        },
    }


# =====================================================================
# 记录列表与详情
# =====================================================================


def _link_filter(project_id: int | None, platform_id: int | None) -> Any:
    stmt = select(PublishLink.id)
    if project_id is not None:
        stmt = stmt.where(PublishLink.project_id == project_id)
    if platform_id is not None:
        stmt = stmt.where(PublishLink.platform_id == platform_id)
    return stmt


def _time_range(column: Any, start: datetime | None, end: datetime | None) -> list[Any]:
    conditions: list[Any] = []
    if start is not None:
        conditions.append(column >= to_utc_naive(start))
    if end is not None:
        conditions.append(column <= to_utc_naive(end))
    return conditions


def _link_briefs(db: Session, link_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not link_ids:
        return {}
    rows = db.execute(
        select(PublishLink.id, PublishLink.url, PublishLink.content_id, PublishPlatform.code)
        .outerjoin(PublishPlatform, PublishPlatform.id == PublishLink.platform_id)
        .where(PublishLink.id.in_(link_ids))
    ).all()
    return {r.id: {"id": r.id, "url": r.url, "platform_code": r.code or "", "content_id": r.content_id} for r in rows}


def _link_check_item(check: LinkCheck, briefs: dict[int, dict[str, Any]]) -> dict[str, Any]:
    item = link_check_service.link_check_item(check)
    item["link"] = briefs.get(check.link_id)
    return item


def list_link_checks(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    project_id: int | None = None,
    platform_id: int | None = None,
    result_status: str | None = None,
    check_type: str | None = None,
    link_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """全部删除检测记录（按 ``checked_at`` 倒序），每条附 ``link{id,url,platform_code,content_id}``。"""
    stmt = scope_link_children(select(LinkCheck), LinkCheck.link_id, scope)
    if project_id is not None or platform_id is not None:
        stmt = stmt.where(LinkCheck.link_id.in_(_link_filter(project_id, platform_id)))
    if result_status:
        stmt = stmt.where(LinkCheck.result_status == result_status)
    if check_type:
        stmt = stmt.where(LinkCheck.check_type == check_type)
    if link_id is not None:
        stmt = stmt.where(LinkCheck.link_id == link_id)
    stmt = stmt.where(*_time_range(LinkCheck.checked_at, start, end))
    total = _count(db, select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(LinkCheck.checked_at.desc(), LinkCheck.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    briefs = _link_briefs(db, {r.link_id for r in rows})
    return [_link_check_item(r, briefs) for r in rows], total


def get_link_check(db: Session, scope: DataScope, check_id: int) -> dict[str, Any]:
    check = get_visible(db, scope, LinkCheck, check_id, message=MSG_CHECK_NOT_FOUND)
    return _link_check_item(check, _link_briefs(db, {check.link_id}))


def list_index_checks(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    kind: str | None = None,
    engine: str | None = None,
    provider: str | None = None,
    result_status: str | None = None,
    project_id: int | None = None,
    platform_id: int | None = None,
    link_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """收录检测记录（按 ``checked_at`` 倒序；列表不附链接信息，前端按 ``link_id`` 跳转详情）。"""
    stmt = scope_link_children(select(IndexCheck), IndexCheck.link_id, scope)
    if project_id is not None or platform_id is not None:
        stmt = stmt.where(IndexCheck.link_id.in_(_link_filter(project_id, platform_id)))
    if kind:
        stmt = stmt.where(IndexCheck.kind == kind)
    if engine:
        stmt = stmt.where(IndexCheck.engine == engine)
    if provider:
        stmt = stmt.where(IndexCheck.provider == provider)
    if result_status:
        stmt = stmt.where(IndexCheck.result_status == result_status)
    if link_id is not None:
        stmt = stmt.where(IndexCheck.link_id == link_id)
    stmt = stmt.where(*_time_range(IndexCheck.checked_at, start, end))
    total = _count(db, select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(IndexCheck.checked_at.desc(), IndexCheck.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return [link_service.index_check_item(r) for r in rows], total


def get_index_check(db: Session, scope: DataScope, check_id: int) -> dict[str, Any]:
    check = get_visible(db, scope, IndexCheck, check_id, message=MSG_CHECK_NOT_FOUND)
    return link_service.index_check_item(check)


__all__ = ["get_index_check", "get_link_check", "list_index_checks", "list_link_checks", "overview"]
