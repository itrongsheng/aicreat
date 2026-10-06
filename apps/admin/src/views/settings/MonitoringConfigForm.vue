<script setup lang="ts">
// 「系统配置 → 监控」Tab 的专用表单（docs/11 §13.1、§11.5）：link_check.*（删除检测）与 index_check.*（SEO / GEO 收录检测）。
// 取值范围与后端 app/schemas/settings.py 的 MonitoringConfig 一致；前端即时校验跨字段规则（退避数组长度 3 且递增、schedule_days
// 严格递增、indexed_recheck_days > monthly_interval_days、deleted_recheck_until_days ≥ deleted_recheck_days、query_by 非空），
// 保存仍由后端校验，400 错误按 loc 回显到对应字段。global_concurrency / index_check.concurrency 修改后需重启 monitor_worker。
import { computed, reactive, watch } from "vue";
import { useI18n } from "vue-i18n";
import { GEO_ENGINE, SEO_ENGINE, type ValidationErrorItem } from "@aicreat/shared";
import { clone, errorAt, fillDefaults, getAt, pathKey, relLoc, setAt, toSortedInts, type Json, type Path } from "@/utils/settingsForm";

type FieldKind = "int" | "bool" | "text" | "backoff" | "intTags" | "multi";

interface FieldDef {
  path: [string, string];
  kind: FieldKind;
  min?: number;
  max?: number;
  step?: number;
  /** 单位（i18n 键 monitoringSettings.units.*） */
  unit?: "days" | "hours" | "seconds" | "bytes" | "count" | "times";
  /** multi：可选项 */
  options?: () => { value: string; label: string }[];
  allowCreate?: boolean;
  /** 修改后需重启 monitor_worker */
  restart?: boolean;
}

const props = withDefaults(
  defineProps<{
    modelValue: Json;
    readonly?: boolean;
    errors?: ValidationErrorItem[];
    /** geo_engines.engines[].code（GEO 列表须与之一致，用作 index_check.geo_engines 的候选项） */
    geoEngineCodes?: string[];
  }>(),
  { readonly: false, errors: () => [], geoEngineCodes: () => [] },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: Json): void;
  (e: "validity", valid: boolean): void;
}>();

const { t, te } = useI18n();

/** docs/11 §13.1 默认值（后端返回值已与默认值深合并，正常不会缺失） */
const DEFAULTS: Json = {
  version: 1,
  link_check: {
    enabled: true,
    initial_days: 7,
    initial_interval_hours: 24,
    regular_interval_days: 7,
    abnormal_backoff_hours: [6, 12, 24],
    unknown_confirm_count: 3,
    suspected_confirm_count: 2,
    deleted_recheck_days: 7,
    deleted_recheck_until_days: 30,
    changed_simhash_distance: 20,
    title_compare: true,
    timeout_seconds: 15,
    max_response_bytes: 2097152,
    max_redirects: 3,
    respect_robots: false,
    allow_http: true,
    user_agent: "aicreatLinkMonitor/1.0 (+https://example.com/contact)",
    global_concurrency: 4,
    per_domain_interval_seconds: 2,
    daily_limit: 5000,
    scan_interval_seconds: 60,
  },
  index_check: {
    enabled: true,
    schedule_days: [1, 3, 7, 14, 30],
    monthly_interval_days: 30,
    indexed_recheck_days: 90,
    seo_engines: ["baidu", "bing", "google"],
    geo_engines: ["baidu_ai", "doubao", "kimi", "deepseek", "perplexity", "chatgpt"],
    query_by: ["url", "title"],
    overdue_days: 30,
    max_checks_per_link_per_engine: 24,
    daily_limit: 2000,
    scan_interval_seconds: 300,
    concurrency: 2,
  },
};

const MAX_RESPONSE_BYTES = 10 * 1024 * 1024;

const seoOptions = () => SEO_ENGINE.map((c) => ({ value: c, label: `${t(`status.seo_engine.${c}`)}（${c}）` }));
const geoOptions = () => {
  const codes = [...new Set([...props.geoEngineCodes, ...GEO_ENGINE])];
  return codes.map((c) => ({ value: c, label: te(`status.geo_engine.${c}`) ? `${t(`status.geo_engine.${c}`)}（${c}）` : c }));
};
const queryByOptions = () => (["url", "title"] as const).map((c) => ({ value: c, label: t(`monitoringSettings.queryBy.${c}`) }));

const L = (field: string, kind: FieldKind, extra: Partial<FieldDef> = {}): FieldDef => ({ path: ["link_check", field], kind, ...extra });
const I = (field: string, kind: FieldKind, extra: Partial<FieldDef> = {}): FieldDef => ({ path: ["index_check", field], kind, ...extra });

const SECTIONS: { key: "link_check" | "index_check"; groups: { key: string; fields: FieldDef[] }[] }[] = [
  {
    key: "link_check",
    groups: [
      {
        key: "schedule",
        fields: [
          L("enabled", "bool"),
          L("initial_days", "int", { min: 0, max: 365, unit: "days" }),
          L("initial_interval_hours", "int", { min: 1, max: 720, unit: "hours" }),
          L("regular_interval_days", "int", { min: 1, max: 365, unit: "days" }),
          L("abnormal_backoff_hours", "backoff", { min: 1, max: 720, unit: "hours" }),
          L("deleted_recheck_days", "int", { min: 1, max: 365, unit: "days" }),
          L("deleted_recheck_until_days", "int", { min: 1, max: 3650, unit: "days" }),
        ],
      },
      {
        key: "judge",
        fields: [
          L("unknown_confirm_count", "int", { min: 1, max: 10, unit: "times" }),
          L("suspected_confirm_count", "int", { min: 1, max: 10, unit: "times" }),
          L("changed_simhash_distance", "int", { min: 1, max: 63 }),
          L("title_compare", "bool"),
        ],
      },
      {
        key: "fetch",
        fields: [
          L("timeout_seconds", "int", { min: 1, max: 120, unit: "seconds" }),
          L("max_response_bytes", "int", { min: 1024, max: MAX_RESPONSE_BYTES, step: 1024 * 256, unit: "bytes" }),
          L("max_redirects", "int", { min: 0, max: 5 }),
          L("respect_robots", "bool"),
          L("allow_http", "bool"),
          L("user_agent", "text"),
        ],
      },
      {
        key: "capacity",
        fields: [
          L("global_concurrency", "int", { min: 1, max: 16, restart: true }),
          L("per_domain_interval_seconds", "int", { min: 0, max: 3600, unit: "seconds" }),
          L("daily_limit", "int", { min: 0, step: 100, unit: "count" }),
          L("scan_interval_seconds", "int", { min: 1, max: 86400, step: 10, unit: "seconds" }),
        ],
      },
    ],
  },
  {
    key: "index_check",
    groups: [
      {
        key: "schedule",
        fields: [
          I("enabled", "bool"),
          I("schedule_days", "intTags", { min: 1, max: 3650, unit: "days" }),
          I("monthly_interval_days", "int", { min: 1, max: 3650, unit: "days" }),
          I("indexed_recheck_days", "int", { min: 1, max: 3650, unit: "days" }),
          I("max_checks_per_link_per_engine", "int", { min: 1, max: 1000, unit: "times" }),
          I("overdue_days", "int", { min: 1, max: 3650, unit: "days" }),
        ],
      },
      {
        key: "engines",
        fields: [
          I("seo_engines", "multi", { options: seoOptions }),
          I("geo_engines", "multi", { options: geoOptions, allowCreate: true }),
          I("query_by", "multi", { options: queryByOptions }),
        ],
      },
      {
        key: "capacity",
        fields: [
          I("daily_limit", "int", { min: 0, step: 100, unit: "count" }),
          I("scan_interval_seconds", "int", { min: 1, max: 86400, step: 10, unit: "seconds" }),
          I("concurrency", "int", { min: 1, max: 8, restart: true }),
        ],
      },
    ],
  },
];

const ALL_FIELDS = SECTIONS.flatMap((s) => s.groups.flatMap((g) => g.fields));

const local = reactive<Json>(fillDefaults(props.modelValue, DEFAULTS));

watch(
  () => props.modelValue,
  (value) => {
    const next = fillDefaults(value, DEFAULTS);
    if (JSON.stringify(next) !== JSON.stringify(local)) {
      for (const k of Object.keys(local)) delete local[k];
      Object.assign(local, next);
    }
  },
  { deep: true },
);

watch(
  local,
  (value) => {
    const next = clone(value as Json);
    if (JSON.stringify(next) !== JSON.stringify(props.modelValue)) emit("update:modelValue", next);
  },
  { deep: true },
);

function get(path: Path): unknown {
  return getAt(local, path);
}

function set(path: Path, value: unknown) {
  setAt(local, path, value);
}

function num(path: Path): number | undefined {
  const v = get(path);
  return typeof v === "number" ? v : v === null || v === undefined || v === "" ? undefined : Number(v);
}

function list(path: Path): unknown[] {
  const v = get(path);
  return Array.isArray(v) ? v : [];
}

function setBackoff(path: Path, idx: number, value: number | null | undefined) {
  const arr = [...list(path)];
  while (arr.length < 3) arr.push(null);
  arr[idx] = value ?? null;
  set(path, arr);
}

function setMulti(field: FieldDef, values: unknown[]) {
  const cleaned = [...new Set(values.map((v) => String(v).trim()).filter(Boolean))];
  set(field.path, field.allowCreate ? cleaned.map((v) => v.toLowerCase()) : cleaned);
}

function multiOptions(field: FieldDef): { value: string; label: string }[] {
  const base = field.options?.() ?? [];
  const known = new Set(base.map((o) => o.value));
  const extra = list(field.path)
    .map(String)
    .filter((v) => !known.has(v))
    .map((v) => ({ value: v, label: v }));
  return [...base, ...extra];
}

function bytesHint(path: Path): string {
  const n = num(path);
  if (!n) return "";
  return `≈ ${(n / 1024 / 1024).toFixed(2)} MB`;
}

// ---------- 前端即时校验（与后端规则一致） ----------
const ENGINE_CODE_RE = /^[a-z][a-z0-9_]{0,31}$/;

const localErrors = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  const lc = (local.link_check ?? {}) as Json;
  const ic = (local.index_check ?? {}) as Json;
  for (const field of ALL_FIELDS) {
    const v = get(field.path);
    const key = pathKey(field.path);
    if (field.kind === "int") {
      if (typeof v !== "number" || !Number.isInteger(v)) out[key] = t("monitoringSettings.errors.required");
      else if ((field.min !== undefined && v < field.min) || (field.max !== undefined && v > field.max)) {
        out[key] = t("monitoringSettings.errors.range", { min: field.min ?? 0, max: field.max ?? "∞" });
      }
    }
  }
  const backoff = Array.isArray(lc.abnormal_backoff_hours) ? (lc.abnormal_backoff_hours as unknown[]) : [];
  if (backoff.length !== 3 || backoff.some((b) => typeof b !== "number" || b < 1 || b > 720)) {
    out["link_check.abnormal_backoff_hours"] = t("monitoringSettings.errors.backoffLength");
  } else if (!(Number(backoff[0]) < Number(backoff[1]) && Number(backoff[1]) < Number(backoff[2]))) {
    out["link_check.abnormal_backoff_hours"] = t("monitoringSettings.errors.backoffIncreasing");
  }
  if (typeof lc.deleted_recheck_until_days === "number" && typeof lc.deleted_recheck_days === "number" && lc.deleted_recheck_until_days < lc.deleted_recheck_days) {
    out["link_check.deleted_recheck_until_days"] = t("monitoringSettings.errors.untilDays");
  }
  const ua = typeof lc.user_agent === "string" ? lc.user_agent.trim() : "";
  if (!ua) out["link_check.user_agent"] = t("monitoringSettings.errors.required");
  else if (ua.length > 255 || /[\x00-\x1f\x7f]/.test(ua)) out["link_check.user_agent"] = t("monitoringSettings.errors.userAgent");
  const schedule = Array.isArray(ic.schedule_days) ? (ic.schedule_days as unknown[]) : [];
  if (!schedule.length) out["index_check.schedule_days"] = t("monitoringSettings.errors.scheduleEmpty");
  if (typeof ic.indexed_recheck_days === "number" && typeof ic.monthly_interval_days === "number" && ic.indexed_recheck_days <= ic.monthly_interval_days) {
    out["index_check.indexed_recheck_days"] = t("monitoringSettings.errors.recheckDays");
  }
  if (!Array.isArray(ic.query_by) || !ic.query_by.length) out["index_check.query_by"] = t("monitoringSettings.errors.queryBy");
  const geo = Array.isArray(ic.geo_engines) ? (ic.geo_engines as unknown[]).map(String) : [];
  const badGeo = geo.filter((c) => !ENGINE_CODE_RE.test(c));
  if (badGeo.length) out["index_check.geo_engines"] = t("monitoringSettings.errors.engineCode", { codes: badGeo.join(", ") });
  return out;
});

watch(
  () => Object.keys(localErrors.value).length === 0,
  (valid) => emit("validity", valid),
  { immediate: true },
);

/** GEO 列表与 geo_engines 配置不一致（只提示，不阻止保存） */
const geoMismatch = computed(() => {
  if (!props.geoEngineCodes.length) return null;
  const listed = list(["index_check", "geo_engines"]).map(String);
  const missing = props.geoEngineCodes.filter((c) => !listed.includes(c));
  const extra = listed.filter((c) => !props.geoEngineCodes.includes(c));
  return missing.length || extra.length ? { missing, extra } : null;
});

function fieldError(field: FieldDef): string | undefined {
  return errorAt(props.errors, field.path) ?? localErrors.value[pathKey(field.path)];
}

/** 归不到具体字段的后端错误（如节级模型校验 loc 为 ["body","value","index_check"]） */
const otherErrors = computed(() => props.errors.filter((item) => !ALL_FIELDS.some((f) => relLoc(item).slice(0, 2).join(".") === pathKey(f.path))));

function label(field: FieldDef): string {
  return t(`monitoringSettings.fields.${field.path[0]}.${field.path[1]}`);
}

function help(field: FieldDef): string {
  const key = `monitoringSettings.fields.${field.path[0]}.${field.path[1]}_help`;
  return te(key) ? t(key) : "";
}
</script>

<template>
  <div class="monitoring-form">
    <el-alert v-if="otherErrors.length" type="error" :closable="false" :title="t('settings.errorsTitle')" class="form-errors">
      <ul>
        <li v-for="(e, idx) in otherErrors" :key="idx">
          <code>{{ relLoc(e).join(".") || "-" }}</code> {{ e.msg }}
        </li>
      </ul>
    </el-alert>

    <el-form label-width="220px" label-position="right" :disabled="readonly" class="settings-form" @submit.prevent>
      <template v-for="section in SECTIONS" :key="section.key">
        <h3 class="section-title">{{ t(`monitoringSettings.sections.${section.key}`) }}</h3>
        <template v-for="group in section.groups" :key="`${section.key}.${group.key}`">
          <el-divider content-position="left">{{ t(`monitoringSettings.groups.${group.key}`) }}</el-divider>
          <el-form-item v-for="field in group.fields" :key="pathKey(field.path)" :label="label(field)" :error="fieldError(field)">
            <div class="field">
              <div class="field-control">
                <el-input-number
                  v-if="field.kind === 'int'"
                  :model-value="num(field.path)"
                  :min="field.min"
                  :max="field.max"
                  :step="field.step ?? 1"
                  :precision="0"
                  controls-position="right"
                  class="num-input"
                  @update:model-value="(v: number | undefined | null) => set(field.path, v ?? null)"
                />
                <el-switch v-else-if="field.kind === 'bool'" :model-value="!!get(field.path)" @update:model-value="(v: string | number | boolean) => set(field.path, !!v)" />
                <el-input
                  v-else-if="field.kind === 'text'"
                  :model-value="String(get(field.path) ?? '')"
                  maxlength="255"
                  class="text-input"
                  @update:model-value="(v: string) => set(field.path, v)"
                />
                <div v-else-if="field.kind === 'backoff'" class="backoff">
                  <template v-for="idx in [0, 1, 2]" :key="idx">
                    <el-input-number
                      :model-value="(list(field.path)[idx] as number | undefined) ?? undefined"
                      :min="field.min"
                      :max="field.max"
                      :precision="0"
                      controls-position="right"
                      class="backoff-input"
                      @update:model-value="(v: number | undefined | null) => setBackoff(field.path, idx, v)"
                    />
                    <span v-if="idx < 2" class="arrow">→</span>
                  </template>
                </div>
                <el-select
                  v-else-if="field.kind === 'intTags'"
                  :model-value="list(field.path)"
                  multiple
                  filterable
                  allow-create
                  default-first-option
                  :reserve-keyword="false"
                  :placeholder="t('monitoringSettings.intTagsPlaceholder')"
                  class="wide-input"
                  @update:model-value="(v: unknown[]) => set(field.path, toSortedInts(v, field.min ?? 1, field.max ?? 3650))"
                >
                  <el-option v-for="n in list(field.path)" :key="String(n)" :label="String(n)" :value="n" />
                </el-select>
                <el-select
                  v-else-if="field.kind === 'multi'"
                  :model-value="list(field.path)"
                  multiple
                  filterable
                  :allow-create="field.allowCreate"
                  default-first-option
                  :reserve-keyword="false"
                  class="wide-input"
                  @update:model-value="(v: unknown[]) => setMulti(field, v)"
                >
                  <el-option v-for="opt in multiOptions(field)" :key="opt.value" :label="opt.label" :value="opt.value" />
                </el-select>
                <span v-if="field.unit" class="unit">{{ t(`monitoringSettings.units.${field.unit}`) }}</span>
                <span v-if="field.unit === 'bytes'" class="unit">{{ bytesHint(field.path) }}</span>
                <el-tag v-if="field.restart" size="small" type="warning" effect="plain" disable-transitions>{{ t("monitoringSettings.restartRequired") }}</el-tag>
              </div>
              <div v-if="help(field)" class="field-help">{{ help(field) }}</div>
              <div v-if="pathKey(field.path) === 'index_check.geo_engines' && geoMismatch" class="field-warn">
                <template v-if="geoMismatch.missing.length">{{ t("monitoringSettings.geoMissing", { codes: geoMismatch.missing.join(", ") }) }}</template>
                <template v-if="geoMismatch.extra.length"> {{ t("monitoringSettings.geoExtra", { codes: geoMismatch.extra.join(", ") }) }}</template>
              </div>
            </div>
          </el-form-item>
        </template>
      </template>
    </el-form>
  </div>
</template>

<style scoped>
.monitoring-form :deep(.el-divider__text) {
  font-weight: 600;
}
.section-title {
  margin: 16px 0 4px;
  font-size: 15px;
  font-weight: 600;
}
.section-title:first-of-type {
  margin-top: 0;
}
.field {
  width: 100%;
  min-width: 0;
}
.field-control {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
}
.num-input {
  width: 200px;
  max-width: 100%;
}
.text-input {
  width: 420px;
  max-width: 100%;
}
.wide-input {
  width: 420px;
  max-width: 100%;
}
.backoff {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.backoff-input {
  width: 110px;
}
.arrow,
.unit {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.field-help {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.field-warn {
  margin-top: 2px;
  color: var(--el-color-warning);
  font-size: 12px;
  line-height: 1.5;
}
.form-errors {
  margin-bottom: 12px;
}
.form-errors ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
@media (max-width: 768px) {
  .settings-form :deep(.el-form-item) {
    flex-direction: column;
    align-items: stretch;
  }
  .settings-form :deep(.el-form-item__label) {
    justify-content: flex-start;
    width: auto !important;
    height: auto;
    line-height: 1.5;
    margin-bottom: 4px;
  }
}
</style>
