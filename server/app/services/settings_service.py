"""系统配置服务（``settings`` 表，docs/03 B.6）。

- 读：``get_config(db, key, locale)`` = ``DEFAULT_SETTINGS[key]`` 深合并数据库值，Redis ``cache:settings:{key}:{locale}`` 60s。
- 写：``set_value`` / ``set_values`` 把提交值深合并到当前值上，按 ``schemas/settings.py`` 同名模型校验，
  再校验模板 code 与模型目录引用，提交后 ``cache_delete_prefix("cache:settings:")``（含 ``cache:settings:runtime``）。
- 启动：``ensure_default_settings(db)`` 对每个配置键「键不存在则插入」默认值深合并环境变量派生值（``ENV_SEED_PATHS``）。
- 密钥永不入表：引用环境变量的字段（``credential_env`` / ``site_url_env`` / ``url_env``）只在同级附加 ``configured``。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings as env_settings
from app.core.exceptions import CODE_NOT_FOUND, BusinessError, field_error, format_validation_errors, invalid_params
from app.core.redis import cache_delete_prefix, cache_get_json, cache_set_json
from app.models import AiModel, PromptTemplate, Setting
from app.schemas.settings import (
    ENV_REFERENCE_FIELDS,
    SETTINGS_MODELS,
    SettingItem,
    env_is_configured,
)

logger = logging.getLogger(__name__)

CACHE_PREFIX = "cache:settings:"
CACHE_TTL_SECONDS = 60
RUNTIME_CACHE_KEY = "cache:settings:runtime"
ANY_LOCALE = "*"
SYSTEM_INFO_KEY = "system_info"
SYSTEM_INFO_LOCALES: tuple[str, ...] = ("zh-CN", "en-US")
SYSTEM_INFO_FALLBACK_LOCALE = "zh-CN"

# =====================================================================
# 默认值（各键结构的权威文档见 docs/03 B.6「settings 配置键清单」）
# =====================================================================

_GEO_PARSE_DEFAULT = {"citation_source": "annotations_or_markdown_links", "match_mode": "url_or_domain", "title_fuzzy_threshold": 0.8}


def _geo_engine(code: str, name: str, protocol: str = "openai_chat", extra: dict | None = None) -> dict[str, Any]:
    return {
        "code": code, "name": name, "enabled": False, "model": "", "protocol": protocol,
        "prompt_template_code": "sys_geo_query", "extra": extra or {}, "parse": dict(_GEO_PARSE_DEFAULT), "timeout_seconds": 120,
    }


DEFAULT_SETTINGS: dict[str, Any] = {
    # docs/09 §4.4
    "generation_config": {
        "version": 1,
        "default_language": "zh-CN",
        "review_required": True,
        "keyword": {"default_count": 20, "max_count": 50, "dedupe_scope": "project", "default_template_code": "sys_keyword", "intent_required": True},
        "title": {"default_count": 5, "max_count": 10, "default_style": "news", "default_template_code": "sys_title"},
        "content": {
            "outline_first": True, "segmented": True, "max_sections": 8,
            "target_word_count": 1500, "min_word_count": 300, "max_word_count": 6000,
            "include_faq": True, "faq_count": 3, "include_seo_meta": True,
            "default_format": "markdown",
            "template_codes": {"outline": "sys_outline", "content": "sys_content", "section": "sys_section", "seo_meta": "sys_seo_meta", "faq": "sys_faq"},
        },
        "rewrite": {
            "modes": ["rewrite", "expand", "shorten", "restyle"], "max_versions": 50,
            "template_codes": {"rewrite": "sys_rewrite", "expand": "sys_expand", "shorten": "sys_shorten", "restyle": "sys_restyle"},
        },
        "quality": {"min_word_count_ratio": 0.6, "require_h2": True, "max_h2": 12, "banned_words": [], "flag_duplicate_title": True},
        "rate_limits": {"generate_per_admin": "60/hour", "media_per_admin": "20/hour"},
        "quota": {"daily_limit": 0, "project_monthly_limit": 0, "warn_percent": 80},
    },
    # docs/10 §9
    "media_config": {
        "version": 1,
        "image": {
            "default_resolution": "1080p", "allowed_resolutions": ["1080p", "2k", "4k"],
            "default_aspect_ratio": "16:9", "allowed_aspect_ratios": ["1:1", "4:3", "3:4", "16:9", "9:16"],
            "max_reference_images": 9, "max_count_per_request": 4,
            "poll_budget_seconds": 600, "poll_intervals_seconds": [5, 10, 15, 30],
            "sync_fallback": True, "edit_extra_fields": [], "image_prompt_template_code": "sys_image_prompt",
        },
        "video": {
            "default_resolution": "720p", "allowed_resolutions": ["480p", "720p", "1080p", "4k"],
            "default_duration": 5, "max_duration": 15, "default_aspect_ratio": "16:9",
            "generate_audio_default": False,
            "poll_budget_seconds": 1200, "poll_intervals_seconds": [15, 30, 60],
            "max_download_mb": 500,
        },
        "transfer": {"max_attempts": 3, "retry_seconds": [30, 120, 600], "max_download_mb": 50},
        "retention": {"failed_days": 30, "orphan_reference_days": 7},
        "daily_limits": {"images": 200, "videos": 20},
    },
    # docs/11 §13.1
    "monitoring_config": {
        "version": 1,
        "link_check": {
            "enabled": True,
            "initial_days": 7, "initial_interval_hours": 24, "regular_interval_days": 7,
            "abnormal_backoff_hours": [6, 12, 24],
            "unknown_confirm_count": 3, "suspected_confirm_count": 2,
            "deleted_recheck_days": 7, "deleted_recheck_until_days": 30,
            "changed_simhash_distance": 20, "title_compare": True,
            "timeout_seconds": 15, "max_response_bytes": 2097152, "max_redirects": 3,
            "respect_robots": False, "allow_http": True, "user_agent": "aicreatLinkMonitor/1.0 (+https://example.com/contact)",
            "global_concurrency": 4, "per_domain_interval_seconds": 2,
            "daily_limit": 5000, "scan_interval_seconds": 60,
        },
        "index_check": {
            "enabled": True,
            "schedule_days": [1, 3, 7, 14, 30], "monthly_interval_days": 30, "indexed_recheck_days": 90,
            "seo_engines": ["baidu", "bing", "google"],
            "geo_engines": ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"],
            "query_by": ["url", "title"], "overdue_days": 30, "max_checks_per_link_per_engine": 24,
            "daily_limit": 2000, "scan_interval_seconds": 300, "concurrency": 2,
        },
    },
    # docs/11 §8.1（真实模式初值；Mock 模式由 ensure_default_settings 改为 enabled=true）
    "geo_engines": {
        "version": 1,
        "engines": [
            _geo_engine("baidu_ai", "百度 AI 搜索"),
            _geo_engine("doubao", "豆包"),
            _geo_engine("kimi", "Kimi"),
            _geo_engine("deepseek", "DeepSeek"),
            _geo_engine("perplexity", "Perplexity"),
            _geo_engine("chatgpt", "ChatGPT", protocol="openai_responses", extra={"tools": [{"type": "web_search"}]}),
        ],
    },
    # docs/11 §7.6
    "seo_providers": {
        "version": 1,
        "engines": {
            "baidu": {"provider": "zhiqi_web_search", "enabled": True, "model": "", "options": {}},
            "bing": {"provider": "zhiqi_web_search", "enabled": True, "model": "", "options": {}},
            "google": {"provider": "zhiqi_web_search", "enabled": False, "model": "", "options": {}},
        },
        "providers": {
            "zhiqi_web_search": {"prompt_template_code": "sys_seo_query", "protocol": "openai_chat", "timeout_seconds": 120, "match_mode": "url_or_domain", "extra": {}},
            "baidu_ai_search": {"endpoint": "https://qianfan.baidubce.com/v2/ai_search/web_search", "credential_env": "SEO_BAIDU_AI_SEARCH_API_KEY", "timeout_seconds": 30, "top_k": 10},
            "bing_webmaster": {"credential_env": "SEO_BING_WEBMASTER_API_KEY", "site_url_env": "SEO_BING_SITE_URL", "timeout_seconds": 30},
            "google_search_console": {"credential_env": "SEO_GSC_CREDENTIALS_FILE", "site_url_env": "SEO_GSC_SITE_URL", "timeout_seconds": 30},
            "manual": {},
        },
    },
    # docs/11 §10.3
    "alert_config": {
        "version": 1,
        "enabled": True,
        "rules": {
            "link_deleted": {"enabled": True, "severity": "warning"},
            "link_restored": {"enabled": True, "severity": "info"},
            "link_changed": {"enabled": True, "severity": "info"},
            "index_overdue": {"enabled": True, "severity": "warning", "days": 30, "min_checks": 3},
            "ai_task_failures": {"enabled": True, "severity": "critical", "consecutive": 5, "window_minutes": 30},
            "ai_breaker_open": {"enabled": True, "severity": "warning"},
            "ai_quota_exceeded": {"enabled": True, "severity": "critical"},
            "ai_auth_failed": {"enabled": True, "severity": "critical"},
            "ai_upstream_unavailable": {"enabled": True, "severity": "critical", "consecutive_probes": 2},
            "media_task_failed": {"enabled": True, "severity": "info"},
            "worker_stale": {"enabled": True, "severity": "warning", "minutes": 5},
        },
        "dedupe_cooldown_minutes": 60,
        "channels": {
            "in_app": {"enabled": True},
            "webhook": {"enabled": False, "url_env": "ALERT_WEBHOOK_URL", "min_severity": "warning"},
            "email": {"enabled": False, "to": [], "min_severity": "critical"},
        },
    },
    # docs/08 §4.2
    "ai_routing_config": {
        "version": 1,
        "retry": {"max_attempts": 3, "base_seconds": 1.0, "max_seconds": 30, "jitter": True, "retry_on": ["upstream_unavailable", "rate_limited", "timeout", "invalid_response"]},
        "breaker": {"failure_threshold": 5, "window_seconds": 300, "open_seconds": 120, "half_open_max_calls": 1},
        "fallback": {
            "enabled": True,
            "fallback_on": ["upstream_unavailable", "rate_limited", "timeout", "route_missing", "model_unrouted", "breaker_open", "media_storage", "invalid_response", "unsupported_parameter", "unknown"],
            "never_fallback_on": ["quota_exceeded", "auth_failed", "content_blocked", "transfer_failed", "cancelled"],
        },
        "pause_seconds": 600,
        "passthrough": {
            "openai_chat": ["tools", "tool_choice", "web_search_options", "reasoning_effort"],
            "openai_responses": ["tools", "tool_choice", "reasoning", "text"],
            "anthropic_messages": ["tools", "thinking"],
        },
        "timeouts": {"text_seconds": 180, "submit_seconds": 60, "poll_seconds": 30},
        "pricing": {"quota_per_unit": 500000, "usd_cny_rate": 7.2, "group_ratio": 1.0},
        "health": {"probe_interval_seconds": 600, "probe_text_prompt": "ping", "probe_max_tokens": 8, "probe_media": False, "degraded_latency_ms": 15000},
        "catalog": {"sync_interval_seconds": 3600, "hide_unavailable_after_days": 7},
        "usage": {"reconcile_interval_seconds": 300, "min_interval_seconds": 60, "max_new_per_pull_warn": 800, "unmatched_retention_days": 30},
    },
    # docs/12 §4.8
    "stats_config": {
        "version": 1,
        "timezone": "Asia/Shanghai",
        "daily_at": "00:30",
        "intraday_refresh_seconds": 600,
        "retention_days": 730,
        "rankings_limit": 10,
        "overview_cache_seconds": 60,
    },
    # docs/04 §7.2（按 locale 各一行）
    "system_info": {
        "zh-CN": {"site_name": "aicreat 内容生成平台", "logo_url": "", "footer": "", "support_contact": ""},
        "en-US": {"site_name": "aicreat Content Platform", "logo_url": "", "footer": "", "support_contact": ""},
    },
}

SETTING_KEYS: tuple[str, ...] = tuple(DEFAULT_SETTINGS)

# 环境变量 → 配置键路径（docs/05 §2.4；仅在 ensure_default_settings 首次插入时生效）
ENV_SEED_PATHS: dict[str, str] = {
    "APP_TIMEZONE": "stats_config.timezone",
    "GENERATE_RATE_LIMIT": "generation_config.rate_limits.generate_per_admin",
    "MEDIA_RATE_LIMIT": "generation_config.rate_limits.media_per_admin",
    "AI_DAILY_QUOTA_LIMIT": "generation_config.quota.daily_limit",
    "AI_PROJECT_MONTHLY_QUOTA_LIMIT": "generation_config.quota.project_monthly_limit",
    "ZHIQI_TIMEOUT_TEXT_SECONDS": "ai_routing_config.timeouts.text_seconds",
    "ZHIQI_TIMEOUT_SUBMIT_SECONDS": "ai_routing_config.timeouts.submit_seconds",
    "ZHIQI_TIMEOUT_POLL_SECONDS": "ai_routing_config.timeouts.poll_seconds",
    "ZHIQI_MAX_RETRIES": "ai_routing_config.retry.max_attempts",
    "ZHIQI_RETRY_BASE_SECONDS": "ai_routing_config.retry.base_seconds",
    "ZHIQI_RETRY_MAX_SECONDS": "ai_routing_config.retry.max_seconds",
    "ZHIQI_BREAKER_FAILURE_THRESHOLD": "ai_routing_config.breaker.failure_threshold",
    "ZHIQI_BREAKER_WINDOW_SECONDS": "ai_routing_config.breaker.window_seconds",
    "ZHIQI_BREAKER_OPEN_SECONDS": "ai_routing_config.breaker.open_seconds",
    "ZHIQI_IMAGE_POLL_BUDGET_SECONDS": "media_config.image.poll_budget_seconds",
    "ZHIQI_VIDEO_POLL_BUDGET_SECONDS": "media_config.video.poll_budget_seconds",
    "ZHIQI_IMAGE_SYNC_FALLBACK": "media_config.image.sync_fallback",
    "ZHIQI_USAGE_RECONCILE_INTERVAL_SECONDS": "ai_routing_config.usage.reconcile_interval_seconds",
    "ZHIQI_MODELS_SYNC_INTERVAL_SECONDS": "ai_routing_config.catalog.sync_interval_seconds",
    "ZHIQI_HEALTH_PROBE_INTERVAL_SECONDS": "ai_routing_config.health.probe_interval_seconds",
    "ZHIQI_QUOTA_PER_UNIT": "ai_routing_config.pricing.quota_per_unit",
    "ZHIQI_USD_CNY_RATE": "ai_routing_config.pricing.usd_cny_rate",
    "ZHIQI_GROUP_RATIO": "ai_routing_config.pricing.group_ratio",
    "MONITOR_USER_AGENT": "monitoring_config.link_check.user_agent",
    "MONITOR_FETCH_TIMEOUT_SECONDS": "monitoring_config.link_check.timeout_seconds",
    "MONITOR_MAX_RESPONSE_BYTES": "monitoring_config.link_check.max_response_bytes",
    "MONITOR_MAX_REDIRECTS": "monitoring_config.link_check.max_redirects",
    "MONITOR_CONCURRENCY": "monitoring_config.link_check.global_concurrency",
    "MONITOR_PER_DOMAIN_INTERVAL_SECONDS": "monitoring_config.link_check.per_domain_interval_seconds",
    "MONITOR_ALLOW_HTTP": "monitoring_config.link_check.allow_http",
}

# 模板 code 引用：配置路径 → 模板 kind（docs/09 §4.4、10 §9、11 §7.6 / §8.1）
TEMPLATE_CODE_REFS: dict[str, dict[tuple[str, ...], str]] = {
    "generation_config": {
        ("keyword", "default_template_code"): "keyword",
        ("title", "default_template_code"): "title",
        **{("content", "template_codes", k): k for k in ("outline", "content", "section", "seo_meta", "faq")},
        **{("rewrite", "template_codes", k): k for k in ("rewrite", "expand", "shorten", "restyle")},
    },
    "media_config": {("image", "image_prompt_template_code"): "image_prompt"},
    "seo_providers": {("providers", "zhiqi_web_search", "prompt_template_code"): "seo_query"},
}

_TITLE_PLACEHOLDER = re.compile(r"\{\{\s*title\s*\}\}")


# =====================================================================
# 通用工具
# =====================================================================


def deep_merge(base: Any, override: Any) -> Any:
    """深合并：两边都是 dict 时按键递归，否则 ``override`` 整体替换（数组整体替换）；返回新对象，不修改入参。"""
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        result: dict[str, Any] = {k: copy.deepcopy(v) for k, v in base.items()}
        for k, v in override.items():
            result[k] = deep_merge(result[k], v) if k in result else copy.deepcopy(v)
        return result
    return copy.deepcopy(override)


def set_path(target: dict[str, Any], path: str | Sequence[str], value: Any) -> dict[str, Any]:
    """按 ``a.b.c`` 写入嵌套 dict（缺失的中间层自动创建），返回 ``target``。"""
    parts = path.split(".") if isinstance(path, str) else list(path)
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value
    return target


def get_path(source: Any, path: str | Sequence[str], default: Any = None) -> Any:
    parts = path.split(".") if isinstance(path, str) else list(path)
    node = source
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def cache_key(key: str, locale: str) -> str:
    return f"{CACHE_PREFIX}{key}:{locale}"


def invalidate_cache() -> int:
    """清除全部配置缓存（含 ``cache:settings:runtime``）。"""
    return cache_delete_prefix(CACHE_PREFIX)


def is_known_key(key: str) -> bool:
    return key in DEFAULT_SETTINGS


def locales_for(key: str) -> tuple[str, ...]:
    """配置键的合法 locale：``system_info`` 为 ``zh-CN`` / ``en-US``，其余为 ``*``。"""
    return SYSTEM_INFO_LOCALES if key == SYSTEM_INFO_KEY else (ANY_LOCALE,)


def _require_key(key: str) -> None:
    if not is_known_key(key):
        raise BusinessError("配置键不存在", code=CODE_NOT_FOUND, data={"key": key})


def resolve_locale(key: str, locale: str | None, loc: Sequence[str | int] = ("query", "locale")) -> str:
    """校验并归一 locale：``system_info`` 的 ``*`` / 空值视为 ``zh-CN``；其它键只接受 ``*``；否则 400。"""
    _require_key(key)
    value = (locale or "").strip() or ANY_LOCALE
    if key == SYSTEM_INFO_KEY:
        if value == ANY_LOCALE:
            return SYSTEM_INFO_FALLBACK_LOCALE
        for candidate in SYSTEM_INFO_LOCALES:
            if value.lower() == candidate.lower():
                return candidate
    elif value == ANY_LOCALE:
        return ANY_LOCALE
    allowed = " / ".join(locales_for(key))
    raise invalid_params(field_error(loc, f"配置键 {key} 的 locale 只能为 {allowed}", "invalid_locale", locale))


def default_value(key: str, locale: str = ANY_LOCALE) -> dict[str, Any]:
    """配置键的默认 JSON（深拷贝）；``system_info`` 按 locale 取对应默认值。"""
    _require_key(key)
    if key == SYSTEM_INFO_KEY:
        per_locale = DEFAULT_SETTINGS[SYSTEM_INFO_KEY]
        return copy.deepcopy(per_locale.get(locale) or per_locale[SYSTEM_INFO_FALLBACK_LOCALE])
    return copy.deepcopy(DEFAULT_SETTINGS[key])


def _decode(raw: str | None, key: str, locale: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("settings 值不是合法 JSON，按默认值处理 key=%s locale=%s", key, locale)
        return None
    if not isinstance(value, dict):
        logger.warning("settings 值不是 JSON 对象，按默认值处理 key=%s locale=%s", key, locale)
        return None
    return value


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_row(db: Session, key: str, locale: str) -> Setting | None:
    return db.get(Setting, (key, locale))


# =====================================================================
# 读取
# =====================================================================


def get_value(db: Session, key: str, locale: str = ANY_LOCALE) -> dict[str, Any] | None:
    """数据库中存储的原始值（不合并默认值、不走缓存）；行不存在或损坏时返回 ``None``。"""
    locale = resolve_locale(key, locale)
    row = _load_row(db, key, locale)
    return _decode(row.value if row else None, key, locale)


def _merged_from_db(db: Session, key: str, locale: str) -> dict[str, Any]:
    stored = get_value(db, key, locale)
    if stored is None and key == SYSTEM_INFO_KEY and locale != SYSTEM_INFO_FALLBACK_LOCALE:
        fallback = get_value(db, key, SYSTEM_INFO_FALLBACK_LOCALE)
        if fallback is not None:  # 该语言行缺失 → 回退 zh-CN 行（docs/04 §7.2）
            return deep_merge(default_value(key, SYSTEM_INFO_FALLBACK_LOCALE), fallback)
    base = default_value(key, locale)
    return deep_merge(base, stored) if stored is not None else base


def get_config(db: Session, key: str, locale: str = ANY_LOCALE) -> dict[str, Any]:
    """``DEFAULT_SETTINGS[key]`` 深合并数据库值，缓存 ``cache:settings:{key}:{locale}`` 60s。"""
    locale = resolve_locale(key, locale)
    ck = cache_key(key, locale)
    cached = cache_get_json(ck)
    if isinstance(cached, dict):
        return cached
    value = _merged_from_db(db, key, locale)
    cache_set_json(ck, value, CACHE_TTL_SECONDS)
    return value


def get_setting(db: Session, key: str, locale: str = ANY_LOCALE, *, with_configured: bool = True) -> dict[str, Any]:
    """单键响应 ``{key, locale, value}``（``GET /admin/settings/{key}``）；默认附加 ``configured``。"""
    locale = resolve_locale(key, locale)
    value = get_config(db, key, locale)
    return {"key": key, "locale": locale, "value": with_configured_flags(value) if with_configured else value}


def list_settings(db: Session) -> list[dict[str, Any]]:
    """全部配置（``GET /admin/settings``）：9 个键（``system_info`` 两行），合并默认值并附加 ``configured``。"""
    items: list[dict[str, Any]] = []
    for key in SETTING_KEYS:
        for locale in locales_for(key):
            items.append({"key": key, "locale": locale, "value": with_configured_flags(get_config(db, key, locale))})
    return items


# =====================================================================
# 环境变量引用：configured 标记
# =====================================================================


def env_configured(name: str | None) -> bool:
    """被配置引用的环境变量是否非空（只返回布尔，永不返回值）。"""
    return env_is_configured(name)


def with_configured_flags(value: Any) -> Any:
    """返回副本：凡含 ``credential_env`` / ``site_url_env`` / ``url_env`` 的对象，在同级附加 ``configured``
    （引用的环境变量全部非空为 ``true``）。环境变量的值永不出现在结果中。"""
    if isinstance(value, Mapping):
        out = {k: with_configured_flags(v) for k, v in value.items() if k != "configured" or not _has_env_ref(value)}
        if _has_env_ref(value):
            names = [value.get(f) for f in ENV_REFERENCE_FIELDS if f in value]
            out["configured"] = bool(names) and all(env_configured(n) for n in names)
        return out
    if isinstance(value, list):
        return [with_configured_flags(v) for v in value]
    return copy.deepcopy(value)


def strip_configured_flags(value: Any) -> Any:
    """去掉 ``with_configured_flags`` 附加的 ``configured``（前端原样回传 GET 结果时不触发「未知字段」）。"""
    if isinstance(value, Mapping):
        has_ref = _has_env_ref(value)
        return {k: strip_configured_flags(v) for k, v in value.items() if not (has_ref and k == "configured")}
    if isinstance(value, list):
        return [strip_configured_flags(v) for v in value]
    return value


def _has_env_ref(value: Mapping[str, Any]) -> bool:
    return any(f in value for f in ENV_REFERENCE_FIELDS)


# =====================================================================
# 校验
# =====================================================================


def _validation_context() -> dict[str, Any]:
    return {"mock_mode": env_settings.zhiqi_mock_mode}


def _prefixed_errors(exc: ValidationError, loc_prefix: Sequence[str | int]) -> list[dict[str, Any]]:
    raw = []
    for err in exc.errors(include_url=False):
        item = dict(err)
        item["loc"] = (*loc_prefix, *err.get("loc", ()))
        raw.append(item)
    return format_validation_errors(raw)


def validate_value(key: str, value: Mapping[str, Any], *, loc_prefix: Sequence[str | int] = ("body", "value")) -> dict[str, Any]:
    """按同名模型校验完整配置值，返回规范化后的 JSON（``model_dump(mode="json")``）；失败抛 400。"""
    _require_key(key)
    model: type[BaseModel] = SETTINGS_MODELS[key]
    try:
        parsed = model.model_validate(dict(value), context=_validation_context())
    except ValidationError as exc:
        raise invalid_params(_prefixed_errors(exc, loc_prefix)) from None
    return parsed.model_dump(mode="json")


def _published_template(db: Session, code: str) -> PromptTemplate | None:
    stmt = (
        select(PromptTemplate)
        .where(PromptTemplate.code == code, PromptTemplate.status == "published")
        .order_by(PromptTemplate.version.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def _template_error(db: Session, code: str, kind: str, loc: Sequence[str | int]) -> dict[str, Any] | None:
    tpl = _published_template(db, code)
    if tpl is None:
        return field_error(loc, "该 code 无已发布版本", "template_not_published", code)
    if tpl.project_id != 0:
        return field_error(loc, "该 code 不是全局模板", "template_not_global", code)
    if tpl.kind != kind:
        return field_error(loc, "模板 kind 不匹配", "kind_mismatch", code)
    return None


def _model_error(db: Session, model_id: str, loc: Sequence[str | int]) -> dict[str, Any] | None:
    row = db.execute(select(AiModel).where(AiModel.model_id == model_id).limit(1)).scalars().first()
    modalities: list[Any] = []
    if row is not None and row.modalities_json:
        try:
            parsed = json.loads(row.modalities_json)
            modalities = parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            modalities = []
    if row is None or "text" not in modalities:
        return field_error(loc, "模型不存在或模态不匹配", "invalid_model", model_id)
    if not row.is_available:
        return field_error(loc, "模型当前不可用", "model_unavailable", model_id)
    return None


def _changed(new: Any, old: Any) -> bool:
    return new != old


def reference_errors(
    db: Session,
    key: str,
    value: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    *,
    loc_prefix: Sequence[str | int] = ("body", "value"),
) -> list[dict[str, Any]]:
    """需要查库的引用校验（模板 code、模型目录、``title_in_prompt``），只检查相对 ``previous`` 有变化的引用，
    使未初始化系统模板 / 模型目录的环境也能修改其它字段。"""
    previous = previous or {}
    errors: list[dict[str, Any]] = []

    for path, kind in TEMPLATE_CODE_REFS.get(key, {}).items():
        code = get_path(value, path)
        if code and _changed(code, get_path(previous, path)):
            err = _template_error(db, code, kind, (*loc_prefix, *path))
            if err:
                errors.append(err)

    if key == "geo_engines":
        old_by_code = {e.get("code"): e for e in previous.get("engines") or [] if isinstance(e, Mapping)}
        for i, engine in enumerate(value.get("engines") or []):
            old = old_by_code.get(engine["code"]) or {}
            base_loc = (*loc_prefix, "engines", i)
            code = engine.get("prompt_template_code")
            template_changed = _changed(code, old.get("prompt_template_code"))
            if code and template_changed:
                err = _template_error(db, code, "geo_query", (*base_loc, "prompt_template_code"))
                if err:
                    errors.append(err)
                    continue
            if engine.get("model") and _changed(engine.get("model"), old.get("model")):
                err = _model_error(db, engine["model"], (*base_loc, "model"))
                if err:
                    errors.append(err)
            match_mode = get_path(engine, "parse.match_mode")
            if match_mode == "title" and (template_changed or get_path(old, "parse.match_mode") != "title"):
                tpl = _published_template(db, code) if code else None
                if tpl is not None and _TITLE_PLACEHOLDER.search(tpl.user_prompt or ""):
                    errors.append(
                        field_error(
                            (*base_loc, "parse", "match_mode"),
                            "match_mode=title 时提示模板的 user_prompt 不得包含 {{title}}",
                            "title_in_prompt",
                            match_mode,
                        )
                    )

    if key == "seo_providers":
        old_engines = previous.get("engines") or {}
        for engine, cfg in (value.get("engines") or {}).items():
            model = cfg.get("model")
            if model and _changed(model, get_path(old_engines, (engine, "model"))):
                err = _model_error(db, model, (*loc_prefix, "engines", engine, "model"))
                if err:
                    errors.append(err)
    return errors


def prepare_value(
    db: Session,
    key: str,
    value: Mapping[str, Any],
    locale: str = ANY_LOCALE,
    *,
    loc_prefix: Sequence[str | int] = ("body", "value"),
) -> tuple[str, dict[str, Any]]:
    """把提交值（可为局部）深合并到当前生效值上并完成全部校验，返回 ``(locale, 规范化完整值)``；不写库。"""
    _require_key(key)
    locale = resolve_locale(key, locale, loc=(*loc_prefix[:-1], "locale") if loc_prefix else ("body", "locale"))
    if not isinstance(value, Mapping):
        raise invalid_params(field_error(loc_prefix, "必须是对象", "dict_type", None))
    current = _merged_from_db(db, key, locale)
    merged = deep_merge(current, strip_configured_flags(value))
    normalized = validate_value(key, merged, loc_prefix=loc_prefix)
    errors = reference_errors(db, key, normalized, current, loc_prefix=loc_prefix)
    if errors:
        raise invalid_params(errors)
    return locale, normalized


def _write_row(db: Session, key: str, locale: str, value: Mapping[str, Any]) -> None:
    row = _load_row(db, key, locale)
    if row is None:
        db.add(Setting(key=key, locale=locale, value=_encode(value)))
    else:
        row.value = _encode(value)


# =====================================================================
# 写入
# =====================================================================


def set_value(db: Session, key: str, value: Mapping[str, Any], locale: str = ANY_LOCALE) -> dict[str, Any]:
    """``PUT /admin/settings/{key}``：校验后写库并提交，清 ``cache:settings:*``，返回 ``{key, locale, value}``
    （``value`` 为与默认值深合并后的完整值，附 ``configured``）。未知键 404，校验失败 400。"""
    locale, normalized = prepare_value(db, key, value, locale)
    try:
        _write_row(db, key, locale, normalized)
        db.commit()
    except Exception:
        db.rollback()
        raise
    invalidate_cache()
    return {"key": key, "locale": locale, "value": with_configured_flags(normalized)}


def set_values(db: Session, items: Iterable[SettingItem | Mapping[str, Any]]) -> list[dict[str, Any]]:
    """``PUT /admin/settings``：逐项校验（错误 ``loc`` 为 ``["body","items",i,…]``），任一失败整体 400、不写库；
    全部通过后在同一事务写入并清缓存。"""
    prepared: list[tuple[str, str, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i, item in enumerate(items):
        data = item.model_dump() if isinstance(item, BaseModel) else dict(item)
        key = str(data.get("key") or "").strip()
        base = ("body", "items", i)
        if not is_known_key(key):
            errors.append(field_error((*base, "key"), "配置键不存在", "unknown_key", key))
            continue
        try:
            locale, normalized = prepare_value(db, key, data.get("value") or {}, data.get("locale") or ANY_LOCALE, loc_prefix=(*base, "value"))
        except BusinessError as exc:
            errors.extend(exc.data if isinstance(exc.data, list) else [field_error(base, exc.message, "value_error", None)])
            continue
        if (key, locale) in seen:
            errors.append(field_error((*base, "key"), "同一配置键重复提交", "duplicate", key))
            continue
        seen.add((key, locale))
        prepared.append((key, locale, normalized))
    if errors:
        raise invalid_params(errors)
    try:
        for key, locale, normalized in prepared:
            _write_row(db, key, locale, normalized)
        db.commit()
    except Exception:
        db.rollback()
        raise
    invalidate_cache()
    return [{"key": k, "locale": loc, "value": with_configured_flags(v)} for k, loc, v in prepared]


# =====================================================================
# 启动 seed
# =====================================================================


def env_seed_overrides() -> dict[str, dict[str, Any]]:
    """按 ``ENV_SEED_PATHS`` 从当前环境变量派生的覆盖值 ``{key: {…}}``。"""
    overrides: dict[str, dict[str, Any]] = {}
    for env_name, path in ENV_SEED_PATHS.items():
        attr = env_name.lower()
        if not hasattr(env_settings, attr):
            continue
        value = getattr(env_settings, attr)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        key, _, rest = path.partition(".")
        set_path(overrides.setdefault(key, {}), rest, value)
    return overrides


def seed_value(key: str, locale: str = ANY_LOCALE, *, mock_mode: bool | None = None) -> dict[str, Any]:
    """首次插入使用的值：默认值深合并环境变量派生值；Mock 模式下 ``geo_engines`` 全部引擎 ``enabled=true``。
    派生值未通过模型校验时记 error 日志并回退纯默认值（不阻塞启动）。"""
    mock = env_settings.zhiqi_mock_mode if mock_mode is None else mock_mode
    base = default_value(key, locale)
    value = deep_merge(base, env_seed_overrides().get(key, {})) if key != SYSTEM_INFO_KEY else base
    if key == "geo_engines" and mock:
        for engine in value.get("engines", []):
            engine["enabled"] = True
    context = {"mock_mode": mock}
    model = SETTINGS_MODELS[key]
    try:
        return model.model_validate(value, context=context).model_dump(mode="json")
    except ValidationError as exc:
        logger.error("环境变量派生的 %s 初值不合法，改用默认值：%s", key, exc.errors(include_url=False))
    if key == "geo_engines" and mock:
        for engine in base.get("engines", []):
            engine["enabled"] = True
    return model.model_validate(base, context=context).model_dump(mode="json")


def ensure_default_settings(db: Session) -> list[tuple[str, str]]:
    """启动钩子：对 9 个配置键（``system_info`` 两行）「键不存在则插入」，已存在的不改写；幂等，
    并发插入冲突（``IntegrityError``）逐条回滚跳过。返回本次插入的 ``(key, locale)``。"""
    existing = {(row.key, row.locale) for row in db.execute(select(Setting.key, Setting.locale)).all()}
    inserted: list[tuple[str, str]] = []
    for key in SETTING_KEYS:
        for locale in locales_for(key):
            if (key, locale) in existing:
                continue
            db.add(Setting(key=key, locale=locale, value=_encode(seed_value(key, locale))))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                continue
            inserted.append((key, locale))
            logger.info("已写入默认配置 key=%s locale=%s", key, locale)
    if inserted:
        invalidate_cache()
    return inserted


# =====================================================================
# 运行时子集（GET /admin/settings/runtime，docs/04 §7.18）
# =====================================================================


def _pick(source: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {f: copy.deepcopy(source[f]) for f in fields if f in source}


def build_runtime(db: Session) -> dict[str, Any]:
    gen = get_config(db, "generation_config")
    media = get_config(db, "media_config")
    mon = get_config(db, "monitoring_config")
    geo = get_config(db, "geo_engines")
    seo = get_config(db, "seo_providers")
    stats = get_config(db, "stats_config")
    return {
        "generation_config": {
            "review_required": gen.get("review_required"),
            "keyword": _pick(gen.get("keyword", {}), ("default_count", "max_count")),
            "title": _pick(gen.get("title", {}), ("default_count", "max_count", "default_style")),
            "content": _pick(
                gen.get("content", {}),
                ("outline_first", "segmented", "max_sections", "target_word_count", "min_word_count", "max_word_count",
                 "include_faq", "include_seo_meta", "default_format"),
            ),
            "rewrite": _pick(gen.get("rewrite", {}), ("modes", "max_versions")),
        },
        "media_config": {
            "image": _pick(
                media.get("image", {}),
                ("default_resolution", "allowed_resolutions", "default_aspect_ratio", "allowed_aspect_ratios",
                 "max_reference_images", "max_count_per_request", "poll_budget_seconds"),
            ),
            "video": _pick(
                media.get("video", {}),
                ("default_resolution", "allowed_resolutions", "default_duration", "max_duration", "default_aspect_ratio",
                 "generate_audio_default", "poll_budget_seconds"),
            ),
            "daily_limits": _pick(media.get("daily_limits", {}), ("images", "videos")),
        },
        "monitoring_config": {
            "index_check": _pick(mon.get("index_check", {}), ("seo_engines", "geo_engines", "query_by")),
        },
        "geo_engines": {
            "engines": [_pick(e, ("code", "name", "enabled")) for e in geo.get("engines", []) if isinstance(e, Mapping)],
        },
        "seo_providers": {
            "engines": {
                name: _pick(cfg, ("provider", "enabled"))
                for name, cfg in (seo.get("engines") or {}).items()
                if isinstance(cfg, Mapping)
            },
        },
        "stats_config": {"timezone": stats.get("timezone")},
        "zhiqi_mode": "mock" if env_settings.zhiqi_mock_mode else "live",
    }


def runtime_settings(db: Session) -> dict[str, Any]:
    """前端表单所需的非敏感运行时子集，缓存 ``cache:settings:runtime`` 60s。"""
    cached = cache_get_json(RUNTIME_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    value = build_runtime(db)
    cache_set_json(RUNTIME_CACHE_KEY, value, CACHE_TTL_SECONDS)
    return value
