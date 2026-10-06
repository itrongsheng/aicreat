"""Redis 客户端与 JSON 缓存助手。

所有键经 ``redis_client``（``decode_responses=True``），无前缀命名空间；键表见 docs/01-architecture.md §6、§7。
缓存助手（``cache_*``）只用于可重建的缓存：Redis 故障时读返回 ``None``、写 / 删返回失败值并记 warning，
调用方回落到数据库，不让缓存故障变成接口 500。队列、锁、计数等需要强一致的操作直接使用 ``redis_client``。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import redis

from app.core.config import settings

logger = logging.getLogger(__name__)


def make_redis_client(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(
        url or settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        health_check_interval=30,
        retry_on_timeout=True,
    )


redis_client: redis.Redis = make_redis_client()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def cache_get_json(key: str) -> Any | None:
    """读取 JSON 缓存；键不存在、内容损坏或 Redis 不可用时返回 ``None``。"""
    try:
        raw = redis_client.get(key)
    except redis.RedisError as exc:
        logger.warning("Redis 读取缓存失败 key=%s: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("缓存内容不是合法 JSON，已删除 key=%s", key)
        cache_delete(key)
        return None


def cache_set_json(key: str, value: Any, ttl: int | float | None = None) -> bool:
    """写入 JSON 缓存（``ttl`` 秒，``None`` / ≤0 表示不过期）；失败返回 ``False``。"""
    try:
        payload = dumps(value)
    except (TypeError, ValueError) as exc:
        logger.warning("缓存值无法序列化 key=%s: %s", key, exc)
        return False
    try:
        if ttl is not None and ttl > 0:
            redis_client.set(key, payload, px=max(1, int(ttl * 1000)))
        else:
            redis_client.set(key, payload)
        return True
    except redis.RedisError as exc:
        logger.warning("Redis 写入缓存失败 key=%s: %s", key, exc)
        return False


def cache_delete(*keys: str) -> int:
    """删除一个或多个键，返回实际删除数；Redis 不可用时返回 0。"""
    keys = tuple(k for k in keys if k)
    if not keys:
        return 0
    try:
        return int(redis_client.delete(*keys))
    except redis.RedisError as exc:
        logger.warning("Redis 删除缓存失败 keys=%s: %s", keys, exc)
        return 0


def _escape_glob(text: str) -> str:
    out = []
    for ch in text:
        if ch in "*?[]\\":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def cache_delete_prefix(prefix: str, batch_size: int = 500) -> int:
    """按前缀删除（``SCAN`` 分批，不使用 ``KEYS``），返回删除数；Redis 不可用时返回 0。"""
    if not prefix:
        raise ValueError("prefix 不能为空")
    pattern = _escape_glob(prefix) + "*"
    deleted = 0
    batch: list[str] = []
    try:
        for key in redis_client.scan_iter(match=pattern, count=batch_size):
            batch.append(key)
            if len(batch) >= batch_size:
                deleted += int(redis_client.unlink(*batch))
                batch.clear()
        if batch:
            deleted += int(redis_client.unlink(*batch))
    except redis.RedisError as exc:
        logger.warning("Redis 按前缀删除缓存失败 prefix=%s: %s", prefix, exc)
    return deleted


def redis_ping() -> bool:
    """健康检查用：Redis 可达返回 ``True``。"""
    try:
        return bool(redis_client.ping())
    except redis.RedisError:
        return False
