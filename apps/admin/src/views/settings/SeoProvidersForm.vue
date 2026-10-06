<script setup lang="ts">
// 「系统配置 → SEO 提供器」Tab 的专用表单（docs/11 §7.6、§11.5）：
// - 每引擎一行：提供器下拉（按引擎限制可选项：baidu_ai_search 仅 baidu、bing_webmaster 仅 bing、google_search_console 仅 google）、
//   启用、model（仅 zhiqi_web_search；留空走 capability_routes(seo_check) 候选链）、options（JSON，展开行）；
// - 提供器参数区：zhiqi_web_search 的模板 / 协议 / 超时 / 匹配方式 / extra；其它提供器显示 credential_env / site_url_env 与
//   configured 状态（只读，密钥永不入 settings）以及 endpoint / timeout_seconds / top_k；
// - 真实模式下凭据未配置的提供器不可启用（前端即时提示，后端 400 兜底）；Mock 模式不受限。
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { SEO_ENGINE, SEO_PROVIDER, type SeoProvider, type ValidationErrorItem, type ZhiqiMode } from "@aicreat/shared";
import JsonEditor from "@/components/JsonEditor.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import { clone, errorAt, fillDefaults, isObject, relLoc, type Json, type Path } from "@/utils/settingsForm";

const props = withDefaults(
  defineProps<{
    modelValue: Json;
    readonly?: boolean;
    errors?: ValidationErrorItem[];
    zhiqiMode?: ZhiqiMode;
  }>(),
  { readonly: false, errors: () => [], zhiqiMode: "mock" },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: Json): void;
  (e: "validity", valid: boolean): void;
}>();

const { t, te } = useI18n();

/** 非 zhiqi 提供器只允许用于对应引擎（与后端 PROVIDER_ENGINE_RESTRICTIONS 一致） */
const PROVIDER_ENGINE: Partial<Record<SeoProvider, string>> = {
  baidu_ai_search: "baidu",
  bing_webmaster: "bing",
  google_search_console: "google",
};
const ENV_PROVIDERS = ["baidu_ai_search", "bing_webmaster", "google_search_console"] as const;
const PROTOCOLS = ["openai_chat", "openai_responses", "anthropic_messages"] as const;
const ZHIQI_MATCH_MODES = ["url_or_domain", "url", "domain"] as const;
const TEMPLATE_RE = /^[a-z][a-z0-9_]{2,79}$/;

const DEFAULTS: Json = {
  version: 1,
  engines: {
    baidu: { provider: "zhiqi_web_search", enabled: true, model: "", options: {} },
    bing: { provider: "zhiqi_web_search", enabled: true, model: "", options: {} },
    google: { provider: "zhiqi_web_search", enabled: false, model: "", options: {} },
  },
  providers: {
    zhiqi_web_search: { prompt_template_code: "sys_seo_query", protocol: "openai_chat", timeout_seconds: 120, match_mode: "url_or_domain", extra: {} },
    baidu_ai_search: { endpoint: "https://qianfan.baidubce.com/v2/ai_search/web_search", credential_env: "SEO_BAIDU_AI_SEARCH_API_KEY", timeout_seconds: 30, top_k: 10 },
    bing_webmaster: { credential_env: "SEO_BING_WEBMASTER_API_KEY", site_url_env: "SEO_BING_SITE_URL", timeout_seconds: 30 },
    google_search_console: { credential_env: "SEO_GSC_CREDENTIALS_FILE", site_url_env: "SEO_GSC_SITE_URL", timeout_seconds: 30 },
    manual: {},
  },
};

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

const live = computed(() => props.zhiqiMode === "live");

const engineMap = computed(() => (isObject(local.engines) ? (local.engines as Record<string, Json>) : {}));
const providerMap = computed(() => (isObject(local.providers) ? (local.providers as Record<string, Json>) : {}));

interface EngineRow {
  code: string;
  cfg: Json;
}

const engineRows = computed<EngineRow[]>(() => {
  const codes = [...SEO_ENGINE, ...Object.keys(engineMap.value).filter((c) => !(SEO_ENGINE as readonly string[]).includes(c))];
  return codes.filter((c) => isObject(engineMap.value[c])).map((code) => ({ code, cfg: engineMap.value[code] }));
});

function providerOptions(engine: string): SeoProvider[] {
  return SEO_PROVIDER.filter((p) => !PROVIDER_ENGINE[p] || PROVIDER_ENGINE[p] === engine);
}

function providerCfg(provider: string): Json {
  return providerMap.value[provider] ?? {};
}

/** 提供器凭据：null = 无需凭据（zhiqi / manual），true / false = 环境变量是否已配置（GET 时后端附加的 configured） */
function credentialState(provider: string): boolean | null {
  const cfg = providerCfg(provider);
  if (!("credential_env" in cfg)) return null;
  return cfg.configured === true;
}

function canEnable(row: EngineRow): boolean {
  if (!live.value) return true;
  return credentialState(String(row.cfg.provider)) !== false;
}

function setEnabled(row: EngineRow, value: boolean) {
  if (value && !canEnable(row)) return;
  row.cfg.enabled = value;
}

function setProvider(row: EngineRow, value: string) {
  row.cfg.provider = value;
  if (value !== "zhiqi_web_search") row.cfg.model = "";
}

// ---------- JSON 子编辑器合法性 ----------
const jsonValid = ref<Record<string, boolean>>({});

function setJsonValid(key: string, valid: boolean) {
  jsonValid.value = { ...jsonValid.value, [key]: valid };
}

// ---------- 前端即时校验 ----------
const localErrors = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  for (const row of engineRows.value) {
    const provider = String(row.cfg.provider ?? "");
    const only = PROVIDER_ENGINE[provider as SeoProvider];
    if (only && only !== row.code) out[`engines.${row.code}.provider`] = t("seoSettings.errors.providerEngine", { provider, engine: only });
    if (row.cfg.enabled && live.value && credentialState(provider) === false) out[`engines.${row.code}.enabled`] = t("seoSettings.errors.credentialMissing");
    if (jsonValid.value[`engines.${row.code}.options`] === false) out[`engines.${row.code}.options`] = t("seoSettings.errors.jsonInvalid");
  }
  const zhiqi = providerCfg("zhiqi_web_search");
  if (!TEMPLATE_RE.test(String(zhiqi.prompt_template_code ?? ""))) out["providers.zhiqi_web_search.prompt_template_code"] = t("geoSettings.errors.templateFormat");
  if (jsonValid.value["providers.zhiqi_web_search.extra"] === false) out["providers.zhiqi_web_search.extra"] = t("seoSettings.errors.jsonInvalid");
  const baidu = providerCfg("baidu_ai_search");
  if (!/^https:\/\/[^\s/?#]+(\/\S*)?$/.test(String(baidu.endpoint ?? "").trim())) out["providers.baidu_ai_search.endpoint"] = t("seoSettings.errors.endpoint");
  const ranges: [string, string, number, number][] = [
    ["zhiqi_web_search", "timeout_seconds", 1, 600],
    ["baidu_ai_search", "timeout_seconds", 1, 300],
    ["baidu_ai_search", "top_k", 1, 50],
    ["bing_webmaster", "timeout_seconds", 1, 300],
    ["google_search_console", "timeout_seconds", 1, 300],
  ];
  for (const [p, f, min, max] of ranges) {
    const v = providerCfg(p)[f];
    if (typeof v !== "number" || !Number.isInteger(v) || v < min || v > max) out[`providers.${p}.${f}`] = t("monitoringSettings.errors.range", { min, max });
  }
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

const KNOWN_ROOTS = new Set(["engines", "providers"]);
/** 归不到具体字段的后端错误（如 credential_missing、provider_engine_mismatch 为模型级 loc） */
const otherErrors = computed(() =>
  props.errors.filter((e) => {
    const rel = relLoc(e);
    return !(KNOWN_ROOTS.has(rel[0]) && rel.length >= 3);
  }),
);

function num(cfg: Json, key: string): number | undefined {
  const v = cfg[key];
  return typeof v === "number" ? v : undefined;
}

function engineDisplay(code: string): string {
  const key = `status.seo_engine.${code}`;
  return te(key) ? t(key) : code;
}
</script>

<template>
  <div class="seo-form">
    <el-alert type="info" :closable="false" show-icon :title="live ? t('seoSettings.liveTip') : t('seoSettings.mockTip')" class="form-tip" />

    <el-alert v-if="otherErrors.length" type="error" :closable="false" :title="t('settings.errorsTitle')" class="form-tip">
      <ul class="error-list">
        <li v-for="(e, idx) in otherErrors" :key="idx">
          <code>{{ relLoc(e).join(".") || "-" }}</code> {{ e.msg }}
        </li>
      </ul>
    </el-alert>

    <h3 class="section-title">{{ t("seoSettings.enginesTitle") }}</h3>
    <div class="engine-list">
      <div v-for="row in engineRows" :key="row.code" class="engine-row" :class="{ 'is-enabled': row.cfg.enabled }">
        <div class="engine-head">
          <el-tooltip :disabled="canEnable(row) || !!row.cfg.enabled" :content="t('seoSettings.enableNeedsCredential')" placement="top">
            <span>
              <el-switch
                :model-value="!!row.cfg.enabled"
                :disabled="readonly || (!row.cfg.enabled && !canEnable(row))"
                @update:model-value="(v: string | number | boolean) => setEnabled(row, !!v)"
              />
            </span>
          </el-tooltip>
          <span class="engine-name">{{ engineDisplay(row.code) }}</span>
          <span class="mono text-secondary">{{ row.code }}</span>
          <span v-if="fieldError(['engines', row.code, 'enabled'])" class="field-error">{{ fieldError(["engines", row.code, "enabled"]) }}</span>
        </div>
        <el-form label-width="110px" :disabled="readonly" class="engine-form" @submit.prevent>
          <el-row :gutter="16">
            <el-col :xs="24" :md="12">
              <el-form-item :label="t('seoSettings.fields.provider')" :error="fieldError(['engines', row.code, 'provider'])">
                <el-select :model-value="String(row.cfg.provider ?? '')" style="width: 100%" @update:model-value="(v: string) => setProvider(row, v)">
                  <el-option v-for="p in providerOptions(row.code)" :key="p" :value="p" :label="`${t(`status.seo_provider.${p}`)}（${p}）`">
                    <span>{{ t(`status.seo_provider.${p}`) }}</span>
                    <span class="mono text-secondary option-code">{{ p }}</span>
                    <el-tag v-if="credentialState(p) === false" size="small" type="warning" effect="plain" class="option-tag">{{ t("settings.notConfigured") }}</el-tag>
                  </el-option>
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :xs="24" :md="12">
              <el-form-item :label="t('seoSettings.fields.model')" :error="fieldError(['engines', row.code, 'model'])">
                <ModelSelect
                  v-if="row.cfg.provider === 'zhiqi_web_search'"
                  :model-value="String(row.cfg.model ?? '')"
                  modality="text"
                  allow-empty
                  :disabled="readonly"
                  @update:model-value="(v: string | null) => (row.cfg.model = v ?? '')"
                />
                <span v-else class="text-secondary small">{{ t("seoSettings.modelOnlyZhiqi") }}</span>
              </el-form-item>
            </el-col>
            <el-col :span="24">
              <el-form-item :label="t('seoSettings.fields.options')" :error="fieldError(['engines', row.code, 'options'])">
                <div class="field">
                  <JsonEditor v-model="row.cfg.options" object-only :rows="3" :readonly="readonly" @validity="(v: boolean) => setJsonValid(`engines.${row.code}.options`, v)" />
                  <div class="field-help">{{ t("seoSettings.optionsHelp") }}</div>
                </div>
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
      </div>
    </div>

    <h3 class="section-title">{{ t("seoSettings.providersTitle") }}</h3>

    <!-- zhiqi_web_search -->
    <el-card shadow="never" class="provider-card">
      <template #header>
        <div class="provider-head">
          <span class="engine-name">{{ t("status.seo_provider.zhiqi_web_search") }}</span>
          <span class="mono text-secondary">zhiqi_web_search</span>
        </div>
      </template>
      <el-form label-width="150px" :disabled="readonly" class="engine-form" @submit.prevent>
        <el-row :gutter="16">
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.prompt_template_code')" :error="fieldError(['providers', 'zhiqi_web_search', 'prompt_template_code'])">
              <el-input v-model="providerCfg('zhiqi_web_search').prompt_template_code" maxlength="80" />
              <div class="field-help">{{ t("seoSettings.templateHelp") }}</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.protocol')" :error="fieldError(['providers', 'zhiqi_web_search', 'protocol'])">
              <el-select v-model="providerCfg('zhiqi_web_search').protocol" style="width: 100%">
                <el-option v-for="p in PROTOCOLS" :key="p" :value="p" :label="p" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.timeout_seconds')" :error="fieldError(['providers', 'zhiqi_web_search', 'timeout_seconds'])">
              <el-input-number
                :model-value="num(providerCfg('zhiqi_web_search'), 'timeout_seconds')"
                :min="1"
                :max="600"
                :precision="0"
                controls-position="right"
                style="width: 160px"
                @update:model-value="(v: number | undefined | null) => (providerCfg('zhiqi_web_search').timeout_seconds = v ?? null)"
              />
              <span class="unit">{{ t("monitoringSettings.units.seconds") }}</span>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.match_mode')" :error="fieldError(['providers', 'zhiqi_web_search', 'match_mode'])">
              <el-select v-model="providerCfg('zhiqi_web_search').match_mode" style="width: 100%">
                <el-option v-for="m in ZHIQI_MATCH_MODES" :key="m" :value="m" :label="t(`geoSettings.matchModes.${m}`)" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="t('geoSettings.fields.extra')" :error="fieldError(['providers', 'zhiqi_web_search', 'extra'])">
              <div class="field">
                <JsonEditor
                  v-model="providerCfg('zhiqi_web_search').extra"
                  object-only
                  :rows="3"
                  :readonly="readonly"
                  @validity="(v: boolean) => setJsonValid('providers.zhiqi_web_search.extra', v)"
                />
                <div class="field-help">{{ t("geoSettings.extraHelp") }}</div>
              </div>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
    </el-card>

    <!-- 需要凭据的提供器 -->
    <el-card v-for="p in ENV_PROVIDERS" :key="p" shadow="never" class="provider-card">
      <template #header>
        <div class="provider-head">
          <span class="engine-name">{{ t(`status.seo_provider.${p}`) }}</span>
          <span class="mono text-secondary">{{ p }}</span>
          <el-tag v-if="credentialState(p) === true" type="success" size="small" disable-transitions>{{ t("settings.configured") }}</el-tag>
          <el-tag v-else type="warning" size="small" disable-transitions>{{ t("settings.notConfigured") }}</el-tag>
          <span class="text-secondary small">{{ t(`seoSettings.providerHelp.${p}`) }}</span>
        </div>
      </template>
      <el-form label-width="150px" :disabled="readonly" class="engine-form" @submit.prevent>
        <el-row :gutter="16">
          <el-col v-if="p === 'baidu_ai_search'" :span="24">
            <el-form-item :label="t('seoSettings.fields.endpoint')" :error="fieldError(['providers', p, 'endpoint'])">
              <el-input v-model="providerCfg(p).endpoint" maxlength="500" />
              <div class="field-help">{{ t("seoSettings.endpointHelp") }}</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('seoSettings.fields.credential_env')">
              <span class="mono">{{ providerCfg(p).credential_env || "-" }}</span>
            </el-form-item>
          </el-col>
          <el-col v-if="'site_url_env' in providerCfg(p)" :xs="24" :md="12">
            <el-form-item :label="t('seoSettings.fields.site_url_env')">
              <span class="mono">{{ providerCfg(p).site_url_env || "-" }}</span>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.timeout_seconds')" :error="fieldError(['providers', p, 'timeout_seconds'])">
              <el-input-number
                :model-value="num(providerCfg(p), 'timeout_seconds')"
                :min="1"
                :max="300"
                :precision="0"
                controls-position="right"
                style="width: 160px"
                @update:model-value="(v: number | undefined | null) => (providerCfg(p).timeout_seconds = v ?? null)"
              />
              <span class="unit">{{ t("monitoringSettings.units.seconds") }}</span>
            </el-form-item>
          </el-col>
          <el-col v-if="p === 'baidu_ai_search'" :xs="24" :md="12">
            <el-form-item :label="t('seoSettings.fields.top_k')" :error="fieldError(['providers', p, 'top_k'])">
              <el-input-number
                :model-value="num(providerCfg(p), 'top_k')"
                :min="1"
                :max="50"
                :precision="0"
                controls-position="right"
                style="width: 160px"
                @update:model-value="(v: number | undefined | null) => (providerCfg(p).top_k = v ?? null)"
              />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <div class="field-help">{{ t("settings.envHint") }}</div>
    </el-card>

    <el-card shadow="never" class="provider-card">
      <template #header>
        <div class="provider-head">
          <span class="engine-name">{{ t("status.seo_provider.manual") }}</span>
          <span class="mono text-secondary">manual</span>
        </div>
      </template>
      <div class="text-secondary small">{{ t("seoSettings.manualHelp") }}</div>
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
.engine-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.engine-row {
  padding: 10px 12px 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.engine-row.is-enabled {
  border-left: 3px solid var(--el-color-success);
}
.engine-head,
.provider-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.provider-head {
  margin-bottom: 0;
}
.engine-name {
  font-weight: 600;
}
.provider-card {
  margin-bottom: 12px;
}
.provider-card :deep(.el-card__header) {
  padding: 10px 16px;
}
.option-code {
  margin-left: 8px;
}
.option-tag {
  margin-left: 6px;
}
.field {
  width: 100%;
  min-width: 0;
}
.field-help {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
  width: 100%;
}
.field-error {
  color: var(--el-color-danger);
  font-size: 12px;
}
.small {
  font-size: 12px;
}
.unit {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
@media (max-width: 768px) {
  .engine-form :deep(.el-form-item) {
    flex-direction: column;
    align-items: stretch;
  }
  .engine-form :deep(.el-form-item__label) {
    justify-content: flex-start;
    width: auto !important;
    height: auto;
    line-height: 1.5;
    margin-bottom: 4px;
  }
}
</style>
