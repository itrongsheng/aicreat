"""监控（``/admin/monitoring``，docs/04 §6.18、§7.11、§7.12；docs/11 §6.7、§9、§11.6；docs/13 §6.2、§6.3、§7.5）。

全部路由声明 ``get_data_scope``：``overview`` 的 ``due`` / ``today`` / ``last_run_at`` 按可见链接计算（``queued`` / ``workers`` /
``daily_limits`` 为平台值）；检测记录经所属链接过滤，单条目标不可见 → 404；``…/run`` 只作用于可见链接（不可见的 ``link_ids``
计入 ``skipped``，``project_id`` 不可见 → ``{enqueued:0, skipped:0}``，``scope.restricted`` 且未给 ``project_id`` / ``link_ids``
时只对负责项目下的链接入队；总后台用户视角由查询参数 ``owner_id`` 带入）。静态子路径先于 ``/{id}`` 注册。
"""

from __future__ import annotations

import logging
from typing import Any

import redis
from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.api.health import worker_replicas
from app.core.response import ok, paginated
from app.models import Admin
from app.schemas.common import PageParams
from app.schemas.monitoring import IndexCheckFilters, IndexChecksRunBody, LinkCheckFilters, LinkChecksRunBody
from app.services import index_check_service, link_service, monitoring_service
from app.services.data_scope_service import DataScope

logger = logging.getLogger(__name__)

router = APIRouter()

CheckId = Path(..., gt=0, description="检测记录 ID")
MONITOR_WORKER = "monitor_worker"


def _workers() -> list[dict[str, Any]]:
    try:
        return worker_replicas(MONITOR_WORKER)
    except redis.RedisError as exc:
        logger.warning("读取 monitor_worker 心跳失败：%s", exc)
        return []


def _scope_summary(scope: DataScope) -> str:
    return f"（用户视角 owner_id={scope.owner_id}）" if scope.restricted and scope.is_all else ""


@router.get("/overview", summary="监控概览（到期数、队列长度、今日检测数、心跳、日上限使用量）")
def overview(
    _admin: Admin = Depends(require_permission("monitoring.link_checks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(monitoring_service.overview(db, scope, workers=_workers()))


@router.get("/link-checks", summary="删除检测记录（每条附 link）")
def list_link_checks(
    filters: LinkCheckFilters = Depends(),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("monitoring.link_checks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = monitoring_service.list_link_checks(
        db, scope, page=pagination.page, page_size=pagination.page_size, **filters.model_dump()
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("/link-checks/run", summary="批量入队 manual 删除检测")
def run_link_checks(
    body: LinkChecksRunBody,
    request: Request,
    admin: Admin = Depends(require_permission("monitoring.link_checks.run")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = link_service.run_link_checks_batch(
        db, scope, admin_id=admin.id, project_id=body.project_id, platform_id=body.platform_id, link_ids=body.link_ids,
        only_due=body.only_due,
    )
    request.state.audit_summary = (
        f"批量触发删除检测：入队 {data['enqueued']}，跳过 {data['skipped']}"
        + (f"，项目 #{body.project_id}" if body.project_id else "") + _scope_summary(scope)
    )
    return ok(data)


@router.get("/link-checks/{check_id}", summary="删除检测记录详情（含 evidence）")
def get_link_check(
    check_id: int = CheckId,
    _admin: Admin = Depends(require_permission("monitoring.link_checks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(monitoring_service.get_link_check(db, scope, check_id))


@router.get("/index-checks", summary="收录检测记录")
def list_index_checks(
    filters: IndexCheckFilters = Depends(),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("monitoring.index_checks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = monitoring_service.list_index_checks(
        db, scope, page=pagination.page, page_size=pagination.page_size, **filters.model_dump()
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("/index-checks/run", summary="批量入队 manual 收录检测")
def run_index_checks(
    body: IndexChecksRunBody,
    request: Request,
    admin: Admin = Depends(require_permission("monitoring.index_checks.run")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = index_check_service.run_index_checks_batch(
        db, scope, admin_id=admin.id, kinds=body.kinds, engines=body.engines, project_id=body.project_id,
        platform_id=body.platform_id, link_ids=body.link_ids, only_due=body.only_due,
    )
    request.state.audit_summary = (
        f"批量触发收录检测（{'/'.join(body.kinds)}）：入队 {data['enqueued']}，跳过 {data['skipped']}"
        + (f"，项目 #{body.project_id}" if body.project_id else "") + _scope_summary(scope)
    )
    return ok(data)


@router.get("/index-checks/{check_id}", summary="收录检测记录详情（含 evidence）")
def get_index_check(
    check_id: int = CheckId,
    _admin: Admin = Depends(require_permission("monitoring.index_checks.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(monitoring_service.get_index_check(db, scope, check_id))
