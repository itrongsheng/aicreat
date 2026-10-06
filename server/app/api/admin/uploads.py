"""上传参考素材（``/admin/uploads``，docs/04 §6.14；docs/10 §6.5、§11.1；docs/13 §7.4、§12.2）。

multipart 只接收 ``file`` 字段（无 ``project_id`` 表单字段）：扩展名与魔数双重校验、大小 ≤ ``MAX_IMAGE_SIZE_MB`` /
``MAX_VIDEO_SIZE_MB``，落 ``media_assets(source=uploaded, usage_type=reference, status=ready, project_id=NULL)``，按上传人归属，
只对上传人与 ``all`` 范围可见。返回 ``{asset_id, url, public}``（``public=false`` 表示真实模式下 zhiqiapi 无法读取该地址；
Mock 模式恒为 ``true``）。审计 ``target_type=media_asset``、``action=create``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, require_permission
from app.core.response import ok
from app.models import Admin
from app.services import media_service
from app.services.data_scope_service import DataScope

router = APIRouter()


def _upload(request: Request, db: Session, scope: DataScope, admin: Admin, kind: str, file: UploadFile) -> dict[str, Any]:
    try:
        data = media_service.upload_asset(db, scope, kind, file.file, file.filename, admin_id=admin.id)
    finally:
        file.file.close()
    label = "参考图" if kind == "image" else "参考视频"
    request.state.audit_target_id = str(data["asset_id"])
    request.state.audit_summary = f"上传{label} #{data['asset_id']}：{(file.filename or '')[:100]}"
    return ok(data)


@router.post("/image", summary="上传参考图（jpg/png/webp/gif ≤ MAX_IMAGE_SIZE_MB）")
def upload_image(
    request: Request,
    file: UploadFile = File(..., description="参考图：jpg/png/webp/gif，≤ MAX_IMAGE_SIZE_MB（默认 10）"),
    admin: Admin = Depends(require_permission("system.upload.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _upload(request, db, scope, admin, "image", file)


@router.post("/video", summary="上传参考视频（mp4/mov ≤ MAX_VIDEO_SIZE_MB）")
def upload_video(
    request: Request,
    file: UploadFile = File(..., description="参考视频：mp4/mov，≤ MAX_VIDEO_SIZE_MB（默认 200）"),
    admin: Admin = Depends(require_permission("system.upload.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _upload(request, db, scope, admin, "video", file)
