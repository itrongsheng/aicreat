"""用量对账（docs/08 §10.3~§10.6；docs/01 §3.5、§5.1 第 4 行；docs/03「用量对账回填」）。

``reconcile() -> dict``：每 ``ai_routing_config.usage.reconcile_interval_seconds``（每轮读 DB，0 关闭；上次拉取
``window_overflow=true`` 时临时降到 ``usage.min_interval_seconds``，见 ``interval_seconds``）在线程池执行，持
``lock:worker:reconcile``（300s，与 ``POST /admin/ai/usage/reconcile`` 互斥）：

1. ``usage.fetch_token_logs(client)``（Mock：``mock_token_logs()``）拉取最近 1000 条；
2. 逐条 ``entry_hash`` 幂等入库 ``ai_usage_logs``；本轮新条目按 ``request_id`` → ``upstream_task_id`` 匹配近 7 天尝试行，
   回填 ``quota_actual`` / ``reconciled_at`` / ``usage_log_type`` / ``cost_cny`` 并重算根任务合计列；
3. 匹配任务涉及的 ``stat_date``：今日 / 昨日交常规聚合，其它日期直接 ``aggregate_daily_stats.aggregate(stat_date)``
   （第 6 步提供；模块不存在时跳过并记日志，见 ``ai_usage_service._trigger_aggregates``）；
4. 清理过期未匹配条目，``SET ai:usage:last_pull``（86400s）。

返回 ``{pulled, new, matched, unmatched, window_overflow, request_ids[]}``；锁被占用 → ``{"skipped": True, ...}``；
拉取失败 → ``{"error": {...}}``（不写库）。对账不回写 ``quota:daily`` / ``stats:rt``。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.exceptions import CODE_CONFLICT, BusinessError
from app.services import ai_usage_service

logger = logging.getLogger(__name__)


def interval_seconds(db: Session) -> int:
    """下一次自动对账的间隔（秒，0 关闭）：``window_overflow`` 后临时降到 ``usage.min_interval_seconds``（docs/08 §10.6）。"""
    return ai_usage_service.next_interval_seconds(db)


def reconcile() -> dict[str, Any]:
    with SessionLocal() as db:
        try:
            return ai_usage_service.reconcile(db)
        except BusinessError as exc:
            db.rollback()
            if exc.code == CODE_CONFLICT:
                logger.info("用量对账正在由其它进程执行，本轮跳过")
                return {"skipped": True, "reason": "locked"}
            logger.warning("用量对账失败：%s data=%s", exc.message, exc.data)
            return {"error": exc.data if isinstance(exc.data, dict) else {"message": exc.message}}
