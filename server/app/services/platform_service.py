"""发布平台与删除特征规则（docs/11 §4.3、§5；docs/04 §6.16；docs/03 B.19；docs/13 §4.3、§6.3）。

- 平台本身不受数据范围约束；列表 / 详情的 ``link_count`` 只统计调用者可见的链接（docs/13 §6.1）；
- 平台列表缓存 ``cache:platforms:all``（300s），任何写操作提交后 ``cache_delete``；
- ``detect(url)``：按 ``sort, id`` 遍历 ``is_active=1`` 平台，对 ``normalized_url`` 执行 ``urls.match_url_patterns``，首个命中即返回，
  未命中归 ``website``（``website`` / ``other`` 的 ``url_patterns`` 为空数组，永不自动命中）；无副作用；
- ``test_rule``：用平台 ``fetch_config`` 经 ``safe_fetch.fetch_page`` 实时抓取一次（受 docs/11 §6.1 全部限制，执行同域名间隔，
  计入 ``limit:link_checks:{date}``），以平台规则执行 ``link_check_service.judge``（无基线、``check_type=manual``），不写库；
- 删除：``is_system=0`` 且无 ``publish_links`` 引用，否则 409 ``{"reason":"in_use"}``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import safe_fetch, urls
from app.core.database import after_commit
from app.core.exceptions import CODE_CONFLICT, CODE_NOT_FOUND, BusinessError, field_error, invalid_params
from app.core.redis import cache_delete, cache_get_json, cache_set_json
from app.models import PublishLink, PublishPlatform
from app.schemas.common import iso_utc
from app.schemas.platform import FETCH_CONFIG_KEYS
from app.services.data_scope_service import DataScope, scope_by_project

logger = logging.getLogger(__name__)

CACHE_KEY = "cache:platforms:all"
CACHE_TTL_SECONDS = 300
FALLBACK_PLATFORM_CODE = "website"
NOT_FOUND = "平台不存在"
MSG_CODE_EXISTS = "平台代码已存在"
MSG_CODE_IMMUTABLE = "平台代码创建后不可修改"
MSG_SYSTEM_PLATFORM = "系统平台不可删除"
MSG_IN_USE = "平台已被链接引用，无法删除"


# =====================================================================
# 规则读取
# =====================================================================


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _str_list(raw: str | None) -> list[str]:
    return [item for item in _loads(raw, []) if isinstance(item, str) and item]


def url_patterns(platform: Any) -> list[str]:
    return _str_list(getattr(platform, "url_patterns_json", None))


def deleted_markers(platform: Any) -> list[str]:
    return _str_list(getattr(platform, "deleted_markers_json", None))


def redirect_markers(platform: Any) -> list[str]:
    return _str_list(getattr(platform, "redirect_markers_json", None))


def fetch_config(platform: Any) -> dict[str, Any]:
    """平台抓取覆盖项（只含 ``user_agent`` / ``headers`` / ``timeout_seconds`` / ``respect_robots`` / ``allow_http``）。"""
    raw = _loads(getattr(platform, "fetch_config_json", None), {})
    return {k: raw[k] for k in FETCH_CONFIG_KEYS if k in raw}


# =====================================================================
# 序列化与缓存
# =====================================================================


def _cache_entry(platform: PublishPlatform) -> dict[str, Any]:
    return {
        "id": platform.id,
        "code": platform.code,
        "name": platform.name,
        "name_en": platform.name_en,
        "icon": platform.icon,
        "home_url": platform.home_url,
        "url_patterns": url_patterns(platform),
        "deleted_markers": deleted_markers(platform),
        "redirect_markers": redirect_markers(platform),
        "fetch_config": fetch_config(platform),
        "is_system": bool(platform.is_system),
        "is_active": bool(platform.is_active),
        "sort": int(platform.sort or 0),
    }


def platform_item(platform: PublishPlatform, link_count: int | None = None) -> dict[str, Any]:
    item = _cache_entry(platform)
    item["created_at"] = iso_utc(platform.created_at)
    item["updated_at"] = iso_utc(platform.updated_at)
    if link_count is not None:
        item["link_count"] = int(link_count)
    return item


def platform_brief(platform: PublishPlatform | None) -> dict[str, Any] | None:
    if platform is None:
        return None
    return {"id": platform.id, "code": platform.code, "name": platform.name, "name_en": platform.name_en, "icon": platform.icon}


def all_platforms(db: Session) -> list[dict[str, Any]]:
    """全部平台（含规则，按 ``sort, id``）；读 ``cache:platforms:all``（300s），缺失时查库回填。"""
    cached = cache_get_json(CACHE_KEY)
    if isinstance(cached, list):
        return cached
    rows = db.scalars(select(PublishPlatform).order_by(PublishPlatform.sort, PublishPlatform.id)).all()
    entries = [_cache_entry(p) for p in rows]
    cache_set_json(CACHE_KEY, entries, CACHE_TTL_SECONDS)
    return entries


def invalidate_cache() -> None:
    cache_delete(CACHE_KEY)


def _invalidate_after_commit(db: Session) -> None:
    after_commit(db, invalidate_cache)


def link_counts(db: Session, scope: DataScope, platform_ids: list[int] | None = None) -> dict[int, int]:
    """按平台统计调用者可见的链接数（docs/13 §6.1）。"""
    stmt = select(PublishLink.platform_id, func.count(PublishLink.id))
    if platform_ids is not None:
        if not platform_ids:
            return {}
        stmt = stmt.where(PublishLink.platform_id.in_(platform_ids))
    stmt = scope_by_project(stmt, PublishLink.project_id, scope).group_by(PublishLink.platform_id)
    return {int(pid): int(count) for pid, count in db.execute(stmt).all()}


# =====================================================================
# 查询
# =====================================================================


def get_platform_row(db: Session, platform_id: int) -> PublishPlatform:
    platform = db.get(PublishPlatform, platform_id) if platform_id and platform_id < 2**63 else None
    if platform is None:
        raise BusinessError(NOT_FOUND, code=CODE_NOT_FOUND, http_status=404)
    return platform


def get_by_code(db: Session, code: str) -> PublishPlatform | None:
    return db.scalar(select(PublishPlatform).where(PublishPlatform.code == code).limit(1))


def list_platforms(db: Session, scope: DataScope, *, is_active: bool | None = None) -> list[dict[str, Any]]:
    """``GET /admin/platforms``：不分页，按 ``sort, id``；每项含 ``link_count``（只统计可见链接）。"""
    stmt = select(PublishPlatform).order_by(PublishPlatform.sort, PublishPlatform.id)
    if is_active is not None:
        stmt = stmt.where(PublishPlatform.is_active == is_active)
    rows = list(db.scalars(stmt).all())
    counts = link_counts(db, scope)
    return [platform_item(p, counts.get(p.id, 0)) for p in rows]


def get_platform(db: Session, scope: DataScope, platform_id: int) -> dict[str, Any]:
    platform = get_platform_row(db, platform_id)
    return platform_item(platform, link_counts(db, scope, [platform.id]).get(platform.id, 0))


# =====================================================================
# 写入
# =====================================================================


def _apply(platform: PublishPlatform, values: Mapping[str, Any]) -> None:
    for name in ("name", "name_en", "icon", "home_url", "is_active", "sort"):
        if name in values:
            if name in ("name", "name_en", "is_active", "sort") and values[name] is None:
                continue
            setattr(platform, name, values[name])
    for name, column in (("url_patterns", "url_patterns_json"), ("deleted_markers", "deleted_markers_json"),
                         ("redirect_markers", "redirect_markers_json")):
        if name in values and values[name] is not None:
            setattr(platform, column, _dumps(list(dict.fromkeys(values[name]))))
    if "fetch_config" in values and values["fetch_config"] is not None:
        setattr(platform, "fetch_config_json", _dumps(values["fetch_config"]))


def _conflict(message: str, data: Any) -> BusinessError:
    return BusinessError(message, code=CODE_CONFLICT, http_status=409, data=data)


def create_platform(db: Session, scope: DataScope, values: Mapping[str, Any]) -> dict[str, Any]:
    """``POST /admin/platforms``：``code`` 唯一（409 ``existing_id``）；提交后清 ``cache:platforms:all``。"""
    code = values["code"]
    existing = get_by_code(db, code)
    if existing is not None:
        raise _conflict(MSG_CODE_EXISTS, {"existing_id": existing.id})
    platform = PublishPlatform(code=code, name=values["name"], name_en=values["name_en"], is_system=False,
                               url_patterns_json="[]", deleted_markers_json="[]", redirect_markers_json="[]", fetch_config_json="{}")
    _apply(platform, values)
    db.add(platform)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = get_by_code(db, code)
        raise _conflict(MSG_CODE_EXISTS, {"existing_id": existing.id if existing else None}) from None
    _invalidate_after_commit(db)
    db.commit()
    return platform_item(platform, 0)


def update_platform(db: Session, scope: DataScope, platform_id: int, values: Mapping[str, Any]) -> dict[str, Any]:
    """``PUT /admin/platforms/{id}``：只写出现的字段；``code`` 不可改（与原值不同 → 400）；编辑规则立即影响后续检测
    （已有基线不变）；``is_active=0`` 后既有链接继续检测，新回填到该平台返回 409。"""
    platform = get_platform_row(db, platform_id)
    code = values.get("code")
    if code is not None and code != platform.code:
        raise invalid_params(field_error(["body", "code"], MSG_CODE_IMMUTABLE, "value_error", code))
    _apply(platform, values)
    _invalidate_after_commit(db)
    db.commit()
    return get_platform(db, scope, platform.id)


def delete_platform(db: Session, scope: DataScope, platform_id: int) -> None:
    """``DELETE /admin/platforms/{id}``：系统平台或有链接引用（不限数据范围）→ 409 ``{"reason":"in_use"}``。"""
    platform = get_platform_row(db, platform_id)
    if platform.is_system:
        raise _conflict(MSG_SYSTEM_PLATFORM, {"reason": "in_use"})
    used = db.scalar(select(func.count(PublishLink.id)).where(PublishLink.platform_id == platform.id)) or 0
    if used:
        raise _conflict(MSG_IN_USE, {"reason": "in_use"})
    db.delete(platform)
    _invalidate_after_commit(db)
    db.commit()


# =====================================================================
# 识别与规则测试
# =====================================================================


def _url_error(url: str, message: str) -> BusinessError:
    return invalid_params(field_error(["body", "url"], message, "value_error", url))


def detect_entry(db: Session, normalized_url: str) -> dict[str, Any] | None:
    """按 ``sort, id`` 遍历启用平台，首个 ``url_patterns`` 命中者；未命中返回 ``website``（不存在时 ``None``）。"""
    platforms = all_platforms(db)
    for entry in platforms:
        if entry.get("is_active") and urls.match_url_patterns(normalized_url, entry.get("url_patterns") or []):
            return entry
    for entry in platforms:
        if entry.get("code") == FALLBACK_PLATFORM_CODE:
            return entry
    return None


def detect(db: Session, url: str) -> dict[str, Any]:
    """``POST /admin/platforms/detect {url}`` → ``{platform_id, code}``；URL 非法 400；无副作用。"""
    try:
        normalized = urls.normalize_url(url)
    except urls.InvalidURLError as exc:
        raise _url_error(url, str(exc)) from None
    entry = detect_entry(db, normalized)
    if entry is None:
        raise BusinessError(NOT_FOUND, code=CODE_NOT_FOUND, http_status=404)
    return {"platform_id": entry["id"], "code": entry["code"]}


def test_rule(db: Session, scope: DataScope, platform_id: int, url: str) -> dict[str, Any]:
    """``POST /admin/platforms/{id}/test {url}``：实时抓取一次并以该平台规则判定（无基线、``check_type=manual``），不写库。

    返回 ``{result_status, matched_rule, http_status, final_url, redirect_count, title, duration_ms, evidence}``。"""
    from app.services import link_check_service  # 避免循环导入（link_check_service 依赖本模块读取规则）

    platform = get_platform_row(db, platform_id)
    cfg = link_check_service.link_check_config(db)
    config = link_check_service.build_fetch_config(platform, cfg)
    try:
        target = safe_fetch.normalize_public_url(url, allow_http=config.allow_http)
    except safe_fetch.FetchBlocked as exc:
        raise _url_error(url, exc.message) from None
    probe = SimpleNamespace(
        url=target, baseline_title=None, baseline_simhash=None, baseline_captured_at=None, alive_status="pending",
    )
    link_check_service.before_fetch(db, urls.extract_domain(target), cfg)
    fetched = link_check_service.fetch(target, config)
    judgement = link_check_service.evaluate(fetched, probe, platform, check_type="manual", cfg=cfg)
    return {
        "result_status": judgement.result_status,
        "matched_rule": judgement.matched_rule,
        "http_status": judgement.http_status,
        "final_url": judgement.final_url,
        "redirect_count": judgement.redirect_count,
        "title": judgement.title,
        "duration_ms": judgement.duration_ms,
        "evidence": judgement.evidence,
    }


__all__ = [
    "CACHE_KEY",
    "all_platforms",
    "create_platform",
    "delete_platform",
    "deleted_markers",
    "detect",
    "detect_entry",
    "fetch_config",
    "get_by_code",
    "get_platform",
    "get_platform_row",
    "invalidate_cache",
    "link_counts",
    "list_platforms",
    "platform_brief",
    "platform_item",
    "redirect_markers",
    "test_rule",
    "update_platform",
    "url_patterns",
]
