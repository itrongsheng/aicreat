"""FastAPI 应用装配（docs/01 §4.2、§4.6；docs/07 §7.6、§7.7）。

``create_app()``：挂载 ``/api/v1``（``api_router``）与 ``GET /media/{key}``、CORS、``register_exception_handlers``、
请求 ID 中间件与审计中间件；lifespan 启动时在 ``lock:bootstrap`` 内执行 ``ensure_rbac_seed`` →
``ensure_default_settings`` → ``ensure_default_routes``（全部幂等），并把 Mock 占位素材复制到本地存储。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import redis
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.routing import APIRoute

from app.api import api_router
from app.api.health import app_version, check_public_base_url
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import CODE_BAD_REQUEST, CODE_NOT_FOUND, BusinessError, register_exception_handlers, unhandled_exception_handler
from app.core.locks import LockTimeout, with_lock
from app.core.storage import InvalidStorageKey, LocalStorage, get_storage, guess_content_type, validate_storage_key
from app.models import Admin
from app.services import admin_rbac_service, settings_service

logger = logging.getLogger("app.main")

API_PREFIX = "/api/v1"
ADMIN_PATH_PREFIX = f"{API_PREFIX}/admin/"
BOOTSTRAP_LOCK_KEY = "lock:bootstrap"
BOOTSTRAP_LOCK_TTL = 60
BOOTSTRAP_LOCK_WAIT = 30
REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# =====================================================================
# 审计映射（docs/07 §7.6）
# =====================================================================

ACTION_LABELS: dict[str, str] = {
    "create": "新增",
    "update": "更新",
    "update_status": "变更状态",
    "delete": "删除",
    "execute": "执行",
    "reset_password": "重置密码",
    "login": "登录",
    "logout": "登出",
}

STATUS_ACTION_WORDS = frozenset({
    "adopt", "discard", "restore", "archive", "unarchive", "approve", "reject", "submit-review", "pause", "resume",
    "acknowledge", "resolve", "ignore", "status", "batch-status", "batch-resolve",
})
EXECUTE_ACTION_WORDS = frozenset({
    "generate", "generate-outline", "generate-body", "generate-seo", "rewrite", "import", "import-file", "batch", "sync",
    "test", "probe", "reconcile", "run", "recompute", "check", "index-check", "rebaseline", "mark-index", "retry", "cancel",
    "transfer", "attach", "detach", "duplicate", "publish", "score", "reset-breaker",
})
PASSWORD_ACTION_WORDS = frozenset({"reset-password", "change-password"})
SESSION_ACTION_WORDS = {"login": "login", "logout": "logout"}
# 无副作用、由处理函数设置 audit_written=True 跳过审计的动作（docs/07 §7.6）；映射为 execute 仅为规则完整
NO_SIDE_EFFECT_ACTION_WORDS = frozenset({"preview", "detect"})

# 路由前缀（去掉 /api/v1，路径参数统一写作 {id}）→ target_type；最长前缀优先
AUDIT_TARGET_TYPES: dict[str, str] = {
    "/admin/projects": "project",
    "/admin/prompt-templates": "prompt_template",
    "/admin/keywords": "keyword",
    "/admin/titles": "title",
    "/admin/contents/{id}/versions": "content_version",
    "/admin/contents": "content",
    "/admin/generation-batches": "generation_batch",
    "/admin/media": "media_asset",
    "/admin/uploads": "media_asset",
    "/admin/ai/routes": "capability_route",
    "/admin/ai/tasks": "ai_task",
    "/admin/ai/models": "ai_model",
    "/admin/ai/health": "ai_model",
    "/admin/ai/usage": "ai_usage_log",
    "/admin/platforms": "publish_platform",
    "/admin/links": "publish_link",
    "/admin/monitoring": "publish_link",
    "/admin/alerts": "alert",
    "/admin/stats": "daily_stats",
    "/admin/settings": "setting",
    "/admin/admins": "admin",
    "/admin/admin-groups": "admin_group",
    "/admin/auth": "admin",
}
# 这些前缀的 target_id 固定为 NULL；/admin/auth 的 target_id 为当前管理员
AUDIT_NULL_TARGET_PREFIXES = frozenset({"/admin/ai/health", "/admin/ai/usage", "/admin/monitoring", "/admin/stats"})
AUDIT_SELF_TARGET_PREFIXES = frozenset({"/admin/auth"})

_PARAM_RE = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")


def _strip_api_prefix(route_path: str) -> str:
    return route_path[len(API_PREFIX):] if route_path.startswith(API_PREFIX + "/") else route_path


def _normalize_template(route_path: str) -> str:
    """去掉 ``/api/v1`` 前缀并把路径参数统一为 ``{id}``（``/admin/contents/{content_id}/versions/{version_id}`` → …``{id}``）。"""
    return _PARAM_RE.sub("{id}", _strip_api_prefix(route_path)).rstrip("/") or "/"


def _path_params(route_path: str) -> list[str]:
    return _PARAM_RE.findall(route_path)


def _matches_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _target_prefix(route_path: str) -> str | None:
    path = _normalize_template(route_path)
    matched = [prefix for prefix in AUDIT_TARGET_TYPES if _matches_prefix(path, prefix)]
    return max(matched, key=len) if matched else None


def resolve_action_with_rule(method: str, route_path: str) -> tuple[str, str]:
    """``(action, rule)``：``rule`` 为命中的规则名，``"fallback"`` 表示落入兜底（带路径参数且末段不在任何动作词表）。

    判定顺序：PUT → DELETE → PATCH → ``/versions/{id}/restore`` → 改密 / 重置密码 → 登录 / 登出 → 状态动作词 →
    执行动作词（含无副作用的 preview / detect）→ 无路径参数的 POST 记 create → 兜底 execute。
    """
    method = method.upper()
    path = _normalize_template(route_path)
    if method == "PUT":
        return "update", "put"
    if method == "DELETE":
        return "delete", "delete"
    if method == "PATCH":
        return "update_status", "patch"
    if path.endswith("/versions/{id}/restore"):
        return "execute", "version_restore"
    last = path.rsplit("/", 1)[-1]
    if last in PASSWORD_ACTION_WORDS:
        return "reset_password", "password"
    if last in SESSION_ACTION_WORDS:
        return SESSION_ACTION_WORDS[last], "session"
    if last in STATUS_ACTION_WORDS:
        return "update_status", "status_word"
    if last in EXECUTE_ACTION_WORDS or last in NO_SIDE_EFFECT_ACTION_WORDS:
        return "execute", "execute_word"
    if not _path_params(route_path):
        return "create", "collection_post"
    return "execute", "fallback"


def resolve_action(method: str, route_path: str) -> str:
    """按「方法 + 路由模板」映射审计动作（docs/07 §7.6）。"""
    return resolve_action_with_rule(method, route_path)[0]


def resolve_target_type(route_path: str) -> str | None:
    """``AUDIT_TARGET_TYPES`` 最长前缀优先（匹配前去掉 ``/api/v1``）；未登记返回 ``None``。"""
    prefix = _target_prefix(route_path)
    return AUDIT_TARGET_TYPES[prefix] if prefix else None


def resolve_target_id(route_path: str, path_params: dict[str, Any], admin_id: int | None = None) -> str | None:
    """路由模板中最后一个路径参数的值；部分前缀固定为 NULL，``/admin/auth`` 为当前管理员。"""
    prefix = _target_prefix(route_path)
    if prefix in AUDIT_NULL_TARGET_PREFIXES:
        return None
    if prefix in AUDIT_SELF_TARGET_PREFIXES:
        return str(admin_id) if admin_id is not None else None
    names = _path_params(route_path)
    for name in reversed(names):
        if name in path_params and path_params[name] is not None:
            return str(path_params[name])
    return None


def default_summary(action: str, target_type: str, target_id: str | int | None) -> str:
    return f"{ACTION_LABELS.get(action, action)} {target_type}" + (f" #{target_id}" if target_id else "")


# =====================================================================
# 路由模板
# =====================================================================


def iter_api_routes(app: FastAPI) -> Iterator[tuple[str, set[str], APIRoute]]:
    """展开（含 include_router 前缀的）全部 ``APIRoute``：``(完整路径模板, methods, route)``。"""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # 旧版 FastAPI：include_router 时已复制为带前缀的路由
        for route in app.routes:
            if isinstance(route, APIRoute):
                yield route.path_format, set(route.methods or ()), route
        return
    for context in iter_route_contexts(app.routes):
        original = context.original_route
        if isinstance(original, APIRoute):
            yield context.path_format or original.path_format, set(context.methods or original.methods or ()), original


def _route_template(app: FastAPI, request: Request) -> str | None:
    """当前请求命中的完整路由模板（如 ``/api/v1/admin/keywords/{keyword_id}/adopt``）。"""
    route = request.scope.get("route")
    if route is None:
        return None
    context = request.scope.get("fastapi", {}).get("effective_route_context") if isinstance(request.scope.get("fastapi"), dict) else None
    if context is not None and getattr(context, "path_format", None):
        return context.path_format
    templates: dict[int, str] | None = getattr(app.state, "route_templates", None)
    if templates is None or id(route) not in templates:
        templates = {}
        for path, _methods, api_route in iter_api_routes(app):
            templates.setdefault(id(api_route), path)
        app.state.route_templates = templates
    return templates.get(id(route)) or getattr(route, "path_format", None) or getattr(route, "path", None)


# =====================================================================
# 启动引导
# =====================================================================


def run_ensure_steps() -> None:
    """``ensure_rbac_seed`` → ``ensure_default_settings`` → ``ensure_default_routes``（第 3 步实现后自动启用）。"""
    with SessionLocal() as db:
        admin_rbac_service.ensure_rbac_seed(db)
        settings_service.ensure_default_settings(db)
        if importlib.util.find_spec("app.services.ai_gateway_service") is not None:
            gateway = importlib.import_module("app.services.ai_gateway_service")
            ensure_routes = getattr(gateway, "ensure_default_routes", None)
            if callable(ensure_routes):
                ensure_routes(db)


def bootstrap() -> None:
    """在 ``lock:bootstrap``（TTL 60s，最多等待 30s）内执行 ensure_*；等锁超时直接继续（持锁进程已完成同样的幂等写入）。"""
    try:
        with with_lock(BOOTSTRAP_LOCK_KEY, BOOTSTRAP_LOCK_TTL, wait_seconds=BOOTSTRAP_LOCK_WAIT):
            run_ensure_steps()
    except LockTimeout:
        logger.warning("等待 %ss 仍未获取 %s，跳过启动引导（由持锁进程完成）", BOOTSTRAP_LOCK_WAIT, BOOTSTRAP_LOCK_KEY)
    except redis.RedisError as exc:
        logger.warning("Redis 不可用，无锁执行启动引导：%s", exc)
        run_ensure_steps()


def copy_mock_assets() -> int:
    """把 ``app/core/zhiqi/mock_assets/`` 复制到 ``LOCAL_STORAGE_DIR/mock/``（目录不存在时跳过）。返回复制的文件数。"""
    source = Path(__file__).resolve().parent / "core" / "zhiqi" / "mock_assets"
    if not source.is_dir():
        return 0
    target = settings.local_storage_path / "mock"
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in source.iterdir():
        if item.is_file() and not item.name.startswith("."):
            dest = target / item.name
            if not dest.exists() or dest.stat().st_size != item.stat().st_size:
                shutil.copyfile(item, dest)
                copied += 1
    return copied


def _startup_warnings() -> None:
    if settings.jwt_secret_is_weak and not settings.dev_mode:
        logger.warning("ADMIN_JWT_SECRET 为默认值或过短（< 32 字节），生产环境必须替换")
    if settings.seed_admin_password == "admin123":
        logger.warning("默认超级管理员密码为 admin123，仅用于首次部署，请登录后立即修改")
    for message in check_public_base_url():
        logger.warning(message)


def _startup() -> None:
    try:
        bootstrap()
    except Exception:  # noqa: BLE001 - 数据库暂不可用时不阻塞进程启动，/api/v1/health 会报告 db=false
        logger.exception("启动引导失败")
    try:
        copy_mock_assets()
    except OSError:
        logger.exception("复制 Mock 占位素材失败")
    _startup_warnings()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await run_in_threadpool(_startup)
    yield


# =====================================================================
# 应用
# =====================================================================


def _configure_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("app").setLevel(settings.log_level)


def _media_response(key: str) -> Response:
    try:
        key = validate_storage_key(key)
    except InvalidStorageKey:
        raise BusinessError("非法的文件路径", code=CODE_BAD_REQUEST, http_status=400) from None
    storage = get_storage()
    media_type = guess_content_type(key)
    headers = {"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if isinstance(storage, LocalStorage):
        try:
            path = storage.path_for(key)
        except InvalidStorageKey:
            raise BusinessError("非法的文件路径", code=CODE_BAD_REQUEST, http_status=400) from None
        if not path.is_file():
            raise BusinessError("文件不存在", code=CODE_NOT_FOUND, http_status=404)
        return FileResponse(path, media_type=media_type, headers=headers)
    try:
        stream = storage.open(key)
    except FileNotFoundError:
        raise BusinessError("文件不存在", code=CODE_NOT_FOUND, http_status=404) from None

    def _iter() -> Iterator[bytes]:
        try:
            while chunk := stream.read(1024 * 1024):
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(_iter(), media_type=media_type, headers=headers)


def create_app() -> FastAPI:
    _configure_logging()
    app = FastAPI(
        title="aicreat API",
        version=app_version(),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    register_exception_handlers(app)
    app.include_router(api_router, prefix=API_PREFIX)

    @app.get("/media/{key:path}", include_in_schema=False)
    def media_file(key: str) -> Response:
        """本地存储文件（``oss`` 模式回源对象存储）；``key`` 为 ``media_assets.storage_key``，路径穿越返回 400。"""
        return _media_response(key)

    # 注册顺序固定：先 request_id_middleware、后 admin_audit_middleware（后注册者在外层，docs/07 §7.7）
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if REQUEST_ID_PATTERN.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - 未捕获异常在此转为 500 外壳，保证响应头带 X-Request-Id
            logger.error("request_id=%s 未捕获异常", request_id)
            response = await unhandled_exception_handler(request, exc)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @app.middleware("http")
    async def admin_audit_middleware(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if (request.url.path.startswith(ADMIN_PATH_PREFIX) and request.method in WRITE_METHODS
                and response.status_code < 400 and getattr(request.state, "permission_code", None)
                and not getattr(request.state, "audit_written", False)):
            try:
                await run_in_threadpool(_write_request_audit, app, request)
            except Exception:  # noqa: BLE001 - 审计失败不影响业务响应，只记错误日志
                logger.exception("后台操作审计日志写入失败 request_id=%s", getattr(request.state, "request_id", None))
        return response

    # CORS 最后注册（最外层），错误响应同样带 CORS 头
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?" if settings.dev_mode else None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[REQUEST_ID_HEADER, "Content-Disposition"],
    )
    return app


def _write_request_audit(app: FastAPI, request: Request) -> None:
    route_path = _route_template(app, request) or request.url.path
    admin_id = getattr(request.state, "admin_id", None)
    action = resolve_action(request.method, route_path)
    target_type = resolve_target_type(route_path) or "unknown"
    target_id = getattr(request.state, "audit_target_id", None) or resolve_target_id(route_path, dict(request.path_params), admin_id)
    summary = getattr(request.state, "audit_summary", None) or default_summary(action, target_type, target_id)
    with SessionLocal() as db:
        admin = db.get(Admin, admin_id) if admin_id is not None else None
        if admin is None:
            logger.error("审计中间件：找不到操作管理员 admin_id=%s path=%s", admin_id, request.url.path)
            return
        admin_rbac_service.write_audit(db, request, admin, request.state.permission_code, action, target_type, target_id, summary)


app = create_app()
