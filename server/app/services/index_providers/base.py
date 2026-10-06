"""收录 / 引用检测提供器抽象（docs/11 §7.1、§7.4、§8.4）。

- ``CheckContext``：一次引擎检测的上下文（链接、引擎、标题 / 关键词、平台 code、``query_by``、触发方式，以及
  ``index_check_service`` 预先创建的同步根任务 ``root_task`` 与该引擎 / 提供器的配置段）；
- ``CheckResult``：提供器返回值，``index_check_service`` 据此写 ``index_checks`` 并回写 ``publish_links``（提供器**不写库**）；
- ``SeoProvider`` / ``GeoEngine``：协议；
- ``match_citations``：URL / 域名 / 标题近似三层命中规则（§8.4），供 SEO 的 ``zhiqi_web_search`` / ``baidu_ai_search`` 与
  GEO 引擎共用；共享域名平台（知乎 / CSDN / 头条…）的域名命中一律忽略并记 ``domain_match_ignored``；
- ``failure_result``：把网关 / 上游异常转为 ``status="unknown"`` + ``error_category``（提供器内部不得抛出未分类异常）。

抓取内容与模型回答都是不可信输入：这里只做 URL 提取与字符串比对，不把它们当指令使用（§14 第 5 条）。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from app.core import fingerprint, safe_fetch, urls
from app.core.exceptions import CODE_QUOTA_LIMIT_REACHED, BusinessError
from app.core.zhiqi.errors import ZhiqiError, sanitize_error_message
from app.core.zhiqi.types import Citation, ErrorCategory

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import AiTask, PublishLink

# 自有 / 非共享域名平台：只有它们的域名命中有效（§8.4）
OWN_DOMAIN_PLATFORMS = frozenset({"website", "other"})
ANSWER_EXCERPT_CHARS = 4000
SNIPPET_CHARS = 1000
TOOL_EVIDENCE_SOURCES = frozenset({"annotation", "mock"})

# error_message 固定取值（只写 error_message、不计 error_category 的为后四个）
MSG_CREDENTIAL_MISSING = "credential_missing"
MSG_NOT_OWN_SITE = "not_own_site"
MSG_EVIDENCE_UNVERIFIED = "evidence_unverified"
MSG_NO_SEARCH_EVIDENCE = "no_search_evidence"
MSG_TITLE_IN_PROMPT = "title_in_prompt"
MSG_LOCAL_QUOTA_LIMIT = "local_quota_limit"
MSG_MANUAL_ONLY = "manual_only"

SEO_ENGINE_NAMES: dict[str, str] = {"baidu": "百度", "bing": "必应 Bing", "google": "Google"}
POSITIVE_STATUS = {"seo": "indexed", "geo": "cited"}
NEGATIVE_STATUS = {"seo": "not_indexed", "geo": "not_cited"}

_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?；;\n]+|(?<=[.])\s+")
_TITLE_PLACEHOLDER_RE = re.compile(r"\{\{\s*title\s*\}\}")


@dataclass
class CheckContext:
    link: PublishLink
    kind: str                                         # "seo" / "geo"
    engine: str                                       # 引擎 code（baidu / doubao …）
    engine_name: str                                  # 展示名（SEO：百度 / 必应 Bing / Google；GEO：geo_engines.engines[].name）
    url: str
    normalized_url: str
    domain: str
    title: str                                        # contents.title（缺省 publish_links.title_snapshot）
    keyword: str | None                               # contents.keyword_id → keywords.keyword
    platform_code: str                                # 域名命中规则用（§8.4）
    query_by: list[str]                               # monitoring_config.index_check.query_by
    check_type: str                                   # scheduled / manual
    triggered_by: int | None
    project_id: int
    language: str | None = None                       # contents.language（模板语言解析）
    root_task: AiTask | None = None                   # zhiqi 类提供器 / GEO 引擎的同步根任务（index_check_service 预先创建）
    engine_config: dict[str, Any] = field(default_factory=dict)    # seo_providers.engines.<e> / geo_engines.engines[]
    provider_config: dict[str, Any] = field(default_factory=dict)  # seo_providers.providers.<provider>（GEO 为空）


@dataclass
class CheckResult:
    status: str                                       # SEO：indexed / not_indexed / unknown；GEO：cited / not_cited / unknown
    provider: str                                     # index_checks.provider
    match_mode: str = "none"                          # url / domain / title / none / manual
    query_text: str | None = None
    evidence_title: str | None = None
    evidence_snippet: str | None = None
    evidence_url: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)          # 写入 evidence_json（原始回答截断 4000 字符）
    confidence: Decimal | None = None
    ai_task_id: int | None = None
    request_id: str | None = None
    model: str | None = None
    duration_ms: int = 0
    error_category: str | None = None
    error_message: str | None = None


class SeoProvider(Protocol):
    code: str

    def check(self, db: Session, link: PublishLink, engine: str, query_by: list[str], *, ctx: CheckContext) -> CheckResult: ...

    def available(self) -> tuple[bool, str | None]: ...


class GeoEngine(Protocol):
    def precheck(self, db: Session, link: PublishLink, engine: dict, *, ctx: CheckContext) -> CheckResult | None: ...

    def check(self, db: Session, link: PublishLink, engine: dict, *, ctx: CheckContext) -> CheckResult: ...


# =====================================================================
# 命中规则（§8.4）
# =====================================================================


def _strip_scheme_slash(url: str) -> str:
    text = (url or "").strip()
    lowered = text.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.rstrip("/").lower()


def _syntax_ok(url: str) -> str | None:
    """引用 URL 先经 ``safe_fetch.normalize_public_url`` 语法校验（scheme / userinfo / 端口），非法返回 ``None``。"""
    try:
        return safe_fetch.normalize_public_url(url, allow_http=True)
    except safe_fetch.FetchError:
        return None
    except Exception:  # noqa: BLE001 - 任何解析异常都按非法 URL 跳过
        return None


def url_matches(candidate: str, link: Any) -> bool:
    """URL 命中：``url_hash(normalize_url(c)) == link.url_hash``，或二者去掉 scheme 与尾斜杠后相等。"""
    forms = {_strip_scheme_slash(candidate)}
    try:
        normalized = urls.normalize_url(candidate)
        if urls.url_hash(normalized) == link.url_hash:
            return True
        forms.add(_strip_scheme_slash(normalized))
    except Exception:  # noqa: BLE001 - normalize_url 对异常 URL 抛 InvalidURLError
        pass
    targets = {_strip_scheme_slash(link.normalized_url), _strip_scheme_slash(link.url)}
    return any(f and f in targets for f in forms)


def domain_of(url: str) -> str:
    try:
        return urls.extract_domain(url)
    except Exception:  # noqa: BLE001
        return ""


def title_similarity(text: str | None, title: str | None) -> float:
    """回答按句切分，``SequenceMatcher(None, normalize_title(title), normalize_title(sentence)).ratio()`` 的最大值
    （不设「规范化标题被回答包含即命中」的规则）。"""
    target = fingerprint.normalize_title(title or "")
    if not target or not text:
        return 0.0
    best = 0.0
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        candidate = fingerprint.normalize_title(sentence)
        if not candidate:
            continue
        ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
        if ratio > best:
            best = ratio
    return round(best, 3)


def match_citations(
    citations: Sequence[Citation],
    link: Any,
    mode: str,
    platform_code: str,
    *,
    notes: dict[str, Any] | None = None,
    answer: str | None = None,
    title: str | None = None,
    threshold: float = 0.8,
) -> tuple[str, Citation | None, Decimal]:
    """返回 ``(match_mode, citation, confidence)``（docs/11 §8.4）。

    - ``mode=url`` 只检查 URL 层，``domain`` 只检查域名层，``url_or_domain`` 先 URL 后域名，三者无命中返回 ``("none", None, 0)``，
      不回退标题近似；
    - ``mode=title`` 只做标题近似（``answer`` 按句与 ``title`` 比较，``>= threshold`` 命中，``confidence`` = 相似度）；
    - 域名命中只在 ``platform_code ∈ {website, other}`` 时有效；共享域名平台的同域引用写 ``notes["domain_match_ignored"]=True``；
    - 引用 URL 先经 ``normalize_public_url`` 语法校验，非法跳过。
    """
    notes = notes if notes is not None else {}
    notes.setdefault("domain_match_ignored", False)
    if mode == "title":
        similarity = title_similarity(answer, title)
        notes["title_similarity"] = similarity
        if similarity >= float(threshold) and similarity > 0:
            return "title", None, Decimal(str(similarity))
        return "none", None, Decimal(0)
    check_url = mode in ("url", "url_or_domain")
    check_domain = mode in ("domain", "url_or_domain")
    valid = [(c, n) for c in citations if (n := _syntax_ok(c.url)) is not None]
    if check_url:
        for citation, normalized in valid:
            if url_matches(normalized, link) or url_matches(citation.url, link):
                return "url", citation, Decimal("1.0")
    if check_domain:
        own = platform_code in OWN_DOMAIN_PLATFORMS
        for citation, normalized in valid:
            if domain_of(normalized) and domain_of(normalized) == link.domain:
                if own:
                    return "domain", citation, Decimal("0.6")
                notes["domain_match_ignored"] = True
    return "none", None, Decimal(0)


# =====================================================================
# 证据与文本工具
# =====================================================================


def citation_dict(c: Citation) -> dict[str, Any]:
    return {"url": c.url, "title": c.title, "snippet": c.snippet, "source": c.source}


def excerpt(text: str | None, limit: int = ANSWER_EXCERPT_CHARS) -> str:
    return (text or "")[:limit]


def sentence_with(text: str | None, needle: str | None, limit: int = SNIPPET_CHARS) -> str | None:
    """回答中含 ``needle``（URL）的句子（≤ ``limit`` 字符）。"""
    if not text or not needle:
        return None
    for line in re.split(r"[\n。！？]+", text):
        if needle in line:
            return line.strip()[:limit] or None
    return None


def matched_evidence(match_mode: str, citation: Citation | None) -> dict[str, Any] | None:
    if match_mode in ("url", "domain") and citation is not None:
        return {"mode": match_mode, "candidate": citation.url}
    if match_mode == "title":
        return {"mode": "title", "candidate": None}
    return None


def is_mock_response(raw: Any) -> bool:
    return isinstance(raw, Mapping) and raw.get("mock") is True


def has_title_placeholder(text: str | None) -> bool:
    return bool(_TITLE_PLACEHOLDER_RE.search(text or ""))


# =====================================================================
# 异常 → unknown
# =====================================================================


def classify_exception(exc: BaseException) -> tuple[str, str]:
    """网关 / 上游异常 → ``(error_category, error_message)``：``ZhiqiError`` 取其分类；``BusinessError``（5021 / 5031）取附带的
    ``error_category``，本地额度 4291 → ``quota_exceeded`` + ``local_quota_limit``；其它 → ``unknown``。"""
    if isinstance(exc, ZhiqiError):
        category = exc.category.value if isinstance(exc.category, ErrorCategory) else str(exc.category)
        return category, sanitize_error_message(exc.message) or category
    if isinstance(exc, BusinessError):
        if exc.code == CODE_QUOTA_LIMIT_REACHED:
            return ErrorCategory.QUOTA_EXCEEDED.value, MSG_LOCAL_QUOTA_LIMIT
        data = exc.data if isinstance(exc.data, Mapping) else {}
        category = getattr(exc, "error_category", None) or data.get("error_category") or ErrorCategory.UNKNOWN.value
        message = getattr(exc, "error_message", None) or exc.message
        return str(category), sanitize_error_message(str(message)) or str(category)
    name = type(exc).__name__
    if name == "TaskAbandoned":
        return ErrorCategory.TIMEOUT.value, sanitize_error_message(str(exc)) or "task_abandoned"
    text = str(exc).replace("\n", " ").strip()
    return ErrorCategory.UNKNOWN.value, sanitize_error_message(f"{name}: {text}" if text else name) or name


def failure_result(provider: str, exc: BaseException, *, query_text: str | None = None, evidence: dict[str, Any] | None = None,
                   duration_ms: int = 0, model: str | None = None) -> CheckResult:
    category, message = classify_exception(exc)
    request_id = getattr(exc, "request_id", None)
    return CheckResult(
        status="unknown", provider=provider, query_text=query_text, evidence=dict(evidence or {}), duration_ms=duration_ms,
        error_category=category, error_message=message, request_id=request_id if isinstance(request_id, str) else None,
        model=model,
    )


def unique_citations(items: Iterable[Citation]) -> list[Citation]:
    seen: set[str] = set()
    result: list[Citation] = []
    for c in items:
        if c.url and c.url not in seen:
            seen.add(c.url)
            result.append(c)
    return result


__all__ = [
    "ANSWER_EXCERPT_CHARS",
    "MSG_CREDENTIAL_MISSING",
    "MSG_EVIDENCE_UNVERIFIED",
    "MSG_LOCAL_QUOTA_LIMIT",
    "MSG_MANUAL_ONLY",
    "MSG_NOT_OWN_SITE",
    "MSG_NO_SEARCH_EVIDENCE",
    "MSG_TITLE_IN_PROMPT",
    "NEGATIVE_STATUS",
    "OWN_DOMAIN_PLATFORMS",
    "POSITIVE_STATUS",
    "SEO_ENGINE_NAMES",
    "TOOL_EVIDENCE_SOURCES",
    "CheckContext",
    "CheckResult",
    "GeoEngine",
    "SeoProvider",
    "citation_dict",
    "classify_exception",
    "domain_of",
    "excerpt",
    "failure_result",
    "has_title_placeholder",
    "is_mock_response",
    "match_citations",
    "matched_evidence",
    "sentence_with",
    "title_similarity",
    "unique_citations",
    "url_matches",
]
