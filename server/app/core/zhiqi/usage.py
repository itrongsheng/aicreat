"""用量估算与 ``/api/log/token`` 拉取（docs/08-zhiqiapi-integration.md §3.6、§5.9、§10）。

```text
quota_type = 0（按量）：quota = round((prompt_tokens + completion_tokens × completion_ratio) × model_ratio × group_ratio)
quota_type = 1（按次）：quota = round(model_price × quota_per_unit × group_ratio)        # × quota_per_unit 未核实
cost_cny = quota / quota_per_unit × usd_cny_rate
```
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.zhiqi.client import ZhiqiClient
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import ErrorCategory, RetryPolicy, UsageLogEntry

logger = logging.getLogger("app.core.zhiqi")

COST_QUANT = Decimal("0.000001")  # DECIMAL(14,6)


def _round_half_up(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def estimate_quota(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    model_ratio: float,
    completion_ratio: float,
    group_ratio: float,
    quota_type: int,
    model_price: float | None,
    quota_per_unit: int,
) -> int:
    """按量：``round((prompt + completion × completion_ratio) × model_ratio × group_ratio)``；
    按次：``round(model_price × quota_per_unit × group_ratio)``。结果不小于 0。"""
    group = float(group_ratio if group_ratio is not None else 1.0)
    if int(quota_type or 0) == 1:
        price = float(model_price or 0.0)
        return max(0, _round_half_up(price * float(quota_per_unit) * group))
    ratio = float(model_ratio if model_ratio is not None else 1.0)
    comp = float(completion_ratio if completion_ratio is not None else 1.0)
    value = (max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0)) * comp) * ratio * group
    return max(0, _round_half_up(value))


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF        # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF     # 扩展 A
        or 0x20000 <= code <= 0x2FA1F   # 扩展 B~ 与兼容补充
        or 0xF900 <= code <= 0xFAFF     # 兼容表意文字
        or 0x3000 <= code <= 0x303F     # CJK 标点
        or 0xFF00 <= code <= 0xFFEF     # 全角字符
    )


def estimate_tokens(text: str) -> int:
    """粗估：中文字符 ×1 + 其它字符按 4 字符 / 1 token（向上取整）。"""
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    others = len(text) - cjk
    return cjk + math.ceil(others / 4)


def quota_to_cny(quota: int, *, quota_per_unit: int, usd_cny_rate: float) -> Decimal:
    """``quota / quota_per_unit × usd_cny_rate``，四舍五入到 6 位小数（``DECIMAL(14,6)``）。"""
    if not quota_per_unit:
        return Decimal(0).quantize(COST_QUANT)
    value = Decimal(int(quota or 0)) / Decimal(int(quota_per_unit)) * Decimal(str(usd_cny_rate))
    return value.quantize(COST_QUANT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 用量日志


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_time(value: Any) -> datetime | None:
    """上游日志时间（字段名未核实）：Unix 秒 / 毫秒或 ISO 字符串 → naive UTC。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None, microsecond=0)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _parse_time(int(text))
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    return None


def parse_usage_entry(item: dict[str, Any]) -> UsageLogEntry:
    """单条 ``/api/log/token`` 条目 → ``UsageLogEntry``（``other`` 可能是 JSON 字符串；退款 ``quota`` 保留原值，入库时取绝对值）。"""
    other = item.get("other")
    if isinstance(other, str):
        try:
            other = json.loads(other) if other.strip() else {}
        except ValueError:
            other = {}
    if not isinstance(other, dict):
        other = {}
    task_id = _opt_str(item.get("task_id")) or _opt_str(other.get("task_id"))
    created = None
    for key in ("created_at", "created_time", "timestamp", "time"):
        if item.get(key) not in (None, ""):
            created = _parse_time(item.get(key))
            break
    return UsageLogEntry(
        request_id=_opt_str(item.get("request_id")) or "",
        log_type=_int(item.get("type")),
        model_name=_opt_str(item.get("model_name") or item.get("model")),
        group=_opt_str(item.get("group")),
        quota=_int(item.get("quota")),
        prompt_tokens=_int(item.get("prompt_tokens")),
        completion_tokens=_int(item.get("completion_tokens")),
        cache_tokens=_int(other.get("cache_tokens")),
        group_ratio=_opt_float(other.get("group_ratio")),
        model_ratio=_opt_float(other.get("model_ratio")),
        completion_ratio=_opt_float(other.get("completion_ratio")),
        request_path=_opt_str(other.get("request_path") or item.get("request_path")),
        task_id=task_id,
        created_at=created,
        raw=item,
        upstream_log_id=_int(item.get("id"), default=0) or None,
    )


def parse_token_logs(body: Any) -> list[UsageLogEntry]:
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = next((data[k] for k in ("items", "list", "data", "logs") if isinstance(data.get(k), list)), [])
        else:
            items = next((body[k] for k in ("items", "list", "logs") if isinstance(body.get(k), list)), [])
    else:
        items = []
    return [parse_usage_entry(item) for item in items if isinstance(item, dict)]


def fetch_token_logs(
    client: ZhiqiClient, *, timeout: float = 30, retry: RetryPolicy | None = None
) -> tuple[list[UsageLogEntry], str | None]:
    """``GET /api/log/token``（最近 1000 条，新在前）→ ``(entries, request_id)``；``retry`` 为幂等 GET 的客户端重试策略
    （§4.2 ``retry``：轮询 / 目录 / 用量按 ``retry_on`` 全量生效，调用方注入；``None`` 不重试）。"""
    response = client.get("/api/log/token", timeout=timeout, retry=retry)
    if response.json is None:
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "用量日志响应为空", http_status=response.http_status, request_id=response.request_id)
    entries = parse_token_logs(response.json)
    logger.info("zhiqi 用量日志 pulled=%s request_id=%s", len(entries), response.request_id or "-")
    return entries, response.request_id
