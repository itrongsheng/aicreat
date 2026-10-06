"""媒体清理（docs/10 §6.4、§11.3；docs/03「媒体转存与素材引用」第 5 条）。

``cleanup() -> dict``：每日 03:00（``stats_config.timezone``，由 ``app.worker`` 的 ``run_daily`` 调度）执行
``media_service.cleanup_media``：

- 失败残留：``failed`` / ``expired`` 超过 ``media_config.retention.failed_days``（30）的本地下载缓存 ``tmp/{asset_id}.part`` 与无 ``url``
  的 ``storage_key`` 对象（记录行保留）；``LOCAL_STORAGE_DIR/tmp/`` 下超过 1 天的 ``*.part`` 无条件删除；
- 孤儿参考素材：上传的 ``reference`` 素材 ``ready`` 超过 ``retention.orphan_reference_days``（7）且未被任何
  ``reference_asset_ids_json`` 引用 → 置 ``deleted`` 并删除存储文件。

返回 ``{failed_cleaned, orphans_deleted}``（同时写 INFO 日志）。``ready`` 的生成素材不自动删除。
"""

from __future__ import annotations

from datetime import datetime

from app.services import media_service


def cleanup(now: datetime | None = None) -> dict[str, int]:
    return media_service.cleanup_media(now)
