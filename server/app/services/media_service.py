"""图片 / 视频生成与素材库（docs/10-media-generation.md 全文；docs/03 B.14「媒体转存与素材引用」「删除规则」「任务幂等与状态收敛」
第 7、8 条；docs/04 §5.2、§5.3、§6.13、§7.8、§7.9；docs/08 §7.2、§7.3、§9.4；docs/11 §10.1；docs/12 §4.6；docs/13 §4.2、§6.1、§7.1）。

职责：

- 创建（API）：``generate_images`` / ``generate_video``——参数映射与默认值优先级（请求 > 项目级路由 ``params_json`` > 全局路由
  ``params_json`` > ``media_config`` 默认值）、``images`` / ``videos.validate_request``、敏感词、校验链（项目可见且 ``active`` →
  400 字段校验 → ``model?`` 覆盖 → 全局暂停 5031 → 频控 ``rate:media:{admin_id}`` 429 → 日上限 ``limit:images|videos:{date}``
  4291 → 真实模式参考 URL 公网校验 4222 → 路由 / 熔断快照 5031 → 额度预占 4291），同一事务创建 ``count`` 个资产与根任务，
  能反解为本系统上传素材的参考 URL 记入 ``reference_asset_ids_json``；
- 执行（worker）：``image_generate`` / ``video_generate`` 处理器（``from_content_prompt`` 时先同步执行内嵌 ``image_prompt`` 根任务），
  提交成功 → 根任务 ``polling``、资产 ``submitted``；图片同步 / 编辑回退直接得到 URL → 根任务 ``succeeded``、资产 ``downloading``；
- 轮询 ``poll_root``：``pending → submitted → generating → downloading``，``failed`` 按 ``classify_task_failure`` 分类
  （``media_storage`` 且有备选 → 复用资产行的备选回退），``expired`` / 超过 ``deadline_at`` → ``expired``，连续 3 次 404 →
  ``failed(route_missing)``，间隔按 ``media_config.<kind>.poll_intervals_seconds`` 封顶；
- 转存 ``transfer_asset``：``lock:media:transfer:{asset_id}``（600s，每 10 MB / 60s 续期）→ 上游 origin 走
  ``client.stream_download``（带 Bearer）、第三方 CDN 走 ``safe_fetch.stream_public_bytes``（不带 Bearer）、Mock 复制占位文件，
  视频失败时同一次尝试内回退 ``/content`` → 魔数 / 大小校验 → ``storage.save`` → ``ready``（``on_asset_ready`` 封面联动）；
  失败按 ``transfer.retry_seconds`` 重排，达到 ``transfer.max_attempts`` → ``failed(transfer_failed)`` + 告警；下载记录写根任务
  ``response_meta_json.download``；
- 人工入口：``retry_asset``（先同步复查旧上游任务）、``request_transfer``、``delete_asset``（409 规则含 ``in_use``）、列表 / 详情 /
  任务摘要（``references`` 按数据范围过滤）；
- 回收钩子（``recover_stale_tasks``）：``on_task_failed`` / ``on_stale_polling`` / ``on_task_expired`` / ``record_transfer_failure``；
- 清理 ``cleanup_media``：失败残留文件与孤儿参考素材；
- 实时计数 ``images_generated`` / ``videos_generated`` / ``media_failed``（``cancelled`` 不计）与 ``media_task_failed`` 告警（写
  资产 ``project_id``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import redis
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core import safe_fetch
from app.core.config import settings
from app.core.database import SessionLocal, after_commit
from app.core.exceptions import (
    CODE_CONFLICT,
    CODE_PUBLIC_URL_REQUIRED,
    CODE_QUOTA_LIMIT_REACHED,
    CODE_UPSTREAM_ERROR,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.locks import acquire_lock, extend_lock, is_locked, release_lock
from app.core.ratelimit import check_rate_limit
from app.core.redis import redis_client
from app.core.storage import (
    IMAGE_MIME_TYPES,
    VIDEO_MIME_TYPES,
    build_storage_key,
    extension_for,
    get_storage,
    probe_image_size,
    public_url_for,
    sniff_media_type,
)
from app.core.zhiqi import get_client, images, videos
from app.core.zhiqi.errors import ZhiqiError, classify_task_failure, is_fallbackable, sanitize_error_message
from app.core.zhiqi.types import Capability, DownloadResult, ErrorCategory, ImageRequest, TaskStatus, TextResult, VideoRequest
from app.models import AiTask, Content, MediaAsset, Project, utcnow
from app.schemas.media import ImageGenerateBody, VideoGenerateBody
from app.services import ai_gateway_service as gateway
from app.services import ai_task_service, alert_service, content_service, generation_service, settings_service, stats_service
from app.services import prompt_template_service as pts
from app.services.data_scope_service import SYSTEM_SCOPE, DataScope, get_visible, require_project, scope_media

logger = logging.getLogger(__name__)

__all__ = [
    "DAILY_LIMIT_PREFIX",
    "RATE_KEY_PREFIX",
    "TRANSFER_LOCK_PREFIX",
    "asset_references",
    "asset_task_summary",
    "build_image_request",
    "build_video_request",
    "cleanup_media",
    "delete_asset",
    "generate_images",
    "generate_video",
    "get_asset_detail",
    "get_asset_task",
    "image_params",
    "list_assets",
    "next_poll_delay",
    "on_asset_ready",
    "on_stale_polling",
    "on_task_cancelled",
    "on_task_expired",
    "on_task_failed",
    "poll_root",
    "record_transfer_failure",
    "reference_asset_ids",
    "register_handlers",
    "request_transfer",
    "retry_asset",
    "run_image_generate",
    "run_video_generate",
    "transfer_asset",
    "upload_asset",
    "video_params",
]

# =====================================================================
# 常量
# =====================================================================

DAILY_LIMIT_PREFIX = {"image": "limit:images:", "video": "limit:videos:"}
DAILY_LIMIT_SCOPE = {"image": "daily_images", "video": "daily_videos"}
DAILY_LIMIT_TTL = 172800
RATE_KEY_PREFIX = "rate:media:"
TRANSFER_LOCK_PREFIX = "lock:media:transfer:"
TRANSFER_LOCK_TTL = 600
LOCK_RENEW_BYTES = 10 * 1024 * 1024
LOCK_RENEW_SECONDS = 60
POLL_CLAIM_SECONDS = 60
CONSECUTIVE_404_LIMIT = 3
DOWNLOAD_REQUEST_IDS_KEEP = 10
IMAGE_PROMPT_MAX = 4000
SUMMARY_FALLBACK_CHARS = 200
PROBE_HEAD_BYTES = 512 * 1024
MB = 1024 * 1024

ACTIVE_ASSET_STATUSES = ("pending", "submitted", "generating", "downloading")
TERMINAL_ASSET_STATUSES = ("ready", "failed", "expired", "deleted")
DELETABLE_STATUSES = ("ready", "failed", "expired")
RETRYABLE_STATUSES = ("failed", "expired")
IN_USE_STATUSES = ("pending", "submitted")
TRANSFERABLE_CATEGORIES = ("transfer_failed", "timeout")
IMAGE_DOWNLOAD_TYPES = ("image/", "application/octet-stream", "binary/octet-stream")
VIDEO_DOWNLOAD_TYPES = ("video/", "application/octet-stream", "binary/octet-stream")
PAUSE_CATEGORY_VALUES = frozenset({"quota_exceeded", "auth_failed"})

MSG_CONTENT_NOT_FOUND = content_service.CONTENT_NOT_FOUND
MSG_PUBLIC_URL = "参考素材 URL 必须是公网可访问地址"
MSG_DAILY_LIMIT = {"image": "今日图片生成已达上限", "video": "今日视频生成已达上限"}
MSG_RETRY_CONFLICT = "当前状态不可重试"
MSG_TRANSFER_CONFLICT = "当前状态不可转存"
MSG_DELETE_CONFLICT = "当前状态不可删除"
MSG_IN_USE = "素材正被待提交的生成任务引用"
MSG_CANCELLED = "任务已取消"
MSG_EXPIRED = "轮询超过截止时间（deadline_at）"
MSG_UPSTREAM_EXPIRED = "上游任务已过期"
MSG_ROUTE_MISSING = f"轮询连续 {CONSECUTIVE_404_LIMIT} 次返回 404，上游任务不存在"
MSG_NO_ASSET = "素材不存在或已删除"
MSG_NO_CONTENT = "关联内容不存在"
MSG_EMPTY_PROMPT = "提示词为空"
MSG_NO_DOWNLOAD_URL = "缺少可下载的上游地址"


# =====================================================================
# 配置与工具
# =====================================================================


def media_config(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "media_config")


def _kind_of(capability: str | None) -> str:
    return "video" if capability == Capability.VIDEO.value else "image"


def _section(cfg: Mapping[str, Any], kind: str) -> dict[str, Any]:
    return dict(cfg.get("video" if kind == "video" else "image") or {})


def poll_intervals(cfg: Mapping[str, Any], kind: str) -> list[int]:
    default = [15, 30, 60] if kind == "video" else [5, 10, 15, 30]
    values = [int(v) for v in (_section(cfg, kind).get("poll_intervals_seconds") or default) if int(v) > 0]
    return values or default


def poll_budget(cfg: Mapping[str, Any], kind: str) -> int:
    default = settings.zhiqi_video_poll_budget_seconds if kind == "video" else settings.zhiqi_image_poll_budget_seconds
    return max(1, int(_section(cfg, kind).get("poll_budget_seconds") or default))


def next_poll_delay(cfg: Mapping[str, Any], kind: str, poll_count: int) -> int:
    """第 k 次轮询（k 从 1 起）后的间隔 ``intervals[min(k, len-1)]``；``k=0`` 为提交成功后的首个间隔（docs/10 §4.7、§5.4）。"""
    intervals = poll_intervals(cfg, kind)
    return intervals[min(max(0, int(poll_count or 0)), len(intervals) - 1)]


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _ref_ids(raw: str | None) -> list[int]:
    value = _loads(raw, [])
    if not isinstance(value, list):
        return []
    return [int(v) for v in value if isinstance(v, int) and not isinstance(v, bool)]


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _kind_label(kind: str) -> str:
    return "视频" if kind == "video" else "图片"


# =====================================================================
# 参数映射（docs/10 §4.1、§5.1）
# =====================================================================


def route_params(db: Session, capability: str, project_id: int | None) -> dict[str, Any]:
    """全局路由 ``params_json`` ← 项目级路由 ``params_json``（项目级优先）。"""
    params: dict[str, Any] = {}
    global_row = gateway.load_route(db, capability, 0)
    if global_row:
        params.update(global_row.get("params") or {})
    if project_id:
        hit = gateway.load_route(db, capability, project_id)
        if hit and int(hit.get("project_id") or 0) == int(project_id):
            params.update(hit.get("params") or {})
    return params


def image_params(db: Session, body: ImageGenerateBody, project_id: int, cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """图片最终发送参数（``params_json`` 快照）：``resolution`` / ``aspect_ratio`` 取值优先级 请求 > 项目级路由 > 全局路由 >
    ``media_config.image.default_*``；``reference_image_urls`` 原样。"""
    icfg = _section(cfg or media_config(db), "image")
    params = route_params(db, "image", project_id)
    return {
        "resolution": body.resolution or params.get("resolution") or icfg.get("default_resolution") or "1080p",
        "aspect_ratio": body.aspect_ratio or params.get("aspect_ratio") or icfg.get("default_aspect_ratio") or "16:9",
        "reference_image_urls": list(body.reference_image_urls or []),
    }


def video_params(db: Session, body: VideoGenerateBody, project_id: int, cfg: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """视频最终发送参数（``params_json`` 快照，11 个键齐全）：``duration`` / ``resolution`` / ``aspect_ratio`` / ``generate_audio``
    省略时按 路由 ``params_json``（项目级优先）> ``media_config.video`` 默认值；``aspect_ratio`` 与 ``size`` 二选一（同时给出保留
    ``aspect_ratio``；只给 ``size`` 时不补默认比例）。"""
    vcfg = _section(cfg or media_config(db), "video")
    params = route_params(db, "video", project_id)
    duration = body.duration if body.duration is not None else (params.get("duration") or vcfg.get("default_duration") or 5)
    resolution = body.resolution or params.get("resolution") or vcfg.get("default_resolution") or "720p"
    if body.aspect_ratio:
        aspect_ratio, size = body.aspect_ratio, None
    elif body.size:
        aspect_ratio, size = None, body.size
    else:
        aspect_ratio, size = (params.get("aspect_ratio") or vcfg.get("default_aspect_ratio") or "16:9"), None
    if body.generate_audio is not None:
        generate_audio = bool(body.generate_audio)
    elif params.get("generate_audio") is not None:
        generate_audio = bool(params.get("generate_audio"))
    else:
        generate_audio = bool(vcfg.get("generate_audio_default", False))
    return {
        "duration": int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else duration,
        "resolution": resolution,
        "aspect_ratio": aspect_ratio,
        "size": size,
        "input_reference": body.input_reference,
        "reference_image_urls": list(body.reference_image_urls or []),
        "reference_video_urls": list(body.reference_video_urls or []),
        "reference_audio_urls": list(body.reference_audio_urls or []),
        "first_frame_image_url": body.first_frame_image_url,
        "last_frame_image_url": body.last_frame_image_url,
        "generate_audio": generate_audio,
    }


def build_image_request(prompt: str, params: Mapping[str, Any], *, model: str = "") -> ImageRequest:
    """``ImageRequest``（``n=1``、``response_format=url``；``model`` 由网关按候选链填写）。"""
    return ImageRequest(
        model=model, prompt=prompt, resolution=str(params.get("resolution") or "1080p"),
        aspect_ratio=str(params.get("aspect_ratio") or "16:9"),
        reference_image_urls=[str(u) for u in (params.get("reference_image_urls") or [])],
    )


def build_video_request(prompt: str, params: Mapping[str, Any], *, negative_prompt: str | None = None, model: str = "") -> VideoRequest:
    """``VideoRequest``（只发送非 None 字段由 ``videos.build_payload`` 保证）。"""
    duration = params.get("duration")
    return VideoRequest(
        model=model, prompt=prompt,
        duration=int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool) else duration,
        resolution=params.get("resolution"), aspect_ratio=params.get("aspect_ratio"), size=params.get("size"),
        input_reference=params.get("input_reference"),
        reference_image_urls=list(params.get("reference_image_urls") or []),
        reference_video_urls=list(params.get("reference_video_urls") or []),
        reference_audio_urls=list(params.get("reference_audio_urls") or []),
        first_frame_image_url=params.get("first_frame_image_url"), last_frame_image_url=params.get("last_frame_image_url"),
        negative_prompt=negative_prompt, generate_audio=params.get("generate_audio"),
    )


def _video_urls(params: Mapping[str, Any]) -> list[str]:
    urls = [params.get(k) for k in ("input_reference", "first_frame_image_url", "last_frame_image_url")]
    for key in ("reference_image_urls", "reference_video_urls", "reference_audio_urls"):
        urls.extend(params.get(key) or [])
    return [str(u) for u in urls if u]


# =====================================================================
# 校验助手
# =====================================================================


def banned_word_errors(db: Session, fields: Mapping[str, str | None]) -> list[dict[str, Any]]:
    """``generation_config.quality.banned_words``（忽略大小写、子串命中）：每个命中字段一条
    ``{"loc":["body",<字段>],"msg":"命中敏感词：xxx","type":"banned_words","input":<提交值，超过 200 字符截断>}``（docs/10 §11.2）。"""
    words = banned_words(db)
    errors: list[dict[str, Any]] = []
    for name, value in fields.items():
        hits = _banned_hits(words, value)
        if hits:
            errors.append(field_error(["body", name], f"命中敏感词：{'、'.join(hits)}", "banned_words", value))
    return errors


def banned_words(db: Session) -> list[str]:
    """``generation_config.quality.banned_words``（与文本生成共用词表）。"""
    cfg = settings_service.get_config(db, "generation_config")
    return [str(w).strip() for w in (settings_service.get_path(cfg, "quality.banned_words") or []) if str(w).strip()]


def _banned_hits(words: Sequence[str], value: str | None) -> list[str]:
    if not value:
        return []
    lowered = value.lower()
    return [w for w in dict.fromkeys(words) if w.lower() in lowered]


def _validate_upstream(fn: Callable[[], None], errors: list[dict[str, Any]]) -> list[str]:
    """执行 ``images`` / ``videos.validate_request``：400 校验错误并入 ``errors``；4222 返回不合格 URL 列表。"""
    try:
        fn()
    except BusinessError as exc:
        if exc.code == CODE_PUBLIC_URL_REQUIRED:
            data = exc.data if isinstance(exc.data, dict) else {}
            return [str(u) for u in (data.get("urls") or [])]
        if isinstance(exc.data, list):
            errors.extend(e for e in exc.data if isinstance(e, dict) and e not in errors)
            return []
        raise
    return []


def _check_content(
    db: Session, scope: DataScope, project: Project, *, content_id: int | None, usage_type: str, from_content_prompt: bool,
    errors: list[dict[str, Any]],
) -> Content | None:
    """``content_id`` 与 ``usage_type`` / ``from_content_prompt`` 的组合（400）；内容须可见（404）且属于同一项目（400）。"""
    if usage_type == "standalone":
        if content_id:
            errors.append(field_error(["body", "content_id"], "独立素材不能指定关联内容", "value_error", content_id))
        if from_content_prompt:
            errors.append(field_error(["body", "from_content_prompt"], "由文章生成提示词须指定关联内容（用途只能是封面或配图）", "value_error", True))
        return None
    if not content_id:
        msg = "由文章生成提示词须指定关联内容" if from_content_prompt else "用途为封面或配图时必须指定关联内容"
        errors.append(field_error(["body", "content_id"], msg, "missing", None))
        return None
    content = get_visible(db, scope, Content, content_id)
    if content.project_id != project.id:
        errors.append(field_error(["body", "content_id"], "内容不属于该项目", "project_mismatch", content_id))
    return content


def _check_rate(db: Session, admin_id: int | None) -> None:
    """``rate:media:{admin_id}``（``generation_config.rate_limits.media_per_admin``，默认 ``MEDIA_RATE_LIMIT=20/hour``），按请求计 1。"""
    if admin_id is None:
        return
    cfg = settings_service.get_config(db, "generation_config")
    rate = settings_service.get_path(cfg, "rate_limits.media_per_admin") or settings.media_rate_limit or "20/hour"
    check_rate_limit(f"{RATE_KEY_PREFIX}{admin_id}", str(rate))


def _daily_key(db: Session, kind: str) -> str:
    return f"{DAILY_LIMIT_PREFIX[kind]}{stats_service.today_date(db).isoformat()}"


def reserve_daily_limit(db: Session, kind: str, count: int) -> tuple[str, int] | None:
    """``INCRBY limit:images|videos:{date} count`` + ``EXPIRE 172800``；超过 ``media_config.daily_limits.<images|videos>``（0 = 不限）
    时 ``DECRBY`` 回滚并抛 4291 ``data={"scope":"daily_images|daily_videos","limit","used"}``（``used`` 为本次之前的已用数）。
    返回 ``(键, 数量)`` 供失败时回滚；Redis 不可用时放行并返回 ``None``。"""
    limits = media_config(db).get("daily_limits") or {}
    limit = int(limits.get("videos" if kind == "video" else "images") or 0)
    key = _daily_key(db, kind)
    try:
        pipe = redis_client.pipeline()
        pipe.incrby(key, int(count))
        pipe.expire(key, DAILY_LIMIT_TTL)
        used = int(pipe.execute()[0])
    except redis.RedisError as exc:
        logger.warning("日上限计数失败（Redis 不可用，放行）key=%s: %s", key, exc)
        return None
    if limit > 0 and used > limit:
        _release_daily((key, int(count)))
        raise BusinessError(
            MSG_DAILY_LIMIT[kind], code=CODE_QUOTA_LIMIT_REACHED,
            data={"scope": DAILY_LIMIT_SCOPE[kind], "limit": limit, "used": used - int(count)},
        )
    return key, int(count)


def _release_daily(reserved: tuple[str, int] | None) -> None:
    if not reserved:
        return
    try:
        redis_client.decrby(reserved[0], reserved[1])
    except redis.RedisError as exc:
        logger.warning("回滚日上限计数失败 key=%s: %s", reserved[0], exc)


def _public_prefixes() -> list[str]:
    prefixes = [
        settings.public_base_url.strip().rstrip("/") + "/media/",
        settings.oss_public_base_url.strip().rstrip("/") + "/",
        settings.media_public_base.rstrip("/") + "/",
    ]
    return [p for p in dict.fromkeys(prefixes) if p and p != "/"]


def reference_asset_ids(db: Session, urls: Iterable[str]) -> list[int]:
    """参考 URL 以 ``PUBLIC_BASE_URL + /media/`` 或 ``OSS_PUBLIC_BASE_URL`` 为前缀时取其后的 ``storage_key`` 查
    ``media_assets(source=uploaded)`` 得到 ID（查不到或外部 URL 不记；不受数据范围约束，docs/13 §7.1）。按 URL 顺序去重。"""
    prefixes = _public_prefixes()
    keys: list[str] = []
    for url in urls:
        if not url:
            continue
        for prefix in prefixes:
            if url.startswith(prefix):
                key = url[len(prefix):].split("#", 1)[0].split("?", 1)[0]
                if key:
                    keys.append(key)
                break
    if not keys:
        return []
    rows = db.execute(
        select(MediaAsset.id, MediaAsset.storage_key)
        .where(MediaAsset.source == "uploaded", MediaAsset.storage_key.in_(list(dict.fromkeys(keys))))
        .order_by(MediaAsset.id)
    ).all()
    by_key: dict[str, int] = {}
    for asset_id, key in rows:
        by_key.setdefault(key, int(asset_id))
    return list(dict.fromkeys(by_key[k] for k in keys if k in by_key))


def _next_sort(db: Session, content_id: int | None) -> int:
    if not content_id:
        return 0
    db.flush()
    current = db.scalar(select(func.max(MediaAsset.sort)).where(MediaAsset.content_id == content_id, MediaAsset.status != "deleted"))
    return int(current or 0) + 1


# =====================================================================
# 创建（POST /admin/media/images|videos/generate）
# =====================================================================


@dataclass
class _Plan:
    kind: str
    project: Project
    content: Content | None
    usage_type: str
    prompt: str | None
    negative_prompt: str | None
    params: dict[str, Any]
    input: dict[str, Any]
    model_override: str | None
    reference_urls: list[str] = field(default_factory=list)


def _pre_checks(db: Session, kind: str, admin_id: int | None, model_override: str | None) -> None:
    """``model?`` 覆盖（400）→ 全局暂停（5031）→ 频控（429）。"""
    capability = Capability.VIDEO.value if kind == "video" else Capability.IMAGE.value
    gateway.validate_model_override(db, capability, model_override)
    generation_service.ensure_not_paused(capability)
    _check_rate(db, admin_id)


def _create(db: Session, plan: _Plan, *, count: int, admin_id: int | None, bad_urls: list[str]) -> dict[str, Any]:
    """日上限 → 4222 → 路由 / 熔断快照 → 同一事务创建资产与根任务并逐个预占额度；任一步失败回滚并撤销预占与日上限计数。"""
    capability = Capability.VIDEO.value if plan.kind == "video" else Capability.IMAGE.value
    operation = f"{plan.kind}_generate"
    daily = reserve_daily_limit(db, plan.kind, count)
    reservations: list[tuple[int | None, int]] = []
    try:
        if bad_urls:
            raise BusinessError(MSG_PUBLIC_URL, code=CODE_PUBLIC_URL_REQUIRED, data={"urls": bad_urls})
        route = gateway.preflight(db, capability, plan.project.id, model_override=plan.model_override)
        estimate = gateway.estimate_route(db, route, prompt=plan.prompt or "", completion_tokens=0)
        ref_ids = reference_asset_ids(db, plan.reference_urls)
        model = plan.model_override or (route.candidates[0] if route.candidates else None)
        asset_ids: list[int] = []
        task_ids: list[int] = []
        sort = _next_sort(db, plan.content.id if plan.content else None)
        for index in range(count):
            asset = MediaAsset(
                project_id=plan.project.id, content_id=plan.content.id if plan.content else None, kind=plan.kind,
                usage_type=plan.usage_type, source="generated", status="pending", prompt=plan.prompt,
                negative_prompt=plan.negative_prompt, model=model, params_json=_dumps(plan.params),
                reference_asset_ids_json=_dumps(ref_ids) if ref_ids else None, progress=0, transfer_attempts=0,
                sort=sort + index if plan.content else 0, created_by=int(admin_id or 0),
            )
            db.add(asset)
            db.flush()
            root = gateway.create_root_task(
                db, capability=capability, operation=operation, project_id=plan.project.id, created_by=admin_id,
                trigger_type="user", target_type="media_asset", target_id=asset.id, input=plan.input, route=route,
            )
            asset.ai_task_id = root.id
            gateway.check_quota(db, project_id=plan.project.id, estimated_quota=estimate, task=root)
            if estimate > 0:
                reservations.append((plan.project.id, estimate))
            asset_ids.append(asset.id)
            task_ids.append(root.id)
        warning = gateway.quota_warning(db, project_id=plan.project.id)
        db.commit()
    except Exception:
        db.rollback()
        for project_id, amount in reservations:
            gateway.release_reservation(db, AiTask(project_id=project_id, quota_reserved=amount))
        _release_daily(daily)
        raise
    result: dict[str, Any]
    if plan.kind == "video":
        result = {"asset_id": asset_ids[0], "task_id": task_ids[0]}
    else:
        result = {"asset_ids": asset_ids, "task_ids": task_ids}
    if warning:
        result["quota_warning"] = warning
    return result


def generate_images(db: Session, scope: DataScope, body: ImageGenerateBody, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/media/images/generate``：每张一条 ``media_assets(pending)`` + 根任务 ``operation=image_generate``；返回
    ``{asset_ids[], task_ids[], quota_warning?}``（docs/10 §4.1、§4.4；docs/04 §7.8）。"""
    project = require_project(db, scope, body.project_id, active=True)
    cfg = media_config(db)
    icfg = _section(cfg, "image")
    errors: list[dict[str, Any]] = []
    content = _check_content(
        db, scope, project, content_id=body.content_id, usage_type=body.usage_type,
        from_content_prompt=bool(body.from_content_prompt), errors=errors,
    )
    prompt = (body.prompt or "").strip() or None
    if body.from_content_prompt:
        prompt = None  # worker 经内嵌 image_prompt 根任务生成
    elif not prompt:
        errors.append(field_error(["body", "prompt"], "提示词不能为空", "missing", body.prompt))
    max_count = int(icfg.get("max_count_per_request") or 4)
    if body.count > max_count:
        errors.append(field_error(["body", "count"], f"单次最多生成 {max_count} 张", "less_than_equal", body.count))
    params = image_params(db, body, project.id, cfg)
    req = build_image_request(prompt or "-", params)
    bad_urls = _validate_upstream(lambda: images.validate_request(req, cfg), errors)
    errors.extend(e for e in banned_word_errors(db, {"prompt": prompt}) if e not in errors)
    if errors:
        raise invalid_params(errors)
    override = (body.model or "").strip() or None
    _pre_checks(db, "image", admin_id, override)
    input_data: dict[str, Any] = {
        "prompt": prompt, "from_content_prompt": bool(body.from_content_prompt), "content_id": body.content_id,
        "usage_type": body.usage_type, **params,
    }
    if override:
        input_data["model"] = override
    plan = _Plan(
        kind="image", project=project, content=content, usage_type=body.usage_type, prompt=prompt, negative_prompt=None,
        params=params, input=input_data, model_override=override, reference_urls=list(params["reference_image_urls"]),
    )
    return _create(db, plan, count=int(body.count), admin_id=admin_id, bad_urls=bad_urls)


def generate_video(db: Session, scope: DataScope, body: VideoGenerateBody, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/media/videos/generate``：一条 ``media_assets(pending)`` + 根任务 ``operation=video_generate``；返回
    ``{asset_id, task_id, quota_warning?}``（docs/10 §5.1；docs/04 §7.9）。"""
    project = require_project(db, scope, body.project_id, active=True)
    cfg = media_config(db)
    errors: list[dict[str, Any]] = []
    if body.usage_type not in ("inline", "standalone"):
        errors.append(field_error(["body", "usage_type"], "视频用途只能是配图或独立素材", "enum", body.usage_type))
        raise invalid_params(errors)
    content = _check_content(
        db, scope, project, content_id=body.content_id, usage_type=body.usage_type, from_content_prompt=False, errors=errors,
    )
    prompt = (body.prompt or "").strip()
    negative = (body.negative_prompt or "").strip() or None
    params = video_params(db, body, project.id, cfg)
    req = build_video_request(prompt, params, negative_prompt=negative)
    bad_urls = _validate_upstream(lambda: videos.validate_request(req, cfg), errors)
    errors.extend(e for e in banned_word_errors(db, {"prompt": prompt, "negative_prompt": negative}) if e not in errors)
    if errors:
        raise invalid_params(errors)
    override = (body.model or "").strip() or None
    _pre_checks(db, "video", admin_id, override)
    input_data: dict[str, Any] = {
        "prompt": prompt, "negative_prompt": negative, "content_id": body.content_id, "usage_type": body.usage_type, **params,
    }
    if override:
        input_data["model"] = override
    plan = _Plan(
        kind="video", project=project, content=content, usage_type=body.usage_type, prompt=prompt, negative_prompt=negative,
        params=params, input=input_data, model_override=override, reference_urls=_video_urls(params),
    )
    return _create(db, plan, count=1, admin_id=admin_id, bad_urls=bad_urls)


# =====================================================================
# 资产 ↔ 根任务
# =====================================================================


def task_assets(db: Session, root: AiTask) -> list[MediaAsset]:
    """根任务对应的资产：``ai_task_id = root.id`` 的行，以及 ``target_type=media_asset`` 指向且未改指其它根任务的行。"""
    db.flush()
    found = {a.id: a for a in db.scalars(select(MediaAsset).where(MediaAsset.ai_task_id == root.id)).all()}
    if root.target_type == "media_asset" and root.target_id and root.target_id not in found:
        asset = db.get(MediaAsset, root.target_id)
        if asset is not None and asset.ai_task_id in (None, root.id):
            found[asset.id] = asset
    return [found[k] for k in sorted(found)]


def _root_asset(db: Session, root: AiTask) -> MediaAsset | None:
    assets = [a for a in task_assets(db, root) if a.status != "deleted"]
    return assets[0] if assets else None


def _download_request_id(db: Session, asset: MediaAsset) -> str | None:
    root = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
    if root is None:
        return None
    download = gateway.task_meta(root).get("download") or {}
    value = download.get("request_id") if isinstance(download, dict) else None
    return str(value) if value else None


def _media_alert(
    db: Session, asset: MediaAsset, *, status: str, category: str, message: str | None, download_request_id: Any = ...,
) -> None:
    """``media_task_failed``（info，``target_type=media_asset``，``target_key={asset_id}``，``project_id`` = 资产的 ``project_id``）+
    ``stats:rt.media_failed``；``cancelled`` 不调用本函数。"""
    root = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
    kind = _kind_label(asset.kind)
    action = "已过期" if status == "expired" else "失败"
    payload: dict[str, Any] = {
        "asset_id": asset.id,
        "kind": asset.kind,
        "status": status,
        "error_category": category,
        "request_id": root.request_id if root is not None else None,
        "model": (root.model if root is not None else None) or asset.model,
        "upstream_task_id": asset.upstream_task_id or (root.upstream_task_id if root is not None else None),
        "ai_task_id": asset.ai_task_id,
    }
    if download_request_id is not ...:
        payload["download_request_id"] = download_request_id
    alert_service.raise_alert(
        db, SYSTEM_SCOPE, "media_task_failed",
        target_type="media_asset", target_id=asset.id, project_id=asset.project_id,
        title=f"{kind}任务{action}：素材 #{asset.id}",
        message=f"{kind}素材 #{asset.id} {action}（{category}）：{message or ''}"[:1000],
        payload=payload,
    )
    stats_service.increment_realtime_after_commit(db, asset.project_id, {"media_failed": 1}, at=asset.failed_at)


def _fail_asset(db: Session, asset: MediaAsset, *, status: str, category: str, message: str | None) -> None:
    now = utcnow()
    asset.status = status
    asset.error_category = category
    asset.error_message = (sanitize_error_message(message) or None) if message else None
    asset.failed_at = now
    asset.next_transfer_at = None
    if category != ErrorCategory.CANCELLED.value:
        _media_alert(db, asset, status=status, category=category, message=message)


def on_task_failed(db: Session, root: AiTask, category: str, message: str | None) -> None:
    """根任务失败（终态事务内，``finalize_root`` 之前）：资产 ``failed(category, failed_at)`` + ``media_task_failed`` 告警 +
    ``media_failed``；``cancelled`` 不告警、不计数。也是 ``recover_stale_tasks`` 的 ``on_task_failed`` 钩子。"""
    for asset in task_assets(db, root):
        if asset.status in TERMINAL_ASSET_STATUSES:
            continue
        _fail_asset(db, asset, status="failed", category=category or "unknown", message=message)
    db.flush()


def on_task_cancelled(db: Session, root: AiTask) -> None:
    """根任务取消（``queued`` / ``polling``）：资产 ``failed(error_category=cancelled, failed_at)``，不告警、不计 ``media_failed``。"""
    for asset in task_assets(db, root):
        if asset.status in TERMINAL_ASSET_STATUSES:
            continue
        _fail_asset(db, asset, status="failed", category=ErrorCategory.CANCELLED.value, message=MSG_CANCELLED)
    db.flush()


def on_task_expired(db: Session, root: AiTask, message: str = MSG_EXPIRED) -> None:
    """根任务过期（``recover_stale_tasks`` ② 与轮询判定）：资产 ``expired(failed_at)`` + 告警（``error_category=timeout``）。"""
    for asset in task_assets(db, root):
        if asset.status in TERMINAL_ASSET_STATUSES:
            continue
        _fail_asset(db, asset, status="expired", category=ErrorCategory.TIMEOUT.value, message=message)
    db.flush()


def on_stale_polling(db: Session, root: AiTask) -> None:
    """``recover_stale_tasks`` ①：心跳超时但上游已受理的根任务改回 ``polling`` 时，资产 ``pending → submitted``。"""
    for asset in task_assets(db, root):
        if asset.status == "pending":
            asset.status = "submitted"
        if asset.status in ("submitted", "generating") and not asset.upstream_task_id:
            asset.upstream_task_id = root.upstream_task_id
    db.flush()


def on_asset_ready(db: Session, asset: MediaAsset) -> None:
    """``usage_type=cover`` 且绑定内容的**图片**资产 ``ready`` 时把 ``contents.cover_asset_id`` 指向它，原封面资产改为 ``inline``
    （仍保持绑定）；同一请求多张封面按 ``ready`` 先后依次替换（docs/10 §4.9）。"""
    if asset.kind != "image" or asset.usage_type != "cover" or not asset.content_id:
        return
    content = db.get(Content, asset.content_id)
    if content is None:
        return
    previous = content.cover_asset_id
    if previous and previous != asset.id:
        old = db.get(MediaAsset, previous)
        if old is not None and old.content_id == content.id and old.usage_type == "cover":
            old.usage_type = "inline"
    content.cover_asset_id = asset.id
    db.flush()


def _enter_downloading(asset: MediaAsset, *, upstream_url: str | None, upstream_task_id: str | None, now: datetime) -> None:
    asset.status = "downloading"
    asset.upstream_url = (upstream_url or None) and upstream_url[:1000]
    if upstream_task_id:
        asset.upstream_task_id = upstream_task_id[:80]
    asset.error_category = None
    asset.error_message = None
    asset.failed_at = None
    asset.next_transfer_at = now  # 立即转存；进程丢失时由 transfer_media.retry_due 在 60s 内补领


# =====================================================================
# worker 处理器（image_generate / video_generate）
# =====================================================================


def _single_line(result: TextResult) -> str:
    """单行英文提示词：首个非空行、去首尾空白、截断至 4000 字符；空输出归 ``invalid_response``（网关同模型重新生成 1 次）。"""
    for line in (result.text or "").splitlines():
        text = line.strip()
        if text:
            return text[:IMAGE_PROMPT_MAX]
    raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出为空")


def _plain_head(body: str | None, fmt: str | None, limit: int) -> str:
    if not body:
        return ""
    text = re.sub(r"<[^>]+>", " ", body) if fmt == "html" else re.sub(r"[#>*_`\[\]()!|~-]+", " ", body)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def image_prompt_variables(db: Session, content: Content, asset: MediaAsset) -> dict[str, Any]:
    """``sys_image_prompt`` 变量：``title``、``summary``（``contents.summary`` → ``seo_description`` → 正文前 200 字符）、
    ``style``、``usage_type``，另含项目内置变量。"""
    project = db.get(Project, content.project_id)
    summary = (content.summary or "").strip() or (content.seo_description or "").strip() or _plain_head(
        content.body, content.format, SUMMARY_FALLBACK_CHARS
    )
    return {
        **generation_service.project_variables(project),
        "title": content.title,
        "summary": summary,
        "style": content.style,
        "usage_type": asset.usage_type,
    }


def _business_category(exc: BusinessError) -> tuple[str, str]:
    category = getattr(exc, "error_category", None)
    if not category:
        data = exc.data if isinstance(exc.data, dict) else {}
        category = data.get("error_category") or ("quota_exceeded" if exc.code == CODE_QUOTA_LIMIT_REACHED else "unknown")
    message = getattr(exc, "error_message", None) or exc.message
    return str(category), str(message)


def _run_image_prompt(ctx: ai_task_service.TaskContext, asset: MediaAsset) -> str | ai_task_service.TaskOutcome:
    """内嵌 ``image_prompt`` 根任务（docs/10 §4.5）：同步执行（``status=running``、``capability=content``、``trigger_type=system``、
    ``target_type=content``、不经队列），成功写 ``media_assets.prompt`` 与该根任务 ``output_excerpt``。失败：图片根任务
    ``failed(同分类)``；``quota_exceeded`` / ``auth_failed`` 时宿主图片根任务回滚 ``queued``（``pause_count < 3``），资产保持 ``pending``。"""
    db, host = ctx.db, ctx.task
    content_id = asset.content_id or ctx.input.get("content_id")
    content = db.get(Content, content_id) if content_id else None
    if content is None:
        return ai_task_service.TaskOutcome(status="failed", error_category="unknown", error_message=MSG_NO_CONTENT)
    template = pts.resolve_template(db, "image_prompt", host.project_id, content.language)
    variables = image_prompt_variables(db, content, asset)
    system, user = pts.render(template, variables)
    prompt_root = gateway.create_root_task(
        db, capability=Capability.CONTENT.value, operation="image_prompt", project_id=host.project_id, created_by=host.created_by,
        trigger_type="system", target_type="content", target_id=content.id, template_id=template.id,
        input={"asset_id": asset.id, "usage_type": asset.usage_type}, sync=True, worker_id=ctx.worker_id,
    )
    db.commit()
    try:
        result, _attempt = gateway.complete_text(
            db, root_task=prompt_root, messages=pts.messages_for(system, user), params=pts.call_params(template),
            response_format=pts.response_format_for(template), metadata=pts.call_metadata(template, "image_prompt"),
            validator=_single_line,
        )
    except ZhiqiError as err:
        return _image_prompt_failed(ctx, prompt_root, err.category.value, sanitize_error_message(err.message) or err.category.value)
    except BusinessError as exc:
        category, message = _business_category(exc)
        return _image_prompt_failed(ctx, prompt_root, category, message)
    text = getattr(result, "parsed", None) or _single_line(result)
    hits = _banned_hits(banned_words(db), text)
    gateway.finalize_root(db, prompt_root, "succeeded", output_excerpt=text)
    if hits:
        db.commit()
        return ai_task_service.TaskOutcome(
            status="failed", error_category=ErrorCategory.CONTENT_BLOCKED.value, error_message=f"命中敏感词：{'、'.join(hits)}",
        )
    asset.prompt = text
    db.commit()
    return text


def _image_prompt_failed(ctx: ai_task_service.TaskContext, prompt_root: AiTask, category: str, message: str) -> ai_task_service.TaskOutcome:
    db, host = ctx.db, ctx.task
    db.rollback()
    prompt_root = db.get(AiTask, prompt_root.id)
    host = db.get(AiTask, host.id)
    if prompt_root is not None and prompt_root.status == "running":
        gateway.finalize_root(db, prompt_root, "failed", error_category=category, error_message=message)
    if category in PAUSE_CATEGORY_VALUES and host is not None and host.status == "running":
        gateway.rollback_for_pause(db, host, commit=False)  # pause_count < 3 → queued（不结算、不发告警），资产保持 pending
    db.commit()
    return ai_task_service.TaskOutcome(status="failed", error_category=category, error_message=message)


def run_image_generate(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``image_generate`` 根任务：（``from_content_prompt`` 且资产尚无提示词时）内嵌 ``image_prompt`` → ``submit_image``。"""
    db, root = ctx.db, ctx.task
    asset = _root_asset(db, root)
    if asset is None:
        return ai_task_service.TaskOutcome(status="failed", error_category="unknown", error_message=MSG_NO_ASSET)
    data = ctx.input
    prompt = (asset.prompt or data.get("prompt") or "").strip()
    if data.get("from_content_prompt") and not (asset.prompt or "").strip():
        generated = _run_image_prompt(ctx, asset)
        if isinstance(generated, ai_task_service.TaskOutcome):
            return generated
        prompt = generated
    if not prompt:
        return ai_task_service.TaskOutcome(status="failed", error_category="invalid_response", error_message=MSG_EMPTY_PROMPT)
    cfg = media_config(db)
    icfg = _section(cfg, "image")
    params = {
        "resolution": data.get("resolution") or icfg.get("default_resolution") or "1080p",
        "aspect_ratio": data.get("aspect_ratio") or icfg.get("default_aspect_ratio") or "16:9",
        "reference_image_urls": list(data.get("reference_image_urls") or []),
    }
    result, attempt = ctx.submit_image(build_image_request(prompt, params))
    asset = db.get(MediaAsset, asset.id)
    asset.model = attempt.model
    if result.mode == "async":
        gateway.start_polling(
            db, root, attempt, first_interval_seconds=next_poll_delay(cfg, "image", 0), budget_seconds=poll_budget(cfg, "image"),
        )
        asset.status = "submitted"
        asset.upstream_task_id = (result.task_id or None) and result.task_id[:80]
        asset.progress = 0
        db.flush()
        return ai_task_service.TaskOutcome(status="polling")
    url = result.urls[0]
    asset_id = asset.id

    def _apply(apply_ctx: ai_task_service.TaskContext) -> None:
        target = apply_ctx.db.get(MediaAsset, asset_id)
        if target is not None and target.status not in TERMINAL_ASSET_STATUSES:
            _enter_downloading(target, upstream_url=url, upstream_task_id=None, now=utcnow())

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=url)


def run_video_generate(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``video_generate`` 根任务：``submit_video``（只有任务式接口，无同步回退）→ ``polling``。"""
    db, root = ctx.db, ctx.task
    asset = _root_asset(db, root)
    if asset is None:
        return ai_task_service.TaskOutcome(status="failed", error_category="unknown", error_message=MSG_NO_ASSET)
    data = ctx.input
    prompt = (asset.prompt or data.get("prompt") or "").strip()
    if not prompt:
        return ai_task_service.TaskOutcome(status="failed", error_category="invalid_response", error_message=MSG_EMPTY_PROMPT)
    cfg = media_config(db)
    req = build_video_request(prompt, data, negative_prompt=asset.negative_prompt or data.get("negative_prompt"))
    result, attempt = ctx.submit_video(req)
    asset = db.get(MediaAsset, asset.id)
    gateway.start_polling(
        db, root, attempt, first_interval_seconds=next_poll_delay(cfg, "video", 0), budget_seconds=poll_budget(cfg, "video"),
    )
    asset.model = attempt.model
    asset.status = "submitted"
    asset.upstream_task_id = result.task_id[:80]
    asset.progress = 0
    db.flush()
    return ai_task_service.TaskOutcome(status="polling")


def register_handlers() -> None:
    """注册 ``image_generate`` / ``video_generate`` 处理器（导入本模块时执行；测试恢复注册表后可再次调用）。"""
    for operation, handler in (("image_generate", run_image_generate), ("video_generate", run_video_generate)):
        ai_task_service.register_handler(operation, handler, on_failed=on_task_failed, on_cancelled=on_task_cancelled)


register_handlers()


# =====================================================================
# 轮询（docs/10 §4.7、§5.4；docs/08 §9.4）
# =====================================================================


def _reschedule(root: AiTask, cfg: Mapping[str, Any], now: datetime) -> None:
    root.next_poll_at = now + timedelta(seconds=next_poll_delay(cfg, _kind_of(root.capability), int(root.poll_count or 0)))


def _expire(db: Session, root: AiTask, message: str) -> None:
    """轮询超出 ``deadline_at`` / 上游 ``expired``：资产 ``expired`` + 告警、根任务 ``expired(timeout)``；按 docs/08 §9.2
    ``timeout`` 行（「或轮询超出预算」计入熔断）对该根任务的 ``(capability, model)`` 计一次熔断失败。"""
    on_task_expired(db, root, message)
    gateway.record_breaker_failure(db, root.capability, root.model, ZhiqiError(ErrorCategory.TIMEOUT, message))
    gateway.finalize_root(db, root, "expired", error_category=ErrorCategory.TIMEOUT.value, error_message=message)


def _fail_root(db: Session, root: AiTask, category: str, message: str | None) -> None:
    on_task_failed(db, root, category, message)
    gateway.finalize_root(db, root, "failed", error_category=category, error_message=message)


def _fallback_media_storage(db: Session, root: AiTask, assets: list[MediaAsset], message: str) -> AiTask | None:
    """轮询阶段 ``media_storage`` 备选回退（docs/10 §4.10）：``fallback.enabled`` 且 ``fallback_on ∋ media_storage``、无请求级覆盖、
    仍有下一候选 → 旧根任务 ``failed(media_storage)``；新根任务（``trigger_type=system``、``parent_task_id``、``candidate_index``
    从下一候选起、复制 ``project_id`` / ``input_json``）入队；**复用同一资产行**（``pending``、``ai_task_id`` 改指、``progress=0``、
    ``transfer_attempts=0``、``error_*`` / ``failed_at`` 清空），不重复计 ``limit:images|videos``。无备选返回 ``None``。"""
    data = gateway.task_input(root)
    if data.get("model"):
        return None
    if not is_fallbackable(ErrorCategory.MEDIA_STORAGE, gateway._cfg(db)):  # noqa: SLF001
        return None
    try:
        route = gateway.resolve_route(db, Capability(root.capability), root.project_id)
    except BusinessError:
        return None
    next_index = int(root.candidate_index or 0) + 1
    if next_index >= len(route.candidates) or not assets:
        return None
    gateway.finalize_root(db, root, "failed", error_category=ErrorCategory.MEDIA_STORAGE.value, error_message=message)
    asset = assets[0]
    new_root = gateway.create_root_task(
        db, capability=root.capability, operation=root.operation, project_id=root.project_id, created_by=root.created_by,
        trigger_type="system", target_type="media_asset", target_id=asset.id, input=data, route=route,
        parent_task_id=root.id, candidate_index=next_index, model=route.candidates[next_index],
    )
    estimate = gateway.estimate_for(db, route.candidates[next_index], _estimate_tokens(asset.prompt), 0)
    try:
        gateway.check_quota(db, project_id=root.project_id, estimated_quota=estimate, task=new_root)
    except BusinessError as exc:
        logger.warning("备选回退根任务 %s 额度预占失败（按未预占继续）：%s", new_root.id, exc.message)
    for item in assets:
        item.status = "pending"
        item.ai_task_id = new_root.id
        item.model = route.candidates[next_index]
        item.progress = 0
        item.transfer_attempts = 0
        item.error_category = None
        item.error_message = None
        item.failed_at = None
        item.next_transfer_at = None
        item.upstream_task_id = None
        item.upstream_url = None
    db.flush()
    return new_root


def _estimate_tokens(prompt: str | None) -> int:
    from app.core.zhiqi.usage import estimate_tokens

    return estimate_tokens(prompt or "")


def poll_root(db: Session, root: AiTask, *, now: datetime | None = None) -> int | None:
    """单次轮询并按结果流转（调用方已抢占 ``next_poll_at``）。返回需要立即转存的资产 ID（上游 ``succeeded``），否则 ``None``。

    - ``queued`` → 资产保持 ``submitted``；``in_progress`` → 资产 ``generating``（``progress`` 同步，``progress=99`` 不做特殊处理）；
    - ``succeeded`` → 根任务 ``succeeded``、资产 ``downloading``（``upstream_url=data[0].url``）；
    - ``failed`` → ``classify_task_failure``：``media_storage`` 且有备选 → 备选回退；其它 → ``failed`` + 告警；
    - ``expired`` 或本地超过 ``deadline_at`` → ``expired`` + 告警；
    - GET 404 连续 3 次 → ``failed(route_missing)``；其它 GET 异常（含 ``auth_failed`` / ``quota_exceeded``，已续写暂停键）按间隔重排。
    """
    if root.status != "polling" or root.root_task_id is not None:
        return None
    cfg = media_config(db)
    kind = _kind_of(root.capability)
    if not root.upstream_task_id:
        _fail_root(db, root, ErrorCategory.UNKNOWN.value, "缺少上游任务 ID，无法轮询")
        db.commit()
        return None
    try:
        status = gateway.poll_task(db, root)
    except ZhiqiError as err:
        db.rollback()
        root = db.get(AiTask, root.id, with_for_update=True, populate_existing=True)  # 与 cancel 互斥（行锁，见下）
        now = now or utcnow()
        if root is None or root.status != "polling":
            return None
        poll = gateway.task_meta(root).get("poll") or {}
        if err.category == ErrorCategory.ROUTE_MISSING and int(poll.get("consecutive_404") or 0) >= CONSECUTIVE_404_LIMIT:
            _fail_root(db, root, ErrorCategory.ROUTE_MISSING.value, MSG_ROUTE_MISSING)
        elif root.deadline_at is not None and now >= root.deadline_at:
            _expire(db, root, MSG_EXPIRED)
        else:
            _reschedule(root, cfg, now)
        db.commit()
        return None
    now = now or utcnow()
    # 网络调用结束后才对根任务加行锁再判定状态：与 ``POST /admin/ai/tasks/{id}/cancel``（同样 ``FOR UPDATE`` 重读）互斥，避免
    # 取消与轮询结果互相覆盖（取消先提交 → 此处读到 ``cancelled`` 直接返回；轮询先提交 → 取消读到终态返回 409）。
    db.refresh(root, with_for_update=True)
    if root.status != "polling":  # 轮询期间被取消
        db.rollback()
        return None
    assets = task_assets(db, root)
    if status.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
        for asset in assets:
            if asset.status in ("pending", "submitted", "generating"):
                asset.progress = int(root.progress or 0)
                if status.status == TaskStatus.IN_PROGRESS:
                    asset.status = "generating"
                elif asset.status == "pending":
                    asset.status = "submitted"
        if root.deadline_at is not None and now >= root.deadline_at:
            _expire(db, root, MSG_EXPIRED)
        else:
            _reschedule(root, cfg, now)
        db.commit()
        return None
    if status.status == TaskStatus.SUCCEEDED:
        url = status.urls[0] if status.urls else None
        if not url and kind == "image":
            _fail_root(db, root, ErrorCategory.INVALID_RESPONSE.value, "上游任务成功但未返回结果 URL")
            db.commit()
            return None
        gateway.finalize_root(db, root, "succeeded", output_excerpt=url)
        target: int | None = None
        for asset in assets:
            if asset.status in TERMINAL_ASSET_STATUSES:
                continue
            _enter_downloading(asset, upstream_url=url, upstream_task_id=root.upstream_task_id, now=now)
            target = target or asset.id
        db.commit()
        return target
    if status.status == TaskStatus.FAILED:
        category = classify_task_failure(status.error_code, status.error_message)
        message = sanitize_error_message(status.error_message or status.error_code or "上游任务失败") or "上游任务失败"
        if category == ErrorCategory.MEDIA_STORAGE:
            # docs/08 §8.4 / §9.2：media_storage 计入熔断（无尝试行，按根任务当前模型计；在回退 / 失败之前，回退会改写 root）
            gateway.record_breaker_failure(db, root.capability, root.model, ZhiqiError(category, message))
        if category == ErrorCategory.MEDIA_STORAGE and _fallback_media_storage(db, root, assets, message) is not None:
            db.commit()
            return None
        _fail_root(db, root, category.value, message)
        db.commit()
        return None
    _expire(db, root, MSG_UPSTREAM_EXPIRED)  # TaskStatus.EXPIRED
    db.commit()
    return None


def claim_due_polls(db: Session, *, limit: int = 20, now: datetime | None = None) -> list[int]:
    """``status='polling' AND next_poll_at <= now`` 按 ``next_poll_at`` 取 ``limit`` 条，逐条以 ``UPDATE … SET next_poll_at =
    now + 60s WHERE id AND status='polling' AND next_poll_at <= now`` 抢占（rowcount=0 跳过）；返回抢到的根任务 ID。"""
    now = now or utcnow()
    ids = db.scalars(
        select(AiTask.id)
        .where(AiTask.root_task_id.is_(None), AiTask.status == "polling", AiTask.next_poll_at.is_not(None), AiTask.next_poll_at <= now)
        .order_by(AiTask.next_poll_at, AiTask.id)
        .limit(max(1, int(limit)))
    ).all()
    claimed: list[int] = []
    for task_id in ids:
        result = db.execute(
            update(AiTask)
            .where(AiTask.id == task_id, AiTask.status == "polling", AiTask.next_poll_at <= now)
            .values(next_poll_at=now + timedelta(seconds=POLL_CLAIM_SECONDS))
        )
        if result.rowcount:
            claimed.append(int(task_id))
    db.commit()
    return claimed


def poll_task_id(task_id: int) -> int | None:
    """独立会话执行一次 ``poll_root``（线程池线程内调用）；返回需要转存的资产 ID。异常只记日志（下次按抢占时写的
    ``next_poll_at`` 再轮询）。"""
    with SessionLocal() as db:
        try:
            root = db.get(AiTask, task_id)
            if root is None or root.status != "polling":
                return None
            return poll_root(db, root)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("媒体任务轮询失败 task_id=%s", task_id)
            return None


# =====================================================================
# 转存（docs/10 §4.8、§5.5）
# =====================================================================


class _LockRenewingWriter:
    """写入目标文件的同时每 10 MB 或 60s 续期 ``lock:media:transfer:{asset_id}``（600s）。"""

    def __init__(self, fh: BinaryIO, key: str, token: str) -> None:
        self._fh = fh
        self._key = key
        self._token = token
        self._since_bytes = 0
        self._since = time.monotonic()

    def write(self, data: bytes) -> int:
        written = self._fh.write(data)
        self._since_bytes += len(data)
        if self._since_bytes >= LOCK_RENEW_BYTES or time.monotonic() - self._since >= LOCK_RENEW_SECONDS:
            try:
                extend_lock(self._key, TRANSFER_LOCK_TTL, token=self._token)
            except redis.RedisError:
                logger.warning("续期 %s 失败", self._key, exc_info=True)
            self._since_bytes = 0
            self._since = time.monotonic()
        return written if written is not None else len(data)

    def flush(self) -> None:
        self._fh.flush()


@dataclass
class _DownloadCall:
    source: str
    request_id: str | None
    http_status: int | None
    request_ids: list[str]


def _tmp_dir() -> Path:
    return settings.local_storage_path / "tmp"


def _tmp_path(asset_id: int) -> Path:
    path = _tmp_dir() / f"{asset_id}.part"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _verify_file(path: Path, kind: str, max_bytes: int) -> tuple[str | None, str | None]:
    """返回 ``(mime, 错误)``：文件非空、≤ 上限、魔数为 PNG/JPEG/WebP/GIF（图片）或 MP4/MOV ``ftyp``（视频）且与 ``kind`` 一致。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        return None, f"读取下载文件失败：{type(exc).__name__}"
    if size <= 0:
        return None, "下载内容为空"
    if size > max_bytes:
        return None, f"文件超过大小上限 {max_bytes} 字节"
    mime = sniff_media_type(head)
    allowed = VIDEO_MIME_TYPES if kind == "video" else IMAGE_MIME_TYPES
    if mime is None or mime not in allowed:
        return None, f"文件类型（{mime or '无法识别'}）与素材类型 {kind} 不符"
    return mime, None


def _download_once(
    fetch: Callable[[BinaryIO], DownloadResult], source: str, path: Path, key: str, token: str, kind: str, max_bytes: int,
    calls: list[_DownloadCall],
) -> tuple[str | None, str | None]:
    """执行一次下载调用并记录 ``DownloadResult`` / ``ZhiqiError`` 的下载信息；返回 ``(mime, 错误)``。"""
    try:
        with path.open("wb") as fh:
            result = fetch(_LockRenewingWriter(fh, key, token))  # type: ignore[arg-type]
    except ZhiqiError as err:
        ids = [i for i in (err.retry_request_ids or ([err.request_id] if err.request_id else [])) if i]
        calls.append(_DownloadCall(source, err.request_id, err.http_status, ids))
        return None, sanitize_error_message(err.message) or "下载失败"
    except OSError as exc:
        calls.append(_DownloadCall(source, None, None, []))
        return None, f"写入临时文件失败：{type(exc).__name__}"
    calls.append(_DownloadCall(result.source or source, result.request_id, result.http_status, [i for i in result.request_ids if i]))
    return _verify_file(path, kind, max_bytes)


def _fetch_media(
    *, kind: str, upstream_url: str | None, upstream_task_id: str | None, path: Path, key: str, token: str, max_bytes: int,
    calls: list[_DownloadCall],
) -> tuple[str | None, str | None]:
    """下载源：``upstream_url`` 的 origin 等于 ``ZHIQI_BASE_URL`` 的 origin → ``client.stream_download``（带 Bearer）；Mock 的
    ``/media/mock/`` → ``MockZhiqiClient.stream_download``（复制占位文件）；其它 → ``safe_fetch.stream_public_bytes``（不带 Bearer，
    逐跳公网校验，≤ 3 跳）。视频在 ``upstream_url`` 为空或失败时同一次尝试内回退 ``videos.download_content``（``/content``）。"""
    client = get_client()
    timeout = float(settings.zhiqi_timeout_download_seconds)
    types = VIDEO_DOWNLOAD_TYPES if kind == "video" else IMAGE_DOWNLOAD_TYPES
    mime: str | None = None
    error: str | None = MSG_NO_DOWNLOAD_URL
    if upstream_url:
        url = upstream_url
        if client.is_mock and "/media/mock/" in url:
            source = "mock"
            fetch: Callable[[BinaryIO], DownloadResult] = lambda dest: client.stream_download(  # noqa: E731
                url, dest, max_bytes=max_bytes, timeout=timeout, source="mock", allowed_content_types=types,
            )
        elif client.is_origin_url(url):
            source = "origin"
            fetch = lambda dest: client.stream_download(  # noqa: E731
                url, dest, max_bytes=max_bytes, timeout=timeout, source="origin", allowed_content_types=types,
            )
        else:
            source = "cdn"
            fetch = lambda dest: safe_fetch.stream_public_bytes(  # noqa: E731
                url, dest, max_bytes=max_bytes, allowed_types=types, max_redirects=3, timeout=timeout,
            )
        mime, error = _download_once(fetch, source, path, key, token, kind, max_bytes, calls)
        if error is None:
            return mime, None
    if kind == "video" and upstream_task_id:
        task_id = upstream_task_id
        content_source = "mock" if client.is_mock else "content"
        mime, error = _download_once(
            lambda dest: videos.download_content(client, task_id, dest, max_bytes=max_bytes, timeout=timeout),
            content_source, path, key, token, kind, max_bytes, calls,
        )
    return (mime, None) if error is None else (None, error)


def _write_download_meta(root: AiTask | None, calls: Sequence[_DownloadCall]) -> None:
    """根任务 ``response_meta_json.download = {source, request_id, http_status, request_ids[]}``：前三项为最后一次下载调用的值，
    ``request_ids[]`` 按时间追加每次调用的非空请求号（含失败、被 ``/content`` 取代的 origin 下载与历次转存），保留最近 10 个。"""
    if root is None or not calls:
        return
    meta = gateway.task_meta(root)
    previous = meta.get("download") if isinstance(meta.get("download"), dict) else {}
    request_ids = [str(i) for i in (previous.get("request_ids") or []) if i]
    for call in calls:
        request_ids.extend(call.request_ids)
    last = calls[-1]
    meta["download"] = {
        "source": last.source,
        "request_id": last.request_id,
        "http_status": last.http_status,
        "request_ids": request_ids[-DOWNLOAD_REQUEST_IDS_KEEP:],
    }
    gateway.set_task_meta(root, meta)


def _file_info(path: Path, kind: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    head = b""
    size = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            if len(head) < PROBE_HEAD_BYTES:
                head += chunk[: PROBE_HEAD_BYTES - len(head)]
            digest.update(chunk)
            size += len(chunk)
    dims = probe_image_size(head) if kind == "image" else None
    return {"size_bytes": size, "file_hash": digest.hexdigest(), "width": dims[0] if dims else None, "height": dims[1] if dims else None}


def _mark_ready(db: Session, asset: MediaAsset, *, key: str, mime: str, info: Mapping[str, Any], now: datetime) -> None:
    url = public_url_for(key)
    asset.storage_key = key
    asset.url = url
    asset.mime_type = mime
    asset.size_bytes = int(info["size_bytes"])
    asset.file_hash = str(info["file_hash"])
    if asset.kind == "image":
        asset.width = info.get("width")
        asset.height = info.get("height")
        asset.thumbnail_key = key
        asset.thumbnail_url = url
    else:
        asset.width = asset.height = asset.duration_seconds = None
        asset.thumbnail_key = asset.thumbnail_url = None
    asset.progress = 100
    asset.status = "ready"
    asset.ready_at = now
    asset.failed_at = None
    asset.next_transfer_at = None
    asset.error_category = None
    asset.error_message = None
    on_asset_ready(db, asset)
    field_name = "videos_generated" if asset.kind == "video" else "images_generated"
    stats_service.increment_realtime_after_commit(db, asset.project_id, {field_name: 1}, at=now)


def record_transfer_failure(db: Session, asset: MediaAsset, error_message: str | None) -> None:
    """一次转存失败（docs/03「媒体转存与素材引用」第 2 条）：``transfer_attempts += 1``；未达 ``transfer.max_attempts`` → 保持
    ``downloading``、``next_transfer_at = now + retry_seconds[transfer_attempts-1]``；达到上限 → ``failed(transfer_failed, failed_at)``、
    ``next_transfer_at=NULL`` + ``media_task_failed`` 告警，``error_message`` 末尾与告警 ``payload`` 附 ``download_request_id``；
    根任务状态不变、不计熔断、不切换备选。也是 ``recover_stale_tasks`` ⑥ 的钩子（只 ``flush``）。"""
    transfer = media_config(db).get("transfer") or {}
    max_attempts = max(1, int(transfer.get("max_attempts") or 3))
    retry_seconds = [int(s) for s in (transfer.get("retry_seconds") or [30, 120, 600]) if int(s) > 0] or [30]
    now = utcnow()
    message = sanitize_error_message(error_message or "转存失败") or "转存失败"
    asset.transfer_attempts = int(asset.transfer_attempts or 0) + 1
    if asset.transfer_attempts < max_attempts:
        delay = retry_seconds[min(asset.transfer_attempts - 1, len(retry_seconds) - 1)]
        asset.next_transfer_at = now + timedelta(seconds=delay)
        asset.error_message = message[:500]
        asset.updated_at = now
        db.flush()
        return
    download_request_id = _download_request_id(db, asset)
    suffix = f"（download_request_id={download_request_id or 'null'}）"
    asset.status = "failed"
    asset.error_category = ErrorCategory.TRANSFER_FAILED.value
    asset.error_message = message[: max(0, 500 - len(suffix))] + suffix
    asset.failed_at = now
    asset.next_transfer_at = None
    asset.updated_at = now
    _media_alert(
        db, asset, status="failed", category=ErrorCategory.TRANSFER_FAILED.value, message=asset.error_message,
        download_request_id=download_request_id,
    )
    db.flush()


def transfer_asset(asset_id: int) -> bool:
    """转存一个 ``downloading`` 资产（worker 线程池执行）；返回是否成功进入 ``ready``。

    获取 ``lock:media:transfer:{asset_id}``（600s，失败返回 ``False``）→ 下载到 ``LOCAL_STORAGE_DIR/tmp/{asset_id}.part`` →
    Content-Type / 魔数 / 大小校验 → ``storage.save(media/{images|videos}/yyyy/mm/uuid.ext)`` → 资产 ``ready``（``storage_key`` /
    ``url`` / ``size_bytes`` / ``mime_type`` / ``file_hash`` / 图片 ``width`` / ``height`` / ``thumbnail_*``）+ 封面联动 + ``stats:rt``；
    失败 → ``record_transfer_failure``。每次尝试的下载结果与资产行同一事务写回根任务 ``response_meta_json.download``；
    临时文件在 ``finally`` 删除。"""
    lock_key = f"{TRANSFER_LOCK_PREFIX}{asset_id}"
    try:
        token = acquire_lock(lock_key, TRANSFER_LOCK_TTL)
    except redis.RedisError as exc:
        logger.warning("获取 %s 失败：%s", lock_key, exc)
        return False
    if not token:
        return False
    path: Path | None = None
    try:
        with SessionLocal() as db:
            asset = db.get(MediaAsset, asset_id)
            if asset is None or asset.status != "downloading":
                return False
            root = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
            cfg = media_config(db)
            kind = asset.kind
            limit_mb = (_section(cfg, "video").get("max_download_mb") or 500) if kind == "video" else (
                (cfg.get("transfer") or {}).get("max_download_mb") or 50
            )
            max_bytes = int(limit_mb) * MB
            upstream_url = asset.upstream_url
            upstream_task_id = asset.upstream_task_id or (root.upstream_task_id if root is not None else None)
            db.rollback()  # 结束读事务：下载期间不持有数据库事务

            path = _tmp_path(asset_id)
            calls: list[_DownloadCall] = []
            mime, error = _fetch_media(
                kind=kind, upstream_url=upstream_url, upstream_task_id=upstream_task_id, path=path, key=lock_key, token=token,
                max_bytes=max_bytes, calls=calls,
            )
            stored_key: str | None = None
            info: dict[str, Any] = {}
            if error is None and mime is not None:
                try:
                    info = _file_info(path, kind)
                    stored_key = build_storage_key("videos" if kind == "video" else "images", extension_for(mime) or "bin")
                    get_storage().save(stored_key, str(path), content_type=mime)
                except Exception as exc:  # noqa: BLE001 - 存储失败按一次转存失败处理
                    logger.exception("素材 %s 写入存储失败", asset_id)
                    error = f"存储写入失败：{type(exc).__name__}"
                    stored_key = None

            asset = db.get(MediaAsset, asset_id)
            if asset is None or asset.status != "downloading":
                if stored_key:
                    _delete_files([stored_key])
                return False
            root = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
            _write_download_meta(root, calls)
            if error is None and stored_key and mime:
                try:
                    _mark_ready(db, asset, key=stored_key, mime=mime, info=info, now=utcnow())
                    db.commit()
                except Exception:
                    db.rollback()
                    _delete_files([stored_key])
                    raise
                return True
            record_transfer_failure(db, asset, error)
            db.commit()
            return False
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("删除临时文件 %s 失败", path, exc_info=True)
        try:
            release_lock(lock_key, token)
        except redis.RedisError:
            logger.warning("释放 %s 失败", lock_key, exc_info=True)


def due_transfers(db: Session, *, limit: int = 10, now: datetime | None = None) -> list[int]:
    """``status='downloading' AND next_transfer_at <= now``（按 ``next_transfer_at``）且转存锁不存在的资产 ID。"""
    now = now or utcnow()
    ids = db.scalars(
        select(MediaAsset.id)
        .where(MediaAsset.status == "downloading", MediaAsset.next_transfer_at.is_not(None), MediaAsset.next_transfer_at <= now)
        .order_by(MediaAsset.next_transfer_at, MediaAsset.id)
        .limit(max(1, int(limit)))
    ).all()
    db.rollback()
    result: list[int] = []
    for asset_id in ids:
        try:
            if is_locked(f"{TRANSFER_LOCK_PREFIX}{asset_id}"):
                continue
        except redis.RedisError:
            pass
        result.append(int(asset_id))
    return result


# =====================================================================
# 人工入口：retry / transfer / delete（docs/10 §4.10、§6.4；docs/04 §6.13）
# =====================================================================


def _probe_root(old: AiTask | None, asset: MediaAsset, upstream_task_id: str) -> AiTask:
    """复查旧上游任务用的瞬态根任务（不加入会话）：``poll_task`` 只读其 ``capability`` / ``upstream_task_id``，不改写旧根任务。"""
    return AiTask(
        id=old.id if old is not None else None,
        project_id=asset.project_id,
        capability="video" if asset.kind == "video" else "image",
        operation=f"{asset.kind}_generate",
        model=(old.model if old is not None else None) or asset.model or "",
        upstream_task_id=upstream_task_id,
        response_meta_json=None,
        poll_count=0,
    )


def _new_retry_root(
    db: Session, asset: MediaAsset, old: AiTask | None, *, admin_id: int | None, route: Any = None, enqueue: bool,
) -> AiTask:
    capability = "video" if asset.kind == "video" else "image"
    data = gateway.task_input(old) if old is not None else {}
    model = data.get("model") or (old.model if old is not None else None) or asset.model
    root = gateway.create_root_task(
        db, capability=capability, operation=f"{asset.kind}_generate", project_id=asset.project_id, created_by=admin_id,
        trigger_type="user", target_type="media_asset", target_id=asset.id, input=data, route=route, model=model,
        parent_task_id=old.id if old is not None else None, enqueue=enqueue,
    )
    if route is None and old is not None:
        root.route_id = old.route_id
        root.protocol = old.protocol
    return root


def _reset_for_resume(asset: MediaAsset, root: AiTask) -> None:
    asset.ai_task_id = root.id
    asset.error_category = None
    asset.error_message = None
    asset.failed_at = None


def retry_asset(db: Session, scope: DataScope, asset_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/media/assets/{id}/retry``（``failed`` / ``expired``，其它状态 409 ``current_status``）。

    ``upstream_task_id`` 非空且资产为 ``expired`` 或 ``failed(timeout)`` → 同步复查旧上游任务：``succeeded`` → 新根任务直接
    ``succeeded``、资产 ``downloading``（``next_transfer_at=now``，API 进程不下载）；``queued`` / ``in_progress`` → 新根任务以
    ``polling`` 创建（继承 ``upstream_task_id``、新预算）；``failed`` / ``expired`` / 404 → 重新提交；复查遇其它 ``ZhiqiError`` →
    5021（``data.error_category`` / ``request_id``），状态不变。其余情况（含 ``transfer_failed`` / ``cancelled``）直接重新提交：
    新根任务 ``queued``，重新 ``check_quota`` 并计入 ``limit:images|videos``。返回 ``{asset, task_id, resumed}``。"""
    asset = get_visible(db, scope, MediaAsset, asset_id)
    if asset.status not in RETRYABLE_STATUSES:
        raise _conflict(MSG_RETRY_CONFLICT, {"current_status": asset.status})
    old = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
    upstream = asset.upstream_task_id or (old.upstream_task_id if old is not None else None)
    kind = asset.kind
    cfg = media_config(db)
    if upstream and (asset.status == "expired" or asset.error_category == ErrorCategory.TIMEOUT.value):
        try:
            status = gateway.poll_task(db, _probe_root(old, asset, upstream))
        except ZhiqiError as err:
            if err.category != ErrorCategory.ROUTE_MISSING:
                raise BusinessError(
                    gateway.UPSTREAM_ERROR_MESSAGE, code=CODE_UPSTREAM_ERROR,
                    data={"error_category": err.category.value, "request_id": err.request_id},
                ) from err
            status = None
        asset = db.get(MediaAsset, asset_id)
        if status is not None and status.status == TaskStatus.SUCCEEDED:
            now = utcnow()
            new_root = _new_retry_root(db, asset, old, admin_id=admin_id, enqueue=False)
            new_root.upstream_task_id = upstream[:80]
            new_root.started_at = now
            gateway.finalize_root(db, new_root, "succeeded", output_excerpt=status.urls[0] if status.urls else None)
            _reset_for_resume(asset, new_root)
            asset.transfer_attempts = 0
            _enter_downloading(asset, upstream_url=status.urls[0] if status.urls else None, upstream_task_id=upstream, now=now)
            return _commit_retry(db, scope, asset, new_root, resumed=True)
        if status is not None and status.status in (TaskStatus.QUEUED, TaskStatus.IN_PROGRESS):
            now = utcnow()
            new_root = _new_retry_root(db, asset, old, admin_id=admin_id, enqueue=False)
            new_root.status = "polling"
            new_root.upstream_task_id = upstream[:80]
            new_root.started_at = now
            new_root.progress = int(status.progress or 0)
            new_root.next_poll_at = now
            new_root.deadline_at = now + timedelta(seconds=poll_budget(cfg, kind))
            _reset_for_resume(asset, new_root)
            asset.status = "generating" if status.status == TaskStatus.IN_PROGRESS else "submitted"
            asset.progress = int(status.progress or 0)
            asset.upstream_task_id = upstream[:80]
            asset.upstream_url = None
            asset.next_transfer_at = None
            return _commit_retry(db, scope, asset, new_root, resumed=True)
    return _resubmit(db, scope, asset, old, admin_id=admin_id)


def _resubmit(db: Session, scope: DataScope, asset: MediaAsset, old: AiTask | None, *, admin_id: int | None) -> dict[str, Any]:
    capability = "video" if asset.kind == "video" else "image"
    data = gateway.task_input(old) if old is not None else {}
    override = data.get("model") or None
    route = gateway.preflight(db, capability, asset.project_id, model_override=override)
    daily = reserve_daily_limit(db, asset.kind, 1)
    reserved = 0
    try:
        new_root = _new_retry_root(db, asset, old, admin_id=admin_id, route=route, enqueue=True)
        estimate = gateway.estimate_route(db, route, prompt=asset.prompt or data.get("prompt") or "", completion_tokens=0)
        gateway.check_quota(db, project_id=asset.project_id, estimated_quota=estimate, task=new_root)
        reserved = estimate
        _reset_for_resume(asset, new_root)
        asset.status = "pending"
        asset.model = new_root.model or asset.model
        asset.progress = 0
        asset.transfer_attempts = 0
        asset.next_transfer_at = None
        asset.upstream_task_id = None
        asset.upstream_url = None
        return _commit_retry(db, scope, asset, new_root, resumed=False)
    except Exception:
        db.rollback()
        if reserved > 0:
            gateway.release_reservation(db, AiTask(project_id=asset.project_id, quota_reserved=reserved))
        _release_daily(daily)
        raise


def _commit_retry(db: Session, scope: DataScope, asset: MediaAsset, root: AiTask, *, resumed: bool) -> dict[str, Any]:
    db.commit()
    return {"asset": content_service.asset_item(db, scope, asset), "task_id": root.id, "resumed": resumed}


def request_transfer(db: Session, scope: DataScope, asset_id: int) -> dict[str, Any]:
    """``POST /admin/media/assets/{id}/transfer``：``failed(transfer_failed | timeout)`` 且 ``upstream_url`` 非空 → ``downloading``，
    ``transfer_attempts`` 清零、``failed_at`` 清空、``next_transfer_at=now``（API 进程不下载，由 ``retry_due`` 领取）；不新建根任务。
    其它情况 409 ``current_status``。返回 ``{asset}``。"""
    asset = get_visible(db, scope, MediaAsset, asset_id)
    if asset.status != "failed" or asset.error_category not in TRANSFERABLE_CATEGORIES or not asset.upstream_url:
        raise _conflict(MSG_TRANSFER_CONFLICT, {"current_status": asset.status})
    asset.transfer_attempts = 0
    _enter_downloading(asset, upstream_url=asset.upstream_url, upstream_task_id=None, now=utcnow())
    db.commit()
    return {"asset": content_service.asset_item(db, scope, asset)}


def _referencing_rows(db: Session, asset_id: int, *, statuses: Sequence[str] | None = None) -> list[MediaAsset]:
    """``reference_asset_ids_json ∋ asset_id`` 的其它资产（不受数据范围约束）。"""
    db.flush()
    stmt = select(MediaAsset).where(
        MediaAsset.id != asset_id, MediaAsset.reference_asset_ids_json.is_not(None),
        MediaAsset.reference_asset_ids_json.like(f"%{int(asset_id)}%"),
    )
    if statuses:
        stmt = stmt.where(MediaAsset.status.in_(list(statuses)))
    return [row for row in db.scalars(stmt.order_by(MediaAsset.id)).all() if int(asset_id) in _ref_ids(row.reference_asset_ids_json)]


def _mark_deleted(db: Session, asset: MediaAsset) -> list[str]:
    """置 ``deleted``、解绑内容（为封面时清空 ``contents.cover_asset_id``）；``url`` / ``storage_key`` 等保留作审计。返回待删除的存储键。"""
    asset.status = "deleted"
    if asset.content_id:
        content = db.get(Content, asset.content_id)
        if content is not None and content.cover_asset_id == asset.id:
            content.cover_asset_id = None
    db.execute(update(Content).where(Content.cover_asset_id == asset.id).values(cover_asset_id=None))
    asset.content_id = None
    asset.next_transfer_at = None
    keys = [k for k in (asset.storage_key, asset.thumbnail_key) if k]
    return list(dict.fromkeys(keys))


def _delete_files(keys: Iterable[str]) -> int:
    removed = 0
    storage = get_storage()
    for key in keys:
        try:
            if storage.delete(key):
                removed += 1
        except FileNotFoundError:
            continue
        except Exception:  # noqa: BLE001 - 文件删除失败只记日志（行已 deleted）
            logger.warning("删除存储对象失败 key=%s", key, exc_info=True)
    return removed


def delete_asset(db: Session, scope: DataScope, asset_id: int) -> None:
    """``DELETE /admin/media/assets/{id}``：仅 ``ready`` / ``failed`` / ``expired``（其它状态含已 ``deleted`` → 409
    ``current_status``）；``usage_type=reference`` 的上传素材被任一 ``pending`` / ``submitted`` 资产引用 → 409 ``reason=in_use``
    （按全部引用判断，不受数据范围约束）。同一事务置 ``deleted`` 并解绑内容，提交后删除存储文件。"""
    asset = get_visible(db, scope, MediaAsset, asset_id)
    if asset.status not in DELETABLE_STATUSES:
        raise _conflict(MSG_DELETE_CONFLICT, {"current_status": asset.status})
    if asset.usage_type == "reference" and _referencing_rows(db, asset.id, statuses=IN_USE_STATUSES):
        raise _conflict(MSG_IN_USE, {"reason": "in_use"})
    try:
        keys = _mark_deleted(db, asset)
        after_commit(db, lambda: _delete_files(keys))
        db.commit()
    except Exception:
        db.rollback()
        raise


# =====================================================================
# 查询（docs/10 §6.1、§6.3；docs/13 §4.2、§6.1）
# =====================================================================


def asset_task_summary(root: AiTask | None) -> dict[str, Any] | None:
    """``{task_id, operation, status, progress, error_category, error_message, model_override, finished_at}``（``model_override`` 取
    根任务 ``input.model``）。"""
    return content_service.task_summary(root) if root is not None else None


def asset_references(db: Session, scope: DataScope, asset: MediaAsset) -> dict[str, Any]:
    """``references{cover_of, bound_content_id, referenced_by_asset_ids[{id,status}], count}``（实时计算；引用方只列调用者可见的素材）。"""
    db.flush()
    cover_of = db.scalar(select(Content.id).where(Content.cover_asset_id == asset.id).order_by(Content.id).limit(1))
    rows = _referencing_rows(db, asset.id)
    if rows and scope.restricted:
        visible = set(db.scalars(scope_media(select(MediaAsset.id).where(MediaAsset.id.in_([r.id for r in rows])), scope)).all())
        rows = [r for r in rows if r.id in visible]
    referenced = [{"id": r.id, "status": r.status} for r in rows]
    return {
        "cover_of": cover_of,
        "bound_content_id": asset.content_id,
        "referenced_by_asset_ids": referenced,
        "count": (1 if cover_of else 0) + (1 if asset.content_id else 0) + len(referenced),
    }


def list_assets(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    project_id: int | None = None,
    content_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    usage_type: str | None = None,
    source: str | None = None,
    created_by: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """``GET /admin/media/assets``：按 ``created_at DESC, id DESC`` 分页（``page_size`` ≤ 100），按数据范围过滤；不计算 ``references``。"""
    conditions: list[Any] = []
    for column, value in (
        (MediaAsset.project_id, project_id), (MediaAsset.content_id, content_id), (MediaAsset.kind, kind),
        (MediaAsset.status, status), (MediaAsset.usage_type, usage_type), (MediaAsset.source, source),
        (MediaAsset.created_by, created_by),
    ):
        if value is not None and value != "":
            conditions.append(column == value)
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    total = int(db.scalar(scope_media(select(func.count(MediaAsset.id)), scope).where(*conditions)) or 0)
    rows = db.scalars(
        scope_media(select(MediaAsset), scope).where(*conditions)
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return [content_service.asset_item(db, scope, row) for row in rows], total


def get_asset_detail(db: Session, scope: DataScope, asset_id: int) -> dict[str, Any]:
    """``GET /admin/media/assets/{id}``：``AssetOut`` + 根任务摘要 ``task`` + 引用 ``references``。"""
    asset = get_visible(db, scope, MediaAsset, asset_id)
    item = content_service.asset_item(db, scope, asset)
    root = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
    item["task"] = asset_task_summary(root)
    item["references"] = asset_references(db, scope, asset)
    return item


def get_asset_task(db: Session, scope: DataScope, asset_id: int) -> dict[str, Any] | None:
    """``GET /admin/media/assets/{id}/task``：当前根任务摘要（备选回退 / 重试后自动指向新根任务）；上传素材无任务返回 ``None``。"""
    asset = get_visible(db, scope, MediaAsset, asset_id)
    root = db.get(AiTask, asset.ai_task_id) if asset.ai_task_id else None
    return asset_task_summary(root)


# =====================================================================
# 上传参考素材（POST /admin/uploads/image|video；docs/10 §6.5、§11.1；docs/13 §7.4、§12.2）
# =====================================================================

UPLOAD_EXTENSIONS: dict[str, dict[str, str]] = {
    "image": {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"},
    "video": {"mp4": "video/mp4", "mov": "video/quicktime"},
}
UPLOAD_CHUNK = 1024 * 1024
MSG_UPLOAD_TYPE = {"image": "仅支持 jpg、png、webp、gif 图片", "video": "仅支持 mp4、mov 视频"}
MSG_UPLOAD_MISMATCH = "文件内容与扩展名不符"
MSG_UPLOAD_EMPTY = "文件内容为空"


def upload_max_bytes(kind: str) -> int:
    """``MAX_IMAGE_SIZE_MB``（默认 10）/ ``MAX_VIDEO_SIZE_MB``（默认 200）→ 字节数。"""
    mb = settings.max_video_size_mb if kind == "video" else settings.max_image_size_mb
    return max(1, int(mb or 0)) * MB


def is_public_media_url(url: str | None) -> bool:
    """上传响应的 ``public``：真实模式 = ``normalize_public_url`` + ``assert_public_url`` 全部通过（``DEV_MODE=false`` 时仅 https、端口
    80/443/缺省、主机解析为公网地址，docs/10 §11.1）；Mock 模式恒为 ``True``（放行本地地址）。"""
    if not url:
        return False
    if settings.zhiqi_mock_mode:
        return True
    try:
        safe_fetch.normalize_public_url(url, allow_http=settings.dev_mode)
        safe_fetch.assert_public_url(url, allow_http=settings.dev_mode)
    except (safe_fetch.FetchError, TypeError, ValueError):
        return False
    return True


def _upload_error(message: str, error_type: str, filename: str | None) -> BusinessError:
    return invalid_params(field_error(["body", "file"], message, error_type, filename))


def _upload_extension(kind: str, filename: str | None) -> str:
    name = (filename or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in UPLOAD_EXTENSIONS[kind]:
        raise _upload_error(MSG_UPLOAD_TYPE[kind], "file_type", filename)
    return ext


def _spool_upload(stream: BinaryIO, dest: Path, max_bytes: int, filename: str | None, kind: str) -> dict[str, Any]:
    """流式写入临时文件并计算 SHA-256 / 大小 / 文件头；超过 ``max_bytes`` 立即中止（400 ``file_too_large``）。"""
    digest = hashlib.sha256()
    head = b""
    size = 0
    with dest.open("wb") as fh:
        while True:
            chunk = stream.read(UPLOAD_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise _upload_error(f"文件大小不能超过 {max_bytes // MB} MB", "file_too_large", filename)
            if len(head) < PROBE_HEAD_BYTES:
                head += chunk[: PROBE_HEAD_BYTES - len(head)]
            digest.update(chunk)
            fh.write(chunk)
    if size <= 0:
        raise _upload_error(MSG_UPLOAD_EMPTY, "file_empty", filename)
    dims = probe_image_size(head) if kind == "image" else None
    return {
        "size_bytes": size, "file_hash": digest.hexdigest(), "head": head,
        "width": dims[0] if dims else None, "height": dims[1] if dims else None,
    }


def upload_asset(
    db: Session, scope: DataScope, kind: str, stream: BinaryIO, filename: str | None, *, admin_id: int,
) -> dict[str, Any]:
    """``POST /admin/uploads/image|video``：扩展名（图片 jpg/png/webp/gif，视频 mp4/mov）与魔数 ``sniff_media_type`` 双重校验（图片
    魔数须与扩展名一致；mp4/mov 均为 ``ftyp`` 容器，按魔数识别的类型落库），大小 ≤ ``MAX_IMAGE_SIZE_MB`` / ``MAX_VIDEO_SIZE_MB``，
    任一不满足 400（``loc=["body","file"]``）。落 ``media_assets(kind, source=uploaded, usage_type=reference, status=ready,
    ready_at=now, project_id=NULL, created_by=上传人)``，``storage_key=media/uploads/{yyyy}/{mm}/{uuid}.{ext}``（文件名不入库），
    图片 ``width/height=probe_image_size``、``thumbnail_key=storage_key``，视频 ``thumbnail_key=NULL``。不做 ``file_hash`` 去重，
    不计 ``images_generated``（报表只计 ``source=generated``）。

    归属：上传素材一律按上传人（``admin_id``）归属，只对上传人与 ``all`` 范围可见；总后台处于某用户视角（``scope.owner_id``）时
    同样归属总后台本人（docs/13 §7.4、§12.2），``scope`` 不改变归属。返回 ``{asset_id, url, public}``。"""
    if kind not in UPLOAD_EXTENSIONS:
        raise ValueError(f"未知素材类型: {kind}")
    del scope  # 归属固定为上传人（docs/13 §12.2），数据范围不参与写入判定
    ext = _upload_extension(kind, filename)
    tmp = _tmp_dir() / f"upload-{uuid.uuid4().hex}.part"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    storage = get_storage()
    key: str | None = None
    try:
        info = _spool_upload(stream, tmp, upload_max_bytes(kind), filename, kind)
        sniffed = sniff_media_type(info["head"])
        if kind == "image":
            if sniffed is None or sniffed != UPLOAD_EXTENSIONS["image"][ext]:
                raise _upload_error(MSG_UPLOAD_MISMATCH, "file_type", filename)
        elif sniffed not in VIDEO_MIME_TYPES:
            raise _upload_error(MSG_UPLOAD_MISMATCH, "file_type", filename)
        mime = str(sniffed)
        key = build_storage_key("uploads", extension_for(mime) or ext)
        storage.save(key, tmp, content_type=mime)
        url = public_url_for(key)
        now = utcnow()
        asset = MediaAsset(
            project_id=None, content_id=None, kind=kind, usage_type="reference", source="uploaded", status="ready",
            storage_key=key, url=url, thumbnail_key=key if kind == "image" else None, thumbnail_url=url if kind == "image" else None,
            mime_type=mime, size_bytes=int(info["size_bytes"]), file_hash=str(info["file_hash"]),
            width=info["width"] if kind == "image" else None, height=info["height"] if kind == "image" else None,
            progress=100, transfer_attempts=0, ready_at=now, sort=0, created_by=int(admin_id),
        )
        db.add(asset)
        db.commit()
    except Exception:
        db.rollback()
        if key is not None:
            _delete_files([key])
        raise
    finally:
        tmp.unlink(missing_ok=True)
    return {"asset_id": asset.id, "url": url, "public": is_public_media_url(url)}


# =====================================================================
# 清理（docs/10 §6.4）
# =====================================================================


def _as_timestamp(value: datetime) -> float:
    return value.replace(tzinfo=UTC).timestamp() if value.tzinfo is None else value.timestamp()


def cleanup_media(now: datetime | None = None, *, batch_size: int = 500) -> dict[str, int]:
    """``cleanup_media.cleanup()``（每日 03:00）：

    - 失败残留：``failed`` / ``expired`` 且 ``failed_at < now − retention.failed_days`` → 删除 ``tmp/{asset_id}.part`` 与无 ``url`` 的
      ``storage_key`` 对象（记录行保留）；``LOCAL_STORAGE_DIR/tmp/`` 下 ``mtime`` 超过 1 天的 ``*.part`` 无条件删除；
    - 孤儿参考素材：``source=uploaded`` 且 ``usage_type=reference`` 且 ``status=ready`` 且 ``created_at < now − retention.orphan_reference_days``
      且未出现在任何 ``reference_asset_ids_json`` 中 → 同手工删除（``deleted`` + 删除存储文件，列保留作审计）。

    返回 ``{failed_cleaned, orphans_deleted}``。"""
    now = now or utcnow()
    failed_cleaned = 0
    orphans_deleted = 0
    with SessionLocal() as db:
        retention = media_config(db).get("retention") or {}
        failed_days = max(1, int(retention.get("failed_days") or 30))
        orphan_days = max(1, int(retention.get("orphan_reference_days") or 7))
        tmp_dir = _tmp_dir()

        cutoff = now - timedelta(days=failed_days)
        last_id = 0
        while True:
            rows = db.scalars(
                select(MediaAsset)
                .where(MediaAsset.status.in_(("failed", "expired")), MediaAsset.failed_at.is_not(None),
                       MediaAsset.failed_at < cutoff, MediaAsset.id > last_id)
                .order_by(MediaAsset.id).limit(batch_size)
            ).all()
            if not rows:
                break
            for asset in rows:
                last_id = asset.id
                removed = False
                part = tmp_dir / f"{asset.id}.part"
                try:
                    if part.exists():
                        part.unlink()
                        removed = True
                except OSError:
                    logger.warning("删除 %s 失败", part, exc_info=True)
                if asset.storage_key and not asset.url and _delete_files([asset.storage_key]):
                    removed = True
                failed_cleaned += int(removed)
        db.rollback()

        if tmp_dir.is_dir():
            threshold = _as_timestamp(now) - 86400
            for part in tmp_dir.glob("*.part"):
                try:
                    if part.stat().st_mtime < threshold:
                        part.unlink()
                        failed_cleaned += 1
                except FileNotFoundError:
                    continue
                except OSError:
                    logger.warning("删除 %s 失败", part, exc_info=True)

        referenced: set[int] = set()
        for raw in db.scalars(select(MediaAsset.reference_asset_ids_json).where(MediaAsset.reference_asset_ids_json.is_not(None))):
            referenced.update(_ref_ids(raw))
        orphan_cutoff = now - timedelta(days=orphan_days)
        orphans = db.scalars(
            select(MediaAsset)
            .where(MediaAsset.source == "uploaded", MediaAsset.usage_type == "reference", MediaAsset.status == "ready",
                   MediaAsset.created_at < orphan_cutoff)
            .order_by(MediaAsset.id)
        ).all()
        keys: list[str] = []
        for asset in orphans:
            if asset.id in referenced:
                continue
            keys.extend(_mark_deleted(db, asset))
            orphans_deleted += 1
        if orphans_deleted:
            db.commit()
            _delete_files(keys)
        else:
            db.rollback()
    result = {"failed_cleaned": failed_cleaned, "orphans_deleted": orphans_deleted}
    logger.info("cleanup_media 完成：%s", result)
    return result
