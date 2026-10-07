"""统计报表的查询参数与响应模型（docs/12 §8、§9；docs/04 §6.20、§7.14）。

查询参数模型以 ``Depends()`` 展开为查询参数（Pydantic 校验错误 ``loc=["query", <参数>]``）；``owner_id`` 由
``get_data_scope`` 声明。指标列表（``metrics`` / ``metric``）、维度矩阵与日期跨度等业务校验在 ``stats_service`` 中按
§9.8 返回 ``unsupported_metric`` / ``unsupported_dimension`` / ``value_error``。

``EXPORT_COLUMNS``：CSV 固定列（键 → 中文列头）与列顺序，按 ``report`` 规定；指标列的列头取 ``METRIC_SPECS`` 的中文名。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import ExportParams

StatsRangeParam = Literal["today", "7d", "30d"]
StatsGranularityParam = Literal["day", "week", "month"]
StatsDimensionParam = Literal["total", "platform", "capability", "model", "admin", "seo_engine", "geo_engine"]
BreakdownDimensionParam = Literal["project", "owner", "platform", "admin", "model", "capability", "seo_engine", "geo_engine"]
RankingTypeParam = Literal["fastest_indexed", "most_deleted_platforms", "top_cost_models", "top_cost_projects", "top_failed_models"]
ExportReportParam = Literal["trends", "breakdown", "rankings"]
AnyDimensionParam = Literal["total", "project", "owner", "platform", "capability", "model", "admin", "seo_engine", "geo_engine"]

ProjectIdParam = Annotated[int, Field(ge=0, le=2**63 - 1, description="0 = 全部项目（按用户统计时为该用户全部项目）")]
MetricsParam = Annotated[str, Field(min_length=1, max_length=2000, description="逗号分隔，1~8 个")]
DimensionKeyParam = Annotated[str, Field(max_length=120)]


class _Query(BaseModel):
    model_config = ConfigDict(extra="ignore")


class OverviewQuery(_Query):
    """``GET /admin/stats/overview?project_id=0&range=7d``。"""

    project_id: ProjectIdParam = 0
    range: StatsRangeParam = "7d"


class TrendsQuery(_Query):
    """``GET /admin/stats/trends``：``metrics`` / ``start`` / ``end`` 必填；``dimension != total`` 时 ``dimension_key`` 必填。"""

    metrics: MetricsParam
    granularity: StatsGranularityParam = "day"
    start: date
    end: date
    project_id: ProjectIdParam = 0
    dimension: StatsDimensionParam = "total"
    dimension_key: DimensionKeyParam = ""


class BreakdownQuery(_Query):
    """``GET /admin/stats/breakdown``：第一个 ``metric`` 为主指标（排序与 ``share`` 依据）。"""

    dimension: BreakdownDimensionParam
    metric: MetricsParam
    start: date
    end: date
    project_id: ProjectIdParam = 0


class RankingsQuery(_Query):
    """``GET /admin/stats/rankings``：``limit`` 缺省取 ``stats_config.rankings_limit``，最大 100。"""

    type: RankingTypeParam
    start: date
    end: date
    project_id: ProjectIdParam = 0
    limit: Annotated[int, Field(ge=1, le=100)] | None = None


class StatsExportParams(ExportParams):
    """``GET /admin/stats/export?report=trends|breakdown|rankings&…``：三类查询参数的并集，按 ``report`` 校验必填项。"""

    report: ExportReportParam
    metrics: MetricsParam | None = None
    metric: MetricsParam | None = None
    granularity: StatsGranularityParam = "day"
    start: date | None = None
    end: date | None = None
    project_id: ProjectIdParam = 0
    dimension: AnyDimensionParam | None = None
    dimension_key: DimensionKeyParam = ""
    type: RankingTypeParam | None = None
    limit: Annotated[int, Field(ge=1, le=100)] | None = None


class RecomputeBody(BaseModel):
    """``POST /admin/stats/recompute`` ``{start_date, end_date}``（统计时区日期，跨度 ≤ 31 天）。"""

    model_config = ConfigDict(extra="forbid")

    start_date: date
    end_date: date


# =====================================================================
# 响应模型（OpenAPI 文档；路由返回 ``ok(dict)`` 外壳）
# =====================================================================


class CompareItem(BaseModel):
    previous: float | None = None
    delta: float | None = None
    delta_rate: float | None = None


class OverviewMeta(BaseModel):
    range: StatsRangeParam
    start_date: str
    end_date: str
    timezone: str
    project_id: int
    scope: Literal["all", "owner"]
    owner_id: int | None = None
    today_source: Literal["daily_stats", "realtime", "none"]
    snapshot_date: str | None = None
    computed_at: str | None = None
    cached: bool = False
    warnings: list[str] = Field(default_factory=list)


class OverviewOut(BaseModel):
    meta: OverviewMeta
    kpis: dict[str, float | int | None]
    compare: dict[str, CompareItem]
    breakdowns: dict[str, Any]
    series: dict[str, list[Any]]


class TrendPoint(BaseModel):
    """``{date, <metric>: value…}``：``date`` 为周期标签，其余键为请求指标与自动附带的分子分母列。"""

    model_config = ConfigDict(extra="allow")

    date: str


class BreakdownRow(BaseModel):
    key: str
    label: str
    value: float | int | None = None
    share: float | None = None
    values: dict[str, float | int | None]


class RankingRow(BaseModel):
    """``fastest_indexed``：链接明细列；其余类型 ``{rank, key, label, value, extra}``。"""

    model_config = ConfigDict(extra="allow")

    rank: int
    key: str | None = None
    label: str | None = None
    value: float | int | None = None
    extra: dict[str, Any] | None = None


class RecomputeOut(BaseModel):
    days: int
    rows_upserted: int | None = None
    skipped: list[str] | None = None
    duration_ms: int | None = None
    queued: bool | None = None


# =====================================================================
# CSV 固定列（docs/12 §8；列头与文件名不随界面语言切换）
# =====================================================================

EXPORT_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # 维度查询（dimension != total）时最前面加「维度」「维度键」；其后为日期 + 请求的每个指标（按 metrics 顺序）
    "trends": [("dimension", "维度"), ("dimension_key", "维度键"), ("date", "日期")],
    # 占比之后按 metric 顺序追加每个指标一列
    "breakdown": [("dimension", "维度"), ("key", "键"), ("label", "名称"), ("value", "数值"), ("share", "占比")],
    # 其它榜单类型：之后追加 extra 中的附加列（中文指标名）
    "rankings": [("rank", "名次"), ("key", "键"), ("label", "名称"), ("value", "数值")],
    "rankings_fastest_indexed": [
        ("rank", "名次"), ("content_id", "内容 ID"), ("title", "标题"), ("link_id", "链接 ID"), ("url", "链接"),
        ("platform_code", "平台代码"), ("platform_name", "平台"), ("project_id", "项目 ID"), ("project_name", "项目"),
        ("published_at", "发布时间"), ("first_indexed_at", "首次收录时间"), ("hours", "收录耗时（小时）"),
    ],
}
