<script setup lang="ts">
// 「系统配置 → GEO 引擎」Tab 的专用表单（docs/11 §8.1、§11.5）：引擎卡片列表——启用开关、model（ModelSelect，文本模态）、protocol、
// 提示模板 code、extra（JSON 透传）、parse.*（引用来源 / 匹配方式 / 标题近似阈值）、timeout_seconds；可新增 / 删除自定义引擎。
// - 真实模式：model 为空的引擎不可启用（开关禁用并提示），后端 400 兜底；Mock 模式：model 可留空（走 mock-text），可逐个关闭；
// - match_mode=title 须换用 user_prompt 不含 {{title}} 的模板（后端校验 title_in_prompt）；
// - 校验错误 loc 形如 ["body","value","engines",2,"model"]，按引擎与字段归位；引擎对象级错误（model_required）显示在卡片顶部。
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ArrowDown, ArrowRight, Delete, Plus } from "@element-plus/icons-vue";
import { GEO_ENGINE, type ValidationErrorItem, type ZhiqiMode } from "@aicreat/shared";
import JsonEditor from "@/components/JsonEditor.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import { clone, errorAt, fillDefaults, isObject, locExact, relLoc, type Json, type Path } from "@/utils/settingsForm";

const props = withDefaults(
  defineProps<{
    modelValue: Json;
    readonly?: boolean;
    errors?: ValidationErrorItem[];
    zhiqiMode?: ZhiqiMode;
    /** 已保存的值：已保存引擎的 code 不可改（index_checks.engine / daily_stats 以 code 为键） */
    saved?: Json;
  }>(),
  { readonly: false, errors: () => [], zhiqiMode: "mock", saved: () => ({}) },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: Json): void;
  (e: "validity", valid: boolean): void;
}>();

const { t, te } = useI18n();

const PROTOCOLS = ["openai_chat", "openai_responses", "anthropic_messages"] as const;
const MATCH_MODES = ["url_or_domain", "url", "domain", "title"] as const;
const CITATION_SOURCES = ["annotations_or_markdown_links"] as const;
const CODE_RE = /^[a-z][a-z0-9_]{0,31}$/;
const TEMPLATE_RE = /^[a-z][a-z0-9_]{2,79}$/;
const MAX_ENGINES = 50;

function engineDefaults(code = "", name = ""): Json {
  return {
    code,
    name,
    enabled: false,
    model: "",
    protocol: "openai_chat",
    prompt_template_code: "sys_geo_query",
    extra: {},
    parse: { citation_source: "annotations_or_markdown_links", match_mode: "url_or_domain", title_fuzzy_threshold: 0.8 },
    timeout_seconds: 120,
  };
}

function normalize(value: unknown): Json {
  const out = fillDefaults(value, { version: 1, engines: [] });
  out.engines = (Array.isArray(out.engines) ? out.engines : []).map((e) => fillDefaults(e, engineDefaults()));
  return out;
}

const local = reactive<Json>(normalize(props.modelValue));
const engines = computed(() => local.engines as Json[]);

watch(
  () => props.modelValue,
  (value) => {
    const next = normalize(value);
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

const savedCodes = computed(() => {
  const list = isObject(props.saved) && Array.isArray(props.saved.engines) ? (props.saved.engines as unknown[]) : [];
  return new Set(list.filter(isObject).map((e) => String(e.code)));
});

const live = computed(() => props.zhiqiMode === "live");

function isBuiltin(code: unknown): boolean {
  return (GEO_ENGINE as readonly string[]).includes(String(code));
}

function codeLocked(engine: Json): boolean {
  return savedCodes.value.has(String(engine.code)) && !!engine.code;
}

function displayName(engine: Json): string {
  const code = String(engine.code || "");
  if (typeof engine.name === "string" && engine.name.trim()) return engine.name;
  return te(`status.geo_engine.${code}`) ? t(`status.geo_engine.${code}`) : code || t("geoSettings.newEngine");
}

function parseOf(engine: Json): Json {
  if (!isObject(engine.parse)) engine.parse = { citation_source: "annotations_or_markdown_links", match_mode: "url_or_domain", title_fuzzy_threshold: 0.8 };
  return engine.parse as Json;
}

function setModel(engine: Json, value: string | null) {
  engine.model = value ?? "";
}

function canEnable(engine: Json): boolean {
  return !live.value || !!String(engine.model ?? "").trim();
}

function setEnabled(engine: Json, value: boolean) {
  if (value && !canEnable(engine)) return;
  engine.enabled = value;
}

// ---------- extra JSON 合法性（每个引擎一个编辑器） ----------
const extraValid = ref<Record<number, boolean>>({});

function setExtraValid(idx: number, valid: boolean) {
  extraValid.value = { ...extraValid.value, [idx]: valid };
}

// ---------- 卡片展开 / 收起（默认展开已启用、新增与有错误的引擎） ----------
const expanded = ref<Record<number, boolean>>({});

function hasError(idx: number): boolean {
  return engineErrors(idx).length > 0 || props.errors.some((e) => relLoc(e)[0] === "engines" && relLoc(e)[1] === String(idx)) || Object.keys(localErrors.value).some((k) => k.startsWith(`${idx}.`));
}

function isExpanded(idx: number, engine: Json): boolean {
  if (hasError(idx)) return true;
  return expanded.value[idx] ?? (!!engine.enabled || !codeLocked(engine));
}

function toggle(idx: number, engine: Json) {
  expanded.value = { ...expanded.value, [idx]: !isExpanded(idx, engine) };
}

function shiftIndexMap<T>(map: Record<number, T>, removed: number): Record<number, T> {
  const next: Record<number, T> = {};
  for (const [k, v] of Object.entries(map)) {
    const i = Number(k);
    if (i < removed) next[i] = v;
    else if (i > removed) next[i - 1] = v;
  }
  return next;
}

// ---------- 新增 / 删除 ----------
function addEngine() {
  if (engines.value.length >= MAX_ENGINES) return;
  (local.engines as Json[]).push(engineDefaults());
  expanded.value = { ...expanded.value, [engines.value.length - 1]: true };
}

function removeEngine(idx: number) {
  (local.engines as Json[]).splice(idx, 1);
  extraValid.value = shiftIndexMap(extraValid.value, idx);
  expanded.value = shiftIndexMap(expanded.value, idx);
}

// ---------- 前端即时校验 ----------
const localErrors = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  const codes = engines.value.map((e) => String(e.code ?? "").trim());
  engines.value.forEach((engine, idx) => {
    const code = codes[idx];
    if (!code) out[`${idx}.code`] = t("geoSettings.errors.codeRequired");
    else if (!CODE_RE.test(code)) out[`${idx}.code`] = t("geoSettings.errors.codeFormat");
    else if (codes.indexOf(code) !== idx) out[`${idx}.code`] = t("geoSettings.errors.codeDuplicate");
    const name = String(engine.name ?? "").trim();
    if (!name) out[`${idx}.name`] = t("geoSettings.errors.nameRequired");
    else if (name.length > 50) out[`${idx}.name`] = t("geoSettings.errors.nameLength");
    const tpl = String(engine.prompt_template_code ?? "").trim();
    if (!TEMPLATE_RE.test(tpl)) out[`${idx}.prompt_template_code`] = t("geoSettings.errors.templateFormat");
    if (engine.enabled && live.value && !String(engine.model ?? "").trim()) out[`${idx}.model`] = t("geoSettings.errors.modelRequired");
    const timeout = engine.timeout_seconds;
    if (typeof timeout !== "number" || !Number.isInteger(timeout) || timeout < 1 || timeout > 600) out[`${idx}.timeout_seconds`] = t("geoSettings.errors.timeout");
    const threshold = parseOf(engine).title_fuzzy_threshold;
    if (typeof threshold !== "number" || threshold < 0 || threshold > 1) out[`${idx}.parse.title_fuzzy_threshold`] = t("geoSettings.errors.threshold");
    if (extraValid.value[idx] === false) out[`${idx}.extra`] = t("geoSettings.errors.extraInvalid");
  });
  return out;
});

watch(
  () => Object.keys(localErrors.value).length === 0,
  (valid) => emit("validity", valid),
  { immediate: true },
);

function fieldError(idx: number, field: string): string | undefined {
  const path: Path = ["engines", idx, ...field.split(".")];
  return errorAt(props.errors, path) ?? localErrors.value[`${idx}.${field}`];
}

/** 引擎对象级错误（如 model_required、engines 列表级的 code 重复） */
function engineErrors(idx: number): ValidationErrorItem[] {
  return props.errors.filter((e) => locExact(e, ["engines", idx]));
}

const KNOWN_FIELDS = ["code", "name", "enabled", "model", "protocol", "prompt_template_code", "extra", "parse", "timeout_seconds"];

/** 归不到引擎字段的后端错误（如 engines 列表级 code 重复） */
const otherErrors = computed(() =>
  props.errors.filter((e) => {
    const rel = relLoc(e);
    return !(rel[0] === "engines" && rel.length >= 2 && (rel.length === 2 || KNOWN_FIELDS.includes(rel[2])));
  }),
);

const titlePlaceholder = "{{title}}";
</script>

<template>
  <div class="geo-form">
    <el-alert v-if="live" type="warning" :closable="false" show-icon :title="t('geoSettings.liveTip')" class="form-tip" />
    <el-alert v-else type="info" :closable="false" show-icon :title="t('geoSettings.mockTip')" class="form-tip" />

    <el-alert v-if="otherErrors.length" type="error" :closable="false" :title="t('settings.errorsTitle')" class="form-tip">
      <ul class="error-list">
        <li v-for="(e, idx) in otherErrors" :key="idx">
          <code>{{ relLoc(e).join(".") || "-" }}</code> {{ e.msg }}
        </li>
      </ul>
    </el-alert>

    <el-empty v-if="!engines.length" :description="t('geoSettings.empty')" :image-size="60" />

    <el-card v-for="(engine, idx) in engines" :key="idx" shadow="never" class="engine-card" :class="{ 'is-enabled': engine.enabled, 'is-collapsed': !isExpanded(idx, engine) }">
      <template #header>
        <div class="engine-header">
          <div class="engine-title">
            <el-button link :icon="isExpanded(idx, engine) ? ArrowDown : ArrowRight" :aria-label="t('geoSettings.toggle')" @click="toggle(idx, engine)" />
            <el-tooltip :disabled="canEnable(engine) || !!engine.enabled" :content="t('geoSettings.enableNeedsModel')" placement="top">
              <span>
                <el-switch
                  :model-value="!!engine.enabled"
                  :disabled="readonly || (!engine.enabled && !canEnable(engine))"
                  @update:model-value="(v: string | number | boolean) => setEnabled(engine, !!v)"
                />
              </span>
            </el-tooltip>
            <span class="engine-name" role="button" tabindex="0" @click="toggle(idx, engine)" @keydown.enter="toggle(idx, engine)">{{ displayName(engine) }}</span>
            <span class="mono text-secondary">{{ engine.code || "-" }}</span>
            <el-tag v-if="!isBuiltin(engine.code)" size="small" effect="plain" disable-transitions>{{ t("geoSettings.custom") }}</el-tag>
            <el-tag v-if="!live && engine.enabled && !engine.model" size="small" type="info" effect="plain" disable-transitions>{{ t("geoSettings.mockModel") }}</el-tag>
            <span v-if="!isExpanded(idx, engine)" class="engine-summary text-secondary">
              {{ engine.model || "-" }} · {{ engine.protocol }} · {{ t(`geoSettings.matchModes.${parseOf(engine).match_mode}`) }}
            </span>
          </div>
          <el-button v-if="!readonly && !isBuiltin(engine.code)" link type="danger" :icon="Delete" @click="removeEngine(idx)">{{ t("common.delete") }}</el-button>
        </div>
      </template>

      <el-alert v-for="(e, eIdx) in engineErrors(idx)" :key="eIdx" type="error" :closable="false" :title="e.msg" class="form-tip" />

      <el-form v-show="isExpanded(idx, engine)" label-width="150px" :disabled="readonly" class="engine-form" @submit.prevent>
        <el-row :gutter="16">
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.code')" :error="fieldError(idx, 'code')">
              <el-input v-model="engine.code" :disabled="codeLocked(engine)" maxlength="32" class="mono-input" :placeholder="t('geoSettings.codePlaceholder')" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.name')" :error="fieldError(idx, 'name')">
              <el-input v-model="engine.name" maxlength="50" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.model')" :error="fieldError(idx, 'model')">
              <ModelSelect
                :model-value="String(engine.model ?? '')"
                modality="text"
                :disabled="readonly"
                :placeholder="live ? t('geoSettings.modelPlaceholderLive') : t('geoSettings.modelPlaceholderMock')"
                @update:model-value="(v: string | null) => setModel(engine, v)"
              />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.protocol')" :error="fieldError(idx, 'protocol')">
              <el-select v-model="engine.protocol" style="width: 100%">
                <el-option v-for="p in PROTOCOLS" :key="p" :value="p" :label="p" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.prompt_template_code')" :error="fieldError(idx, 'prompt_template_code')">
              <el-input v-model="engine.prompt_template_code" maxlength="80" class="mono-input" />
              <div class="field-help">{{ t("geoSettings.templateHelp") }}</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.timeout_seconds')" :error="fieldError(idx, 'timeout_seconds')">
              <el-input-number
                :model-value="typeof engine.timeout_seconds === 'number' ? engine.timeout_seconds : undefined"
                :min="1"
                :max="600"
                :precision="0"
                controls-position="right"
                style="width: 160px"
                @update:model-value="(v: number | undefined | null) => (engine.timeout_seconds = v ?? null)"
              />
              <span class="unit">{{ t("monitoringSettings.units.seconds") }}</span>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.match_mode')" :error="fieldError(idx, 'parse.match_mode')">
              <el-select v-model="parseOf(engine).match_mode" style="width: 100%">
                <el-option v-for="m in MATCH_MODES" :key="m" :value="m" :label="t(`geoSettings.matchModes.${m}`)" />
              </el-select>
              <div v-if="parseOf(engine).match_mode === 'title'" class="field-warn">{{ t("geoSettings.titleModeWarn", { ph: titlePlaceholder }) }}</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.title_fuzzy_threshold')" :error="fieldError(idx, 'parse.title_fuzzy_threshold')">
              <el-input-number
                :model-value="typeof parseOf(engine).title_fuzzy_threshold === 'number' ? (parseOf(engine).title_fuzzy_threshold as number) : undefined"
                :min="0"
                :max="1"
                :step="0.05"
                :precision="2"
                :disabled="readonly || parseOf(engine).match_mode !== 'title'"
                controls-position="right"
                style="width: 160px"
                @update:model-value="(v: number | undefined | null) => (parseOf(engine).title_fuzzy_threshold = v ?? null)"
              />
              <div class="field-help">{{ t("geoSettings.thresholdHelp") }}</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :md="12">
            <el-form-item :label="t('geoSettings.fields.citation_source')" :error="fieldError(idx, 'parse.citation_source')">
              <el-select v-model="parseOf(engine).citation_source" style="width: 100%">
                <el-option v-for="c in CITATION_SOURCES" :key="c" :value="c" :label="t('geoSettings.citationSource')" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="t('geoSettings.fields.extra')" :error="fieldError(idx, 'extra')">
              <div class="field">
                <JsonEditor v-model="engine.extra" object-only :rows="4" :readonly="readonly" @validity="(v: boolean) => setExtraValid(idx, v)" />
                <div class="field-help">{{ t("geoSettings.extraHelp") }}</div>
              </div>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
    </el-card>

    <div v-if="!readonly" class="add-row">
      <el-button :icon="Plus" :disabled="engines.length >= MAX_ENGINES" @click="addEngine">{{ t("geoSettings.addEngine") }}</el-button>
      <span class="field-help">{{ t("geoSettings.addHelp") }}</span>
    </div>
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
.engine-card {
  margin-bottom: 12px;
}
.engine-card.is-enabled {
  border-left: 3px solid var(--el-color-success);
}
.engine-card.is-collapsed :deep(.el-card__body) {
  display: none;
}
.engine-card.is-collapsed :deep(.el-card__header) {
  border-bottom: none;
}
.engine-card :deep(.el-card__header) {
  padding: 10px 16px;
}
.engine-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.engine-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.engine-name {
  font-weight: 600;
  cursor: pointer;
}
.engine-summary {
  font-size: 12px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
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
.field-warn {
  margin-top: 2px;
  color: var(--el-color-warning);
  font-size: 12px;
  line-height: 1.5;
  width: 100%;
}
.unit {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.add-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
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
