"""回填链接（``/admin/links``，docs/04 §6.17、§7.10~§7.12；docs/11 §4、§6.7、§6.8；docs/13 §6.3、§7.5、§8）。

全部路由声明 ``get_data_scope``：列表 / 导出按 ``project_id IN P`` 过滤，``/{id}*`` 的目标须可见（否则 404）；回填的 ``content_id``
须可见；``url_hash`` 命中不可见链接时 409 ``owned_by_other``（不暴露对方链接 ID）。静态子路径 ``export`` / ``batch`` 先于 ``/{id}``
注册（docs/04 §6.0）。收录检测：``POST /{id}/index-check``（手动入队，频控 ``30/hour``）与 ``POST /{id}/mark-index``（人工标记，
API 事务内直接写 ``index_checks`` 与回写，不入队）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, IndexKind
from app.schemas.common import PageParams
from app.schemas.link import (
    LinkBatchCreate,
    LinkCreate,
    LinkFilters,
    LinkIndexCheckBody,
    LinkMarkIndexBody,
    LinkUpdate,
)
from app.services import index_check_service, link_service
from app.services.data_scope_service import DataScope

router = APIRouter()

LinkId = Path(..., gt=0, description="链接 ID")


def _filters(filters: LinkFilters) -> dict[str, Any]:
    return filters.model_dump()


# =====================================================================
# 集合级
# =====================================================================


@router.get("", summary="链接列表")
def list_links(
    filters: LinkFilters = Depends(),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("publish.links.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = link_service.list_links(db, scope, page=pagination.page, page_size=pagination.page_size, **_filters(filters))
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="回填链接（单条），入队基线检测")
def create_link(
    body: LinkCreate,
    request: Request,
    admin: Admin = Depends(require_permission("publish.links.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = link_service.backfill(db, scope, body, admin.id)
    link = data["link"]
    request.state.audit_target_id = str(link["id"])
    request.state.audit_summary = f"回填链接 #{link['id']}：内容 #{link['content_id']}，平台 #{link['platform_id']}，{link['domain']}"
    return ok(data)


@router.get("/export", summary="导出链接 CSV（同列表筛选）")
def export_links(
    filters: LinkFilters = Depends(),
    format: str = Query("csv", pattern="^csv$"),
    _admin: Admin = Depends(require_permission("publish.links.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> Response:
    del format
    filename, content = link_service.export_links(db, scope, **_filters(filters))
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/batch", summary="批量回填（≤ 100 条，逐条独立事务）")
def batch_create_links(
    body: LinkBatchCreate,
    request: Request,
    admin: Admin = Depends(require_permission("publish.links.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = link_service.batch_backfill(db, scope, body.items, admin.id)
    created_ids = [str(r["link_id"]) for r in data["results"] if r["ok"]]
    request.state.audit_summary = (
        f"批量回填链接：成功 {data['created']}，失败 {data['failed']}" + (f"（#{', #'.join(created_ids[:20])}）" if created_ids else "")
    )
    return ok(data)


# =====================================================================
# 对象级
# =====================================================================


@router.get("/{link_id}", summary="链接详情（含 platform、content 摘要、基线、收录状态、last_check）")
def get_link(
    link_id: int = LinkId,
    _admin: Admin = Depends(require_permission("publish.links.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(link_service.get_link(db, scope, link_id))


@router.put("/{link_id}", summary="编辑链接（URL 不可改）")
def update_link(
    body: LinkUpdate,
    request: Request,
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    item = link_service.update_link(db, scope, link_id, values, admin_id=admin.id)
    request.state.audit_summary = f"编辑链接 #{link_id}：{'、'.join(sorted(values)) or '无变化'}"
    return ok(item)


@router.delete("/{link_id}", summary="删除链接（级联检测记录，更新内容计数与状态，自动解决告警）")
def delete_link(
    request: Request,
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = link_service.delete_link(db, scope, link_id, admin_id=admin.id)
    summary = f"删除链接 #{link_id}（内容 #{result['content_id']} → {result['content_status']}）"
    if result["alerts_resolved"]:
        summary += f"；因链接删除而解决告警 {result['alerts_resolved']} 条"
    request.state.audit_summary = summary
    return ok(None)


@router.post("/{link_id}/check", summary="立即删除检测（manual 插队，不受日上限拦截）")
def check_link(
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.check")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(link_service.check_now(db, scope, link_id, admin_id=admin.id))


@router.post("/{link_id}/index-check", summary="立即收录检测（manual 插队，频控 30/hour，入队前预扣日上限）")
def index_check_link(
    body: LinkIndexCheckBody,
    request: Request,
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.check")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = index_check_service.request_index_check(db, scope, link_id, body.kinds, body.engines, admin_id=admin.id)
    engines = "、".join(body.engines) if body.engines else "全部启用引擎"
    request.state.audit_summary = (
        f"收录检测链接 #{link_id}（{'/'.join(body.kinds)}：{engines}）："
        + ("已入队" if data.get("queued") else f"未入队（{data.get('reason')}）")
    )
    return ok(data)


@router.post("/{link_id}/mark-index", summary="人工标记收录 / 引用（写 index_checks，不改排程）")
def mark_index_link(
    body: LinkMarkIndexBody,
    request: Request,
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.mark")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = index_check_service.mark_index(db, scope, link_id, body.model_dump(), admin_id=admin.id)
    request.state.audit_summary = f"人工标记链接 #{link_id}：{body.kind}/{body.engine} → {body.status}"
    return ok(item)


@router.post("/{link_id}/rebaseline", summary="清空基线并入队 manual 检测")
def rebaseline_link(
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.check")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(link_service.rebaseline(db, scope, link_id, admin_id=admin.id))


@router.post("/{link_id}/pause", summary="暂停监控")
def pause_link(
    link_id: int = LinkId,
    _admin: Admin = Depends(require_permission("publish.links.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(link_service.pause(db, scope, link_id))


@router.post("/{link_id}/resume", summary="恢复监控并重算排程")
def resume_link(
    link_id: int = LinkId,
    admin: Admin = Depends(require_permission("publish.links.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(link_service.resume(db, scope, link_id, admin_id=admin.id))


@router.get("/{link_id}/checks", summary="删除检测历史（按 checked_at 倒序）")
def list_link_checks(
    link_id: int = LinkId,
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("publish.links.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = link_service.list_checks(db, scope, link_id, page=pagination.page, page_size=pagination.page_size)
    return paginated(items, total, pagination.page, pagination.page_size)


@router.get("/{link_id}/index-checks", summary="收录检测历史（kind / engine 筛选）")
def list_link_index_checks(
    link_id: int = LinkId,
    kind: IndexKind | None = Query(None),
    engine: str | None = Query(None, max_length=32),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("publish.links.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = link_service.list_index_checks(
        db, scope, link_id, kind=kind, engine=engine, page=pagination.page, page_size=pagination.page_size
    )
    return paginated(items, total, pagination.page, pagination.page_size)
