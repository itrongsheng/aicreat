"""内容（``/admin/contents``，docs/04 §6.11、§7.6、§7.7、§8；docs/09 §8；docs/10 §4.9；docs/13 §6.3、§7.1、§7.4）。

全部路由声明 ``get_data_scope``：列表按 ``project_id IN P`` 过滤，``/{id}*`` 的目标须可见（否则 404，与不存在相同）；手工创建与生成的
``project_id``、``title_id(s)``、``keyword_id``、``template_id``，以及 ``attach`` 的 ``asset_id`` 须可见。静态子路径 ``generate`` 先于
``/{id}`` 注册（docs/04 §6.0）。生成类接口只建任务并入队，进度经 ``GET /{id}/task``（单内容）或批次详情轮询。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, ContentStatus
from app.schemas.common import PageParams
from app.schemas.content import (
    ApproveBody,
    AttachBody,
    ContentCreateBody,
    ContentGenerateBody,
    ContentUpdateBody,
    ExportFormat,
    GenerateBodyBody,
    GenerateOutlineBody,
    GenerateSeoBody,
    RejectBody,
    RewriteBody,
)
from app.services import content_service
from app.services.data_scope_service import DataScope

router = APIRouter()

ContentId = Path(..., gt=0, description="内容 ID")
VersionId = Path(..., gt=0, description="版本 ID")
AssetId = Path(..., gt=0, description="素材 ID")


# =====================================================================
# 集合级
# =====================================================================


@router.get("", summary="内容列表（不含 body，附 active_task_id）")
def list_contents(
    project_id: int | None = Query(None, gt=0),
    status: ContentStatus | None = Query(None),
    keyword_id: int | None = Query(None, gt=0),
    title_id: int | None = Query(None, gt=0),
    batch_id: int | None = Query(None, gt=0),
    keyword: str | None = Query(None, max_length=200, description="标题搜索"),
    created_by: int | None = Query(None, gt=0),
    has_links: bool | None = Query(None),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = content_service.list_contents(
        db, scope, page=pagination.page, page_size=pagination.page_size, project_id=project_id, status=status,
        keyword_id=keyword_id, title_id=title_id, batch_id=batch_id, keyword=keyword, created_by=created_by, has_links=has_links,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="手工创建内容草稿")
def create_content(
    body: ContentCreateBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.contents.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = content_service.create_content(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"新建内容 {item['title'][:60]}"
    return ok(item)


@router.post("/generate", summary="生成内容（每个已采用标题一篇，批次 kind=content）")
def generate_contents(
    body: ContentGenerateBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.contents.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = content_service.generate_contents(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_summary = (
        f"生成内容：项目 #{body.project_id}，批次 #{data['batch_id']}，标题 {len(data['content_ids'])} 个"
    )
    return ok(data)


# =====================================================================
# 对象级
# =====================================================================


@router.get("/{content_id}", summary="内容详情（含当前版本、素材、active_task_id、pending_tasks）")
def get_content(
    content_id: int = ContentId,
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.content_detail(db, scope, content_id))


@router.put("/{content_id}", summary="人工编辑（新版本 source=manual，current_version_id 冲突检查）")
def update_content(
    body: ContentUpdateBody,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.update_content(db, scope, content_id, body.changes(), admin_id=admin.id))


@router.delete("/{content_id}", summary="删除内容（仅 draft / archived 且无链接）")
def delete_content(
    content_id: int = ContentId,
    _admin: Admin = Depends(require_permission("content.contents.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    content_service.delete_content(db, scope, content_id)
    return ok(None)


@router.get("/{content_id}/task", summary="进行中（或最近一个）内容根任务摘要")
def get_content_task(
    content_id: int = ContentId,
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.content_task(db, scope, content_id))


@router.post("/{content_id}/generate-outline", summary="生成大纲（content_outline，不改状态）")
def generate_outline(
    body: GenerateOutlineBody | None = None,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = (body or GenerateOutlineBody()).model_dump()
    return ok(content_service.generate_outline(db, scope, content_id, payload, admin_id=admin.id))


@router.post("/{content_id}/generate-body", summary="生成正文（content_body，→ generating）")
def generate_body(
    body: GenerateBodyBody | None = None,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = (body or GenerateBodyBody()).model_dump()
    return ok(content_service.generate_body(db, scope, content_id, payload, admin_id=admin.id))


@router.post("/{content_id}/rewrite", summary="重写 / 扩写 / 缩写 / 改风格（content_rewrite）")
def rewrite_content(
    body: RewriteBody,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.rewrite_content(db, scope, content_id, body.model_dump(), admin_id=admin.id))


@router.post("/{content_id}/generate-seo", summary="生成 SEO 要素与 FAQ（content_seo，不改状态）")
def generate_seo(
    body: GenerateSeoBody | None = None,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payload = (body or GenerateSeoBody()).model_dump()
    return ok(content_service.generate_seo(db, scope, content_id, payload, admin_id=admin.id))


@router.post("/{content_id}/submit-review", summary="提审（ready → reviewing；review_required=false 时直接 approved）")
def submit_review(
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.submit_review(db, scope, content_id, admin_id=admin.id))


@router.post("/{content_id}/approve", summary="审核通过（reviewing → approved）")
def approve_content(
    body: ApproveBody | None = None,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.review")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    note = body.note if body is not None else None
    return ok(content_service.approve_content(db, scope, content_id, admin_id=admin.id, note=note))


@router.post("/{content_id}/reject", summary="驳回（reviewing → rejected，note 必填）")
def reject_content(
    body: RejectBody,
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.review")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.reject_content(db, scope, content_id, admin_id=admin.id, note=body.note))


@router.post("/{content_id}/archive", summary="归档（写 prev_status；generating / reviewing 409）")
def archive_content(
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.archive_content(db, scope, content_id, admin_id=admin.id))


@router.post("/{content_id}/unarchive", summary="恢复归档（archived → prev_status）")
def unarchive_content(
    content_id: int = ContentId,
    admin: Admin = Depends(require_permission("content.contents.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.unarchive_content(db, scope, content_id, admin_id=admin.id))


# =====================================================================
# 版本
# =====================================================================


@router.get("/{content_id}/versions", summary="版本列表（不分页；with_body=1 含正文）")
def list_versions(
    content_id: int = ContentId,
    with_body: bool = Query(False),
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.list_versions(db, scope, content_id, with_body=with_body))


@router.get("/{content_id}/versions/{version_id}", summary="版本详情")
def get_version(
    content_id: int = ContentId,
    version_id: int = VersionId,
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.get_version(db, scope, content_id, version_id))


@router.post("/{content_id}/versions/{version_id}/restore", summary="恢复到该版本（新版本 source=restore）")
def restore_version(
    content_id: int = ContentId,
    version_id: int = VersionId,
    admin: Admin = Depends(require_permission("content.contents.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.restore_version(db, scope, content_id, version_id, admin_id=admin.id))


@router.delete("/{content_id}/versions/{version_id}", summary="删除历史版本（当前版本 409）")
def delete_version(
    content_id: int = ContentId,
    version_id: int = VersionId,
    _admin: Admin = Depends(require_permission("content.contents.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    content_service.delete_version(db, scope, content_id, version_id)
    return ok(None)


# =====================================================================
# 素材 / 链接 / 导出
# =====================================================================


@router.get("/{content_id}/assets", summary="绑定素材列表（按 sort）")
def list_assets(
    content_id: int = ContentId,
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.list_assets(db, scope, content_id))


@router.post("/{content_id}/assets/{asset_id}/attach", summary="绑定素材（cover / inline）")
def attach_asset(
    body: AttachBody,
    content_id: int = ContentId,
    asset_id: int = AssetId,
    admin: Admin = Depends(require_permission("content.contents.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(
        content_service.attach_asset(
            db, scope, content_id, asset_id, usage_type=body.usage_type, sort=body.sort, admin_id=admin.id,
        )
    )


@router.post("/{content_id}/assets/{asset_id}/detach", summary="解绑素材（不删除文件）")
def detach_asset(
    content_id: int = ContentId,
    asset_id: int = AssetId,
    admin: Admin = Depends(require_permission("content.contents.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.detach_asset(db, scope, content_id, asset_id, admin_id=admin.id))


@router.get("/{content_id}/links", summary="该内容的回填链接（不分页）")
def list_links(
    content_id: int = ContentId,
    _admin: Admin = Depends(require_permission("content.contents.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(content_service.list_links(db, scope, content_id))


@router.get("/{content_id}/export", summary="导出当前版本（md / html / json）")
def export_content(
    content_id: int = ContentId,
    format: ExportFormat = Query("md"),  # noqa: A002
    _admin: Admin = Depends(require_permission("content.contents.export")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> Response:
    filename, media_type, text = content_service.export_content(db, scope, content_id, format)
    return Response(
        content=text.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
    )
