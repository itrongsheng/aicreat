"""回填链接、发布平台与删除检测测试（docs/11 §16.1、§16.2 中删除检测相关部分）。

- 单元：URL 规范化与哈希、SSRF 安全抓取（``httpx.MockTransport``，不访问网络）、``judge`` 八条规则表、确认阈值与饱和、
  ``compute_next_check_at`` / ``due`` / ``compute_next_index_check_at``、``enqueue_check``、``schedule_link_checks`` 日上限与
  ``check_type`` 选择、``run_link_checks.process_one``（锁竞争、异常落记录、计数）、平台 schema 校验；
- 集成：回填事务与校验顺序（含 ``owned_by_other`` 单条 / 批量）、入队失败后的基线补检、平台 CRUD / 识别 / 规则测试、
  编辑 / 删除级联 / 暂停 / 恢复 / 重建基线、权限与审计；
- 删除 / 恢复闭环：pytest 内起本地 HTTP 服务，测试夹具 monkeypatch ``safe_fetch.assert_public_url``（只放行回环地址）并放宽
  ``urls.ALLOWED_PORTS``（``normalize_public_url`` 的端口限制），仅在测试进程生效（docs/11 §16.2），生产代码不提供任何 SSRF 豁免。
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import fingerprint, safe_fetch, urls
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.models import (
    AdminOperationLog,
    Alert,
    Content,
    IndexCheck,
    LinkCheck,
    Project,
    PublishLink,
    PublishPlatform,
    utcnow,
)
from app.services import (
    alert_service,
    link_check_service,
    link_service,
    platform_service,
    settings_service,
    stats_service,
)
from app.services.data_scope_service import SYSTEM_SCOPE
from app.tasks import run_link_checks, schedule_link_checks
from tests.conftest import ADMIN_API, User, UserFactory, ok_data

pytestmark = pytest.mark.usefixtures("redis_required")

LINKS = f"{ADMIN_API}/links"
PLATFORMS = f"{ADMIN_API}/platforms"
LINK_CFG = settings_service.DEFAULT_SETTINGS["monitoring_config"]["link_check"]
INDEX_CFG = settings_service.DEFAULT_SETTINGS["monitoring_config"]["index_check"]
LONG_TEXT = "智能门锁选购需要关注锁芯等级、指纹识别速度、电池续航与售后服务。" * 40
OTHER_TEXT = "The quick brown fox jumps over the lazy dog while engineers debate cache invalidation strategies. " * 8


# =====================================================================
# 夹具与助手
# =====================================================================


@pytest.fixture
def owner_a(users: UserFactory) -> User:
    return users.create("operator", username="owner_a", display_name="运营 A")


@pytest.fixture
def owner_b(users: UserFactory) -> User:
    return users.create("operator", username="owner_b", display_name="运营 B")


@pytest.fixture
def platforms(db: Session) -> dict[str, PublishPlatform]:
    from seeds.seed import seed_publish_platforms

    seed_publish_platforms(db)
    return {p.code: p for p in db.scalars(select(PublishPlatform)).all()}


@pytest.fixture
def no_interval(db: Session) -> None:
    """测试中关闭同域名最小间隔（否则同域名连续检测需等待 2s）。"""
    settings_service.set_value(db, "monitoring_config", {"link_check": {"per_domain_interval_seconds": 0}})


def make_project(db: Session, owner: User, name: str = "项目 A", slug: str = "proj-a", status: str = "active") -> Project:
    project = Project(name=name, slug=slug, owner_id=owner.id, created_by=owner.id, status=status)
    db.add(project)
    db.commit()
    return project


def make_content(db: Session, project: Project, owner: User, *, status: str = "approved", title: str = "智能门锁怎么选") -> Content:
    content = Content(project_id=project.id, title=title, language="zh-CN", style="news", status=status, body="正文",
                      created_by=owner.id, updated_by=owner.id)
    db.add(content)
    db.commit()
    return content


def err(response: Any, status: int, code: int | None = None) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == (code if code is not None else status), body
    return body


def backfill(client: TestClient, user: User, content: Content, url: str, **extra: Any) -> Any:
    return client.post(LINKS, json={"content_id": content.id, "url": url, **extra}, headers=user.headers)


def queue_items() -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in redis_client.lrange(link_service.QUEUE_LINK_CHECKS, 0, -1)]


def page(status: int = 200, *, title: str | None = "智能门锁怎么选 - 知乎", text: str = LONG_TEXT, is_html: bool = True,
         final_url: str = "https://zhuanlan.zhihu.com/p/1", chain: list[tuple[int, str]] | None = None,
         headers: dict[str, str] | None = None) -> safe_fetch.PageResult:
    return safe_fetch.PageResult(
        status=status, final_url=final_url, redirect_chain=list(chain or []),
        headers=headers or {"content-type": "text/html; charset=utf-8" if is_html else "application/pdf", "server": "nginx",
                            "set-cookie": "x=1"},
        is_html=is_html, title=title if is_html else None, text=text if is_html else "", response_bytes=1234, duration_ms=12,
    )


def rule_platform(markers: list[str] | None = None, redirects: list[str] | None = None, fetch_config: dict | None = None) -> Any:
    return SimpleNamespace(
        code="zhihu",
        deleted_markers_json=json.dumps(markers if markers is not None else ["内容已被删除", "404 Not Found"], ensure_ascii=False),
        redirect_markers_json=json.dumps(redirects if redirects is not None else [r"^https?://www\.zhihu\.com/signin"]),
        fetch_config_json=json.dumps(fetch_config or {}),
    )


def probe(url: str = "https://zhuanlan.zhihu.com/p/1", *, baseline: bool = False, text: str = LONG_TEXT,
          title: str = "智能门锁怎么选 - 知乎") -> Any:
    return SimpleNamespace(
        url=url,
        baseline_title=title if baseline else None,
        baseline_simhash=fingerprint.text_simhash(text) if baseline else None,
        baseline_captured_at=utcnow() if baseline else None,
    )


def fake_fetch(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[str]:
    calls: list[str] = []

    def _fetch(url: str, *, config: safe_fetch.FetchConfig, transport: Any = None) -> Any:
        calls.append(url)
        value = result() if callable(result) else result
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(safe_fetch, "fetch_page", _fetch)
    return calls


def public_dns(monkeypatch: pytest.MonkeyPatch, ip: str = "93.184.216.34") -> None:
    def _resolve(host: str, port: int, *args: Any, **kwargs: Any) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


def new_link(db: Session, content: Content, platform: PublishPlatform, url: str = "https://zhuanlan.zhihu.com/p/1", **extra: Any) -> PublishLink:
    normalized = urls.normalize_url(url)
    now = utcnow()
    values: dict[str, Any] = dict(
        project_id=content.project_id, content_id=content.id, platform_id=platform.id, url=url, normalized_url=normalized,
        url_hash=urls.url_hash(normalized), domain=urls.extract_domain(normalized), published_at=now, backfilled_by=content.created_by,
        title_snapshot=content.title, alive_status="pending", next_check_at=now, is_monitoring=True,
    )
    values.update(extra)
    link = PublishLink(**values)
    db.add(link)
    content.link_count = int(content.link_count or 0) + 1
    db.commit()
    return link


# =====================================================================
# 1. URL 规范化与哈希（§4.2）
# =====================================================================


def test_normalize_url_and_hash() -> None:
    assert urls.normalize_url("https://www.zhihu.com/question/1/answer/2?utm_source=wechat&spm=a#top") == "https://www.zhihu.com/question/1/answer/2"
    assert urls.normalize_url("https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc&chksm=xyz&scene=126") == \
        "https://mp.weixin.qq.com/s?__biz=MzA&idx=1&mid=1&sn=abc"
    assert urls.normalize_url("HTTPS://Zhuanlan.ZHIHU.com:443/p/1/") == "https://zhuanlan.zhihu.com/p/1"
    assert urls.normalize_url("http://例子.中国/a%7Eb") == "http://xn--fsqu00a.xn--fiqs8s/a~b"
    a = urls.url_hash(urls.normalize_url("https://zhuanlan.zhihu.com/p/1?utm_medium=x"))
    assert a == urls.url_hash("https://zhuanlan.zhihu.com/p/1") and len(a) == 64
    assert urls.extract_domain("https://www.csdn.net/a") == "csdn.net"


# =====================================================================
# 2. SSRF 安全抓取（§6.1）
# =====================================================================


@pytest.mark.parametrize("bad", [
    "ftp://example.com/a", "javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "http://user:pw@example.com/",
    "http://example.com:8080/", "https://example.com:22/",
])
def test_normalize_public_url_rejects(bad: str) -> None:
    with pytest.raises(safe_fetch.FetchBlocked):
        safe_fetch.normalize_public_url(bad)


def test_assert_public_url_blocks_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("http://localhost/", "http://a.local/", "http://svc.internal/", "http://x.localhost/", "http://127.0.0.1/",
                "http://10.0.0.1/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data", "http://[fd00::1]/",
                "http://[::1]/", "http://0.0.0.0/"):
        with pytest.raises(safe_fetch.FetchBlocked):
            safe_fetch.assert_public_url(bad)
    with pytest.raises(safe_fetch.FetchBlocked):
        safe_fetch.normalize_public_url("http://example.com/", allow_http=False)
    public_dns(monkeypatch, "10.1.2.3")
    with pytest.raises(safe_fetch.FetchBlocked):
        safe_fetch.assert_public_url("https://rebind.example.com/")


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_fetch_public_bytes_redirects_headers_and_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    public_dns(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/next", "set-cookie": "sid=abc; Path=/"})
        return httpx.Response(200, html="<html><head><title>你好 - 站点</title></head><body><article>" + LONG_TEXT + "</article></body></html>",
                              headers={"set-cookie": "b=2"})

    config = safe_fetch.FetchConfig(headers={"X-Trace": "1", "Referer": "https://ref.example.com/", "Cookie": "a=1",
                                             "Authorization": "Bearer t", "Host": "evil"}, user_agent="aicreatLinkMonitor/1.0 test")
    result = safe_fetch.fetch_page("https://site.example.com/start", config=config, transport=_transport(handler))
    assert result.status == 200 and result.final_url == "https://site.example.com/next"
    assert result.redirect_chain == [(302, "https://site.example.com/start")] and result.redirects == ["https://site.example.com/start"]
    assert result.is_html and result.title == "你好 - 站点" and result.text.startswith("智能门锁")
    assert "set-cookie" not in result.headers and result.headers["content-type"].startswith("text/html")
    for request in seen:
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.headers["user-agent"] == "aicreatLinkMonitor/1.0 test"
        assert request.headers["x-trace"] == "1" and request.headers["referer"] == "https://ref.example.com/"
        assert request.headers["host"] == "site.example.com"


def test_fetch_public_bytes_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    public_dns(monkeypatch)
    loop = _transport(lambda r: httpx.Response(302, headers={"location": f"/r{len(r.url.path)}"}))
    with pytest.raises(safe_fetch.FetchError) as exc:
        safe_fetch.fetch_public_bytes("https://a.example.com/x", config=safe_fetch.FetchConfig(max_redirects=3), transport=loop)
    assert exc.value.reason == "too_many_redirects" and len(exc.value.redirect_chain) == 4

    to_private = _transport(lambda r: httpx.Response(301, headers={"location": "http://169.254.169.254/latest"}))
    with pytest.raises(safe_fetch.FetchBlocked):
        safe_fetch.fetch_page("https://a.example.com/x", config=safe_fetch.FetchConfig(), transport=to_private)

    big = _transport(lambda r: httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"}))
    with pytest.raises(safe_fetch.FetchError) as exc:
        safe_fetch.fetch_public_bytes("https://a.example.com/x", config=safe_fetch.FetchConfig(max_response_bytes=1024), transport=big)
    assert exc.value.reason == "response_too_large"

    pdf = _transport(lambda r: httpx.Response(200, content=b"%PDF-1.4 ...", headers={"content-type": "application/pdf"}))
    result = safe_fetch.fetch_page("https://a.example.com/x.pdf", config=safe_fetch.FetchConfig(), transport=pdf)
    assert not result.is_html and result.title is None and result.text == "" and result.response_bytes == 12

    gbk = _transport(lambda r: httpx.Response(200, content="<html><title>中文标题</title><body>正文</body></html>".encode("gbk"),
                                              headers={"content-type": "text/html; charset=gbk"}))
    assert safe_fetch.fetch_page("https://a.example.com/g", config=safe_fetch.FetchConfig(), transport=gbk).title == "中文标题"

    not_found = _transport(lambda r: httpx.Response(404, text="nope", headers={"content-type": "text/plain"}))
    assert safe_fetch.fetch_page("https://a.example.com/404", config=safe_fetch.FetchConfig(), transport=not_found).status == 404

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(safe_fetch.FetchError) as exc:
        safe_fetch.fetch_page("https://a.example.com/slow", config=safe_fetch.FetchConfig(timeout_seconds=15), transport=_transport(timeout))
    assert str(exc.value) == "ReadTimeout after 15s" and not isinstance(exc.value, safe_fetch.FetchBlocked)


def test_robots_allowed_and_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    public_dns(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.host == "r.example.com":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        if request.url.host == "none.example.com":
            return httpx.Response(404)
        return httpx.Response(500)

    t = _transport(handler)
    assert safe_fetch.robots_allowed("https://r.example.com/public", user_agent="aicreat", timeout=5, transport=t)
    assert not safe_fetch.robots_allowed("https://r.example.com/private/x", user_agent="aicreat", timeout=5, transport=t)
    assert calls.count("/robots.txt") == 1                                      # 第二次命中 robots:{domain} 缓存
    assert safe_fetch.robots_allowed("https://none.example.com/a", user_agent="aicreat", timeout=5, transport=t)
    assert not safe_fetch.robots_allowed("https://err.example.com/a", user_agent="aicreat", timeout=5, transport=t)
    assert 0 < redis_client.ttl("robots:err.example.com") <= 900
    config = safe_fetch.FetchConfig(respect_robots=True)
    with pytest.raises(safe_fetch.FetchBlocked) as exc:
        safe_fetch.fetch_page("https://r.example.com/private/y", config=config, transport=t)
    assert exc.value.reason == "blocked_by_robots"


# =====================================================================
# 3. judge 八条规则（§6.3）
# =====================================================================


def test_judge_rules_table() -> None:
    plat = rule_platform()
    link = probe()
    based = probe(baseline=True)

    assert link_check_service.judge(safe_fetch.FetchBlocked("x"), link, plat, check_type="scheduled")[:2] == ("unknown", "ssrf_blocked")
    assert link_check_service.judge(safe_fetch.FetchBlocked("blocked_by_robots", reason="blocked_by_robots"), link, plat,
                                    check_type="scheduled")[:2] == ("unknown", "blocked_by_robots")
    assert link_check_service.judge(safe_fetch.FetchError("dns_failed"), link, plat, check_type="scheduled")[:2] == ("unknown", "network_error")
    for status in (500, 503, 401, 403, 429, 400):
        assert link_check_service.judge(page(status), based, plat, check_type="scheduled")[:2] == ("unknown", "network_error")
    for status in (404, 410, 451):
        assert link_check_service.judge(page(status), link, plat, check_type="baseline")[:2] == ("suspected_deleted", f"http_{status}")
        assert link_check_service.judge(page(status), based, plat, check_type="scheduled")[:2] == ("deleted", f"http_{status}")
    # 规则 3 先于规则 4：跳转后 404 仍按 http_404
    redirected_404 = page(404, chain=[(302, "https://zhuanlan.zhihu.com/p/1")], final_url="https://www.zhihu.com/")
    assert link_check_service.judge(redirected_404, based, plat, check_type="scheduled")[:2] == ("deleted", "http_404")
    # 规则 4：跳转首页 / 登录页
    home = page(chain=[(302, "https://zhuanlan.zhihu.com/p/1")], final_url="https://www.zhihu.com/")
    assert link_check_service.judge(home, based, plat, check_type="scheduled")[:2] == ("suspected_deleted", "redirect_home")
    login = page(chain=[(302, "https://zhuanlan.zhihu.com/p/1")], final_url="https://www.zhihu.com/signin?next=/p/1")
    status, rule, evidence = link_check_service.judge(login, based, plat, check_type="scheduled")
    assert (status, rule) == ("suspected_deleted", "redirect_login") and evidence["redirects"] == ["https://zhuanlan.zhihu.com/p/1"]
    passport = page(chain=[(302, "https://blog.csdn.net/a/1")], final_url="https://passport.csdn.net/login")
    csdn_link = probe("https://blog.csdn.net/a/1", baseline=True)
    assert link_check_service.judge(passport, csdn_link, rule_platform(redirects=[r"^https?://passport\.csdn\.net/"]),
                                    check_type="scheduled")[1] == "redirect_login"
    # 未命中 redirect_markers 且最终 path 非 "/"：不是规则 4（登录页须由平台规则识别）
    assert link_check_service.judge(passport, csdn_link, rule_platform(redirects=[]), check_type="scheduled")[:2] == ("alive", "ok")
    moved = page(chain=[(301, "http://zhuanlan.zhihu.com/p/1")], final_url="https://zhuanlan.zhihu.com/p/1")
    assert link_check_service.judge(moved, based, plat, check_type="scheduled")[:2] == ("alive", "ok")
    # 规则 5：删除文案（title 与正文前 2000 字符，忽略大小写；基线同样立即 deleted）
    hit = page(title="提示", text="很抱歉，内容已被删除，返回首页看看其它内容吧。" + LONG_TEXT)
    status, rule, evidence = link_check_service.judge(hit, link, plat, check_type="baseline")
    assert (status, rule) == ("deleted", "marker:内容已被删除")
    assert evidence["marker"] == "内容已被删除" and "内容已被删除" in evidence["context"] and evidence["headers"].get("set-cookie") is None
    assert link_check_service.judge(page(title="404 not found"), based, plat, check_type="scheduled")[1] == "marker:404 Not Found"
    far = page(text="甲" * 2100 + "内容已被删除")
    assert link_check_service.judge(far, link, plat, check_type="scheduled")[:2] == ("alive", "ok")      # 超出前 2000 字符
    long_marker = rule_platform(markers=["很" * 100])
    assert len(link_check_service.judge(page(title="很" * 100), based, long_marker, check_type="scheduled")[1]) == 100
    # 规则 6：标题 / 正文变化
    assert link_check_service.judge(page(title="完全不同的标题 - 知乎"), based, plat, check_type="scheduled")[:2] == ("changed", "title_changed")
    assert link_check_service.judge(page(title="智能门锁怎么选 | 知乎"), based, plat, check_type="scheduled")[:2] == ("alive", "ok")
    assert link_check_service.judge(page(text=OTHER_TEXT), based, plat, check_type="scheduled")[:2] == ("changed", "body_changed")
    no_title_cmp = dict(LINK_CFG, title_compare=False)
    assert link_check_service.judge(page(title="完全不同的标题"), based, plat, check_type="scheduled", cfg=no_title_cmp)[:2] == ("alive", "ok")
    # 规则 7：无基线 → alive 并写基线
    judgement = link_check_service.evaluate(page(), link, plat, check_type="baseline", cfg=LINK_CFG)
    assert (judgement.result_status, judgement.matched_rule) == ("alive", "ok")
    assert judgement.baseline["baseline_title"] == "智能门锁怎么选 - 知乎" and judgement.baseline["baseline_simhash"] is not None
    assert len(judgement.baseline["baseline_excerpt"]) == 1000 and judgement.evidence["short_text"] is False
    short = link_check_service.evaluate(page(text="很短的正文"), link, plat, check_type="baseline", cfg=LINK_CFG)
    assert short.simhash is None and short.baseline["baseline_simhash"] is None and short.evidence["short_text"] is True
    # 规则 8：非 HTML 200 只按状态码（不做文案匹配、不写基线、不做变更检测）
    pdf = link_check_service.evaluate(page(is_html=False), based, plat, check_type="scheduled", cfg=LINK_CFG)
    assert (pdf.result_status, pdf.matched_rule, pdf.baseline, pdf.simhash) == ("alive", "ok", None, None)
    assert pdf.evidence["short_text"] is True
    assert link_check_service.judge(page(204), based, plat, check_type="scheduled")[:2] == ("alive", "ok")


def test_build_fetch_config_override() -> None:
    plat = rule_platform(fetch_config={"user_agent": "Mobile aicreatLinkMonitor/1.0", "headers": {"X-A": "1"}, "timeout_seconds": 8,
                                       "allow_http": False})
    cfg = link_check_service.build_fetch_config(plat, {"link_check": dict(LINK_CFG, max_redirects=2, respect_robots=True)})
    assert cfg.user_agent == "Mobile aicreatLinkMonitor/1.0" and cfg.headers == {"X-A": "1"} and cfg.timeout_seconds == 8
    assert cfg.allow_http is False and cfg.max_redirects == 2 and cfg.respect_robots is True and cfg.max_response_bytes == 2097152
    base = link_check_service.build_fetch_config(rule_platform(), LINK_CFG)
    assert base.user_agent == LINK_CFG["user_agent"] and base.headers == {} and base.allow_http is True


# =====================================================================
# 4. 确认阈值与排程（§6.4、§6.6、§7.5）
# =====================================================================


def test_thresholds_counters_and_saturation() -> None:
    apply = link_check_service.apply_thresholds
    assert apply("alive", "unknown", 0, 0, LINK_CFG) == ("alive", 1, 0)
    assert apply("alive", "unknown", 1, 0, LINK_CFG) == ("alive", 2, 0)
    assert apply("alive", "unknown", 2, 0, LINK_CFG) == ("unknown", 3, 0)
    assert apply("pending", "unknown", 0, 0, LINK_CFG) == ("pending", 1, 0)
    assert apply("deleted", "unknown", 2, 0, LINK_CFG) == ("unknown", 3, 0)
    assert apply("alive", "suspected_deleted", 0, 0, LINK_CFG) == ("suspected_deleted", 0, 1)
    assert apply("suspected_deleted", "suspected_deleted", 0, 1, LINK_CFG) == ("deleted", 0, 2)
    assert apply("deleted", "suspected_deleted", 0, 0, LINK_CFG) == ("deleted", 0, 1)        # deleted 下跳转保持 deleted
    # 混合序列：suspected → unknown → unknown 清零后各自累计
    assert apply("suspected_deleted", "unknown", 0, 1, LINK_CFG) == ("suspected_deleted", 1, 0)
    assert apply("suspected_deleted", "alive", 2, 0, LINK_CFG) == ("alive", 0, 0)
    assert apply("alive", "unknown", 100, 0, LINK_CFG) == ("unknown", 100, 0)                 # 饱和于 100
    assert apply("deleted", "suspected_deleted", 0, 100, LINK_CFG) == ("deleted", 0, 100)
    assert apply("alive", "deleted", 0, 0, LINK_CFG) == ("deleted", 0, 0)
    assert apply("alive", "changed", 1, 0, LINK_CFG) == ("changed", 0, 0)


def _link_ns(**kw: Any) -> Any:
    now = utcnow()
    values = dict(is_monitoring=True, published_at=now, alive_changed_at=now, consecutive_unknown=0, consecutive_suspected=0,
                  index_check_count=0, alive_status="alive", seo_status_json=None, geo_status_json=None)
    values.update(kw)
    return SimpleNamespace(**values)


def test_compute_next_check_at() -> None:
    now = utcnow()
    nxt = link_service.compute_next_check_at
    assert nxt(_link_ns(), "alive", "pending", "alive", now=now, cfg=LINK_CFG) == now + timedelta(hours=24)
    old = _link_ns(published_at=now - timedelta(days=8))
    assert nxt(old, "alive", "alive", "alive", now=now, cfg=LINK_CFG) == now + timedelta(days=7)
    for n, hours in ((1, 6), (2, 12), (3, 24), (100, 24)):
        assert nxt(_link_ns(consecutive_unknown=n), "unknown", "alive", "alive", now=now, cfg=LINK_CFG) == now + timedelta(hours=hours)
    assert nxt(_link_ns(consecutive_suspected=1), "suspected_deleted", "alive", "suspected_deleted", now=now, cfg=LINK_CFG) == \
        now + timedelta(hours=6)
    assert nxt(_link_ns(), "changed", "alive", "changed", now=now, cfg=LINK_CFG) == now + timedelta(hours=6)
    assert nxt(old, "changed", "changed", "changed", now=now, cfg=LINK_CFG) == now + timedelta(days=7)
    deleted = _link_ns(alive_changed_at=now)
    assert nxt(deleted, "deleted", "alive", "deleted", now=now, cfg=LINK_CFG) == now + timedelta(days=7)
    assert nxt(_link_ns(consecutive_suspected=2), "suspected_deleted", "suspected_deleted", "deleted", now=now, cfg=LINK_CFG) == \
        now + timedelta(days=7)
    stale = _link_ns(alive_changed_at=now - timedelta(days=24))
    assert nxt(stale, "suspected_deleted", "deleted", "deleted", now=now, cfg=LINK_CFG) is None      # 超过 30 天不再检测
    assert nxt(_link_ns(is_monitoring=False), "alive", "alive", "alive", now=now, cfg=LINK_CFG) is None


def test_due_and_compute_next_index_check_at() -> None:
    now = utcnow()
    published = now - timedelta(days=10)
    link = _link_ns(published_at=published, index_check_count=link_service.expired_rounds(published, now, [1, 3, 7, 14, 30]))
    assert link.index_check_count == 3
    assert link_service.due(None, link, now=now, cfg=INDEX_CFG) == published + timedelta(days=14)
    late = _link_ns(published_at=now - timedelta(days=400), index_check_count=5)
    assert link_service.due(None, late, now=now, cfg=INDEX_CFG) == now                               # 轮次越界 → now
    checked = published + timedelta(days=3, hours=1)
    iso = checked.isoformat() + "Z"
    assert link_service.due({"status": "indexed", "checked_at": iso, "check_count": 2}, link, now=now, cfg=INDEX_CFG) == \
        checked + timedelta(days=90)
    assert link_service.due({"status": "not_indexed", "checked_at": iso, "check_count": 2}, link, now=now, cfg=INDEX_CFG) == \
        published + timedelta(days=7)
    after_all = (published + timedelta(days=31)).isoformat() + "Z"
    assert link_service.due({"status": "unknown", "checked_at": after_all, "check_count": 5}, link, now=now, cfg=INDEX_CFG) == \
        published + timedelta(days=61)
    assert link_service.due({"status": "not_indexed", "checked_at": iso, "check_count": 24}, link, now=now, cfg=INDEX_CFG) is None

    engines = [("seo", "baidu"), ("geo", "doubao")]
    link.seo_status_json = json.dumps({"baidu": {"status": "indexed", "checked_at": iso, "check_count": 1}})
    assert link_service.compute_next_index_check_at(link, now=now, cfg=INDEX_CFG, engines=engines) == published + timedelta(days=14)
    assert link_service.compute_next_index_check_at(link, now=now, cfg=INDEX_CFG, engines=[]) is None
    link.alive_status = "deleted"
    assert link_service.compute_next_index_check_at(link, now=now, cfg=INDEX_CFG, engines=engines) is None
    link.alive_status = "alive"
    link.is_monitoring = False
    assert link_service.compute_next_index_check_at(link, now=now, cfg=INDEX_CFG, engines=engines) is None


# =====================================================================
# 5. 入队、调度与消费（§6.7）
# =====================================================================


def test_enqueue_check_semantics(db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    link = new_link(db, content, platforms["zhihu"])
    original = link.next_check_at
    assert link_service.enqueue_check(link, "manual", owner_a.id, db=db) is True
    assert link.next_check_at == original                                                # manual 不触碰
    assert link_service.enqueue_check(link, "scheduled", None, db=db) is False           # 已在队列
    redis_client.delete(link_service.queued_key(link.id))
    other = new_link(db, content, platforms["zhihu"], url="https://zhuanlan.zhihu.com/p/2")
    assert link_service.enqueue_check(other, "scheduled", None, db=db) is True
    db.refresh(other)
    assert other.next_check_at >= utcnow() + timedelta(minutes=59)
    assert link_service.enqueue_check(link, "manual", owner_a.id) is True
    items = queue_items()
    assert [i["link_id"] for i in items] == [link.id, link.id, other.id]                  # manual LPUSH 插队
    assert items[0] == {"link_id": link.id, "check_type": "manual", "triggered_by": owner_a.id}
    assert 0 < redis_client.ttl(link_service.queued_key(other.id)) <= 3600


def test_schedule_link_checks_limit_and_types(db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    past = utcnow() - timedelta(minutes=5)
    baseline = new_link(db, content, platforms["zhihu"], url="https://zhuanlan.zhihu.com/p/10", next_check_at=past)
    retry = new_link(db, content, platforms["zhihu"], url="https://zhuanlan.zhihu.com/p/11", next_check_at=past - timedelta(minutes=1),
                     check_count=2, consecutive_unknown=1, alive_status="alive")
    scheduled = new_link(db, content, platforms["zhihu"], url="https://zhuanlan.zhihu.com/p/12", next_check_at=past - timedelta(minutes=2),
                         check_count=3, alive_status="alive")
    paused = new_link(db, content, platforms["zhihu"], url="https://zhuanlan.zhihu.com/p/13", next_check_at=past, is_monitoring=False)
    future = new_link(db, content, platforms["zhihu"], url="https://zhuanlan.zhihu.com/p/14", next_check_at=utcnow() + timedelta(hours=3))

    key = link_check_service.limit_key(db)
    redis_client.set(key, 5000)                                                          # 已达日上限：本轮不入队、next_check_at 不变
    assert schedule_link_checks.enqueue_due() == 0
    db.expire_all()
    assert db.get(PublishLink, baseline.id).next_check_at == past and queue_items() == []

    redis_client.set(key, 4998)                                                          # 剩余 2：按 next_check_at 顺序入队 2 条
    assert schedule_link_checks.enqueue_due() == 2
    assert {(i["link_id"], i["check_type"]) for i in queue_items()} == {(scheduled.id, "scheduled"), (retry.id, "retry")}
    redis_client.set(key, 0)
    assert schedule_link_checks.enqueue_due() == 1
    assert queue_items()[-1] == {"link_id": baseline.id, "check_type": "baseline", "triggered_by": None}
    db.expire_all()
    assert db.get(PublishLink, baseline.id).next_check_at > utcnow() + timedelta(minutes=59)
    assert db.get(PublishLink, paused.id).next_check_at == past
    assert db.get(PublishLink, future.id).next_check_at > utcnow()

    settings_service.set_value(db, "monitoring_config", {"link_check": {"enabled": False}})
    db.get(PublishLink, retry.id).next_check_at = past
    db.commit()
    redis_client.delete(link_service.queued_key(retry.id))
    assert schedule_link_checks.enqueue_due() == 0                                       # enabled=false 不扫描


def test_process_one_lock_exception_and_counting(db: Session, owner_a: User, platforms: dict[str, PublishPlatform],
                                                 no_interval: None, monkeypatch: pytest.MonkeyPatch) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    link = new_link(db, content, platforms["zhihu"])
    calls = fake_fetch(monkeypatch, page())
    payload = json.dumps({"link_id": link.id, "check_type": "baseline", "triggered_by": None})

    # 锁竞争：丢弃元素且不删除标记
    redis_client.set(link_service.queued_key(link.id), "1", ex=3600)
    token = acquire_lock(run_link_checks.lock_key(link.id), 300)
    assert run_link_checks.process_one(payload) is False
    assert redis_client.exists(link_service.queued_key(link.id)) and calls == []
    release_lock(run_link_checks.lock_key(link.id), token)

    # 正常：抓取前计数，写回后删除标记与锁
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    assert run_link_checks.process_one(payload, semaphore=sem) is True
    assert sem.acquire(blocking=False)                                                  # 信号量已释放
    assert calls == [link.url] and int(redis_client.get(link_check_service.limit_key(db))) == 1
    assert not redis_client.exists(link_service.queued_key(link.id)) and not redis_client.exists(run_link_checks.lock_key(link.id))
    db.expire_all()
    stored = db.get(PublishLink, link.id)
    assert stored.alive_status == "alive" and stored.baseline_captured_at is not None and stored.check_count == 1

    # 异常：落 unknown / network_error 记录并清理标记
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom secret-body")

    monkeypatch.setattr(link_check_service, "evaluate", boom)
    redis_client.set(link_service.queued_key(link.id), "1", ex=3600)
    assert run_link_checks.process_one(json.dumps({"link_id": link.id, "check_type": "scheduled"})) is False
    record = db.scalars(select(LinkCheck).where(LinkCheck.link_id == link.id).order_by(LinkCheck.id.desc())).first()
    assert (record.result_status, record.matched_rule, record.applied_status) == ("unknown", "network_error", "alive")
    assert record.error_message.startswith("RuntimeError: ")
    assert not redis_client.exists(link_service.queued_key(link.id))
    db.expire_all()
    assert db.get(PublishLink, link.id).consecutive_unknown == 1

    # 暂停监控：非手动元素丢弃，手动仍执行一次
    monkeypatch.undo()
    fake_fetch(monkeypatch, page())
    stored = db.get(PublishLink, link.id)
    stored.is_monitoring = False
    db.commit()
    before = db.scalar(select(func.count(LinkCheck.id)))
    assert run_link_checks.process_one(json.dumps({"link_id": link.id, "check_type": "scheduled"})) is False
    assert run_link_checks.process_one(json.dumps({"link_id": link.id, "check_type": "manual", "triggered_by": owner_a.id})) is True
    assert db.scalar(select(func.count(LinkCheck.id))) == before + 1
    assert run_link_checks.process_one(json.dumps({"link_id": 999999, "check_type": "manual"})) is False
    assert run_link_checks.process_one("not-json") is False


def test_drain_respects_semaphore(db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    for i in range(3):
        link_service.enqueue_check(new_link(db, content, platforms["zhihu"], url=f"https://zhuanlan.zhihu.com/p/{100 + i}"), "scheduled", None)
    submitted: list[Any] = []
    pool = SimpleNamespace(is_full=lambda: False, submit=lambda fn, raw, semaphore=None: submitted.append((raw, semaphore)))
    sems = {"link_check": threading.BoundedSemaphore(2)}
    assert run_link_checks.drain(pool, limit=20, sems=sems) == 2                         # 信号量只有 2
    assert redis_client.llen(link_service.QUEUE_LINK_CHECKS) == 1


# =====================================================================
# 6. 回填（§4.1、§4.4）与数据范围
# =====================================================================


def test_backfill_transaction(client: TestClient, db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    project = make_project(db, owner_a)
    content = make_content(db, project, owner_a)
    data = ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/123456?utm_source=wechat#x",
                            publish_account="品牌官方号", published_at="2026-01-02T03:04:05Z"))
    assert data["queued"] is True and "reason" not in data
    link = data["link"]
    assert "platform" not in link and link["platform_id"] == platforms["zhihu"].id
    assert link["normalized_url"] == "https://zhuanlan.zhihu.com/p/123456" and link["domain"] == "zhuanlan.zhihu.com"
    assert link["url"] == "https://zhuanlan.zhihu.com/p/123456?utm_source=wechat#x"
    assert link["alive_status"] == "pending" and link["backfilled_by"] == owner_a.id and link["title_snapshot"] == content.title
    assert link["published_at"] == "2026-01-02T03:04:05Z" and link["is_monitoring"] is True
    assert link["index_check_count"] == 5 and link["index_checks_done"] == 0               # 早已发布：全部轮次已过期
    next_check = datetime.fromisoformat(link["next_check_at"].rstrip("Z"))
    assert next_check >= utcnow() + timedelta(minutes=59)                                   # 入队成功后推后 1h
    assert queue_items() == [{"link_id": link["id"], "check_type": "baseline", "triggered_by": owner_a.id}]
    db.expire_all()
    stored_content = db.get(Content, content.id)
    assert stored_content.status == "published" and stored_content.link_count == 1
    assert stored_content.first_published_at == datetime(2026, 1, 2, 3, 4, 5)
    today_key = stats_service.realtime_key(stats_service.today_date(db), project.id)
    assert redis_client.hget(today_key, "links_backfilled") == "1"
    assert redis_client.hget(stats_service.realtime_key(stats_service.today_date(db), 0), "links_backfilled") == "1"
    assert redis_client.hget(today_key, "contents_published") is None                       # 归属日期为过去，不写 Redis

    second = ok_data(backfill(client, owner_a, content, "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=abc&chksm=xyz"))
    assert second["link"]["platform_id"] == platforms["wechat_mp"].id
    assert second["link"]["index_check_count"] == 0
    published = datetime.fromisoformat(second["link"]["published_at"].rstrip("Z"))
    assert second["link"]["next_index_check_at"] == (published + timedelta(days=1)).isoformat() + "Z"
    other = ok_data(backfill(client, owner_a, content, "https://example.org/blog/a"))
    assert other["link"]["platform_id"] == platforms["website"].id                          # 未命中归 website
    db.expire_all()
    assert db.get(Content, content.id).link_count == 3
    log = db.scalars(select(AdminOperationLog).where(AdminOperationLog.target_type == "publish_link")).first()
    assert log is not None and log.action == "create" and log.target_id == str(link["id"])


def test_backfill_validation_order(client: TestClient, db: Session, owner_a: User, owner_b: User,
                                   platforms: dict[str, PublishPlatform]) -> None:
    project = make_project(db, owner_a)
    content = make_content(db, project, owner_a)
    draft = make_content(db, project, owner_a, status="draft", title="草稿")
    body = err(backfill(client, owner_a, draft, "https://zhuanlan.zhihu.com/p/1"), 409)
    assert body["message"] == "内容尚未审核通过" and body["data"] == {"current_status": "draft"}
    b_content = make_content(db, make_project(db, owner_b, slug="proj-b"), owner_b)
    assert err(backfill(client, owner_a, b_content, "https://zhuanlan.zhihu.com/p/1"), 404)["data"] is None
    archived = make_project(db, owner_a, name="归档项目", slug="arch", status="archived")
    body = err(backfill(client, owner_a, make_content(db, archived, owner_a), "https://zhuanlan.zhihu.com/p/1"), 409)
    assert body["data"] == {"current_status": "archived"}
    body = err(backfill(client, owner_a, content, "ftp://example.com/a"), 400)
    assert body["data"][0]["loc"] == ["body", "url"] and body["data"][0]["input"] == "ftp://example.com/a"
    assert body["data"][0]["type"] == "value_error"
    for bad in ("http://user:pw@example.com/", "https://example.com:8443/a", "javascript:alert(1)"):
        err(backfill(client, owner_a, content, bad), 400)
    assert err(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1", platform_id=99999), 404)["message"] == "平台不存在"
    platforms["csdn"].is_active = False
    db.commit()
    body = err(backfill(client, owner_a, content, "https://blog.csdn.net/a/1", platform_id=platforms["csdn"].id), 409)
    assert body["message"] == "平台已停用"
    future = (utcnow() + timedelta(minutes=10)).isoformat() + "Z"
    body = err(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1", published_at=future), 400)
    assert body["data"][0]["loc"] == ["body", "published_at"] and body["data"][0]["msg"] == "发布时间超出允许范围"
    err(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1", published_at="2010-01-01T00:00:00Z"), 400)
    body = err(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1", publish_account="a" * 101), 400)
    assert body["data"][0]["loc"] == ["body", "publish_account"]
    assert err(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1", note="n" * 501), 400)["data"][0]["loc"] == ["body", "note"]
    # 允许早于内容 created_at（补录历史文章）
    early = (utcnow() - timedelta(days=400)).isoformat() + "Z"
    ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1", published_at=early))
    assert db.scalar(select(func.count(PublishLink.id))) == 1


def test_duplicate_and_owned_by_other(client: TestClient, db: Session, owner_a: User, owner_b: User, super_admin: User,
                                      platforms: dict[str, PublishPlatform]) -> None:
    a_content = make_content(db, make_project(db, owner_a), owner_a)
    b_content = make_content(db, make_project(db, owner_b, slug="proj-b"), owner_b)
    b_link = ok_data(backfill(client, owner_b, b_content, "https://zhuanlan.zhihu.com/p/777"))["link"]
    body = err(backfill(client, owner_a, a_content, "https://zhuanlan.zhihu.com/p/777/?utm_source=x#frag"), 409)
    assert body["message"] == "该链接已由其他用户回填" and body["data"] == {"existing_id": None, "reason": "owned_by_other"}
    body = err(backfill(client, owner_b, b_content, "HTTPS://ZHUANLAN.zhihu.com/p/777"), 409)
    assert body["message"] == "链接已存在" and body["data"] == {"existing_id": b_link["id"]}
    body = err(backfill(client, super_admin, a_content, "https://zhuanlan.zhihu.com/p/777"), 409)
    assert body["data"] == {"existing_id": b_link["id"]}

    a_link = ok_data(backfill(client, owner_a, a_content, "https://zhuanlan.zhihu.com/p/1"))["link"]
    items = [
        {"content_id": a_content.id, "url": "https://zhuanlan.zhihu.com/p/777"},
        {"content_id": a_content.id, "url": "https://zhuanlan.zhihu.com/p/2", "publish_account": "账号"},
        {"content_id": a_content.id, "url": "https://zhuanlan.zhihu.com/p/1?spm=1"},
        {"content_id": a_content.id, "url": "ftp://bad"},
        {"content_id": b_content.id, "url": "https://zhuanlan.zhihu.com/p/3"},
        {"content_id": "x", "url": "https://zhuanlan.zhihu.com/p/4"},
        {"content_id": a_content.id, "url": "https://zhuanlan.zhihu.com/p/2"},
    ]
    data = ok_data(client.post(f"{LINKS}/batch", json={"items": items}, headers=owner_a.headers))
    assert data["created"] == 1 and data["failed"] == 6
    r = data["results"]
    assert r[0] == {"index": 0, "ok": False, "link_id": None, "queued": False, "code": 409, "message": "该链接已由其他用户回填",
                    "reason": "owned_by_other"}
    assert r[1]["ok"] is True and r[1]["code"] == 0 and r[1]["message"] == "ok" and r[1]["queued"] is True and "reason" not in r[1]
    assert r[2] == {"index": 2, "ok": False, "link_id": a_link["id"], "queued": False, "code": 409, "message": "链接已存在"}
    assert r[3]["code"] == 400 and r[3]["link_id"] is None and "reason" not in r[3]
    assert r[4]["code"] == 404
    assert r[5]["code"] == 400
    assert r[6]["code"] == 409 and r[6]["link_id"] == r[1]["link_id"]
    too_many = [{"content_id": a_content.id, "url": f"https://zhuanlan.zhihu.com/p/{i}"} for i in range(101)]
    err(client.post(f"{LINKS}/batch", json={"items": too_many}, headers=owner_a.headers), 400)

    # 列表 / 详情范围
    listed = ok_data(client.get(LINKS, headers=owner_a.headers))
    assert {i["id"] for i in listed["items"]} == {a_link["id"], r[1]["link_id"]} and listed["total"] == 2
    assert ok_data(client.get(LINKS, params={"project_id": b_content.project_id}, headers=owner_a.headers))["total"] == 0
    err(client.get(f"{LINKS}/{b_link['id']}", headers=owner_a.headers), 404)
    err(client.post(f"{LINKS}/{b_link['id']}/check", headers=owner_a.headers), 404)
    assert ok_data(client.get(LINKS, headers=super_admin.headers))["total"] == 3
    assert ok_data(client.get(LINKS, params={"owner_id": owner_b.id}, headers=super_admin.headers))["total"] == 1
    found = ok_data(client.get(LINKS, params={"keyword": "p/2"}, headers=owner_a.headers))
    assert [i["id"] for i in found["items"]] == [r[1]["link_id"]]


def test_enqueue_failure_falls_back_to_scheduler(client: TestClient, db: Session, owner_a: User, platforms: dict[str, PublishPlatform],
                                                 no_interval: None, monkeypatch: pytest.MonkeyPatch) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    real_set = redis_client.set

    def broken_set(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("queued:link_check:"):
            raise redis.ConnectionError("redis down")
        return real_set(name, *args, **kwargs)

    monkeypatch.setattr(redis_client, "set", broken_set)
    data = ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/9"))
    monkeypatch.setattr(redis_client, "set", real_set)
    assert data["queued"] is False and queue_items() == []
    db.expire_all()
    link = db.get(PublishLink, data["link"]["id"])
    assert link.next_check_at <= utcnow() and link.check_count == 0                     # 已落库且到期
    assert schedule_link_checks.enqueue_due() == 1
    assert queue_items() == [{"link_id": link.id, "check_type": "baseline", "triggered_by": None}]
    fake_fetch(monkeypatch, page(404))
    assert run_link_checks.process_one(redis_client.lpop(link_service.QUEUE_LINK_CHECKS)) is True
    record = db.scalars(select(LinkCheck).where(LinkCheck.link_id == link.id)).one()
    assert (record.check_type, record.result_status, record.applied_status, record.matched_rule) == \
        ("baseline", "suspected_deleted", "suspected_deleted", "http_404")


# =====================================================================
# 7. 发布平台（§5）
# =====================================================================


def test_platform_schema_and_crud(client: TestClient, db: Session, super_admin: User, owner_a: User,
                                  platforms: dict[str, PublishPlatform]) -> None:
    base = {"code": "juejin", "name": "掘金", "name_en": "Juejin", "url_patterns": [r"^https?://juejin\.cn/"],
            "deleted_markers": ["文章不存在"], "redirect_markers": [], "fetch_config": {}}
    body = err(client.post(PLATFORMS, json={**base, "url_patterns": [r"^ok$", r"^https?://(www\.example\.com/"]},
                           headers=super_admin.headers), 400)
    item = body["data"][0]
    assert item["loc"] == ["body", "url_patterns", 1] and item["msg"].startswith("正则无法编译：")
    assert item["type"] == "value_error" and item["input"] == r"^https?://(www\.example\.com/"
    body = err(client.post(PLATFORMS, json={**base, "redirect_markers": ["("]}, headers=super_admin.headers), 400)
    assert body["data"][0]["loc"] == ["body", "redirect_markers", 0]
    body = err(client.post(PLATFORMS, json={**base, "deleted_markers": ["删除"]}, headers=super_admin.headers), 400)
    assert body["data"][0]["loc"] == ["body", "deleted_markers", 0]
    err(client.post(PLATFORMS, json={**base, "deleted_markers": ["很" * 101]}, headers=super_admin.headers), 400)
    err(client.post(PLATFORMS, json={**base, "deleted_markers": [f"文案{i:04d}" for i in range(51)]}, headers=super_admin.headers), 400)
    for headers in ({"Cookie": "a=1"}, {"authorization": "x"}, {"Proxy-Authorization": "x"}, {"User-Agent": "x"}, {"Host": "x"}):
        body = err(client.post(PLATFORMS, json={**base, "fetch_config": {"headers": headers}}, headers=super_admin.headers), 400)
        assert body["data"][0]["loc"] == ["body", "fetch_config", "headers"]
    body = err(client.post(PLATFORMS, json={**base, "fetch_config": {"user_agent": "Mozilla/5.0"}}, headers=super_admin.headers), 400)
    assert body["data"][0]["loc"] == ["body", "fetch_config", "user_agent"]
    err(client.post(PLATFORMS, json={**base, "fetch_config": {"user_agent": "aicreat " + "x" * 200}}, headers=super_admin.headers), 400)
    err(client.post(PLATFORMS, json={**base, "code": "Bad-Code"}, headers=super_admin.headers), 400)
    err(client.post(PLATFORMS, json=base, headers=owner_a.headers), 403)

    platform_service.all_platforms(db)
    assert redis_client.exists(platform_service.CACHE_KEY)
    created = ok_data(client.post(PLATFORMS, json={**base, "fetch_config": {
        "headers": {"Accept-Language": "en-US", "X-Client": "a"}, "user_agent": "Mozilla/5.0 aicreatLinkMonitor/1.0", "allow_http": False}},
        headers=super_admin.headers))
    assert not redis_client.exists(platform_service.CACHE_KEY)                           # 写后失效
    assert created["fetch_config"] == {"headers": {"Accept-Language": "en-US", "X-Client": "a"},
                                       "user_agent": "Mozilla/5.0 aicreatLinkMonitor/1.0", "allow_http": False}
    assert created["is_system"] is False and created["link_count"] == 0
    body = err(client.post(PLATFORMS, json=base, headers=super_admin.headers), 409)
    assert body["data"] == {"existing_id": created["id"]}
    pid = created["id"]
    body = err(client.put(f"{PLATFORMS}/{pid}", json={"code": "other_code"}, headers=super_admin.headers), 400)
    assert body["data"][0]["loc"] == ["body", "code"]
    updated = ok_data(client.put(f"{PLATFORMS}/{pid}", json={"code": "juejin", "name": "稀土掘金", "deleted_markers": ["文章不存在了"]},
                                 headers=super_admin.headers))
    assert updated["name"] == "稀土掘金" and updated["deleted_markers"] == ["文章不存在了"] and updated["url_patterns"] == base["url_patterns"]

    # 识别：首个命中；未命中 website；非法 URL 400；停用平台不参与识别
    detect = lambda url: client.post(f"{PLATFORMS}/detect", json={"url": url}, headers=owner_a.headers)  # noqa: E731
    assert ok_data(detect("https://juejin.cn/post/1")) == {"platform_id": pid, "code": "juejin"}
    assert ok_data(detect("https://zhuanlan.zhihu.com/p/1"))["code"] == "zhihu"
    assert ok_data(detect("https://xhslink.com/abc"))["code"] == "xiaohongshu"
    assert ok_data(detect("https://unknown.example.com/x"))["code"] == "website"
    err(detect("ftp://x"), 400)
    ok_data(client.put(f"{PLATFORMS}/{pid}", json={"is_active": False}, headers=super_admin.headers))
    assert ok_data(detect("https://juejin.cn/post/1"))["code"] == "website"
    assert db.scalar(select(func.count(AdminOperationLog.id)).where(AdminOperationLog.summary.contains("detect"))) == 0

    # 删除：系统平台 / 有链接引用 409，否则可删
    body = err(client.delete(f"{PLATFORMS}/{platforms['zhihu'].id}", headers=super_admin.headers), 409)
    assert body["data"] == {"reason": "in_use"} and body["message"] == "系统平台不可删除"
    ok_data(client.put(f"{PLATFORMS}/{pid}", json={"is_active": True}, headers=super_admin.headers))
    content = make_content(db, make_project(db, owner_a), owner_a)
    link = ok_data(backfill(client, owner_a, content, "https://juejin.cn/post/9"))["link"]
    assert link["platform_id"] == pid
    assert err(client.delete(f"{PLATFORMS}/{pid}", headers=super_admin.headers), 409)["data"] == {"reason": "in_use"}
    # link_count 只统计可见链接
    listed = {p["code"]: p for p in ok_data(client.get(PLATFORMS, headers=owner_a.headers))}
    assert listed["juejin"]["link_count"] == 1 and listed["zhihu"]["link_count"] == 0 and len(listed) == 9
    b_user = UserFactory(db).create("operator", username="owner_x")
    assert ok_data(client.get(f"{PLATFORMS}/{pid}", headers=b_user.headers))["link_count"] == 0
    ok_data(client.delete(f"{LINKS}/{link['id']}", headers=owner_a.headers))
    ok_data(client.delete(f"{PLATFORMS}/{pid}", headers=super_admin.headers))
    err(client.get(f"{PLATFORMS}/{pid}", headers=super_admin.headers), 404)


def test_platform_rule_test_does_not_write(client: TestClient, db: Session, super_admin: User, platforms: dict[str, PublishPlatform],
                                           no_interval: None, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [page(title="知乎", text="你似乎来到了没有知识存在的荒原 " + LONG_TEXT), page()]
    calls = fake_fetch(monkeypatch, lambda: pages.pop(0))
    data = ok_data(client.post(f"{PLATFORMS}/{platforms['zhihu'].id}/test", json={"url": "https://zhuanlan.zhihu.com/p/1"},
                               headers=super_admin.headers))
    assert data["result_status"] == "deleted" and data["matched_rule"] == "marker:你似乎来到了没有知识存在的荒原"
    assert data["http_status"] == 200 and data["redirect_count"] == 0 and data["title"] == "知乎"
    assert set(data["evidence"]) >= {"marker", "context", "redirects", "headers", "title", "text_excerpt", "short_text"}
    assert data["evidence"]["text_excerpt"].startswith("你似乎来到了")
    assert calls == ["https://zhuanlan.zhihu.com/p/1"]
    assert int(redis_client.get(link_check_service.limit_key(db))) == 1                 # 计入日上限计数
    assert db.scalar(select(func.count(LinkCheck.id))) == 0 and db.scalar(select(func.count(PublishLink.id))) == 0
    ok_page = ok_data(client.post(f"{PLATFORMS}/{platforms['zhihu'].id}/test", json={"url": "https://zhuanlan.zhihu.com/p/2"},
                                  headers=super_admin.headers))
    assert (ok_page["result_status"], ok_page["matched_rule"]) == ("alive", "ok")
    err(client.post(f"{PLATFORMS}/{platforms['zhihu'].id}/test", json={"url": "file:///etc/passwd"}, headers=super_admin.headers), 400)
    log = db.scalars(select(AdminOperationLog).where(AdminOperationLog.target_type == "publish_platform")).first()
    assert log is not None and log.action == "execute"


# =====================================================================
# 8. 编辑 / 删除 / 暂停 / 恢复 / 重建基线（§4.5、§6.8）
# =====================================================================


def test_update_link(client: TestClient, db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    link = ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1"))["link"]
    err(client.put(f"{LINKS}/{link['id']}", json={"url": "https://zhuanlan.zhihu.com/p/2"}, headers=owner_a.headers), 400)
    new_published = (utcnow() - timedelta(days=5)).replace(microsecond=0)
    data = ok_data(client.put(f"{LINKS}/{link['id']}", json={"published_at": new_published.isoformat() + "Z", "publish_account": "新账号",
                                                             "platform_id": platforms["other"].id, "note": ""},
                              headers=owner_a.headers))
    assert data["platform_id"] == platforms["other"].id and data["publish_account"] == "新账号" and data["note"] is None
    assert data["next_index_check_at"] == (new_published + timedelta(days=1)).isoformat() + "Z"
    assert data["index_check_count"] == 0 and data["next_check_at"] == link["next_check_at"]   # 排程指针与删除检测排程不变
    db.expire_all()
    assert db.get(Content, content.id).first_published_at == new_published
    err(client.put(f"{LINKS}/{link['id']}", json={"published_at": "2099-01-01T00:00:00Z"}, headers=owner_a.headers), 400)
    platforms["csdn"].is_active = False
    db.commit()
    err(client.put(f"{LINKS}/{link['id']}", json={"platform_id": platforms["csdn"].id}, headers=owner_a.headers), 409)


def test_delete_link_cascades(client: TestClient, db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    link_id = ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1"))["link"]["id"]
    now = utcnow()
    db.add(LinkCheck(link_id=link_id, check_type="baseline", result_status="alive", previous_status="pending", applied_status="alive",
                     matched_rule="ok", duration_ms=1, checked_at=now))
    db.add(IndexCheck(link_id=link_id, kind="seo", engine="baidu", provider="manual", check_type="manual", result_status="indexed",
                      previous_status="unknown", match_mode="manual", checked_at=now))
    link = db.get(PublishLink, link_id)
    alert_service.raise_alert(db, SYSTEM_SCOPE, "link_deleted", target_type="publish_link", target_id=link_id,
                              project_id=link.project_id, title="t", message="m")
    alert_service.raise_alert(db, SYSTEM_SCOPE, "link_changed", target_type="publish_link", target_id=link_id,
                              project_id=link.project_id, title="t", message="m")
    db.commit()
    assert redis_client.exists(link_service.queued_key(link_id))
    redis_client.set(f"queued:index_check:{link_id}:seo", "1", ex=3600)
    ok_data(client.delete(f"{LINKS}/{link_id}", headers=owner_a.headers))
    db.expire_all()
    assert db.get(PublishLink, link_id) is None
    assert db.scalar(select(func.count(LinkCheck.id))) == 0 and db.scalar(select(func.count(IndexCheck.id))) == 0
    stored = db.get(Content, content.id)
    assert (stored.status, stored.link_count, stored.first_published_at) == ("approved", 0, None)
    alerts = db.scalars(select(Alert)).all()
    assert len(alerts) == 2 and all(a.status == "resolved" and a.resolved_by is None and a.resolution_note == "auto" for a in alerts)
    assert not redis_client.exists(link_service.queued_key(link_id)) and not redis_client.exists(f"queued:index_check:{link_id}:seo")
    log = db.scalars(select(AdminOperationLog).where(AdminOperationLog.action == "delete",
                                                     AdminOperationLog.target_type == "publish_link")).one()
    assert "因链接删除而解决告警 2 条" in log.summary and log.target_id == str(link_id)
    err(client.delete(f"{LINKS}/{link_id}", headers=owner_a.headers), 404)


def test_pause_resume_rebaseline_check(client: TestClient, db: Session, owner_a: User, platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    link_id = ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/1"))["link"]["id"]
    assert ok_data(client.post(f"{LINKS}/{link_id}/check", headers=owner_a.headers)) == {"queued": False, "reason": "already_queued"}
    redis_client.delete(link_service.QUEUE_LINK_CHECKS, link_service.queued_key(link_id))

    paused = ok_data(client.post(f"{LINKS}/{link_id}/pause", headers=owner_a.headers))
    assert paused["is_monitoring"] is False and paused["next_check_at"] is None and paused["next_index_check_at"] is None
    resumed = ok_data(client.post(f"{LINKS}/{link_id}/resume", headers=owner_a.headers))
    assert resumed["is_monitoring"] is True and resumed["queued"] is True                        # pending → 直接入队基线
    assert queue_items()[0]["check_type"] == "baseline" and resumed["next_index_check_at"] is not None

    link = db.get(PublishLink, link_id)
    link.alive_status = "changed"
    link.check_count = 3
    link.last_checked_at = utcnow() - timedelta(hours=2)
    link.baseline_title = "旧标题"
    link.baseline_simhash = 123
    link.baseline_excerpt = "摘录"
    link.baseline_captured_at = utcnow()
    alert_service.raise_alert(db, SYSTEM_SCOPE, "link_changed", target_type="publish_link", target_id=link_id,
                              project_id=link.project_id, title="t", message="m")
    db.commit()
    redis_client.delete(link_service.QUEUE_LINK_CHECKS, link_service.queued_key(link_id))
    assert ok_data(client.post(f"{LINKS}/{link_id}/rebaseline", headers=owner_a.headers)) == {"queued": True}
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert (link.baseline_title, link.baseline_simhash, link.baseline_excerpt, link.baseline_captured_at) == (None, None, None, None)
    assert link.alive_status == "changed"
    assert db.scalars(select(Alert).where(Alert.alert_type == "link_changed")).one().status == "resolved"
    assert queue_items() == [{"link_id": link_id, "check_type": "manual", "triggered_by": owner_a.id}]

    # 恢复监控：以最近检测时间为基准推算、不早于当前时间
    link.alive_status = "alive"
    link.published_at = utcnow() - timedelta(days=40)
    db.commit()
    ok_data(client.post(f"{LINKS}/{link_id}/pause", headers=owner_a.headers))
    resumed = ok_data(client.post(f"{LINKS}/{link_id}/resume", headers=owner_a.headers))
    expected = link.last_checked_at + timedelta(days=7)
    assert resumed["next_check_at"] == expected.isoformat() + "Z" and resumed["queued"] is False
    assert resumed["index_check_count"] == 5


def test_permissions_and_export(client: TestClient, db: Session, owner_a: User, read_only: User, super_admin: User,
                                platforms: dict[str, PublishPlatform]) -> None:
    content = make_content(db, make_project(db, owner_a), owner_a)
    link_id = ok_data(backfill(client, owner_a, content, "https://zhuanlan.zhihu.com/p/=1"))["link"]["id"]
    err(backfill(client, read_only, content, "https://zhuanlan.zhihu.com/p/2"), 403)
    err(client.post(f"{LINKS}/{link_id}/check", headers=read_only.headers), 403)
    err(client.delete(f"{LINKS}/{link_id}", headers=read_only.headers), 403)
    err(client.post(f"{PLATFORMS}/{platforms['zhihu'].id}/test", json={"url": "https://a.com/"}, headers=owner_a.headers), 403)
    resp = client.get(f"{LINKS}/export", headers=super_admin.headers)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/csv")
    text = resp.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0].startswith("ID,项目 ID,内容 ID") and "SEO baidu" in lines[0] and "GEO doubao" in lines[0]
    assert len(lines) == 2 and ",zhihu," in lines[1]
    detail = ok_data(client.get(f"{LINKS}/{link_id}", headers=super_admin.headers))
    assert detail["platform"]["code"] == "zhihu" and detail["content"]["id"] == content.id and detail["last_check"] is None
    assert {e["engine"] for e in detail["engines"]["seo"]} == {"baidu", "bing", "google"}
    assert ok_data(client.get(f"{LINKS}/{link_id}/checks", headers=read_only.headers))["total"] == 0
    assert ok_data(client.get(f"{LINKS}/{link_id}/index-checks", params={"kind": "seo"}, headers=read_only.headers))["total"] == 0


# =====================================================================
# 9. 删除 / 恢复闭环（§16.2：本地 HTTP 服务 + 测试专用 SSRF monkeypatch）
# =====================================================================


class _Site:
    def __init__(self) -> None:
        self.mode = "ok"
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> int:
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # noqa: D401 - 静默
                return

            def _send(self, status: int, body: str, extra: dict[str, str] | None = None) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/":
                    self._send(200, "<html><head><title>首页</title></head><body>欢迎</body></html>")
                    return
                if site.mode == "gone":
                    self._send(404, "<html><title>404 Not Found</title></html>")
                elif site.mode == "home":
                    self._send(302, "", {"Location": "/"})
                else:
                    self._send(200, f"<html><head><title>智能门锁怎么选</title></head><body><article>{LONG_TEXT}</article></body></html>",
                               {"Set-Cookie": "sid=1"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return int(self.server.server_address[1])

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


@pytest.fixture
def local_site(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Site, int]]:
    """本地 HTTP 服务 + 测试专用 SSRF 放宽：``assert_public_url`` 只放行回环地址、``ALLOWED_PORTS`` 加入测试端口。"""
    site = _Site()
    port = site.start()
    monkeypatch.setattr(urls, "ALLOWED_PORTS", (80, 443, port))
    normalize = safe_fetch.normalize_public_url

    def loopback_only(value: str, *, allow_http: bool = True) -> str:
        url = normalize(value, allow_http=allow_http)
        if urlsplit(url).hostname != "127.0.0.1":
            raise safe_fetch.FetchBlocked("测试只允许访问本地服务")
        return url

    monkeypatch.setattr(safe_fetch, "assert_public_url", loopback_only)
    try:
        yield site, port
    finally:
        site.stop()


def _run_queued() -> bool:
    raw = redis_client.lpop(link_service.QUEUE_LINK_CHECKS)
    assert raw is not None
    return run_link_checks.process_one(raw)


def _alerts(db: Session, alert_type: str) -> list[Alert]:
    db.expire_all()
    return list(db.scalars(select(Alert).where(Alert.alert_type == alert_type).order_by(Alert.id)).all())


def test_deletion_restore_closed_loop(client: TestClient, db: Session, owner_a: User, platforms: dict[str, PublishPlatform],
                                      no_interval: None, local_site: tuple[_Site, int]) -> None:
    site, port = local_site
    project = make_project(db, owner_a)
    content = make_content(db, project, owner_a)
    url = f"http://127.0.0.1:{port}/p/1"
    link_id = ok_data(backfill(client, owner_a, content, url))["link"]["id"]
    rt_key = stats_service.realtime_key(stats_service.today_date(db), project.id)

    # 基线 200 → alive，写入 baseline_*
    assert _run_queued() is True
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert link.alive_status == "alive" and link.baseline_title == "智能门锁怎么选" and link.baseline_simhash is not None
    assert link.baseline_captured_at is not None and link.check_count == 1 and link.last_http_status == 200
    baseline = db.scalars(select(LinkCheck).where(LinkCheck.link_id == link_id)).one()
    assert (baseline.check_type, baseline.previous_status, baseline.applied_status, baseline.matched_rule) == ("baseline", "pending", "alive", "ok")
    assert "set-cookie" not in json.loads(baseline.evidence_json)["headers"]

    # 页面改为 404：非基线 404 一次即 deleted
    site.mode = "gone"
    assert ok_data(client.post(f"{LINKS}/{link_id}/check", headers=owner_a.headers)) == {"queued": True}
    assert _run_queued() is True
    check = db.scalars(select(LinkCheck).where(LinkCheck.link_id == link_id).order_by(LinkCheck.id.desc())).first()
    assert (check.check_type, check.result_status, check.applied_status, check.matched_rule, check.http_status) == \
        ("manual", "deleted", "deleted", "http_404", 404)
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert link.alive_status == "deleted" and link.next_index_check_at is None
    deleted_alerts = _alerts(db, "link_deleted")
    assert len(deleted_alerts) == 1 and deleted_alerts[0].status == "open" and deleted_alerts[0].project_id == project.id
    assert deleted_alerts[0].title == f"链接已被删除：127.0.0.1:{port}/p/1"
    assert deleted_alerts[0].message == "平台 website 返回 HTTP 404"
    payload = json.loads(deleted_alerts[0].payload_json)
    assert payload == {"url": url, "platform_code": "website", "matched_rule": "http_404", "link_check_id": check.id, "content_id": content.id}
    assert redis_client.hget(rt_key, "links_deleted") == "1"

    # 恢复 200 → alive，link_restored（创建即 resolved），link_deleted 自动解决
    site.mode = "ok"
    ok_data(client.post(f"{LINKS}/{link_id}/check", headers=owner_a.headers))
    assert _run_queued() is True
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert link.alive_status == "alive" and link.next_index_check_at is not None
    restored = _alerts(db, "link_restored")
    assert len(restored) == 1 and restored[0].status == "resolved" and restored[0].resolution_note == "auto"
    deleted_alert = _alerts(db, "link_deleted")[0]
    assert deleted_alert.status == "resolved" and deleted_alert.resolved_by is None and deleted_alert.resolution_note == "auto"
    assert redis_client.hget(rt_key, "links_restored") == "1"

    # 连续 2 次跳转首页确认 deleted
    site.mode = "home"
    ok_data(client.post(f"{LINKS}/{link_id}/check", headers=owner_a.headers))
    assert _run_queued() is True
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert link.alive_status == "suspected_deleted" and link.consecutive_suspected == 1
    first = db.scalars(select(LinkCheck).where(LinkCheck.link_id == link_id).order_by(LinkCheck.id.desc())).first()
    assert first.matched_rule == "redirect_home" and first.redirect_count == 1
    assert json.loads(first.evidence_json)["redirects"] == [url]
    ok_data(client.post(f"{LINKS}/{link_id}/check", headers=owner_a.headers))
    assert _run_queued() is True
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert link.alive_status == "deleted" and link.consecutive_suspected == 2
    deleted_alerts = _alerts(db, "link_deleted")
    assert len(deleted_alerts) == 2 and deleted_alerts[-1].status == "open"
    assert deleted_alerts[-1].message == "平台 website 连续 2 次跳转到首页/登录页"
    assert redis_client.hget(rt_key, "links_deleted") == "2"

    # deleted 状态下再次跳转：保持 deleted，不再产生 link_deleted、不计 links_deleted
    ok_data(client.post(f"{LINKS}/{link_id}/check", headers=owner_a.headers))
    assert _run_queued() is True
    db.expire_all()
    link = db.get(PublishLink, link_id)
    assert link.alive_status == "deleted" and link.consecutive_suspected == 3
    last = db.scalars(select(LinkCheck).where(LinkCheck.link_id == link_id).order_by(LinkCheck.id.desc())).first()
    assert (last.result_status, last.previous_status, last.applied_status) == ("suspected_deleted", "deleted", "deleted")
    assert len(_alerts(db, "link_deleted")) == 2 and _alerts(db, "link_deleted")[-1].trigger_count == 1
    assert redis_client.hget(rt_key, "links_deleted") == "2"
    assert redis_client.hget(rt_key, "links_checked") == "6"
    assert int(redis_client.get(link_check_service.limit_key(db))) == 6

    # 内网地址（非回环）仍被拒绝：ssrf_blocked
    other = ok_data(backfill(client, owner_a, content, "http://10.0.0.8/p/2"))["link"]["id"]
    assert _run_queued() is True
    blocked = db.scalars(select(LinkCheck).where(LinkCheck.link_id == other)).one()
    assert (blocked.result_status, blocked.matched_rule, blocked.applied_status) == ("unknown", "ssrf_blocked", "pending")


def test_changed_and_unknown_flow(db: Session, owner_a: User, platforms: dict[str, PublishPlatform], no_interval: None,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_project(db, owner_a)
    content = make_content(db, project, owner_a)
    link = new_link(db, content, platforms["zhihu"], published_at=utcnow() - timedelta(days=30))
    state: dict[str, Any] = {"value": page()}
    fake_fetch(monkeypatch, lambda: state["value"])

    def run(check_type: str = "scheduled") -> LinkCheck:
        db.expire_all()
        return link_check_service.check_link(db, SYSTEM_SCOPE, db.get(PublishLink, link.id), check_type, None)

    run("baseline")
    state["value"] = page(title="另一个完全不同的标题 - 知乎")
    record = run()
    assert (record.result_status, record.applied_status, record.matched_rule) == ("changed", "changed", "title_changed")
    changed_alerts = _alerts(db, "link_changed")
    assert len(changed_alerts) == 1 and changed_alerts[0].status == "open" and changed_alerts[0].project_id == project.id
    stored = db.get(PublishLink, link.id)
    assert stored.next_check_at == record.checked_at + timedelta(hours=6)                   # 首次 changed：6h 复核
    run()                                                                                  # 持续 changed：按 alive 规则
    stored = db.get(PublishLink, link.id)
    assert stored.next_check_at == stored.last_checked_at + timedelta(days=7) and len(_alerts(db, "link_changed")) == 1
    state["value"] = page()
    record = run()                                                                         # 与基线一致 → alive，解决 link_changed
    assert (record.applied_status, record.matched_rule) == ("alive", "ok") and _alerts(db, "link_changed")[0].status == "resolved"

    # unknown 两次不改状态、第三次改
    state["value"] = safe_fetch.FetchError("ReadTimeout after 15s", reason="timeout")
    first = run()
    second = run()
    third = run()
    assert [r.applied_status for r in (first, second, third)] == ["alive", "alive", "unknown"]
    assert third.error_message == "ReadTimeout after 15s" and third.http_status is None
    stored = db.get(PublishLink, link.id)
    assert stored.consecutive_unknown == 3 and stored.next_check_at == third.checked_at + timedelta(hours=24)
    # 手动检测：状态未变化时不改排程
    before = stored.next_check_at
    manual = run("manual")
    assert manual.applied_status == "unknown" and db.get(PublishLink, link.id).next_check_at == before
    state["value"] = page()
    restored = run("manual")                                                               # 状态变化 → 重算
    stored = db.get(PublishLink, link.id)
    assert restored.applied_status == "alive" and stored.next_check_at == restored.checked_at + timedelta(days=7)
    assert stored.consecutive_unknown == 0


# =====================================================================
# 10. monitor_worker 主循环（docs/01 §5.2、§5.3）
# =====================================================================


def test_monitor_worker_tick(db: Session, owner_a: User, platforms: dict[str, PublishPlatform], no_interval: None,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    from app import monitor_worker

    content = make_content(db, make_project(db, owner_a), owner_a)
    link = new_link(db, content, platforms["zhihu"], next_check_at=utcnow() - timedelta(minutes=1))
    fake_fetch(monkeypatch, page())
    monkeypatch.setattr(monitor_worker, "optional_task", lambda module, attr: None)     # 后续步骤的任务模块不参与本测试
    worker = monitor_worker.MonitorWorker(poll_interval=0, link_concurrency=1, index_concurrency=1)
    assert worker.pool.max_workers == 4 and set(worker.sems) == {"link_check", "index_check"}
    try:
        submitted = worker.tick()                                                       # 调度入队（主循环线程）→ 消费（线程池）
        assert submitted == 1
        assert len(list(redis_client.scan_iter("worker:heartbeat:monitor_worker:*"))) == 1
    finally:
        worker.shutdown()
    keys = list(redis_client.scan_iter("worker:heartbeat:monitor_worker:*"))
    assert keys == []                                                                   # 正常退出删除本副本心跳
    db.expire_all()
    stored = db.get(PublishLink, link.id)
    assert stored.alive_status == "alive" and stored.check_count == 1
    assert db.scalars(select(LinkCheck).where(LinkCheck.link_id == link.id)).one().check_type == "baseline"
    timers = monitor_worker.InlineTimers(clock=lambda: 100.0)
    hits: list[int] = []
    assert timers.run_due("x", 60, lambda: hits.append(1)) is None and hits == [1]
    timers.run_due("x", 60, lambda: hits.append(2))
    timers.run_due("off", 0, lambda: hits.append(3))
    assert hits == [1]
