"""用量对账（``/admin/ai/usage``，docs/04 §6.15、§7.17；docs/08 §10.7；docs/13 §4.3、§6.3）。

``logs`` / ``summary`` 只含可见尝试行（未匹配条目只对总后台可见）；``last-pull`` 在 ``own`` 范围（或带 ``owner_id``）只返回
``{pulled_at, window_overflow}``；``reconcile`` 不受范围约束（运维动作），但按 ``SCOPED_ROUTE_PREFIXES`` 约定同样声明依赖。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, get_args

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.exceptions import field_error, invalid_params
from app.core.response import ok, paginated
from app.models import Admin, UsageLogType
from app.schemas.ai import UsageSummaryGroupBy
from app.schemas.common import PageParams
from app.services import ai_usage_service
from app.services.data_scope_service import DataScope

router = APIRouter()

USAGE_LOG_TYPES = frozenset(get_args(UsageLogType))


@router.get("/logs", summary="用量日志（分页）")
def list_logs(
    model_name: str | None = Query(None, max_length=120),
    log_type: int | None = Query(None, description="2 消费 / 5 失败 / 6 退款"),
    matched: bool | None = Query(None, description="true：已匹配到尝试行"),
    request_id: str | None = Query(None, max_length=64),
    start: datetime | None = Query(None, description="拉取时间，ISO 8601 UTC，含"),
    end: datetime | None = Query(None, description="拉取时间，ISO 8601 UTC，不含"),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("ai.usage.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if log_type is not None and log_type not in USAGE_LOG_TYPES:
        raise invalid_params(field_error(["query", "log_type"], "取值必须是 2、5、6 之一", "literal_error", log_type))
    items, total = ai_usage_service.list_usage_logs(
        db, scope, model_name=model_name, log_type=log_type, matched=matched, request_id=request_id, start=start, end=end,
        page=pagination.page, page_size=pagination.page_size,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("/reconcile", summary="立即对账")
def reconcile(
    _admin: Admin = Depends(require_permission("ai.usage.reconcile")),
    _scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """拉取 ``/api/log/token`` 对账（锁 ``lock:worker:reconcile``，占用中 409；拉取失败 5021；Mock 同样执行）。"""
    return ok(ai_usage_service.reconcile(db))


@router.get("/summary", summary="用量汇总")
def summary(
    group_by: UsageSummaryGroupBy = Query("model"),
    start: date | None = Query(None, description="YYYY-MM-DD（stats_config.timezone），闭区间；缺省最近 30 天"),
    end: date | None = Query(None, description="YYYY-MM-DD，闭区间"),
    _admin: Admin = Depends(require_permission("ai.usage.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_usage_service.usage_summary(db, scope, group_by=group_by, start=start, end=end))


@router.get("/last-pull", summary="最近一次对账拉取摘要")
def last_pull(
    _admin: Admin = Depends(require_permission("ai.usage.view")),
    scope: DataScope = Depends(get_data_scope),
) -> dict[str, Any]:
    return ok(ai_usage_service.last_pull_view(scope))
