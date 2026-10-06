<script setup lang="ts">
// 可编辑字符串列表（平台规则 url_patterns / deleted_markers / redirect_markers，docs/11 §5.2、§11.4）：
// 逐条即时校验（validator 返回错误文案或 null），服务端 400 的 loc 序号错误通过 serverErrors 回显；条数上限 max。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { Delete, Plus } from "@element-plus/icons-vue";

const props = withDefaults(
  defineProps<{
    modelValue: string[];
    validator?: (value: string) => string | null;
    max?: number;
    placeholder?: string;
    /** 服务端校验错误：序号 → 文案 */
    serverErrors?: Record<number, string>;
    mono?: boolean;
    disabled?: boolean;
  }>(),
  { validator: undefined, max: 50, placeholder: "", serverErrors: () => ({}), mono: false, disabled: false },
);

const emit = defineEmits<{ (e: "update:modelValue", value: string[]): void }>();

const { t } = useI18n();

const errors = computed(() =>
  props.modelValue.map((v, i) => props.serverErrors[i] || (props.validator && v.trim() !== "" ? props.validator(v) : null) || ""),
);

function update(index: number, value: string) {
  const next = [...props.modelValue];
  next[index] = value;
  emit("update:modelValue", next);
}

function add() {
  if (props.modelValue.length >= props.max) return;
  emit("update:modelValue", [...props.modelValue, ""]);
}

function removeAt(index: number) {
  const next = [...props.modelValue];
  next.splice(index, 1);
  emit("update:modelValue", next);
}
</script>

<template>
  <div class="rule-list">
    <div v-for="(item, idx) in modelValue" :key="idx" class="rule-list__row">
      <div class="rule-list__field">
        <el-input
          :model-value="item"
          :placeholder="placeholder"
          :class="{ 'is-error': !!errors[idx], mono }"
          :disabled="disabled"
          size="small"
          @update:model-value="(v: string) => update(idx, v)"
        >
          <template #prepend>{{ idx + 1 }}</template>
        </el-input>
        <div v-if="errors[idx]" class="rule-list__error">{{ errors[idx] }}</div>
      </div>
      <el-button link type="danger" size="small" :icon="Delete" :disabled="disabled" :title="t('common.delete')" @click="removeAt(idx)" />
    </div>
    <div class="rule-list__footer">
      <el-button size="small" :icon="Plus" :disabled="disabled || modelValue.length >= max" @click="add">{{ t("platforms.form.addItem") }}</el-button>
      <span class="text-secondary">{{ t("platforms.form.itemCount", { n: modelValue.length, max }) }}</span>
    </div>
  </div>
</template>

<style scoped>
.rule-list {
  width: 100%;
}
.rule-list__row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-bottom: 6px;
}
.rule-list__field {
  flex: 1;
  min-width: 0;
}
.rule-list__field .is-error :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}
.rule-list__field .mono :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
}
.rule-list__error {
  margin-top: 2px;
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.4;
}
.rule-list__footer {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.rule-list__row .el-button {
  margin-top: 4px;
}
</style>
