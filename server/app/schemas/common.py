"""通用请求 / 响应模型：分页、批量 ID、状态变更、CSV 导出参数与 UTC 时间格式化（docs/04 §1、§3、§9）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.core.response import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

# CSV 导出上限（docs/04 §3）与批量 ids[] 上限（docs/04 §9）
EXPORT_MAX_ROWS = 50_000
BATCH_MAX_IDS = 500


def to_utc_naive(value: datetime | None) -> datetime | None:
    """带时区的时间转为 UTC naive（库内 ``DATETIME`` 一律存 UTC）；naive 输入视为 UTC 原样返回。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def iso_utc(value: datetime | None) -> str | None:
    """库内 UTC 时间 → ISO 8601 UTC 字符串 ``2026-10-06T08:00:00Z``（``None`` 原样返回）。"""
    if value is None:
        return None
    value = to_utc_naive(value)
    return value.replace(microsecond=0).isoformat() + "Z"


# 响应模型中的 UTC 时间字段：序列化为 ``…Z``
UtcDatetime = Annotated[datetime, PlainSerializer(lambda v: iso_utc(v), return_type=str, when_used="json")]


class PageParams(BaseModel):
    """分页参数（``app.api.deps.get_pagination`` 解析）：``page`` 从 1 开始，``page_size`` 默认 20、最大 100。"""

    model_config = ConfigDict(frozen=True)

    page: int = Field(1, ge=1)
    page_size: int = Field(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class IdList(BaseModel):
    """批量操作的 ID 列表 ``{ids: [...]}``（1~500 个正整数，去重保序由 service 处理）。"""

    model_config = ConfigDict(extra="forbid")

    ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=BATCH_MAX_IDS)


class StatusBody(BaseModel):
    """启用 / 停用类状态变更 ``{is_active: bool}``。"""

    model_config = ConfigDict(extra="forbid")

    is_active: bool


class ExportParams(BaseModel):
    """CSV 导出参数基类：``format=csv`` + 各资源列表的筛选参数（子类声明），忽略分页参数。"""

    model_config = ConfigDict(extra="ignore")

    format: Literal["csv"] = "csv"
