"""回填链接（docs/11 §4、§6.6、§6.7、§6.8、§7.5；docs/03「链接哈希去重与回填」「删除规则」；docs/04 §6.17、§7.10~§7.12；
docs/13 §6.3、§7.5、§8）。

- ``backfill(db, scope, body, admin_id)``：按 docs/11 §4.1 的顺序校验（内容可见且 ``approved`` / ``published`` → 项目 ``active`` →
  ``safe_fetch.normalize_public_url`` 语法预校验（不做 DNS）→ 平台（缺省 ``platform_service.detect``）→ ``url_hash`` 全局唯一
  （不可见时 ``owned_by_other``）→ ``published_at`` 范围 → 长度），同一事务插入 ``publish_links``、``contents.link_count += 1``、
  ``approved → published``、重算 ``first_published_at``；提交后 ``stats:rt`` 计数并 ``enqueue_check(link, "baseline", admin_id)``；
- ``batch_backfill``：逐条独立事务，返回 ``{created, failed, results[{index, ok, link_id, queued, code, message, reason?}]}``；
- ``enqueue_check`` / ``compute_next_check_at`` / ``compute_next_index_check_at``（排程权威在 MySQL，队列只是触发器）；
- 编辑、删除（级联检测记录、``contents`` 同事务更新、自动解决告警）、暂停 / 恢复、``rebaseline``、手动检测、列表 / 详情 / 导出 / 检测历史。

所有读写 ``publish_links`` / ``link_checks`` / ``index_checks`` 的函数都以 ``scope`` 为紧随 ``db`` 的必填参数（worker 传 ``SYSTEM_SCOPE``）。
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import redis
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import safe_fetch, urls
from app.core.database import after_commit
from app.core.exceptions import (
    CODE_CONFLICT,
    CODE_NOT_FOUND,
    BusinessError,
    field_error,
    format_validation_errors,
    invalid_params,
)
from app.core.redis import redis_client
from app.models import (
    Content,
    IndexCheck,
    LinkCheck,
    Project,
    PublishLink,
    PublishPlatform,
    utcnow,
)
from app.schemas.common import EXPORT_MAX_ROWS, iso_utc, to_utc_naive
from app.schemas.link import LinkCreate
from app.services import alert_service, content_service, link_check_service, platform_service, settings_service, stats_service
from app.services.data_scope_service import (
    DataScope,
    get_visible,
    is_visible,
    scope_by_project,
    scope_link_children,
)

logger = logging.getLogger(__name__)

QUEUE_LINK_CHECKS = "queue:link_checks"
QUEUED_LINK_CHECK_PREFIX = "queued:link_check:"
QUEUED_INDEX_CHECK_PREFIX = "queued:index_check:"
QUEUED_TTL_SECONDS = 3600
ENQUEUE_DEFER = timedelta(hours=1)
PUBLISHED_AT_FUTURE_TOLERANCE = timedelta(minutes=5)
PUBLISHED_AT_MAX_AGE = timedelta(days=3650)
MAX_PUBLISH_ACCOUNT = 100
MAX_NOTE = 500
MAX_URL = 1000
MAX_TITLE_SNAPSHOT = 300
INDEX_KINDS = ("seo", "geo")
LINK_ALERT_TYPES = ("link_deleted", "link_changed", "index_overdue")

MSG_CONTENT_NOT_FOUND = "内容不存在"
MSG_CONTENT_NOT_APPROVED = "内容尚未审核通过"
MSG_PROJECT_ARCHIVED = "项目已归档"
MSG_PLATFORM_NOT_FOUND = "平台不存在"
MSG_PLATFORM_INACTIVE = "平台已停用"
MSG_LINK_EXISTS = "链接已存在"
MSG_OWNED_BY_OTHER = "该链接已由其他用户回填"
MSG_PUBLISHED_AT_RANGE = "发布时间超出允许范围"

EXPORT_COLUMNS_BASE: tuple[tuple[str, str], ...] = (
    ("id", "ID"),
    ("project_id", "项目 ID"),
    ("content_id", "内容 ID"),
    ("title_snapshot", "标题快照"),
    ("platform_code", "平台"),
    ("url", "URL"),
    ("normalized_url", "规范化 URL"),
    ("domain", "域名"),
    ("publish_account", "发布账号"),
    ("published_at", "发布时间"),
    ("alive_status", "存活状态"),
    ("last_checked_at", "最近检测时间"),
    ("last_http_status", "最近 HTTP 状态"),
    ("next_check_at", "下次检测时间"),
    ("seo_indexed_any", "SEO 已收录"),
    ("first_indexed_at", "首次收录时间"),
    ("geo_cited_any", "GEO 已引用"),
    ("first_cited_at", "首次引用时间"),
    ("last_index_checked_at", "最近收录检测时间"),
    ("next_index_check_at", "下次收录检测时间"),
    ("is_monitoring", "监控中"),
    ("note", "备注"),
    ("created_at", "回填时间"),
)
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


# =====================================================================
# 通用
# =====================================================================


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _not_found(message: str) -> BusinessError:
    return BusinessError(message, code=CODE_NOT_FOUND, http_status=404)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return to_utc_naive(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return to_utc_naive(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def monitoring_config(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "monitoring_config")


def index_check_config(db: Session) -> dict[str, Any]:
    return dict(monitoring_config(db).get("index_check") or {})


# =====================================================================
# 排程（§6.6、§7.5）
# =====================================================================


def compute_next_check_at(
    link: Any, result: str, previous: str, applied: str, *, now: datetime, cfg: Mapping[str, Any]
) -> datetime | None:
    """下次删除检测时间（docs/11 §6.6；``cfg = monitoring_config.link_check``，``applied`` 为应用阈值后写回的状态）。"""
    if not link.is_monitoring:
        return None
    if applied == "deleted" and result in ("deleted", "suspected_deleted"):
        # 进入 deleted 时 alive_changed_at 已按本次写回置为 now；deleted 状态下的跳转复检同样走此分支
        changed_at = link.alive_changed_at or now
        until = changed_at + timedelta(days=int(cfg["deleted_recheck_until_days"]))
        nxt = now + timedelta(days=int(cfg["deleted_recheck_days"]))
        return nxt if nxt <= until else None
    backoff = list(cfg["abnormal_backoff_hours"])
    if result in ("suspected_deleted", "unknown"):
        n = max(int(link.consecutive_unknown or 0), int(link.consecutive_suspected or 0), 1)  # 已含本次累加（饱和于 100）
        return now + timedelta(hours=float(backoff[min(n - 1, 2)]))
    if result == "changed" and previous != "changed":
        return now + timedelta(hours=float(backoff[0]))
    if (now - link.published_at).days <= int(cfg["initial_days"]):
        return now + timedelta(hours=float(cfg["initial_interval_hours"]))
    return now + timedelta(days=int(cfg["regular_interval_days"]))


def enabled_index_engines(db: Session) -> list[tuple[str, str]]:
    """参与自动排程的引擎 ``enabled_engines("seo") ∪ enabled_engines("geo")``：``seo_providers.engines`` 中 ``enabled=true``
    且属于 ``monitoring_config.index_check.seo_engines`` 的引擎（固定集合；提供器为 ``manual`` 的引擎只能人工标记，不参与
    自动排程），``geo_engines.engines[]`` 中 ``enabled=true`` 的引擎。"""
    seo_allowed = set(index_check_config(db).get("seo_engines") or ())
    seo_cfg = settings_service.get_config(db, "seo_providers").get("engines") or {}
    result: list[tuple[str, str]] = []
    for code, engine in seo_cfg.items():
        if not isinstance(engine, Mapping) or not engine.get("enabled") or engine.get("provider") == "manual":
            continue
        if not seo_allowed or code in seo_allowed:
            result.append(("seo", str(code)))
    for engine in settings_service.get_config(db, "geo_engines").get("engines") or []:
        if isinstance(engine, Mapping) and engine.get("enabled") and engine.get("code"):
            result.append(("geo", str(engine["code"])))
    return result


def expired_rounds(published_at: datetime, now: datetime, schedule_days: Sequence[int]) -> int:
    """``schedule_days`` 中已过期的轮次数（``published_at + d <= now`` 的个数），用于晚回填 / 恢复监控时初始化 ``index_check_count``。"""
    return sum(1 for d in schedule_days if published_at + timedelta(days=int(d)) <= now)


def due(engine_state: Mapping[str, Any] | None, link: Any, *, now: datetime, cfg: Mapping[str, Any]) -> datetime | None:
    """单引擎到期时间（docs/11 §7.5）。"""
    st = engine_state or {}
    if int(st.get("check_count", 0) or 0) >= int(cfg["max_checks_per_link_per_engine"]):
        return None
    schedule = [int(d) for d in cfg["schedule_days"]]
    checked_at = _parse_dt(st.get("checked_at"))
    # 从未检测：按排程轮次指针 index_check_count 取值。「从未检测」以 scheduled 次数 check_count 判定：manual 检测与人工标记
    # 也会写 checked_at，但不推进排程（§7.5「手动 / 人工标记」行、§17 第 5 条「手动检测不打乱排程」），否则晚回填链接在首个
    # scheduled 轮次执行前做一次手动检测，就会被 checked_at + monthly_interval_days 推迟一个月而错过本应立即执行的轮次。
    if checked_at is None or int(st.get("check_count", 0) or 0) == 0:
        i = int(link.index_check_count or 0)
        return link.published_at + timedelta(days=schedule[i]) if i < len(schedule) else now
    if st.get("status") in ("indexed", "cited"):
        return checked_at + timedelta(days=int(cfg["indexed_recheck_days"]))
    d = next((d for d in schedule if link.published_at + timedelta(days=d) > checked_at), None)
    return link.published_at + timedelta(days=d) if d is not None else checked_at + timedelta(days=int(cfg["monthly_interval_days"]))


def engine_states(link: Any) -> dict[str, dict[str, Any]]:
    return {
        "seo": _loads(getattr(link, "seo_status_json", None), {}),
        "geo": _loads(getattr(link, "geo_status_json", None), {}),
    }


def compute_next_index_check_at(
    link: Any,
    *,
    now: datetime | None = None,
    cfg: Mapping[str, Any] | None = None,
    engines: Iterable[tuple[str, str]] | None = None,
    db: Session | None = None,
) -> datetime | None:
    """``min(due(e))``，``e`` 遍历启用的 SEO / GEO 引擎；``alive_status=deleted`` / ``is_monitoring=0`` / 无启用引擎 /
    全部不再到期 → ``None``。``index_check.enabled=false`` 不置 ``None``（调度器不扫描即可）。

    ``cfg`` 缺省读 ``monitoring_config.index_check``，``engines`` 缺省读 ``enabled_index_engines``（二者缺省时须给 ``db``）。"""
    if not link.is_monitoring or link.alive_status == "deleted":
        return None
    now = now or utcnow()
    if cfg is None or engines is None:
        if db is None:
            raise ValueError("compute_next_index_check_at 需要 cfg 与 engines，或提供 db")
        cfg = cfg if cfg is not None else index_check_config(db)
        engines = engines if engines is not None else enabled_index_engines(db)
    states = engine_states(link)
    candidates = [due((states.get(kind) or {}).get(code), link, now=now, cfg=cfg) for kind, code in engines]
    candidates = [c for c in candidates if c is not None]
    return min(candidates) if candidates else None


# =====================================================================
# 入队（§6.7）
# =====================================================================


def queued_key(link_id: int) -> str:
    return f"{QUEUED_LINK_CHECK_PREFIX}{link_id}"


def enqueue_check(link: PublishLink, check_type: str, triggered_by: int | None, *, db: Session | None = None) -> bool:
    """``SET queued:link_check:{id} 1 NX EX 3600``（失败返回 ``False``）→ ``manual`` ``LPUSH`` 插队、其它 ``RPUSH`` →
    ``baseline`` / ``scheduled`` / ``retry`` 把 ``next_check_at`` 推后 1h（``manual`` 不触碰）。日上限不在此判定。

    给出 ``db`` 时立即提交 ``next_check_at``；否则只改对象，由调用方提交（调度器批量提交）。Redis 异常向上抛出。"""
    key = queued_key(link.id)
    if not redis_client.set(key, "1", nx=True, ex=QUEUED_TTL_SECONDS):
        return False
    payload = json.dumps({"link_id": link.id, "check_type": check_type, "triggered_by": triggered_by}, separators=(",", ":"))
    try:
        if check_type == "manual":
            redis_client.lpush(QUEUE_LINK_CHECKS, payload)
        else:
            redis_client.rpush(QUEUE_LINK_CHECKS, payload)
    except redis.RedisError:
        try:
            redis_client.delete(key)
        except redis.RedisError:
            pass
        raise
    if check_type != "manual":
        link.next_check_at = utcnow() + ENQUEUE_DEFER
        if db is not None:
            db.commit()
    return True


def _enqueue_result(link: PublishLink, check_type: str, triggered_by: int | None, db: Session) -> dict[str, Any]:
    queued = enqueue_check(link, check_type, triggered_by, db=db)
    return {"queued": True} if queued else {"queued": False, "reason": "already_queued"}


# =====================================================================
# 序列化
# =====================================================================


def _overdue(link: PublishLink, overdue_days: int | None, now: datetime) -> bool:
    if not overdue_days:
        return False
    return (
        link.alive_status in ("alive", "changed")
        and not link.seo_indexed_any
        and link.published_at <= now - timedelta(days=int(overdue_days))
    )


def link_item(link: PublishLink, *, overdue_days: int | None = None, now: datetime | None = None) -> dict[str, Any]:
    """扁平链接对象（docs/04 §7.10；不嵌套 ``platform``）；``index_overdue`` 为「超期未收录」标记
    （``monitoring_config.index_check.overdue_days``，只用于列表 / 详情展示）。"""
    item = content_service.link_item(link)
    item.pop("platform", None)
    item["index_overdue"] = _overdue(link, overdue_days, now or utcnow())
    return item


def _content_brief(content: Content | None) -> dict[str, Any] | None:
    if content is None:
        return None
    return {"id": content.id, "project_id": content.project_id, "title": content.title, "status": content.status,
            "link_count": content.link_count, "first_published_at": iso_utc(content.first_published_at)}


def _engine_providers(db: Session) -> dict[str, list[dict[str, Any]]]:
    seo_cfg = settings_service.get_config(db, "seo_providers").get("engines") or {}
    geo_cfg = settings_service.get_config(db, "geo_engines").get("engines") or []
    return {
        "seo": [{"engine": code, "provider": e.get("provider"), "enabled": bool(e.get("enabled"))}
                for code, e in seo_cfg.items() if isinstance(e, Mapping)],
        "geo": [{"engine": e.get("code"), "name": e.get("name"), "provider": "zhiqi_model", "enabled": bool(e.get("enabled"))}
                for e in geo_cfg if isinstance(e, Mapping)],
    }


def link_detail(db: Session, scope: DataScope, link: PublishLink) -> dict[str, Any]:
    """详情：扁平字段 + ``platform`` + ``content`` 摘要（可见时）+ ``last_check`` + ``engines``（引擎提供器）。"""
    overdue_days = index_check_config(db).get("overdue_days")
    item = link_item(link, overdue_days=overdue_days)
    item["platform"] = platform_service.platform_brief(db.get(PublishPlatform, link.platform_id))
    content = db.get(Content, link.content_id)
    item["content"] = _content_brief(content) if content is not None and is_visible(db, scope, content) else None
    last = db.scalars(
        select(LinkCheck).where(LinkCheck.link_id == link.id).order_by(LinkCheck.checked_at.desc(), LinkCheck.id.desc()).limit(1)
    ).first()
    item["last_check"] = link_check_service.link_check_item(last) if last is not None else None
    item["engines"] = _engine_providers(db)
    return item


def index_check_item(check: IndexCheck) -> dict[str, Any]:
    """收录检测记录（docs/04 §7.12）。"""
    return {
        "id": check.id,
        "link_id": check.link_id,
        "kind": check.kind,
        "engine": check.engine,
        "provider": check.provider,
        "check_type": check.check_type,
        "result_status": check.result_status,
        "previous_status": check.previous_status,
        "match_mode": check.match_mode,
        "query_text": check.query_text,
        "ai_task_id": check.ai_task_id,
        "request_id": check.request_id,
        "model": check.model,
        "evidence_title": check.evidence_title,
        "evidence_snippet": check.evidence_snippet,
        "evidence_url": check.evidence_url,
        "evidence": _loads(check.evidence_json, {}) if check.evidence_json else None,
        "confidence": float(check.confidence) if check.confidence is not None else None,
        "duration_ms": check.duration_ms,
        "error_category": check.error_category,
        "error_message": check.error_message,
        "checked_at": iso_utc(check.checked_at),
        "triggered_by": check.triggered_by,
    }


# =====================================================================
# 回填（§4.1、§4.4）
# =====================================================================


def _check_published_at(value: datetime | None, now: datetime, loc: Sequence[str | int]) -> datetime:
    if value is None:
        return now
    normalized = to_utc_naive(value).replace(microsecond=0)
    if normalized > now + PUBLISHED_AT_FUTURE_TOLERANCE or normalized < now - PUBLISHED_AT_MAX_AGE:
        raise invalid_params(field_error(loc, MSG_PUBLISHED_AT_RANGE, "value_error", iso_utc(normalized)))
    return normalized


def _clean_text(value: str | None, limit: int, loc: Sequence[str | int]) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise invalid_params(field_error(loc, f"长度不能超过 {limit} 个字符", "string_too_long", value))
    return text


def _require_platform(db: Session, platform_id: int) -> PublishPlatform:
    platform = db.get(PublishPlatform, platform_id) if platform_id < 2**63 else None
    if platform is None:
        raise _not_found(MSG_PLATFORM_NOT_FOUND)
    if not platform.is_active:
        raise _conflict(MSG_PLATFORM_INACTIVE, {"reason": "platform_inactive"})
    return platform


def _duplicate_error(db: Session, scope: DataScope, existing: PublishLink) -> BusinessError:
    if is_visible(db, scope, existing):
        return _conflict(MSG_LINK_EXISTS, {"existing_id": existing.id})
    return _conflict(MSG_OWNED_BY_OTHER, {"existing_id": None, "reason": "owned_by_other"})


def recompute_first_published_at(db: Session, content: Content) -> None:
    """``contents.first_published_at = MIN(publish_links.published_at WHERE content_id = …)``（无链接时 NULL）。"""
    db.flush()
    content.first_published_at = db.scalar(select(func.min(PublishLink.published_at)).where(PublishLink.content_id == content.id))


def _insert_link(db: Session, scope: DataScope, body: LinkCreate, admin_id: int) -> tuple[PublishLink, Content, bool]:
    """校验（§4.1 第 1~7 步）+ 同事务写入（§4.4 第 1~3 步），不提交。返回 ``(link, content, 是否由 approved 转为 published)``。"""
    now = utcnow()
    content = get_visible(db, scope, Content, body.content_id)          # 1
    if content.status not in ("approved", "published"):
        raise _conflict(MSG_CONTENT_NOT_APPROVED, {"current_status": content.status})
    project = db.get(Project, content.project_id)                                                    # 2
    if project is None or project.status != "active":
        raise _conflict(MSG_PROJECT_ARCHIVED, {"current_status": project.status if project else "archived"})
    mon = monitoring_config(db)
    link_cfg = mon.get("link_check") or {}
    url = body.url if isinstance(body.url, str) else ""
    try:                                                                                             # 3
        safe_fetch.normalize_public_url(url, allow_http=bool(link_cfg.get("allow_http", True)))
        normalized = urls.normalize_url(url)
    except (safe_fetch.FetchBlocked, urls.InvalidURLError) as exc:
        message = exc.message if isinstance(exc, safe_fetch.FetchError) else str(exc)
        raise invalid_params(field_error(["body", "url"], message, "value_error", url)) from None
    if len(normalized) > MAX_URL:
        raise invalid_params(field_error(["body", "url"], f"URL 规范化后长度超过 {MAX_URL} 个字符", "value_error", url))
    if body.platform_id is not None:                                                                 # 4
        platform = _require_platform(db, body.platform_id)
    else:
        entry = platform_service.detect_entry(db, normalized)
        if entry is None:
            raise _not_found(MSG_PLATFORM_NOT_FOUND)
        platform = _require_platform(db, int(entry["id"]))
    digest = urls.url_hash(normalized)                                                               # 5
    existing = db.scalar(select(PublishLink).where(PublishLink.url_hash == digest).limit(1))
    if existing is not None:
        raise _duplicate_error(db, scope, existing)
    published_at = _check_published_at(body.published_at, now, ["body", "published_at"])           # 6
    publish_account = _clean_text(body.publish_account, MAX_PUBLISH_ACCOUNT, ["body", "publish_account"])  # 7
    note = _clean_text(body.note, MAX_NOTE, ["body", "note"])

    idx_cfg = mon.get("index_check") or {}
    link = PublishLink(
        project_id=content.project_id,
        content_id=content.id,
        platform_id=platform.id,
        url=url.strip(),
        normalized_url=normalized,
        url_hash=digest,
        domain=urls.extract_domain(normalized)[:255],
        publish_account=publish_account,
        published_at=published_at,
        backfilled_by=admin_id,
        title_snapshot=(content.title or "")[:MAX_TITLE_SNAPSHOT],
        alive_status="pending",
        next_check_at=now,
        check_count=0,
        consecutive_unknown=0,
        consecutive_suspected=0,
        index_check_count=expired_rounds(published_at, now, idx_cfg.get("schedule_days") or []),
        index_checks_done=0,
        is_monitoring=True,
        note=note,
    )
    link.next_index_check_at = compute_next_index_check_at(link, now=now, cfg=idx_cfg, engines=enabled_index_engines(db))
    db.add(link)
    content.link_count = int(content.link_count or 0) + 1
    published_now = content.status == "approved"
    content_service.transition(content, "publish", admin_id, db=db)
    recompute_first_published_at(db, content)
    stats_service.increment_realtime_after_commit(db, link.project_id, {"links_backfilled": 1}, at=now)
    if published_now:
        stats_service.increment_realtime_after_commit(db, content.project_id, {"contents_published": 1},
                                                      at=content.first_published_at or now)
    return link, content, published_now


def _commit_link(db: Session, scope: DataScope, link: PublishLink) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(PublishLink).where(PublishLink.url_hash == link.url_hash).limit(1))
        if existing is not None:
            raise _duplicate_error(db, scope, existing) from None
        raise


def _enqueue_baseline(db: Session, link: PublishLink, admin_id: int | None) -> dict[str, Any]:
    """提交后入队基线检测；入队抛错（Redis 不可用）时链接已落库、``next_check_at`` 已到期，由调度器以 ``baseline`` 补检。"""
    try:
        return _enqueue_result(link, "baseline", admin_id, db)
    except (redis.RedisError, OSError) as exc:
        logger.warning("回填后基线检测入队失败 link_id=%s（由 schedule_link_checks 补检）: %s", link.id, exc)
        db.rollback()
        return {"queued": False}


def backfill(db: Session, scope: DataScope, body: LinkCreate | Mapping[str, Any], admin_id: int) -> dict[str, Any]:
    """``POST /admin/links`` → ``{link, queued, reason?}``（``reason=already_queued`` 仅在入队去重命中时出现）。"""
    if not isinstance(body, LinkCreate):
        body = LinkCreate.model_validate(dict(body))
    try:
        link, _content, _published = _insert_link(db, scope, body, admin_id)
    except Exception:
        db.rollback()
        raise
    _commit_link(db, scope, link)
    result = _enqueue_baseline(db, link, admin_id)
    overdue_days = index_check_config(db).get("overdue_days")
    return {"link": link_item(link, overdue_days=overdue_days), **result}


def _item_failure(index: int, exc: BusinessError) -> dict[str, Any]:
    data = exc.data
    message = exc.message
    if exc.code == 400 and isinstance(data, list) and data and isinstance(data[0], Mapping) and data[0].get("msg"):
        message = str(data[0]["msg"])
    result: dict[str, Any] = {"index": index, "ok": False, "link_id": None, "queued": False, "code": exc.code, "message": message}
    if exc.code == CODE_CONFLICT and isinstance(data, Mapping):
        if data.get("reason") == "owned_by_other":
            result["reason"] = "owned_by_other"
        elif data.get("existing_id"):
            result["link_id"] = data["existing_id"]
    return result


def batch_backfill(db: Session, scope: DataScope, items: Sequence[Mapping[str, Any]], admin_id: int) -> dict[str, Any]:
    """``POST /admin/links/batch``：逐条按单条规则校验、逐条独立事务（单条失败不影响其它条），整体返回
    ``{created, failed, results:[{index, ok, link_id, queued, code, message, reason?}]}``。"""
    results: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        try:
            if not isinstance(raw, Mapping):
                raise invalid_params(field_error(["body", "items", index], "必须是对象", "dict_type", None))
            try:
                body = LinkCreate.model_validate(dict(raw))
            except ValidationError as exc:
                raise invalid_params(format_validation_errors(exc.errors())) from None
            data = backfill(db, scope, body, admin_id)
            results.append({"index": index, "ok": True, "link_id": data["link"]["id"], "queued": bool(data["queued"]),
                            "code": 0, "message": "ok"})
        except BusinessError as exc:
            db.rollback()
            results.append(_item_failure(index, exc))
        except Exception:  # noqa: BLE001 - 单条异常不影响其它条
            db.rollback()
            logger.exception("批量回填第 %s 条失败", index)
            results.append({"index": index, "ok": False, "link_id": None, "queued": False, "code": 500, "message": "服务器内部错误"})
    created = sum(1 for r in results if r["ok"])
    return {"created": created, "failed": len(results) - created, "results": results}


# =====================================================================
# 查询
# =====================================================================


def get_link_row(db: Session, scope: DataScope, link_id: int) -> PublishLink:
    return get_visible(db, scope, PublishLink, link_id)


def get_link(db: Session, scope: DataScope, link_id: int) -> dict[str, Any]:
    return link_detail(db, scope, get_link_row(db, scope, link_id))


def _list_conditions(
    *,
    project_id: int | None = None,
    content_id: int | None = None,
    platform_id: int | None = None,
    alive_status: str | None = None,
    seo_indexed_any: bool | None = None,
    geo_cited_any: bool | None = None,
    is_monitoring: bool | None = None,
    keyword: str | None = None,
    published_start: datetime | None = None,
    published_end: datetime | None = None,
) -> list[Any]:
    conditions: list[Any] = []
    if project_id is not None:
        conditions.append(PublishLink.project_id == project_id)
    if content_id is not None:
        conditions.append(PublishLink.content_id == content_id)
    if platform_id is not None:
        conditions.append(PublishLink.platform_id == platform_id)
    if alive_status:
        conditions.append(PublishLink.alive_status == alive_status)
    if seo_indexed_any is not None:
        conditions.append(PublishLink.seo_indexed_any == seo_indexed_any)
    if geo_cited_any is not None:
        conditions.append(PublishLink.geo_cited_any == geo_cited_any)
    if is_monitoring is not None:
        conditions.append(PublishLink.is_monitoring == is_monitoring)
    if keyword and keyword.strip():
        term = keyword.strip()
        conditions.append(or_(
            PublishLink.url.contains(term, autoescape=True),
            PublishLink.normalized_url.contains(term, autoescape=True),
            PublishLink.title_snapshot.contains(term, autoescape=True),
        ))
    if published_start is not None:
        conditions.append(PublishLink.published_at >= to_utc_naive(published_start))
    if published_end is not None:
        conditions.append(PublishLink.published_at <= to_utc_naive(published_end))
    return conditions


def list_links(db: Session, scope: DataScope, *, page: int = 1, page_size: int = 20, **filters: Any) -> tuple[list[dict[str, Any]], int]:
    """``GET /admin/links``：分页，按 ``id`` 倒序；筛选见 docs/04 §6.17；不可见的筛选对象与范围取交集（空结果）。"""
    conditions = _list_conditions(**filters)
    total = int(db.scalar(scope_by_project(select(func.count(PublishLink.id)), PublishLink.project_id, scope).where(*conditions)) or 0)
    rows = db.scalars(
        scope_by_project(select(PublishLink), PublishLink.project_id, scope).where(*conditions)
        .order_by(PublishLink.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    overdue_days = index_check_config(db).get("overdue_days")
    now = utcnow()
    return [link_item(r, overdue_days=overdue_days, now=now) for r in rows], total


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def export_links(db: Session, scope: DataScope, **filters: Any) -> tuple[str, str]:
    """``GET /admin/links/export``：同列表筛选，UTF-8 BOM CSV（含按引擎收录状态与最近检测），最多 50,000 行（超出 400）。"""
    conditions = _list_conditions(**filters)
    total = int(db.scalar(scope_by_project(select(func.count(PublishLink.id)), PublishLink.project_id, scope).where(*conditions)) or 0)
    if total > EXPORT_MAX_ROWS:
        raise invalid_params(
            field_error(["query"], f"导出行数 {total} 超过上限 {EXPORT_MAX_ROWS}，请缩小筛选范围", "too_many_rows", total)
        )
    idx_cfg = index_check_config(db)
    seo_engines = [str(e) for e in idx_cfg.get("seo_engines") or []]
    geo_engines = [str(e.get("code")) for e in settings_service.get_config(db, "geo_engines").get("engines") or []
                   if isinstance(e, Mapping) and e.get("code")]
    platforms = {p.id: p.code for p in db.scalars(select(PublishPlatform)).all()}
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow([h for _, h in EXPORT_COLUMNS_BASE] + [f"SEO {e}" for e in seo_engines] + [f"GEO {e}" for e in geo_engines])
    stmt = scope_by_project(select(PublishLink), PublishLink.project_id, scope).where(*conditions).order_by(PublishLink.id.desc())
    for link in db.scalars(stmt).yield_per(1000):
        item = link_item(link)
        item["platform_code"] = platforms.get(link.platform_id, "")
        states = engine_states(link)
        row = [_csv_cell(item.get(key)) for key, _ in EXPORT_COLUMNS_BASE]
        row += [_csv_cell((states["seo"].get(e) or {}).get("status")) for e in seo_engines]
        row += [_csv_cell((states["geo"].get(e) or {}).get("status")) for e in geo_engines]
        writer.writerow(row)
    filename = f"links-{stats_service.today_date(db).strftime('%Y%m%d')}.csv"
    return filename, buffer.getvalue()


def list_checks(db: Session, scope: DataScope, link_id: int, *, page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    """``GET /admin/links/{id}/checks``：删除检测历史（按 ``checked_at`` 倒序）。"""
    link = get_link_row(db, scope, link_id)
    base = scope_link_children(select(LinkCheck), LinkCheck.link_id, scope).where(LinkCheck.link_id == link.id)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = db.scalars(base.order_by(LinkCheck.checked_at.desc(), LinkCheck.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return [link_check_service.link_check_item(r) for r in rows], total


def list_index_checks(
    db: Session, scope: DataScope, link_id: int, *, kind: str | None = None, engine: str | None = None, page: int = 1, page_size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    """``GET /admin/links/{id}/index-checks``：收录检测历史（``kind`` / ``engine`` 筛选，按 ``checked_at`` 倒序）。"""
    link = get_link_row(db, scope, link_id)
    base = scope_link_children(select(IndexCheck), IndexCheck.link_id, scope).where(IndexCheck.link_id == link.id)
    if kind:
        base = base.where(IndexCheck.kind == kind)
    if engine:
        base = base.where(IndexCheck.engine == engine)
    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = db.scalars(base.order_by(IndexCheck.checked_at.desc(), IndexCheck.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return [index_check_item(r) for r in rows], total


# =====================================================================
# 编辑 / 删除 / 暂停 / 恢复（§4.5）
# =====================================================================


def update_link(db: Session, scope: DataScope, link_id: int, values: Mapping[str, Any], *, admin_id: int | None = None) -> dict[str, Any]:
    """``PUT /admin/links/{id}``：可改 ``platform_id`` / ``publish_account`` / ``published_at`` / ``note``；URL 不可改。
    改 ``published_at`` 时同事务重算 ``next_index_check_at`` 与 ``contents.first_published_at``（``index_check_count`` /
    ``index_checks_done`` / ``next_check_at`` 不变）；改平台后下次删除检测按新平台规则判定。"""
    del admin_id
    link = get_link_row(db, scope, link_id)
    now = utcnow()
    try:
        platform_id = values.get("platform_id")
        if platform_id is not None and platform_id != link.platform_id:
            link.platform_id = _require_platform(db, int(platform_id)).id
        if "publish_account" in values:
            link.publish_account = _clean_text(values.get("publish_account"), MAX_PUBLISH_ACCOUNT, ["body", "publish_account"])
        if "note" in values:
            link.note = _clean_text(values.get("note"), MAX_NOTE, ["body", "note"])
        published_at = values.get("published_at")
        if published_at is not None:
            new_value = _check_published_at(published_at, now, ["body", "published_at"])
            if new_value != link.published_at:
                link.published_at = new_value
                link.next_index_check_at = compute_next_index_check_at(link, now=now, db=db)
                content = db.get(Content, link.content_id)
                if content is not None:
                    recompute_first_published_at(db, content)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return link_detail(db, scope, link)


def _clear_queue_markers(link_id: int) -> None:
    keys = [queued_key(link_id)] + [f"{QUEUED_INDEX_CHECK_PREFIX}{link_id}:{kind}" for kind in INDEX_KINDS]
    try:
        redis_client.delete(*keys)
    except redis.RedisError as exc:
        logger.warning("删除链接后清理队列标记失败 link_id=%s: %s", link_id, exc)


def delete_link(db: Session, scope: DataScope, link_id: int, *, admin_id: int | None = None) -> dict[str, Any]:
    """``DELETE /admin/links/{id}``：同事务级联删除 ``link_checks`` / ``index_checks``、``contents.link_count -= 1``、重算
    ``first_published_at``、``published`` 且链接归零回到 ``approved``、自动解决该链接 ``open`` / ``acknowledged`` 的
    ``link_deleted`` / ``link_changed`` / ``index_overdue``（``resolution_note='auto'``）；提交后 ``DEL`` 队列标记。
    返回审计摘要用的 ``{id, content_id, alerts_resolved, content_status}``。"""
    link = get_link_row(db, scope, link_id)
    target_key = str(link.id)
    try:
        db.execute(delete(LinkCheck).where(LinkCheck.link_id == link.id))
        db.execute(delete(IndexCheck).where(IndexCheck.link_id == link.id))
        resolved = sum(alert_service.resolve_alert(db, scope, alert_type, "publish_link", target_key) for alert_type in LINK_ALERT_TYPES)
        content = db.get(Content, link.content_id)
        db.delete(link)
        db.flush()
        content_status = None
        if content is not None:
            content.link_count = max(0, int(content.link_count or 0) - 1)
            recompute_first_published_at(db, content)
            if content.status == "published" and content.link_count == 0:
                content_service.transition(content, "unpublish", admin_id, db=db)
            content_status = content.status
        after_commit(db, lambda: _clear_queue_markers(int(target_key)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"id": int(target_key), "content_id": content.id if content is not None else None,
            "alerts_resolved": resolved, "content_status": content_status}


def pause(db: Session, scope: DataScope, link_id: int) -> dict[str, Any]:
    """``POST /admin/links/{id}/pause``：``is_monitoring=0``、``next_check_at=NULL``、``next_index_check_at=NULL``。"""
    link = get_link_row(db, scope, link_id)
    link.is_monitoring = False
    link.next_check_at = None
    link.next_index_check_at = None
    db.commit()
    return link_detail(db, scope, link)


def resume(db: Session, scope: DataScope, link_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/links/{id}/resume``：``is_monitoring=1``；``next_check_at = max(now, compute_next_check_at(link, s, s, s,
    now=last_checked_at or now))``（``None`` 时保持 ``NULL``）；``pending`` 链接直接入队基线检测；按 §7.5 重新初始化
    ``index_check_count`` 后重算 ``next_index_check_at``。返回链接详情并附 ``queued``。"""
    link = get_link_row(db, scope, link_id)
    now = utcnow()
    mon = monitoring_config(db)
    link_cfg = mon.get("link_check") or {}
    idx_cfg = mon.get("index_check") or {}
    status = link.alive_status
    link.is_monitoring = True
    nxt = compute_next_check_at(link, status, status, status, now=link.last_checked_at or now, cfg=link_cfg)
    link.next_check_at = max(now, nxt) if nxt is not None else None
    link.index_check_count = expired_rounds(link.published_at, now, idx_cfg.get("schedule_days") or [])
    link.next_index_check_at = compute_next_index_check_at(link, now=now, cfg=idx_cfg, engines=enabled_index_engines(db))
    db.commit()
    queued: dict[str, Any] = {"queued": False}
    if status == "pending":
        queued = _enqueue_baseline(db, link, admin_id)
    item = link_detail(db, scope, link)
    item.update(queued)
    return item


def rebaseline(db: Session, scope: DataScope, link_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/links/{id}/rebaseline``：同事务清空 ``baseline_*``（``alive_status`` 暂不变）并 ``resolve_alert(link_changed)``，
    提交后 ``enqueue_check(link, "manual", admin_id)`` → ``{queued, reason?}``（只计数、不受日上限拦截）。"""
    link = get_link_row(db, scope, link_id)
    link.baseline_title = None
    link.baseline_simhash = None
    link.baseline_excerpt = None
    link.baseline_captured_at = None
    alert_service.resolve_alert(db, scope, "link_changed", "publish_link", str(link.id))
    db.commit()
    return _enqueue_result(link, "manual", admin_id, db)


def check_now(db: Session, scope: DataScope, link_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/links/{id}/check``：``enqueue_check(link, "manual", admin_id)`` 插队 → ``{queued:true}`` 或
    ``{queued:false, reason:"already_queued"}``；不受日上限拦截（暂停监控的链接手动检测仍执行一次）。"""
    link = get_link_row(db, scope, link_id)
    return _enqueue_result(link, "manual", admin_id, db)


# =====================================================================
# 批量手动检测（POST /admin/monitoring/link-checks/run，docs/11 §6.7、docs/13 §7.5）
# =====================================================================


def run_link_checks_batch(
    db: Session,
    scope: DataScope,
    *,
    admin_id: int | None,
    project_id: int | None = None,
    platform_id: int | None = None,
    link_ids: Sequence[int] | None = None,
    only_due: bool = True,
    limit: int = 1000,
) -> dict[str, int]:
    """批量入队 ``manual`` 删除检测 → ``{enqueued, skipped}``：不可见的 ``link_ids`` 计入 ``skipped``；``project_id`` 不可见时
    ``{0, 0}``；入队前读取 ``limit:link_checks:{date}``，超出剩余额度与已在队列的链接计入 ``skipped``；``only_due`` 只处理
    ``is_monitoring=1 AND next_check_at <= now`` 的链接。"""
    skipped = 0
    stmt = scope_by_project(select(PublishLink), PublishLink.project_id, scope)
    if project_id is not None:
        project = db.get(Project, project_id) if project_id < 2**63 else None
        if project is None or not is_visible(db, scope, project):
            return {"enqueued": 0, "skipped": 0}
        stmt = stmt.where(PublishLink.project_id == project_id)
    if platform_id is not None:
        stmt = stmt.where(PublishLink.platform_id == platform_id)
    if link_ids:
        wanted = list(dict.fromkeys(int(i) for i in link_ids))
        stmt = stmt.where(PublishLink.id.in_(wanted))
    if only_due:
        stmt = stmt.where(PublishLink.is_monitoring == True, PublishLink.next_check_at <= utcnow())  # noqa: E712
    rows = list(db.scalars(stmt.order_by(PublishLink.next_check_at, PublishLink.id).limit(max(1, int(limit)))).all())
    if link_ids:
        found = {r.id for r in rows}
        skipped += sum(1 for i in dict.fromkeys(int(i) for i in link_ids) if i not in found)
    cfg = link_check_service.link_check_config(db)
    remaining = max(0, int(cfg.get("daily_limit") or 0) - link_check_service.used_today(db))
    enqueued = 0
    for link in rows:
        if enqueued >= remaining:
            skipped += 1
            continue
        if enqueue_check(link, "manual", admin_id):
            enqueued += 1
        else:
            skipped += 1
    return {"enqueued": enqueued, "skipped": skipped}


__all__ = [
    "QUEUE_LINK_CHECKS",
    "backfill",
    "batch_backfill",
    "check_now",
    "compute_next_check_at",
    "compute_next_index_check_at",
    "delete_link",
    "due",
    "enabled_index_engines",
    "engine_states",
    "enqueue_check",
    "expired_rounds",
    "export_links",
    "get_link",
    "get_link_row",
    "index_check_item",
    "link_detail",
    "link_item",
    "list_checks",
    "list_index_checks",
    "list_links",
    "pause",
    "queued_key",
    "rebaseline",
    "recompute_first_published_at",
    "resume",
    "run_link_checks_batch",
    "update_link",
]
