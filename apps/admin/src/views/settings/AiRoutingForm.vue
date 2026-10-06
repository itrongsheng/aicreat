<script setup lang="ts">
// 「系统配置 → AI 路由」Tab 的专用表单（docs/08 §4.2、§4.3）：按 ai_routing_config 的节（retry / breaker / fallback / pause_seconds /
// passthrough / timeouts / pricing / health / catalog / usage）逐字段编辑并给出字段说明；取值范围与后端 app/schemas/settings.py
// 的 AiRoutingConfig 一致，保存仍走 PUT /admin/settings/ai_routing_config 由后端校验（错误按 loc 回显到对应字段）。
// 本表单只编辑全局路由参数；各能力的主模型与备选链在「AI 网关 → 能力路由」维护。
import { computed, reactive, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ERROR_CATEGORY, type ValidationErrorItem } from "@aicreat/shared";

type Json = Record<string, unknown>;
type FieldKind = "int" | "float" | "bool" | "categories" | "tags" | "text";

interface FieldDef {
  path: string[];
  kind: FieldKind;
  label: string;
  help?: string;
  min?: number;
  max?: number;
  step?: number;
  precision?: number;
}

interface SectionDef {
  key: string;
  fields: FieldDef[];
}

const props = withDefaults(
  defineProps<{
    modelValue: Json;
    readonly?: boolean;
    /** PUT /admin/settings/ai_routing_config 的 400 校验错误（loc 形如 ["body","value","retry","max_attempts"]） */
    errors?: ValidationErrorItem[];
  }>(),
  { readonly: false, errors: () => [] },
);

const emit = defineEmits<{ (e: "update:modelValue", value: Json): void }>();

const { t } = useI18n();

/** docs/08 §4.2 默认值：缺失的节 / 字段按此补齐（后端返回值已与默认值深合并，正常不会缺失） */
const DEFAULTS: Json = {
  version: 1,
  retry: { max_attempts: 3, base_seconds: 1.0, max_seconds: 30, jitter: true, retry_on: ["upstream_unavailable", "rate_limited", "timeout", "invalid_response"] },
  breaker: { failure_threshold: 5, window_seconds: 300, open_seconds: 120, half_open_max_calls: 1 },
  fallback: {
    enabled: true,
    fallback_on: ["upstream_unavailable", "rate_limited", "timeout", "route_missing", "model_unrouted", "breaker_open", "media_storage", "invalid_response", "unsupported_parameter", "unknown"],
    never_fallback_on: ["quota_exceeded", "auth_failed", "content_blocked", "transfer_failed", "cancelled"],
  },
  pause_seconds: 600,
  passthrough: {
    openai_chat: ["tools", "tool_choice", "web_search_options", "reasoning_effort"],
    openai_responses: ["tools", "tool_choice", "reasoning", "text"],
    anthropic_messages: ["tools", "thinking"],
  },
  timeouts: { text_seconds: 180, submit_seconds: 60, poll_seconds: 30 },
  pricing: { quota_per_unit: 500000, usd_cny_rate: 7.2, group_ratio: 1.0 },
  health: { probe_interval_seconds: 600, probe_text_prompt: "ping", probe_max_tokens: 8, probe_media: false, degraded_latency_ms: 15000 },
  catalog: { sync_interval_seconds: 3600, hide_unavailable_after_days: 7 },
  usage: { reconcile_interval_seconds: 300, min_interval_seconds: 60, max_new_per_pull_warn: 800, unmatched_retention_days: 30 },
};

const F = "aiRouting.fields";
const f = (section: string, field: string, kind: FieldKind, extra: Partial<FieldDef> = {}): FieldDef => ({
  path: [section, field],
  kind,
  label: `${F}.${section}.${field}`,
  help: `${F}.${section}.${field}_help`,
  ...extra,
});

/** 取值范围与后端 AiRoutingConfig 一致 */
const SECTIONS: SectionDef[] = [
  {
    key: "retry",
    fields: [
      f("retry", "max_attempts", "int", { min: 0, max: 10 }),
      f("retry", "base_seconds", "float", { min: 0.01, max: 60, step: 0.5, precision: 2 }),
      f("retry", "max_seconds", "float", { min: 0.01, max: 600, step: 1, precision: 2 }),
      f("retry", "jitter", "bool"),
      f("retry", "retry_on", "categories"),
    ],
  },
  {
    key: "breaker",
    fields: [
      f("breaker", "failure_threshold", "int", { min: 1, max: 1000 }),
      f("breaker", "window_seconds", "int", { min: 10, max: 86400, step: 10 }),
      f("breaker", "open_seconds", "int", { min: 1, max: 86400, step: 10 }),
      f("breaker", "half_open_max_calls", "int", { min: 1, max: 100 }),
    ],
  },
  {
    key: "fallback",
    fields: [f("fallback", "enabled", "bool"), f("fallback", "fallback_on", "categories"), f("fallback", "never_fallback_on", "categories")],
  },
  {
    key: "pause",
    fields: [{ path: ["pause_seconds"], kind: "int", label: `${F}.pause_seconds`, help: `${F}.pause_seconds_help`, min: 1, max: 86400, step: 60 }],
  },
  {
    key: "passthrough",
    fields: [
      { path: ["passthrough", "openai_chat"], kind: "tags", label: `${F}.passthrough.openai_chat` },
      { path: ["passthrough", "openai_responses"], kind: "tags", label: `${F}.passthrough.openai_responses` },
      { path: ["passthrough", "anthropic_messages"], kind: "tags", label: `${F}.passthrough.anthropic_messages`, help: `${F}.passthrough.help` },
    ],
  },
  {
    key: "timeouts",
    fields: [
      f("timeouts", "text_seconds", "int", { min: 1, max: 3600, step: 10 }),
      f("timeouts", "submit_seconds", "int", { min: 1, max: 600, step: 10 }),
      f("timeouts", "poll_seconds", "int", { min: 1, max: 300, step: 5 }),
    ],
  },
  {
    key: "pricing",
    fields: [
      f("pricing", "quota_per_unit", "float", { min: 1, step: 10000, precision: 0 }),
      f("pricing", "usd_cny_rate", "float", { min: 0.0001, max: 1000, step: 0.1, precision: 4 }),
      f("pricing", "group_ratio", "float", { min: 0.0001, max: 1000, step: 0.1, precision: 4 }),
    ],
  },
  {
    key: "health",
    fields: [
      f("health", "probe_interval_seconds", "int", { min: 0, max: 86400, step: 60 }),
      f("health", "probe_text_prompt", "text"),
      f("health", "probe_max_tokens", "int", { min: 1, max: 256 }),
      f("health", "probe_media", "bool"),
      f("health", "degraded_latency_ms", "int", { min: 1, max: 29999, step: 1000 }),
    ],
  },
  {
    key: "catalog",
    fields: [f("catalog", "sync_interval_seconds", "int", { min: 0, max: 604800, step: 600 }), f("catalog", "hide_unavailable_after_days", "int", { min: 1, max: 3650 })],
  },
  {
    key: "usage",
    fields: [
      f("usage", "reconcile_interval_seconds", "int", { min: 0, max: 86400, step: 60 }),
      f("usage", "min_interval_seconds", "int", { min: 1, max: 86400, step: 10 }),
      f("usage", "max_new_per_pull_warn", "int", { min: 1, max: 1000, step: 50 }),
      f("usage", "unmatched_retention_days", "int", { min: 1, max: 3650 }),
    ],
  },
];

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value ?? null)) as T;
}

function isObject(value: unknown): value is Json {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/** 以默认值补齐缺失的节与字段（不覆盖已有值、不删除未知键，未知键交由后端校验） */
function fillDefaults(value: Json): Json {
  const out = clone(isObject(value) ? value : {});
  for (const [k, v] of Object.entries(DEFAULTS)) {
    if (!(k in out)) out[k] = clone(v);
    else if (isObject(v) && isObject(out[k])) {
      const section = out[k] as Json;
      for (const [fk, fv] of Object.entries(v)) if (!(fk in section)) section[fk] = clone(fv);
    }
  }
  return out;
}

const local = reactive<Json>(fillDefaults(props.modelValue));

watch(
  () => props.modelValue,
  (value) => {
    const next = fillDefaults(value);
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

function getAt(path: string[]): unknown {
  let cur: unknown = local;
  for (const key of path) {
    if (!isObject(cur)) return undefined;
    cur = cur[key];
  }
  return cur;
}

function setAt(path: string[], value: unknown) {
  let cur: Json = local;
  for (const key of path.slice(0, -1)) {
    if (!isObject(cur[key])) cur[key] = {};
    cur = cur[key] as Json;
  }
  cur[path[path.length - 1]] = value;
}

function numberValue(path: string[]): number | undefined {
  const v = getAt(path);
  return typeof v === "number" ? v : v === null || v === undefined || v === "" ? undefined : Number(v);
}

function arrayValue(path: string[]): string[] {
  const v = getAt(path);
  return Array.isArray(v) ? (v as string[]) : [];
}

/** 字段级错误：loc 去掉 body / value 前缀后与字段路径前缀匹配 */
function relLoc(item: ValidationErrorItem): string[] {
  const loc = item.loc.map(String);
  let i = 0;
  if (loc[i] === "body") i += 1;
  if (loc[i] === "value") i += 1;
  return loc.slice(i);
}

const fieldErrorMap = computed(() => {
  const out: Record<string, string> = {};
  for (const item of props.errors) {
    const rel = relLoc(item);
    for (const section of SECTIONS) {
      for (const field of section.fields) {
        const key = field.path.join(".");
        if (out[key]) continue;
        if (field.path.every((p, idx) => rel[idx] === p)) out[key] = item.msg;
      }
    }
  }
  return out;
});

/** 归不到具体字段的错误（如节级模型校验 retry.max_seconds < base_seconds 的 loc 为 ["body","value","retry"]） */
const otherErrors = computed(() =>
  props.errors.filter((item) => {
    const rel = relLoc(item);
    return !SECTIONS.some((s) => s.fields.some((fd) => fd.path.every((p, idx) => rel[idx] === p)));
  }),
);

const categoryOptions = computed(() => ERROR_CATEGORY.map((c) => ({ value: c, label: `${t(`status.error_category.${c}`)}（${c}）` })));
</script>

<template>
  <div class="ai-routing-form">
    <el-alert v-if="otherErrors.length" type="error" :closable="false" :title="t('settings.errorsTitle')" class="form-errors">
      <ul>
        <li v-for="(e, idx) in otherErrors" :key="idx">
          <code>{{ relLoc(e).join(".") || "-" }}</code> {{ e.msg }}
        </li>
      </ul>
    </el-alert>

    <el-form label-width="210px" :disabled="readonly" @submit.prevent>
      <template v-for="section in SECTIONS" :key="section.key">
        <el-divider content-position="left">{{ t(`aiRouting.sections.${section.key}`) }}</el-divider>
        <el-form-item v-for="field in section.fields" :key="field.path.join('.')" :label="t(field.label)" :error="fieldErrorMap[field.path.join('.')]">
          <div class="field">
            <el-input-number
              v-if="field.kind === 'int' || field.kind === 'float'"
              :model-value="numberValue(field.path)"
              :min="field.min"
              :max="field.max"
              :step="field.step ?? 1"
              :precision="field.kind === 'int' ? 0 : field.precision"
              :step-strictly="false"
              controls-position="right"
              style="width: 220px"
              @update:model-value="(v: number | undefined | null) => setAt(field.path, v ?? null)"
            />
            <el-switch v-else-if="field.kind === 'bool'" :model-value="!!getAt(field.path)" @update:model-value="(v: string | number | boolean) => setAt(field.path, !!v)" />
            <el-select
              v-else-if="field.kind === 'categories'"
              :model-value="arrayValue(field.path)"
              multiple
              filterable
              collapse-tags-tooltip
              style="width: 100%"
              @update:model-value="(v: string[]) => setAt(field.path, v)"
            >
              <el-option v-for="opt in categoryOptions" :key="opt.value" :label="opt.label" :value="opt.value" />
            </el-select>
            <el-select
              v-else-if="field.kind === 'tags'"
              :model-value="arrayValue(field.path)"
              multiple
              filterable
              allow-create
              default-first-option
              :reserve-keyword="false"
              :placeholder="t('aiRouting.fieldNamePlaceholder')"
              style="width: 100%"
              @update:model-value="(v: string[]) => setAt(field.path, v.map((x) => String(x).trim()).filter(Boolean))"
            >
              <el-option v-for="opt in arrayValue(field.path)" :key="opt" :label="opt" :value="opt" />
            </el-select>
            <el-input
              v-else-if="field.kind === 'text'"
              :model-value="String(getAt(field.path) ?? '')"
              maxlength="200"
              style="width: 320px"
              @update:model-value="(v: string) => setAt(field.path, v)"
            />
            <div v-if="field.help" class="field-help">{{ t(field.help) }}</div>
          </div>
        </el-form-item>
      </template>
    </el-form>
  </div>
</template>

<style scoped>
.ai-routing-form :deep(.el-divider__text) {
  font-weight: 600;
}
.field {
  width: 100%;
}
.field-help {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
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
</style>
