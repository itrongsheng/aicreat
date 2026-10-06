<script setup lang="ts">
// 用户视角切换器（顶栏，仅总后台渲染）与项目表单「负责人」下拉（v-model 模式）。
// 选项来自 GET /admin/projects/owner-options（缓存在 store/project），顶栏模式首项「全部用户」（值 0）。
import { computed, getCurrentInstance, onMounted } from "vue";
import { useI18n } from "vue-i18n";
import { useProjectStore } from "@/store/project";

const props = withDefaults(
  defineProps<{
    /** 不传时绑定全局用户视角 ownerId */
    modelValue?: number | null;
    /** 是否提供「全部用户」（值 0），顶栏模式默认 true */
    allowAll?: boolean;
    /** 表单模式下是否隐藏已禁用用户（转移负责人须为启用用户） */
    activeOnly?: boolean;
    placeholder?: string;
    width?: string;
    size?: "large" | "default" | "small";
    disabled?: boolean;
  }>(),
  { modelValue: undefined, allowAll: undefined, activeOnly: false, placeholder: "", width: "180px", size: "default", disabled: false },
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
const showAll = computed(() => props.allowAll ?? isGlobal.value);
const options = computed(() => (props.activeOnly ? store.owners.filter((o) => o.is_active) : store.owners));

const value = computed<number | undefined>({
  get: () => {
    const v = isGlobal.value ? store.ownerId : (props.modelValue ?? 0);
    if (!v && !showAll.value) return undefined;
    return v;
  },
  set: (v) => {
    const next = typeof v === "number" && v > 0 ? v : 0;
    if (isGlobal.value) void store.setOwner(next);
    else emit("update:modelValue", next);
    emit("change", next);
  },
});

onMounted(() => {
  if (!store.ownersLoaded && !store.loadingOwners) store.loadOwners().catch(() => undefined);
});
</script>

<template>
  <el-select
    v-model="value"
    filterable
    :loading="store.loadingOwners"
    :placeholder="placeholder || t('scope.ownerPlaceholder')"
    :size="size"
    :disabled="disabled"
    :style="{ width }"
  >
    <el-option v-if="showAll" :label="t('scope.allUsers')" :value="0" />
    <el-option v-for="o in options" :key="o.id" :label="o.display_name || o.username" :value="o.id">
      <div class="owner-option">
        <span class="owner-name">{{ o.display_name || o.username }}</span>
        <span class="owner-meta">
          <el-tag v-if="!o.is_active" size="small" type="info">{{ t("scope.ownerDisabled") }}</el-tag>
          <span>{{ t("scope.projectCount", { count: o.project_count }) }}</span>
        </span>
      </div>
    </el-option>
  </el-select>
</template>

<style scoped>
.owner-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.owner-meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
