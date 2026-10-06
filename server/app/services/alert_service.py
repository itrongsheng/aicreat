"""告警服务（docs/11-link-backfill-and-monitoring.md §10；docs/03 B.23「告警去重」；docs/13 §11）。

- 写入口只有 ``raise_alert`` / ``resolve_alert``（事件型告警与业务写入同事务：只 ``flush`` 不 ``commit``，由调用方提交）；
  ``dedupe_key = {alert_type}:{target_type}:{target_key}``，命中 ``open`` / ``acknowledged`` 行只累加 ``trigger_count``；
  ``resolved`` / ``ignored`` 为终态，再次触发新建一行；``link_restored`` 创建即 ``resolved``。
- 通道投递 ``deliver(alert, event)`` 在事务提交后执行：``in_app`` 落库即完成；``webhook`` 按 ``alert_config.channels.webhook``
  投递（默认关闭）；``email`` 按 ``channels.email`` 经 SMTP 投递（默认关闭）。``alert:cooldown:{dedupe_key}`` 冷却期内不重复投递通道。
- 人工处理 ``acknowledge`` / ``resolve`` / ``ignore`` / ``batch_resolve`` 与 ``list_alerts`` / ``get_alert`` / ``summary``
  均按数据范围过滤（不可见按不存在处理）；``project_id`` 为 NULL 的系统告警只对总后台可见。
- ``evaluate(db)``：``evaluate_alerts`` 的周期规则 ①~⑤（``index_overdue`` / ``ai_task_failures`` / ``ai_breaker_open`` 兜底 /
  ``worker_stale``（``worker``）/ 自动解决），持 ``lock:monitor:evaluate_alerts``（300s），各步骤独立提交。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import smtplib
import ssl
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

import httpx
import redis
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, after_commit
from app.core.exceptions import CODE_CONFLICT, BusinessError
from app.core.locks import acquire_lock, release_lock
from app.core.redis import redis_client
from app.models import AiTask, Alert, PublishLink, PublishPlatform, utcnow
from app.schemas.common import iso_utc, to_utc_naive
from app.services import settings_service, stats_service
from app.services.data_scope_service import (
    SYSTEM_SCOPE,
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
# 默认 transport（``None`` = httpx 默认网络栈）；测试可替换为 ``httpx.MockTransport``，不提供任何地址豁免
WEBHOOK_TRANSPORT: httpx.BaseTransport | None = None
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
        self.transport = transport if transport is not None else WEBHOOK_TRANSPORT

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


ADMIN_UI_PREFIX = "/admin"
EMAIL_TIMEOUT_SECONDS = 10


def alert_detail_url(alert: dict[str, Any]) -> str:
    """后台详情页 URL（``PUBLIC_BASE_URL`` + 管理端路由，docs/11 §11.7 的目标跳转）：``publish_link`` → 链接详情，
    ``ai_model`` / ``capability_route`` → 「AI 网关 → 能力路由」，``media_asset`` → 素材库，其它 → 告警中心。"""
    base = (settings.public_base_url or "").strip().rstrip("/") + ADMIN_UI_PREFIX
    target_type = alert.get("target_type")
    target_id = alert.get("target_id")
    if target_type == "publish_link" and target_id:
        return f"{base}/links/{target_id}"
    if target_type in ("ai_model", "capability_route"):
        return f"{base}/ai/routes"
    if target_type == "media_asset":
        return f"{base}/media/assets"
    return f"{base}/alerts"


def _header_text(value: Any) -> str:
    return " ".join(str(value or "").split())


class EmailChannel:
    """SMTP 邮件通道（docs/11 §10.4；默认关闭）：``SMTP_HOST`` / ``SMTP_PORT`` / ``SMTP_USER`` / ``SMTP_PASSWORD`` / ``MAIL_FROM``，
    主题 ``[aicreat][{severity}] {title}``，正文为 message + 后台详情 URL。端口 465 走 SMTPS，其它端口在服务器支持时
    STARTTLS；超时 10s；配置不完整或投递失败只记 WARNING、返回 ``False``（不重试、不影响告警落库）。"""

    name = "email"

    def __init__(self, config: dict[str, Any], *, smtp_factory: Any = None) -> None:
        self.config = config or {}
        self.smtp_factory = smtp_factory

    @property
    def recipients(self) -> list[str]:
        return [str(r).strip() for r in self.config.get("to") or [] if str(r or "").strip()]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not (settings.smtp_host or "").strip():
            errors.append("SMTP_HOST 未配置")
        if not self.recipients:
            errors.append("收件人列表为空")
        return errors

    @staticmethod
    def subject(alert: dict[str, Any]) -> str:
        return _header_text(f"[aicreat][{alert.get('severity')}] {alert.get('title')}")

    @staticmethod
    def body(alert: dict[str, Any], event: str) -> str:
        state = "已解决" if event == EVENT_RESOLVED else "已触发"
        lines = [
            str(alert.get("message") or alert.get("title") or ""),
            "",
            f"告警类型：{alert.get('alert_type')}（{state}，severity={alert.get('severity')}，触发 {alert.get('trigger_count') or 1} 次）",
            f"目标：{alert.get('target_type') or '-'} {alert.get('target_key') or ''}".rstrip(),
            f"最近触发：{alert.get('last_triggered_at') or '-'}",
            f"详情：{alert_detail_url(alert)}",
        ]
        return "\n".join(lines) + "\n"

    def build_message(self, alert: dict[str, Any], event: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = self.subject(alert)
        message["From"] = _header_text(settings.mail_from) or "no-reply@example.com"
        message["To"] = ", ".join(self.recipients)
        message["X-Aicreat-Event"] = event
        message.set_content(self.body(alert, event))
        return message

    def _connect(self) -> smtplib.SMTP:
        if self.smtp_factory is not None:
            return self.smtp_factory()
        host = settings.smtp_host.strip()
        port = int(settings.smtp_port or 465)
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=EMAIL_TIMEOUT_SECONDS, context=ssl.create_default_context())
        client = smtplib.SMTP(host, port, timeout=EMAIL_TIMEOUT_SECONDS)
        client.ehlo()
        if client.has_extn("starttls"):
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        return client

    def send(self, alert: dict[str, Any], event: str) -> bool:
        problems = self.validate()
        if problems:
            logger.warning("告警邮件通道配置不完整，跳过投递：%s", "；".join(problems))
            return False
        try:
            message = self.build_message(alert, event)
            client = self._connect()
            try:
                user = (settings.smtp_user or "").strip()
                if user:
                    client.login(user, settings.smtp_password or "")
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except (smtplib.SMTPException, OSError):
                    pass
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            logger.warning("告警邮件投递失败 alert_id=%s: %s", alert.get("id"), type(exc).__name__)
            return False
        return True


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
# 周期评估（docs/11 §10.1、§10.5；docs/01 §5.2 monitor_worker 第 6 行）
# =====================================================================

EVALUATE_LOCK_KEY = "lock:monitor:evaluate_alerts"
EVALUATE_LOCK_TTL = 300
INDEX_OVERDUE_BATCH = 500
FAILURE_STREAK_SCAN = 200
BREAKER_SCAN_LIMIT = 1000
LINK_ALIVE_STATES = ("alive", "changed")
HEARTBEAT_ALIVE_SECONDS = 90
EVALUATED_TYPES = ("index_overdue", "ai_task_failures", "ai_breaker_open", "worker_stale")


def _rule(cfg: Mapping[str, Any], alert_type: str) -> dict[str, Any]:
    rule = (cfg.get("rules") or {}).get(alert_type) or {}
    return dict(rule) if isinstance(rule, Mapping) else {}


def _rule_active(cfg: Mapping[str, Any], alert_type: str) -> bool:
    return bool(cfg.get("enabled", True)) and bool(_rule(cfg, alert_type).get("enabled", True))


def _pos_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _strip_scheme(url: str | None) -> str:
    raw = url or ""
    lowered = raw.lower()
    for prefix in ("https://", "http://"):
        if lowered.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _active_payload(db: Session, alert_type: str, target_type: str, target_key: str) -> dict[str, Any] | None:
    """该 ``dedupe_key`` 最新一条 ``open`` / ``acknowledged`` 告警的 ``payload``；无活动告警 → ``None``。"""
    dedupe_key = dedupe_key_for(alert_type, target_type, target_key)[:255]
    row = db.scalars(
        select(Alert)
        .where(Alert.dedupe_key == dedupe_key, Alert.status.in_(ACTIVE_STATUSES))
        .order_by(Alert.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    payload = _loads(row.payload_json, {})
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------- ① index_overdue


def evaluate_index_overdue(db: Session, now: datetime, cfg: Mapping[str, Any], *, scope: DataScope = SYSTEM_SCOPE) -> int:
    """① ``published_at <= now − days`` 且 ``seo_indexed_any=0`` 且 ``index_checks_done >= min_checks`` 且
    ``alive_status ∈ {alive, changed}`` → ``index_overdue``（``publish_link`` / ``{link_id}``，``project_id`` = 链接所属项目）。

    已有 ``open`` / ``acknowledged`` 告警的链接本轮跳过（条件持续成立不重复累加 ``trigger_count``、不重复投递）；
    每轮最多 ``INDEX_OVERDUE_BATCH`` 条，其余留待下一轮。返回本轮新触发数。"""
    if not _rule_active(cfg, "index_overdue"):
        return 0
    rule = _rule(cfg, "index_overdue")
    days = _pos_int(rule.get("days"), 30)
    min_checks = _pos_int(rule.get("min_checks"), 3)
    cutoff = now - timedelta(days=days)
    active = exists().where(
        Alert.alert_type == "index_overdue",
        Alert.target_type == "publish_link",
        Alert.target_id == PublishLink.id,
        Alert.status.in_(ACTIVE_STATUSES),
    )
    links = db.scalars(
        scope_by_project(select(PublishLink), PublishLink.project_id, scope)
        .where(
            PublishLink.published_at <= cutoff,
            PublishLink.seo_indexed_any == False,  # noqa: E712
            PublishLink.index_checks_done >= min_checks,
            PublishLink.alive_status.in_(LINK_ALIVE_STATES),
            ~active,
        )
        .order_by(PublishLink.id)
        .limit(INDEX_OVERDUE_BATCH)
    ).all()
    if not links:
        return 0
    platform_ids = sorted({link.platform_id for link in links})
    codes = dict(db.execute(select(PublishPlatform.id, PublishPlatform.code).where(PublishPlatform.id.in_(platform_ids))).all())
    raised = 0
    for link in links:
        age_days = max(days, int((now - link.published_at).total_seconds() // 86400))
        platform_code = codes.get(link.platform_id) or ""
        alert = raise_alert(
            db, scope, "index_overdue",
            target_type="publish_link", target_id=link.id, project_id=link.project_id,
            title=f"链接超期未收录：{_strip_scheme(link.normalized_url)}",
            message=(
                f"平台 {platform_code or '-'} 的链接发布已 {age_days} 天，完成 {int(link.index_checks_done or 0)} 轮定时收录检测，"
                f"仍未被任何 SEO 引擎收录（阈值 {days} 天 / {min_checks} 轮）"
            ),
            payload={
                "url": link.url,
                "platform_code": platform_code,
                "content_id": link.content_id,
                "published_at": iso_utc(link.published_at),
                "index_checks_done": int(link.index_checks_done or 0),
                "days": days,
                "min_checks": min_checks,
            },
        )
        if alert is not None:
            raised += 1
    db.commit()
    return raised


# ---------------------------------------------------------------- ② ai_task_failures


def _attempt_time() -> Any:
    return func.coalesce(AiTask.finished_at, AiTask.created_at)


def _attempt_conditions() -> list[Any]:
    """计入「连续失败」判定的尝试行：``root_task_id IS NOT NULL``、已结束（``succeeded`` / ``failed``）、
    ``trigger_type != health_probe``、``error_category != cancelled``。"""
    return [
        AiTask.root_task_id.is_not(None),
        AiTask.status.in_(("succeeded", "failed")),
        AiTask.trigger_type != "health_probe",
        or_(AiTask.error_category.is_(None), AiTask.error_category != "cancelled"),
    ]


def evaluate_ai_task_failures(
    db: Session, now: datetime, cfg: Mapping[str, Any], *, scope: DataScope = SYSTEM_SCOPE
) -> tuple[int, set[str]]:
    """② 同 ``capability+model`` 在 ``window_minutes`` 内（按 ``COALESCE(finished_at, created_at)``）最近的尝试行连续失败
    ≥ ``consecutive`` → ``ai_task_failures``（``ai_model`` / ``{capability}:{model}``，``project_id=NULL``）。

    同 key 已有活动告警且没有更新的失败（``payload.last_failed_task_id`` 未变）时不重复触发。返回 ``(触发数, 仍在连续失败的 key 集合)``。"""
    if not _rule_active(cfg, "ai_task_failures"):
        return 0, set()
    rule = _rule(cfg, "ai_task_failures")
    consecutive = _pos_int(rule.get("consecutive"), 5)
    window = _pos_int(rule.get("window_minutes"), 30)
    since = now - timedelta(minutes=window)
    ts = _attempt_time()
    base = [*_attempt_conditions(), ts >= since]
    candidates = db.execute(
        select(AiTask.capability, AiTask.model)
        .where(*base, AiTask.status == "failed")
        .group_by(AiTask.capability, AiTask.model)
        .having(func.count(AiTask.id) >= consecutive)
    ).all()
    raised = 0
    failing: set[str] = set()
    for capability, model in candidates:
        rows = db.execute(
            select(AiTask.id, AiTask.status, ts.label("ts"), AiTask.error_category, AiTask.request_id, AiTask.http_status)
            .where(*base, AiTask.capability == capability, AiTask.model == model)
            .order_by(ts.desc(), AiTask.id.desc())
            .limit(FAILURE_STREAK_SCAN)
        ).all()
        streak: list[Any] = []
        for row in rows:
            if row.status != "failed":
                break
            streak.append(row)
        if len(streak) < consecutive:
            continue
        target_key = f"{capability}:{model}"
        failing.add(target_key)
        latest = streak[0]
        previous = _active_payload(db, "ai_task_failures", "ai_model", target_key)
        if previous is not None and previous.get("last_failed_task_id") == latest.id:
            continue
        categories: dict[str, int] = {}
        for row in streak:
            key = str(row.error_category or "unknown")
            categories[key] = categories.get(key, 0) + 1
        alert = raise_alert(
            db, scope, "ai_task_failures",
            target_type="ai_model", target_key=target_key,
            title=f"AI 调用连续失败：{target_key}",
            message=(
                f"模型 {model}（能力 {capability}）最近 {window} 分钟内连续失败 {len(streak)} 次（阈值 {consecutive} 次），"
                f"最近错误 {latest.error_category or 'unknown'}"
            ),
            payload={
                "capability": capability,
                "model": model,
                "consecutive_failures": len(streak),
                "window_minutes": window,
                "error_categories": categories,
                "last_failed_task_id": latest.id,
                "last_failed_at": iso_utc(latest.ts),
                "error_category": latest.error_category,
                "request_id": latest.request_id,
                "http_status": latest.http_status,
            },
        )
        if alert is not None:
            raised += 1
    db.commit()
    return raised, failing


# ---------------------------------------------------------------- ③ ai_breaker_open 兜底


def _breaker_keys() -> list[tuple[str, str]]:
    """``SCAN ai:breaker:*``（排除 ``ai:breaker:failures:*``）→ ``[(capability, model)]``。"""
    from app.core.zhiqi.breaker import FAILURES_PREFIX, KEY_PREFIX

    pairs: list[tuple[str, str]] = []
    for key in redis_client.scan_iter(match=f"{KEY_PREFIX}*", count=200):
        if key.startswith(FAILURES_PREFIX):
            continue
        capability, sep, model = key[len(KEY_PREFIX):].partition(":")
        if sep and capability and model:
            pairs.append((capability, model))
        if len(pairs) >= BREAKER_SCAN_LIMIT:
            break
    return sorted(set(pairs))


def evaluate_breakers(db: Session, cfg: Mapping[str, Any], *, scope: DataScope = SYSTEM_SCOPE) -> int:
    """③ 扫描熔断器状态键，``open`` 且无 ``open`` / ``acknowledged`` 的 ``ai_breaker_open`` 告警 → 补发（``half_open`` 不补发）。"""
    if not _rule_active(cfg, "ai_breaker_open"):
        return 0
    from app.services import ai_gateway_service

    breaker = ai_gateway_service.get_breaker(settings_service.get_config(db, "ai_routing_config"))
    raised = 0
    for capability, model in _breaker_keys():
        if breaker.state(capability, model) != "open":
            continue
        target_key = f"{capability}:{model}"
        if has_active(db, "ai_breaker_open", "ai_model", target_key):
            continue
        reason = breaker.reason(capability, model) or "failures"
        alert = raise_alert(
            db, scope, "ai_breaker_open",
            target_type="ai_model", target_key=target_key,
            title=f"AI 模型熔断：{target_key}",
            message=f"模型 {model}（能力 {capability}）的熔断器处于打开状态（原因 {reason}），由周期评估补发告警",
            payload={"capability": capability, "model": model, "reason": reason, "source": "evaluate_alerts"},
        )
        if alert is not None:
            raised += 1
    db.commit()
    return raised


# ---------------------------------------------------------------- ④ worker_stale


def evaluate_worker_stale(db: Session, now: datetime) -> dict[str, int]:
    """④ ``SCAN worker:heartbeat:worker:*`` → 副本级 / 进程级 ``worker_stale``（复用 ``recover_stale_tasks.check_worker_stale``，
    其中也完成 ``worker`` 名下的恢复与过期自动解决）。"""
    from app.tasks.recover_stale_tasks import check_worker_stale

    return check_worker_stale(db, "worker", now)


# ---------------------------------------------------------------- ⑤ 自动解决


def _link_target_conditions(alert_type: str, link_condition: Any) -> list[Any]:
    """``alert_type`` 的链接告警中，目标链接满足 ``link_condition`` 或已不存在（孤儿告警）。"""
    link_exists = exists().where(PublishLink.id == Alert.target_id)
    matched = exists().where(PublishLink.id == Alert.target_id, link_condition)
    return [Alert.alert_type == alert_type, Alert.target_type == "publish_link", or_(matched, ~link_exists)]


def _resolve_ai_task_failures(db: Session, failing: set[str]) -> int:
    rows = db.scalars(
        select(Alert).where(Alert.alert_type == "ai_task_failures", Alert.status.in_(ACTIVE_STATUSES)).order_by(Alert.id)
    ).all()
    if not rows:
        return 0
    now = utcnow()
    ts = _attempt_time()
    resolved = 0
    for alert in rows:
        if alert.target_key in failing:
            continue
        capability, sep, model = (alert.target_key or "").partition(":")
        if not sep or not capability or not model:
            continue
        payload = _loads(alert.payload_json, {})
        payload = payload if isinstance(payload, dict) else {}
        last_id = payload.get("last_failed_task_id")
        last_at = to_utc_naive(_parse_iso(payload.get("last_failed_at"))) or alert.first_triggered_at
        newer = [ts > last_at]
        if isinstance(last_id, int):
            newer.append(AiTask.id > last_id)
        success = db.scalar(
            select(AiTask.id)
            .where(
                *_attempt_conditions(),
                AiTask.status == "succeeded",
                AiTask.capability == capability,
                AiTask.model == model,
                or_(*newer),
            )
            .limit(1)
        )
        if success is None:
            continue
        _mark_resolved(db, alert, now=now, note=AUTO_NOTE, resolved_by=None)
        resolved += 1
    db.flush()
    return resolved


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _resolve_monitor_worker_stale(db: Session, now: datetime, cfg: Mapping[str, Any], scope: DataScope = SYSTEM_SCOPE) -> int:
    """``monitor_worker`` 的 ``worker_stale`` 只做自动解决（触发由 ``app.worker`` 的 ``recover_stale_tasks`` ⑤ 负责）：
    副本 ``at`` 恢复或副本键过期 → 解决副本级；任一副本存活 → 解决进程级。"""
    from app.tasks.recover_stale_tasks import read_heartbeats

    name = "monitor_worker"
    minutes = _pos_int(_rule(cfg, "worker_stale").get("minutes"), 5)
    replicas = read_heartbeats(name)
    resolved = 0
    fresh = [r["target_key"] for r in replicas if now - r["at"] <= timedelta(minutes=minutes)]
    current = [r["target_key"] for r in replicas]
    for target_key in fresh:
        resolved += resolve_alert(db, scope, "worker_stale", "worker", target_key)
    expired = [Alert.alert_type == "worker_stale", Alert.target_type == "worker", Alert.target_key.like(f"{name}:%")]
    if current:
        expired.append(Alert.target_key.not_in(sorted(current)))
    resolved += auto_resolve_where(db, scope, *expired)
    if any(now - r["at"] < timedelta(seconds=HEARTBEAT_ALIVE_SECONDS) for r in replicas):
        resolved += resolve_alert(db, scope, "worker_stale", "worker", name)
    return resolved


def evaluate_auto_resolve(
    db: Session, now: datetime, cfg: Mapping[str, Any], *, failing: set[str] | None = None, scope: DataScope = SYSTEM_SCOPE
) -> dict[str, int]:
    """⑤ 自动解决（``resolved_by=NULL``、``resolution_note='auto'``）：``link_deleted``（链接已 ``alive`` / ``changed``）、
    ``index_overdue``（``seo_indexed_any=1``）、``ai_task_failures``（同模型出现更新的成功尝试行）、``worker_stale``
    （``worker`` 已在 ④ 处理，此处补 ``monitor_worker`` 的恢复）；目标链接已不存在的链接告警一并解决。"""
    result = {
        "link_deleted": auto_resolve_where(
            db, scope, *_link_target_conditions("link_deleted", PublishLink.alive_status.in_(LINK_ALIVE_STATES))
        ),
        "index_overdue": auto_resolve_where(
            db, scope, *_link_target_conditions("index_overdue", PublishLink.seo_indexed_any == True)  # noqa: E712
        ),
    }
    if not scope.restricted:
        result["ai_task_failures"] = _resolve_ai_task_failures(db, failing or set())
        result["worker_stale"] = _resolve_monitor_worker_stale(db, now, cfg, scope)
    db.commit()
    return result


# ---------------------------------------------------------------- 入口


def evaluate(db: Session, scope: DataScope, *, now: datetime | None = None) -> dict[str, Any]:
    """``evaluate_alerts`` 的周期规则 ①~⑤（docs/11 §10.5），持 ``lock:monitor:evaluate_alerts``（300s）：
    ① ``index_overdue``；② ``ai_task_failures``；③ ``ai_breaker_open`` 兜底；④ ``worker_stale``（``worker``）；⑤ 自动解决。
    ``scope`` 为紧随 ``db`` 的必填参数（docs/11 §4.1 注、docs/13 §9.3）：``monitor_worker`` 传 ``SYSTEM_SCOPE``；受限范围只扫描 /
    解决其可见项目的链接告警（系统告警 ``project_id IS NULL`` 对受限范围不可见，不会被其解决）。

    各步骤独立提交，单步异常回滚并记日志、不影响其余步骤；锁被占用或 Redis 不可用 → ``{"skipped": True}``。
    返回 ``{index_overdue, ai_task_failures, ai_breaker_open, worker_stale, auto_resolved, auto_resolved_detail, errors}``。"""
    try:
        token = acquire_lock(EVALUATE_LOCK_KEY, EVALUATE_LOCK_TTL)
    except redis.RedisError as exc:
        logger.warning("evaluate_alerts 获取锁失败（Redis 不可用）: %s", exc)
        return {"skipped": True, "reason": "redis_unavailable"}
    if not token:
        logger.info("%s 被占用，本轮告警评估跳过", EVALUATE_LOCK_KEY)
        return {"skipped": True, "reason": "locked"}
    now = now or utcnow()
    result: dict[str, Any] = {
        "index_overdue": 0,
        "ai_task_failures": 0,
        "ai_breaker_open": 0,
        "worker_stale": 0,
        "auto_resolved": 0,
        "auto_resolved_detail": {},
        "errors": [],
    }
    failing: set[str] = set()

    def _step(label: str, fn: Any) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001 - 单步失败不影响其余步骤
            db.rollback()
            logger.exception("告警评估 %s 执行失败", label)
            result["errors"].append(label)
            return None

    try:
        cfg = _config(db)
        result["index_overdue"] = _step("index_overdue", lambda: evaluate_index_overdue(db, now, cfg, scope=scope)) or 0
        stale: dict[str, Any] = {}
        if not scope.restricted:  # ②③④ 是系统告警（project_id IS NULL），只在不受限范围（SYSTEM_SCOPE）下评估
            outcome = _step("ai_task_failures", lambda: evaluate_ai_task_failures(db, now, cfg, scope=scope))
            if outcome is not None:
                result["ai_task_failures"], failing = outcome
            result["ai_breaker_open"] = _step("ai_breaker_open", lambda: evaluate_breakers(db, cfg, scope=scope)) or 0
            stale = _step("worker_stale", lambda: evaluate_worker_stale(db, now)) or {}
        result["worker_stale"] = int(stale.get("worker_stale_raised") or 0)
        resolved = _step("auto_resolve", lambda: evaluate_auto_resolve(db, now, cfg, failing=failing, scope=scope)) or {}
        resolved = dict(resolved)
        if stale.get("worker_stale_resolved"):
            resolved["worker_stale"] = int(resolved.get("worker_stale") or 0) + int(stale["worker_stale_resolved"])
        result["auto_resolved_detail"] = resolved
        result["auto_resolved"] = sum(int(v or 0) for v in resolved.values())
    finally:
        try:
            release_lock(EVALUATE_LOCK_KEY, token)
        except redis.RedisError as exc:  # 锁 300s 自过期
            logger.warning("evaluate_alerts 释放锁失败: %s", exc)
    raised = sum(int(result[k] or 0) for k in EVALUATED_TYPES)
    if raised or result["auto_resolved"] or result["errors"]:
        logger.info("告警评估完成：%s", result)
    return result


__all__ = [
    "ACTIVE_STATUSES",
    "EmailChannel",
    "WebhookChannel",
    "acknowledge",
    "alert_detail_url",
    "alert_item",
    "auto_resolve_where",
    "batch_resolve",
    "dedupe_key_for",
    "deliver",
    "evaluate",
    "evaluate_ai_task_failures",
    "evaluate_auto_resolve",
    "evaluate_breakers",
    "evaluate_index_overdue",
    "evaluate_worker_stale",
    "get_alert",
    "has_active",
    "ignore",
    "list_alerts",
    "raise_alert",
    "resolve",
    "resolve_alert",
    "summary",
]
