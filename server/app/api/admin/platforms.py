"""发布平台（``/admin/platforms``，docs/04 §6.16；docs/11 §4.3、§5；docs/13 §4.3、§6.3）。

平台本身不受数据范围约束，但全部路由声明 ``get_data_scope``（规则单一，docs/13 §9.3）：列表 / 详情的 ``link_count`` 只统计可见链接；
``detect`` / ``test`` 不受约束。静态子路径 ``detect`` 先于 ``/{id}`` 注册（docs/04 §6.0）。``detect`` 无副作用、不写审计；
``test`` 实时抓取一次但不写库（审计 ``execute``）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, require_permission
from app.core.response import ok
from app.models import Admin
from app.schemas.platform import PlatformCreate, PlatformUpdate, PlatformUrlBody
from app.services import platform_service
from app.services.data_scope_service import DataScope

router = APIRouter()

PlatformId = Path(..., gt=0, description="平台 ID")


@router.get("", summary="平台列表（不分页，含规则与可见链接数）")
def list_platforms(
    is_active: bool | None = Query(None),
    _admin: Admin = Depends(require_permission("publish.platforms.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(platform_service.list_platforms(db, scope, is_active=is_active))


@router.post("", summary="新建平台")
def create_platform(
    body: PlatformCreate,
    request: Request,
    _admin: Admin = Depends(require_permission("publish.platforms.create")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    values = body.model_dump()
    values["fetch_config"] = body.fetch_config.stored()
    item = platform_service.create_platform(db, scope, values)
    request.state.audit_target_id = str(item["id"])
    request.state.audit_summary = f"新增平台 {item['code']}（{item['name']}）"
    return ok(item)


@router.post("/detect", summary="按 url_patterns 识别平台（无副作用）")
def detect_platform(
    body: PlatformUrlBody,
    request: Request,
    _admin: Admin = Depends(require_permission("publish.platforms.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    del scope
    request.state.audit_written = True                    # 无副作用，不写审计（docs/11 §4.3）
    return ok(platform_service.detect(db, body.url))


@router.get("/{platform_id}", summary="平台详情（含规则 JSON）")
def get_platform(
    platform_id: int = PlatformId,
    _admin: Admin = Depends(require_permission("publish.platforms.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(platform_service.get_platform(db, scope, platform_id))


@router.put("/{platform_id}", summary="编辑平台规则 / 状态（code 不可改）")
def update_platform(
    body: PlatformUpdate,
    request: Request,
    platform_id: int = PlatformId,
    _admin: Admin = Depends(require_permission("publish.platforms.update")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    values = body.model_dump(exclude_unset=True)
    if body.fetch_config is not None:
        values["fetch_config"] = body.fetch_config.stored()
    item = platform_service.update_platform(db, scope, platform_id, values)
    changed = "、".join(sorted(values)) or "无变化"
    request.state.audit_summary = f"编辑平台 {item['code']}：{changed}"
    return ok(item)


@router.delete("/{platform_id}", summary="删除平台（非系统平台且无链接引用）")
def delete_platform(
    platform_id: int = PlatformId,
    _admin: Admin = Depends(require_permission("publish.platforms.delete")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    platform_service.delete_platform(db, scope, platform_id)
    return ok(None)


@router.post("/{platform_id}/test", summary="规则测试：实时抓取一次并判定（不写库）")
def test_platform(
    body: PlatformUrlBody,
    request: Request,
    platform_id: int = PlatformId,
    _admin: Admin = Depends(require_permission("publish.platforms.test")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = platform_service.test_rule(db, scope, platform_id, body.url)
    request.state.audit_summary = (
        f"测试平台 #{platform_id} 规则：{result['result_status']} / {result['matched_rule']}（HTTP {result['http_status']}）"
    )
    return ok(result)
