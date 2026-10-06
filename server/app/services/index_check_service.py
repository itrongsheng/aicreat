"""SEO 收录 / GEO 引用检测执行、写回与排程（docs/11 §7.4~§7.6、§8.6、§8.7、§9；docs/03「检测写回」第 3~5 条、B.20、B.22；
docs/04 §6.17、§7.11；docs/01 §5.2 第 3、4 行）。

- ``enabled_engines(kind)``：``seo_providers.engines`` / ``geo_engines.engines[]`` 中 ``enabled=true`` 的引擎（报表维度行也用它）；
  自动排程用 ``schedulable_engines``（同上，但提供器为 ``manual`` 的 SEO 引擎只能人工标记，不参与自动检测）；
- ``enqueue``：入队前按引擎数预扣 ``limit:index_checks:{date}``（超 ``daily_limit`` 退回并返回 ``daily_limit``）→
  ``SET NX queued:index_check:{link_id}:{kind} EX 3600``（全部 kind 已在队列 → ``already_queued``）→ ``RPUSH``（手动 ``LPUSH``
  插队）``queue:index_checks``；``scheduled`` 入队后 ``next_index_check_at = now + 1h``；
- ``run(db, scope, link, kinds, engines, check_type, triggered_by)``：持锁者（``run_index_checks.process_one``）调用；
  ``ai:paused:*`` → 整条链接延后 ``pause_seconds``；逐引擎（先 SEO 后 GEO）：GEO 先 ``precheck``（``title_in_prompt``
  不建根任务）→ zhiqi 类提供器 / GEO 引擎创建同步根任务（``check_quota`` 预占，4291 → ``unknown(quota_exceeded,
  local_quota_limit)``）→ ``provider.check`` / ``geo_engine.check`` → **独立事务**：``finalize_root`` + ``SELECT … FOR UPDATE``
  重读链接 → ``INSERT index_checks`` → 投影（``unknown`` 不覆盖原 ``status``）、``*_any`` 重算、``first_*_at``、
  ``last_index_checked_at`` → 提交后 ``stats:rt``；每引擎后 ``EXPIRE lock:monitor:index_check:{link_id} 900``；全部完成：
  ``scheduled`` → ``index_check_count += 1``、``index_checks_done += 1``、重算 ``next_index_check_at``（本地额度命中时推到次日
  00:00）；``manual`` 排程不变；
- ``mark_index``：人工标记（B.22 固定写法），按同一套投影写回，不改排程计数；
- ``request_index_check``：``POST /admin/links/{id}/index-check``（400 未启用引擎 / 409 已删除或暂停 / 429 ``30/hour``）；
- ``run_index_checks_batch``：``POST /admin/monitoring/index-checks/run``；
- ``recompute_null_schedules``：保存 ``seo_providers`` / ``geo_engines`` 后重算 ``next_index_check_at IS NULL`` 的链接。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.exceptions import CODE_CONFLICT, BusinessError, field_error, invalid_params
from app.core.locks import extend_lock
from app.core.ratelimit import check_rate_limit
from app.core.redis import redis_client
from app.core.zhiqi.types import Capability, Protocol
from app.models import AiTask, Content, IndexCheck, Keyword, Project, PublishLink, PublishPlatform, utcnow
from app.schemas.common import iso_utc
from app.services import ai_gateway_service as gateway
from app.services import link_service, settings_service, stats_service
from app.services.data_scope_service import DataScope, is_visible, scope_by_project
from app.services.index_providers import get_geo_engine, get_seo_provider
from app.services.index_providers.base import (
    MSG_LOCAL_QUOTA_LIMIT,
    NEGATIVE_STATUS,
    POSITIVE_STATUS,
    SEO_ENGINE_NAMES,
    CheckContext,
    CheckResult,
    classify_exception,
    failure_result,
)
from app.services.index_providers.manual import ManualProvider

logger = logging.getLogger(__name__)

QUEUE_INDEX_CHECKS = "queue:index_checks"
QUEUED_PREFIX = link_service.QUEUED_INDEX_CHECK_PREFIX          # queued:index_check:{link_id}:{kind}
QUEUED_TTL_SECONDS = 3600
LIMIT_KEY_PREFIX = "limit:index_checks:"
LIMIT_KEY_TTL_SECONDS = 172800
LOCK_PREFIX = "lock:monitor:index_check:"
LOCK_TTL_SECONDS = 900
LOCK_BUSY_DEFER = timedelta(seconds=300)
ENQUEUE_DEFER = timedelta(hours=1)
MANUAL_RATE = "30/hour"
MANUAL_RATE_PREFIX = "rate:index_check_manual:"
DEFAULT_PAUSE_SECONDS = 600
KINDS: tuple[str, ...] = ("seo", "geo")
ZHIQI_SEO_PROVIDER = "zhiqi_web_search"
QUOTA_PROMPT_TOKENS = 300                       # §8.6 估算口径：prompt ≈ 300 tokens
QUOTA_COMPLETION_TOKENS = 800                   # 路由 params 无 max_tokens 时的 completion 估算

MAX_QUERY_TEXT = 500
MAX_EVIDENCE_TITLE = 300
MAX_EVIDENCE_SNIPPET = 1000
MAX_EVIDENCE_URL = 1000
MAX_ERROR_MESSAGE = 500
MAX_MODEL = 120
MAX_REQUEST_ID = 64
MAX_NOTE = 500

MANUAL_STATUSES = {"seo": ("indexed", "not_indexed"), "geo": ("cited", "not_cited")}

MSG_LINK_DELETED = "链接已被删除，无法检测收录"
MSG_MONITORING_PAUSED = "链接已暂停监控，请先恢复监控"
MSG_ENGINE_DISABLED = "引擎未启用或不属于所选类型"
MSG_ENGINE_MANUAL = "该引擎配置为人工标记，请使用人工标记"
MSG_NO_ENABLED_ENGINE = "所选类型下没有启用的引擎"
MSG_ENGINE_NOT_FOUND = "引擎不存在"
MSG_STATUS_MISMATCH = "status 与 kind 不匹配：kind=seo 只能是 indexed / not_indexed，kind=geo 只能是 cited / not_cited"
MSG_EVIDENCE_URL = "evidence_url 必须是 http(s) URL"


# =====================================================================
# 配置与引擎集合
# =====================================================================


def index_check_config(db: Session) -> dict[str, Any]:
    return link_service.index_check_config(db)


def _seo_engines_cfg(db: Session) -> dict[str, dict[str, Any]]:
    engines = settings_service.get_config(db, "seo_providers").get("engines") or {}
    return {str(k): dict(v) for k, v in engines.items() if isinstance(v, Mapping)}


def _seo_providers_cfg(db: Session) -> dict[str, dict[str, Any]]:
    providers = settings_service.get_config(db, "seo_providers").get("providers") or {}
    return {str(k): dict(v) for k, v in providers.items() if isinstance(v, Mapping)}


def _geo_engines_cfg(db: Session) -> dict[str, dict[str, Any]]:
    engines = settings_service.get_config(db, "geo_engines").get("engines") or []
    return {str(e["code"]): dict(e) for e in engines if isinstance(e, Mapping) and e.get("code")}


def seo_engine_codes(db: Session) -> list[str]:
    """SEO 引擎集合（固定 ``monitoring_config.index_check.seo_engines``，缺省 baidu / bing / google）。"""
    codes = [str(c) for c in index_check_config(db).get("seo_engines") or []]
    return codes or list(SEO_ENGINE_NAMES)


def _enabled(db: Session, kind: str) -> list[str]:
    if kind == "seo":
        allowed = set(seo_engine_codes(db))
        return [code for code, cfg in _seo_engines_cfg(db).items() if cfg.get("enabled") and code in allowed]
    if kind == "geo":
        return [code for code, cfg in _geo_engines_cfg(db).items() if cfg.get("enabled")]
    raise ValueError(f"未知 kind: {kind}")


def enabled_engines(kind: str, db: Session | None = None) -> list[str]:
    """``enabled_engines("seo"|"geo")``：配置中 ``enabled=true`` 的引擎 code（不区分提供器）。未给 ``db`` 时自开会话。"""
    if db is not None:
        return _enabled(db, kind)
    with SessionLocal() as session:
        return _enabled(session, kind)


def schedulable_engines(db: Session) -> list[tuple[str, str]]:
    """自动检测（定时 / 手动 ``index-check`` 缺省）使用的 ``(kind, engine)``：启用且提供器不是 ``manual``，先 SEO 后 GEO。"""
    return link_service.enabled_index_engines(db)


def _pause_seconds(db: Session) -> int:
    value = settings_service.get_config(db, "ai_routing_config").get("pause_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_PAUSE_SECONDS


def next_day_start(db: Session | None = None) -> datetime:
    """次日 00:00（``stats_config.timezone``）对应的 UTC 时间（naive）。"""
    return stats_service.day_bounds(stats_service.today_date(db), db)[1]


# =====================================================================
# 日上限（limit:index_checks:{date}，入队时预扣）
# =====================================================================


def limit_key(db: Session | None = None) -> str:
    return f"{LIMIT_KEY_PREFIX}{stats_service.today_date(db).isoformat()}"


def used_today(db: Session | None = None) -> int:
    try:
        return int(redis_client.get(limit_key(db)) or 0)
    except (redis.RedisError, ValueError) as exc:
        logger.warning("读取 limit:index_checks 失败: %s", exc)
        return 0


def _reserve(key: str, count: int, daily_limit: int) -> bool:
    """``INCRBY key count`` + ``EXPIRE 172800``；超过 ``daily_limit``（> 0）时退回并返回 ``False``。"""
    pipe = redis_client.pipeline(transaction=True)
    pipe.incrby(key, count)
    pipe.expire(key, LIMIT_KEY_TTL_SECONDS)
    used, _ = pipe.execute()
    if daily_limit > 0 and int(used) > daily_limit:
        redis_client.decrby(key, count)
        return False
    return True


def _refund(key: str, count: int) -> None:
    if count <= 0:
        return
    try:
        redis_client.decrby(key, count)
    except redis.RedisError as exc:
        logger.warning("退回 limit:index_checks 预扣失败: %s", exc)


# =====================================================================
# 入队（§7.5、§9）
# =====================================================================


def queued_key(link_id: int, kind: str) -> str:
    return f"{QUEUED_PREFIX}{link_id}:{kind}"


def _ordered_pairs(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    unique = list(dict.fromkeys((str(k), str(c)) for k, c in pairs))
    return [p for p in unique if p[0] == "seo"] + [p for p in unique if p[0] == "geo"]


def enqueue(
    db: Session,
    link: PublishLink,
    pairs: Sequence[tuple[str, str]],
    check_type: str,
    triggered_by: int | None,
    *,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """预扣日上限 → 去重标记 → 入队，返回 ``(queued, reason)``（``reason`` ∈ ``daily_limit`` / ``already_queued``）。

    ``scheduled`` 入队后把 ``next_index_check_at`` 推后 1h（只改对象，由调用方提交）；``manual`` 不触碰排程。
    已有部分 kind 在队列时只入队其余 kind 的引擎，并退回其预扣。Redis 异常向上抛出（已回滚标记与预扣）。"""
    ordered = _ordered_pairs(pairs)
    if not ordered:
        return False, None
    cfg = cfg if cfg is not None else index_check_config(db)
    key = limit_key(db)
    if not _reserve(key, len(ordered), int(cfg.get("daily_limit") or 0)):
        return False, "daily_limit"
    acquired: list[str] = []
    try:
        for kind in dict.fromkeys(k for k, _ in ordered):
            if redis_client.set(queued_key(link.id, kind), "1", nx=True, ex=QUEUED_TTL_SECONDS):
                acquired.append(kind)
    except redis.RedisError:
        _release_markers(link.id, acquired)
        _refund(key, len(ordered))
        raise
    kept = [p for p in ordered if p[0] in acquired]
    _refund(key, len(ordered) - len(kept))
    if not kept:
        return False, "already_queued"
    payload = json.dumps(
        {"link_id": link.id, "kinds": acquired, "engines": [c for _, c in kept], "check_type": check_type,
         "triggered_by": triggered_by},
        separators=(",", ":"),
    )
    try:
        if check_type == "manual":
            redis_client.lpush(QUEUE_INDEX_CHECKS, payload)
        else:
            redis_client.rpush(QUEUE_INDEX_CHECKS, payload)
    except redis.RedisError:
        _release_markers(link.id, acquired)
        _refund(key, len(kept))
        raise
    if check_type != "manual":
        link.next_index_check_at = utcnow() + ENQUEUE_DEFER
    return True, None


def _release_markers(link_id: int, kinds: Iterable[str]) -> None:
    keys = [queued_key(link_id, k) for k in kinds]
    if not keys:
        return
    try:
        redis_client.delete(*keys)
    except redis.RedisError as exc:  # 标记 1h 自过期
        logger.warning("删除 queued:index_check 标记失败 link_id=%s: %s", link_id, exc)


release_markers = _release_markers


def due_pairs(link: PublishLink, engines: Sequence[tuple[str, str]], *, now: datetime, cfg: Mapping[str, Any]) -> list[tuple[str, str]]:
    """已到期的引擎（``due(e) <= now``；``check_count`` 达上限的引擎不再到期）。"""
    states = link_service.engine_states(link)
    result: list[tuple[str, str]] = []
    for kind, code in engines:
        when = link_service.due((states.get(kind) or {}).get(code), link, now=now, cfg=cfg)
        if when is not None and when <= now:
            result.append((kind, code))
    return result


# =====================================================================
# 执行（§9）
# =====================================================================


def _resolve_payload_pairs(db: Session, kinds: Sequence[str], engines: Sequence[str]) -> list[tuple[str, str]]:
    """payload ``engines`` → ``(kind, engine)``：按入队时的引擎执行（入队后被停用的引擎照常执行；已从配置删除的 GEO 引擎跳过）。"""
    seo_codes = set(seo_engine_codes(db)) | set(_seo_engines_cfg(db))
    geo_codes = set(_geo_engines_cfg(db))
    pairs: list[tuple[str, str]] = []
    for code in engines:
        if "seo" in kinds and code in seo_codes:
            pairs.append(("seo", code))
        if "geo" in kinds and code in geo_codes:
            pairs.append(("geo", code))
    return _ordered_pairs(pairs)


def _content_context(db: Session, link: PublishLink) -> dict[str, Any]:
    content = db.get(Content, link.content_id)
    keyword = None
    if content is not None and content.keyword_id:
        row = db.get(Keyword, content.keyword_id)
        keyword = row.keyword if row is not None else None
    platform = db.get(PublishPlatform, link.platform_id)
    return {
        "title": (content.title if content is not None and content.title else link.title_snapshot) or "",
        "keyword": keyword,
        "language": content.language if content is not None else None,
        "platform_code": platform.code if platform is not None else "",
    }


def _context(link: PublishLink, kind: str, engine: str, info: Mapping[str, Any], *, query_by: list[str], check_type: str,
             triggered_by: int | None, engine_cfg: Mapping[str, Any], provider_cfg: Mapping[str, Any]) -> CheckContext:
    if kind == "seo":
        name = SEO_ENGINE_NAMES.get(engine, engine)
    else:
        name = str(engine_cfg.get("name") or engine)
    return CheckContext(
        link=link, kind=kind, engine=engine, engine_name=name, url=link.url, normalized_url=link.normalized_url,
        domain=link.domain, title=str(info.get("title") or ""), keyword=info.get("keyword"),
        platform_code=str(info.get("platform_code") or ""), query_by=list(query_by), check_type=check_type,
        triggered_by=triggered_by, project_id=link.project_id, language=info.get("language"),
        engine_config=dict(engine_cfg), provider_config=dict(provider_cfg),
    )


def _create_root(db: Session, link: PublishLink, kind: str, engine: str, *, check_type: str, triggered_by: int | None,
                 query_by: list[str], model: str | None) -> AiTask:
    capability = "seo_check" if kind == "seo" else "geo_check"
    manual = check_type == "manual"
    root = gateway.create_root_task(
        db, capability=capability, operation=capability, project_id=link.project_id,
        created_by=triggered_by if manual else None, trigger_type="user" if manual else "worker",
        target_type="publish_link", target_id=link.id, model=model or None,
        input={"engine": engine, "kind": kind, "query_by": list(query_by)}, sync=True,
    )
    db.commit()
    return root


def _reserve_quota(db: Session, root: AiTask, *, capability: str, model: str | None, protocol: str | None) -> None:
    """本地额度预占（§8.6）：``estimate_for(候选首模型, 300, max_tokens)``；4291 / 路由不可用以 ``BusinessError`` 抛出。"""
    route = gateway.resolve_route(db, Capability(capability), root.project_id, model_override=model or None,
                                  protocol_override=Protocol(protocol) if protocol else None)
    completion = int(route.params.get("max_tokens") or QUOTA_COMPLETION_TOKENS)
    estimated = gateway.estimate_for(db, route.candidates[0], QUOTA_PROMPT_TOKENS, completion)
    try:
        gateway.check_quota(db, project_id=root.project_id, estimated_quota=estimated, task=root)
    except redis.RedisError as exc:
        logger.warning("收录检测额度预占失败（Redis 不可用，跳过预占）task_id=%s: %s", root.id, exc)
        return
    db.commit()


def _last_attempt(db: Session, root_id: int) -> AiTask | None:
    return db.scalars(select(AiTask).where(AiTask.root_task_id == root_id).order_by(AiTask.id.desc()).limit(1)).first()


def _check_engine(
    db: Session,
    link: PublishLink,
    kind: str,
    engine: str,
    info: Mapping[str, Any],
    *,
    check_type: str,
    triggered_by: int | None,
    query_by: list[str],
    seo_engines: Mapping[str, Mapping[str, Any]],
    seo_providers: Mapping[str, Mapping[str, Any]],
    geo_engines: Mapping[str, Mapping[str, Any]],
) -> tuple[CheckResult, int | None]:
    """单引擎检测，返回 ``(CheckResult, 根任务 ID | None)``；任何异常都转为 ``unknown`` + ``error_category``。"""
    root: AiTask | None = None
    if kind == "seo":
        engine_cfg = dict(seo_engines.get(engine) or {"provider": ZHIQI_SEO_PROVIDER})
        provider_code = str(engine_cfg.get("provider") or ZHIQI_SEO_PROVIDER)
        provider_cfg = dict(seo_providers.get(provider_code) or {})
        ctx = _context(link, kind, engine, info, query_by=query_by, check_type=check_type, triggered_by=triggered_by,
                       engine_cfg=engine_cfg, provider_cfg=provider_cfg)
        try:
            provider = get_seo_provider(provider_code)
        except KeyError:
            return CheckResult(status="unknown", provider=provider_code[:32], error_category="unknown",
                               error_message=f"未知提供器 {provider_code}"), None
        model = str(engine_cfg.get("model") or "").strip() or None
        protocol = provider_cfg.get("protocol") if provider_code == ZHIQI_SEO_PROVIDER else None
        uses_gateway = provider_code == ZHIQI_SEO_PROVIDER
    else:
        engine_cfg = dict(geo_engines.get(engine) or {})
        ctx = _context(link, kind, engine, info, query_by=query_by, check_type=check_type, triggered_by=triggered_by,
                       engine_cfg=engine_cfg, provider_cfg={})
        geo = get_geo_engine(engine)
        provider_code = "zhiqi_model"
        try:
            pre = geo.precheck(db, link, engine_cfg, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            pre = failure_result(provider_code, exc)
        if pre is not None:
            return pre, None                                  # title_in_prompt：不建根任务、不调用
        model = str(engine_cfg.get("model") or "").strip() or None
        protocol = engine_cfg.get("protocol") or None
        uses_gateway = True

    started = time.monotonic()
    try:
        if uses_gateway:
            root = _create_root(db, link, kind, engine, check_type=check_type, triggered_by=triggered_by,
                                query_by=query_by, model=model)
            ctx.root_task = root
            _reserve_quota(db, root, capability="seo_check" if kind == "seo" else "geo_check", model=model, protocol=protocol)
        if kind == "seo":
            result = provider.check(db, link, engine, list(query_by), ctx=ctx)
        else:
            result = geo.check(db, link, engine_cfg, ctx=ctx)
    except Exception as exc:  # noqa: BLE001 - 单引擎异常只影响该引擎
        db.rollback()
        category, _message = classify_exception(exc)
        if category == "unknown":
            logger.warning("收录检测引擎异常 link_id=%s %s/%s", link.id, kind, engine, exc_info=True)
        result = failure_result(provider_code, exc, duration_ms=int((time.monotonic() - started) * 1000))
    if root is not None and result.ai_task_id is None:
        attempt = _last_attempt(db, root.id)
        if attempt is not None:
            result = replace(result, ai_task_id=attempt.id, model=result.model or attempt.model,
                             request_id=result.request_id or attempt.request_id)
    if root is not None and not result.model:
        result = replace(result, model=root.model or None)
    return result, (root.id if root is not None else None)


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def _dumps(value: Any) -> str | None:
    if value is None or value == {}:
        return None
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _confidence(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return max(Decimal(0), min(Decimal(1), number)).quantize(Decimal("0.001"))


def _status_json(link: PublishLink, kind: str) -> dict[str, Any]:
    raw = link.seo_status_json if kind == "seo" else link.geo_status_json
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def apply_result(
    db: Session,
    link_id: int,
    kind: str,
    engine: str,
    result: CheckResult,
    *,
    check_type: str,
    triggered_by: int | None,
    root_id: int | None = None,
    now: datetime | None = None,
) -> IndexCheck | None:
    """单引擎写回（独立事务，docs/03「检测写回」第 3、4 条）：``finalize_root`` → ``SELECT publish_links … FOR UPDATE`` →
    ``INSERT index_checks`` → 投影 → 提交 → ``stats:rt``。链接已不存在返回 ``None``。"""
    now = now or utcnow()
    positive, negative = POSITIVE_STATUS[kind], NEGATIVE_STATUS[kind]
    status = result.status if result.status in (positive, negative) else "unknown"
    try:
        if root_id is not None:
            root = db.get(AiTask, root_id)
            if root is not None and root.status == "running":
                gateway.finalize_root(
                    db, root, "failed" if result.error_category else "succeeded",
                    error_category=result.error_category, error_message=result.error_message,
                )
        link = db.scalars(
            select(PublishLink).where(PublishLink.id == link_id).with_for_update().execution_options(populate_existing=True)
        ).first()
        if link is None:
            db.commit()
            return None
        states = _status_json(link, kind)
        state = dict(states.get(engine) or {}) if isinstance(states.get(engine), Mapping) else {}
        previous = str(state.get("status") or "unknown")
        evidence = dict(result.evidence or {})
        if previous == positive and status == negative:
            evidence["dropped"] = True
        record = IndexCheck(
            link_id=link.id,
            kind=kind,
            engine=engine[:32],
            provider=(result.provider or "")[:32],
            check_type=check_type,
            result_status=status,
            previous_status=previous if previous in (positive, negative, "unknown") else "unknown",
            match_mode=(result.match_mode or "none")[:16],
            query_text=_truncate(result.query_text, MAX_QUERY_TEXT),
            ai_task_id=result.ai_task_id,
            request_id=_truncate(result.request_id, MAX_REQUEST_ID),
            model=_truncate(result.model, MAX_MODEL),
            evidence_title=_truncate(result.evidence_title, MAX_EVIDENCE_TITLE),
            evidence_snippet=_truncate(result.evidence_snippet, MAX_EVIDENCE_SNIPPET),
            evidence_url=_truncate(result.evidence_url, MAX_EVIDENCE_URL),
            evidence_json=_dumps(evidence),
            confidence=_confidence(result.confidence),
            duration_ms=max(0, int(result.duration_ms or 0)),
            error_category=_truncate(result.error_category, 32),
            error_message=_truncate(result.error_message, MAX_ERROR_MESSAGE),
            checked_at=now,
            triggered_by=triggered_by,
        )
        db.add(record)

        # 投影（§7.4）：unknown 只更新 checked_at（与 scheduled 计数），保留原 status；从未成功检测过才写 unknown
        first_key = "first_indexed_at" if kind == "seo" else "first_cited_at"
        if status != "unknown":
            state["status"] = status
        elif not state.get("status"):
            state["status"] = "unknown"
        state["checked_at"] = iso_utc(now)
        state.setdefault(first_key, None)
        if status == positive and not state.get(first_key):
            state[first_key] = iso_utc(now)
        count = int(state.get("check_count") or 0)
        state["check_count"] = count + 1 if check_type == "scheduled" else count
        states[engine] = state
        encoded = json.dumps(states, ensure_ascii=False, separators=(",", ":"))
        any_hit = any(isinstance(s, Mapping) and s.get("status") == positive for s in states.values())
        fields: dict[str, int] = {}
        if kind == "seo":
            link.seo_status_json = encoded
            link.seo_indexed_any = any_hit
            fields["seo_checks"] = 1
            if status == positive and link.first_indexed_at is None:
                link.first_indexed_at = now
                fields["seo_newly_indexed"] = 1
                created = link.created_at or now
                if created - link.published_at <= timedelta(hours=stats_service.MAX_BACKFILL_DELAY_HOURS):
                    fields["index_hours_sum"] = max(0, int((now - link.published_at).total_seconds() // 3600))
                    fields["index_hours_links"] = 1
        else:
            link.geo_status_json = encoded
            link.geo_cited_any = any_hit
            fields["geo_checks"] = 1
            if status == positive and link.first_cited_at is None:
                link.first_cited_at = now
                fields["geo_newly_cited"] = 1
        link.last_index_checked_at = now
        db.flush()
        stats_service.increment_realtime_after_commit(db, link.project_id, fields, at=now)
        db.commit()
        return record
    except Exception:
        db.rollback()
        raise


def _extend(lock_key: str | None, lock_token: str | None) -> None:
    if not lock_key:
        return
    try:
        extend_lock(lock_key, LOCK_TTL_SECONDS, lock_token)
    except redis.RedisError as exc:
        logger.warning("续期 %s 失败: %s", lock_key, exc)


def _finish(db: Session, link_id: int, *, check_type: str, completed: int, interrupted: bool, quota_hit: bool) -> None:
    """全部引擎完成后的排程事务：``scheduled`` 推进轮次并重算 ``next_index_check_at``；暂停中断 → ``now + pause_seconds``。"""
    now = utcnow()
    try:
        link = db.scalars(
            select(PublishLink).where(PublishLink.id == link_id).with_for_update().execution_options(populate_existing=True)
        ).first()
        if link is None:
            db.commit()
            return
        active = bool(link.is_monitoring) and link.alive_status != "deleted"
        if interrupted:
            if active:
                link.next_index_check_at = now + timedelta(seconds=_pause_seconds(db))
        elif check_type == "scheduled":
            if completed > 0:
                link.index_check_count = int(link.index_check_count or 0) + 1
                link.index_checks_done = int(link.index_checks_done or 0) + 1
            nxt = link_service.compute_next_index_check_at(link, now=now, db=db)
            if nxt is not None and quota_hit:
                nxt = max(nxt, next_day_start(db))
            link.next_index_check_at = nxt
        db.commit()
    except Exception:
        db.rollback()
        raise


def run(
    db: Session,
    scope: DataScope,
    link: PublishLink | None,
    kinds: Sequence[str],
    engines: Sequence[str],
    check_type: str,
    triggered_by: int | None,
    *,
    lock_key: str | None = None,
    lock_token: str | None = None,
) -> list[IndexCheck]:
    """执行一个队列元素（§9 第 2~5 步；锁由调用方持有，``lock_key`` / ``lock_token`` 用于每引擎完成后续期）。"""
    if link is None or not is_visible(db, scope, link):
        return []
    link_id = link.id
    if link.alive_status == "deleted" or (check_type != "manual" and not link.is_monitoring):
        return []
    if gateway.check_paused():
        _finish(db, link_id, check_type=check_type, completed=0, interrupted=True, quota_hit=False)
        return []
    cfg = index_check_config(db)
    query_by = [str(q) for q in cfg.get("query_by") or ["url", "title"]]
    seo_engines, seo_providers, geo_engines = _seo_engines_cfg(db), _seo_providers_cfg(db), _geo_engines_cfg(db)
    pairs = _resolve_payload_pairs(db, list(kinds), list(engines))
    info = _content_context(db, link)
    records: list[IndexCheck] = []
    interrupted = quota_hit = False
    for kind, engine in pairs:
        current = db.get(PublishLink, link_id)
        if current is None:
            break
        provider = (seo_engines.get(engine) or {}).get("provider", ZHIQI_SEO_PROVIDER) if kind == "seo" else "zhiqi_model"
        if provider in (ZHIQI_SEO_PROVIDER, "zhiqi_model") and gateway.check_paused():
            interrupted = True
            break
        try:
            result, root_id = _check_engine(
                db, current, kind, engine, info, check_type=check_type, triggered_by=triggered_by, query_by=query_by,
                seo_engines=seo_engines, seo_providers=seo_providers, geo_engines=geo_engines,
            )
            record = apply_result(db, link_id, kind, engine, result, check_type=check_type, triggered_by=triggered_by,
                                  root_id=root_id)
        except Exception:  # noqa: BLE001 - 写回失败只影响该引擎
            db.rollback()
            logger.exception("收录检测写回失败 link_id=%s %s/%s", link_id, kind, engine)
            continue
        finally:
            _extend(lock_key, lock_token)
        if record is not None:
            records.append(record)
        if result.error_category == "quota_exceeded" and result.error_message == MSG_LOCAL_QUOTA_LIMIT:
            quota_hit = True
    _finish(db, link_id, check_type=check_type, completed=len(records), interrupted=interrupted, quota_hit=quota_hit)
    return records


# =====================================================================
# 人工标记（POST /admin/links/{id}/mark-index）
# =====================================================================


def _known_engines(db: Session, kind: str) -> set[str]:
    if kind == "seo":
        return set(seo_engine_codes(db)) | set(_seo_engines_cfg(db))
    return set(_geo_engines_cfg(db))


def mark_index(db: Session, scope: DataScope, link_id: int, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """人工标记：``status`` 须与 ``kind`` 对应（否则 400 ``loc=["body","status"]``）；写 ``index_checks(provider=manual,
    check_type=manual, match_mode=manual, confidence=1)`` 并按 §7.4 回写；不改 ``index_check_count`` / ``index_checks_done`` /
    ``next_index_check_at``。返回更新后的链接对象。"""
    link = link_service.get_link_row(db, scope, link_id)
    kind = str(values.get("kind") or "")
    status = str(values.get("status") or "")
    engine = str(values.get("engine") or "").strip()
    errors: list[dict[str, Any]] = []
    if status not in MANUAL_STATUSES.get(kind, ()):
        errors.append(field_error(["body", "status"], MSG_STATUS_MISMATCH, "value_error", values.get("status")))
    if kind in MANUAL_STATUSES and engine not in _known_engines(db, kind):
        errors.append(field_error(["body", "engine"], MSG_ENGINE_NOT_FOUND, "value_error", values.get("engine")))
    note = values.get("note")
    note = str(note).strip()[:MAX_NOTE] if isinstance(note, str) and note.strip() else None
    evidence_url = values.get("evidence_url")
    evidence_url = str(evidence_url).strip() if isinstance(evidence_url, str) and evidence_url.strip() else None
    if evidence_url and not evidence_url.lower().startswith(("http://", "https://")):
        errors.append(field_error(["body", "evidence_url"], MSG_EVIDENCE_URL, "value_error", values.get("evidence_url")))
    if errors:
        raise invalid_params(errors)
    result = ManualProvider.result(status, note=note, evidence_url=evidence_url)
    apply_result(db, link.id, kind, engine, result, check_type="manual", triggered_by=admin_id)
    db.refresh(link)
    overdue_days = index_check_config(db).get("overdue_days")
    return link_service.link_item(link, overdue_days=overdue_days)


# =====================================================================
# 手动 / 批量入队（POST /admin/links/{id}/index-check、/admin/monitoring/index-checks/run）
# =====================================================================


def resolve_requested(
    db: Session, kinds: Sequence[str], engines: Sequence[str] | None, *, loc: Sequence[str | int] = ("body",)
) -> list[tuple[str, str]]:
    """``engines`` 缺省 → 所选 ``kinds`` 下全部启用（非人工）引擎；给出时逐个校验：未启用或不属于所选 kind → 400
    ``loc=[…,"engines",i]``；提供器为 ``manual`` 的 SEO 引擎 → 400（只能人工标记）。结果为空 → 400 ``loc=[…,"kinds"]``。"""
    wanted_kinds = [k for k in KINDS if k in set(kinds or ())]
    auto = [p for p in schedulable_engines(db) if p[0] in wanted_kinds]
    if engines is None:
        if not auto:
            raise invalid_params(field_error([*loc, "kinds"], MSG_NO_ENABLED_ENGINE, "no_enabled_engine", list(kinds or [])))
        return auto
    enabled = {kind: set(_enabled(db, kind)) for kind in wanted_kinds}
    seo_cfg = _seo_engines_cfg(db)
    pairs: list[tuple[str, str]] = []
    errors: list[dict[str, Any]] = []
    for i, code in enumerate(engines):
        matched = [(kind, code) for kind in wanted_kinds if code in enabled[kind]]
        if not matched:
            errors.append(field_error([*loc, "engines", i], MSG_ENGINE_DISABLED, "engine_disabled", code))
            continue
        for kind, _ in matched:
            if kind == "seo" and (seo_cfg.get(code) or {}).get("provider") == "manual":
                errors.append(field_error([*loc, "engines", i], MSG_ENGINE_MANUAL, "manual_engine", code))
                continue
            pairs.append((kind, code))
    if errors:
        raise invalid_params(errors)
    pairs = _ordered_pairs(pairs)
    if not pairs:
        raise invalid_params(field_error([*loc, "engines"], MSG_NO_ENABLED_ENGINE, "no_enabled_engine", list(engines)))
    return pairs


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def request_index_check(
    db: Session, scope: DataScope, link_id: int, kinds: Sequence[str], engines: Sequence[str] | None, *, admin_id: int
) -> dict[str, Any]:
    """``POST /admin/links/{id}/index-check`` → ``{queued:true}`` / ``{queued:false, reason:"already_queued"|"daily_limit"}``。"""
    link = link_service.get_link_row(db, scope, link_id)
    pairs = resolve_requested(db, kinds, engines)
    if link.alive_status == "deleted":
        raise _conflict(MSG_LINK_DELETED, {"current_status": "deleted"})
    if not link.is_monitoring:
        raise _conflict(MSG_MONITORING_PAUSED, {"current_status": link.alive_status, "reason": "monitoring_paused"})
    check_rate_limit(f"{MANUAL_RATE_PREFIX}{admin_id}", MANUAL_RATE)
    queued, reason = enqueue(db, link, pairs, "manual", admin_id)
    return {"queued": True} if queued else {"queued": False, "reason": reason or "already_queued"}


def run_index_checks_batch(
    db: Session,
    scope: DataScope,
    *,
    admin_id: int | None,
    kinds: Sequence[str],
    engines: Sequence[str] | None = None,
    project_id: int | None = None,
    platform_id: int | None = None,
    link_ids: Sequence[int] | None = None,
    only_due: bool = True,
    limit: int = 1000,
) -> dict[str, int]:
    """批量入队 ``manual`` 收录检测 → ``{enqueued, skipped}``：不可见的 ``link_ids`` 计入 ``skipped``；``project_id`` 不可见时
    ``{0, 0}``；未给 ``project_id`` / ``link_ids`` 时 ``scope.restricted`` 只对负责项目下的链接入队（``scope_by_project``）；
    ``only_due`` 只处理 ``next_index_check_at <= now``；已删除 / 暂停监控 / 已在队列 / 超日上限计入 ``skipped``。"""
    pairs = resolve_requested(db, kinds, engines)
    stmt = scope_by_project(select(PublishLink), PublishLink.project_id, scope)
    if project_id is not None:
        project = db.get(Project, project_id) if project_id < 2**63 else None
        if project is None or not is_visible(db, scope, project):
            return {"enqueued": 0, "skipped": 0}
        stmt = stmt.where(PublishLink.project_id == project_id)
    if platform_id is not None:
        stmt = stmt.where(PublishLink.platform_id == platform_id)
    wanted = list(dict.fromkeys(int(i) for i in link_ids)) if link_ids else []
    if wanted:
        stmt = stmt.where(PublishLink.id.in_(wanted))
    if only_due:
        stmt = stmt.where(PublishLink.next_index_check_at <= utcnow())
    rows = list(db.scalars(stmt.order_by(PublishLink.next_index_check_at, PublishLink.id).limit(max(1, int(limit)))).all())
    skipped = 0
    if wanted:
        found = {r.id for r in rows}
        skipped += sum(1 for i in wanted if i not in found)
    cfg = index_check_config(db)
    enqueued = 0
    for link in rows:
        if link.alive_status == "deleted" or not link.is_monitoring:
            skipped += 1
            continue
        queued, _reason = enqueue(db, link, pairs, "manual", admin_id, cfg=cfg)
        if queued:
            enqueued += 1
        else:
            skipped += 1
    return {"enqueued": enqueued, "skipped": skipped}


# =====================================================================
# 设置保存后的排程重算（§7.6、§8.1）
# =====================================================================


def recompute_null_schedules(db: Session, *, batch_size: int = 500) -> int:
    """对 ``is_monitoring=1 AND alive_status != 'deleted' AND next_index_check_at IS NULL`` 的链接按
    ``compute_next_index_check_at`` 重算（覆盖因无启用引擎而被置 NULL 的链接），返回得到非空排程的链接数。"""
    cfg = index_check_config(db)
    engines = schedulable_engines(db)
    if not engines:
        return 0
    now = utcnow()
    updated = 0
    last_id = 0
    while True:
        rows = list(db.scalars(
            select(PublishLink)
            .where(PublishLink.is_monitoring == True, PublishLink.alive_status != "deleted",  # noqa: E712
                   PublishLink.next_index_check_at.is_(None), PublishLink.id > last_id)
            .order_by(PublishLink.id).limit(batch_size)
        ).all())
        if not rows:
            break
        for link in rows:
            nxt = link_service.compute_next_index_check_at(link, now=now, cfg=cfg, engines=engines)
            if nxt is not None:
                link.next_index_check_at = nxt
                updated += 1
        last_id = rows[-1].id
        db.commit()
    return updated


__all__ = [
    "LOCK_PREFIX",
    "LOCK_TTL_SECONDS",
    "QUEUE_INDEX_CHECKS",
    "apply_result",
    "due_pairs",
    "enabled_engines",
    "enqueue",
    "index_check_config",
    "limit_key",
    "mark_index",
    "next_day_start",
    "queued_key",
    "recompute_null_schedules",
    "release_markers",
    "request_index_check",
    "resolve_requested",
    "run",
    "run_index_checks_batch",
    "schedulable_engines",
    "seo_engine_codes",
    "used_today",
]
