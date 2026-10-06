"""Redis 分布式锁（``SET key token NX EX ttl``）。

- ``acquire_lock(key, ttl)`` 成功返回随机 token，失败返回 ``None``；
- ``release_lock(key, token)`` 只删除自己持有的锁（Lua 比对 token）；
- ``extend_lock(key, ttl, token=None)`` 续期（带 token 时只续自己的锁）；
- ``with_lock(key, ttl, wait_seconds=0)`` 上下文：``wait_seconds > 0`` 时轮询等待，获取失败抛 ``LockTimeout``。

锁键与 TTL 见 docs/01-architecture.md §6.2（如 ``lock:bootstrap`` 60s）。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from app.core.redis import redis_client


class LockTimeout(RuntimeError):
    """在 ``wait_seconds`` 内未能获取锁。"""

    def __init__(self, key: str, wait_seconds: float = 0) -> None:
        super().__init__(f"获取锁失败: {key}（等待 {wait_seconds:g}s）")
        self.key = key
        self.wait_seconds = wait_seconds


_RELEASE_SCRIPT = redis_client.register_script(
    """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
)

_EXTEND_SCRIPT = redis_client.register_script(
    """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
)


def _ttl_seconds(ttl: int | float) -> int:
    return max(1, int(round(ttl)))


def acquire_lock(key: str, ttl: int | float, token: str | None = None) -> str | None:
    """尝试获取锁一次；成功返回 token（释放 / 续期时使用），已被占用返回 ``None``。"""
    token = token or uuid.uuid4().hex
    if redis_client.set(key, token, nx=True, ex=_ttl_seconds(ttl)):
        return token
    return None


def release_lock(key: str, token: str | None) -> bool:
    """释放锁：仅当锁仍由 ``token`` 持有时删除（过期后被他人取得的锁不受影响）。"""
    if not token:
        return False
    return bool(_RELEASE_SCRIPT(keys=[key], args=[token]))


def extend_lock(key: str, ttl: int | float, token: str | None = None) -> bool:
    """续期锁（``EXPIRE``）；给出 ``token`` 时只续期自己持有的锁。返回是否续期成功。"""
    if token:
        return bool(_EXTEND_SCRIPT(keys=[key], args=[token, _ttl_seconds(ttl)]))
    return bool(redis_client.expire(key, _ttl_seconds(ttl)))


def is_locked(key: str) -> bool:
    return bool(redis_client.exists(key))


def wait_for_lock(key: str, ttl: int | float, wait_seconds: float = 0, poll_interval: float = 0.2) -> str:
    """获取锁，最多等待 ``wait_seconds`` 秒；失败抛 ``LockTimeout``。"""
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        token = acquire_lock(key, ttl)
        if token:
            return token
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockTimeout(key, wait_seconds)
        time.sleep(min(poll_interval, remaining))


@contextmanager
def with_lock(key: str, ttl: int | float, wait_seconds: float = 0, poll_interval: float = 0.2) -> Iterator[str]:
    """持锁执行代码块，``finally`` 释放；``yield`` 出 token 以便块内 ``extend_lock`` 续期。"""
    token = wait_for_lock(key, ttl, wait_seconds=wait_seconds, poll_interval=poll_interval)
    try:
        yield token
    finally:
        try:
            release_lock(key, token)
        except Exception:  # noqa: BLE001 - 释放失败只会让锁自然过期
            pass
