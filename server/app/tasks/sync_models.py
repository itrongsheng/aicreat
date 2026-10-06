"""模型目录与价格同步（docs/08 §11.1；docs/01 §5.1 第 5 行）。

``sync_models() -> dict``：worker 启动时立即一次，之后每 ``ai_routing_config.catalog.sync_interval_seconds``（0 关闭）。
互斥 ``lock:ai:models_sync``（300s，与 ``POST /admin/ai/models/sync`` 共用，由 ``ai_catalog_service.sync_models`` 获取）；
返回 ``{total, added, updated, unavailable, synced_at, request_ids:{models, pricing}}``。锁被占用时返回
``{"skipped": True, "reason": "locked"}``；上游拉取失败（5021）不写 ``ai_models``，返回 ``{"error": {...}}`` 并记 warning。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.database import SessionLocal
from app.core.exceptions import CODE_CONFLICT, BusinessError
from app.services import ai_catalog_service

logger = logging.getLogger(__name__)


def sync_models() -> dict[str, Any]:
    with SessionLocal() as db:
        try:
            return ai_catalog_service.sync_models(db)
        except BusinessError as exc:
            db.rollback()
            if exc.code == CODE_CONFLICT:
                logger.info("模型目录正在由其它进程同步，本轮跳过")
                return {"skipped": True, "reason": "locked"}
            logger.warning("模型目录同步失败：%s data=%s", exc.message, exc.data)
            return {"error": exc.data if isinstance(exc.data, dict) else {"message": exc.message}}
