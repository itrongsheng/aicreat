<script setup lang="ts">
// 「系统配置 → 统计」Tab 的专用表单（docs/12 §4.7、§4.8）：stats_config 的时区、每日聚合时间、今日刷新间隔、保留天数、
// 榜单默认条数、总览缓存秒数。前端即时校验与后端 StatsConfig 一致（时区 = 可加载的 IANA 名、daily_at = HH:MM、
// intraday_refresh_seconds = 0 或 60~86400、retention_days 30~3650、rankings_limit 1~100、overview_cache_seconds 0~3600）；
// 时区与已保存值不同时提示「历史统计需按新时区重算（每次最多 31 天）」（后端不自动全量重算）。
import { computed, reactive, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { ValidationErrorItem } from "@aicreat/shared";
import { clone, errorAt, fillDefaults, relLoc, type Json, type Path } from "@/utils/settingsForm";

const props = withDefaults(
  defineProps<{
    modelValue: Json;
    readonly?: boolean;
    errors?: ValidationErrorItem[];
    /** 已保存的值（用于判断时区是否被修改） */
    saved?: Json;
  }>(),
  { readonly: false, errors: () => [], saved: () => ({}) },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: Json): void;
  (e: "validity", valid: boolean): void;
}>();

const { t } = useI18n();

const DEFAULTS: Json = {
  version: 1,
  timezone: "Asia/Shanghai",
  daily_at: "00:30",
  intraday_refresh_seconds: 600,
  retention_days: 730,
  rankings_limit: 10,
  overview_cache_seconds: 60,
};

const DAILY_AT_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

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

/** 浏览器支持时列出全部 IANA 时区作为候选（仍允许手工输入） */
const timezones = computed<string[]>(() => {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
  let list: string[] = [];
  try {
    list = intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    list = [];
  }
  const current = typeof local.timezone === "string" ? local.timezone : "";
  const base = list.length ? list : ["Asia/Shanghai", "Asia/Hong_Kong", "Asia/Tokyo", "Asia/Singapore", "Europe/London", "America/New_York", "UTC"];
  return current && !base.includes(current) ? [current, ...base] : base;
});

function isValidTimezone(tz: unknown): boolean {
  if (typeof tz !== "string" || !tz.trim() || tz.startsWith("/") || tz.includes("..")) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz.trim() });
    return true;
  } catch {
    return false;
  }
}

function intIn(v: unknown, min: number, max: number): boolean {
  return typeof v === "number" && Number.isInteger(v) && v >= min && v <= max;
}

const localErrors = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  if (!isValidTimezone(local.timezone)) out.timezone = t("statsSettings.errors.timezone");
  if (typeof local.daily_at !== "string" || !DAILY_AT_RE.test(local.daily_at)) out.daily_at = t("statsSettings.errors.dailyAt");
  const intraday = local.intraday_refresh_seconds;
  if (!(intraday === 0 || intIn(intraday, 60, 86400))) out.intraday_refresh_seconds = t("statsSettings.errors.intraday");
  if (!intIn(local.retention_days, 30, 3650)) out.retention_days = t("statsSettings.errors.range", { min: 30, max: 3650 });
  if (!intIn(local.rankings_limit, 1, 100)) out.rankings_limit = t("statsSettings.errors.range", { min: 1, max: 100 });
  if (!intIn(local.overview_cache_seconds, 0, 3600)) out.overview_cache_seconds = t("statsSettings.errors.range", { min: 0, max: 3600 });
  return out;
});

watch(
  () => Object.keys(localErrors.value).length === 0,
  (valid) => emit("validity", valid),
  { immediate: true },
);

function fieldError(path: Path): string | undefined {
  return errorAt(props.errors, path) ?? localErrors.value[path.join(".")];
}

const timezoneChanged = computed(() => {
  const saved = props.saved?.timezone;
  return typeof saved === "string" && saved !== "" && typeof local.timezone === "string" && local.timezone.trim() !== saved;
});

const KNOWN = ["version", "timezone", "daily_at", "intraday_refresh_seconds", "retention_days", "rankings_limit", "overview_cache_seconds"];
const otherErrors = computed(() => props.errors.filter((e) => !KNOWN.includes(relLoc(e)[0] ?? "")));

function numModel(key: string): number | undefined {
  const v = local[key];
  return typeof v === "number" ? v : undefined;
}

function setNum(key: string, v: number | undefined | null) {
  local[key] = v ?? null;
}
</script>

<template>
  <div class="stats-form">
    <el-alert v-if="otherErrors.length" type="error" :closable="false" :title="t('settings.errorsTitle')" class="form-tip">
      <ul class="error-list">
        <li v-for="(e, idx) in otherErrors" :key="idx">
          <code>{{ relLoc(e).join(".") || "-" }}</code> {{ e.msg }}
        </li>
      </ul>
    </el-alert>
    <el-alert v-if="timezoneChanged" type="warning" :closable="false" show-icon :title="t('statsSettings.timezoneChanged')" class="form-tip" />

    <el-form label-width="160px" :disabled="readonly" class="top-form" @submit.prevent>
      <el-form-item :label="t('statsSettings.timezone')" :error="fieldError(['timezone'])">
        <el-select v-model="local.timezone" filterable allow-create default-first-option style="width: 260px">
          <el-option v-for="tz in timezones" :key="tz" :value="tz" :label="tz" />
        </el-select>
        <div class="field-help">{{ t("statsSettings.timezoneHelp") }}</div>
      </el-form-item>
      <el-form-item :label="t('statsSettings.dailyAt')" :error="fieldError(['daily_at'])">
        <el-time-picker v-model="local.daily_at" format="HH:mm" value-format="HH:mm" :clearable="false" style="width: 160px" />
        <div class="field-help">{{ t("statsSettings.dailyAtHelp") }}</div>
      </el-form-item>
      <el-form-item :label="t('statsSettings.intraday')" :error="fieldError(['intraday_refresh_seconds'])">
        <el-input-number
          :model-value="numModel('intraday_refresh_seconds')"
          :min="0"
          :max="86400"
          :precision="0"
          :step="60"
          controls-position="right"
          style="width: 160px"
          @update:model-value="(v: number | undefined | null) => setNum('intraday_refresh_seconds', v)"
        />
        <span class="unit">{{ t("statsSettings.units.seconds") }}</span>
        <div class="field-help">{{ t("statsSettings.intradayHelp") }}</div>
      </el-form-item>
      <el-form-item :label="t('statsSettings.retention')" :error="fieldError(['retention_days'])">
        <el-input-number
          :model-value="numModel('retention_days')"
          :min="30"
          :max="3650"
          :precision="0"
          :step="30"
          controls-position="right"
          style="width: 160px"
          @update:model-value="(v: number | undefined | null) => setNum('retention_days', v)"
        />
        <span class="unit">{{ t("statsSettings.units.days") }}</span>
        <div class="field-help">{{ t("statsSettings.retentionHelp") }}</div>
      </el-form-item>
      <el-form-item :label="t('statsSettings.rankingsLimit')" :error="fieldError(['rankings_limit'])">
        <el-input-number
          :model-value="numModel('rankings_limit')"
          :min="1"
          :max="100"
          :precision="0"
          controls-position="right"
          style="width: 160px"
          @update:model-value="(v: number | undefined | null) => setNum('rankings_limit', v)"
        />
        <span class="unit">{{ t("statsSettings.units.rows") }}</span>
        <div class="field-help">{{ t("statsSettings.rankingsLimitHelp") }}</div>
      </el-form-item>
      <el-form-item :label="t('statsSettings.overviewCache')" :error="fieldError(['overview_cache_seconds'])">
        <el-input-number
          :model-value="numModel('overview_cache_seconds')"
          :min="0"
          :max="3600"
          :precision="0"
          :step="10"
          controls-position="right"
          style="width: 160px"
          @update:model-value="(v: number | undefined | null) => setNum('overview_cache_seconds', v)"
        />
        <span class="unit">{{ t("statsSettings.units.seconds") }}</span>
        <div class="field-help">{{ t("statsSettings.overviewCacheHelp") }}</div>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.form-tip {
  margin-bottom: 12px;
}
.error-list {
  margin: 4px 0 0;
  padding-left: 18px;
}
.field-help {
  width: 100%;
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.unit {
  margin-left: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
@media (max-width: 768px) {
  .top-form :deep(.el-form-item) {
    flex-direction: column;
    align-items: stretch;
  }
  .top-form :deep(.el-form-item__label) {
    justify-content: flex-start;
    width: auto !important;
    height: auto;
    line-height: 1.5;
    margin-bottom: 4px;
  }
}
</style>
