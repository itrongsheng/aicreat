<script setup lang="ts">
// 关联内容的可搜索下拉（docs/10 §7.1「关联内容」：GET /admin/contents?project_id=&keyword=）。
// 远程按标题搜索（防抖 300ms），已选值不在当前选项中时按 ID 取一次详情补齐标签；切换项目时清空选项。
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { Content } from "@aicreat/shared";
import * as contentsApi from "@/api/contents";
import StatusTag from "@/components/StatusTag.vue";

const props = withDefaults(
  defineProps<{
    modelValue: number | null | undefined;
    /** 限定项目；不传 / 0 时搜索全部可见内容 */
    projectId?: number | null;
    placeholder?: string;
    clearable?: boolean;
    disabled?: boolean;
    width?: string;
    size?: "large" | "default" | "small";
  }>(),
  { projectId: null, placeholder: "", clearable: true, disabled: false, width: "100%", size: "default" },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: number | null): void;
  (e: "change", value: number | null, content: Content | null): void;
}>();

const { t } = useI18n();

const options = ref<Content[]>([]);
const loading = ref(false);
/** 已选内容（不在当前搜索结果中时用于显示标签） */
const selected = ref<Content | null>(null);
let seq = 0;
let timer: ReturnType<typeof setTimeout> | null = null;

async function search(keyword = "") {
  const my = ++seq;
  loading.value = true;
  try {
    const res = await contentsApi.list(
      { project_id: props.projectId || undefined, keyword: keyword.trim() || undefined, page: 1, page_size: 20 },
      { silent: true },
    );
    if (my === seq) options.value = res.items;
  } catch {
    if (my === seq) options.value = [];
  } finally {
    if (my === seq) loading.value = false;
  }
}

function remote(query: string) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => void search(query), 300);
}

async function ensureSelected(id: number | null | undefined) {
  if (!id) {
    selected.value = null;
    return;
  }
  const hit = options.value.find((c) => c.id === id) ?? (selected.value?.id === id ? selected.value : null);
  if (hit) {
    selected.value = hit;
    return;
  }
  try {
    selected.value = await contentsApi.get(id, { silent: true });
  } catch {
    selected.value = null;
  }
}

const merged = computed<Content[]>(() => {
  const list = [...options.value];
  if (selected.value && !list.some((c) => c.id === selected.value!.id)) list.unshift(selected.value);
  return list;
});

const value = computed<number | undefined>({
  get: () => props.modelValue ?? undefined,
  set: (v) => {
    const next = typeof v === "number" && v > 0 ? v : null;
    const content = next ? (merged.value.find((c) => c.id === next) ?? null) : null;
    selected.value = content;
    emit("update:modelValue", next);
    emit("change", next, content);
  },
});

watch(
  () => props.projectId,
  () => {
    options.value = [];
    void search();
  },
);
watch(
  () => props.modelValue,
  (id) => void ensureSelected(id),
);

onMounted(() => {
  void search();
  void ensureSelected(props.modelValue);
});

defineExpose({ selected });
</script>

<template>
  <el-select
    v-model="value"
    filterable
    remote
    :remote-method="remote"
    :loading="loading"
    :clearable="clearable"
    :disabled="disabled"
    :size="size"
    :placeholder="placeholder || t('media.contentSelect.placeholder')"
    :no-data-text="t('media.contentSelect.empty')"
    :style="{ width }"
    @visible-change="(open: boolean) => open && !options.length && search()"
  >
    <el-option v-for="c in merged" :key="c.id" :value="c.id" :label="`#${c.id} ${c.title}`">
      <div class="content-option">
        <span class="content-option__title">#{{ c.id }} {{ c.title }}</span>
        <StatusTag kind="content_status" :value="c.status" />
      </div>
    </el-option>
  </el-select>
</template>

<style scoped>
.content-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.content-option__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
