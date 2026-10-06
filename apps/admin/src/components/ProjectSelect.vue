<script setup lang="ts">
// 项目选择器：顶栏全局（不传 modelValue，绑定 store/project.currentId）或表单内（v-model）。
// 只列可见的 active 项目；总后台选了用户视角时只列该用户的项目。
import { computed, getCurrentInstance, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useProjectStore } from "@/store/project";

const props = withDefaults(
  defineProps<{
    /** 不传时绑定全局当前项目 */
    modelValue?: number | null;
    /** 是否提供「全部项目」（值 0） */
    allowAll?: boolean;
    clearable?: boolean;
    placeholder?: string;
    width?: string;
    size?: "large" | "default" | "small";
    disabled?: boolean;
  }>(),
  { modelValue: undefined, allowAll: true, clearable: false, placeholder: "", width: "200px", size: "default", disabled: false },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: number): void;
  (e: "change", value: number): void;
}>();

const store = useProjectStore();
const { t } = useI18n();

// 父组件未绑定 v-model（vnode 上无 modelValue）时为全局模式；绑定了值为 undefined 的 v-model 仍是表单模式
const vnodeProps = getCurrentInstance()?.vnode.props ?? {};
const bound = "modelValue" in vnodeProps || "model-value" in vnodeProps;
const isGlobal = computed(() => !bound);

const value = computed<number | undefined>({
  get: () => {
    const v = isGlobal.value ? store.currentId : (props.modelValue ?? 0);
    if (!v && !props.allowAll) return undefined;
    return v;
  },
  set: (v) => {
    const next = typeof v === "number" && v > 0 ? v : 0;
    if (isGlobal.value) store.setCurrent(next);
    else emit("update:modelValue", next);
    emit("change", next);
  },
});

onMounted(() => {
  if (!store.projectsLoaded && !store.loadingProjects) store.load().catch(() => undefined);
});
</script>

<template>
  <el-select
    v-model="value"
    filterable
    :clearable="clearable"
    :loading="store.loadingProjects"
    :placeholder="placeholder || t('project.placeholder')"
    :no-data-text="t('project.empty')"
    :size="size"
    :disabled="disabled"
    :style="{ width }"
  >
    <el-option v-if="allowAll" :label="t('project.allProjects')" :value="0" />
    <el-option v-for="p in store.projects" :key="p.id" :label="p.name" :value="p.id" />
  </el-select>
</template>
