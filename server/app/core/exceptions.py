"""业务异常、业务码常量与全局异常处理（docs/04-api-spec.md §2、§5）。

所有错误都转换为 ``{code, message, data}`` 外壳：
- ``BusinessError`` → ``code`` / ``http_status`` / ``data`` 原样；
- 请求校验错误（Pydantic）→ 400「参数错误」，``data`` 为 ``[{loc, msg, type, input}]``；
- ``HTTPException``（路由不存在 404、方法不允许 405 等）→ 同名 code；
- 未捕获异常 → 500，``DEV_MODE`` 时 ``data.traceback`` 带堆栈，否则 ``null``。
"""

from __future__ import annotations

import logging
import traceback
from http import HTTPStatus
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 业务码（04 §5.1）

CODE_OK = 0
CODE_BAD_REQUEST = 400
CODE_UNAUTHORIZED = 401
CODE_FORBIDDEN = 403
CODE_NOT_FOUND = 404
CODE_CONFLICT = 409
CODE_RATE_LIMITED = 429
CODE_TEMPLATE_VARIABLE_MISSING = 4221
CODE_PUBLIC_URL_REQUIRED = 4222
CODE_QUOTA_LIMIT_REACHED = 4291
CODE_UPSTREAM_ERROR = 5021
CODE_CAPABILITY_UNAVAILABLE = 5031
CODE_INTERNAL_ERROR = 500

# 业务码 → HTTP 状态码
CODE_HTTP_STATUS: dict[int, int] = {
    CODE_BAD_REQUEST: 400,
    CODE_UNAUTHORIZED: 401,
    CODE_FORBIDDEN: 403,
    CODE_NOT_FOUND: 404,
    CODE_CONFLICT: 409,
    CODE_RATE_LIMITED: 429,
    CODE_TEMPLATE_VARIABLE_MISSING: 422,
    CODE_PUBLIC_URL_REQUIRED: 422,
    CODE_QUOTA_LIMIT_REACHED: 429,
    CODE_UPSTREAM_ERROR: 502,
    CODE_CAPABILITY_UNAVAILABLE: 503,
    CODE_INTERNAL_ERROR: 500,
}

DEFAULT_MESSAGES: dict[int, str] = {
    400: "参数错误",
    401: "未登录或令牌已失效",
    403: "无权执行此操作",
    404: "资源不存在",
    405: "请求方法不允许",
    406: "不支持的响应格式",
    409: "数据冲突",
    413: "请求体过大",
    415: "不支持的请求类型",
    422: "参数错误",
    429: "请求过于频繁",
    500: "服务器内部错误",
    502: "上游服务调用失败",
    503: "服务暂不可用",
}

PASSWORD_FIELDS = frozenset({"password", "old_password", "new_password"})
MAX_INPUT_CHARS = 200
MAX_INPUT_ITEMS = 50


class BusinessError(Exception):
    """业务异常：``BusinessError(message, code=400, http_status=400, data=None)``。

    ``http_status`` 缺省时按业务码推导（如 ``code=404`` → HTTP 404、``4221`` → 422），未知业务码为 400。
    """

    def __init__(self, message: str, code: int = CODE_BAD_REQUEST, http_status: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = int(code)
        self.http_status = int(http_status) if http_status is not None else CODE_HTTP_STATUS.get(self.code, 400)
        self.data = data

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"BusinessError(code={self.code}, http_status={self.http_status}, message={self.message!r})"


# ---------------------------------------------------------------- 校验错误项


def _safe_input(value: Any, depth: int = 0) -> Any:
    """把被拒绝的输入转为可 JSON 化的值：字符串超过 200 字符截断，容器递归（限制规模）。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_INPUT_CHARS]
    if isinstance(value, (bytes, bytearray)):
        return None
    if depth >= 3:
        return None
    if isinstance(value, Mapping):
        items = list(value.items())[:MAX_INPUT_ITEMS]
        return {str(k)[:MAX_INPUT_CHARS]: _safe_input(v, depth + 1) for k, v in items}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_input(v, depth + 1) for v in list(value)[:MAX_INPUT_ITEMS]]
    try:
        return str(jsonable_encoder(value))[:MAX_INPUT_CHARS]
    except Exception:  # noqa: BLE001
        return str(value)[:MAX_INPUT_CHARS]


def field_error(loc: Sequence[str | int], msg: str, type: str = "value_error", input: Any = None) -> dict[str, Any]:  # noqa: A002
    """组装一项校验错误 ``{loc, msg, type, input}``（业务校验与 Pydantic 校验同结构）。"""
    loc_list = list(loc)
    item: dict[str, Any] = {"loc": loc_list, "msg": msg, "type": type}
    if not any(isinstance(part, str) and part in PASSWORD_FIELDS for part in loc_list):
        item["input"] = _safe_input(input)
    return item


def invalid_params(errors: Iterable[Mapping[str, Any]] | Mapping[str, Any], message: str = "参数错误") -> BusinessError:
    """构造 400 业务异常：``data`` 为校验错误列表。用法 ``raise invalid_params(field_error(...))``。"""
    if isinstance(errors, Mapping):
        errors = [errors]
    return BusinessError(message, code=CODE_BAD_REQUEST, http_status=400, data=[dict(e) for e in errors])


# Pydantic 内置错误类型 → 中文说明（ctx 中的占位符按需填充）
_MESSAGES_ZH: dict[str, str] = {
    "missing": "必填字段",
    "extra_forbidden": "不允许的字段",
    "string_type": "必须是字符串",
    "string_too_short": "长度不能少于 {min_length} 个字符",
    "string_too_long": "长度不能超过 {max_length} 个字符",
    "string_pattern_mismatch": "格式不正确",
    "too_short": "至少需要 {min_length} 项",
    "too_long": "最多允许 {max_length} 项",
    "greater_than": "必须大于 {gt}",
    "greater_than_equal": "不能小于 {ge}",
    "less_than": "必须小于 {lt}",
    "less_than_equal": "不能大于 {le}",
    "multiple_of": "必须是 {multiple_of} 的倍数",
    "int_type": "必须是整数",
    "int_parsing": "必须是整数",
    "int_from_float": "必须是整数",
    "float_type": "必须是数字",
    "float_parsing": "必须是数字",
    "decimal_type": "必须是数字",
    "decimal_parsing": "必须是数字",
    "decimal_max_digits": "总位数不能超过 {max_digits} 位",
    "decimal_max_places": "小数位数不能超过 {decimal_places} 位",
    "decimal_whole_digits": "整数部分不能超过 {whole_digits} 位",
    "finite_number": "必须是有限数值",
    "bool_type": "必须是布尔值",
    "bool_parsing": "必须是布尔值",
    "enum": "取值必须是 {expected} 之一",
    "literal_error": "取值必须是 {expected} 之一",
    "dict_type": "必须是对象",
    "model_type": "必须是对象",
    "model_attributes_type": "必须是对象",
    "list_type": "必须是数组",
    "tuple_type": "必须是数组",
    "set_type": "必须是数组",
    "json_invalid": "JSON 格式错误",
    "json_type": "JSON 格式错误",
    "datetime_type": "日期时间格式错误",
    "datetime_parsing": "日期时间格式错误",
    "datetime_from_date_parsing": "日期时间格式错误",
    "timezone_aware": "日期时间必须带时区",
    "timezone_naive": "日期时间不能带时区",
    "date_type": "日期格式错误",
    "date_parsing": "日期格式错误",
    "date_from_datetime_parsing": "日期格式错误",
    "date_from_datetime_inexact": "日期格式错误",
    "url_type": "URL 格式错误",
    "url_parsing": "URL 格式错误",
    "url_scheme": "URL 协议不支持",
    "url_too_long": "URL 过长",
    "uuid_type": "UUID 格式错误",
    "uuid_parsing": "UUID 格式错误",
    "bytes_type": "必须是文件",
    "none_required": "必须为空",
}


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _translate(err: Mapping[str, Any]) -> str:
    err_type = str(err.get("type", ""))
    msg = str(err.get("msg", ""))
    if err_type == "value_error":
        return msg.removeprefix("Value error, ")
    if err_type == "assertion_error":
        return msg.removeprefix("Assertion failed, ")
    template = _MESSAGES_ZH.get(err_type)
    if not template:
        return msg
    ctx = {k: v for k, v in (err.get("ctx") or {}).items()}
    return template.format_map(_SafeFormat({k: str(v) for k, v in ctx.items()}))


def format_validation_errors(errors: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pydantic ``errors()`` → 只保留 ``loc/msg/type/input`` 四个键（密码类字段不带 ``input``）。"""
    result: list[dict[str, Any]] = []
    for err in errors:
        loc = [part if isinstance(part, int) else str(part) for part in err.get("loc", ())]
        err_type = str(err.get("type", "value_error"))
        raw_input = err.get("input")
        # missing 与模型级校验（Pydantic 给出整个父对象）一律为 null
        if err_type == "missing" or isinstance(raw_input, Mapping) or len(loc) <= 1:
            raw_input = None
        result.append(field_error(loc, _translate(err), err_type, raw_input))
    return result


# ---------------------------------------------------------------- 处理器


def _envelope(code: int, message: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def _json(status_code: int, code: int, message: str, data: Any = None, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(_envelope(code, message, data)), headers=dict(headers) if headers else None)


async def business_error_handler(_request: Request, exc: BusinessError) -> JSONResponse:
    return _json(exc.http_status, exc.code, exc.message, exc.data)


async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return _json(400, CODE_BAD_REQUEST, DEFAULT_MESSAGES[400], format_validation_errors(exc.errors()))


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = int(exc.status_code)
    detail = exc.detail
    data: Any = None
    message = DEFAULT_MESSAGES.get(status, "请求失败")
    if isinstance(detail, str):
        # Starlette 默认 detail 为英文状态短语（如 "Not Found"），只采用自定义文案
        try:
            default_phrase = HTTPStatus(status).phrase
        except ValueError:
            default_phrase = ""
        if detail and detail != default_phrase:
            message = detail
    elif detail is not None:
        data = detail
    return _json(status, status, message, data, headers=getattr(exc, "headers", None))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未捕获异常 %s %s", request.method, request.url.path, exc_info=exc)
    data = {"traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))} if settings.dev_mode else None
    return _json(500, CODE_INTERNAL_ERROR, DEFAULT_MESSAGES[500], data)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全部异常处理器（``main.create_app`` 调用）。"""
    app.add_exception_handler(BusinessError, business_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
