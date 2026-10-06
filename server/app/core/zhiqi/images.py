"""图片生成（docs/08-zhiqiapi-integration.md §3.7、§5.6；docs/10-media-generation.md §4.1）。

默认异步：``POST /v1/images/generations/async`` → ``GET /v1/images/generations/{task_id}`` 轮询；
同步回退 ``POST /v1/images/generations`` / ``POST /v1/images/edits``（multipart，``image`` 为重复的公网 URL 字段）
由 ``ai_gateway_service.submit_image`` 决定是否调用，本模块不做任何回退与同尝试行内重试。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.core.exceptions import (
    CODE_BAD_REQUEST,
    CODE_PUBLIC_URL_REQUIRED,
    BusinessError,
    field_error,
)
from app.core.zhiqi.client import ZhiqiClient, ZhiqiResponse
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import (
    ErrorCategory,
    ImageRequest,
    ImageSubmitResult,
    ImageTaskStatus,
    RetryPolicy,
    TaskStatus,
)

IMAGE_RESOLUTIONS = ("1080p", "2k", "4k")
IMAGE_ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16")
MAX_REFERENCE_IMAGES = 9
MAX_PROMPT_CHARS = 4000
EDIT_EXTRA_FIELDS_ALLOWED = ("resolution", "aspect_ratio")

ASYNC_PATH = "/v1/images/generations/async"
SYNC_PATH = "/v1/images/generations"
EDIT_PATH = "/v1/images/edits"


def build_async_payload(req: ImageRequest) -> dict[str, Any]:
    """异步 / 同步共用请求体：不含 ``size`` / ``quality`` / ``ratio``；``n`` 固定 1；无参考图时不发送 ``reference_image_urls``。"""
    payload: dict[str, Any] = {
        "model": req.model,
        "prompt": req.prompt,
        "n": 1,
        "resolution": req.resolution,
        "aspect_ratio": req.aspect_ratio,
        "response_format": req.response_format or "url",
    }
    if req.reference_image_urls:
        payload["reference_image_urls"] = list(req.reference_image_urls)
    return payload


def response_dict(response: ZhiqiResponse) -> dict[str, Any]:
    if not isinstance(response.json, dict):
        raise ZhiqiError(
            ErrorCategory.INVALID_RESPONSE, "图片接口响应不是 JSON 对象", http_status=response.http_status, request_id=response.request_id
        )
    return response.json


def extract_urls(body: dict[str, Any]) -> list[str]:
    """``data[].url``（兼容顶层 ``url``）。"""
    urls: list[str] = []
    data = body.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].strip():
                urls.append(item["url"].strip())
            elif isinstance(item, str) and item.strip():
                urls.append(item.strip())
    elif isinstance(data, dict) and isinstance(data.get("url"), str):
        urls.append(data["url"].strip())
    if not urls and isinstance(body.get("url"), str) and body["url"].strip():
        urls.append(body["url"].strip())
    return urls


def parse_task_status(value: Any, response: ZhiqiResponse) -> TaskStatus:
    try:
        return TaskStatus(str(value).lower())
    except ValueError as exc:
        raise ZhiqiError(
            ErrorCategory.INVALID_RESPONSE, f"未知的任务状态：{value!r}", http_status=response.http_status, request_id=response.request_id
        ) from exc


def extract_error(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """失败体 ``error_code`` / ``error_message``（字段名未核实；兼容 ``error{code,message}`` 与 ``error`` 字符串）。"""
    code = body.get("error_code")
    message = body.get("error_message")
    err = body.get("error")
    if isinstance(err, dict):
        code = code or err.get("code") or err.get("type")
        message = message or err.get("message")
    elif isinstance(err, str) and not message:
        message = err
    if not message and isinstance(body.get("fail_reason"), str):
        message = body["fail_reason"]
    return (str(code) if code else None), (str(message) if message else None)


def parse_progress(value: Any, status: TaskStatus) -> int:
    if status == TaskStatus.SUCCEEDED:
        return 100
    try:
        progress = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(99, progress))


def submit_async(client: ZhiqiClient, req: ImageRequest, *, timeout: float, retry: RetryPolicy | None = None) -> ImageSubmitResult:
    """``POST /v1/images/generations/async``，期望 ``202 {id:"task_…", status:"queued"}``；失败直接抛 ``ZhiqiError``。"""
    response = client.post(ASYNC_PATH, json=build_async_payload(req), timeout=timeout, retry=retry)
    body = response_dict(response)
    task_id = body.get("id") or body.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ZhiqiError(
            ErrorCategory.INVALID_RESPONSE, "异步提交响应缺少任务 id", http_status=response.http_status, request_id=response.request_id, raw=body
        )
    status = parse_task_status(body.get("status") or TaskStatus.QUEUED.value, response)
    return ImageSubmitResult(
        mode="async", task_id=task_id.strip(), status=status, urls=[], request_id=response.request_id,
        latency_ms=response.latency_ms, http_status=response.http_status, raw=body,
    )


def get_generation(client: ZhiqiClient, task_id: str, *, timeout: float, retry: RetryPolicy | None = None) -> ImageTaskStatus:
    """``GET /v1/images/generations/{task_id}``（幂等，按 ``retry`` 重试）。"""
    response = client.get(f"{SYNC_PATH}/{quote(task_id, safe='')}", timeout=timeout, retry=retry)
    body = response_dict(response)
    status = parse_task_status(body.get("status"), response)
    error_code, error_message = extract_error(body) if status in (TaskStatus.FAILED, TaskStatus.EXPIRED) else (None, None)
    return ImageTaskStatus(
        task_id=str(body.get("id") or task_id), status=status, progress=parse_progress(body.get("progress"), status),
        urls=extract_urls(body) if status == TaskStatus.SUCCEEDED else [], error_code=error_code, error_message=error_message,
        request_id=response.request_id, raw=body,
    )


def _sync_result(mode: str, response: ZhiqiResponse) -> ImageSubmitResult:
    body = response_dict(response)
    urls = extract_urls(body)
    if not urls:
        raise ZhiqiError(
            ErrorCategory.INVALID_RESPONSE, "同步图片响应缺少 data[].url", http_status=response.http_status, request_id=response.request_id, raw=body
        )
    return ImageSubmitResult(
        mode=mode, task_id=None, status=TaskStatus.SUCCEEDED, urls=urls, request_id=response.request_id,
        latency_ms=response.latency_ms, http_status=response.http_status, raw=body,
    )


def generate_sync(client: ZhiqiClient, req: ImageRequest, *, timeout: float) -> ImageSubmitResult:
    """``POST /v1/images/generations`` → ``200 {data:[{url}]}``（同步回退，不重试）。"""
    return _sync_result("sync", client.post(SYNC_PATH, json=build_async_payload(req), timeout=timeout))


def build_edit_form(req: ImageRequest, extra_fields: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    """``/v1/images/edits`` multipart 字段：``image`` × N（公网 URL）、``model``、``prompt``、``response_format=url``，
    再追加 ``extra_fields``（``media_config.image.edit_extra_fields``，只允许 resolution / aspect_ratio）。"""
    form: list[tuple[str, str]] = [("image", url) for url in req.reference_image_urls]
    form += [("model", req.model), ("prompt", req.prompt), ("response_format", req.response_format or "url")]
    for name in extra_fields:
        if name not in EDIT_EXTRA_FIELDS_ALLOWED:
            continue
        value = getattr(req, name, None)
        if value is not None and value != "":
            form.append((name, str(value)))
    return form


def edit_sync(client: ZhiqiClient, req: ImageRequest, *, timeout: float, extra_fields: tuple[str, ...] = ()) -> ImageSubmitResult:
    """``POST /v1/images/edits``（multipart；``unsupported_parameter`` 直接抛出，由网关按 ``fallback_on`` 切换候选）。"""
    response = client.post(EDIT_PATH, data=build_edit_form(req, tuple(extra_fields or ())), multipart=True, timeout=timeout)
    return _sync_result("edit", response)


# ---------------------------------------------------------------- 校验


def _check_public_urls(urls: list[str]) -> list[str]:
    """真实模式下返回不合格（非公网）的 URL；Mock 模式放行（本地地址亦可）。"""
    if settings.zhiqi_mock_mode:
        return []
    from app.core import safe_fetch

    bad: list[str] = []
    for url in urls:
        try:
            safe_fetch.normalize_public_url(url, allow_http=settings.dev_mode)
            safe_fetch.assert_public_url(url, allow_http=settings.dev_mode)
        except (safe_fetch.FetchError, TypeError):
            bad.append(url)
    return bad


def raise_if_not_public(urls: list[str]) -> None:
    bad = _check_public_urls([u for u in urls if u])
    if bad:
        raise BusinessError("参考素材 URL 非公网", code=CODE_PUBLIC_URL_REQUIRED, data={"urls": bad})


def validate_request(req: ImageRequest, media_config: dict) -> None:
    """枚举（``media_config.image.allowed_*``）、``n=1``、提示词非空且 ≤ 4000、参考图数量 ≤ ``max_reference_images``（≤ 9）
    → 400 校验错误列表；真实模式参考 URL 非公网 → 4222 ``data={"urls":[…]}``。``media_config`` 可传完整配置或其 ``image`` 节。"""
    cfg = media_config.get("image", media_config) if isinstance(media_config, dict) else {}
    allowed_resolutions = [r for r in (cfg.get("allowed_resolutions") or IMAGE_RESOLUTIONS) if r in IMAGE_RESOLUTIONS]
    allowed_ratios = [r for r in (cfg.get("allowed_aspect_ratios") or IMAGE_ASPECT_RATIOS) if r in IMAGE_ASPECT_RATIOS]
    max_refs = min(MAX_REFERENCE_IMAGES, int(cfg.get("max_reference_images", MAX_REFERENCE_IMAGES)))
    errors: list[dict[str, Any]] = []
    if not (req.prompt or "").strip():
        errors.append(field_error(["body", "prompt"], "提示词不能为空", "value_error", req.prompt))
    elif len(req.prompt) > MAX_PROMPT_CHARS:
        errors.append(field_error(["body", "prompt"], f"长度不能超过 {MAX_PROMPT_CHARS} 个字符", "string_too_long", req.prompt))
    if req.resolution not in allowed_resolutions:
        errors.append(field_error(["body", "resolution"], f"取值必须是 {'、'.join(allowed_resolutions)} 之一", "enum", req.resolution))
    if req.aspect_ratio not in allowed_ratios:
        errors.append(field_error(["body", "aspect_ratio"], f"取值必须是 {'、'.join(allowed_ratios)} 之一", "enum", req.aspect_ratio))
    if req.n != 1:
        errors.append(field_error(["body", "n"], "n 固定为 1", "value_error", req.n))
    refs = list(req.reference_image_urls or [])
    if len(refs) > max_refs:
        errors.append(field_error(["body", "reference_image_urls"], f"最多允许 {max_refs} 项", "too_long", refs))
    for index, url in enumerate(refs):
        if not isinstance(url, str) or not url.strip():
            errors.append(field_error(["body", "reference_image_urls", index], "URL 不能为空", "value_error", url))
    if errors:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=errors)
    raise_if_not_public(refs)
