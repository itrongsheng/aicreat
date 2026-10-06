// 前后端共享常量。上传限制为前端预检默认值（与 MAX_IMAGE_SIZE_MB / MAX_VIDEO_SIZE_MB 默认一致），以后端为准。
import type { Modality } from "./enums";

export const API_PREFIX = "/api/v1";
export const DEFAULT_PAGE_SIZE = 20;
export const MAX_PAGE_SIZE = 100;

export const SUPPORTED_LOCALES = ["zh-CN", "en-US"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];
/** `settings.locale` 另有 `*`（语言无关） */
export type SettingLocale = Locale | "*";

export const UPLOAD_LIMITS = { image_mb: 10, video_mb: 200 } as const;

export const IMAGE_RESOLUTIONS = ["1080p", "2k", "4k"] as const;
export type ImageResolution = (typeof IMAGE_RESOLUTIONS)[number];

export const ASPECT_RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16"] as const;
export type AspectRatio = (typeof ASPECT_RATIOS)[number];

export const VIDEO_RESOLUTIONS = ["480p", "720p", "1080p", "4k"] as const;
export type VideoResolution = (typeof VIDEO_RESOLUTIONS)[number];

export const CAPABILITIES = ["keyword", "title", "content", "rewrite", "image", "video", "geo_check", "seo_check"] as const;
export type Capability = (typeof CAPABILITIES)[number];

/** 文本类能力（与后端 `app.core.zhiqi.types.TEXT_CAPABILITIES` 一致，docs/08 §5.2） */
export const TEXT_CAPABILITIES = ["keyword", "title", "content", "rewrite", "geo_check", "seo_check"] as const satisfies readonly Capability[];

/** 能力 → 模态（`ai_models.modalities_json`；`ModelSelect.vue` 按此过滤，docs/08 §5.2 `MODALITY_OF`） */
export const MODALITY_OF: Record<Capability, Modality> = {
  keyword: "text",
  title: "text",
  content: "text",
  rewrite: "text",
  geo_check: "text",
  seo_check: "text",
  image: "image",
  video: "video",
};

export const BUSINESS_CODES = {
  BAD_REQUEST: 400, UNAUTHORIZED: 401, FORBIDDEN: 403, NOT_FOUND: 404, CONFLICT: 409, RATE_LIMITED: 429,
  TEMPLATE_VARIABLE_MISSING: 4221, PUBLIC_URL_REQUIRED: 4222, QUOTA_LIMIT_REACHED: 4291,
  UPSTREAM_ERROR: 5021, CAPABILITY_UNAVAILABLE: 5031,
} as const;
export type BusinessCode = (typeof BUSINESS_CODES)[keyof typeof BUSINESS_CODES];

// ---------- Prompt 模板（docs/09 §5） ----------
import type { PromptKind } from "./enums";

/** 系统模板 code 前缀；自定义 code 不得以此开头（docs/09 §5.1） */
export const SYSTEM_TEMPLATE_PREFIX = "sys_";
/** 自定义模板 code 规则 `^[a-z][a-z0-9_]{2,79}$`（docs/09 §5.1） */
export const PROMPT_CODE_PATTERN = /^[a-z][a-z0-9_]{2,79}$/;

/** `prompt_kind → capability` 固定推导（docs/09 §5.1、docs/03 B.8） */
export const PROMPT_KIND_CAPABILITY: Record<PromptKind, Capability> = {
  keyword: "keyword",
  title: "title",
  outline: "content",
  content: "content",
  section: "content",
  seo_meta: "content",
  faq: "content",
  image_prompt: "content",
  rewrite: "rewrite",
  expand: "rewrite",
  shorten: "rewrite",
  restyle: "rewrite",
  geo_query: "geo_check",
  seo_query: "seo_check",
};

/** 各 kind 的系统默认模板 code（docs/09 §5.2、§5.7） */
export const PROMPT_DEFAULT_CODES: Record<PromptKind, string> = {
  keyword: "sys_keyword",
  title: "sys_title",
  outline: "sys_outline",
  content: "sys_content",
  section: "sys_section",
  rewrite: "sys_rewrite",
  expand: "sys_expand",
  shorten: "sys_shorten",
  restyle: "sys_restyle",
  seo_meta: "sys_seo_meta",
  faq: "sys_faq",
  image_prompt: "sys_image_prompt",
  geo_query: "sys_geo_query",
  seo_query: "sys_seo_query",
};

const PROMPT_COMMON_VARIABLES = ["language", "industry", "audience", "brand_info"] as const;
const PROMPT_LENGTH_VARIABLES = ["target_word_count", "section_word_count", "max_sections", "faq_count", "format"] as const;
const PROMPT_REWRITE_VARIABLES = [
  ...PROMPT_COMMON_VARIABLES,
  "keyword",
  "intent",
  "title",
  "style",
  "text",
  "instruction",
  ...PROMPT_LENGTH_VARIABLES,
] as const;

/**
 * 各 kind 可直接引用的内置变量（docs/09 §5.3 变量表 ∪ §5.7 系统模板变量），由 service 在执行时计算；
 * 模板编辑器以只读 chip 列出、点击插入 `{{name}}`，预览表单以此为可覆盖的示例变量。
 */
export const PROMPT_BUILTIN_VARIABLES: Record<PromptKind, readonly string[]> = {
  keyword: [...PROMPT_COMMON_VARIABLES, "seeds", "competitors", "count"],
  title: [...PROMPT_COMMON_VARIABLES, "count", "keyword", "intent", "style"],
  outline: [...PROMPT_COMMON_VARIABLES, "keyword", "intent", "style", "title", ...PROMPT_LENGTH_VARIABLES],
  content: [...PROMPT_COMMON_VARIABLES, "keyword", "intent", "style", "title", "outline", ...PROMPT_LENGTH_VARIABLES],
  section: [
    ...PROMPT_COMMON_VARIABLES,
    "keyword",
    "intent",
    "style",
    "title",
    "outline",
    "section",
    "previous_text",
    ...PROMPT_LENGTH_VARIABLES,
  ],
  rewrite: PROMPT_REWRITE_VARIABLES,
  expand: PROMPT_REWRITE_VARIABLES,
  shorten: PROMPT_REWRITE_VARIABLES,
  restyle: PROMPT_REWRITE_VARIABLES,
  seo_meta: [...PROMPT_COMMON_VARIABLES, "keyword", "intent", "title", "body"],
  faq: [...PROMPT_COMMON_VARIABLES, "keyword", "intent", "title", "body", ...PROMPT_LENGTH_VARIABLES],
  image_prompt: [...PROMPT_COMMON_VARIABLES, "title", "summary", "style", "usage_type"],
  geo_query: [...PROMPT_COMMON_VARIABLES, "keyword", "title", "url", "domain", "engine_name"],
  seo_query: [...PROMPT_COMMON_VARIABLES, "title", "url", "domain", "engine_name"],
};

/** 模板变量占位 `{{name}}`（允许 `{{ name }}`，docs/09 §5.3） */
export const PROMPT_VARIABLE_PATTERN = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;

// ---------- 关键词 / 标题 / 内容 / 批次（docs/09 §6~§10） ----------
import type { BatchStatus, ContentStatus } from "./enums";

/** 批次终态（docs/09 §9.3）：轮询到这些状态后停止 */
export const BATCH_TERMINAL_STATUSES: readonly BatchStatus[] = ["succeeded", "partial", "failed", "cancelled"];

/** `POST …/batch-status` 的 `ids` 上限（docs/04 §9） */
export const BATCH_IDS_MAX = 500;
/** `POST /admin/keywords/import` 的 `items` 与 CSV 数据行上限（docs/09 §6.7） */
export const KEYWORD_IMPORT_MAX = 5000;
/** `POST /admin/keywords/import-file` 文件大小上限（字节） */
export const KEYWORD_IMPORT_FILE_MAX_BYTES = 2 * 1024 * 1024;
/** 关键词生成：种子词 1~20 个、每个 ≤ 60 字符；竞品 ≤ 10 个、每个 ≤ 60 字符；受众 ≤ 255（docs/09 §6.1） */
export const KEYWORD_GENERATE_LIMITS = { seeds: 20, seedLength: 60, competitors: 10, competitorLength: 60, audience: 255 } as const;
/** 关键词长度上限（清洗后，docs/09 §6.2、§6.7）与人工标签上限（docs/09 §6.8） */
export const KEYWORD_LIMITS = { keyword: 120, tags: 20, tagLength: 30 } as const;
/** 标题：生成一次最多 50 个关键词，标题 ≤ 200 字符，人工分 0~10 步长 0.5（docs/09 §7.2~§7.4） */
export const TITLE_LIMITS = { keywordIds: 50, title: 200, manualScoreMax: 10, manualScoreStep: 0.5 } as const;
/** 内容：一次最多 20 个标题；重写补充要求 ≤ 500；大纲 1~40 项、标题 ≤ 120、要点 ≤ 8 个且每个 ≤ 200；SEO 字段上限（docs/09 §8.1~§8.7） */
export const CONTENT_LIMITS = {
  titleIds: 20,
  instruction: 500,
  outlineItems: 40,
  outlineHeading: 120,
  outlinePoints: 8,
  outlinePoint: 200,
  summary: 500,
  seoTitle: 200,
  seoDescription: 500,
  seoKeywords: 10,
  seoKeyword: 60,
  faqQuestion: 200,
  faqAnswer: 1000,
  /** SEO 面板提示阈值：seo_title 超过 60 字、seo_description 不在 80~160 字时黄色提示（服务端不校验） */
  seoTitleSuggest: 60,
  seoDescriptionMin: 80,
  seoDescriptionMax: 160,
  /** 质量分低于该值在列表标红 */
  qualityWarn: 60,
} as const;

/** 内容编辑器动作（docs/09 §10.6）；`backfill` = 回填链接（docs/11） */
export const CONTENT_ACTION = [
  "save",
  "generate_outline",
  "generate_body",
  "generate_seo",
  "rewrite",
  "submit_review",
  "approve",
  "reject",
  "archive",
  "unarchive",
  "delete",
  "backfill",
] as const;
export type ContentAction = (typeof CONTENT_ACTION)[number];

/**
 * 状态 → 允许动作（与后端 `content_service.transition` 及各生成入口的起始状态一致，docs/09 §8.1、§8.10、§10.6）。
 * 附加条件由页面判断：`rewrite`/`generate_seo` 需正文非空；`delete` 需 `link_count=0`；`approve`/`reject` 需 `content.contents.review`。
 */
export const CONTENT_ACTIONS: Record<ContentStatus, readonly ContentAction[]> = {
  draft: ["save", "generate_outline", "generate_body", "generate_seo", "rewrite", "archive", "delete"],
  generating: [],
  ready: ["save", "generate_outline", "generate_body", "generate_seo", "rewrite", "archive", "submit_review"],
  reviewing: ["approve", "reject"],
  approved: ["save", "generate_outline", "generate_seo", "rewrite", "archive", "backfill"],
  rejected: ["save", "generate_outline", "generate_body", "generate_seo", "rewrite", "archive"],
  published: ["save", "generate_outline", "generate_seo", "rewrite", "archive", "backfill"],
  archived: ["unarchive", "delete"],
};

/** 质量规则风险标记（docs/09 §8.9） */
export const CONTENT_RISK_FLAG = [
  "too_short",
  "too_long",
  "missing_h2",
  "too_many_h2",
  "banned_word",
  "duplicate_title",
  "missing_seo_meta",
  "missing_faq",
  "truncated",
] as const;
export type ContentRiskFlag = (typeof CONTENT_RISK_FLAG)[number];

/** 阻断提审的风险标记（docs/09 §8.9） */
export const REVIEW_BLOCKING_FLAGS: readonly ContentRiskFlag[] = ["banned_word", "too_short"];

/** 批次 `error_summary` 中的分项计数键（docs/09 §9.3）；其余键为失败根任务的 `error_category` 或 `stale_after_submit` */
export const BATCH_APPLY_COUNT_KEYS = ["duplicates", "invalid", "intent_missing", "empty_output", "too_long"] as const;

// ---------- 回填链接与发布平台（docs/11 §4.1、§5.2、§5.3） ----------

/** 回填校验（与后端 `link_service.backfill` 一致，前端预检用） */
export const LINK_LIMITS = {
  /** `POST /admin/links/batch` 的 `items` 上限 */
  batchMax: 100,
  publishAccount: 100,
  note: 500,
  /** `published_at` 不得晚于当前时间 + 5 分钟 */
  publishedAtFutureMinutes: 5,
  /** `published_at` 不得早于当前时间 − 3650 天 */
  publishedAtPastDays: 3650,
  /** `index_check.overdue_days` 默认值：列表 / 详情「超期未收录」标记 */
  overdueDays: 30,
} as const;

/** 平台规则校验（docs/11 §5.2、§5.3） */
export const PLATFORM_LIMITS = {
  code: 32,
  name: 50,
  nameEn: 80,
  icon: 500,
  homeUrl: 255,
  markerMin: 4,
  markerMax: 100,
  markersMax: 50,
  userAgentMax: 200,
  /** 覆盖 UA 必须包含的子串 */
  userAgentMarker: "aicreat",
  /** `url_patterns` / `redirect_markers` 各 ≤ 50 条、单条 ≤ 500 字符 */
  patternsMax: 50,
  patternMax: 500,
  /** `fetch_config.headers` ≤ 20 个，值 ≤ 500 字符且不含换行 */
  headersMax: 20,
  headerValueMax: 500,
  /** `fetch_config.timeout_seconds` 取值范围（秒） */
  timeoutMin: 1,
  timeoutMax: 60,
} as const;

/** `fetch_config` 可覆盖的键（`max_response_bytes` / `max_redirects` 只能全局调整） */
export const PLATFORM_FETCH_CONFIG_KEYS = ["user_agent", "headers", "timeout_seconds", "respect_robots", "allow_http"] as const;

/** `fetch_config.headers` 禁止的请求头（忽略大小写） */
export const PLATFORM_FORBIDDEN_HEADERS = ["cookie", "authorization", "proxy-authorization"] as const;

/** `fetch_config.headers` 允许的请求头（忽略大小写），另允许 `X-*` */
export const PLATFORM_ALLOWED_HEADERS = ["accept-language", "referer"] as const;
