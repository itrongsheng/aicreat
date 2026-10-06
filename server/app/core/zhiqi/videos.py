"""视频任务（docs/08-zhiqiapi-integration.md §3.8、§5.7；docs/10-media-generation.md §5.1）。

``POST /v1/videos``（必须 JSON）→ ``GET /v1/videos/{id}`` 轮询 → ``GET /v1/videos/{id}/content`` 字节流下载。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, BinaryIO
from urllib.parse import quote

from app.core.exceptions import CODE_BAD_REQUEST, BusinessError, field_error
from app.core.zhiqi.client import ZhiqiClient
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.images import (
    MAX_PROMPT_CHARS,
    MAX_REFERENCE_IMAGES,
    extract_error,
    extract_urls,
    parse_progress,
    parse_task_status,
    raise_if_not_public,
    response_dict,
)
from app.core.zhiqi.types import (
    DownloadResult,
    ErrorCategory,
    RetryPolicy,
    TaskStatus,
    VideoRequest,
    VideoSubmitResult,
    VideoTaskStatus,
)

VIDEO_RESOLUTIONS = ("480p", "720p", "1080p", "4k")
MAX_REFERENCE_VIDEOS = 3
MAX_REFERENCE_AUDIOS = 1
MAX_NEGATIVE_PROMPT_CHARS = 2000
DEFAULT_MAX_DURATION = 15
ASPECT_RATIO_RE = re.compile(r"^[1-9]\d{0,4}:[1-9]\d{0,4}$")
SIZE_RE = re.compile(r"^[1-9]\d{1,4}x[1-9]\d{1,4}$")

VIDEOS_PATH = "/v1/videos"


def build_payload(req: VideoRequest) -> dict[str, Any]:
    """只发送非 None 字段（列表为空不发送）；``aspect_ratio`` 与 ``size`` 二选一（同时给出保留 ``aspect_ratio``）；
    只用主字段名 ``duration`` / ``aspect_ratio`` / ``input_reference``，不发别名。"""
    payload: dict[str, Any] = {"model": req.model, "prompt": req.prompt}
    if req.duration is not None:
        payload["duration"] = int(req.duration)
    if req.resolution is not None:
        payload["resolution"] = req.resolution
    if req.aspect_ratio is not None:
        payload["aspect_ratio"] = req.aspect_ratio
    elif req.size is not None:
        payload["size"] = req.size
    if req.input_reference is not None:
        payload["input_reference"] = req.input_reference
    for name in ("reference_image_urls", "reference_video_urls", "reference_audio_urls"):
        values = getattr(req, name) or []
        if values:
            payload[name] = list(values)
    for name in ("first_frame_image_url", "last_frame_image_url", "negative_prompt"):
        value = getattr(req, name)
        if value is not None:
            payload[name] = value
    if req.generate_audio is not None:
        payload["generate_audio"] = bool(req.generate_audio)
    if req.n is not None:
        payload["n"] = int(req.n)
    return payload


def submit_video(client: ZhiqiClient, req: VideoRequest, *, timeout: float, retry: RetryPolicy | None = None) -> VideoSubmitResult:
    """``POST /v1/videos``（JSON，禁止 multipart）→ ``200 {id:"vidtask_…", status:"queued"}``。"""
    response = client.post(VIDEOS_PATH, json=build_payload(req), timeout=timeout, retry=retry)
    body = response_dict(response)
    task_id = body.get("id") or body.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ZhiqiError(
            ErrorCategory.INVALID_RESPONSE, "视频提交响应缺少任务 id", http_status=response.http_status, request_id=response.request_id, raw=body
        )
    status = parse_task_status(body.get("status") or TaskStatus.QUEUED.value, response)
    return VideoSubmitResult(
        task_id=task_id.strip(), status=status, request_id=response.request_id, latency_ms=response.latency_ms,
        http_status=response.http_status, raw=body,
    )


def parse_expires_at(value: Any) -> datetime | None:
    """``expires_at``（字段名未核实）：Unix 秒 / 毫秒或 ISO 字符串 → naive UTC。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) / 1000 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None, microsecond=0)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return parse_expires_at(int(text))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    return None


def get_video(client: ZhiqiClient, task_id: str, *, timeout: float, retry: RetryPolicy | None = None) -> VideoTaskStatus:
    """``GET /v1/videos/{id}``（幂等）。"""
    response = client.get(f"{VIDEOS_PATH}/{quote(task_id, safe='')}", timeout=timeout, retry=retry)
    body = response_dict(response)
    status = parse_task_status(body.get("status"), response)
    error_code, error_message = extract_error(body) if status in (TaskStatus.FAILED, TaskStatus.EXPIRED) else (None, None)
    urls = extract_urls(body) if status == TaskStatus.SUCCEEDED else []
    return VideoTaskStatus(
        task_id=str(body.get("id") or task_id), status=status, progress=parse_progress(body.get("progress"), status),
        urls=urls, error_code=error_code, error_message=error_message, expires_at=parse_expires_at(body.get("expires_at")),
        request_id=response.request_id, raw=body,
    )


def download_content(client: ZhiqiClient, task_id: str, dest: BinaryIO, *, max_bytes: int, timeout: float) -> DownloadResult:
    """``GET /v1/videos/{id}/content``（相对路径，带 Bearer）→ ``DownloadResult(source="content", …)``；
    失败抛 ``ZhiqiError(TRANSFER_FAILED)``，同样携带 ``request_id`` / ``http_status``。"""
    return client.stream_download(
        f"{VIDEOS_PATH}/{quote(task_id, safe='')}/content", dest, max_bytes=max_bytes, timeout=timeout, source="content"
    )


def validate_request(req: VideoRequest, media_config: dict) -> None:
    """分辨率枚举（``video.allowed_resolutions``）、``1 ≤ duration ≤ video.max_duration``、``aspect_ratio`` / ``size`` 格式、
    ``input_reference`` 与 ``first_frame_image_url`` 互斥、``last_frame_image_url`` 须与首帧同时出现、参考素材数量
    → 400 校验错误列表；真实模式全部参考 URL（含 ``reference_audio_urls``）须公网 → 4222。``media_config`` 为完整配置。"""
    media_config = media_config if isinstance(media_config, dict) else {}
    vcfg = media_config.get("video") or {}
    icfg = media_config.get("image") or {}
    allowed_resolutions = [r for r in (vcfg.get("allowed_resolutions") or VIDEO_RESOLUTIONS) if r in VIDEO_RESOLUTIONS]
    max_duration = int(vcfg.get("max_duration", DEFAULT_MAX_DURATION))
    max_ref_images = min(MAX_REFERENCE_IMAGES, int(icfg.get("max_reference_images", MAX_REFERENCE_IMAGES)))
    errors: list[dict[str, Any]] = []
    if not (req.prompt or "").strip():
        errors.append(field_error(["body", "prompt"], "提示词不能为空", "value_error", req.prompt))
    elif len(req.prompt) > MAX_PROMPT_CHARS:
        errors.append(field_error(["body", "prompt"], f"长度不能超过 {MAX_PROMPT_CHARS} 个字符", "string_too_long", req.prompt))
    if req.negative_prompt is not None and len(req.negative_prompt) > MAX_NEGATIVE_PROMPT_CHARS:
        errors.append(
            field_error(["body", "negative_prompt"], f"长度不能超过 {MAX_NEGATIVE_PROMPT_CHARS} 个字符", "string_too_long", req.negative_prompt)
        )
    if req.resolution is not None and req.resolution not in allowed_resolutions:
        errors.append(field_error(["body", "resolution"], f"取值必须是 {'、'.join(allowed_resolutions)} 之一", "enum", req.resolution))
    if req.duration is not None:
        if isinstance(req.duration, bool) or not isinstance(req.duration, int):
            errors.append(field_error(["body", "duration"], "必须是整数", "int_type", req.duration))
        elif not 1 <= req.duration <= max_duration:
            errors.append(field_error(["body", "duration"], f"时长必须在 1~{max_duration} 秒之间", "value_error", req.duration))
    if req.aspect_ratio is not None and not ASPECT_RATIO_RE.match(req.aspect_ratio):
        errors.append(field_error(["body", "aspect_ratio"], "格式应为 宽:高，如 16:9", "value_error", req.aspect_ratio))
    if req.aspect_ratio is None and req.size is not None and not SIZE_RE.match(req.size):
        errors.append(field_error(["body", "size"], "格式应为 宽x高，如 1280x720", "value_error", req.size))
    if req.input_reference and req.first_frame_image_url:
        errors.append(field_error(["body", "input_reference"], "input_reference 不能与 first_frame_image_url 同时使用", "value_error", req.input_reference))
    if req.last_frame_image_url and not req.first_frame_image_url:
        errors.append(field_error(["body", "last_frame_image_url"], "last_frame_image_url 须与 first_frame_image_url 同时出现", "value_error", req.last_frame_image_url))
    for name, limit in (
        ("reference_image_urls", max_ref_images), ("reference_video_urls", MAX_REFERENCE_VIDEOS), ("reference_audio_urls", MAX_REFERENCE_AUDIOS),
    ):
        values = list(getattr(req, name) or [])
        if len(values) > limit:
            errors.append(field_error(["body", name], f"最多允许 {limit} 项", "too_long", values))
        for index, url in enumerate(values):
            if not isinstance(url, str) or not url.strip():
                errors.append(field_error(["body", name, index], "URL 不能为空", "value_error", url))
    if req.n != 1:
        errors.append(field_error(["body", "n"], "n 固定为 1", "value_error", req.n))
    if errors:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=errors)
    urls = [u for u in (req.input_reference, req.first_frame_image_url, req.last_frame_image_url) if u]
    urls += list(req.reference_image_urls or []) + list(req.reference_video_urls or []) + list(req.reference_audio_urls or [])
    raise_if_not_public(urls)
