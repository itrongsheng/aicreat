<script lang="ts">
// 生成表单的模板下拉（docs/09 §10.3、§10.8）：只列指定 kind 的 published 模板（全局 + 当前项目），首项「项目默认」（不传 template_id，
// 按 projects.default_templates → generation_config 默认 code 解析，docs/09 §4.2）。
// 需要 content.prompt_templates.view：调用方仅在 usePermission().has('content.prompt_templates.view') 时渲染本组件。
// 选项在模块级缓存 60s，同一 kind 的并发请求合并。
import type { PromptKind, PromptTemplate } from "@aicreat/shared";
import { MAX_PAGE_SIZE } from "@aicreat/shared";
import * as templatesApi from "@/api/promptTemplates";

const CACHE_TTL_MS = 60_000;
const cache = new Map<PromptKind, { at: number; promise: Promise<PromptTemplate[]> }>();

function loadKind(kind: PromptKind, force: boolean): Promise<PromptTemplate[]> {
  const hit = cache.get(kind);
  if (!force && hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.promise;
  const promise = templatesApi
    .list({ kind, status: "published", page: 1, page_size: MAX_PAGE_SIZE }, { silent: true })
    .then((res) => res.items);
  cache.set(kind, { at: Date.now(), promise });
  promise.catch(() => cache.delete(kind));
  return promise;
}

/** 清空缓存（模板发布 / 归档后调用） */
export function invalidateTemplateOptions(): void {
  cache.clear();
}
</script>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";

const props = withDefaults(
  defineProps<{
    modelValue: number | null | undefined;
    /** 可选的模板 kind（如正文生成为 content + section） */
    kinds: PromptKind[];
    /** 当前项目：只列全局模板与该项目的模板 */
    projectId?: number;
    /** 项目默认模板 ID（按 kind），用于在「项目默认」项上提示实际使用的模板 */
    projectDefaults?: Partial<Record<PromptKind, number>>;
    disabled?: boolean;
    width?: string;
  }>(),
  { projectId: 0, projectDefaults: () => ({}), disabled: false, width: "100%" },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: number | null): void;
  (e: "change", value: number | null, template: PromptTemplate | null): void;
}>();

const { t } = useI18n();
const options = ref<PromptTemplate[]>([]);
const loading = ref(false);

async function load(force = false) {
  loading.value = true;
  try {
    const lists = await Promise.all(props.kinds.map((k) => loadKind(k, force).catch(() => [] as PromptTemplate[])));
    options.value = lists
      .flat()
      .filter((tpl) => !tpl.project_id || tpl.project_id === props.projectId)
      .sort((a, b) => Number(!!b.project_id) - Number(!!a.project_id) || Number(b.is_system) - Number(a.is_system) || a.code.localeCompare(b.code));
  } finally {
    loading.value = false;
  }
}

watch(
  () => [props.kinds.join(","), props.projectId],
  () => void load(),
);
onMounted(() => void load());

const DEFAULT = 0;

const selected = computed<number>({
  get: () => props.modelValue ?? DEFAULT,
  set: (v) => {
    const next = v ? v : null;
    emit("update:modelValue", next);
    emit("change", next, next ? (options.value.find((o) => o.id === next) ?? null) : null);
  },
});

const defaultHint = computed(() => {
  const ids = props.kinds.map((k) => props.projectDefaults[k]).filter((id): id is number => !!id);
  if (!ids.length) return t("templateSelect.systemDefault");
  const tpl = options.value.find((o) => ids.includes(o.id));
  return tpl ? `${tpl.name}（${tpl.code} v${tpl.version}）` : `#${ids[0]}`;
});

/** 当前值不在选项中（已归档 / 不可见）时仍显示 */
const missing = computed(() => !!props.modelValue && !options.value.some((o) => o.id === props.modelValue));
</script>

<template>
  <el-select
    v-model="selected"
    filterable
    :loading="loading"
    :disabled="disabled"
    :style="{ width }"
    :placeholder="t('templateSelect.placeholder')"
    popper-class="template-select__popper"
  >
    <el-option :value="DEFAULT" :label="t('templateSelect.projectDefault')">
      <div class="template-select__option">
        <span>{{ t("templateSelect.projectDefault") }}</span>
        <span class="template-select__meta">{{ defaultHint }}</span>
      </div>
    </el-option>
    <el-option v-if="missing && modelValue" :value="modelValue" :label="`#${modelValue}`" />
    <el-option v-for="tpl in options" :key="tpl.id" :value="tpl.id" :label="`${tpl.name}（${tpl.code} v${tpl.version}）`">
      <div class="template-select__option">
        <span class="template-select__name">{{ tpl.name }}</span>
        <span class="template-select__meta">
          <span class="mono">{{ tpl.code }} v{{ tpl.version }}</span>
          <el-tag v-if="kinds.length > 1" size="small" effect="plain">{{ t(`status.prompt_kind.${tpl.kind}`) }}</el-tag>
          <el-tag size="small" :type="tpl.project_id ? 'success' : 'info'" effect="plain">
            {{ tpl.project_id ? t("templateSelect.project") : t("templateSelect.global") }}
          </el-tag>
        </span>
      </div>
    </el-option>
  </el-select>
</template>

<style scoped>
.template-select__option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.template-select__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.template-select__meta {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: none;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
