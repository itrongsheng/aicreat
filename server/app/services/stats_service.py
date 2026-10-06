"""统计服务（docs/12-dashboard-reports.md）。

本阶段只实现「实时计数兜底」与时区工具（§4.6、§4.7）：

- ``get_tz()`` / ``today_date()`` / ``day_bounds(stat_date)`` / ``local_date(dt)``：按 ``stats_config.timezone`` 切日，
  ``day_bounds`` 返回 naive UTC ``[start, end)``，聚合查询统一写成 ``col >= :start AND col < :end``；
- ``increment_realtime(project_id, fields, at=…)``：``stats:rt:{date}:{project_id}`` Hash 计数（同时累加 ``:0`` 汇总键），
  只在归属日期 == 今日时写，整数列 ``HINCRBY``、``cost_cny`` ``HINCRBYFLOAT``，每次写后 ``EXPIRE 259200``；
- ``increment_realtime_after_commit(db, …)``：事件发生的 service 在事务提交后写入（docs/03「事务边界总则」）；
- ``realtime_today(scope, project_id)``：读今日实时计数（``owner`` 范围对 P 内项目逐键求和，不读 ``:0`` 汇总键）。

``overview`` / ``trends`` / ``breakdown`` / ``rankings`` / ``export`` / ``aggregate_daily`` 由第 6 步补充到本文件。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, after_commit
from app.core.redis import redis_client
from app.models import DAILY_STATS_METRIC_COLUMNS, Project
from app.services import settings_service
from app.services.data_scope_service import DataScope

logger = logging.getLogger(__name__)

REALTIME_KEY_PREFIX = "stats:rt:"
REALTIME_TTL_SECONDS = 259200
# 历史补录判定：回填时间晚于发布时间超过该小时数的链接不累计收录耗时（docs/12 §4.1）
MAX_BACKFILL_DELAY_HOURS = 72

FLOAT_FIELDS = frozenset({"cost_cny"})
# 实时计数不写入的列（§4.6）：对账回填列与全部快照列
REALTIME_EXCLUDED_FIELDS = frozenset(
    {"quota_actual", "quota_reconciled_calls"} | {c for c in DAILY_STATS_METRIC_COLUMNS if c.endswith("_snapshot")}
)
REALTIME_FIELDS: tuple[str, ...] = tuple(c for c in DAILY_STATS_METRIC_COLUMNS if c not in REALTIME_EXCLUDED_FIELDS)
_REALTIME_FIELD_SET = frozenset(REALTIME_FIELDS)


# =====================================================================
# 时区（§4.7）
# =====================================================================


def _timezone_name(db: Session | None) -> str:
    try:
        if db is not None:
            return str(settings_service.get_config(db, "stats_config").get("timezone") or settings.app_timezone)
        with SessionLocal() as session:
            return str(settings_service.get_config(session, "stats_config").get("timezone") or settings.app_timezone)
    except Exception:  # noqa: BLE001 - 配置不可读时回退环境变量，不让计数路径失败
        logger.warning("读取 stats_config.timezone 失败，回退 APP_TIMEZONE", exc_info=True)
        return settings.app_timezone


def get_tz(db: Session | None = None) -> ZoneInfo:
    """``zoneinfo.ZoneInfo(stats_config.timezone)``；时区名非法时回退 ``APP_TIMEZONE``，再回退 UTC。"""
    for name in (_timezone_name(db), settings.app_timezone):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("无效的时区 %r", name)
    return ZoneInfo("UTC")


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def local_date(value: datetime | None = None, db: Session | None = None, *, tz: ZoneInfo | None = None) -> date:
    """naive UTC 时间（缺省为当前时刻）在统计时区下的日期。"""
    tz = tz or get_tz(db)
    value = value or _now_utc()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).date()


def today_date(db: Session | None = None, *, tz: ZoneInfo | None = None) -> date:
    """统计时区的今天。"""
    return local_date(None, db, tz=tz)


def day_bounds(stat_date: date, db: Session | None = None, *, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """统计日的 UTC 边界 ``[start, end)``（naive UTC）：本地 ``00:00`` 与次日 ``00:00`` 换算为 UTC。"""
    tz = tz or get_tz(db)
    start_local = datetime(stat_date.year, stat_date.month, stat_date.day, tzinfo=tz)
    nxt = stat_date + timedelta(days=1)
    end_local = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return (
        start_local.astimezone(UTC).replace(tzinfo=None),
        end_local.astimezone(UTC).replace(tzinfo=None),
    )


def range_bounds(start: date, end: date, db: Session | None = None, *, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    """闭区间 ``[start, end]`` 日期的 UTC 边界 ``[day_bounds(start)[0], day_bounds(end)[1])``。"""
    tz = tz or get_tz(db)
    return day_bounds(start, tz=tz)[0], day_bounds(end, tz=tz)[1]


# =====================================================================
# 实时计数 stats:rt:{date}:{project_id}（§4.6）
# =====================================================================


def realtime_key(stat_date: date | str, project_id: int | None) -> str:
    day = stat_date.isoformat() if isinstance(stat_date, date) else str(stat_date)
    return f"{REALTIME_KEY_PREFIX}{day}:{int(project_id or 0)}"


def _clean_fields(fields: Mapping[str, Any]) -> dict[str, int | float]:
    cleaned: dict[str, int | float] = {}
    for name, value in fields.items():
        if name not in _REALTIME_FIELD_SET:
            raise ValueError(f"stats:rt 不支持的字段：{name}")
        if value is None:
            continue
        if name in FLOAT_FIELDS:
            number = float(value)
            if number:
                cleaned[name] = number
        else:
            number_i = int(value)
            if number_i:
                cleaned[name] = number_i
    return cleaned


def _write_realtime(stat_date: date, project_id: int | None, fields: Mapping[str, int | float]) -> bool:
    if not fields:
        return False
    keys = [realtime_key(stat_date, project_id)]
    if int(project_id or 0) != 0:
        keys.append(realtime_key(stat_date, 0))
    try:
        pipe = redis_client.pipeline(transaction=False)
        for key in keys:
            for name, value in fields.items():
                if isinstance(value, float):
                    pipe.hincrbyfloat(key, name, value)
                else:
                    pipe.hincrby(key, name, int(value))
            pipe.expire(key, REALTIME_TTL_SECONDS)
        pipe.execute()
        return True
    except redis.RedisError as exc:
        logger.warning("stats:rt 计数写入失败 project_id=%s: %s", project_id, exc)
        return False


def increment_realtime(
    project_id: int | None,
    fields: Mapping[str, Any],
    *,
    at: datetime | None = None,
    db: Session | None = None,
    stat_date: date | None = None,
) -> bool:
    """按事件归属时间 ``at``（naive UTC，缺省当前时刻）累加今日实时计数；归属日期不是今日时不写（由重算体现）。

    ``fields`` 的键为 ``daily_stats`` 列名（不含 ``quota_actual`` / ``quota_reconciled_calls`` / ``*_snapshot``）。
    返回是否写入。Redis 不可用时记 warning 并返回 ``False``，不影响业务。
    """
    cleaned = _clean_fields(fields)
    if not cleaned:
        return False
    tz = get_tz(db)
    event_date = stat_date or local_date(at, tz=tz)
    if event_date != today_date(tz=tz):
        return False
    return _write_realtime(event_date, project_id, cleaned)


def increment_realtime_after_commit(
    db: Session, project_id: int | None, fields: Mapping[str, Any], *, at: datetime | None = None
) -> None:
    """在 ``db`` 下一次提交成功后写实时计数（归属日期与「今日」在登记时按当前配置计算，回调内不再访问数据库）。"""
    cleaned = _clean_fields(fields)
    if not cleaned:
        return
    tz = get_tz(db)
    event_date = local_date(at, tz=tz)
    if event_date != today_date(tz=tz):
        return
    after_commit(db, lambda: _write_realtime(event_date, project_id, cleaned))


def _parse_hash(raw: Mapping[str, str]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for name, value in raw.items():
        if name not in _REALTIME_FIELD_SET:
            continue
        try:
            result[name] = float(Decimal(value)) if name in FLOAT_FIELDS else int(Decimal(value))
        except (ArithmeticError, TypeError, ValueError):
            continue
    return result


def _sum_hashes(hashes: list[Mapping[str, str]]) -> dict[str, int | float]:
    total: dict[str, int | float] = {}
    for raw in hashes:
        for name, value in _parse_hash(raw or {}).items():
            total[name] = total.get(name, 0) + value
    if "cost_cny" in total:
        total["cost_cny"] = round(float(total["cost_cny"]), 6)
    return total


def realtime_today(scope: DataScope, project_id: int = 0, *, db: Session | None = None) -> dict[str, int | float] | None:
    """今日实时计数（缺失的字段不出现在结果中，调用方按 0 处理）；Redis 不可用返回 ``None``（调用方附
    ``meta.warnings[]=realtime_unavailable``、``today_source=none``）。

    - ``all`` 范围（未带 ``owner_id``）：``HGETALL stats:rt:{date}:{project_id}``；
    - ``owner`` 范围：``project_id>0``（调用方已校验属于 P）读单键；``project_id=0`` 时对 P 内每个项目
      ``HGETALL`` 后按字段求和，不读 ``:0`` 汇总键（docs/13 §10.2）。
    """
    owns_session = db is None
    session = db or SessionLocal()
    try:
        tz = get_tz(session)
        today = today_date(tz=tz)
        if not scope.restricted or int(project_id or 0) > 0:
            keys = [realtime_key(today, project_id)]
        else:
            ids = session.scalars(select(Project.id).where(Project.owner_id == scope.owner_id)).all()
            keys = [realtime_key(today, pid) for pid in ids]
    finally:
        if owns_session:
            session.close()
    if not keys:
        return {}
    try:
        pipe = redis_client.pipeline(transaction=False)
        for key in keys:
            pipe.hgetall(key)
        hashes = pipe.execute()
    except redis.RedisError as exc:
        logger.warning("stats:rt 读取失败: %s", exc)
        return None
    return _sum_hashes(hashes)
