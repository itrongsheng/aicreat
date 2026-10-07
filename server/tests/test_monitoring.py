"""SEO 收录 / GEO 引用检测与监控接口测试（docs/11 §16.1、§16.2 中收录检测相关部分；docs/13 §16 监控隔离）。

- 单元：``match_citations``（URL / 共享域名忽略 / 自有域名 / 标题近似阈值 / 不回退标题 / 非法 URL）、``extract_citations``、
  ``zhiqi_web_search`` 八种判定情形、``geo_engine``（无引用 → ``no_search_evidence``、``not_cited``、标题模式、``precheck``）、
  非 zhiqi 提供器（``credential_missing`` / ``not_own_site`` / HTTP 错误映射，``httpx.MockTransport``，不访问网络）、人工标记；
- 执行与写回：Mock zhiqi 命中 / 未命中（``source=mock``、根任务 + 尝试行、``ai_task_id`` 指向尝试行）、``unknown`` 不覆盖、
  ``*_any`` 重算、``dropped``、``scheduled`` 推进计数而 ``manual`` 不推进、全局暂停、熔断、``title_in_prompt``、本地额度；
- 排程：``schedule_index_checks`` 只写到期引擎、日上限预扣与次日顺延、去重标记、锁竞争不重入队、``drain`` 全流程；
- 接口：``index-check`` / ``mark-index`` 规则、监控概览与记录的数据范围、``run`` 的 ``owner_id`` 用户视角、设置保存后的排程重算、
  ``index_overdue`` 前置数据（``index_checks_done >= 3``、``seo_indexed_any=0``）；
- 告警（docs/11 §10、§16.1、§16.2）：``evaluate_alerts`` ①~⑤（``index_overdue`` 触发 / 不重复 / 自动解决、``ai_task_failures`` 连续失败
  与成功后解决、``ai_breaker_open`` 兜底、``worker_stale`` 副本级 / 进程级与恢复、``link_deleted`` 兜底解决、锁互斥）、webhook 签名 /
  ``min_severity`` / 失败容忍、邮件通道（SMTP 假实现）、``/admin/alerts`` 接口（筛选、摘要 ``today_*`` 时区、处理动作、批量解决、
  数据范围、``owner_id`` 用户视角、权限与审计）。
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import urls
from app.core.config import settings
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.core.zhiqi import mock as zhiqi_mock
from app.core.zhiqi import text as zhiqi_text
from app.core.zhiqi.types import Citation
from app.models import (
    AdminOperationLog,
    AiTask,
    Alert,
    Content,
    IndexCheck,
    Keyword,
    LinkCheck,
    Project,
    PromptTemplate,
    PublishLink,
    PublishPlatform,
    utcnow,
)
from app.services import (
    ai_gateway_service,
    alert_service,
    index_check_service,
    link_service,
    settings_service,
    stats_service,
)
from app.services.data_scope_service import SYSTEM_SCOPE, DataScope
from app.services.index_providers import get_geo_engine, get_seo_provider, http as provider_http
from app.services.index_providers.base import CheckContext, CheckResult, match_citations, title_similarity
from app.services.index_providers.zhiqi_web_search import judge_answer
from app.tasks import evaluate_alerts, run_index_checks, schedule_index_checks
from tests.conftest import ADMIN_API, User, UserFactory, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")

LINKS = f"{ADMIN_API}/links"
MONITORING = f"{ADMIN_API}/monitoring"
INDEX_CFG = settings_service.DEFAULT_SETTINGS["monitoring_config"]["index_check"]
SEO_MOCK_ENGINES = ["baidu", "bing"]
GEO_MOCK_ENGINES = ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"]


# =====================================================================
# 夹具与助手
# =====================================================================


@pytest.fixture
def owner_a(users: UserFactory) -> User:
    return users.create("operator", username="mon_owner_a", display_name="运营 A")


@pytest.fixture
def owner_b(users: UserFactory) -> User:
    return users.create("operator", username="mon_owner_b", display_name="运营 B")


@pytest.fixture
def platforms(db: Session) -> dict[str, PublishPlatform]:
    from seeds.seed import seed_publish_platforms

    seed_publish_platforms(db)
    return {p.code: p for p in db.scalars(select(PublishPlatform)).all()}


@pytest.fixture
def env(db: Session, system_templates: dict[str, Any], platforms: dict[str, PublishPlatform]) -> dict[str, Any]:
    """系统模板（含 sys_seo_query / sys_geo_query）+ 默认能力路由（Mock 模型）+ 8 个平台。"""
    ai_gateway_service.ensure_default_routes(db)
    return {"templates": system_templates, "platforms": platforms}


@pytest.fixture
def always_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zhiqi_mock, "HIT_PROBABILITY", 1.0)


@pytest.fixture
def never_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zhiqi_mock, "HIT_PROBABILITY", 0.0)


def make_project(db: Session, owner: User, name: str = "监控项目 A", slug: str = "mon-a") -> Project:
    project = Project(name=name, slug=slug, owner_id=owner.id, created_by=owner.id, status="active")
    db.add(project)
    db.commit()
    return project


def make_content(db: Session, project: Project, owner: User, *, title: str = "智能门锁怎么选", keyword: str | None = "智能门锁") -> Content:
    keyword_id = None
    if keyword:
        row = Keyword(project_id=project.id, keyword=keyword, normalized_keyword=keyword, language="zh-CN", status="adopted", source="manual",
                      created_by=owner.id)
        db.add(row)
        db.flush()
        keyword_id = row.id
    content = Content(project_id=project.id, title=title, language="zh-CN", style="news", status="published", body="正文",
                      keyword_id=keyword_id, created_by=owner.id, updated_by=owner.id, link_count=1)
    db.add(content)
    db.commit()
    return content


def make_link(
    db: Session,
    content: Content,
    platform: PublishPlatform,
    url: str,
    *,
    days_ago: float = 2,
    alive_status: str = "alive",
    is_monitoring: bool = True,
    created_days_ago: float | None = None,
) -> PublishLink:
    now = utcnow()
    published = (now - timedelta(days=days_ago)).replace(microsecond=0)
    normalized = urls.normalize_url(url)
    link = PublishLink(
        project_id=content.project_id, content_id=content.id, platform_id=platform.id, url=url, normalized_url=normalized,
        url_hash=urls.url_hash(normalized), domain=urls.extract_domain(normalized), published_at=published,
        backfilled_by=content.created_by, title_snapshot=content.title, alive_status=alive_status, is_monitoring=is_monitoring,
        index_check_count=link_service.expired_rounds(published, now, INDEX_CFG["schedule_days"]), index_checks_done=0,
        check_count=1,
    )
    if created_days_ago is not None:
        link.created_at = now - timedelta(days=created_days_ago)
    link.next_index_check_at = link_service.compute_next_index_check_at(link, now=now, db=db)
    db.add(link)
    db.commit()
    return link


@pytest.fixture
def setup_a(db: Session, env: dict[str, Any], owner_a: User) -> dict[str, Any]:
    project = make_project(db, owner_a)
    content = make_content(db, project, owner_a)
    link = make_link(db, content, env["platforms"]["zhihu"], "https://zhuanlan.zhihu.com/p/1001?utm_source=wechat")
    return {"project": project, "content": content, "link": link}


@pytest.fixture
def setup_b(db: Session, env: dict[str, Any], owner_b: User) -> dict[str, Any]:
    project = make_project(db, owner_b, name="监控项目 B", slug="mon-b")
    content = make_content(db, project, owner_b, title="空气净化器推荐", keyword="空气净化器")
    link = make_link(db, content, env["platforms"]["csdn"], "https://blog.csdn.net/u/article/details/2002")
    return {"project": project, "content": content, "link": link}


def ctx_for(link: Any, *, kind: str = "geo", engine: str = "doubao", platform_code: str = "zhihu", title: str = "智能门锁怎么选",
            keyword: str | None = "智能门锁", provider_config: dict | None = None, engine_config: dict | None = None) -> CheckContext:
    return CheckContext(
        link=link, kind=kind, engine=engine, engine_name=engine, url=link.url, normalized_url=link.normalized_url,
        domain=link.domain, title=title, keyword=keyword, platform_code=platform_code, query_by=["url", "title"],
        check_type="scheduled", triggered_by=None, project_id=getattr(link, "project_id", 0) or 0, language="zh-CN",
        engine_config=dict(engine_config or {}), provider_config=dict(provider_config or {}),
    )


class FakeLink:
    """只含命中规则用到的字段。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.normalized_url = urls.normalize_url(url)
        self.url_hash = urls.url_hash(self.normalized_url)
        self.domain = urls.extract_domain(self.normalized_url)
        self.project_id = 0


def states(link: PublishLink, kind: str) -> dict[str, Any]:
    raw = link.seo_status_json if kind == "seo" else link.geo_status_json
    return json.loads(raw) if raw else {}


def run_link(db: Session, link: PublishLink, *, kinds: tuple[str, ...] = ("seo", "geo"), engines: list[str] | None = None,
             check_type: str = "scheduled", triggered_by: int | None = None) -> list[IndexCheck]:
    engines = engines if engines is not None else [*SEO_MOCK_ENGINES, *GEO_MOCK_ENGINES]
    return index_check_service.run(db, SYSTEM_SCOPE, db.get(PublishLink, link.id), list(kinds), engines, check_type, triggered_by)


def err(response: Any, status: int, code: int | None = None) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == (code if code is not None else status), body
    return body


def queue_items() -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in redis_client.lrange(index_check_service.QUEUE_INDEX_CHECKS, 0, -1)]


# =====================================================================
# 单元：命中规则与引用提取（§8.4）
# =====================================================================


def test_match_citations_url_layer_normalizes_tracking_and_trailing_slash() -> None:
    link = FakeLink("https://zhuanlan.zhihu.com/p/1001")
    cites = [Citation(url="https://example.com/a"), Citation(url="http://zhuanlan.zhihu.com/p/1001/?utm_source=x#top")]
    mode, cite, conf = match_citations(cites, link, "url_or_domain", "zhihu")
    assert (mode, cite.url, conf) == ("url", cites[1].url, Decimal("1.0"))


def test_match_citations_shared_domain_ignored_and_website_domain_hit() -> None:
    link = FakeLink("https://zhuanlan.zhihu.com/p/1001")
    other_article = [Citation(url="https://zhuanlan.zhihu.com/p/9999")]
    notes: dict[str, Any] = {}
    assert match_citations(other_article, link, "url_or_domain", "zhihu", notes=notes)[0] == "none"
    assert notes["domain_match_ignored"] is True
    own = FakeLink("https://www.brand.example.com/news/1")
    notes = {}
    mode, cite, conf = match_citations([Citation(url="https://brand.example.com/about")], own, "url_or_domain", "website", notes=notes)
    assert (mode, conf, notes["domain_match_ignored"]) == ("domain", Decimal("0.6"), False)
    # mode=url 只看 URL 层；mode=domain 只看域名层
    assert match_citations([Citation(url="https://brand.example.com/about")], own, "url", "website")[0] == "none"
    assert match_citations([Citation(url=own.url)], own, "domain", "website")[0] == "domain"


def test_match_citations_never_falls_back_to_title_and_skips_invalid_urls() -> None:
    link = FakeLink("https://zhuanlan.zhihu.com/p/1001")
    answer = "推荐阅读：智能门锁怎么选。"
    for mode in ("url", "domain", "url_or_domain"):
        assert match_citations([], link, mode, "zhihu", answer=answer, title="智能门锁怎么选")[0] == "none"
    invalid = [Citation(url="https://user:pw@zhuanlan.zhihu.com/p/1001"), Citation(url="https://zhuanlan.zhihu.com:8443/p/1001")]
    assert match_citations(invalid, link, "url_or_domain", "zhihu")[0] == "none"


def test_title_mode_threshold_and_no_containment_rule() -> None:
    link = FakeLink("https://zhuanlan.zhihu.com/p/1001")
    notes: dict[str, Any] = {}
    mode, cite, conf = match_citations([], link, "title", "zhihu", notes=notes, answer="今天聊聊。智能门锁怎么选？\n其它内容",
                                       title="智能门锁怎么选 - 知乎", threshold=0.8)
    assert mode == "title" and cite is None and conf == Decimal("1.0") and notes["title_similarity"] == 1.0
    long_sentence = "关于智能门锁怎么选这个问题我们从锁芯等级指纹识别速度电池续航售后服务价格区间安装方式等很多维度来展开详细的说明"
    assert title_similarity(long_sentence, "智能门锁怎么选") < 0.8          # 只被包含不算命中
    assert match_citations([], link, "title", "zhihu", answer=long_sentence, title="智能门锁怎么选", threshold=0.8)[0] == "none"


def test_extract_citations_sources_and_dedupe() -> None:
    raw = {"choices": [{"message": {"annotations": [
        {"type": "url_citation", "url_citation": {"url": "https://a.example.com/1", "title": "A"}},
        {"type": "url_citation", "url_citation": {"url": "https://a.example.com/1", "title": "A2"}},
    ]}}]}
    cites = zhiqi_text.extract_citations("正文 https://b.example.com/x", raw)
    assert [(c.url, c.source) for c in cites] == [("https://a.example.com/1", "annotation")]
    text = "见 [文章](https://c.example.com/p) 与 https://d.example.com/q。以及 https://c.example.com/p"
    cites = zhiqi_text.extract_citations(text, {})
    assert [(c.url, c.source) for c in cites] == [("https://c.example.com/p", "markdown_link"), ("https://d.example.com/q", "plain_url")]
    assert {c.source for c in zhiqi_text.extract_citations(text, {"mock": True})} == {"mock"}


# =====================================================================
# 单元：zhiqi_web_search 判定表（§7.3）
# =====================================================================

TARGET = "https://zhuanlan.zhihu.com/p/1001"


def _judge(answer: str, citations: list[Citation], platform: str = "zhihu") -> dict[str, Any]:
    return judge_answer(answer, citations, FakeLink(TARGET), mode="url_or_domain", platform_code=platform)


@pytest.mark.parametrize(
    ("answer", "citations", "status", "match_mode", "confidence", "error_message"),
    [
        # 工具证据命中 + JSON indexed=true
        (json.dumps({"indexed": True, "evidence": [{"url": TARGET}]}), [Citation(url=TARGET)], "indexed", "url", Decimal("1.0"), None),
        # 工具证据命中 + 非 JSON
        ("检索结果中出现了该页面，确认已收录。", [Citation(url=TARGET)], "indexed", "url", Decimal("0.9"), None),
        # 工具证据命中 + indexed=false
        (json.dumps({"indexed": False, "evidence": []}), [Citation(url=TARGET)], "indexed", "url", Decimal("0.9"), None),
        # 工具证据未命中，URL 只出现在 JSON evidence（自述）→ evidence_unverified
        (json.dumps({"indexed": True, "evidence": [{"url": TARGET}]}), [Citation(url="https://x.example.com/1")], "unknown", "none",
         Decimal("0.3"), "evidence_unverified"),
        # 无任何工具证据，提示词回显 URL → evidence_unverified
        (f"未联网，无法核查 URL：{TARGET} 是否收录。", [], "unknown", "none", Decimal("0.3"), "evidence_unverified"),
        # indexed=true 但无任何命中
        (json.dumps({"indexed": True, "evidence": []}), [Citation(url="https://x.example.com/1")], "unknown", "none", Decimal("0.3"),
         "evidence_unverified"),
        # 无检索痕迹 + indexed=false → no_search_evidence
        (json.dumps({"indexed": False, "evidence": []}), [], "unknown", "none", None, "no_search_evidence"),
        # 有工具证据但未命中 + JSON indexed=false
        (json.dumps({"indexed": False, "evidence": []}), [Citation(url="https://x.example.com/1")], "not_indexed", "none",
         Decimal("0.8"), None),
        # 有工具证据但未命中 + 非 JSON
        ("检索结果中没有找到该页面，判断为未收录。", [Citation(url="https://x.example.com/1")], "not_indexed", "none", Decimal("0.5"), None),
    ],
)
def test_zhiqi_web_search_judgement_table(answer: str, citations: list[Citation], status: str, match_mode: str,
                                          confidence: Decimal | None, error_message: str | None) -> None:
    verdict = _judge(answer, citations)
    assert (verdict["status"], verdict["match_mode"], verdict["confidence"], verdict["error_message"]) == (
        status, match_mode, confidence, error_message)
    assert verdict["error_category"] is None


def test_zhiqi_web_search_empty_answer_is_invalid_response() -> None:
    verdict = _judge("  {}  ", [Citation(url=TARGET)])
    assert (verdict["status"], verdict["error_category"]) == ("unknown", "invalid_response")


def test_zhiqi_web_search_shared_domain_tool_evidence_is_not_a_hit() -> None:
    verdict = _judge(json.dumps({"indexed": False, "evidence": []}), [Citation(url="https://zhuanlan.zhihu.com/p/77")])
    assert verdict["status"] == "not_indexed" and verdict["notes"]["domain_match_ignored"] is True


# =====================================================================
# 单元：非 zhiqi 提供器与人工标记（§7.2）
# =====================================================================


def test_non_zhiqi_providers_credential_missing_in_mock_mode() -> None:
    link = FakeLink("https://www.brand.example.com/news/1")
    for code, engine in (("baidu_ai_search", "baidu"), ("bing_webmaster", "bing"), ("google_search_console", "google")):
        provider = get_seo_provider(code)
        assert provider.available() == (False, "mock_mode")
        result = provider.check(None, link, engine, ["url", "title"], ctx=ctx_for(link, kind="seo", engine=engine))  # type: ignore[arg-type]
        assert (result.status, result.error_category, result.error_message) == ("unknown", "auth_failed", "credential_missing")
        assert result.query_text is None and result.model is None and result.request_id is None and result.ai_task_id is None
    with pytest.raises(KeyError):
        get_seo_provider("serp_scraper")


@pytest.fixture
def live_mode(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """真实模式（只影响提供器的可用性判定；本组测试不调用 zhiqiapi）。"""
    monkeypatch.setattr(settings, "zhiqi_api_key", "sk-test-not-used")
    return monkeypatch


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(provider_http, "client_factory", lambda timeout: httpx.Client(transport=httpx.MockTransport(_handler)))
    return seen


def test_baidu_ai_search_live_hit_miss_and_error_mapping(live_mode: pytest.MonkeyPatch) -> None:
    link = FakeLink("https://zhuanlan.zhihu.com/p/1001")
    ctx = ctx_for(link, kind="seo", engine="baidu", provider_config={"endpoint": "https://qianfan.example.com/v2/ai_search/web_search"})
    provider = get_seo_provider("baidu_ai_search")
    assert provider.check(None, link, "baidu", ["url", "title"], ctx=ctx).error_message == "credential_missing"  # type: ignore[arg-type]
    live_mode.setattr(settings, "seo_baidu_ai_search_api_key", "bce-key")
    assert provider.available() == (True, None)

    def refs(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query = body["messages"][0]["content"]
        assert request.headers["Authorization"] == "Bearer bce-key"
        url = TARGET if query == "智能门锁怎么选" else "https://other.example.com/x"
        return httpx.Response(200, json={"references": [{"url": url, "title": "智能门锁怎么选", "content": "摘要"}]})

    seen = mock_http(live_mode, refs)
    result = provider.check(None, link, "baidu", ["url", "title"], ctx=ctx)  # type: ignore[arg-type]
    assert (result.status, result.match_mode, result.evidence_url) == ("indexed", "url", TARGET)
    assert result.query_text == f"{link.url} | 智能门锁怎么选" and len(seen) == 2
    assert result.model is None and result.request_id is None and result.ai_task_id is None

    mock_http(live_mode, lambda r: httpx.Response(200, json={"references": [{"url": "https://zhuanlan.zhihu.com/p/5"}]}))
    result = provider.check(None, link, "baidu", ["url", "title"], ctx=ctx)  # type: ignore[arg-type]
    assert result.status == "not_indexed" and result.evidence["domain_match_ignored"] is True

    for status, category in ((401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited"), (503, "upstream_unavailable")):
        mock_http(live_mode, lambda r, s=status: httpx.Response(s, text="secret body"))
        result = provider.check(None, link, "baidu", ["url"], ctx=ctx)  # type: ignore[arg-type]
        assert (result.status, result.error_category) == ("unknown", category)
        assert "secret body" not in (result.error_message or "")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    mock_http(live_mode, timeout)
    assert provider.check(None, link, "baidu", ["url"], ctx=ctx).error_category == "timeout"  # type: ignore[arg-type]


def test_bing_webmaster_not_own_site_and_indexed(live_mode: pytest.MonkeyPatch) -> None:
    provider = get_seo_provider("bing_webmaster")
    live_mode.setattr(settings, "seo_bing_webmaster_api_key", "bing-key")
    own = FakeLink("https://blog.brand.example.com/post/1")
    assert provider.check(None, own, "bing", [], ctx=ctx_for(own, kind="seo")).error_message == "credential_missing"  # type: ignore[arg-type]
    live_mode.setattr(settings, "seo_bing_site_url", "https://www.brand.example.com/")
    other = FakeLink("https://zhuanlan.zhihu.com/p/1")
    result = provider.check(None, other, "bing", [], ctx=ctx_for(other, kind="seo"))  # type: ignore[arg-type]
    assert (result.status, result.error_category, result.error_message, result.query_text) == ("unknown", None, "not_own_site", None)

    def info(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == "bing-key" and request.url.params["url"] == own.url
        return httpx.Response(200, json={"d": {"Url": own.url, "HttpStatus": 200, "IsPage": True,
                                               "LastCrawledDate": "/Date(1759708800000)/"}})

    mock_http(live_mode, info)
    result = provider.check(None, own, "bing", [], ctx=ctx_for(own, kind="seo"))  # type: ignore[arg-type]
    assert (result.status, result.query_text, result.model) == ("indexed", None, None)
    mock_http(live_mode, lambda r: httpx.Response(200, json={"d": {"Url": own.url, "LastCrawledDate": "/Date(-62135596800000)/"}}))
    assert provider.check(None, own, "bing", [], ctx=ctx_for(own, kind="seo")).status == "not_indexed"  # type: ignore[arg-type]


def test_google_search_console_token_flow_and_verdict(live_mode: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app.services.index_providers import google_search_console

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    cred = tmp_path / "sa.json"
    cred.write_text(json.dumps({"type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
                                "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token"}))
    live_mode.setattr(settings, "seo_gsc_credentials_file", str(cred))
    live_mode.setattr(settings, "seo_gsc_site_url", "sc-domain:brand.example.com")
    google_search_console.clear_token_cache()
    provider = get_seo_provider("google_search_console")
    other = FakeLink("https://www.other.example.org/a")
    assert provider.check(None, other, "google", [], ctx=ctx_for(other, kind="seo")).error_message == "not_own_site"  # type: ignore[arg-type]

    own = FakeLink("https://www.brand.example.com/a")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            assert b"jwt-bearer" in request.content
            return httpx.Response(200, json={"access_token": "ya29.token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer ya29.token"
        assert json.loads(request.content) == {"inspectionUrl": own.url, "siteUrl": "sc-domain:brand.example.com"}
        return httpx.Response(200, json={"inspectionResult": {"indexStatusResult": {"verdict": "PASS",
                                                                                     "coverageState": "Submitted and indexed"}}})

    seen = mock_http(live_mode, handler)
    result = provider.check(None, own, "google", [], ctx=ctx_for(own, kind="seo"))  # type: ignore[arg-type]
    assert result.status == "indexed" and result.evidence["index_status"]["verdict"] == "PASS" and len(seen) == 2
    mock_http(live_mode, lambda r: httpx.Response(200, json={"inspectionResult": {"indexStatusResult": {
        "verdict": "NEUTRAL", "coverageState": "Crawled - currently not indexed"}}}))
    assert provider.check(None, own, "google", [], ctx=ctx_for(own, kind="seo")).status == "not_indexed"  # type: ignore[arg-type]
    mock_http(live_mode, lambda r: httpx.Response(429, json={"error": {"message": "Quota exceeded"}}))
    assert provider.check(None, own, "google", [], ctx=ctx_for(own, kind="seo")).error_category == "rate_limited"  # type: ignore[arg-type]
    google_search_console.clear_token_cache()


def test_manual_provider_result_fixed_shape() -> None:
    from app.services.index_providers.manual import ManualProvider

    result = ManualProvider.result("indexed", note="站长后台确认", evidence_url="https://www.google.com/search?q=x")
    assert (result.provider, result.match_mode, result.confidence, result.duration_ms) == ("manual", "manual", Decimal(1), 0)
    assert result.query_text is None and result.model is None and result.request_id is None
    assert result.evidence == {"note": "站长后台确认", "source": "manual"}


# =====================================================================
# 单元：GEO 引擎（§8.2）
# =====================================================================


def _fake_text(answer: str, raw: dict | None = None) -> Any:
    from app.core.zhiqi.types import TextResult

    raw = raw or {}
    return TextResult(text=answer, model="mock-text", request_id="req-1", citations=zhiqi_text.extract_citations(answer, raw), raw=raw)


@pytest.fixture
def fake_complete(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """替换 complete_text：返回预设回答（不建尝试行）。"""
    holder: dict[str, Any] = {"answer": "", "raw": {}, "calls": []}

    def _complete(db: Session, **kwargs: Any) -> Any:
        holder["calls"].append(kwargs)
        return _fake_text(holder["answer"], holder["raw"]), type("Attempt", (), {"id": 777, "model": "mock-text"})()

    monkeypatch.setattr(ai_gateway_service, "complete_text", _complete)
    return holder


def test_geo_engine_judgement_without_and_with_citations(db: Session, env: dict[str, Any], setup_a: dict[str, Any],
                                                          fake_complete: dict[str, Any]) -> None:
    link = setup_a["link"]
    engine = {"code": "doubao", "model": "", "protocol": "openai_chat", "prompt_template_code": "sys_geo_query", "extra": {},
              "parse": {"match_mode": "url_or_domain", "title_fuzzy_threshold": 0.8}, "timeout_seconds": 120}
    ctx = ctx_for(link, engine_config=engine)
    ctx.root_task = AiTask(id=1)
    geo = get_geo_engine("doubao")
    assert geo.precheck(db, link, engine, ctx=ctx) is None

    fake_complete["answer"] = "智能门锁需要关注锁芯等级与指纹识别速度，这里没有给出任何来源。"
    result = geo.check(db, link, engine, ctx=ctx)
    assert (result.status, result.error_message, result.error_category) == ("unknown", "no_search_evidence", None)
    call = fake_complete["calls"][-1]
    assert call["model_override"] is None and call["protocol_override"] == "openai_chat" and call["timeout_override"] == 120.0
    assert "智能门锁" in call["messages"][-1]["content"] and link.url not in call["messages"][-1]["content"]

    fake_complete["answer"] = "参考：[知乎专栏](https://zhuanlan.zhihu.com/p/42) 与 [官网](https://brand.example.com/a)"
    result = geo.check(db, link, engine, ctx=ctx)
    assert (result.status, result.match_mode) == ("not_cited", "none")
    assert result.evidence["domain_match_ignored"] is True and result.ai_task_id == 777

    fake_complete["answer"] = f"推荐阅读[智能门锁怎么选]({link.url}) 这篇文章。"
    result = geo.check(db, link, engine, ctx=ctx)
    assert (result.status, result.match_mode, result.confidence, result.evidence_url) == ("cited", "url", Decimal("1.0"), link.url)
    assert result.evidence_snippet and link.url in result.evidence_snippet
    assert result.evidence["source"] == "zhiqi_model" and result.evidence["citations"][0]["source"] == "markdown_link"


def test_geo_engine_title_mode_and_precheck(db: Session, env: dict[str, Any], setup_a: dict[str, Any], owner_a: User,
                                            fake_complete: dict[str, Any]) -> None:
    link = setup_a["link"]
    engine = {"code": "kimi", "model": "", "protocol": "openai_chat", "prompt_template_code": "sys_geo_query", "extra": {},
              "parse": {"match_mode": "title", "title_fuzzy_threshold": 0.8}, "timeout_seconds": 60}
    geo = get_geo_engine("kimi")
    # 默认 sys_geo_query 的 user_prompt 含 {{title}} → title_in_prompt
    pre = geo.precheck(db, link, engine, ctx=ctx_for(link, engine="kimi"))
    assert pre is not None and (pre.status, pre.error_message, pre.provider) == ("unknown", "title_in_prompt", "zhiqi_model")
    # keyword 为空同样拦截
    pre = geo.precheck(db, link, engine, ctx=ctx_for(link, engine="kimi", keyword=None))
    assert pre is not None and pre.error_message == "title_in_prompt"
    # 换用不含 {{title}} 的全局已发布模板后放行，并按句相似度判定
    tpl = PromptTemplate(code="geo_kw_only", version=1, kind="geo_query", capability="geo_check", name="仅关键词", status="published",
                         project_id=0, language="zh-CN", user_prompt="关于「{{keyword}}」有哪些文章？请列出来源链接。", output_format="text",
                         created_by=owner_a.id, updated_by=owner_a.id)
    db.add(tpl)
    db.commit()
    engine["prompt_template_code"] = "geo_kw_only"
    ctx = ctx_for(link, engine="kimi", engine_config=engine)
    ctx.root_task = AiTask(id=1)
    assert geo.precheck(db, link, engine, ctx=ctx) is None
    fake_complete["answer"] = "推荐阅读下面这篇。智能门锁怎么选？来源 https://other.example.com/a"
    result = geo.check(db, link, engine, ctx=ctx)
    assert (result.status, result.match_mode) == ("cited", "title") and result.confidence >= Decimal("0.8")
    assert result.evidence["title_similarity"] >= 0.8
    fake_complete["answer"] = "完全无关的回答，讨论的是咖啡机。来源 https://other.example.com/a"
    assert geo.check(db, link, engine, ctx=ctx).status == "not_cited"


# =====================================================================
# 执行与写回（§7.4、§9；docs/03「检测写回」第 3~5 条）
# =====================================================================


def test_run_mock_hit_writes_records_tasks_and_projection(db: Session, setup_a: dict[str, Any], always_hit: None) -> None:
    link = setup_a["link"]
    before_count = link.index_check_count
    records = run_link(db, link)
    assert len(records) == 8
    db.expire_all()
    link = db.get(PublishLink, link.id)
    rows = db.scalars(select(IndexCheck).where(IndexCheck.link_id == link.id).order_by(IndexCheck.id)).all()
    assert [(r.kind, r.engine) for r in rows] == [("seo", e) for e in SEO_MOCK_ENGINES] + [("geo", e) for e in GEO_MOCK_ENGINES]
    for row in rows:
        evidence = json.loads(row.evidence_json)
        assert evidence["source"] == "mock"
        assert row.result_status == ("indexed" if row.kind == "seo" else "cited")
        assert row.provider == ("zhiqi_web_search" if row.kind == "seo" else "zhiqi_model")
        assert row.previous_status == "unknown" and row.match_mode == "url" and row.confidence == Decimal("1.000")
        assert row.check_type == "scheduled" and row.request_id and row.model == "mock-text"
        attempt = db.get(AiTask, row.ai_task_id)
        assert attempt is not None and attempt.root_task_id is not None and attempt.status == "succeeded"
        root = db.get(AiTask, attempt.root_task_id)
        assert root.capability == ("seo_check" if row.kind == "seo" else "geo_check") and root.operation == root.capability
        assert (root.status, root.trigger_type, root.target_type, root.target_id, root.project_id) == (
            "succeeded", "worker", "publish_link", link.id, link.project_id)
        assert json.loads(root.input_json) == {"engine": row.engine, "kind": row.kind, "query_by": ["url", "title"]}
    assert link.seo_indexed_any and link.geo_cited_any
    assert link.first_indexed_at is not None and link.first_cited_at is not None and link.last_index_checked_at is not None
    seo = states(link, "seo")
    assert set(seo) == set(SEO_MOCK_ENGINES)
    assert seo["baidu"]["status"] == "indexed" and seo["baidu"]["check_count"] == 1 and seo["baidu"]["first_indexed_at"]
    assert states(link, "geo")["doubao"]["first_cited_at"]
    assert (link.index_check_count, link.index_checks_done) == (before_count + 1, 1)
    assert link.next_index_check_at is not None and link.next_index_check_at > utcnow()
    rt = redis_client.hgetall(stats_service.realtime_key(stats_service.today_date(db), link.project_id))
    assert int(rt["seo_checks"]) == 2 and int(rt["geo_checks"]) == 6
    assert int(rt["seo_newly_indexed"]) == 1 and int(rt["geo_newly_cited"]) == 1
    assert int(rt["index_hours_links"]) == 1 and int(rt["index_hours_sum"]) >= 47
    assert int(redis_client.hget(stats_service.realtime_key(stats_service.today_date(db), 0), "seo_checks")) == 2


def test_run_miss_then_dropped_and_unknown_keeps_status(db: Session, setup_a: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    link = setup_a["link"]
    monkeypatch.setattr(zhiqi_mock, "HIT_PROBABILITY", 1.0)
    run_link(db, link, kinds=("seo",), engines=["baidu"])
    monkeypatch.setattr(zhiqi_mock, "HIT_PROBABILITY", 0.0)
    run_link(db, link, kinds=("seo", "geo"), engines=["baidu", "doubao"], check_type="manual", triggered_by=9)
    db.expire_all()
    link = db.get(PublishLink, link.id)
    rows = db.scalars(select(IndexCheck).where(IndexCheck.link_id == link.id).order_by(IndexCheck.id)).all()
    dropped = rows[1]
    assert (dropped.result_status, dropped.previous_status, dropped.check_type, dropped.triggered_by) == (
        "not_indexed", "indexed", "manual", 9)
    assert json.loads(dropped.evidence_json)["dropped"] is True and dropped.confidence == Decimal("0.800")
    assert rows[2].result_status == "not_cited"
    assert not link.seo_indexed_any and link.first_indexed_at is not None          # 首次收录时间只写一次、不清空
    assert states(link, "seo")["baidu"] == {**states(link, "seo")["baidu"], "status": "not_indexed", "check_count": 1}
    root = db.get(AiTask, db.get(AiTask, rows[1].ai_task_id).root_task_id)
    assert (root.trigger_type, root.created_by) == ("user", 9)

    # unknown（调用失败）只更新 checked_at，保留原 status；从未成功检测的引擎写 unknown
    index_check_service.apply_result(db, link.id, "seo", "baidu", CheckResult(status="unknown", provider="zhiqi_web_search",
                                     error_category="timeout", error_message="ReadTimeout"), check_type="scheduled",
                                     triggered_by=None)
    index_check_service.apply_result(db, link.id, "seo", "google", CheckResult(status="unknown", provider="zhiqi_web_search",
                                     error_message="no_search_evidence"), check_type="scheduled", triggered_by=None)
    db.expire_all()
    link = db.get(PublishLink, link.id)
    seo = states(link, "seo")
    assert seo["baidu"]["status"] == "not_indexed" and seo["baidu"]["check_count"] == 2
    assert seo["google"] == {**seo["google"], "status": "unknown", "first_indexed_at": None, "check_count": 1}


def test_any_flags_recomputed_over_all_engines_including_disabled(db: Session, setup_a: dict[str, Any], always_hit: None) -> None:
    link = setup_a["link"]
    run_link(db, link, kinds=("seo",), engines=["bing"])
    settings_service.set_value(db, "seo_providers", {"engines": {"bing": {"enabled": False}}})
    index_check_service.apply_result(db, link.id, "seo", "baidu", CheckResult(status="not_indexed", provider="zhiqi_web_search"),
                                     check_type="manual", triggered_by=None)
    db.expire_all()
    assert db.get(PublishLink, link.id).seo_indexed_any is True          # 已停用的 bing 仍为 indexed


def test_manual_run_does_not_touch_schedule(db: Session, setup_a: dict[str, Any], never_hit: None) -> None:
    link = setup_a["link"]
    before = (link.index_check_count, link.index_checks_done, link.next_index_check_at)
    run_link(db, link, check_type="manual", triggered_by=1)
    db.expire_all()
    link = db.get(PublishLink, link.id)
    assert (link.index_check_count, link.index_checks_done, link.next_index_check_at) == before
    assert all(s["check_count"] == 0 for s in states(link, "seo").values())


def test_manual_check_before_first_scheduled_round_keeps_late_backfill_due(db: Session, env: dict[str, Any],
                                                                         setup_a: dict[str, Any], never_hit: None) -> None:
    """晚回填（主计划已用尽）的链接在首个 scheduled 轮次前先做手动检测：手动写入的 ``checked_at`` 不推迟排程，
    下一次扫描仍对全部引擎执行 scheduled 轮次（docs/06 第 10~11 步、docs/11 §7.5「手动 / 人工标记」、§17 第 5 条）。"""
    link = make_link(db, setup_a["content"], env["platforms"]["zhihu"], "https://zhuanlan.zhihu.com/p/3131", days_ago=35)
    link.next_index_check_at = utcnow() - timedelta(seconds=5)          # 回填时 due=now
    db.commit()
    run_link(db, link, check_type="manual", triggered_by=1)
    db.expire_all()
    link = db.get(PublishLink, link.id)
    assert all(s["check_count"] == 0 and s["checked_at"] for s in states(link, "seo").values())
    assert link.next_index_check_at <= utcnow()                          # 手动不重算
    assert schedule_index_checks.enqueue_due() == 1
    payload = [p for p in queue_items() if p["link_id"] == link.id][0]
    assert payload["check_type"] == "scheduled"
    assert payload["engines"] == [*SEO_MOCK_ENGINES, *GEO_MOCK_ENGINES]


def test_run_skips_when_paused_and_respects_deleted(db: Session, setup_a: dict[str, Any]) -> None:
    link = setup_a["link"]
    redis_client.set("ai:paused:quota_exceeded", "x", ex=600)
    assert run_link(db, link) == []
    db.expire_all()
    link = db.get(PublishLink, link.id)
    delta = (link.next_index_check_at - utcnow()).total_seconds()
    assert 590 <= delta <= 601 and db.scalar(select(func.count(IndexCheck.id))) == 0
    assert db.scalar(select(func.count(AiTask.id))) == 0
    redis_client.delete("ai:paused:quota_exceeded")
    link.alive_status = "deleted"
    db.commit()
    assert run_link(db, link) == []
    link.alive_status, link.is_monitoring = "alive", False
    db.commit()
    assert run_link(db, link, kinds=("seo",), engines=["baidu"]) == []                     # 非手动且暂停监控
    assert len(run_link(db, link, kinds=("seo",), engines=["baidu"], check_type="manual")) == 1


def test_breaker_open_only_affects_that_engine(db: Session, setup_a: dict[str, Any], always_hit: None) -> None:
    link = setup_a["link"]
    breaker = ai_gateway_service.get_breaker(ai_gateway_service._cfg(db))  # noqa: SLF001
    breaker.force_open("geo_check", "mock-text", reason="manual")
    records = run_link(db, link, kinds=("seo", "geo"), engines=["baidu", "doubao"])
    by_engine = {r.engine: r for r in records}
    assert by_engine["baidu"].result_status == "indexed"
    assert (by_engine["doubao"].result_status, by_engine["doubao"].error_category) == ("unknown", "breaker_open")
    root = db.get(AiTask, db.get(AiTask, by_engine["doubao"].ai_task_id).root_task_id)
    assert (root.status, root.error_category) == ("failed", "breaker_open")
    db.expire_all()
    assert states(db.get(PublishLink, link.id), "geo")["doubao"]["status"] == "unknown"


def test_title_in_prompt_creates_no_root_task(db: Session, env: dict[str, Any], owner_a: User, setup_a: dict[str, Any]) -> None:
    db.add(PromptTemplate(code="geo_kw_only", version=1, kind="geo_query", capability="geo_check", name="仅关键词", status="published",
                          project_id=0, language="zh-CN", user_prompt="关于「{{keyword}}」有哪些文章？请列出来源链接。",
                          output_format="text", created_by=owner_a.id, updated_by=owner_a.id))
    db.commit()
    settings_service.set_value(db, "geo_engines", {"engines": [
        {"code": "doubao", "name": "豆包", "enabled": True, "prompt_template_code": "geo_kw_only",
         "parse": {"match_mode": "title", "title_fuzzy_threshold": 0.8}}]})
    content = make_content(db, setup_a["project"], owner_a, title="无关键词的文章", keyword=None)
    link = make_link(db, content, env["platforms"]["website"], "https://www.brand.example.com/news/1")
    tasks_before = db.scalar(select(func.count(AiTask.id)))
    records = run_link(db, link, kinds=("geo",), engines=["doubao"])
    assert len(records) == 1 and (records[0].result_status, records[0].error_message, records[0].ai_task_id) == (
        "unknown", "title_in_prompt", None)
    assert db.scalar(select(func.count(AiTask.id))) == tasks_before


def test_local_quota_limit_pushes_link_to_next_day(db: Session, setup_a: dict[str, Any]) -> None:
    link = setup_a["link"]
    settings_service.set_value(db, "generation_config", {"quota": {"daily_limit": 1}})
    records = run_link(db, link, kinds=("seo",), engines=["baidu"])
    assert (records[0].result_status, records[0].error_category, records[0].error_message) == (
        "unknown", "quota_exceeded", "local_quota_limit")
    db.expire_all()
    link = db.get(PublishLink, link.id)
    assert link.next_index_check_at >= index_check_service.next_day_start(db)
    root = db.scalars(select(AiTask).where(AiTask.root_task_id.is_(None))).first()
    assert (root.status, root.error_category) == ("failed", "quota_exceeded")


# =====================================================================
# 排程：到期扫描、日上限预扣、去重、锁竞争、drain（§7.5、§9）
# =====================================================================


class SyncPool:
    """同步执行的线程池替身。"""

    def __init__(self) -> None:
        self.submitted = 0

    def is_full(self) -> bool:
        return False

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        self.submitted += 1
        fn(*args, **kwargs)


@pytest.fixture
def due_link(db: Session, setup_a: dict[str, Any], env: dict[str, Any]) -> PublishLink:
    """发布 40 天前（主计划已用尽，从未检测的引擎即刻到期），baidu 刚检测为 indexed（90 天后才复核）。"""
    link = make_link(db, setup_a["content"], env["platforms"]["zhihu"], "https://zhuanlan.zhihu.com/p/4040", days_ago=40)
    link.seo_status_json = json.dumps({"baidu": {"status": "indexed", "checked_at": utcnow().replace(microsecond=0).isoformat() + "Z",
                                                 "first_indexed_at": None, "check_count": 1}})
    link.next_index_check_at = utcnow() - timedelta(minutes=1)
    db.commit()
    return link


def test_compute_next_index_check_at_late_backfill_and_excludes_manual_engines(db: Session, env: dict[str, Any],
                                                                               setup_a: dict[str, Any]) -> None:
    link = make_link(db, setup_a["content"], env["platforms"]["zhihu"], "https://zhuanlan.zhihu.com/p/5", days_ago=10)
    assert link.index_check_count == 3                                    # 1/3/7 已过期
    assert abs((link.next_index_check_at - (link.published_at + timedelta(days=14))).total_seconds()) < 1
    settings_service.set_value(db, "seo_providers", {"engines": {"baidu": {"provider": "manual"}}})
    assert ("seo", "baidu") not in index_check_service.schedulable_engines(db)
    assert "baidu" in index_check_service.enabled_engines("seo", db)
    assert set(index_check_service.enabled_engines("geo")) == set(GEO_MOCK_ENGINES)


def test_schedule_enqueues_only_due_engines_and_prededucts_limit(db: Session, due_link: PublishLink) -> None:
    assert schedule_index_checks.enqueue_due() == 1
    items = queue_items()
    assert len(items) == 1
    payload = items[0]
    assert payload == {"link_id": due_link.id, "kinds": ["seo", "geo"], "engines": ["bing", *GEO_MOCK_ENGINES],
                       "check_type": "scheduled", "triggered_by": None}
    assert int(redis_client.get(index_check_service.limit_key(db))) == 7
    assert redis_client.exists(index_check_service.queued_key(due_link.id, "seo"))
    assert redis_client.exists(index_check_service.queued_key(due_link.id, "geo"))
    db.expire_all()
    nxt = db.get(PublishLink, due_link.id).next_index_check_at
    assert 3590 <= (nxt - utcnow()).total_seconds() <= 3601
    assert schedule_index_checks.enqueue_due() == 0                      # 已推后 1h


def test_schedule_daily_limit_defers_to_next_day_without_counting(db: Session, due_link: PublishLink) -> None:
    settings_service.set_value(db, "monitoring_config", {"index_check": {"daily_limit": 5}})
    assert schedule_index_checks.enqueue_due() == 0
    assert queue_items() == [] and int(redis_client.get(index_check_service.limit_key(db)) or 0) == 0
    db.expire_all()
    assert db.get(PublishLink, due_link.id).next_index_check_at == index_check_service.next_day_start(db)


def test_schedule_already_queued_and_disabled(db: Session, due_link: PublishLink) -> None:
    for kind in ("seo", "geo"):
        redis_client.set(index_check_service.queued_key(due_link.id, kind), "1", ex=3600)
    assert schedule_index_checks.enqueue_due() == 0
    assert queue_items() == [] and int(redis_client.get(index_check_service.limit_key(db)) or 0) == 0
    db.expire_all()
    link = db.get(PublishLink, due_link.id)
    assert link.next_index_check_at > utcnow()
    link.next_index_check_at = utcnow() - timedelta(minutes=1)
    db.commit()
    settings_service.set_value(db, "monitoring_config", {"index_check": {"enabled": False}})
    redis_client.delete(index_check_service.queued_key(due_link.id, "seo"), index_check_service.queued_key(due_link.id, "geo"))
    assert schedule_index_checks.enqueue_due() == 0 and queue_items() == []
    db.expire_all()
    assert db.get(PublishLink, due_link.id).next_index_check_at is not None      # enabled=false 不置 NULL


def test_process_one_lock_busy_does_not_requeue(db: Session, due_link: PublishLink) -> None:
    payload = {"link_id": due_link.id, "kinds": ["seo"], "engines": ["bing"], "check_type": "manual", "triggered_by": 1}
    redis_client.set(index_check_service.queued_key(due_link.id, "seo"), "1", ex=3600)
    token = acquire_lock(run_index_checks.lock_key(due_link.id), 900)
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    try:
        assert run_index_checks.process_one(json.dumps(payload), semaphore=sem) is False
    finally:
        release_lock(run_index_checks.lock_key(due_link.id), token)
    assert sem.acquire(blocking=False)                                    # 信号量已释放
    assert not redis_client.exists(index_check_service.queued_key(due_link.id, "seo")) and queue_items() == []
    db.expire_all()
    delta = (db.get(PublishLink, due_link.id).next_index_check_at - utcnow()).total_seconds()
    assert 290 <= delta <= 301 and db.scalar(select(func.count(IndexCheck.id))) == 0


def test_drain_runs_scheduled_payload_end_to_end(db: Session, due_link: PublishLink, always_hit: None) -> None:
    before = db.get(PublishLink, due_link.id).index_check_count
    assert schedule_index_checks.enqueue_due() == 1
    pool = SyncPool()
    sems = {"index_check": threading.BoundedSemaphore(2)}
    assert run_index_checks.drain(pool, limit=10, sems=sems) == 1
    assert queue_items() == [] and not redis_client.exists(run_index_checks.lock_key(due_link.id))
    assert not redis_client.exists(index_check_service.queued_key(due_link.id, "seo"))
    db.expire_all()
    link = db.get(PublishLink, due_link.id)
    engines = {r.engine for r in db.scalars(select(IndexCheck).where(IndexCheck.link_id == link.id)).all()}
    assert engines == {"bing", *GEO_MOCK_ENGINES}
    assert (link.index_check_count, link.index_checks_done) == (before + 1, 1)
    # 刚检测完：已收录的引擎 90 天后复核，其余引擎按月检
    assert link.next_index_check_at is not None and link.next_index_check_at > utcnow() + timedelta(days=29)
    assert run_index_checks.drain(pool, limit=10, sems=sems) == 0


def test_monitor_worker_activates_index_tasks() -> None:
    from app.worker import optional_task

    assert optional_task("schedule_index_checks", "enqueue_due") is schedule_index_checks.enqueue_due
    assert optional_task("run_index_checks", "drain") is run_index_checks.drain


def test_parse_payload_rejects_invalid() -> None:
    assert run_index_checks.parse_payload("not json") is None
    assert run_index_checks.parse_payload({"link_id": 1, "kinds": [], "engines": ["baidu"]}) is None
    assert run_index_checks.parse_payload({"link_id": 1, "kinds": ["seo", "xx"], "engines": ["baidu", "baidu"]}) == {
        "link_id": 1, "kinds": ["seo"], "engines": ["baidu"], "check_type": "scheduled", "triggered_by": None}


# =====================================================================
# 接口：index-check / mark-index（docs/04 §6.17、§7.11）
# =====================================================================


def test_index_check_api_rules(client: TestClient, db: Session, owner_a: User, setup_a: dict[str, Any], setup_b: dict[str, Any],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    link = setup_a["link"]
    url = f"{LINKS}/{link.id}/index-check"
    assert ok_data(client.post(url, json={"kinds": ["seo"]}, headers=owner_a.headers)) == {"queued": True}
    item = queue_items()[0]
    assert item == {"link_id": link.id, "kinds": ["seo"], "engines": SEO_MOCK_ENGINES, "check_type": "manual",
                    "triggered_by": owner_a.id}
    assert int(redis_client.get(index_check_service.limit_key(db))) == 2
    assert ok_data(client.post(url, json={"kinds": ["seo"]}, headers=owner_a.headers)) == {"queued": False, "reason": "already_queued"}
    assert int(redis_client.get(index_check_service.limit_key(db))) == 2                     # 去重命中退回预扣
    # GEO 部分仍可入队（标记按 kind）
    assert ok_data(client.post(url, json={"kinds": ["seo", "geo"], "engines": ["doubao"]}, headers=owner_a.headers))["queued"] is True

    body = err(client.post(url, json={"kinds": ["seo"], "engines": ["google"]}, headers=owner_a.headers), 400)
    assert body["data"][0]["loc"] == ["body", "engines", 0] and body["data"][0]["type"] == "engine_disabled"
    body = err(client.post(url, json={"kinds": ["geo"], "engines": ["baidu"]}, headers=owner_a.headers), 400)
    assert body["data"][0]["loc"] == ["body", "engines", 0]
    err(client.post(url, json={"kinds": []}, headers=owner_a.headers), 400)
    err(client.post(f"{LINKS}/{setup_b['link'].id}/index-check", json={"kinds": ["seo"]}, headers=owner_a.headers), 404)

    link.is_monitoring = False
    db.commit()
    body = err(client.post(url, json={"kinds": ["seo"]}, headers=owner_a.headers), 409)
    assert body["data"] == {"current_status": "alive", "reason": "monitoring_paused"}
    link.is_monitoring, link.alive_status = True, "deleted"
    db.commit()
    body = err(client.post(url, json={"kinds": ["seo"]}, headers=owner_a.headers), 409)
    assert body["data"] == {"current_status": "deleted"} and body["message"] == "链接已被删除，无法检测收录"

    link.alive_status = "alive"
    db.commit()
    redis_client.delete(index_check_service.queued_key(link.id, "seo"), index_check_service.queued_key(link.id, "geo"))
    settings_service.set_value(db, "monitoring_config", {"index_check": {"daily_limit": 3}})
    assert ok_data(client.post(url, json={"kinds": ["seo"]}, headers=owner_a.headers)) == {"queued": False, "reason": "daily_limit"}

    monkeypatch.setattr(index_check_service, "MANUAL_RATE", "1/hour")
    redis_client.delete(index_check_service.limit_key(db))
    for key in redis_client.scan_iter(match="rate:index_check_manual:*"):
        redis_client.delete(key)
    ok_data(client.post(url, json={"kinds": ["seo"], "engines": ["baidu"]}, headers=owner_a.headers))
    body = err(client.post(url, json={"kinds": ["seo"], "engines": ["baidu"]}, headers=owner_a.headers), 429)
    assert body["data"]["retry_after"] > 0


def test_mark_index_api(client: TestClient, db: Session, owner_a: User, read_only: User, setup_a: dict[str, Any]) -> None:
    link = setup_a["link"]
    before = (link.index_check_count, link.index_checks_done, link.next_index_check_at)
    url = f"{LINKS}/{link.id}/mark-index"
    data = ok_data(client.post(url, json={"kind": "seo", "engine": "google", "status": "indexed", "note": "站长后台已确认",
                                          "evidence_url": "https://www.google.com/search?q=x"}, headers=owner_a.headers))
    assert data["seo_status"]["google"]["status"] == "indexed" and data["seo_status"]["google"]["check_count"] == 0
    assert data["seo_indexed_any"] is True and data["first_indexed_at"] is not None
    row = db.scalars(select(IndexCheck).where(IndexCheck.link_id == link.id)).one()
    assert (row.provider, row.check_type, row.match_mode, row.confidence, row.duration_ms, row.triggered_by) == (
        "manual", "manual", "manual", Decimal("1.000"), 0, owner_a.id)
    assert row.query_text is None and row.model is None and row.request_id is None and row.ai_task_id is None
    assert row.evidence_url == "https://www.google.com/search?q=x"
    assert json.loads(row.evidence_json) == {"note": "站长后台已确认", "source": "manual"}
    db.expire_all()
    link = db.get(PublishLink, link.id)
    assert (link.index_check_count, link.index_checks_done, link.next_index_check_at) == before

    log = db.scalars(select(AdminOperationLog).where(AdminOperationLog.admin_id == owner_a.id)
                     .order_by(AdminOperationLog.id.desc())).first()
    assert (log.action, log.target_type, log.target_id) == ("execute", "publish_link", str(link.id))
    assert "seo/google → indexed" in log.summary
    data = ok_data(client.post(url, json={"kind": "geo", "engine": "doubao", "status": "cited"}, headers=owner_a.headers))
    assert data["geo_cited_any"] is True and data["geo_status"]["doubao"]["first_cited_at"]

    for body, loc in (({"kind": "seo", "engine": "google", "status": "unknown"}, ["body", "status"]),
                      ({"kind": "seo", "engine": "google", "status": "cited"}, ["body", "status"]),
                      ({"kind": "geo", "engine": "doubao", "status": "indexed"}, ["body", "status"]),
                      ({"kind": "seo", "engine": "yahoo", "status": "indexed"}, ["body", "engine"]),
                      ({"kind": "seo", "engine": "google", "status": "indexed", "evidence_url": "ftp://x"}, ["body", "evidence_url"])):
        res = err(client.post(url, json=body, headers=owner_a.headers), 400)
        assert res["data"][0]["loc"] == loc, res
    err(client.post(url, json={"kind": "seo", "engine": "google", "status": "indexed"}, headers=read_only.headers), 403)

    page = ok_data(client.get(f"{LINKS}/{link.id}/index-checks", params={"kind": "geo"}, headers=owner_a.headers))
    assert page["total"] == 1 and page["items"][0]["engine"] == "doubao" and page["items"][0]["evidence"]["source"] == "manual"


# =====================================================================
# 接口：监控概览 / 记录 / 批量触发（docs/04 §6.18；docs/13 §6.3、§7.5）
# =====================================================================


def _add_link_check(db: Session, link: PublishLink, *, result: str = "alive", minutes_ago: int = 5) -> LinkCheck:
    row = LinkCheck(link_id=link.id, check_type="scheduled", result_status=result, previous_status="alive", applied_status=result,
                    http_status=200, matched_rule="ok", duration_ms=10, checked_at=utcnow() - timedelta(minutes=minutes_ago))
    db.add(row)
    db.commit()
    return row


def _add_index_check(db: Session, link: PublishLink, *, kind: str = "seo", engine: str = "baidu", status: str = "indexed") -> IndexCheck:
    row = IndexCheck(link_id=link.id, kind=kind, engine=engine, provider="zhiqi_web_search" if kind == "seo" else "zhiqi_model",
                     check_type="scheduled", result_status=status, previous_status="unknown", match_mode="url",
                     duration_ms=5, checked_at=utcnow() - timedelta(minutes=3))
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def records(db: Session, setup_a: dict[str, Any], setup_b: dict[str, Any]) -> dict[str, Any]:
    for link in (setup_a["link"], setup_b["link"]):
        link.next_check_at = utcnow() - timedelta(minutes=1)
        link.next_index_check_at = utcnow() - timedelta(minutes=1)
    db.commit()
    return {
        "lc_a": _add_link_check(db, setup_a["link"]),
        "lc_b": _add_link_check(db, setup_b["link"], result="deleted", minutes_ago=1),
        "ic_a": _add_index_check(db, setup_a["link"]),
        "ic_b": _add_index_check(db, setup_b["link"], kind="geo", engine="doubao", status="not_cited"),
    }


def test_monitoring_overview_scope(client: TestClient, db: Session, owner_a: User, super_admin: User, records: dict[str, Any]) -> None:
    redis_client.rpush("queue:link_checks", "x", "y")
    redis_client.rpush(index_check_service.QUEUE_INDEX_CHECKS, "z")
    redis_client.set(f"limit:link_checks:{stats_service.today_date(db).isoformat()}", 12)
    redis_client.set(index_check_service.limit_key(db), 34)
    redis_client.set("worker:heartbeat:monitor_worker:mon-host:4321",
                     json.dumps({"hostname": "mon-host", "pid": 4321, "at": utcnow().isoformat() + "Z"}), ex=900)
    mine = ok_data(client.get(f"{MONITORING}/overview", headers=owner_a.headers))
    assert mine["link_checks"]["due"] == 1 and mine["link_checks"]["today"] == 1 and mine["link_checks"]["queued"] == 2
    assert mine["index_checks"]["due"] == 1 and mine["index_checks"]["today"] == 1 and mine["index_checks"]["queued"] == 1
    assert mine["link_checks"]["last_run_at"] == records["lc_a"].checked_at.replace(microsecond=0).isoformat() + "Z"
    assert mine["daily_limits"] == {"link_checks": {"limit": 5000, "used": 12}, "index_checks": {"limit": 2000, "used": 34}}
    assert mine["workers"][0]["name"] == "monitor_worker" and mine["workers"][0]["alive"] is True
    everyone = ok_data(client.get(f"{MONITORING}/overview", headers=super_admin.headers))
    assert everyone["link_checks"]["due"] == 2 and everyone["link_checks"]["today"] == 2
    as_a = ok_data(client.get(f"{MONITORING}/overview", params={"owner_id": owner_a.id}, headers=super_admin.headers))
    assert as_a == mine


def test_monitoring_records_lists_and_details(client: TestClient, owner_a: User, super_admin: User, records: dict[str, Any],
                                              setup_a: dict[str, Any], setup_b: dict[str, Any], env: dict[str, Any]) -> None:
    page = ok_data(client.get(f"{MONITORING}/link-checks", headers=owner_a.headers))
    assert page["total"] == 1
    item = page["items"][0]
    assert item["link"] == {"id": setup_a["link"].id, "url": setup_a["link"].url, "platform_code": "zhihu",
                            "content_id": setup_a["content"].id}
    page = ok_data(client.get(f"{MONITORING}/link-checks", params={"result_status": "deleted"}, headers=super_admin.headers))
    assert [i["id"] for i in page["items"]] == [records["lc_b"].id]
    page = ok_data(client.get(f"{MONITORING}/link-checks", params={"platform_id": env["platforms"]["zhihu"].id},
                              headers=super_admin.headers))
    assert [i["id"] for i in page["items"]] == [records["lc_a"].id]
    page = ok_data(client.get(f"{MONITORING}/link-checks", params={"project_id": setup_b["project"].id}, headers=owner_a.headers))
    assert page["total"] == 0                                                 # 不可见对象的筛选 → 空结果
    err(client.get(f"{MONITORING}/link-checks/{records['lc_b'].id}", headers=owner_a.headers), 404)
    detail = ok_data(client.get(f"{MONITORING}/link-checks/{records['lc_b'].id}", headers=super_admin.headers))
    assert detail["link"]["platform_code"] == "csdn"

    page = ok_data(client.get(f"{MONITORING}/index-checks", headers=owner_a.headers))
    assert [i["id"] for i in page["items"]] == [records["ic_a"].id] and "link" not in page["items"][0]
    page = ok_data(client.get(f"{MONITORING}/index-checks", params={"kind": "geo", "result_status": "not_cited",
                                                                     "provider": "zhiqi_model"}, headers=super_admin.headers))
    assert [i["id"] for i in page["items"]] == [records["ic_b"].id]
    err(client.get(f"{MONITORING}/index-checks/{records['ic_b'].id}", headers=owner_a.headers), 404)
    assert ok_data(client.get(f"{MONITORING}/index-checks/{records['ic_a'].id}", headers=owner_a.headers))["engine"] == "baidu"


def test_link_checks_run_scope_and_owner_view(client: TestClient, owner_a: User, super_admin: User, records: dict[str, Any],
                                              setup_a: dict[str, Any], setup_b: dict[str, Any]) -> None:
    data = ok_data(client.post(f"{MONITORING}/link-checks/run", params={"owner_id": owner_a.id}, json={"only_due": True},
                               headers=super_admin.headers))
    assert data == {"enqueued": 1, "skipped": 0}
    assert [json.loads(r)["link_id"] for r in redis_client.lrange("queue:link_checks", 0, -1)] == [setup_a["link"].id]
    data = ok_data(client.post(f"{MONITORING}/link-checks/run",
                               json={"link_ids": [setup_a["link"].id, setup_b["link"].id], "only_due": False}, headers=owner_a.headers))
    assert data == {"enqueued": 0, "skipped": 2}                               # A 已在队列；B 不可见
    data = ok_data(client.post(f"{MONITORING}/link-checks/run", json={"project_id": setup_b["project"].id}, headers=owner_a.headers))
    assert data == {"enqueued": 0, "skipped": 0}


def test_index_checks_run_scope_and_skips(client: TestClient, db: Session, owner_a: User, super_admin: User, read_only: User,
                                          records: dict[str, Any], setup_a: dict[str, Any], setup_b: dict[str, Any]) -> None:
    url = f"{MONITORING}/index-checks/run"
    data = ok_data(client.post(url, params={"owner_id": owner_a.id}, json={"kinds": ["seo"], "engines": ["baidu"]},
                               headers=super_admin.headers))
    assert data == {"enqueued": 1, "skipped": 0}
    item = queue_items()[0]
    assert (item["link_id"], item["engines"], item["check_type"], item["triggered_by"]) == (
        setup_a["link"].id, ["baidu"], "manual", super_admin.id)
    data = ok_data(client.post(url, json={"kinds": ["seo"], "link_ids": [setup_a["link"].id, setup_b["link"].id],
                                          "only_due": False}, headers=owner_a.headers))
    assert data == {"enqueued": 0, "skipped": 2}
    setup_b["link"].is_monitoring = False
    db.commit()
    data = ok_data(client.post(url, json={"kinds": ["geo"], "only_due": False}, headers=super_admin.headers))
    assert data == {"enqueued": 1, "skipped": 1}                               # B 暂停监控计入 skipped
    err(client.post(url, json={"kinds": ["seo"], "engines": ["google"]}, headers=super_admin.headers), 400)
    err(client.post(url, json={"kinds": ["seo"]}, headers=read_only.headers), 403)
    assert ok_data(client.get(f"{MONITORING}/overview", headers=read_only.headers))["link_checks"]["due"] >= 0


# =====================================================================
# 设置保存后的排程重算、index_overdue 前置数据、seed 模板
# =====================================================================


def test_saving_engine_settings_recomputes_null_schedules(client: TestClient, db: Session, super_admin: User,
                                                          setup_a: dict[str, Any], env: dict[str, Any]) -> None:
    settings_service.set_value(db, "seo_providers", {"engines": {"baidu": {"enabled": False}, "bing": {"enabled": False}}})
    settings_service.set_value(db, "geo_engines", {"engines": []})
    link = make_link(db, setup_a["content"], env["platforms"]["zhihu"], "https://zhuanlan.zhihu.com/p/777")
    assert link.next_index_check_at is None
    ok_data(client.put(f"{ADMIN_API}/settings/seo_providers", json={"value": {"engines": {"baidu": {"enabled": True}}}},
                       headers=super_admin.headers))
    db.expire_all()
    link = db.get(PublishLink, link.id)
    assert link.next_index_check_at is not None
    assert abs((link.next_index_check_at - (link.published_at + timedelta(days=3))).total_seconds()) < 1


def test_index_overdue_precondition_after_three_scheduled_rounds(db: Session, env: dict[str, Any], setup_a: dict[str, Any],
                                                                 never_hit: None) -> None:
    link = make_link(db, setup_a["content"], env["platforms"]["zhihu"], "https://zhuanlan.zhihu.com/p/3131", days_ago=31,
                     created_days_ago=31)
    for _ in range(3):
        run_link(db, link, kinds=("seo",), engines=SEO_MOCK_ENGINES)
    db.expire_all()
    link = db.get(PublishLink, link.id)
    assert link.index_checks_done == 3 and not link.seo_indexed_any and link.alive_status in ("alive", "changed")
    assert link.published_at <= utcnow() - timedelta(days=30)
    assert all(s["status"] == "not_indexed" and s["check_count"] == 3 for s in states(link, "seo").values())
    assert link_service.link_item(link, overdue_days=30)["index_overdue"] is True
    index_check_service.apply_result(db, link.id, "seo", "baidu", CheckResult(status="indexed", provider="manual",
                                     match_mode="manual"), check_type="manual", triggered_by=1)
    db.expire_all()
    assert db.get(PublishLink, link.id).seo_indexed_any is True


def test_seed_index_query_templates(system_templates: dict[str, Any]) -> None:
    seo, geo = system_templates["sys_seo_query"], system_templates["sys_geo_query"]
    assert (seo.kind, seo.capability, seo.status, seo.output_format, seo.project_id) == ("seo_query", "seo_check", "published", "json", 0)
    assert "{{engine_name}}" in seo.user_prompt and "{{url}}" in seo.user_prompt and "收录核查" in seo.system_prompt
    assert (geo.kind, geo.capability, geo.status, geo.output_format) == ("geo_query", "geo_check", "published", "text")
    assert "{{keyword}}" in geo.user_prompt and "{{url}}" not in geo.user_prompt and "{{domain}}" not in geo.user_prompt


# =====================================================================
# 告警：evaluate_alerts ①~⑤（docs/11 §10.1、§10.5；docs/01 §5.2 第 6 行）
# =====================================================================

ALERTS = f"{ADMIN_API}/alerts"


def _heartbeat(name: str, host: str = "host-1", pid: int = 100, *, minutes_ago: float = 0) -> None:
    at = (utcnow() - timedelta(minutes=minutes_ago)).isoformat() + "Z"
    redis_client.set(f"worker:heartbeat:{name}:{host}:{pid}", json.dumps({"hostname": host, "pid": pid, "at": at}), ex=900)


def _alerts(db: Session, alert_type: str) -> list[Alert]:
    db.expire_all()
    return list(db.scalars(select(Alert).where(Alert.alert_type == alert_type).order_by(Alert.id)).all())


@pytest.fixture
def workers_alive() -> None:
    """两个进程都有新鲜心跳，避免 ④ 产生与用例无关的 worker_stale。"""
    _heartbeat("worker")
    _heartbeat("monitor_worker")


def _overdue_link(db: Session, setup: dict[str, Any], env: dict[str, Any], url: str, *, days_ago: float = 31, done: int = 3,
                  alive_status: str = "alive", indexed: bool = False) -> PublishLink:
    link = make_link(db, setup["content"], env["platforms"]["zhihu"], url, days_ago=days_ago, alive_status=alive_status)
    link.index_checks_done = done
    link.seo_indexed_any = indexed
    db.commit()
    return link


def test_evaluate_index_overdue_raise_dedupe_and_auto_resolve(db: Session, env: dict[str, Any], setup_a: dict[str, Any],
                                                             workers_alive: None) -> None:
    hit = _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5001")
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5002", done=2)                    # 轮次不足
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5003", days_ago=20)               # 未满 30 天
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5004", alive_status="deleted")    # 已删除
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5005", alive_status="pending")    # 未检测
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5006", indexed=True)              # 已收录
    changed = _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5007", alive_status="changed")

    result = evaluate_alerts.evaluate()
    assert result["index_overdue"] == 2 and result["errors"] == []
    rows = _alerts(db, "index_overdue")
    assert [a.target_id for a in rows] == [hit.id, changed.id]
    alert = rows[0]
    assert (alert.severity, alert.status, alert.project_id, alert.target_type, alert.target_key, alert.dedupe_key) == (
        "warning", "open", setup_a["project"].id, "publish_link", str(hit.id), f"index_overdue:publish_link:{hit.id}")
    assert alert.title == "链接超期未收录：zhuanlan.zhihu.com/p/5001"
    payload = json.loads(alert.payload_json)
    assert payload["index_checks_done"] == 3 and payload["platform_code"] == "zhihu" and payload["content_id"] == setup_a["content"].id

    # 条件持续成立：不重复触发、不累加
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["index_overdue"] == 0
    assert [a.trigger_count for a in _alerts(db, "index_overdue")] == [1, 1]

    # ⑤ seo_indexed_any=1 → 自动解决；另一条保持
    hit = db.get(PublishLink, hit.id)
    hit.seo_indexed_any = True
    db.commit()
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["auto_resolved_detail"]["index_overdue"] == 1
    first, second = _alerts(db, "index_overdue")
    assert (first.status, first.resolved_by, first.resolution_note) == ("resolved", None, "auto")
    assert second.status == "open"

    # 解决后再次满足条件 → 新建一行
    hit.seo_indexed_any = False
    db.commit()
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["index_overdue"] == 1
    assert len(_alerts(db, "index_overdue")) == 3


def test_evaluate_index_overdue_respects_rule_thresholds(db: Session, env: dict[str, Any], setup_a: dict[str, Any],
                                                         workers_alive: None) -> None:
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5101", days_ago=40, done=3)
    settings_service.set_value(db, "alert_config", {"rules": {"index_overdue": {"days": 45, "min_checks": 3}}})
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["index_overdue"] == 0
    settings_service.set_value(db, "alert_config", {"rules": {"index_overdue": {"days": 30, "min_checks": 4}}})
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["index_overdue"] == 0
    settings_service.set_value(db, "alert_config", {"rules": {"index_overdue": {"enabled": False, "days": 30, "min_checks": 3}}})
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["index_overdue"] == 0
    settings_service.set_value(db, "alert_config", {"rules": {"index_overdue": {"enabled": True, "days": 30, "min_checks": 3}}})
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["index_overdue"] == 1


def _attempt(db: Session, status: str, *, capability: str = "content", model: str = "gpt-x", minutes_ago: float = 1,
             trigger_type: str = "worker", category: str | None = None) -> AiTask:
    at = utcnow() - timedelta(minutes=minutes_ago)
    root = AiTask(capability=capability, operation="content_generate", model=model, status=status, trigger_type=trigger_type,
                  created_at=at, finished_at=at)
    db.add(root)
    db.flush()
    row = AiTask(capability=capability, operation="content_generate", model=model, status=status, trigger_type=trigger_type,
                 root_task_id=root.id, error_category=category if status == "failed" else None, request_id=f"req-{root.id}",
                 created_at=at, started_at=at, finished_at=at)
    db.add(row)
    db.commit()
    return row


def test_evaluate_ai_task_failures_streak_and_auto_resolve(db: Session, workers_alive: None) -> None:
    for i in range(4):
        _attempt(db, "failed", minutes_ago=20 - i, category="timeout")
    _attempt(db, "failed", minutes_ago=15, category="cancelled")                 # cancelled 不计
    _attempt(db, "failed", minutes_ago=14, trigger_type="health_probe", category="timeout")   # 探测不计
    _attempt(db, "failed", minutes_ago=50, category="timeout")                   # 窗口外
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["ai_task_failures"] == 0

    last = _attempt(db, "failed", minutes_ago=10, category="upstream_unavailable")
    _attempt(db, "failed", model="other", minutes_ago=5, category="timeout")
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["ai_task_failures"] == 1
    (alert,) = _alerts(db, "ai_task_failures")
    assert (alert.severity, alert.project_id, alert.target_type, alert.target_id, alert.target_key) == (
        "critical", None, "ai_model", None, "content:gpt-x")
    assert alert.title == "AI 调用连续失败：content:gpt-x"
    payload = json.loads(alert.payload_json)
    assert payload["consecutive_failures"] == 5 and payload["last_failed_task_id"] == last.id
    assert payload["error_categories"] == {"timeout": 4, "upstream_unavailable": 1}

    # 没有新的失败 → 不累加；新失败 → trigger_count += 1
    alert_service.evaluate(db, SYSTEM_SCOPE)
    assert _alerts(db, "ai_task_failures")[0].trigger_count == 1
    _attempt(db, "failed", minutes_ago=2, category="timeout")
    alert_service.evaluate(db, SYSTEM_SCOPE)
    assert _alerts(db, "ai_task_failures")[0].trigger_count == 2

    # 健康探测的成功不算；业务尝试成功 → 自动解决
    _attempt(db, "succeeded", minutes_ago=1, trigger_type="health_probe")
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["auto_resolved_detail"]["ai_task_failures"] == 0
    _attempt(db, "succeeded", minutes_ago=0.5)
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["auto_resolved_detail"]["ai_task_failures"] == 1 and result["ai_task_failures"] == 0
    alert = _alerts(db, "ai_task_failures")[0]
    assert (alert.status, alert.resolved_by, alert.resolution_note) == ("resolved", None, "auto")


def test_evaluate_ai_breaker_open_fallback(db: Session, workers_alive: None) -> None:
    breaker = ai_gateway_service.get_breaker(settings_service.get_config(db, "ai_routing_config"))
    breaker.force_open("content", "gpt-open", reason="manual")
    breaker.force_open("title", "gpt-half", reason="failures")
    redis_client.hset(breaker.key("title", "gpt-half"), "opened_at", "0")    # open_seconds 已过 → half_open，不补发
    redis_client.rpush(breaker.failures_key("keyword", "gpt-fail"), "1")      # 失败窗口键不是状态键
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["ai_breaker_open"] == 1
    (alert,) = _alerts(db, "ai_breaker_open")
    assert (alert.target_type, alert.target_key, alert.project_id, alert.severity) == ("ai_model", "content:gpt-open", None, "warning")
    assert json.loads(alert.payload_json)["reason"] == "manual"
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["ai_breaker_open"] == 0                    # 已有 open 告警不补发
    assert _alerts(db, "ai_breaker_open")[0].trigger_count == 1
    # 熔断器关闭由调用方解决（reset → resolve_alert）
    ai_gateway_service.reset_breaker(db, "content", "gpt-open")
    db.commit()
    assert _alerts(db, "ai_breaker_open")[0].status == "resolved"


def test_evaluate_worker_stale_replica_process_and_recovery(db: Session) -> None:
    _heartbeat("monitor_worker")
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["worker_stale"] == 1                                            # worker 无副本 → 进程级
    (alert,) = _alerts(db, "worker_stale")
    assert (alert.target_type, alert.target_key, alert.project_id, alert.target_id) == ("worker", "worker", None, None)

    _heartbeat("worker", "host-a", 1)
    _heartbeat("worker", "host-b", 2, minutes_ago=8)                             # 副本落后 > 5 分钟
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    rows = {a.target_key: a for a in _alerts(db, "worker_stale")}
    assert rows["worker"].status == "resolved" and rows["worker"].resolution_note == "auto"
    assert rows["worker:host-b:2"].status == "open"
    assert result["auto_resolved_detail"]["worker_stale"] >= 1

    redis_client.delete("worker:heartbeat:worker:host-b:2")                       # 副本键过期 → 解决
    alert_service.evaluate(db, SYSTEM_SCOPE)
    assert all(a.status == "resolved" for a in _alerts(db, "worker_stale"))

    # monitor_worker 的告警由 app.worker 触发，这里只负责恢复后的自动解决
    alert_service.raise_alert(db, SYSTEM_SCOPE, "worker_stale", target_type="worker", target_key="monitor_worker", title="t", message="m")
    alert_service.raise_alert(db, SYSTEM_SCOPE, "worker_stale", target_type="worker", target_key="monitor_worker:gone:9",
                              title="t", message="m")
    db.commit()
    alert_service.evaluate(db, SYSTEM_SCOPE)
    assert all(a.status == "resolved" for a in _alerts(db, "worker_stale"))


def test_evaluate_link_deleted_fallback_resolution_and_lock(db: Session, setup_a: dict[str, Any], workers_alive: None) -> None:
    link = setup_a["link"]
    alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=link.id,
                              project_id=link.project_id, title="删", message="m")
    alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=999999,
                              project_id=link.project_id, title="孤儿", message="m")
    db.commit()
    link.alive_status = "deleted"
    db.commit()
    token = acquire_lock("lock:monitor:evaluate_alerts", 60)
    try:
        assert alert_service.evaluate(db, SYSTEM_SCOPE) == {"skipped": True, "reason": "locked"}
    finally:
        release_lock("lock:monitor:evaluate_alerts", token)
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["auto_resolved_detail"]["link_deleted"] == 1                    # 只解决孤儿告警
    link.alive_status = "alive"
    db.commit()
    assert alert_service.evaluate(db, SYSTEM_SCOPE)["auto_resolved_detail"]["link_deleted"] == 1
    assert all(a.status == "resolved" and a.resolution_note == "auto" for a in _alerts(db, "link_deleted"))
    assert redis_client.get("lock:monitor:evaluate_alerts") is None


def test_evaluate_step_failure_does_not_block_others(db: Session, env: dict[str, Any], setup_a: dict[str, Any],
                                                     workers_alive: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5201")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(alert_service, "evaluate_ai_task_failures", boom)
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["errors"] == ["ai_task_failures"] and result["index_overdue"] == 1


def test_evaluate_takes_scope_and_restricted_scope_only_touches_own_projects(
    db: Session, env: dict[str, Any], setup_a: dict[str, Any], setup_b: dict[str, Any], owner_a: User, workers_alive: None
) -> None:
    """docs/11 §4.1 注：``evaluate`` 以 ``scope`` 为紧随 ``db`` 的必填参数；受限范围只评估 / 解决本人项目的链接告警，
    系统告警（②③④，``project_id IS NULL``）只在不受限范围下评估。"""
    import inspect

    params = list(inspect.signature(alert_service.evaluate).parameters.values())
    assert params[1].name == "scope" and params[1].default is inspect.Parameter.empty
    hit_a = _overdue_link(db, setup_a, env, "https://zhuanlan.zhihu.com/p/5301")
    hit_b = _overdue_link(db, setup_b, env, "https://zhuanlan.zhihu.com/p/5302")
    for link in (setup_a["link"], setup_b["link"]):
        alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=link.id,
                                  project_id=link.project_id, title="删", message="m")
    db.commit()
    scope_a = DataScope(admin_id=owner_a.id, scope="own", owner_id=owner_a.id)
    result = alert_service.evaluate(db, scope_a)
    assert result["index_overdue"] == 1 and result["errors"] == []
    assert [a.target_id for a in _alerts(db, "index_overdue")] == [hit_a.id]
    assert "ai_task_failures" not in result["auto_resolved_detail"] and "worker_stale" not in result["auto_resolved_detail"]
    assert result["auto_resolved_detail"]["link_deleted"] == 1                     # setup_a 的链接为 alive
    statuses = {a.target_id: a.status for a in _alerts(db, "link_deleted")}
    assert statuses == {setup_a["link"].id: "resolved", setup_b["link"].id: "open"}
    result = alert_service.evaluate(db, SYSTEM_SCOPE)
    assert result["index_overdue"] == 1 and {a.target_id for a in _alerts(db, "index_overdue")} == {hit_a.id, hit_b.id}
    assert all(a.status == "resolved" for a in _alerts(db, "link_deleted"))


def test_monitor_worker_registers_evaluate_alerts() -> None:
    from app.monitor_worker import EVALUATE_ALERTS_INTERVAL_SECONDS
    from app.worker import optional_task

    assert optional_task("evaluate_alerts", "evaluate") is evaluate_alerts.evaluate
    assert EVALUATE_ALERTS_INTERVAL_SECONDS == evaluate_alerts.INTERVAL_SECONDS == 300


# =====================================================================
# 告警：通道（docs/11 §10.4）
# =====================================================================


def test_webhook_channel_delivery_signature_min_severity_and_failure(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    status = {"code": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status["code"])

    monkeypatch.setattr(alert_service, "WEBHOOK_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example.com/aicreat")
    monkeypatch.setattr(settings, "alert_webhook_secret", "whsec")
    settings_service.set_value(db, "alert_config", {"channels": {"webhook": {"enabled": True, "min_severity": "warning"}},
                                                    "dedupe_cooldown_minutes": 60})
    # 失败：只记日志，不追加通道，告警照常落库
    failed = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_auth_failed", target_type="system", title="鉴权失败", message="m")
    db.commit()
    assert len(calls) == 1
    db.refresh(failed)
    assert json.loads(failed.notified_channels_json) == ["in_app"] and failed.status == "open"
    # info 低于 min_severity → 不投递
    alert_service.raise_alert(db, SYSTEM_SCOPE, "media_task_failed", target_type="media_asset", target_id=7, title="t", message="m")
    db.commit()
    assert len(calls) == 1
    # 成功：签名头与 JSON 体
    status["code"] = 204
    ok_alert = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key="content:m1",
                                         title="熔断", message="m", payload={"model": "m1"})
    db.commit()
    request = calls[-1]
    assert request.headers["x-aicreat-event"] == "alert.triggered"
    assert request.headers["x-aicreat-signature"] == alert_service.WebhookChannel.signature("whsec", request.content)
    assert request.headers["x-aicreat-signature"].startswith("sha256=")
    body = json.loads(request.content)
    assert body["event"] == "alert.triggered" and body["alert"]["target_key"] == "content:m1" and body["alert"]["payload"] == {"model": "m1"}
    db.refresh(ok_alert)
    assert json.loads(ok_alert.notified_channels_json) == ["in_app", "webhook"]
    # 冷却期内再次触发不投递；resolved 事件不受冷却限制
    alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key="content:m1", title="熔断", message="m")
    db.commit()
    assert len(calls) == 2
    alert_service.resolve(db, SYSTEM_SCOPE, ok_alert.id, admin_id=None)
    assert len(calls) == 3 and calls[-1].headers["x-aicreat-event"] == "alert.resolved"
    # 未配置密钥时省略签名头
    monkeypatch.setattr(settings, "alert_webhook_secret", "")
    alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_quota_exceeded", target_type="system", title="额度", message="m")
    db.commit()
    assert "x-aicreat-signature" not in calls[-1].headers


class FakeSMTP:
    sent: list[Any] = []
    fail = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.logged_in: tuple[str, str] | None = None

    def login(self, user: str, password: str) -> None:
        self.logged_in = (user, password)

    def send_message(self, message: Any) -> None:
        if FakeSMTP.fail:
            raise OSError("connection reset")
        FakeSMTP.sent.append((message, self.logged_in))

    def quit(self) -> None:
        return None


def test_email_channel_smtp_delivery_and_failure(db: Session, setup_a: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    monkeypatch.setattr(alert_service.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 465)
    monkeypatch.setattr(settings, "smtp_user", "mailer")
    monkeypatch.setattr(settings, "smtp_password", "pw")
    monkeypatch.setattr(settings, "mail_from", "alerts@example.com")
    monkeypatch.setattr(settings, "public_base_url", "https://aicreat.example.com/")
    settings_service.set_value(db, "alert_config", {"rules": {"link_deleted": {"enabled": True, "severity": "critical"}},
                                                    "channels": {"email": {"enabled": True, "to": ["ops@example.com"],
                                                                           "min_severity": "critical"}}})
    link = setup_a["link"]
    alert = alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=link.id,
                                      project_id=link.project_id, title="链接已被删除：zhuanlan.zhihu.com/p/1001",
                                      message="平台 zhihu 返回 HTTP 404")
    db.commit()
    (message, login), = FakeSMTP.sent
    assert message["Subject"] == "[aicreat][critical] 链接已被删除：zhuanlan.zhihu.com/p/1001"
    assert message["From"] == "alerts@example.com" and message["To"] == "ops@example.com" and login == ("mailer", "pw")
    content = message.get_content()
    assert "平台 zhihu 返回 HTTP 404" in content and f"https://aicreat.example.com/admin/links/{link.id}" in content
    db.refresh(alert)
    assert json.loads(alert.notified_channels_json) == ["in_app", "email"]
    # warning 低于 min_severity=critical → 不投递
    alert_service.raise_alert(db, SYSTEM_SCOPE, "worker_stale", target_type="worker", target_key="worker", title="t", message="m")
    db.commit()
    assert len(FakeSMTP.sent) == 1
    # SMTP 失败：返回 False、不影响落库
    FakeSMTP.fail = True
    failed = alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_auth_failed", target_type="system", title="鉴权", message="m")
    db.commit()
    db.refresh(failed)
    assert json.loads(failed.notified_channels_json) == ["in_app"]
    # 配置不完整 → 不连接
    monkeypatch.setattr(settings, "smtp_host", "")
    assert alert_service.EmailChannel({"to": ["ops@example.com"]}).send(alert_service.alert_item(alert), "alert.triggered") is False
    assert alert_service.alert_detail_url({"target_type": "ai_model"}) == "https://aicreat.example.com/admin/ai/routes"


def test_channels_disabled_by_default(db: Session) -> None:
    cfg = settings_service.get_config(db, "alert_config")
    assert cfg["channels"]["webhook"]["enabled"] is False and cfg["channels"]["email"]["enabled"] is False
    assert cfg["channels"]["in_app"]["enabled"] is True
    assert alert_service.deliver({"id": 1, "severity": "critical", "dedupe_key": "k"}, "alert.triggered", config=cfg) == ["in_app"]


# =====================================================================
# 告警：接口与数据范围（docs/04 §6.19、§7.13；docs/13 §11）
# =====================================================================


@pytest.fixture
def alert_world(db: Session, setup_a: dict[str, Any], setup_b: dict[str, Any]) -> dict[str, Alert]:
    a, b = setup_a["link"], setup_b["link"]
    rows = {
        "a_deleted": alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=a.id,
                                               project_id=a.project_id, title="A 删除", message="m", payload={"url": a.url}),
        "a_changed": alert_service.raise_alert(db, SYSTEM_SCOPE, "link_changed", target_type="publish_link", target_id=a.id,
                                               project_id=a.project_id, title="A 变化", message="m"),
        "b_deleted": alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=b.id,
                                               project_id=b.project_id, title="B 删除", message="m"),
        "system": alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_auth_failed", target_type="system", title="鉴权失败", message="m"),
        "model": alert_service.raise_alert(db, SYSTEM_SCOPE, "ai_breaker_open", target_type="ai_model", target_key="content:gpt",
                                           title="熔断", message="m"),
    }
    db.commit()
    return rows


def _ids(page: dict[str, Any]) -> list[int]:
    return [item["id"] for item in page["items"]]


def test_alerts_list_filters_and_scope(client: TestClient, owner_a: User, super_admin: User, read_only: User,
                                       setup_a: dict[str, Any], setup_b: dict[str, Any], alert_world: dict[str, Alert]) -> None:
    w = alert_world
    mine = ok_data(client.get(ALERTS, headers=owner_a.headers))
    assert mine["total"] == 2 and set(_ids(mine)) == {w["a_deleted"].id, w["a_changed"].id}
    item = next(i for i in mine["items"] if i["id"] == w["a_deleted"].id)
    assert item["dedupe_key"] == f"link_deleted:publish_link:{setup_a['link'].id}" and item["payload"] == {"url": setup_a["link"].url}
    assert item["notified_channels"] == ["in_app"] and item["first_triggered_at"].endswith("Z")

    everyone = ok_data(client.get(ALERTS, headers=super_admin.headers))
    assert everyone["total"] == 5
    assert ok_data(client.get(ALERTS, headers=read_only.headers))["total"] == 5             # read_only 为 all 范围
    as_a = ok_data(client.get(ALERTS, params={"owner_id": owner_a.id}, headers=super_admin.headers))
    assert set(_ids(as_a)) == set(_ids(mine))                                               # 用户视角不含系统告警

    timeline = ok_data(client.get(ALERTS, params={"target_type": "publish_link", "target_id": setup_a["link"].id},
                                  headers=owner_a.headers))
    assert set(_ids(timeline)) == {w["a_deleted"].id, w["a_changed"].id}
    assert ok_data(client.get(ALERTS, params={"target_type": "publish_link", "target_id": setup_b["link"].id},
                              headers=owner_a.headers))["total"] == 0                       # 不可见目标 → 空
    assert _ids(ok_data(client.get(ALERTS, params={"target_type": "ai_model"}, headers=super_admin.headers))) == [w["model"].id]
    assert _ids(ok_data(client.get(ALERTS, params={"alert_type": "link_deleted", "project_id": setup_b["project"].id},
                                   headers=super_admin.headers))) == [w["b_deleted"].id]
    assert ok_data(client.get(ALERTS, params={"project_id": setup_b["project"].id}, headers=owner_a.headers))["total"] == 0
    assert set(_ids(ok_data(client.get(ALERTS, params={"severity": "critical"}, headers=super_admin.headers)))) == {w["system"].id}
    future = (utcnow() + timedelta(hours=1)).isoformat() + "Z"
    assert ok_data(client.get(ALERTS, params={"start": future}, headers=super_admin.headers))["total"] == 0
    page = ok_data(client.get(ALERTS, params={"page": 2, "page_size": 2}, headers=super_admin.headers))
    assert page["page"] == 2 and page["page_size"] == 2 and len(page["items"]) == 2
    err(client.get(ALERTS, params={"status": "bogus"}, headers=super_admin.headers), 400)
    err(client.get(ALERTS, params={"target_type": "nope"}, headers=super_admin.headers), 400)

    # 详情：不可见（含系统告警）按不存在处理
    assert ok_data(client.get(f"{ALERTS}/{w['a_deleted'].id}", headers=owner_a.headers))["title"] == "A 删除"
    err(client.get(f"{ALERTS}/{w['system'].id}", headers=owner_a.headers), 404)
    err(client.get(f"{ALERTS}/{w['b_deleted'].id}", headers=owner_a.headers), 404)
    err(client.get(f"{ALERTS}/{w['system'].id}", params={"owner_id": owner_a.id}, headers=super_admin.headers), 404)
    assert ok_data(client.get(f"{ALERTS}/{w['system'].id}", headers=super_admin.headers))["target_type"] == "system"
    err(client.get(f"{ALERTS}/999999", headers=super_admin.headers), 404)


def test_alerts_summary_scope_and_timezone(client: TestClient, db: Session, owner_a: User, super_admin: User,
                                           alert_world: dict[str, Alert]) -> None:
    w = alert_world
    settings_service.set_value(db, "stats_config", {"timezone": "Asia/Shanghai"})
    start, _end = stats_service.day_bounds(stats_service.today_date(db), db)
    yesterday = db.get(Alert, w["b_deleted"].id)
    yesterday.first_triggered_at = start - timedelta(minutes=1)                  # 统计时区的昨天
    db.commit()
    alert_service.acknowledge(db, SYSTEM_SCOPE, w["a_changed"].id, admin_id=super_admin.id)
    alert_service.resolve(db, SYSTEM_SCOPE, w["model"].id, admin_id=super_admin.id)

    everyone = ok_data(client.get(f"{ALERTS}/summary", headers=super_admin.headers))
    assert everyone == {
        "open": {"info": 0, "warning": 2, "critical": 1},
        "acknowledged": {"info": 1, "warning": 0, "critical": 0},
        "today_opened": 4,
        "today_resolved": 1,
    }
    mine = ok_data(client.get(f"{ALERTS}/summary", headers=owner_a.headers))
    assert mine == {"open": {"info": 0, "warning": 1, "critical": 0}, "acknowledged": {"info": 1, "warning": 0, "critical": 0},
                    "today_opened": 2, "today_resolved": 0}
    assert ok_data(client.get(f"{ALERTS}/summary", params={"owner_id": owner_a.id}, headers=super_admin.headers)) == mine


def test_alerts_actions_batch_permissions_and_audit(client: TestClient, db: Session, owner_a: User, read_only: User,
                                                    super_admin: User, alert_world: dict[str, Alert]) -> None:
    w = alert_world
    a_id, c_id = w["a_deleted"].id, w["a_changed"].id
    data = ok_data(client.post(f"{ALERTS}/{a_id}/acknowledge", headers=owner_a.headers))
    assert data["status"] == "acknowledged" and data["acknowledged_by"] == owner_a.id and data["acknowledged_at"]
    body = err(client.post(f"{ALERTS}/{a_id}/acknowledge", headers=owner_a.headers), 409)
    assert body["data"] == {"current_status": "acknowledged"}
    data = ok_data(client.post(f"{ALERTS}/{a_id}/resolve", json={"note": "已联系平台恢复"}, headers=owner_a.headers))
    assert (data["status"], data["resolved_by"], data["resolution_note"]) == ("resolved", owner_a.id, "已联系平台恢复")
    err(client.post(f"{ALERTS}/{a_id}/resolve", headers=owner_a.headers), 409)
    err(client.post(f"{ALERTS}/{a_id}/ignore", json={}, headers=owner_a.headers), 409)
    data = ok_data(client.post(f"{ALERTS}/{c_id}/ignore", json={"note": "页面改版，忽略"}, headers=owner_a.headers))
    assert data["status"] == "ignored" and data["resolution_note"] == "页面改版，忽略"
    err(client.post(f"{ALERTS}/{c_id}/resolve", json={"note": "x" * 501}, headers=owner_a.headers), 400)
    err(client.post(f"{ALERTS}/{w['system'].id}/acknowledge", headers=owner_a.headers), 404)   # 系统告警对普通用户不可见
    err(client.post(f"{ALERTS}/{w['b_deleted'].id}/resolve", headers=owner_a.headers), 404)
    err(client.post(f"{ALERTS}/{w['system'].id}/acknowledge", headers=read_only.headers), 403)

    result = ok_data(client.post(f"{ALERTS}/batch-resolve",
                                 json={"ids": [w["b_deleted"].id, a_id, w["system"].id, 999999], "note": "批量处理"},
                                 headers=owner_a.headers))
    assert result == {"updated": 0, "skipped": [{"id": w["b_deleted"].id, "reason": "not_found"},
                                                {"id": a_id, "reason": "invalid_transition"},
                                                {"id": w["system"].id, "reason": "not_found"},
                                                {"id": 999999, "reason": "not_found"}]}
    result = ok_data(client.post(f"{ALERTS}/batch-resolve", json={"ids": [w["b_deleted"].id, w["system"].id, a_id]},
                                 headers=super_admin.headers))
    assert result == {"updated": 2, "skipped": [{"id": a_id, "reason": "invalid_transition"}]}
    err(client.post(f"{ALERTS}/batch-resolve", json={"ids": []}, headers=super_admin.headers), 400)
    err(client.post(f"{ALERTS}/batch-resolve", json={"ids": [1]}, headers=read_only.headers), 403)

    db.expire_all()
    logs = db.scalars(select(AdminOperationLog).where(AdminOperationLog.target_type == "alert").order_by(AdminOperationLog.id)).all()
    summaries = [log.summary for log in logs]
    assert any(s.startswith(f"确认告警 #{a_id}") for s in summaries)
    assert any(s.startswith(f"解决告警 #{a_id}") and "已联系平台恢复" in s for s in summaries)
    assert any(s.startswith(f"忽略告警 #{c_id}") for s in summaries)
    assert "批量解决告警：2 条，跳过 1 条" in summaries
    assert all(log.action == "update_status" for log in logs)
