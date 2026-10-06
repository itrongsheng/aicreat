<script setup lang="ts">
// Prompt 模板编辑器（docs/09 §5、§10.2、docs/04 §6.8、docs/13 §7.3、§12.2）：
// - 左侧：code（新建时填，不得以 sys_ 开头）、名称、kind（创建后只读）、语言、项目范围、说明、output_format、
//   model_params（temperature / max_tokens / top_p / stop）、output_schema（JsonEditor，仅 json）、变量表 PromptVariablesForm；
// - 右侧：system_prompt / user_prompt 编辑器（点击内置变量 chip 插入到最近聚焦的编辑器光标处）+ 预览面板
//   （POST /{id}/preview，示例变量可编辑，显示渲染后的 system / user；4221 缺失变量标红）；
// - 底部：同 code 的全部可见版本，可打开或与当前版本只读对比（VersionDiff）。
// published 模板保存时后端复制为同 code 新版本 draft（id 不同），据此提示「已创建 v{n} 草稿」并跳转；archived 只读。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import { ArrowLeft, Refresh, View } from "@element-plus/icons-vue";
import {
  PROMPT_BUILTIN_VARIABLES,
  PROMPT_CODE_PATTERN,
  PROMPT_KIND,
  PROMPT_KIND_CAPABILITY,
  PROMPT_OUTPUT_FORMAT,
  PROMPT_VARIABLE_PATTERN,
  SUPPORTED_LOCALES,
  SYSTEM_TEMPLATE_PREFIX,
  type ConflictData,
  type PromptKind,
  type PromptOutputFormat,
  type PromptPreviewResult,
  type PromptTemplate,
  type PromptTemplateCreateBody,
  type PromptVariable,
  type ValidationErrorItem,
} from "@aicreat/shared";
import { fieldErrors, isApiError, validationErrors } from "@/api/client";
import * as promptTemplatesApi from "@/api/promptTemplates";
import JsonEditor from "@/components/JsonEditor.vue";
import MarkdownEditor from "@/components/MarkdownEditor.vue";
import PromptVariablesForm from "@/components/PromptVariablesForm.vue";
import StatusTag from "@/components/StatusTag.vue";
import VersionDiff from "@/components/VersionDiff.vue";
import { usePermission } from "@/composables/usePermission";
import { useProjectStore } from "@/store/project";
import { formatDateTime } from "@/utils/format";

const { t } = useI18n();
const { has, isAllScope } = usePermission();
const route = useRoute();
const router = useRouter();
const projectStore = useProjectStore();

/** kind → 新建时的默认输出格式（docs/09 §5.2 输出列） */
const DEFAULT_OUTPUT_FORMAT: Record<PromptKind, PromptOutputFormat> = {
  keyword: "json",
  title: "json",
  outline: "json",
  content: "markdown",
  section: "markdown",
  rewrite: "markdown",
  expand: "markdown",
  shorten: "markdown",
  restyle: "markdown",
  seo_meta: "json",
  faq: "json",
  image_prompt: "text",
  geo_query: "text",
  seo_query: "json",
};
const KNOWN_PARAM_KEYS = ["temperature", "max_tokens", "top_p", "stop"];
/** 占位语法示例（模板中不能直接写双花括号字面量） */
const VAR_EXAMPLE = "{" + "{keyword}" + "}";

const isNew = computed(() => route.name === "prompt-template-new");
const templateId = computed(() => (isNew.value ? 0 : Number(route.params.id)));

const template = ref<PromptTemplate | null>(null);
const loading = ref(false);
const notFound = ref(false);
const saving = ref(false);

// ---------- 表单 ----------
const formRef = ref<FormInstance>();
const varsForm = ref<InstanceType<typeof PromptVariablesForm>>();
const schemaEditor = ref<InstanceType<typeof JsonEditor>>();
const schemaValid = ref(true);
const serverErrors = ref<Record<string, string>>({});
const schemaErrors = ref<ValidationErrorItem[]>([]);

const form = reactive({
  code: "",
  kind: "keyword" as PromptKind,
  name: "",
  description: "",
  language: "zh-CN" as string,
  project_id: 0,
  system_prompt: "",
  user_prompt: "",
  variables: [] as PromptVariable[],
  output_format: "json" as PromptOutputFormat,
  output_schema: null as unknown,
  temperature: undefined as number | undefined,
  max_tokens: undefined as number | undefined,
  top_p: undefined as number | undefined,
  stop: [] as string[],
  /** 其余未在表单展示的 model_params 键原样保留 */
  extraParams: {} as Record<string, unknown>,
});

const baseline = ref("");

function snapshot(): string {
  return JSON.stringify({ ...form, variables: form.variables.map((v) => ({ ...v })) });
}

const dirty = computed(() => baseline.value !== "" && snapshot() !== baseline.value);

const canCreate = computed(() => has("content.prompt_templates.create"));
const canUpdate = computed(() => has("content.prompt_templates.update"));
const canPublish = computed(() => has("content.prompt_templates.publish"));
const canDelete = computed(() => has("content.prompt_templates.delete"));

const status = computed(() => template.value?.status ?? "draft");
const readonly = computed(() => {
  if (isNew.value) return !canCreate.value;
  if (!template.value) return true;
  if (template.value.status === "archived") return true;
  return !canUpdate.value;
});
const isSystem = computed(() => !!template.value?.is_system);

const versions = ref<PromptTemplate[]>([]);
const versionsLoading = ref(false);
const nextVersion = computed(() => Math.max(template.value?.version ?? 0, ...versions.value.map((v) => v.version)) + 1);
const editingPublished = computed(() => !isNew.value && status.value === "published" && canUpdate.value);

const viewingOwner = computed(() => isAllScope.value && projectStore.ownerId > 0);
const builtins = computed(() => PROMPT_BUILTIN_VARIABLES[form.kind] ?? []);
const capability = computed(() => PROMPT_KIND_CAPABILITY[form.kind]);

const referenced = computed(() => {
  const names = new Set<string>();
  for (const text of [form.system_prompt, form.user_prompt]) {
    for (const m of (text ?? "").matchAll(PROMPT_VARIABLE_PATTERN)) names.add(m[1]);
  }
  return [...names];
});

const scopeOptions = computed(() => {
  const opts = [{ label: t("promptTemplates.scopeGlobal"), value: 0 }, ...projectStore.projects.map((p) => ({ label: p.name, value: p.id }))];
  if (form.project_id > 0 && !opts.some((o) => o.value === form.project_id)) opts.push({ label: `#${form.project_id}`, value: form.project_id });
  return opts;
});

const rules = computed<FormRules>(() => ({
  code: isNew.value
    ? [
        { required: true, message: t("promptTemplates.form.codeRequired"), trigger: "blur" },
        {
          validator: (_r: unknown, value: string, cb: (e?: Error) => void) => {
            if (value && value.startsWith(SYSTEM_TEMPLATE_PREFIX)) cb(new Error(t("promptTemplates.form.codeSysPrefix")));
            else if (value && !PROMPT_CODE_PATTERN.test(value)) cb(new Error(t("promptTemplates.form.codeRule")));
            else cb();
          },
          trigger: "blur",
        },
      ]
    : [],
  name: [
    { required: true, message: t("promptTemplates.form.nameRequired"), trigger: "blur" },
    { max: 100, message: t("promptTemplates.form.maxLength", { max: 100 }), trigger: "blur" },
  ],
  description: [{ max: 500, message: t("promptTemplates.form.maxLength", { max: 500 }), trigger: "blur" }],
  user_prompt: [{ required: true, message: t("promptTemplates.form.userPromptRequired"), trigger: "blur" }],
}));

function fillFrom(tpl: PromptTemplate) {
  form.code = tpl.code;
  form.kind = tpl.kind;
  form.name = tpl.name;
  form.description = tpl.description ?? "";
  form.language = tpl.language || "zh-CN";
  form.project_id = tpl.project_id;
  form.system_prompt = tpl.system_prompt ?? "";
  form.user_prompt = tpl.user_prompt ?? "";
  form.variables = (tpl.variables ?? []).map((v) => ({ ...v }));
  form.output_format = tpl.output_format;
  form.output_schema = tpl.output_schema ?? null;
  const params = { ...(tpl.model_params ?? {}) };
  form.temperature = typeof params.temperature === "number" ? params.temperature : undefined;
  form.max_tokens = typeof params.max_tokens === "number" ? params.max_tokens : undefined;
  form.top_p = typeof params.top_p === "number" ? params.top_p : undefined;
  form.stop = Array.isArray(params.stop) ? params.stop.map(String) : typeof params.stop === "string" ? [params.stop] : [];
  for (const k of KNOWN_PARAM_KEYS) delete params[k];
  form.extraParams = params;
  serverErrors.value = {};
  schemaErrors.value = [];
  schemaValid.value = true;
  baseline.value = snapshot();
}

function fillNew() {
  const qKind = route.query.kind as PromptKind | undefined;
  const kind = qKind && (PROMPT_KIND as readonly string[]).includes(qKind) ? qKind : "keyword";
  const qProject = Number(route.query.project_id);
  form.code = "";
  form.kind = kind;
  form.name = "";
  form.description = "";
  form.language = "zh-CN";
  form.project_id = Number.isInteger(qProject) && qProject > 0 ? qProject : 0;
  form.system_prompt = "";
  form.user_prompt = "";
  form.variables = [];
  form.output_format = DEFAULT_OUTPUT_FORMAT[kind];
  form.output_schema = null;
  form.temperature = undefined;
  form.max_tokens = undefined;
  form.top_p = undefined;
  form.stop = [];
  form.extraParams = {};
  serverErrors.value = {};
  schemaErrors.value = [];
  baseline.value = snapshot();
}

function onKindChange(kind: PromptKind) {
  if (isNew.value) form.output_format = DEFAULT_OUTPUT_FORMAT[kind];
}

function modelParams(): Record<string, unknown> | null {
  const out: Record<string, unknown> = { ...form.extraParams };
  if (typeof form.temperature === "number") out.temperature = form.temperature;
  if (typeof form.max_tokens === "number") out.max_tokens = form.max_tokens;
  if (typeof form.top_p === "number") out.top_p = form.top_p;
  const stop = form.stop.map((s) => s).filter((s) => s !== "");
  if (stop.length) out.stop = stop;
  return Object.keys(out).length ? out : null;
}

function bodyOf(): PromptTemplateCreateBody {
  return {
    code: form.code.trim(),
    kind: form.kind,
    name: form.name.trim(),
    description: form.description.trim() || null,
    language: form.language,
    project_id: form.project_id,
    system_prompt: form.system_prompt.trim() ? form.system_prompt : null,
    user_prompt: form.user_prompt,
    variables: form.variables.map((v) => ({
      name: (v.name ?? "").trim(),
      label: v.label ?? "",
      required: !!v.required,
      default: v.default == null ? "" : String(v.default),
    })),
    output_format: form.output_format,
    output_schema: form.output_format === "json" && form.output_schema && typeof form.output_schema === "object" ? (form.output_schema as Record<string, unknown>) : null,
    model_params: modelParams(),
  };
}

// ---------- 加载 ----------
async function load() {
  notFound.value = false;
  preview.value = null;
  previewMissing.value = [];
  if (isNew.value) {
    template.value = null;
    versions.value = [];
    fillNew();
    return;
  }
  const id = templateId.value;
  if (!Number.isInteger(id) || id <= 0) {
    notFound.value = true;
    return;
  }
  loading.value = true;
  try {
    template.value = await promptTemplatesApi.get(id, { silent: true });
    fillFrom(template.value);
    void loadVersions();
  } catch (err) {
    if (isApiError(err) && err.code === 404) notFound.value = true;
    else if (isApiError(err)) ElMessage.error(err.message);
  } finally {
    loading.value = false;
  }
}

async function loadVersions() {
  if (!template.value) return;
  versionsLoading.value = true;
  try {
    versions.value = [...(await promptTemplatesApi.versions(template.value.id, { silent: true }))].sort((a, b) => b.version - a.version);
  } catch {
    versions.value = [];
  } finally {
    versionsLoading.value = false;
  }
}

// ---------- 变量插入 ----------
const systemEditor = ref<InstanceType<typeof MarkdownEditor>>();
const userEditor = ref<InstanceType<typeof MarkdownEditor>>();
const lastFocused = ref<"system" | "user">("user");

function insertVariable(name: string) {
  const editor = lastFocused.value === "system" ? systemEditor.value : userEditor.value;
  void editor?.insert("{" + "{" + name + "}" + "}");
}

// ---------- 保存 / 发布 / 归档 / 删除 ----------
function applyErrors(err: unknown) {
  const items = validationErrors(err);
  serverErrors.value = fieldErrors(err);
  schemaErrors.value = items.filter((e) => e.loc[1] === "output_schema");
  const unknown = items.filter((e) => e.type === "unknown_variable");
  if (unknown.length) ElMessage.error(t("promptTemplates.unknownVariables", { names: unknown.map((e) => String(e.input ?? "")).join("、") }));
  else if (items.length) ElMessage.error(t("common.validationFailed"));
}

async function validateAll(): Promise<boolean> {
  let ok = true;
  try {
    await formRef.value?.validate();
  } catch {
    ok = false;
  }
  if (varsForm.value && !varsForm.value.validate()) {
    ok = false;
    ElMessage.error(t("promptTemplates.form.variablesInvalid"));
  }
  if (form.output_format === "json" && schemaEditor.value && !schemaEditor.value.validate()) ok = false;
  if (!schemaValid.value) ok = false;
  return ok;
}

/** 新草稿是全局模板而当前处于用户视角时，跳转前先回到全部用户（13 §12.2） */
async function leaveOwnerViewIfNeeded(tpl: PromptTemplate) {
  if (tpl.project_id === 0 && viewingOwner.value) await projectStore.setOwner(0);
}

/** 草稿保存的返回附带 `warnings`（未声明变量，docs/09 §5.3：草稿只告警，publish 时 400）：以服务端结果为准提示 */
function notifySaveWarnings(tpl: PromptTemplate) {
  const names = [...new Set((tpl.warnings ?? []).filter((w) => w.type === "unknown_variable").map((w) => String(w.input ?? "")))].filter(Boolean);
  if (names.length) ElMessage.warning({ message: t("promptTemplates.savedWithUnknownVariables", { names: names.join("、") }), duration: 6000 });
}

/** 保存：返回保存后的模板（新建或 published 复制时 id 变化并跳转）；失败返回 null */
async function save(silentSuccess = false): Promise<PromptTemplate | null> {
  if (readonly.value) return null;
  if (!(await validateAll())) return null;
  saving.value = true;
  serverErrors.value = {};
  schemaErrors.value = [];
  try {
    const body = bodyOf();
    if (isNew.value) {
      const created = await promptTemplatesApi.create(body, { silent: true });
      baseline.value = snapshot();
      if (!silentSuccess) {
        ElMessage.success(t("promptTemplates.createdDraft", { code: created.code }));
        notifySaveWarnings(created);
      }
      await leaveOwnerViewIfNeeded(created);
      await router.replace({ name: "prompt-template-edit", params: { id: created.id } });
      return created;
    }
    const current = template.value as PromptTemplate;
    const { code: _code, kind: _kind, ...update } = body;
    void _code;
    void _kind;
    const saved = await promptTemplatesApi.update(current.id, update, { silent: true });
    if (saved.id !== current.id) {
      baseline.value = snapshot();
      ElMessage.success(t("promptTemplates.newVersionDraft", { version: saved.version }));
      if (!silentSuccess) notifySaveWarnings(saved);
      await leaveOwnerViewIfNeeded(saved);
      await router.replace({ name: "prompt-template-edit", params: { id: saved.id } });
      return saved;
    }
    template.value = saved;
    fillFrom(saved);
    void loadVersions();
    if (!silentSuccess) {
      ElMessage.success(t("common.saved"));
      notifySaveWarnings(saved);
    }
    return saved;
  } catch (err) {
    if (!isApiError(err)) return null;
    if (err.code === 409) {
      const data = (err.data ?? {}) as ConflictData;
      if (data.reason === "owned_by_other") serverErrors.value = { code: t("promptTemplates.codeOwnedByOther") };
      else if (isNew.value) serverErrors.value = { code: t("promptTemplates.codeExists") };
      ElMessage.error(serverErrors.value.code ?? err.message);
    } else {
      applyErrors(err);
      if (!validationErrors(err).length) ElMessage.error(err.message);
    }
    return null;
  } finally {
    saving.value = false;
  }
}

const publishing = ref(false);

async function publish() {
  if (!template.value || status.value !== "draft") return;
  try {
    await ElMessageBox.confirm(t("promptTemplates.publishConfirm", { code: template.value.code, version: template.value.version }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  publishing.value = true;
  try {
    let target: PromptTemplate | null = template.value;
    if (dirty.value && !readonly.value) target = await save(true);
    if (!target) return;
    const published = await promptTemplatesApi.publish(target.id, { silent: true });
    template.value = published;
    fillFrom(published);
    void loadVersions();
    ElMessage.success(t("promptTemplates.published"));
  } catch (err) {
    if (isApiError(err)) {
      applyErrors(err);
      if (!validationErrors(err).length) ElMessage.error(err.message);
    }
  } finally {
    publishing.value = false;
  }
}

async function archive() {
  const tpl = template.value;
  if (!tpl || tpl.status !== "published") return;
  try {
    await ElMessageBox.confirm(t("promptTemplates.archiveConfirm", { code: tpl.code, version: tpl.version }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  try {
    const archived = await promptTemplatesApi.archive(tpl.id, { silent: true });
    template.value = archived;
    fillFrom(archived);
    void loadVersions();
    ElMessage.success(t("promptTemplates.archived"));
  } catch (err) {
    if (isApiError(err)) {
      const data = (err.data ?? {}) as ConflictData;
      ElMessage.error(err.code === 409 && data.reason === "last_published" ? t("promptTemplates.lastPublished") : err.message);
    }
  }
}

async function remove() {
  const tpl = template.value;
  if (!tpl) return;
  try {
    await ElMessageBox.confirm(t("promptTemplates.deleteConfirm", { code: tpl.code, version: tpl.version }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  try {
    await promptTemplatesApi.remove(tpl.id);
    baseline.value = snapshot();
    ElMessage.success(t("promptTemplates.deleted"));
    void router.push({ name: "prompt-templates" });
  } catch {
    /* 拦截器已提示 */
  }
}

// ---------- 预览 ----------
const previewValues = ref<Record<string, string>>({});
const preview = ref<PromptPreviewResult | null>(null);
const previewing = ref(false);
const previewMissing = ref<string[]>([]);

async function runPreview() {
  if (!template.value) return;
  previewing.value = true;
  previewMissing.value = [];
  try {
    preview.value = await promptTemplatesApi.preview(template.value.id, previewValues.value, { silent: true });
  } catch (err) {
    preview.value = null;
    if (!isApiError(err)) return;
    if (err.code === 4221) {
      const missing = (err.data as { missing?: string[] } | null)?.missing ?? [];
      previewMissing.value = missing;
      ElMessage.error(t("promptTemplates.preview.missing", { names: missing.join("、") }));
    } else ElMessage.error(err.message);
  } finally {
    previewing.value = false;
  }
}

// ---------- 版本对比 ----------
const diffVisible = ref(false);
const diffBase = ref<PromptTemplate | null>(null);
const diffTab = ref<"user" | "system" | "settings">("user");

function settingsText(tpl: PromptTemplate | null, useForm = false): string {
  if (useForm) {
    const b = bodyOf();
    return JSON.stringify(
      { name: b.name, description: b.description, language: b.language, project_id: b.project_id, output_format: b.output_format, variables: b.variables, output_schema: b.output_schema, model_params: b.model_params },
      null,
      2,
    );
  }
  if (!tpl) return "";
  return JSON.stringify(
    {
      name: tpl.name,
      description: tpl.description,
      language: tpl.language,
      project_id: tpl.project_id,
      output_format: tpl.output_format,
      variables: tpl.variables,
      output_schema: tpl.output_schema,
      model_params: tpl.model_params,
    },
    null,
    2,
  );
}

function openDiff(v: PromptTemplate) {
  diffBase.value = v;
  diffTab.value = "user";
  diffVisible.value = true;
}

const currentLabel = computed(() => (dirty.value ? t("promptTemplates.diff.currentEditing", { version: template.value?.version ?? "" }) : `v${template.value?.version ?? ""}`));

function openVersion(v: PromptTemplate) {
  void router.push({ name: "prompt-template-edit", params: { id: v.id } });
}

// ---------- 生命周期 ----------
onMounted(() => {
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void load();
});

watch(
  () => [route.name, route.params.id],
  ([name], [prevName]) => {
    if (name === "prompt-template-new" || name === "prompt-template-edit" || prevName === "prompt-template-new") void load();
  },
);

async function confirmLeave(): Promise<boolean> {
  if (!dirty.value || readonly.value) return true;
  try {
    await ElMessageBox.confirm(t("promptTemplates.leaveConfirm"), t("common.tip"), { type: "warning" });
    return true;
  } catch {
    return false;
  }
}

onBeforeRouteLeave(confirmLeave);
// 同一路由内切换版本（/prompt-templates/:id → 另一 id）
onBeforeRouteUpdate(confirmLeave);

function back() {
  void router.push({ name: "prompt-templates" });
}
</script>

<template>
  <el-card v-if="notFound" shadow="never" class="page-card">
    <el-result icon="warning" :title="t('promptTemplates.notFound')">
      <template #extra>
        <el-button type="primary" :icon="ArrowLeft" @click="back">{{ t("promptTemplates.backToList") }}</el-button>
      </template>
    </el-result>
  </el-card>

  <template v-else>
    <el-card v-loading="loading" shadow="never" class="page-card">
      <template #header>
        <div class="page-header">
          <div class="title-line">
            <el-button :icon="ArrowLeft" circle size="small" @click="back" />
            <h2 class="page-title">
              {{ isNew ? t("menu.promptTemplateNew") : template ? `${template.code} · v${template.version}` : t("menu.promptTemplateEdit") }}
            </h2>
            <template v-if="template">
              <StatusTag kind="prompt_status" :value="template.status" />
              <el-tag v-if="template.is_system" size="small" type="warning" effect="plain">{{ t("promptTemplates.systemTag") }}</el-tag>
              <el-tag size="small" type="info" effect="plain">{{ t("promptTemplates.capability") }}：{{ t(`status.capability.${capability}`) }}</el-tag>
            </template>
            <el-tag v-if="dirty" size="small" type="warning">{{ t("promptTemplates.unsaved") }}</el-tag>
          </div>
          <div class="header-actions">
            <el-button v-if="!isNew" :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
            <el-button v-if="!isNew && status === 'published' && canPublish" type="warning" plain @click="archive">{{ t("promptTemplates.archive") }}</el-button>
            <el-button v-if="!isNew && status === 'draft' && !isSystem && canDelete" type="danger" plain @click="remove">{{ t("common.delete") }}</el-button>
            <el-button v-if="!isNew && status === 'draft' && canPublish" type="success" :loading="publishing" @click="publish">{{ t("promptTemplates.publish") }}</el-button>
            <el-button v-if="!readonly" type="primary" :loading="saving" :disabled="!isNew && !dirty" @click="save()">
              {{ editingPublished ? t("promptTemplates.saveAsDraft", { version: nextVersion }) : t("common.save") }}
            </el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="editingPublished" type="info" :closable="false" show-icon :title="t('promptTemplates.publishedEditHint', { version: nextVersion })" class="hint-alert" />
      <el-alert v-if="status === 'archived' && !isNew" type="info" :closable="false" show-icon :title="t('promptTemplates.archivedHint')" class="hint-alert" />
      <el-alert v-if="!isNew && status === 'draft' && !canPublish" type="info" :closable="false" show-icon :title="t('promptTemplates.noPublishHint')" class="hint-alert" />
      <el-alert
        v-if="!readonly && form.project_id === 0 && viewingOwner"
        type="warning"
        :closable="false"
        show-icon
        :title="t('promptTemplates.ownerViewHint')"
        class="hint-alert"
      />
      <el-alert
        v-else-if="!readonly && form.project_id === 0 && !isAllScope && (isNew || editingPublished)"
        type="info"
        :closable="false"
        show-icon
        :title="t('promptTemplates.ownDraftHint')"
        class="hint-alert"
      />

      <el-row :gutter="16">
        <!-- 左：基本信息 / 参数 / 变量 -->
        <el-col :xs="24" :lg="10">
          <el-form ref="formRef" :model="form" :rules="rules" label-width="104px" :disabled="readonly" @submit.prevent>
            <el-form-item :label="t('promptTemplates.code')" prop="code" :error="serverErrors.code">
              <el-input v-if="isNew" v-model="form.code" maxlength="80" class="mono-input" :placeholder="t('promptTemplates.form.codePlaceholder')" />
              <span v-else class="mono">{{ form.code }}</span>
              <div v-if="isNew" class="form-hint">{{ t("promptTemplates.form.codeHint") }}</div>
            </el-form-item>
            <el-form-item :label="t('promptTemplates.name')" prop="name" :error="serverErrors.name">
              <el-input v-model="form.name" maxlength="100" show-word-limit />
            </el-form-item>
            <el-form-item :label="t('promptTemplates.kind')" prop="kind" :error="serverErrors.kind">
              <el-select v-if="isNew" v-model="form.kind" filterable style="width: 100%" @change="onKindChange">
                <el-option v-for="k in PROMPT_KIND" :key="k" :label="`${t(`status.prompt_kind.${k}`)} (${k})`" :value="k" />
              </el-select>
              <template v-else>
                <StatusTag kind="prompt_kind" :value="form.kind" effect="plain" />
                <span class="mono text-secondary inline-gap">{{ form.kind }}</span>
              </template>
            </el-form-item>
            <el-form-item :label="t('promptTemplates.language')" :error="serverErrors.language">
              <el-select v-model="form.language" style="width: 100%">
                <el-option v-for="l in SUPPORTED_LOCALES" :key="l" :label="l" :value="l" />
              </el-select>
            </el-form-item>
            <el-form-item :label="t('promptTemplates.scope')" :error="serverErrors.project_id">
              <el-select v-model="form.project_id" filterable :disabled="readonly || isSystem" style="width: 100%">
                <el-option v-for="o in scopeOptions" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <div class="form-hint">{{ t("promptTemplates.form.scopeHint") }}</div>
            </el-form-item>
            <el-form-item :label="t('promptTemplates.description')" prop="description" :error="serverErrors.description">
              <el-input v-model="form.description" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" maxlength="500" show-word-limit />
            </el-form-item>
            <el-form-item :label="t('promptTemplates.outputFormat')" :error="serverErrors.output_format">
              <el-radio-group v-model="form.output_format">
                <el-radio-button v-for="f in PROMPT_OUTPUT_FORMAT" :key="f" :value="f">{{ t(`status.prompt_output_format.${f}`) }}</el-radio-button>
              </el-radio-group>
            </el-form-item>

            <el-divider content-position="left">{{ t("promptTemplates.modelParams") }}</el-divider>
            <el-form-item label="temperature" :error="serverErrors.model_params">
              <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" :precision="2" controls-position="right" :placeholder="t('promptTemplates.form.followRoute')" />
            </el-form-item>
            <el-form-item label="max_tokens">
              <el-input-number v-model="form.max_tokens" :min="1" :max="200000" :step="256" controls-position="right" :placeholder="t('promptTemplates.form.followRoute')" />
            </el-form-item>
            <el-form-item label="top_p">
              <el-input-number v-model="form.top_p" :min="0.01" :max="1" :step="0.05" :precision="2" controls-position="right" :placeholder="t('promptTemplates.form.followRoute')" />
            </el-form-item>
            <el-form-item label="stop">
              <el-select v-model="form.stop" multiple :multiple-limit="4" filterable allow-create default-first-option :reserve-keyword="false" :placeholder="t('promptTemplates.form.stopPlaceholder')" style="width: 100%" />
            </el-form-item>
            <div class="form-hint params-hint">
              {{ t("promptTemplates.form.paramsHint") }}
              <template v-if="Object.keys(form.extraParams).length">
                <br />{{ t("promptTemplates.form.extraParams") }}<span class="mono">{{ JSON.stringify(form.extraParams) }}</span>
              </template>
            </div>

            <template v-if="form.output_format === 'json'">
              <el-divider content-position="left">{{ t("promptTemplates.outputSchema") }}</el-divider>
              <el-form-item label-width="0" :error="schemaErrors.length ? undefined : serverErrors.output_schema">
                <div class="full">
                  <JsonEditor
                    ref="schemaEditor"
                    v-model="form.output_schema"
                    :rows="10"
                    object-only
                    nullable
                    :readonly="readonly"
                    :errors="schemaErrors"
                    :placeholder="t('promptTemplates.form.schemaPlaceholder')"
                    @validity="(v: boolean) => (schemaValid = v)"
                  />
                  <div class="form-hint">{{ t("promptTemplates.form.schemaHint") }}</div>
                </div>
              </el-form-item>
            </template>
          </el-form>

          <el-divider content-position="left">{{ t("promptTemplates.variables") }}</el-divider>
          <div v-if="serverErrors.variables" class="row-error">{{ serverErrors.variables }}</div>
          <PromptVariablesForm
            ref="varsForm"
            v-model:variables="form.variables"
            mode="define"
            :builtins="builtins"
            :referenced="referenced"
            :readonly="readonly"
            @insert="insertVariable"
          />
        </el-col>

        <!-- 右：提示词与预览 -->
        <el-col :xs="24" :lg="14">
          <div class="prompt-block">
            <div class="prompt-label">system_prompt</div>
            <MarkdownEditor
              ref="systemEditor"
              v-model="form.system_prompt"
              format="text"
              monospace
              :rows="8"
              :readonly="readonly"
              :invalid="!!serverErrors.system_prompt"
              :placeholder="t('promptTemplates.form.systemPlaceholder')"
              @focusin="lastFocused = 'system'"
            />
            <div v-if="serverErrors.system_prompt" class="row-error">{{ serverErrors.system_prompt }}</div>
          </div>
          <div class="prompt-block">
            <div class="prompt-label">user_prompt <span class="required">*</span></div>
            <MarkdownEditor
              ref="userEditor"
              v-model="form.user_prompt"
              format="text"
              monospace
              :rows="14"
              :readonly="readonly"
              :invalid="!!serverErrors.user_prompt"
              :placeholder="t('promptTemplates.form.userPlaceholder')"
              @focusin="lastFocused = 'user'"
            />
            <div v-if="serverErrors.user_prompt" class="row-error">{{ serverErrors.user_prompt }}</div>
            <div class="form-hint">{{ t("promptTemplates.form.placeholderHint", { example: VAR_EXAMPLE }) }}</div>
          </div>

          <el-card shadow="never" class="preview-card">
            <template #header>
              <div class="page-header">
                <span class="section-title">{{ t("promptTemplates.preview.title") }}</span>
                <el-button type="primary" plain size="small" :icon="View" :loading="previewing" :disabled="isNew || !template" @click="runPreview">
                  {{ t("promptTemplates.preview.run") }}
                </el-button>
              </div>
            </template>
            <el-alert v-if="isNew" type="info" :closable="false" :title="t('promptTemplates.preview.saveFirst')" />
            <template v-else>
              <el-alert v-if="dirty" type="warning" :closable="false" show-icon :title="t('promptTemplates.preview.dirtyHint')" class="hint-alert" />
              <el-collapse>
                <el-collapse-item :title="t('promptTemplates.preview.variables')" name="vars">
                  <PromptVariablesForm
                    v-model:values="previewValues"
                    mode="values"
                    :variables="template?.variables ?? []"
                    :builtins="PROMPT_BUILTIN_VARIABLES[template?.kind ?? form.kind]"
                    :missing="previewMissing"
                  />
                </el-collapse-item>
              </el-collapse>
              <div v-if="preview" class="preview-result">
                <div class="prompt-label">system</div>
                <pre class="preview-pre">{{ preview.system_prompt ?? t("promptTemplates.preview.noSystem") }}</pre>
                <div class="prompt-label">user</div>
                <pre class="preview-pre">{{ preview.user_prompt }}</pre>
              </div>
              <el-empty v-else :image-size="48" :description="t('promptTemplates.preview.empty')" />
            </template>
          </el-card>
        </el-col>
      </el-row>
    </el-card>

    <!-- 版本列表 -->
    <el-card v-if="!isNew && template" shadow="never" class="page-card">
      <template #header>
        <div class="page-header">
          <span class="section-title">{{ t("promptTemplates.versionsTitle", { code: template.code }) }}</span>
          <el-button :icon="Refresh" size="small" :loading="versionsLoading" @click="loadVersions">{{ t("common.refresh") }}</el-button>
        </div>
      </template>
      <el-table v-loading="versionsLoading" :data="versions" size="small" border>
        <el-table-column :label="t('promptTemplates.version')" width="90">
          <template #default="{ row }">
            v{{ row.version }}
            <el-tag v-if="row.id === template.id" size="small" effect="plain">{{ t("promptTemplates.currentTag") }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('promptTemplates.name')" min-width="160" prop="name" show-overflow-tooltip />
        <el-table-column :label="t('promptTemplates.status')" width="90">
          <template #default="{ row }"><StatusTag kind="prompt_status" :value="row.status" /></template>
        </el-table-column>
        <el-table-column :label="t('promptTemplates.scope')" width="120">
          <template #default="{ row }">
            {{ row.project_id === 0 ? t("promptTemplates.scopeGlobal") : (projectStore.projects.find((p) => p.id === row.project_id)?.name ?? `#${row.project_id}`) }}
          </template>
        </el-table-column>
        <el-table-column :label="t('promptTemplates.publishedAt')" width="165">
          <template #default="{ row }">{{ formatDateTime(row.published_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('promptTemplates.updated')" width="165">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="150">
          <template #default="{ row }">
            <el-button v-if="row.id !== template.id" link type="primary" @click="openVersion(row)">{{ t("promptTemplates.open") }}</el-button>
            <el-button link type="primary" :disabled="row.id === template.id && !dirty" @click="openDiff(row)">{{ t("promptTemplates.compare") }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 版本对比（只读） -->
    <el-dialog v-model="diffVisible" :title="t('promptTemplates.diff.title')" width="min(1100px, 96vw)" top="6vh">
      <el-tabs v-if="diffBase" v-model="diffTab">
        <el-tab-pane label="user_prompt" name="user">
          <VersionDiff :old-text="diffBase.user_prompt" :new-text="form.user_prompt" :old-label="`v${diffBase.version}`" :new-label="currentLabel" max-height="60vh" />
        </el-tab-pane>
        <el-tab-pane label="system_prompt" name="system">
          <VersionDiff :old-text="diffBase.system_prompt ?? ''" :new-text="form.system_prompt" :old-label="`v${diffBase.version}`" :new-label="currentLabel" max-height="60vh" />
        </el-tab-pane>
        <el-tab-pane :label="t('promptTemplates.diff.settings')" name="settings">
          <VersionDiff :old-text="settingsText(diffBase)" :new-text="settingsText(null, true)" :old-label="`v${diffBase.version}`" :new-label="currentLabel" max-height="60vh" />
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
  </template>
</template>

<style scoped>
.title-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.header-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.hint-alert {
  margin-bottom: 12px;
}
.inline-gap {
  margin-left: 8px;
}
.full {
  width: 100%;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.params-hint {
  margin: -8px 0 8px 104px;
  width: auto;
}
.row-error {
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.4;
}
.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
.prompt-block {
  margin-bottom: 14px;
}
.prompt-label {
  margin: 0 0 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.prompt-label .required {
  color: var(--el-color-danger);
}
.section-title {
  font-size: 14px;
  font-weight: 600;
}
.preview-card :deep(.el-card__header) {
  padding: 8px 12px;
}
.preview-result {
  margin-top: 10px;
}
.preview-pre {
  margin: 0 0 10px;
  padding: 10px 12px;
  max-height: 360px;
  overflow: auto;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
@media (max-width: 1199px) {
  .params-hint {
    margin-left: 0;
  }
}
</style>
