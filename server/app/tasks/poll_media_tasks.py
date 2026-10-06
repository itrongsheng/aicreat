"""媒体任务轮询（docs/10 §4.7、§5.4；docs/01 §5.1；docs/08 §7.2、§7.3、§9.4）。

``poll_due(pool, limit=20) -> int`` 由 ``app.worker`` 主循环每轮调用（只做短 SQL 与抢占，不做网络 I/O）：

1. ``SELECT … FROM ai_tasks WHERE status='polling' AND next_poll_at <= now ORDER BY next_poll_at LIMIT 20``；
2. 逐条 ``UPDATE … SET next_poll_at = now + 60s WHERE id=? AND status='polling' AND next_poll_at <= now`` 抢占（rowcount=0 跳过）；
3. 抢到的根任务提交线程池执行 ``poll_one``：``media_service.poll_root``（``ai_gateway_service.poll_task`` → 状态流转与间隔重排），
   上游 ``succeeded`` 时立即 ``pool.submit(transfer_media.transfer_asset, asset_id)``。

不受 ``ai:paused:*`` 影响（轮询 ``GET`` 不计费）；多副本互斥依赖第 2 步的条件更新。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.database import SessionLocal
from app.services import media_service

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 20


def poll_one(task_id: int, pool: Any = None) -> int | None:
    """执行一次轮询；上游成功时把转存提交到 ``pool``（未给出 ``pool`` 时由 ``transfer_media.retry_due`` 在 60s 内领取）。
    返回进入 ``downloading`` 的资产 ID。"""
    asset_id = media_service.poll_task_id(task_id)
    if asset_id and pool is not None:
        from app.tasks import transfer_media

        try:
            pool.submit(transfer_media.transfer_asset, asset_id)
        except RuntimeError:  # 线程池已关闭：由 retry_due 补领（资产 next_transfer_at = now）
            logger.info("线程池已关闭，素材 %s 的转存由 retry_due 补领", asset_id)
    return asset_id


def poll_due(pool: Any, limit: int = DEFAULT_LIMIT) -> int:
    """抢占到期的 ``polling`` 根任务并提交线程池，返回提交数。"""
    try:
        with SessionLocal() as db:
            claimed = media_service.claim_due_polls(db, limit=limit)
    except Exception:  # noqa: BLE001 - 主循环线程内：异常只记日志
        logger.exception("查询到期的媒体轮询任务失败")
        return 0
    for task_id in claimed:
        pool.submit(poll_one, task_id, pool)
    return len(claimed)
