"""用量对账（docs/08-zhiqiapi-integration.md §10.3~§10.7；docs/03「用量对账回填」；docs/13 §4.2、§4.3）。

``reconcile(db)``：持 ``lock:worker:reconcile`` 拉取 ``GET /api/log/token``（最近 1000 条，新在前）→ 逐条
``entry_hash = SHA-256(按键排序、无空白的规范化 raw_json)`` 幂等入库 ``ai_usage_logs`` → 本轮新插入条目按
``request_id ↔ ai_tasks.request_id``（近 7 天尝试行）、失败则 ``task_id ↔ upstream_task_id`` 匹配（近 7 天入库的历史未匹配条目
一并重试，见 ``_reconcile_locked`` ②）→ 回填
``quota_actual = max(0, Σ type2 − Σ|type6|)`` / ``reconciled_at`` / ``usage_log_type`` / ``cost_cny`` 并重算根任务合计列 →
涉及的历史日期重聚合 → 清理过期未匹配条目 → ``SET ai:usage:last_pull``。对账不回写 ``quota:daily`` / ``stats:rt``。
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    CODE_BAD_REQUEST,
    CODE_CONFLICT,
    CODE_UPSTREAM_ERROR,
    BusinessError,
    field_error,
)
from app.core.locks import acquire_lock, release_lock
from app.core.redis import cache_get_json, cache_set_json
from app.core.zhiqi import get_client, usage
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import UsageLogEntry
from app.models import AiTask, AiUsageLog, utcnow
from app.schemas.common import iso_utc, to_utc_naive
from app.services import settings_service, stats_service
from app.services.ai_gateway_service import get_retry_policy, quota_to_cny
from app.services.data_scope_service import (
    DataScope,
    scope_by_project,
    scope_usage_logs,
)

logger = logging.getLogger(__name__)

RECONCILE_LOCK_KEY = "lock:worker:reconcile"
RECONCILE_LOCK_TTL = 300
LAST_PULL_KEY = "ai:usage:last_pull"
LAST_PULL_TTL = 86400
UPSTREAM_WINDOW = 1000
MATCH_WINDOW_DAYS = 7
RETRY_UNMATCHED_LIMIT = 5000      # 每轮重试的历史未匹配条目上限（最近入库者优先）
LOG_TYPE_CONSUME = 2
LOG_TYPE_FAILED = 5
LOG_TYPE_REFUND = 6
SUMMARY_GROUPS = ("model", "capability", "project", "day")
SUMMARY_DEFAULT_DAYS = 30
SUMMARY_MAX_DAYS = 366


# =====================================================================
# 工具
# =====================================================================


def entry_hash(raw: Any) -> str:
    """``SHA-256(按键排序、无空白的规范化 raw_json)``。"""
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decimal_ratio(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    except (ArithmeticError, ValueError):
        return None


def _routing_config(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "ai_routing_config")


def _log_row(entry: UsageLogEntry, digest: str, pulled_at: datetime) -> AiUsageLog:
    return AiUsageLog(
        entry_hash=digest,
        upstream_log_id=entry.upstream_log_id,
        request_id=(entry.request_id or None) and entry.request_id[:64],
        log_type=int(entry.log_type or 0),
        model_name=(entry.model_name or None) and entry.model_name[:120],
        group_name=(entry.group or None) and entry.group[:50],
        quota=abs(int(entry.quota or 0)),
        prompt_tokens=max(0, int(entry.prompt_tokens or 0)),
        completion_tokens=max(0, int(entry.completion_tokens or 0)),
        cache_tokens=max(0, int(entry.cache_tokens or 0)),
        group_ratio=_decimal_ratio(entry.group_ratio),
        model_ratio=_decimal_ratio(entry.model_ratio),
        completion_ratio=_decimal_ratio(entry.completion_ratio),
        request_path=(entry.request_path or None) and entry.request_path[:255],
        upstream_task_id=(entry.task_id or None) and entry.task_id[:80],
        upstream_created_at=entry.created_at,
        pulled_at=pulled_at,
        raw_json=json.dumps(entry.raw, ensure_ascii=False, separators=(",", ":"), default=str),
    )


def _quota_actual(logs: Iterable[AiUsageLog]) -> int:
    consumed = sum(int(log.quota or 0) for log in logs if log.log_type == LOG_TYPE_CONSUME)
    refunded = sum(abs(int(log.quota or 0)) for log in logs if log.log_type == LOG_TYPE_REFUND)
    return max(0, consumed - refunded)


def _latest(logs: list[tuple[int, AiUsageLog]]) -> AiUsageLog:
    """最近命中的条目：上游时间最大者；时间缺失时取拉取列表中最靠前（新在前）的条目。"""
    return min(
        logs,
        key=lambda item: (-(item[1].upstream_created_at.timestamp() if item[1].upstream_created_at else float("-inf")), item[0]),
    )[1]


def next_interval_seconds(db: Session) -> int:
    """下一次自动对账的间隔：上次拉取 ``window_overflow=true`` 时临时降到 ``usage.min_interval_seconds``，否则
    ``usage.reconcile_interval_seconds``（0 表示关闭）。"""
    usage_cfg = _routing_config(db).get("usage") or {}
    interval = int(usage_cfg.get("reconcile_interval_seconds") or 0)
    if interval <= 0:
        return 0
    last = cache_get_json(LAST_PULL_KEY)
    if isinstance(last, dict) and last.get("window_overflow"):
        return min(interval, max(1, int(usage_cfg.get("min_interval_seconds") or 60)))
    return interval


def _trigger_aggregates(stat_dates: set[date], today: date) -> None:
    """今日 / 昨日交常规聚合；其它日期直接 ``aggregate_daily_stats.aggregate(stat_date)``（第 6 步提供，缺失时跳过）。"""
    targets = sorted(d for d in stat_dates if d < today - timedelta(days=1))
    if not targets:
        return
    if importlib.util.find_spec("app.tasks.aggregate_daily_stats") is None:
        logger.info("aggregate_daily_stats 尚未提供，跳过对账涉及日期的重聚合：%s", [d.isoformat() for d in targets])
        return
    module = importlib.import_module("app.tasks.aggregate_daily_stats")
    aggregate = getattr(module, "aggregate", None)
    if not callable(aggregate):
        return
    for stat_date in targets:
        try:
            aggregate(stat_date)
        except Exception:  # noqa: BLE001
            logger.warning("对账后重聚合 %s 失败", stat_date, exc_info=True)


# =====================================================================
# 对账
# =====================================================================


def reconcile(db: Session) -> dict[str, Any]:
    """立即对账（周期任务与 ``POST /admin/ai/usage/reconcile`` 共用，持 ``lock:worker:reconcile``）。

    返回 ``{pulled, new, matched, unmatched, window_overflow, request_ids[]}``；锁占用 → 409；拉取失败 → 5021（不写库）。
    """
    token = acquire_lock(RECONCILE_LOCK_KEY, RECONCILE_LOCK_TTL)
    if not token:
        raise BusinessError("用量对账正在进行，请稍后再试", code=CODE_CONFLICT, http_status=409, data=None)
    try:
        return _reconcile_locked(db)
    finally:
        release_lock(RECONCILE_LOCK_KEY, token)


def _reconcile_locked(db: Session) -> dict[str, Any]:
    cfg = _routing_config(db)
    try:
        # 幂等 GET：按 ai_routing_config.retry 全量重试（docs/08 §4.2、§8.2）
        entries, request_id = usage.fetch_token_logs(get_client(), retry=get_retry_policy(cfg))
    except ZhiqiError as err:
        logger.warning("拉取用量日志失败 category=%s request_id=%s", err.category.value, err.request_id or "-")
        raise BusinessError(
            "上游服务调用失败",
            code=CODE_UPSTREAM_ERROR,
            data={"error_category": err.category.value, "request_id": err.request_id, "model": None, "hint": None},
        ) from err

    usage_cfg = cfg.get("usage") or {}
    pricing_group_ratio = float((cfg.get("pricing") or {}).get("group_ratio") or 1.0)
    now = utcnow()
    tz = stats_service.get_tz(db)

    # ---- ① entry_hash 幂等入库（INSERT IGNORE 语义：已存在的哈希跳过）
    digests = [entry_hash(e.raw) for e in entries]
    unique: dict[str, tuple[int, UsageLogEntry]] = {}
    for position, (digest, entry) in enumerate(zip(digests, entries, strict=True)):
        unique.setdefault(digest, (position, entry))
    existing: set[str] = set()
    hash_list = list(unique)
    for start in range(0, len(hash_list), 500):
        chunk = hash_list[start:start + 500]
        existing.update(db.scalars(select(AiUsageLog.entry_hash).where(AiUsageLog.entry_hash.in_(chunk))).all())
    prev_newest = db.scalar(select(func.max(AiUsageLog.upstream_created_at)))
    new_rows: list[tuple[int, AiUsageLog]] = []
    for digest, (position, entry) in unique.items():
        if digest in existing:
            continue
        row = _log_row(entry, digest, now)
        db.add(row)
        new_rows.append((position, row))
    db.flush()

    group_ratio_mismatch = {
        float(row.group_ratio) for _, row in new_rows if row.group_ratio is not None and abs(float(row.group_ratio) - pricing_group_ratio) > 1e-9
    }
    if group_ratio_mismatch:
        logger.warning(
            "用量日志 group_ratio=%s 与 ai_routing_config.pricing.group_ratio=%s 不一致，请修正配置",
            sorted(group_ratio_mismatch), pricing_group_ratio,
        )

    # ---- ② 匹配（近 7 天尝试行：request_id → upstream_task_id）
    since = now - timedelta(days=MATCH_WINDOW_DAYS)
    # 先前轮次入库但未匹配的条目（近 7 天、可匹配键非空）与本轮新条目一起重试：上游日志可能先于本地尝试行提交出现
    # （worker 收到响应到写库之间被周期对账拉到），只匹配新条目会让该行永久未对账。未匹配条目从未计入任何尝试行，
    # 重试不会重复累加；已对账行仍只接受新的 type=6 退款条目（下方规则不变）。
    positions = {digest: position for digest, (position, _) in unique.items()}
    new_log_ids = {row.id for _, row in new_rows}
    previous_unmatched = db.scalars(
        select(AiUsageLog)
        .where(
            AiUsageLog.ai_task_id.is_(None),
            AiUsageLog.pulled_at >= since,
            or_(AiUsageLog.request_id.is_not(None), AiUsageLog.upstream_task_id.is_not(None)),
        )
        .order_by(AiUsageLog.id.desc())
        .limit(RETRY_UNMATCHED_LIMIT + len(new_log_ids))
    ).all()
    retry_rows: list[tuple[int, AiUsageLog]] = [
        (positions.get(row.entry_hash, len(entries) + offset), row)
        for offset, row in enumerate(r for r in previous_unmatched if r.id not in new_log_ids)
    ][:RETRY_UNMATCHED_LIMIT]
    match_rows = new_rows + retry_rows
    request_ids = {row.request_id for _, row in match_rows if row.request_id}
    task_ids = {row.upstream_task_id for _, row in match_rows if row.upstream_task_id}
    by_request: dict[str, AiTask] = {}
    by_upstream: dict[str, AiTask] = {}
    if request_ids or task_ids:
        conditions = []
        if request_ids:
            conditions.append(AiTask.request_id.in_(request_ids))
        if task_ids:
            conditions.append(AiTask.upstream_task_id.in_(task_ids))
        candidates = db.scalars(
            select(AiTask)
            .where(AiTask.root_task_id.is_not(None), AiTask.created_at >= since, or_(*conditions))
            .order_by(AiTask.id)
        ).all()
        for task in candidates:
            if task.request_id and task.request_id in request_ids:
                by_request[task.request_id] = task  # 同一 request_id 取最新的尝试行
            if task.upstream_task_id and task.upstream_task_id in task_ids:
                by_upstream.setdefault(task.upstream_task_id, task)  # 提交成功的尝试行（最早记录 upstream_task_id 者）

    grouped: dict[int, list[tuple[int, AiUsageLog]]] = defaultdict(list)
    tasks: dict[int, AiTask] = {}
    for position, row in match_rows:
        task = by_request.get(row.request_id) if row.request_id else None
        if task is None and row.upstream_task_id:
            task = by_upstream.get(row.upstream_task_id)
        if task is not None:
            grouped[task.id].append((position, row))
            tasks[task.id] = task

    matched = 0
    rematched = 0
    new_ids = {id(row) for _, row in new_rows}
    touched_roots: set[int] = set()
    stat_dates: set[date] = set()
    for task_id, logs in grouped.items():
        task = tasks[task_id]
        if task.reconciled_at is None:
            if not any(row.log_type in (LOG_TYPE_CONSUME, LOG_TYPE_FAILED) for _, row in logs):
                # 未对账的行只由 type=2 / type=5 完成首次对账；先到的退款条目先关联，待消费条目到达后一并累加
                accepted = logs
                reconcile_now = False
            else:
                accepted = logs
                reconcile_now = True
        else:
            # 已对账的行只接受新的 type=6 退款条目累加，不因重复的 type=2 条目重写
            accepted = [(p, row) for p, row in logs if row.log_type == LOG_TYPE_REFUND]
            reconcile_now = bool(accepted)
        for _, row in accepted:
            row.ai_task_id = task.id
            row.matched_at = now
            if not row.model_name:
                row.model_name = task.model[:120] if task.model else None
        fresh = sum(1 for _, row in accepted if id(row) in new_ids)
        matched += fresh
        rematched += len(accepted) - fresh
        if not reconcile_now:
            continue
        db.flush()
        linked = db.scalars(select(AiUsageLog).where(AiUsageLog.ai_task_id == task.id)).all()
        task.quota_actual = _quota_actual(linked)
        task.usage_log_type = int(_latest(accepted).log_type)
        task.reconciled_at = now
        task.cost_cny = quota_to_cny(cfg, task.quota_actual)
        if task.root_task_id:
            touched_roots.add(task.root_task_id)
        if task.finished_at is not None:
            stat_dates.add(stats_service.local_date(task.finished_at, tz=tz))
    db.flush()

    # ---- ③ 根任务合计列重算（quota_actual / cost_cny 求和）
    for root_id in touched_roots:
        root = db.get(AiTask, root_id)
        if root is None:
            continue
        attempts = db.scalars(select(AiTask).where(AiTask.root_task_id == root_id)).all()
        actuals = [int(a.quota_actual) for a in attempts if a.quota_actual is not None]
        root.quota_actual = sum(actuals) if actuals else None
        costs = [Decimal(a.cost_cny) for a in attempts if a.cost_cny is not None]
        root.cost_cny = sum(costs, Decimal(0)) if costs else None

    # ---- ④ 清理过期的未匹配条目
    retention_days = int(usage_cfg.get("unmatched_retention_days") or 30)
    cleaned = db.execute(
        delete(AiUsageLog).where(AiUsageLog.ai_task_id.is_(None), AiUsageLog.pulled_at < now - timedelta(days=retention_days))
    ).rowcount or 0
    db.commit()

    # ---- ⑤ 窗口溢出与摘要
    pulled = len(entries)
    new_count = len(new_rows)
    oldest = min((e.created_at for e in entries if e.created_at is not None), default=None)
    window_overflow = pulled >= UPSTREAM_WINDOW and (
        new_count >= pulled or (oldest is not None and prev_newest is not None and oldest > prev_newest)
    )
    if window_overflow:
        logger.warning(
            "用量对账窗口溢出：本次拉取 %s 条全部为新记录，下一次间隔降到 %ss；溢出部分永久无法对账",
            pulled, usage_cfg.get("min_interval_seconds", 60),
        )
    if new_count > int(usage_cfg.get("max_new_per_pull_warn") or 800):
        logger.warning("用量对账单次新增 %s 条，超过 usage.max_new_per_pull_warn", new_count)
    result = {
        "pulled": pulled,
        "new": new_count,
        "matched": matched,
        "unmatched": new_count - matched,
        "window_overflow": bool(window_overflow),
        "request_ids": [request_id] if request_id else [],
    }
    cache_set_json(LAST_PULL_KEY, {"pulled_at": iso_utc(now), **result}, LAST_PULL_TTL)
    logger.info("用量对账完成 %s rematched=%s cleaned_unmatched=%s", result, rematched, cleaned)
    _trigger_aggregates(stat_dates, stats_service.today_date(tz=tz))
    return result


# =====================================================================
# 查询（GET /admin/ai/usage/*）
# =====================================================================


def usage_log_item(row: AiUsageLog) -> dict[str, Any]:
    def _num(value: Decimal | None) -> float | None:
        return float(value) if value is not None else None

    return {
        "id": row.id,
        "entry_hash": row.entry_hash,
        "upstream_log_id": row.upstream_log_id,
        "request_id": row.request_id,
        "log_type": row.log_type,
        "model_name": row.model_name,
        "group_name": row.group_name,
        "quota": row.quota,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "cache_tokens": row.cache_tokens,
        "group_ratio": _num(row.group_ratio),
        "model_ratio": _num(row.model_ratio),
        "completion_ratio": _num(row.completion_ratio),
        "request_path": row.request_path,
        "upstream_task_id": row.upstream_task_id,
        "upstream_created_at": iso_utc(row.upstream_created_at),
        "ai_task_id": row.ai_task_id,
        "matched": row.ai_task_id is not None,
        "matched_at": iso_utc(row.matched_at),
        "pulled_at": iso_utc(row.pulled_at),
        "raw": _raw(row.raw_json),
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }


def _raw(raw_json: str | None) -> Any:
    """条目原文（``ai/Usage.vue`` 的「原文」抽屉）；解析失败时原样返回字符串。"""
    if not raw_json:
        return None
    try:
        return json.loads(raw_json)
    except (TypeError, ValueError):
        return raw_json


def list_usage_logs(
    db: Session,
    scope: DataScope,
    *,
    model_name: str | None = None,
    log_type: int | None = None,
    matched: bool | None = None,
    request_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """分页 ``ai_usage_logs``（按 ``pulled_at`` 倒序，``start`` / ``end`` 作用于 ``pulled_at``，左闭右开）；
    受限范围只返回匹配到可见尝试行的条目（未匹配条目只对总后台可见）。"""
    conditions: list[Any] = []
    if model_name:
        conditions.append(AiUsageLog.model_name == model_name)
    if log_type is not None:
        conditions.append(AiUsageLog.log_type == int(log_type))
    if matched is True:
        conditions.append(AiUsageLog.ai_task_id.is_not(None))
    elif matched is False:
        conditions.append(AiUsageLog.ai_task_id.is_(None))
    if request_id:
        conditions.append(AiUsageLog.request_id == request_id)
    if start is not None:
        conditions.append(AiUsageLog.pulled_at >= to_utc_naive(start))
    if end is not None:
        conditions.append(AiUsageLog.pulled_at < to_utc_naive(end))  # 左闭右开（docs/04 §3）
    stmt = scope_usage_logs(select(AiUsageLog), scope).where(*conditions)
    count_stmt = scope_usage_logs(select(func.count(AiUsageLog.id)), scope).where(*conditions)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(stmt.order_by(AiUsageLog.pulled_at.desc(), AiUsageLog.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return [usage_log_item(r) for r in rows], total


def _summary_range(db: Session, start: date | None, end: date | None) -> tuple[date, date]:
    today = stats_service.today_date(db)
    end = end or today
    start = start or (end - timedelta(days=SUMMARY_DEFAULT_DAYS - 1))
    if start > end:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["query", "start"], "开始日期不能晚于结束日期", "value_error", start.isoformat())])
    if (end - start).days + 1 > SUMMARY_MAX_DAYS:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["query", "end"], f"跨度不能超过 {SUMMARY_MAX_DAYS} 天", "value_error", end.isoformat())])
    return start, end


def usage_summary(
    db: Session, scope: DataScope, *, group_by: str = "model", start: date | None = None, end: date | None = None
) -> list[dict[str, Any]]:
    """``[{key, calls, prompt_tokens, completion_tokens, quota_estimated, quota_actual, cost_cny, reconciled_rate}]``：
    按终态尝试行（``status ∈ succeeded/failed``、``trigger_type != health_probe``），``finished_at`` 落在
    ``[start, end]``（``stats_config.timezone`` 切日，缺省最近 30 天，跨度 ≤ 366 天）；只汇总可见尝试行。"""
    if group_by not in SUMMARY_GROUPS:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["query", "group_by"], f"取值必须是 {'、'.join(SUMMARY_GROUPS)} 之一", "enum", group_by)])
    start, end = _summary_range(db, start, end)
    tz = stats_service.get_tz(db)
    lower, upper = stats_service.range_bounds(start, end, tz=tz)
    base_conditions = [
        AiTask.root_task_id.is_not(None),
        AiTask.status.in_(("succeeded", "failed")),
        AiTask.trigger_type != "health_probe",
        AiTask.finished_at >= lower,
        AiTask.finished_at < upper,
    ]
    metrics = (
        func.count(AiTask.id),
        func.coalesce(func.sum(AiTask.prompt_tokens), 0),
        func.coalesce(func.sum(AiTask.completion_tokens), 0),
        func.coalesce(func.sum(AiTask.quota_estimated), 0),
        func.coalesce(func.sum(AiTask.quota_actual), 0),
        func.coalesce(func.sum(AiTask.cost_cny), 0),
        func.count(AiTask.reconciled_at),
    )
    rows: list[tuple[Any, ...]]
    if group_by == "day":
        stmt = scope_by_project(
            select(
                AiTask.finished_at, AiTask.prompt_tokens, AiTask.completion_tokens, AiTask.quota_estimated,
                AiTask.quota_actual, AiTask.cost_cny, AiTask.reconciled_at,
            ).where(*base_conditions),
            AiTask.project_id,
            scope,
        )
        buckets: dict[str, list[Any]] = {}
        for finished_at, pt, ct, qe, qa, cost, reconciled_at in db.execute(stmt).yield_per(2000):
            key = stats_service.local_date(finished_at, tz=tz).isoformat()
            bucket = buckets.setdefault(key, [0, 0, 0, 0, 0, Decimal(0), 0])
            bucket[0] += 1
            bucket[1] += int(pt or 0)
            bucket[2] += int(ct or 0)
            bucket[3] += int(qe or 0)
            bucket[4] += int(qa or 0)
            bucket[5] += Decimal(str(cost or 0))
            bucket[6] += 1 if reconciled_at is not None else 0
        rows = [(key, *values) for key, values in sorted(buckets.items())]
    else:
        column = {"model": AiTask.model, "capability": AiTask.capability, "project": AiTask.project_id}[group_by]
        stmt = scope_by_project(select(column, *metrics).where(*base_conditions), AiTask.project_id, scope).group_by(column)
        rows = [tuple(r) for r in db.execute(stmt).all()]
    items = []
    for key, calls, pt, ct, qe, qa, cost, reconciled in rows:
        calls = int(calls or 0)
        items.append(
            {
                "key": None if key is None else str(key),
                "calls": calls,
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "quota_estimated": int(qe or 0),
                "quota_actual": int(qa or 0),
                "cost_cny": round(float(cost or 0), 6),
                "reconciled_rate": round(int(reconciled or 0) / calls, 4) if calls else 0.0,
            }
        )
    if group_by != "day":
        items.sort(key=lambda item: (-item["calls"], item["key"] or ""))
    return items


def last_pull_view(scope: DataScope) -> dict[str, Any] | None:
    """``GET /admin/ai/usage/last-pull``：键不存在 → ``None``；``all`` 范围（未带 ``owner_id``）返回完整摘要，
    ``own`` 范围或带 ``owner_id`` 只返回 ``{pulled_at, window_overflow}``（docs/13 §4.3）。"""
    data = cache_get_json(LAST_PULL_KEY)
    if not isinstance(data, dict):
        return None
    if scope.is_all and not scope.restricted:
        return {
            "pulled_at": data.get("pulled_at"),
            "pulled": int(data.get("pulled") or 0),
            "new": int(data.get("new") or 0),
            "matched": int(data.get("matched") or 0),
            "unmatched": int(data.get("unmatched") or 0),
            "window_overflow": bool(data.get("window_overflow")),
            "request_ids": list(data.get("request_ids") or []),
        }
    return {"pulled_at": data.get("pulled_at"), "window_overflow": bool(data.get("window_overflow"))}
