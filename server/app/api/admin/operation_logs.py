"""操作日志（``/admin/admin-operation-logs``，docs/07 §6.4、docs/13 §6.3）：只读分页列表。

``own`` 范围只返回本人记录（``admin_id`` 参数被忽略）；``owner_id`` 查询参数对本接口不生效（总后台按用户筛选用 ``admin_id``）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_data_scope, get_db, get_pagination, require_permission
from app.core.response import paginated
from app.models import Admin, OperationAction
from app.schemas.common import PageParams
from app.services import admin_rbac_service
from app.services.data_scope_service import DataScope

router = APIRouter()


@router.get("", summary="操作日志列表")
def list_operation_logs(
    admin_id: int | None = Query(None, gt=0, description="操作用户 ID（own 范围忽略）"),
    module: str | None = Query(None, pattern=r"^[a-z_]{1,50}$", description="模块：permission_code 前缀，如 content / auth"),
    action: OperationAction | None = Query(None),
    target_type: str | None = Query(None, max_length=50),
    start: datetime | None = Query(None, description="ISO 8601 UTC，含"),
    end: datetime | None = Query(None, description="ISO 8601 UTC，含"),
    pagination: PageParams = Depends(get_pagination),
    _admin: Admin = Depends(require_permission("security.audit.view")),
    scope: DataScope = Depends(get_data_scope),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    items, total = admin_rbac_service.list_operation_logs(
        db, scope, admin_id=admin_id, module=module, action=action, target_type=target_type, start=start, end=end,
        page=pagination.page, page_size=pagination.page_size,
    )
    return paginated(items, total, pagination.page, pagination.page_size)
