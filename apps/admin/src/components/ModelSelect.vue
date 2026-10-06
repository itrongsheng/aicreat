<script lang="ts">
// 模型下拉（docs/02 §3.6、docs/08 §11.5）：GET /admin/ai/models/options?modality=（不分页，按模态过滤），
// 选项显示供应商、可用性与最近健康状态。选项在模块级缓存 60s（与后端 cache:ai:models:options:* 一致），同一模态的并发请求合并。
import type { AiModelOption, Modality } from "@aicreat/shared";
import * as aiApi from "@/api/ai";

const CACHE_TTL_MS = 60_000;
const cache = new Map<string, { at: number; promise: Promise<AiModelOption[]> }>();

function loadOptions(modality: Modality | undefined, isAvailable: boolean, force: boolean): Promise<AiModelOption[]> {
  const key = `${modality ?? "*"}:${isAvailable ? 1 : 0}`;
  const hit = cache.get(key);
  if (!force && hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.promise;
  const promise = aiApi.listModelOptions(modality, isAvailable, { silent: true });
  cache.set(key, { at: Date.now(), promise });
  promise.catch(() => cache.delete(key));
  return promise;
}

/** 清空选项缓存（同步模型目录后调用） */
export function invalidateModelOptions(): void {
  cache.clear();
}

/**
 * 取某模态的模型选项。includeUnavailable=true 时另取 `is_available=0` 的选项并按 model_id 合并（路由编辑允许保存不可用模型）。
 */
export async function fetchModelOptions(modality?: Modality, includeUnavailable = false, force = false): Promise<AiModelOption[]> {
  const available = await loadOptions(modality, true, force);
  if (!includeUnavailable) return available;
  let rest: AiModelOption[] = [];
  try {
    rest = await loadOptions(modality, false, force);
  } catch {
    rest = [];
  }
  const seen = new Set(available.map((o) => o.model_id));
  const merged = [...available];
  for (const opt of rest) {
    if (!seen.has(opt.model_id)) {
      seen.add(opt.model_id);
      merged.push(opt);
    }
  }
  return merged;
}
</script>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import StatusTag from "@/components/StatusTag.vue";

const props = withDefaults(
  defineProps<{
    modelValue: string | null | undefined;
    /** 按模态过滤；不传则列出全部模态 */
    modality?: Modality;
    /** 同时列出 is_available=0 的模型（标记「不可用」，仍可选） */
    includeUnavailable?: boolean;
    /** 显示「按路由（默认）」空选项：生成表单的 model? 覆盖 */
    allowEmpty?: boolean;
    /** 排除的模型（如备选链中已被主模型 / 其它备选占用的模型） */
    exclude?: string[];
    placeholder?: string;
    clearable?: boolean;
    disabled?: boolean;
    width?: string;
    size?: "large" | "default" | "small";
  }>(),
  {
    modality: undefined,
    includeUnavailable: false,
    allowEmpty: false,
    exclude: () => [],
    placeholder: "",
    clearable: true,
    disabled: false,
    width: "100%",
    size: "default",
  },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: string | null): void;
  (e: "change", value: string | null, option: AiModelOption | null): void;
}>();

const { t } = useI18n();

const options = ref<AiModelOption[]>([]);
const loading = ref(false);
const failed = ref(false);

async function load(force = false) {
  loading.value = true;
  failed.value = false;
  try {
    options.value = await fetchModelOptions(props.modality, props.includeUnavailable, force);
  } catch {
    options.value = [];
    failed.value = true;
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.modality, props.includeUnavailable],
  () => void load(),
);
onMounted(() => void load());

const EMPTY = "__by_route__";

const visible = computed(() => {
  const excluded = new Set(props.exclude.filter((m) => m && m !== props.modelValue));
  return options.value.filter((o) => !excluded.has(o.model_id));
});

/** 当前值不在选项中（不在目录 / 已隐藏）时仍需显示 */
const missingCurrent = computed(() => !!props.modelValue && !options.value.some((o) => o.model_id === props.modelValue));

const selected = computed<string>({
  get: () => (props.modelValue ? props.modelValue : props.allowEmpty ? EMPTY : ""),
  set: (v) => {
    const next = !v || v === EMPTY ? null : v;
    emit("update:modelValue", next);
    emit("change", next, next ? (options.value.find((o) => o.model_id === next) ?? null) : null);
  },
});

const current = computed(() => options.value.find((o) => o.model_id === props.modelValue) ?? null);

defineExpose({ reload: () => load(true), options });
</script>

<template>
  <div class="model-select" :style="{ width }">
    <el-select
      v-model="selected"
      filterable
      :clearable="clearable && !!modelValue"
      :loading="loading"
      :disabled="disabled"
      :size="size"
      :placeholder="placeholder || t('modelSelect.placeholder')"
      :no-data-text="failed ? t('modelSelect.loadFailed') : t('modelSelect.empty')"
      class="model-select__input"
      popper-class="model-select__popper"
    >
      <template v-if="current || missingCurrent" #prefix>
        <span v-if="missingCurrent" class="model-select__dot is-missing" />
        <span v-else-if="current && !current.is_available" class="model-select__dot is-down" />
      </template>
      <el-option v-if="allowEmpty" :value="EMPTY" :label="t('modelSelect.byRoute')" />
      <el-option v-if="missingCurrent && modelValue" :value="modelValue" :label="modelValue">
        <div class="model-select__option">
          <span class="model-select__id mono">{{ modelValue }}</span>
          <el-tag type="danger" size="small" effect="plain">{{ t("modelSelect.notInCatalog") }}</el-tag>
        </div>
      </el-option>
      <el-option v-for="opt in visible" :key="opt.model_id" :value="opt.model_id" :label="opt.model_id">
        <div class="model-select__option">
          <span class="model-select__id mono">{{ opt.model_id }}</span>
          <span class="model-select__meta">
            <span v-if="opt.vendor_name" class="model-select__vendor">{{ opt.vendor_name }}</span>
            <el-tag size="small" effect="plain" type="info">{{ opt.quota_type === 1 ? t("modelSelect.perCall") : t("modelSelect.perToken") }}</el-tag>
            <el-tag v-if="!opt.is_available" type="danger" size="small" effect="plain">{{ t("modelSelect.unavailable") }}</el-tag>
            <StatusTag kind="health_status" :value="opt.last_health_status" />
          </span>
        </div>
      </el-option>
    </el-select>
    <el-tooltip :content="t('modelSelect.refresh')" placement="top">
      <el-button :icon="Refresh" :size="size" :loading="loading" :disabled="disabled" class="model-select__refresh" @click="load(true)" />
    </el-tooltip>
  </div>
</template>

<style scoped>
.model-select {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
}
.model-select__input {
  flex: 1;
  min-width: 0;
}
.model-select__refresh {
  flex: none;
}
.model-select__dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}
.model-select__dot.is-down {
  background: var(--el-color-danger);
}
.model-select__dot.is-missing {
  background: var(--el-color-warning);
}
.model-select__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.model-select__id {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.model-select__meta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
}
.model-select__vendor {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>

<style>
.model-select__popper .el-select-dropdown__item {
  height: auto;
  min-height: 34px;
  line-height: 1.4;
  padding-top: 6px;
  padding-bottom: 6px;
}
</style>
