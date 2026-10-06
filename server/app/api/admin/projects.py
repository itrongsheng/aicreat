"""项目（``/admin/projects``，docs/04 §6.7、docs/13 §6.4、§7.2）。

当前只有负责人候选 ``GET /owner-options``；项目 CRUD、归档、概览与路由覆盖由第 4 步在本文件补充。
``owner-options`` 是静态子路径，必须在 ``GET /{id}`` 之前注册。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, require_permission
from app.core.response import ok
from app.models import Admin
from app.services import data_scope_service
from app.services.data_scope_service import DataScope

router = APIRouter()


@router.get("/owner-options", summary="负责人候选（不分页）")
def owner_options(
    _admin: Admin = Depends(require_permission("content.projects.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(data_scope_service.owner_options(db, scope))
