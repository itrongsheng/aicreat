<script setup lang="ts">
// FAQ 列表编辑（docs/09 §8.4、§10.6）：`[{q, a}]`，q ≤ 200、a ≤ 1000 字符；增删与上下移动，随编辑器「保存」提交。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { ArrowDown, ArrowUp, Delete, Plus } from "@element-plus/icons-vue";
import { CONTENT_LIMITS, type FaqItem } from "@aicreat/shared";

const props = withDefaults(defineProps<{ modelValue: FaqItem[] | null | undefined; readonly?: boolean }>(), { readonly: false });
const emit = defineEmits<{ (e: "update:modelValue", value: FaqItem[]): void }>();

const { t } = useI18n();
const items = computed(() => props.modelValue ?? []);

function update(index: number, patch: Partial<FaqItem>) {
  emit(
    "update:modelValue",
    items.value.map((it, i) => (i === index ? { ...it, ...patch } : it)),
  );
}

function add() {
  emit("update:modelValue", [...items.value, { q: "", a: "" }]);
}

function remove(index: number) {
  emit(
    "update:modelValue",
    items.value.filter((_, i) => i !== index),
  );
}

function move(from: number, to: number) {
  if (to < 0 || to >= items.value.length) return;
  const next = [...items.value];
  const [it] = next.splice(from, 1);
  next.splice(to, 0, it);
  emit("update:modelValue", next);
}
</script>

<template>
  <div class="faq-editor">
    <div v-if="!items.length" class="text-secondary empty">{{ t("faq.empty") }}</div>
    <div v-for="(item, index) in items" :key="index" class="faq-item">
      <div class="faq-item__head">
        <span class="faq-item__no">Q{{ index + 1 }}</span>
        <el-input
          :model-value="item.q"
          size="small"
          :readonly="readonly"
          :maxlength="CONTENT_LIMITS.faqQuestion"
          :placeholder="t('faq.question')"
          @update:model-value="(v: string) => update(index, { q: v })"
        />
        <template v-if="!readonly">
          <el-button size="small" text :icon="ArrowUp" :disabled="index === 0" @click="move(index, index - 1)" />
          <el-button size="small" text :icon="ArrowDown" :disabled="index === items.length - 1" @click="move(index, index + 1)" />
          <el-button size="small" text type="danger" :icon="Delete" @click="remove(index)" />
        </template>
      </div>
      <el-input
        :model-value="item.a"
        type="textarea"
        :autosize="{ minRows: 2, maxRows: 6 }"
        :readonly="readonly"
        :maxlength="CONTENT_LIMITS.faqAnswer"
        show-word-limit
        :placeholder="t('faq.answer')"
        @update:model-value="(v: string) => update(index, { a: v })"
      />
    </div>
    <el-button v-if="!readonly" size="small" :icon="Plus" @click="add">{{ t("faq.add") }}</el-button>
  </div>
</template>

<style scoped>
.faq-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: stretch;
}
.faq-editor > .el-button {
  align-self: flex-start;
}
.empty {
  font-size: 13px;
}
.faq-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.faq-item__head {
  display: flex;
  align-items: center;
  gap: 4px;
}
.faq-item__head .el-button {
  margin-left: 0;
  padding: 4px;
}
.faq-item__no {
  flex: none;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
