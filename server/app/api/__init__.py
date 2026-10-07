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
    alerts,
    ai_models,
    ai_routes,
    ai_tasks,
    ai_usage,
    contents,
    generation_batches,
    keywords,
    links,
    media,
    monitoring,
    operation_logs,
    platforms,
    projects,
    prompt_templates,
    settings,
    stats,
    titles,
    uploads,
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
admin.include_router(prompt_templates.router, prefix="/prompt-templates", tags=["content"])
admin.include_router(keywords.router, prefix="/keywords", tags=["content"])
admin.include_router(titles.router, prefix="/titles", tags=["content"])
admin.include_router(contents.router, prefix="/contents", tags=["content"])
admin.include_router(generation_batches.router, prefix="/generation-batches", tags=["content"])
admin.include_router(media.router, prefix="/media", tags=["media"])
admin.include_router(uploads.router, prefix="/uploads", tags=["media"])
admin.include_router(platforms.router, prefix="/platforms", tags=["publish"])
admin.include_router(links.router, prefix="/links", tags=["publish"])
admin.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])
admin.include_router(alerts.router, prefix="/alerts", tags=["monitoring"])
admin.include_router(stats.router, prefix="/stats", tags=["stats"])
# AI 网关：/ai/models、/ai/tasks、/ai/usage 先于 /ai（ai_routes 的 /routes…、/health…）挂载
admin.include_router(ai_models.router, prefix="/ai/models", tags=["ai"])
admin.include_router(ai_tasks.router, prefix="/ai/tasks", tags=["ai"])
admin.include_router(ai_usage.router, prefix="/ai/usage", tags=["ai"])
admin.include_router(ai_routes.router, prefix="/ai", tags=["ai"])
api_router.include_router(admin)
