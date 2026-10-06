"""告警服务（docs/11-link-backfill-and-monitoring.md §10；docs/03 B.23「告警去重」；docs/13 §11）。

- 写入口只有 ``raise_alert`` / ``resolve_alert``（事件型告警与业务写入同事务：只 ``flush`` 不 ``commit``，由调用方提交）；
  ``dedupe_key = {alert_type}:{target_type}:{target_key}``，命中 ``open`` / ``acknowledged`` 行只累加 ``trigger_count``；
  ``resolved`` / ``ignored`` 为终态，再次触发新建一行；``link_restored`` 创建即 ``resolved``。
- 通道投递 ``deliver(alert, event)`` 在事务提交后执行：``in_app`` 落库即完成；``webhook`` 按 ``alert_config.channels.webhook``
  投递（默认关闭）；``email`` 首版只保留骨架与配置校验，不发送。``alert:cooldown:{dedupe_key}`` 冷却期内不重复投递通道。
- 人工处理 ``acknowledge`` / ``resolve`` / ``ignore`` / ``batch_resolve`` 与 ``list_alerts`` / ``get_alert`` / ``summary``
  均按数据范围过滤（不可见按不存在处理）；``project_id`` 为 NULL 的系统告警只对总后台可见。
- ``evaluate(db)``（周期规则 ①~⑤）由第 5 步替换，本文件中为占位实现。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import redis
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, after_commit
from app.core.exceptions import CODE_CONFLICT, BusinessError
from app.core.redis import redis_client
from app.models import Alert, utcnow
from app.schemas.common import iso_utc, to_utc_naive
from app.services import settings_service, stats_service
from app.services.data_scope_service import (
    DataScope,
    get_visible,
    is_visible,
    scope_by_project,
)

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("open", "acknowledged")
TERMINAL_STATUSES = ("resolved", "ignored")
SEVERITIES = ("info", "warning", "critical")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
COOLDOWN_PREFIX = "alert:cooldown:"
EVENT_TRIGGERED = "alert.triggered"
EVENT_RESOLVED = "alert.resolved"
WEBHOOK_TIMEOUT_SECONDS = 5
AUTO_NOTE = "auto"
MAX_TITLE = 200
MAX_MESSAGE = 1000
MAX_NOTE = 500
# 创建即解决的告警类型（docs/03「告警去重」）
CREATED_RESOLVED_TYPES = frozenset({"link_restored"})


# =====================================================================
# 工具
# =====================================================================


def dedupe_key_for(alert_type: str, target_type: str | None, target_key: str) -> str:
    return f"{alert_type}:{target_type or ''}:{target_key or ''}"


def _config(db: Session) -> dict[str, Any]:
    return settings_service.get_config(db, "alert_config")


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def alert_item(alert: Alert) -> dict[str, Any]:
    """告警对象（docs/04 §7.13）。"""
    return {
        "id": alert.id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "status": alert.status,
        "project_id": alert.project_id,
        "target_type": alert.target_type,
        "target_id": alert.target_id,
        "target_key": alert.target_key,
        "dedupe_key": alert.dedupe_key,
        "title": alert.title,
        "message": alert.message,
        "payload": _loads(alert.payload_json, None),
        "first_triggered_at": iso_utc(alert.first_triggered_at),
        "last_triggered_at": iso_utc(alert.last_triggered_at),
        "trigger_count": alert.trigger_count,
        "acknowledged_by": alert.acknowledged_by,
        "acknowledged_at": iso_utc(alert.acknowledged_at),
        "resolved_by": alert.resolved_by,
        "resolved_at": iso_utc(alert.resolved_at),
        "resolution_note": alert.resolution_note,
        "notified_channels": _loads(alert.notified_channels_json, []),
        "created_at": iso_utc(alert.created_at),
        "updated_at": iso_utc(alert.updated_at),
    }


def _merge_channels(existing: str | None, channels: Iterable[str]) -> str:
    merged: list[str] = list(_loads(existing, []) or [])
    for channel in channels:
        if channel not in merged:
            merged.append(channel)
    return _dumps(merged)


def _schedule_deliver(db: Session, alert: Alert, event: str) -> None:
    """提交后投递通道，并把新成功的外部通道追加到 ``notified_channels_json``（新开会话写入）。"""
    snapshot = alert_item(alert)
    alert_id = alert.id

    def _run() -> None:
        channels = deliver(snapshot, event)
        external = [c for c in channels if c != "in_app"]
        if not external:
            return
        try:
            with SessionLocal() as session:
                row = session.get(Alert, alert_id)
                if row is not None:
                    row.notified_channels_json = _merge_channels(row.notified_channels_json, external)
                    session.commit()
        except Exception:  # noqa: BLE001
            logger.warning("回写告警 notified_channels 失败 alert_id=%s", alert_id, exc_info=True)

    after_commit(db, _run)


def _count_stat(db: Session, project_id: int | None, field: str, at: datetime) -> None:
    stats_service.increment_realtime_after_commit(db, project_id, {field: 1}, at=at)


# =====================================================================
# 写入口
# =====================================================================


def raise_alert(
    db: Session,
    scope: DataScope,
    alert_type: str,
    *,
    target_type: str,
    target_id: int | None = None,
    target_key: str = "",
    project_id: int | None = None,
    title: str,
    message: str,
    payload: dict | None = None,
) -> Alert | None:
    """触发告警（同事务，只 ``flush``）：``alert_config.enabled=false`` 或规则 ``enabled=false`` → 不创建，返回 ``None``；
    ``severity`` 取 ``rules[alert_type].severity``；``target_key`` 缺省为 ``str(target_id)``；同 ``dedupe_key`` 存在
    ``open`` / ``acknowledged`` 行时只累加 ``trigger_count``、``last_triggered_at``、``payload_json``；提交后 ``deliver``。

    ``scope`` 为调用方的数据范围（worker 传 ``SYSTEM_SCOPE``），告警的归属只由 ``project_id`` 决定。
    """
    del scope  # 归属只看 project_id（docs/13 §11）；保留参数以统一 service 签名
    cfg = _config(db)
    rule = (cfg.get("rules") or {}).get(alert_type) or {}
    if not cfg.get("enabled", True) or not rule.get("enabled", True):
        return None
    severity = rule.get("severity") if rule.get("severity") in SEVERITIES else "warning"
    if not target_key and target_id is not None:
        target_key = str(target_id)
    target_key = (target_key or "")[:160]
    dedupe_key = dedupe_key_for(alert_type, target_type, target_key)[:255]
    now = utcnow()
    payload_json = _dumps(payload) if payload is not None else None

    existing = None
    if alert_type not in CREATED_RESOLVED_TYPES:
        existing = db.scalars(
            select(Alert)
            .where(Alert.dedupe_key == dedupe_key, Alert.status.in_(ACTIVE_STATUSES))
            .order_by(Alert.id.desc())
            .limit(1)
        ).first()
    if existing is not None:
        existing.trigger_count = int(existing.trigger_count or 0) + 1
        existing.last_triggered_at = now
        if payload_json is not None:
            existing.payload_json = payload_json
        db.flush()
        _schedule_deliver(db, existing, EVENT_TRIGGERED)
        return existing

    alert = Alert(
        alert_type=alert_type,
        severity=severity,
        status="open",
        project_id=project_id,
        target_type=target_type,
        target_id=target_id,
        target_key=target_key,
        dedupe_key=dedupe_key,
        title=(title or alert_type)[:MAX_TITLE],
        message=(message or "")[:MAX_MESSAGE],
        payload_json=payload_json,
        first_triggered_at=now,
        last_triggered_at=now,
        trigger_count=1,
        notified_channels_json=_dumps(["in_app"]),
    )
    if alert_type in CREATED_RESOLVED_TYPES:
        alert.status = "resolved"
        alert.resolved_at = now
        alert.resolution_note = AUTO_NOTE
    db.add(alert)
    db.flush()
    _count_stat(db, project_id, "alerts_opened", now)
    if alert.status == "resolved":
        _count_stat(db, project_id, "alerts_resolved", now)
    _schedule_deliver(db, alert, EVENT_TRIGGERED)
    return alert


def resolve_alert(
    db: Session,
    scope: DataScope,
    alert_type: str,
    target_type: str,
    target_key: str,
    *,
    note: str = AUTO_NOTE,
    resolved_by: int | None = None,
) -> int:
    """解决该 ``dedupe_key`` 下所有 ``open`` / ``acknowledged`` 告警（同事务，只 ``flush``），返回条数；
    自动解决 ``resolved_by=NULL``、``resolution_note='auto'``。``scope`` 受限时只解决可见项目的告警。"""
    dedupe_key = dedupe_key_for(alert_type, target_type, target_key)[:255]
    stmt = select(Alert).where(Alert.dedupe_key == dedupe_key, Alert.status.in_(ACTIVE_STATUSES))
    stmt = scope_by_project(stmt, Alert.project_id, scope)
    rows = db.scalars(stmt).all()
    if not rows:
        return 0
    now = utcnow()
    for alert in rows:
        _mark_resolved(db, alert, now=now, note=note, resolved_by=resolved_by)
    db.flush()
    return len(rows)


def _mark_resolved(db: Session, alert: Alert, *, now: datetime, note: str | None, resolved_by: int | None) -> None:
    alert.status = "resolved"
    alert.resolved_at = now
    alert.resolved_by = resolved_by
    alert.resolution_note = note[:MAX_NOTE] if note else None
    _count_stat(db, alert.project_id, "alerts_resolved", now)
    _schedule_deliver(db, alert, EVENT_RESOLVED)


# =====================================================================
# 通道投递（§10.4）
# =====================================================================


class WebhookChannel:
    """``POST ${ALERT_WEBHOOK_URL}``（JSON，超时 5s）；头 ``X-Aicreat-Event`` 与可选 ``X-Aicreat-Signature``。"""

    name = "webhook"

    def __init__(self, config: dict[str, Any], *, transport: httpx.BaseTransport | None = None) -> None:
        self.config = config or {}
        self.transport = transport

    @property
    def url(self) -> str:
        env_name = str(self.config.get("url_env") or "ALERT_WEBHOOK_URL")
        value = getattr(settings, env_name.lower(), "") if env_name.isupper() else ""
        return str(value or "").strip()

    def validate(self) -> list[str]:
        return [] if self.url else ["ALERT_WEBHOOK_URL 未配置"]

    @staticmethod
    def build_body(alert: dict[str, Any], event: str) -> dict[str, Any]:
        keys = (
            "id", "alert_type", "severity", "status", "project_id", "target_type", "target_id", "target_key",
            "title", "message", "trigger_count", "first_triggered_at", "last_triggered_at", "payload",
        )
        return {
            "event": event,
            "sent_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "alert": {k: alert.get(k) for k in keys},
        }

    @staticmethod
    def signature(secret: str, body: bytes) -> str:
        return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    def send(self, alert: dict[str, Any], event: str) -> bool:
        url = self.url
        if not url:
            logger.warning("告警 webhook 已启用但 ALERT_WEBHOOK_URL 为空，跳过投递")
            return False
        body = json.dumps(self.build_body(alert, event), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-Aicreat-Event": event, "User-Agent": settings.zhiqi_user_agent}
        secret = (settings.alert_webhook_secret or "").strip()
        if secret:
            headers["X-Aicreat-Signature"] = self.signature(secret, body)
        try:
            with httpx.Client(timeout=WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False, transport=self.transport) as client:
                resp = client.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("告警 webhook 投递失败 alert_id=%s: %s", alert.get("id"), type(exc).__name__)
            return False
        if not 200 <= resp.status_code < 300:
            logger.warning("告警 webhook 投递失败 alert_id=%s http_status=%s", alert.get("id"), resp.status_code)
            return False
        return True


class EmailChannel:
    """SMTP 邮件通道骨架：首版只做配置校验，不发送（docs/11 §10.4）。"""

    name = "email"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not (settings.smtp_host or "").strip():
            errors.append("SMTP_HOST 未配置")
        recipients = self.config.get("to") or []
        if not recipients:
            errors.append("收件人列表为空")
        return errors

    @staticmethod
    def subject(alert: dict[str, Any]) -> str:
        return f"[aicreat][{alert.get('severity')}] {alert.get('title')}"

    def send(self, alert: dict[str, Any], event: str) -> bool:
        problems = self.validate()
        if problems:
            logger.warning("告警邮件通道配置不完整：%s", "；".join(problems))
        else:
            logger.info("告警邮件通道首版不发送（alert_id=%s event=%s subject=%s）", alert.get("id"), event, self.subject(alert))
        return False


def _severity_allowed(alert_severity: str | None, min_severity: str | None) -> bool:
    return SEVERITY_RANK.get(alert_severity or "info", 0) >= SEVERITY_RANK.get(min_severity or "info", 0)


def _cooldown_acquire(dedupe_key: str, minutes: int) -> bool:
    if minutes <= 0:
        return True
    try:
        return bool(redis_client.set(f"{COOLDOWN_PREFIX}{dedupe_key}", "1", nx=True, ex=int(minutes) * 60))
    except redis.RedisError as exc:
        logger.warning("告警冷却键写入失败，按可投递处理: %s", exc)
        return True


def deliver(alert: Alert | dict[str, Any], event: str, *, config: dict[str, Any] | None = None) -> list[str]:
    """按 ``alert_config.channels`` 逐通道投递，返回成功投递的通道（``in_app`` 落库即完成，恒在其中）。

    ``severity`` 低于通道 ``min_severity`` 不投递；``alert.triggered`` 在 ``alert:cooldown:{dedupe_key}`` 冷却期内不投递外部通道；
    失败只记 WARNING，不重试、不影响告警落库。
    """
    data = alert_item(alert) if isinstance(alert, Alert) else dict(alert)
    if config is None:
        try:
            with SessionLocal() as session:
                config = _config(session)
        except Exception:  # noqa: BLE001
            logger.warning("读取 alert_config 失败，跳过外部通道投递", exc_info=True)
            return ["in_app"]
    channels_cfg = (config or {}).get("channels") or {}
    candidates: list[Any] = []
    webhook_cfg = channels_cfg.get("webhook") or {}
    if webhook_cfg.get("enabled") and _severity_allowed(data.get("severity"), webhook_cfg.get("min_severity")):
        candidates.append(WebhookChannel(webhook_cfg))
    email_cfg = channels_cfg.get("email") or {}
    if email_cfg.get("enabled") and _severity_allowed(data.get("severity"), email_cfg.get("min_severity")):
        candidates.append(EmailChannel(email_cfg))
    delivered = ["in_app"]
    if not candidates:
        return delivered
    if event == EVENT_TRIGGERED and not _cooldown_acquire(str(data.get("dedupe_key") or ""), int((config or {}).get("dedupe_cooldown_minutes") or 0)):
        return delivered
    for channel in candidates:
        try:
            if channel.send(data, event):
                delivered.append(channel.name)
        except Exception:  # noqa: BLE001
            logger.warning("告警通道 %s 投递异常 alert_id=%s", channel.name, data.get("id"), exc_info=True)
    return delivered


# =====================================================================
# 人工处理（/admin/alerts/*）
# =====================================================================


def _conflict(alert: Alert, message: str) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data={"current_status": alert.status})


def get_alert(db: Session, scope: DataScope, alert_id: int) -> dict[str, Any]:
    return alert_item(get_visible(db, scope, Alert, alert_id))


def acknowledge(db: Session, scope: DataScope, alert_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``open → acknowledged``；其它状态 409 ``current_status``。"""
    alert = get_visible(db, scope, Alert, alert_id)
    if alert.status != "open":
        raise _conflict(alert, "只有未处理的告警可以确认")
    alert.status = "acknowledged"
    alert.acknowledged_by = admin_id
    alert.acknowledged_at = utcnow()
    db.commit()
    return alert_item(alert)


def resolve(db: Session, scope: DataScope, alert_id: int, *, admin_id: int | None, note: str | None = None) -> dict[str, Any]:
    """``open`` / ``acknowledged → resolved``（``{note?}``）；终态 409。"""
    alert = get_visible(db, scope, Alert, alert_id)
    if alert.status not in ACTIVE_STATUSES:
        raise _conflict(alert, "告警已处理")
    _mark_resolved(db, alert, now=utcnow(), note=note, resolved_by=admin_id)
    db.commit()
    return alert_item(alert)


def ignore(db: Session, scope: DataScope, alert_id: int, *, admin_id: int | None, note: str | None = None) -> dict[str, Any]:
    """``open`` / ``acknowledged → ignored``；终态 409。"""
    alert = get_visible(db, scope, Alert, alert_id)
    if alert.status not in ACTIVE_STATUSES:
        raise _conflict(alert, "告警已处理")
    alert.status = "ignored"
    if alert.acknowledged_at is None:  # 记录处理人；resolved_at 只用于「已解决」（daily_stats.alerts_resolved 按其归属）
        alert.acknowledged_by = admin_id
        alert.acknowledged_at = utcnow()
    if note:
        alert.resolution_note = note[:MAX_NOTE]
    db.commit()
    return alert_item(alert)


def batch_resolve(
    db: Session, scope: DataScope, ids: Sequence[int], *, admin_id: int | None, note: str | None = None
) -> dict[str, Any]:
    """逐条独立判定与事务：不可见 / 不存在 → ``skipped[{id, reason:not_found}]``；终态 → ``invalid_transition``。"""
    updated = 0
    skipped: list[dict[str, Any]] = []
    seen: set[int] = set()
    for alert_id in ids:
        if alert_id in seen:
            continue
        seen.add(alert_id)
        alert = db.get(Alert, alert_id)
        if alert is None or not is_visible(db, scope, alert):
            skipped.append({"id": alert_id, "reason": "not_found"})
            continue
        if alert.status not in ACTIVE_STATUSES:
            skipped.append({"id": alert_id, "reason": "invalid_transition"})
            continue
        try:
            _mark_resolved(db, alert, now=utcnow(), note=note, resolved_by=admin_id)
            db.commit()
            updated += 1
        except Exception:
            db.rollback()
            raise
    return {"updated": updated, "skipped": skipped}


def list_alerts(
    db: Session,
    scope: DataScope,
    *,
    status: str | None = None,
    severity: str | None = None,
    alert_type: str | None = None,
    project_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """分页（按 ``last_triggered_at`` 倒序）；``start`` / ``end`` 作用于 ``last_triggered_at``（闭区间）。"""
    conditions = []
    if status:
        conditions.append(Alert.status == status)
    if severity:
        conditions.append(Alert.severity == severity)
    if alert_type:
        conditions.append(Alert.alert_type == alert_type)
    if project_id is not None:
        conditions.append(Alert.project_id == project_id)
    if target_type:
        conditions.append(Alert.target_type == target_type)
    if target_id is not None:
        conditions.append(Alert.target_id == target_id)
    if start is not None:
        conditions.append(Alert.last_triggered_at >= to_utc_naive(start))
    if end is not None:
        conditions.append(Alert.last_triggered_at <= to_utc_naive(end))
    stmt = scope_by_project(select(Alert), Alert.project_id, scope)
    count_stmt = scope_by_project(select(func.count(Alert.id)), Alert.project_id, scope)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(
        stmt.order_by(Alert.last_triggered_at.desc(), Alert.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return [alert_item(a) for a in rows], total


def summary(db: Session, scope: DataScope) -> dict[str, Any]:
    """``{open{info,warning,critical}, acknowledged{…}, today_opened, today_resolved}``（今日按 ``stats_config.timezone``）。"""
    result: dict[str, Any] = {
        "open": {s: 0 for s in SEVERITIES},
        "acknowledged": {s: 0 for s in SEVERITIES},
        "today_opened": 0,
        "today_resolved": 0,
    }
    stmt = scope_by_project(
        select(Alert.status, Alert.severity, func.count(Alert.id)).where(Alert.status.in_(ACTIVE_STATUSES)),
        Alert.project_id,
        scope,
    ).group_by(Alert.status, Alert.severity)
    for status, severity, count in db.execute(stmt).all():
        if status in result and severity in result[status]:
            result[status][severity] = int(count)
    start, end = stats_service.day_bounds(stats_service.today_date(db), db)
    opened = scope_by_project(
        select(func.count(Alert.id)).where(and_(Alert.first_triggered_at >= start, Alert.first_triggered_at < end)),
        Alert.project_id,
        scope,
    )
    resolved = scope_by_project(
        select(func.count(Alert.id)).where(and_(Alert.resolved_at >= start, Alert.resolved_at < end), Alert.status == "resolved"),
        Alert.project_id,
        scope,
    )
    result["today_opened"] = int(db.scalar(opened) or 0)
    result["today_resolved"] = int(db.scalar(resolved) or 0)
    return result


def auto_resolve_where(db: Session, scope: DataScope, *conditions: Any, note: str = AUTO_NOTE) -> int:
    """按条件批量自动解决 ``open`` / ``acknowledged`` 告警（供周期评估 ⑤ 使用），返回条数（只 ``flush``）。"""
    stmt = scope_by_project(select(Alert).where(Alert.status.in_(ACTIVE_STATUSES), *conditions), Alert.project_id, scope)
    rows = db.scalars(stmt).all()
    now = utcnow()
    for alert in rows:
        _mark_resolved(db, alert, now=now, note=note, resolved_by=None)
    db.flush()
    return len(rows)


def has_active(db: Session, alert_type: str, target_type: str, target_key: str) -> bool:
    """该 ``dedupe_key`` 是否存在 ``open`` / ``acknowledged`` 告警（周期评估兜底补发前判断）。"""
    dedupe_key = dedupe_key_for(alert_type, target_type, target_key)[:255]
    return db.scalar(
        select(func.count(Alert.id)).where(Alert.dedupe_key == dedupe_key, Alert.status.in_(ACTIVE_STATUSES))
    ) > 0


# =====================================================================
# 周期评估（第 5 步实现）
# =====================================================================


def evaluate(db: Session) -> dict[str, Any]:
    """PHASE-5 PLACEHOLDER：``evaluate_alerts`` 的周期规则 ①~⑤（``index_overdue`` / ``ai_task_failures`` /
    ``ai_breaker_open`` 兜底 / ``worker_stale`` / 自动解决）由第 5 步以同名函数替换。当前不做任何评估，只返回空计数。"""
    del db
    return {"index_overdue": 0, "ai_task_failures": 0, "ai_breaker_open": 0, "worker_stale": 0, "auto_resolved": 0, "placeholder": True}


__all__ = [
    "ACTIVE_STATUSES",
    "EmailChannel",
    "WebhookChannel",
    "acknowledge",
    "alert_item",
    "auto_resolve_where",
    "batch_resolve",
    "dedupe_key_for",
    "deliver",
    "evaluate",
    "get_alert",
    "has_active",
    "ignore",
    "list_alerts",
    "raise_alert",
    "resolve",
    "resolve_alert",
    "summary",
]
