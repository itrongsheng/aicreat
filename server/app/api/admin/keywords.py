"""关键词（``/admin/keywords``，docs/04 §6.9、§7.4；docs/09 §6；docs/13 §6.3、§7.1、§7.5）。

全部路由声明 ``get_data_scope``：列表 / 导出按 ``project_id IN P`` 过滤，``/{id}*`` 的目标须可见（否则 404）；生成、导入、
手工新增的 ``project_id`` 须可见。静态子路径 ``generate`` / ``import`` / ``import-file`` / ``export`` / ``batch-status`` 先于
``/{id}`` 注册（docs/04 §6.0）。
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, KeywordIntent, KeywordStatus, KeywordType
from app.schemas.common import PageParams
from app.schemas.keyword import (
    IMPORT_FILE_MAX_BYTES,
    BatchStatusBody,
    KeywordCreateBody,
    KeywordGenerateBody,
    KeywordImportBody,
    KeywordUpdateBody,
)
from app.services import keyword_service
from app.services.data_scope_service import DataScope

router = APIRouter()

KeywordId = Path(..., gt=0, description="关键词 ID")
SortField = Literal["score", "created_at"]
SortOrder = Literal["asc", "desc"]


class KeywordFilters:
    """列表与导出共用的筛选参数（``project_id`` 必填）。"""

    def __init__(
        self,
        project_id: int = Query(..., gt=0),
        status: KeywordStatus | None = Query(None),
        intent: KeywordIntent | None = Query(None),
        keyword_type: KeywordType | None = Query(None),
        keyword: str | None = Query(None, max_length=120, description="关键词模糊匹配"),
        batch_id: int | None = Query(None, gt=0),
        sort: SortField | None = Query(None, description="score / created_at；缺省 created_at DESC, id DESC"),
        order: SortOrder = Query("desc"),
    ) -> None:
        self.values: dict[str, Any] = {
            "project_id": project_id, "status": status, "intent": intent, "keyword_type": keyword_type, "keyword": keyword,
            "batch_id": batch_id, "sort": sort, "order": order,
        }


@router.get("", summary="关键词列表")
def list_keywords(
    filters: KeywordFilters = Depends(),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("content.keywords.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = keyword_service.list_keywords(db, scope, page=pagination.page, page_size=pagination.page_size, **filters.values)
    return paginated(items, total, pagination.page, pagination.page_size)


@router.post("", summary="手工新增关键词")
def create_keyword(
    body: KeywordCreateBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.keywords.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    item = keyword_service.create_keyword(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"新增关键词 {item['keyword']}"
    return ok(item)


@router.post("/generate", summary="生成关键词（建批次并入队）")
def generate_keywords(
    body: KeywordGenerateBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.keywords.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = keyword_service.generate_keywords(db, scope, body.model_dump(), admin_id=admin.id)
    request.state.audit_summary = f"生成关键词：项目 #{body.project_id}，批次 #{data['batch_id']}，数量 {body.count}"
    return ok(data)


@router.post("/import", summary="导入关键词（JSON）")
def import_keywords(
    body: KeywordImportBody,
    request: Request,
    admin: Admin = Depends(require_permission("content.keywords.import")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = keyword_service.import_keywords(
        db, scope, body.project_id, [item.model_dump() for item in body.items], admin_id=admin.id
    )
    request.state.audit_summary = f"导入关键词：项目 #{body.project_id}，新增 {result['created']}，跳过 {result['skipped']}，错误 {len(result['errors'])}"
    return ok(result)


@router.post("/import-file", summary="导入关键词（CSV 文件）")
def import_keywords_file(
    request: Request,
    file: UploadFile = File(..., description="UTF-8 CSV，首行表头 keyword,intent,keyword_type，≤ 2 MB、≤ 5,000 数据行"),
    project_id: int = Form(..., gt=0),
    admin: Admin = Depends(require_permission("content.keywords.import")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = file.file.read(IMPORT_FILE_MAX_BYTES + 1)
    result = keyword_service.import_keywords_csv(db, scope, project_id, data, admin_id=admin.id)
    request.state.audit_summary = (
        f"导入关键词文件 {file.filename or ''}：项目 #{project_id}，新增 {result['created']}，跳过 {result['skipped']}，错误 {len(result['errors'])}"
    )
    return ok(result)


@router.get("/export", summary="导出关键词 CSV")
def export_keywords(
    filters: KeywordFilters = Depends(),
    format: str = Query("csv", pattern="^csv$"),
    _admin: Admin = Depends(require_permission("content.keywords.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> Response:
    del format
    filename, content = keyword_service.export_keywords(db, scope, **filters.values)
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/batch-status", summary="批量采用 / 弃用 / 恢复")
def batch_status(
    body: BatchStatusBody,
    admin: Admin = Depends(require_permission("content.keywords.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(keyword_service.batch_status(db, scope, body.ids, body.action, admin_id=admin.id))


@router.get("/{keyword_id}", summary="关键词详情")
def get_keyword(
    keyword_id: int = KeywordId,
    _admin: Admin = Depends(require_permission("content.keywords.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(keyword_service.get_keyword(db, scope, keyword_id))


@router.put("/{keyword_id}", summary="编辑关键词")
def update_keyword(
    body: KeywordUpdateBody,
    keyword_id: int = KeywordId,
    admin: Admin = Depends(require_permission("content.keywords.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(keyword_service.update_keyword(db, scope, keyword_id, body.changes(), admin_id=admin.id))


@router.delete("/{keyword_id}", summary="删除关键词（仅无标题 / 内容关联）")
def delete_keyword(
    keyword_id: int = KeywordId,
    _admin: Admin = Depends(require_permission("content.keywords.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    keyword_service.delete_keyword(db, scope, keyword_id)
    return ok(None)


@router.post("/{keyword_id}/adopt", summary="采用关键词")
def adopt_keyword(
    keyword_id: int = KeywordId,
    admin: Admin = Depends(require_permission("content.keywords.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(keyword_service.adopt_keyword(db, scope, keyword_id, admin_id=admin.id))


@router.post("/{keyword_id}/discard", summary="弃用关键词（级联弃用候选标题）")
def discard_keyword(
    keyword_id: int = KeywordId,
    admin: Admin = Depends(require_permission("content.keywords.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(keyword_service.discard_keyword(db, scope, keyword_id, admin_id=admin.id))


@router.post("/{keyword_id}/restore", summary="恢复关键词（discarded → candidate）")
def restore_keyword(
    keyword_id: int = KeywordId,
    admin: Admin = Depends(require_permission("content.keywords.status")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(keyword_service.restore_keyword(db, scope, keyword_id, admin_id=admin.id))
