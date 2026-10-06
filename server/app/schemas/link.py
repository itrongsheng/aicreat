"""回填链接接口的请求模型（docs/04 §6.17、§7.10、§7.11；docs/11 §4）。

字段的业务校验（内容可见性与状态、项目 ``active``、URL 预校验、平台、``url_hash`` 去重、``published_at`` 范围、
``publish_account`` ≤ 100 / ``note`` ≤ 500 字符）按 docs/11 §4.1 的顺序在 ``link_service`` 中执行，因此这里只声明类型，
不设长度约束（否则 Pydantic 会先于第 1~6 步报错）。批量回填的 ``items`` 为原始对象，逐条在 service 中解析，单条失败不影响其它条。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import IndexKind, LinkAliveStatus

BATCH_MAX_ITEMS = 100
PositiveId = Annotated[int, Field(gt=0)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkCreate(_Body):
    """单条回填 ``{content_id, platform_id?, url, publish_account?, published_at?, note?}``。"""

    content_id: PositiveId
    platform_id: PositiveId | None = None
    url: str
    publish_account: str | None = None
    published_at: datetime | None = None
    note: str | None = None


class LinkBatchCreate(_Body):
    """批量回填 ``{items:[…]}``（1~100 条，超出 400）；每条按单条规则逐条独立校验与提交。"""

    items: list[dict[str, Any]] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)


class LinkUpdate(_Body):
    """编辑 ``{platform_id?, publish_account?, published_at?, note?}``（URL 不可改：出现 ``url`` 返回 400 不允许的字段）。

    只写出现的字段；``publish_account`` / ``note`` 传 ``null`` 或空串清空。"""

    platform_id: PositiveId | None = None
    publish_account: str | None = None
    published_at: datetime | None = None
    note: str | None = None


class LinkFilters(BaseModel):
    """列表 / 导出的筛选参数（``GET /admin/links``、``GET /admin/links/export``）。"""

    model_config = ConfigDict(extra="ignore")

    project_id: PositiveId | None = None
    content_id: PositiveId | None = None
    platform_id: PositiveId | None = None
    alive_status: LinkAliveStatus | None = None
    seo_indexed_any: bool | None = None
    geo_cited_any: bool | None = None
    is_monitoring: bool | None = None
    keyword: Annotated[str, Field(max_length=200)] | None = None
    published_start: datetime | None = None
    published_end: datetime | None = None


class LinkIndexCheckBody(_Body):
    """``POST /admin/links/{id}/index-check`` ``{kinds:[seo,geo], engines?[]}``：``engines`` 省略时取所选 ``kinds`` 下全部
    启用引擎；传入未启用引擎在 service 中返回 400（``loc=["body","engines",i]``）。"""

    kinds: list[IndexKind] = Field(min_length=1, max_length=2)
    engines: list[Annotated[str, Field(min_length=1, max_length=32)]] | None = Field(None, max_length=50)


class LinkMarkIndexBody(_Body):
    """``POST /admin/links/{id}/mark-index`` ``{kind, engine, status, note?, evidence_url?}``：``status`` 只接受确定结论；与
    ``kind`` 不符（如 ``kind=seo`` 配 ``cited``）在 service 中返回 400 ``loc=["body","status"]``。"""

    kind: IndexKind
    engine: Annotated[str, Field(min_length=1, max_length=32)]
    status: Literal["indexed", "not_indexed", "cited", "not_cited"]
    note: Annotated[str, Field(max_length=500)] | None = None
    evidence_url: Annotated[str, Field(max_length=1000)] | None = None
