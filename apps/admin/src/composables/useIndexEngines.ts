// SEO / GEO 引擎目录（docs/11 §7.6、§8.1、§11.2、§11.3）：读 `GET /admin/settings/runtime`（已登录即可读）的
// `seo_providers.engines`（引擎 → 提供器 / 启用）、`geo_engines.engines[]`（code / name / 启用）与 `monitoring_config.index_check`。
// 模块级缓存 60s；接口不可用时回落到 shared 枚举（全部视为未启用）。
import { computed, ref } from "vue";
import { GEO_ENGINE, LINK_LIMITS, SEO_ENGINE, type IndexKind, type RuntimeSettings, type SeoProvider } from "@aicreat/shared";
import * as settingsApi from "@/api/settings";
import { t, te } from "@/i18n";

export interface IndexEngineInfo {
  kind: IndexKind;
  code: string;
  /** 配置中的显示名（GEO 引擎可自定义） */
  name: string | null;
  enabled: boolean;
  /** SEO：配置的提供器；GEO：固定 `zhiqi_model` */
  provider: SeoProvider | "zhiqi_model";
}

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

/** 清空运行时子集缓存（系统配置页保存 geo_engines / seo_providers / monitoring_config 后调用） */
export function invalidateIndexEngines(): void {
  cached = null;
}

function buildEngines(rt: RuntimeSettings | null): IndexEngineInfo[] {
  const out: IndexEngineInfo[] = [];
  const seoCfg = (rt?.seo_providers?.engines ?? {}) as Record<string, { provider?: SeoProvider; enabled?: boolean } | undefined>;
  const seoCodes = [...SEO_ENGINE, ...Object.keys(seoCfg).filter((c) => !(SEO_ENGINE as readonly string[]).includes(c))];
  for (const code of seoCodes) {
    const cfg = seoCfg[code];
    out.push({ kind: "seo", code, name: null, enabled: !!cfg?.enabled, provider: cfg?.provider ?? "zhiqi_web_search" });
  }
  const geoList = Array.isArray(rt?.geo_engines?.engines) ? rt!.geo_engines.engines : [];
  const seen = new Set<string>();
  for (const e of geoList) {
    if (!e || typeof e.code !== "string" || seen.has(e.code)) continue;
    seen.add(e.code);
    out.push({ kind: "geo", code: e.code, name: typeof e.name === "string" && e.name ? e.name : null, enabled: !!e.enabled, provider: "zhiqi_model" });
  }
  for (const code of GEO_ENGINE) {
    if (!seen.has(code)) out.push({ kind: "geo", code, name: null, enabled: false, provider: "zhiqi_model" });
  }
  return out;
}

/** 引擎显示名：内置引擎走 i18n（status.seo_engine / status.geo_engine），自定义 GEO 引擎取配置名 */
export function engineLabel(kind: IndexKind, code: string, configName?: string | null): string {
  const key = `status.${kind === "seo" ? "seo_engine" : "geo_engine"}.${code}`;
  if (te(key)) return t(key);
  return configName || code;
}

export function useIndexEngines() {
  const runtime = ref<RuntimeSettings | null>(null);
  const loaded = ref(false);

  const engines = computed(() => buildEngines(runtime.value));
  const seoEngines = computed(() => engines.value.filter((e) => e.kind === "seo"));
  const geoEngines = computed(() => engines.value.filter((e) => e.kind === "geo"));
  const enabledEngines = computed(() => engines.value.filter((e) => e.enabled));
  /** `index_check.overdue_days`（运行时子集未暴露时取默认 30） */
  const overdueDays = computed(() => {
    const ic = (runtime.value?.monitoring_config?.index_check ?? {}) as Record<string, unknown>;
    const n = Number(ic.overdue_days);
    return Number.isFinite(n) && n > 0 ? n : LINK_LIMITS.overdueDays;
  });

  function find(kind: IndexKind, code: string): IndexEngineInfo | undefined {
    return engines.value.find((e) => e.kind === kind && e.code === code);
  }

  function label(kind: IndexKind, code: string): string {
    return engineLabel(kind, code, find(kind, code)?.name);
  }

  async function load(force = false): Promise<void> {
    try {
      runtime.value = await fetchRuntime(force);
    } catch {
      runtime.value = null;
    }
    loaded.value = true;
  }

  return { engines, seoEngines, geoEngines, enabledEngines, overdueDays, loaded, load, find, label };
}
