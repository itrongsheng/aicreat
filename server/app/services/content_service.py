"""内容（docs/09 §8、§9；docs/03 B.12、B.13、「冗余计数回写」「内容版本保留」「内容状态流转」「删除规则」；docs/04 §6.11、§7.6、
§7.7；docs/10 §4.9；docs/13 §6.3、§7.1、§7.4）。

- 状态机 ``transition(content, action, actor)``：所有 ``contents.status`` 变更的唯一入口（非法流转 409 ``current_status``）；进入
  ``generating`` / ``archived`` 写 ``prev_status``，离开时恢复并清空；审核动作同事务写 ``review_result`` / ``reviewed_by`` /
  ``reviewed_at`` / ``review_note``；
- 版本 ``write_version``：8 个版本化字段的任何变更都在同一事务内计算 ``content_hash``（与当前版本相同 → 不建版本，
  ``version_created=false``）→ 达到 ``rewrite.max_versions`` 时自动裁剪（``change_summary`` 追加 ``auto_pruned=<version_no>``）→
  ``INSERT content_versions`` 并同步 ``contents`` 的 8 列 / ``current_version_id`` / ``version_count`` / ``word_count`` → 质量规则；
- 文本工具：``sanitize_html``（无第三方依赖的允许列表清理）、``count_words``、``validate_outline``、``split_sections`` /
  ``locate_section`` / ``replace_section``（小节重写）、``assemble_sections`` / ``assemble_whole``（分段拼接，§8.3）、
  ``markdown_to_html``（导出）；
- 质量规则 ``evaluate_quality`` → ``quality_score`` / ``risk_flags``（§8.9），``submit-review`` 命中 ``banned_word`` / ``too_short`` 时 409
  ``reason=quality_blocked``；
- 接口：列表 / 详情（``active_task_id`` / ``pending_tasks``）/ 任务摘要 / 手工创建与编辑（``current_version_id`` 乐观检查）/ 审核 /
  归档 / 删除 / 版本 / 素材绑定 / 链接 / 导出；生成接口 ``generate_contents``（批次）与 ``generate_outline`` / ``generate_body`` /
  ``rewrite_content`` / ``generate_seo``（单内容根任务，``(target_type, target_id, operation, 非终态)`` 查重 409 ``existing_id``）；
- worker 处理器：``content_generate``（大纲先行 → 分段正文 → SEO 要素 → FAQ，终态事务一次写 v1）、``content_outline``、
  ``content_body``、``content_rewrite``、``content_seo``；失败 / 取消恢复 ``prev_status``；重试前按 §8.1 起始状态重新校验；
- 实时计数（docs/12 §4.6）：内容创建 ``contents_created``、审核通过（含自动通过）``contents_approved``。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.zhiqi import text as zhiqi_text
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import ErrorCategory, TextResult
from app.models import (
    CONTENT_VERSIONED_FIELDS,
    AiTask,
    Content,
    ContentVersion,
    Keyword,
    MediaAsset,
    Project,
    PromptTemplate,
    PublishLink,
    PublishPlatform,
    Title,
    utcnow,
)
from app.schemas.common import iso_utc
from app.services import ai_gateway_service as gateway
from app.services import (
    ai_task_service,
    generation_service,
    settings_service,
    stats_service,
)
from app.services import prompt_template_service as pts
from app.services.data_scope_service import (
    MAX_BIGINT,
    DataScope,
    get_visible,
    require_project,
    scope_by_project,
    scope_media,
)
from app.services.title_service import style_text, title_dedup_key

logger = logging.getLogger(__name__)

__all__ = [
    "ACTIVE_TASK_OPERATIONS",
    "CONTENT_OPERATIONS",
    "PENDING_TASK_OPERATIONS",
    "QUALITY_DEDUCTIONS",
    "Section",
    "TRANSITIONS",
    "approve_content",
    "archive_content",
    "assemble_sections",
    "assemble_whole",
    "asset_item",
    "attach_asset",
    "clean_output",
    "content_detail",
    "content_hash",
    "content_item",
    "content_task",
    "count_h2",
    "count_words",
    "create_content",
    "delete_content",
    "delete_version",
    "detach_asset",
    "evaluate_quality",
    "export_content",
    "generate_body",
    "generate_contents",
    "generate_outline",
    "generate_seo",
    "generation_params",
    "get_content_row",
    "get_version",
    "link_item",
    "list_assets",
    "list_contents",
    "list_links",
    "list_versions",
    "locate_section",
    "markdown_to_html",
    "refresh_quality",
    "reject_content",
    "render_outline",
    "render_section",
    "replace_section",
    "restore_version",
    "rewrite_content",
    "sanitize_html",
    "split_sections",
    "submit_review",
    "task_summary",
    "transition",
    "unarchive_content",
    "update_content",
    "validate_outline",
    "version_item",
    "write_version",
]

# =====================================================================
# 常量
# =====================================================================

CONTENT_NOT_FOUND = "内容不存在"
VERSION_NOT_FOUND = "版本不存在"
ASSET_NOT_FOUND = "素材不存在"
MSG_STATUS_CONFLICT = "当前状态不允许该操作"
MSG_VERSION_CONFLICT = "内容已被修改，请刷新后再保存"
MSG_QUALITY_BLOCKED = "内容未通过质量检查，不能提审"
MSG_TASK_EXISTS = "该内容已有进行中的同类任务"
MSG_IN_USE = "内容已有回填链接，不能删除"
MSG_CURRENT_VERSION = "不能删除当前版本"
MSG_ASSET_NOT_READY = "素材未就绪"
MSG_BODY_EMPTY = "正文为空"
MSG_INVALID_TITLE = "标题未采用或不属于该项目"

ACTIVE_ROOT_STATUSES = ("queued", "running", "polling")
CONTENT_OPERATIONS: tuple[str, ...] = ("content_generate", "content_outline", "content_body", "content_seo", "content_rewrite")
ACTIVE_TASK_OPERATIONS: tuple[str, ...] = ("content_generate", "content_body", "content_rewrite")   # → active_task_id
PENDING_TASK_OPERATIONS: tuple[str, ...] = ("content_outline", "content_seo")                       # → pending_tasks[]
BODY_OPERATIONS = ACTIVE_TASK_OPERATIONS

GENERATE_START = ("draft", "ready", "rejected")                       # generate-body / rewrite → generating
SIDE_TASK_STATES = ("draft", "ready", "rejected", "approved", "published")   # generate-outline / generate-seo（不改状态）
EDITABLE_STATES = ("draft", "ready", "rejected", "approved", "published")    # PUT
REWRITE_NO_CHANGE = ("approved", "published")

BLOCKING_FLAGS = ("banned_word", "too_short")
QUALITY_DEDUCTIONS: dict[str, int] = {
    "too_short": 30,
    "too_long": 10,
    "missing_h2": 15,
    "too_many_h2": 5,
    "banned_word": 40,
    "duplicate_title": 15,
    "missing_seo_meta": 10,
    "missing_faq": 5,
    "truncated": 20,
}

TITLE_MAX = 200
SUMMARY_MAX = 500
SEO_TITLE_MAX = 200
SEO_DESCRIPTION_MAX = 500
SEO_KEYWORDS_MAX = 10
SEO_KEYWORD_MAX = 60
FAQ_Q_MAX = 200
FAQ_A_MAX = 1000
OUTLINE_MAX_ITEMS = 40
OUTLINE_HEADING_MAX = 120
OUTLINE_POINTS_MAX = 8
OUTLINE_POINT_MAX = 200
CHANGE_SUMMARY_MAX = 300
PREVIOUS_TEXT_CHARS = 600
BODY_VARIABLE_LIMIT = 20000
BODY_VARIABLE_HEAD = 16000
BODY_VARIABLE_TAIL = 4000
BODY_OMISSION = "……（中间省略）……"
FAQ_HEADING = "常见问题"


# =====================================================================
# JSON 与配置工具
# =====================================================================


def _loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_column(value: Any) -> str | None:
    """列表 / 对象 → JSON 列（空列表 / ``None`` → ``NULL``）；已是字符串时规范化后写回。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = _loads(value, None)
        if value is None:
            return None
    if isinstance(value, (list, tuple)) and not value:
        return None
    return _dumps(value)


def _canonical(raw: str | None) -> str:
    """``content_hash`` 的 JSON 段：按键排序、无空白；``NULL`` 视为空串。"""
    if not raw:
        return ""
    value = _loads(raw, None)
    if value is None:
        return raw
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _gen_cfg(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "generation_config")


def _cfg_path(cfg: Mapping[str, Any], path: str, default: Any) -> Any:
    value = settings_service.get_path(cfg, path)
    return default if value is None else value


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _status_conflict(status: str | None, message: str = MSG_STATUS_CONFLICT) -> BusinessError:
    return _conflict(message, {"current_status": status})


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


# =====================================================================
# HTML 清理（docs/09 §8.5）
# =====================================================================

ALLOWED_TAGS = frozenset({
    "h2", "h3", "h4", "p", "br", "ul", "ol", "li", "strong", "em", "b", "i", "a", "img", "blockquote", "pre", "code",
    "table", "thead", "tbody", "tr", "th", "td", "hr", "section",
})
DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "form", "noscript", "template", "textarea", "select", "head", "title"})
VOID_TAGS = frozenset({"br", "img", "hr"})
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:")
_DIMENSION_RE = re.compile(r"^\d{1,5}%?$")


def safe_url(value: str | None) -> str | None:
    """只允许 ``http`` / ``https`` 与相对路径；``javascript:`` / ``data:`` 等其它 scheme 返回 ``None``。"""
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    compact = re.sub(r"[\x00-\x20\x7f]", "", url).lower()
    if compact.startswith(("http://", "https://")):
        return url
    if _SCHEME_RE.match(compact):
        return None
    return url


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.skip:
            if tag in DROP_CONTENT_TAGS:
                self.skip.append(tag)
            return
        if tag in DROP_CONTENT_TAGS:
            self.skip.append(tag)
            return
        if tag not in ALLOWED_TAGS:
            return
        values = {k.lower(): (v or "") for k, v in attrs}
        rendered = ""
        if tag == "a":
            href = safe_url(values.get("href"))
            if href is not None:
                rendered = f' href="{html_lib.escape(href, quote=True)}" rel="noopener"'
        elif tag == "img":
            src = safe_url(values.get("src"))
            if src is None:
                return
            parts = [f' src="{html_lib.escape(src, quote=True)}"']
            if "alt" in values:
                parts.append(f' alt="{html_lib.escape(values["alt"], quote=True)}"')
            for dim in ("width", "height"):
                if _DIMENSION_RE.match(values.get(dim, "").strip()):
                    parts.append(f' {dim}="{values[dim].strip()}"')
            rendered = "".join(parts)
        self.out.append(f"<{tag}{rendered}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS and not self.skip and tag.lower() in ALLOWED_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS or tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(html_lib.escape(data, quote=False))

    def result(self) -> str:
        self.close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_html(body: str | None) -> str:
    """服务端 HTML 允许列表清理（每次写入 ``body`` 时执行，非法输入不报错）：只保留 ``h2 h3 h4 p br ul ol li strong em b i a
    img blockquote pre code table thead tbody tr th td hr section``；``a`` 只保留安全 ``href`` 并追加 ``rel="noopener"``，``img`` 只保留
    安全 ``src`` / ``alt`` / ``width`` / ``height``；``script`` / ``style`` / ``iframe`` / ``object`` / ``embed`` / ``form`` 连同内容移除；
    其它标签去掉标签保留文本；全部 ``on*`` / ``style`` 等属性移除。"""
    if not body:
        return ""
    parser = _Sanitizer()
    parser.feed(body)
    return parser.result().strip()


# =====================================================================
# 字数（docs/09 §8.9）
# =====================================================================

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ가-힯\U00020000-\U0002ebef]")
_WORD_CHAR_RE = re.compile(r"[^\W_]")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _markdown_plain(text: str) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            line = re.sub(r"^\s*(```|~~~)\S*", "", line)
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^(\s*>\s?)+", "", line)
        line = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line)
        lines.append(line)
    plain = "\n".join(lines)
    plain = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", plain)
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    return re.sub(r"[*_~`|]", " ", plain)


def _html_plain(text: str) -> str:
    plain = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    plain = re.sub(r"<[^>]+>", " ", plain)
    return html_lib.unescape(plain)


def count_words(body: str | None, format: str = "markdown") -> int:  # noqa: A002
    """Markdown 去除标记语法（标题符号、强调符号、链接保留文字、图片整体移除、代码围栏符号）或 HTML 去标签后：CJK 字符每个计 1，
    其它按空白切分的词（含字母或数字）每个计 1。"""
    if not body:
        return 0
    plain = _html_plain(body) if format == "html" else _markdown_plain(body.replace("\r\n", "\n"))
    cjk = len(_CJK_RE.findall(plain))
    rest = _CJK_RE.sub(" ", plain)
    return cjk + sum(1 for token in rest.split() if _WORD_CHAR_RE.search(token))


# =====================================================================
# 小节、标题与拼接（docs/09 §8.3、§8.7）
# =====================================================================

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HTML_HEADING_RE = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.IGNORECASE | re.DOTALL)


def _norm_heading(text: str | None) -> str:
    value = html_lib.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", value).strip().rstrip("#").strip().lower()


def _md_lines(text: str) -> list[tuple[int, str, bool]]:
    """``[(行首偏移, 行文本, 是否在代码围栏内)]``（围栏行本身记为 True）。"""
    result: list[tuple[int, str, bool]] = []
    offset = 0
    fenced = False
    for line in text.split("\n"):
        is_fence = bool(_FENCE_RE.match(line))
        result.append((offset, line, fenced or is_fence))
        if is_fence:
            fenced = not fenced
        offset += len(line) + 1
    return result


@dataclass
class Section:
    """正文中的一个 H2 / H3 小节：``[start, end)`` 为整节（含标题行），``heading_end`` 为标题行结束位置。"""

    level: int
    heading: str
    start: int
    heading_end: int
    end: int


def _headings(body: str, format: str) -> list[tuple[int, str, int, int]]:  # noqa: A002
    """全部标题 ``[(level, 文本, 起点, 标题结束)]``（Markdown 跳过代码围栏）。"""
    found: list[tuple[int, str, int, int]] = []
    if format == "html":
        for m in _HTML_HEADING_RE.finditer(body):
            found.append((int(m.group(1)), _norm_text(m.group(2)), m.start(), m.end()))
        return found
    for offset, line, fenced in _md_lines(body):
        if fenced:
            continue
        m = _MD_HEADING_RE.match(line)
        if m:
            found.append((len(m.group(1)), m.group(2).strip(), offset, offset + len(line)))
    return found


def _norm_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def split_sections(body: str | None, format: str = "markdown") -> list[Section]:  # noqa: A002
    """按 H2 / H3 切分正文：``level=2`` 小节为该 H2 至下一个 H2，``level=3`` 小节为该 H3 至下一个 H2 / H3（docs/09 §8.7）。"""
    text = body or ""
    heads = [h for h in _headings(text, format) if h[0] in (2, 3)]
    sections: list[Section] = []
    for index, (level, heading, start, heading_end) in enumerate(heads):
        end = len(text)
        for later in heads[index + 1:]:
            if later[0] == 2 or (level == 3 and later[0] == 3):
                end = later[2]
                break
        sections.append(Section(level=level, heading=heading, start=start, heading_end=heading_end, end=end))
    return sections


def locate_section(body: str | None, format: str, outline: Sequence[Mapping[str, Any]] | None, section_index: int) -> Section | None:  # noqa: A002
    """``outline`` 第 ``section_index`` 项（1 起）在正文中对应的小节（同级同名的第 k 次出现对应大纲中第 k 个同级同名项）；找不到为
    ``None``。"""
    items = list(outline or [])
    if section_index < 1 or section_index > len(items):
        return None
    item = items[section_index - 1]
    level = int(item.get("level") or 2)
    key = _norm_heading(str(item.get("heading") or ""))
    occurrence = sum(
        1 for prev in items[: section_index - 1]
        if int(prev.get("level") or 2) == level and _norm_heading(str(prev.get("heading") or "")) == key
    )
    candidates = [s for s in split_sections(body, format) if s.level == level and _norm_heading(s.heading) == key]
    return candidates[occurrence] if occurrence < len(candidates) else None


def _strip_fences(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if not re.match(r"^\s*(```|~~~)[\w+-]*\s*$", line))


def clean_output(text: str | None, format: str = "markdown") -> str:  # noqa: A002
    """模型正文输出的通用清理（§8.3 第 1 步）：去首尾空白、去 Markdown 代码围栏与一级标题行（``# …`` / ``<h1>``）；HTML 另经
    ``sanitize_html``。"""
    value = _strip_fences((text or "").replace("\r\n", "\n")).strip()
    if format == "html":
        value = re.sub(r"<h1\b[^>]*>.*?</h1\s*>", "", value, flags=re.IGNORECASE | re.DOTALL)
        return sanitize_html(value)
    lines = [line for line in value.split("\n") if not re.match(r"^#\s+", line)]
    return "\n".join(lines).strip()


def _ensure_heading(text: str, heading: str, format: str) -> str:  # noqa: A002
    """节首必须是该节标题：缺失则补上；标题文字与大纲不一致时以大纲为准替换（§8.3 第 2 步）。"""
    if format == "html":
        tag = f"<h2>{html_lib.escape(heading, quote=False)}</h2>"
        m = re.match(r"\s*<h2\b[^>]*>.*?</h2\s*>", text, re.IGNORECASE | re.DOTALL)
        if m:
            return tag + text[m.end():]
        return f"{tag}\n{text}" if text else tag
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.match(r"^##\s+", line) and not re.match(r"^###", line):
            lines[index] = f"## {heading}"
            return "\n".join(lines[index:]).strip()
        break
    return f"## {heading}\n\n{text}".strip()


def _cut_at_headings(text: str, later: set[str], format: str) -> str:  # noqa: A002
    """节内出现后续小节标题（模型越界续写）时从该行截断（§8.3 第 3 步；不检查节首标题本身）。"""
    if not later:
        return text
    heads = _headings(text, format)
    for _level, heading, start, _end in heads[1:]:
        if _norm_heading(heading) in later:
            return text[:start].rstrip()
    return text


def _level2(outline: Sequence[Mapping[str, Any]] | None) -> list[Mapping[str, Any]]:
    return [item for item in (outline or []) if int(item.get("level") or 2) == 2]


def assemble_sections(sections: Sequence[str], outline: Sequence[Mapping[str, Any]] | None, format: str = "markdown") -> str:  # noqa: A002
    """分段拼接（``content_service.assemble_sections``，§8.3）：``sections[i]`` 对应大纲第 i 个 ``level=2`` 项；每节清理围栏与 H1、
    补齐 / 校正节首标题、在后续小节标题处截断；Markdown 以两个换行连接，HTML 以换行连接并经 ``sanitize_html``。"""
    heads = _level2(outline)
    parts: list[str] = []
    for index, raw in enumerate(sections):
        heading = str(heads[index].get("heading") or "") if index < len(heads) else ""
        later = {_norm_heading(str(h.get("heading") or "")) for h in heads[index + 1:]}
        text = clean_output(raw, format)
        if heading:
            text = _ensure_heading(text, heading, format)
        text = _cut_at_headings(text, later, format).strip()
        if text:
            parts.append(text)
    if format == "html":
        return sanitize_html("\n".join(parts))
    return "\n\n".join(parts)


def count_h2(body: str | None, format: str = "markdown") -> int:  # noqa: A002
    return sum(1 for h in _headings(body or "", format) if h[0] == 2)


def assemble_whole(text: str | None, outline: Sequence[Mapping[str, Any]] | None, format: str = "markdown") -> str:  # noqa: A002
    """整篇生成的清理（§8.3 第 5 步）：同第 1 步与 HTML 清理；正文完全没有 H2 时在开头补第一个小节标题，其余不强行插入。"""
    body = clean_output(text, format)
    heads = _level2(outline)
    if heads and count_h2(body, format) == 0:
        heading = str(heads[0].get("heading") or "")
        if heading:
            body = _ensure_heading(body, heading, format)
            if format == "html":
                body = sanitize_html(body)
    return body.strip()


def _strip_leading_heading(text: str, format: str) -> str:  # noqa: A002
    if format == "html":
        return re.sub(r"^\s*<h[2-6]\b[^>]*>.*?</h[2-6]\s*>", "", text, count=1, flags=re.IGNORECASE | re.DOTALL).strip()
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if re.match(r"^#{2,6}\s+", line):
            return "\n".join(lines[index + 1:]).strip()
        break
    return text.strip()


def replace_section(body: str, format: str, section: Section, new_text: str) -> str:  # noqa: A002
    """替换小节（保留原标题行）后重新拼接：改写结果去掉自带的节首标题，并在越界的同级（或更高级）标题处截断。"""
    content = _strip_leading_heading(clean_output(new_text, format), format)
    limit_levels = (2,) if section.level == 2 else (2, 3)
    for level, _heading, start, _end in _headings(content, format):
        if level in limit_levels:
            content = content[:start].rstrip()
            break
    heading = body[section.start:section.heading_end].rstrip()
    before = body[: section.start]
    after = body[section.end:].lstrip("\n")
    if format == "html":
        merged = f"{before}{heading}\n{content}\n{after}"
        return sanitize_html(merged)
    piece = f"{heading}\n\n{content}".rstrip() if content else heading
    return f"{before}{piece}" + (f"\n\n{after}" if after.strip() else "\n")


# =====================================================================
# 大纲（docs/09 §8.2）
# =====================================================================


def _max_sections(params: Mapping[str, Any] | None, db: Session | None = None) -> int:
    value = (params or {}).get("sections")
    if value:
        return int(value)
    if db is not None:
        return int(_cfg_path(_gen_cfg(db), "content.max_sections", 8))
    return 8


def _truncate_outline(items: list[dict[str, Any]], max_sections: int) -> list[dict[str, Any]]:
    """只保留前 ``max_sections`` 个 ``level=2`` 项及其 ``level=3`` 子节。"""
    result: list[dict[str, Any]] = []
    count = 0
    for item in items:
        if item["level"] == 2:
            count += 1
            if count > max_sections:
                break
        result.append(item)
    return result


def validate_outline(
    items: Any, *, max_sections: int, strict: bool = True, loc: Sequence[str | int] = ("body", "outline"),
) -> list[dict[str, Any]]:
    """大纲校验与规范化（``content_service.validate_outline``）：数组 1~40 项；``heading`` 1~120 字符；``level ∈ {2,3}`` 且首项为 2；
    ``points`` 0~8 项、每项 ≤ 200 字符；``level=2`` 项数超过 ``max_sections`` 时截断。

    ``strict=True``（人工编辑）：不合规 → 400 校验错误列表；``strict=False``（模型输出）：字段级容错——标题截断、非法 ``level`` 归为
    2、要点截断 / 丢弃非字符串，无可用项时抛 ``ZhiqiError(INVALID_RESPONSE)``。"""
    base = list(loc)
    if not isinstance(items, list):
        if strict:
            raise invalid_params(field_error(base, "大纲必须为数组", "list_type", items))
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：大纲应为数组")
    errors: list[dict[str, Any]] = []
    result: list[dict[str, Any]] = []
    if strict and not 1 <= len(items) <= OUTLINE_MAX_ITEMS:
        errors.append(field_error(base, f"大纲须为 1~{OUTLINE_MAX_ITEMS} 项", "too_long" if items else "too_short", len(items)))
    for index, raw in enumerate(items[:OUTLINE_MAX_ITEMS] if not strict else items):
        if not isinstance(raw, Mapping):
            if strict:
                errors.append(field_error([*base, index], "大纲项必须为对象", "dict_type", raw))
            continue
        heading = raw.get("heading")
        heading = re.sub(r"\s+", " ", heading).strip() if isinstance(heading, str) else ""
        try:
            level = int(raw.get("level") if raw.get("level") is not None else 2)
        except (TypeError, ValueError):
            level = 0
        points_raw = raw.get("points") if isinstance(raw.get("points"), list) else []
        if strict:
            if not heading or len(heading) > OUTLINE_HEADING_MAX:
                errors.append(field_error([*base, index, "heading"], f"标题须为 1~{OUTLINE_HEADING_MAX} 个字符", "string_too_long" if heading else "string_too_short", raw.get("heading")))
            if level not in (2, 3):
                errors.append(field_error([*base, index, "level"], "level 只能为 2 或 3", "literal_error", raw.get("level")))
            elif not result and level != 2:
                errors.append(field_error([*base, index, "level"], "大纲首项必须为 level=2 的主小节", "value_error", raw.get("level")))
            if len(points_raw) > OUTLINE_POINTS_MAX:
                errors.append(field_error([*base, index, "points"], f"要点不能超过 {OUTLINE_POINTS_MAX} 项", "too_long", len(points_raw)))
            points = []
            for p_index, point in enumerate(points_raw):
                text = point.strip() if isinstance(point, str) else ""
                if isinstance(point, str) and len(text) > OUTLINE_POINT_MAX:
                    errors.append(field_error([*base, index, "points", p_index], f"要点不能超过 {OUTLINE_POINT_MAX} 个字符", "string_too_long", point))
                if text:
                    points.append(text[:OUTLINE_POINT_MAX])
        else:
            if not heading:
                continue
            heading = heading[:OUTLINE_HEADING_MAX]
            if level not in (2, 3) or not result:
                level = 2
            points = []
            for point in points_raw:
                if isinstance(point, (str, int, float)) and not isinstance(point, bool):
                    text = str(point).strip()
                    if text:
                        points.append(text[:OUTLINE_POINT_MAX])
                if len(points) >= OUTLINE_POINTS_MAX:
                    break
        result.append({"heading": heading, "level": level, "points": points[:OUTLINE_POINTS_MAX]})
    if errors:
        raise invalid_params(errors)
    if not result:
        if strict:
            raise invalid_params(field_error(base, f"大纲须为 1~{OUTLINE_MAX_ITEMS} 项", "too_short", items))
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：大纲为空")
    return _truncate_outline(result, max(1, int(max_sections)))


def render_outline(outline: Sequence[Mapping[str, Any]] | None) -> str:
    """模板变量 ``outline``：``## heading`` / ``### heading`` + ``- point`` 的 Markdown 列表；无大纲为空串（渲染为 ``（无大纲）``）。"""
    lines: list[str] = []
    for item in outline or []:
        prefix = "##" if int(item.get("level") or 2) == 2 else "###"
        lines.append(f"{prefix} {item.get('heading')}")
        lines.extend(f"- {p}" for p in item.get("points") or [])
    return "\n".join(lines)


def render_section(outline: Sequence[Mapping[str, Any]], position: int) -> str:
    """模板变量 ``section``：大纲第 ``position`` 项（0 起，``level=2``）及其后续 ``level=3`` 子节（直到下一个 ``level=2``）。"""
    items = list(outline)
    block = [items[position]]
    for item in items[position + 1:]:
        if int(item.get("level") or 2) == 2:
            break
        block.append(item)
    return render_outline(block)


# =====================================================================
# 生成参数与质量规则（docs/09 §8.9）
# =====================================================================


def _stored_params(content: Content) -> dict[str, Any]:
    raw = _loads(content.generation_params_json, {})
    return raw if isinstance(raw, dict) else {}


def generation_params(db: Session, content: Content) -> dict[str, Any]:
    """生成参数：``contents.generation_params_json`` 优先，缺失项取 ``generation_config.content`` 默认（``sections`` 对应
    ``content.max_sections``）。"""
    cfg = _gen_cfg(db)
    stored = _stored_params(content)

    def pick(key: str, path: str, default: Any) -> Any:
        value = stored.get(key)
        return _cfg_path(cfg, path, default) if value is None else value

    return {
        "outline_first": bool(pick("outline_first", "content.outline_first", True)),
        "segmented": bool(pick("segmented", "content.segmented", True)),
        "target_word_count": int(pick("target_word_count", "content.target_word_count", 1500)),
        "include_faq": bool(pick("include_faq", "content.include_faq", True)),
        "faq_count": int(pick("faq_count", "content.faq_count", 3)),
        "include_seo_meta": bool(pick("include_seo_meta", "content.include_seo_meta", True)),
        "format": content.format,
        "sections": int(pick("sections", "content.max_sections", 8)),
    }


def evaluate_quality(
    content: Content,
    quality: Mapping[str, Any],
    *,
    content_config: Mapping[str, Any] | None = None,
    duplicate_title: bool = False,
    truncated: bool = False,
) -> tuple[int, list[str]]:
    """质量规则（``content_service.evaluate_quality(content, config.quality) -> (quality_score, risk_flags)``）：九个标记与扣分见
    ``QUALITY_DEDUCTIONS``，``quality_score = max(0, 100 − Σ 扣分)``；``too_short`` 阈值 ``max(min_word_count, target_word_count ×
    min_word_count_ratio)``（无生成参数时只看 ``min_word_count``）。``duplicate_title`` / ``truncated`` 由调用方按库计算后传入。"""
    content_config = content_config or {}
    params = _stored_params(content)
    fmt = content.format or "markdown"
    words = int(content.word_count or 0)
    min_words = int(content_config.get("min_word_count") or 0)
    max_words = int(content_config.get("max_word_count") or 0)
    threshold: float = min_words
    if params.get("target_word_count"):
        ratio = float(quality.get("min_word_count_ratio") or 0)
        threshold = max(min_words, int(params["target_word_count"]) * ratio)
    h2 = count_h2(content.body, fmt)
    flags: list[str] = []
    if words < threshold:
        flags.append("too_short")
    if max_words and words > max_words:
        flags.append("too_long")
    if quality.get("require_h2", True) and h2 == 0:
        flags.append("missing_h2")
    if quality.get("max_h2") and h2 > int(quality["max_h2"]):
        flags.append("too_many_h2")
    haystack = f"{content.title or ''}\n{content.body or ''}".lower()
    banned = [str(w).strip().lower() for w in (quality.get("banned_words") or []) if str(w).strip()]
    if any(word in haystack for word in banned):
        flags.append("banned_word")
    if duplicate_title and quality.get("flag_duplicate_title", True):
        flags.append("duplicate_title")
    if params.get("include_seo_meta") and (not content.seo_title or not content.seo_description):
        flags.append("missing_seo_meta")
    if params.get("include_faq") and int(params.get("faq_count") or 0) > 0 and not _loads(content.faq_json, None):
        flags.append("missing_faq")
    if truncated:
        flags.append("truncated")
    score = max(0, 100 - sum(QUALITY_DEDUCTIONS[f] for f in flags))
    return score, flags


def _has_duplicate_title(db: Session, content: Content) -> bool:
    key = title_dedup_key(content.title)
    if not key:
        return False
    others = db.scalars(select(Content.title).where(Content.project_id == content.project_id, Content.id != content.id)).all()
    return any(title_dedup_key(t) == key for t in others)


def body_truncated(db: Session, content: Content) -> bool:
    """最近一次成功的正文生成根任务（``content_generate`` / ``content_body`` / ``content_rewrite``）中产出正文的尝试行是否
    ``response_meta_json.finish_reason=length``（分段生成看全部分段尝试行；整篇看版本记录的尝试行）。"""
    root = db.scalar(
        select(AiTask).where(
            AiTask.target_type == "content", AiTask.target_id == content.id, AiTask.root_task_id.is_(None),
            AiTask.operation.in_(BODY_OPERATIONS), AiTask.status == "succeeded",
        ).order_by(AiTask.id.desc()).limit(1)
    )
    if root is None:
        return False
    attempts = list(db.scalars(select(AiTask).where(AiTask.root_task_id == root.id, AiTask.status == "succeeded")).all())
    segments = [a for a in attempts if a.segment_index is not None]
    if segments:
        body_attempts = segments
    else:
        versioned = set(
            db.scalars(
                select(ContentVersion.ai_task_id).where(
                    ContentVersion.content_id == content.id, ContentVersion.ai_task_id.in_([a.id for a in attempts] or [0])
                )
            ).all()
        )
        body_attempts = [a for a in attempts if a.id in versioned or root.operation != "content_generate"]
    return any(gateway.task_meta(a).get("finish_reason") == "length" for a in body_attempts)


def refresh_quality(db: Session, content: Content, *, truncated: bool | None = None) -> tuple[int, list[str]]:
    """按当前配置执行质量规则并写 ``contents.quality_score`` / ``risk_flags_json``（版本写入后调用）。"""
    cfg = _gen_cfg(db)
    quality = cfg.get("quality") or {}
    dup = bool(quality.get("flag_duplicate_title", True)) and _has_duplicate_title(db, content)
    if truncated is None:
        truncated = body_truncated(db, content)
    score, flags = evaluate_quality(content, quality, content_config=cfg.get("content") or {}, duplicate_title=dup, truncated=truncated)
    content.quality_score = score
    content.risk_flags_json = _dumps(flags)
    return score, flags


def _risk_flags(content: Content) -> list[str]:
    value = _loads(content.risk_flags_json, [])
    return [str(v) for v in value] if isinstance(value, list) else []


# =====================================================================
# 状态机（docs/09 §8.10；docs/03「内容状态流转」）
# =====================================================================

# action → 允许的起始状态
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "generate": GENERATE_START,                                   # → generating（写 prev_status）
    "generate_succeeded": ("generating",),                        # → ready（清空 prev_status）
    "restore_prev": ("generating",),                              # → prev_status（失败 / 取消）
    "save": EDITABLE_STATES,                                      # PUT：draft（有正文）/ rejected → ready，其它不变
    "submit_review": ("ready",),                                  # → reviewing / approved（review_required=false）
    "approve": ("reviewing",),
    "reject": ("reviewing",),
    "archive": ("draft", "ready", "rejected", "approved", "published"),
    "unarchive": ("archived",),
    "publish": ("approved", "published"),                         # 首条回填链接（docs/11）；已 published 不变
    "unpublish": ("published",),                                  # 链接数归零
}


def transition(
    content: Content,
    action: str,
    actor: int | None = None,
    *,
    db: Session | None = None,
    note: str | None = None,
    review_required: bool = True,
    has_body: bool | None = None,
) -> str:
    """内容状态流转的唯一入口（只改对象、不提交）。非法流转 → 409 ``data={"current_status": 当前状态}``。

    - ``generate``：``draft`` / ``ready`` / ``rejected → generating``，写 ``prev_status``；``generate_succeeded``：``→ ready`` 并清空；
      ``restore_prev``：恢复 ``prev_status``（缺省 ``draft``）并清空；
    - ``save``（``PUT``）：``draft``（有正文）/ ``rejected → ready``，``ready`` / ``approved`` / ``published`` 不变；
    - ``submit_review``：``ready → reviewing``；``review_required=False`` 时直接 ``approved`` 并写 ``review_result='approved'``、
      ``reviewed_by``=提交人、``reviewed_at``、``review_note='auto'``；``approve`` / ``reject``：``reviewing → approved / rejected``，
      写审核字段（``reject`` 的 ``note`` 必填，由接口校验）；
    - ``archive``：写 ``prev_status``；``unarchive``：恢复 ``prev_status``（``published`` 且 ``link_count=0`` → ``approved``）；
    - ``publish`` / ``unpublish``：回填链接驱动（docs/11）。

    传入 ``db`` 时，进入 ``approved`` 的审核动作在提交后累加 ``stats:rt.contents_approved``。返回新状态。"""
    allowed = TRANSITIONS.get(action)
    if allowed is None:
        raise ValueError(f"未知的内容流转动作：{action}")
    current = content.status
    if current not in allowed:
        raise _status_conflict(current)
    now = utcnow()
    target = current
    if action == "generate":
        content.prev_status = current
        target = "generating"
    elif action == "generate_succeeded":
        content.prev_status = None
        target = "ready"
    elif action == "restore_prev":
        target = content.prev_status if content.prev_status in GENERATE_START else "draft"
        content.prev_status = None
    elif action == "save":
        body_present = bool((content.body or "").strip()) if has_body is None else has_body
        if current == "rejected" or (current == "draft" and body_present):
            target = "ready"
    elif action == "submit_review":
        if review_required:
            target = "reviewing"
        else:
            target = "approved"
            content.review_result = "approved"
            content.reviewed_by = actor
            content.reviewed_at = now
            content.review_note = "auto"
    elif action in ("approve", "reject"):
        target = "approved" if action == "approve" else "rejected"
        content.review_result = target
        content.reviewed_by = actor
        content.reviewed_at = now
        content.review_note = (note or "").strip()[:500] or None
    elif action == "archive":
        content.prev_status = current
        target = "archived"
    elif action == "unarchive":
        prev = content.prev_status if content.prev_status in TRANSITIONS["archive"] else "draft"
        if prev == "published" and int(content.link_count or 0) <= 0:
            prev = "approved"
        target = prev
        content.prev_status = None
    elif action == "publish":
        target = "published"
    elif action == "unpublish":
        target = "approved"
    content.status = target
    if actor is not None:
        content.updated_by = actor
    if db is not None and target == "approved" and action in ("approve", "submit_review"):
        stats_service.increment_realtime_after_commit(db, content.project_id, {"contents_approved": 1}, at=now)
    return target


# =====================================================================
# 版本（docs/09 §8.8；docs/03 B.13、「内容版本保留」）
# =====================================================================


def _snapshot(content: Content) -> dict[str, Any]:
    return {field: getattr(content, field) for field in CONTENT_VERSIONED_FIELDS}


def _normalize_snapshot(snapshot: Mapping[str, Any], format: str, fallback_title: str) -> dict[str, Any]:  # noqa: A002
    body = snapshot.get("body") or ""
    if format == "html" and body:
        body = sanitize_html(body)

    def short(value: Any, limit: int) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text[:limit] or None

    title = (str(snapshot.get("title") or "").strip() or fallback_title or "")[:TITLE_MAX]
    return {
        "title": title,
        "body": body,
        "outline_json": _json_column(snapshot.get("outline_json")),
        "summary": short(snapshot.get("summary"), SUMMARY_MAX),
        "seo_title": short(snapshot.get("seo_title"), SEO_TITLE_MAX),
        "seo_description": short(snapshot.get("seo_description"), SEO_DESCRIPTION_MAX),
        "seo_keywords_json": _json_column(snapshot.get("seo_keywords_json")),
        "faq_json": _json_column(snapshot.get("faq_json")),
    }


def content_hash(snapshot: Mapping[str, Any]) -> str:
    """``SHA-256(title \\n body \\n outline_json \\n summary \\n seo_title \\n seo_description \\n seo_keywords_json \\n faq_json)``；
    JSON 列取「按键排序、无空白」的规范化串，``NULL`` 视为空串（docs/03 B.13）。"""
    parts = []
    for field in CONTENT_VERSIONED_FIELDS:
        value = snapshot.get(field)
        if field.endswith("_json"):
            parts.append(_canonical(value))
        else:
            parts.append("" if value is None else str(value))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _prune_versions(db: Session, content: Content, max_versions: int) -> list[int]:
    """达到上限时物理删除最旧的、非当前、``source != manual`` 的版本（全部为 ``manual`` / 当前版本时删最旧的非当前版本），直到
    低于上限；返回被删的 ``version_no``（只读取元信息，不加载正文）。"""
    rows = [
        (row.id, row.version_no, row.source)
        for row in db.execute(
            select(ContentVersion.id, ContentVersion.version_no, ContentVersion.source)
            .where(ContentVersion.content_id == content.id).order_by(ContentVersion.version_no)
        ).all()
    ]
    pruned: list[int] = []
    victims: list[int] = []
    while len(rows) >= max(1, max_versions):
        candidates = [r for r in rows if r[0] != content.current_version_id]
        if not candidates:
            break
        victim = next((r for r in candidates if r[2] != "manual"), candidates[0])
        rows.remove(victim)
        victims.append(victim[0])
        pruned.append(victim[1])
    if victims:
        db.execute(delete(ContentVersion).where(ContentVersion.id.in_(victims)).execution_options(synchronize_session="fetch"))
        db.flush()
    return pruned


def _summary_with_prune(summary: str | None, pruned: Sequence[int]) -> str | None:
    base = (summary or "").strip()
    if not pruned:
        return base[:CHANGE_SUMMARY_MAX] or None
    suffix = "auto_pruned=" + ",".join(str(n) for n in pruned)
    room = CHANGE_SUMMARY_MAX - len(suffix) - 1
    head = base[: max(0, room)].rstrip()
    return f"{head} {suffix}".strip()[:CHANGE_SUMMARY_MAX]


def write_version(
    db: Session,
    content: Content,
    changes: Mapping[str, Any],
    *,
    source: str,
    actor_id: int | None,
    ai_task_id: int | None = None,
    template_id: int | None = None,
    model: str | None = None,
    change_summary: str | None = None,
    restored_from_version_id: int | None = None,
    truncated: bool | None = None,
) -> tuple[ContentVersion | None, bool]:
    """版本化字段变更（只 ``flush``，与调用方同一事务提交）。``changes`` 的键为 ``contents`` 列名（``outline_json`` 等 JSON 列可传
    列表 / 对象），未给出的字段沿用当前值。新快照 ``content_hash`` 与当前版本相同 → 不建版本，返回 ``(当前版本, False)``；否则
    （必要时先自动裁剪）``INSERT content_versions(version_no = version_count + 1)``，同步 ``contents`` 的 8 列与
    ``current_version_id`` / ``version_count`` / ``word_count``，执行质量规则，返回 ``(新版本, True)``。"""
    snapshot = _snapshot(content)
    for key, value in changes.items():
        if key in CONTENT_VERSIONED_FIELDS:
            snapshot[key] = value
    snapshot = _normalize_snapshot(snapshot, content.format or "markdown", content.title)
    digest = content_hash(snapshot)
    current = db.get(ContentVersion, content.current_version_id) if content.current_version_id else None
    if current is not None and current.content_hash == digest:
        return current, False
    if current is None and digest == content_hash(_normalize_snapshot(_snapshot(content), content.format or "markdown", content.title)):
        return None, False      # 尚无版本且没有任何字段变化（如对无正文草稿的空 PUT）
    cfg = _gen_cfg(db)
    pruned = _prune_versions(db, content, int(_cfg_path(cfg, "rewrite.max_versions", 50)))
    words = count_words(snapshot["body"], content.format or "markdown")
    creator = actor_id if actor_id is not None else content.created_by
    version = ContentVersion(
        content_id=content.id,
        version_no=int(content.version_count or 0) + 1,
        source=source,
        title=snapshot["title"],
        body=snapshot["body"] or "",
        content_hash=digest,
        outline_json=snapshot["outline_json"],
        summary=snapshot["summary"],
        seo_title=snapshot["seo_title"],
        seo_description=snapshot["seo_description"],
        seo_keywords_json=snapshot["seo_keywords_json"],
        faq_json=snapshot["faq_json"],
        word_count=words,
        ai_task_id=ai_task_id,
        template_id=template_id,
        model=(model or None) and str(model)[:120],
        change_summary=_summary_with_prune(change_summary, pruned),
        restored_from_version_id=restored_from_version_id,
        created_by=int(creator or 0),
    )
    db.add(version)
    db.flush()
    for field in CONTENT_VERSIONED_FIELDS:
        value = snapshot[field]
        setattr(content, field, (value or None) if field == "body" else value)
    content.current_version_id = version.id
    content.version_count = version.version_no
    content.word_count = words
    if actor_id is not None:
        content.updated_by = actor_id
    refresh_quality(db, content, truncated=truncated)
    db.flush()
    return version, True


def version_item(version: ContentVersion, *, with_body: bool = False) -> dict[str, Any]:
    """版本列表项（docs/04 §7.6）：``{id, version_no, source, title, word_count, model, change_summary, restored_from_version_id,
    created_by, created_at}``（``with_body`` 时含 ``body``）。"""
    item = {
        "id": version.id,
        "version_no": version.version_no,
        "source": version.source,
        "title": version.title,
        "word_count": version.word_count,
        "model": version.model,
        "ai_task_id": version.ai_task_id,
        "change_summary": version.change_summary,
        "restored_from_version_id": version.restored_from_version_id,
        "created_by": version.created_by,
        "created_at": iso_utc(version.created_at),
    }
    if with_body:
        item["body"] = version.body
    return item


def version_detail(version: ContentVersion) -> dict[str, Any]:
    """版本详情：全部版本化字段（JSON 列解码）与元信息。"""
    return {
        **version_item(version, with_body=True),
        "content_id": version.content_id,
        "content_hash": version.content_hash,
        "outline": _loads(version.outline_json, None),
        "summary": version.summary,
        "seo_title": version.seo_title,
        "seo_description": version.seo_description,
        "seo_keywords": _loads(version.seo_keywords_json, None),
        "faq": _loads(version.faq_json, None),
        "template_id": version.template_id,
        "updated_at": iso_utc(version.updated_at),
    }


# =====================================================================
# 序列化
# =====================================================================


def content_item(content: Content, *, with_body: bool = True) -> dict[str, Any]:
    """内容对象（docs/04 §7.6）；列表不含 ``body``。"""
    item: dict[str, Any] = {
        "id": content.id,
        "project_id": content.project_id,
        "title_id": content.title_id,
        "keyword_id": content.keyword_id,
        "title": content.title,
        "format": content.format,
        "language": content.language,
        "style": content.style,
        "status": content.status,
        "prev_status": content.prev_status,
        "review_result": content.review_result,
        "current_version_id": content.current_version_id,
        "version_count": content.version_count,
        "outline": _loads(content.outline_json, None),
        "summary": content.summary,
        "seo_title": content.seo_title,
        "seo_description": content.seo_description,
        "seo_keywords": _loads(content.seo_keywords_json, None),
        "faq": _loads(content.faq_json, None),
        "word_count": content.word_count,
        "cover_asset_id": content.cover_asset_id,
        "template_id": content.template_id,
        "generation_params": _loads(content.generation_params_json, None),
        "batch_id": content.batch_id,
        "ai_task_id": content.ai_task_id,
        "quality_score": content.quality_score,
        "risk_flags": _risk_flags(content),
        "reviewed_by": content.reviewed_by,
        "reviewed_at": iso_utc(content.reviewed_at),
        "review_note": content.review_note,
        "link_count": content.link_count,
        "first_published_at": iso_utc(content.first_published_at),
        "created_by": content.created_by,
        "updated_by": content.updated_by,
        "created_at": iso_utc(content.created_at),
        "updated_at": iso_utc(content.updated_at),
    }
    if with_body:
        item["body"] = content.body
    return item


def asset_item(db: Session, scope: DataScope, asset: MediaAsset) -> dict[str, Any]:
    """素材对象（docs/04 §7.8）；``reference_asset_ids`` 只列出调用者可见的素材（docs/13 §6.1）。"""
    refs = _loads(asset.reference_asset_ids_json, [])
    ref_ids = [int(r) for r in refs if isinstance(r, int) and not isinstance(r, bool)] if isinstance(refs, list) else []
    if ref_ids and scope.restricted:
        visible = set(db.scalars(scope_media(select(MediaAsset.id).where(MediaAsset.id.in_(ref_ids)), scope)).all())
        ref_ids = [r for r in ref_ids if r in visible]
    params = _loads(asset.params_json, {})
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "content_id": asset.content_id,
        "kind": asset.kind,
        "usage_type": asset.usage_type,
        "source": asset.source,
        "status": asset.status,
        "ai_task_id": asset.ai_task_id,
        "prompt": asset.prompt,
        "negative_prompt": asset.negative_prompt,
        "model": asset.model,
        "params": params if isinstance(params, dict) else {},
        "reference_asset_ids": ref_ids,
        "upstream_task_id": asset.upstream_task_id,
        "upstream_url": asset.upstream_url,
        "storage_key": asset.storage_key,
        "url": asset.url,
        "thumbnail_key": asset.thumbnail_key,
        "thumbnail_url": asset.thumbnail_url,
        "mime_type": asset.mime_type,
        "size_bytes": asset.size_bytes,
        "width": asset.width,
        "height": asset.height,
        "duration_seconds": asset.duration_seconds,
        "file_hash": asset.file_hash,
        "progress": asset.progress,
        "error_category": asset.error_category,
        "error_message": asset.error_message,
        "transfer_attempts": asset.transfer_attempts,
        "next_transfer_at": iso_utc(asset.next_transfer_at),
        "ready_at": iso_utc(asset.ready_at),
        "failed_at": iso_utc(asset.failed_at),
        "sort": asset.sort,
        "created_by": asset.created_by,
        "created_at": iso_utc(asset.created_at),
        "updated_at": iso_utc(asset.updated_at),
    }


def link_item(link: PublishLink, platform: PublishPlatform | None = None) -> dict[str, Any]:
    """回填链接对象（docs/04 §7.10；``baseline_simhash`` 为有符号 64 位整数，按字符串返回）。"""
    seo = _loads(link.seo_status_json, {})
    geo = _loads(link.geo_status_json, {})
    return {
        "id": link.id,
        "project_id": link.project_id,
        "content_id": link.content_id,
        "platform_id": link.platform_id,
        "url": link.url,
        "normalized_url": link.normalized_url,
        "url_hash": link.url_hash,
        "domain": link.domain,
        "publish_account": link.publish_account,
        "published_at": iso_utc(link.published_at),
        "backfilled_by": link.backfilled_by,
        "title_snapshot": link.title_snapshot,
        "alive_status": link.alive_status,
        "alive_changed_at": iso_utc(link.alive_changed_at),
        "last_checked_at": iso_utc(link.last_checked_at),
        "next_check_at": iso_utc(link.next_check_at),
        "check_count": link.check_count,
        "consecutive_unknown": link.consecutive_unknown,
        "consecutive_suspected": link.consecutive_suspected,
        "last_http_status": link.last_http_status,
        "baseline_title": link.baseline_title,
        "baseline_simhash": str(link.baseline_simhash) if link.baseline_simhash is not None else None,
        "baseline_excerpt": link.baseline_excerpt,
        "baseline_captured_at": iso_utc(link.baseline_captured_at),
        "seo_status": seo if isinstance(seo, dict) else {},
        "geo_status": geo if isinstance(geo, dict) else {},
        "seo_indexed_any": bool(link.seo_indexed_any),
        "geo_cited_any": bool(link.geo_cited_any),
        "first_indexed_at": iso_utc(link.first_indexed_at),
        "first_cited_at": iso_utc(link.first_cited_at),
        "last_index_checked_at": iso_utc(link.last_index_checked_at),
        "next_index_check_at": iso_utc(link.next_index_check_at),
        "index_check_count": link.index_check_count,
        "index_checks_done": link.index_checks_done,
        "is_monitoring": bool(link.is_monitoring),
        "note": link.note,
        "created_at": iso_utc(link.created_at),
        "updated_at": iso_utc(link.updated_at),
        "platform": (
            {"id": platform.id, "code": platform.code, "name": platform.name, "name_en": platform.name_en}
            if platform is not None else None
        ),
    }


def task_summary(root: AiTask) -> dict[str, Any]:
    """内容任务摘要（``GET /admin/contents/{id}/task``、详情 ``pending_tasks[]``）：``{task_id, operation, status, progress,
    error_category, error_message, model_override, finished_at}``；``model_override`` 取根任务 ``input.model``。"""
    override = gateway.task_input(root).get("model")
    return {
        "task_id": root.id,
        "operation": root.operation,
        "status": root.status,
        "progress": root.progress,
        "error_category": root.error_category,
        "error_message": root.error_message,
        "model_override": str(override) if override else None,
        "finished_at": iso_utc(root.finished_at),
    }


# =====================================================================
# 查询
# =====================================================================


def get_content_row(db: Session, scope: DataScope, content_id: int) -> Content:
    """可见内容（按所属项目判断，不可见与不存在一律 404）。"""
    return get_visible(db, scope, Content, content_id, message=CONTENT_NOT_FOUND)


def _content_roots(db: Session, content_ids: Sequence[int], operations: Sequence[str], *, active: bool) -> list[AiTask]:
    if not content_ids:
        return []
    stmt = select(AiTask).where(
        AiTask.target_type == "content", AiTask.target_id.in_(list(content_ids)), AiTask.root_task_id.is_(None),
        AiTask.operation.in_(list(operations)),
    )
    if active:
        stmt = stmt.where(AiTask.status.in_(ACTIVE_ROOT_STATUSES))
    return list(db.scalars(stmt.order_by(AiTask.id)).all())


def _active_task_ids(db: Session, content_ids: Sequence[int]) -> dict[int, int]:
    result: dict[int, int] = {}
    for root in _content_roots(db, content_ids, ACTIVE_TASK_OPERATIONS, active=True):
        result[int(root.target_id or 0)] = root.id
    return result


def _active_same_operation(db: Session, content_id: int, operation: str, *, exclude: int | None = None) -> int | None:
    for root in _content_roots(db, [content_id], [operation], active=True):
        if root.id != exclude:
            return root.id
    return None


def list_contents(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    project_id: int | None = None,
    status: str | None = None,
    keyword_id: int | None = None,
    title_id: int | None = None,
    batch_id: int | None = None,
    keyword: str | None = None,
    created_by: int | None = None,
    has_links: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """分页（``created_at DESC, id DESC``，不含 ``body``，附 ``active_task_id``）；按 ``project_id IN P`` 过滤，筛选引用不可见对象时为
    空结果。``keyword`` 为标题搜索。"""
    conditions: list[Any] = []
    for column, value in (
        (Content.project_id, project_id), (Content.status, status), (Content.keyword_id, keyword_id),
        (Content.title_id, title_id), (Content.batch_id, batch_id), (Content.created_by, created_by),
    ):
        if value is not None and value != "":
            conditions.append(column == value)
    text = (keyword or "").strip()
    if text:
        conditions.append(Content.title.contains(text, autoescape=True))
    if has_links is not None:
        conditions.append(Content.link_count > 0 if has_links else Content.link_count == 0)
    stmt = scope_by_project(select(Content), Content.project_id, scope).where(*conditions)
    total = int(db.scalar(scope_by_project(select(func.count(Content.id)), Content.project_id, scope).where(*conditions)) or 0)
    rows = list(db.scalars(stmt.order_by(Content.created_at.desc(), Content.id.desc()).offset((page - 1) * page_size).limit(page_size)).all())
    active = _active_task_ids(db, [r.id for r in rows])
    items = []
    for row in rows:
        item = content_item(row, with_body=False)
        item["active_task_id"] = active.get(row.id)
        items.append(item)
    return items, total


def _assets_of(db: Session, content_id: int) -> list[MediaAsset]:
    return list(
        db.scalars(
            select(MediaAsset).where(MediaAsset.content_id == content_id, MediaAsset.status != "deleted")
            .order_by(MediaAsset.sort, MediaAsset.id)
        ).all()
    )


def _detail(db: Session, scope: DataScope, content: Content) -> dict[str, Any]:
    item = content_item(content)
    current = db.get(ContentVersion, content.current_version_id) if content.current_version_id else None
    item["current_version"] = (
        {
            "id": current.id, "version_no": current.version_no, "source": current.source, "model": current.model,
            "ai_task_id": current.ai_task_id, "change_summary": current.change_summary, "created_by": current.created_by,
            "created_at": iso_utc(current.created_at),
        }
        if current is not None else None
    )
    item["assets"] = [asset_item(db, scope, a) for a in _assets_of(db, content.id)]
    item["active_task_id"] = _active_task_ids(db, [content.id]).get(content.id)
    item["pending_tasks"] = [task_summary(r) for r in _content_roots(db, [content.id], PENDING_TASK_OPERATIONS, active=True)]
    return item


def content_detail(db: Session, scope: DataScope, content_id: int) -> dict[str, Any]:
    """``GET /admin/contents/{id}``：当前版本摘要、``outline``、SEO 要素、``assets[]``、``link_count``、``active_task_id``
    （``content_generate`` / ``content_body`` / ``content_rewrite`` 的非终态根任务）、``pending_tasks[]``（非终态的
    ``content_outline`` / ``content_seo`` 根任务摘要）。"""
    return _detail(db, scope, get_content_row(db, scope, content_id))


def content_task(db: Session, scope: DataScope, content_id: int) -> dict[str, Any] | None:
    """``GET /admin/contents/{id}/task``：``active_task_id`` 对应根任务摘要；无进行中任务返回最近一个 ``content_*`` 根任务；从未
    生成过为 ``None``。"""
    content = get_content_row(db, scope, content_id)
    active = _active_task_ids(db, [content.id]).get(content.id)
    root = db.get(AiTask, active) if active else None
    if root is None:
        root = db.scalar(
            select(AiTask).where(
                AiTask.target_type == "content", AiTask.target_id == content.id, AiTask.root_task_id.is_(None),
                AiTask.operation.in_(CONTENT_OPERATIONS),
            ).order_by(AiTask.id.desc()).limit(1)
        )
    return task_summary(root) if root is not None else None


# =====================================================================
# 冗余计数（docs/03「冗余计数回写」）
# =====================================================================


def _bump_content_count(db: Session, model: type[Keyword | Title], row_id: int | None, delta: int) -> None:
    if not row_id or delta == 0:
        return
    db.execute(
        update(model).where(model.id == row_id)
        .values(content_count=case((model.content_count + delta < 0, 0), else_=model.content_count + delta), updated_at=utcnow())
        .execution_options(synchronize_session="fetch")
    )


# =====================================================================
# 手工创建 / 编辑 / 删除
# =====================================================================


def create_content(db: Session, scope: DataScope, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/contents``：项目须可见（404）且 ``active``（409）；``title_id`` / ``keyword_id`` 须属于该项目（否则 400
    ``invalid_title`` / ``invalid_keyword``；只给 ``title_id`` 时 ``keyword_id`` 取标题的关键词，二者都给须一致）；``status=draft``、
    ``language=projects.language``；同事务 ``keywords.content_count`` / ``titles.content_count += 1``；带 ``body`` 时建版本
    ``source=manual``（状态仍为 ``draft``）。"""
    project = require_project(db, scope, int(values["project_id"]), active=True)
    title_id = values.get("title_id")
    keyword_id = values.get("keyword_id")
    errors: list[dict[str, Any]] = []
    title_row = db.get(Title, title_id) if title_id else None
    if title_id and (title_row is None or title_row.project_id != project.id):
        errors.append(field_error(["body", "title_id"], "标题不存在或不属于该项目", "invalid_title", title_id))
        title_row = None
    keyword_row = db.get(Keyword, keyword_id) if keyword_id else None
    if keyword_id and (keyword_row is None or keyword_row.project_id != project.id):
        errors.append(field_error(["body", "keyword_id"], "关键词不存在或不属于该项目", "invalid_keyword", keyword_id))
        keyword_row = None
    if title_row is not None and keyword_row is not None and title_row.keyword_id != keyword_row.id:
        errors.append(field_error(["body", "keyword_id"], "关键词与标题不匹配", "keyword_mismatch", keyword_id))
    if errors:
        raise invalid_params(errors)
    if keyword_row is None and title_row is not None:
        keyword_row = db.get(Keyword, title_row.keyword_id)
    try:
        content = Content(
            project_id=project.id, title_id=title_row.id if title_row else None, keyword_id=keyword_row.id if keyword_row else None,
            title=str(values["title"]).strip()[:TITLE_MAX], format=values["format"], language=project.language,
            style=values["style"], status="draft", version_count=0, word_count=0, created_by=admin_id, updated_by=admin_id,
        )
        db.add(content)
        db.flush()
        _bump_content_count(db, Keyword, content.keyword_id, 1)
        _bump_content_count(db, Title, content.title_id, 1)
        body = values.get("body")
        if body is not None and str(body).strip():
            write_version(db, content, {"body": str(body)}, source="manual", actor_id=admin_id)
        stats_service.increment_realtime_after_commit(db, project.id, {"contents_created": 1})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _detail(db, scope, content)


def _put_changes(db: Session, content: Content, values: Mapping[str, Any]) -> dict[str, Any]:
    """``PUT`` 请求体 → ``contents`` 列名（大纲按小节上限截断，JSON 列为 Python 值）。"""
    changes: dict[str, Any] = {}
    if "title" in values and values["title"] is not None:
        changes["title"] = str(values["title"]).strip()
    if "body" in values:
        changes["body"] = values["body"] or ""
    if "outline" in values:
        outline = values["outline"]
        if outline is None:
            changes["outline_json"] = None
        else:
            items = [dict(item) for item in outline]
            changes["outline_json"] = validate_outline(items, max_sections=_max_sections(_stored_params(content), db))
    for key in ("summary", "seo_title", "seo_description"):
        if key in values:
            changes[key] = values[key]
    if "seo_keywords" in values:
        keywords = [str(k).strip() for k in (values["seo_keywords"] or []) if str(k).strip()]
        changes["seo_keywords_json"] = list(dict.fromkeys(keywords)) or None
    if "faq" in values:
        faq = [{"q": str(i["q"]).strip(), "a": str(i["a"]).strip()} for i in (values["faq"] or [])]
        changes["faq_json"] = faq or None
    return changes


def update_content(db: Session, scope: DataScope, content_id: int, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``PUT /admin/contents/{id}``：允许 ``draft`` / ``ready`` / ``rejected`` / ``approved`` / ``published``（``generating`` 等其它状态
    409 ``current_status``）；``current_version_id`` 出现且与服务端不一致 → 409 ``data={"current_version_id": 服务端当前值}``，不写入；
    版本化字段变更写版本 ``source=manual``（哈希未变 ``version_created=false``）；``draft``（有正文）/ ``rejected → ready``。
    返回详情并附 ``version_created``。"""
    content = get_content_row(db, scope, content_id)
    if content.status not in EDITABLE_STATES:
        raise _status_conflict(content.status)
    if "current_version_id" in values:
        expected = values.get("current_version_id") or None
        if expected != (content.current_version_id or None):
            raise _conflict(MSG_VERSION_CONFLICT, {"current_version_id": content.current_version_id})
    changes = _put_changes(db, content, values)
    try:
        _version, created = write_version(db, content, changes, source="manual", actor_id=admin_id)
        transition(content, "save", admin_id, db=db)
        content.updated_by = admin_id
        db.commit()
    except Exception:
        db.rollback()
        raise
    data = _detail(db, scope, content)
    data["version_created"] = created
    return data


def delete_content(db: Session, scope: DataScope, content_id: int) -> None:
    """仅 ``draft`` / ``archived`` 且 ``link_count=0``（否则 409：状态不符 ``current_status``、有链接 ``reason=in_use``）；同事务删除
    版本、解绑素材（``content_id=NULL``，``usage_type`` 保留）、``keywords.content_count`` / ``titles.content_count −= 1``。"""
    content = get_content_row(db, scope, content_id)
    if content.status not in ("draft", "archived"):
        raise _status_conflict(content.status)
    if int(content.link_count or 0) > 0:
        raise _conflict(MSG_IN_USE, {"reason": "in_use"})
    try:
        keyword_id, title_id = content.keyword_id, content.title_id
        db.execute(
            update(MediaAsset).where(MediaAsset.content_id == content.id).values(content_id=None, updated_at=utcnow())
            .execution_options(synchronize_session="fetch")
        )
        for version in db.scalars(select(ContentVersion).where(ContentVersion.content_id == content.id)).all():
            db.delete(version)
        db.flush()
        db.delete(content)
        db.flush()
        _bump_content_count(db, Keyword, keyword_id, -1)
        _bump_content_count(db, Title, title_id, -1)
        db.commit()
    except Exception:
        db.rollback()
        raise


# =====================================================================
# 审核 / 归档（docs/09 §8.10）
# =====================================================================


def _change_status(db: Session, scope: DataScope, content_id: int, action: str, *, admin_id: int | None, **kwargs: Any) -> dict[str, Any]:
    content = get_content_row(db, scope, content_id)
    try:
        transition(content, action, admin_id, db=db, **kwargs)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _detail(db, scope, content)


def submit_review(db: Session, scope: DataScope, content_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``ready → reviewing``；``risk_flags`` 含 ``banned_word`` / ``too_short`` → 409 ``data={"current_status":"ready",
    "reason":"quality_blocked","flags":[…]}``（状态不变）；``review_required=false`` 时同事务自动通过（``review_note='auto'``）。"""
    content = get_content_row(db, scope, content_id)
    if content.status != "ready":
        raise _status_conflict(content.status)
    blocked = [f for f in _risk_flags(content) if f in BLOCKING_FLAGS]
    if blocked:
        raise _conflict(MSG_QUALITY_BLOCKED, {"current_status": content.status, "reason": "quality_blocked", "flags": blocked})
    required = bool(_cfg_path(_gen_cfg(db), "review_required", True))
    return _change_status(db, scope, content_id, "submit_review", admin_id=admin_id, review_required=required)


def approve_content(db: Session, scope: DataScope, content_id: int, *, admin_id: int | None, note: str | None = None) -> dict[str, Any]:
    """``reviewing → approved``（``content.contents.review``），写审核字段；``reviewed_at`` 为报表 ``contents_approved`` 的归属时间。"""
    return _change_status(db, scope, content_id, "approve", admin_id=admin_id, note=note)


def reject_content(db: Session, scope: DataScope, content_id: int, *, admin_id: int | None, note: str) -> dict[str, Any]:
    """``reviewing → rejected``（``note`` 必填）。"""
    return _change_status(db, scope, content_id, "reject", admin_id=admin_id, note=note)


def archive_content(db: Session, scope: DataScope, content_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``draft`` / ``ready`` / ``rejected`` / ``approved`` / ``published → archived``（写 ``prev_status``）；``generating`` /
    ``reviewing`` 409。"""
    return _change_status(db, scope, content_id, "archive", admin_id=admin_id)


def unarchive_content(db: Session, scope: DataScope, content_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``archived → prev_status``（``published`` 且 ``link_count=0`` → ``approved``）。"""
    return _change_status(db, scope, content_id, "unarchive", admin_id=admin_id)


# =====================================================================
# 版本接口
# =====================================================================


def list_versions(db: Session, scope: DataScope, content_id: int, *, with_body: bool = False) -> list[dict[str, Any]]:
    """``GET /admin/contents/{id}/versions``：不分页，``version_no`` 降序。"""
    content = get_content_row(db, scope, content_id)
    rows = db.scalars(
        select(ContentVersion).where(ContentVersion.content_id == content.id).order_by(ContentVersion.version_no.desc())
    ).all()
    return [version_item(v, with_body=with_body) for v in rows]


def _version_row(db: Session, content: Content, version_id: int) -> ContentVersion:
    version = db.get(ContentVersion, version_id) if 0 < int(version_id) <= MAX_BIGINT else None
    if version is None or version.content_id != content.id:
        raise BusinessError(VERSION_NOT_FOUND, code=CODE_NOT_FOUND, http_status=404)
    return version


def get_version(db: Session, scope: DataScope, content_id: int, version_id: int) -> dict[str, Any]:
    content = get_content_row(db, scope, content_id)
    return version_detail(_version_row(db, content, version_id))


def restore_version(db: Session, scope: DataScope, content_id: int, version_id: int, *, admin_id: int) -> dict[str, Any]:
    """以该版本全部版本化字段新建版本 ``source=restore``（``restored_from_version_id``），不改状态；``generating`` 409；与当前版本
    ``content_hash`` 相同则 ``version_created=false``。"""
    content = get_content_row(db, scope, content_id)
    if content.status == "generating":
        raise _status_conflict(content.status)
    version = _version_row(db, content, version_id)
    changes = {field: getattr(version, field) for field in CONTENT_VERSIONED_FIELDS}
    try:
        _row, created = write_version(
            db, content, changes, source="restore", actor_id=admin_id, restored_from_version_id=version.id,
            change_summary=f"恢复到 v{version.version_no}",
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    data = _detail(db, scope, content)
    data["version_created"] = created
    return data


def delete_version(db: Session, scope: DataScope, content_id: int, version_id: int) -> None:
    """物理删除历史版本；当前版本 409 ``reason=in_use``；``version_count`` 不回退。"""
    content = get_content_row(db, scope, content_id)
    version = _version_row(db, content, version_id)
    if version.id == content.current_version_id:
        raise _conflict(MSG_CURRENT_VERSION, {"reason": "in_use"})
    try:
        db.delete(version)
        db.commit()
    except Exception:
        db.rollback()
        raise


# =====================================================================
# 素材绑定与链接（docs/10 §4.9；docs/13 §7.4）
# =====================================================================


def list_assets(db: Session, scope: DataScope, content_id: int) -> list[dict[str, Any]]:
    """``GET /admin/contents/{id}/assets``：不分页，按 ``sort, id``。"""
    content = get_content_row(db, scope, content_id)
    return [asset_item(db, scope, a) for a in _assets_of(db, content.id)]


def _clear_cover(db: Session, content_id: int | None, asset_id: int) -> None:
    if not content_id:
        return
    other = db.get(Content, content_id)
    if other is not None and other.cover_asset_id == asset_id:
        other.cover_asset_id = None


def attach_asset(
    db: Session, scope: DataScope, content_id: int, asset_id: int, *, usage_type: str, sort: int | None, admin_id: int | None,
) -> dict[str, Any]:
    """``attach``：内容 ``generating`` 409；素材须可见（404）、``status=ready``（否则 409 ``current_status``）、``project_id ∈ {NULL,
    内容项目}``（否则 400 ``project_mismatch``，为 NULL 时写入内容的项目）；``cover`` 要求 ``kind=image``（否则 400），写
    ``contents.cover_asset_id`` 并把原封面改为 ``inline``；重复 attach 只更新 ``usage_type`` / ``sort``。"""
    content = get_content_row(db, scope, content_id)
    if content.status == "generating":
        raise _status_conflict(content.status)
    asset = get_visible(db, scope, MediaAsset, asset_id, message=ASSET_NOT_FOUND)
    if asset.status != "ready":
        raise _conflict(MSG_ASSET_NOT_READY, {"current_status": asset.status})
    if asset.project_id is not None and asset.project_id != content.project_id:
        raise invalid_params(field_error(["path", "asset_id"], "素材不属于该内容所在项目", "project_mismatch", asset_id))
    if usage_type == "cover" and asset.kind != "image":
        raise invalid_params(field_error(["body", "usage_type"], "封面只能是图片", "kind_mismatch", usage_type))
    try:
        if asset.content_id not in (None, content.id):
            _clear_cover(db, asset.content_id, asset.id)
        if sort is None:
            if asset.content_id == content.id:
                sort = asset.sort
            else:
                current_max = db.scalar(select(func.max(MediaAsset.sort)).where(MediaAsset.content_id == content.id, MediaAsset.id != asset.id))
                sort = int(current_max or 0) + 1
        if asset.project_id is None:
            asset.project_id = content.project_id
        asset.content_id = content.id
        asset.usage_type = usage_type
        asset.sort = int(sort)
        if usage_type == "cover":
            previous = content.cover_asset_id
            if previous and previous != asset.id:
                old = db.get(MediaAsset, previous)
                if old is not None and old.content_id == content.id and old.usage_type == "cover":
                    old.usage_type = "inline"
            content.cover_asset_id = asset.id
        elif content.cover_asset_id == asset.id:
            content.cover_asset_id = None
        if admin_id is not None:
            content.updated_by = admin_id
        db.commit()
    except Exception:
        db.rollback()
        raise
    return asset_item(db, scope, asset)


def detach_asset(db: Session, scope: DataScope, content_id: int, asset_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``detach``：素材须绑定在该内容上（否则 404）；``content_id=NULL``、``usage_type=standalone``、``sort=0``，封面则清空
    ``cover_asset_id``；文件不删除。"""
    content = get_content_row(db, scope, content_id)
    asset = get_visible(db, scope, MediaAsset, asset_id, message=ASSET_NOT_FOUND)
    if asset.content_id != content.id:
        raise BusinessError(ASSET_NOT_FOUND, code=CODE_NOT_FOUND, http_status=404)
    try:
        asset.content_id = None
        asset.usage_type = "standalone"
        asset.sort = 0
        if content.cover_asset_id == asset.id:
            content.cover_asset_id = None
        if admin_id is not None:
            content.updated_by = admin_id
        db.commit()
    except Exception:
        db.rollback()
        raise
    return asset_item(db, scope, asset)


def list_links(db: Session, scope: DataScope, content_id: int) -> list[dict[str, Any]]:
    """``GET /admin/contents/{id}/links``：不分页，``published_at DESC, id DESC``（附平台摘要）。"""
    content = get_content_row(db, scope, content_id)
    rows = list(
        db.scalars(
            scope_by_project(select(PublishLink).where(PublishLink.content_id == content.id), PublishLink.project_id, scope)
            .order_by(PublishLink.published_at.desc(), PublishLink.id.desc())
        ).all()
    )
    platform_ids = {r.platform_id for r in rows}
    platforms = {p.id: p for p in db.scalars(select(PublishPlatform).where(PublishPlatform.id.in_(platform_ids or {0}))).all()}
    return [link_item(r, platforms.get(r.platform_id)) for r in rows]


# =====================================================================
# 导出（docs/09 §8.4、§8.5）
# =====================================================================

_INLINE_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|`([^`]+)`"
)


def _emphasis(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", text)


def _inline(text: str) -> str:
    parts: list[str] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        parts.append(_emphasis(html_lib.escape(text[pos:m.start()], quote=False)))
        if m.group(2) is not None:
            url = safe_url(m.group(2))
            alt = html_lib.escape(m.group(1), quote=True)
            parts.append(f'<img src="{html_lib.escape(url, quote=True)}" alt="{alt}">' if url else html_lib.escape(m.group(1), quote=False))
        elif m.group(4) is not None:
            url = safe_url(m.group(4))
            label = _emphasis(html_lib.escape(m.group(3), quote=False))
            parts.append(f'<a href="{html_lib.escape(url, quote=True)}">{label}</a>' if url else label)
        else:
            parts.append(f"<code>{html_lib.escape(m.group(5), quote=False)}</code>")
        pos = m.end()
    parts.append(_emphasis(html_lib.escape(text[pos:], quote=False)))
    return "".join(parts)


def _table_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _md_blocks(lines: list[str]) -> list[str]:
    out: list[str] = []
    paragraph: list[str] = []
    list_re = re.compile(r"^\s*([-*+]|\d+[.)])\s+(.*)$")

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(p) for p in paragraph) + "</p>")
            paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            marker = fence.group(1)
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                code.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html_lib.escape("\n".join(code), quote=False) + "</code></pre>")
            continue
        if not stripped:
            flush()
            i += 1
            continue
        heading = _MD_HEADING_RE.match(stripped)
        if heading:
            flush()
            level = min(4, max(2, len(heading.group(1))))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if re.match(r"^(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$", stripped):
            flush()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush()
            quote: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + "".join(_md_blocks(quote)) + "</blockquote>")
            continue
        if stripped.startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            flush()
            header = _table_cells(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_table_cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body_html = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>" for row in rows)
            out.append(f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>")
            continue
        item = list_re.match(line)
        if item:
            flush()
            ordered = item.group(1)[0].isdigit()
            entries: list[str] = []
            while i < len(lines):
                match = list_re.match(lines[i])
                if match and match.group(1)[0].isdigit() == ordered:
                    entries.append(match.group(2))
                    i += 1
                elif entries and lines[i].strip() and lines[i].startswith(("  ", "\t")) and not list_re.match(lines[i]):
                    entries[-1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(e)}</li>" for e in entries) + f"</{tag}>")
            continue
        paragraph.append(stripped)
        i += 1
    flush()
    return out


def markdown_to_html(text: str | None) -> str:
    """导出用的最小 Markdown → HTML 转换（标题、段落、列表、引用、代码、表格、分隔线、强调、链接、图片），原始 HTML 一律转义，
    结果再经 ``sanitize_html``。"""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    return sanitize_html("\n".join(_md_blocks(lines)))


def _faq_items(content: Content) -> list[dict[str, str]]:
    value = _loads(content.faq_json, [])
    if not isinstance(value, list):
        return []
    return [
        {"q": str(i.get("q") or ""), "a": str(i.get("a") or "")}
        for i in value if isinstance(i, Mapping) and i.get("q") and i.get("a")
    ]


def export_content(db: Session, scope: DataScope, content_id: int, export_format: str) -> tuple[str, str, str]:
    """``GET /admin/contents/{id}/export?format=md|html|json``：返回 ``(文件名, 媒体类型, 文本)``。``md`` 为 ``# {title}`` + 正文 + FAQ
    小节（``## 常见问题``，``**Q：**`` / ``A：``）；``html`` 为完整文档（``<title>`` 取 ``seo_title``，``<meta name="description">``
    取 ``seo_description``，FAQ 为 ``<section class="faq">``）；``json`` 为当前版本全部版本化字段与素材 URL 列表。导出不含管理员信息。"""
    content = get_content_row(db, scope, content_id)
    faq = _faq_items(content)
    body = content.body or ""
    day = stats_service.today_date(db).strftime("%Y%m%d")
    if export_format == "md":
        parts = [f"# {content.title}", body.strip()]
        if faq:
            lines = [f"## {FAQ_HEADING}"]
            for item in faq:
                lines.append(f"**Q：**{item['q']}\n\nA：{item['a']}")
            parts.append("\n\n".join(lines))
        text = "\n\n".join(p for p in parts if p) + "\n"
        return f"content-{content.id}-{day}.md", "text/markdown; charset=utf-8", text
    if export_format == "html":
        body_html = sanitize_html(body) if content.format == "html" else markdown_to_html(body)
        keywords = _loads(content.seo_keywords_json, []) or []
        head = [
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{html_lib.escape(content.seo_title or content.title, quote=False)}</title>",
            f'<meta name="description" content="{html_lib.escape(content.seo_description or "", quote=True)}">',
        ]
        if keywords:
            head.append(f'<meta name="keywords" content="{html_lib.escape(",".join(str(k) for k in keywords), quote=True)}">')
        article = [f"<h1>{html_lib.escape(content.title, quote=False)}</h1>", body_html]
        if faq:
            faq_html = "".join(
                f"<h3>{html_lib.escape(i['q'], quote=False)}</h3><p>{html_lib.escape(i['a'], quote=False)}</p>" for i in faq
            )
            article.append(f'<section class="faq"><h2>{FAQ_HEADING}</h2>{faq_html}</section>')
        lang = html_lib.escape(content.language or "zh-CN", quote=True)
        document = (
            f'<!DOCTYPE html>\n<html lang="{lang}">\n<head>\n' + "\n".join(head) + "\n</head>\n<body>\n<article>\n"
            + "\n".join(article) + "\n</article>\n</body>\n</html>\n"
        )
        return f"content-{content.id}-{day}.html", "text/html; charset=utf-8", document
    version = db.get(ContentVersion, content.current_version_id) if content.current_version_id else None
    cover = db.get(MediaAsset, content.cover_asset_id) if content.cover_asset_id else None
    data = {
        "id": content.id,
        "title": content.title,
        "format": content.format,
        "language": content.language,
        "style": content.style,
        "status": content.status,
        "version_id": content.current_version_id,
        "version_no": version.version_no if version is not None else None,
        "word_count": content.word_count,
        "body": body,
        "outline": _loads(content.outline_json, None),
        "summary": content.summary,
        "seo_title": content.seo_title,
        "seo_description": content.seo_description,
        "seo_keywords": _loads(content.seo_keywords_json, None),
        "faq": _loads(content.faq_json, None),
        "cover_url": cover.url if cover is not None and cover.status != "deleted" else None,
        "assets": [
            {"id": a.id, "kind": a.kind, "usage_type": a.usage_type, "url": a.url, "sort": a.sort}
            for a in _assets_of(db, content.id) if a.url
        ],
        "exported_at": iso_utc(utcnow()),
    }
    return f"content-{content.id}-{day}.json", "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False, indent=2)


# =====================================================================
# 生成接口（API 侧，docs/09 §8.1、§6.1 校验链）
# =====================================================================


def _keyword_of(db: Session, content: Content) -> Keyword | None:
    return db.get(Keyword, content.keyword_id) if content.keyword_id else None


def _body_variable(body: str | None) -> str:
    """模板变量 ``body``：超过 20000 字符时取前 16000 + 后 4000 字符并以 ``……（中间省略）……`` 连接（§5.3）。"""
    text = body or ""
    if len(text) <= BODY_VARIABLE_LIMIT:
        return text
    return text[:BODY_VARIABLE_HEAD] + BODY_OMISSION + text[-BODY_VARIABLE_TAIL:]


def _base_variables(db: Session, content: Content, project: Project | None, params: Mapping[str, Any]) -> dict[str, Any]:
    keyword = _keyword_of(db, content)
    variables = generation_service.project_variables(project)
    variables.update(
        language=content.language or variables["language"],
        keyword=keyword.keyword if keyword is not None else "",
        intent=keyword.intent if keyword is not None else "",
        style=style_text(content.style),
        title=content.title,
        format=content.format,
        target_word_count=int(params["target_word_count"]),
        max_sections=int(params["sections"]),
        faq_count=int(params["faq_count"]),
    )
    return variables


def _outline_of(content: Content) -> list[dict[str, Any]]:
    value = _loads(content.outline_json, [])
    return [dict(i) for i in value if isinstance(i, Mapping)] if isinstance(value, list) else []


def _whole_max_tokens(target: int) -> int:
    return _clamp(math.ceil(target * 1.6) + 500, 2048, 8192)


def _section_word_count(target: int, sections: int) -> int:
    return max(100, round(target / max(1, sections)))


def _section_max_tokens(section_words: int) -> int:
    return _clamp(math.ceil(section_words * 1.6) + 300, 1024, 4096)


def _body_kind(db: Session, template_id: int | None, segmented_mode: bool) -> str:
    """正文模板 kind：显式 ``template_id`` 为 ``content`` / ``section`` 时取其 kind，否则按模式（分段 ``section``、整篇 ``content``）。"""
    if template_id:
        tpl = db.get(PromptTemplate, template_id)
        if tpl is not None and tpl.kind in ("content", "section"):
            return tpl.kind
    return "section" if segmented_mode else "content"


def _resolve(db: Session, kind: str, project: Project) -> PromptTemplate:
    tpl = pts.resolve_template(db, kind, project.id, project.language)
    pts.check_required_variables(tpl, {})
    return tpl


def _estimate_body(
    db: Session, route: Any, template: PromptTemplate, variables: Mapping[str, Any], *, segmented_mode: bool, target: int,
    sections: int, outline: Sequence[Mapping[str, Any]] | None = None,
) -> int:
    if segmented_mode:
        section_words = _section_word_count(target, sections)
        sample = dict(variables)
        level2 = [i for i, item in enumerate(outline or []) if int(item.get("level") or 2) == 2]
        if outline and level2:
            sample.update(outline=render_outline(outline), section=render_section(outline, level2[0]))
        sample["section_word_count"] = section_words
        return generation_service.estimate_call(
            db, route, template, sample, dynamic_params={"max_tokens": _section_max_tokens(section_words)}, calls=sections,
        )
    sample = dict(variables)
    if outline:
        sample["outline"] = render_outline(outline)
    return generation_service.estimate_call(db, route, template, sample, dynamic_params={"max_tokens": _whole_max_tokens(target)})


def generate_contents(db: Session, scope: DataScope, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/contents/generate``：校验链（docs/09 §6.1；``target_word_count ∈ [min_word_count, max_word_count]``、
    ``title_ids`` 须属于该项目且 ``status=adopted``，否则 400，每个不合法 ID 一项 ``type=invalid_title``）→ 为每个标题创建
    ``contents(status=generating, prev_status=draft)``（同事务 ``keywords.content_count`` / ``titles.content_count += 1``）+ 批次
    ``kind=content``（``task_total = requested_count = 标题数``）+ 每篇一个 ``content_generate`` 根任务（``input_json`` = 请求体 +
    ``title_id`` + ``content_id``；额度按计划调用次数求和预占）；返回 ``{batch_id, content_ids[], quota_warning?}``。"""
    cfg = _gen_cfg(db)
    content_cfg = cfg.get("content") or {}
    min_words = int(content_cfg.get("min_word_count") or 300)
    max_words = int(content_cfg.get("max_word_count") or 6000)
    target = int(body["target_word_count"])
    outline_first = bool(body["outline_first"])
    include_faq = bool(body["include_faq"])
    include_seo = bool(body["include_seo_meta"])
    faq_count = int(content_cfg.get("faq_count") or 0)
    sections = int(content_cfg.get("max_sections") or 8)
    segmented_cfg = bool(content_cfg.get("segmented", True))
    segmented_mode = segmented_cfg and outline_first
    model = (str(body.get("model") or "").strip() or None)
    template_id = body.get("template_id")
    raw_ids = [int(t) for t in body.get("title_ids") or []]
    selected: list[Title] = []

    def _validate(project: Project) -> None:
        errors: list[dict[str, Any]] = []
        if target < min_words:
            errors.append(field_error(["body", "target_word_count"], f"不能小于 {min_words}", "greater_than_equal", target))
        elif target > max_words:
            errors.append(field_error(["body", "target_word_count"], f"不能大于 {max_words}", "less_than_equal", target))
        rows = {t.id: t for t in db.scalars(select(Title).where(Title.id.in_(list(dict.fromkeys(raw_ids)) or [0]))).all()}
        seen: set[int] = set()
        for index, title_id in enumerate(raw_ids):
            row = rows.get(title_id)
            if row is None or row.project_id != project.id or row.status != "adopted":
                errors.append(field_error(["body", "title_ids", index], MSG_INVALID_TITLE, "invalid_title", title_id))
                continue
            if title_id not in seen:
                seen.add(title_id)
                selected.append(row)
        if errors:
            raise invalid_params(errors)

    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=int(body["project_id"]), capability="content",
        kind=_body_kind(db, template_id, segmented_mode), template_id=template_id, model=model, validate=_validate,
    )
    project = plan.project
    outline_tpl = _resolve(db, "outline", project) if outline_first else None
    seo_tpl = _resolve(db, "seo_meta", project) if include_seo else None
    faq_tpl = _resolve(db, "faq", project) if include_faq and faq_count > 0 else None
    params = {
        "outline_first": outline_first, "segmented": segmented_cfg, "target_word_count": target, "include_faq": include_faq,
        "faq_count": faq_count, "include_seo_meta": include_seo, "format": body["format"], "sections": sections,
    }
    request = {
        "project_id": project.id, "title_ids": [t.id for t in selected], "template_id": template_id, "outline_first": outline_first,
        "target_word_count": target, "include_faq": include_faq, "include_seo_meta": include_seo, "format": body["format"],
        "model": model,
    }
    units: list[generation_service.BatchUnit] = []
    created: list[Content] = []
    try:
        for title in selected:
            content = Content(
                project_id=project.id, title_id=title.id, keyword_id=title.keyword_id, title=title.title, format=body["format"],
                language=project.language, style=title.style, status="draft", template_id=plan.template.id,
                generation_params_json=_dumps(params), version_count=0, word_count=0, created_by=admin_id, updated_by=admin_id,
            )
            db.add(content)
            db.flush()
            transition(content, "generate", admin_id)
            _bump_content_count(db, Keyword, title.keyword_id, 1)
            _bump_content_count(db, Title, title.id, 1)
            stats_service.increment_realtime_after_commit(db, project.id, {"contents_created": 1})
            created.append(content)
            variables = _base_variables(db, content, project, params)
            estimate = 0
            if outline_tpl is not None:
                estimate += generation_service.estimate_call(db, plan.route, outline_tpl, variables)
            estimate += _estimate_body(
                db, plan.route, plan.template, variables, segmented_mode=segmented_mode, target=target, sections=sections,
            )
            if seo_tpl is not None:
                estimate += generation_service.estimate_call(db, plan.route, seo_tpl, variables)
            if faq_tpl is not None:
                estimate += generation_service.estimate_call(db, plan.route, faq_tpl, variables)
            units.append(
                generation_service.BatchUnit(
                    target_type="content", target_id=content.id, input={**request, "title_id": title.id, "content_id": content.id},
                    estimated_quota=estimate, template_id=plan.template.id, extra={"content": content},
                )
            )
    except Exception:
        db.rollback()
        raise

    def _before_commit(batch: Any, pairs: list[tuple[generation_service.BatchUnit, AiTask]]) -> None:
        for unit, task in pairs:
            row: Content = unit.extra["content"]
            row.batch_id = batch.id
            row.ai_task_id = task.id

    batch = generation_service.create_batch(
        db, project=project, kind="content", capability="content", operation="content_generate", template=plan.template,
        route=plan.route, batch_input=request, requested_count=len(selected), units=units, created_by=admin_id,
        before_commit=_before_commit,
    )
    return generation_service.batch_created_response(db, batch, extra={"content_ids": [c.id for c in created]})


def _task_created(
    db: Session,
    content: Content,
    *,
    plan: generation_service.GenerationPlan,
    capability: str,
    operation: str,
    input_data: Mapping[str, Any],
    estimated: int,
    template_id: int,
    actor_id: int,
    start_generating: bool,
) -> dict[str, Any]:
    """单内容根任务（不建批次）：创建根任务、预占额度、按需 ``→ generating``，``content_generate`` / ``content_body`` /
    ``content_rewrite`` 同事务把 ``contents.ai_task_id`` 指向新根任务；提交后入队。返回 ``{task_id, quota_warning?}``。"""
    task: AiTask | None = None
    try:
        task = gateway.create_root_task(
            db, capability=capability, operation=operation, project_id=content.project_id, created_by=actor_id, trigger_type="user",
            target_type="content", target_id=content.id, template_id=template_id, input=dict(input_data), route=plan.route,
        )
        if estimated > 0:
            gateway.check_quota(db, project_id=content.project_id, estimated_quota=estimated, task=task)
        if start_generating:
            transition(content, "generate", actor_id)
        if operation in ACTIVE_TASK_OPERATIONS:
            content.ai_task_id = task.id
        db.commit()
    except Exception:
        db.rollback()
        if task is not None:
            gateway.release_reservation(db, task)
        raise
    data: dict[str, Any] = {"task_id": task.id}
    warning = gateway.quota_warning(db, project_id=content.project_id)
    if warning:
        data["quota_warning"] = warning
    return data


def _check_side_task(db: Session, content: Content, operation: str) -> None:
    if content.status not in SIDE_TASK_STATES:
        raise _status_conflict(content.status)
    existing = _active_same_operation(db, content.id, operation)
    if existing:
        raise _conflict(MSG_TASK_EXISTS, {"existing_id": existing})


def generate_outline(db: Session, scope: DataScope, content_id: int, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/contents/{id}/generate-outline`` → ``content_outline`` 根任务（不改状态）；允许 ``draft`` / ``ready`` /
    ``rejected`` / ``approved`` / ``published``（其它 409 ``current_status``）；已有非终态同类任务 409 ``existing_id``。"""
    content = get_content_row(db, scope, content_id)
    model = (str(body.get("model") or "").strip() or None)
    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=content.project_id, capability="content", kind="outline",
        template_id=body.get("template_id"), model=model, validate=lambda _p: _check_side_task(db, content, "content_outline"),
    )
    params = generation_params(db, content)
    variables = _base_variables(db, content, plan.project, params)
    estimate = generation_service.estimate_call(db, plan.route, plan.template, variables)
    return _task_created(
        db, content, plan=plan, capability="content", operation="content_outline",
        input_data={"content_id": content.id, "template_id": body.get("template_id"), "model": model},
        estimated=estimate, template_id=plan.template.id, actor_id=admin_id, start_generating=False,
    )


def generate_body(db: Session, scope: DataScope, content_id: int, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/contents/{id}/generate-body`` → ``content_body`` 根任务；``draft`` / ``ready`` / ``rejected → generating``（写
    ``prev_status``），其它状态 409 ``current_status``。``segmented``（缺省 ``generation_params_json.segmented``）且有大纲时逐节生成，
    无大纲时自动退化为整篇生成。"""
    content = get_content_row(db, scope, content_id)
    model = (str(body.get("model") or "").strip() or None)
    params = generation_params(db, content)
    segmented = params["segmented"] if body.get("segmented") is None else bool(body["segmented"])
    outline = _outline_of(content)
    level2 = _level2(outline)
    segmented_mode = segmented and bool(level2)

    def _validate(_project: Project) -> None:
        if content.status not in GENERATE_START:
            raise _status_conflict(content.status)
        existing = _active_same_operation(db, content.id, "content_body")
        if existing:
            raise _conflict(MSG_TASK_EXISTS, {"existing_id": existing})

    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=content.project_id, capability="content",
        kind=_body_kind(db, body.get("template_id"), segmented_mode), template_id=body.get("template_id"), model=model,
        validate=_validate,
    )
    variables = _base_variables(db, content, plan.project, params)
    estimate = _estimate_body(
        db, plan.route, plan.template, variables, segmented_mode=segmented_mode, target=params["target_word_count"],
        sections=len(level2) or 1, outline=outline,
    )
    return _task_created(
        db, content, plan=plan, capability="content", operation="content_body",
        input_data={"content_id": content.id, "segmented": body.get("segmented"), "template_id": body.get("template_id"), "model": model},
        estimated=estimate, template_id=plan.template.id, actor_id=admin_id, start_generating=True,
    )


def generate_seo(db: Session, scope: DataScope, content_id: int, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/contents/{id}/generate-seo`` → ``content_seo`` 根任务（``seo_meta`` + 可选 ``faq``，不改状态）；允许状态同
    ``generate-outline`` 且正文非空（否则 409 ``current_status``）。``template_id`` 为 ``seo_meta`` 模板，FAQ 模板按 §4.2 解析。"""
    content = get_content_row(db, scope, content_id)
    model = (str(body.get("model") or "").strip() or None)
    params = generation_params(db, content)
    include_faq = params["include_faq"] if body.get("include_faq") is None else bool(body["include_faq"])

    def _validate(_project: Project) -> None:
        _check_side_task(db, content, "content_seo")
        if not (content.body or "").strip():
            raise _status_conflict(content.status, MSG_BODY_EMPTY)

    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=content.project_id, capability="content", kind="seo_meta",
        template_id=body.get("template_id"), model=model, validate=_validate,
    )
    variables = {**_base_variables(db, content, plan.project, params), "body": _body_variable(content.body)}
    estimate = generation_service.estimate_call(db, plan.route, plan.template, variables)
    if include_faq and params["faq_count"] > 0:
        estimate += generation_service.estimate_call(db, plan.route, _resolve(db, "faq", plan.project), variables)
    return _task_created(
        db, content, plan=plan, capability="content", operation="content_seo",
        input_data={"content_id": content.id, "include_faq": include_faq, "template_id": body.get("template_id"), "model": model},
        estimated=estimate, template_id=plan.template.id, actor_id=admin_id, start_generating=False,
    )


def _rewrite_target_words(mode: str, current: int, *, section: bool, min_words: int, max_words: int) -> int:
    """``target_word_count`` 按模式派生：``rewrite`` / ``restyle`` 取当前字数；``expand`` 取 ``min(当前 × 1.5, max_word_count)``；
    ``shorten`` 取 ``max(当前 × 0.6, min_word_count)``（``scope=section`` 时按该节字数计算、不受全局上下限约束）。"""
    if mode == "expand":
        value = current * 1.5
        return max(1, round(value if section else min(value, max_words)))
    if mode == "shorten":
        value = current * 0.6
        return max(1, round(value if section else max(value, min_words)))
    return max(1, current)


def rewrite_content(db: Session, scope: DataScope, content_id: int, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/contents/{id}/rewrite`` → ``content_rewrite`` 根任务（能力 ``rewrite``）：``draft``（有正文）/ ``ready`` /
    ``rejected → generating``；``approved`` / ``published`` 不改状态（已有非终态同类任务 409 ``existing_id``）；``generating`` /
    ``reviewing`` / ``archived`` 或无正文 → 409 ``current_status``。``mode`` 须在 ``generation_config.rewrite.modes`` 内；``restyle``
    必带 ``style``；``scope=section`` 必带 ``section_index``，超出大纲长度 / 无大纲 / 正文中找不到该小节均 400
    ``loc=["body","section_index"]``。"""
    content = get_content_row(db, scope, content_id)
    cfg = _gen_cfg(db)
    mode = str(body["mode"])
    scope_kind = str(body.get("scope") or "full")
    section_index = body.get("section_index")
    style = body.get("style")
    instruction = (body.get("instruction") or "").strip() or None
    model = (str(body.get("model") or "").strip() or None)
    outline = _outline_of(content)
    start_generating = content.status in GENERATE_START

    def _validate(_project: Project) -> None:
        if not (content.body or "").strip() or content.status not in (*GENERATE_START, *REWRITE_NO_CHANGE):
            raise _status_conflict(content.status)
        if content.status in REWRITE_NO_CHANGE:
            existing = _active_same_operation(db, content.id, "content_rewrite")
            if existing:
                raise _conflict(MSG_TASK_EXISTS, {"existing_id": existing})
        errors: list[dict[str, Any]] = []
        modes = [str(m) for m in (_cfg_path(cfg, "rewrite.modes", []) or [])]
        if mode not in modes:
            errors.append(field_error(["body", "mode"], "该重写模式未启用", "mode_disabled", mode))
        if mode == "restyle" and not style:
            errors.append(field_error(["body", "style"], "改风格时必须指定风格", "missing", style))
        if scope_kind == "section":
            if section_index is None:
                errors.append(field_error(["body", "section_index"], "指定小节时必须提供 section_index", "missing", None))
            elif not outline:
                errors.append(field_error(["body", "section_index"], "内容没有大纲", "outline_missing", section_index))
            elif int(section_index) > len(outline):
                errors.append(field_error(["body", "section_index"], "超出大纲范围", "out_of_range", section_index))
            elif locate_section(content.body, content.format, outline, int(section_index)) is None:
                errors.append(field_error(["body", "section_index"], "正文中找不到该小节", "section_not_found", section_index))
        if errors:
            raise invalid_params(errors)

    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=content.project_id, capability="rewrite", kind=mode,
        template_id=body.get("template_id"), model=model, validate=_validate,
    )
    params = generation_params(db, content)
    text = content.body or ""
    if scope_kind == "section" and section_index:
        section = locate_section(content.body, content.format, outline, int(section_index))
        if section is not None:
            text = (content.body or "")[section.start:section.end].strip()
    content_cfg = cfg.get("content") or {}
    target = _rewrite_target_words(
        mode, count_words(text, content.format), section=scope_kind == "section",
        min_words=int(content_cfg.get("min_word_count") or 300), max_words=int(content_cfg.get("max_word_count") or 6000),
    )
    variables = {
        **_base_variables(db, content, plan.project, params), "text": text, "instruction": instruction or "",
        "style": style_text(style if mode == "restyle" and style else content.style), "target_word_count": target,
    }
    estimate = generation_service.estimate_call(
        db, plan.route, plan.template, variables, dynamic_params={"max_tokens": _whole_max_tokens(target)},
    )
    input_data = {
        "content_id": content.id, "mode": mode, "scope": scope_kind,
        "section_index": int(section_index) if scope_kind == "section" and section_index else None,
        "style": style if mode == "restyle" else None, "instruction": instruction, "template_id": body.get("template_id"),
        "model": model,
    }
    return _task_created(
        db, content, plan=plan, capability="rewrite", operation="content_rewrite", input_data=input_data, estimated=estimate,
        template_id=plan.template.id, actor_id=admin_id, start_generating=start_generating,
    )


# =====================================================================
# worker 处理器（docs/09 §8.2~§8.7、§9.4）
# =====================================================================


def _failed(message: str, category: str = "unknown") -> ai_task_service.TaskOutcome:
    return ai_task_service.TaskOutcome(status="failed", error_category=category, error_message=message)


def _progress(ctx: ai_task_service.TaskContext, value: int) -> None:
    ctx.task.progress = max(int(ctx.task.progress or 0), min(99, int(value)))
    ctx.db.commit()


def _tag_attempt(attempt: AiTask, template: PromptTemplate) -> None:
    if attempt.template_id != template.id:
        attempt.template_id = template.id


def _call_text(
    ctx: ai_task_service.TaskContext,
    template: PromptTemplate,
    variables: Mapping[str, Any],
    *,
    dynamic: Mapping[str, Any] | None = None,
    segment_index: int | None = None,
) -> tuple[TextResult, AiTask]:
    """文本调用（正文 / 小节 / 改写）：空输出按 ``invalid_response`` 由网关同模型重新生成 1 次。"""
    system, user = pts.render(template, variables)

    def _validate(result: TextResult) -> str:
        if not (result.text or "").strip():
            raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出为空")
        return result.text

    result, attempt = ctx.complete_text(
        messages=pts.messages_for(system, user),
        params=pts.call_params(template, dynamic),
        response_format=pts.response_format_for(template),
        metadata=pts.call_metadata(template, ctx.task.operation),
        validator=_validate,
        segment_index=segment_index,
    )
    _tag_attempt(attempt, template)
    return result, attempt


def _call_json(
    ctx: ai_task_service.TaskContext,
    template: PromptTemplate,
    variables: Mapping[str, Any],
    normalize: Callable[[Any], Any],
) -> tuple[Any, AiTask]:
    """JSON 调用：``extract_json`` + ``output_schema_json`` 校验后再经 ``normalize``（结构性错误抛 ``INVALID_RESPONSE`` → 网关
    同模型重新生成 1 次）。返回 ``(规范化结果, 成功尝试行)``。"""
    system, user = pts.render(template, variables)
    base = pts.output_validator(template)

    def _validate(result: TextResult) -> Any:
        data = base(result) if base is not None else zhiqi_text.extract_json(result.text)
        return normalize(data)

    result, attempt = ctx.complete_text(
        messages=pts.messages_for(system, user),
        params=pts.call_params(template, None),
        response_format=pts.response_format_for(template),
        metadata=pts.call_metadata(template, ctx.task.operation),
        validator=_validate,
    )
    _tag_attempt(attempt, template)
    parsed = getattr(result, "parsed", None)
    return (parsed if parsed is not None else _validate(result)), attempt


def _normalize_seo(data: Any, *, title: str, keyword: str | None) -> dict[str, Any]:
    """``sys_seo_meta`` 输出 → 列值（§8.4）：``summary`` 截断 500；``seo_title`` 空时回退标题、截断 200；``seo_description`` 截断
    500；``seo_keywords`` ≤ 10 项、每项 ≤ 60 字符，为空时填 ``[keyword]``。"""
    if not isinstance(data, Mapping):
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：$ 应为对象")

    def text(value: Any) -> str:
        return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""

    raw_keywords = data.get("seo_keywords")
    if isinstance(raw_keywords, str):
        raw_keywords = re.split(r"[,，、;；]", raw_keywords)
    keywords: list[str] = []
    for item in raw_keywords if isinstance(raw_keywords, list) else []:
        value = text(item if isinstance(item, str) else str(item) if isinstance(item, (int, float)) else "")[:SEO_KEYWORD_MAX]
        if value and value not in keywords:
            keywords.append(value)
        if len(keywords) >= SEO_KEYWORDS_MAX:
            break
    if not keywords and keyword:
        keywords = [keyword[:SEO_KEYWORD_MAX]]
    return {
        "summary": text(data.get("summary"))[:SUMMARY_MAX] or None,
        "seo_title": (text(data.get("seo_title")) or title)[:SEO_TITLE_MAX],
        "seo_description": text(data.get("seo_description"))[:SEO_DESCRIPTION_MAX] or None,
        "seo_keywords_json": keywords or None,
    }


def _normalize_faq(data: Any, *, count: int) -> list[dict[str, str]]:
    """``sys_faq`` 输出 → ``[{"q","a"}]``：最多 ``faq_count`` 项，``q`` ≤ 200、``a`` ≤ 1000 字符，缺 ``q`` / ``a`` 的项丢弃。"""
    if not isinstance(data, list):
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：$ 应为数组")
    items: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        q = item.get("q")
        a = item.get("a")
        if not isinstance(q, str) or not isinstance(a, str) or not q.strip() or not a.strip():
            continue
        items.append({"q": q.strip()[:FAQ_Q_MAX], "a": a.strip()[:FAQ_A_MAX]})
        if len(items) >= max(0, count):
            break
    return items


@dataclass
class _BodyResult:
    body: str
    attempt: AiTask
    template: PromptTemplate
    truncated: bool


def _explicit_body_template(db: Session, task: AiTask) -> PromptTemplate | None:
    explicit = gateway.task_input(task).get("template_id")
    if explicit:
        tpl = db.get(PromptTemplate, explicit)
        if tpl is not None and tpl.kind in ("content", "section"):
            return tpl
    return None


def _body_template(db: Session, task: AiTask, project: Project, kind: str) -> PromptTemplate:
    """正文模板：显式 ``template_id``（分段时作为节模板，否则作为全文模板）→ 根任务 ``template_id`` 快照（kind 一致时）→ 按 §4.2 解析。"""
    explicit = _explicit_body_template(db, task)
    if explicit is not None:
        return explicit
    if task.template_id:
        tpl = db.get(PromptTemplate, task.template_id)
        if tpl is not None and tpl.kind == kind:
            return tpl
    return pts.resolve_template(db, kind, project.id, project.language)


def _generate_body(
    ctx: ai_task_service.TaskContext,
    content: Content,
    project: Project,
    params: Mapping[str, Any],
    outline: Sequence[Mapping[str, Any]] | None,
    *,
    segmented: bool,
    variables: Mapping[str, Any],
    progress: tuple[int, int],
) -> _BodyResult:
    """正文生成（§8.3）：``segmented`` 且有 ``level=2`` 小节时逐节串行调用 ``sys_section``（每节一行尝试行，``segment_index=1..n``），
    否则整篇调用 ``sys_content``；任一节失败由异常向上抛出（根任务 ``failed``，不写部分正文）。"""
    db, task = ctx.db, ctx.task
    fmt = content.format or "markdown"
    target = int(params["target_word_count"])
    heads = [i for i, item in enumerate(outline or []) if int(item.get("level") or 2) == 2]
    start, end = progress
    if segmented and heads:
        template = _body_template(db, task, project, "section")
        n = len(heads)
        section_words = _section_word_count(target, n)
        outputs: list[str] = []
        truncated = False
        attempt: AiTask | None = None
        for number, position in enumerate(heads, start=1):
            assembled = assemble_sections(outputs, outline, fmt) if outputs else ""
            values = {
                **variables, "outline": render_outline(outline), "section": render_section(outline or [], position),
                "previous_text": assembled[-PREVIOUS_TEXT_CHARS:], "section_word_count": section_words,
            }
            result, attempt = _call_text(
                ctx, template, values, dynamic={"max_tokens": _section_max_tokens(section_words)}, segment_index=number,
            )
            outputs.append(result.text)
            truncated = truncated or result.finish_reason == "length"
            _progress(ctx, start + (end - start) * number // n)
        assert attempt is not None
        return _BodyResult(assemble_sections(outputs, outline, fmt), attempt, template, truncated)
    template = _body_template(db, task, project, "content")
    values = {**variables, "outline": render_outline(outline)}
    result, attempt = _call_text(ctx, template, values, dynamic={"max_tokens": _whole_max_tokens(target)})
    _progress(ctx, end)
    return _BodyResult(assemble_whole(result.text, outline, fmt), attempt, template, result.finish_reason == "length")


def _load(ctx: ai_task_service.TaskContext) -> tuple[Content | None, Project | None]:
    content_id = ctx.input.get("content_id") or ctx.task.target_id
    content = ctx.db.get(Content, content_id) if content_id else None
    project = ctx.db.get(Project, content.project_id) if content is not None else None
    return content, project


def _actor(task: AiTask, content: Content) -> int:
    return int(task.created_by or content.created_by or 0)


def _generate_outline(
    ctx: ai_task_service.TaskContext, template: PromptTemplate, variables: Mapping[str, Any], max_sections: int,
) -> tuple[list[dict[str, Any]], AiTask]:
    return _call_json(ctx, template, variables, lambda data: validate_outline(data, max_sections=max_sections, strict=False))


def run_content_generate(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``content_generate``（§8.2）：``outline_first`` → 大纲（暂存于执行上下文，``progress=10``）→ 正文（``progress`` 10→80）→
    ``include_seo_meta`` → ``seo_meta``（90）→ ``include_faq`` → ``faq``（95）→ 终态事务内一次写版本 v1（``source=generate``，含暂存
    大纲）与质量规则 → ``generating → ready``。任一步失败 / 批次已取消：不写 ``outline_json``、不建版本，恢复 ``prev_status``。"""
    db = ctx.db
    content, project = _load(ctx)
    if content is None or project is None:
        return _failed(CONTENT_NOT_FOUND)
    if content.status != "generating":
        return _failed("内容不处于生成中")
    params = generation_params(db, content)
    variables = _base_variables(db, content, project, params)
    outline: list[dict[str, Any]] | None = None
    if params["outline_first"]:
        outline_tpl = pts.resolve_template(db, "outline", project.id, project.language)
        outline, _ = _generate_outline(ctx, outline_tpl, variables, params["sections"])
        _progress(ctx, 10)
    else:
        outline = _outline_of(content) or None
    body = _generate_body(
        ctx, content, project, params, outline, segmented=params["segmented"], variables=variables, progress=(10, 80),
    )
    seo: dict[str, Any] | None = None
    keyword = _keyword_of(db, content)
    seo_vars = {**variables, "body": _body_variable(body.body)}
    if params["include_seo_meta"]:
        seo_tpl = pts.resolve_template(db, "seo_meta", project.id, project.language)
        seo, _ = _call_json(
            ctx, seo_tpl, seo_vars, lambda data: _normalize_seo(data, title=content.title, keyword=keyword.keyword if keyword else None),
        )
        _progress(ctx, 90)
    faq: list[dict[str, str]] | None = None
    if params["include_faq"] and params["faq_count"] > 0:
        faq_tpl = pts.resolve_template(db, "faq", project.id, project.language)
        faq, _ = _call_json(ctx, faq_tpl, seo_vars, lambda data: _normalize_faq(data, count=params["faq_count"]))
        _progress(ctx, 95)

    def _apply(c: ai_task_service.TaskContext) -> None:
        row = c.db.get(Content, content.id)
        if row is None:
            raise ZhiqiError(ErrorCategory.UNKNOWN, CONTENT_NOT_FOUND)
        changes: dict[str, Any] = {"body": body.body}
        if params["outline_first"]:
            changes["outline_json"] = outline
        if seo is not None:
            changes.update(seo)
        if faq is not None:
            changes["faq_json"] = faq or None
        write_version(
            c.db, row, changes, source="generate", actor_id=_actor(c.task, row), ai_task_id=body.attempt.id,
            template_id=body.template.id, model=body.attempt.model, truncated=body.truncated,
        )
        if row.status == "generating":
            transition(row, "generate_succeeded")

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=body.body[: gateway.OUTPUT_EXCERPT_LIMIT])


def run_content_outline(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``content_outline``：生成大纲（小节上限截断）→ 写版本 ``source=generate``，不改状态。"""
    db, task = ctx.db, ctx.task
    content, project = _load(ctx)
    if content is None or project is None:
        return _failed(CONTENT_NOT_FOUND)
    params = generation_params(db, content)
    template = generation_service.task_template(db, task, "outline")
    outline, attempt = _generate_outline(ctx, template, _base_variables(db, content, project, params), params["sections"])

    def _apply(c: ai_task_service.TaskContext) -> None:
        row = c.db.get(Content, content.id)
        if row is None:
            raise ZhiqiError(ErrorCategory.UNKNOWN, CONTENT_NOT_FOUND)
        write_version(
            c.db, row, {"outline_json": outline}, source="generate", actor_id=_actor(c.task, row), ai_task_id=attempt.id,
            template_id=template.id, model=attempt.model,
        )

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=_dumps(outline)[: gateway.OUTPUT_EXCERPT_LIMIT])


def run_content_body(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``content_body``：按大纲分段或整篇生成正文（参数取 ``generation_params_json``，``segmented`` 可被请求覆盖；无大纲自动整篇）
    → 写版本 ``source=generate`` → ``generating → ready``。"""
    db = ctx.db
    content, project = _load(ctx)
    if content is None or project is None:
        return _failed(CONTENT_NOT_FOUND)
    if content.status != "generating":
        return _failed("内容不处于生成中")
    params = generation_params(db, content)
    override = ctx.input.get("segmented")
    segmented = params["segmented"] if override is None else bool(override)
    outline = _outline_of(content) or None
    body = _generate_body(
        ctx, content, project, params, outline, segmented=segmented, variables=_base_variables(db, content, project, params),
        progress=(5, 95),
    )

    def _apply(c: ai_task_service.TaskContext) -> None:
        row = c.db.get(Content, content.id)
        if row is None:
            raise ZhiqiError(ErrorCategory.UNKNOWN, CONTENT_NOT_FOUND)
        write_version(
            c.db, row, {"body": body.body}, source="generate", actor_id=_actor(c.task, row), ai_task_id=body.attempt.id,
            template_id=body.template.id, model=body.attempt.model, truncated=body.truncated,
        )
        if row.status == "generating":
            transition(row, "generate_succeeded")

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=body.body[: gateway.OUTPUT_EXCERPT_LIMIT])


def run_content_seo(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``content_seo``：``seo_meta`` 与（``include_faq`` 时）``faq`` 各调用一次 → 写 ``summary`` / ``seo_title`` / ``seo_description`` /
    ``seo_keywords_json`` / ``faq_json`` 版本（``source=generate``），不改状态。"""
    db, task = ctx.db, ctx.task
    content, project = _load(ctx)
    if content is None or project is None:
        return _failed(CONTENT_NOT_FOUND)
    if not (content.body or "").strip():
        return _failed(MSG_BODY_EMPTY)
    params = generation_params(db, content)
    include_faq = params["include_faq"] if ctx.input.get("include_faq") is None else bool(ctx.input["include_faq"])
    keyword = _keyword_of(db, content)
    variables = {**_base_variables(db, content, project, params), "body": _body_variable(content.body)}
    seo_tpl = generation_service.task_template(db, task, "seo_meta")
    seo, attempt = _call_json(
        ctx, seo_tpl, variables, lambda data: _normalize_seo(data, title=content.title, keyword=keyword.keyword if keyword else None),
    )
    _progress(ctx, 50)
    faq: list[dict[str, str]] | None = None
    if include_faq and params["faq_count"] > 0:
        faq_tpl = pts.resolve_template(db, "faq", project.id, project.language)
        faq, attempt = _call_json(ctx, faq_tpl, variables, lambda data: _normalize_faq(data, count=params["faq_count"]))

    def _apply(c: ai_task_service.TaskContext) -> None:
        row = c.db.get(Content, content.id)
        if row is None:
            raise ZhiqiError(ErrorCategory.UNKNOWN, CONTENT_NOT_FOUND)
        changes: dict[str, Any] = dict(seo)
        if faq is not None:
            changes["faq_json"] = faq or None
        write_version(
            c.db, row, changes, source="generate", actor_id=_actor(c.task, row), ai_task_id=attempt.id, template_id=seo_tpl.id,
            model=attempt.model,
        )

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=_dumps(seo)[: gateway.OUTPUT_EXCERPT_LIMIT])


def run_content_rewrite(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """``content_rewrite``（§8.7）：变量 ``text`` 为全文或该小节文本，``target_word_count`` 按模式派生；结果经 §8.3 清理后
    ``scope=full`` 替换整篇、``scope=section`` 用 ``split_sections`` 定位并替换该小节（执行时定位不到 → ``failed(unknown)``、不写
    版本）；写版本 ``source=mode``、``change_summary=f"{mode}/{scope}[#{section_index}] {instruction[:200]}"``；``restyle`` 同步
    ``contents.style``；起始为 ``generating`` 时 → ``ready``，``approved`` / ``published`` 不改状态。"""
    db, task = ctx.db, ctx.task
    content, project = _load(ctx)
    if content is None or project is None:
        return _failed(CONTENT_NOT_FOUND)
    if content.status not in ("generating", *REWRITE_NO_CHANGE):
        return _failed("内容状态已变化，无法改写")
    mode = str(ctx.input.get("mode") or "rewrite")
    scope_kind = str(ctx.input.get("scope") or "full")
    section_index = int(ctx.input.get("section_index") or 0)
    style = ctx.input.get("style") if mode == "restyle" else None
    instruction = str(ctx.input.get("instruction") or "").strip()
    fmt = content.format or "markdown"
    original = content.body or ""
    if not original.strip():
        return _failed(MSG_BODY_EMPTY)
    text = original
    if scope_kind == "section":
        section = locate_section(original, fmt, _outline_of(content), section_index)
        if section is None:
            return _failed("正文中找不到要改写的小节")
        text = original[section.start:section.end].strip()
    params = generation_params(db, content)
    content_cfg = _gen_cfg(db).get("content") or {}
    target = _rewrite_target_words(
        mode, count_words(text, fmt), section=scope_kind == "section",
        min_words=int(content_cfg.get("min_word_count") or 300), max_words=int(content_cfg.get("max_word_count") or 6000),
    )
    template = generation_service.task_template(db, task, mode)
    variables = {
        **_base_variables(db, content, project, params), "text": text, "instruction": instruction,
        "style": style_text(style or content.style), "target_word_count": target,
    }
    result, attempt = _call_text(ctx, template, variables, dynamic={"max_tokens": _whole_max_tokens(target)})
    db.refresh(content)
    current = content.body or ""
    if scope_kind == "section":
        section = locate_section(current, fmt, _outline_of(content), section_index)
        if section is None:
            return _failed("正文已变更，找不到要改写的小节")
        new_body = replace_section(current, fmt, section, result.text)
    else:
        new_body = clean_output(result.text, fmt)
    summary = f"{mode}/{scope_kind}" + (f"#{section_index}" if scope_kind == "section" else "") + (f" {instruction[:200]}" if instruction else "")

    def _apply(c: ai_task_service.TaskContext) -> None:
        row = c.db.get(Content, content.id)
        if row is None:
            raise ZhiqiError(ErrorCategory.UNKNOWN, CONTENT_NOT_FOUND)
        if style:
            row.style = style
        write_version(
            c.db, row, {"body": new_body}, source=mode, actor_id=_actor(c.task, row), ai_task_id=attempt.id,
            template_id=template.id, model=attempt.model, change_summary=summary, truncated=result.finish_reason == "length",
        )
        if row.status == "generating":
            transition(row, "generate_succeeded")

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=new_body[: gateway.OUTPUT_EXCERPT_LIMIT])


# =====================================================================
# 失败 / 取消 / 重试钩子（docs/09 §9.4 第 4 步、§9.7）
# =====================================================================


def _restore_if_owner(db: Session, root: AiTask) -> None:
    """内容任务失败 / 取消：内容仍为 ``generating`` 且 ``contents.ai_task_id`` 指向该根任务时恢复 ``prev_status``。"""
    if root.target_type != "content" or not root.target_id:
        return
    content = db.get(Content, root.target_id)
    if content is None or content.status != "generating":
        return
    if content.ai_task_id not in (None, root.id):
        return
    transition(content, "restore_prev")


def _on_failed(db: Session, root: AiTask, category: str, message: str | None) -> None:
    del category, message
    _restore_if_owner(db, root)


def _on_cancelled(db: Session, root: AiTask) -> None:
    _restore_if_owner(db, root)


def _before_retry(db: Session, old: AiTask, new: AiTask) -> None:
    """重试新建根任务时（批次 retry / 单任务 retry / ``recover_stale_tasks`` ① 自动重试，同一事务）按原 ``operation`` 的起始状态
    规则重新校验（不满足 409 ``current_status``；同类非终态任务 409 ``existing_id``）；``content_generate`` / ``content_body`` /
    ``content_rewrite`` 把 ``contents.ai_task_id`` 指向新根任务，起始状态为 ``draft`` / ``ready`` / ``rejected`` 时 → ``generating``。"""
    del old
    if new.target_type != "content" or not new.target_id:
        return
    content = db.get(Content, new.target_id)
    if content is None:
        raise BusinessError(CONTENT_NOT_FOUND, code=CODE_NOT_FOUND, http_status=404)
    operation = new.operation
    has_body = bool((content.body or "").strip())
    if operation in ("content_generate", "content_body"):
        if content.status not in GENERATE_START:
            raise _status_conflict(content.status)
        transition(content, "generate")
    elif operation == "content_rewrite":
        if not has_body or content.status not in (*GENERATE_START, *REWRITE_NO_CHANGE):
            raise _status_conflict(content.status)
        if content.status in REWRITE_NO_CHANGE:
            existing = _active_same_operation(db, content.id, operation, exclude=new.id)
            if existing:
                raise _conflict(MSG_TASK_EXISTS, {"existing_id": existing})
        else:
            transition(content, "generate")
    elif operation in PENDING_TASK_OPERATIONS:
        if content.status not in SIDE_TASK_STATES or (operation == "content_seo" and not has_body):
            raise _status_conflict(content.status)
        existing = _active_same_operation(db, content.id, operation, exclude=new.id)
        if existing:
            raise _conflict(MSG_TASK_EXISTS, {"existing_id": existing})
    if operation in ACTIVE_TASK_OPERATIONS:
        content.ai_task_id = new.id
    db.flush()


_HANDLERS: dict[str, Callable[[ai_task_service.TaskContext], ai_task_service.TaskOutcome]] = {
    "content_generate": run_content_generate,
    "content_outline": run_content_outline,
    "content_body": run_content_body,
    "content_seo": run_content_seo,
    "content_rewrite": run_content_rewrite,
}
for _operation, _handler in _HANDLERS.items():
    ai_task_service.register_handler(
        _operation, _handler, on_failed=_on_failed, on_cancelled=_on_cancelled, before_retry=_before_retry,
    )
