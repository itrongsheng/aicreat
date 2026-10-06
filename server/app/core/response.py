"""统一响应外壳 ``{code, message, data}`` 与分页结构（docs/04-api-spec.md §2、§3）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class ApiResponse(BaseModel, Generic[T]):
    """响应外壳（供 OpenAPI 文档与类型标注使用）。"""

    code: int = 0
    message: str = "ok"
    data: T | None = None


class Page(BaseModel, Generic[T]):
    """分页数据：``{items, total, page, page_size}``。"""

    items: list[T]
    total: int
    page: int
    page_size: int


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """成功响应：``{"code": 0, "message": "ok", "data": data}``。"""
    return {"code": 0, "message": message, "data": data}


def fail(code: int | str, message: str | int, data: Any = None) -> dict[str, Any]:
    """失败响应外壳（一般无需直接使用：路由抛 ``BusinessError``，由异常处理器生成）。

    规范参数顺序为 ``fail(code, message, data)``；为兼容 docs/04 §2 的 ``fail(message, code, data)``
    写法，第一个参数为字符串时自动交换。
    """
    if isinstance(code, str) and not isinstance(message, str):
        code, message = message, code
    return {"code": int(code), "message": str(message), "data": data}


def paginated(items: Sequence[Any], total: int, page: int, page_size: int) -> dict[str, Any]:
    """分页响应：``data = {items, total, page, page_size}``。"""
    return ok({"items": list(items), "total": int(total), "page": int(page), "page_size": int(page_size)})
