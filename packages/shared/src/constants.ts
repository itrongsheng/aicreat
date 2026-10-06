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
