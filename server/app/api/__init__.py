"""汇总所有路由，挂载到 /api/v1（docs/02 §2.4）。

后续阶段新增资源时在下方 ``admin.include_router(...)`` 追加一行，并在 ``main.AUDIT_TARGET_TYPES`` /
``data_scope_service.SCOPED_ROUTE_PREFIXES`` 同步登记（docs/02 §6.4 触点清单）。
"""

from fastapi import APIRouter

from app.api import health
from app.api.admin import (
    admin_groups,
    admin_permissions,
    admins,
    operation_logs,
    projects,
    settings,
)
from app.api.admin import auth as admin_auth

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])                 # GET /api/v1/health

admin = APIRouter(prefix="/admin")
admin.include_router(admin_auth.router, prefix="/auth", tags=["admin-auth"])
admin.include_router(admins.router, prefix="/admins", tags=["admin-rbac"])
admin.include_router(admin_groups.router, prefix="/admin-groups", tags=["admin-rbac"])
admin.include_router(admin_permissions.router, prefix="/admin-permissions", tags=["admin-rbac"])
admin.include_router(operation_logs.router, prefix="/admin-operation-logs", tags=["admin-rbac"])
admin.include_router(settings.router, prefix="/settings", tags=["system"])
admin.include_router(projects.router, prefix="/projects", tags=["content"])
api_router.include_router(admin)
