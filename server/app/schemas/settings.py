"""系统配置（``settings`` 表）的请求体与 9 个配置键的校验模型。

配置键 → 模型（docs/02-project-structure.md §2.9）：``generation_config`` → ``GenerationConfig``、
``media_config`` → ``MediaConfig``、``monitoring_config`` → ``MonitoringConfig``、``geo_engines`` → ``GeoEngines``、
``seo_providers`` → ``SeoProviders``、``alert_config`` → ``AlertConfig``、``ai_routing_config`` → ``AiRoutingConfig``、
``stats_config`` → ``StatsConfig``、``system_info`` → ``SystemInfo``。

各键结构与校验规则的权威文档：09 §4.4、10 §9、11 §13.1 / §8.1 / §7.6 / §10.3、08 §4.2、12 §4.8、04 §7.2。
全部模型 ``extra="forbid"``（未知字段 400）。需要查库的规则（模板 code、模型目录）不在这里，
由 ``settings_service`` 在模型校验通过后执行。

真实 / Mock 模式相关的规则从校验上下文 ``context={"mock_mode": bool}`` 读取，缺省取 ``settings.zhiqi_mock_mode``。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.core.config import settings as env_settings
from app.models import (
    AlertSeverity,
    ContentFormat,
    ContentStyle,
    ErrorCategory,
    ImageAspectRatio,
    ImageResolution,
    Locale,
    RewriteMode,
    SeoEngine,
    SeoProvider,
    VideoResolution,
    enum_values,
)

# =====================================================================
# 公共：环境变量引用与模式判定
# =====================================================================

# 配置中允许引用的环境变量（credential_env / site_url_env / url_env），只回显 configured，永不回显值
ENV_REFERENCE_NAMES: frozenset[str] = frozenset(
    {
        "SEO_BAIDU_AI_SEARCH_API_KEY",
        "SEO_BING_WEBMASTER_API_KEY",
        "SEO_BING_SITE_URL",
        "SEO_GSC_CREDENTIALS_FILE",
        "SEO_GSC_SITE_URL",
        "ALERT_WEBHOOK_URL",
        "ALERT_WEBHOOK_SECRET",
    }
)
ENV_REFERENCE_FIELDS: tuple[str, ...] = ("credential_env", "site_url_env", "url_env")


def env_is_configured(name: str | None) -> bool:
    """环境变量（白名单内）是否非空；不在白名单或为空 → ``False``。"""
    if not name or name not in ENV_REFERENCE_NAMES:
        return False
    value = getattr(env_settings, name.lower(), "")
    return bool(str(value or "").strip())


def _mock_mode(info: ValidationInfo | None) -> bool:
    ctx = info.context if info is not None else None
    if isinstance(ctx, dict) and "mock_mode" in ctx:
        return bool(ctx["mock_mode"])
    return env_settings.zhiqi_mock_mode


def _env_name(value: str) -> str:
    value = (value or "").strip()
    if value and value not in ENV_REFERENCE_NAMES:
        raise PydanticCustomError("invalid_env_name", "不支持引用的环境变量：{name}", {"name": value})
    return value


EnvName = Annotated[str, Field(max_length=64)]

TEMPLATE_CODE_PATTERN = r"^[a-z][a-z0-9_]{2,79}$"
TemplateCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=TEMPLATE_CODE_PATTERN, max_length=80)]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
NonNegInt = Annotated[int, Field(ge=0)]
PosInt = Annotated[int, Field(ge=1)]

RATE_LIMIT_PATTERN = re.compile(r"^\s*(\d+)\s*/\s*(minute|hour|day)\s*$")
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _unique(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    out: list[Any] = []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _check_poll_intervals(values: list[int]) -> list[int]:
    if not values:
        raise ValueError("轮询间隔不能为空")
    for v in values:
        if v < 1 or v > 300:
            raise ValueError("轮询间隔每项须在 1~300 秒之间")
    if any(b < a for a, b in zip(values, values[1:])):
        raise ValueError("轮询间隔须递增")
    return values


# =====================================================================
# generation_config（docs/09 §4.4）
# =====================================================================


class KeywordGenerationConfig(_Strict):
    default_count: int = Field(20, ge=1, le=100)
    max_count: int = Field(50, ge=1, le=100)
    dedupe_scope: Literal["project"] = "project"
    default_template_code: TemplateCode = "sys_keyword"
    intent_required: bool = True

    @model_validator(mode="after")
    def _counts(self) -> KeywordGenerationConfig:
        if self.default_count > self.max_count:
            raise ValueError("default_count 不能大于 max_count")
        return self


class TitleGenerationConfig(_Strict):
    default_count: int = Field(5, ge=1, le=20)
    max_count: int = Field(10, ge=1, le=20)
    default_style: ContentStyle = "news"
    default_template_code: TemplateCode = "sys_title"

    @model_validator(mode="after")
    def _counts(self) -> TitleGenerationConfig:
        if self.default_count > self.max_count:
            raise ValueError("default_count 不能大于 max_count")
        return self


class ContentTemplateCodes(_Strict):
    outline: TemplateCode = "sys_outline"
    content: TemplateCode = "sys_content"
    section: TemplateCode = "sys_section"
    seo_meta: TemplateCode = "sys_seo_meta"
    faq: TemplateCode = "sys_faq"


class ContentGenerationConfig(_Strict):
    outline_first: bool = True
    segmented: bool = True
    max_sections: int = Field(8, ge=1, le=20)
    target_word_count: int = Field(1500, ge=100, le=20000)
    min_word_count: int = Field(300, ge=100, le=20000)
    max_word_count: int = Field(6000, ge=100, le=20000)
    include_faq: bool = True
    faq_count: int = Field(3, ge=0, le=10)
    include_seo_meta: bool = True
    default_format: ContentFormat = "markdown"
    template_codes: ContentTemplateCodes = Field(default_factory=ContentTemplateCodes)

    @model_validator(mode="after")
    def _word_counts(self) -> ContentGenerationConfig:
        if not (self.min_word_count <= self.target_word_count <= self.max_word_count):
            raise ValueError("字数须满足 100 ≤ min_word_count ≤ target_word_count ≤ max_word_count ≤ 20000")
        return self


class RewriteTemplateCodes(_Strict):
    rewrite: TemplateCode = "sys_rewrite"
    expand: TemplateCode = "sys_expand"
    shorten: TemplateCode = "sys_shorten"
    restyle: TemplateCode = "sys_restyle"


class RewriteConfig(_Strict):
    modes: list[RewriteMode] = Field(default_factory=lambda: list(enum_values(RewriteMode)), min_length=1)
    max_versions: int = Field(50, ge=5, le=200)
    template_codes: RewriteTemplateCodes = Field(default_factory=RewriteTemplateCodes)

    @field_validator("modes")
    @classmethod
    def _dedupe_modes(cls, v: list[str]) -> list[str]:
        return _unique(v)


class QualityConfig(_Strict):
    min_word_count_ratio: float = Field(0.6, gt=0, le=1)
    require_h2: bool = True
    max_h2: int = Field(12, ge=1, le=50)
    banned_words: list[str] = Field(default_factory=list)
    flag_duplicate_title: bool = True

    @field_validator("banned_words")
    @classmethod
    def _banned_words(cls, v: list[str]) -> list[str]:
        words = _unique([w.strip() for w in v if w and w.strip()])
        if len(words) > 500:
            raise PydanticCustomError("too_long", "最多允许 {max_length} 项", {"max_length": 500})
        for w in words:
            if len(w) > 50:
                raise ValueError(f"敏感词长度不能超过 50 个字符：{w[:50]}")
        return words


def _rate_limit(value: str) -> str:
    m = RATE_LIMIT_PATTERN.match(value or "")
    if not m:
        raise ValueError("格式须为 N/minute、N/hour 或 N/day")
    n = int(m.group(1))
    if n < 1 or n > 100000:
        raise ValueError("次数须在 1~100000 之间")
    return f"{n}/{m.group(2)}"


class RateLimitsConfig(_Strict):
    generate_per_admin: str = "60/hour"
    media_per_admin: str = "20/hour"

    @field_validator("generate_per_admin", "media_per_admin")
    @classmethod
    def _check_rate(cls, v: str) -> str:
        return _rate_limit(v)


class QuotaConfig(_Strict):
    daily_limit: NonNegInt = 0
    project_monthly_limit: NonNegInt = 0
    warn_percent: int = Field(80, ge=1, le=100)


class GenerationConfig(_Strict):
    version: PosInt = 1
    default_language: Locale = "zh-CN"
    review_required: bool = True
    keyword: KeywordGenerationConfig = Field(default_factory=KeywordGenerationConfig)
    title: TitleGenerationConfig = Field(default_factory=TitleGenerationConfig)
    content: ContentGenerationConfig = Field(default_factory=ContentGenerationConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    quota: QuotaConfig = Field(default_factory=QuotaConfig)


# =====================================================================
# media_config（docs/10 §9）
# =====================================================================

ASPECT_RATIO_PATTERN = re.compile(r"^([1-9]\d{0,2}):([1-9]\d{0,2})$")


class ImageMediaConfig(_Strict):
    default_resolution: ImageResolution = "1080p"
    allowed_resolutions: list[ImageResolution] = Field(default_factory=lambda: list(enum_values(ImageResolution)), min_length=1)
    default_aspect_ratio: ImageAspectRatio = "16:9"
    allowed_aspect_ratios: list[ImageAspectRatio] = Field(
        default_factory=lambda: list(enum_values(ImageAspectRatio)), min_length=1
    )
    max_reference_images: int = Field(9, ge=0, le=9)
    max_count_per_request: int = Field(4, ge=1, le=4)
    poll_budget_seconds: int = Field(600, ge=60, le=1800)
    poll_intervals_seconds: list[int] = Field(default_factory=lambda: [5, 10, 15, 30])
    sync_fallback: bool = True
    edit_extra_fields: list[Literal["resolution", "aspect_ratio"]] = Field(default_factory=list)
    image_prompt_template_code: TemplateCode = "sys_image_prompt"

    @field_validator("allowed_resolutions", "allowed_aspect_ratios", "edit_extra_fields")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return _unique(v)

    @field_validator("poll_intervals_seconds")
    @classmethod
    def _intervals(cls, v: list[int]) -> list[int]:
        return _check_poll_intervals(v)

    @model_validator(mode="after")
    def _defaults_allowed(self) -> ImageMediaConfig:
        if self.default_resolution not in self.allowed_resolutions:
            raise ValueError("allowed_resolutions 必须包含 default_resolution")
        if self.default_aspect_ratio not in self.allowed_aspect_ratios:
            raise ValueError("allowed_aspect_ratios 必须包含 default_aspect_ratio")
        return self


class VideoMediaConfig(_Strict):
    default_resolution: VideoResolution = "720p"
    allowed_resolutions: list[VideoResolution] = Field(default_factory=lambda: list(enum_values(VideoResolution)), min_length=1)
    default_duration: int = Field(5, ge=1, le=60)
    max_duration: int = Field(15, ge=1, le=60)
    default_aspect_ratio: str = "16:9"
    generate_audio_default: bool = False
    poll_budget_seconds: int = Field(1200, ge=60, le=3600)
    poll_intervals_seconds: list[int] = Field(default_factory=lambda: [15, 30, 60])
    max_download_mb: int = Field(500, ge=1, le=10240)

    @field_validator("allowed_resolutions")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return _unique(v)

    @field_validator("default_aspect_ratio")
    @classmethod
    def _aspect(cls, v: str) -> str:
        v = (v or "").strip()
        if not ASPECT_RATIO_PATTERN.match(v):
            raise ValueError("default_aspect_ratio 须为 {w}:{h} 格式")
        return v

    @field_validator("poll_intervals_seconds")
    @classmethod
    def _intervals(cls, v: list[int]) -> list[int]:
        return _check_poll_intervals(v)

    @model_validator(mode="after")
    def _rules(self) -> VideoMediaConfig:
        if self.default_duration > self.max_duration:
            raise ValueError("须满足 1 ≤ default_duration ≤ max_duration ≤ 60")
        if self.default_resolution not in self.allowed_resolutions:
            raise ValueError("allowed_resolutions 必须包含 default_resolution")
        return self


class TransferConfig(_Strict):
    max_attempts: int = Field(3, ge=1, le=10)
    retry_seconds: list[Annotated[int, Field(ge=1, le=86400)]] = Field(default_factory=lambda: [30, 120, 600])
    max_download_mb: int = Field(50, ge=1, le=10240)

    @model_validator(mode="after")
    def _retry_len(self) -> TransferConfig:
        if len(self.retry_seconds) < self.max_attempts - 1:
            raise ValueError("retry_seconds 的长度须不少于 max_attempts - 1")
        return self


class RetentionConfig(_Strict):
    failed_days: int = Field(30, ge=1, le=3650)
    orphan_reference_days: int = Field(7, ge=1, le=3650)


class DailyLimitsConfig(_Strict):
    images: NonNegInt = 200
    videos: NonNegInt = 20


class MediaConfig(_Strict):
    version: PosInt = 1
    image: ImageMediaConfig = Field(default_factory=ImageMediaConfig)
    video: VideoMediaConfig = Field(default_factory=VideoMediaConfig)
    transfer: TransferConfig = Field(default_factory=TransferConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    daily_limits: DailyLimitsConfig = Field(default_factory=DailyLimitsConfig)


# =====================================================================
# monitoring_config（docs/11 §13.1）
# =====================================================================

ENGINE_CODE_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
EngineCode = Annotated[str, StringConstraints(strip_whitespace=True, pattern=ENGINE_CODE_PATTERN, max_length=32)]
MAX_RESPONSE_BYTES_LIMIT = 10 * 1024 * 1024


class LinkCheckConfig(_Strict):
    enabled: bool = True
    initial_days: int = Field(7, ge=0, le=365)
    initial_interval_hours: int = Field(24, ge=1, le=720)
    regular_interval_days: int = Field(7, ge=1, le=365)
    abnormal_backoff_hours: list[Annotated[int, Field(ge=1, le=720)]] = Field(default_factory=lambda: [6, 12, 24])
    unknown_confirm_count: int = Field(3, ge=1, le=10)
    suspected_confirm_count: int = Field(2, ge=1, le=10)
    deleted_recheck_days: int = Field(7, ge=1, le=365)
    deleted_recheck_until_days: int = Field(30, ge=1, le=3650)
    changed_simhash_distance: int = Field(20, ge=1, le=63)
    title_compare: bool = True
    timeout_seconds: int = Field(15, ge=1, le=120)
    max_response_bytes: int = Field(2097152, ge=1024, le=MAX_RESPONSE_BYTES_LIMIT)
    max_redirects: int = Field(3, ge=0, le=5)
    respect_robots: bool = False
    allow_http: bool = True
    user_agent: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] = (
        "aicreatLinkMonitor/1.0 (+https://example.com/contact)"
    )
    global_concurrency: int = Field(4, ge=1, le=16)
    per_domain_interval_seconds: int = Field(2, ge=0, le=3600)
    daily_limit: NonNegInt = 5000
    scan_interval_seconds: int = Field(60, ge=1, le=86400)

    @field_validator("abnormal_backoff_hours")
    @classmethod
    def _backoff(cls, v: list[int]) -> list[int]:
        if len(v) != 3:
            raise ValueError("abnormal_backoff_hours 长度必须为 3")
        if any(b <= a for a, b in zip(v, v[1:])):
            raise ValueError("abnormal_backoff_hours 须递增")
        return v

    @field_validator("user_agent")
    @classmethod
    def _ua(cls, v: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
            raise ValueError("user_agent 不能包含控制字符")
        return v

    @model_validator(mode="after")
    def _deleted(self) -> LinkCheckConfig:
        if self.deleted_recheck_until_days < self.deleted_recheck_days:
            raise ValueError("deleted_recheck_until_days 不能小于 deleted_recheck_days")
        return self


class IndexCheckConfig(_Strict):
    enabled: bool = True
    schedule_days: list[Annotated[int, Field(ge=1, le=3650)]] = Field(default_factory=lambda: [1, 3, 7, 14, 30], min_length=1)
    monthly_interval_days: int = Field(30, ge=1, le=3650)
    indexed_recheck_days: int = Field(90, ge=1, le=3650)
    seo_engines: list[SeoEngine] = Field(default_factory=lambda: list(enum_values(SeoEngine)))
    geo_engines: list[EngineCode] = Field(
        default_factory=lambda: ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"]
    )
    query_by: list[Literal["url", "title"]] = Field(default_factory=lambda: ["url", "title"], min_length=1)
    overdue_days: int = Field(30, ge=1, le=3650)
    max_checks_per_link_per_engine: int = Field(24, ge=1, le=1000)
    daily_limit: NonNegInt = 2000
    scan_interval_seconds: int = Field(300, ge=1, le=86400)
    concurrency: int = Field(2, ge=1, le=8)

    @field_validator("schedule_days")
    @classmethod
    def _schedule(cls, v: list[int]) -> list[int]:
        if any(b <= a for a, b in zip(v, v[1:])):
            raise ValueError("schedule_days 须为严格递增的正整数")
        return v

    @field_validator("seo_engines", "geo_engines", "query_by")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return _unique(v)

    @model_validator(mode="after")
    def _recheck(self) -> IndexCheckConfig:
        if self.indexed_recheck_days <= self.monthly_interval_days:
            raise ValueError("indexed_recheck_days 必须大于 monthly_interval_days")
        return self


class MonitoringConfig(_Strict):
    version: PosInt = 1
    link_check: LinkCheckConfig = Field(default_factory=LinkCheckConfig)
    index_check: IndexCheckConfig = Field(default_factory=IndexCheckConfig)


# =====================================================================
# geo_engines（docs/11 §8.1）
# =====================================================================

TextProtocol = Literal["openai_chat", "openai_responses", "anthropic_messages"]
CitationMatchMode = Literal["url", "domain", "url_or_domain", "title"]


class GeoParseConfig(_Strict):
    citation_source: Literal["annotations_or_markdown_links"] = "annotations_or_markdown_links"
    match_mode: CitationMatchMode = "url_or_domain"
    title_fuzzy_threshold: float = Field(0.8, ge=0, le=1)


class GeoEngineItem(_Strict):
    code: EngineCode
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    enabled: bool = False
    model: ModelId = ""
    protocol: TextProtocol = "openai_chat"
    prompt_template_code: TemplateCode = "sys_geo_query"
    extra: dict[str, Any] = Field(default_factory=dict)
    parse: GeoParseConfig = Field(default_factory=GeoParseConfig)
    timeout_seconds: int = Field(120, ge=1, le=600)

    @model_validator(mode="after")
    def _model_required(self, info: ValidationInfo) -> GeoEngineItem:
        if self.enabled and not self.model and not _mock_mode(info):
            raise PydanticCustomError("model_required", "真实模式下启用的引擎必须填写 model")
        return self


class GeoEngines(_Strict):
    version: PosInt = 1
    engines: list[GeoEngineItem] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _unique_codes(self) -> GeoEngines:
        codes = [e.code for e in self.engines]
        dup = sorted({c for c in codes if codes.count(c) > 1})
        if dup:
            raise ValueError(f"引擎 code 重复：{', '.join(dup)}")
        return self


# =====================================================================
# seo_providers（docs/11 §7.6）
# =====================================================================

# 非 zhiqi 提供器只允许用于对应引擎
PROVIDER_ENGINE_RESTRICTIONS: dict[str, str] = {
    "baidu_ai_search": "baidu",
    "bing_webmaster": "bing",
    "google_search_console": "google",
}


class SeoEngineConfig(_Strict):
    provider: SeoProvider = "zhiqi_web_search"
    enabled: bool = False
    model: ModelId = ""
    options: dict[str, Any] = Field(default_factory=dict)


class ZhiqiWebSearchProvider(_Strict):
    prompt_template_code: TemplateCode = "sys_seo_query"
    protocol: TextProtocol = "openai_chat"
    timeout_seconds: int = Field(120, ge=1, le=600)
    match_mode: Literal["url", "domain", "url_or_domain"] = "url_or_domain"
    extra: dict[str, Any] = Field(default_factory=dict)


class _EnvRefModel(_Strict):
    @field_validator("credential_env", "site_url_env", check_fields=False)
    @classmethod
    def _env(cls, v: str) -> str:
        return _env_name(v)


class BaiduAiSearchProvider(_EnvRefModel):
    endpoint: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = (
        "https://qianfan.baidubce.com/v2/ai_search/web_search"
    )
    credential_env: EnvName = "SEO_BAIDU_AI_SEARCH_API_KEY"
    timeout_seconds: int = Field(30, ge=1, le=300)
    top_k: int = Field(10, ge=1, le=50)

    @field_validator("endpoint")
    @classmethod
    def _https(cls, v: str) -> str:
        if not re.match(r"^https://[^\s/?#]+(/[^\s]*)?$", v):
            raise ValueError("endpoint 必须是 https URL")
        return v


class BingWebmasterProvider(_EnvRefModel):
    credential_env: EnvName = "SEO_BING_WEBMASTER_API_KEY"
    site_url_env: EnvName = "SEO_BING_SITE_URL"
    timeout_seconds: int = Field(30, ge=1, le=300)


class GoogleSearchConsoleProvider(_EnvRefModel):
    credential_env: EnvName = "SEO_GSC_CREDENTIALS_FILE"
    site_url_env: EnvName = "SEO_GSC_SITE_URL"
    timeout_seconds: int = Field(30, ge=1, le=300)


class ManualProvider(_Strict):
    pass


class SeoProvidersMap(_Strict):
    zhiqi_web_search: ZhiqiWebSearchProvider = Field(default_factory=ZhiqiWebSearchProvider)
    baidu_ai_search: BaiduAiSearchProvider = Field(default_factory=BaiduAiSearchProvider)
    bing_webmaster: BingWebmasterProvider = Field(default_factory=BingWebmasterProvider)
    google_search_console: GoogleSearchConsoleProvider = Field(default_factory=GoogleSearchConsoleProvider)
    manual: ManualProvider = Field(default_factory=ManualProvider)


class SeoProviders(_Strict):
    version: PosInt = 1
    engines: dict[SeoEngine, SeoEngineConfig] = Field(
        default_factory=lambda: {
            "baidu": SeoEngineConfig(enabled=True),
            "bing": SeoEngineConfig(enabled=True),
            "google": SeoEngineConfig(enabled=False),
        }
    )
    providers: SeoProvidersMap = Field(default_factory=SeoProvidersMap)

    @model_validator(mode="after")
    def _rules(self, info: ValidationInfo) -> SeoProviders:
        mock = _mock_mode(info)
        for engine, cfg in self.engines.items():
            only_for = PROVIDER_ENGINE_RESTRICTIONS.get(cfg.provider)
            if only_for and only_for != engine:
                raise PydanticCustomError(
                    "provider_engine_mismatch",
                    "提供器 {provider} 只能用于 {only_for}（engines.{engine}）",
                    {"provider": cfg.provider, "only_for": only_for, "engine": engine},
                )
            if cfg.enabled and not mock:
                provider_cfg = getattr(self.providers, cfg.provider)
                env_name = getattr(provider_cfg, "credential_env", None)
                if env_name is not None and not env_is_configured(env_name):
                    raise PydanticCustomError(
                        "credential_missing",
                        "engines.{engine} 启用的提供器 {provider} 未配置凭据（环境变量 {env} 为空）",
                        {"engine": engine, "provider": cfg.provider, "env": env_name or "-"},
                    )
        return self


# =====================================================================
# alert_config（docs/11 §10.3）
# =====================================================================


class AlertRule(_Strict):
    enabled: bool = True
    severity: AlertSeverity = "warning"


class IndexOverdueRule(AlertRule):
    days: PosInt = 30
    min_checks: PosInt = 3


class AiTaskFailuresRule(AlertRule):
    severity: AlertSeverity = "critical"
    consecutive: PosInt = 5
    window_minutes: PosInt = 30


class AiUpstreamUnavailableRule(AlertRule):
    severity: AlertSeverity = "critical"
    consecutive_probes: PosInt = 2


class WorkerStaleRule(AlertRule):
    minutes: PosInt = 5


class AlertRules(_Strict):
    link_deleted: AlertRule = Field(default_factory=lambda: AlertRule(severity="warning"))
    link_restored: AlertRule = Field(default_factory=lambda: AlertRule(severity="info"))
    link_changed: AlertRule = Field(default_factory=lambda: AlertRule(severity="info"))
    index_overdue: IndexOverdueRule = Field(default_factory=IndexOverdueRule)
    ai_task_failures: AiTaskFailuresRule = Field(default_factory=AiTaskFailuresRule)
    ai_breaker_open: AlertRule = Field(default_factory=lambda: AlertRule(severity="warning"))
    ai_quota_exceeded: AlertRule = Field(default_factory=lambda: AlertRule(severity="critical"))
    ai_auth_failed: AlertRule = Field(default_factory=lambda: AlertRule(severity="critical"))
    ai_upstream_unavailable: AiUpstreamUnavailableRule = Field(default_factory=AiUpstreamUnavailableRule)
    media_task_failed: AlertRule = Field(default_factory=lambda: AlertRule(severity="info"))
    worker_stale: WorkerStaleRule = Field(default_factory=WorkerStaleRule)


class InAppChannel(_Strict):
    enabled: Literal[True] = True


class WebhookChannel(_Strict):
    enabled: bool = False
    url_env: EnvName = "ALERT_WEBHOOK_URL"
    min_severity: AlertSeverity = "warning"

    @field_validator("url_env")
    @classmethod
    def _url_env(cls, v: str) -> str:
        return _env_name(v)

    @model_validator(mode="after")
    def _configured(self) -> WebhookChannel:
        if self.enabled and not env_is_configured(self.url_env):
            raise PydanticCustomError(
                "credential_missing", "启用 webhook 通道需配置环境变量 {env}", {"env": self.url_env or "ALERT_WEBHOOK_URL"}
            )
        return self


class EmailChannel(_Strict):
    enabled: bool = False
    to: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]] = Field(
        default_factory=list, max_length=50
    )
    min_severity: AlertSeverity = "critical"

    @field_validator("to")
    @classmethod
    def _emails(cls, v: list[str]) -> list[str]:
        v = _unique([e for e in v if e])
        for e in v:
            if not EMAIL_PATTERN.match(e):
                raise ValueError(f"邮箱格式不正确：{e}")
        return v

    @model_validator(mode="after")
    def _smtp(self) -> EmailChannel:
        if self.enabled:
            if not str(env_settings.smtp_host or "").strip():
                raise PydanticCustomError("credential_missing", "启用邮件通道需配置环境变量 SMTP_HOST")
            if not self.to:
                raise ValueError("启用邮件通道时 to 不能为空")
        return self


class AlertChannels(_Strict):
    in_app: InAppChannel = Field(default_factory=InAppChannel)
    webhook: WebhookChannel = Field(default_factory=WebhookChannel)
    email: EmailChannel = Field(default_factory=EmailChannel)


class AlertConfig(_Strict):
    version: PosInt = 1
    enabled: bool = True
    rules: AlertRules = Field(default_factory=AlertRules)
    dedupe_cooldown_minutes: int = Field(60, ge=0, le=10080)
    channels: AlertChannels = Field(default_factory=AlertChannels)


# =====================================================================
# ai_routing_config（docs/08 §4.2）
# =====================================================================

ErrorCategoryList = list[ErrorCategory]
PASSTHROUGH_FIELD_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
# 请求体核心字段由网关组装，不允许经 extra 透传覆盖
PASSTHROUGH_RESERVED_FIELDS: frozenset[str] = frozenset(
    {"model", "messages", "input", "prompt", "system", "stream", "max_tokens", "max_output_tokens", "response_format", "n"}
)


class RetryConfig(_Strict):
    max_attempts: int = Field(3, ge=0, le=10)
    base_seconds: float = Field(1.0, gt=0, le=60)
    max_seconds: float = Field(30, gt=0, le=600)
    jitter: bool = True
    retry_on: ErrorCategoryList = Field(
        default_factory=lambda: ["upstream_unavailable", "rate_limited", "timeout", "invalid_response"]
    )

    @field_validator("retry_on")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return _unique(v)

    @model_validator(mode="after")
    def _bounds(self) -> RetryConfig:
        if self.max_seconds < self.base_seconds:
            raise ValueError("max_seconds 不能小于 base_seconds")
        return self


class BreakerConfig(_Strict):
    failure_threshold: int = Field(5, ge=1, le=1000)
    window_seconds: int = Field(300, ge=10, le=86400)
    open_seconds: int = Field(120, ge=1, le=86400)
    half_open_max_calls: int = Field(1, ge=1, le=100)


class FallbackConfig(_Strict):
    enabled: bool = True
    fallback_on: ErrorCategoryList = Field(
        default_factory=lambda: [
            "upstream_unavailable", "rate_limited", "timeout", "route_missing", "model_unrouted", "breaker_open",
            "media_storage", "invalid_response", "unsupported_parameter", "unknown",
        ]
    )
    never_fallback_on: ErrorCategoryList = Field(
        default_factory=lambda: ["quota_exceeded", "auth_failed", "content_blocked", "transfer_failed", "cancelled"]
    )

    @field_validator("fallback_on", "never_fallback_on")
    @classmethod
    def _dedupe(cls, v: list[str]) -> list[str]:
        return _unique(v)


def _passthrough_fields(v: list[str]) -> list[str]:
    v = _unique([f.strip() for f in v if f and f.strip()])
    for f in v:
        if not PASSTHROUGH_FIELD_PATTERN.match(f):
            raise ValueError(f"透传字段名不合法：{f}")
        if f in PASSTHROUGH_RESERVED_FIELDS:
            raise ValueError(f"透传字段不能包含请求核心字段：{f}")
    return v


class PassthroughConfig(_Strict):
    openai_chat: list[str] = Field(default_factory=lambda: ["tools", "tool_choice", "web_search_options", "reasoning_effort"])
    openai_responses: list[str] = Field(default_factory=lambda: ["tools", "tool_choice", "reasoning", "text"])
    anthropic_messages: list[str] = Field(default_factory=lambda: ["tools", "thinking"])

    @field_validator("openai_chat", "openai_responses", "anthropic_messages")
    @classmethod
    def _check_fields(cls, v: list[str]) -> list[str]:
        return _passthrough_fields(v)


class TimeoutsConfig(_Strict):
    text_seconds: int = Field(180, ge=1, le=3600)
    submit_seconds: int = Field(60, ge=1, le=600)
    poll_seconds: int = Field(30, ge=1, le=300)


class PricingConfig(_Strict):
    quota_per_unit: float = Field(500000, gt=0)
    usd_cny_rate: float = Field(7.2, gt=0, le=1000)
    group_ratio: float = Field(1.0, gt=0, le=1000)


HEALTH_PROBE_TIMEOUT_MS = 30000


class HealthConfig(_Strict):
    probe_interval_seconds: int = Field(600, ge=0, le=86400)
    probe_text_prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] = "ping"
    probe_max_tokens: int = Field(8, ge=1, le=256)
    probe_media: bool = False
    degraded_latency_ms: int = Field(15000, ge=1, lt=HEALTH_PROBE_TIMEOUT_MS)


class CatalogConfig(_Strict):
    sync_interval_seconds: int = Field(3600, ge=0, le=604800)
    hide_unavailable_after_days: int = Field(7, ge=1, le=3650)


class UsageConfig(_Strict):
    reconcile_interval_seconds: int = Field(300, ge=0, le=86400)
    min_interval_seconds: int = Field(60, ge=1, le=86400)
    max_new_per_pull_warn: int = Field(800, ge=1, le=1000)
    unmatched_retention_days: int = Field(30, ge=1, le=3650)


class AiRoutingConfig(_Strict):
    version: PosInt = 1
    retry: RetryConfig = Field(default_factory=RetryConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    pause_seconds: int = Field(600, ge=1, le=86400)
    passthrough: PassthroughConfig = Field(default_factory=PassthroughConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)


# =====================================================================
# stats_config（docs/12 §4.8）
# =====================================================================

DAILY_AT_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class StatsConfig(_Strict):
    version: PosInt = 1
    timezone: str = "Asia/Shanghai"
    daily_at: str = "00:30"
    intraday_refresh_seconds: int = Field(600, ge=0, le=86400)
    retention_days: int = Field(730, ge=30, le=3650)
    rankings_limit: int = Field(10, ge=1, le=100)
    overview_cache_seconds: int = Field(60, ge=0, le=3600)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        v = (v or "").strip()
        try:
            if not v or v.startswith("/") or ".." in v:
                raise ZoneInfoNotFoundError(v)
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise PydanticCustomError("invalid_timezone", "不是合法的 IANA 时区名") from None
        return v

    @field_validator("daily_at")
    @classmethod
    def _daily_at(cls, v: str) -> str:
        v = (v or "").strip()
        if not DAILY_AT_PATTERN.match(v):
            raise ValueError("daily_at 须为 HH:MM 格式")
        return v

    @field_validator("intraday_refresh_seconds")
    @classmethod
    def _intraday(cls, v: int) -> int:
        if v != 0 and v < 60:
            raise ValueError("intraday_refresh_seconds 须为 0 或 60~86400")
        return v


# =====================================================================
# system_info（docs/04 §7.2）
# =====================================================================


class SystemInfo(_Strict):
    site_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] = "aicreat 内容生成平台"
    logo_url: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    footer: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""
    support_contact: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""

    @field_validator("logo_url")
    @classmethod
    def _logo(cls, v: str) -> str:
        if v and not (re.match(r"^https?://[^\s]+$", v) or (v.startswith("/") and not v.startswith("//") and " " not in v)):
            raise ValueError("logo_url 须为 http(s) URL 或以 / 开头的站内路径")
        return v


# =====================================================================
# 键 → 模型、请求体
# =====================================================================

SETTINGS_MODELS: dict[str, type[BaseModel]] = {
    "generation_config": GenerationConfig,
    "media_config": MediaConfig,
    "monitoring_config": MonitoringConfig,
    "geo_engines": GeoEngines,
    "seo_providers": SeoProviders,
    "alert_config": AlertConfig,
    "ai_routing_config": AiRoutingConfig,
    "stats_config": StatsConfig,
    "system_info": SystemInfo,
}

SettingKey = Literal[
    "generation_config", "media_config", "monitoring_config", "geo_engines", "seo_providers",
    "alert_config", "ai_routing_config", "stats_config", "system_info",
]
SettingLocaleParam = Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]


class SettingItem(_Strict):
    """批量保存的一项 ``{key, locale, value}``（``value`` 由 ``settings_service`` 按 key 选模型校验）。"""

    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    locale: SettingLocaleParam = "*"
    value: dict[str, Any]


SettingsItem = SettingItem


class SettingsBatchBody(_Strict):
    """``PUT /admin/settings``：``{items:[{key,locale,value}]}``，任一失败整体 400。"""

    items: list[SettingItem] = Field(min_length=1, max_length=20)


class SettingUpdateBody(_Strict):
    """``PUT /admin/settings/{key}``：``{locale?, value}``（``locale`` 默认 ``*``）。"""

    locale: SettingLocaleParam = "*"
    value: dict[str, Any]


class SettingOut(BaseModel):
    key: str
    locale: str
    value: dict[str, Any]
