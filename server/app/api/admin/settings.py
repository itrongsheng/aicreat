"""系统配置（``/admin/settings``，docs/04 §6.6、§7.18）。

``GET /runtime`` 已登录即可读（必须先于 ``/{key}`` 注册）；其余接口需 ``system.settings.view`` / ``system.settings.update``。
密钥类字段只返回 ``configured: true/false``，永不回显环境变量的值。保存 ``seo_providers`` / ``geo_engines`` 成功后，对
``is_monitoring=1 AND alive_status != 'deleted' AND next_index_check_at IS NULL`` 的链接按 ``compute_next_index_check_at``
重算排程（docs/11 §7.6、§8.1）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin, get_db, require_permission
from app.core.response import ok
from app.models import Admin
from app.schemas.settings import SettingsBatchBody, SettingUpdateBody
from app.services import index_check_service, settings_service

router = APIRouter()

# 保存后需要重算收录检测排程的配置键（docs/11 §7.6、§8.1）
INDEX_ENGINE_KEYS = frozenset({"seo_providers", "geo_engines"})


def _after_save(db: Session, keys: set[str]) -> None:
    if keys & INDEX_ENGINE_KEYS:
        index_check_service.recompute_null_schedules(db)


SettingKey = Annotated[str, Path(min_length=1, max_length=80, description="配置键")]


@router.get("", summary="全部配置")
def list_settings(
    _admin: Admin = Depends(require_permission("system.settings.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(settings_service.list_settings(db))


@router.put("", summary="批量保存配置")
def save_settings(
    body: SettingsBatchBody,
    _admin: Admin = Depends(require_permission("system.settings.update")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    saved = settings_service.set_values(db, body.items)
    _after_save(db, {item["key"] for item in saved})
    return ok(saved)


@router.get("/runtime", summary="运行时非敏感配置子集（已登录）")
def runtime_settings(
    _admin: Admin = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(settings_service.runtime_settings(db))


@router.get("/{key}", summary="单个配置键")
def get_setting(
    key: SettingKey,
    locale: str = Query("*", max_length=10, description="* / zh-CN / en-US（system_info 用语言）"),
    _admin: Admin = Depends(require_permission("system.settings.view")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return ok(settings_service.get_setting(db, key, locale))


@router.put("/{key}", summary="保存单个配置键")
def save_setting(
    body: SettingUpdateBody,
    key: SettingKey,
    _admin: Admin = Depends(require_permission("system.settings.update")),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    saved = settings_service.set_value(db, key, body.value, body.locale)
    _after_save(db, {saved["key"]})
    return ok(saved)
