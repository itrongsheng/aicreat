// `GET /admin/settings/runtime`（已登录即可读）的模块级缓存：生成抽屉的默认值与上限（docs/09 §4.4、§10.3）。
// 缓存 60s；失败时返回内置缺省（与后端 generation_config 默认值一致）。
import { ref } from "vue";
import {
  ASPECT_RATIOS,
  IMAGE_RESOLUTIONS,
  VIDEO_RESOLUTIONS,
  type AspectRatio,
  type ImageResolution,
  type RuntimeSettings,
  type VideoResolution,
  type ZhiqiMode,
} from "@aicreat/shared";
import * as settingsApi from "@/api/settings";
import { useAuthStore } from "@/store/auth";

export type GenerationRuntime = RuntimeSettings["generation_config"];

/** 后端 `generation_config` 默认值（docs/09 §4.4），接口不可用时兜底 */
export const GENERATION_DEFAULTS: GenerationRuntime = {
  review_required: true,
  keyword: { default_count: 20, max_count: 50 },
  title: { default_count: 5, max_count: 10, default_style: "news" },
  content: {
    outline_first: true,
    segmented: true,
    max_sections: 8,
    target_word_count: 1500,
    min_word_count: 300,
    max_word_count: 6000,
    include_faq: true,
    include_seo_meta: true,
    default_format: "markdown",
  },
  rewrite: { modes: ["rewrite", "expand", "shorten", "restyle"], max_versions: 50 },
};

const CACHE_TTL_MS = 60_000;
let cached: { at: number; promise: Promise<RuntimeSettings> } | null = null;

function fetchRuntime(force = false): Promise<RuntimeSettings> {
  if (!force && cached && Date.now() - cached.at < CACHE_TTL_MS) return cached.promise;
  const promise = settingsApi.runtime();
  cached = { at: Date.now(), promise };
  promise.catch(() => {
    cached = null;
  });
  return promise;
}

function merge(gen: Partial<GenerationRuntime> | undefined): GenerationRuntime {
  const g = gen ?? {};
  return {
    ...GENERATION_DEFAULTS,
    ...g,
    keyword: { ...GENERATION_DEFAULTS.keyword, ...(g.keyword ?? {}) },
    title: { ...GENERATION_DEFAULTS.title, ...(g.title ?? {}) },
    content: { ...GENERATION_DEFAULTS.content, ...(g.content ?? {}) },
    rewrite: { ...GENERATION_DEFAULTS.rewrite, ...(g.rewrite ?? {}) },
  };
}

export function useRuntimeSettings() {
  const generation = ref<GenerationRuntime>(merge(undefined));
  const loaded = ref(false);

  async function load(force = false): Promise<GenerationRuntime> {
    try {
      const rt = await fetchRuntime(force);
      generation.value = merge(rt.generation_config);
    } catch {
      generation.value = merge(undefined);
    }
    loaded.value = true;
    return generation.value;
  }

  return { generation, loaded, load };
}

// ---------- 媒体（docs/10 §7、§9：media_config 的 image / video / daily_limits 与 zhiqi_mode） ----------

export interface MediaImageRuntime {
  default_resolution: ImageResolution;
  allowed_resolutions: ImageResolution[];
  default_aspect_ratio: AspectRatio;
  allowed_aspect_ratios: AspectRatio[];
  max_reference_images: number;
  max_count_per_request: number;
  poll_budget_seconds: number;
}

export interface MediaVideoRuntime {
  default_resolution: VideoResolution;
  allowed_resolutions: VideoResolution[];
  default_duration: number;
  max_duration: number;
  default_aspect_ratio: string;
  generate_audio_default: boolean;
  poll_budget_seconds: number;
}

export interface MediaRuntime {
  image: MediaImageRuntime;
  video: MediaVideoRuntime;
  daily_limits: { images: number; videos: number };
  zhiqi_mode: ZhiqiMode;
}

/** 后端 `media_config` 默认值（docs/10 §9），接口不可用时兜底 */
export const MEDIA_DEFAULTS: MediaRuntime = {
  image: {
    default_resolution: "1080p",
    allowed_resolutions: [...IMAGE_RESOLUTIONS],
    default_aspect_ratio: "16:9",
    allowed_aspect_ratios: [...ASPECT_RATIOS],
    max_reference_images: 9,
    max_count_per_request: 4,
    poll_budget_seconds: 600,
  },
  video: {
    default_resolution: "720p",
    allowed_resolutions: [...VIDEO_RESOLUTIONS],
    default_duration: 5,
    max_duration: 15,
    default_aspect_ratio: "16:9",
    generate_audio_default: false,
    poll_budget_seconds: 1200,
  },
  daily_limits: { images: 200, videos: 20 },
  zhiqi_mode: "mock",
};

function pickList<T extends string>(value: unknown, allowed: readonly T[], fallback: T[]): T[] {
  if (!Array.isArray(value)) return fallback;
  const out = value.filter((v): v is T => typeof v === "string" && (allowed as readonly string[]).includes(v));
  return out.length ? out : fallback;
}

function pickOne<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

function num(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/** 合并运行时 `media_config`：`allowed_*` 只保留枚举内取值（以 shared 常量为准），缺省回落默认值 */
export function mergeMediaRuntime(rt: Partial<RuntimeSettings> | undefined): MediaRuntime {
  const media = (rt?.media_config ?? {}) as Partial<RuntimeSettings["media_config"]>;
  const img = (media.image ?? {}) as Record<string, unknown>;
  const vid = (media.video ?? {}) as Record<string, unknown>;
  const d = MEDIA_DEFAULTS;
  const imageResolutions = pickList(img.allowed_resolutions, IMAGE_RESOLUTIONS, d.image.allowed_resolutions);
  const imageRatios = pickList(img.allowed_aspect_ratios, ASPECT_RATIOS, d.image.allowed_aspect_ratios);
  const videoResolutions = pickList(vid.allowed_resolutions, VIDEO_RESOLUTIONS, d.video.allowed_resolutions);
  const imageDefaultRes = pickOne(img.default_resolution, IMAGE_RESOLUTIONS, d.image.default_resolution);
  const imageDefaultRatio = pickOne(img.default_aspect_ratio, ASPECT_RATIOS, d.image.default_aspect_ratio);
  const videoDefaultRes = pickOne(vid.default_resolution, VIDEO_RESOLUTIONS, d.video.default_resolution);
  const maxDuration = num(vid.max_duration, d.video.max_duration);
  return {
    image: {
      allowed_resolutions: imageResolutions,
      allowed_aspect_ratios: imageRatios,
      default_resolution: imageResolutions.includes(imageDefaultRes) ? imageDefaultRes : imageResolutions[0],
      default_aspect_ratio: imageRatios.includes(imageDefaultRatio) ? imageDefaultRatio : imageRatios[0],
      max_reference_images: num(img.max_reference_images, d.image.max_reference_images),
      max_count_per_request: num(img.max_count_per_request, d.image.max_count_per_request),
      poll_budget_seconds: num(img.poll_budget_seconds, d.image.poll_budget_seconds),
    },
    video: {
      allowed_resolutions: videoResolutions,
      default_resolution: videoResolutions.includes(videoDefaultRes) ? videoDefaultRes : videoResolutions[0],
      max_duration: maxDuration,
      default_duration: Math.min(num(vid.default_duration, d.video.default_duration), maxDuration),
      default_aspect_ratio:
        typeof vid.default_aspect_ratio === "string" && /^\d+:\d+$/.test(vid.default_aspect_ratio) ? vid.default_aspect_ratio : d.video.default_aspect_ratio,
      generate_audio_default: typeof vid.generate_audio_default === "boolean" ? vid.generate_audio_default : d.video.generate_audio_default,
      poll_budget_seconds: num(vid.poll_budget_seconds, d.video.poll_budget_seconds),
    },
    daily_limits: {
      images: num(media.daily_limits?.images, d.daily_limits.images),
      videos: num(media.daily_limits?.videos, d.daily_limits.videos),
    },
    zhiqi_mode: rt?.zhiqi_mode === "live" ? "live" : rt?.zhiqi_mode === "mock" ? "mock" : useAuthStore().zhiqiMode,
  };
}

/** 媒体工作台的运行时配置（与生成配置共用 60s 缓存） */
export function useMediaRuntime() {
  const media = ref<MediaRuntime>(mergeMediaRuntime(undefined));
  const loaded = ref(false);

  async function load(force = false): Promise<MediaRuntime> {
    try {
      media.value = mergeMediaRuntime(await fetchRuntime(force));
    } catch {
      media.value = mergeMediaRuntime(undefined);
    }
    loaded.value = true;
    return media.value;
  }

  return { media, loaded, load };
}
