"""能力路由、健康快照与探测（挂载 ``/admin/ai``：``/routes``、``/routes/{id}``、``/routes/{id}/test``、
``/routes/{id}/reset-breaker``、``/health``、``/health/probe``；docs/04 §6.15、§7.16；docs/08 §6.7、§12.3）。

``/admin/ai/routes*`` 属于 ``SCOPED_ROUTE_PREFIXES``：列表返回全局行与可见项目的覆盖行，项目覆盖行的读写与 ``test`` /
``reset-breaker`` 要求项目可见（否则 404，docs/13 §6.3）；``/health*`` 是运维动作，不受数据范围约束（docs/13 §4.3）。
"""

from __future__ import annotations

import logging
from typing import Any

import redis
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, require_permission
from app.api.health import WORKER_NAMES, worker_replicas
from app.core.response import ok
from app.models import Admin
from app.schemas.ai import ProbeBody, RouteTestBody, RouteUpdate, RouteUpsert
from app.services import ai_gateway_service, ai_task_service
from app.services.data_scope_service import DataScope

logger = logging.getLogger(__name__)

router = APIRouter()

RouteId = Path(..., gt=0, description="路由 ID")


@router.get("/routes", summary="能力路由（不分页）")
def list_routes(
    project_id: int | None = Query(None, gt=0, description="含该项目的覆盖行；项目不可见时只返回全局行"),
    _admin: Admin = Depends(require_permission("ai.routes.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_gateway_service.list_routes(db, scope, project_id=project_id))


@router.post("/routes", summary="新建项目覆盖路由")
def create_route(
    body: RouteUpsert,
    request: Request,
    admin: Admin = Depends(require_permission("ai.routes.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = ai_gateway_service.create_route(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"新增 capability_route #{item['id']}（{item['capability']} / 项目 {item['project_id']}）"
    return ok(item)


@router.get("/routes/{route_id}", summary="路由详情")
def get_route(
    route_id: int = RouteId,
    _admin: Admin = Depends(require_permission("ai.routes.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_gateway_service.get_route(db, scope, route_id))


@router.put("/routes/{route_id}", summary="编辑路由")
def update_route(
    body: RouteUpdate,
    route_id: int = RouteId,
    admin: Admin = Depends(require_permission("ai.routes.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_gateway_service.update_route(db, scope, route_id, body.changes(), admin_id=admin.id))


@router.delete("/routes/{route_id}", summary="删除项目覆盖路由")
def delete_route(
    route_id: int = RouteId,
    _admin: Admin = Depends(require_permission("ai.routes.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ai_gateway_service.delete_route(db, scope, route_id)
    return ok(None)


@router.post("/routes/{route_id}/test", summary="一键测试（主 / 备模型各探测一次）")
def test_route(
    body: RouteTestBody | None = None,
    route_id: int = RouteId,
    admin: Admin = Depends(require_permission("ai.routes.test")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    probe_media = bool(body.probe_media) if body is not None else False
    return ok(ai_task_service.test_route(db, scope, route_id, probe_media=probe_media, admin_id=admin.id))


@router.post("/routes/{route_id}/reset-breaker", summary="重置熔断并解除全局暂停")
def reset_breaker(
    route_id: int = RouteId,
    _admin: Admin = Depends(require_permission("ai.routes.reset_breaker")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(ai_gateway_service.reset_route_breakers(db, scope, route_id))


@router.get("/health", summary="AI 网关健康快照")
def health_snapshot(
    _admin: Admin = Depends(require_permission("ai.routes.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = ai_task_service.health_snapshot(db)
    workers: list[dict[str, Any]] = []
    try:
        for name in WORKER_NAMES:
            workers.extend(worker_replicas(name))
    except redis.RedisError as exc:
        logger.warning("读取 worker 心跳失败：%s", exc)
    data["workers"] = workers
    return ok(data)


@router.post("/health/probe", summary="单模型探测")
def probe(
    body: ProbeBody,
    admin: Admin = Depends(require_permission("ai.routes.test")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """探测失败不抛业务码，以 ``status="down"`` + ``error_category`` / ``error_message`` 返回；``model`` 不在目录 → 400。"""
    return ok(
        ai_task_service.probe_model(
            db, capability=body.capability, model=body.model, protocol=body.protocol, probe_media=body.probe_media, admin_id=admin.id,
        )
    )
