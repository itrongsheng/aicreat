// 契约接口：以 API 字段名声明（JSON 列去掉 _json 后缀，值为解码后的对象 / 数组），
// 字段与 docs/04-api-spec.md 的响应一致；时间字段均为 ISO 8601 UTC 字符串，日期字段为 YYYY-MM-DD。
import type { AspectRatio, Capability, ImageResolution, Locale, SettingLocale, VideoResolution } from "./constants";
import type {
  AdminGroupCode,
  AiTaskOperation,
  AiTaskStatus,
  AiTaskTargetType,
  AiTaskTriggerType,
  AlertChannel,
  AlertSeverity,
  AlertStatus,
  AlertTargetType,
  AlertType,
  BatchKind,
  BatchStatus,
  BreakerReason,
  BreakerState,
  CheckType,
  ContentFormat,
  ContentStatus,
  ContentStyle,
  DataScope,
  DownloadSource,
  ErrorCategory,
  GeoCiteStatus,
  GeoEngine,
  GeoProvider,
  HealthStatus,
  IndexCheckType,
  IndexKind,
  IndexMatchMode,
  KeywordIntent,
  KeywordSource,
  KeywordStatus,
  KeywordType,
  LinkAliveStatus,
  LinkCheckResult,
  LinkCheckRule,
  MediaKind,
  MediaSource,
  MediaStatus,
  MediaUsageType,
  Modality,
  OperationAction,
  PausedReason,
  PermissionType,
  ProjectStatus,
  PromptKind,
  PromptOutputFormat,
  PromptStatus,
  Protocol,
  QuotaType,
  RankingType,
  ReviewResult,
  SeoEngine,
  SeoIndexStatus,
  SeoProvider,
  StatsDimension,
  StatsRange,
  TitleSource,
  TitleStatus,
  UsageLogType,
  VersionSource,
  ZhiqiMode,
} from "./enums";

// =====================================================================
// 通用
// =====================================================================

/** 统一响应外壳 `{code, message, data}`（04 §2）；`code=0` 为成功 */
export interface ApiResponse<T = unknown> {
  code: number;
  message: string;
  data: T;
}

/** 分页数据（04 §3） */
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** 400 校验错误项（04 §5.1）；密码类字段的错误项不带 `input` */
export interface ValidationErrorItem {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
}

/** 409 冲突的 `data`（04 §5.3），字段按场景出现 */
export interface ConflictData {
  current_status?: string;
  existing_id?: number | null;
  reason?: "owned_by_other" | "quality_blocked" | "last_published" | "in_use" | "monitoring_paused" | string;
  flags?: string[];
  current_version_id?: number;
  hint?: string;
}

/** 4291 本地额度 / 数量上限的 `data` */
export interface QuotaLimitData {
  scope: "daily" | "project_monthly" | "daily_images" | "daily_videos";
  limit: number;
  used: number;
}

/** 5021 上游失败的 `data` */
export interface UpstreamErrorData {
  error_category: ErrorCategory;
  request_id: string | null;
  model: string | null;
  hint: "prompt_blocked" | "model_override" | null;
}

/** 5031 能力不可用 / 全局暂停的 `data` */
export interface CapabilityUnavailableData {
  capability: Capability;
  breaker_open: string[];
  unavailable_models: string[];
  paused_reason: PausedReason | null;
  hint?: "model_override";
}

/** 生成接口的额度预警（04 §5.2）：一次请求最多一个 */
export interface QuotaWarning {
  scope: "daily" | "project_monthly";
  limit: number;
  used: number;
  percent: number;
}

/** 批量状态动作的跳过项（04 §9） */
export interface SkippedItem {
  id: number;
  reason: "not_found" | "invalid_transition" | string;
}

/** `POST …/batch-status`、`POST /admin/alerts/batch-resolve` 的返回 */
export interface BatchUpdateResult {
  updated: number;
  skipped: SkippedItem[];
}

/** 入队类动作的返回 `{queued}` / `{queued:false, reason}` */
export interface QueueResult {
  queued: boolean;
  reason?: "already_queued" | "daily_limit";
}

/** 批量入队 `{enqueued, skipped}` */
export interface EnqueueResult {
  enqueued: number;
  skipped: number;
}

/** 用户摘要（日志、负责人等嵌套对象） */
export interface AdminBrief {
  id: number;
  username: string;
  display_name: string | null;
}

// =====================================================================
// 管理员、用户组、权限与操作日志（07 §9.7）
// =====================================================================

export interface AdminGroupRef {
  id: number;
  code: AdminGroupCode | string;
  name: string;
}

export interface AdminProfile {
  id: number;
  username: string;
  display_name: string | null;
  group: AdminGroupRef | null;
  permissions: string[];
  data_scope: DataScope;
  last_login_at?: string | null;
  zhiqi_mode?: ZhiqiMode;
}

export interface LoginResult {
  token: string;
  expires_in: number;
  admin: AdminProfile;
}

export interface AdminItem {
  id: number;
  username: string;
  display_name: string | null;
  group: AdminGroupRef | null;
  /** 取所属用户组 */
  data_scope: DataScope;
  is_active: boolean;
  last_login_at: string | null;
  created_by: number | null;
  created_at: string;
  permissions?: string[];
}

export interface AdminGroupItem {
  id: number;
  code: AdminGroupCode | string;
  name: string;
  name_en: string;
  description: string | null;
  is_system: boolean;
  is_active: boolean;
  data_scope: DataScope;
  admin_count: number;
  created_at: string;
  permission_codes?: string[];
}

export interface AdminPermissionItem {
  code: string;
  module: string;
  name: string;
  type: PermissionType;
  parent_code: string | null;
  sort: number;
}

/** `GET /admin/admin-permissions/tree`：module → menu → action */
export interface PermissionTreeAction {
  code: string;
  name: string;
  type: "action";
}

export interface PermissionTreeMenu {
  code: string;
  name: string;
  type: "menu";
  children: PermissionTreeAction[];
}

export interface PermissionTreeModule {
  module: string;
  name: string;
  items: PermissionTreeMenu[];
}

export interface AdminOperationLogItem {
  id: number;
  admin: AdminBrief | null;
  group_name: string | null;
  permission_code: string;
  action: OperationAction;
  target_type: string;
  target_id: string | null;
  summary: string;
  request_id: string | null;
  ip: string | null;
  created_at: string;
}

/** 负责人候选 `GET /admin/projects/owner-options`（13 §6.4、§12.4） */
export interface OwnerOption {
  id: number;
  username: string;
  display_name: string | null;
  is_active: boolean;
  data_scope: DataScope;
  project_count: number;
}

// =====================================================================
// 系统配置（04 §6.6、§7.2、§7.18）
// =====================================================================

export interface Setting<V = Record<string, unknown>> {
  key: string;
  locale: SettingLocale;
  value: V;
}

/** `system_info`（`GET /admin/auth/site-info`） */
export interface SiteInfo {
  site_name: string;
  logo_url: string;
  footer: string;
  support_contact: string;
}

/** `GET /admin/settings/runtime`：前端表单所需的非敏感运行时子集 */
export interface RuntimeSettings {
  generation_config: {
    review_required: boolean;
    keyword: { default_count: number; max_count: number };
    title: { default_count: number; max_count: number; default_style: ContentStyle };
    content: {
      outline_first: boolean;
      segmented: boolean;
      max_sections: number;
      target_word_count: number;
      min_word_count: number;
      max_word_count: number;
      include_faq: boolean;
      include_seo_meta: boolean;
      default_format: ContentFormat;
    };
    rewrite: { modes: string[]; max_versions: number };
    [key: string]: unknown;
  };
  media_config: {
    image: Record<string, unknown>;
    video: Record<string, unknown>;
    daily_limits: { images: number; videos: number };
    [key: string]: unknown;
  };
  monitoring_config: {
    index_check: { seo_engines: SeoEngine[]; geo_engines: GeoEngine[]; query_by: ("url" | "title")[] };
    [key: string]: unknown;
  };
  geo_engines: { engines: { code: GeoEngine | string; name: string; enabled: boolean; [key: string]: unknown }[] };
  seo_providers: { engines: Partial<Record<SeoEngine, { provider: SeoProvider; enabled: boolean }>>; [key: string]: unknown };
  stats_config: { timezone: string; [key: string]: unknown };
  zhiqi_mode: ZhiqiMode;
}

// =====================================================================
// 项目（04 §6.7、§7.3）
// =====================================================================

export interface ProjectCounts {
  keywords: number;
  contents: number;
  links: number;
}

export interface Project {
  id: number;
  name: string;
  slug: string;
  industry: string | null;
  audience: string | null;
  brand_name: string | null;
  brand_info: string | null;
  description: string | null;
  language: Locale | string;
  default_style: ContentStyle;
  default_format: ContentFormat;
  /** 以 prompt_kind 为键的 `{kind: template_id}` */
  default_templates: Partial<Record<PromptKind, number>>;
  default_platform_ids: number[];
  status: ProjectStatus;
  /** 项目负责人（数据归属用户） */
  owner_id: number;
  created_by: number;
  created_at: string;
  updated_at: string;
  /** 列表项附带：负责人摘要 */
  owner?: AdminBrief | null;
  /** 列表项附带 */
  counts?: ProjectCounts;
  /** 详情附带：项目级 capability_routes 覆盖行 */
  routes?: CapabilityRoute[];
}

// =====================================================================
// Prompt 模板（04 §6.8，03 B.8）
// =====================================================================

export interface PromptVariable {
  name: string;
  label?: string;
  required?: boolean;
  default?: string;
  [key: string]: unknown;
}

export interface PromptTemplate {
  id: number;
  code: string;
  version: number;
  kind: PromptKind;
  capability: Capability;
  name: string;
  description: string | null;
  language: Locale | string;
  /** 0 = 全局模板 */
  project_id: number;
  system_prompt: string | null;
  user_prompt: string;
  variables: PromptVariable[];
  output_format: PromptOutputFormat;
  output_schema: Record<string, unknown> | null;
  model_params: Record<string, unknown> | null;
  status: PromptStatus;
  is_system: boolean;
  published_at: string | null;
  created_by: number | null;
  updated_by: number | null;
  created_at: string;
  updated_at: string;
  /**
   * 草稿保存（新建、编辑、published 复制为新版本、duplicate）的返回附带：未声明且非内置变量的告警
   * （`type=unknown_variable`，`loc=["body","system_prompt"|"user_prompt"]`，`input` 为变量名）；
   * 草稿只告警、`publish` 时同样的问题返回 400（docs/09 §5.3）
   */
  warnings?: ValidationErrorItem[];
}

/** `POST /admin/prompt-templates/{id}/preview` */
export interface PromptPreviewResult {
  system_prompt: string | null;
  user_prompt: string;
}

// =====================================================================
// 生成批次与任务摘要（04 §6.12、§7.4）
// =====================================================================

/** 根任务摘要（`GET /admin/generation-batches/{id}` 的 `tasks[]` 项） */
export interface BatchTaskSummary {
  id: number;
  operation: AiTaskOperation;
  target_type: AiTaskTargetType | null;
  target_id: number | null;
  /** 重试根任务指向被重试的旧根任务；首次创建为 null */
  parent_task_id?: number | null;
  status: AiTaskStatus;
  model: string | null;
  /** 根任务 `input.model`（请求级覆盖模型），未覆盖为 null */
  model_override: string | null;
  protocol: Protocol | null;
  candidate_index: number;
  attempt_count: number;
  last_error_category: ErrorCategory | null;
  last_error_message: string | null;
  request_id: string | null;
  progress: number;
  started_at: string | null;
  finished_at: string | null;
}

/** 根任务状态摘要：`GET /admin/contents/{id}/task`、`GET /admin/media/assets/{id}/task`、内容详情 `pending_tasks[]`、素材详情 `task` */
export interface AiTaskSummary {
  task_id: number;
  operation: AiTaskOperation;
  status: AiTaskStatus;
  progress: number;
  error_category: ErrorCategory | null;
  error_message: string | null;
  /** 根任务 `input.model`（文本与媒体相同），未覆盖为 null */
  model_override: string | null;
  finished_at: string | null;
}

export interface GenerationBatch {
  id: number;
  project_id: number;
  kind: BatchKind;
  status: BatchStatus;
  input: Record<string, unknown>;
  template_id: number | null;
  template_version: number | null;
  requested_count: number;
  produced_count: number;
  task_total: number;
  task_done: number;
  task_failed: number;
  error_summary: string | null;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  /** 详情附带 */
  tasks?: BatchTaskSummary[];
}

/** `POST /admin/keywords/generate`、`POST /admin/titles/generate` */
export interface BatchCreatedResult {
  batch_id: number;
  quota_warning?: QuotaWarning;
}

/** `POST /admin/contents/generate` */
export interface ContentGenerateResult {
  batch_id: number;
  content_ids: number[];
  quota_warning?: QuotaWarning;
}

/** `POST /admin/contents/{id}/generate-outline|generate-body|rewrite|generate-seo`、`POST /admin/ai/tasks/{id}/retry` */
export interface TaskCreatedResult {
  task_id: number;
  quota_warning?: QuotaWarning;
}

// =====================================================================
// 关键词与标题（04 §6.9、§6.10）
// =====================================================================

export interface Keyword {
  id: number;
  project_id: number;
  keyword: string;
  normalized_keyword: string;
  language: Locale | string;
  intent: KeywordIntent;
  keyword_type: KeywordType;
  difficulty: number | null;
  heat: number | null;
  score: number | null;
  seed: string | null;
  source: KeywordSource;
  batch_id: number | null;
  /** 产出该词的尝试行 */
  ai_task_id: number | null;
  reason: string | null;
  tags: string[];
  status: KeywordStatus;
  adopted_by: number | null;
  adopted_at: string | null;
  title_count: number;
  content_count: number;
  created_by: number | null;
  created_at: string;
  updated_at: string;
}

/** `POST /admin/keywords/import`、`POST /admin/keywords/import-file` */
export interface KeywordImportResult {
  created: number;
  skipped: number;
  errors: { index: number; keyword: string; reason: "empty" | "too_long" | "invalid_intent" | "invalid_keyword_type" }[];
}

export interface Title {
  id: number;
  project_id: number;
  keyword_id: number;
  title: string;
  original_title: string | null;
  style: ContentStyle;
  ai_score: number | null;
  manual_score: number | null;
  is_edited: boolean;
  source: TitleSource;
  batch_id: number | null;
  ai_task_id: number | null;
  status: TitleStatus;
  adopted_by: number | null;
  adopted_at: string | null;
  content_count: number;
  created_by: number | null;
  created_at: string;
  updated_at: string;
}

// =====================================================================
// 内容与版本（04 §6.11、§7.6）
// =====================================================================

export interface OutlineItem {
  heading: string;
  level: number;
  points?: string[];
  [key: string]: unknown;
}

export interface FaqItem {
  q: string;
  a: string;
}

export interface ContentGenerationParams {
  outline_first?: boolean;
  target_word_count?: number;
  include_faq?: boolean;
  include_seo_meta?: boolean;
  segmented?: boolean;
  [key: string]: unknown;
}

/** 内容详情中的 `current_version` 摘要 */
export interface ContentVersionRef {
  id: number;
  version_no: number;
  source: VersionSource;
  model: string | null;
  ai_task_id: number | null;
  change_summary: string | null;
  created_by: number | null;
  created_at: string;
}

export interface Content {
  id: number;
  project_id: number;
  title_id: number | null;
  keyword_id: number | null;
  title: string;
  format: ContentFormat;
  language: Locale | string;
  style: ContentStyle;
  status: ContentStatus;
  prev_status: ContentStatus | null;
  review_result: ReviewResult | null;
  current_version_id: number | null;
  version_count: number;
  outline: OutlineItem[] | null;
  /** 列表不含 body */
  body?: string | null;
  summary: string | null;
  seo_title: string | null;
  seo_description: string | null;
  seo_keywords: string[] | null;
  faq: FaqItem[] | null;
  word_count: number;
  cover_asset_id: number | null;
  template_id: number | null;
  generation_params: ContentGenerationParams | null;
  batch_id: number | null;
  /** 根任务 */
  ai_task_id: number | null;
  quality_score: number | null;
  risk_flags: string[];
  reviewed_by: number | null;
  reviewed_at: string | null;
  review_note: string | null;
  link_count: number;
  first_published_at: string | null;
  created_by: number | null;
  updated_by: number | null;
  created_at: string;
  updated_at: string;
  /** 详情附带 */
  current_version?: ContentVersionRef | null;
  assets?: MediaAsset[];
  active_task_id?: number | null;
  pending_tasks?: AiTaskSummary[];
  /** `PUT /admin/contents/{id}`、版本恢复的返回附带 */
  version_created?: boolean;
}

/** 版本：列表返回 `{id,version_no,source,title,word_count,model,change_summary,restored_from_version_id,created_by,created_at}`，详情返回全部字段 */
export interface ContentVersion {
  id: number;
  content_id?: number;
  version_no: number;
  source: VersionSource;
  title: string;
  body?: string;
  content_hash?: string;
  outline?: OutlineItem[] | null;
  summary?: string | null;
  seo_title?: string | null;
  seo_description?: string | null;
  seo_keywords?: string[] | null;
  faq?: FaqItem[] | null;
  word_count: number;
  ai_task_id?: number | null;
  template_id?: number | null;
  model: string | null;
  change_summary: string | null;
  restored_from_version_id: number | null;
  created_by: number | null;
  created_at: string;
  updated_at?: string;
}

// =====================================================================
// 素材（04 §6.13、§6.14、§7.8、§7.9）
// =====================================================================

export interface ImageMediaParams {
  resolution: ImageResolution;
  aspect_ratio: AspectRatio;
  reference_image_urls: string[];
}

export interface VideoMediaParams {
  duration: number;
  resolution: VideoResolution;
  aspect_ratio: AspectRatio | string | null;
  size: string | null;
  input_reference: string | null;
  reference_image_urls: string[];
  reference_video_urls: string[];
  reference_audio_urls: string[];
  first_frame_image_url: string | null;
  last_frame_image_url: string | null;
  generate_audio: boolean;
}

/** 图片为 `ImageMediaParams`，视频为 `VideoMediaParams` */
export type MediaParams = (Partial<ImageMediaParams> & Partial<VideoMediaParams>) & Record<string, unknown>;

/** 素材引用（仅详情返回、实时计算） */
export interface MediaAssetReferences {
  cover_of: number | null;
  bound_content_id: number | null;
  referenced_by_asset_ids: { id: number; status: MediaStatus }[];
  count: number;
}

export interface MediaAsset {
  id: number;
  project_id: number | null;
  content_id: number | null;
  kind: MediaKind;
  usage_type: MediaUsageType;
  source: MediaSource;
  status: MediaStatus;
  /** 根任务 */
  ai_task_id: number | null;
  prompt: string | null;
  negative_prompt: string | null;
  model: string | null;
  params: MediaParams;
  reference_asset_ids: number[];
  upstream_task_id: string | null;
  upstream_url: string | null;
  storage_key: string | null;
  /** 转存后的稳定地址，`ready` 才有 */
  url: string | null;
  thumbnail_key: string | null;
  thumbnail_url: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  width: number | null;
  height: number | null;
  duration_seconds: number | null;
  file_hash: string | null;
  progress: number;
  error_category: ErrorCategory | null;
  error_message: string | null;
  transfer_attempts: number;
  next_transfer_at: string | null;
  ready_at: string | null;
  failed_at: string | null;
  sort: number;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  /** 详情附带 */
  task?: AiTaskSummary | null;
  references?: MediaAssetReferences;
}

/** `POST /admin/media/images/generate` */
export interface ImageGenerateResult {
  asset_ids: number[];
  task_ids: number[];
  quota_warning?: QuotaWarning;
}

/** `POST /admin/media/videos/generate` */
export interface VideoGenerateResult {
  asset_id: number;
  task_id: number;
  quota_warning?: QuotaWarning;
}

/** `POST /admin/media/assets/{id}/retry` */
export interface MediaRetryResult {
  asset: MediaAsset;
  task_id: number;
  /** true 表示复用了旧上游任务，不重复计费 */
  resumed: boolean;
}

/** `POST /admin/media/assets/{id}/transfer` */
export interface MediaTransferResult {
  asset: MediaAsset;
}

/** `POST /admin/uploads/image|video` */
export interface UploadResult {
  asset_id: number;
  url: string;
  /** false 表示 URL 非公网，真实 zhiqiapi 不可用 */
  public: boolean;
}

// =====================================================================
// AI 网关（04 §6.15、§7.15~§7.17）
// =====================================================================

export interface AiTask {
  id: number;
  project_id: number | null;
  capability: Capability;
  operation: AiTaskOperation;
  protocol: Protocol | null;
  trigger_type: AiTaskTriggerType;
  target_type: AiTaskTargetType | null;
  target_id: number | null;
  batch_id: number | null;
  /** 尝试行所属根任务；根任务行为 null */
  root_task_id: number | null;
  parent_task_id: number | null;
  route_id: number | null;
  candidate_index: number;
  attempt: number;
  segment_index: number | null;
  model: string | null;
  template_id: number | null;
  status: AiTaskStatus;
  pause_count: number;
  request_id: string | null;
  upstream_task_id: string | null;
  output_excerpt: string | null;
  prompt_tokens: number;
  completion_tokens: number;
  cache_tokens: number;
  quota_reserved: number;
  quota_estimated: number;
  quota_actual: number | null;
  cost_cny: number | null;
  reconciled_at: string | null;
  usage_log_type: UsageLogType | null;
  error_category: ErrorCategory | null;
  error_message: string | null;
  http_status: number | null;
  duration_ms: number | null;
  upstream_latency_ms: number | null;
  progress: number;
  poll_count: number;
  next_poll_at: string | null;
  deadline_at: string | null;
  locked_by: string | null;
  heartbeat_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  /** 详情附带：根任务业务参数（尝试行为 null） */
  input?: Record<string, unknown> | null;
  /** 详情附带：尝试行脱敏后的实际请求体 */
  request_payload?: Record<string, unknown> | null;
  /** 详情附带：响应元数据（尝试行 / 媒体根任务 poll、download / 文本根任务 apply_counts） */
  response_meta?: AiTaskResponseMeta | null;
  /** 详情附带：根任务的全部尝试行 */
  attempts?: AiTask[];
}

export interface AiTaskResponseMeta {
  finish_reason?: string | null;
  usage?: Record<string, unknown> | null;
  http_status?: number | null;
  usage_missing?: boolean;
  degraded_params?: string[];
  fallback_from?: number | null;
  retry_request_ids?: string[];
  poll?: {
    last_request_id: string | null;
    last_http_status: number | null;
    error_code: string | null;
    error_message: string | null;
    consecutive_404: number;
    request_ids: string[];
  };
  download?: {
    source: DownloadSource;
    request_id: string | null;
    http_status: number | null;
    request_ids: string[];
  };
  apply_counts?: { duplicates: number; invalid: number; intent_missing: number; empty_output: number; too_long: number };
  stale_after_submit?: boolean;
  [key: string]: unknown;
}

export interface AiModel {
  id: number;
  model_id: string;
  owned_by: string | null;
  vendor_id: number | null;
  vendor_name: string | null;
  description: string | null;
  tags: string[];
  icon: string | null;
  cover_url: string | null;
  supported_endpoint_types: string[];
  modalities: Modality[];
  quota_type: QuotaType;
  model_ratio: number | null;
  model_price: number | null;
  completion_ratio: number | null;
  cache_ratio: number | null;
  create_cache_ratio: number | null;
  enable_groups: string[];
  billing_mode: string | null;
  billing_expr: string | null;
  model_price_type: string | null;
  sort_order: number;
  is_available: boolean;
  last_seen_at: string | null;
  last_health_status: HealthStatus;
  last_health_at: string | null;
  last_health_latency_ms: number | null;
  synced_at: string | null;
  created_at: string;
  updated_at: string;
  /** 仅 `GET /admin/ai/models/{id}` 返回 */
  raw_pricing?: Record<string, unknown> | null;
}

/** `GET /admin/ai/models/options` */
export interface AiModelOption {
  model_id: string;
  vendor_name: string | null;
  modalities: Modality[];
  is_available: boolean;
  last_health_status: HealthStatus;
  quota_type: QuotaType;
}

/** `POST /admin/ai/models/sync` */
export interface ModelSyncResult {
  total: number;
  added: number;
  updated: number;
  unavailable: number;
  synced_at: string;
  request_ids: { models: string | null; pricing: string | null };
}

/** 路由列表中主 / 备模型的健康与熔断标签 */
export interface RouteModelStatus {
  model: string;
  /** 0 主模型，1..n 备选 */
  candidate_index: number;
  health: HealthStatus;
  /** `ai_models.is_available`；模型不在 `ai_models`（尚未同步到目录）时为 null */
  is_available: boolean | null;
  breaker_state: BreakerState;
  breaker_reason: BreakerReason | null;
}

export interface CapabilityRoute {
  id: number;
  capability: Capability;
  /** 0 = 全局；>0 = 项目覆盖 */
  project_id: number;
  protocol: Protocol;
  primary_model: string;
  fallback_models: string[];
  params: Record<string, unknown>;
  timeout_seconds: number | null;
  max_attempts: number;
  is_enabled: boolean;
  note: string | null;
  updated_by: number | null;
  created_at: string;
  updated_at: string;
  /** `GET /admin/ai/routes` 附带：主模型的熔断状态 */
  breaker_state?: BreakerState;
  breaker_reason?: BreakerReason | null;
  /** `GET /admin/ai/routes` 附带：主 / 备模型（按候选链顺序）的 health、is_available 与熔断状态 */
  models?: RouteModelStatus[];
  /** `POST /admin/ai/routes`、`PUT /admin/ai/routes/{id}` 附带：`is_available=0` 的主 / 备模型各一项，`type=model_unavailable` */
  warnings?: ValidationErrorItem[];
}

/** `POST /admin/ai/routes/{id}/test` 的结果项 */
export interface RouteTestResultItem {
  model: string;
  status: HealthStatus;
  latency_ms: number | null;
  request_id: string | null;
  error_category: ErrorCategory | null;
}

/** `POST /admin/ai/routes/{id}/reset-breaker` */
export interface ResetBreakerResult {
  reset_models: string[];
  paused_cleared: boolean;
}

/** `POST /admin/ai/health/probe` */
export interface HealthResult {
  capability: Capability;
  model: string;
  protocol: Protocol | null;
  status: HealthStatus;
  latency_ms: number | null;
  request_id: string | null;
  error_category: ErrorCategory | null;
  error_message: string | null;
  checked_at: string;
}

export interface WorkerHeartbeat {
  name: "worker" | "monitor_worker" | string;
  hostname: string;
  /** 心跳值缺 pid 时取键名中的 pid 段（通常为整数） */
  pid: number | string;
  /** 心跳值无法解析时为 null（此时 `alive=false`） */
  heartbeat_at: string | null;
  alive: boolean;
}

/** `GET /admin/ai/health` */
export interface AiHealthSnapshot {
  zhiqi_mode: ZhiqiMode;
  base_url: string;
  paused: { quota_exceeded: boolean; auth_failed: boolean };
  models: {
    capability: Capability;
    model: string;
    status: HealthStatus;
    latency_ms: number | null;
    checked_at: string | null;
    breaker_state: BreakerState;
    breaker_reason: BreakerReason | null;
    /** 模型不在 `ai_models` 时为 null */
    is_available: boolean | null;
  }[];
  workers: WorkerHeartbeat[];
}

export interface AiUsageLog {
  id: number;
  entry_hash: string;
  upstream_log_id: number | null;
  request_id: string | null;
  log_type: UsageLogType;
  model_name: string | null;
  group_name: string | null;
  quota: number;
  prompt_tokens: number;
  completion_tokens: number;
  cache_tokens: number;
  group_ratio: number | null;
  model_ratio: number | null;
  completion_ratio: number | null;
  request_path: string | null;
  upstream_task_id: string | null;
  upstream_created_at: string | null;
  /** 匹配到的本地尝试行 */
  ai_task_id: number | null;
  /** 派生字段：`ai_task_id` 非空 */
  matched: boolean;
  matched_at: string | null;
  pulled_at: string;
  raw: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

/** `POST /admin/ai/usage/reconcile` */
export interface ReconcileResult {
  pulled: number;
  new: number;
  matched: number;
  unmatched: number;
  window_overflow: boolean;
  request_ids: string[];
}

/** `GET /admin/ai/usage/summary` 的行 */
export interface AiUsageSummaryRow {
  /** 分组键（model / capability / 项目 ID 字符串 / YYYY-MM-DD）；分组列为空（如无项目的任务、无模型）时为 null */
  key: string | null;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  quota_estimated: number;
  quota_actual: number;
  cost_cny: number;
  /** `reconciled_at` 非空的尝试行数 / calls（0~1） */
  reconciled_rate: number;
}

/**
 * `GET /admin/ai/usage/last-pull`（Redis `ai:usage:last_pull`；键不存在时接口 `data=null`）。
 * `all` 范围（未带 `owner_id`）返回完整摘要；`own` 范围（或带 `owner_id`）只返回 `pulled_at` / `window_overflow`（docs/13 §4.3）。
 */
export interface UsageLastPull {
  pulled_at: string;
  window_overflow: boolean;
  pulled?: number;
  new?: number;
  matched?: number;
  unmatched?: number;
  request_ids?: string[];
}

/** `POST /admin/ai/tasks/{id}/retry` */
export interface AiTaskRetryResult {
  task_id: number;
}

/** `POST /admin/ai/routes` 请求体（项目覆盖路由，`project_id > 0`） */
export interface CapabilityRouteCreateBody {
  capability: Capability;
  project_id: number;
  protocol: Protocol;
  primary_model: string;
  fallback_models: string[];
  params: Record<string, unknown>;
  timeout_seconds: number | null;
  max_attempts: number;
  is_enabled: boolean;
  note: string | null;
}

/** `PUT /admin/ai/routes/{id}` 请求体：`capability` / `project_id` 不可改 */
export type CapabilityRouteUpdateBody = Partial<Omit<CapabilityRouteCreateBody, "capability" | "project_id">>;

// =====================================================================
// 发布平台、回填链接与检测（04 §6.16~§6.18、§7.10~§7.12）
// =====================================================================

export interface PlatformFetchConfig {
  user_agent?: string;
  headers?: Record<string, string>;
  timeout_seconds?: number;
  respect_robots?: boolean;
  allow_http?: boolean;
  [key: string]: unknown;
}

export interface Platform {
  id: number;
  code: string;
  name: string;
  name_en: string;
  icon: string | null;
  home_url: string | null;
  url_patterns: string[];
  deleted_markers: string[];
  redirect_markers: string[];
  fetch_config: PlatformFetchConfig;
  is_system: boolean;
  is_active: boolean;
  sort: number;
  created_at: string;
  updated_at: string;
  /** 列表附带：调用者可见的链接数 */
  link_count?: number;
}

/** 删除检测证据（`link_checks.evidence`，与平台测试的 `evidence` 同构） */
export interface LinkCheckEvidence {
  marker: string | null;
  context: string | null;
  redirects: string[];
  headers: Record<string, string>;
  title?: string | null;
  text_excerpt?: string | null;
  short_text?: boolean;
  [key: string]: unknown;
}

/** `POST /admin/platforms/{id}/test` */
export interface PlatformTestResult {
  result_status: LinkCheckResult;
  matched_rule: LinkCheckRule;
  http_status: number | null;
  final_url: string | null;
  redirect_count: number;
  title: string | null;
  duration_ms: number | null;
  evidence: LinkCheckEvidence | null;
}

/** `POST /admin/platforms/detect` */
export interface PlatformDetectResult {
  platform_id: number;
  code: string;
}

export interface SeoEngineStatus {
  status: SeoIndexStatus;
  checked_at: string | null;
  first_indexed_at: string | null;
  check_count: number;
}

export interface GeoEngineStatus {
  status: GeoCiteStatus;
  checked_at: string | null;
  first_cited_at: string | null;
  check_count: number;
}

export interface PublishLink {
  id: number;
  project_id: number;
  content_id: number;
  platform_id: number;
  url: string;
  normalized_url: string;
  url_hash: string;
  domain: string;
  publish_account: string | null;
  published_at: string;
  backfilled_by: number | null;
  title_snapshot: string | null;
  alive_status: LinkAliveStatus;
  alive_changed_at: string | null;
  last_checked_at: string | null;
  next_check_at: string | null;
  check_count: number;
  consecutive_unknown: number;
  consecutive_suspected: number;
  last_http_status: number | null;
  baseline_title: string | null;
  /** 有符号 64 位整数，可能超过 2^53，按字符串展示、不参与计算 */
  baseline_simhash: number | string | null;
  baseline_excerpt: string | null;
  baseline_captured_at: string | null;
  /** 按引擎解码后的收录状态 */
  seo_status: Partial<Record<SeoEngine, SeoEngineStatus>>;
  geo_status: Partial<Record<GeoEngine, GeoEngineStatus>>;
  seo_indexed_any: boolean;
  geo_cited_any: boolean;
  first_indexed_at: string | null;
  first_cited_at: string | null;
  last_index_checked_at: string | null;
  next_index_check_at: string | null;
  index_check_count: number;
  index_checks_done: number;
  is_monitoring: boolean;
  note: string | null;
  created_at: string;
  updated_at: string;
  /** 「超期未收录」标记（`index_check.overdue_days`，只用于列表 / 详情展示） */
  index_overdue?: boolean;
  /** 详情（及 `GET /admin/contents/{id}/links`）附带的平台摘要 */
  platform?: { id: number; code: string; name: string; name_en?: string; icon?: string | null } | null;
  /** 详情附带：内容摘要（内容对调用者不可见时为 null） */
  content?: {
    id: number;
    title: string;
    status: ContentStatus;
    project_id?: number;
    link_count?: number;
    first_published_at?: string | null;
  } | null;
  last_check?: LinkCheck | null;
  /** 详情附带：当前配置的收录引擎与提供器（`seo_providers.engines` / `geo_engines.engines[]`，GEO 提供器固定 `zhiqi_model`） */
  engines?: {
    seo: { engine: string; provider: SeoProvider; enabled: boolean }[];
    geo: { engine: string; name: string | null; provider: "zhiqi_model"; enabled: boolean }[];
  };
}

/** `POST /admin/links` */
export interface LinkCreateResult {
  link: PublishLink;
  queued: boolean;
}

/** `POST /admin/links/batch` 的逐条结果；`reason` 仅在已存在链接对调用者不可见时出现（`owned_by_other`，`link_id=null`） */
export interface LinkBatchResultItem {
  index: number;
  ok: boolean;
  link_id: number | null;
  queued: boolean;
  code: number;
  message: string;
  reason?: "owned_by_other";
}

/** `POST /admin/links/batch` */
export interface LinkBatchResult {
  created: number;
  failed: number;
  results: LinkBatchResultItem[];
}

/** `POST /admin/links` 与 `POST /admin/links/batch` 的 `items[]`（04 §6.17、§7.10） */
export interface LinkCreateBody {
  content_id: number;
  platform_id?: number | null;
  url: string;
  publish_account?: string | null;
  /** ISO 8601 UTC；缺省取当前时间 */
  published_at?: string | null;
  note?: string | null;
}

/** `PUT /admin/links/{id}`：URL 不可改 */
export interface LinkUpdateBody {
  platform_id?: number;
  publish_account?: string | null;
  published_at?: string;
  note?: string | null;
}

/** `POST /admin/links/{id}/check`、`/rebaseline`、`/index-check` 的返回 */
export interface LinkQueuedResult {
  queued: boolean;
  reason?: "already_queued" | "daily_limit";
}

/** `POST /admin/links/{id}/index-check`：`engines` 省略时取 `kinds` 下全部启用引擎 */
export interface LinkIndexCheckBody {
  kinds: IndexKind[];
  engines?: string[];
}

/** `POST /admin/links/{id}/mark-index`：`kind=seo` 只能 `indexed`/`not_indexed`，`kind=geo` 只能 `cited`/`not_cited` */
export interface LinkMarkIndexBody {
  kind: IndexKind;
  engine: string;
  status: "indexed" | "not_indexed" | "cited" | "not_cited";
  note?: string | null;
  evidence_url?: string | null;
}

/** `POST /admin/platforms`（`PUT` 时 `code` 不可改，其余字段可选） */
export interface PlatformCreateBody {
  code: string;
  name: string;
  name_en: string;
  icon?: string | null;
  home_url?: string | null;
  url_patterns: string[];
  deleted_markers: string[];
  redirect_markers: string[];
  fetch_config: PlatformFetchConfig;
  is_active: boolean;
  sort: number;
}

export type PlatformUpdateBody = Partial<Omit<PlatformCreateBody, "code">>;

export interface LinkCheck {
  id: number;
  link_id: number;
  check_type: CheckType;
  result_status: LinkCheckResult;
  previous_status: LinkAliveStatus;
  applied_status: LinkAliveStatus;
  http_status: number | null;
  final_url: string | null;
  redirect_count: number;
  matched_rule: LinkCheckRule | null;
  title: string | null;
  /** 有符号 64 位整数，按字符串展示 */
  simhash: number | string | null;
  hamming_distance: number | null;
  response_bytes: number | null;
  duration_ms: number | null;
  error_message: string | null;
  evidence: LinkCheckEvidence | null;
  checked_at: string;
  triggered_by: number | null;
  created_at?: string;
  updated_at?: string;
  /** 监控页全局记录附带 */
  link?: { id: number; url: string; platform_code: string; content_id?: number };
}

export interface IndexCheckEvidence {
  citations?: { url: string; title?: string | null; snippet?: string | null; source?: string | null }[];
  answer_excerpt?: string | null;
  source?: string | null;
  [key: string]: unknown;
}

export interface IndexCheck {
  id: number;
  link_id: number;
  kind: IndexKind;
  engine: SeoEngine | GeoEngine;
  provider: SeoProvider | GeoProvider;
  check_type: IndexCheckType;
  result_status: SeoIndexStatus | GeoCiteStatus;
  previous_status: SeoIndexStatus | GeoCiteStatus | null;
  match_mode: IndexMatchMode;
  query_text: string | null;
  ai_task_id: number | null;
  request_id: string | null;
  model: string | null;
  evidence_title: string | null;
  evidence_snippet: string | null;
  evidence_url: string | null;
  evidence: IndexCheckEvidence | null;
  confidence: number | null;
  duration_ms: number | null;
  error_category: ErrorCategory | null;
  error_message: string | null;
  checked_at: string;
  triggered_by: number | null;
  created_at?: string;
  updated_at?: string;
  /** 监控页全局记录附带 */
  link?: { id: number; url: string; platform_code: string; content_id?: number };
}

/** `GET /admin/monitoring/overview` */
export interface MonitoringOverview {
  link_checks: { due: number; queued: number; today: number; last_run_at: string | null };
  index_checks: { due: number; queued: number; today: number; last_run_at: string | null };
  workers: WorkerHeartbeat[];
  daily_limits: {
    link_checks: { limit: number; used: number };
    index_checks: { limit: number; used: number };
  };
}

// =====================================================================
// 告警（04 §6.19、§7.13）
// =====================================================================

export interface Alert {
  id: number;
  alert_type: AlertType;
  severity: AlertSeverity;
  status: AlertStatus;
  project_id: number | null;
  target_type: AlertTargetType;
  /** `ai_model` / `worker` / `system` 类为 null */
  target_id: number | null;
  target_key: string;
  dedupe_key: string;
  title: string;
  message: string;
  payload: Record<string, unknown>;
  first_triggered_at: string;
  last_triggered_at: string;
  trigger_count: number;
  acknowledged_by: number | null;
  acknowledged_at: string | null;
  resolved_by: number | null;
  resolved_at: string | null;
  resolution_note: string | null;
  notified_channels: AlertChannel[];
  created_at: string;
  updated_at: string;
}

export type SeverityCounts = Record<AlertSeverity, number>;

/** `GET /admin/alerts/summary` */
export interface AlertSummary {
  open: SeverityCounts;
  acknowledged: SeverityCounts;
  today_opened: number;
  today_resolved: number;
}

/** `POST /admin/alerts/batch-resolve` */
export interface AlertBatchResolveBody {
  ids: number[];
  note?: string | null;
}

/** `POST /admin/monitoring/link-checks/run`（用户视角下由 client.ts 附加查询参数 `owner_id`） */
export interface MonitoringLinkChecksRunBody {
  project_id?: number | null;
  platform_id?: number | null;
  link_ids?: number[] | null;
  only_due: boolean;
}

/** `POST /admin/monitoring/index-checks/run`：`engines` 省略时取 `kinds` 下全部启用引擎 */
export interface MonitoringIndexChecksRunBody extends MonitoringLinkChecksRunBody {
  kinds: IndexKind[];
  engines?: string[] | null;
}

// =====================================================================
// 统计与报表（04 §6.20、§7.14，12 §9）
// =====================================================================

/** `daily_stats` 行 */
export interface DailyStats {
  id: number;
  stat_date: string;
  project_id: number;
  dimension: StatsDimension;
  dimension_key: string;
  keywords_created: number;
  keywords_adopted: number;
  titles_created: number;
  titles_adopted: number;
  contents_created: number;
  contents_approved: number;
  contents_published: number;
  links_backfilled: number;
  links_checked: number;
  links_deleted: number;
  links_changed: number;
  links_restored: number;
  links_alive_snapshot: number;
  links_total_snapshot: number;
  seo_checks: number;
  seo_newly_indexed: number;
  seo_indexed_snapshot: number;
  geo_checks: number;
  geo_newly_cited: number;
  geo_cited_snapshot: number;
  index_hours_sum: number;
  index_hours_links: number;
  ai_calls: number;
  ai_succeeded: number;
  ai_failed: number;
  ai_duration_ms_sum: number;
  tasks_succeeded: number;
  tasks_failed: number;
  task_duration_ms_sum: number;
  prompt_tokens: number;
  completion_tokens: number;
  quota_estimated: number;
  quota_actual: number;
  quota_reconciled_calls: number;
  cost_cny: number;
  images_generated: number;
  videos_generated: number;
  media_failed: number;
  alerts_opened: number;
  alerts_resolved: number;
  extra: { quota_estimated_reconciled?: number; [key: string]: unknown } | null;
  computed_at: string;
  created_at: string;
  updated_at: string;
}

export interface StatsOverviewMeta {
  range: StatsRange;
  start_date: string;
  end_date: string;
  timezone: string;
  project_id: number;
  /** `all` 全平台；`owner` 某一用户负责的项目（13 §10） */
  scope: "all" | "owner";
  owner_id: number | null;
  today_source: "daily_stats" | "realtime" | "none";
  snapshot_date: string | null;
  computed_at: string | null;
  cached: boolean;
  warnings: ("realtime_unavailable" | "cache_unavailable" | "p50_sampled" | string)[];
}

/** 总览标量指标（比率为 0~1 小数，分母为 0 时 null） */
export interface StatsOverviewKpis {
  keywords_total: number;
  keywords_created: number;
  keywords_adopted: number;
  keyword_adopt_rate: number | null;
  titles_total: number;
  titles_created: number;
  titles_adopted: number;
  contents_total: number;
  contents_created: number;
  contents_approved: number;
  contents_published: number;
  links_total: number;
  links_backfilled: number;
  links_alive: number;
  links_deleted: number;
  link_alive_rate: number | null;
  link_deleted_rate: number | null;
  links_checked: number;
  seo_index_rate: number | null;
  geo_cite_rate: number | null;
  seo_newly_indexed: number;
  geo_newly_cited: number;
  time_to_index_hours_avg: number | null;
  time_to_index_hours_p50: number | null;
  ai_calls: number;
  ai_success_rate: number | null;
  ai_avg_duration_ms: number | null;
  task_success_rate: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  tokens_total: number;
  quota_estimated: number;
  quota_actual: number;
  quota_reconciled_rate: number | null;
  cost_cny: number;
  cost_cny_per_content: number | null;
  images_generated: number;
  videos_generated: number;
  media_failed: number;
  media_success_rate: number | null;
  alerts_opened: number;
  alerts_resolved: number;
}

export interface StatsCompareItem {
  previous: number | null;
  delta: number | null;
  delta_rate: number | null;
}

export interface CostByModelRow {
  model: string;
  ai_calls: number;
  ai_success_rate: number | null;
  tokens_total: number;
  quota_estimated: number;
  quota_actual: number;
  cost_cny: number;
  share: number | null;
}

export interface CostByCapabilityRow {
  capability: Capability;
  ai_calls: number;
  ai_success_rate: number | null;
  tokens_total: number;
  quota_estimated: number;
  quota_actual: number;
  cost_cny: number;
  share: number | null;
  task_success_rate: number | null;
  task_avg_duration_ms: number | null;
}

/** 存量口径的引擎收录 / 引用率 */
export interface EngineRate {
  rate: number | null;
  hit: number | null;
  total: number;
}

export interface StatsOverviewBreakdowns {
  keywords_by_status: Record<KeywordStatus, number>;
  titles_by_status: Record<TitleStatus, number>;
  contents_by_status: Record<ContentStatus, number>;
  links_by_status: Record<LinkAliveStatus, number>;
  alerts_open: SeverityCounts;
  cost_by_capability: CostByCapabilityRow[];
  cost_by_model: CostByModelRow[];
  seo_index_rate_by_engine: Partial<Record<SeoEngine, EngineRate>>;
  geo_cite_rate_by_engine: Partial<Record<GeoEngine, EngineRate>>;
}

export interface StatsOverviewSeries {
  dates: string[];
  ai_calls: number[];
  cost_cny: number[];
  links_backfilled: number[];
  seo_newly_indexed: number[];
}

/** `GET /admin/stats/overview`、`GET /admin/projects/{id}/overview` */
export interface StatsOverview {
  meta: StatsOverviewMeta;
  kpis: StatsOverviewKpis;
  /** 只含流量类与比率类指标 */
  compare: Partial<Record<keyof StatsOverviewKpis, StatsCompareItem>>;
  breakdowns: StatsOverviewBreakdowns;
  series: StatsOverviewSeries;
}

/** `GET /admin/stats/trends` 的行：`date` 为周期标签（`YYYY-MM-DD` / `YYYY-Www` / `YYYY-MM`），其余键为指标值 */
export interface TrendPoint {
  date: string;
  [metric: string]: number | string | null;
}

/** `GET /admin/stats/breakdown` 的行 */
export interface BreakdownRow {
  key: string;
  label: string;
  value: number | null;
  share: number | null;
  values: Record<string, number | null>;
}

/** 榜单 `fastest_indexed` 的行 */
export interface FastestIndexedRankingRow {
  rank: number;
  content_id: number;
  title: string;
  link_id: number;
  url: string;
  platform_code: string;
  platform_name: string;
  project_id: number;
  project_name: string;
  published_at: string;
  first_indexed_at: string;
  hours: number;
}

/** 榜单 `most_deleted_platforms` / `top_cost_models` / `top_cost_projects` / `top_failed_models` 的行 */
export interface MetricRankingRow {
  rank: number;
  key: string;
  label: string;
  value: number;
  /** most_deleted_platforms: `{links_total}`；top_cost_models / top_failed_models: `{ai_calls, ai_success_rate}`；top_cost_projects: `{contents_created, cost_cny_per_content}` */
  extra: Record<string, number | null>;
}

/** `GET /admin/stats/rankings` 的行 */
export type RankingRow = FastestIndexedRankingRow | MetricRankingRow;

/** 榜单类型 → 行结构 */
export type RankingRowOf<T extends RankingType> = T extends "fastest_indexed" ? FastestIndexedRankingRow : MetricRankingRow;

/** `POST /admin/stats/recompute`：≤ 7 天同步返回，> 7 天入队返回 202 */
export type RecomputeResult =
  | { days: number; rows_upserted: number; skipped: string[]; duration_ms: number }
  | { queued: true; days: number };

// =====================================================================
// 健康检查（04 §6.21、§7.16）
// =====================================================================

export interface WorkerGroupHealth {
  alive: boolean;
  replicas: number;
  heartbeat_at: string | null;
}

/** `GET /api/v1/health` */
export interface SystemHealth {
  status: "ok" | "degraded";
  db: boolean;
  redis: boolean;
  zhiqi_mode: ZhiqiMode;
  workers: { worker: WorkerGroupHealth; monitor_worker: WorkerGroupHealth };
  warnings: string[];
  version: string;
}

// =====================================================================
// 项目与 Prompt 模板的请求体（04 §6.7、§6.8、§7.3）
// =====================================================================

/** `POST /admin/projects`；`PUT /admin/projects/{id}` 同结构（`owner_id` 变化即转移负责人） */
export interface ProjectBody {
  name: string;
  slug: string;
  industry?: string | null;
  audience?: string | null;
  brand_name?: string | null;
  brand_info?: string | null;
  description?: string | null;
  language: Locale | string;
  default_style: ContentStyle;
  default_format: ContentFormat;
  /** 键 ∈ prompt_kind，值为该 kind 的可见 published 模板 ID（省略的 kind 按系统模板回退） */
  default_templates?: Partial<Record<PromptKind, number>>;
  default_platform_ids?: number[];
  /** 省略时为当前用户；`own` 范围只能为本人 */
  owner_id?: number;
}

/** `PUT /admin/projects/{id}/routes` 的一行（未出现的能力删除覆盖行） */
export interface ProjectRouteInput {
  capability: Capability;
  primary_model: string;
  fallback_models: string[];
  params?: Record<string, unknown>;
  protocol?: Protocol;
}

/** `POST /admin/prompt-templates` */
export interface PromptTemplateCreateBody {
  code: string;
  kind: PromptKind;
  name: string;
  description?: string | null;
  language: Locale | string;
  /** 0 = 全局 */
  project_id: number;
  system_prompt?: string | null;
  user_prompt: string;
  variables: PromptVariable[];
  output_format: PromptOutputFormat;
  output_schema?: Record<string, unknown> | null;
  model_params?: Record<string, unknown> | null;
}

/** `PUT /admin/prompt-templates/{id}`：`code`/`kind` 创建后不可修改 */
export type PromptTemplateUpdateBody = Partial<Omit<PromptTemplateCreateBody, "code" | "kind">>;

/** `POST /admin/prompt-templates/{id}/duplicate` */
export interface PromptTemplateDuplicateBody {
  code: string;
  name: string;
  /** 复制到的范围（0 = 全局，>0 = 项目）；省略沿用源模板（从系统模板派生项目模板时指定） */
  project_id?: number;
}

// =====================================================================
// 关键词 / 标题 / 内容 / 批次的请求体（04 §6.9~§6.12、§7.4~§7.7；09 §6~§8）
// =====================================================================
import type { RewriteMode, RewriteScope } from "./enums";

/** 关键词 / 标题的状态动作 */
export type AdoptAction = "adopt" | "discard" | "restore";

/** `POST /admin/keywords/generate`（`schemas/keyword.py::KeywordGenerateBody`） */
export interface KeywordGenerateBody {
  project_id: number;
  seeds: string[];
  count: number;
  competitors?: string[];
  audience?: string | null;
  template_id?: number | null;
  model?: string | null;
}

/** `POST /admin/keywords`（手工新增） */
export interface KeywordCreateBody {
  project_id: number;
  keyword: string;
  intent: KeywordIntent;
  keyword_type: KeywordType;
}

/** `PUT /admin/keywords/{id}`；状态不可在此修改 */
export interface KeywordUpdateBody {
  keyword?: string;
  intent?: KeywordIntent;
  keyword_type?: KeywordType;
  difficulty?: number | null;
  heat?: number | null;
  score?: number | null;
  tags?: string[];
}

/** `POST /admin/keywords/import` 的一项 */
export interface KeywordImportItem {
  keyword: string;
  intent: KeywordIntent | string;
  keyword_type: KeywordType | string;
}

/** `POST /admin/titles/generate`（`schemas/title.py::TitleGenerateBody`） */
export interface TitleGenerateBody {
  project_id: number;
  keyword_ids: number[];
  count: number;
  style: ContentStyle;
  template_id?: number | null;
  model?: string | null;
}

/** `POST /admin/titles`（手工新增，`source=manual`） */
export interface TitleCreateBody {
  keyword_id: number;
  title: string;
  style: ContentStyle;
}

/** `PUT /admin/titles/{id}` */
export interface TitleUpdateBody {
  title: string;
  style?: ContentStyle;
}

/** `POST /admin/contents`（手工创建草稿） */
export interface ContentCreateBody {
  project_id: number;
  title: string;
  title_id?: number | null;
  keyword_id?: number | null;
  format: ContentFormat;
  style: ContentStyle;
  body?: string | null;
}

/** `POST /admin/contents/generate`（`schemas/content.py::ContentGenerateBody`） */
export interface ContentGenerateBody {
  project_id: number;
  title_ids: number[];
  template_id?: number | null;
  outline_first: boolean;
  target_word_count: number;
  include_faq: boolean;
  include_seo_meta: boolean;
  format: ContentFormat;
  model?: string | null;
}

/** `PUT /admin/contents/{id}`：人工编辑版本化字段；`current_version_id` 用于并发冲突检查（09 §12） */
export interface ContentUpdateBody {
  title?: string;
  body?: string | null;
  outline?: OutlineItem[] | null;
  summary?: string | null;
  seo_title?: string | null;
  seo_description?: string | null;
  seo_keywords?: string[] | null;
  faq?: FaqItem[] | null;
  current_version_id?: number | null;
}

/** `POST /admin/contents/{id}/generate-outline` */
export interface ContentOutlineBody {
  template_id?: number | null;
  model?: string | null;
}

/** `POST /admin/contents/{id}/generate-body` */
export interface ContentBodyBody {
  segmented?: boolean;
  template_id?: number | null;
  model?: string | null;
}

/** `POST /admin/contents/{id}/generate-seo` */
export interface ContentSeoBody {
  include_faq?: boolean;
  template_id?: number | null;
  model?: string | null;
}

/** `POST /admin/contents/{id}/rewrite`（`schemas/content.py::RewriteBody`） */
export interface ContentRewriteBody {
  mode: RewriteMode;
  scope: RewriteScope;
  /** `scope=section` 必填，1 起，对应 `outline` 顺序 */
  section_index?: number | null;
  /** `restyle` 必填 */
  style?: ContentStyle | null;
  instruction?: string | null;
  template_id?: number | null;
  model?: string | null;
}

/** `POST /admin/contents/{id}/assets/{asset_id}/attach` */
export interface ContentAttachBody {
  usage_type: "cover" | "inline";
  sort?: number;
}

/** `GET /admin/contents/{id}/export?format=` */
export type ContentExportFormat = "md" | "html" | "json";

// =====================================================================
// 媒体生成请求体（docs/04 §6.13、§7.8、§7.9；docs/10 §4.1、§5.1）
// =====================================================================

/** `POST /admin/media/images/generate`（`schemas/media.py::ImageGenerateBody`） */
export interface ImageGenerateBody {
  project_id: number;
  /** `usage_type ∈ cover/inline` 时必填；`standalone` 时必须为空 */
  content_id?: number | null;
  usage_type: "cover" | "inline" | "standalone";
  /** ≤ 4000 字符；`from_content_prompt=false` 时必填 */
  prompt?: string | null;
  from_content_prompt?: boolean;
  /** 1~`media_config.image.max_count_per_request` */
  count?: number;
  resolution?: ImageResolution;
  aspect_ratio?: AspectRatio;
  /** ≤ `media_config.image.max_reference_images`；真实模式须公网（否则 4222） */
  reference_image_urls?: string[];
  model?: string | null;
}

/** `POST /admin/media/videos/generate`（`schemas/media.py::VideoGenerateBody`） */
export interface VideoGenerateBody {
  project_id: number;
  /** 仅 `usage_type=inline` 时必填；`standalone` 时必须为空 */
  content_id?: number | null;
  usage_type: "inline" | "standalone";
  prompt: string;
  negative_prompt?: string | null;
  /** 1~`media_config.video.max_duration` 秒 */
  duration?: number;
  resolution?: VideoResolution;
  /** `{w}:{h}`，与 `size` 二选一 */
  aspect_ratio?: string | null;
  /** `{w}x{h}`，与 `aspect_ratio` 二选一 */
  size?: string | null;
  /** 图生视频；不得与 `first_frame_image_url` 同时传 */
  input_reference?: string | null;
  reference_image_urls?: string[];
  reference_video_urls?: string[];
  reference_audio_urls?: string[];
  first_frame_image_url?: string | null;
  /** 须与 `first_frame_image_url` 同时出现 */
  last_frame_image_url?: string | null;
  generate_audio?: boolean;
  model?: string | null;
}

/** 4222 参考素材 URL 非公网的 `data` */
export interface PublicUrlRequiredData {
  urls: string[];
}
