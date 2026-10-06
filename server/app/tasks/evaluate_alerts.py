"""告警周期评估（docs/11 §10.1、§10.5；docs/01 §5.2 monitor_worker 第 6 行；docs/03「告警去重」）。

``evaluate() -> dict`` 由 ``app.monitor_worker`` 每 300s 提交线程池执行（``optional_task("evaluate_alerts", "evaluate")``），
在独立会话内调用 ``alert_service.evaluate(db, SYSTEM_SCOPE)``：持 ``lock:monitor:evaluate_alerts``（300s）依次执行
① ``index_overdue``、② ``ai_task_failures``、③ ``ai_breaker_open`` 兜底、④ ``worker_stale``（``SCAN worker:heartbeat:worker:*``）、
⑤ 自动解决（``link_deleted`` / ``index_overdue`` / ``ai_task_failures`` / ``worker_stale``）。``raise_alert`` 以 ``dedupe_key`` 幂等；
事件型告警（``link_*``、``media_task_failed``、``ai_quota_exceeded``、``ai_auth_failed``）不经本任务。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.core.database import SessionLocal
from app.services import alert_service
from app.services.data_scope_service import SYSTEM_SCOPE

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 300


def evaluate(now: datetime | None = None) -> dict[str, Any]:
    """执行一轮告警评估，返回各规则计数；锁被占用时 ``{"skipped": True}``。"""
    with SessionLocal() as db:
        return alert_service.evaluate(db, SYSTEM_SCOPE, now=now)


__all__ = ["INTERVAL_SECONDS", "evaluate"]
