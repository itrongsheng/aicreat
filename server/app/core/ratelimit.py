"""Redis 频控：滑动窗口、登录失败锁定、同域名最小间隔。

- ``check_rate_limit(key, limit, window_seconds=None)``：有序集合滑动窗口（键 ``rate:*``），超限抛
  ``BusinessError("请求过于频繁", code=CODE_RATE_LIMITED, http_status=429, data={"retry_after": 秒})``；
- 登录失败计数 ``rate:admin_login:{username}``（docs/07-admin-rbac.md §7.5）；
- ``enforce_interval(key, interval_seconds)``：``domain:last_fetch:{domain}`` 同域名最小间隔。

并发限流不在此处：worker 的能力 / 检测并发为进程内 ``threading.BoundedSemaphore``。
频控是保护性措施：Redis 不可用时放行并记 warning（登录锁定同理），不让 Redis 故障阻断业务。
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid

import redis

from app.core.config import settings
from app.core.exceptions import CODE_RATE_LIMITED, BusinessError
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

ADMIN_LOGIN_WINDOW_SECONDS = 900
ADMIN_LOGIN_LOCKED_MESSAGE = "登录失败次数过多，请稍后再试"
RATE_LIMITED_MESSAGE = "请求过于频繁"

_UNITS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_RATE_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)?\s*([a-zA-Z]+)\s*$")


def parse_rate(rate: str) -> tuple[int, int]:
    """``"60/hour"`` → ``(60, 3600)``；支持 ``second/minute/hour/day``（及 ``s/m/h/d``、复数）与 ``"10/5minutes"``。"""
    match = _RATE_RE.match(rate or "")
    if not match:
        raise ValueError(f"频控格式错误: {rate!r}（应为 次数/单位，如 60/hour）")
    count = int(match.group(1))
    multiplier = int(match.group(2) or 1)
    unit = match.group(3).lower()
    if unit not in _UNITS or multiplier <= 0:
        raise ValueError(f"频控单位错误: {rate!r}")
    return count, _UNITS[unit] * multiplier


# KEYS[1]=键；ARGV: now_ms, window_ms, limit, member, ttl_seconds
_SLIDING_WINDOW = redis_client.register_script(
    """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry = window
    if oldest[2] then retry = tonumber(oldest[2]) + window - now end
    return {0, retry, count}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('EXPIRE', key, tonumber(ARGV[5]))
return {1, 0, count + 1}
"""
)


def check_rate_limit(key: str, limit: int | str, window_seconds: int | None = None) -> int:
    """滑动窗口计数一次；超限抛 429 ``BusinessError``（本次不计数）。

    ``limit`` 可为整数（需同时给 ``window_seconds``）或频控串（如 ``"60/hour"``）；``limit <= 0`` 表示不限。
    返回窗口内（含本次）的请求数；Redis 不可用时放行并返回 0。
    """
    if isinstance(limit, str):
        limit, window_seconds = parse_rate(limit)
    if window_seconds is None or window_seconds <= 0:
        raise ValueError("window_seconds 必须为正整数")
    if limit <= 0:
        return 0
    now_ms = int(time.time() * 1000)
    window_ms = int(window_seconds * 1000)
    member = f"{now_ms}-{uuid.uuid4().hex[:8]}"
    try:
        allowed, retry_ms, count = _SLIDING_WINDOW(
            keys=[key], args=[now_ms, window_ms, int(limit), member, int(math.ceil(window_seconds))]
        )
    except redis.RedisError as exc:
        logger.warning("频控检查失败（Redis 不可用，放行）key=%s: %s", key, exc)
        return 0
    if not int(allowed):
        retry_after = max(1, int(math.ceil(int(retry_ms) / 1000)))
        raise BusinessError(RATE_LIMITED_MESSAGE, code=CODE_RATE_LIMITED, http_status=429, data={"retry_after": retry_after})
    return int(count)


# ---------------------------------------------------------------- 管理员登录失败锁定（07 §7.5）


def admin_login_key(username: str) -> str:
    """``rate:admin_login:{username}``：用户名先 ``strip()`` 再转小写，避免大小写绕过。"""
    return f"rate:admin_login:{(username or '').strip().lower()}"


def admin_login_retry_after(username: str) -> int:
    """已锁定时返回剩余秒数（键 TTL），未锁定返回 0。锁定判定只读，不 ``INCR``、不续期。"""
    key = admin_login_key(username)
    try:
        count = redis_client.get(key)
        if count is None or int(count) < settings.admin_login_max_failures:
            return 0
        ttl = int(redis_client.ttl(key))
    except (redis.RedisError, ValueError) as exc:
        logger.warning("读取登录失败计数失败 key=%s: %s", key, exc)
        return 0
    return ttl if ttl > 0 else 0


def check_admin_login_allowed(username: str) -> None:
    """计数 ≥ ``ADMIN_LOGIN_MAX_FAILURES`` 时抛 429「登录失败次数过多，请稍后再试」，``data={"retry_after": 秒}``。"""
    retry_after = admin_login_retry_after(username)
    if retry_after > 0:
        raise BusinessError(ADMIN_LOGIN_LOCKED_MESSAGE, code=CODE_RATE_LIMITED, http_status=429, data={"retry_after": retry_after})


def record_admin_login_failure(username: str) -> int:
    """登录失败：``INCR`` 后立即 ``EXPIRE 900``（锁定时长 = 最后一次失败起 15 分钟）。返回当前计数。"""
    key = admin_login_key(username)
    try:
        pipe = redis_client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, ADMIN_LOGIN_WINDOW_SECONDS)
        count, _ = pipe.execute()
        return int(count)
    except redis.RedisError as exc:
        logger.warning("记录登录失败计数失败 key=%s: %s", key, exc)
        return 0


def clear_admin_login_failures(username: str) -> None:
    """登录成功：``DEL`` 计数。"""
    key = admin_login_key(username)
    try:
        redis_client.delete(key)
    except redis.RedisError as exc:
        logger.warning("清除登录失败计数失败 key=%s: %s", key, exc)


# ---------------------------------------------------------------- 同域名最小间隔


def enforce_interval(key: str, interval_seconds: float, *, max_wait_seconds: float = 60.0) -> float:
    """保证同一 ``key``（如 ``domain:last_fetch:{domain}``）两次放行之间至少间隔 ``interval_seconds``。

    以 ``SET key <时间戳> NX PX interval`` 原子占位：占位成功即放行；否则按键剩余 TTL 休眠后重试。
    最多等待 ``max_wait_seconds`` 后直接放行。返回实际等待秒数；Redis 不可用时立即放行。
    """
    if interval_seconds <= 0:
        return 0.0
    interval_ms = max(1, int(interval_seconds * 1000))
    started = time.monotonic()
    while True:
        try:
            if redis_client.set(key, f"{time.time():.3f}", nx=True, px=interval_ms):
                return time.monotonic() - started
            pttl = int(redis_client.pttl(key))
            if pttl == -1:  # 无 TTL 的残留键：补上过期时间
                redis_client.pexpire(key, interval_ms)
                pttl = interval_ms
        except redis.RedisError as exc:
            logger.warning("同域名间隔控制失败（Redis 不可用，放行）key=%s: %s", key, exc)
            return time.monotonic() - started
        waited = time.monotonic() - started
        remaining = max_wait_seconds - waited
        if remaining <= 0:
            logger.info("同域名间隔等待超过 %.1fs，直接放行 key=%s", max_wait_seconds, key)
            return waited
        sleep_for = max(pttl, 10) / 1000 if pttl > 0 else 0.01
        time.sleep(min(sleep_for, remaining))
