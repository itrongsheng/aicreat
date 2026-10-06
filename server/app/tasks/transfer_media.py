"""媒体转存（docs/10 §2.2、§4.8、§5.5；docs/03「媒体转存与素材引用」第 1、2 条）。

- ``transfer_asset(asset_id) -> bool``：持 ``lock:media:transfer:{asset_id}``（600s，下载中每 10 MB / 60s 续期）下载上游结果
  （上游 origin 带 Bearer、第三方 CDN 经 ``safe_fetch.stream_public_bytes`` 不带 Bearer、视频失败回退 ``/content``、Mock 复制占位
  文件）→ 魔数 / 大小校验 → ``storage.save`` → 资产 ``ready``；失败按 ``media_config.transfer`` 重排或置 ``failed(transfer_failed)``；
  实现见 ``media_service.transfer_asset``；
- ``retry_due(pool, limit=10) -> int``：每 60s 查询 ``status='downloading' AND next_transfer_at <= now``（且转存锁不存在）并提交线程池。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.database import SessionLocal
from app.services import media_service

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 10


def transfer_asset(asset_id: int) -> bool:
    """转存一个 ``downloading`` 资产（线程池线程内调用）；返回是否进入 ``ready``。异常只记日志（由 ``retry_due`` / 僵死回收兜底）。"""
    try:
        return media_service.transfer_asset(asset_id)
    except Exception:  # noqa: BLE001
        logger.exception("素材 %s 转存异常", asset_id)
        return False


def retry_due(pool: Any, limit: int = DEFAULT_LIMIT) -> int:
    """到期待转存的资产提交线程池，返回提交数。"""
    try:
        with SessionLocal() as db:
            due = media_service.due_transfers(db, limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("查询待转存素材失败")
        return 0
    for asset_id in due:
        pool.submit(transfer_asset, asset_id)
    return len(due)
