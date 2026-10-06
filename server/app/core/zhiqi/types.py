"""zhiqiapi 适配层的枚举与数据结构（docs/08-zhiqiapi-integration.md §5.2）。

本模块不依赖适配层内其它模块，``client.py`` / ``safe_fetch.py`` / 网关服务都从这里导入；
``DownloadResult`` 定义在本文件而非 ``client.py``，避免 ``client`` ↔ ``safe_fetch`` 循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class Capability(str, Enum):
    KEYWORD = "keyword"; TITLE = "title"; CONTENT = "content"; REWRITE = "rewrite"
    IMAGE = "image"; VIDEO = "video"; GEO_CHECK = "geo_check"; SEO_CHECK = "seo_check"


TEXT_CAPABILITIES = {
    Capability.KEYWORD, Capability.TITLE, Capability.CONTENT, Capability.REWRITE, Capability.GEO_CHECK, Capability.SEO_CHECK,
}
# 能力 → 模态（ai_models.modalities_json）：TEXT_CAPABILITIES → "text"；IMAGE → "image"；VIDEO → "video"
MODALITY_OF: dict[Capability, str] = {**{c: "text" for c in TEXT_CAPABILITIES}, Capability.IMAGE: "image", Capability.VIDEO: "video"}


class Protocol(str, Enum):
    OPENAI_CHAT = "openai_chat"; OPENAI_RESPONSES = "openai_responses"; ANTHROPIC_MESSAGES = "anthropic_messages"
    IMAGE_ASYNC = "image_async"; IMAGE_SYNC = "image_sync"; IMAGE_EDIT = "image_edit"; VIDEO = "video"


TEXT_PROTOCOLS = frozenset({Protocol.OPENAI_CHAT, Protocol.OPENAI_RESPONSES, Protocol.ANTHROPIC_MESSAGES})
IMAGE_PROTOCOLS = frozenset({Protocol.IMAGE_ASYNC, Protocol.IMAGE_SYNC, Protocol.IMAGE_EDIT})


class ErrorCategory(str, Enum):
    UNSUPPORTED_PARAMETER = "unsupported_parameter"; ROUTE_MISSING = "route_missing"; MODEL_UNROUTED = "model_unrouted"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"; RATE_LIMITED = "rate_limited"; TIMEOUT = "timeout"
    QUOTA_EXCEEDED = "quota_exceeded"; AUTH_FAILED = "auth_failed"; CONTENT_BLOCKED = "content_blocked"
    MEDIA_STORAGE = "media_storage"; TRANSFER_FAILED = "transfer_failed"
    INVALID_RESPONSE = "invalid_response"; BREAKER_OPEN = "breaker_open"; CANCELLED = "cancelled"; UNKNOWN = "unknown"


class TaskStatus(str, Enum):  # 上游异步任务状态（图片/视频）
    QUEUED = "queued"; IN_PROGRESS = "in_progress"; SUCCEEDED = "succeeded"; FAILED = "failed"; EXPIRED = "expired"


@dataclass(frozen=True)
class Timeouts:
    connect: float = 10.0; read: float = 180.0; write: float = 30.0; pool: float = 10.0


@dataclass(frozen=True)
class RetryPolicy:  # 客户端 HTTP 层幂等重试策略，由 ai_gateway_service 按 ai_routing_config.retry 构造并按调用注入
    max_attempts: int = 3; base_seconds: float = 1.0; max_seconds: float = 30.0; jitter: bool = True
    retry_on: frozenset[ErrorCategory] = frozenset(
        {ErrorCategory.UPSTREAM_UNAVAILABLE, ErrorCategory.RATE_LIMITED, ErrorCategory.TIMEOUT, ErrorCategory.INVALID_RESPONSE}
    )

    @classmethod
    def from_config(cls, retry_config: dict[str, Any] | None) -> RetryPolicy:
        """按 ``ai_routing_config.retry`` 构造（缺省键取类默认值；未知分类忽略）。"""
        cfg = retry_config or {}
        default = cls()
        retry_on = cfg.get("retry_on")
        if retry_on is None:
            categories = default.retry_on
        else:
            categories = frozenset(ErrorCategory(c) for c in retry_on if c in ErrorCategory._value2member_map_)
        return cls(
            max_attempts=max(1, int(cfg.get("max_attempts", default.max_attempts))),
            base_seconds=max(0.0, float(cfg.get("base_seconds", default.base_seconds))),
            max_seconds=max(0.0, float(cfg.get("max_seconds", default.max_seconds))),
            jitter=bool(cfg.get("jitter", default.jitter)),
            retry_on=categories,
        )


@dataclass
class Citation:
    url: str; title: str | None = None; snippet: str | None = None; source: str = "annotation"  # annotation / markdown_link / plain_url / mock


@dataclass
class TextRequest:
    model: str; protocol: Protocol
    messages: list[dict[str, str]]                       # [{"role":"system"|"user"|"assistant","content":str}]
    max_tokens: int = 2048; temperature: float = 0.7; top_p: float | None = None
    response_format: str = "text"                        # text / json（json 时 chat 走 response_format={"type":"json_object"}，responses 走 text.format，anthropic 靠提示词约束）
    stream: bool = False; stop: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # 透传上游字段（白名单见 ai_routing_config.passthrough，build_payload 按协议过滤）
    metadata: dict[str, Any] = field(default_factory=dict)  # 本地用：capability、task_id、target_url；不发送，只经 client.request(metadata=…) 交给 MockZhiqiClient


@dataclass
class TextResult:
    text: str; model: str; request_id: str | None
    prompt_tokens: int = 0; completion_tokens: int = 0; cache_tokens: int = 0
    usage_missing: bool = False                          # 响应缺少 usage 字段时 True（网关写 response_meta_json.usage_missing）
    finish_reason: str | None = None; citations: list[Citation] = field(default_factory=list)
    latency_ms: int = 0; http_status: int = 200; raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageRequest:
    model: str; prompt: str
    resolution: str = "1080p"; aspect_ratio: str = "16:9"; n: int = 1; response_format: str = "url"
    reference_image_urls: list[str] = field(default_factory=list)   # ≤ 9 个公网 URL


@dataclass
class ImageSubmitResult:
    mode: str                              # async / sync / edit
    task_id: str | None; status: TaskStatus | None
    urls: list[str]                        # sync/edit 直接返回
    request_id: str | None; latency_ms: int; http_status: int; raw: dict[str, Any]


@dataclass
class ImageTaskStatus:
    task_id: str; status: TaskStatus; progress: int
    urls: list[str]; error_code: str | None; error_message: str | None  # 字段名以官方文档为准
    request_id: str | None; raw: dict[str, Any]


@dataclass
class VideoRequest:
    model: str; prompt: str
    duration: int | None = None; resolution: str | None = None; aspect_ratio: str | None = None; size: str | None = None
    input_reference: str | None = None
    reference_image_urls: list[str] = field(default_factory=list)
    reference_video_urls: list[str] = field(default_factory=list)
    reference_audio_urls: list[str] = field(default_factory=list)
    first_frame_image_url: str | None = None; last_frame_image_url: str | None = None
    negative_prompt: str | None = None; generate_audio: bool | None = None; n: int = 1


@dataclass
class VideoSubmitResult:
    task_id: str; status: TaskStatus; request_id: str | None; latency_ms: int; http_status: int; raw: dict[str, Any]


@dataclass
class VideoTaskStatus:
    task_id: str; status: TaskStatus; progress: int
    urls: list[str]; error_code: str | None; error_message: str | None
    expires_at: datetime | None; request_id: str | None; raw: dict[str, Any]  # expires_at 字段名以官方文档为准


@dataclass
class DownloadResult:              # 媒体下载结果：ZhiqiClient.stream_download / videos.download_content / safe_fetch.stream_public_bytes 共用
    source: str                    # origin / content / cdn / mock
    request_id: str | None         # 本次下载响应头 x-oneapi-request-id；cdn 恒为 None；Mock 为 "mock-" + uuid4 hex
    http_status: int               # 下载响应状态码（cdn 为最终一跳的状态码）；失败不返回本结构，改抛 ZhiqiError(TRANSFER_FAILED)
    request_ids: list[str] = field(default_factory=list)   # 本次调用收到的全部非空 request_id（cdn 为 []）


@dataclass
class ModelInfo:
    id: str; owned_by: str | None; supported_endpoint_types: list[str]


@dataclass
class PricingEntry:
    model_name: str; description: str | None; cover_url: str | None; tags: list[str]; vendor_id: int | None
    sort_order: int; quota_type: int; model_ratio: float | None; model_price: float | None
    completion_ratio: float | None; cache_ratio: float | None; create_cache_ratio: float | None
    enable_groups: list[str]; supported_endpoint_types: list[str]; billing_mode: str | None
    billing_expr: str | None; icon: str | None; model_price_type: str | None; raw: dict[str, Any]


@dataclass
class PricingCatalog:
    models: list[PricingEntry]; vendors: list[dict[str, Any]]; model_parameter_capabilities: dict[str, Any]


@dataclass
class UsageLogEntry:
    request_id: str; log_type: int; model_name: str | None; group: str | None; quota: int
    prompt_tokens: int; completion_tokens: int; cache_tokens: int
    group_ratio: float | None; model_ratio: float | None; completion_ratio: float | None
    request_path: str | None; task_id: str | None; created_at: datetime | None; raw: dict[str, Any]
    upstream_log_id: int | None = None                   # 条目 id（字段名未核实，以官方文档为准），写 ai_usage_logs.upstream_log_id


@dataclass
class HealthResult:
    capability: Capability; model: str; protocol: Protocol; status: str  # healthy / degraded / down / unknown
    latency_ms: int; request_id: str | None; error_category: ErrorCategory | None; error_message: str | None
    checked_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """``POST /admin/ai/health/probe`` 的返回结构（04 §7.16）。"""
        return {
            "capability": self.capability.value,
            "model": self.model,
            "protocol": self.protocol.value,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "request_id": self.request_id,
            "error_category": self.error_category.value if self.error_category else None,
            "error_message": self.error_message,
            "checked_at": self.checked_at,
        }


def utcnow() -> datetime:
    """当前 UTC 时间（naive，秒精度），与库内 DATETIME 存 UTC 的约定一致（core 不导入 models）。"""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)
