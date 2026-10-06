<script setup lang="ts">
// 「系统配置 → 告警」Tab 的专用表单（docs/11 §10.3、§10.4、§11.5）：总开关、规则表（启用、severity、阈值参数）、去重冷却分钟、
// 通道（in_app 固定启用只读；webhook / email 为预留通道，显示环境变量 configured 状态与说明）。
// 校验：阈值参数为正整数；webhook 启用时 ALERT_WEBHOOK_URL 须已配置；email 启用时 to 须为合法邮箱且非空（SMTP_HOST 由后端校验）。
import { computed, reactive, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ALERT_SEVERITY, ALERT_TYPE, type AlertType, type ValidationErrorItem } from "@aicreat/shared";
import { clone, errorAt, fillDefaults, isObject, relLoc, type Json, type Path } from "@/utils/settingsForm";

const props = withDefaults(
  defineProps<{
    modelValue: Json;
    readonly?: boolean;
    errors?: ValidationErrorItem[];
  }>(),
  { readonly: false, errors: () => [] },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: Json): void;
  (e: "validity", valid: boolean): void;
}>();

const { t } = useI18n();

/** 各规则的阈值参数（docs/11 §10.3） */
const RULE_PARAMS: Partial<Record<AlertType, { key: string; unit?: "days" | "minutes" | "times" }[]>> = {
  index_overdue: [
    { key: "days", unit: "days" },
    { key: "min_checks", unit: "times" },
  ],
  ai_task_failures: [
    { key: "consecutive", unit: "times" },
    { key: "window_minutes", unit: "minutes" },
  ],
  ai_upstream_unavailable: [{ key: "consecutive_probes", unit: "times" }],
  worker_stale: [{ key: "minutes", unit: "minutes" }],
};

const RULE_DEFAULTS: Record<AlertType, Json> = {
  link_deleted: { enabled: true, severity: "warning" },
  link_restored: { enabled: true, severity: "info" },
  link_changed: { enabled: true, severity: "info" },
  index_overdue: { enabled: true, severity: "warning", days: 30, min_checks: 3 },
  ai_task_failures: { enabled: true, severity: "critical", consecutive: 5, window_minutes: 30 },
  ai_breaker_open: { enabled: true, severity: "warning" },
  ai_quota_exceeded: { enabled: true, severity: "critical" },
  ai_auth_failed: { enabled: true, severity: "critical" },
  ai_upstream_unavailable: { enabled: true, severity: "critical", consecutive_probes: 2 },
  media_task_failed: { enabled: true, severity: "info" },
  worker_stale: { enabled: true, severity: "warning", minutes: 5 },
};

const DEFAULTS: Json = {
  version: 1,
  enabled: true,
  rules: RULE_DEFAULTS,
  dedupe_cooldown_minutes: 60,
  channels: {
    in_app: { enabled: true },
    webhook: { enabled: false, url_env: "ALERT_WEBHOOK_URL", min_severity: "warning" },
    email: { enabled: false, to: [], min_severity: "critical" },
  },
};

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

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

const rules = computed(() => (isObject(local.rules) ? (local.rules as Record<string, Json>) : {}));
const channels = computed(() => (isObject(local.channels) ? (local.channels as Record<string, Json>) : {}));
const webhook = computed(() => channels.value.webhook ?? {});
const email = computed(() => channels.value.email ?? {});

interface RuleRow {
  type: AlertType;
  cfg: Json;
}

const ruleRows = computed<RuleRow[]>(() => ALERT_TYPE.filter((type) => isObject(rules.value[type])).map((type) => ({ type, cfg: rules.value[type] })));

const severityOptions = computed(() => ALERT_SEVERITY.map((s) => ({ value: s, label: t(`status.alert_severity.${s}`) })));

function numValue(cfg: Json, key: string): number | undefined {
  const v = cfg[key];
  return typeof v === "number" ? v : undefined;
}

const webhookConfigured = computed<boolean | null>(() => (typeof webhook.value.configured === "boolean" ? (webhook.value.configured as boolean) : null));

const emailList = computed<string[]>(() => (Array.isArray(email.value.to) ? (email.value.to as unknown[]).map(String) : []));

function setEmails(values: unknown[]) {
  email.value.to = [...new Set(values.map((v) => String(v).trim()).filter(Boolean))];
}

// ---------- 前端即时校验 ----------
const localErrors = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  for (const row of ruleRows.value) {
    for (const p of RULE_PARAMS[row.type] ?? []) {
      const v = row.cfg[p.key];
      if (typeof v !== "number" || !Number.isInteger(v) || v < 1) out[`rules.${row.type}.${p.key}`] = t("alertSettings.errors.positiveInt");
    }
  }
  const cooldown = local.dedupe_cooldown_minutes;
  if (typeof cooldown !== "number" || !Number.isInteger(cooldown) || cooldown < 0 || cooldown > 10080) {
    out.dedupe_cooldown_minutes = t("monitoringSettings.errors.range", { min: 0, max: 10080 });
  }
  if (webhook.value.enabled && webhookConfigured.value === false) out["channels.webhook.enabled"] = t("alertSettings.errors.webhookEnv");
  const bad = emailList.value.filter((e) => !EMAIL_RE.test(e));
  if (bad.length) out["channels.email.to"] = t("alertSettings.errors.emailFormat", { emails: bad.join(", ") });
  else if (email.value.enabled && !emailList.value.length) out["channels.email.to"] = t("alertSettings.errors.emailRequired");
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

function ruleError(type: AlertType): string | undefined {
  return errorAt(props.errors, ["rules", type]) ?? Object.entries(localErrors.value).find(([k]) => k.startsWith(`rules.${type}.`))?.[1];
}

/** 归不到具体字段的后端错误 */
const otherErrors = computed(() =>
  props.errors.filter((e) => {
    const rel = relLoc(e);
    if (rel[0] === "rules" && rel.length >= 2) return false;
    if (rel[0] === "channels" && rel.length >= 2) return false;
    return !["enabled", "dedupe_cooldown_minutes"].includes(rel[0] ?? "");
  }),
);
</script>

<template>
  <div class="alert-form">
    <el-alert v-if="otherErrors.length" type="error" :closable="false" :title="t('settings.errorsTitle')" class="form-tip">
      <ul class="error-list">
        <li v-for="(e, idx) in otherErrors" :key="idx">
          <code>{{ relLoc(e).join(".") || "-" }}</code> {{ e.msg }}
        </li>
      </ul>
    </el-alert>

    <el-form label-width="180px" :disabled="readonly" class="top-form" @submit.prevent>
      <el-form-item :label="t('alertSettings.enabled')" :error="fieldError(['enabled'])">
        <el-switch v-model="local.enabled" />
        <span class="field-help inline">{{ t("alertSettings.enabledHelp") }}</span>
      </el-form-item>
      <el-form-item :label="t('alertSettings.cooldown')" :error="fieldError(['dedupe_cooldown_minutes'])">
        <el-input-number
          :model-value="typeof local.dedupe_cooldown_minutes === 'number' ? (local.dedupe_cooldown_minutes as number) : undefined"
          :min="0"
          :max="10080"
          :precision="0"
          :step="10"
          controls-position="right"
          style="width: 160px"
          @update:model-value="(v: number | undefined | null) => (local.dedupe_cooldown_minutes = v ?? null)"
        />
        <span class="unit">{{ t("monitoringSettings.units.minutes") }}</span>
        <div class="field-help">{{ t("alertSettings.cooldownHelp") }}</div>
      </el-form-item>
    </el-form>

    <h3 class="section-title">{{ t("alertSettings.rulesTitle") }}</h3>
    <div class="rule-list">
      <div v-for="row in ruleRows" :key="row.type" class="rule-row" :class="{ 'is-off': !row.cfg.enabled }">
        <div class="rule-main">
          <el-switch v-model="row.cfg.enabled" :disabled="readonly" />
          <div class="rule-text">
            <div class="rule-name">
              {{ t(`status.alert_type.${row.type}`) }}
              <span class="mono text-secondary">{{ row.type }}</span>
            </div>
            <div class="field-help">{{ t(`alertSettings.ruleHelp.${row.type}`) }}</div>
          </div>
        </div>
        <div class="rule-params">
          <el-select v-model="row.cfg.severity" :disabled="readonly" size="small" class="severity-select">
            <el-option v-for="opt in severityOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
          </el-select>
          <label v-for="p in RULE_PARAMS[row.type] ?? []" :key="p.key" class="param">
            <span class="param-label">{{ t(`alertSettings.params.${p.key}`) }}</span>
            <el-input-number
              :model-value="numValue(row.cfg, p.key)"
              :min="1"
              :precision="0"
              :disabled="readonly"
              size="small"
              controls-position="right"
              class="param-input"
              @update:model-value="(v: number | undefined | null) => (row.cfg[p.key] = v ?? null)"
            />
            <span v-if="p.unit" class="unit">{{ t(`monitoringSettings.units.${p.unit}`) }}</span>
          </label>
        </div>
        <div v-if="ruleError(row.type)" class="field-error">{{ ruleError(row.type) }}</div>
      </div>
    </div>

    <h3 class="section-title">{{ t("alertSettings.channelsTitle") }}</h3>

    <el-card shadow="never" class="channel-card">
      <div class="channel-head">
        <el-switch :model-value="true" disabled />
        <span class="rule-name">{{ t("status.alert_channel.in_app") }}</span>
        <span class="mono text-secondary">in_app</span>
        <el-tag size="small" type="success" effect="plain" disable-transitions>{{ t("alertSettings.fixedOn") }}</el-tag>
      </div>
      <div class="field-help">{{ t("alertSettings.inAppHelp") }}</div>
    </el-card>

    <el-card shadow="never" class="channel-card">
      <div class="channel-head">
        <el-switch v-model="webhook.enabled" :disabled="readonly" />
        <span class="rule-name">{{ t("status.alert_channel.webhook") }}</span>
        <span class="mono text-secondary">webhook</span>
        <el-tag size="small" type="info" effect="plain" disable-transitions>{{ t("alertSettings.reserved") }}</el-tag>
      </div>
      <el-form label-width="150px" :disabled="readonly" class="channel-form" @submit.prevent>
        <el-form-item :label="t('alertSettings.urlEnv')" :error="fieldError(['channels', 'webhook', 'enabled']) ?? fieldError(['channels', 'webhook', 'url_env'])">
          <span class="mono">{{ webhook.url_env || "ALERT_WEBHOOK_URL" }}</span>
          <el-tag v-if="webhookConfigured === true" type="success" size="small" class="env-tag" disable-transitions>{{ t("settings.configured") }}</el-tag>
          <el-tag v-else-if="webhookConfigured === false" type="warning" size="small" class="env-tag" disable-transitions>{{ t("settings.notConfigured") }}</el-tag>
        </el-form-item>
        <el-form-item :label="t('alertSettings.minSeverity')" :error="fieldError(['channels', 'webhook', 'min_severity'])">
          <el-select v-model="webhook.min_severity" style="width: 160px">
            <el-option v-for="opt in severityOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="field-help">{{ t("alertSettings.webhookHelp") }}</div>
    </el-card>

    <el-card shadow="never" class="channel-card">
      <div class="channel-head">
        <el-switch v-model="email.enabled" :disabled="readonly" />
        <span class="rule-name">{{ t("status.alert_channel.email") }}</span>
        <span class="mono text-secondary">email</span>
        <el-tag size="small" type="info" effect="plain" disable-transitions>{{ t("alertSettings.reserved") }}</el-tag>
      </div>
      <el-form label-width="150px" :disabled="readonly" class="channel-form" @submit.prevent>
        <el-form-item :label="t('alertSettings.emailTo')" :error="fieldError(['channels', 'email', 'to']) ?? fieldError(['channels', 'email'])">
          <el-select
            :model-value="emailList"
            multiple
            filterable
            allow-create
            default-first-option
            :reserve-keyword="false"
            :placeholder="t('alertSettings.emailPlaceholder')"
            style="width: 100%; max-width: 480px"
            @update:model-value="setEmails"
          >
            <el-option v-for="e in emailList" :key="e" :value="e" :label="e" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('alertSettings.minSeverity')" :error="fieldError(['channels', 'email', 'min_severity'])">
          <el-select v-model="email.min_severity" style="width: 160px">
            <el-option v-for="opt in severityOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="field-help">{{ t("alertSettings.emailHelp") }}</div>
    </el-card>
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
.section-title {
  margin: 16px 0 8px;
  font-size: 15px;
  font-weight: 600;
}
.rule-list {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.rule-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  padding: 10px 12px;
}
.rule-row + .rule-row {
  border-top: 1px solid var(--el-border-color-lighter);
}
.rule-row.is-off .rule-text {
  opacity: 0.6;
}
.rule-main {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  flex: 1 1 320px;
}
.rule-text {
  min-width: 0;
}
.rule-name {
  font-weight: 600;
  display: inline-flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}
.rule-params {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
}
.severity-select {
  width: 110px;
}
.param {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.param-label {
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.param-input {
  width: 110px;
}
.channel-card {
  margin-bottom: 10px;
}
.channel-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.env-tag {
  margin-left: 8px;
}
.field-help {
  width: 100%;
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.field-help.inline {
  width: auto;
  margin-top: 0;
  margin-left: 10px;
}
.field-error {
  width: 100%;
  color: var(--el-color-danger);
  font-size: 12px;
}
.unit {
  margin-left: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
@media (max-width: 768px) {
  .top-form :deep(.el-form-item),
  .channel-form :deep(.el-form-item) {
    flex-direction: column;
    align-items: stretch;
  }
  .top-form :deep(.el-form-item__label),
  .channel-form :deep(.el-form-item__label) {
    justify-content: flex-start;
    width: auto !important;
    height: auto;
    line-height: 1.5;
    margin-bottom: 4px;
  }
}
</style>
