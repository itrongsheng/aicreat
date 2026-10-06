"""监控接口的请求模型（``/admin/monitoring``，docs/04 §6.18、§7.11、§7.12；docs/11 §6.7、§9、§11.6；docs/13 §6.3、§7.5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models import CheckType, IndexKind, LinkCheckResult
from app.schemas.common import BATCH_MAX_IDS

PositiveId = Annotated[int, Field(gt=0)]
EngineCode = Annotated[str, Field(min_length=1, max_length=32)]
# link_ids[] 与其它 ids[] 类批量同为 ≤ 500 个（docs/04 §5「批量接口」），超出 400
RUN_MAX_LINK_IDS = BATCH_MAX_IDS


class LinkCheckFilters(BaseModel):
    """``GET /admin/monitoring/link-checks``：``project_id`` / ``platform_id`` / ``result_status`` / ``check_type`` / ``link_id`` /
    ``start`` / ``end``（按 ``checked_at``）。"""

    model_config = ConfigDict(extra="ignore")

    project_id: PositiveId | None = None
    platform_id: PositiveId | None = None
    result_status: LinkCheckResult | None = None
    check_type: CheckType | None = None
    link_id: PositiveId | None = None
    start: datetime | None = None
    end: datetime | None = None


class IndexCheckFilters(BaseModel):
    """``GET /admin/monitoring/index-checks``：``kind`` / ``engine`` / ``provider`` / ``result_status`` / ``project_id`` /
    ``platform_id`` / ``link_id`` / ``start`` / ``end``。"""

    model_config = ConfigDict(extra="ignore")

    kind: IndexKind | None = None
    engine: Annotated[str, Field(max_length=32)] | None = None
    provider: Annotated[str, Field(max_length=32)] | None = None
    result_status: Annotated[str, Field(max_length=16)] | None = None
    project_id: PositiveId | None = None
    platform_id: PositiveId | None = None
    link_id: PositiveId | None = None
    start: datetime | None = None
    end: datetime | None = None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkChecksRunBody(_Body):
    """``POST /admin/monitoring/link-checks/run`` ``{project_id?, platform_id?, link_ids?[], only_due:true}``。"""

    project_id: PositiveId | None = None
    platform_id: PositiveId | None = None
    link_ids: list[PositiveId] | None = Field(None, max_length=RUN_MAX_LINK_IDS)
    only_due: bool = True


class IndexChecksRunBody(_Body):
    """``POST /admin/monitoring/index-checks/run`` ``{kinds:[seo,geo], engines?[], project_id?, platform_id?, link_ids?[],
    only_due:true}``。"""

    kinds: list[IndexKind] = Field(min_length=1, max_length=2)
    engines: list[EngineCode] | None = Field(None, max_length=50)
    project_id: PositiveId | None = None
    platform_id: PositiveId | None = None
    link_ids: list[PositiveId] | None = Field(None, max_length=RUN_MAX_LINK_IDS)
    only_due: bool = True
