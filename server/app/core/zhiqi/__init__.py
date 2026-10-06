"""zhiqiapi 适配层：只做传输与契约（请求体映射、响应解析、错误分类、按注入 ``RetryPolicy`` 的幂等重试、熔断器状态），
不读数据库、不读 ``settings`` 表、不写 ``ai_tasks``（docs/08-zhiqiapi-integration.md §2.5）。

``get_client()`` 为进程内单例：``ZHIQI_API_KEY`` 为空返回 ``MockZhiqiClient``，否则 ``ZhiqiClient``；``reset_client()`` 供测试使用。
"""

from app.core.zhiqi.types import (  # noqa: I001 - types / errors 须先于 client 导入（client → safe_fetch → errors）
    MODALITY_OF,
    TEXT_CAPABILITIES,
    Capability,
    ErrorCategory,
    Protocol,
    RetryPolicy,
    TaskStatus,
    Timeouts,
)
from app.core.zhiqi.errors import ZhiqiBreakerOpen, ZhiqiError
from app.core.zhiqi.client import ZhiqiClient, ZhiqiResponse, get_client, reset_client

__all__ = [
    "MODALITY_OF",
    "TEXT_CAPABILITIES",
    "Capability",
    "ErrorCategory",
    "Protocol",
    "RetryPolicy",
    "TaskStatus",
    "Timeouts",
    "ZhiqiBreakerOpen",
    "ZhiqiClient",
    "ZhiqiError",
    "ZhiqiResponse",
    "get_client",
    "reset_client",
]
