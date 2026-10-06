"""媒体素材与生成（``/admin/media``，docs/04 §6.13、§7.8、§7.9、§8；docs/10 §4、§5、§6、§8；docs/13 §4.2、§6.1、§6.3、§7.1）。

全部路由声明 ``get_data_scope``：列表按 ``project_id IN P OR (project_id IS NULL AND created_by = :me)`` 过滤；``/assets/{id}*`` 的
目标须可见（否则 404，与不存在相同）；生成接口的 ``project_id`` / ``content_id`` 须可见。静态子路径 ``images/generate`` /
``videos/generate`` 先于 ``/assets/{id}`` 注册（docs/04 §6.0）。生成只做本地校验并入队，进度经 ``GET /assets/{id}/task`` 轮询；
``retry`` 可能同步复查旧上游任务（5021），``transfer`` 只写 ``next_transfer_at=now``，均不在 API 进程下载。

审计（``main.AUDIT_TARGET_TYPES``：``/admin/media`` → ``media_asset``）：``generate`` / ``retry`` / ``transfer`` 记 ``execute``，删除记
``delete``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import ok, paginated
from app.models import Admin, MediaKind, MediaSource, MediaStatus, MediaUsageType
from app.schemas.common import PageParams
from app.schemas.media import ImageGenerateBody, VideoGenerateBody
from app.services import media_service
from app.services.data_scope_service import DataScope

router = APIRouter()

AssetId = Path(..., gt=0, description="素材 ID")


def _ids_text(ids: list[int]) -> str:
    return "、".join(f"#{i}" for i in ids)


# =====================================================================
# 生成（静态子路径先注册）
# =====================================================================


@router.post("/images/generate", summary="生成图片（每张一个资产与根任务 image_generate）")
def generate_images(
    body: ImageGenerateBody,
    request: Request,
    admin: Admin = Depends(require_permission("media.images.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = media_service.generate_images(db, scope, body, admin_id=admin.id)
    asset_ids = list(data.get("asset_ids") or [])
    if len(asset_ids) == 1:
        request.state.audit_target_id = str(asset_ids[0])
    request.state.audit_summary = (
        f"生成图片：项目 #{body.project_id}，{body.usage_type}，素材 {_ids_text(asset_ids)}"
        + (f"，模型 {body.model}" if body.model else "")
    )
    return ok(data)


@router.post("/videos/generate", summary="生成视频（一个资产与根任务 video_generate）")
def generate_video(
    body: VideoGenerateBody,
    request: Request,
    admin: Admin = Depends(require_permission("media.videos.generate")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = media_service.generate_video(db, scope, body, admin_id=admin.id)
    request.state.audit_target_id = str(data["asset_id"])
    request.state.audit_summary = (
        f"生成视频：项目 #{body.project_id}，{body.usage_type}，素材 #{data['asset_id']}"
        + (f"，模型 {body.model}" if body.model else "")
    )
    return ok(data)


# =====================================================================
# 素材库
# =====================================================================


@router.get("/assets", summary="素材列表（按数据范围过滤，不计算 references）")
def list_assets(
    project_id: int | None = Query(None, gt=0),
    content_id: int | None = Query(None, gt=0),
    kind: MediaKind | None = Query(None),
    status: MediaStatus | None = Query(None),
    usage_type: MediaUsageType | None = Query(None),
    source: MediaSource | None = Query(None),
    created_by: int | None = Query(None, gt=0),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("media.assets.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = media_service.list_assets(
        db, scope, page=pagination.page, page_size=pagination.page_size, project_id=project_id, content_id=content_id,
        kind=kind, status=status, usage_type=usage_type, source=source, created_by=created_by,
    )
    return paginated(items, total, pagination.page, pagination.page_size)


@router.get("/assets/{asset_id}", summary="素材详情（含 task、params、reference_asset_ids、references）")
def get_asset(
    asset_id: int = AssetId,
    _admin: Admin = Depends(require_permission("media.assets.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(media_service.get_asset_detail(db, scope, asset_id))


@router.get("/assets/{asset_id}/task", summary="当前根任务摘要（前端轮询；上传素材无任务为 null）")
def get_asset_task(
    asset_id: int = AssetId,
    _admin: Admin = Depends(require_permission("media.assets.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(media_service.get_asset_task(db, scope, asset_id))


@router.post("/assets/{asset_id}/retry", summary="失败 / 过期重试（先复查旧上游任务）")
def retry_asset(
    request: Request,
    asset_id: int = AssetId,
    admin: Admin = Depends(require_permission("media.assets.retry")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = media_service.retry_asset(db, scope, asset_id, admin_id=admin.id)
    request.state.audit_summary = (
        f"重试素材 #{asset_id}：新根任务 #{data['task_id']}" + ("（复用旧上游任务）" if data.get("resumed") else "（重新提交）")
    )
    return ok(data)


@router.post("/assets/{asset_id}/transfer", summary="重新转存（failed(transfer_failed/timeout) 且有 upstream_url）")
def transfer_asset(
    request: Request,
    asset_id: int = AssetId,
    _admin: Admin = Depends(require_permission("media.assets.retry")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = media_service.request_transfer(db, scope, asset_id)
    request.state.audit_summary = f"重新转存素材 #{asset_id}"
    return ok(data)


@router.delete("/assets/{asset_id}", summary="删除素材与文件（仅 ready / failed / expired）")
def delete_asset(
    request: Request,
    asset_id: int = AssetId,
    _admin: Admin = Depends(require_permission("media.assets.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    media_service.delete_asset(db, scope, asset_id)
    request.state.audit_summary = f"删除素材 #{asset_id}"
    return ok(None)
