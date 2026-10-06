<script setup lang="ts">
// 关键词页（docs/09 §6、§10.3；docs/04 §6.9、§7.4）：
// - 顶部状态 Tab（全部 / 候选 / 采用 / 弃用）、筛选 intent / keyword_type / batch_id / 关键词搜索、排序（分数 / 创建时间，默认 sort=score）；
// - 生成抽屉：种子词（1~20）、数量（runtime 默认 / 上限）、竞品、受众（默认项目受众）、模板（has content.prompt_templates.view）、
//   模型（has ai.models.view）；提交后抽屉内 BatchProgress 每 3s 轮询批次直到终态，成功后列表按 batch_id 筛选；
// - 导入：粘贴（JSON / 每行一个，前端转 items[]，缺省 intent=unknown、keyword_type=core，≤ 5,000 条）与上传 CSV（import-file，提供模板下载），
//   结果展示 created / skipped / errors[]；
// - 手工新增、编辑、采用 / 弃用 / 恢复（单条与批量 batch-status）、生成标题（跳转标题页并预选该关键词）、删除（仅无标题 / 内容）、导出 CSV；
// - 429 / 4291 / 5031 / 4221 按 useGenerateGuard 提示，quota_warning 显示黄色横幅；未选项目时显示空态。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules, type UploadFile } from "element-plus";
import { Delete, Download, InfoFilled, MagicStick, Plus, Refresh, Search, Upload } from "@element-plus/icons-vue";
import {
  BATCH_IDS_MAX,
  DEFAULT_PAGE_SIZE,
  KEYWORD_GENERATE_LIMITS,
  KEYWORD_IMPORT_FILE_MAX_BYTES,
  KEYWORD_IMPORT_MAX,
  KEYWORD_INTENT,
  KEYWORD_LIMITS,
  KEYWORD_TYPE,
  type AdoptAction,
  type ConflictData,
  type GenerationBatch,
  type Keyword,
  type KeywordImportItem,
  type KeywordImportResult,
  type KeywordIntent,
  type KeywordStatus,
  type KeywordType,
  type KeywordUpdateBody,
} from "@aicreat/shared";
import { fieldErrors, isApiError, validationErrors } from "@/api/client";
import * as keywordsApi from "@/api/keywords";
import BatchProgress from "@/components/BatchProgress.vue";
import GenerateNotice from "@/components/GenerateNotice.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import TemplateSelect from "@/components/TemplateSelect.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import { useGenerateGuard } from "@/composables/useGenerateGuard";
import { usePermission } from "@/composables/usePermission";
import { useProject } from "@/composables/useProject";
import { useRuntimeSettings } from "@/composables/useRuntimeSettings";
import { datedFilename, downloadBlob, downloadText } from "@/utils/download";
import { formatDateTime, formatNumber } from "@/utils/format";

type StatusTab = "all" | KeywordStatus;

const { t } = useI18n();
const { has } = usePermission();
const { projectId, project, store: projectStore } = useProject();
const route = useRoute();
const router = useRouter();

const canGenerate = computed(() => has("content.keywords.generate") && has("content.batches.view"));
const canStatus = computed(() => has("content.keywords.status"));

// ---------- 列表 ----------
const statusTab = ref<StatusTab>("all");
const filters = reactive({
  keyword: "",
  intent: undefined as KeywordIntent | undefined,
  keyword_type: undefined as KeywordType | undefined,
  batch_id: undefined as number | undefined,
  sort: "score" as "score" | "created_at",
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<Keyword[]>([]);
const loading = ref(false);
const selection = ref<Keyword[]>([]);
let loadSeq = 0;

function queryParams() {
  return {
    project_id: projectId.value,
    status: statusTab.value === "all" ? undefined : statusTab.value,
    intent: filters.intent,
    keyword_type: filters.keyword_type,
    keyword: filters.keyword.trim() || undefined,
    batch_id: filters.batch_id || undefined,
    sort: filters.sort,
    order: "desc" as const,
  };
}

async function load() {
  if (!projectId.value) {
    rows.value = [];
    total.value = 0;
    return;
  }
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const res = await keywordsApi.list({ ...queryParams(), page: page.value, page_size: pageSize.value });
    if (seq !== loadSeq) return;
    rows.value = res.items;
    total.value = res.total;
  } catch {
    if (seq !== loadSeq) return;
    rows.value = [];
    total.value = 0;
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.keyword = "";
  filters.intent = undefined;
  filters.keyword_type = undefined;
  filters.batch_id = undefined;
  filters.sort = "score";
  statusTab.value = "all";
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

watch(projectId, () => {
  filters.batch_id = undefined;
  selection.value = [];
  search();
});

// ---------- 单条状态动作 ----------
const acting = ref<number | null>(null);

async function act(row: Keyword, action: AdoptAction) {
  if (action === "discard") {
    try {
      await ElMessageBox.confirm(t("keywords.discardConfirm", { keyword: row.keyword }), t("common.tip"), { type: "warning" });
    } catch {
      return;
    }
  }
  acting.value = row.id;
  try {
    const updated = await keywordsApi[action](row.id);
    Object.assign(row, updated);
    ElMessage.success(t(`keywords.actionDone.${action}`));
    if (statusTab.value !== "all") void load();
  } catch {
    /* 拦截器已提示（409 current_status） */
  } finally {
    acting.value = null;
  }
}

// ---------- 批量 ----------
const batchActing = ref(false);

async function batchAct(action: AdoptAction) {
  const ids = selection.value.map((r) => r.id);
  if (!ids.length) return;
  if (ids.length > BATCH_IDS_MAX) {
    ElMessage.warning(t("keywords.batchTooMany", { max: BATCH_IDS_MAX }));
    return;
  }
  if (action === "discard") {
    try {
      await ElMessageBox.confirm(t("keywords.batchDiscardConfirm", { count: ids.length }), t("common.tip"), { type: "warning" });
    } catch {
      return;
    }
  }
  batchActing.value = true;
  try {
    const res = await keywordsApi.batchStatus(ids, action);
    const msg = t("keywords.batchResult", { updated: res.updated, skipped: res.skipped.length });
    if (res.skipped.length) ElMessage.warning(msg);
    else ElMessage.success(msg);
    void load();
  } catch {
    /* 已提示 */
  } finally {
    batchActing.value = false;
  }
}

// ---------- 删除 ----------
async function removeKeyword(row: Keyword) {
  try {
    await ElMessageBox.confirm(t("keywords.deleteConfirm", { keyword: row.keyword }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await keywordsApi.remove(row.id, { silent: true });
    ElMessage.success(t("keywords.deleted"));
    void load();
  } catch (err) {
    if (isApiError(err)) ElMessage.error(err.code === 409 ? t("keywords.deleteInUse") : err.message);
  } finally {
    acting.value = null;
  }
}

function gotoTitles(row: Keyword) {
  void router.push({ path: "/titles", query: { keyword_id: String(row.id) } });
}

// ---------- 导出 ----------
const exporting = ref(false);

async function exportCsv() {
  if (!projectId.value) return;
  exporting.value = true;
  try {
    const { blob, filename } = await keywordsApi.exportKeywords(queryParams());
    downloadBlob(blob, filename || datedFilename("keywords", "csv"));
  } catch {
    /* 已提示（超过 50,000 行 400） */
  } finally {
    exporting.value = false;
  }
}

// ---------- 生成抽屉 ----------
const { generation, load: loadRuntime } = useRuntimeSettings();
const guard = useGenerateGuard();
const genVisible = ref(false);
const genBatchId = ref(0);
const genBatch = ref<GenerationBatch | null>(null);
const genForm = reactive({
  seeds: [] as string[],
  count: 20,
  competitors: [] as string[],
  audience: "",
  template_id: null as number | null,
  model: null as string | null,
});
const genErrors = ref<Record<string, string>>({});

async function openGenerate() {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  const gen = await loadRuntime();
  if (!genBatchId.value || (genBatch.value && ["succeeded", "partial", "failed", "cancelled"].includes(genBatch.value.status))) {
    resetGenerateForm(gen.keyword.default_count);
  }
  genVisible.value = true;
}

function resetGenerateForm(count = generation.value.keyword.default_count) {
  guard.reset();
  genBatchId.value = 0;
  genBatch.value = null;
  genErrors.value = {};
  genForm.seeds = [];
  genForm.count = count;
  genForm.competitors = [];
  genForm.audience = project.value?.audience ?? "";
  genForm.template_id = null;
  genForm.model = null;
}

function normalizeList(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values) {
    const s = v.trim().replace(/\s+/g, " ");
    if (!s) continue;
    const key = s.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(s);
  }
  return out;
}

function validateGenerate(): boolean {
  const errs: Record<string, string> = {};
  const seeds = normalizeList(genForm.seeds);
  const L = KEYWORD_GENERATE_LIMITS;
  if (!seeds.length) errs.seeds = t("keywords.gen.seedsRequired");
  else if (seeds.length > L.seeds) errs.seeds = t("keywords.gen.seedsTooMany", { max: L.seeds });
  else if (seeds.some((s) => s.length > L.seedLength)) errs.seeds = t("keywords.gen.itemTooLong", { max: L.seedLength });
  const competitors = normalizeList(genForm.competitors);
  if (competitors.length > L.competitors) errs.competitors = t("keywords.gen.competitorsTooMany", { max: L.competitors });
  else if (competitors.some((s) => s.length > L.competitorLength)) errs.competitors = t("keywords.gen.itemTooLong", { max: L.competitorLength });
  const max = generation.value.keyword.max_count;
  if (!genForm.count || genForm.count < 1 || genForm.count > max) errs.count = t("keywords.gen.countRange", { max });
  if (genForm.audience.length > L.audience) errs.audience = t("keywords.gen.audienceTooLong", { max: L.audience });
  genErrors.value = errs;
  return !Object.keys(errs).length;
}

const genFieldErrors = computed(() => ({ ...guard.fields.value, ...genErrors.value }));

async function submitGenerate() {
  if (!validateGenerate()) return;
  const res = await guard.run(() =>
    keywordsApi.generate(
      {
        project_id: projectId.value,
        seeds: normalizeList(genForm.seeds),
        count: genForm.count,
        competitors: normalizeList(genForm.competitors),
        audience: genForm.audience.trim() || null,
        ...(has("content.prompt_templates.view") && genForm.template_id ? { template_id: genForm.template_id } : {}),
        ...(has("ai.models.view") && genForm.model ? { model: genForm.model } : {}),
      },
      { silent: true },
    ),
  );
  if (!res) return;
  genBatchId.value = res.batch_id;
  ElMessage.success(t("keywords.gen.queued", { id: res.batch_id }));
}

function onBatchFinished(batch: GenerationBatch) {
  genBatch.value = batch;
  if (batch.status === "succeeded" || batch.status === "partial") showBatch(batch.id, false);
}

/** 列表按 batch_id 筛选展示新词 */
function showBatch(batchId: number, close = true) {
  filters.batch_id = batchId;
  statusTab.value = "all";
  search();
  if (close) genVisible.value = false;
}

// ---------- 手工新增 ----------
const createVisible = ref(false);
const createSaving = ref(false);
const createFormRef = ref<FormInstance>();
const createErrors = ref<Record<string, string>>({});
const createForm = reactive({ keyword: "", intent: "unknown" as KeywordIntent, keyword_type: "core" as KeywordType });

const keywordRules = computed<FormRules>(() => ({
  keyword: [
    { required: true, whitespace: true, message: t("keywords.form.keywordRequired"), trigger: "blur" },
    { max: KEYWORD_LIMITS.keyword, message: t("keywords.form.keywordTooLong", { max: KEYWORD_LIMITS.keyword }), trigger: "blur" },
  ],
}));

function openCreate() {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  createForm.keyword = "";
  createForm.intent = "unknown";
  createForm.keyword_type = "core";
  createErrors.value = {};
  createVisible.value = true;
  createFormRef.value?.clearValidate();
}

function conflictMessage(err: unknown): string | null {
  if (!isApiError(err) || err.code !== 409) return null;
  const data = (err.data ?? {}) as ConflictData;
  if (data.existing_id) return t("keywords.form.duplicate", { id: data.existing_id });
  return err.message;
}

async function submitCreate() {
  try {
    await createFormRef.value?.validate();
  } catch {
    return;
  }
  createSaving.value = true;
  createErrors.value = {};
  try {
    await keywordsApi.create(
      { project_id: projectId.value, keyword: createForm.keyword.trim(), intent: createForm.intent, keyword_type: createForm.keyword_type },
      { silent: true },
    );
    ElMessage.success(t("keywords.created"));
    createVisible.value = false;
    void load();
  } catch (err) {
    const conflict = conflictMessage(err);
    if (conflict) createErrors.value = { keyword: conflict };
    else if (isApiError(err)) {
      createErrors.value = fieldErrors(err);
      ElMessage.error(err.message);
    }
  } finally {
    createSaving.value = false;
  }
}

// ---------- 编辑 ----------
const editVisible = ref(false);
const editSaving = ref(false);
const editing = ref<Keyword | null>(null);
const editFormRef = ref<FormInstance>();
const editErrors = ref<Record<string, string>>({});
const editForm = reactive({
  keyword: "",
  intent: "unknown" as KeywordIntent,
  keyword_type: "core" as KeywordType,
  difficulty: null as number | null,
  heat: null as number | null,
  score: null as number | null,
  tags: [] as string[],
});

function openEdit(row: Keyword) {
  editing.value = row;
  editForm.keyword = row.keyword;
  editForm.intent = row.intent;
  editForm.keyword_type = row.keyword_type;
  editForm.difficulty = row.difficulty;
  editForm.heat = row.heat;
  editForm.score = row.score;
  editForm.tags = [...(row.tags ?? [])];
  editErrors.value = {};
  editVisible.value = true;
  editFormRef.value?.clearValidate();
}

async function submitEdit() {
  const row = editing.value;
  if (!row) return;
  try {
    await editFormRef.value?.validate();
  } catch {
    return;
  }
  const tags = normalizeList(editForm.tags);
  if (tags.length > KEYWORD_LIMITS.tags || tags.some((x) => x.length > KEYWORD_LIMITS.tagLength)) {
    editErrors.value = { tags: t("keywords.form.tagsRule", { max: KEYWORD_LIMITS.tags, len: KEYWORD_LIMITS.tagLength }) };
    return;
  }
  // 只提交变化的字段
  const body: KeywordUpdateBody = {};
  if (editForm.keyword.trim() !== row.keyword) body.keyword = editForm.keyword.trim();
  if (editForm.intent !== row.intent) body.intent = editForm.intent;
  if (editForm.keyword_type !== row.keyword_type) body.keyword_type = editForm.keyword_type;
  if (editForm.difficulty !== row.difficulty) body.difficulty = editForm.difficulty;
  if (editForm.heat !== row.heat) body.heat = editForm.heat;
  if (editForm.score !== row.score) body.score = editForm.score;
  if (JSON.stringify(tags) !== JSON.stringify(row.tags ?? [])) body.tags = tags;
  if (!Object.keys(body).length) {
    editVisible.value = false;
    return;
  }
  editSaving.value = true;
  editErrors.value = {};
  try {
    const updated = await keywordsApi.update(row.id, body, { silent: true });
    Object.assign(row, updated);
    ElMessage.success(t("common.saved"));
    editVisible.value = false;
  } catch (err) {
    const conflict = conflictMessage(err);
    if (conflict) editErrors.value = { keyword: conflict };
    else if (isApiError(err)) {
      editErrors.value = fieldErrors(err);
      ElMessage.error(err.message);
    }
  } finally {
    editSaving.value = false;
  }
}

// ---------- 导入 ----------
const importVisible = ref(false);
const importTab = ref<"paste" | "csv">("paste");
const importText = ref("");
const importFile = ref<File | null>(null);
const importing = ref(false);
const importResult = ref<KeywordImportResult | null>(null);
const importError = ref("");

function openImport() {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  importText.value = "";
  importFile.value = null;
  importResult.value = null;
  importError.value = "";
  importVisible.value = true;
}

/** 粘贴内容 → items[]：JSON 数组（字符串或 {keyword,intent?,keyword_type?}）或每行一个关键词 */
function parsePaste(text: string): KeywordImportItem[] {
  const src = text.trim();
  if (!src) return [];
  if (src.startsWith("[")) {
    let data: unknown;
    try {
      data = JSON.parse(src);
    } catch {
      throw new Error(t("keywords.import.invalidJson"));
    }
    if (!Array.isArray(data)) throw new Error(t("keywords.import.invalidJson"));
    return data.map((item) => {
      if (typeof item === "string") return { keyword: item, intent: "unknown", keyword_type: "core" };
      const obj = (item ?? {}) as Record<string, unknown>;
      return {
        keyword: typeof obj.keyword === "string" ? obj.keyword : String(obj.keyword ?? ""),
        intent: typeof obj.intent === "string" && obj.intent ? obj.intent : "unknown",
        keyword_type: typeof obj.keyword_type === "string" && obj.keyword_type ? obj.keyword_type : "core",
      };
    });
  }
  return src
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((keyword) => ({ keyword, intent: "unknown", keyword_type: "core" }));
}

const pasteCount = computed(() => {
  try {
    return parsePaste(importText.value).length;
  } catch {
    return 0;
  }
});

function onFileChange(file: UploadFile) {
  importError.value = "";
  const raw = file.raw ?? null;
  if (raw && raw.size > KEYWORD_IMPORT_FILE_MAX_BYTES) {
    importError.value = t("keywords.import.fileTooLarge");
    importFile.value = null;
    return;
  }
  importFile.value = raw;
}

function onFileRemove() {
  importFile.value = null;
}

function downloadTemplate() {
  const csv = "﻿keyword,intent,keyword_type\n智能门锁怎么选,informational,question\n指纹锁品牌推荐,commercial,long_tail\n智能门锁,,\n";
  downloadText(csv, "keywords-import-template.csv", "text/csv;charset=utf-8");
}

async function submitImport() {
  importError.value = "";
  importResult.value = null;
  let call: () => Promise<KeywordImportResult>;
  if (importTab.value === "paste") {
    let items: KeywordImportItem[];
    try {
      items = parsePaste(importText.value);
    } catch (e) {
      importError.value = (e as Error).message;
      return;
    }
    if (!items.length) {
      importError.value = t("keywords.import.empty");
      return;
    }
    if (items.length > KEYWORD_IMPORT_MAX) {
      importError.value = t("keywords.import.tooMany", { max: KEYWORD_IMPORT_MAX });
      return;
    }
    call = () => keywordsApi.importKeywords(projectId.value, items, { silent: true });
  } else {
    const file = importFile.value;
    if (!file) {
      importError.value = t("keywords.import.chooseFile");
      return;
    }
    call = () => keywordsApi.importFile(projectId.value, file, { silent: true });
  }
  importing.value = true;
  try {
    importResult.value = await call();
    ElMessage.success(t("keywords.import.done", { created: importResult.value.created }));
    void load();
  } catch (err) {
    if (isApiError(err)) {
      const msgs = validationErrors(err).slice(0, 5).map((i) => i.msg);
      importError.value = msgs.length ? `${err.message}：${msgs.join("；")}` : err.message;
    }
  } finally {
    importing.value = false;
  }
}

// ---------- 进入页面 ----------
onMounted(() => {
  const batchId = Number(route.query.batch_id);
  if (Number.isInteger(batchId) && batchId > 0) filters.batch_id = batchId;
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void load();
});

const tabs: StatusTab[] = ["all", "candidate", "adopted", "discarded"];
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.keywords") }}</h2>
        <div v-if="projectId" class="header-actions">
          <el-button v-if="canGenerate" type="primary" :icon="MagicStick" @click="openGenerate">{{ t("keywords.generate") }}</el-button>
          <el-button v-permission="'content.keywords.import'" :icon="Upload" @click="openImport">{{ t("keywords.importBtn") }}</el-button>
          <el-button v-permission="'content.keywords.create'" :icon="Plus" @click="openCreate">{{ t("keywords.create") }}</el-button>
          <el-button :icon="Download" :loading="exporting" @click="exportCsv">{{ t("keywords.exportCsv") }}</el-button>
        </div>
      </div>
    </template>

    <el-empty v-if="!projectId" :description="t('generation.noProject')">
      <router-link v-if="has('content.projects.view')" to="/projects">
        <el-button type="primary">{{ t("generation.gotoProjects") }}</el-button>
      </router-link>
    </el-empty>

    <template v-else>
      <el-tabs v-model="statusTab" class="status-tabs" @tab-change="search">
        <el-tab-pane v-for="tab in tabs" :key="tab" :name="tab" :label="tab === 'all' ? t('common.all') : t(`status.keyword_status.${tab}`)" />
      </el-tabs>

      <div class="toolbar">
        <el-input v-model="filters.keyword" :placeholder="t('keywords.searchPlaceholder')" clearable style="width: 200px" @keyup.enter="search" @clear="search" />
        <ToolbarSelect v-model="filters.intent" enum-name="keyword_intent" :placeholder="t('keywords.intent')" width="130px" @change="search" />
        <ToolbarSelect v-model="filters.keyword_type" enum-name="keyword_type" :placeholder="t('keywords.keywordType')" width="130px" @change="search" />
        <el-input-number v-model="filters.batch_id" :min="1" :controls="false" :placeholder="t('keywords.batchId')" style="width: 110px" @change="search" />
        <el-select v-model="filters.sort" style="width: 140px" @change="search">
          <el-option value="score" :label="t('keywords.sortScore')" />
          <el-option value="created_at" :label="t('keywords.sortCreated')" />
        </el-select>
        <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
        <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
        <span class="spacer" />
        <el-button :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
      </div>

      <div v-if="canStatus && selection.length" class="batch-bar">
        <span>{{ t("keywords.selected", { count: selection.length }) }}</span>
        <el-button size="small" type="success" plain :loading="batchActing" @click="batchAct('adopt')">{{ t("keywords.batchAdopt") }}</el-button>
        <el-button size="small" type="warning" plain :loading="batchActing" @click="batchAct('discard')">{{ t("keywords.batchDiscard") }}</el-button>
        <el-button size="small" plain :loading="batchActing" @click="batchAct('restore')">{{ t("keywords.batchRestore") }}</el-button>
      </div>

      <el-alert
        v-if="filters.batch_id"
        type="info"
        :title="t('keywords.batchFilterOn', { id: filters.batch_id })"
        :closable="true"
        show-icon
        class="filter-alert"
        @close="
          filters.batch_id = undefined;
          search();
        "
      />

      <el-table v-loading="loading" :data="rows" row-key="id" stripe @selection-change="(v: Keyword[]) => (selection = v)">
        <el-table-column v-if="canStatus" type="selection" width="44" />
        <el-table-column :label="t('keywords.keyword')" min-width="200">
          <template #default="{ row }">
            <span class="kw">{{ row.keyword }}</span>
            <el-tooltip v-if="row.reason" :content="row.reason" placement="top" :show-after="200">
              <el-icon class="reason-icon"><InfoFilled /></el-icon>
            </el-tooltip>
            <div v-if="row.tags?.length" class="tags">
              <el-tag v-for="tag in row.tags" :key="tag" size="small" effect="plain" type="info">{{ tag }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('keywords.intent')" width="100">
          <template #default="{ row }"><StatusTag kind="keyword_intent" :value="row.intent" effect="plain" /></template>
        </el-table-column>
        <el-table-column :label="t('keywords.keywordType')" width="90">
          <template #default="{ row }">{{ t(`status.keyword_type.${row.keyword_type}`) }}</template>
        </el-table-column>
        <el-table-column :label="t('keywords.difficulty')" width="70" align="right">
          <template #default="{ row }">{{ row.difficulty ?? "-" }}</template>
        </el-table-column>
        <el-table-column :label="t('keywords.heat')" width="70" align="right">
          <template #default="{ row }">{{ row.heat ?? "-" }}</template>
        </el-table-column>
        <el-table-column :label="t('keywords.score')" width="80" align="right">
          <template #default="{ row }"><b>{{ row.score == null ? "-" : formatNumber(row.score, 2) }}</b></template>
        </el-table-column>
        <el-table-column :label="t('keywords.seed')" min-width="100">
          <template #default="{ row }">{{ row.seed || "-" }}</template>
        </el-table-column>
        <el-table-column :label="t('keywords.source')" width="90">
          <template #default="{ row }">{{ t(`status.keyword_source.${row.source}`) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.status')" width="90">
          <template #default="{ row }"><StatusTag kind="keyword_status" :value="row.status" /></template>
        </el-table-column>
        <el-table-column :label="t('keywords.titleCount')" width="80" align="right">
          <template #default="{ row }">
            <el-link v-if="row.title_count" type="primary" :underline="false" @click="gotoTitles(row)">{{ row.title_count }}</el-link>
            <span v-else>0</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.createdAt')" width="160">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="250" fixed="right">
          <template #default="{ row }">
            <template v-if="canStatus">
              <el-button v-if="row.status === 'candidate'" link type="success" :loading="acting === row.id" @click="act(row, 'adopt')">{{ t("keywords.adopt") }}</el-button>
              <el-button v-if="row.status !== 'discarded'" link type="warning" :loading="acting === row.id" @click="act(row, 'discard')">{{ t("keywords.discard") }}</el-button>
              <el-button v-else link type="primary" :loading="acting === row.id" @click="act(row, 'restore')">{{ t("keywords.restore") }}</el-button>
            </template>
            <el-button v-permission="'content.keywords.update'" link type="primary" @click="openEdit(row)">{{ t("common.edit") }}</el-button>
            <el-button v-if="has('content.titles.view')" link type="primary" :disabled="row.status === 'discarded'" @click="gotoTitles(row)">{{ t("keywords.genTitles") }}</el-button>
            <span v-permission="'content.keywords.delete'">
              <el-tooltip :content="t('keywords.deleteTip')" :disabled="!row.title_count && !row.content_count" placement="top">
                <el-button link type="danger" :icon="Delete" :disabled="!!row.title_count || !!row.content_count" @click="removeKeyword(row)" />
              </el-tooltip>
            </span>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :page-sizes="[20, 50, 100]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="load"
          @size-change="onPageSizeChange"
        />
      </div>
    </template>

    <!-- 生成抽屉 -->
    <el-drawer v-model="genVisible" :title="t('keywords.gen.title')" size="min(560px, 96vw)" :close-on-click-modal="false">
      <GenerateNotice :notice="guard.notice.value" :quota-warning="guard.quotaWarning.value" @close="guard.notice.value = null" />
      <template v-if="!genBatchId">
        <el-form label-position="top" @submit.prevent="submitGenerate">
          <el-form-item :label="t('keywords.gen.seeds')" :error="genFieldErrors.seeds" required>
            <el-input-tag
              v-model="genForm.seeds"
              :max="KEYWORD_GENERATE_LIMITS.seeds"
              :placeholder="t('keywords.gen.seedsPlaceholder')"
              clearable
              trigger="Enter"
              delimiter=","
            />
            <div class="form-hint">{{ t("keywords.gen.seedsHint", { max: KEYWORD_GENERATE_LIMITS.seeds, len: KEYWORD_GENERATE_LIMITS.seedLength }) }}</div>
          </el-form-item>
          <el-form-item :label="t('keywords.gen.count')" :error="genFieldErrors.count" required>
            <el-input-number v-model="genForm.count" :min="1" :max="generation.keyword.max_count" controls-position="right" />
            <span class="form-hint inline">{{ t("keywords.gen.countHint", { max: generation.keyword.max_count }) }}</span>
          </el-form-item>
          <el-form-item :label="t('keywords.gen.competitors')" :error="genFieldErrors.competitors">
            <el-input-tag
              v-model="genForm.competitors"
              :max="KEYWORD_GENERATE_LIMITS.competitors"
              :placeholder="t('keywords.gen.competitorsPlaceholder')"
              clearable
              trigger="Enter"
              delimiter=","
            />
          </el-form-item>
          <el-form-item :label="t('keywords.gen.audience')" :error="genFieldErrors.audience">
            <el-input v-model="genForm.audience" :maxlength="KEYWORD_GENERATE_LIMITS.audience" show-word-limit :placeholder="t('keywords.gen.audiencePlaceholder')" />
          </el-form-item>
          <el-form-item v-if="has('content.prompt_templates.view')" :label="t('generation.template')" :error="genFieldErrors.template_id">
            <TemplateSelect v-model="genForm.template_id" :kinds="['keyword']" :project-id="projectId" :project-defaults="project?.default_templates" />
          </el-form-item>
          <el-form-item v-if="has('ai.models.view')" :label="t('generation.model')" :error="genFieldErrors.model">
            <ModelSelect v-model="genForm.model" modality="text" allow-empty />
          </el-form-item>
        </el-form>
      </template>
      <template v-else>
        <BatchProgress :batch-id="genBatchId" @finished="onBatchFinished" />
      </template>
      <template #footer>
        <template v-if="!genBatchId">
          <el-button @click="genVisible = false">{{ t("common.cancel") }}</el-button>
          <el-button type="primary" :loading="guard.submitting.value" :disabled="guard.cooldown.value > 0" @click="submitGenerate">
            {{ guard.cooldown.value > 0 ? t("generation.retryIn", { seconds: guard.cooldown.value }) : t("keywords.gen.submit") }}
          </el-button>
        </template>
        <template v-else>
          <el-button @click="resetGenerateForm()">{{ t("generation.again") }}</el-button>
          <el-button type="primary" @click="showBatch(genBatchId)">{{ t("keywords.gen.showBatch") }}</el-button>
        </template>
      </template>
    </el-drawer>

    <!-- 手工新增 -->
    <el-dialog v-model="createVisible" :title="t('keywords.create')" width="min(480px, 96vw)" :close-on-click-modal="false">
      <el-form ref="createFormRef" :model="createForm" :rules="keywordRules" label-width="90px" @submit.prevent="submitCreate">
        <el-form-item :label="t('keywords.keyword')" prop="keyword" :error="createErrors.keyword">
          <el-input v-model="createForm.keyword" :maxlength="KEYWORD_LIMITS.keyword" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('keywords.intent')" :error="createErrors.intent">
          <el-select v-model="createForm.intent" style="width: 100%">
            <el-option v-for="v in KEYWORD_INTENT" :key="v" :value="v" :label="t(`status.keyword_intent.${v}`)" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('keywords.keywordType')" :error="createErrors.keyword_type">
          <el-select v-model="createForm.keyword_type" style="width: 100%">
            <el-option v-for="v in KEYWORD_TYPE" :key="v" :value="v" :label="t(`status.keyword_type.${v}`)" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="createSaving" @click="submitCreate">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 -->
    <el-dialog v-model="editVisible" :title="t('keywords.editTitle')" width="min(560px, 96vw)" :close-on-click-modal="false">
      <el-form ref="editFormRef" :model="editForm" :rules="keywordRules" label-width="90px" @submit.prevent="submitEdit">
        <el-form-item :label="t('keywords.keyword')" prop="keyword" :error="editErrors.keyword">
          <el-input v-model="editForm.keyword" :maxlength="KEYWORD_LIMITS.keyword" show-word-limit />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item :label="t('keywords.intent')" :error="editErrors.intent">
              <el-select v-model="editForm.intent" style="width: 100%">
                <el-option v-for="v in KEYWORD_INTENT" :key="v" :value="v" :label="t(`status.keyword_intent.${v}`)" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="t('keywords.keywordType')" :error="editErrors.keyword_type">
              <el-select v-model="editForm.keyword_type" style="width: 100%">
                <el-option v-for="v in KEYWORD_TYPE" :key="v" :value="v" :label="t(`status.keyword_type.${v}`)" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item :label="t('keywords.difficulty')" :error="editErrors.difficulty">
              <el-input-number v-model="editForm.difficulty" :min="1" :max="100" :precision="0" :value-on-clear="null" controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item :label="t('keywords.heat')" :error="editErrors.heat">
              <el-input-number v-model="editForm.heat" :min="1" :max="100" :precision="0" :value-on-clear="null" controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item :label="t('keywords.score')" :error="editErrors.score">
              <el-input-number v-model="editForm.score" :min="0" :max="100" :precision="2" :value-on-clear="null" controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
        </el-row>
        <div class="form-hint">{{ t("keywords.form.scoreHint") }}</div>
        <el-form-item :label="t('keywords.tags')" :error="editErrors.tags">
          <el-input-tag v-model="editForm.tags" :max="KEYWORD_LIMITS.tags" clearable trigger="Enter" delimiter="," :placeholder="t('keywords.form.tagsPlaceholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="editSaving" @click="submitEdit">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>

    <!-- 导入 -->
    <el-dialog v-model="importVisible" :title="t('keywords.import.title')" width="min(680px, 96vw)" :close-on-click-modal="false">
      <el-tabs v-model="importTab">
        <el-tab-pane name="paste" :label="t('keywords.import.paste')">
          <el-input v-model="importText" type="textarea" :rows="10" :placeholder="t('keywords.import.pastePlaceholder')" />
          <div class="form-hint">{{ t("keywords.import.pasteHint", { count: pasteCount, max: KEYWORD_IMPORT_MAX }) }}</div>
        </el-tab-pane>
        <el-tab-pane name="csv" :label="t('keywords.import.csv')">
          <el-upload drag :auto-upload="false" :limit="1" accept=".csv,text/csv" :on-change="onFileChange" :on-remove="onFileRemove">
            <el-icon class="el-icon--upload"><Upload /></el-icon>
            <div class="el-upload__text">{{ t("keywords.import.dropHint") }}</div>
          </el-upload>
          <div class="form-hint">
            {{ t("keywords.import.csvHint", { max: KEYWORD_IMPORT_MAX }) }}
            <el-link type="primary" :underline="false" @click="downloadTemplate">{{ t("keywords.import.template") }}</el-link>
          </div>
        </el-tab-pane>
      </el-tabs>
      <el-alert v-if="importError" type="error" :title="importError" :closable="false" show-icon class="mt" />
      <div v-if="importResult" class="import-result">
        <div class="import-result__stats">
          <el-tag type="success">{{ t("keywords.import.created", { n: importResult.created }) }}</el-tag>
          <el-tag type="info">{{ t("keywords.import.skipped", { n: importResult.skipped }) }}</el-tag>
          <el-tag :type="importResult.errors.length ? 'danger' : 'info'">{{ t("keywords.import.errors", { n: importResult.errors.length }) }}</el-tag>
        </div>
        <el-table v-if="importResult.errors.length" :data="importResult.errors" size="small" max-height="260">
          <el-table-column prop="index" :label="t('keywords.import.index')" width="80" />
          <el-table-column prop="keyword" :label="t('keywords.keyword')" min-width="200" show-overflow-tooltip />
          <el-table-column :label="t('keywords.import.reason')" width="160">
            <template #default="{ row }">{{ t(`keywords.import.reasons.${row.reason}`) }}</template>
          </el-table-column>
        </el-table>
      </div>
      <template #footer>
        <el-button @click="importVisible = false">{{ t("common.close") }}</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">{{ t("keywords.import.submit") }}</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.header-actions > * {
  margin-left: 0 !important;
}
.status-tabs {
  margin-top: -8px;
}
.batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-size: 13px;
}
.filter-alert {
  margin-bottom: 10px;
}
.kw {
  font-weight: 500;
}
.reason-icon {
  margin-left: 4px;
  color: var(--el-text-color-secondary);
  vertical-align: middle;
  cursor: help;
}
.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 2px;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.form-hint.inline {
  width: auto;
  margin-left: 8px;
}
.mt {
  margin-top: 12px;
}
.import-result {
  margin-top: 12px;
}
.import-result__stats {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
</style>
