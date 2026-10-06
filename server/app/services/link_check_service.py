"""单链接删除检测：抓取 → 判定 → 确认阈值 → 写回 → 告警（docs/11 §6.3~§6.9；docs/03「检测写回」第 1 条；docs/01 §3.3）。

- ``build_fetch_config(platform, monitoring_config)``：以 ``monitoring_config.link_check`` 为底、平台 ``fetch_config_json`` 非空键覆盖
  （``user_agent`` / ``headers`` / ``timeout_seconds`` / ``respect_robots`` / ``allow_http``；``max_response_bytes`` / ``max_redirects`` 只取全局）；
- ``judge(fetch_result, link, platform, *, check_type)`` → ``(result_status, matched_rule, evidence)``，按 §6.3 八条规则首个命中；
  ``evaluate`` 返回含指纹、海明距离与基线快照的完整判定（``judge`` 与平台规则测试共用）；
- ``apply_thresholds``：§6.4 计数器（饱和于 100）与 ``applied_status``；
- ``check_link(db, scope, link, check_type, triggered_by)``：``enforce_interval("domain:last_fetch:{domain}")`` →
  ``INCR limit:link_checks:{date}``（``EXPIRE 172800``，只计数不拦截）→ ``safe_fetch.fetch_page`` → ``judge`` → 同一事务
  ``INSERT link_checks`` + ``UPDATE publish_links`` + ``link_deleted`` / ``link_restored`` / ``link_changed`` 告警 → 提交后 ``stats:rt``。
  单链接互斥锁 ``lock:monitor:link_check:{link_id}`` 由调用方 ``run_link_checks.process_one`` 持有；
- ``record_failure``：检测流程自身异常时以 ``unknown`` / ``network_error`` / 脱敏 ``error_message`` 落一条记录（同一套状态机）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import redis
from sqlalchemy.orm import Session

from app.core import fingerprint, ratelimit, safe_fetch
from app.core.redis import redis_client
from app.models import LINK_CHECK_RULE_MARKER_PREFIX, LinkCheck, PublishLink, PublishPlatform, utcnow
from app.schemas.common import iso_utc
from app.services import alert_service, platform_service, settings_service, stats_service
from app.services.data_scope_service import DataScope

logger = logging.getLogger(__name__)

LIMIT_KEY_PREFIX = "limit:link_checks:"
LIMIT_KEY_TTL_SECONDS = 172800
DOMAIN_INTERVAL_PREFIX = "domain:last_fetch:"
COUNTER_CAP = 100
MARKER_SCOPE_CHARS = 2000
CONTEXT_RADIUS = 40
TEXT_EXCERPT_CHARS = 300
BASELINE_EXCERPT_CHARS = 1000
MAX_TITLE = 300
MAX_FINAL_URL = 1000
MAX_ERROR_MESSAGE = 500
MAX_MATCHED_RULE = 100
DELETED_HTTP_STATUSES = (404, 410, 451)
LOGIN_TOKEN_RE = re.compile(r"(?<![a-z])(login|signin|passport|sso)(?![a-z])", re.IGNORECASE)
SUMMARY_HEADERS = (
    "content-type", "content-length", "server", "location", "last-modified", "etag", "cache-control", "date", "retry-after",
)
ALERT_LINK_DELETED = "link_deleted"
ALERT_LINK_RESTORED = "link_restored"
ALERT_LINK_CHANGED = "link_changed"


# =====================================================================
# 配置
# =====================================================================


def link_check_config(db: Session) -> dict[str, Any]:
    """``monitoring_config.link_check``（与默认值深合并）。"""
    return dict(settings_service.get_config(db, "monitoring_config").get("link_check") or {})


def _link_cfg(monitoring_config: dict[str, Any] | None) -> dict[str, Any]:
    if not monitoring_config:
        return dict(settings_service.DEFAULT_SETTINGS["monitoring_config"]["link_check"])
    if "link_check" in monitoring_config and isinstance(monitoring_config["link_check"], dict):
        return monitoring_config["link_check"]
    return monitoring_config


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, dict, list)) and not value:
        return False
    return True


def build_fetch_config(platform: Any, monitoring_config: dict[str, Any] | None) -> safe_fetch.FetchConfig:
    """``monitoring_config``（整份或其 ``link_check`` 段）为底，平台 ``fetch_config_json`` 非空键覆盖。"""
    cfg = _link_cfg(monitoring_config)
    override = platform_service.fetch_config(platform) if platform is not None else {}
    defaults = safe_fetch.FetchConfig()
    user_agent = str(cfg.get("user_agent") or defaults.user_agent)
    headers: dict[str, str] = {}
    timeout = float(cfg.get("timeout_seconds") or defaults.timeout_seconds)
    respect_robots = bool(cfg.get("respect_robots", defaults.respect_robots))
    allow_http = bool(cfg.get("allow_http", defaults.allow_http))
    if _present(override.get("user_agent")):
        user_agent = str(override["user_agent"])
    if _present(override.get("headers")) and isinstance(override["headers"], dict):
        headers = {str(k): str(v) for k, v in override["headers"].items()}
    if _present(override.get("timeout_seconds")):
        timeout = float(override["timeout_seconds"])
    if _present(override.get("respect_robots")):
        respect_robots = bool(override["respect_robots"])
    if _present(override.get("allow_http")):
        allow_http = bool(override["allow_http"])
    return safe_fetch.FetchConfig(
        timeout_seconds=timeout,
        max_response_bytes=int(cfg.get("max_response_bytes") or defaults.max_response_bytes),
        max_redirects=int(cfg.get("max_redirects") if cfg.get("max_redirects") is not None else defaults.max_redirects),
        user_agent=user_agent,
        headers=headers,
        allow_http=allow_http,
        respect_robots=respect_robots,
    )


# =====================================================================
# 抓取前：同域名间隔与日上限计数
# =====================================================================


def limit_key(db: Session | None = None) -> str:
    """``limit:link_checks:{date}``（``{date}`` 按 ``stats_config.timezone``）。"""
    return f"{LIMIT_KEY_PREFIX}{stats_service.today_date(db).isoformat()}"


def used_today(db: Session | None = None) -> int:
    """当日已抓取次数（Redis 不可用时按 0）。"""
    try:
        return int(redis_client.get(limit_key(db)) or 0)
    except (redis.RedisError, ValueError) as exc:
        logger.warning("读取 limit:link_checks 失败: %s", exc)
        return 0


def count_fetch(db: Session | None = None) -> int:
    """``INCR limit:link_checks:{date}`` + ``EXPIRE 172800``（只计数、不拦截）；返回计数，Redis 不可用时 0。"""
    key = limit_key(db)
    try:
        pipe = redis_client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, LIMIT_KEY_TTL_SECONDS)
        count, _ = pipe.execute()
        return int(count)
    except redis.RedisError as exc:
        logger.warning("写入 limit:link_checks 计数失败: %s", exc)
        return 0


def before_fetch(db: Session, domain: str, cfg: dict[str, Any]) -> None:
    """``enforce_interval("domain:last_fetch:{domain}", per_domain_interval_seconds)`` → ``INCR limit:link_checks:{date}``。"""
    interval = float(cfg.get("per_domain_interval_seconds") or 0)
    if domain and interval > 0:
        ratelimit.enforce_interval(f"{DOMAIN_INTERVAL_PREFIX}{domain}", interval)
    count_fetch(db)


def fetch(url: str, config: safe_fetch.FetchConfig) -> safe_fetch.PageResult | safe_fetch.FetchError:
    """``safe_fetch.fetch_page``；``FetchError`` 原样返回，抓取器自身的其它异常转为脱敏的 ``FetchError``（不丢记录）。"""
    try:
        return safe_fetch.fetch_page(url, config=config)
    except safe_fetch.FetchError as exc:
        return exc
    except Exception as exc:  # noqa: BLE001 - 抓取器自身异常同样按 network_error 落记录
        logger.warning("抓取异常 %s: %s", urlsplit(url).netloc, type(exc).__name__, exc_info=True)
        return safe_fetch.FetchError(sanitize_error(exc), reason="network_error")


def sanitize_error(exc: BaseException) -> str:
    """脱敏错误：只含异常类型与简短原因（不含目标站响应体），≤ 500 字符。"""
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    message = f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__
    return message[:MAX_ERROR_MESSAGE]


# =====================================================================
# 判定（§6.3）
# =====================================================================


@dataclass
class Judgement:
    result_status: str
    matched_rule: str
    evidence: dict[str, Any]
    http_status: int | None = None
    final_url: str | None = None
    redirect_count: int = 0
    title: str | None = None
    simhash: int | None = None
    hamming_distance: int | None = None
    response_bytes: int | None = None
    duration_ms: int = 0
    error_message: str | None = None
    baseline: dict[str, Any] | None = field(default=None)

    def as_tuple(self) -> tuple[str, str, dict[str, Any]]:
        return self.result_status, self.matched_rule, self.evidence


def _evidence(
    *, marker: str | None = None, context: str | None = None, redirects: list[str] | None = None,
    headers: dict[str, str] | None = None, title: str | None = None, text_excerpt: str | None = None, short_text: bool = True,
) -> dict[str, Any]:
    return {
        "marker": marker,
        "context": context,
        "redirects": list(redirects or []),
        "headers": dict(headers or {}),
        "title": title,
        "text_excerpt": text_excerpt,
        "short_text": bool(short_text),
    }


def _summary(headers: dict[str, str]) -> dict[str, str]:
    return {k: v[:300] for k, v in headers.items() if k in SUMMARY_HEADERS}


def _find_marker(markers: list[str], title: str | None, text: str) -> tuple[str, str] | None:
    """忽略大小写匹配 ``title`` 与 ``text[:2000]``，返回 ``(文案, 命中处上下文)``。"""
    haystacks = [h for h in (title or "", text[:MARKER_SCOPE_CHARS]) if h]
    for marker in markers:
        needle = marker.strip()
        if not needle:
            continue
        lowered = needle.lower()
        for hay in haystacks:
            pos = hay.lower().find(lowered)
            if pos < 0:
                continue
            start = max(0, pos - CONTEXT_RADIUS)
            end = min(len(hay), pos + len(needle) + CONTEXT_RADIUS)
            context = ("…" if start > 0 else "") + hay[start:end] + ("…" if end < len(hay) else "")
            return needle, context
    return None


def _path(url: str | None) -> str:
    try:
        return urlsplit(url or "").path or "/"
    except ValueError:
        return "/"


def _redirect_rule(page: safe_fetch.PageResult, link: Any, patterns: list[str]) -> str | None:
    """规则 4：发生过重定向且最终 URL 命中 ``redirect_markers`` 或最终 path 为 ``/``（原 path 非 ``/``）。"""
    if not page.redirect_chain:
        return None
    final_url = page.final_url or ""
    matched_pattern = next((p for p in patterns if _regex_search(p, final_url)), None)
    to_root = _path(final_url) == "/" and _path(getattr(link, "url", "")) != "/"
    if matched_pattern is None and not to_root:
        return None
    if (matched_pattern and LOGIN_TOKEN_RE.search(matched_pattern)) or LOGIN_TOKEN_RE.search(final_url):
        return "redirect_login"
    return "redirect_home"


def _regex_search(pattern: str, value: str) -> bool:
    try:
        return re.search(pattern, value, re.IGNORECASE) is not None
    except re.error:
        return False


def _error_judgement(exc: safe_fetch.FetchError) -> Judgement:
    if isinstance(exc, safe_fetch.FetchBlocked):
        rule = "blocked_by_robots" if exc.reason == "blocked_by_robots" else "ssrf_blocked"
    else:
        rule = "network_error"
    chain = list(getattr(exc, "redirect_chain", []) or [])
    evidence = _evidence(redirects=[url for _s, url in chain])
    evidence["error"] = exc.reason
    return Judgement(
        result_status="unknown", matched_rule=rule, evidence=evidence, http_status=getattr(exc, "http_status", None),
        redirect_count=len(chain), error_message=(exc.message or exc.reason or type(exc).__name__)[:MAX_ERROR_MESSAGE],
    )


def evaluate(
    fetch_result: safe_fetch.PageResult | safe_fetch.FetchError,
    link: Any,
    platform: Any,
    *,
    check_type: str,
    cfg: dict[str, Any] | None = None,
) -> Judgement:
    """§6.3 完整判定（首个命中）：``link`` 只需 ``url`` / ``baseline_title`` / ``baseline_simhash`` / ``baseline_captured_at``。"""
    cfg = _link_cfg(cfg)
    if isinstance(fetch_result, safe_fetch.FetchError):
        return _error_judgement(fetch_result)                                         # 规则 1 / 2
    page = fetch_result
    status = int(page.status)
    text = page.text or ""
    simhash = fingerprint.text_simhash(text) if page.is_html else None
    baseline_simhash = getattr(link, "baseline_simhash", None)
    hamming = fingerprint.hamming_distance(simhash, baseline_simhash) if simhash is not None and baseline_simhash is not None else None
    evidence = _evidence(
        redirects=page.redirects, headers=_summary(page.headers), title=page.title,
        text_excerpt=text[:TEXT_EXCERPT_CHARS] if page.is_html else None,
        short_text=(not page.is_html) or len(text) < fingerprint.SIMHASH_MIN_TEXT_CHARS,
    )
    base = dict(
        http_status=status, final_url=(page.final_url or "")[:MAX_FINAL_URL] or None, redirect_count=page.redirect_count,
        title=(page.title or None) and page.title[:MAX_TITLE], simhash=simhash, hamming_distance=hamming,
        response_bytes=page.response_bytes, duration_ms=page.duration_ms,
    )

    def done(result: str, rule: str, **extra: Any) -> Judgement:
        return Judgement(result_status=result, matched_rule=rule[:MAX_MATCHED_RULE], evidence=evidence, **base, **extra)

    if status >= 500 or (400 <= status < 500 and status not in DELETED_HTTP_STATUSES):
        return done("unknown", "network_error")                                       # 规则 2：5xx 与 401/403/429 等
    if status in DELETED_HTTP_STATUSES:                                               # 规则 3
        return done("suspected_deleted" if check_type == "baseline" else "deleted", f"http_{status}")
    redirect_rule = _redirect_rule(page, link, platform_service.redirect_markers(platform))
    if redirect_rule:                                                                 # 规则 4
        return done("suspected_deleted", redirect_rule)
    if status == 200 and page.is_html:
        hit = _find_marker(platform_service.deleted_markers(platform), page.title, text)
        if hit:                                                                       # 规则 5
            marker, context = hit
            evidence["marker"] = marker
            evidence["context"] = context
            return done("deleted", f"{LINK_CHECK_RULE_MARKER_PREFIX}{marker}")
        if getattr(link, "baseline_captured_at", None) is not None:                   # 规则 6
            current_title = fingerprint.normalize_title(page.title)
            baseline_title = fingerprint.normalize_title(getattr(link, "baseline_title", None))
            if cfg.get("title_compare", True) and current_title and baseline_title and current_title != baseline_title:
                return done("changed", "title_changed")
            if hamming is not None and hamming >= int(cfg.get("changed_simhash_distance") or 20):
                return done("changed", "body_changed")
        else:                                                                         # 规则 7：无基线 → 写基线
            baseline = {
                "baseline_title": (page.title or None) and page.title[:MAX_TITLE],
                "baseline_simhash": simhash,
                "baseline_excerpt": text[:BASELINE_EXCERPT_CHARS] or None,
            }
            return done("alive", "ok", baseline=baseline)
    return done("alive", "ok")                                                        # 规则 8


def judge(
    fetch_result: safe_fetch.PageResult | safe_fetch.FetchError,
    link: Any,
    platform: Any,
    *,
    check_type: str,
    cfg: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """``(result_status, matched_rule, evidence)``（docs/11 §6.3）。"""
    return evaluate(fetch_result, link, platform, check_type=check_type, cfg=cfg).as_tuple()


# =====================================================================
# 确认阈值（§6.4）
# =====================================================================


def apply_thresholds(
    previous: str, result: str, consecutive_unknown: int, consecutive_suspected: int, cfg: dict[str, Any]
) -> tuple[str, int, int]:
    """返回 ``(applied_status, consecutive_unknown, consecutive_suspected)``（累加饱和于 100）。"""
    cfg = _link_cfg(cfg)
    unknown_confirm = int(cfg.get("unknown_confirm_count") or 3)
    suspected_confirm = int(cfg.get("suspected_confirm_count") or 2)
    if result == "unknown":
        cu = min(int(consecutive_unknown or 0) + 1, COUNTER_CAP)
        return ("unknown" if cu >= unknown_confirm else previous), cu, 0
    if result == "suspected_deleted":
        cs = min(int(consecutive_suspected or 0) + 1, COUNTER_CAP)
        if previous == "deleted" or cs >= suspected_confirm:
            return "deleted", 0, cs
        return "suspected_deleted", 0, cs
    return result, 0, 0


# =====================================================================
# 写回（§6.9）
# =====================================================================


def _strip_scheme(url: str) -> str:
    return re.sub(r"^https?://", "", url or "", flags=re.IGNORECASE)


def _deleted_message(platform_code: str, judgement: Judgement, consecutive_suspected: int) -> str:
    rule = judgement.matched_rule
    if rule.startswith(LINK_CHECK_RULE_MARKER_PREFIX):
        marker = judgement.evidence.get("marker") or rule[len(LINK_CHECK_RULE_MARKER_PREFIX):]
        return f"平台 {platform_code} 命中删除特征「{marker}」（HTTP {judgement.http_status}）"
    if rule.startswith("http_"):
        return f"平台 {platform_code} 返回 HTTP {judgement.http_status}"
    if rule.startswith("redirect_"):
        return f"平台 {platform_code} 连续 {consecutive_suspected} 次跳转到首页/登录页"
    return f"平台 {platform_code} 判定链接已删除（{rule}）"


def _changed_message(platform_code: str, judgement: Judgement) -> str:
    if judgement.matched_rule == "title_changed":
        return f"平台 {platform_code} 页面标题与基线不一致：{judgement.title or ''}"[:1000]
    return f"平台 {platform_code} 页面正文与基线差异显著（海明距离 {judgement.hamming_distance}）"


def _restored_message(platform_code: str, judgement: Judgement, applied: str) -> str:
    suffix = "，但内容与基线不一致" if applied == "changed" else ""
    return f"平台 {platform_code} 链接恢复访问（HTTP {judgement.http_status}）{suffix}"


def _payload(link: PublishLink, platform_code: str, judgement: Judgement, record: LinkCheck) -> dict[str, Any]:
    return {
        "url": link.url,
        "platform_code": platform_code,
        "matched_rule": judgement.matched_rule,
        "link_check_id": record.id,
        "content_id": link.content_id,
    }


def write_back(
    db: Session,
    scope: DataScope,
    link: PublishLink,
    platform: PublishPlatform | None,
    judgement: Judgement,
    *,
    check_type: str,
    triggered_by: int | None,
    cfg: dict[str, Any],
    now: datetime | None = None,
) -> LinkCheck:
    """同一事务：``INSERT link_checks`` → ``UPDATE publish_links`` → 告警 → 提交；提交后 ``HINCRBY stats:rt``。"""
    from app.services import link_service  # 排程函数在 link_service（其依赖本模块的入队路径之外的内容）

    now = now or utcnow()
    previous = link.alive_status
    applied, cu, cs = apply_thresholds(previous, judgement.result_status, link.consecutive_unknown, link.consecutive_suspected, cfg)

    record = LinkCheck(
        link_id=link.id,
        check_type=check_type,
        result_status=judgement.result_status,
        previous_status=previous,
        applied_status=applied,
        http_status=judgement.http_status,
        final_url=(judgement.final_url or None) and judgement.final_url[:MAX_FINAL_URL],
        redirect_count=min(int(judgement.redirect_count or 0), 100),
        matched_rule=judgement.matched_rule[:MAX_MATCHED_RULE],
        title=(judgement.title or None) and judgement.title[:MAX_TITLE],
        simhash=judgement.simhash,
        hamming_distance=judgement.hamming_distance,
        response_bytes=judgement.response_bytes,
        duration_ms=int(judgement.duration_ms or 0),
        error_message=(judgement.error_message or None) and judgement.error_message[:MAX_ERROR_MESSAGE],
        evidence_json=json.dumps(judgement.evidence, ensure_ascii=False, separators=(",", ":")) if judgement.evidence else None,
        checked_at=now,
        triggered_by=triggered_by,
    )
    db.add(record)
    db.flush()

    link.consecutive_unknown = cu
    link.consecutive_suspected = cs
    if applied != previous:
        link.alive_status = applied
        link.alive_changed_at = now
    link.last_checked_at = now
    link.check_count = int(link.check_count or 0) + 1
    link.last_http_status = judgement.http_status
    if judgement.baseline is not None:
        link.baseline_title = judgement.baseline.get("baseline_title")
        link.baseline_simhash = judgement.baseline.get("baseline_simhash")
        link.baseline_excerpt = judgement.baseline.get("baseline_excerpt")
        link.baseline_captured_at = now
    if check_type != "manual" or applied != previous:
        link.next_check_at = link_service.compute_next_check_at(
            link, judgement.result_status, previous, applied, now=now, cfg=cfg
        )
    entered_deleted = applied == "deleted" and previous != "deleted"
    left_deleted = previous == "deleted" and applied != "deleted"
    if entered_deleted:
        link.next_index_check_at = None
    elif left_deleted:
        link.next_index_check_at = link_service.compute_next_index_check_at(link, now=now, db=db)

    platform_code = platform.code if platform is not None else ""
    target_key = str(link.id)
    title_url = _strip_scheme(link.normalized_url)
    payload = _payload(link, platform_code, judgement, record)
    restored = previous == "deleted" and applied in ("alive", "changed")
    entered_changed = applied == "changed" and previous != "changed"
    if entered_deleted:
        alert_service.raise_alert(
            db, scope, ALERT_LINK_DELETED, target_type="publish_link", target_id=link.id, project_id=link.project_id,
            title=f"链接已被删除：{title_url}", message=_deleted_message(platform_code, judgement, cs), payload=payload,
        )
    if restored:
        alert_service.raise_alert(
            db, scope, ALERT_LINK_RESTORED, target_type="publish_link", target_id=link.id, project_id=link.project_id,
            title=f"链接已恢复：{title_url}", message=_restored_message(platform_code, judgement, applied), payload=payload,
        )
        alert_service.resolve_alert(db, scope, ALERT_LINK_DELETED, "publish_link", target_key)
    if entered_changed:
        alert_service.raise_alert(
            db, scope, ALERT_LINK_CHANGED, target_type="publish_link", target_id=link.id, project_id=link.project_id,
            title=f"链接内容已变化：{title_url}", message=_changed_message(platform_code, judgement), payload=payload,
        )
    if applied == "alive" and previous != "alive":
        alert_service.resolve_alert(db, scope, ALERT_LINK_CHANGED, "publish_link", target_key)

    fields: dict[str, int] = {"links_checked": 1}
    if entered_deleted:
        fields["links_deleted"] = 1
    if entered_changed:
        fields["links_changed"] = 1
    if restored:
        fields["links_restored"] = 1
    stats_service.increment_realtime_after_commit(db, link.project_id, fields, at=now)
    db.commit()
    return record


def check_link(
    db: Session,
    scope: DataScope,
    link: PublishLink,
    check_type: str,
    triggered_by: int | None = None,
) -> LinkCheck:
    """删除检测一次：同域名间隔 → 计数 → ``fetch_page(link.url, build_fetch_config(platform, monitoring_config))`` →
    ``judge`` → 写回（§6.9）。抓取始终使用原始 ``url``。返回插入的 ``LinkCheck``。"""
    cfg = link_check_config(db)
    platform = db.get(PublishPlatform, link.platform_id)
    config = build_fetch_config(platform, cfg)
    before_fetch(db, link.domain, cfg)
    fetched = fetch(link.url, config)
    judgement = evaluate(fetched, link, platform, check_type=check_type, cfg=cfg)
    return write_back(db, scope, link, platform, judgement, check_type=check_type, triggered_by=triggered_by, cfg=cfg)


def record_failure(
    db: Session,
    scope: DataScope,
    link_id: int,
    check_type: str,
    triggered_by: int | None,
    exc: BaseException,
) -> LinkCheck | None:
    """检测流程异常的兜底记录：``result_status=unknown``、``matched_rule=network_error``、``error_message=脱敏异常``，
    计数器与状态机照常推进。链接已不存在时返回 ``None``。"""
    db.rollback()
    link = db.get(PublishLink, link_id)
    if link is None:
        return None
    db.refresh(link)
    cfg = link_check_config(db)
    platform = db.get(PublishPlatform, link.platform_id)
    evidence = _evidence()
    evidence["error"] = "internal_error"
    judgement = Judgement(result_status="unknown", matched_rule="network_error", evidence=evidence, error_message=sanitize_error(exc))
    return write_back(db, scope, link, platform, judgement, check_type=check_type, triggered_by=triggered_by, cfg=cfg)


# =====================================================================
# 序列化
# =====================================================================


def _loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def link_check_item(check: LinkCheck) -> dict[str, Any]:
    """删除检测记录（docs/04 §7.12；``simhash`` 为有符号 64 位整数，按字符串返回以免 JS 精度丢失）。"""
    return {
        "id": check.id,
        "link_id": check.link_id,
        "check_type": check.check_type,
        "result_status": check.result_status,
        "previous_status": check.previous_status,
        "applied_status": check.applied_status,
        "http_status": check.http_status,
        "final_url": check.final_url,
        "redirect_count": check.redirect_count,
        "matched_rule": check.matched_rule,
        "title": check.title,
        "simhash": str(check.simhash) if check.simhash is not None else None,
        "hamming_distance": check.hamming_distance,
        "response_bytes": check.response_bytes,
        "duration_ms": check.duration_ms,
        "error_message": check.error_message,
        "evidence": _loads(check.evidence_json),
        "checked_at": iso_utc(check.checked_at),
        "triggered_by": check.triggered_by,
    }


__all__ = [
    "Judgement",
    "apply_thresholds",
    "before_fetch",
    "build_fetch_config",
    "check_link",
    "count_fetch",
    "evaluate",
    "fetch",
    "judge",
    "limit_key",
    "link_check_config",
    "link_check_item",
    "record_failure",
    "sanitize_error",
    "used_today",
    "write_back",
]
