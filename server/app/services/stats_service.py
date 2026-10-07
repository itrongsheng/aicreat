"""统计服务（docs/12-dashboard-reports.md；数据范围 docs/13 §10）。

- 时区（§4.7）：``get_tz()`` / ``today_date()`` / ``day_bounds(stat_date)`` / ``local_date(dt)``，按 ``stats_config.timezone``
  切日，``day_bounds`` 返回 naive UTC ``[start, end)``，聚合查询统一写成 ``col >= :start AND col < :end``；
- 实时计数（§4.6）：``increment_realtime`` / ``increment_realtime_after_commit`` 写 ``stats:rt:{date}:{project_id}``，
  ``realtime_today(scope, project_id)`` 读今日计数（``owner`` 范围对 P 内项目逐键求和）；
- 指标规格 ``METRIC_SPECS``（§3.2）与维度 × 列矩阵 ``DIMENSION_COLUMNS``（§4.2），参数校验（§9.8）；
- 预聚合 ``aggregate_daily(db, stat_date)``（§4.1~§4.3，被 ``tasks/aggregate_daily_stats.py`` 调用）与保留期清理
  ``cleanup_retention``；
- 查询 ``overview`` / ``trends`` / ``breakdown`` / ``rankings`` / ``export_csv``（均以 ``DataScope`` 为必填参数：范围键
  ``all`` 读 ``project_id=0`` 汇总行，``owner:{id}`` 对该用户项目行求和），缓存 ``cache:stats:*``（§10.3），
  标签解析 ``resolve_labels``；重算 ``recompute``（≤ 7 天同步、否则入 ``queue:stats_recompute``，§4.5）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import statistics
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis
from sqlalchemy import BigInteger, Select, and_, case, delete, func, or_, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.sql.functions import FunctionElement

from app.core.config import settings
from app.core.database import SessionLocal, after_commit
from app.core.exceptions import CODE_BAD_REQUEST, CODE_NOT_FOUND, BusinessError, field_error, invalid_params
from app.core.redis import cache_delete_prefix, cache_set_json, dumps, redis_client
from app.models import (
    DAILY_STATS_METRIC_COLUMNS,
    Admin,
    AiModel,
    AiTask,
    Alert,
    Content,
    DailyStat,
    IndexCheck,
    Keyword,
    LinkCheck,
    MediaAsset,
    Project,
    PublishLink,
    PublishPlatform,
    Title,
    utcnow,
)
from app.schemas.common import EXPORT_MAX_ROWS, iso_utc
from app.services import settings_service
from app.services.data_scope_service import NOT_FOUND_MESSAGE, DataScope, visible_project_ids

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


# =====================================================================
# 指标规格 METRIC_SPECS（§3.2；趋势 / 分解合法取值见 docs/04 §7.14）
# =====================================================================

STATS_DIMENSIONS: tuple[str, ...] = ("total", "platform", "capability", "model", "admin", "seo_engine", "geo_engine")
BREAKDOWN_DIMENSIONS: tuple[str, ...] = ("project", "owner", "platform", "admin", "model", "capability", "seo_engine", "geo_engine")
GRANULARITIES: tuple[str, ...] = ("day", "week", "month")
RANGES: dict[str, int] = {"today": 1, "7d": 7, "30d": 30}
RANKING_TYPES: tuple[str, ...] = ("fastest_indexed", "most_deleted_platforms", "top_cost_models", "top_cost_projects", "top_failed_models")
CAPABILITIES: tuple[str, ...] = ("keyword", "title", "content", "rewrite", "image", "video", "geo_check", "seo_check")

MAX_METRICS = 8
MAX_SPAN_DAYS = 731
RECOMPUTE_MAX_DAYS = 31
RECOMPUTE_SYNC_MAX_DAYS = 7
RECOMPUTE_QUEUE = "queue:stats_recompute"
STATS_CACHE_PREFIX = "cache:stats:"
QUERY_CACHE_TTL_SECONDS = 300
SERIES_MIN_DAYS = 7
OVERVIEW_TOP_ROWS = 8
P50_SAMPLE_LIMIT = 10000
RETENTION_BATCH = 1000
UPSERT_CHUNK = 200
CONTENT_COST_CAPABILITIES: tuple[str, ...] = ("content", "rewrite")
EXTRA_COLUMN = "quota_estimated_reconciled"          # daily_stats.extra_json 内的伪列
CONTENT_COST = "_content_cost"                       # cost_cny_per_content 的分子：capability ∈ content/rewrite 行的 cost_cny
TOTAL_LINKS = "_total_links_snapshot"                # *_by_engine 的分母：同日 total 行的 links_total_snapshot

METRIC_COLUMNS: tuple[str, ...] = DAILY_STATS_METRIC_COLUMNS
METRIC_COLUMN_SET = frozenset(METRIC_COLUMNS)
SNAPSHOT_COLUMNS = frozenset(c for c in METRIC_COLUMNS if c.endswith("_snapshot"))
FLOW_COLUMNS: tuple[str, ...] = tuple(c for c in METRIC_COLUMNS if c not in SNAPSHOT_COLUMNS)
FLOAT_COLUMNS = frozenset({"cost_cny", CONTENT_COST})

_AI_MEDIA_COLUMNS = frozenset({
    "ai_calls", "ai_succeeded", "ai_failed", "ai_duration_ms_sum", "prompt_tokens", "completion_tokens", "quota_estimated",
    "quota_actual", "quota_reconciled_calls", "cost_cny", "tasks_succeeded", "tasks_failed", "task_duration_ms_sum",
    "images_generated", "videos_generated", "media_failed", EXTRA_COLUMN,
})

# §4.2 维度 × 列矩阵：维度行只填写这些列（其余保持 0）；接口按此校验指标与维度是否匹配
DIMENSION_COLUMNS: dict[str, frozenset[str]] = {
    "total": METRIC_COLUMN_SET | {EXTRA_COLUMN},
    "platform": frozenset({
        "links_backfilled", "links_checked", "links_deleted", "links_changed", "links_restored", "links_alive_snapshot",
        "links_total_snapshot", "seo_checks", "seo_newly_indexed", "seo_indexed_snapshot", "geo_checks", "geo_newly_cited",
        "geo_cited_snapshot", "index_hours_sum", "index_hours_links",
    }),
    "capability": _AI_MEDIA_COLUMNS,
    "model": _AI_MEDIA_COLUMNS,
    "admin": frozenset({
        "keywords_created", "keywords_adopted", "titles_created", "titles_adopted", "contents_created", "contents_approved",
        "links_backfilled", "images_generated", "videos_generated", "media_failed", "ai_calls", "ai_succeeded", "ai_failed",
        "prompt_tokens", "completion_tokens", "quota_estimated", "quota_actual", "cost_cny", "tasks_succeeded", "tasks_failed",
        "task_duration_ms_sum", EXTRA_COLUMN,
    }),
    "seo_engine": frozenset({"seo_checks", "seo_newly_indexed", "seo_indexed_snapshot", "index_hours_sum", "index_hours_links"}),
    "geo_engine": frozenset({"geo_checks", "geo_newly_cited", "geo_cited_snapshot"}),
}
# extra_json 只写入这些维度的行（§4.1）
EXTRA_DIMENSIONS = frozenset({"total", "capability", "model", "admin"})


@dataclass(frozen=True)
class MetricSpec:
    """一个指标的口径（§3.2）。``kind``：

    - ``flow``：``daily_stats`` 流量列（含 ``extra_json`` 伪列 ``quota_estimated_reconciled``），range / 周期内求和；
    - ``derived``：由流量列求和后计算的非比率值（``tokens_total``、``quota_diff``）；
    - ``snapshot``：日终快照列，取末日（周期末日）值；
    - ``ratio``：分子分母先求和再相除（比率 4 位小数、耗时 / 金额按 ``decimals``），分母为 0 返回 ``null``；
    - ``current``：业务表当前值（仅总览）；``distribution``：分布（总览 ``breakdowns``）；``ranking``：榜单；
      ``detail``：只在详情页实时计算，不经 ``/admin/stats/*``。

    ``columns`` 为计算所依赖的 ``daily_stats`` 列（矩阵校验依据）；``attach`` 为趋势响应自动附带的分子分母列。
    """

    key: str
    label: str
    label_en: str
    category: str
    kind: str
    unit: str
    source: str
    formula: str
    dimensions: tuple[str, ...]
    columns: tuple[str, ...] = ()
    attach: tuple[str, ...] = ()
    decimals: int | None = None
    label_current: str | None = None

    @property
    def queryable(self) -> bool:
        """可作为 ``/admin/stats/trends`` 的 ``metrics`` 与 ``/admin/stats/breakdown`` 的 ``metric``。"""
        return self.kind in QUERYABLE_KINDS

    @property
    def is_ratio(self) -> bool:
        return self.kind == "ratio"


QUERYABLE_KINDS = frozenset({"flow", "derived", "snapshot", "ratio"})

_D = "day"


def _spec(key: str, label: str, label_en: str, category: str, kind: str, unit: str, source: str, formula: str,
          dimensions: tuple[str, ...], columns: tuple[str, ...] | None = None, **extra: Any) -> MetricSpec:
    if columns is None:
        columns = (key,) if kind in ("flow", "snapshot") else ()
    return MetricSpec(key, label, label_en, category, kind, unit, source, formula, dimensions, columns, **extra)


def _flow(key: str, label: str, label_en: str, category: str, dims: tuple[str, ...], unit: str = "count", **extra: Any) -> MetricSpec:
    return _spec(key, label, label_en, category, "flow", unit, "daily_stats", f"Σ daily_stats.{key}", dims, **extra)


def _ratio(key: str, label: str, label_en: str, category: str, num: tuple[str, ...], den: tuple[str, ...], formula: str,
           dims: tuple[str, ...], *, unit: str = "percent", decimals: int = 4, attach: tuple[str, ...] | None = None,
           columns: tuple[str, ...] | None = None) -> MetricSpec:
    cols = columns if columns is not None else tuple(dict.fromkeys(num + den))
    att = attach if attach is not None else tuple(c for c in cols if not c.startswith("_"))
    return _spec(key, label, label_en, category, "ratio", unit, "daily_stats", formula, dims, cols, attach=att, decimals=decimals)


def _current(key: str, label: str, label_en: str, category: str, source: str, formula: str, dims: tuple[str, ...],
             unit: str = "count", kind: str = "current", decimals: int | None = None) -> MetricSpec:
    return _spec(key, label, label_en, category, kind, unit, source, formula, dims, (), decimals=decimals)


_AI_DIMS = ("project", "capability", "model", "admin", _D)

_SPECS: tuple[MetricSpec, ...] = (
    # ---- 内容生产 ----
    _current("keywords_total", "关键词总数", "Keywords", "content", "keywords", "COUNT(keywords)（当前）", ("project",)),
    _current("keywords_by_status", "关键词状态分布", "Keywords by status", "content", "keywords",
             "COUNT(keywords) GROUP BY status", ("project",), kind="distribution"),
    _flow("keywords_created", "关键词新增", "Keywords created", "content", ("project", "admin", _D)),
    _flow("keywords_adopted", "关键词采用", "Keywords adopted", "content", ("project", "admin", _D)),
    _ratio("keyword_adopt_rate", "关键词采用率", "Keyword adoption rate", "content", ("keywords_adopted",), ("keywords_created",),
           "Σ keywords_adopted / Σ keywords_created", ("project", _D)),
    _current("titles_total", "标题总数", "Titles", "content", "titles", "COUNT(titles)（当前）", ("project",)),
    _current("titles_by_status", "标题状态分布", "Titles by status", "content", "titles", "COUNT(titles) GROUP BY status",
             ("project",), kind="distribution"),
    _flow("titles_created", "标题新增", "Titles created", "content", ("project", "admin", _D)),
    _flow("titles_adopted", "标题采用", "Titles adopted", "content", ("project", "admin", _D)),
    _current("contents_total", "内容总数", "Contents", "content", "contents", "COUNT(contents)（当前）", ("project",)),
    _current("contents_by_status", "内容状态分布", "Contents by status", "content", "contents", "COUNT(contents) GROUP BY status",
             ("project",), kind="distribution"),
    _flow("contents_created", "内容新增", "Contents created", "content", ("project", "admin", _D)),
    _flow("contents_approved", "审核通过", "Contents approved", "content", ("project", "admin", _D)),
    _flow("contents_published", "内容发布", "Contents published", "content", ("project", _D)),
    # ---- 链接与存活 ----
    _current("links_total", "链接总数", "Links", "links", "publish_links", "COUNT(publish_links)", ("project", "platform")),
    _flow("links_backfilled", "链接回填", "Links backfilled", "links", ("project", "platform", "admin", _D)),
    _current("links_alive", "当前存活链接数", "Alive links", "links", "publish_links",
             "COUNT(alive_status IN ('alive','changed'))", ("project", "platform")),
    _current("links_by_status", "链接状态分布", "Links by status", "links", "publish_links",
             "COUNT(publish_links) GROUP BY alive_status", ("project", "platform"), kind="distribution"),
    _current("link_alive_rate", "链接存活率", "Link alive rate", "links", "publish_links",
             "COUNT(alive_status IN ('alive','changed')) / COUNT(alive_status != 'pending')", ("project", "platform"),
             unit="percent", decimals=4),
    _current("link_deleted_rate", "链接删除率", "Link deleted rate", "links", "publish_links",
             "COUNT(alive_status = 'deleted') / COUNT(alive_status != 'pending')", ("project", "platform"),
             unit="percent", decimals=4),
    _flow("links_checked", "删除检测次数", "Link checks", "links", (_D, "platform")),
    _flow("links_deleted", "新增删除", "Links deleted", "links", (_D, "platform", "project"), label_current="当前已删除链接数"),
    _flow("links_changed", "新增变更", "Links changed", "links", (_D, "platform", "project")),
    _flow("links_restored", "恢复", "Links restored", "links", (_D, "platform", "project")),
    _spec("links_alive_snapshot", "日终存活链接数", "Alive links (EOD)", "links", "snapshot", "count", "daily_stats",
          "末日快照：日终存活链接数", (_D, "platform")),
    _spec("links_total_snapshot", "日终链接总数", "Links (EOD)", "links", "snapshot", "count", "daily_stats",
          "末日快照：日终链接总数", (_D, "platform")),
    # ---- 收录 ----
    _current("seo_index_rate", "SEO 收录率（检测口径）", "SEO index rate (checked)", "index", "publish_links",
             "COUNT(seo_indexed_any=1 ∩ D) / COUNT(D)，D = published_at <= now-1d AND index_checks_done >= 1",
             ("project", "platform"), unit="percent", decimals=4),
    _ratio("seo_index_rate_by_engine", "SEO 收录率（存量口径）", "SEO index rate (stock)", "index",
           ("seo_indexed_snapshot",), (TOTAL_LINKS,),
           "seo_engine 行 seo_indexed_snapshot / 同日 total 行 links_total_snapshot", ("seo_engine",),
           attach=("seo_indexed_snapshot", "links_total_snapshot")),
    _current("geo_cite_rate", "GEO 引用率（检测口径）", "GEO cite rate (checked)", "index", "publish_links",
             "COUNT(geo_cited_any=1 ∩ D) / COUNT(D)，D = index_checks_done >= 1", ("project", "platform"),
             unit="percent", decimals=4),
    _ratio("geo_cite_rate_by_engine", "GEO 引用率（存量口径）", "GEO cite rate (stock)", "index",
           ("geo_cited_snapshot",), (TOTAL_LINKS,),
           "geo_engine 行 geo_cited_snapshot / 同日 total 行 links_total_snapshot", ("geo_engine",),
           attach=("geo_cited_snapshot", "links_total_snapshot")),
    _ratio("time_to_index_hours_avg", "平均收录耗时（小时）", "Avg time to index (h)", "index", ("index_hours_sum",),
           ("index_hours_links",), "Σ index_hours_sum / Σ index_hours_links（当前值：AVG(TIMESTAMPDIFF(HOUR, published_at, "
           "first_indexed_at))，不含补录延迟 > 72 小时的历史补录）", ("project", "platform", "seo_engine", _D),
           unit="hours", decimals=1),
    _current("time_to_index_hours_p50", "收录耗时中位数（小时）", "Median time to index (h)", "index", "publish_links",
             "MEDIAN(TIMESTAMPDIFF(HOUR, published_at, first_indexed_at))，不含历史补录", ("project", "platform"),
             unit="hours", decimals=1),
    _flow("seo_newly_indexed", "SEO 新收录", "Newly indexed (SEO)", "index", (_D, "project", "platform", "seo_engine")),
    _flow("geo_newly_cited", "GEO 新引用", "Newly cited (GEO)", "index", (_D, "project", "platform", "geo_engine")),
    _flow("seo_checks", "SEO 检测次数", "SEO checks", "index", (_D, "platform", "seo_engine")),
    _flow("geo_checks", "GEO 检测次数", "GEO checks", "index", (_D, "platform", "geo_engine")),
    _spec("seo_indexed_snapshot", "日终已收录链接数", "Indexed links (EOD)", "index", "snapshot", "count", "daily_stats",
          "末日快照：日终已收录链接数", (_D, "platform", "seo_engine")),
    _spec("geo_cited_snapshot", "日终已引用链接数", "Cited links (EOD)", "index", "snapshot", "count", "daily_stats",
          "末日快照：日终已引用链接数", (_D, "platform", "geo_engine")),
    _flow("index_hours_sum", "收录耗时合计（小时）", "Time to index sum (h)", "index", (_D, "project", "platform", "seo_engine"),
          unit="hours"),
    _flow("index_hours_links", "收录耗时链接数", "Time to index links", "index", (_D, "project", "platform", "seo_engine")),
    # ---- AI 调用与消耗 ----
    _flow("ai_calls", "AI 调用", "AI calls", "ai", _AI_DIMS),
    _flow("ai_succeeded", "AI 调用成功", "AI calls succeeded", "ai", _AI_DIMS),
    _flow("ai_failed", "AI 调用失败", "AI calls failed", "ai", _AI_DIMS),
    _ratio("ai_success_rate", "尝试成功率", "Attempt success rate", "ai", ("ai_succeeded",), ("ai_calls",),
           "Σ ai_succeeded / Σ ai_calls", _AI_DIMS),
    _flow("ai_duration_ms_sum", "成功调用耗时合计（毫秒）", "Attempt duration sum (ms)", "ai", ("project", "capability", "model", _D),
          unit="duration"),
    _ratio("ai_avg_duration_ms", "平均耗时（毫秒）", "Avg attempt duration (ms)", "ai", ("ai_duration_ms_sum",), ("ai_succeeded",),
           "Σ ai_duration_ms_sum / Σ ai_succeeded", ("project", "capability", "model", _D), unit="duration", decimals=0),
    _flow("tasks_succeeded", "任务成功数", "Tasks succeeded", "ai", _AI_DIMS),
    _flow("tasks_failed", "任务失败数", "Tasks failed", "ai", _AI_DIMS),
    _flow("task_duration_ms_sum", "成功任务耗时合计（毫秒）", "Task duration sum (ms)", "ai", _AI_DIMS, unit="duration"),
    _ratio("task_success_rate", "任务成功率", "Task success rate", "ai", ("tasks_succeeded",), ("tasks_succeeded", "tasks_failed"),
           "Σ tasks_succeeded / Σ (tasks_succeeded + tasks_failed)", _AI_DIMS),
    _ratio("task_avg_duration_ms", "任务平均耗时（毫秒）", "Avg task duration (ms)", "ai", ("task_duration_ms_sum",),
           ("tasks_succeeded",), "Σ task_duration_ms_sum / Σ tasks_succeeded", _AI_DIMS, unit="duration", decimals=0),
    _current("ai_p95_duration_ms", "P95 耗时（毫秒）", "P95 duration (ms)", "ai", "ai_tasks",
             "PERCENTILE_95(ai_tasks.duration_ms)（尝试行，仅详情页）", ("capability", "model"), unit="duration", kind="detail"),
    _current("ai_failures_by_category", "失败分类", "Failures by category", "ai", "ai_tasks",
             "COUNT(尝试行) GROUP BY error_category（仅详情页）", ("capability", "model"), kind="detail"),
    _flow("prompt_tokens", "输入 tokens", "Prompt tokens", "ai", _AI_DIMS, unit="tokens"),
    _flow("completion_tokens", "输出 tokens", "Completion tokens", "ai", _AI_DIMS, unit="tokens"),
    _spec("tokens_total", "tokens 合计", "Total tokens", "ai", "derived", "tokens", "daily_stats",
          "prompt_tokens + completion_tokens", _AI_DIMS, ("prompt_tokens", "completion_tokens")),
    _flow("quota_estimated", "估算额度", "Estimated quota", "ai", _AI_DIMS, unit="quota"),
    _flow("quota_actual", "实扣额度", "Actual quota", "ai", _AI_DIMS, unit="quota"),
    _flow("quota_reconciled_calls", "已对账调用数", "Reconciled calls", "ai", ("project", "capability", "model", _D)),
    _ratio("quota_reconciled_rate", "对账率", "Reconciled rate", "ai", ("quota_reconciled_calls",), ("ai_calls",),
           "Σ quota_reconciled_calls / Σ ai_calls", ("project", "capability", "model", _D)),
    _flow("cost_cny", "费用（元）", "Cost (CNY)", "ai", _AI_DIMS, unit="currency", decimals=6),
    _ratio("cost_cny_per_content", "单篇内容成本（元）", "Cost per content (CNY)", "ai", (CONTENT_COST,), ("contents_created",),
           "cost_cny(capability IN ('content','rewrite')) / contents_created", ("project",), unit="currency", decimals=6,
           attach=("contents_created",)),
    _spec("quota_estimated_reconciled", "已对账估算额度", "Reconciled estimated quota", "ai", "flow", "quota", "daily_stats",
          "Σ daily_stats.extra_json.quota_estimated_reconciled", _AI_DIMS, (EXTRA_COLUMN,)),
    _spec("quota_diff", "对账差异", "Quota diff", "ai", "derived", "quota", "daily_stats",
          "Σ quota_actual − Σ quota_estimated_reconciled", _AI_DIMS, ("quota_actual", EXTRA_COLUMN)),
    _ratio("quota_diff_rate", "对账差异率", "Quota diff rate", "ai", ("quota_actual", EXTRA_COLUMN), (EXTRA_COLUMN,),
           "quota_diff / Σ quota_estimated_reconciled", _AI_DIMS),
    # ---- 媒体 ----
    _flow("images_generated", "图片生成", "Images generated", "media", ("project", "capability", "model", "admin", _D)),
    _flow("videos_generated", "视频生成", "Videos generated", "media", ("project", "capability", "model", "admin", _D)),
    _flow("media_failed", "媒体失败", "Media failed", "media", ("project", "capability", "model", "admin", _D)),
    _ratio("media_success_rate", "媒体成功率", "Media success rate", "media", ("images_generated", "videos_generated"),
           ("images_generated", "videos_generated", "media_failed"),
           "(images_generated + videos_generated) / (images_generated + videos_generated + media_failed)", ("project", _D)),
    # ---- 告警 ----
    _current("alerts_open", "未处理告警", "Open alerts", "alerts", "alerts",
             "COUNT(alerts WHERE status IN ('open','acknowledged')) GROUP BY severity", ("project",), kind="distribution"),
    _flow("alerts_opened", "新增告警", "Alerts opened", "alerts", (_D,)),
    _flow("alerts_resolved", "已解决告警", "Alerts resolved", "alerts", (_D,)),
    # ---- 榜单 ----
    _current("fastest_indexed", "最快收录", "Fastest indexed", "ranking", "publish_links",
             "first_indexed_at − published_at 升序（不含历史补录）", ("project",), kind="ranking"),
    _current("most_deleted_platforms", "删除最多的平台", "Most deleted platforms", "ranking", "daily_stats",
             "Σ links_deleted（platform 行）降序", ("platform",), kind="ranking"),
    _current("top_cost_models", "费用最高的模型", "Top cost models", "ranking", "daily_stats", "Σ cost_cny（model 行）降序",
             ("model",), kind="ranking"),
    _current("top_cost_projects", "费用最高的项目", "Top cost projects", "ranking", "daily_stats",
             "Σ cost_cny（各项目 total 行）降序", ("project",), kind="ranking"),
    _current("top_failed_models", "失败最多的模型", "Top failed models", "ranking", "daily_stats", "Σ ai_failed（model 行）降序",
             ("model",), kind="ranking"),
)

METRIC_SPECS: dict[str, MetricSpec] = {spec.key: spec for spec in _SPECS}
QUERYABLE_METRICS: tuple[str, ...] = tuple(spec.key for spec in _SPECS if spec.queryable)
_QUERYABLE_SET = frozenset(QUERYABLE_METRICS)

# 未在上表单独列出的 daily_stats 列同样可作 metric（§3.2「daily_stats 列名也可直接作为 metric」）
assert METRIC_COLUMN_SET <= _QUERYABLE_SET, sorted(METRIC_COLUMN_SET - _QUERYABLE_SET)


def metric_label(metric: str, locale: str = "zh-CN") -> str:
    spec = METRIC_SPECS.get(metric)
    if spec is None:
        return metric
    return spec.label_en if str(locale).lower().startswith("en") else spec.label


def metric_supported(metric: str, dimension: str) -> bool:
    """§4.2 矩阵：``metric`` 依赖的列都属于 ``dimension`` 填写的列（``project`` / ``owner`` 分解按 ``total`` 行）。

    特例：``cost_cny_per_content`` 的分子取 ``capability`` 行、分母取 ``total`` 行，只在 ``total`` 趋势与 ``project`` /
    ``owner`` 分解中可用；``*_by_engine`` 只在对应引擎维度可用。"""
    spec = METRIC_SPECS.get(metric)
    if spec is None or not spec.queryable:
        return False
    if metric == "cost_cny_per_content":
        return dimension in ("total", "project", "owner")
    if metric == "seo_index_rate_by_engine":
        return dimension == "seo_engine"
    if metric == "geo_cite_rate_by_engine":
        return dimension == "geo_engine"
    columns = DIMENSION_COLUMNS["total" if dimension in ("project", "owner") else dimension]
    return set(spec.columns) <= columns


def needed_columns(metrics: Iterable[str]) -> set[str]:
    """计算 ``metrics`` 需要读取的列（含伪列 ``quota_estimated_reconciled`` / ``_content_cost`` / ``_total_links_snapshot``）。"""
    needed: set[str] = set()
    for metric in metrics:
        needed.update(METRIC_SPECS[metric].columns)
    return needed


def _round(value: float | None, decimals: int | None) -> float | int | None:
    if value is None:
        return None
    if decimals is None:
        return value
    if decimals == 0:
        return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))


def _cost(value: Any) -> float:
    return _round(float(value or 0), 6)  # type: ignore[return-value]


def _ratio_value(num: float, den: float, decimals: int | None) -> float | int | None:
    if not den:
        return None
    return _round(num / den, decimals)


_RATIO_PARTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "keyword_adopt_rate": (("keywords_adopted",), ("keywords_created",)),
    "ai_success_rate": (("ai_succeeded",), ("ai_calls",)),
    "ai_avg_duration_ms": (("ai_duration_ms_sum",), ("ai_succeeded",)),
    "task_success_rate": (("tasks_succeeded",), ("tasks_succeeded", "tasks_failed")),
    "task_avg_duration_ms": (("task_duration_ms_sum",), ("tasks_succeeded",)),
    "quota_reconciled_rate": (("quota_reconciled_calls",), ("ai_calls",)),
    "media_success_rate": (("images_generated", "videos_generated"), ("images_generated", "videos_generated", "media_failed")),
    "time_to_index_hours_avg": (("index_hours_sum",), ("index_hours_links",)),
    "cost_cny_per_content": ((CONTENT_COST,), ("contents_created",)),
    "seo_index_rate_by_engine": (("seo_indexed_snapshot",), (TOTAL_LINKS,)),
    "geo_cite_rate_by_engine": (("geo_cited_snapshot",), (TOTAL_LINKS,)),
}


def compute_metric(metric: str, agg: Mapping[str, Any]) -> float | int | None:
    """由已汇总的列值（流量列求和、快照列取末日）计算一个指标的值；缺失的列按 0。"""
    spec = METRIC_SPECS[metric]

    def v(col: str) -> float:
        return float(agg.get(col) or 0)

    if metric == "tokens_total":
        return int(v("prompt_tokens") + v("completion_tokens"))
    if metric == "quota_diff":
        return int(v("quota_actual") - v(EXTRA_COLUMN))
    if metric == "quota_diff_rate":
        return _ratio_value(v("quota_actual") - v(EXTRA_COLUMN), v(EXTRA_COLUMN), 4)
    if spec.kind == "ratio":
        num_cols, den_cols = _RATIO_PARTS[metric]
        return _ratio_value(sum(v(c) for c in num_cols), sum(v(c) for c in den_cols), spec.decimals)
    if metric in FLOAT_COLUMNS:
        return _cost(agg.get(metric))
    return int(v(metric))


# =====================================================================
# 参数校验（§9.8）
# =====================================================================


def _bad(message: str, *errors: dict[str, Any]) -> BusinessError:
    return BusinessError(message, code=CODE_BAD_REQUEST, http_status=400, data=list(errors))


def parse_metrics(raw: str | Sequence[str] | None, *, param: str = "metrics") -> list[str]:
    """逗号分隔的指标列表 → 去重保序的列表；未知指标 ``unsupported_metric``，超过 8 个 ``value_error``（``loc=["query",param]``）。"""
    if raw is None:
        items: list[str] = []
    elif isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    else:
        items = [str(part).strip() for part in raw]
    items = list(dict.fromkeys(item for item in items if item))
    if not items:
        raise _bad("请至少选择 1 个指标", field_error(["query", param], "请至少选择 1 个指标", "value_error", raw))
    unknown = [item for item in items if item not in _QUERYABLE_SET]
    if unknown:
        raise _bad(f"不支持的指标：{', '.join(unknown)}", field_error(["query", param], "不支持的指标", "unsupported_metric", unknown))
    if len(items) > MAX_METRICS:
        raise _bad(f"指标最多 {MAX_METRICS} 个", field_error(["query", param], f"指标最多 {MAX_METRICS} 个", "value_error", items))
    return items


def check_dimension(metrics: Sequence[str], dimension: str) -> None:
    """指标与维度不匹配 → 400，每个不匹配的指标一项 ``unsupported_dimension``（``loc=["query","dimension"]``）。"""
    bad = [m for m in metrics if not metric_supported(m, dimension)]
    if bad:
        raise _bad(
            "该指标不支持此维度：" + "、".join(f"{m}（{dimension}）" for m in bad),
            *[field_error(["query", "dimension"], "该指标不支持此维度", "unsupported_dimension", {"metric": m, "dimension": dimension})
              for m in bad],
        )


def check_date_range(start: date, end: date) -> int:
    """``start <= end`` 且跨度（含首尾）≤ 731 天，否则 400 ``loc=["query","end"]``；返回天数。"""
    if start > end:
        raise _bad("结束日期不能早于开始日期", field_error(["query", "end"], "结束日期不能早于开始日期", "value_error", end.isoformat()))
    days = (end - start).days + 1
    if days > MAX_SPAN_DAYS:
        raise _bad(f"时间跨度不能超过 {MAX_SPAN_DAYS} 天",
                   field_error(["query", "end"], f"时间跨度不能超过 {MAX_SPAN_DAYS} 天", "value_error", end.isoformat()))
    return days


def _require_choice(value: Any, choices: Sequence[str], param: str) -> str:
    if value not in choices:
        expected = "、".join(choices)
        raise invalid_params(field_error(["query", param], f"取值必须是 {expected} 之一", "literal_error", value))
    return str(value)


def _project_param(db: Session, scope: DataScope, project_id: int | None) -> int:
    """``project_id``：``all`` 范围不校验（已删除项目仍可查其保留行）；按用户统计时须属于该用户，否则 404。"""
    pid = int(project_id or 0)
    if pid < 0:
        raise invalid_params(field_error(["query", "project_id"], "不能小于 0", "greater_than_equal", pid))
    if pid and scope.restricted:
        project = db.get(Project, pid) if pid <= 2**63 - 1 else None
        if project is None or project.owner_id != scope.owner_id:
            raise BusinessError(NOT_FOUND_MESSAGE, code=CODE_NOT_FOUND, http_status=404)
    return pid


# =====================================================================
# 方言无关的时间差（MySQL TIMESTAMPDIFF / SQLite strftime('%s')）
# =====================================================================


class seconds_between(FunctionElement):  # noqa: N801 - SQL 函数风格命名
    """``later − earlier`` 的整秒数：MySQL ``TIMESTAMPDIFF(SECOND, earlier, later)``，SQLite ``strftime('%s')`` 相减。"""

    type = BigInteger()
    name = "seconds_between"
    inherit_cache = True


class hours_between(FunctionElement):  # noqa: N801
    """``TIMESTAMPDIFF(HOUR, earlier, later)``（向零截断）；SQLite 为整秒差整除 3600。"""

    type = BigInteger()
    name = "hours_between"
    inherit_cache = True


def _args(element: FunctionElement, compiler: Any, **kw: Any) -> tuple[str, str]:
    later, earlier = list(element.clauses)
    return compiler.process(later, **kw), compiler.process(earlier, **kw)


def _sqlite_seconds(later: str, earlier: str) -> str:
    return f"(CAST(strftime('%s', {later}) AS INTEGER) - CAST(strftime('%s', {earlier}) AS INTEGER))"


@compiles(seconds_between)
def _seconds_default(element: FunctionElement, compiler: Any, **kw: Any) -> str:
    return _sqlite_seconds(*_args(element, compiler, **kw))


@compiles(seconds_between, "mysql")
def _seconds_mysql(element: FunctionElement, compiler: Any, **kw: Any) -> str:
    later, earlier = _args(element, compiler, **kw)
    return f"TIMESTAMPDIFF(SECOND, {earlier}, {later})"


@compiles(seconds_between, "postgresql")
def _seconds_pg(element: FunctionElement, compiler: Any, **kw: Any) -> str:
    later, earlier = _args(element, compiler, **kw)
    return f"CAST(EXTRACT(EPOCH FROM ({later} - {earlier})) AS BIGINT)"


@compiles(hours_between)
def _hours_default(element: FunctionElement, compiler: Any, **kw: Any) -> str:
    return f"({_sqlite_seconds(*_args(element, compiler, **kw))} / 3600)"


@compiles(hours_between, "mysql")
def _hours_mysql(element: FunctionElement, compiler: Any, **kw: Any) -> str:
    later, earlier = _args(element, compiler, **kw)
    return f"TIMESTAMPDIFF(HOUR, {earlier}, {later})"


@compiles(hours_between, "postgresql")
def _hours_pg(element: FunctionElement, compiler: Any, **kw: Any) -> str:
    later, earlier = _args(element, compiler, **kw)
    return f"CAST(TRUNC(EXTRACT(EPOCH FROM ({later} - {earlier})) / 3600) AS BIGINT)"


def backfill_within_limit(created_col: Any, published_col: Any) -> Any:
    """``created_at <= published_at + INTERVAL 72 HOUR``（补录延迟不超过 ``MAX_BACKFILL_DELAY_HOURS``，恰为 72 小时计入）。"""
    return seconds_between(created_col, published_col) <= MAX_BACKFILL_DELAY_HOURS * 3600


def _hours(later: datetime, earlier: datetime) -> int:
    """``TIMESTAMPDIFF(HOUR, earlier, later)`` 的 Python 等价（向零截断），负值按 0（与实时计数一致）。"""
    return max(0, int((later - earlier).total_seconds() // 3600))


def _within_backfill_limit(created_at: datetime | None, published_at: datetime | None) -> bool:
    if created_at is None or published_at is None:
        return False
    return created_at - published_at <= timedelta(hours=MAX_BACKFILL_DELAY_HOURS)


def _parse_utc(value: Any) -> datetime | None:
    """``seo_status_json`` 等 JSON 中的 ISO 8601 时间 → naive UTC。"""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _plain(value: Any) -> int | float:
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    return value


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# =====================================================================
# 预聚合 aggregate_daily（§4.1 ~ §4.3）
# =====================================================================

RowKey = tuple[int, str, str]


class _Rows:
    """``(project_id, dimension, dimension_key) -> {列: 值}``；``bump`` 同时累加项目行与 ``project_id=0`` 汇总行。"""

    def __init__(self) -> None:
        self.rows: dict[RowKey, dict[str, Any]] = {}

    def bump(self, project_id: int | None, dimension: str, key: Any, **cols: Any) -> None:
        dim_key = "" if dimension == "total" else str(key)[:120]
        for pid in {int(project_id or 0), 0}:
            row = self.rows.setdefault((pid, dimension, dim_key), {})
            for col, val in cols.items():
                if val is None:
                    continue
                row[col] = row.get(col, 0) + val

    def ensure(self, key: RowKey) -> None:
        self.rows.setdefault(key, {})


def _between(col: Any, start: datetime, end: datetime) -> Any:
    return and_(col >= start, col < end)


def _collect_content(db: Session, start: datetime, end: datetime, acc: _Rows) -> None:
    """keywords / titles / contents（含 admin 行）。"""
    for model, created_col, adopted_prefix in ((Keyword, "keywords_created", "keywords_adopted"), (Title, "titles_created", "titles_adopted")):
        stmt = (select(model.project_id, model.created_by, func.count()).where(_between(model.created_at, start, end))
                .group_by(model.project_id, model.created_by))
        for pid, by, n in db.execute(stmt):
            acc.bump(pid, "total", "", **{created_col: int(n)})
            if by is not None:
                acc.bump(pid, "admin", by, **{created_col: int(n)})
        stmt = (select(model.project_id, model.adopted_by, func.count()).where(_between(model.adopted_at, start, end))
                .group_by(model.project_id, model.adopted_by))
        for pid, by, n in db.execute(stmt):
            acc.bump(pid, "total", "", **{adopted_prefix: int(n)})
            if by is not None:
                acc.bump(pid, "admin", by, **{adopted_prefix: int(n)})
    stmt = (select(Content.project_id, Content.created_by, func.count()).where(_between(Content.created_at, start, end))
            .group_by(Content.project_id, Content.created_by))
    for pid, by, n in db.execute(stmt):
        acc.bump(pid, "total", "", contents_created=int(n))
        if by is not None:
            acc.bump(pid, "admin", by, contents_created=int(n))
    is_auto = case((Content.review_note == "auto", 1), else_=0)
    stmt = (select(Content.project_id, Content.reviewed_by, is_auto, func.count())
            .where(_between(Content.reviewed_at, start, end), Content.review_result == "approved")
            .group_by(Content.project_id, Content.reviewed_by, is_auto))
    for pid, by, auto, n in db.execute(stmt):
        acc.bump(pid, "total", "", contents_approved=int(n))       # total 行含自动通过
        if by is not None and not auto:                             # admin 行只计人工审核
            acc.bump(pid, "admin", by, contents_approved=int(n))
    stmt = (select(Content.project_id, func.count()).where(_between(Content.first_published_at, start, end))
            .group_by(Content.project_id))
    for pid, n in db.execute(stmt):
        acc.bump(pid, "total", "", contents_published=int(n))


def _platform_codes(db: Session) -> dict[int, str]:
    return {pid: code for pid, code in db.execute(select(PublishPlatform.id, PublishPlatform.code))}


def _platform_key(codes: Mapping[int, str], platform_id: int | None) -> str:
    return codes.get(int(platform_id or 0)) or f"#{platform_id}"


def _collect_links(db: Session, start: datetime, end: datetime, acc: _Rows) -> None:
    """publish_links / link_checks / index_checks 的流量列（含 platform / admin / 引擎行）。"""
    codes = _platform_codes(db)
    # 回填
    stmt = (select(PublishLink.project_id, PublishLink.platform_id, PublishLink.backfilled_by, func.count())
            .where(_between(PublishLink.created_at, start, end))
            .group_by(PublishLink.project_id, PublishLink.platform_id, PublishLink.backfilled_by))
    for pid, platform_id, by, n in db.execute(stmt):
        cols = {"links_backfilled": int(n)}
        acc.bump(pid, "total", "", **cols)
        acc.bump(pid, "platform", _platform_key(codes, platform_id), **cols)
        if by is not None:
            acc.bump(pid, "admin", by, **cols)
    # 删除检测
    deleted = func.sum(case((and_(LinkCheck.applied_status == "deleted", LinkCheck.previous_status != "deleted"), 1), else_=0))
    changed = func.sum(case((and_(LinkCheck.applied_status == "changed", LinkCheck.previous_status != "changed"), 1), else_=0))
    restored = func.sum(case((and_(LinkCheck.previous_status == "deleted", LinkCheck.applied_status.in_(("alive", "changed"))), 1),
                             else_=0))
    stmt = (select(PublishLink.project_id, PublishLink.platform_id, func.count(), deleted, changed, restored)
            .select_from(LinkCheck).join(PublishLink, PublishLink.id == LinkCheck.link_id)
            .where(_between(LinkCheck.checked_at, start, end))
            .group_by(PublishLink.project_id, PublishLink.platform_id))
    for pid, platform_id, n, d, c, r in db.execute(stmt):
        cols = {"links_checked": int(n), "links_deleted": int(d or 0), "links_changed": int(c or 0), "links_restored": int(r or 0)}
        acc.bump(pid, "total", "", **cols)
        acc.bump(pid, "platform", _platform_key(codes, platform_id), **cols)
    # 收录检测次数
    stmt = (select(PublishLink.project_id, PublishLink.platform_id, IndexCheck.kind, IndexCheck.engine, func.count())
            .select_from(IndexCheck).join(PublishLink, PublishLink.id == IndexCheck.link_id)
            .where(_between(IndexCheck.checked_at, start, end))
            .group_by(PublishLink.project_id, PublishLink.platform_id, IndexCheck.kind, IndexCheck.engine))
    for pid, platform_id, kind, engine, n in db.execute(stmt):
        col = "seo_checks" if kind == "seo" else "geo_checks"
        acc.bump(pid, "total", "", **{col: int(n)})
        acc.bump(pid, "platform", _platform_key(codes, platform_id), **{col: int(n)})
        acc.bump(pid, "seo_engine" if kind == "seo" else "geo_engine", engine, **{col: int(n)})
    # 新收录 / 新引用（引擎行在 Python 侧解析 JSON 列；候选集 first_*_at IS NOT NULL AND first_*_at < :end）
    stmt = (select(PublishLink.project_id, PublishLink.platform_id, PublishLink.created_at, PublishLink.published_at,
                   PublishLink.first_indexed_at, PublishLink.seo_status_json)
            .where(PublishLink.first_indexed_at.is_not(None), PublishLink.first_indexed_at < end))
    for pid, platform_id, created_at, published_at, first_at, raw in db.execute(stmt):
        eligible = _within_backfill_limit(created_at, published_at)
        if start <= first_at < end:
            cols: dict[str, int] = {"seo_newly_indexed": 1}
            if eligible:
                cols.update(index_hours_sum=_hours(first_at, published_at), index_hours_links=1)
            acc.bump(pid, "total", "", **cols)
            acc.bump(pid, "platform", _platform_key(codes, platform_id), **cols)
        states = _loads(raw)
        if isinstance(states, Mapping):
            for engine, state in states.items():
                engine_first = _parse_utc(state.get("first_indexed_at")) if isinstance(state, Mapping) else None
                if engine_first is None or not (start <= engine_first < end):
                    continue
                cols = {"seo_newly_indexed": 1}
                if eligible:
                    cols.update(index_hours_sum=_hours(engine_first, published_at), index_hours_links=1)
                acc.bump(pid, "seo_engine", engine, **cols)
    stmt = (select(PublishLink.project_id, PublishLink.platform_id, PublishLink.first_cited_at, PublishLink.geo_status_json)
            .where(PublishLink.first_cited_at.is_not(None), PublishLink.first_cited_at < end))
    for pid, platform_id, first_at, raw in db.execute(stmt):
        if start <= first_at < end:
            acc.bump(pid, "total", "", geo_newly_cited=1)
            acc.bump(pid, "platform", _platform_key(codes, platform_id), geo_newly_cited=1)
        states = _loads(raw)
        if isinstance(states, Mapping):
            for engine, state in states.items():
                engine_first = _parse_utc(state.get("first_cited_at")) if isinstance(state, Mapping) else None
                if engine_first is not None and start <= engine_first < end:
                    acc.bump(pid, "geo_engine", engine, geo_newly_cited=1)


def _collect_ai(db: Session, start: datetime, end: datetime, acc: _Rows) -> None:
    """``ai_tasks`` 尝试行（``ai_*`` / tokens / 额度 / 成本 / ``extra_json``）+ 根任务行（``tasks_*`` / ``task_duration_ms_sum``）。"""
    reconciled = case((AiTask.reconciled_at.is_not(None), 1), else_=0)
    stmt = (
        select(
            AiTask.project_id, AiTask.capability, AiTask.model, AiTask.created_by, AiTask.status, reconciled,
            func.count(), func.sum(func.coalesce(AiTask.prompt_tokens, 0)), func.sum(func.coalesce(AiTask.completion_tokens, 0)),
            func.sum(func.coalesce(AiTask.quota_estimated, 0)), func.sum(func.coalesce(AiTask.quota_actual, 0)),
            func.sum(func.coalesce(AiTask.cost_cny, 0)), func.sum(func.coalesce(AiTask.duration_ms, 0)),
        )
        .where(
            AiTask.root_task_id.is_not(None), AiTask.status.in_(("succeeded", "failed")), AiTask.trigger_type != "health_probe",
            _between(AiTask.finished_at, start, end), AiTask.created_at >= start - timedelta(days=1), AiTask.created_at < end,
        )
        .group_by(AiTask.project_id, AiTask.capability, AiTask.model, AiTask.created_by, AiTask.status, reconciled)
    )
    for pid, capability, model, by, status, rec, n, pt, ct, qe, qa, cost, dur in db.execute(stmt):
        n = int(n)
        succeeded = status == "succeeded"
        cols: dict[str, Any] = {
            "ai_calls": n, "ai_succeeded": n if succeeded else 0, "ai_failed": 0 if succeeded else n,
            "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0), "quota_estimated": int(qe or 0),
            "quota_actual": int(qa or 0) if rec else 0, "cost_cny": Decimal(str(cost or 0)),
            EXTRA_COLUMN: int(qe or 0) if rec else 0,
        }
        dim_cols = dict(cols, ai_duration_ms_sum=int(dur or 0) if succeeded else 0, quota_reconciled_calls=n if rec else 0)
        acc.bump(pid, "total", "", **dim_cols)
        acc.bump(pid, "capability", capability, **dim_cols)
        if model:
            acc.bump(pid, "model", model, **dim_cols)
        if by is not None:
            acc.bump(pid, "admin", by, **cols)       # admin 行不填 ai_duration_ms_sum / quota_reconciled_calls（§4.2）
    stmt = (
        select(AiTask.project_id, AiTask.capability, AiTask.model, AiTask.created_by, AiTask.status, func.count(),
               func.sum(func.coalesce(AiTask.duration_ms, 0)))
        .where(
            AiTask.root_task_id.is_(None), AiTask.status.in_(("succeeded", "failed", "expired")), AiTask.trigger_type != "health_probe",
            _between(AiTask.finished_at, start, end), AiTask.created_at < end,
        )
        .group_by(AiTask.project_id, AiTask.capability, AiTask.model, AiTask.created_by, AiTask.status)
    )
    for pid, capability, model, by, status, n, dur in db.execute(stmt):
        succeeded = status == "succeeded"
        cols = {"tasks_succeeded": int(n) if succeeded else 0, "tasks_failed": 0 if succeeded else int(n),
                "task_duration_ms_sum": int(dur or 0) if succeeded else 0}
        acc.bump(pid, "total", "", **cols)
        acc.bump(pid, "capability", capability, **cols)
        if model:
            acc.bump(pid, "model", model, **cols)
        if by is not None:
            acc.bump(pid, "admin", by, **cols)


def _collect_media(db: Session, start: datetime, end: datetime, acc: _Rows) -> None:
    """``media_assets``：按 ``ready_at`` / ``failed_at`` 归属，不看当前 status（含 capability / model / admin 行）。"""
    stmt = (select(MediaAsset.project_id, MediaAsset.kind, MediaAsset.model, MediaAsset.created_by, func.count())
            .where(MediaAsset.source == "generated", _between(MediaAsset.ready_at, start, end))
            .group_by(MediaAsset.project_id, MediaAsset.kind, MediaAsset.model, MediaAsset.created_by))
    for pid, kind, model, by, n in db.execute(stmt):
        cols = {"videos_generated" if kind == "video" else "images_generated": int(n)}
        _bump_media(acc, pid, kind, model, by, cols)
    stmt = (select(MediaAsset.project_id, MediaAsset.kind, MediaAsset.model, MediaAsset.created_by, func.count())
            .where(_between(MediaAsset.failed_at, start, end),
                   or_(MediaAsset.error_category.is_(None), MediaAsset.error_category != "cancelled"))
            .group_by(MediaAsset.project_id, MediaAsset.kind, MediaAsset.model, MediaAsset.created_by))
    for pid, kind, model, by, n in db.execute(stmt):
        _bump_media(acc, pid, kind, model, by, {"media_failed": int(n)})


def _bump_media(acc: _Rows, pid: int | None, kind: str, model: str | None, by: int | None, cols: dict[str, int]) -> None:
    acc.bump(pid, "total", "", **cols)
    acc.bump(pid, "capability", "video" if kind == "video" else "image", **cols)
    if model:
        acc.bump(pid, "model", model, **cols)
    if by is not None:
        acc.bump(pid, "admin", by, **cols)


def _collect_alerts(db: Session, start: datetime, end: datetime, acc: _Rows) -> None:
    """``alerts_opened`` / ``alerts_resolved``；``project_id`` 为 NULL 的告警只进 ``project_id=0`` 行。"""
    for col, time_col in (("alerts_opened", Alert.first_triggered_at), ("alerts_resolved", Alert.resolved_at)):
        stmt = select(Alert.project_id, func.count()).where(_between(time_col, start, end)).group_by(Alert.project_id)
        for pid, n in db.execute(stmt):
            acc.bump(pid, "total", "", **{col: int(n)})


def _collect_snapshots(db: Session, end: datetime, acc: _Rows) -> None:
    """§4.3：从检测历史回放到日终（``checked_at < :end``），不取业务表当前状态。"""
    codes = _platform_codes(db)
    last_check = (
        select(LinkCheck.link_id, LinkCheck.applied_status,
               func.row_number().over(partition_by=LinkCheck.link_id,
                                      order_by=(LinkCheck.checked_at.desc(), LinkCheck.id.desc())).label("rn"))
        .where(LinkCheck.checked_at < end)
        .subquery()
    )
    alive = func.sum(case((last_check.c.applied_status.in_(("alive", "changed")), 1), else_=0))
    stmt = (select(PublishLink.project_id, PublishLink.platform_id, func.count(PublishLink.id), alive)
            .select_from(PublishLink)
            .outerjoin(last_check, and_(last_check.c.link_id == PublishLink.id, last_check.c.rn == 1))
            .where(PublishLink.created_at < end)
            .group_by(PublishLink.project_id, PublishLink.platform_id))
    for pid, platform_id, total, alive_n in db.execute(stmt):
        cols = {"links_total_snapshot": int(total), "links_alive_snapshot": int(alive_n or 0)}
        acc.bump(pid, "total", "", **cols)
        acc.bump(pid, "platform", _platform_key(codes, platform_id), **cols)
    # 收录 / 引用：每链接每引擎最后一条非 unknown 的结果
    last_idx = (
        select(IndexCheck.link_id, IndexCheck.kind, IndexCheck.engine, IndexCheck.result_status,
               func.row_number().over(partition_by=(IndexCheck.link_id, IndexCheck.kind, IndexCheck.engine),
                                      order_by=(IndexCheck.checked_at.desc(), IndexCheck.id.desc())).label("rn"))
        .where(IndexCheck.checked_at < end, IndexCheck.result_status != "unknown")
        .subquery()
    )
    stmt = (select(last_idx.c.kind, last_idx.c.engine, last_idx.c.link_id, PublishLink.project_id, PublishLink.platform_id)
            .select_from(last_idx).join(PublishLink, PublishLink.id == last_idx.c.link_id)
            .where(last_idx.c.rn == 1, PublishLink.created_at < end,
                   or_(and_(last_idx.c.kind == "seo", last_idx.c.result_status == "indexed"),
                       and_(last_idx.c.kind == "geo", last_idx.c.result_status == "cited"))))
    engine_hits: Counter[tuple[int, str, str]] = Counter()
    any_hits: dict[str, dict[int, tuple[int, int]]] = {"seo": {}, "geo": {}}
    for kind, engine, link_id, pid, platform_id in db.execute(stmt):
        engine_hits[(int(pid or 0), kind, engine)] += 1
        any_hits.setdefault(kind, {})[int(link_id)] = (int(pid or 0), int(platform_id or 0))
    for (pid, kind, engine), n in engine_hits.items():
        if kind == "seo":
            acc.bump(pid, "seo_engine", engine, seo_indexed_snapshot=n)
        else:
            acc.bump(pid, "geo_engine", engine, geo_cited_snapshot=n)
    for kind, links in any_hits.items():
        col = "seo_indexed_snapshot" if kind == "seo" else "geo_cited_snapshot"
        per: Counter[tuple[int, int]] = Counter(links.values())      # 「任一引擎」：同一 (kind, link) 只计一次
        for (pid, platform_id), n in per.items():
            acc.bump(pid, "total", "", **{col: n})
            acc.bump(pid, "platform", _platform_key(codes, platform_id), **{col: n})


def _ensure_engine_rows(db: Session, acc: _Rows) -> None:
    """``project_id=0`` 下每个启用引擎必写一行（即使全零，§4.1 规则 2）。"""
    from app.services import index_check_service  # 延迟导入：index_check_service 依赖本模块

    for kind, dimension in (("seo", "seo_engine"), ("geo", "geo_engine")):
        for code in index_check_service.enabled_engines(kind, db):
            acc.ensure((0, dimension, str(code)[:120]))


def _row_values(dimension: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """完整列值：矩阵外的列保持 0（§4.1 规则 4）；``extra_json`` 只写 total / capability / model / admin 行。"""
    allowed = DIMENSION_COLUMNS[dimension]
    out: dict[str, Any] = {}
    for col in METRIC_COLUMNS:
        raw = values.get(col, 0) if col in allowed else 0
        if col == "cost_cny":
            out[col] = Decimal(str(raw or 0)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        else:
            out[col] = int(raw or 0)
    out["extra_json"] = (dumps({EXTRA_COLUMN: int(values.get(EXTRA_COLUMN, 0) or 0)})
                         if dimension in EXTRA_DIMENSIONS else None)
    return out


def _nonzero(values: Mapping[str, Any]) -> bool:
    return any(bool(v) for v in values.values())


def compute_daily_rows(db: Session, stat_date: date) -> dict[RowKey, dict[str, Any]]:
    """计算 ``stat_date`` 的全部 ``daily_stats`` 行（不写库）：total 行 + 维度行，按 §4.1 行生成规则过滤。"""
    tz = get_tz(db)
    start, end = day_bounds(stat_date, tz=tz)
    acc = _Rows()
    _collect_content(db, start, end, acc)
    _collect_links(db, start, end, acc)
    _collect_ai(db, start, end, acc)
    _collect_media(db, start, end, acc)
    _collect_alerts(db, start, end, acc)
    _collect_snapshots(db, end, acc)
    acc.ensure((0, "total", ""))
    _ensure_engine_rows(db, acc)
    existing = set(db.scalars(select(Project.id)).all())
    rows: dict[RowKey, dict[str, Any]] = {}
    for key, values in acc.rows.items():
        pid, dimension, _ = key
        if pid and pid not in existing:
            continue  # 已删除项目的行既不重算也不删除（§4.1 规则 3）
        if dimension == "total" or (pid == 0 and dimension in ("seo_engine", "geo_engine")):
            rows[key] = _row_values(dimension, values)
            continue
        full = _row_values(dimension, values)
        metrics_only = {c: full[c] for c in METRIC_COLUMNS}
        if _nonzero(metrics_only) or (dimension in EXTRA_DIMENSIONS and values.get(EXTRA_COLUMN)):
            rows[key] = full
    return rows


def _upsert_rows(db: Session, stat_date: date, rows: Mapping[RowKey, Mapping[str, Any]], computed_at: datetime) -> None:
    table = DailyStat.__table__
    payload = [
        {"stat_date": stat_date, "project_id": pid, "dimension": dimension, "dimension_key": key, **values,
         "computed_at": computed_at, "created_at": computed_at, "updated_at": computed_at}
        for (pid, dimension, key), values in rows.items()
    ]
    update_cols = [*METRIC_COLUMNS, "extra_json", "computed_at", "updated_at"]
    dialect = db.get_bind().dialect.name
    for i in range(0, len(payload), UPSERT_CHUNK):
        chunk = payload[i:i + UPSERT_CHUNK]
        if dialect == "mysql":
            stmt = mysql_insert(table).values(chunk)
            db.execute(stmt.on_duplicate_key_update({c: stmt.inserted[c] for c in update_cols}))
        elif dialect == "sqlite":
            stmt = sqlite_insert(table).values(chunk)
            db.execute(stmt.on_conflict_do_update(
                index_elements=["stat_date", "project_id", "dimension", "dimension_key"],
                set_={c: stmt.excluded[c] for c in update_cols},
            ))
        else:  # 其它方言：逐行查找后更新 / 插入
            for item in chunk:
                existing = db.scalar(select(DailyStat).where(
                    DailyStat.stat_date == stat_date, DailyStat.project_id == item["project_id"],
                    DailyStat.dimension == item["dimension"], DailyStat.dimension_key == item["dimension_key"]))
                if existing is None:
                    db.add(DailyStat(**{k: v for k, v in item.items() if k not in ("created_at", "updated_at")}))
                else:
                    for col in update_cols:
                        setattr(existing, col, item[col])
            db.flush()


def _delete_stale_rows(db: Session, stat_date: date, keep: set[RowKey]) -> int:
    """删除同一 ``stat_date`` 下不在本次结果中的旧行；只针对 ``project_id = 0`` 或仍存在于 ``projects`` 的行。"""
    stmt = select(DailyStat.id, DailyStat.project_id, DailyStat.dimension, DailyStat.dimension_key).where(
        DailyStat.stat_date == stat_date,
        or_(DailyStat.project_id == 0, DailyStat.project_id.in_(select(Project.id))),
    )
    stale = [row_id for row_id, pid, dimension, key in db.execute(stmt) if (int(pid), dimension, key) not in keep]
    for i in range(0, len(stale), RETENTION_BATCH):
        db.execute(delete(DailyStat).where(DailyStat.id.in_(stale[i:i + RETENTION_BATCH])))
    return len(stale)


def aggregate_daily(db: Session, stat_date: date) -> int:
    """计算 ``stat_date`` 的全部行并在单事务内「全量 upsert + 清理」（§4.1 规则 3），写 ``computed_at``；返回 upsert 行数。

    调用方（``tasks/aggregate_daily_stats.aggregate``）负责 ``lock:monitor:daily_stats:{date}`` 与提交后清缓存。"""
    stat_date = _as_date(stat_date)
    started = time.monotonic()
    try:
        rows = compute_daily_rows(db, stat_date)
        computed = time.monotonic() - started
        _upsert_rows(db, stat_date, rows, utcnow())
        _delete_stale_rows(db, stat_date, set(rows))
        db.commit()
    except Exception:
        db.rollback()
        raise
    elapsed = time.monotonic() - started
    if elapsed > 60:
        logger.warning("daily_stats 聚合耗时 %.1fs stat_date=%s（计算 %.1fs，写入 %.1fs）", elapsed, stat_date, computed,
                       elapsed - computed)
    return len(rows)


def cleanup_retention(db: Session, *, today: date | None = None) -> int:
    """删除 ``stat_date < today − retention_days`` 的行，每批 1000 行直到无行可删；返回删除行数。"""
    cfg = settings_service.get_config(db, "stats_config")
    try:
        retention = max(1, int(cfg.get("retention_days") or 730))
    except (TypeError, ValueError):
        retention = 730
    cutoff = (today or today_date(db)) - timedelta(days=retention)
    removed = 0
    while True:
        ids = db.scalars(select(DailyStat.id).where(DailyStat.stat_date < cutoff).order_by(DailyStat.id).limit(RETENTION_BATCH)).all()
        if not ids:
            break
        db.execute(delete(DailyStat).where(DailyStat.id.in_(ids)))
        db.commit()
        removed += len(ids)
    if removed:
        logger.info("daily_stats 保留期清理：删除 %s 行（stat_date < %s）", removed, cutoff)
    return removed


# =====================================================================
# 读取 daily_stats（范围键：all 读 project_id 指定行，owner 对 P 内项目行求和）
# =====================================================================

_GROUP_COLUMNS: dict[str, Any] = {"date": DailyStat.stat_date, "key": DailyStat.dimension_key, "project": DailyStat.project_id}


def _scope_rows(stmt: Select, scope: DataScope, project_id: int, *, project_rows: bool = False) -> Select:
    """``project_rows=False``：``all`` 范围取 ``project_id`` 指定行（0 = 汇总行），``owner`` 范围 ``project_id=0`` 时取 P 内项目行；
    ``project_rows=True``（``dimension=project`` / ``owner`` 分解、``top_cost_projects``）：全部项目行（``project_id > 0``），
    ``owner`` 范围限 P。"""
    projects = visible_project_ids(scope)
    if project_rows:
        stmt = stmt.where(DailyStat.project_id > 0)
        return stmt.where(DailyStat.project_id.in_(projects)) if projects is not None else stmt
    if project_id:
        return stmt.where(DailyStat.project_id == project_id)
    if projects is not None:
        return stmt.where(DailyStat.project_id.in_(projects))
    return stmt.where(DailyStat.project_id == 0)


def _extra_value(raw: str | None) -> int:
    data = _loads(raw)
    if isinstance(data, Mapping):
        try:
            return int(data.get(EXTRA_COLUMN) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def load_grouped(
    db: Session,
    scope: DataScope,
    *,
    project_id: int,
    start: date,
    end: date,
    dimension: str,
    group: Sequence[str],
    columns: Iterable[str],
    keys: Sequence[str] | None = None,
    project_rows: bool = False,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    """``daily_stats`` 按 ``group``（``date`` / ``key`` / ``project`` 的组合）对 ``columns`` 求和；只返回有行的分组。

    ``columns`` 可含伪列 ``quota_estimated_reconciled``（解析 ``extra_json`` 后求和）；其它伪列忽略。"""
    columns = set(columns)
    physical = [c for c in METRIC_COLUMNS if c in columns]
    group_cols = [_GROUP_COLUMNS[g] for g in group]
    stmt = select(*group_cols, func.count().label("_rows"),
                  *[func.coalesce(func.sum(getattr(DailyStat, c)), 0).label(c) for c in physical])
    stmt = stmt.where(DailyStat.stat_date >= start, DailyStat.stat_date <= end, DailyStat.dimension == dimension)
    if keys is not None:
        stmt = stmt.where(DailyStat.dimension_key.in_(list(keys)))
    stmt = _scope_rows(stmt, scope, project_id, project_rows=project_rows)
    if group_cols:
        stmt = stmt.group_by(*group_cols)
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    width = len(group_cols)
    for row in db.execute(stmt):
        if not row[width]:
            continue
        gkey = tuple(_as_date(v) if g == "date" else (int(v) if g == "project" else v) for g, v in zip(group, row[:width]))
        out[gkey] = {c: _plain(row[width + 1 + i]) for i, c in enumerate(physical)}
    if EXTRA_COLUMN in columns:
        extra = select(*group_cols, DailyStat.extra_json).where(
            DailyStat.stat_date >= start, DailyStat.stat_date <= end, DailyStat.dimension == dimension,
            DailyStat.extra_json.is_not(None))
        if keys is not None:
            extra = extra.where(DailyStat.dimension_key.in_(list(keys)))
        extra = _scope_rows(extra, scope, project_id, project_rows=project_rows)
        for values in out.values():
            values[EXTRA_COLUMN] = 0
        for row in db.execute(extra):
            gkey = tuple(_as_date(v) if g == "date" else (int(v) if g == "project" else v) for g, v in zip(group, row[:width]))
            if gkey in out:
                out[gkey][EXTRA_COLUMN] += _extra_value(row[width])
    return out


def _latest_row_date(db: Session, scope: DataScope, *, project_id: int, dimension: str, on_or_before: date,
                     after: date | None = None, keys: Sequence[str] | None = None, project_rows: bool = False) -> date | None:
    stmt = select(func.max(DailyStat.stat_date)).where(DailyStat.dimension == dimension, DailyStat.stat_date <= on_or_before)
    if after is not None:
        stmt = stmt.where(DailyStat.stat_date >= after)
    if keys is not None:
        stmt = stmt.where(DailyStat.dimension_key.in_(list(keys)))
    stmt = _scope_rows(stmt, scope, project_id, project_rows=project_rows)
    value = db.scalar(stmt)
    return _as_date(value) if value is not None else None


def _summary_row_date(db: Session, on_or_before: date) -> date | None:
    """``project_id=0`` 的 ``total`` 行中 ``stat_date <= on_or_before`` 的最近日期（快照取值日，各范围一致）。"""
    value = db.scalar(select(func.max(DailyStat.stat_date)).where(
        DailyStat.project_id == 0, DailyStat.dimension == "total", DailyStat.stat_date <= on_or_before))
    return _as_date(value) if value is not None else None


def _summary_row_exists(db: Session, stat_date: date) -> bool:
    return db.scalar(select(DailyStat.id).where(
        DailyStat.stat_date == stat_date, DailyStat.project_id == 0, DailyStat.dimension == "total").limit(1)) is not None


# =====================================================================
# 缓存（§10.3）
# =====================================================================


def _cache_read(key: str) -> tuple[Any | None, bool]:
    """``(value, redis_ok)``：区分「未命中」与「Redis 不可用」（后者在总览 ``meta.warnings[]`` 追加 ``cache_unavailable``）。"""
    try:
        raw = redis_client.get(key)
    except redis.RedisError as exc:
        logger.warning("读取统计缓存失败 key=%s: %s", key, exc)
        return None, False
    if raw is None:
        return None, True
    try:
        return json.loads(raw), True
    except (TypeError, ValueError):
        return None, True


def query_cache_key(kind: str, scope: DataScope, params: Mapping[str, Any]) -> str:
    """``cache:stats:{kind}:{sha1(query)}``：参数按键名排序、值去首尾空白、``metrics`` 按原顺序拼接；``scope_key`` /
    ``project_id`` / ``locale`` 参与哈希。"""
    items = dict(params)
    items["scope_key"] = scope.cache_key
    parts = []
    for name in sorted(items):
        value = items[name]
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v).strip() for v in value)
        elif isinstance(value, date):
            value = value.isoformat()
        elif value is None:
            value = ""
        parts.append(f"{name}={str(value).strip()}")
    digest = hashlib.sha1("&".join(parts).encode("utf-8")).hexdigest()
    return f"{STATS_CACHE_PREFIX}{kind}:{digest}"


def overview_cache_key(scope: DataScope, project_id: int, range_key: str) -> str:
    return f"{STATS_CACHE_PREFIX}overview:{scope.cache_key}:{int(project_id)}:{range_key}"


def clear_cache() -> int:
    """``cache_delete_prefix("cache:stats:")``（聚合完成、项目创建 / 删除 / 转移负责人后调用）。"""
    return cache_delete_prefix(STATS_CACHE_PREFIX)


def _cached_query(kind: str, scope: DataScope, params: Mapping[str, Any], compute: Callable[[], Any], *, use_cache: bool = True) -> Any:
    if not use_cache:
        return compute()
    key = query_cache_key(kind, scope, params)
    cached, _ok = _cache_read(key)
    if cached is not None:
        return cached
    value = compute()
    cache_set_json(key, value, QUERY_CACHE_TTL_SECONDS)
    return value


# =====================================================================
# 标签解析（§6.2）
# =====================================================================

CAPABILITY_LABELS: dict[str, tuple[str, str]] = {
    "keyword": ("关键词", "Keyword"), "title": ("标题", "Title"), "content": ("内容", "Content"), "rewrite": ("重写", "Rewrite"),
    "image": ("图片", "Image"), "video": ("视频", "Video"), "geo_check": ("GEO 检测", "GEO check"), "seo_check": ("SEO 检测", "SEO check"),
}
DELETED_PROJECTS_LABEL = ("已删除项目", "Deleted projects")
DISABLED_SUFFIX = ("（已禁用）", " (disabled)")


def _is_en(locale: str | None) -> bool:
    return str(locale or "").lower().startswith("en")


def _int_keys(keys: Iterable[str]) -> list[int]:
    out = []
    for key in keys:
        try:
            value = int(key)
        except (TypeError, ValueError):
            continue
        if 0 < value <= 2**63 - 1:
            out.append(value)
    return out


def resolve_labels(dimension: str, keys: Iterable[str], locale: str = "zh-CN", *, db: Session | None = None) -> dict[str, str]:
    """维度键 → 显示名（§6.2）；解析失败回退为 code（项目 / 人员回退 ``#<id>``）。"""
    keys = [str(k) for k in keys]
    if db is None:
        with SessionLocal() as session:
            return resolve_labels(dimension, keys, locale, db=session)
    en = _is_en(locale)
    labels: dict[str, str] = {}
    if dimension == "platform":
        rows = db.execute(select(PublishPlatform.code, PublishPlatform.name, PublishPlatform.name_en)
                          .where(PublishPlatform.code.in_(keys))).all() if keys else []
        found = {code: (name_en if en and name_en else name) for code, name, name_en in rows}
        labels = {k: found.get(k, k) for k in keys}
    elif dimension == "capability":
        labels = {k: CAPABILITY_LABELS.get(k, (k, k))[1 if en else 0] for k in keys}
    elif dimension == "model":
        vendors = dict(db.execute(select(AiModel.model_id, AiModel.vendor_name).where(AiModel.model_id.in_(keys))).all()) if keys else {}
        for k in keys:
            vendor = vendors.get(k)
            labels[k] = (f"{k} ({vendor})" if en else f"{k}（{vendor}）") if vendor else k
    elif dimension in ("admin", "owner"):
        ids = _int_keys(keys)
        admins = {a.id: a for a in db.scalars(select(Admin).where(Admin.id.in_(ids))).all()} if ids else {}
        for k in keys:
            if dimension == "owner" and k == "0":
                labels[k] = DELETED_PROJECTS_LABEL[1 if en else 0]
                continue
            admin = admins.get(int(k)) if k.isdigit() else None
            if admin is None:
                labels[k] = f"#{k}"
                continue
            label = admin.display_name or admin.username
            if dimension == "owner" and not admin.is_active:
                label += DISABLED_SUFFIX[1 if en else 0]
            labels[k] = label
    elif dimension == "project":
        ids = _int_keys(keys)
        names = dict(db.execute(select(Project.id, Project.name).where(Project.id.in_(ids))).all()) if ids else {}
        labels = {k: names.get(int(k), f"#{k}") if k.isdigit() else f"#{k}" for k in keys}
    elif dimension == "seo_engine":
        labels = {k: k.upper() for k in keys}
    elif dimension == "geo_engine":
        engines = settings_service.get_config(db, "geo_engines").get("engines") or []
        names = {str(e.get("code")): str(e.get("name") or e.get("code")) for e in engines if isinstance(e, Mapping) and e.get("code")}
        labels = {k: names.get(k, k) for k in keys}
    else:
        labels = {k: k for k in keys}
    return labels


# =====================================================================
# 趋势（§9.2）
# =====================================================================


def period_label(day: date, granularity: str) -> str:
    """``day`` → ``YYYY-MM-DD``；``week`` → ISO 周 ``YYYY-Www``（= MySQL ``%x-W%v``）；``month`` → ``YYYY-MM``。"""
    if granularity == "week":
        iso = day.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "month":
        return f"{day.year}-{day.month:02d}"
    return day.isoformat()


def periods(start: date, end: date, granularity: str) -> list[tuple[str, list[date]]]:
    """``[start, end]`` 内的周期（首尾周期只含范围内的日期，标签仍用完整周期）。"""
    out: list[tuple[str, list[date]]] = []
    day = start
    while day <= end:
        label = period_label(day, granularity)
        if out and out[-1][0] == label:
            out[-1][1].append(day)
        else:
            out.append((label, [day]))
        day += timedelta(days=1)
    return out


def _merge_into(target: dict[tuple[Any, ...], dict[str, Any]], extra: Mapping[tuple[Any, ...], Mapping[str, Any]],
                column: str, source: str) -> None:
    for gkey, values in extra.items():
        if gkey in target:
            target[gkey][column] = values.get(source, 0)


def _period_values(per_date: Mapping[date, Mapping[str, Any]], days: Sequence[date], needed: set[str],
                   carry: dict[str, Any]) -> dict[str, Any]:
    """周期内汇总：流量列求和；快照列（含 ``_total_links_snapshot``）取周期内最后一个有行日期的值，无行沿用 ``carry``。"""
    present = [d for d in days if d in per_date]
    agg: dict[str, Any] = {}
    snapshot_cols = {c for c in needed if c in SNAPSHOT_COLUMNS or c == TOTAL_LINKS}
    for col in needed - snapshot_cols:
        agg[col] = sum(per_date[d].get(col, 0) for d in present)
    if present:
        last = per_date[present[-1]]
        for col in snapshot_cols:
            carry[col] = last.get(col, 0)
    for col in snapshot_cols:
        agg[col] = carry.get(col, 0)
    return agg


def _trend_rows(db: Session, scope: DataScope, metrics: Sequence[str], granularity: str, start: date, end: date,
                project_id: int, dimension: str, dimension_key: str) -> list[dict[str, Any]]:
    needed = needed_columns(metrics)
    keys = [dimension_key if dimension != "total" else ""]
    common = {"project_id": project_id, "dimension": dimension, "keys": keys}
    rows = load_grouped(db, scope, start=start, end=end, group=("date",), columns=needed, **common)
    if CONTENT_COST in needed:
        cost = load_grouped(db, scope, project_id=project_id, start=start, end=end, dimension="capability", group=("date",),
                            columns={"cost_cny"}, keys=CONTENT_COST_CAPABILITIES)
        for values in rows.values():
            values[CONTENT_COST] = 0
        _merge_into(rows, cost, CONTENT_COST, "cost_cny")
    if TOTAL_LINKS in needed:
        totals = load_grouped(db, scope, project_id=project_id, start=start, end=end, dimension="total", group=("date",),
                              columns={"links_total_snapshot"})
        for values in rows.values():
            values[TOTAL_LINKS] = 0
        _merge_into(rows, totals, TOTAL_LINKS, "links_total_snapshot")
    per_date = {gkey[0]: values for gkey, values in rows.items()}
    carry: dict[str, Any] = {}
    if any(c in SNAPSHOT_COLUMNS or c == TOTAL_LINKS for c in needed):
        before = _latest_row_date(db, scope, project_id=project_id, dimension=dimension, on_or_before=start - timedelta(days=1),
                                  keys=keys)
        if before is not None:
            prior = load_grouped(db, scope, start=before, end=before, group=(), columns=needed, **common).get((), {})
            if TOTAL_LINKS in needed:
                totals = load_grouped(db, scope, project_id=project_id, start=before, end=before, dimension="total", group=(),
                                      columns={"links_total_snapshot"})
                prior[TOTAL_LINKS] = totals.get((), {}).get("links_total_snapshot", 0)
            carry = {c: prior.get(c, 0) for c in needed if c in SNAPSHOT_COLUMNS or c == TOTAL_LINKS}
    attach: list[str] = []
    for metric in metrics:
        for col in METRIC_SPECS[metric].attach:
            if col not in metrics and col not in attach:
                attach.append(col)
    out: list[dict[str, Any]] = []
    for label, days in periods(start, end, granularity):
        agg = _period_values(per_date, days, needed, carry)
        row: dict[str, Any] = {"date": label}
        for metric in metrics:
            row[metric] = compute_metric(metric, agg)
        for col in attach:
            # *_by_engine 的分母取同日 total 行的 links_total_snapshot（引擎行不填该列）
            source = TOTAL_LINKS if col == "links_total_snapshot" else col
            row[col] = compute_metric(col, {col: agg.get(source, 0)})
        out.append(row)
    return out


def trends(
    db: Session,
    scope: DataScope,
    *,
    metrics: str | Sequence[str],
    start: date,
    end: date,
    granularity: str = "day",
    project_id: int = 0,
    dimension: str = "total",
    dimension_key: str | None = "",
    locale: str = "zh-CN",
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """``GET /admin/stats/trends``：``[{date, <metric>…, <自动附带的分子分母列>…}]``；无行的周期不省略（流量 0、比率 ``null``、
    快照沿用上一周期）。按用户统计时维度行为该用户各项目同维度行之和。缓存 ``cache:stats:trends:{sha1}`` 300s。"""
    metric_list = parse_metrics(metrics, param="metrics")
    granularity = _require_choice(granularity or "day", GRANULARITIES, "granularity")
    dimension = _require_choice(dimension or "total", STATS_DIMENSIONS, "dimension")
    check_date_range(start, end)
    key = (dimension_key or "").strip()
    if dimension != "total" and not key:
        raise _bad("dimension 不为 total 时 dimension_key 必填",
                   field_error(["query", "dimension_key"], "dimension 不为 total 时 dimension_key 必填", "value_error", dimension_key))
    check_dimension(metric_list, dimension)
    pid = _project_param(db, scope, project_id)
    params = {"metrics": metric_list, "granularity": granularity, "start": start, "end": end, "project_id": pid,
              "dimension": dimension, "dimension_key": key if dimension != "total" else "", "locale": locale}
    return _cached_query("trends", scope, params, lambda: _trend_rows(
        db, scope, metric_list, granularity, start, end, pid, dimension, key if dimension != "total" else ""), use_cache=use_cache)


# =====================================================================
# 分解（§9.3）
# =====================================================================


def _sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    value = item.get("value")
    key = str(item.get("key"))
    numeric = (0, int(key), "") if key.isdigit() else (1, 0, key)
    return (value is None, -(value or 0), numeric)


def _owner_map(db: Session, project_ids: Iterable[int]) -> dict[int, int]:
    ids = list(set(project_ids))
    if not ids:
        return {}
    return {pid: owner for pid, owner in db.execute(select(Project.id, Project.owner_id).where(Project.id.in_(ids)))}


def _breakdown_values(db: Session, scope: DataScope, metrics: Sequence[str], dimension: str, start: date, end: date,
                      project_id: int) -> dict[str, dict[str, Any]]:
    """各键在范围内的汇总值：流量列求和，快照列取 ``end`` 当日（无行时取范围内最近一个有行日期）的值。"""
    needed = needed_columns(metrics)
    snapshot_cols = {c for c in needed if c in SNAPSHOT_COLUMNS}
    flow_cols = needed - snapshot_cols - {TOTAL_LINKS, CONTENT_COST}
    by_project = dimension in ("project", "owner")
    stored_dimension = "total" if by_project else dimension
    group = ("project",) if by_project else ("key",)
    common: dict[str, Any] = {"project_id": project_id, "project_rows": by_project}
    rows = load_grouped(db, scope, start=start, end=end, dimension=stored_dimension, group=group, columns=flow_cols, **common)
    snapshot_day = None
    if snapshot_cols or TOTAL_LINKS in needed:
        snapshot_day = _latest_row_date(db, scope, dimension=stored_dimension, on_or_before=end, after=start, **common)
    if snapshot_day is not None and snapshot_cols:
        snaps = load_grouped(db, scope, start=snapshot_day, end=snapshot_day, dimension=stored_dimension, group=group,
                             columns=snapshot_cols, **common)
        for gkey, values in snaps.items():
            rows.setdefault(gkey, {}).update(values)
    if CONTENT_COST in needed:
        cost = load_grouped(db, scope, start=start, end=end, dimension="capability", group=group, columns={"cost_cny"},
                            keys=CONTENT_COST_CAPABILITIES, **common)
        for gkey, values in rows.items():
            values[CONTENT_COST] = cost.get(gkey, {}).get("cost_cny", 0)
    if TOTAL_LINKS in needed:
        total_links = 0
        if snapshot_day is not None:
            totals = load_grouped(db, scope, project_id=project_id, start=snapshot_day, end=snapshot_day, dimension="total",
                                  group=(), columns={"links_total_snapshot"})
            total_links = totals.get((), {}).get("links_total_snapshot", 0)
        for values in rows.values():
            values[TOTAL_LINKS] = total_links
    out: dict[str, dict[str, Any]] = {}
    if dimension == "owner":
        owners = _owner_map(db, (gkey[0] for gkey in rows))
        for (pid,), values in rows.items():
            key = str(owners.get(pid, 0))       # 已删除项目归入键 0
            target = out.setdefault(key, {})
            for col, val in values.items():
                if col == TOTAL_LINKS:
                    target[col] = val
                else:
                    target[col] = target.get(col, 0) + val
        return out
    return {str(gkey[0]): values for gkey, values in rows.items()}


def _breakdown_rows(db: Session, scope: DataScope, metrics: Sequence[str], dimension: str, start: date, end: date,
                    project_id: int, locale: str) -> list[dict[str, Any]]:
    values_by_key = _breakdown_values(db, scope, metrics, dimension, start, end, project_id)
    primary = metrics[0]
    primary_ratio = METRIC_SPECS[primary].is_ratio
    items: list[dict[str, Any]] = []
    for key, agg in values_by_key.items():
        values = {m: compute_metric(m, agg) for m in metrics}
        items.append({"key": key, "value": values[primary], "values": values})
    total = sum((item["value"] or 0) for item in items) if not primary_ratio else 0
    labels = resolve_labels(dimension, [item["key"] for item in items], locale, db=db)
    out: list[dict[str, Any]] = []
    for item in sorted(items, key=_sort_key):
        share = None if primary_ratio or not total else _round((item["value"] or 0) / total, 4)
        out.append({"key": item["key"], "label": labels.get(item["key"], item["key"]), "value": item["value"], "share": share,
                    "values": item["values"]})
    return out


def breakdown(
    db: Session,
    scope: DataScope,
    *,
    dimension: str,
    metric: str | Sequence[str],
    start: date,
    end: date,
    project_id: int = 0,
    locale: str = "zh-CN",
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """``GET /admin/stats/breakdown``：``[{key, label, value, share, values}]``，按 ``value`` 降序、``key`` 升序；返回范围内存在
    聚合行的全部键（含 ``value=0``）。``project`` / ``owner`` 取各项目 ``total`` 行（忽略 ``project_id``）。"""
    metric_list = parse_metrics(metric, param="metric")
    dimension = _require_choice(dimension, BREAKDOWN_DIMENSIONS, "dimension")
    check_date_range(start, end)
    check_dimension(metric_list, dimension)
    pid = 0 if dimension in ("project", "owner") else _project_param(db, scope, project_id)
    params = {"metric": metric_list, "dimension": dimension, "start": start, "end": end, "project_id": pid, "locale": locale}
    return _cached_query("breakdown", scope, params, lambda: _breakdown_rows(
        db, scope, metric_list, dimension, start, end, pid, locale), use_cache=use_cache)


# =====================================================================
# 榜单（§9.4）
# =====================================================================


def _rankings_limit(db: Session, limit: int | None) -> int:
    if limit is None:
        try:
            limit = int(settings_service.get_config(db, "stats_config").get("rankings_limit") or 10)
        except (TypeError, ValueError):
            limit = 10
    if not 1 <= int(limit) <= 100:
        raise invalid_params(field_error(["query", "limit"], "取值范围 1~100", "value_error", limit))
    return int(limit)


def _fastest_indexed(db: Session, scope: DataScope, start: date, end: date, project_id: int, limit: int,
                     locale: str) -> list[dict[str, Any]]:
    tz = get_tz(db)
    lower, upper = range_bounds(start, end, tz=tz)
    seconds = seconds_between(PublishLink.first_indexed_at, PublishLink.published_at)
    stmt = (
        select(PublishLink, Content.title, PublishPlatform.code, PublishPlatform.name, PublishPlatform.name_en, Project.name,
               hours_between(PublishLink.first_indexed_at, PublishLink.published_at))
        .outerjoin(Content, Content.id == PublishLink.content_id)
        .outerjoin(PublishPlatform, PublishPlatform.id == PublishLink.platform_id)
        .outerjoin(Project, Project.id == PublishLink.project_id)
        .where(PublishLink.first_indexed_at >= lower, PublishLink.first_indexed_at < upper,
               backfill_within_limit(PublishLink.created_at, PublishLink.published_at))
        .order_by(seconds.asc(), PublishLink.id.asc())
        .limit(limit)
    )
    projects = visible_project_ids(scope)
    if project_id:
        stmt = stmt.where(PublishLink.project_id == project_id)
    elif projects is not None:
        stmt = stmt.where(PublishLink.project_id.in_(projects))
    en = _is_en(locale)
    out = []
    for rank, (link, title, code, name, name_en, project_name, hours) in enumerate(db.execute(stmt).all(), start=1):
        out.append({
            "rank": rank, "content_id": link.content_id, "title": title or link.title_snapshot, "link_id": link.id, "url": link.url,
            "platform_code": code or f"#{link.platform_id}", "platform_name": (name_en if en and name_en else name) or code or "",
            "project_id": link.project_id, "project_name": project_name or f"#{link.project_id}",
            "published_at": iso_utc(link.published_at), "first_indexed_at": iso_utc(link.first_indexed_at),
            "hours": max(0, int(hours or 0)),
        })
    return out


def _current_links_by_platform(db: Session, scope: DataScope, project_id: int) -> dict[str, int]:
    stmt = (select(PublishPlatform.code, func.count(PublishLink.id)).select_from(PublishLink)
            .join(PublishPlatform, PublishPlatform.id == PublishLink.platform_id).group_by(PublishPlatform.code))
    stmt = _scope_current(stmt, PublishLink.project_id, scope, project_id)
    return {code: int(n) for code, n in db.execute(stmt)}


def _ranking_rows(db: Session, scope: DataScope, ranking_type: str, start: date, end: date, project_id: int, limit: int,
                  locale: str) -> list[dict[str, Any]]:
    if ranking_type == "fastest_indexed":
        return _fastest_indexed(db, scope, start, end, project_id, limit, locale)
    if ranking_type == "most_deleted_platforms":
        dimension, metric, extras = "platform", "links_deleted", ()
    elif ranking_type == "top_cost_models":
        dimension, metric, extras = "model", "cost_cny", ("ai_calls", "ai_success_rate")
    elif ranking_type == "top_failed_models":
        dimension, metric, extras = "model", "ai_failed", ("ai_calls", "ai_success_rate")
    else:  # top_cost_projects
        dimension, metric, extras = "project", "cost_cny", ("contents_created", "cost_cny_per_content")
    values_by_key = _breakdown_values(db, scope, [metric, *extras], dimension, start, end,
                                      0 if dimension == "project" else project_id)
    if dimension == "project":
        values_by_key.pop("0", None)
    items = [{"key": key, "value": compute_metric(metric, agg), "extra": {m: compute_metric(m, agg) for m in extras}}
             for key, agg in values_by_key.items()]
    if ranking_type == "most_deleted_platforms":
        current = _current_links_by_platform(db, scope, project_id)
        for item in items:
            item["extra"] = {"links_total": current.get(item["key"], 0)}
    items.sort(key=_sort_key)
    items = items[:limit]
    labels = resolve_labels(dimension, [item["key"] for item in items], locale, db=db)
    return [{"rank": rank, "key": item["key"], "label": labels.get(item["key"], item["key"]), "value": item["value"],
             "extra": item["extra"]} for rank, item in enumerate(items, start=1)]


def rankings(
    db: Session,
    scope: DataScope,
    *,
    ranking_type: str,
    start: date,
    end: date,
    project_id: int = 0,
    limit: int | None = None,
    locale: str = "zh-CN",
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    """``GET /admin/stats/rankings``：``fastest_indexed`` 为链接明细行，其余四类 ``{rank, key, label, value, extra}``。"""
    ranking_type = _require_choice(ranking_type, RANKING_TYPES, "type")
    check_date_range(start, end)
    limit_value = _rankings_limit(db, limit)
    pid = 0 if ranking_type == "top_cost_projects" else _project_param(db, scope, project_id)
    params = {"type": ranking_type, "start": start, "end": end, "project_id": pid, "limit": limit_value, "locale": locale}
    return _cached_query("rankings", scope, params, lambda: _ranking_rows(
        db, scope, ranking_type, start, end, pid, limit_value, locale), use_cache=use_cache)


# =====================================================================
# 总览（§5、§9.1）
# =====================================================================

KPI_KEYS: tuple[str, ...] = (
    "keywords_total", "keywords_created", "keywords_adopted", "keyword_adopt_rate",
    "titles_total", "titles_created", "titles_adopted",
    "contents_total", "contents_created", "contents_approved", "contents_published",
    "links_total", "links_backfilled", "links_alive", "links_deleted", "link_alive_rate", "link_deleted_rate", "links_checked",
    "seo_index_rate", "geo_cite_rate", "seo_newly_indexed", "geo_newly_cited", "time_to_index_hours_avg", "time_to_index_hours_p50",
    "ai_calls", "ai_success_rate", "ai_avg_duration_ms", "task_success_rate",
    "prompt_tokens", "completion_tokens", "tokens_total", "quota_estimated", "quota_actual", "quota_reconciled_rate",
    "cost_cny", "cost_cny_per_content",
    "images_generated", "videos_generated", "media_failed", "media_success_rate", "alerts_opened", "alerts_resolved",
)
# 当前值指标（不受 range 影响、无环比）
CURRENT_KPIS = frozenset({
    "keywords_total", "titles_total", "contents_total", "links_total", "links_alive", "links_deleted", "link_alive_rate",
    "link_deleted_rate", "seo_index_rate", "geo_cite_rate", "time_to_index_hours_avg", "time_to_index_hours_p50",
})
FLOW_KPIS: tuple[str, ...] = tuple(k for k in KPI_KEYS if k not in CURRENT_KPIS)
SERIES_KEYS: tuple[str, ...] = ("ai_calls", "cost_cny", "links_backfilled", "seo_newly_indexed")
STATUS_VALUES: dict[str, tuple[str, ...]] = {
    "keywords": ("candidate", "adopted", "discarded"),
    "titles": ("candidate", "adopted", "discarded"),
    "contents": ("draft", "generating", "ready", "reviewing", "approved", "rejected", "published", "archived"),
    "links": ("pending", "alive", "changed", "suspected_deleted", "deleted", "unknown"),
    "alerts": ("info", "warning", "critical"),
}
CAPABILITY_COST_COLUMNS = ("ai_calls", "ai_succeeded", "prompt_tokens", "completion_tokens", "quota_estimated", "quota_actual",
                           "cost_cny", "tasks_succeeded", "tasks_failed", "task_duration_ms_sum")


def _scope_current(stmt: Select, column: Any, scope: DataScope, project_id: int) -> Select:
    """当前值指标的项目条件：``project_id > 0`` 取该项目；否则 ``owner`` 范围附加 ``project_id IN P``。"""
    if project_id:
        return stmt.where(column == project_id)
    projects = visible_project_ids(scope)
    return stmt.where(column.in_(projects)) if projects is not None else stmt


def _status_counts(db: Session, model: Any, status_col: Any, scope: DataScope, project_id: int, values: Sequence[str],
                   *conditions: Any) -> dict[str, int]:
    stmt = select(status_col, func.count()).select_from(model).where(*conditions).group_by(status_col)
    stmt = _scope_current(stmt, model.project_id, scope, project_id)
    counts = {str(status): int(n) for status, n in db.execute(stmt)}
    result = {value: counts.pop(value, 0) for value in values}
    result.update(counts)
    return result


def _current_metrics(db: Session, scope: DataScope, project_id: int) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """当前值 KPI 与状态分布（只受 ``project_id`` 与数据范围影响）。"""
    warnings: list[str] = []
    keywords = _status_counts(db, Keyword, Keyword.status, scope, project_id, STATUS_VALUES["keywords"])
    titles = _status_counts(db, Title, Title.status, scope, project_id, STATUS_VALUES["titles"])
    contents = _status_counts(db, Content, Content.status, scope, project_id, STATUS_VALUES["contents"])
    links = _status_counts(db, PublishLink, PublishLink.alive_status, scope, project_id, STATUS_VALUES["links"])
    alerts = _status_counts(db, Alert, Alert.severity, scope, project_id, STATUS_VALUES["alerts"],
                            Alert.status.in_(("open", "acknowledged")))
    links_total = sum(links.values())
    checked = links_total - links.get("pending", 0)
    alive = links.get("alive", 0) + links.get("changed", 0)
    now = utcnow()

    def _rate(num_cond: Any, den_cond: Any) -> float | None:
        stmt = select(func.count(), func.coalesce(func.sum(case((num_cond, 1), else_=0)), 0)).select_from(PublishLink).where(den_cond)
        stmt = _scope_current(stmt, PublishLink.project_id, scope, project_id)
        den, num = db.execute(stmt).one()
        return _ratio_value(float(num or 0), float(den or 0), 4)

    seo_rate = _rate(PublishLink.seo_indexed_any == True,  # noqa: E712
                     and_(PublishLink.published_at <= now - timedelta(days=1), PublishLink.index_checks_done >= 1))
    geo_rate = _rate(PublishLink.geo_cited_any == True, PublishLink.index_checks_done >= 1)  # noqa: E712
    hours = hours_between(PublishLink.first_indexed_at, PublishLink.published_at)
    eligible = and_(PublishLink.first_indexed_at.is_not(None), backfill_within_limit(PublishLink.created_at, PublishLink.published_at))
    avg_stmt = _scope_current(select(func.avg(hours)).select_from(PublishLink).where(eligible), PublishLink.project_id, scope, project_id)
    avg = db.scalar(avg_stmt)
    sample_stmt = _scope_current(
        select(hours).select_from(PublishLink).where(eligible)
        .order_by(PublishLink.first_indexed_at.desc(), PublishLink.id.desc()).limit(P50_SAMPLE_LIMIT + 1),
        PublishLink.project_id, scope, project_id)
    sample = [int(v) for v in db.scalars(sample_stmt).all() if v is not None]
    if len(sample) > P50_SAMPLE_LIMIT:
        sample = sample[:P50_SAMPLE_LIMIT]
        warnings.append("p50_sampled")
    kpis = {
        "keywords_total": sum(keywords.values()),
        "titles_total": sum(titles.values()),
        "contents_total": sum(contents.values()),
        "links_total": links_total,
        "links_alive": alive,
        "links_deleted": links.get("deleted", 0),
        "link_alive_rate": _ratio_value(alive, checked, 4),
        "link_deleted_rate": _ratio_value(links.get("deleted", 0), checked, 4),
        "seo_index_rate": seo_rate,
        "geo_cite_rate": geo_rate,
        "time_to_index_hours_avg": _round(float(avg), 1) if avg is not None else None,
        "time_to_index_hours_p50": _round(float(statistics.median(sample)), 1) if sample else None,
    }
    breakdowns = {"keywords_by_status": keywords, "titles_by_status": titles, "contents_by_status": contents,
                  "links_by_status": links, "alerts_open": alerts}
    return kpis, breakdowns, warnings


def _flow_kpis(agg: Mapping[str, Any]) -> dict[str, Any]:
    return {key: compute_metric(key, agg) for key in FLOW_KPIS}


def _sum_days(per_date: Mapping[date, Mapping[str, Any]], start: date, end: date, columns: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {c: 0 for c in columns}
    for day, values in per_date.items():
        if start <= day <= end:
            for col in out:
                out[col] += values.get(col, 0)
    return out


def _compare(current: Mapping[str, Any], previous: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in FLOW_KPIS:
        spec = METRIC_SPECS[key]
        cur, prev = current.get(key), previous.get(key)
        if cur is None or prev is None:
            delta = None
        else:
            decimals = 6 if spec.unit == "currency" else (spec.decimals if spec.is_ratio else None)
            delta = _round(cur - prev, decimals) if decimals is not None else cur - prev
        rate = _ratio_value(float(delta), float(prev), 4) if delta is not None and prev else None
        out[key] = {"previous": prev, "delta": delta, "delta_rate": rate}
    return out


def _cost_table(db: Session, scope: DataScope, project_id: int, start: date, end: date, dimension: str) -> list[dict[str, Any]]:
    """``cost_by_capability`` / ``cost_by_model``：range 内求和，按 ``cost_cny`` 降序最多 8 行，``share`` 为费用占比。"""
    columns = set(CAPABILITY_COST_COLUMNS)
    rows = load_grouped(db, scope, project_id=project_id, start=start, end=end, dimension=dimension, group=("key",), columns=columns)
    total_cost = sum(float(values.get("cost_cny", 0)) for values in rows.values())
    name = "capability" if dimension == "capability" else "model"
    items = []
    for (key,), agg in rows.items():
        item: dict[str, Any] = {
            name: key, "ai_calls": int(agg.get("ai_calls", 0)), "ai_success_rate": compute_metric("ai_success_rate", agg),
            "tokens_total": compute_metric("tokens_total", agg), "quota_estimated": int(agg.get("quota_estimated", 0)),
            "quota_actual": int(agg.get("quota_actual", 0)), "cost_cny": _cost(agg.get("cost_cny")),
            "share": _ratio_value(float(agg.get("cost_cny", 0)), total_cost, 4),
        }
        if dimension == "capability":
            item["task_success_rate"] = compute_metric("task_success_rate", agg)
            item["task_avg_duration_ms"] = compute_metric("task_avg_duration_ms", agg)
        items.append(item)
    items.sort(key=lambda it: (-it["cost_cny"], str(it[name])))
    return items[:OVERVIEW_TOP_ROWS]


def _engine_rates(db: Session, scope: DataScope, project_id: int, snapshot_day: date | None) -> dict[str, dict[str, dict[str, Any]]]:
    """存量口径 ``{engine: {rate, hit, total}}``（§9.1）：引擎集合取 ``snapshot_date`` 下 ``project_id=0`` 的引擎行（SEO 另含固定
    引擎集合，未启用的引擎 ``rate`` / ``hit`` 为 ``null``）；``total`` 为同日 ``total`` 行的 ``links_total_snapshot``。"""
    from app.services import index_check_service  # 延迟导入：index_check_service 依赖本模块

    result: dict[str, dict[str, dict[str, Any]]] = {}
    total = 0
    if snapshot_day is not None:
        totals = load_grouped(db, scope, project_id=project_id, start=snapshot_day, end=snapshot_day, dimension="total", group=(),
                              columns={"links_total_snapshot"})
        total = int(totals.get((), {}).get("links_total_snapshot", 0))
    for kind, dimension, col, out_key in (("seo", "seo_engine", "seo_indexed_snapshot", "seo_index_rate_by_engine"),
                                          ("geo", "geo_engine", "geo_cited_snapshot", "geo_cite_rate_by_engine")):
        summary_keys: list[str] = []
        hits: dict[str, int] = {}
        if snapshot_day is not None:
            summary_keys = sorted(db.scalars(select(DailyStat.dimension_key).where(
                DailyStat.stat_date == snapshot_day, DailyStat.project_id == 0, DailyStat.dimension == dimension)).all())
            rows = load_grouped(db, scope, project_id=project_id, start=snapshot_day, end=snapshot_day, dimension=dimension,
                                group=("key",), columns={col})
            hits = {key: int(values.get(col, 0)) for (key,), values in rows.items()}
        keys = list(summary_keys)
        if kind == "seo":
            keys += [code for code in index_check_service.seo_engine_codes(db) if code not in keys]
        engines: dict[str, dict[str, Any]] = {}
        for key in keys:
            if key not in summary_keys:
                hit = None
            elif key in hits:
                hit = hits[key]
            else:
                hit = 0 if scope.restricted and not project_id else None   # owner 范围：P 内无行计 0；单项目无该引擎行为 null
            engines[key] = {"rate": _ratio_value(hit, total, 4) if hit is not None else None, "hit": hit, "total": total}
        result[out_key] = engines
    return result


def _computed_at(db: Session, start: date, end: date) -> str | None:
    value = db.scalar(select(func.max(DailyStat.computed_at)).where(
        DailyStat.project_id == 0, DailyStat.dimension == "total", DailyStat.stat_date >= start, DailyStat.stat_date <= end))
    return iso_utc(value) if value is not None else None


def _overview_data(db: Session, scope: DataScope, project_id: int, range_key: str) -> dict[str, Any]:
    tz = get_tz(db)
    tz_name = getattr(tz, "key", str(tz))
    today = today_date(tz=tz)
    days = RANGES[range_key]
    start = today - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    series_days = max(days, SERIES_MIN_DAYS)
    series_start = today - timedelta(days=series_days - 1)
    load_start = min(prev_start, series_start)
    warnings: list[str] = []
    flow_cols = set(FLOW_COLUMNS)
    rows = load_grouped(db, scope, project_id=project_id, start=load_start, end=today, dimension="total", group=("date",),
                        columns=flow_cols)
    per_date: dict[date, dict[str, Any]] = {gkey[0]: values for gkey, values in rows.items()}
    cost = load_grouped(db, scope, project_id=project_id, start=load_start, end=today, dimension="capability", group=("date",),
                        columns={"cost_cny"}, keys=CONTENT_COST_CAPABILITIES)
    for (day,), values in cost.items():
        per_date.setdefault(day, {})[CONTENT_COST] = values.get("cost_cny", 0)
    # 今日：以今日 project_id=0 的 total 行是否存在为准（各范围一致），存在只用 daily_stats，否则用 stats:rt，二者不叠加
    if _summary_row_exists(db, today):
        today_source = "daily_stats"
    else:
        realtime = realtime_today(scope, project_id, db=db)
        if realtime is None:
            today_source = "none"
            warnings.append("realtime_unavailable")
            per_date.pop(today, None)
        else:
            today_source = "realtime"
            per_date[today] = {c: realtime.get(c, 0) for c in FLOW_COLUMNS}
    sum_cols = flow_cols | {CONTENT_COST}
    current = _flow_kpis(_sum_days(per_date, start, today, sum_cols))
    previous = _flow_kpis(_sum_days(per_date, prev_start, prev_end, sum_cols))
    current_kpis, distributions, current_warnings = _current_metrics(db, scope, project_id)
    warnings.extend(current_warnings)
    kpis_all = {**current, **current_kpis}
    kpis = {key: kpis_all.get(key) for key in KPI_KEYS}
    snapshot_day = _summary_row_date(db, today)
    breakdowns: dict[str, Any] = dict(distributions)
    breakdowns["cost_by_capability"] = _cost_table(db, scope, project_id, start, today, "capability")
    breakdowns["cost_by_model"] = _cost_table(db, scope, project_id, start, today, "model")
    breakdowns.update(_engine_rates(db, scope, project_id, snapshot_day))
    series_dates = [series_start + timedelta(days=i) for i in range(series_days)]
    series: dict[str, Any] = {"dates": [d.isoformat() for d in series_dates]}
    for key in SERIES_KEYS:
        series[key] = [compute_metric(key, per_date.get(d, {})) for d in series_dates]
    meta = {
        "range": range_key, "start_date": start.isoformat(), "end_date": today.isoformat(), "timezone": tz_name,
        "project_id": project_id, "scope": "owner" if scope.restricted else "all",
        "owner_id": scope.owner_id if scope.restricted else None, "today_source": today_source,
        "snapshot_date": snapshot_day.isoformat() if snapshot_day else None,
        "computed_at": _computed_at(db, start, today), "cached": False, "warnings": warnings,
    }
    return {"meta": meta, "kpis": kpis, "compare": _compare(current, previous), "breakdowns": breakdowns, "series": series}


def overview(db: Session, scope: DataScope, *, project_id: int = 0, range: str = "7d") -> dict[str, Any]:  # noqa: A002
    """``GET /admin/stats/overview``（``GET /admin/projects/{id}/overview`` 复用）：``{meta, kpis, compare, breakdowns, series}``。

    缓存 ``cache:stats:overview:{scope_key}:{project_id}:{range}``，TTL ``stats_config.overview_cache_seconds``（0 关闭）；
    命中时 ``meta.cached=true``；Redis 不可用时直接查库并在 ``meta.warnings[]`` 追加 ``cache_unavailable``。"""
    range_key = _require_choice(range or "7d", tuple(RANGES), "range")
    pid = _project_param(db, scope, project_id)
    try:
        ttl = int(settings_service.get_config(db, "stats_config").get("overview_cache_seconds") or 0)
    except (TypeError, ValueError):
        ttl = 0
    key = overview_cache_key(scope, pid, range_key)
    cache_ok = True
    if ttl > 0:
        cached, cache_ok = _cache_read(key)
        if isinstance(cached, dict) and isinstance(cached.get("meta"), dict):
            cached["meta"]["cached"] = True
            return cached
    data = _overview_data(db, scope, pid, range_key)
    if ttl > 0 and cache_ok:
        cache_ok = cache_set_json(key, data, ttl)
    if not cache_ok:
        data["meta"]["warnings"].append("cache_unavailable")
    return data


# =====================================================================
# 导出 CSV（§8）
# =====================================================================

EXPORT_REPORTS: tuple[str, ...] = ("trends", "breakdown", "rankings")
EXPORT_REQUIRED: dict[str, tuple[str, ...]] = {
    "trends": ("metrics", "start", "end"),
    "breakdown": ("dimension", "metric", "start", "end"),
    "rankings": ("type", "start", "end"),
}


def csv_value(value: Any, metric: str | None = None) -> str:
    """整数原样；``cost_cny`` 等金额 6 位小数；比率 4 位小数；``null`` 留空。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    spec = METRIC_SPECS.get(metric or "")
    if spec is not None and isinstance(value, (int, float, Decimal)):
        if spec.unit == "currency":
            return f"{float(value):.6f}"
        if spec.is_ratio and spec.unit == "percent" or metric == "share":
            return f"{float(value):.4f}"
    if metric == "share" and isinstance(value, (int, float)):
        return f"{float(value):.4f}"
    if isinstance(value, float) and value.is_integer() and (spec is None or spec.decimals in (None, 0)):
        return str(int(value))
    return str(value)


def _export_too_many(rows: int) -> BusinessError:
    message = f"导出行数超过 {EXPORT_MAX_ROWS}，请收窄筛选范围"
    return _bad(message, field_error(["query"], message, "value_error", rows))


def _render_csv(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def export_csv(db: Session, scope: DataScope, report: str, params: Mapping[str, Any], *, locale: str = "zh-CN") -> tuple[str, str]:
    """``GET /admin/stats/export``：返回 ``(filename, csv_text)``（UTF-8 BOM、``\\r\\n``，中文列头见 ``schemas.stats.EXPORT_COLUMNS``）；
    参数与对应查询接口完全相同，按 ``report`` 校验必填项；超过 50,000 行 400。"""
    from app.schemas.stats import EXPORT_COLUMNS  # 延迟导入：schemas.stats 引用本模块常量

    report = _require_choice(report, EXPORT_REPORTS, "report")
    missing = [name for name in EXPORT_REQUIRED[report] if params.get(name) in (None, "")]
    if missing:
        raise invalid_params([field_error(["query", name], "必填字段", "missing", None) for name in missing])
    start, end = params["start"], params["end"]
    project_id = int(params.get("project_id") or 0)
    if report == "trends":
        granularity = _require_choice(params.get("granularity") or "day", GRANULARITIES, "granularity")
        dimension = _require_choice(params.get("dimension") or "total", STATS_DIMENSIONS, "dimension")
        metric_list = parse_metrics(params["metrics"], param="metrics")
        check_date_range(start, end)
        estimated = len(periods(start, end, granularity))
        if estimated > EXPORT_MAX_ROWS:
            raise _export_too_many(estimated)
        data = trends(db, scope, metrics=metric_list, start=start, end=end, granularity=granularity, project_id=project_id,
                      dimension=dimension, dimension_key=params.get("dimension_key") or "", locale=locale)
        fixed = [(k, h) for k, h in EXPORT_COLUMNS["trends"] if dimension != "total" or k not in ("dimension", "dimension_key")]
        header = [h for _, h in fixed] + [metric_label(m) for m in metric_list]
        key = params.get("dimension_key") or ""
        context = {"dimension": dimension, "dimension_key": key}
        body = ([csv_value(context.get(k, row.get(k))) for k, _ in fixed] + [csv_value(row.get(m), m) for m in metric_list]
                for row in data)
    elif report == "breakdown":
        metric_list = parse_metrics(params["metric"], param="metric")
        dimension = str(params["dimension"])
        data = breakdown(db, scope, dimension=dimension, metric=metric_list, start=start, end=end, project_id=project_id,
                         locale=locale)
        if len(data) > EXPORT_MAX_ROWS:
            raise _export_too_many(len(data))
        primary = metric_list[0]
        fixed = EXPORT_COLUMNS["breakdown"]
        header = [h for _, h in fixed] + [metric_label(m) for m in metric_list]
        body = ([dimension, row["key"], row["label"], csv_value(row["value"], primary), csv_value(row["share"], "share")]
                + [csv_value(row["values"].get(m), m) for m in metric_list] for row in data)
    else:
        ranking_type = str(params["type"])
        limit = params.get("limit")
        data = rankings(db, scope, ranking_type=ranking_type, start=start, end=end, project_id=project_id,
                        limit=int(limit) if limit not in (None, "") else None, locale=locale)
        if len(data) > EXPORT_MAX_ROWS:
            raise _export_too_many(len(data))
        if ranking_type == "fastest_indexed":
            fixed = EXPORT_COLUMNS["rankings_fastest_indexed"]
            header = [h for _, h in fixed]
            body = ([csv_value(row.get(k)) for k, _ in fixed] for row in data)
        else:
            fixed = EXPORT_COLUMNS["rankings"]
            value_metric = {"most_deleted_platforms": "links_deleted", "top_failed_models": "ai_failed"}.get(ranking_type, "cost_cny")
            extra_keys = list(data[0]["extra"]) if data else _RANKING_EXTRAS[ranking_type]
            header = [h for _, h in fixed] + [metric_label(m) for m in extra_keys]
            body = ([str(row["rank"]), row["key"], row["label"], csv_value(row["value"], value_metric)]
                    + [csv_value(row["extra"].get(m), m) for m in extra_keys] for row in data)
    filename = f"stats-{report}-{today_date(db).strftime('%Y%m%d')}.csv"
    return filename, _render_csv(header, body)


_RANKING_EXTRAS: dict[str, list[str]] = {
    "fastest_indexed": [],
    "most_deleted_platforms": ["links_total"],
    "top_cost_models": ["ai_calls", "ai_success_rate"],
    "top_failed_models": ["ai_calls", "ai_success_rate"],
    "top_cost_projects": ["contents_created", "cost_cny_per_content"],
}


# =====================================================================
# 重算（§4.5）
# =====================================================================


def recompute(db: Session, start_date: date, end_date: date, *, requested_by: int | None = None) -> tuple[int, dict[str, Any]]:
    """``POST /admin/stats/recompute``（全局动作，不受数据范围影响）：返回 ``(http_status, data)``。

    - 跨度 ≤ 7 天：API 进程内逐日 ``aggregate``（持 ``lock:monitor:daily_stats:{date}``），锁被占用的日期进 ``skipped[]``，
      返回 200 ``{days, rows_upserted, skipped, duration_ms}``；
    - 8~31 天：``RPUSH queue:stats_recompute {"start_date","end_date","requested_by"}``，返回 202 ``{queued: true, days}``；
    - ``start_date > end_date``、``end_date`` 晚于今日、跨度 > 31 天 → 400 ``loc=["body","end_date"]``。"""
    today = today_date(db)

    def _reject(message: str) -> BusinessError:
        return _bad(message, field_error(["body", "end_date"], message, "value_error", end_date.isoformat()))

    if start_date > end_date:
        raise _reject("end_date 不能早于 start_date")
    if end_date > today:
        raise _reject("end_date 不能晚于今日")
    days = (end_date - start_date).days + 1
    if days > RECOMPUTE_MAX_DAYS:
        raise _reject(f"重算跨度不能超过 {RECOMPUTE_MAX_DAYS} 天")
    if days > RECOMPUTE_SYNC_MAX_DAYS:
        payload = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "requested_by": requested_by}
        try:
            redis_client.rpush(RECOMPUTE_QUEUE, json.dumps(payload, separators=(",", ":")))
        except redis.RedisError as exc:
            logger.error("重算入队失败: %s", exc)
            raise BusinessError("Redis 不可用，请稍后重试", code=503, http_status=503) from exc
        return 202, {"queued": True, "days": days}
    from app.tasks import aggregate_daily_stats  # 延迟导入：任务模块依赖本模块

    started = time.monotonic()
    rows = 0
    skipped: list[str] = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        upserted = aggregate_daily_stats.aggregate_locked(day)
        if upserted is None:
            skipped.append(day.isoformat())
        else:
            rows += upserted
    return 200, {"days": days, "rows_upserted": rows, "skipped": skipped,
                 "duration_ms": int((time.monotonic() - started) * 1000)}
