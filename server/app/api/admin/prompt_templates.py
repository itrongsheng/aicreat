"""Prompt 模板（``/admin/prompt-templates``，docs/04 §6.8；docs/09 §5；docs/13 §7.3）。

全部路由声明 ``get_data_scope``：全局草稿只对创建人与总后台可见，项目模板随项目负责人可见；不可见与不存在一律 404。
``preview`` 无副作用，处理函数设置 ``audit_written=True`` 跳过审计（docs/07 §7.6）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, PromptKind, PromptStatus
from app.schemas.common import PageParams
from app.schemas.prompt_template import TemplateCreate, TemplateDuplicate, TemplatePreview, TemplateUpdate
from app.services import prompt_template_service
from app.services.data_scope_service import DataScope

router = APIRouter()

TemplateId = Path(..., gt=0, description="模板 ID")


@router.get("", summary="模板列表（默认每个 code 只返回最新可见版本）")
def list_templates(
    kind: PromptKind | None = Query(None),
    status: PromptStatus | None = Query(None),
    project_id: int | None = Query(None, ge=0, description="0 = 全局模板；>0 = 该项目模板"),
    include_global: bool = Query(False, description="project_id>0 时同时返回全局模板（默认模板下拉用）"),
    keyword: str | None = Query(None, max_length=100, description="code / 名称模糊匹配"),
    all_versions: bool = Query(False, description="1 = 返回全部可见版本"),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("content.prompt_templates.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = prompt_template_service.list_templates(
        db, scope, page=pagination.page, page_size=pagination.page_size, kind=kind, status=status, project_id=project_id,
        include_global=include_global, keyword=keyword, all_versions=all_versions,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="新建模板草稿")
def create_template(
    body: TemplateCreate,
    request: Request,
    admin: Admin = Depends(require_permission("content.prompt_templates.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = prompt_template_service.create_template(db, scope, body.values(), admin_id=admin.id)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"新建 Prompt 模板 {item['code']} v{item['version']}"
    return ok(item)


@router.get("/{template_id}", summary="模板详情")
def get_template(
    template_id: int = TemplateId,
    _admin: Admin = Depends(require_permission("content.prompt_templates.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(prompt_template_service.get_template(db, scope, template_id))


@router.put("/{template_id}", summary="编辑模板（已发布模板复制为新版本草稿）")
def update_template(
    body: TemplateUpdate,
    request: Request,
    template_id: int = TemplateId,
    admin: Admin = Depends(require_permission("content.prompt_templates.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = prompt_template_service.update_template(db, scope, template_id, body.changes(), admin_id=admin.id)
    if item["id"] != template_id:
        request.state.audit_summary = f"基于模板 #{template_id} 创建 {item['code']} v{item['version']} 草稿 #{item['id']}"
    return ok(item)


@router.delete("/{template_id}", summary="删除草稿模板")
def delete_template(
    template_id: int = TemplateId,
    _admin: Admin = Depends(require_permission("content.prompt_templates.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    prompt_template_service.delete_template(db, scope, template_id)
    return ok(None)


@router.post("/{template_id}/publish", summary="发布（同 code 旧版本自动归档）")
def publish_template(
    request: Request,
    template_id: int = TemplateId,
    admin: Admin = Depends(require_permission("content.prompt_templates.publish")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = prompt_template_service.publish_template(db, scope, template_id, admin_id=admin.id)
    request.state.audit_summary = f"发布 Prompt 模板 {item['code']} v{item['version']}"
    return ok(item)


@router.post("/{template_id}/archive", summary="归档已发布模板")
def archive_template(
    request: Request,
    template_id: int = TemplateId,
    admin: Admin = Depends(require_permission("content.prompt_templates.publish")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = prompt_template_service.archive_template(db, scope, template_id, admin_id=admin.id)
    request.state.audit_summary = f"归档 Prompt 模板 {item['code']} v{item['version']}"
    return ok(item)


@router.post("/{template_id}/duplicate", summary="复制为新 code 草稿")
def duplicate_template(
    body: TemplateDuplicate,
    request: Request,
    template_id: int = TemplateId,
    admin: Admin = Depends(require_permission("content.prompt_templates.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = prompt_template_service.duplicate_template(db, scope, template_id, body.model_dump(), admin_id=admin.id)
    request.state.audit_summary = f"复制 Prompt 模板 #{template_id} 为 {item['code']}（#{item['id']}）"
    return ok(item)


@router.post("/{template_id}/preview", summary="预览渲染结果（不调用模型）")
def preview_template(
    request: Request,
    body: TemplatePreview | None = None,
    template_id: int = TemplateId,
    _admin: Admin = Depends(require_permission("content.prompt_templates.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    request.state.audit_written = True                    # 无副作用，不写审计
    variables = body.variables if body is not None else {}
    return ok(prompt_template_service.preview_template(db, scope, template_id, variables))


@router.get("/{template_id}/versions", summary="同 code 的全部可见版本（不分页）")
def list_versions(
    template_id: int = TemplateId,
    _admin: Admin = Depends(require_permission("content.prompt_templates.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(prompt_template_service.list_versions(db, scope, template_id))
