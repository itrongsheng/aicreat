"""标题（docs/09 §7；docs/03 B.11「冗余计数回写」「关键词与标题」「删除规则」；docs/04 §6.10、§7.5；docs/13 §6.3、§7.1、§7.5）。

- ``STYLE_GUIDE``：``content_style`` → ``(标签, 写作要求)``，模板变量 ``style`` 渲染为 ``标签：写作要求``（§7.1）；
- 去重键 ``title_dedup_key``：Unicode NFKC → 去掉标点（Unicode 类别 ``P*``）与空白 → 小写，**不**剥离站点后缀（不复用
  ``fingerprint.normalize_title``）；生成写入去重与内容质量标记 ``duplicate_title`` 共用；
- 生成：``generate_titles``（API：每个关键词一个 ``title_generate`` 根任务，各自预占额度）与 worker 处理器
  ``run_title_generate``（渲染 ``sys_title`` → JSON → ``apply_generated``：清洗、同关键词去重、插入并同事务
  ``keywords.title_count += n``，分项计数写根任务 ``response_meta_json.apply_counts``）；
- 手工新增 / 编辑（首次编辑保留 ``original_title``）/ 打分；状态 ``adopt``（要求关键词已 ``adopted``，否则 409
  ``current_status`` = 关键词状态、「关键词未采用」）/ ``discard`` / ``restore`` / ``batch-status``；删除仅 ``content_count=0``，
  同事务 ``keywords.title_count −= 1``；
- 实时计数（docs/12 §4.6）：标题插入 ``titles_created``、采用 ``titles_adopted``。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import (
    CODE_BAD_REQUEST,
    CODE_CONFLICT,
    BusinessError,
    field_error,
    invalid_params,
)
from app.core.zhiqi.errors import ZhiqiError
from app.core.zhiqi.types import ErrorCategory
from app.models import (
    AiTask,
    ContentStyle,
    GenerationBatch,
    Keyword,
    Project,
    Title,
    enum_values,
    utcnow,
)
from app.schemas.common import iso_utc
from app.schemas.title import TITLE_MAX_CHARS
from app.services import ai_gateway_service as gateway
from app.services import (
    ai_task_service,
    generation_service,
    settings_service,
    stats_service,
)
from app.services.data_scope_service import (
    DataScope,
    get_visible,
    is_visible,
    require_project,
    scope_by_project,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONTENT_STYLES",
    "STYLE_GUIDE",
    "adopt_title",
    "apply_generated",
    "batch_status",
    "change_status",
    "clean_title",
    "create_title",
    "delete_title",
    "discard_title",
    "generate_titles",
    "get_title",
    "get_title_row",
    "list_titles",
    "restore_title",
    "run_title_generate",
    "score_title",
    "style_text",
    "title_dedup_key",
    "title_item",
    "update_title",
]

CONTENT_STYLES: tuple[str, ...] = enum_values(ContentStyle)

# docs/09 §7.1（权威）：code → (标签, 写作要求)
STYLE_GUIDE: dict[str, tuple[str, str]] = {
    "news": ("资讯", "客观陈述，突出时效与要点，首段给结论"),
    "tutorial": ("教程", "步骤清晰、可操作，使用编号列表与前置条件说明"),
    "review": ("测评", "对比维度明确，给出优缺点与适用人群"),
    "qa": ("问答", "以问题驱动，每节回答一个具体问题"),
    "recommend": ("种草", "场景化描述与真实感受，弱化硬广，结尾给购买/使用建议"),
    "listicle": ("清单", "N 个并列条目，每条有小标题与一句话摘要"),
    "story": ("故事", "有人物、冲突与转折，结尾回扣主题"),
}

AI_SCORE_QUANT = Decimal("0.1")
SCORE_MIN, SCORE_MAX = Decimal(0), Decimal(10)
TITLE_NOT_FOUND = "标题不存在"
KEYWORD_NOT_FOUND = "关键词不存在"
MSG_STATUS_CONFLICT = "当前状态不允许该操作"
MSG_KEYWORD_NOT_ADOPTED = "关键词未采用"
MSG_IN_USE = "标题已有关联内容，不能删除"
MSG_INVALID_KEYWORD = "关键词已弃用或不属于该项目"

_WS_RE = re.compile(r"\s+")
# 成对引号（去掉首尾成对出现的引号 / 书名式引号）
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ("《", "》"), ("〝", "〞"), ("＂", "＂"),
)

TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "adopt": (("candidate",), "adopted"),
    "discard": (("candidate", "adopted"), "discarded"),
    "restore": (("discarded",), "candidate"),
}


# =====================================================================
# 风格、去重键与清洗（docs/09 §7.1、§7.3）
# =====================================================================


def style_text(style: str | None) -> str:
    """``content_style`` code → ``标签：写作要求``；未知 code 原样返回。"""
    if not style:
        return ""
    guide = STYLE_GUIDE.get(style)
    return f"{guide[0]}：{guide[1]}" if guide else str(style)


def title_dedup_key(title: str | None) -> str:
    """标题去重键：Unicode NFKC → 去掉标点（Unicode 类别 ``P*``）与空白 → 小写；**不**剥离站点后缀（「智能门锁选购指南 - 新手
    必看」与「智能门锁选购指南 - 避坑大全」不相同）。与前端 ``normalize("NFKC")`` + ``/[\\p{P}\\s]/gu`` + ``toLowerCase()`` 一致。"""
    value = unicodedata.normalize("NFKC", title or "")
    kept = [ch for ch in value if not ch.isspace() and not unicodedata.category(ch).startswith("P")]
    return "".join(kept).lower()


def clean_title(text: Any) -> str:
    """去首尾空白与成对引号、折叠空白（不截断；超过 200 字符由调用方处理）。"""
    if text is None:
        return ""
    value = _WS_RE.sub(" ", str(text)).strip()
    changed = True
    while changed and len(value) >= 2:
        changed = False
        for left, right in _QUOTE_PAIRS:
            if value.startswith(left) and value.endswith(right) and len(value) >= len(left) + len(right):
                value = value[len(left) : len(value) - len(right)].strip()
                changed = True
                break
    return value


def _ai_score(value: Any) -> Decimal | None:
    """``ai_score`` 钳制到 0~10、保留一位小数；缺失 / 非数值为 NULL。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    number = max(SCORE_MIN, min(SCORE_MAX, number))
    return number.quantize(AI_SCORE_QUANT, rounding=ROUND_HALF_UP)


# =====================================================================
# 序列化与查询
# =====================================================================


def _num(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def title_item(title: Title) -> dict[str, Any]:
    """标题对象（docs/04 §7.5）。"""
    return {
        "id": title.id,
        "project_id": title.project_id,
        "keyword_id": title.keyword_id,
        "title": title.title,
        "original_title": title.original_title,
        "style": title.style,
        "ai_score": _num(title.ai_score),
        "manual_score": _num(title.manual_score),
        "is_edited": bool(title.is_edited),
        "source": title.source,
        "batch_id": title.batch_id,
        "ai_task_id": title.ai_task_id,
        "status": title.status,
        "adopted_by": title.adopted_by,
        "adopted_at": iso_utc(title.adopted_at),
        "content_count": title.content_count,
        "created_by": title.created_by,
        "created_at": iso_utc(title.created_at),
        "updated_at": iso_utc(title.updated_at),
    }


def list_titles(
    db: Session,
    scope: DataScope,
    *,
    page: int = 1,
    page_size: int = 20,
    project_id: int | None = None,
    keyword_id: int | None = None,
    status: str | None = None,
    style: str | None = None,
    batch_id: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """分页（``created_at DESC, id DESC``，不提供 ``sort``）；按 ``project_id IN P`` 过滤，筛选引用不可见对象时为空结果。"""
    conditions: list[Any] = []
    for column, value in (
        (Title.project_id, project_id), (Title.keyword_id, keyword_id), (Title.status, status), (Title.style, style),
        (Title.batch_id, batch_id),
    ):
        if value is not None and value != "":
            conditions.append(column == value)
    stmt = scope_by_project(select(Title), Title.project_id, scope).where(*conditions)
    total = int(db.scalar(scope_by_project(select(func.count(Title.id)), Title.project_id, scope).where(*conditions)) or 0)
    rows = db.scalars(stmt.order_by(Title.created_at.desc(), Title.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return [title_item(r) for r in rows], total


def get_title_row(db: Session, scope: DataScope, title_id: int) -> Title:
    return get_visible(db, scope, Title, title_id, message=TITLE_NOT_FOUND)


def get_title(db: Session, scope: DataScope, title_id: int) -> dict[str, Any]:
    return title_item(get_title_row(db, scope, title_id))


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _bump_title_count(db: Session, keyword_id: int, delta: int) -> None:
    """``keywords.title_count ± n``（条件表达式更新，并行根任务不互相覆盖）。"""
    if delta == 0:
        return
    db.execute(
        update(Keyword).where(Keyword.id == keyword_id)
        .values(
            title_count=case((Keyword.title_count + delta < 0, 0), else_=Keyword.title_count + delta), updated_at=utcnow(),
        )
        .execution_options(synchronize_session="fetch")
    )


# =====================================================================
# 手工新增 / 编辑 / 打分（docs/09 §7.3、§7.4）
# =====================================================================


def create_title(db: Session, scope: DataScope, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/titles``：关键词须可见（404），所属项目 ``active``（409）；``source=manual``、``status=candidate``；同事务
    ``keywords.title_count += 1``。同关键词下重复标题服务端不拦截。"""
    keyword = get_visible(db, scope, Keyword, int(values["keyword_id"]), message=KEYWORD_NOT_FOUND)
    project = require_project(db, scope, keyword.project_id, active=True)
    text = clean_title(values.get("title"))
    if not text:
        raise invalid_params(field_error(["body", "title"], "标题不能为空", "value_error", values.get("title")))
    if len(text) > TITLE_MAX_CHARS:
        raise invalid_params(field_error(["body", "title"], f"长度不能超过 {TITLE_MAX_CHARS}", "too_long", text))
    try:
        row = Title(
            project_id=project.id, keyword_id=keyword.id, title=text, style=values["style"], source="manual",
            status="candidate", created_by=admin_id,
        )
        db.add(row)
        db.flush()
        _bump_title_count(db, keyword.id, 1)
        stats_service.increment_realtime_after_commit(db, project.id, {"titles_created": 1})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return title_item(row)


def update_title(db: Session, scope: DataScope, title_id: int, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``PUT /admin/titles/{id}``：``{title, style?}``；标题首次变化时把原值写入 ``original_title`` 并置 ``is_edited=1``
    （已编辑过的保留最初的 ``original_title``）；重复标题不拦截。"""
    del admin_id
    title = get_title_row(db, scope, title_id)
    if "title" in values:
        text = clean_title(values["title"])
        if not text:
            raise invalid_params(field_error(["body", "title"], "标题不能为空", "value_error", values["title"]))
        if len(text) > TITLE_MAX_CHARS:
            raise invalid_params(field_error(["body", "title"], f"长度不能超过 {TITLE_MAX_CHARS}", "too_long", text))
        if text != title.title:
            if not title.is_edited:
                title.original_title = title.title
                title.is_edited = True
            title.title = text
    if values.get("style"):
        title.style = values["style"]
    db.commit()
    return title_item(title)


def score_title(db: Session, scope: DataScope, title_id: int, manual_score: Decimal | float | None) -> dict[str, Any]:
    """``POST /admin/titles/{id}/score``：人工打分 0~10（步长 0.5，``null`` 清空）；不影响状态。"""
    title = get_title_row(db, scope, title_id)
    title.manual_score = Decimal(str(manual_score)).quantize(Decimal("0.01")) if manual_score is not None else None
    db.commit()
    return title_item(title)


# =====================================================================
# 状态 / 删除（docs/09 §7.4、§7.5）
# =====================================================================


def _transition(db: Session, title: Title, action: str, *, admin_id: int | None) -> None:
    allowed, target = TRANSITIONS[action]
    if title.status not in allowed:
        raise _conflict(MSG_STATUS_CONFLICT, {"current_status": title.status})
    if action == "adopt":
        keyword_status = db.scalar(select(Keyword.status).where(Keyword.id == title.keyword_id))
        if keyword_status != "adopted":
            raise _conflict(MSG_KEYWORD_NOT_ADOPTED, {"current_status": keyword_status or "candidate"})
        now = utcnow()
        title.adopted_by = admin_id
        title.adopted_at = now
        stats_service.increment_realtime_after_commit(db, title.project_id, {"titles_adopted": 1}, at=now)
    title.status = target
    db.flush()


def change_status(db: Session, scope: DataScope, title_id: int, action: str, *, admin_id: int | None) -> dict[str, Any]:
    title = get_title_row(db, scope, title_id)
    try:
        _transition(db, title, action, admin_id=admin_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return title_item(title)


def adopt_title(db: Session, scope: DataScope, title_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``candidate → adopted``：要求关键词 ``adopted``，否则 409 ``{"current_status": 关键词状态}``（「关键词未采用」）。"""
    return change_status(db, scope, title_id, "adopt", admin_id=admin_id)


def discard_title(db: Session, scope: DataScope, title_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``candidate`` / ``adopted → discarded``（不影响已生成内容）。"""
    return change_status(db, scope, title_id, "discard", admin_id=admin_id)


def restore_title(db: Session, scope: DataScope, title_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``discarded → candidate``。"""
    return change_status(db, scope, title_id, "restore", admin_id=admin_id)


def batch_status(db: Session, scope: DataScope, ids: Sequence[int], action: str, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/titles/batch-status``：逐条独立判定与提交；不存在 / 不可见 → ``not_found``，非法流转（含关键词未采用时的
    ``adopt``）→ ``invalid_transition``。返回 ``{updated, skipped:[{id, reason}]}``。"""
    if action not in TRANSITIONS:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["body", "action"], "不支持的操作", "enum", action)])
    updated = 0
    skipped: list[dict[str, Any]] = []
    for title_id in dict.fromkeys(ids):
        title = db.get(Title, title_id)
        if title is None or not is_visible(db, scope, title):
            skipped.append({"id": title_id, "reason": "not_found"})
            continue
        try:
            _transition(db, title, action, admin_id=admin_id)
            db.commit()
            updated += 1
        except BusinessError:
            db.rollback()
            skipped.append({"id": title_id, "reason": "invalid_transition"})
    return {"updated": updated, "skipped": skipped}


def delete_title(db: Session, scope: DataScope, title_id: int) -> None:
    """仅 ``content_count=0``（不限状态）可物理删除，同事务 ``keywords.title_count −= 1``；否则 409 ``data={"reason":"in_use"}``。"""
    title = get_title_row(db, scope, title_id)
    if int(title.content_count or 0) > 0:
        raise _conflict(MSG_IN_USE, {"reason": "in_use"})
    try:
        keyword_id = title.keyword_id
        db.delete(title)
        db.flush()
        _bump_title_count(db, keyword_id, -1)
        db.commit()
    except Exception:
        db.rollback()
        raise


# =====================================================================
# 生成（docs/09 §7.2、§7.3）
# =====================================================================


def _title_variables(project: Project | None, keyword: Keyword, *, count: int, style: str | None) -> dict[str, Any]:
    return {
        **generation_service.project_variables(project),
        "keyword": keyword.keyword,
        "intent": keyword.intent,
        "style": style_text(style),
        "count": int(count),
    }


def _keyword_errors(db: Session, project: Project, keyword_ids: Sequence[int]) -> tuple[list[Keyword], list[dict[str, Any]]]:
    rows = {k.id: k for k in db.scalars(select(Keyword).where(Keyword.id.in_(list(dict.fromkeys(keyword_ids))))).all()}
    keywords: list[Keyword] = []
    errors: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, keyword_id in enumerate(keyword_ids):
        kw = rows.get(keyword_id)
        if kw is None or kw.project_id != project.id or kw.status == "discarded":
            errors.append(field_error(["body", "keyword_ids", index], MSG_INVALID_KEYWORD, "invalid_keyword", keyword_id))
            continue
        if keyword_id not in seen:
            seen.add(keyword_id)
            keywords.append(kw)
    return keywords, errors


def generate_titles(db: Session, scope: DataScope, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/titles/generate``：校验链（docs/09 §6.1；``keyword_ids`` 须属于该项目且 ``status != discarded``，否则 400，
    每个不合法 ID 一项 ``type=invalid_keyword``）→ 批次 ``kind=title``（``requested_count = count × 关键词数``、``task_total`` =
    关键词数）+ 每个关键词一个 ``title_generate`` 根任务（``target_type=keyword``、``input_json={keyword_id, count, style,
    template_id, model}``，各自预占额度）；返回 ``{batch_id, quota_warning?}``（按全部根任务预占完成后的累计用量判断）。"""
    cfg = settings_service.get_config(db, "generation_config")
    max_count = int(settings_service.get_path(cfg, "title.max_count") or 10)
    count = int(body["count"])
    style = str(body["style"])
    model = (str(body.get("model") or "").strip() or None)
    keyword_ids = [int(k) for k in body.get("keyword_ids") or []]
    selected: list[Keyword] = []

    def _validate(project: Project) -> None:
        errors: list[dict[str, Any]] = []
        keywords, keyword_errors = _keyword_errors(db, project, keyword_ids)
        errors.extend(keyword_errors)
        limit_error = generation_service.count_limit_error(count, max_count)
        if limit_error is not None:
            errors.extend(limit_error.data)
        if errors:
            raise invalid_params(errors)
        selected.extend(keywords)

    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=int(body["project_id"]), capability="title", kind="title",
        template_id=body.get("template_id"), model=model, validate=_validate,
    )
    request = {
        "project_id": plan.project.id, "keyword_ids": [k.id for k in selected], "count": count, "style": style,
        "template_id": body.get("template_id"), "model": model,
    }
    units = []
    for kw in selected:
        variables = _title_variables(plan.project, kw, count=count, style=style)
        units.append(
            generation_service.BatchUnit(
                target_type="keyword", target_id=kw.id,
                input={"keyword_id": kw.id, "count": count, "style": style, "template_id": body.get("template_id"), "model": model},
                estimated_quota=generation_service.estimate_call(db, plan.route, plan.template, variables),
            )
        )
    batch = generation_service.create_batch(
        db, project=plan.project, kind="title", capability="title", operation="title_generate", template=plan.template,
        route=plan.route, batch_input=request, requested_count=count * len(selected), created_by=admin_id, units=units,
    )
    return generation_service.batch_created_response(db, batch)


def apply_generated(
    db: Session, root_task: AiTask, attempt_task: AiTask, items: Sequence[Any], *, keyword: Keyword | None = None,
    style: str | None = None,
) -> dict[str, int]:
    """把模型输出写入 ``titles``（根任务终态事务内，docs/09 §7.3）：``title`` 去首尾空白与成对引号、折叠空白，超过 200 字符丢弃
    并计 ``too_long``；``ai_score`` 钳制 0~10、一位小数；同一关键词内按 ``title_dedup_key`` 去重（输出内部重复只留首个，与该
    关键词已有标题相同的跳过，计 ``duplicates``）；插入 ``source=generated``、``status=candidate``、``ai_task_id``=尝试行，同事务
    ``keywords.title_count += n``。空数组计 ``empty_output``。分项计数写根任务 ``response_meta_json.apply_counts``。"""
    if not isinstance(items, (list, tuple)):
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：$ 应为数组")
    data = gateway.task_input(root_task)
    if keyword is None:
        keyword_id = data.get("keyword_id") or root_task.target_id
        keyword = db.get(Keyword, keyword_id) if keyword_id else None
    if keyword is None:
        raise ZhiqiError(ErrorCategory.UNKNOWN, "关键词不存在")
    style = style or data.get("style") or "news"
    batch = db.get(GenerationBatch, root_task.batch_id) if root_task.batch_id else None
    created_by = (batch.created_by if batch is not None else None) or root_task.created_by or 0
    counts = generation_service.empty_apply_counts()
    if not items:
        counts["empty_output"] = 1
    existing = {title_dedup_key(t) for t in db.scalars(select(Title.title).where(Title.keyword_id == keyword.id)).all()}
    created = 0
    for item in items:
        raw = item.get("title") if isinstance(item, dict) else None
        if not isinstance(raw, str):
            continue
        text = clean_title(raw)
        if not text:
            continue
        if len(text) > TITLE_MAX_CHARS:
            counts["too_long"] += 1
            continue
        key = title_dedup_key(text)
        if not key or key in existing:
            counts["duplicates"] += 1
            continue
        existing.add(key)
        db.add(
            Title(
                project_id=keyword.project_id, keyword_id=keyword.id, title=text, original_title=None, style=style,
                ai_score=_ai_score(item.get("ai_score")), source="generated", batch_id=root_task.batch_id,
                ai_task_id=attempt_task.id, status="candidate", created_by=created_by,
            )
        )
        created += 1
    db.flush()
    _bump_title_count(db, keyword.id, created)
    if created:
        stats_service.increment_realtime_after_commit(db, keyword.project_id, {"titles_created": created})
    written = generation_service.write_apply_counts(root_task, counts)
    return {**written, "created": created}


def run_title_generate(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """worker 处理器（``operation=title_generate``，每个关键词一个根任务）：渲染 ``sys_title`` → ``complete_text``（JSON +
    schema / 结构校验）→ 终态事务内 ``apply_generated``。"""
    db, task = ctx.db, ctx.task
    keyword_id = ctx.input.get("keyword_id") or task.target_id
    keyword = db.get(Keyword, keyword_id) if keyword_id else None
    if keyword is None:
        return ai_task_service.TaskOutcome(status="failed", error_category="unknown", error_message="关键词不存在")
    project = db.get(Project, keyword.project_id)
    template = generation_service.task_template(db, task, "title")
    style = str(ctx.input.get("style") or "news")
    count = int(ctx.input.get("count") or 5)
    variables = _title_variables(project, keyword, count=count, style=style)
    items, attempt, result = generation_service.run_json_list(ctx, template, variables, required_key="title")

    def _apply(c: ai_task_service.TaskContext) -> None:
        kw = c.db.get(Keyword, keyword.id)
        apply_generated(c.db, c.task, attempt, items, keyword=kw, style=style)

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=(result.text or "")[: gateway.OUTPUT_EXCERPT_LIMIT])


ai_task_service.register_handler("title_generate", run_title_generate)
