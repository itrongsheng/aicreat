"""关键词接口的请求模型与 CSV 导出列（docs/04 §6.9、§7.4；docs/09 §6）。

需要查库或读配置的规则（``count`` 上限 ``generation_config.keyword.max_count``、项目状态、去重、模板 / 模型校验）由
``keyword_service`` / ``generation_service`` 实现，错误项同为 ``[{loc, msg, type, input}]``。导入条目的 ``intent`` /
``keyword_type`` 接收任意字符串，非法值由 service 计入 ``errors[]``（``invalid_intent`` / ``invalid_keyword_type``）而不是整体 400。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import KeywordIntent, KeywordType
from app.schemas.common import BATCH_MAX_IDS

MAX_SEEDS = 20
SEED_MAX_CHARS = 60
MAX_COMPETITORS = 10
COMPETITOR_MAX_CHARS = 60
AUDIENCE_MAX_CHARS = 255
KEYWORD_MAX_CHARS = 120
IMPORT_MAX_ITEMS = 5000
IMPORT_FILE_MAX_BYTES = 2 * 1024 * 1024
IMPORT_FILE_COLUMNS: tuple[str, ...] = ("keyword", "intent", "keyword_type")
MAX_TAGS = 20
TAG_MAX_CHARS = 30

PositiveId = Annotated[int, Field(gt=0)]
Seed = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SEED_MAX_CHARS)]
Competitor = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=COMPETITOR_MAX_CHARS)]
KeywordText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=KEYWORD_MAX_CHARS)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TAG_MAX_CHARS)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]

# CSV 导出列（docs/09 §6.8 的列顺序；列头为中文，与列表页表头一致，docs/04 §9）
EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("keyword", "关键词"),
    ("intent", "意图"),
    ("keyword_type", "类型"),
    ("difficulty", "难度"),
    ("heat", "热度"),
    ("score", "分数"),
    ("seed", "种子词"),
    ("source", "来源"),
    ("status", "状态"),
    ("title_count", "标题数"),
    ("content_count", "内容数"),
    ("created_at", "创建时间"),
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeywordGenerateBody(_Body):
    """``POST /admin/keywords/generate``（docs/09 §6.1）：``seeds`` 1~20 个（每个去首尾空白后 1~60 字符，归一化后去重）；
    ``count`` 的上限 ``generation_config.keyword.max_count`` 由 service 校验；``template_id`` / ``model`` 见 docs/09 §4.2、§6.1。"""

    project_id: PositiveId
    seeds: list[Seed] = Field(min_length=1, max_length=MAX_SEEDS)
    count: int = Field(ge=1, le=100)
    competitors: list[Competitor] = Field(default_factory=list, max_length=MAX_COMPETITORS)
    audience: Annotated[str, StringConstraints(strip_whitespace=True, max_length=AUDIENCE_MAX_CHARS)] | None = None
    template_id: PositiveId | None = None
    model: ModelId | None = None


class KeywordCreateBody(_Body):
    """``POST /admin/keywords`` 手工新增（``source=manual``、``status=candidate``）；``normalized_keyword`` 重复 409 ``existing_id``。"""

    project_id: PositiveId
    keyword: KeywordText
    intent: KeywordIntent
    keyword_type: KeywordType


class KeywordImportItem(_Body):
    """导入条目：``intent`` / ``keyword_type`` 可空（取 ``unknown`` / ``core``），非法值计入 ``errors[]``。"""

    keyword: str | None = None
    intent: str | None = None
    keyword_type: str | None = None


class KeywordImportBody(_Body):
    """``POST /admin/keywords/import``：``items`` 1~5,000 条（超出 400）。"""

    project_id: PositiveId
    items: list[KeywordImportItem] = Field(min_length=1, max_length=IMPORT_MAX_ITEMS)


class KeywordUpdateBody(_Body):
    """``PUT /admin/keywords/{id}``：只写出现的字段；``keyword`` / ``intent`` / ``keyword_type`` 传 ``null`` 视为未修改，
    ``difficulty`` / ``heat`` / ``score`` / ``tags`` 传 ``null`` 清空；状态不可在此修改。"""

    keyword: KeywordText | None = None
    intent: KeywordIntent | None = None
    keyword_type: KeywordType | None = None
    difficulty: int | None = Field(None, ge=1, le=100)
    heat: int | None = Field(None, ge=1, le=100)
    score: Decimal | None = Field(None, ge=0, le=100, max_digits=5, decimal_places=2)
    tags: list[Tag] | None = Field(None, max_length=MAX_TAGS)

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True)
        for key in ("keyword", "intent", "keyword_type"):
            if key in data and data[key] is None:
                data.pop(key)
        return data


StatusAction = Literal["adopt", "discard", "restore"]


class BatchStatusBody(_Body):
    """``POST /admin/keywords/batch-status`` / ``POST /admin/titles/batch-status``：``ids`` ≤ 500（超出 400）。"""

    ids: list[PositiveId] = Field(min_length=1, max_length=BATCH_MAX_IDS)
    action: StatusAction
