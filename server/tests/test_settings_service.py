"""settings_service / schemas.settings / i18n：默认值合并、环境变量 seed、校验、缓存失效、runtime 子集、configured 标记。"""

from __future__ import annotations

import os

# 须在导入 app.core 之前设置（conftest 已设置时不覆盖）
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("DATABASE_URL", "sqlite://")

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app import models as m
from app.core.config import settings as env_settings
from app.core.database import Base, make_engine
from app.core.exceptions import BusinessError
from app.core.redis import redis_client
from app.schemas.settings import SETTINGS_MODELS, GeoEngines, SettingsBatchBody, SeoProviders
from app.services import settings_service as svc
from app.services.i18n import normalize_locale, pick_locale


def _redis_on_db15() -> bool:
    try:
        return redis_client.connection_pool.connection_kwargs.get("db") == 15 and bool(redis_client.ping())
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _redis_on_db15(), reason="需要 Redis db 15")


@pytest.fixture(autouse=True)
def _flush_redis() -> Iterator[None]:
    redis_client.flushdb()
    yield
    redis_client.flushdb()


@pytest.fixture
def db() -> Iterator[Session]:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "zhiqi_api_key", "sk-test")


@pytest.fixture
def mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "zhiqi_api_key", "")


def _errors(exc: pytest.ExceptionInfo[BusinessError]) -> list[dict]:
    assert exc.value.code == 400
    assert exc.value.http_status == 400
    return exc.value.data


def _add_template(db: Session, code: str, kind: str, *, project_id: int = 0, status: str = "published",
                  user_prompt: str = "请回答 {{keyword}}") -> None:
    db.add(m.PromptTemplate(
        code=code, version=1, kind=kind, capability="content", name=code, language="zh-CN", project_id=project_id,
        user_prompt=user_prompt, output_format="text", status=status, is_system=False, created_by=1, updated_by=1,
    ))
    db.commit()


def _add_model(db: Session, model_id: str, *, modalities: list[str], available: bool = True) -> None:
    db.add(m.AiModel(model_id=model_id, modalities_json=json.dumps(modalities), is_available=available, synced_at=m.utcnow()))
    db.commit()


# ---------------------------------------------------------------- 工具与默认值


def test_deep_merge_replaces_lists_and_does_not_mutate() -> None:
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    override = {"a": {"c": [3]}, "e": {"f": 1}}
    merged = svc.deep_merge(base, override)
    assert merged == {"a": {"b": 1, "c": [3]}, "d": 1, "e": {"f": 1}}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    merged["e"]["f"] = 2
    assert override["e"]["f"] == 1
    assert svc.deep_merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}


def test_default_settings_cover_all_keys_and_validate() -> None:
    assert set(svc.DEFAULT_SETTINGS) == set(SETTINGS_MODELS) == {
        "generation_config", "media_config", "monitoring_config", "geo_engines", "seo_providers",
        "alert_config", "ai_routing_config", "stats_config", "system_info",
    }
    for key in svc.SETTING_KEYS:
        for locale in svc.locales_for(key):
            default = svc.default_value(key, locale)
            dumped = SETTINGS_MODELS[key].model_validate(default, context={"mock_mode": False}).model_dump(mode="json")
            assert dumped == default, key  # 默认值即规范化结果（数值 30 == 30.0）
            # 模型自身的字段默认值与 DEFAULT_SETTINGS 一致（geo_engines 的引擎列表除外）
            if key != "geo_engines":
                assert SETTINGS_MODELS[key]().model_dump(mode="json") == svc.default_value(key, "zh-CN" if key == "system_info" else "*"), key


def test_default_values_match_docs_examples() -> None:
    d = svc.DEFAULT_SETTINGS
    assert d["generation_config"]["keyword"]["max_count"] == 50
    assert d["generation_config"]["content"]["template_codes"]["seo_meta"] == "sys_seo_meta"
    assert d["media_config"]["transfer"]["retry_seconds"] == [30, 120, 600]
    assert d["monitoring_config"]["link_check"]["max_response_bytes"] == 2097152
    assert [e["code"] for e in d["geo_engines"]["engines"]] == ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"]
    assert all(e["enabled"] is False for e in d["geo_engines"]["engines"])
    assert d["geo_engines"]["engines"][-1]["protocol"] == "openai_responses"
    assert d["geo_engines"]["engines"][-1]["extra"] == {"tools": [{"type": "web_search"}]}
    assert d["seo_providers"]["engines"]["google"]["enabled"] is False
    assert d["alert_config"]["rules"]["index_overdue"] == {"enabled": True, "severity": "warning", "days": 30, "min_checks": 3}
    assert d["ai_routing_config"]["usage"]["max_new_per_pull_warn"] == 800
    assert d["stats_config"] == {
        "version": 1, "timezone": "Asia/Shanghai", "daily_at": "00:30", "intraday_refresh_seconds": 600,
        "retention_days": 730, "rankings_limit": 10, "overview_cache_seconds": 60,
    }
    assert d["system_info"]["zh-CN"] == {"site_name": "aicreat 内容生成平台", "logo_url": "", "footer": "", "support_contact": ""}
    assert set(d["system_info"]["en-US"]) == {"site_name", "logo_url", "footer", "support_contact"}


def test_env_seed_paths_point_to_existing_default_paths() -> None:
    for env_name, path in svc.ENV_SEED_PATHS.items():
        key, _, rest = path.partition(".")
        assert svc.get_path(svc.DEFAULT_SETTINGS[key], rest, default=KeyError) is not KeyError, path
        assert hasattr(env_settings, env_name.lower()), env_name
    assert len(svc.ENV_SEED_PATHS) == 30


# ---------------------------------------------------------------- 读取与合并


def test_get_config_without_row_returns_defaults(db: Session) -> None:
    assert svc.get_config(db, "stats_config") == svc.DEFAULT_SETTINGS["stats_config"]
    assert svc.get_config(db, "system_info", "en-US") == svc.DEFAULT_SETTINGS["system_info"]["en-US"]
    assert svc.get_config(db, "system_info") == svc.DEFAULT_SETTINGS["system_info"]["zh-CN"]


def test_get_config_deep_merges_stored_value(db: Session) -> None:
    db.add(m.Setting(key="generation_config", locale="*", value=json.dumps({"keyword": {"default_count": 10}, "new_field": 1})))
    db.commit()
    cfg = svc.get_config(db, "generation_config")
    assert cfg["keyword"]["default_count"] == 10
    assert cfg["keyword"]["max_count"] == 50  # 来自默认值
    assert cfg["new_field"] == 1
    assert svc.get_value(db, "generation_config") == {"keyword": {"default_count": 10}, "new_field": 1}


def test_corrupt_row_falls_back_to_default(db: Session) -> None:
    db.add(m.Setting(key="stats_config", locale="*", value="not json"))
    db.commit()
    assert svc.get_config(db, "stats_config") == svc.DEFAULT_SETTINGS["stats_config"]


def test_system_info_missing_locale_falls_back_to_zh_cn_row(db: Session) -> None:
    db.add(m.Setting(key="system_info", locale="zh-CN", value=json.dumps({"site_name": "我的平台"})))
    db.commit()
    assert svc.get_config(db, "system_info", "en-US")["site_name"] == "我的平台"


def test_unknown_key_and_bad_locale(db: Session) -> None:
    with pytest.raises(BusinessError) as exc:
        svc.get_config(db, "nope")
    assert exc.value.code == 404
    with pytest.raises(BusinessError) as exc2:
        svc.get_config(db, "generation_config", "zh-CN")
    assert _errors(exc2)[0]["type"] == "invalid_locale"
    with pytest.raises(BusinessError):
        svc.get_config(db, "system_info", "fr-FR")
    assert svc.resolve_locale("system_info", "en-us") == "en-US"


# ---------------------------------------------------------------- ensure_default_settings / 环境变量 seed


def test_ensure_default_settings_inserts_missing_only(db: Session, live_mode: None) -> None:
    inserted = svc.ensure_default_settings(db)
    assert len(inserted) == 10
    assert ("system_info", "zh-CN") in inserted and ("system_info", "en-US") in inserted
    assert svc.ensure_default_settings(db) == []

    row = db.get(m.Setting, ("stats_config", "*"))
    row.value = json.dumps({"timezone": "UTC"})
    db.commit()
    db.delete(db.get(m.Setting, ("media_config", "*")))
    db.commit()
    assert svc.ensure_default_settings(db) == [("media_config", "*")]
    assert json.loads(db.get(m.Setting, ("stats_config", "*")).value) == {"timezone": "UTC"}  # 已存在不改写


def test_ensure_default_settings_uses_env_seed(db: Session, live_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "app_timezone", "UTC")
    monkeypatch.setattr(env_settings, "generate_rate_limit", "30/minute")
    monkeypatch.setattr(env_settings, "ai_daily_quota_limit", 1000)
    monkeypatch.setattr(env_settings, "zhiqi_timeout_text_seconds", 90)
    monkeypatch.setattr(env_settings, "zhiqi_image_sync_fallback", False)
    monkeypatch.setattr(env_settings, "monitor_allow_http", False)
    monkeypatch.setattr(env_settings, "monitor_user_agent", "Bot/2.0 (+https://corp.example/contact)")
    monkeypatch.setattr(env_settings, "zhiqi_usd_cny_rate", 7.1)
    svc.ensure_default_settings(db)

    assert svc.get_config(db, "stats_config")["timezone"] == "UTC"
    gen = svc.get_config(db, "generation_config")
    assert gen["rate_limits"]["generate_per_admin"] == "30/minute"
    assert gen["quota"]["daily_limit"] == 1000
    routing = svc.get_config(db, "ai_routing_config")
    assert routing["timeouts"]["text_seconds"] == 90
    assert routing["pricing"]["usd_cny_rate"] == 7.1
    assert svc.get_config(db, "media_config")["image"]["sync_fallback"] is False
    link = svc.get_config(db, "monitoring_config")["link_check"]
    assert link["allow_http"] is False
    assert link["user_agent"] == "Bot/2.0 (+https://corp.example/contact)"
    # 真实模式：GEO 引擎全部未启用
    assert all(not e["enabled"] for e in svc.get_config(db, "geo_engines")["engines"])


def test_mock_mode_seeds_geo_engines_enabled(db: Session, mock_mode: None) -> None:
    svc.ensure_default_settings(db)
    engines = svc.get_config(db, "geo_engines")["engines"]
    assert len(engines) == 6 and all(e["enabled"] and e["model"] == "" for e in engines)


def test_invalid_env_seed_falls_back_to_default(db: Session, live_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "app_timezone", "Mars/Olympus")
    monkeypatch.setattr(env_settings, "generate_rate_limit", "lots")
    svc.ensure_default_settings(db)
    assert svc.get_config(db, "stats_config")["timezone"] == "Asia/Shanghai"
    assert svc.get_config(db, "generation_config")["rate_limits"]["generate_per_admin"] == "60/hour"


# ---------------------------------------------------------------- 写入与校验


def test_set_value_merges_partial_and_returns_full(db: Session, live_mode: None) -> None:
    svc.ensure_default_settings(db)
    out = svc.set_value(db, "alert_config", {"rules": {"index_overdue": {"enabled": True, "severity": "warning", "days": 45, "min_checks": 3}}})
    assert out["key"] == "alert_config" and out["locale"] == "*"
    assert out["value"]["rules"]["index_overdue"]["days"] == 45
    assert out["value"]["rules"]["worker_stale"]["minutes"] == 5
    assert out["value"]["channels"]["webhook"]["configured"] is False
    stored = svc.get_value(db, "alert_config")
    assert stored["rules"]["index_overdue"]["days"] == 45
    assert "configured" not in stored["channels"]["webhook"]  # 标记不入库
    # 第二次局部保存保留第一次的修改
    svc.set_value(db, "alert_config", {"dedupe_cooldown_minutes": 10})
    cfg = svc.get_config(db, "alert_config")
    assert cfg["dedupe_cooldown_minutes"] == 10 and cfg["rules"]["index_overdue"]["days"] == 45


def test_set_value_normalizes(db: Session) -> None:
    out = svc.set_value(db, "generation_config", {"quality": {"banned_words": [" 违禁 ", "违禁", "", "x"]}, "rate_limits": {"media_per_admin": " 5 / day "}})
    assert out["value"]["quality"]["banned_words"] == ["违禁", "x"]
    assert out["value"]["rate_limits"]["media_per_admin"] == "5/day"


@pytest.mark.parametrize(
    ("key", "value", "loc_tail", "err_type"),
    [
        ("generation_config", {"keyword": {"default_count": 60}}, ["keyword"], "value_error"),
        ("generation_config", {"keyword": {"max_count": 101}}, ["keyword", "max_count"], "less_than_equal"),
        ("generation_config", {"title": {"default_style": "poem"}}, ["title", "default_style"], "literal_error"),
        ("generation_config", {"content": {"min_word_count": 2000}}, ["content"], "value_error"),
        ("generation_config", {"rewrite": {"modes": []}}, ["rewrite", "modes"], "too_short"),
        ("generation_config", {"rewrite": {"max_versions": 4}}, ["rewrite", "max_versions"], "greater_than_equal"),
        ("generation_config", {"quality": {"min_word_count_ratio": 0}}, ["quality", "min_word_count_ratio"], "greater_than"),
        ("generation_config", {"quality": {"banned_words": ["x" * 51]}}, ["quality", "banned_words"], "value_error"),
        ("generation_config", {"rate_limits": {"generate_per_admin": "0/hour"}}, ["rate_limits", "generate_per_admin"], "value_error"),
        ("generation_config", {"rate_limits": {"generate_per_admin": "10/week"}}, ["rate_limits", "generate_per_admin"], "value_error"),
        ("generation_config", {"quota": {"warn_percent": 0}}, ["quota", "warn_percent"], "greater_than_equal"),
        ("generation_config", {"unknown": 1}, ["unknown"], "extra_forbidden"),
        ("media_config", {"image": {"allowed_resolutions": ["2k"]}}, ["image"], "value_error"),
        ("media_config", {"image": {"max_count_per_request": 5}}, ["image", "max_count_per_request"], "less_than_equal"),
        ("media_config", {"image": {"poll_intervals_seconds": [10, 5]}}, ["image", "poll_intervals_seconds"], "value_error"),
        ("media_config", {"image": {"edit_extra_fields": ["size"]}}, ["image", "edit_extra_fields", 0], "literal_error"),
        ("media_config", {"video": {"default_duration": 20}}, ["video"], "value_error"),
        ("media_config", {"video": {"default_aspect_ratio": "wide"}}, ["video", "default_aspect_ratio"], "value_error"),
        ("media_config", {"video": {"poll_budget_seconds": 3601}}, ["video", "poll_budget_seconds"], "less_than_equal"),
        ("media_config", {"transfer": {"max_attempts": 5}}, ["transfer"], "value_error"),
        ("monitoring_config", {"link_check": {"abnormal_backoff_hours": [6, 12]}}, ["link_check", "abnormal_backoff_hours"], "value_error"),
        ("monitoring_config", {"link_check": {"abnormal_backoff_hours": [6, 6, 24]}}, ["link_check", "abnormal_backoff_hours"], "value_error"),
        ("monitoring_config", {"link_check": {"unknown_confirm_count": 11}}, ["link_check", "unknown_confirm_count"], "less_than_equal"),
        ("monitoring_config", {"link_check": {"changed_simhash_distance": 64}}, ["link_check", "changed_simhash_distance"], "less_than_equal"),
        ("monitoring_config", {"link_check": {"max_response_bytes": 10 * 1024 * 1024 + 1}}, ["link_check", "max_response_bytes"], "less_than_equal"),
        ("monitoring_config", {"link_check": {"max_redirects": 6}}, ["link_check", "max_redirects"], "less_than_equal"),
        ("monitoring_config", {"link_check": {"global_concurrency": 17}}, ["link_check", "global_concurrency"], "less_than_equal"),
        ("monitoring_config", {"index_check": {"schedule_days": [1, 3, 3]}}, ["index_check", "schedule_days"], "value_error"),
        ("monitoring_config", {"index_check": {"indexed_recheck_days": 30}}, ["index_check"], "value_error"),
        ("monitoring_config", {"index_check": {"query_by": []}}, ["index_check", "query_by"], "too_short"),
        ("monitoring_config", {"index_check": {"query_by": ["keyword"]}}, ["index_check", "query_by", 0], "literal_error"),
        ("monitoring_config", {"index_check": {"seo_engines": ["yandex"]}}, ["index_check", "seo_engines", 0], "literal_error"),
        ("monitoring_config", {"index_check": {"concurrency": 9}}, ["index_check", "concurrency"], "less_than_equal"),
        ("alert_config", {"rules": {"link_deleted": {"severity": "fatal"}}}, ["rules", "link_deleted", "severity"], "literal_error"),
        ("alert_config", {"rules": {"index_overdue": {"days": 0}}}, ["rules", "index_overdue", "days"], "greater_than_equal"),
        ("alert_config", {"channels": {"in_app": {"enabled": False}}}, ["channels", "in_app", "enabled"], "literal_error"),
        ("alert_config", {"channels": {"email": {"to": ["not-an-email"]}}}, ["channels", "email", "to"], "value_error"),
        ("ai_routing_config", {"retry": {"retry_on": ["boom"]}}, ["retry", "retry_on", 0], "literal_error"),
        ("ai_routing_config", {"retry": {"base_seconds": 10, "max_seconds": 5}}, ["retry"], "value_error"),
        ("ai_routing_config", {"health": {"degraded_latency_ms": 30000}}, ["health", "degraded_latency_ms"], "less_than"),
        ("ai_routing_config", {"passthrough": {"openai_chat": ["model"]}}, ["passthrough", "openai_chat"], "value_error"),
        ("stats_config", {"timezone": "Mars/Olympus"}, ["timezone"], "invalid_timezone"),
        ("stats_config", {"daily_at": "24:00"}, ["daily_at"], "value_error"),
        ("stats_config", {"intraday_refresh_seconds": 30}, ["intraday_refresh_seconds"], "value_error"),
        ("stats_config", {"retention_days": 29}, ["retention_days"], "greater_than_equal"),
        ("stats_config", {"rankings_limit": 101}, ["rankings_limit"], "less_than_equal"),
        ("stats_config", {"overview_cache_seconds": 3601}, ["overview_cache_seconds"], "less_than_equal"),
        ("system_info", {"site_name": ""}, ["site_name"], "string_too_short"),
        ("system_info", {"logo_url": "javascript:alert(1)"}, ["logo_url"], "value_error"),
        ("system_info", {"extra": 1}, ["extra"], "extra_forbidden"),
        ("geo_engines", {"engines": [{"code": "Bad-Code", "name": "x"}]}, ["engines", 0, "code"], "string_pattern_mismatch"),
        ("geo_engines", {"engines": [{"code": "a1", "name": "x"}, {"code": "a1", "name": "y"}]}, [], "value_error"),
        ("geo_engines", {"engines": [{"code": "a1", "name": "x", "parse": {"title_fuzzy_threshold": 1.5}}]}, ["engines", 0, "parse", "title_fuzzy_threshold"], "less_than_equal"),
        ("seo_providers", {"engines": {"bing": {"provider": "baidu_ai_search"}}}, [], "provider_engine_mismatch"),
        ("seo_providers", {"engines": {"yahoo": {"provider": "manual"}}}, ["engines", "yahoo", "[key]"], "literal_error"),
        ("seo_providers", {"providers": {"baidu_ai_search": {"credential_env": "ADMIN_JWT_SECRET"}}}, ["providers", "baidu_ai_search", "credential_env"], "invalid_env_name"),
        ("seo_providers", {"providers": {"baidu_ai_search": {"endpoint": "http://insecure.example/x"}}}, ["providers", "baidu_ai_search", "endpoint"], "value_error"),
    ],
)
def test_set_value_validation_failures(db: Session, key: str, value: dict, loc_tail: list, err_type: str) -> None:
    locale = "zh-CN" if key == "system_info" else "*"
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, key, value, locale)
    errors = _errors(exc)
    assert any(e["loc"] == ["body", "value", *loc_tail] and e["type"] == err_type for e in errors), errors
    assert all(set(e) <= {"loc", "msg", "type", "input"} for e in errors)
    assert all(isinstance(e["msg"], str) and e["msg"] for e in errors)
    assert svc.get_value(db, key, locale) is None  # 未写库


def test_validation_messages_are_chinese(db: Session) -> None:
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "generation_config", {"keyword": {"max_count": 120}})
    assert _errors(exc) == [
        {"loc": ["body", "value", "keyword", "max_count"], "msg": "不能大于 100", "type": "less_than_equal", "input": 120}
    ]
    with pytest.raises(BusinessError) as exc2:
        svc.set_value(db, "generation_config", {"keyword": {"default_count": 60}})
    assert _errors(exc2)[0]["msg"] == "default_count 不能大于 max_count"


def test_poll_intervals_allow_equal_steps(db: Session) -> None:
    out = svc.set_value(db, "media_config", {"image": {"poll_intervals_seconds": [1, 1, 1]}})
    assert out["value"]["image"]["poll_intervals_seconds"] == [1, 1, 1]


def test_geo_engines_enable_rules_live_vs_mock(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    value = {"engines": [{"code": "doubao", "name": "豆包", "enabled": True, "model": ""}]}
    monkeypatch.setattr(env_settings, "zhiqi_api_key", "sk-live")
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "geo_engines", value)
    assert _errors(exc)[0]["loc"] == ["body", "value", "engines", 0] and _errors(exc)[0]["type"] == "model_required"
    monkeypatch.setattr(env_settings, "zhiqi_api_key", "")
    out = svc.set_value(db, "geo_engines", value)
    assert out["value"]["engines"][0]["enabled"] is True
    # 直接用模型校验时，上下文决定模式
    with pytest.raises(Exception):
        GeoEngines.model_validate(value, context={"mock_mode": False})
    GeoEngines.model_validate(value, context={"mock_mode": True})


def test_seo_provider_credentials_live_vs_mock(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    value = {"engines": {"baidu": {"provider": "baidu_ai_search", "enabled": True}}}
    monkeypatch.setattr(env_settings, "zhiqi_api_key", "sk-live")
    monkeypatch.setattr(env_settings, "seo_baidu_ai_search_api_key", "")
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "seo_providers", value)
    err = _errors(exc)[0]
    assert err["type"] == "credential_missing" and "SEO_BAIDU_AI_SEARCH_API_KEY" in err["msg"]
    # 未启用时允许选择该提供器
    svc.set_value(db, "seo_providers", {"engines": {"baidu": {"provider": "baidu_ai_search", "enabled": False}}})
    monkeypatch.setattr(env_settings, "seo_baidu_ai_search_api_key", "secret-value")
    out = svc.set_value(db, "seo_providers", value)
    assert out["value"]["engines"]["baidu"]["enabled"] is True
    assert out["value"]["providers"]["baidu_ai_search"]["configured"] is True
    assert "secret-value" not in json.dumps(out, ensure_ascii=False)
    # Mock 模式例外
    monkeypatch.setattr(env_settings, "seo_baidu_ai_search_api_key", "")
    SeoProviders.model_validate(svc.deep_merge(svc.DEFAULT_SETTINGS["seo_providers"], value), context={"mock_mode": True})


def test_alert_channels_require_env(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "alert_webhook_url", "")
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "alert_config", {"channels": {"webhook": {"enabled": True}}})
    assert _errors(exc)[0]["type"] == "credential_missing"
    monkeypatch.setattr(env_settings, "alert_webhook_url", "https://hooks.example/x")
    out = svc.set_value(db, "alert_config", {"channels": {"webhook": {"enabled": True}}})
    assert out["value"]["channels"]["webhook"]["configured"] is True

    monkeypatch.setattr(env_settings, "smtp_host", "")
    with pytest.raises(BusinessError) as exc2:
        svc.set_value(db, "alert_config", {"channels": {"email": {"enabled": True, "to": ["ops@example.com"]}}})
    assert _errors(exc2)[0]["type"] == "credential_missing"
    monkeypatch.setattr(env_settings, "smtp_host", "smtp.example.com")
    with pytest.raises(BusinessError):
        svc.set_value(db, "alert_config", {"channels": {"email": {"enabled": True, "to": []}}})
    svc.set_value(db, "alert_config", {"channels": {"email": {"enabled": True, "to": ["ops@example.com"]}}})


# ---------------------------------------------------------------- 查库引用校验


def test_template_code_references(db: Session) -> None:
    def put(code: str) -> list[dict]:
        with pytest.raises(BusinessError) as exc:
            svc.set_value(db, "generation_config", {"title": {"default_template_code": code}})
        return _errors(exc)

    loc = ["body", "value", "title", "default_template_code"]
    assert put("my_title") == [{"loc": loc, "msg": "该 code 无已发布版本", "type": "template_not_published", "input": "my_title"}]
    _add_template(db, "draft_title", "title", status="draft")
    assert put("draft_title")[0]["type"] == "template_not_published"
    _add_template(db, "proj_title", "title", project_id=5)
    assert put("proj_title")[0]["type"] == "template_not_global"
    _add_template(db, "kw_tpl", "keyword")
    assert put("kw_tpl")[0]["type"] == "kind_mismatch"
    _add_template(db, "my_title", "title")
    out = svc.set_value(db, "generation_config", {"title": {"default_template_code": "my_title"}})
    assert out["value"]["title"]["default_template_code"] == "my_title"

    # 未改动的 code 不查库：系统模板尚未 seed 时仍可修改其它字段
    svc.set_value(db, "generation_config", {"keyword": {"default_count": 30}})
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "media_config", {"image": {"image_prompt_template_code": "kw_tpl"}})
    assert _errors(exc)[0]["loc"] == ["body", "value", "image", "image_prompt_template_code"]


def test_geo_engine_model_and_title_prompt_checks(db: Session, live_mode: None) -> None:
    engine = {"code": "kimi", "name": "Kimi", "enabled": True, "model": "kimi-search"}
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "geo_engines", {"engines": [engine]})
    assert _errors(exc) == [{"loc": ["body", "value", "engines", 0, "model"], "msg": "模型不存在或模态不匹配", "type": "invalid_model", "input": "kimi-search"}]
    _add_model(db, "kimi-search", modalities=["text"], available=False)
    with pytest.raises(BusinessError) as exc2:
        svc.set_value(db, "geo_engines", {"engines": [engine]})
    assert _errors(exc2)[0]["type"] == "model_unavailable"
    _add_model(db, "kimi-ok", modalities=["text"])
    svc.set_value(db, "geo_engines", {"engines": [dict(engine, model="kimi-ok")]})

    _add_template(db, "geo_title", "geo_query", user_prompt="{{ title }} 是否被引用？")
    with pytest.raises(BusinessError) as exc3:
        svc.set_value(db, "geo_engines", {"engines": [dict(engine, model="kimi-ok", prompt_template_code="geo_title", parse={"match_mode": "title"})]})
    assert _errors(exc3)[0]["type"] == "title_in_prompt"
    _add_template(db, "geo_plain", "geo_query", user_prompt="关于 {{keyword}} 有哪些资料？")
    out = svc.set_value(db, "geo_engines", {"engines": [dict(engine, model="kimi-ok", prompt_template_code="geo_plain", parse={"match_mode": "title"})]})
    assert out["value"]["engines"][0]["parse"]["match_mode"] == "title"


def test_seo_engine_model_check(db: Session, mock_mode: None) -> None:
    with pytest.raises(BusinessError) as exc:
        svc.set_value(db, "seo_providers", {"engines": {"bing": {"model": "img-model"}}})
    assert _errors(exc)[0]["loc"] == ["body", "value", "engines", "bing", "model"]
    _add_model(db, "img-model", modalities=["image"])
    with pytest.raises(BusinessError):
        svc.set_value(db, "seo_providers", {"engines": {"bing": {"model": "img-model"}}})
    _add_model(db, "web-model", modalities=["text"])
    svc.set_value(db, "seo_providers", {"engines": {"bing": {"model": "web-model"}}})


# ---------------------------------------------------------------- 批量保存


def test_set_values_batch_is_all_or_nothing(db: Session) -> None:
    body = SettingsBatchBody.model_validate({"items": [
        {"key": "stats_config", "locale": "*", "value": {"timezone": "UTC"}},
        {"key": "system_info", "locale": "en-US", "value": {"site_name": ""}},
        {"key": "bogus", "value": {}},
    ]})
    with pytest.raises(BusinessError) as exc:
        svc.set_values(db, body.items)
    errors = _errors(exc)
    assert {tuple(e["loc"][:4]) for e in errors} == {("body", "items", 1, "value"), ("body", "items", 2, "key")}
    assert svc.get_value(db, "stats_config") is None

    with pytest.raises(BusinessError) as dup:
        svc.set_values(db, [{"key": "stats_config", "value": {}}, {"key": "stats_config", "locale": "*", "value": {}}])
    assert _errors(dup)[0]["type"] == "duplicate"

    out = svc.set_values(db, [
        {"key": "stats_config", "value": {"timezone": "UTC"}},
        {"key": "system_info", "locale": "en-US", "value": {"site_name": "My Platform"}},
    ])
    assert [(o["key"], o["locale"]) for o in out] == [("stats_config", "*"), ("system_info", "en-US")]
    assert svc.get_config(db, "system_info", "en-US")["site_name"] == "My Platform"
    assert svc.get_config(db, "system_info", "zh-CN")["site_name"] == "aicreat 内容生成平台"

    with pytest.raises(BusinessError) as bad_locale:
        svc.set_values(db, [{"key": "stats_config", "locale": "zh-CN", "value": {}}])
    assert _errors(bad_locale)[0]["loc"] == ["body", "items", 0, "locale"]


# ---------------------------------------------------------------- 缓存


def test_get_config_cache_and_invalidation(db: Session) -> None:
    svc.ensure_default_settings(db)
    assert svc.get_config(db, "stats_config")["timezone"] == "Asia/Shanghai"
    assert redis_client.ttl("cache:settings:stats_config:*") > 0
    assert redis_client.ttl("cache:settings:stats_config:*") <= 60

    # 绕过 service 直接改库：缓存期内仍返回旧值
    row = db.get(m.Setting, ("stats_config", "*"))
    row.value = json.dumps({"timezone": "UTC"})
    db.commit()
    assert svc.get_config(db, "stats_config")["timezone"] == "Asia/Shanghai"
    svc.invalidate_cache()
    assert svc.get_config(db, "stats_config")["timezone"] == "UTC"

    svc.runtime_settings(db)
    svc.get_config(db, "media_config")
    assert redis_client.exists("cache:settings:runtime", "cache:settings:media_config:*") == 2
    svc.set_value(db, "stats_config", {"timezone": "Europe/Berlin"})
    assert redis_client.exists("cache:settings:runtime", "cache:settings:media_config:*", "cache:settings:stats_config:*") == 0
    assert svc.runtime_settings(db)["stats_config"]["timezone"] == "Europe/Berlin"
    assert svc.get_config(db, "stats_config")["timezone"] == "Europe/Berlin"


def test_system_info_cache_key_per_locale(db: Session) -> None:
    svc.get_config(db, "system_info", "en-US")
    svc.get_config(db, "system_info", "zh-CN")
    assert redis_client.exists("cache:settings:system_info:en-US", "cache:settings:system_info:zh-CN") == 2


# ---------------------------------------------------------------- runtime 子集


def test_runtime_settings_subset(db: Session, mock_mode: None, monkeypatch: pytest.MonkeyPatch) -> None:
    svc.ensure_default_settings(db)
    rt = svc.runtime_settings(db)
    assert set(rt) == {"generation_config", "media_config", "monitoring_config", "geo_engines", "seo_providers", "stats_config", "zhiqi_mode"}
    assert rt["zhiqi_mode"] == "mock"
    assert rt["generation_config"] == {
        "review_required": True,
        "keyword": {"default_count": 20, "max_count": 50},
        "title": {"default_count": 5, "max_count": 10, "default_style": "news"},
        "content": {"outline_first": True, "segmented": True, "max_sections": 8, "target_word_count": 1500, "min_word_count": 300,
                    "max_word_count": 6000, "include_faq": True, "include_seo_meta": True, "default_format": "markdown"},
        "rewrite": {"modes": ["rewrite", "expand", "shorten", "restyle"], "max_versions": 50},
    }
    assert rt["media_config"]["daily_limits"] == {"images": 200, "videos": 20}
    assert rt["media_config"]["video"]["poll_budget_seconds"] == 1200
    assert "transfer" not in rt["media_config"]
    assert rt["monitoring_config"] == {"index_check": {
        "seo_engines": ["baidu", "bing", "google"],
        "geo_engines": ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"],
        "query_by": ["url", "title"],
    }}
    assert rt["geo_engines"]["engines"][1] == {"code": "doubao", "name": "豆包", "enabled": True}
    assert rt["seo_providers"] == {"engines": {
        "baidu": {"provider": "zhiqi_web_search", "enabled": True},
        "bing": {"provider": "zhiqi_web_search", "enabled": True},
        "google": {"provider": "zhiqi_web_search", "enabled": False},
    }}
    assert rt["stats_config"] == {"timezone": "Asia/Shanghai"}
    text = json.dumps(rt)
    for secret_field in ("credential_env", "url_env", "passthrough", "pricing", "prompt_template_code", "model"):
        assert f'"{secret_field}"' not in text
    assert json.loads(redis_client.get("cache:settings:runtime")) == rt
    assert 0 < redis_client.ttl("cache:settings:runtime") <= 60

    redis_client.delete("cache:settings:runtime")
    monkeypatch.setattr(env_settings, "zhiqi_api_key", "sk-live")
    assert svc.runtime_settings(db)["zhiqi_mode"] == "live"


# ---------------------------------------------------------------- configured 标记


def test_configured_flags_never_expose_env_values(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(env_settings, "seo_bing_webmaster_api_key", "bing-secret-key")
    monkeypatch.setattr(env_settings, "seo_bing_site_url", "")
    monkeypatch.setattr(env_settings, "seo_gsc_credentials_file", "/secret/gsc.json")
    monkeypatch.setattr(env_settings, "seo_gsc_site_url", "https://mine.example/")
    monkeypatch.setattr(env_settings, "alert_webhook_url", "")
    items = svc.list_settings(db)
    assert [(i["key"], i["locale"]) for i in items][-2:] == [("system_info", "zh-CN"), ("system_info", "en-US")]
    assert len(items) == 10
    by_key = {i["key"]: i["value"] for i in items if i["locale"] == "*"}
    providers = by_key["seo_providers"]["providers"]
    assert providers["baidu_ai_search"]["configured"] is False
    assert providers["bing_webmaster"]["configured"] is False  # site_url 为空
    assert providers["google_search_console"]["configured"] is True
    assert "configured" not in providers["zhiqi_web_search"]
    assert by_key["alert_config"]["channels"]["webhook"]["configured"] is False
    assert "configured" not in by_key["alert_config"]["channels"]["email"]
    dumped = json.dumps(items, ensure_ascii=False)
    for secret in ("bing-secret-key", "/secret/gsc.json", "https://mine.example/"):
        assert secret not in dumped
    # 不在白名单的变量一律 configured=false
    assert svc.env_configured("ADMIN_JWT_SECRET") is False
    assert svc.with_configured_flags({"credential_env": "ADMIN_JWT_SECRET"}) == {"credential_env": "ADMIN_JWT_SECRET", "configured": False}

    # GET 结果原样回传 PUT：configured 被剥离，不触发「未知字段」，也不入库
    single = svc.get_setting(db, "seo_providers")
    assert single["value"]["providers"]["google_search_console"]["configured"] is True
    out = svc.set_value(db, "seo_providers", single["value"])
    assert out["value"] == single["value"]
    assert "configured" not in json.dumps(svc.get_value(db, "seo_providers"))
    # 缓存中的配置不含 configured
    assert "configured" not in json.dumps(svc.get_config(db, "seo_providers"))


# ---------------------------------------------------------------- i18n


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "zh-CN"), ("", "zh-CN"), ("zh-CN", "zh-CN"), ("en-US", "en-US"), ("en_us", "en-US"), ("EN", "en-US"),
        ("en-GB", "en-US"), ("zh-TW", "zh-CN"), ("fr-FR", "zh-CN"), ("*", "zh-CN"),
        ("fr-FR,en-US;q=0.8,zh-CN;q=0.5", "en-US"), ("zh-CN;q=0.3,en;q=0.9", "en-US"), ("en;q=0", "zh-CN"),
    ],
)
def test_normalize_locale(value: str | None, expected: str) -> None:
    assert normalize_locale(value) == expected


def test_pick_locale_order() -> None:
    assert pick_locale("en-US", "zh-CN") == "en-US"
    assert pick_locale("xx", "en-US,zh;q=0.5") == "en-US"
    assert pick_locale(None, None) == "zh-CN"
