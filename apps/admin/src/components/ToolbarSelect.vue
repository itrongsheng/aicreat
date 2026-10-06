<script setup lang="ts">
// 列表页筛选下拉：传 enum（StatusTag 枚举名）自动生成选项，或直接传 options
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { statusOptions, type StatusEnumName } from "@/components/StatusTag.vue";

type Value = string | number | boolean | null | undefined;

export interface ToolbarOption {
  label: string;
  value: string | number | boolean;
  disabled?: boolean;
}

const props = withDefaults(
  defineProps<{
    modelValue: Value;
    enumName?: StatusEnumName;
    options?: ToolbarOption[];
    placeholder?: string;
    clearable?: boolean;
    filterable?: boolean;
    width?: string;
    size?: "large" | "default" | "small";
    disabled?: boolean;
  }>(),
  { enumName: undefined, options: undefined, placeholder: "", clearable: true, filterable: false, width: "160px", size: "default", disabled: false },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: Value): void;
  (e: "change", value: Value): void;
}>();

const { t, locale } = useI18n();

const items = computed<ToolbarOption[]>(() => {
  void locale.value;
  if (props.options) return props.options;
  return props.enumName ? statusOptions(props.enumName) : [];
});

const inner = computed({
  get: () => (props.modelValue === null ? undefined : props.modelValue),
  set: (v: Value) => {
    const next = v === "" ? undefined : v;
    emit("update:modelValue", next);
    emit("change", next);
  },
});
</script>

<template>
  <el-select
    v-model="inner"
    :placeholder="placeholder || t('common.select')"
    :clearable="clearable"
    :filterable="filterable"
    :size="size"
    :disabled="disabled"
    :style="{ width }"
  >
    <el-option v-for="opt in items" :key="String(opt.value)" :label="opt.label" :value="opt.value" :disabled="opt.disabled" />
  </el-select>
</template>
