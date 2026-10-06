"""内容接口的请求模型（docs/04 §6.11、§7.6、§7.7；docs/09 §8）。

结构性约束（类型、长度、枚举、数组项数）在此声明，错误按 docs/04 §5.1 以 ``loc=["body", …]`` 返回；依赖配置或数据库的规则
（``target_word_count`` 落在 ``[min_word_count, max_word_count]``、``title_ids`` 须为该项目已采用标题、``mode`` 须在
``generation_config.rewrite.modes`` 内、``section_index`` 定位、大纲小节上限截断等）由 ``content_service`` 实现。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import ContentFormat, ContentStyle, RewriteMode, RewriteScope

__all__ = [
    "BODY_MAX_CHARS",
    "FAQ_A_MAX",
    "FAQ_MAX_ITEMS",
    "FAQ_Q_MAX",
    "MAX_TITLE_IDS",
    "OUTLINE_HEADING_MAX",
    "OUTLINE_MAX_ITEMS",
    "OUTLINE_POINT_MAX",
    "OUTLINE_POINTS_MAX",
    "SEO_KEYWORD_MAX",
    "SEO_KEYWORDS_MAX",
    "AttachBody",
    "ContentCreateBody",
    "ContentGenerateBody",
    "ContentUpdateBody",
    "ExportFormat",
    "FaqItemIn",
    "GenerateBodyBody",
    "GenerateOutlineBody",
    "GenerateSeoBody",
    "OutlineItemIn",
    "ApproveBody",
    "RejectBody",
    "RewriteBody",
]

MAX_TITLE_IDS = 20
CONTENT_TITLE_MAX = 200
BODY_MAX_CHARS = 1_000_000
OUTLINE_MAX_ITEMS = 40
OUTLINE_HEADING_MAX = 120
OUTLINE_POINTS_MAX = 8
OUTLINE_POINT_MAX = 200
SUMMARY_MAX = 500
SEO_TITLE_MAX = 200
SEO_DESCRIPTION_MAX = 500
SEO_KEYWORDS_MAX = 10
SEO_KEYWORD_MAX = 60
FAQ_MAX_ITEMS = 20
FAQ_Q_MAX = 200
FAQ_A_MAX = 1000
INSTRUCTION_MAX = 500
REVIEW_NOTE_MAX = 500

PositiveId = Annotated[int, Field(gt=0)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
ContentTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=CONTENT_TITLE_MAX)]
ExportFormat = Literal["md", "html", "json"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# =====================================================================
# 生成
# =====================================================================


class ContentGenerateBody(_Body):
    """``POST /admin/contents/generate``（docs/09 §8.1）：``title_ids`` 1~20 个（属于该项目且 ``status=adopted``，否则 400
    ``invalid_title``）；``target_word_count`` 须在 ``[content.min_word_count, content.max_word_count]``（service 校验）。"""

    project_id: PositiveId
    title_ids: list[PositiveId] = Field(min_length=1, max_length=MAX_TITLE_IDS)
    template_id: PositiveId | None = None
    outline_first: bool
    target_word_count: int = Field(ge=1, le=20000)
    include_faq: bool
    include_seo_meta: bool
    format: ContentFormat
    model: ModelId | None = None


class GenerateOutlineBody(_Body):
    """``POST /admin/contents/{id}/generate-outline``。"""

    template_id: PositiveId | None = None
    model: ModelId | None = None


class GenerateBodyBody(_Body):
    """``POST /admin/contents/{id}/generate-body``：``segmented`` 覆盖 ``generation_params_json.segmented``。"""

    segmented: bool | None = None
    template_id: PositiveId | None = None
    model: ModelId | None = None


class GenerateSeoBody(_Body):
    """``POST /admin/contents/{id}/generate-seo``：``include_faq`` 缺省取 ``generation_params_json.include_faq``（再缺省取配置）。"""

    include_faq: bool | None = None
    template_id: PositiveId | None = None
    model: ModelId | None = None


class RewriteBody(_Body):
    """``POST /admin/contents/{id}/rewrite``（docs/09 §8.7）：``scope=section`` 必带 ``section_index``（1 起，对应 ``outline`` 顺序）；
    ``restyle`` 必带 ``style``（其它模式忽略）。两条必填规则与定位校验由 service 以 ``loc=["body", <字段>]`` 返回。"""

    mode: RewriteMode
    scope: RewriteScope = "full"
    section_index: int | None = Field(None, ge=1)
    style: ContentStyle | None = None
    instruction: str | None = Field(None, max_length=INSTRUCTION_MAX)
    template_id: PositiveId | None = None
    model: ModelId | None = None


# =====================================================================
# 手工创建 / 编辑
# =====================================================================


class OutlineItemIn(_Body):
    """大纲项（docs/09 §8.2）：``heading`` 1~120 字符、``level ∈ {2,3}``、``points`` 0~8 项（每项 ≤ 200 字符）。"""

    heading: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=OUTLINE_HEADING_MAX)]
    level: Literal[2, 3]
    points: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=OUTLINE_POINT_MAX)]] = Field(
        default_factory=list, max_length=OUTLINE_POINTS_MAX
    )


class FaqItemIn(_Body):
    """FAQ 项：``q`` ≤ 200、``a`` ≤ 1000 字符。"""

    q: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FAQ_Q_MAX)]
    a: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FAQ_A_MAX)]


class ContentCreateBody(_Body):
    """``POST /admin/contents`` 手工创建草稿：``title_id`` / ``keyword_id`` 可选（须属于该项目）；带 ``body`` 时建版本
    ``source=manual``（状态仍为 ``draft``）。"""

    project_id: PositiveId
    title: ContentTitle
    title_id: PositiveId | None = None
    keyword_id: PositiveId | None = None
    format: ContentFormat
    style: ContentStyle
    body: str | None = Field(None, max_length=BODY_MAX_CHARS)


class ContentUpdateBody(_Body):
    """``PUT /admin/contents/{id}``：只提交需要修改的版本化字段（``outline`` / ``seo_keywords`` / ``faq`` 为 ``null`` 表示清空）；
    ``current_version_id`` 出现时做并发冲突检查（与服务端不一致 409 且不写入）。"""

    title: ContentTitle | None = None
    body: str | None = Field(None, max_length=BODY_MAX_CHARS)
    outline: list[OutlineItemIn] | None = Field(None, min_length=1, max_length=OUTLINE_MAX_ITEMS)
    summary: str | None = Field(None, max_length=SUMMARY_MAX)
    seo_title: str | None = Field(None, max_length=SEO_TITLE_MAX)
    seo_description: str | None = Field(None, max_length=SEO_DESCRIPTION_MAX)
    seo_keywords: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=SEO_KEYWORD_MAX)]] | None = Field(
        None, max_length=SEO_KEYWORDS_MAX
    )
    faq: list[FaqItemIn] | None = Field(None, max_length=FAQ_MAX_ITEMS)
    current_version_id: int | None = Field(None, ge=0)

    @field_validator("outline")
    @classmethod
    def _first_level(cls, value: list[OutlineItemIn] | None) -> list[OutlineItemIn] | None:
        if value and value[0].level != 2:
            raise ValueError("大纲首项必须为 level=2 的主小节")
        return value

    def changes(self) -> dict[str, Any]:
        """显式提交的字段（``exclude_unset``）；``title: null`` 视为未提交。"""
        data = self.model_dump(exclude_unset=True)
        if data.get("title") is None:
            data.pop("title", None)
        return data


# =====================================================================
# 审核 / 素材
# =====================================================================


class ApproveBody(_Body):
    """``POST /admin/contents/{id}/approve``：``note`` 可选。"""

    note: str | None = Field(None, max_length=REVIEW_NOTE_MAX)


class RejectBody(_Body):
    """``POST /admin/contents/{id}/reject``：``note`` 必填。"""

    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REVIEW_NOTE_MAX)]


class AttachBody(_Body):
    """``POST /admin/contents/{id}/assets/{asset_id}/attach``：``cover`` 要求 ``kind=image`` 且 ``status=ready``；``sort`` 缺省时
    已绑定的保持原值、新绑定取该内容现有素材 ``MAX(sort)+1``。"""

    usage_type: Literal["cover", "inline"]
    sort: int | None = Field(None, ge=0, le=100000)
