<script setup lang="ts">
// 配置 JSON 编辑：本地解析校验，合法时回写 v-model；支持格式化、只读与后端校验错误列表展示
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { ValidationErrorItem } from "@aicreat/shared";

const props = withDefaults(
  defineProps<{
    modelValue: unknown;
    rows?: number;
    readonly?: boolean;
    /** 只接受 JSON 对象（settings 配置值） */
    objectOnly?: boolean;
    /** 后端 400 校验错误（04 §5.1） */
    errors?: ValidationErrorItem[];
    placeholder?: string;
  }>(),
  { rows: 18, readonly: false, objectOnly: false, errors: () => [], placeholder: "" },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: unknown): void;
  (e: "validity", valid: boolean): void;
}>();

const { t } = useI18n();

function stringify(value: unknown): string {
  if (value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "";
  }
}

const text = ref(stringify(props.modelValue));
const parseError = ref("");

watch(
  () => props.modelValue,
  (value) => {
    // 外部更新（加载、保存后回显）时同步文本；自身编辑产生的回写不覆盖用户排版
    let same = false;
    try {
      same = JSON.stringify(JSON.parse(text.value || "null")) === JSON.stringify(value ?? null);
    } catch {
      same = false;
    }
    if (!same) {
      text.value = stringify(value);
      parseError.value = "";
    }
  },
  { deep: true },
);

function parse(source: string): { ok: true; value: unknown } | { ok: false; message: string } {
  try {
    const value: unknown = source.trim() === "" ? null : JSON.parse(source);
    if (props.objectOnly && (value === null || typeof value !== "object" || Array.isArray(value))) {
      return { ok: false, message: t("settings.jsonMustBeObject") };
    }
    return { ok: true, value };
  } catch (err) {
    return { ok: false, message: t("settings.jsonInvalid", { message: err instanceof Error ? err.message : String(err) }) };
  }
}

function onInput(value: string) {
  text.value = value;
  const res = parse(value);
  if (res.ok) {
    parseError.value = "";
    emit("update:modelValue", res.value);
    emit("validity", true);
  } else {
    parseError.value = res.message;
    emit("validity", false);
  }
}

function format() {
  const res = parse(text.value);
  if (res.ok) text.value = stringify(res.value);
}

/** 提交前调用：返回是否合法 */
function validate(): boolean {
  const res = parse(text.value);
  parseError.value = res.ok ? "" : res.message;
  return res.ok;
}

const errorLines = computed(() =>
  props.errors.map((e) => {
    const loc = e.loc.filter((p, i) => !(i === 0 && (p === "body" || p === "query" || p === "path")));
    return { path: loc.join("."), msg: e.msg, type: e.type };
  }),
);

defineExpose({ validate, format });
</script>

<template>
  <div class="json-editor" :class="{ 'is-invalid': parseError || errors.length }">
    <div v-if="!readonly" class="json-editor__toolbar">
      <el-button size="small" text @click="format">{{ t("common.format") }}</el-button>
    </div>
    <el-input
      :model-value="text"
      type="textarea"
      :rows="rows"
      :readonly="readonly"
      :placeholder="placeholder"
      spellcheck="false"
      class="json-editor__input"
      @update:model-value="onInput"
    />
    <div v-if="parseError" class="json-editor__error">{{ parseError }}</div>
    <el-alert v-if="errorLines.length" type="error" :closable="false" class="json-editor__errors" :title="t('settings.errorsTitle')">
      <ul>
        <li v-for="(line, idx) in errorLines" :key="idx">
          <code v-if="line.path">{{ line.path }}</code>
          {{ line.msg }}
          <span class="json-editor__type">({{ line.type }})</span>
        </li>
      </ul>
    </el-alert>
  </div>
</template>

<style scoped>
.json-editor__toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 4px;
}
.json-editor__input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 13px;
  line-height: 1.5;
}
.json-editor.is-invalid .json-editor__input :deep(.el-textarea__inner) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}
.json-editor__error {
  margin-top: 4px;
  color: var(--el-color-danger);
  font-size: 12px;
}
.json-editor__errors {
  margin-top: 8px;
}
.json-editor__errors ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.json-editor__errors code {
  margin-right: 4px;
}
.json-editor__type {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
