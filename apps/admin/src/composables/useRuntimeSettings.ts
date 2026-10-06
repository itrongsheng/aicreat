// `GET /admin/settings/runtime`（已登录即可读）的模块级缓存：生成抽屉的默认值与上限（docs/09 §4.4、§10.3）。
// 缓存 60s；失败时返回内置缺省（与后端 generation_config 默认值一致）。
import { ref } from "vue";
import type { RuntimeSettings } from "@aicreat/shared";
import * as settingsApi from "@/api/settings";

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
