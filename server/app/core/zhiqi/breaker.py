"""熔断器（docs/08-zhiqiapi-integration.md §5.11、§8.4；Redis 键见 docs/01-architecture.md §7）。

- 状态 Hash ``ai:breaker:{capability}:{model}``：``{state, failures, opened_at, half_open_calls, reason}``，
  TTL ``window_seconds + open_seconds``（每次写入续期）；``reason=model_unavailable`` 时 TTL = ``permanent_ttl_seconds``；
- 失败窗口 List ``ai:breaker:failures:{capability}:{model}``（时间戳），TTL ``window_seconds``；
- ``closed → open``：窗口内计入失败 ≥ ``failure_threshold``（reason=failures）或 ``force_open``；
  ``open → half_open``：``open_seconds`` 到期（reason=model_unavailable 不自动转换）；
  ``half_open → closed``：``record_success``；``half_open → open``：``record_failure``；``reset`` 删除键。
不持有配置，由 ``ai_gateway_service.get_breaker(config)`` 每次按当前 ``ai_routing_config.breaker`` 构造。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from redis import Redis

from app.core.zhiqi.types import ErrorCategory

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

REASON_FAILURES = "failures"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_PROBE_DOWN = "probe_down"
REASON_MANUAL = "manual"
REASONS = (REASON_FAILURES, REASON_MODEL_UNAVAILABLE, REASON_PROBE_DOWN, REASON_MANUAL)

# 计入熔断的分类（§8.4）；其它分类不计
COUNTED_CATEGORIES = frozenset(
    {
        ErrorCategory.UPSTREAM_UNAVAILABLE, ErrorCategory.TIMEOUT, ErrorCategory.RATE_LIMITED,
        ErrorCategory.MODEL_UNROUTED, ErrorCategory.ROUTE_MISSING, ErrorCategory.MEDIA_STORAGE,
    }
)

KEY_PREFIX = "ai:breaker:"
FAILURES_PREFIX = "ai:breaker:failures:"


class CircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        *,
        failure_threshold: int = 5,
        window_seconds: int = 300,
        open_seconds: int = 120,
        half_open_max_calls: int = 1,
        permanent_ttl_seconds: int = 3660,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.redis = redis
        self.failure_threshold = max(1, int(failure_threshold))
        self.window_seconds = max(1, int(window_seconds))
        self.open_seconds = max(1, int(open_seconds))
        self.half_open_max_calls = max(1, int(half_open_max_calls))
        self.permanent_ttl_seconds = max(1, int(permanent_ttl_seconds))
        self._clock = clock

    # ------------------------------------------------------------ 键

    @staticmethod
    def key(capability: str, model: str) -> str:
        return f"{KEY_PREFIX}{_value(capability)}:{model}"

    @staticmethod
    def failures_key(capability: str, model: str) -> str:
        return f"{FAILURES_PREFIX}{_value(capability)}:{model}"

    @property
    def state_ttl(self) -> int:
        return self.window_seconds + self.open_seconds

    # ------------------------------------------------------------ 内部

    def _load(self, capability: str, model: str) -> dict[str, str]:
        return self.redis.hgetall(self.key(capability, model)) or {}

    def _write(self, capability: str, model: str, mapping: dict[str, Any], *, ttl: int | None = None) -> None:
        key = self.key(capability, model)
        pipe = self.redis.pipeline()
        pipe.hset(key, mapping={k: "" if v is None else str(v) for k, v in mapping.items()})
        pipe.expire(key, ttl or self.state_ttl)
        pipe.execute()

    def _effective(self, capability: str, model: str, data: dict[str, str]) -> str:
        """读取状态；``open`` 到期（reason≠model_unavailable）时写入 ``half_open`` 并返回。"""
        state = data.get("state") or CLOSED
        if state != OPEN:
            return state if state in (CLOSED, HALF_OPEN) else CLOSED
        if data.get("reason") == REASON_MODEL_UNAVAILABLE:
            return OPEN
        opened_at = _float(data.get("opened_at"))
        if opened_at is not None and self._clock() - opened_at >= self.open_seconds:
            self._write(capability, model, {"state": HALF_OPEN, "half_open_calls": 0})
            data["state"] = HALF_OPEN
            data["half_open_calls"] = "0"
            return HALF_OPEN
        return OPEN

    def _open(self, capability: str, model: str, *, reason: str, failures: int | None = None) -> None:
        mapping: dict[str, Any] = {"state": OPEN, "opened_at": f"{self._clock():.3f}", "half_open_calls": 0, "reason": reason}
        if failures is not None:
            mapping["failures"] = failures
        ttl = self.permanent_ttl_seconds if reason == REASON_MODEL_UNAVAILABLE else self.state_ttl
        self._write(capability, model, mapping, ttl=ttl)

    # ------------------------------------------------------------ 公共

    def state(self, capability: str, model: str) -> str:
        """``closed`` / ``open`` / ``half_open``（open 到期自动转 half_open；reason=model_unavailable 的 open 不自动转换）。"""
        return self._effective(capability, model, self._load(capability, model))

    def reason(self, capability: str, model: str) -> str | None:
        """``failures`` / ``model_unavailable`` / ``probe_down`` / ``manual``；closed 时为 ``None``。"""
        data = self._load(capability, model)
        if self._effective(capability, model, data) == CLOSED:
            return None
        return data.get("reason") or None

    def allow(self, capability: str, model: str) -> bool:
        """closed → True；half_open → 占用一次试探名额（超过 ``half_open_max_calls`` 返回 False）；open → False。"""
        state = self.state(capability, model)
        if state == CLOSED:
            return True
        if state == OPEN:
            return False
        key = self.key(capability, model)
        used = int(self.redis.hincrby(key, "half_open_calls", 1))
        self.redis.expire(key, self.state_ttl)
        return used <= self.half_open_max_calls

    def record_success(self, capability: str, model: str) -> bool:
        """half_open → closed（删除状态并清空失败窗口）；返回是否发生 → closed 转换（调用方据此自动解决 ai_breaker_open）。"""
        if self.state(capability, model) != HALF_OPEN:
            return False
        self.redis.delete(self.key(capability, model), self.failures_key(capability, model))
        return True

    def record_failure(self, capability: str, model: str, category: ErrorCategory) -> bool:
        """仅 ``COUNTED_CATEGORIES`` 计入；窗口内 ≥ threshold → open（reason=failures）；half_open 失败 → 重新 open。
        返回是否发生 非 open → open 转换（调用方 raise_alert）。"""
        try:
            category = ErrorCategory(category)
        except ValueError:
            return False
        if category not in COUNTED_CATEGORIES:
            return False
        state = self.state(capability, model)
        if state == OPEN:
            return False
        if state == HALF_OPEN:
            self._open(capability, model, reason=REASON_FAILURES)
            self.redis.delete(self.failures_key(capability, model))
            return True
        now = self._clock()
        fkey = self.failures_key(capability, model)
        pipe = self.redis.pipeline()
        pipe.rpush(fkey, f"{now:.3f}")
        pipe.expire(fkey, self.window_seconds)
        pipe.lrange(fkey, 0, -1)
        stamps = pipe.execute()[2]
        cutoff = now - self.window_seconds
        old = 0
        for stamp in stamps:
            value = _float(stamp)
            if value is not None and value >= cutoff:
                break
            old += 1
        if old:
            self.redis.ltrim(fkey, old, -1)
        count = len(stamps) - old
        if count >= self.failure_threshold:
            self._open(capability, model, reason=REASON_FAILURES, failures=count)
            self.redis.delete(fkey)
            return True
        self._write(capability, model, {"state": CLOSED, "failures": count})
        return False

    def force_open(self, capability: str, model: str, *, reason: str) -> bool:
        """强制打开（reason ∈ failures / model_unavailable / probe_down / manual）；model_unavailable 写 permanent TTL 且不自动半开。
        返回是否发生 非 open → open 转换。"""
        if reason not in REASONS:
            raise ValueError(f"未知的熔断原因：{reason}")
        before = self.state(capability, model)
        self._open(capability, model, reason=reason)
        return before != OPEN

    def reset(self, capability: str, model: str) -> bool:
        """删除状态与失败窗口；返回是否原为 open / half_open（调用方自动解决告警）。"""
        data = self._load(capability, model)
        was = data.get("state") in (OPEN, HALF_OPEN)
        self.redis.delete(self.key(capability, model), self.failures_key(capability, model))
        return was

    def snapshot(self) -> list[dict[str, Any]]:
        """扫描 ``ai:breaker:*``（跳过失败窗口键）→ ``[{capability, model, state, reason, failures, opened_at, half_open_calls}]``。"""
        items: list[dict[str, Any]] = []
        for key in self.redis.scan_iter(match=f"{KEY_PREFIX}*", count=500):
            if key.startswith(FAILURES_PREFIX):
                continue
            rest = key[len(KEY_PREFIX):]
            if ":" not in rest:
                continue
            capability, model = rest.split(":", 1)
            data = self.redis.hgetall(key) or {}
            if not data:
                continue
            state = self._effective(capability, model, data)
            opened = _float(data.get("opened_at"))
            items.append(
                {
                    "capability": capability,
                    "model": model,
                    "state": state,
                    "reason": (data.get("reason") or None) if state != CLOSED else None,
                    "failures": int(_float(data.get("failures")) or 0),
                    "opened_at": datetime.fromtimestamp(opened, UTC).replace(tzinfo=None, microsecond=0).isoformat() if opened else None,
                    "half_open_calls": int(_float(data.get("half_open_calls")) or 0),
                }
            )
        items.sort(key=lambda item: (item["capability"], item["model"]))
        return items


def _value(capability: Any) -> str:
    return capability.value if hasattr(capability, "value") else str(capability)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
