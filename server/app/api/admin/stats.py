"""统计报表（``/admin/stats``，docs/04 §6.20、§7.14；docs/12 §9；数据范围 docs/13 §10）。

全部路由声明 ``get_data_scope``：查询类接口（``overview`` / ``trends`` / ``breakdown`` / ``rankings`` / ``export``）按范围键
统计（``all`` 读汇总行，``owner:{id}`` 对该用户项目行求和，``project_id`` 不属于该用户 → 404）；``recompute`` 为全局动作，
声明依赖只为保持规则单一（docs/13 §9.3）。400 一律为 docs/04 §5.1 校验错误列表（docs/12 §9.8）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_locale, require_permission
from app.core.response import ok
from app.models import Admin, Locale
from app.schemas.stats import BreakdownQuery, OverviewQuery, RankingsQuery, RecomputeBody, StatsExportParams, TrendsQuery
from app.services import stats_service
from app.services.data_scope_service import DataScope

router = APIRouter()


@router.get("/overview", summary="总览：KPI + 环比 + 分解 + 四条轻量序列")
def overview(
    query: OverviewQuery = Depends(),
    _admin: Admin = Depends(require_permission("dashboard.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(stats_service.overview(db, scope, project_id=query.project_id, range=query.range))


@router.get("/trends", summary="趋势（日 / 周 / 月）")
def trends(
    query: TrendsQuery = Depends(),
    locale: Locale = Depends(get_locale),
    _admin: Admin = Depends(require_permission("stats.reports.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(stats_service.trends(
        db, scope, metrics=query.metrics, start=query.start, end=query.end, granularity=query.granularity,
        project_id=query.project_id, dimension=query.dimension, dimension_key=query.dimension_key, locale=locale,
    ))


@router.get("/breakdown", summary="分解（按项目 / 用户 / 平台 / 人员 / 模型 / 能力 / 引擎）")
def breakdown(
    query: BreakdownQuery = Depends(),
    locale: Locale = Depends(get_locale),
    _admin: Admin = Depends(require_permission("stats.reports.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(stats_service.breakdown(
        db, scope, dimension=query.dimension, metric=query.metric, start=query.start, end=query.end,
        project_id=query.project_id, locale=locale,
    ))


@router.get("/rankings", summary="榜单")
def rankings(
    query: RankingsQuery = Depends(),
    locale: Locale = Depends(get_locale),
    _admin: Admin = Depends(require_permission("stats.reports.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(stats_service.rankings(
        db, scope, ranking_type=query.type, start=query.start, end=query.end, project_id=query.project_id,
        limit=query.limit, locale=locale,
    ))


@router.get("/export", summary="导出趋势 / 分解 / 榜单 CSV")
def export(
    params: StatsExportParams = Depends(),
    locale: Locale = Depends(get_locale),
    _admin: Admin = Depends(require_permission("stats.reports.export")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> Response:
    filename, content = stats_service.export_csv(db, scope, params.report, params.model_dump(), locale=locale)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/recompute", summary="重算 daily_stats（≤ 7 天同步，8~31 天入队 202）")
def recompute(
    body: RecomputeBody,
    request: Request,
    admin: Admin = Depends(require_permission("stats.reports.recompute")),
    _scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> Any:
    status, data = stats_service.recompute(db, body.start_date, body.end_date, requested_by=admin.id)
    request.state.audit_summary = f"重算统计 {body.start_date.isoformat()}~{body.end_date.isoformat()}"
    if status == 202:
        return JSONResponse(status_code=202, content=ok(data))
    return ok(data)
