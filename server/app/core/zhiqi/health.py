"""单模型最小探测（docs/08-zhiqiapi-integration.md §5.10、§12.1）。

文本能力：最小 ``TextRequest``（不重试、不带 tools），timeout 固定 30s；
image / video：``probe_media=False`` 不发 HTTP，按调用方查得的 ``model_available``（``ai_models.is_available``，无行传 ``None``）
判定 healthy / down(model_unrouted) / unknown；``probe_media=True`` 时提交最小任务并立刻返回（不等待完成）。
窗口规则（§12.4）与写入 ``ai:health:*`` / ``ai_models`` 由调用方（``health_probe`` 任务、一键测试接口）完成。
"""

from __future__ import annotations

import time

from app.core.zhiqi import images, text, videos
from app.core.zhiqi.client import ZhiqiClient
from app.core.zhiqi.errors import ZhiqiError, sanitize_error_message
from app.core.zhiqi.types import (
    TEXT_CAPABILITIES,
    Capability,
    ErrorCategory,
    HealthResult,
    ImageRequest,
    Protocol,
    TextRequest,
    VideoRequest,
    utcnow,
)

DEFAULT_DEGRADED_LATENCY_MS = 15000
PROBE_IMAGE_PROMPT = "A simple flat icon of a blue circle on a white background"
PROBE_VIDEO_PROMPT = "A blue circle slowly moving across a white background"


def status_from(latency_ms: int, error: ZhiqiError | None, degraded_latency_ms: int) -> str:
    """无错误且 ``latency_ms < degraded_latency_ms`` → healthy；无错误但延迟超阈值 → degraded；有错误 → down。"""
    if error is not None:
        return "down"
    return "healthy" if latency_ms < degraded_latency_ms else "degraded"


def probe(
    client: ZhiqiClient,
    *,
    capability: Capability,
    model: str,
    protocol: Protocol,
    prompt: str = "ping",
    max_tokens: int = 8,
    timeout: float = 30,
    probe_media: bool = False,
    model_available: bool | None = None,
    degraded_latency_ms: int = DEFAULT_DEGRADED_LATENCY_MS,
) -> HealthResult:
    """单模型探测；失败不抛异常，以 ``status="down"`` + ``error_category`` / ``error_message`` 返回。"""
    capability = Capability(capability)
    protocol = Protocol(protocol)
    checked_at = utcnow()

    if capability not in TEXT_CAPABILITIES and not probe_media:
        if model_available is True:
            status, category, message = "healthy", None, None
        elif model_available is False:
            status, category, message = "down", ErrorCategory.MODEL_UNROUTED, "模型不在 /v1/models 目录"
        else:
            status, category, message = "unknown", None, None
        return HealthResult(
            capability=capability, model=model, protocol=protocol, status=status, latency_ms=0, request_id=None,
            error_category=category, error_message=message, checked_at=checked_at,
        )

    started = time.monotonic()
    request_id: str | None = None
    error: ZhiqiError | None = None
    try:
        if capability in TEXT_CAPABILITIES:
            req = TextRequest(
                model=model, protocol=protocol, messages=[{"role": "user", "content": prompt}], max_tokens=max_tokens,
                metadata={"capability": capability.value, "operation": "route_probe"},
            )
            result = text.complete(client, req, timeout=timeout, retry=None, passthrough={})
            request_id = result.request_id
        elif capability == Capability.IMAGE:
            submitted = images.submit_async(
                client, ImageRequest(model=model, prompt=PROBE_IMAGE_PROMPT, resolution="1080p", aspect_ratio="1:1"), timeout=timeout
            )
            request_id = submitted.request_id
        else:
            submitted_video = videos.submit_video(client, VideoRequest(model=model, prompt=PROBE_VIDEO_PROMPT), timeout=timeout)
            request_id = submitted_video.request_id
    except ZhiqiError as exc:
        error = exc
        request_id = exc.request_id
    except Exception as exc:  # noqa: BLE001 - 探测不向上抛
        error = ZhiqiError(ErrorCategory.UNKNOWN, f"{type(exc).__name__}: {exc}")
    latency_ms = int((time.monotonic() - started) * 1000)
    return HealthResult(
        capability=capability,
        model=model,
        protocol=protocol,
        status=status_from(latency_ms, error, degraded_latency_ms),
        latency_ms=latency_ms,
        request_id=request_id,
        error_category=error.category if error else None,
        error_message=sanitize_error_message(error.message) if error else None,
        checked_at=checked_at,
    )
