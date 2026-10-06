"""标题接口的请求模型（docs/04 §6.10、§7.5；docs/09 §7）。

``count`` 的上限 ``generation_config.title.max_count``、``keyword_ids`` 的归属与状态（400 ``invalid_keyword``）、项目状态与模板 /
模型校验由 ``title_service`` / ``generation_service`` 实现。批量状态请求体复用 ``schemas.keyword.BatchStatusBody``。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import ContentStyle
from app.schemas.keyword import BatchStatusBody

__all__ = [
    "MAX_KEYWORD_IDS",
    "TITLE_MAX_CHARS",
    "BatchStatusBody",
    "TitleCreateBody",
    "TitleGenerateBody",
    "TitleScoreBody",
    "TitleUpdateBody",
]

MAX_KEYWORD_IDS = 50
TITLE_MAX_CHARS = 200

PositiveId = Annotated[int, Field(gt=0)]
TitleText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_CHARS)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TitleGenerateBody(_Body):
    """``POST /admin/titles/generate``（docs/09 §7.2）：``keyword_ids`` 1~50 个（均属于该项目且 ``status != discarded``）；
    ``count`` 为每个关键词的候选数（上限 ``title.max_count``）；``style`` 必填。"""

    project_id: PositiveId
    keyword_ids: list[PositiveId] = Field(min_length=1, max_length=MAX_KEYWORD_IDS)
    count: int = Field(ge=1, le=20)
    style: ContentStyle
    template_id: PositiveId | None = None
    model: ModelId | None = None


class TitleCreateBody(_Body):
    """``POST /admin/titles`` 手工新增（``source=manual``）；同关键词下重复标题服务端不拦截（前端提交前比对确认）。"""

    keyword_id: PositiveId
    title: TitleText
    style: ContentStyle


class TitleUpdateBody(_Body):
    """``PUT /admin/titles/{id}``：``{title, style?}``；首次编辑把原文写入 ``original_title``、``is_edited=true``。"""

    title: TitleText
    style: ContentStyle | None = None

    def changes(self) -> dict[str, Any]:
        data = self.model_dump(exclude_unset=True)
        if data.get("style") is None:
            data.pop("style", None)
        return data


class TitleScoreBody(_Body):
    """``POST /admin/titles/{id}/score``：``manual_score`` 0~10、步长 0.5（``null`` 清空）。"""

    manual_score: Decimal | None = Field(..., ge=0, le=10)

    @field_validator("manual_score")
    @classmethod
    def _step(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (value * 2) % 1 != 0:
            raise ValueError("步长必须为 0.5")
        return value
