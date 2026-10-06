"""告警中心（``/admin/alerts``，docs/04 §6.19、§7.13；docs/11 §10.2、§11.7；docs/13 §6.2、§11）。

全部路由声明 ``get_data_scope``：按 ``alerts.project_id`` 归属过滤，``project_id`` 为 NULL 的系统告警（``ai_*`` / ``worker_stale``）
只对总后台（``all`` 且未带 ``owner_id``）可见；详情与处理动作的目标不可见按不存在处理（404）。``summary`` 的 ``today_*`` 按
``stats_config.timezone`` 切日。静态子路径 ``summary`` / ``batch-resolve`` 先于 ``/{id}`` 注册（docs/04 §6.0）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin
from app.schemas.alert import AlertBatchResolveBody, AlertFilters, AlertNoteBody
from app.schemas.common import PageParams
from app.services import alert_service
from app.services.data_scope_service import DataScope

router = APIRouter()

AlertId = Path(..., gt=0, description="告警 ID")


def _note(body: AlertNoteBody | None) -> str | None:
    return (body.note or None) if body is not None else None


def _label(alert: dict[str, Any]) -> str:
    return f"#{alert['id']}（{alert['alert_type']}，{alert['target_type'] or '-'}:{alert['target_key'] or ''}）"


# =====================================================================
# 集合级（静态子路径先于 /{id}）
# =====================================================================


@router.get("", summary="告警列表（按 last_triggered_at 倒序）")
def list_alerts(
    filters: AlertFilters = Depends(),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("monitoring.alerts.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = alert_service.list_alerts(
        db, scope, page=pagination.page, page_size=pagination.page_size, **filters.model_dump()
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.get("/summary", summary="告警摘要（按 severity 的 open / acknowledged 数、今日新增 / 解决）")
def alert_summary(
    _admin: Admin = Depends(require_permission("monitoring.alerts.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(alert_service.summary(db, scope))


@router.post("/batch-resolve", summary="批量解决（终态 / 不可见计入 skipped）")
def batch_resolve(
    body: AlertBatchResolveBody,
    request: Request,
    admin: Admin = Depends(require_permission("monitoring.alerts.handle")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = alert_service.batch_resolve(db, scope, body.ids, admin_id=admin.id, note=body.note or None)
    request.state.audit_summary = (
        f"批量解决告警：{data['updated']} 条，跳过 {len(data['skipped'])} 条"
        + (f"，备注：{body.note}" if body.note else "")
    )[:500]
    return ok(data)


# =====================================================================
# 对象级
# =====================================================================


@router.get("/{alert_id}", summary="告警详情（含 payload）")
def get_alert(
    alert_id: int = AlertId,
    _admin: Admin = Depends(require_permission("monitoring.alerts.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(alert_service.get_alert(db, scope, alert_id))


@router.post("/{alert_id}/acknowledge", summary="确认（open → acknowledged）")
def acknowledge(
    request: Request,
    alert_id: int = AlertId,
    admin: Admin = Depends(require_permission("monitoring.alerts.handle")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alert = alert_service.acknowledge(db, scope, alert_id, admin_id=admin.id)
    request.state.audit_summary = f"确认告警 {_label(alert)}"
    return ok(alert)


@router.post("/{alert_id}/resolve", summary="解决（open / acknowledged → resolved，{note?}）")
def resolve(
    request: Request,
    body: AlertNoteBody | None = None,
    alert_id: int = AlertId,
    admin: Admin = Depends(require_permission("monitoring.alerts.handle")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alert = alert_service.resolve(db, scope, alert_id, admin_id=admin.id, note=_note(body))
    request.state.audit_summary = f"解决告警 {_label(alert)}" + (f"：{_note(body)}" if _note(body) else "")
    return ok(alert)


@router.post("/{alert_id}/ignore", summary="忽略（open / acknowledged → ignored，{note?}）")
def ignore(
    request: Request,
    body: AlertNoteBody | None = None,
    alert_id: int = AlertId,
    admin: Admin = Depends(require_permission("monitoring.alerts.handle")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    alert = alert_service.ignore(db, scope, alert_id, admin_id=admin.id, note=_note(body))
    request.state.audit_summary = f"忽略告警 {_label(alert)}" + (f"：{_note(body)}" if _note(body) else "")
    return ok(alert)
