"""``GET /api/v1/health``（公开，docs/04 §6.21、§7.16）。

``{status, db, redis, zhiqi_mode, workers{worker{alive,replicas,heartbeat_at}, monitor_worker{…}}, warnings[], version}``：
db / redis 不可用返回 HTTP 503；worker 不活跃只令 ``status="degraded"``（HTTP 200）。
worker 心跳键 ``worker:heartbeat:{name}:{hostname}:{pid}``（JSON ``{hostname, pid, at}``，TTL 900s），
``alive`` = 任一副本 ``now − at < 90s``（docs/01 §6.3）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import tomllib
from datetime import UTC, datetime
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import SERVER_DIR, settings
from app.core.database import get_db
from app.core.redis import redis_client
from app.core.response import ok

logger = logging.getLogger(__name__)

router = APIRouter()

WORKER_NAMES: tuple[str, ...] = ("worker", "monitor_worker")
HEARTBEAT_PREFIX = "worker:heartbeat:"
HEARTBEAT_ALIVE_SECONDS = 90
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------- 版本


@lru_cache(maxsize=1)
def app_version() -> str:
    """``server/pyproject.toml`` 的 ``project.version``；读不到时回退安装包元数据。"""
    try:
        with (Path(SERVER_DIR) / "pyproject.toml").open("rb") as fh:
            version = tomllib.load(fh).get("project", {}).get("version")
        if version:
            return str(version)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("aicreat-server")
    except metadata.PackageNotFoundError:
        return "0.0.0"


# ---------------------------------------------------------------- worker 心跳


def _parse_at(value: Any) -> datetime:
    """心跳 ``at``：ISO 8601（``Z`` / 偏移 / naive 视为 UTC）或 Unix 时间戳；无法解析视为 1970-01-01。"""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return _EPOCH
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC)
        except ValueError:
            pass
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return _EPOCH
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return _EPOCH


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _read_replicas(name: str, now: datetime) -> list[tuple[datetime, dict[str, Any]]]:
    keys = sorted(redis_client.scan_iter(match=f"{HEARTBEAT_PREFIX}{name}:*", count=200))
    if not keys:
        return []
    values = redis_client.mget(keys)
    replicas: list[tuple[datetime, dict[str, Any]]] = []
    for key, raw in zip(keys, values, strict=False):
        hostname, _, pid = key[len(HEARTBEAT_PREFIX) + len(name) + 1 :].rpartition(":")
        data: dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw)
                data = loaded if isinstance(loaded, dict) else {"at": loaded}
            except (TypeError, ValueError):
                data = {}
        at = _parse_at(data.get("at"))
        try:
            pid_value: Any = int(data.get("pid", pid))
        except (TypeError, ValueError):
            pid_value = data.get("pid", pid)
        replicas.append((at, {
            "name": name,
            "hostname": data.get("hostname") or hostname,
            "pid": pid_value,
            "heartbeat_at": _iso(at) if at > _EPOCH else None,
            "alive": (now - at).total_seconds() < HEARTBEAT_ALIVE_SECONDS,
        }))
    return replicas


def worker_replicas(name: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """``SCAN worker:heartbeat:{name}:*`` 读取各副本：``[{name, hostname, pid, heartbeat_at, alive}]``（Redis 异常时抛出）。"""
    return [item for _at, item in _read_replicas(name, now or datetime.now(UTC))]


def summarize_workers(now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """两个 worker 进程的汇总：``alive``（任一副本存活）、``replicas``（存活副本数）、``heartbeat_at``（最新心跳）。"""
    now = now or datetime.now(UTC)
    result: dict[str, dict[str, Any]] = {}
    for name in WORKER_NAMES:
        replicas = _read_replicas(name, now)
        latest = max((at for at, _item in replicas), default=_EPOCH)
        alive = [item for _at, item in replicas if item["alive"]]
        result[name] = {
            "alive": bool(alive),
            "replicas": len(alive),
            "heartbeat_at": _iso(latest) if latest > _EPOCH else None,
        }
    return result


# ---------------------------------------------------------------- PUBLIC_BASE_URL 公网检查（真实模式）

_public_warning_lock = threading.Lock()
_public_warning_cache: dict[str, list[str]] = {}


def _non_public(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def check_public_base_url(url: str | None = None) -> list[str]:
    """``PUBLIC_BASE_URL`` 主机解析为非公网地址时返回提示（仅真实模式检查；结果按 URL 缓存于进程内）。"""
    if settings.zhiqi_mock_mode:
        return []
    url = (url or settings.public_base_url or "").strip()
    with _public_warning_lock:
        if url in _public_warning_cache:
            return list(_public_warning_cache[url])
    warnings: list[str] = []
    host = urlsplit(url).hostname or ""
    if not host:
        warnings.append("PUBLIC_BASE_URL 未配置或格式错误，真实模式下参考素材无法被 zhiqiapi 访问")
    else:
        addresses: list[str] = []
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            if host.lower() == "localhost" or "." not in host:
                addresses = ["127.0.0.1"]
            else:
                try:
                    addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
                except OSError:
                    warnings.append(f"PUBLIC_BASE_URL 主机 {host} 无法解析，真实模式下参考素材无法被 zhiqiapi 访问")
        if addresses and any(_non_public(addr) for addr in addresses):
            warnings.append(f"PUBLIC_BASE_URL 主机 {host} 解析为非公网地址，真实模式下参考素材无法被 zhiqiapi 访问")
    with _public_warning_lock:
        _public_warning_cache[url] = list(warnings)
    return warnings


def reset_public_base_url_cache() -> None:
    with _public_warning_lock:
        _public_warning_cache.clear()


# ---------------------------------------------------------------- 路由


def _db_ok(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查：数据库不可用 %s", exc)
        return False


@router.get("/health", summary="健康检查（公开）")
def health(db: Session = Depends(get_db)) -> Any:
    db_ok = _db_ok(db)
    try:
        redis_ok = bool(redis_client.ping())
    except redis.RedisError as exc:
        logger.warning("健康检查：Redis 不可用 %s", exc)
        redis_ok = False

    workers = {name: {"alive": False, "replicas": 0, "heartbeat_at": None} for name in WORKER_NAMES}
    if redis_ok:
        try:
            workers = summarize_workers()
        except redis.RedisError as exc:
            logger.warning("健康检查：读取 worker 心跳失败 %s", exc)
            redis_ok = False

    healthy = db_ok and redis_ok and all(w["alive"] for w in workers.values())
    data = {
        "status": "ok" if healthy else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "zhiqi_mode": "mock" if settings.zhiqi_mock_mode else "live",
        "workers": workers,
        "warnings": check_public_base_url(),
        "version": app_version(),
    }
    if not (db_ok and redis_ok):
        return JSONResponse(status_code=503, content={"code": 503, "message": "服务暂不可用", "data": data})
    return ok(data)
