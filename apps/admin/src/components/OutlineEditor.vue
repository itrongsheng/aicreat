<script lang="ts">
// 大纲编辑（docs/09 §8.2、§10.6）：H2 / H3 列表的增删改、拖拽与上下移动排序、要点编辑；修改随编辑器「保存」提交（形成 source=manual 版本）。
// 校验规则与后端 content_service.validate_outline 一致：1~40 项；heading 1~120 字符；level ∈ {2,3} 且首项为 2；points 0~8 项、每项 ≤ 200 字符。
import { CONTENT_LIMITS, type OutlineItem } from "@aicreat/shared";
import { t } from "@/i18n";

/** 返回第一条校验错误（无错误为 null）；空大纲视为「不设置大纲」，不报错 */
export function validateOutline(items: OutlineItem[] | null | undefined): string | null {
  if (!items || !items.length) return null;
  const L = CONTENT_LIMITS;
  if (items.length > L.outlineItems) return t("outline.tooMany", { max: L.outlineItems });
  if (items[0].level !== 2) return t("outline.firstMustBeH2");
  for (let i = 0; i < items.length; i += 1) {
    const it = items[i];
    const heading = (it.heading ?? "").trim();
    if (!heading) return t("outline.headingRequired", { index: i + 1 });
    if (heading.length > L.outlineHeading) return t("outline.headingTooLong", { index: i + 1, max: L.outlineHeading });
    if (it.level !== 2 && it.level !== 3) return t("outline.levelInvalid", { index: i + 1 });
    const points = it.points ?? [];
    if (points.length > L.outlinePoints) return t("outline.pointsTooMany", { index: i + 1, max: L.outlinePoints });
    if (points.some((p) => String(p).length > L.outlinePoint)) return t("outline.pointTooLong", { index: i + 1, max: L.outlinePoint });
  }
  return null;
}

/** 规范化为提交结构（去首尾空白，空要点剔除） */
export function normalizeOutline(items: OutlineItem[] | null | undefined): OutlineItem[] {
  return (items ?? []).map((it) => ({
    heading: (it.heading ?? "").trim(),
    level: it.level === 3 ? 3 : 2,
    points: (it.points ?? []).map((p) => String(p).trim()).filter(Boolean),
  }));
}
</script>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ArrowDown, ArrowUp, Delete, Plus, Rank } from "@element-plus/icons-vue";

const props = withDefaults(
  defineProps<{
    modelValue: OutlineItem[] | null | undefined;
    readonly?: boolean;
  }>(),
  { readonly: false },
);

const emit = defineEmits<{ (e: "update:modelValue", value: OutlineItem[]): void }>();

const { t: tt } = useI18n();
const items = computed(() => props.modelValue ?? []);
const error = computed(() => validateOutline(items.value));

function commit(next: OutlineItem[]) {
  emit("update:modelValue", next);
}

function update(index: number, patch: Partial<OutlineItem>) {
  commit(items.value.map((it, i) => (i === index ? { ...it, ...patch } : it)));
}

function add(level: 2 | 3, after = items.value.length - 1) {
  if (items.value.length >= CONTENT_LIMITS.outlineItems) return;
  const next = [...items.value];
  next.splice(after + 1, 0, { heading: "", level: items.value.length ? level : 2, points: [] });
  commit(next);
}

function remove(index: number) {
  commit(items.value.filter((_, i) => i !== index));
}

function move(from: number, to: number) {
  if (to < 0 || to >= items.value.length || from === to) return;
  const next = [...items.value];
  const [it] = next.splice(from, 1);
  next.splice(to, 0, it);
  commit(next);
}

// 拖拽排序（原生 HTML5 DnD，仅拖动手柄所在行）
const dragFrom = ref(-1);
const dragOver = ref(-1);

function onDragStart(index: number, event: DragEvent) {
  dragFrom.value = index;
  event.dataTransfer?.setData("text/plain", String(index));
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}

function onDragOver(index: number, event: DragEvent) {
  if (dragFrom.value < 0) return;
  event.preventDefault();
  dragOver.value = index;
}

function onDrop(index: number) {
  if (dragFrom.value >= 0) move(dragFrom.value, index);
  dragFrom.value = -1;
  dragOver.value = -1;
}

function onDragEnd() {
  dragFrom.value = -1;
  dragOver.value = -1;
}

defineExpose({ validate: () => validateOutline(items.value) });
</script>

<template>
  <div class="outline-editor">
    <div v-if="!items.length" class="outline-editor__empty">{{ tt("outline.empty") }}</div>
    <div
      v-for="(item, index) in items"
      :key="index"
      class="outline-row"
      :class="{ 'is-h3': item.level === 3, 'is-over': dragOver === index && dragFrom !== index }"
      @dragover="onDragOver(index, $event)"
      @drop.prevent="onDrop(index)"
      @dragend="onDragEnd"
    >
      <div class="outline-row__head">
        <span v-if="!readonly" class="outline-row__handle" draggable="true" :title="tt('outline.drag')" @dragstart="onDragStart(index, $event)">
          <el-icon><Rank /></el-icon>
        </span>
        <span class="outline-row__no">{{ index + 1 }}</span>
        <el-select
          :model-value="item.level"
          size="small"
          :disabled="readonly || index === 0"
          style="width: 64px"
          @update:model-value="(v: number) => update(index, { level: v })"
        >
          <el-option :value="2" label="H2" />
          <el-option :value="3" label="H3" />
        </el-select>
        <el-input
          :model-value="item.heading"
          size="small"
          :readonly="readonly"
          :maxlength="CONTENT_LIMITS.outlineHeading"
          :placeholder="tt('outline.headingPlaceholder')"
          @update:model-value="(v: string) => update(index, { heading: v })"
        />
        <template v-if="!readonly">
          <el-button size="small" text :icon="ArrowUp" :disabled="index === 0" @click="move(index, index - 1)" />
          <el-button size="small" text :icon="ArrowDown" :disabled="index === items.length - 1" @click="move(index, index + 1)" />
          <el-tooltip :content="tt('outline.addH3After')" placement="top">
            <el-button size="small" text :icon="Plus" @click="add(3, index)" />
          </el-tooltip>
          <el-button size="small" text type="danger" :icon="Delete" @click="remove(index)" />
        </template>
      </div>
      <el-input-tag
        :model-value="item.points ?? []"
        size="small"
        :readonly="readonly"
        :max="CONTENT_LIMITS.outlinePoints"
        :maxlength="CONTENT_LIMITS.outlinePoint"
        trigger="Enter"
        :placeholder="readonly ? '' : tt('outline.pointsPlaceholder')"
        class="outline-row__points"
        @update:model-value="(v: string[] | undefined) => update(index, { points: v ?? [] })"
      />
    </div>
    <div v-if="!readonly" class="outline-editor__actions">
      <el-button size="small" :icon="Plus" :disabled="items.length >= CONTENT_LIMITS.outlineItems" @click="add(2)">{{ tt("outline.addH2") }}</el-button>
      <el-button size="small" :icon="Plus" :disabled="!items.length || items.length >= CONTENT_LIMITS.outlineItems" @click="add(3)">{{ tt("outline.addH3") }}</el-button>
      <span class="text-secondary count">{{ items.length }} / {{ CONTENT_LIMITS.outlineItems }}</span>
    </div>
    <div v-if="error" class="outline-editor__error">{{ error }}</div>
  </div>
</template>

<style scoped>
.outline-editor {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.outline-editor__empty {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.outline-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
}
.outline-row.is-h3 {
  margin-left: 20px;
}
.outline-row.is-over {
  border-color: var(--el-color-primary);
  box-shadow: 0 -2px 0 var(--el-color-primary);
}
.outline-row__head {
  display: flex;
  align-items: center;
  gap: 4px;
}
.outline-row__head .el-button {
  margin-left: 0;
  padding: 4px;
}
.outline-row__handle {
  display: inline-flex;
  cursor: grab;
  color: var(--el-text-color-secondary);
}
.outline-row__no {
  min-width: 18px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-align: right;
}
.outline-row__points {
  margin-left: 22px;
  width: calc(100% - 22px);
}
.outline-editor__actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.outline-editor__actions .count {
  margin-left: auto;
  font-size: 12px;
}
.outline-editor__error {
  color: var(--el-color-danger);
  font-size: 12px;
}
</style>
