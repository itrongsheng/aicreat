"""生成批次编排（docs/09 §2.2、§2.3、§6.1、§6.2、§7.2、§9；docs/03 B.9「冗余计数回写」「任务幂等与状态收敛」；docs/04 §6.12）。

职责：

- 生成接口的公共校验链（docs/09 §6.1 校验顺序）：项目可见且 ``active``（404 / 409）→ 字段校验（含 ``model?`` 覆盖，400
  ``data={"model":…}``）→ 全局暂停（5031 ``paused_reason``）→ 频控 ``rate:generate:{admin_id}``（429 ``retry_after``）→ 模板解析与
  必填变量检查（404 / 400 / 4221）→ ``preflight``（路由 / 熔断快照，5031）→ 额度预占（4291）；
- ``create_batch``：同一事务创建 ``generation_batches`` 与各单元的根任务（``(batch_id, target_type, target_id, operation)`` 去重）、
  逐个 ``check_quota`` 预占，提交后 ``RPUSH queue:ai_tasks``；关键词（1 个根任务）、标题（每关键词一个）、内容（每标题一个，
  第 6 步内容阶段经 ``before_commit`` 写 ``contents``）共用；
- 收敛 ``on_task_finished(batch_id)``：根任务终态提交后调用一次，持 ``lock:generation_batch:{batch_id}``（60s）按数据库重算
  ``task_done`` / ``task_failed`` / ``produced_count`` / ``error_summary``，计数齐全时收敛 ``succeeded`` / ``partial`` / ``failed``；
  ``recompute_batch(db, batch_id)`` 供 ``recover_stale_tasks`` ④ 在已持锁时兜底收敛（只写库、不提交）；
- 批次取消 / 重试与列表 / 详情（``/admin/generation-batches*``，按 ``project_id IN P`` 过滤）。

收敛口径（与 ``recover_stale_tasks.recompute_batch_fallback`` 相同）：``task_failed`` 只计 ``failed`` / ``cancelled`` / ``expired`` 且
不存在重试子任务（``parent_task_id`` 指向它的根任务）的根任务，因此批次 ``retry``、单任务 ``retry``、① 自动重试的
``task_failed −1`` 与按库重算结果一致；``produced_count`` 关键词 / 标题批次统计 ``ai_task_id`` 指向本批次尝试行的业务行数，
内容批次为 ``content_generate`` 的 ``succeeded`` 根任务数；``error_summary`` 汇总根任务 ``response_meta_json.apply_counts`` 与
失败根任务的 ``error_category``（``stale_after_submit`` 标记单独计数）。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.exceptions import (
    CODE_CONFLICT,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.locks import LockTimeout, with_lock
from app.core.ratelimit import check_rate_limit
from app.core.zhiqi import text as zhiqi_text
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import ErrorCategory, TextResult
from app.models import (
    AiTask,
    GenerationBatch,
    Keyword,
    Project,
    PromptTemplate,
    Title,
    utcnow,
)
from app.schemas.common import iso_utc
from app.services import ai_gateway_service as gateway
from app.services import ai_task_service, settings_service
from app.services import prompt_template_service as pts
from app.services.ai_gateway_service import ResolvedRoute
from app.services.data_scope_service import (
    DataScope,
    get_visible,
    require_project,
    scope_by_project,
)

logger = logging.getLogger(__name__)

__all__ = [
    "APPLY_COUNT_KEYS",
    "BATCH_LOCK_PREFIX",
    "BatchUnit",
    "GenerationPlan",
    "batch_created_response",
    "batch_detail",
    "batch_item",
    "brand_info_text",
    "cancel_batch",
    "check_generate_rate_limit",
    "count_limit_error",
    "create_batch",
    "empty_apply_counts",
    "ensure_not_paused",
    "estimate_call",
    "get_batch",
    "list_batches",
    "on_task_finished",
    "prepare_generation",
    "project_variables",
    "recompute_batch",
    "retry_batch",
    "run_json_list",
    "task_summary",
    "task_template",
    "write_apply_counts",
]

BATCH_LOCK_PREFIX = "lock:generation_batch:"
BATCH_LOCK_TTL = 60
BATCH_LOCK_WAIT_SECONDS = 30
RATE_KEY_PREFIX = "rate:generate:"
DEFAULT_GENERATE_RATE = "60/hour"
ERROR_SUMMARY_MAX = 1000
APPLY_COUNT_KEYS: tuple[str, ...] = ("duplicates", "invalid", "intent_missing", "empty_output", "too_long")
FAILED_ROOT_STATUSES = ("failed", "cancelled", "expired")
ACTIVE_ROOT_STATUSES = ("queued", "running", "polling")
CONVERGE_FROM = ("queued", "running")
MSG_BATCH_NOT_CANCELLABLE = "当前批次状态不可取消"
MSG_BATCH_NOT_RETRYABLE = "当前批次没有可重试的失败任务"
MSG_PAUSED = "AI 调用已暂停"


# =====================================================================
# 生成接口的公共校验链（docs/09 §6.1）
# =====================================================================


def ensure_not_paused(capability: str) -> None:
    """全局暂停（``ai:paused:*``）→ 5031 ``data.paused_reason``（校验链中位于频控之前）。"""
    reason = gateway.check_paused()
    if reason:
        raise gateway._unavailable_error(
            str(capability), paused_reason=reason, error_category=reason, error_message=MSG_PAUSED,
        )


def check_generate_rate_limit(db: Session, admin_id: int) -> None:
    """``rate:generate:{admin_id}``（``generation_config.rate_limits.generate_per_admin``，默认 ``60/hour``），每个 HTTP 请求计 1；
    超限 429 ``data={"retry_after":秒}``。"""
    cfg = settings_service.get_config(db, "generation_config")
    rate = settings_service.get_path(cfg, "rate_limits.generate_per_admin") or DEFAULT_GENERATE_RATE
    check_rate_limit(f"{RATE_KEY_PREFIX}{admin_id}", str(rate))


@dataclass
class GenerationPlan:
    """校验链通过后的生成计划：项目、模板（快照 ID / 版本）、已解析的路由（候选链首个模型用于额度估算）。"""

    project: Project
    template: PromptTemplate
    route: ResolvedRoute
    model_override: str | None


def prepare_generation(
    db: Session,
    scope: DataScope,
    *,
    admin_id: int,
    project_id: int,
    capability: str,
    kind: str,
    template_id: int | None = None,
    model: str | None = None,
    validate: Callable[[Project], None] | None = None,
    variables: Mapping[str, Any] | None = None,
) -> GenerationPlan:
    """按 docs/09 §6.1 的顺序执行校验链，返回生成计划（不写库、不预占额度）：

    项目可见（404）且 ``active``（409 ``current_status``）→ ``validate(project)``（需要查库的字段校验，400）→ ``model?``
    覆盖（400 ``data={"model":…}``）→ 全局暂停（5031）→ 频控（429）→ 模板（``template_id`` 不可见 404 ``data=null``、不合规 400、
    缺省解析为空 404 ``data.kind``）与必填自定义变量（4221）→ ``preflight``（5031，覆盖模型熔断附 ``hint``）。
    """
    project = require_project(db, scope, project_id, active=True)
    if validate is not None:
        validate(project)
    override = (model or "").strip() or None
    gateway.validate_model_override(db, capability, override)
    ensure_not_paused(capability)
    check_generate_rate_limit(db, admin_id)
    template = pts.template_for_generation(db, scope, kind=kind, project=project, template_id=template_id)
    pts.check_required_variables(template, variables or {})
    route = gateway.preflight(db, capability, project.id, model_override=override)
    return GenerationPlan(project=project, template=template, route=route, model_override=override)


def brand_info_text(project: Project | None) -> str:
    """模板变量 ``brand_info``：``brand_name`` 非空时拼为 ``品牌：{brand_name}。{brand_info}``（docs/09 §4.1）。"""
    if project is None:
        return ""
    name = (project.brand_name or "").strip()
    info = (project.brand_info or "").strip()
    if name:
        return f"品牌：{name}。{info}".strip()
    return info


def project_variables(project: Project | None, *, audience: str | None = None) -> dict[str, Any]:
    """全部 kind 共用的内置变量：``language`` / ``industry`` / ``audience``（请求体优先）/ ``brand_info``（空值由 ``render``
    渲染为 ``（未提供）``）。"""
    return {
        "language": (project.language if project is not None else None) or pts.FALLBACK_LANGUAGE,
        "industry": (project.industry if project is not None else None) or "",
        "audience": (audience or "").strip() or ((project.audience if project is not None else None) or ""),
        "brand_info": brand_info_text(project),
    }


def estimate_call(db: Session, plan_route: ResolvedRoute, template: PromptTemplate, variables: Mapping[str, Any],
                  *, dynamic_params: Mapping[str, Any] | None = None, calls: int = 1) -> int:
    """一次（或 ``calls`` 次）调用的预占额度：``estimate_for(候选链首个模型, estimate_tokens(渲染后 prompt), params.max_tokens)``；
    ``params`` 按 docs/09 §5.8 合成（路由 ``params_json`` ← 模板 ``model_params_json`` ← 场景动态值）。"""
    system, user = pts.render(template, variables)
    params = {**plan_route.params, **pts.call_params(template, dynamic_params)}
    completion = int(params.get("max_tokens") or 0)
    prompt = "\n".join(part for part in (system, user) if part)
    return gateway.estimate_route(db, plan_route, prompt=prompt, completion_tokens=completion, calls=calls)


# =====================================================================
# 建批次与根任务
# =====================================================================


@dataclass
class BatchUnit:
    """批次内的一个业务单元（一个根任务）。``target_id=None`` 表示指向批次自身（``target_type=generation_batch``）。"""

    target_type: str
    target_id: int | None
    input: dict[str, Any]
    estimated_quota: int = 0
    template_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)   # 调用方自用（如内容阶段的 content 对象），不写库


def _release(db: Session, reservations: Iterable[tuple[int | None, int]]) -> None:
    for project_id, amount in reservations:
        if amount > 0:
            gateway.release_reservation(db, AiTask(project_id=project_id, quota_reserved=amount))


def create_batch(
    db: Session,
    *,
    project: Project,
    kind: str,
    capability: str,
    operation: str,
    template: PromptTemplate,
    route: ResolvedRoute,
    batch_input: Mapping[str, Any],
    requested_count: int,
    units: Sequence[BatchUnit],
    created_by: int,
    before_commit: Callable[[GenerationBatch, list[tuple[BatchUnit, AiTask]]], None] | None = None,
) -> GenerationBatch:
    """同一事务创建批次（``status=queued``、``input_json``、模板 ID / 版本快照、``requested_count``、``task_total``）与各单元的根任务
    （``trigger_type=user``、``status=queued``、``model`` = 覆盖模型或候选链首个模型、``input_json`` = 单元输入），逐个
    ``check_quota`` 预占 ``estimated_quota``；同一 ``(target_type, target_id, operation)`` 只建一个根任务。``before_commit``
    在提交前调用（内容阶段写 ``contents.batch_id`` / ``ai_task_id`` 等）。提交后各根任务 ``RPUSH queue:ai_tasks``；任一步失败
    整体回滚并撤销已预占的额度。"""
    reservations: list[tuple[int | None, int]] = []
    try:
        batch = GenerationBatch(
            project_id=project.id,
            kind=kind,
            status="queued",
            input_json=gateway._dumps(dict(batch_input)),
            template_id=template.id,
            template_version=template.version,
            requested_count=max(0, int(requested_count)),
            task_total=0,
            created_by=created_by,
        )
        db.add(batch)
        db.flush()
        created: list[tuple[BatchUnit, AiTask]] = []
        seen: set[tuple[str, int]] = set()
        for unit in units:
            target_id = unit.target_id if unit.target_id is not None else batch.id
            key = (unit.target_type, int(target_id))
            if key in seen:
                continue
            seen.add(key)
            task = gateway.create_root_task(
                db, capability=capability, operation=operation, project_id=project.id, created_by=created_by,
                trigger_type="user", target_type=unit.target_type, target_id=target_id, batch_id=batch.id,
                template_id=unit.template_id or template.id, input=unit.input, route=route,
            )
            if unit.estimated_quota > 0:
                gateway.check_quota(db, project_id=project.id, estimated_quota=unit.estimated_quota, task=task)
                reservations.append((project.id, int(unit.estimated_quota)))
            created.append((unit, task))
        batch.task_total = len(created)
        if before_commit is not None:
            before_commit(batch, created)
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        _release(db, reservations)
        raise
    return batch


def batch_created_response(db: Session, batch: GenerationBatch, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """生成接口响应 ``{batch_id, …, quota_warning?}``：全部根任务预占完成后按累计用量判断预警（一次请求至多一个）。"""
    data: dict[str, Any] = {"batch_id": batch.id, **dict(extra or {})}
    warning = gateway.quota_warning(db, project_id=batch.project_id)
    if warning:
        data["quota_warning"] = warning
    return data


# =====================================================================
# worker 侧公共工具（keyword_generate / title_generate / content_*）
# =====================================================================


def task_template(db: Session, task: AiTask, kind: str) -> PromptTemplate:
    """根任务使用的模板：``ai_tasks.template_id`` 快照（同一根任务只解析一次，模板此后被归档也按快照执行）；
    快照缺失或 kind 不一致时按项目重新 ``resolve_template``。"""
    if task.template_id:
        tpl = db.get(PromptTemplate, task.template_id)
        if tpl is not None and tpl.kind == kind:
            return tpl
    tpl = pts.resolve_template(db, kind, task.project_id)
    task.template_id = tpl.id
    return tpl


def run_json_list(
    ctx: ai_task_service.TaskContext,
    template: PromptTemplate,
    variables: Mapping[str, Any],
    *,
    required_key: str,
    dynamic_params: Mapping[str, Any] | None = None,
    segment_index: int | None = None,
) -> tuple[list[dict[str, Any]], AiTask, TextResult]:
    """渲染模板并调用 ``complete_text``，输出按 JSON 数组校验（docs/09 §5.5）：``output_format=json`` 时网关以
    ``response_format="json"`` 调用并按 ``output_schema_json`` 校验；无论模板 schema 是否完整，都再检查结构（顶层数组、
    每项为对象且含 ``required_key``），不满足按 ``invalid_response`` 由网关同模型重新生成 1 次。返回 ``(items, 成功尝试行, 结果)``。"""
    system, user = pts.render(template, variables)
    base = pts.output_validator(template)

    def _validate(result: TextResult) -> list[dict[str, Any]]:
        data = base(result) if base is not None else zhiqi_text.extract_json(result.text)
        if not isinstance(data, list):
            raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：$ 应为数组")
        for index, item in enumerate(data):
            if not isinstance(item, dict) or required_key not in item:
                raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, f"模型输出不符合约定：$[{index}].{required_key} 缺少必填字段")
        return data

    result, attempt = ctx.complete_text(
        messages=pts.messages_for(system, user),
        params=pts.call_params(template, dynamic_params),
        response_format=pts.response_format_for(template),
        metadata=pts.call_metadata(template, ctx.task.operation),
        validator=_validate,
        segment_index=segment_index,
    )
    items = getattr(result, "parsed", None)
    if items is None:
        items = _validate(result)
    return list(items), attempt, result


def empty_apply_counts() -> dict[str, int]:
    return dict.fromkeys(APPLY_COUNT_KEYS, 0)


def write_apply_counts(root_task: AiTask, counts: Mapping[str, int]) -> dict[str, int]:
    """把本根任务的分项计数写入根任务 ``response_meta_json.apply_counts``（与根任务终态同一事务，docs/09 §6.2 第 5 步）。"""
    data = empty_apply_counts()
    for key, value in counts.items():
        if key in data:
            data[key] = max(0, int(value or 0))
    gateway.update_task_meta(root_task, apply_counts=data)
    return data


# =====================================================================
# 收敛（docs/09 §9.3）
# =====================================================================


def _apply_counts(root: AiTask) -> dict[str, int]:
    counts = gateway.task_meta(root).get("apply_counts")
    if not isinstance(counts, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in counts.items():
        try:
            result[str(key)] = int(value or 0)
        except (TypeError, ValueError):
            continue
    return result


def _children_of(db: Session, root_ids: Sequence[int]) -> set[int]:
    if not root_ids:
        return set()
    return set(
        db.scalars(
            select(AiTask.parent_task_id).where(AiTask.parent_task_id.in_(list(root_ids)), AiTask.root_task_id.is_(None))
        ).all()
    )


def _produced_count(db: Session, batch: GenerationBatch, roots: Sequence[AiTask]) -> int:
    if batch.kind in ("keyword", "title"):
        model = Keyword if batch.kind == "keyword" else Title
        attempts = select(AiTask.id).where(AiTask.batch_id == batch.id, AiTask.root_task_id.is_not(None))
        return int(db.scalar(select(func.count(model.id)).where(model.ai_task_id.in_(attempts))) or 0)
    return sum(1 for r in roots if r.status == "succeeded" and r.operation == "content_generate")


def _error_summary(roots: Sequence[AiTask], failed: Sequence[AiTask]) -> str | None:
    items: Counter[str] = Counter()
    for root in roots:
        for key, value in _apply_counts(root).items():
            items[key] += value
    failures: Counter[str] = Counter()
    for root in failed:
        if gateway.task_meta(root).get("stale_after_submit"):
            failures["stale_after_submit"] += 1
        else:
            failures[root.error_category or ("cancelled" if root.status == "cancelled" else "unknown")] += 1
    ordered = [k for k in APPLY_COUNT_KEYS if items.get(k)] + sorted(k for k in items if k not in APPLY_COUNT_KEYS and items[k])
    parts = [f"{k}={items[k]}" for k in ordered] + [f"{k}={v}" for k, v in sorted(failures.items()) if v]
    return ";".join(parts)[:ERROR_SUMMARY_MAX] or None


def _recompute(db: Session, batch: GenerationBatch, *, force: bool) -> str:
    """按库重算并（计数齐全或 ``force`` 且全部根任务终态时）收敛；只写库、不提交。"""
    now = utcnow()
    roots = list(db.scalars(select(AiTask).where(AiTask.batch_id == batch.id, AiTask.root_task_id.is_(None))).all())
    with_child = _children_of(db, [r.id for r in roots])
    succeeded = [r for r in roots if r.status == "succeeded"]
    failed = [r for r in roots if r.status in FAILED_ROOT_STATUSES and r.id not in with_child]
    batch.task_done = len(succeeded)
    batch.task_failed = len(failed)
    batch.produced_count = _produced_count(db, batch, roots)
    batch.error_summary = _error_summary(roots, failed)
    batch.heartbeat_at = now
    complete = batch.task_done + batch.task_failed >= int(batch.task_total or 0)
    all_terminal = bool(roots) and not any(r.status in ACTIVE_ROOT_STATUSES for r in roots)
    if batch.status in CONVERGE_FROM and (complete or (force and all_terminal)):
        if batch.task_failed == 0:
            batch.status = "succeeded"
        elif batch.task_done == 0:
            batch.status = "failed"
        else:
            batch.status = "partial"
        batch.finished_at = now
    db.flush()
    return batch.status


def recompute_batch(db: Session, batch_id: int) -> str | None:
    """``recover_stale_tasks`` ④ 的批次兜底（调用方已持 ``lock:generation_batch:{batch_id}``，只写库、不提交）：按库重算
    ``task_done`` / ``task_failed`` / ``produced_count`` / ``error_summary`` 后立即收敛，返回批次状态。"""
    batch = db.get(GenerationBatch, batch_id)
    if batch is None:
        return None
    return _recompute(db, batch, force=True)


def on_task_finished(batch_id: int) -> str | None:
    """根任务终态提交后调用一次（``ai_task_service`` 收敛钩子）：持 ``lock:generation_batch:{batch_id}``（60s）在独立会话内按库
    重算计数，``task_done + task_failed == task_total`` 时收敛并写 ``finished_at``。重复调用结果相同；锁等待超时只记日志
    （由 ``recover_stale_tasks`` ④ 兜底）。返回批次状态。"""
    if not batch_id:
        return None
    key = f"{BATCH_LOCK_PREFIX}{batch_id}"
    try:
        with with_lock(key, BATCH_LOCK_TTL, wait_seconds=BATCH_LOCK_WAIT_SECONDS):
            with SessionLocal() as db:
                try:
                    batch = db.get(GenerationBatch, batch_id)
                    if batch is None:
                        return None
                    status = _recompute(db, batch, force=False)
                    db.commit()
                    return status
                except Exception:
                    db.rollback()
                    raise
    except LockTimeout:
        logger.warning("批次 %s 收敛未取得锁（由 recover_stale_tasks ④ 兜底）", batch_id)
        return None


ai_task_service.set_batch_finished_hook(on_task_finished)


# =====================================================================
# 列表 / 详情（/admin/generation-batches）
# =====================================================================


def batch_item(batch: GenerationBatch) -> dict[str, Any]:
    """批次对象（docs/04 §7.4）；``input`` 为解码后的 ``input_json``。"""
    raw = gateway._loads(batch.input_json, {})
    return {
        "id": batch.id,
        "project_id": batch.project_id,
        "kind": batch.kind,
        "status": batch.status,
        "input": raw if isinstance(raw, dict) else {},
        "template_id": batch.template_id,
        "template_version": batch.template_version,
        "requested_count": batch.requested_count,
        "produced_count": batch.produced_count,
        "task_total": batch.task_total,
        "task_done": batch.task_done,
        "task_failed": batch.task_failed,
        "error_summary": batch.error_summary,
        "started_at": iso_utc(batch.started_at),
        "finished_at": iso_utc(batch.finished_at),
        "heartbeat_at": iso_utc(batch.heartbeat_at),
        "created_by": batch.created_by,
        "created_at": iso_utc(batch.created_at),
        "updated_at": iso_utc(batch.updated_at),
    }


def task_summary(root: AiTask, attempts: Sequence[AiTask]) -> dict[str, Any]:
    """根任务摘要（批次详情 ``tasks[]``，docs/04 §7.4、§6.0）：``attempt_count`` 为尝试行数；``last_error_*`` 在根任务失败 / 取消
    时取根任务的错误，执行中取最近一次失败尝试行，成功时为 ``null``；``model_override`` 取根任务 ``input.model``。"""
    if root.status in FAILED_ROOT_STATUSES:
        last_category, last_message = root.error_category, root.error_message
    elif root.status in ACTIVE_ROOT_STATUSES:
        failed = [a for a in attempts if a.status == "failed"]
        last = failed[-1] if failed else None
        last_category = last.error_category if last else None
        last_message = last.error_message if last else None
    else:
        last_category = last_message = None
    override = gateway.task_input(root).get("model")
    return {
        "id": root.id,
        "operation": root.operation,
        "target_type": root.target_type,
        "target_id": root.target_id,
        "parent_task_id": root.parent_task_id,
        "status": root.status,
        "model": root.model,
        "model_override": str(override) if override else None,
        "protocol": root.protocol,
        "candidate_index": root.candidate_index,
        "attempt_count": len(attempts),
        "last_error_category": last_category,
        "last_error_message": last_message,
        "request_id": root.request_id,
        "progress": root.progress,
        "started_at": iso_utc(root.started_at),
        "finished_at": iso_utc(root.finished_at),
    }


def _batch_tasks(db: Session, batch_id: int) -> list[dict[str, Any]]:
    roots = list(
        db.scalars(select(AiTask).where(AiTask.batch_id == batch_id, AiTask.root_task_id.is_(None)).order_by(AiTask.id)).all()
    )
    if not roots:
        return []
    attempts: dict[int, list[AiTask]] = {r.id: [] for r in roots}
    for attempt in db.scalars(
        select(AiTask).where(AiTask.root_task_id.in_(list(attempts))).order_by(AiTask.id)
    ).all():
        attempts.setdefault(int(attempt.root_task_id or 0), []).append(attempt)
    return [task_summary(r, attempts.get(r.id, [])) for r in roots]


def get_batch(db: Session, scope: DataScope, batch_id: int) -> GenerationBatch:
    """可见批次（按所属项目判断，不可见与不存在一律 404）。"""
    return get_visible(db, scope, GenerationBatch, batch_id)


def batch_detail(db: Session, scope: DataScope, batch_id: int) -> dict[str, Any]:
    """``GET /admin/generation-batches/{id}``：批次 + ``tasks[]``（根任务摘要，按 ID 升序，含重试根任务）。"""
    batch = get_batch(db, scope, batch_id)
    item = batch_item(batch)
    item["tasks"] = _batch_tasks(db, batch.id)
    return item


def list_batches(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    project_id: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    created_by: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """分页（``created_at DESC, id DESC``）；按 ``project_id IN P`` 过滤，``project_id`` 指向不可见项目时为空结果。"""
    conditions: list[Any] = []
    for column, value in (
        (GenerationBatch.project_id, project_id), (GenerationBatch.kind, kind), (GenerationBatch.status, status),
        (GenerationBatch.created_by, created_by),
    ):
        if value is not None and value != "":
            conditions.append(column == value)
    stmt = scope_by_project(select(GenerationBatch), GenerationBatch.project_id, scope).where(*conditions)
    count_stmt = scope_by_project(select(func.count(GenerationBatch.id)), GenerationBatch.project_id, scope).where(*conditions)
    total = int(db.scalar(count_stmt) or 0)
    rows = db.scalars(
        stmt.order_by(GenerationBatch.created_at.desc(), GenerationBatch.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return [batch_item(r) for r in rows], total


# =====================================================================
# 取消 / 重试（docs/09 §9.7）
# =====================================================================


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def cancel_batch(db: Session, scope: DataScope, batch_id: int, *, admin_id: int | None = None) -> dict[str, Any]:
    """``queued`` / ``running → cancelled``：``queued`` 根任务置 ``cancelled``（``error_category=cancelled``，``settle_quota`` 释放
    预占；注册了 ``on_cancelled`` 的处理器同事务恢复业务对象，如内容恢复 ``prev_status``）；``running`` 根任务不打断，完成后由
    ``ai_task_service`` 复查到批次已取消 → ``cancelled``、不写业务对象。提交后按库重算计数（批次保持 ``cancelled``）。"""
    del admin_id
    batch = get_batch(db, scope, batch_id)
    if batch.status not in ("queued", "running"):
        raise _conflict(MSG_BATCH_NOT_CANCELLABLE, {"current_status": batch.status})
    ai_task_service.load_handlers()
    now = utcnow()
    try:
        batch.status = "cancelled"
        batch.finished_at = now
        batch.heartbeat_at = now
        queued = list(
            db.scalars(
                select(AiTask).where(AiTask.batch_id == batch.id, AiTask.root_task_id.is_(None), AiTask.status == "queued")
                .order_by(AiTask.id)
            ).all()
        )
        for root in queued:
            # 与 worker claim（UPDATE … WHERE status='queued'）互斥：条件更新失败说明已被领取，按运行中处理
            result = db.execute(
                update(AiTask).where(AiTask.id == root.id, AiTask.status == "queued").values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            if not result.rowcount:
                continue
            spec = ai_task_service.get_handler(root.operation)
            if spec is not None and spec.on_cancelled is not None:
                spec.on_cancelled(db, root)
            gateway.finalize_root(db, root, "cancelled")
        db.commit()
    except Exception:
        db.rollback()
        raise
    on_task_finished(batch.id)
    db.expire_all()
    return batch_detail(db, scope, batch.id)


def retry_batch(db: Session, scope: DataScope, batch_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``partial`` / ``failed → running``：只为尚无重试子任务的 ``failed`` 根任务新建重试根任务（``parent_task_id``，复制
    ``project_id`` / ``capability`` / ``operation`` / ``target_type`` / ``target_id`` / ``batch_id`` / ``template_id`` / ``input_json``，
    重新 ``check_quota``），每新建一个同事务 ``task_failed −1``、``task_total`` 不变、批次置 ``running``；注册了 ``before_retry`` 的
    处理器校验业务状态（如内容须为 ``draft`` / ``ready`` / ``rejected``），不满足的单元跳过、原根任务保持 ``failed``；
    ``cancelled`` 根任务不重试。批次状态不符或无可重试根任务 → 409 ``data={"current_status": 批次状态}``。返回批次详情。"""
    batch = get_batch(db, scope, batch_id)
    if batch.status not in ("partial", "failed"):
        raise _conflict(MSG_BATCH_NOT_RETRYABLE, {"current_status": batch.status})
    roots = list(
        db.scalars(
            select(AiTask).where(AiTask.batch_id == batch.id, AiTask.root_task_id.is_(None), AiTask.status == "failed")
            .order_by(AiTask.id)
        ).all()
    )
    with_child = _children_of(db, [r.id for r in roots])
    candidates = [r for r in roots if r.id not in with_child]
    if not candidates:
        raise _conflict(MSG_BATCH_NOT_RETRYABLE, {"current_status": batch.status})
    ai_task_service.load_handlers()
    reservations: list[tuple[int | None, int]] = []
    created: list[int] = []
    routes: dict[tuple[str, str | None], ResolvedRoute] = {}
    try:
        for old in candidates:
            input_data = gateway.task_input(old)
            override = (str(input_data.get("model") or "").strip() or None)
            route_key = (old.capability, override)
            if route_key not in routes:
                routes[route_key] = gateway.preflight(db, old.capability, old.project_id, model_override=override)
            spec = ai_task_service.get_handler(old.operation)
            savepoint = db.begin_nested()
            try:
                new = gateway.create_root_task(
                    db, capability=old.capability, operation=old.operation, project_id=old.project_id, created_by=admin_id,
                    trigger_type="user", target_type=old.target_type, target_id=old.target_id, batch_id=old.batch_id,
                    template_id=old.template_id, input=input_data, route=routes[route_key], parent_task_id=old.id,
                    enqueue=False,
                )
                if spec is not None and spec.before_retry is not None:
                    spec.before_retry(db, old, new)
                savepoint.commit()
            except BusinessError as exc:
                savepoint.rollback()
                logger.info("批次 %s 跳过根任务 %s 的重试：%s", batch.id, old.id, exc.message)
                continue
            if int(old.quota_reserved or 0) > 0:
                gateway.check_quota(db, project_id=old.project_id, estimated_quota=int(old.quota_reserved), task=new)
                reservations.append((old.project_id, int(old.quota_reserved)))
            created.append(new.id)
        if not created:
            db.rollback()
            raise _conflict(MSG_BATCH_NOT_RETRYABLE, {"current_status": batch.status})
        batch.task_failed = max(0, int(batch.task_failed or 0) - len(created))
        batch.status = "running"
        batch.finished_at = None
        batch.heartbeat_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        _release(db, reservations)
        raise
    for task_id in created:
        gateway.enqueue_task(task_id)
    return batch_detail(db, scope, batch.id)


# =====================================================================
# 400 校验错误的小工具（keyword / title / content 的生成接口共用）
# =====================================================================


def count_limit_error(count: int, maximum: int, loc: Sequence[str | int] = ("body", "count")) -> BusinessError | None:
    """``count`` 超过配置上限 → 400（``type=less_than_equal``，与 Pydantic 内置类型一致）。"""
    if int(count) > int(maximum):
        return invalid_params(field_error(list(loc), f"不能大于 {maximum}", "less_than_equal", count))
    return None
