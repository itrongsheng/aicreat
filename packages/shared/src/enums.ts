// 全部状态枚举（as const）。取值集合以 docs/00-overview.md §8 为蓝本，
// 后端 server/app/models.py 顶部以 Literal 声明同名集合，两边取值必须逐字一致（新增枚举值时两处同时修改）。

// ---------- 8.1 内容生产 ----------

export const PROJECT_STATUS = ["active", "archived"] as const;
export type ProjectStatus = (typeof PROJECT_STATUS)[number];

export const CONTENT_STYLE = ["news", "tutorial", "review", "qa", "recommend", "listicle", "story"] as const;
export type ContentStyle = (typeof CONTENT_STYLE)[number];

/** 内容格式：`projects.default_format`、`contents.format` */
export const CONTENT_FORMAT = ["markdown", "html"] as const;
export type ContentFormat = (typeof CONTENT_FORMAT)[number];

export const PROMPT_KIND = [
  "keyword",
  "title",
  "outline",
  "content",
  "section",
  "rewrite",
  "expand",
  "shorten",
  "restyle",
  "seo_meta",
  "faq",
  "image_prompt",
  "geo_query",
  "seo_query",
] as const;
export type PromptKind = (typeof PROMPT_KIND)[number];

export const PROMPT_STATUS = ["draft", "published", "archived"] as const;
export type PromptStatus = (typeof PROMPT_STATUS)[number];

/** 模板输出格式：`prompt_templates.output_format` */
export const PROMPT_OUTPUT_FORMAT = ["json", "markdown", "text"] as const;
export type PromptOutputFormat = (typeof PROMPT_OUTPUT_FORMAT)[number];

/** 批次类型：`generation_batches.kind` */
export const BATCH_KIND = ["keyword", "title", "content"] as const;
export type BatchKind = (typeof BATCH_KIND)[number];

export const BATCH_STATUS = ["queued", "running", "succeeded", "partial", "failed", "cancelled"] as const;
export type BatchStatus = (typeof BATCH_STATUS)[number];

export const KEYWORD_STATUS = ["candidate", "adopted", "discarded"] as const;
export type KeywordStatus = (typeof KEYWORD_STATUS)[number];

export const KEYWORD_INTENT = ["informational", "navigational", "transactional", "commercial", "unknown"] as const;
export type KeywordIntent = (typeof KEYWORD_INTENT)[number];

export const KEYWORD_TYPE = ["core", "long_tail", "question", "brand", "competitor"] as const;
export type KeywordType = (typeof KEYWORD_TYPE)[number];

export const KEYWORD_SOURCE = ["generated", "imported", "manual"] as const;
export type KeywordSource = (typeof KEYWORD_SOURCE)[number];

/** 标题来源（无 `imported`） */
export const TITLE_SOURCE = ["generated", "manual"] as const;
export type TitleSource = (typeof TITLE_SOURCE)[number];

export const TITLE_STATUS = ["candidate", "adopted", "discarded"] as const;
export type TitleStatus = (typeof TITLE_STATUS)[number];

export const CONTENT_STATUS = ["draft", "generating", "ready", "reviewing", "approved", "rejected", "published", "archived"] as const;
export type ContentStatus = (typeof CONTENT_STATUS)[number];

/** 审核结果：`contents.review_result`（可空） */
export const REVIEW_RESULT = ["approved", "rejected"] as const;
export type ReviewResult = (typeof REVIEW_RESULT)[number];

export const VERSION_SOURCE = ["generate", "rewrite", "expand", "shorten", "restyle", "manual", "restore"] as const;
export type VersionSource = (typeof VERSION_SOURCE)[number];

export const REWRITE_MODE = ["rewrite", "expand", "shorten", "restyle"] as const;
export type RewriteMode = (typeof REWRITE_MODE)[number];

export const REWRITE_SCOPE = ["full", "section"] as const;
export type RewriteScope = (typeof REWRITE_SCOPE)[number];

// ---------- 8.2 媒体 ----------

export const MEDIA_KIND = ["image", "video"] as const;
export type MediaKind = (typeof MEDIA_KIND)[number];

export const MEDIA_USAGE_TYPE = ["cover", "inline", "standalone", "reference"] as const;
export type MediaUsageType = (typeof MEDIA_USAGE_TYPE)[number];

export const MEDIA_SOURCE = ["generated", "uploaded"] as const;
export type MediaSource = (typeof MEDIA_SOURCE)[number];

export const MEDIA_STATUS = ["pending", "submitted", "generating", "downloading", "ready", "failed", "expired", "deleted"] as const;
export type MediaStatus = (typeof MEDIA_STATUS)[number];

/** 上游任务状态：zhiqiapi 图片 / 视频轮询响应 `status` */
export const UPSTREAM_TASK_STATUS = ["queued", "in_progress", "succeeded", "failed", "expired"] as const;
export type UpstreamTaskStatus = (typeof UPSTREAM_TASK_STATUS)[number];

// ---------- 8.3 AI 网关 ----------

export const MODALITY = ["text", "image", "video"] as const;
export type Modality = (typeof MODALITY)[number];

export const PROTOCOL = [
  "openai_chat",
  "openai_responses",
  "anthropic_messages",
  "image_async",
  "image_sync",
  "image_edit",
  "video",
] as const;
export type Protocol = (typeof PROTOCOL)[number];

export const AI_TASK_STATUS = ["queued", "running", "polling", "succeeded", "failed", "cancelled", "expired"] as const;
export type AiTaskStatus = (typeof AI_TASK_STATUS)[number];

export const AI_TASK_OPERATION = [
  "keyword_generate",
  "title_generate",
  "content_generate",
  "content_outline",
  "content_body",
  "content_seo",
  "content_rewrite",
  "image_prompt",
  "image_generate",
  "video_generate",
  "seo_check",
  "geo_check",
  "route_probe",
] as const;
export type AiTaskOperation = (typeof AI_TASK_OPERATION)[number];

export const AI_TASK_TRIGGER_TYPE = ["user", "worker", "health_probe", "system"] as const;
export type AiTaskTriggerType = (typeof AI_TASK_TRIGGER_TYPE)[number];

export const AI_TASK_TARGET_TYPE = ["generation_batch", "keyword", "content", "media_asset", "publish_link", "route_probe"] as const;
export type AiTaskTargetType = (typeof AI_TASK_TARGET_TYPE)[number];

export const ERROR_CATEGORY = [
  "unsupported_parameter",
  "route_missing",
  "model_unrouted",
  "upstream_unavailable",
  "rate_limited",
  "timeout",
  "quota_exceeded",
  "auth_failed",
  "content_blocked",
  "media_storage",
  "transfer_failed",
  "invalid_response",
  "breaker_open",
  "cancelled",
  "unknown",
] as const;
export type ErrorCategory = (typeof ERROR_CATEGORY)[number];

export const HEALTH_STATUS = ["healthy", "degraded", "down", "unknown"] as const;
export type HealthStatus = (typeof HEALTH_STATUS)[number];

export const BREAKER_STATE = ["closed", "open", "half_open"] as const;
export type BreakerState = (typeof BREAKER_STATE)[number];

/** 熔断原因：Redis `ai:breaker:{capability}:{model}` 的 `reason` */
export const BREAKER_REASON = ["failures", "model_unavailable", "probe_down", "manual"] as const;
export type BreakerReason = (typeof BREAKER_REASON)[number];

/** 全局暂停原因：Redis `ai:paused:{reason}`、业务码 5031 的 `paused_reason` */
export const PAUSED_REASON = ["quota_exceeded", "auth_failed"] as const;
export type PausedReason = (typeof PAUSED_REASON)[number];

/** `GET /admin/auth/me`、`GET /api/v1/health` 的 `zhiqi_mode` */
export const ZHIQI_MODE = ["mock", "live"] as const;
export type ZhiqiMode = (typeof ZHIQI_MODE)[number];

/** 用量日志类型 `ai_usage_logs.log_type`：2 消费 / 5 失败 / 6 异步任务退款 */
export const USAGE_LOG_TYPE = [2, 5, 6] as const;
export type UsageLogType = (typeof USAGE_LOG_TYPE)[number];

/** `ai_models.quota_type`：0 按量 / 1 按次 */
export const QUOTA_TYPE = [0, 1] as const;
export type QuotaType = (typeof QUOTA_TYPE)[number];

// ---------- 8.4 发布与监控 ----------

export const LINK_ALIVE_STATUS = ["pending", "alive", "changed", "suspected_deleted", "deleted", "unknown"] as const;
export type LinkAliveStatus = (typeof LINK_ALIVE_STATUS)[number];

/** 删除检测结果 `link_checks.result_status`（`applied_status` / `previous_status` 为 `LINK_ALIVE_STATUS` 全集） */
export const LINK_CHECK_RESULT = ["alive", "changed", "suspected_deleted", "deleted", "unknown"] as const;
export type LinkCheckResult = (typeof LINK_CHECK_RESULT)[number];

/** `link_checks.check_type` 全集；`index_checks.check_type` 只用其中 `scheduled` / `manual` */
export const CHECK_TYPE = ["baseline", "scheduled", "manual", "retry"] as const;
export type CheckType = (typeof CHECK_TYPE)[number];
export type IndexCheckType = Extract<CheckType, "scheduled" | "manual">;

/** 删除特征文案规则的前缀：`marker:<文案>` */
export const LINK_CHECK_MARKER_PREFIX = "marker:";

/** `link_checks.matched_rule` 的固定取值；另有动态取值 `marker:<文案>`（见 `LinkCheckRule`） */
export const LINK_CHECK_RULE = [
  "http_404",
  "http_410",
  "http_451",
  "redirect_home",
  "redirect_login",
  "title_changed",
  "body_changed",
  "network_error",
  "blocked_by_robots",
  "ssrf_blocked",
  "ok",
] as const;
export type LinkCheckRule = (typeof LINK_CHECK_RULE)[number] | `marker:${string}`;

export const INDEX_KIND = ["seo", "geo"] as const;
export type IndexKind = (typeof INDEX_KIND)[number];

export const SEO_ENGINE = ["baidu", "bing", "google"] as const;
export type SeoEngine = (typeof SEO_ENGINE)[number];

export const GEO_ENGINE = ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"] as const;
export type GeoEngine = (typeof GEO_ENGINE)[number];

export const SEO_PROVIDER = ["zhiqi_web_search", "baidu_ai_search", "bing_webmaster", "google_search_console", "manual"] as const;
export type SeoProvider = (typeof SEO_PROVIDER)[number];

export const GEO_PROVIDER = ["zhiqi_model", "manual"] as const;
export type GeoProvider = (typeof GEO_PROVIDER)[number];

export const SEO_INDEX_STATUS = ["indexed", "not_indexed", "unknown"] as const;
export type SeoIndexStatus = (typeof SEO_INDEX_STATUS)[number];

export const GEO_CITE_STATUS = ["cited", "not_cited", "unknown"] as const;
export type GeoCiteStatus = (typeof GEO_CITE_STATUS)[number];

export const INDEX_MATCH_MODE = ["url", "domain", "title", "none", "manual"] as const;
export type IndexMatchMode = (typeof INDEX_MATCH_MODE)[number];

export const ALERT_TYPE = [
  "link_deleted",
  "link_restored",
  "link_changed",
  "index_overdue",
  "ai_task_failures",
  "ai_breaker_open",
  "ai_quota_exceeded",
  "ai_auth_failed",
  "ai_upstream_unavailable",
  "media_task_failed",
  "worker_stale",
] as const;
export type AlertType = (typeof ALERT_TYPE)[number];

export const ALERT_SEVERITY = ["info", "warning", "critical"] as const;
export type AlertSeverity = (typeof ALERT_SEVERITY)[number];

export const ALERT_STATUS = ["open", "acknowledged", "resolved", "ignored"] as const;
export type AlertStatus = (typeof ALERT_STATUS)[number];

export const ALERT_TARGET_TYPE = [
  "publish_link",
  "content",
  "ai_task",
  "ai_model",
  "capability_route",
  "media_asset",
  "worker",
  "system",
] as const;
export type AlertTargetType = (typeof ALERT_TARGET_TYPE)[number];

/** 告警通道：`alerts.notified_channels_json`、`alert_config.channels` */
export const ALERT_CHANNEL = ["in_app", "webhook", "email"] as const;
export type AlertChannel = (typeof ALERT_CHANNEL)[number];

// ---------- 8.5 报表、系统与安全 ----------

/** `daily_stats.dimension`；`GET /admin/stats/breakdown` 另支持 `project` / `owner`（不落库，见 `BREAKDOWN_DIMENSION`） */
export const STATS_DIMENSION = ["total", "platform", "capability", "model", "admin", "seo_engine", "geo_engine"] as const;
export type StatsDimension = (typeof STATS_DIMENSION)[number];

/** `GET /admin/stats/breakdown?dimension=` 的取值（不含 `total`） */
export const BREAKDOWN_DIMENSION = ["project", "owner", "platform", "admin", "model", "capability", "seo_engine", "geo_engine"] as const;
export type BreakdownDimension = (typeof BREAKDOWN_DIMENSION)[number];

export const STATS_GRANULARITY = ["day", "week", "month"] as const;
export type StatsGranularity = (typeof STATS_GRANULARITY)[number];

/** 总览区间 `GET /admin/stats/overview?range=` */
export const STATS_RANGE = ["today", "7d", "30d"] as const;
export type StatsRange = (typeof STATS_RANGE)[number];

/** 榜单类型 `GET /admin/stats/rankings?type=` */
export const RANKING_TYPE = [
  "fastest_indexed",
  "most_deleted_platforms",
  "top_cost_models",
  "top_cost_projects",
  "top_failed_models",
] as const;
export type RankingType = (typeof RANKING_TYPE)[number];

/** 系统用户组代码（自定义组为 `custom_*`） */
export const ADMIN_GROUP_CODE = ["super_admin", "operator", "reviewer", "read_only"] as const;
export type AdminGroupCode = (typeof ADMIN_GROUP_CODE)[number];

export const DATA_SCOPE = ["all", "own"] as const;
export type DataScope = (typeof DATA_SCOPE)[number];

/** 权限类型 `admin_permissions.type` */
export const PERMISSION_TYPE = ["menu", "action"] as const;
export type PermissionType = (typeof PERMISSION_TYPE)[number];

export const OPERATION_ACTION = ["create", "update", "update_status", "delete", "execute", "login", "logout", "reset_password"] as const;
export type OperationAction = (typeof OPERATION_ACTION)[number];
