<script setup lang="ts">
// 按模板变量定义渲染表单（docs/02 §3.6、docs/09 §5.3、§10.2）。两种模式：
// - define：编辑变量声明 `variables[]`（name/label/required/default），内置变量以只读 chip 列出、点击插入 `{{name}}`（emit insert）；
//   列出提示词中引用但未声明且非内置的变量（publish 时会 400 unknown_variable），可一键补声明；
// - values：按「内置变量 + 声明变量」渲染取值输入（模板预览的示例变量、生成抽屉），v-model 为 {name: value}。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { Delete, Plus } from "@element-plus/icons-vue";
import type { PromptVariable } from "@aicreat/shared";

const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;
/** 长文本变量用多行输入 */
const LONG_VARIABLES = new Set(["text", "body", "outline", "section", "previous_text", "brand_info", "instruction", "summary"]);

const props = withDefaults(
  defineProps<{
    mode?: "define" | "values";
    /** define 模式：变量声明；values 模式：用于渲染声明变量的标签 / 必填 / 默认值 */
    variables?: PromptVariable[];
    /** values 模式的取值 */
    values?: Record<string, string>;
    /** 当前 kind 的内置变量名 */
    builtins?: readonly string[];
    /** define 模式：提示词中引用到的变量名（用于提示未声明 / 未使用） */
    referenced?: string[];
    readonly?: boolean;
    /** values 模式：是否列出内置变量的输入框（预览用；生成抽屉只需要声明变量） */
    includeBuiltins?: boolean;
    /** 后端返回的缺失变量（4221 data.missing），values 模式标红 */
    missing?: string[];
  }>(),
  {
    mode: "define",
    variables: () => [],
    values: () => ({}),
    builtins: () => [],
    referenced: () => [],
    readonly: false,
    includeBuiltins: true,
    missing: () => [],
  },
);

const emit = defineEmits<{
  (e: "update:variables", value: PromptVariable[]): void;
  (e: "update:values", value: Record<string, string>): void;
  (e: "insert", name: string): void;
}>();

const { t } = useI18n();

const builtinSet = computed(() => new Set(props.builtins));

// ---------- define ----------
function update(index: number, patch: Partial<PromptVariable>) {
  const next = props.variables.map((v, i) => (i === index ? { ...v, ...patch } : v));
  emit("update:variables", next);
}

function add(name = "") {
  emit("update:variables", [...props.variables, { name, label: "", required: false, default: "" }]);
}

function removeAt(index: number) {
  emit("update:variables", props.variables.filter((_, i) => i !== index));
}

/** 每行的校验错误 */
const rowErrors = computed(() => {
  const seen = new Map<string, number>();
  return props.variables.map((v, i) => {
    const name = (v.name ?? "").trim();
    if (!name) return t("promptVars.nameRequired");
    if (!NAME_RE.test(name)) return t("promptVars.nameInvalid");
    if (seen.has(name)) return t("promptVars.nameDuplicate");
    seen.set(name, i);
    return "";
  });
});

/** 每行的提示（非阻断） */
function rowHint(v: PromptVariable): string {
  const name = (v.name ?? "").trim();
  if (!name) return "";
  if (builtinSet.value.has(name)) return t("promptVars.builtinDeclared");
  if (v.required && !(v.default ?? "").toString().trim()) return t("promptVars.requiredNoDefault");
  if (!v.required && !(v.default ?? "").toString().trim()) return t("promptVars.customNeedsDefault");
  if (props.referenced.length && !props.referenced.includes(name)) return t("promptVars.unused");
  return "";
}

const declared = computed(() => new Set(props.variables.map((v) => (v.name ?? "").trim()).filter(Boolean)));
const undeclared = computed(() => [...new Set(props.referenced)].filter((n) => !builtinSet.value.has(n) && !declared.value.has(n)));
const usedBuiltins = computed(() => new Set(props.referenced.filter((n) => builtinSet.value.has(n))));

/** `{{name}}` 占位文本 */
function placeholderOf(name: string): string {
  return "{" + "{" + name + "}" + "}";
}

function validate(): boolean {
  return rowErrors.value.every((e) => !e);
}

// ---------- values ----------
interface ValueField {
  name: string;
  label: string;
  required: boolean;
  placeholder: string;
  builtin: boolean;
  long: boolean;
}

const valueFields = computed<ValueField[]>(() => {
  const out: ValueField[] = [];
  const seen = new Set<string>();
  if (props.includeBuiltins) {
    for (const name of props.builtins) {
      seen.add(name);
      const decl = props.variables.find((v) => v.name === name);
      out.push({
        name,
        label: decl?.label || name,
        required: false,
        placeholder: decl?.default ? String(decl.default) : t("promptVars.builtinPlaceholder"),
        builtin: true,
        long: LONG_VARIABLES.has(name),
      });
    }
  }
  for (const v of props.variables) {
    const name = (v.name ?? "").trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({
      name,
      label: v.label || name,
      required: !!v.required,
      placeholder: v.default ? String(v.default) : "",
      builtin: builtinSet.value.has(name),
      long: LONG_VARIABLES.has(name),
    });
  }
  return out;
});

function setValue(name: string, value: string) {
  const next = { ...props.values };
  if (value === "") delete next[name];
  else next[name] = value;
  emit("update:values", next);
}

defineExpose({ validate });
</script>

<template>
  <!-- 变量声明 -->
  <div v-if="mode === 'define'" class="pv">
    <div v-if="builtins.length" class="pv__builtins">
      <span class="pv__caption">{{ t("promptVars.builtins") }}</span>
      <el-tooltip :content="readonly ? t('promptVars.builtinTipReadonly') : t('promptVars.builtinTip')" placement="top">
        <span class="pv__chips">
          <el-tag
            v-for="name in builtins"
            :key="name"
            size="small"
            :type="usedBuiltins.has(name) ? 'primary' : 'info'"
            :effect="usedBuiltins.has(name) ? 'light' : 'plain'"
            class="pv__chip"
            :class="{ 'is-clickable': !readonly }"
            @click="!readonly && emit('insert', name)"
          >
            {{ placeholderOf(name) }}
          </el-tag>
        </span>
      </el-tooltip>
    </div>

    <el-alert v-if="undeclared.length" type="warning" :closable="false" show-icon class="pv__alert">
      <template #title>
        {{ t("promptVars.undeclared", { names: undeclared.join("、") }) }}
        <template v-if="!readonly">
          <el-button v-for="name in undeclared" :key="name" link type="primary" size="small" @click="add(name)">
            {{ t("promptVars.declare", { name }) }}
          </el-button>
        </template>
      </template>
    </el-alert>

    <el-table :data="variables" size="small" border class="pv__table" :empty-text="t('promptVars.empty')">
      <el-table-column :label="t('promptVars.name')" min-width="150">
        <template #default="{ row, $index }">
          <el-input
            :model-value="row.name"
            size="small"
            :disabled="readonly"
            class="mono-input"
            :placeholder="t('promptVars.namePlaceholder')"
            @update:model-value="(v: string) => update($index, { name: v.trim() })"
          />
          <div v-if="rowErrors[$index]" class="pv__error">{{ rowErrors[$index] }}</div>
          <div v-else-if="rowHint(row)" class="pv__hint">{{ rowHint(row) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('promptVars.label')" min-width="120">
        <template #default="{ row, $index }">
          <el-input :model-value="row.label ?? ''" size="small" :disabled="readonly" @update:model-value="(v: string) => update($index, { label: v })" />
        </template>
      </el-table-column>
      <el-table-column :label="t('promptVars.required')" width="70" align="center">
        <template #default="{ row, $index }">
          <el-checkbox :model-value="!!row.required" :disabled="readonly" @update:model-value="(v) => update($index, { required: !!v })" />
        </template>
      </el-table-column>
      <el-table-column :label="t('promptVars.default')" min-width="160">
        <template #default="{ row, $index }">
          <el-input
            :model-value="row.default == null ? '' : String(row.default)"
            size="small"
            :disabled="readonly"
            @update:model-value="(v: string) => update($index, { default: v })"
          />
        </template>
      </el-table-column>
      <el-table-column v-if="!readonly" width="54" align="center">
        <template #default="{ $index }">
          <el-button :icon="Delete" size="small" circle type="danger" plain @click="removeAt($index)" />
        </template>
      </el-table-column>
    </el-table>
    <div v-if="!readonly" class="pv__actions">
      <el-button :icon="Plus" size="small" @click="add()">{{ t("promptVars.add") }}</el-button>
      <span class="pv__hint">{{ t("promptVars.defineHint") }}</span>
    </div>
  </div>

  <!-- 变量取值 -->
  <el-form v-else label-position="top" class="pv pv--values" @submit.prevent>
    <el-empty v-if="!valueFields.length" :image-size="48" :description="t('promptVars.noValues')" />
    <el-form-item
      v-for="f in valueFields"
      :key="f.name"
      :required="f.required"
      :error="missing.includes(f.name) ? t('promptVars.missingValue') : undefined"
      class="pv__field"
    >
      <template #label>
        <span class="pv__field-label">
          <span>{{ f.label }}</span>
          <code v-if="f.label !== f.name" class="pv__field-name">{{ f.name }}</code>
          <el-tag v-if="f.builtin" size="small" type="info" effect="plain">{{ t("promptVars.builtinTag") }}</el-tag>
        </span>
      </template>
      <el-input
        :model-value="values[f.name] ?? ''"
        :type="f.long ? 'textarea' : 'text'"
        :autosize="f.long ? { minRows: 2, maxRows: 8 } : undefined"
        :placeholder="f.placeholder"
        :disabled="readonly"
        clearable
        @update:model-value="(v: string) => setValue(f.name, v)"
      />
    </el-form-item>
  </el-form>
</template>

<style scoped>
.pv {
  width: 100%;
}
.pv__builtins {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
}
.pv__caption {
  flex: none;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 24px;
}
.pv__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.pv__chip {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
.pv__chip.is-clickable {
  cursor: pointer;
}
.pv__alert {
  margin-bottom: 8px;
}
.pv__table :deep(.mono-input input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
.pv__error {
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.4;
}
.pv__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.4;
}
.pv__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.pv__field {
  margin-bottom: 12px;
}
.pv__field-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.pv__field-name {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
