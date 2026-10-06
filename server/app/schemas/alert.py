"""告警接口的请求模型（``/admin/alerts``，docs/04 §6.19、§7.13；docs/11 §10.2、§11.7；docs/13 §11）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import AlertSeverity, AlertStatus, AlertTargetType, AlertType
from app.schemas.common import BATCH_MAX_IDS

PositiveId = Annotated[int, Field(gt=0)]
# alerts.resolution_note VARCHAR(500)（docs/03 B.23）
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class AlertFilters(BaseModel):
    """``GET /admin/alerts``：``status`` / ``severity`` / ``alert_type`` / ``project_id`` / ``target_type`` / ``target_id`` /
    ``start`` / ``end``（作用于 ``last_triggered_at``）。不传 ``status`` 时不按状态过滤（含已解决）；``target_id`` 只匹配数值目标。"""

    model_config = ConfigDict(extra="ignore")

    status: AlertStatus | None = None
    severity: AlertSeverity | None = None
    alert_type: AlertType | None = None
    project_id: PositiveId | None = None
    target_type: AlertTargetType | None = None
    target_id: PositiveId | None = None
    start: datetime | None = None
    end: datetime | None = None


class AlertNoteBody(BaseModel):
    """``POST /admin/alerts/{id}/resolve`` / ``ignore`` 的可选 ``{note?}``。"""

    model_config = ConfigDict(extra="forbid")

    note: Note | None = None


class AlertBatchResolveBody(BaseModel):
    """``POST /admin/alerts/batch-resolve`` ``{ids[], note?}`` → ``{updated, skipped[{id, reason}]}``。"""

    model_config = ConfigDict(extra="forbid")

    ids: list[PositiveId] = Field(min_length=1, max_length=BATCH_MAX_IDS)
    note: Note | None = None


__all__ = ["AlertBatchResolveBody", "AlertFilters", "AlertNoteBody"]
