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
