"""关键词（docs/09 §6；docs/03 B.10「关键词与标题」「删除规则」；docs/04 §6.9、§7.4；docs/13 §6.3、§7.1、§7.5）。

- 归一化 ``normalize_keyword``：Unicode NFKC → 去首尾空白并折叠连续空白 → 小写 → 全角转半角；项目内以
  ``UNIQUE(project_id, normalized_keyword)`` 去重（生成 / 导入冲突跳过并计数，手工新增与改名冲突 409 ``existing_id``）；
- 手工新增、JSON / CSV 导入（``{created, skipped, errors[]}``，逐条 SAVEPOINT）、编辑、导出 CSV、删除（仅无标题 / 内容关联）；
- 状态流转 ``candidate → adopted / discarded``、``discarded → candidate``：``discard`` 同事务把该词下 ``candidate`` 标题置
  ``discarded``；``batch-status`` 逐条独立判定与事务（``skipped[]`` 元素 ``{id, reason}``）；
- 生成：``generate_keywords``（API：校验链 + 批次 + 1 个 ``keyword_generate`` 根任务）与 worker 处理器
  ``run_keyword_generate``（渲染 ``sys_keyword`` → JSON → ``apply_generated`` 清洗、去重、入库，分项计数写根任务
  ``response_meta_json.apply_counts``）；
- 实时计数（docs/12 §4.6）：关键词插入 ``keywords_created``、采用 ``keywords_adopted``，事务提交后写 ``stats:rt``。
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
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
    GenerationBatch,
    Keyword,
    KeywordIntent,
    KeywordType,
    Project,
    Title,
    enum_values,
    utcnow,
)
from app.schemas.common import EXPORT_MAX_ROWS, iso_utc
from app.schemas.keyword import (
    EXPORT_COLUMNS,
    IMPORT_FILE_COLUMNS,
    IMPORT_FILE_MAX_BYTES,
    IMPORT_MAX_ITEMS,
    KEYWORD_MAX_CHARS,
)
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
    "KEYWORD_INTENTS",
    "KEYWORD_TYPES",
    "adopt_keyword",
    "apply_generated",
    "batch_status",
    "change_status",
    "clean_keyword",
    "compute_score",
    "create_keyword",
    "delete_keyword",
    "discard_keyword",
    "export_keywords",
    "generate_keywords",
    "get_keyword",
    "get_keyword_row",
    "import_keywords",
    "import_keywords_csv",
    "keyword_item",
    "list_keywords",
    "normalize_keyword",
    "parse_import_csv",
    "restore_keyword",
    "run_keyword_generate",
    "update_keyword",
]

KEYWORD_INTENTS: tuple[str, ...] = enum_values(KeywordIntent)
KEYWORD_TYPES: tuple[str, ...] = enum_values(KeywordType)
DEFAULT_INTENT = "unknown"
DEFAULT_TYPE = "core"
REASON_MAX_CHARS = 500
SEED_MAX_CHARS = 120
NORMALIZED_MAX_CHARS = 160
SCORE_QUANT = Decimal("0.01")
IN_CHUNK = 500

KEYWORD_NOT_FOUND = "关键词不存在"
MSG_KEYWORD_EXISTS = "关键词已存在"
MSG_KEYWORD_EMPTY = "关键词不能为空"
MSG_STATUS_CONFLICT = "当前状态不允许该操作"
MSG_IN_USE = "关键词已有关联标题或内容，不能删除"

_WS_RE = re.compile(r"\s+")
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

# 状态流转：action → (允许的起始状态, 目标状态)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "adopt": (("candidate",), "adopted"),
    "discard": (("candidate", "adopted"), "discarded"),
    "restore": (("discarded",), "candidate"),
}


# =====================================================================
# 归一化与清洗（docs/09 §6.2、§6.4、§6.5）
# =====================================================================


def _fullwidth_to_halfwidth(text: str) -> str:
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize_keyword(text: str | None) -> str:
    """去重键：Unicode NFKC → 去首尾空白并折叠连续空白 → 小写 → 全角转半角（与 docs/03 ``keywords.normalized_keyword`` 一致）。"""
    value = unicodedata.normalize("NFKC", text or "")
    value = _WS_RE.sub(" ", value).strip()
    value = value.lower()
    value = _fullwidth_to_halfwidth(value)
    return _WS_RE.sub(" ", value).strip()[:NORMALIZED_MAX_CHARS]


def clean_keyword(text: Any) -> str:
    """展示原文：去首尾空白、折叠空白（不小写化、不截断）。"""
    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def compute_score(difficulty: int | None, heat: int | None) -> Decimal | None:
    """综合优先级分 ``0.6 × heat + 0.4 × (100 − difficulty)``（两位小数），任一输入为 NULL 时为 NULL。"""
    if difficulty is None or heat is None:
        return None
    value = Decimal("0.6") * Decimal(int(heat)) + Decimal("0.4") * (Decimal(100) - Decimal(int(difficulty)))
    return value.quantize(SCORE_QUANT, rounding=ROUND_HALF_UP)


def _clamp_metric(value: Any) -> int | None:
    """``difficulty`` / ``heat``：整数（或整值小数）钳制到 1~100，非整数 / 非数值 → NULL。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int):
        return None
    return max(1, min(100, value))


def _enum_or_none(value: Any, allowed: Iterable[str]) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if candidate in set(allowed) else None


def _pick_seed(normalized: str, seeds: Sequence[str]) -> str | None:
    """与 ``normalized_keyword`` 存在包含关系的最长种子词（无则 NULL）。"""
    best: str | None = None
    for seed in seeds:
        norm = normalize_keyword(seed)
        if norm and (norm in normalized or normalized in norm) and (best is None or len(seed) > len(best)):
            best = seed
    return best[:SEED_MAX_CHARS] if best else None


def _csv_cell(value: Any) -> Any:
    """CSV 公式注入防护：以 ``=``/``+``/``-``/``@`` 开头的字符串单元格加前导 ``'``（docs/09 §13.2）。"""
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


# =====================================================================
# 序列化与查询
# =====================================================================


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def keyword_item(kw: Keyword) -> dict[str, Any]:
    """关键词对象（docs/04 §7.4）。"""
    tags = _loads(kw.tags_json, [])
    return {
        "id": kw.id,
        "project_id": kw.project_id,
        "keyword": kw.keyword,
        "normalized_keyword": kw.normalized_keyword,
        "language": kw.language,
        "intent": kw.intent,
        "keyword_type": kw.keyword_type,
        "difficulty": kw.difficulty,
        "heat": kw.heat,
        "score": float(kw.score) if kw.score is not None else None,
        "seed": kw.seed,
        "source": kw.source,
        "batch_id": kw.batch_id,
        "ai_task_id": kw.ai_task_id,
        "reason": kw.reason,
        "tags": tags if isinstance(tags, list) else [],
        "status": kw.status,
        "adopted_by": kw.adopted_by,
        "adopted_at": iso_utc(kw.adopted_at),
        "title_count": kw.title_count,
        "content_count": kw.content_count,
        "created_by": kw.created_by,
        "created_at": iso_utc(kw.created_at),
        "updated_at": iso_utc(kw.updated_at),
    }


def _list_conditions(
    *,
    project_id: int | None = None,
    status: str | None = None,
    intent: str | None = None,
    keyword_type: str | None = None,
    keyword: str | None = None,
    batch_id: int | None = None,
) -> list[Any]:
    conditions: list[Any] = []
    for column, value in (
        (Keyword.project_id, project_id), (Keyword.status, status), (Keyword.intent, intent),
        (Keyword.keyword_type, keyword_type), (Keyword.batch_id, batch_id),
    ):
        if value is not None and value != "":
            conditions.append(column == value)
    text = (keyword or "").strip()
    if text:
        like = f"%{text}%"
        conditions.append(or_(Keyword.keyword.like(like), Keyword.normalized_keyword.like(f"%{normalize_keyword(text)}%")))
    return conditions


def _ordering(sort: str | None, order: str | None) -> list[Any]:
    descending = (order or "desc").lower() != "asc"
    created = [Keyword.created_at.desc(), Keyword.id.desc()]
    if sort == "score":
        score = Keyword.score.desc() if descending else Keyword.score.asc()
        return [Keyword.score.is_(None), score, *created]          # NULL 分数排在最后
    if not descending:
        return [Keyword.created_at.asc(), Keyword.id.asc()]
    return created


def list_keywords(
    db: Session,
    scope: DataScope,
    *,
    project_id: int,
    page: int = 1,
    page_size: int = 20,
    sort: str | None = None,
    order: str | None = None,
    **filters: Any,
) -> tuple[list[dict[str, Any]], int]:
    """分页；``project_id`` 必填（不可见项目为空结果）；未传 ``sort`` 按 ``created_at DESC, id DESC``，``sort=score`` 按
    ``score DESC, created_at DESC``（``order=asc`` 反向）。"""
    conditions = _list_conditions(project_id=project_id, **filters)
    stmt = scope_by_project(select(Keyword), Keyword.project_id, scope).where(*conditions)
    total = int(db.scalar(scope_by_project(select(func.count(Keyword.id)), Keyword.project_id, scope).where(*conditions)) or 0)
    rows = db.scalars(stmt.order_by(*_ordering(sort, order)).offset((page - 1) * page_size).limit(page_size)).all()
    return [keyword_item(r) for r in rows], total


def export_keywords(
    db: Session, scope: DataScope, *, project_id: int, sort: str | None = None, order: str | None = None, **filters: Any
) -> tuple[str, str]:
    """``GET /admin/keywords/export``：与列表相同筛选，UTF-8 BOM CSV（列见 ``schemas.keyword.EXPORT_COLUMNS``），最多 50,000 行
    （超出 400）。返回 ``(文件名, CSV 文本)``，文件名 ``keywords-<YYYYMMDD>.csv``。"""
    conditions = _list_conditions(project_id=project_id, **filters)
    total = int(db.scalar(scope_by_project(select(func.count(Keyword.id)), Keyword.project_id, scope).where(*conditions)) or 0)
    if total > EXPORT_MAX_ROWS:
        raise invalid_params(
            field_error(["query"], f"导出行数 {total} 超过上限 {EXPORT_MAX_ROWS}，请缩小筛选范围", "too_many_rows", total)
        )
    stmt = scope_by_project(select(Keyword), Keyword.project_id, scope).where(*conditions).order_by(*_ordering(sort, order))
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer)
    writer.writerow([header for _, header in EXPORT_COLUMNS])
    for kw in db.scalars(stmt).yield_per(1000):
        item = keyword_item(kw)
        writer.writerow([_csv_cell(item.get(key)) for key, _ in EXPORT_COLUMNS])
    filename = f"keywords-{stats_service.today_date(db).strftime('%Y%m%d')}.csv"
    return filename, buffer.getvalue()


def get_keyword_row(db: Session, scope: DataScope, keyword_id: int) -> Keyword:
    return get_visible(db, scope, Keyword, keyword_id)


def get_keyword(db: Session, scope: DataScope, keyword_id: int) -> dict[str, Any]:
    return keyword_item(get_keyword_row(db, scope, keyword_id))


def _existing_id(db: Session, project_id: int, normalized: str, *, exclude_id: int | None = None) -> int | None:
    stmt = select(Keyword.id).where(Keyword.project_id == project_id, Keyword.normalized_keyword == normalized)
    if exclude_id is not None:
        stmt = stmt.where(Keyword.id != exclude_id)
    return db.scalar(stmt.limit(1))


def _existing_normalized(db: Session, project_id: int, values: Iterable[str]) -> set[str]:
    pending = list(dict.fromkeys(v for v in values if v))
    found: set[str] = set()
    for start in range(0, len(pending), IN_CHUNK):
        chunk = pending[start : start + IN_CHUNK]
        found.update(
            db.scalars(
                select(Keyword.normalized_keyword).where(Keyword.project_id == project_id, Keyword.normalized_keyword.in_(chunk))
            ).all()
        )
    return found


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def _insert(db: Session, row: Keyword) -> bool:
    """SAVEPOINT 内插入一行；命中唯一索引（并发写入同一 ``normalized_keyword``）回滚该条并返回 ``False``。"""
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return False
    return True


def _count_created(db: Session, project_id: int, created: int) -> None:
    if created > 0:
        stats_service.increment_realtime_after_commit(db, project_id, {"keywords_created": created})


# =====================================================================
# 手工新增 / 导入（docs/09 §6.7）
# =====================================================================


def create_keyword(db: Session, scope: DataScope, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/keywords``：项目须可见且 ``active``；``source=manual``、``status=candidate``；冲突 409
    ``data={"existing_id":…}``。"""
    project = require_project(db, scope, int(values["project_id"]), active=True)
    text = clean_keyword(values.get("keyword"))
    normalized = normalize_keyword(text)
    if not text or not normalized:
        raise invalid_params(field_error(["body", "keyword"], MSG_KEYWORD_EMPTY, "value_error", values.get("keyword")))
    if len(text) > KEYWORD_MAX_CHARS:
        raise invalid_params(field_error(["body", "keyword"], f"长度不能超过 {KEYWORD_MAX_CHARS}", "too_long", text))
    existing = _existing_id(db, project.id, normalized)
    if existing:
        raise _conflict(MSG_KEYWORD_EXISTS, {"existing_id": existing})
    row = Keyword(
        project_id=project.id, keyword=text, normalized_keyword=normalized, language=project.language,
        intent=values.get("intent") or DEFAULT_INTENT, keyword_type=values.get("keyword_type") or DEFAULT_TYPE,
        source="manual", status="candidate", created_by=admin_id,
    )
    if not _insert(db, row):
        db.rollback()
        raise _conflict(MSG_KEYWORD_EXISTS, {"existing_id": _existing_id(db, project.id, normalized)})
    _count_created(db, project.id, 1)
    db.commit()
    return keyword_item(row)


def import_keywords(
    db: Session, scope: DataScope, project_id: int, items: Sequence[Mapping[str, Any]], *, admin_id: int
) -> dict[str, Any]:
    """批量导入（JSON 与 CSV 共用）：逐条校验 → ``errors[]``（``{index, keyword, reason}``，``reason`` ∈ ``empty`` / ``too_long`` /
    ``invalid_intent`` / ``invalid_keyword_type``）；与项目已有或本批次内重复的计入 ``skipped``；其余在单事务内逐条 SAVEPOINT
    插入（``source=imported``、``status=candidate``、``language=projects.language``、``created_by``=导入人），遇唯一冲突回滚该条
    并计入 ``skipped``。不经过 LLM、不建批次、不计频控。``items`` 的元素可带 ``index``（CSV 数据行号），缺省为输入顺序。"""
    project = require_project(db, scope, project_id, active=True)
    errors: list[dict[str, Any]] = []
    valid: list[tuple[str, str, str, str]] = []          # (keyword, normalized, intent, keyword_type)
    skipped = 0
    seen: set[str] = set()
    for position, item in enumerate(items):
        index = int(item.get("index", position)) if isinstance(item.get("index", position), int) else position
        raw = item.get("keyword")
        raw_text = "" if raw is None else str(raw)
        text = clean_keyword(raw_text)
        normalized = normalize_keyword(text)
        reason: str | None = None
        intent_raw = item.get("intent")
        type_raw = item.get("keyword_type")
        intent = DEFAULT_INTENT if intent_raw is None or not str(intent_raw).strip() else _enum_or_none(str(intent_raw), KEYWORD_INTENTS)
        kw_type = DEFAULT_TYPE if type_raw is None or not str(type_raw).strip() else _enum_or_none(str(type_raw), KEYWORD_TYPES)
        if not text or not normalized:
            reason = "empty"
        elif len(text) > KEYWORD_MAX_CHARS:
            reason = "too_long"
        elif intent is None:
            reason = "invalid_intent"
        elif kw_type is None:
            reason = "invalid_keyword_type"
        if reason:
            errors.append({"index": index, "keyword": raw_text[:200], "reason": reason})
            continue
        if normalized in seen:
            skipped += 1
            continue
        seen.add(normalized)
        valid.append((text, normalized, intent or DEFAULT_INTENT, kw_type or DEFAULT_TYPE))
    existing = _existing_normalized(db, project.id, (v[1] for v in valid))
    created = 0
    try:
        for text, normalized, intent, kw_type in valid:
            if normalized in existing:
                skipped += 1
                continue
            row = Keyword(
                project_id=project.id, keyword=text, normalized_keyword=normalized, language=project.language,
                intent=intent, keyword_type=kw_type, source="imported", status="candidate", created_by=admin_id,
            )
            if _insert(db, row):
                created += 1
            else:
                skipped += 1
        _count_created(db, project.id, created)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"created": created, "skipped": skipped, "errors": errors}


def _file_error(message: str, type_: str, input_value: Any = None) -> BusinessError:
    return invalid_params(field_error(["body", "file"], message, type_, input_value))


def parse_import_csv(data: bytes) -> list[dict[str, Any]]:
    """解析导入 CSV（docs/09 §6.7）：UTF-8（允许 BOM）、≤ 2 MB、首行表头 ``keyword,intent,keyword_type``（不区分大小写），
    数据行缺列视为空值、全空行忽略，≤ 5,000 数据行。返回 ``[{index, keyword, intent, keyword_type}]``（``index`` 为数据行号，
    0 起，计入空行）。表头缺失 / 列名不符 / 非 UTF-8 / 超限 → 整体 400。"""
    if len(data) > IMPORT_FILE_MAX_BYTES:
        raise _file_error("文件不能超过 2 MB", "file_too_large", len(data))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _file_error("文件不是 UTF-8 编码", "invalid_encoding") from None
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        raise _file_error(f"CSV 格式错误：{exc}", "invalid_csv") from None
    if not rows:
        raise _file_error("缺少表头 keyword,intent,keyword_type", "invalid_header")
    header = [cell.strip().lower() for cell in rows[0]]
    while header and not header[-1]:
        header.pop()
    if tuple(header) != IMPORT_FILE_COLUMNS:
        raise _file_error("表头必须为 keyword,intent,keyword_type", "invalid_header", ",".join(rows[0])[:200])
    items: list[dict[str, Any]] = []
    for index, row in enumerate(rows[1:]):
        if not any(cell.strip() for cell in row):
            continue
        cells = list(row) + [""] * (len(IMPORT_FILE_COLUMNS) - len(row))
        items.append({"index": index, "keyword": cells[0], "intent": cells[1], "keyword_type": cells[2]})
        if len(items) > IMPORT_MAX_ITEMS:
            raise _file_error(f"数据行不能超过 {IMPORT_MAX_ITEMS} 行", "too_many_rows", len(items))
    return items


def import_keywords_csv(db: Session, scope: DataScope, project_id: int, data: bytes, *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/keywords/import-file``：先校验项目（404 / 409），再解析 CSV（400），按 ``import_keywords`` 写入。"""
    require_project(db, scope, project_id, active=True)
    items = parse_import_csv(data)
    return import_keywords(db, scope, project_id, items, admin_id=admin_id)


# =====================================================================
# 编辑 / 状态 / 删除（docs/09 §6.6、§6.8）
# =====================================================================


def update_keyword(db: Session, scope: DataScope, keyword_id: int, values: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``PUT /admin/keywords/{id}``：可改 ``keyword`` / ``intent`` / ``keyword_type`` / ``difficulty`` / ``heat`` / ``score`` /
    ``tags``；改 ``keyword`` 时重算 ``normalized_keyword``（冲突 409 ``existing_id``）；改 ``difficulty`` / ``heat`` 而未给
    ``score`` 时按公式重算 ``score``（人工可直接覆盖 ``score``）。"""
    del admin_id
    kw = get_keyword_row(db, scope, keyword_id)
    if "keyword" in values:
        text = clean_keyword(values["keyword"])
        normalized = normalize_keyword(text)
        if not text or not normalized:
            raise invalid_params(field_error(["body", "keyword"], MSG_KEYWORD_EMPTY, "value_error", values["keyword"]))
        if normalized != kw.normalized_keyword:
            existing = _existing_id(db, kw.project_id, normalized, exclude_id=kw.id)
            if existing:
                raise _conflict(MSG_KEYWORD_EXISTS, {"existing_id": existing})
        kw.keyword = text
        kw.normalized_keyword = normalized
    for key in ("intent", "keyword_type"):
        if key in values:
            setattr(kw, key, values[key])
    metrics_changed = False
    for key in ("difficulty", "heat"):
        if key in values:
            setattr(kw, key, values[key])
            metrics_changed = True
    if "score" in values:
        score = values["score"]
        kw.score = Decimal(str(score)).quantize(SCORE_QUANT, rounding=ROUND_HALF_UP) if score is not None else None
    elif metrics_changed:
        kw.score = compute_score(kw.difficulty, kw.heat)
    if "tags" in values:
        tags = list(dict.fromkeys(t.strip() for t in (values["tags"] or []) if t and t.strip()))
        kw.tags_json = json.dumps(tags, ensure_ascii=False) if tags else None
    try:
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _conflict(MSG_KEYWORD_EXISTS, {"existing_id": _existing_id(db, kw.project_id, normalize_keyword(values.get("keyword")))}) from None
    return keyword_item(kw)


def _transition(db: Session, kw: Keyword, action: str, *, admin_id: int | None) -> None:
    """按 ``TRANSITIONS`` 流转（只写库、不提交）；非法流转 409 ``data={"current_status":…}``。"""
    allowed, target = TRANSITIONS[action]
    if kw.status not in allowed:
        raise _conflict(MSG_STATUS_CONFLICT, {"current_status": kw.status})
    kw.status = target
    if action == "adopt":
        now = utcnow()
        kw.adopted_by = admin_id
        kw.adopted_at = now
        stats_service.increment_realtime_after_commit(db, kw.project_id, {"keywords_adopted": 1}, at=now)
    elif action == "discard":
        # 同事务级联弃用该关键词下的 candidate 标题（已 adopted 标题与内容不动；restore 不恢复）
        db.execute(
            update(Title).where(Title.keyword_id == kw.id, Title.status == "candidate").values(status="discarded", updated_at=utcnow())
            .execution_options(synchronize_session="fetch")
        )
    db.flush()


def change_status(db: Session, scope: DataScope, keyword_id: int, action: str, *, admin_id: int | None) -> dict[str, Any]:
    kw = get_keyword_row(db, scope, keyword_id)
    try:
        _transition(db, kw, action, admin_id=admin_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return keyword_item(kw)


def adopt_keyword(db: Session, scope: DataScope, keyword_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``candidate → adopted``（写 ``adopted_by`` / ``adopted_at``）；重复采用 409 ``current_status=adopted``。"""
    return change_status(db, scope, keyword_id, "adopt", admin_id=admin_id)


def discard_keyword(db: Session, scope: DataScope, keyword_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``candidate`` / ``adopted → discarded``，同事务把其 ``candidate`` 标题置 ``discarded``。"""
    return change_status(db, scope, keyword_id, "discard", admin_id=admin_id)


def restore_keyword(db: Session, scope: DataScope, keyword_id: int, *, admin_id: int | None) -> dict[str, Any]:
    """``discarded → candidate``（不恢复被级联弃用的标题）。"""
    return change_status(db, scope, keyword_id, "restore", admin_id=admin_id)


def batch_status(db: Session, scope: DataScope, ids: Sequence[int], action: str, *, admin_id: int | None) -> dict[str, Any]:
    """``POST /admin/keywords/batch-status``：逐条按单条规则独立判定与提交，不整体失败；不存在 / 不可见 → ``not_found``，
    非法流转 → ``invalid_transition``。返回 ``{updated, skipped:[{id, reason}]}``。"""
    if action not in TRANSITIONS:
        raise BusinessError("参数错误", code=CODE_BAD_REQUEST, data=[field_error(["body", "action"], "不支持的操作", "enum", action)])
    updated = 0
    skipped: list[dict[str, Any]] = []
    for keyword_id in dict.fromkeys(ids):
        kw = db.get(Keyword, keyword_id)
        if kw is None or not is_visible(db, scope, kw):
            skipped.append({"id": keyword_id, "reason": "not_found"})
            continue
        try:
            _transition(db, kw, action, admin_id=admin_id)
            db.commit()
            updated += 1
        except BusinessError:
            db.rollback()
            skipped.append({"id": keyword_id, "reason": "invalid_transition"})
    return {"updated": updated, "skipped": skipped}


def delete_keyword(db: Session, scope: DataScope, keyword_id: int) -> None:
    """仅 ``title_count=0 AND content_count=0``（不限状态）可物理删除，否则 409 ``data={"reason":"in_use"}``。"""
    kw = get_keyword_row(db, scope, keyword_id)
    if int(kw.title_count or 0) > 0 or int(kw.content_count or 0) > 0:
        raise _conflict(MSG_IN_USE, {"reason": "in_use"})
    db.delete(kw)
    db.commit()


# =====================================================================
# 生成（docs/09 §6.1、§6.2）
# =====================================================================


def _dedupe_seeds(seeds: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        text = clean_keyword(seed)
        norm = normalize_keyword(text)
        if text and norm and norm not in seen:
            seen.add(norm)
            result.append(text)
    return result


def _keyword_variables(project: Project | None, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **generation_service.project_variables(project, audience=data.get("audience")),
        "seeds": list(data.get("seeds") or []),
        "competitors": list(data.get("competitors") or []),
        "count": int(data.get("count") or 0),
    }


def generate_keywords(db: Session, scope: DataScope, body: Mapping[str, Any], *, admin_id: int) -> dict[str, Any]:
    """``POST /admin/keywords/generate``：校验链（docs/09 §6.1）→ 同一事务创建 ``generation_batches(kind=keyword)`` 与 1 个
    ``keyword_generate`` 根任务（``target_type=generation_batch``、``target_id=batch_id``、``input_json``=请求体，含 ``model``
    原值），预占额度后入队；返回 ``{batch_id, quota_warning?}``。"""
    cfg = settings_service.get_config(db, "generation_config")
    max_count = int(settings_service.get_path(cfg, "keyword.max_count") or 50)
    count = int(body["count"])
    model = (str(body.get("model") or "").strip() or None)
    request = {
        "project_id": int(body["project_id"]),
        "seeds": _dedupe_seeds(body.get("seeds") or []),
        "count": count,
        "competitors": [clean_keyword(c) for c in (body.get("competitors") or []) if clean_keyword(c)],
        "audience": (str(body.get("audience") or "").strip() or None),
        "template_id": body.get("template_id"),
        "model": model,
    }

    def _validate(_project: Project) -> None:
        error = generation_service.count_limit_error(count, max_count)
        if error is not None:
            raise error

    plan = generation_service.prepare_generation(
        db, scope, admin_id=admin_id, project_id=request["project_id"], capability="keyword", kind="keyword",
        template_id=request["template_id"], model=model, validate=_validate,
    )
    variables = _keyword_variables(plan.project, request)
    estimated = generation_service.estimate_call(db, plan.route, plan.template, variables)
    batch = generation_service.create_batch(
        db, project=plan.project, kind="keyword", capability="keyword", operation="keyword_generate", template=plan.template,
        route=plan.route, batch_input=request, requested_count=count, created_by=admin_id,
        units=[generation_service.BatchUnit(target_type="generation_batch", target_id=None, input=request, estimated_quota=estimated)],
    )
    return generation_service.batch_created_response(db, batch)


def apply_generated(db: Session, root_task: AiTask, attempt_task: AiTask, items: Sequence[Any]) -> dict[str, int]:
    """把模型输出写入 ``keywords``（根任务终态事务内，docs/09 §6.2 第 2~5 步）：

    逐项清洗（``keyword`` 折叠空白并截断 120；``intent`` 非法 → ``unknown``，``intent_required=true`` 时计入 ``intent_missing``；
    ``keyword_type`` 非法 → ``core``；``difficulty`` / ``heat`` 钳制 1~100、非整数 → NULL；``reason`` 截断 500）→ 归一化后为空计入
    ``invalid`` → 输出内部重复只留首个、与项目已有冲突跳过（``duplicates``）→ 插入（``status=candidate``、``source=generated``、
    ``batch_id``、``ai_task_id``=尝试行、``seed``、``score``、``language=projects.language``、``created_by``=批次发起人）。空数组计
    ``empty_output``。分项计数写根任务 ``response_meta_json.apply_counts`` 并返回（含 ``created``）。"""
    if not isinstance(items, (list, tuple)):
        raise ZhiqiError(ErrorCategory.INVALID_RESPONSE, "模型输出不符合约定：$ 应为数组")
    project = db.get(Project, root_task.project_id) if root_task.project_id else None
    if project is None:
        raise ZhiqiError(ErrorCategory.UNKNOWN, "项目不存在")
    batch = db.get(GenerationBatch, root_task.batch_id) if root_task.batch_id else None
    cfg = settings_service.get_config(db, "generation_config")
    intent_required = bool(settings_service.get_path(cfg, "keyword.intent_required", True))
    seeds = list(gateway.task_input(root_task).get("seeds") or [])
    created_by = (batch.created_by if batch is not None else None) or root_task.created_by or 0
    counts = generation_service.empty_apply_counts()
    if not items:
        counts["empty_output"] = 1
    rows: list[Keyword] = []
    seen: set[str] = set()
    for item in items:
        raw = item.get("keyword") if isinstance(item, dict) else None
        text = clean_keyword(raw if isinstance(raw, (str, int, float)) and not isinstance(raw, bool) else None)[:KEYWORD_MAX_CHARS]
        normalized = normalize_keyword(text)
        if not text or not normalized:
            counts["invalid"] += 1
            continue
        if normalized in seen:
            counts["duplicates"] += 1
            continue
        seen.add(normalized)
        intent = _enum_or_none(item.get("intent"), KEYWORD_INTENTS)
        if intent is None:
            intent = DEFAULT_INTENT
            if intent_required:
                counts["intent_missing"] += 1
        kw_type = _enum_or_none(item.get("keyword_type"), KEYWORD_TYPES) or DEFAULT_TYPE
        difficulty = _clamp_metric(item.get("difficulty"))
        heat = _clamp_metric(item.get("heat"))
        reason_raw = item.get("reason")
        reason = clean_keyword(reason_raw)[:REASON_MAX_CHARS] if isinstance(reason_raw, str) else None
        rows.append(
            Keyword(
                project_id=project.id, keyword=text, normalized_keyword=normalized, language=project.language,
                intent=intent, keyword_type=kw_type, difficulty=difficulty, heat=heat, score=compute_score(difficulty, heat),
                seed=_pick_seed(normalized, seeds), source="generated", batch_id=root_task.batch_id, ai_task_id=attempt_task.id,
                reason=reason or None, status="candidate", created_by=created_by,
            )
        )
    existing = _existing_normalized(db, project.id, (r.normalized_keyword for r in rows))
    created = 0
    for row in rows:
        if row.normalized_keyword in existing or not _insert(db, row):
            counts["duplicates"] += 1
            continue
        created += 1
    _count_created(db, project.id, created)
    written = generation_service.write_apply_counts(root_task, counts)
    return {**written, "created": created}


def run_keyword_generate(ctx: ai_task_service.TaskContext) -> ai_task_service.TaskOutcome:
    """worker 处理器（``operation=keyword_generate``）：渲染 ``sys_keyword``（或批次快照模板）→ ``complete_text``
    （``response_format=json`` + schema / 结构校验）→ 终态事务内 ``apply_generated``。"""
    db, task = ctx.db, ctx.task
    project = db.get(Project, task.project_id) if task.project_id else None
    if project is None:
        return ai_task_service.TaskOutcome(status="failed", error_category="unknown", error_message="项目不存在")
    template = generation_service.task_template(db, task, "keyword")
    variables = _keyword_variables(project, ctx.input)
    items, attempt, result = generation_service.run_json_list(ctx, template, variables, required_key="keyword")

    def _apply(c: ai_task_service.TaskContext) -> None:
        apply_generated(c.db, c.task, attempt, items)

    return ai_task_service.TaskOutcome(status="succeeded", apply=_apply, output_excerpt=(result.text or "")[: gateway.OUTPUT_EXCERPT_LIMIT])


ai_task_service.register_handler("keyword_generate", run_keyword_generate)
