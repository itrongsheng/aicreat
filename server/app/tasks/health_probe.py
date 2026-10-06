"""自动健康探测（docs/08 §12.1、§12.2、§12.4；docs/01 §5.1 第 6 行）。

``probe_routes(pool) -> list[HealthResult]``：每 ``ai_routing_config.health.probe_interval_seconds``（600，0 关闭）在线程池执行：

- 互斥 ``lock:ai:health_probe``（TTL = 探测间隔，最小 120s），由本汇总任务持有，全部探测完成后 ``finally`` 释放；
- 探测对象 ``ai_task_service.probe_targets``：每条 ``is_enabled=1`` 路由的主 / 备模型 + 启用的 GEO 引擎（``geo_check``，引擎自身
  ``model`` / ``protocol``，不带 ``tools``）+ ``seo_providers.engines.<e>.model`` 非空的覆盖模型（``seo_check``），按
  ``(capability, model)`` 去重；各模型提交线程池并行（每个 ≤ 30s）；
- 文本最小探测 ``health.probe``（``probe_text_prompt="ping"``、``probe_max_tokens=8``）；``image`` / ``video`` 仅
  ``health.probe_media=true`` 时真实提交，否则按 ``ai_models.is_available`` 判定；
- 每次探测记 ``ai_tasks(trigger_type=health_probe, operation=route_probe, target_type=route_probe)``；按 §12.4 窗口规则写
  ``ai:health:{capability}:{model}``（86400s）与 ``ai_models.last_health_*``；同模型连续 2 次失败 →
  ``force_open(reason="probe_down")`` + ``ai_upstream_unavailable``；恢复 → 解决告警、``DEL ai:paused:*``、``probe_down`` 熔断 ``reset()``
  （``ai_task_service.record_health_result``）。
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from concurrent.futures import wait as futures_wait
from typing import Any

from app.core.database import SessionLocal
from app.core.locks import acquire_lock, release_lock
from app.core.zhiqi.types import ErrorCategory, HealthResult
from app.services import ai_task_service, settings_service
from app.services.ai_task_service import ProbeTarget

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:ai:health_probe"
MIN_LOCK_TTL = 120


def _probe_config() -> tuple[int, bool]:
    with SessionLocal() as db:
        health = (settings_service.get_config(db, "ai_routing_config").get("health") or {})
    return int(health.get("probe_interval_seconds") or 0), bool(health.get("probe_media"))


def _targets() -> list[ProbeTarget]:
    with SessionLocal() as db:
        return ai_task_service.probe_targets(db)


def _probe(target: ProbeTarget, probe_media: bool) -> HealthResult:
    with SessionLocal() as db:
        try:
            return ai_task_service.probe_and_record(
                db, capability=target.capability, model=target.model, preferred_protocol=target.preferred_protocol,
                route_id=target.route_id, created_by=None, probe_media=probe_media,
            )
        except Exception as exc:  # noqa: BLE001 - 单个模型探测失败不影响其它模型
            db.rollback()
            logger.exception("自动探测异常 capability=%s model=%s", target.capability, target.model)
            return ai_task_service.failed_probe_result(target, ErrorCategory.UNKNOWN, f"{type(exc).__name__}: {exc}"[:500])


def _parallel(pool: Any) -> bool:
    """有线程池且不是 SQLite 测试会话（共享单连接）时并行。"""
    if pool is None or not hasattr(pool, "submit"):
        return False
    bind = SessionLocal.kw.get("bind")
    return not (bind is not None and getattr(bind.dialect, "name", "") == "sqlite")


def probe_routes(pool: Any = None) -> list[HealthResult]:
    """自动探测全部对象；``pool`` 为 worker 线程池（``None`` 时串行，供测试与手动调用）。锁被占用时返回 ``[]``。"""
    interval, probe_media = _probe_config()
    ttl = max(interval, MIN_LOCK_TTL)
    token = acquire_lock(LOCK_KEY, ttl)
    if not token:
        logger.info("自动健康探测仍在进行（%s 被占用），本轮跳过", LOCK_KEY)
        return []
    try:
        targets = _targets()
        if not targets:
            return []
        results: list[HealthResult] = []
        if _parallel(pool):
            futures: list[tuple[ProbeTarget, Future | None]] = []
            for target in targets:
                try:
                    futures.append((target, pool.submit(_probe, target, probe_media)))
                except RuntimeError:  # 线程池已关闭：剩余模型在本线程串行完成
                    futures.append((target, None))
            futures_wait([f for _, f in futures if f is not None], timeout=ttl)
            for target, future in futures:
                if future is None:
                    results.append(_probe(target, probe_media))
                elif future.done() and not future.cancelled():
                    results.append(future.result())
                else:
                    results.append(ai_task_service.failed_probe_result(target, ErrorCategory.TIMEOUT, "探测超时"))
        else:
            results = [_probe(target, probe_media) for target in targets]
        down = [f"{r.capability.value}:{r.model}" for r in results if r.status == "down"]
        logger.info("自动健康探测完成：%s 个模型，失败 %s", len(results), down or "无")
        return results
    finally:
        release_lock(LOCK_KEY, token)
