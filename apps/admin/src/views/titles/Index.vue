<script setup lang="ts">
// 标题页（docs/09 §7、§10.4；docs/04 §6.10、§7.5）：
// - 左侧关键词选择：当前项目未弃用（candidate / adopted）的关键词，状态 Tag 区分、可搜索；路由参数 keyword_id 预选；
//   「全部标题」视图按 status / style / batch_id 筛选（路由参数 batch_id 预置）；
// - 右侧标题表格：行内编辑（保存前按 §7.3 去重键与该关键词已加载标题比对，重复时「仍保留」确认）、已编辑标记与原文、风格、
//   AI 分 / 人工分（el-input-number 0~10 步长 0.5，失焦即 POST score；两列只对当前页本地排序）、状态、内容数、
//   采用 / 弃用 / 恢复（单条与批量）、生成内容（仅 adopted）、删除（仅 content_count=0）；
// - 生成抽屉：关键词多选（≤ 50）、每词数量（title.default_count）、风格（项目风格 → title.default_style）、模板、模型（§10.8）；
//   BatchProgress 按关键词显示各根任务状态；
// - 「生成内容」对话框（ContentGenerateDialog）只接受已采用标题，成功后跳转内容列表并按 batch_id 筛选。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { Check, Close, Delete, Document, EditPen, MagicStick, Plus, Refresh, Search } from "@element-plus/icons-vue";
import {
  BATCH_IDS_MAX,
  CONTENT_STYLE,
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  TITLE_LIMITS,
  type AdoptAction,
  type ContentGenerateResult,
  type ContentStyle,
  type GenerationBatch,
  type Keyword,
  type Title,
  type TitleStatus,
} from "@aicreat/shared";
import { isApiError } from "@/api/client";
import * as keywordsApi from "@/api/keywords";
import * as titlesApi from "@/api/titles";
import BatchProgress from "@/components/BatchProgress.vue";
import ContentGenerateDialog from "@/components/ContentGenerateDialog.vue";
import GenerateNotice from "@/components/GenerateNotice.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import TemplateSelect from "@/components/TemplateSelect.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import { useGenerateGuard } from "@/composables/useGenerateGuard";
import { usePermission } from "@/composables/usePermission";
import { useProject } from "@/composables/useProject";
import { useRuntimeSettings } from "@/composables/useRuntimeSettings";
import { formatDateTime } from "@/utils/format";
import { findDuplicateTitle } from "@/utils/titleDedup";

const { t } = useI18n();
const { has } = usePermission();
const { projectId, project, store: projectStore } = useProject();
const route = useRoute();
const router = useRouter();
const { generation, load: loadRuntime } = useRuntimeSettings();

const canStatus = computed(() => has("content.titles.status"));
const canUpdate = computed(() => has("content.titles.update"));
// 生成与手工新增需要从关键词列表中选择（content.keywords.view）
const canGenerate = computed(() => has("content.titles.generate") && has("content.batches.view") && has("content.keywords.view"));
const canCreate = computed(() => has("content.titles.create") && has("content.keywords.view"));
const canGenerateContent = computed(() => has("content.contents.generate") && has("content.batches.view"));

function defaultStyle(): ContentStyle {
  return project.value?.default_style ?? generation.value.title.default_style ?? "news";
}

// ---------- 左侧：关键词 ----------
const kwSearch = ref("");
const keywords = ref<Keyword[]>([]);
const kwLoading = ref(false);
const kwTruncated = ref(false);
/** 0 = 全部标题 */
const selectedKeywordId = ref(0);
let kwSeq = 0;

const keywordMap = computed(() => new Map(keywords.value.map((k) => [k.id, k])));
const selectedKeyword = computed(() => keywordMap.value.get(selectedKeywordId.value) ?? null);

/** 关键词列表需要 content.keywords.view；没有时只提供「全部标题」视图 */
const canViewKeywords = computed(() => has("content.keywords.view"));

async function loadKeywords(ensureId = 0) {
  if (!projectId.value || !canViewKeywords.value) {
    keywords.value = [];
    return;
  }
  const seq = ++kwSeq;
  kwLoading.value = true;
  try {
    const common = { project_id: projectId.value, keyword: kwSearch.value.trim() || undefined, sort: "score" as const, page: 1, page_size: MAX_PAGE_SIZE };
    const [adopted, candidate] = await Promise.all([
      keywordsApi.list({ ...common, status: "adopted" }),
      keywordsApi.list({ ...common, status: "candidate" }),
    ]);
    if (seq !== kwSeq) return;
    kwTruncated.value = adopted.total > adopted.items.length || candidate.total > candidate.items.length;
    const merged = [...adopted.items, ...candidate.items];
    const keep = ensureId || selectedKeywordId.value;
    if (keep && !merged.some((k) => k.id === keep)) {
      const prev = keywords.value.find((k) => k.id === keep);
      if (prev) merged.unshift(prev);
      else {
        try {
          merged.unshift(await keywordsApi.get(keep, { silent: true }));
        } catch {
          /* 不可见 / 不存在 */
        }
      }
    }
    keywords.value = merged;
  } catch {
    if (seq === kwSeq) keywords.value = [];
  } finally {
    if (seq === kwSeq) kwLoading.value = false;
  }
}

function selectKeyword(id: number) {
  if (selectedKeywordId.value === id) return;
  selectedKeywordId.value = id;
  selection.value = [];
  if (id) filters.batch_id = undefined;
  page.value = 1;
  void loadTitles();
}

const kwActing = ref(false);

async function adoptKeyword() {
  const kw = selectedKeyword.value;
  if (!kw) return;
  kwActing.value = true;
  try {
    const updated = await keywordsApi.adopt(kw.id);
    Object.assign(kw, updated);
    ElMessage.success(t("titles.keywordAdopted"));
  } catch {
    /* 已提示 */
  } finally {
    kwActing.value = false;
  }
}

// ---------- 右侧：标题 ----------
const filters = reactive({
  status: undefined as TitleStatus | undefined,
  style: undefined as ContentStyle | undefined,
  batch_id: undefined as number | undefined,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<Title[]>([]);
const loading = ref(false);
const selection = ref<Title[]>([]);
let titleSeq = 0;

async function loadTitles() {
  if (!projectId.value) {
    rows.value = [];
    total.value = 0;
    return;
  }
  const seq = ++titleSeq;
  loading.value = true;
  try {
    const res = await titlesApi.list({
      project_id: projectId.value,
      keyword_id: selectedKeywordId.value || undefined,
      status: filters.status,
      style: filters.style,
      batch_id: filters.batch_id || undefined,
      page: page.value,
      page_size: pageSize.value,
    });
    if (seq !== titleSeq) return;
    rows.value = res.items;
    total.value = res.total;
    syncScoreDrafts();
  } catch {
    if (seq !== titleSeq) return;
    rows.value = [];
    total.value = 0;
  } finally {
    if (seq === titleSeq) loading.value = false;
  }
}

function search() {
  page.value = 1;
  void loadTitles();
}

function resetFilters() {
  filters.status = undefined;
  filters.style = undefined;
  filters.batch_id = undefined;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

function keywordText(id: number): string {
  return keywordMap.value.get(id)?.keyword ?? `#${id}`;
}

/** 同关键词已加载的标题（去重比对范围） */
function loadedTitlesOf(keywordId: number): Title[] {
  return rows.value.filter((r) => r.keyword_id === keywordId);
}

async function confirmDuplicate(title: string, keywordId: number, excludeId?: number, extra: Title[] = []): Promise<boolean> {
  const dup = findDuplicateTitle(title, [...loadedTitlesOf(keywordId), ...extra], excludeId);
  if (!dup) return true;
  try {
    await ElMessageBox.confirm(t("titles.duplicateConfirm", { title: dup.title }), t("titles.duplicateTitle"), {
      type: "warning",
      confirmButtonText: t("titles.keepAnyway"),
    });
    return true;
  } catch {
    return false;
  }
}

// ---------- 行内编辑 ----------
const editingId = ref(0);
const editText = ref("");
const editStyle = ref<ContentStyle>("news");
const savingId = ref(0);

function startEdit(row: Title) {
  editingId.value = row.id;
  editText.value = row.title;
  editStyle.value = row.style;
}

function cancelEdit() {
  editingId.value = 0;
}

async function saveEdit(row: Title) {
  const title = editText.value.trim().replace(/\s+/g, " ");
  if (!title) {
    ElMessage.warning(t("titles.titleRequired"));
    return;
  }
  if (title.length > TITLE_LIMITS.title) {
    ElMessage.warning(t("titles.titleTooLong", { max: TITLE_LIMITS.title }));
    return;
  }
  if (title === row.title && editStyle.value === row.style) {
    cancelEdit();
    return;
  }
  if (title !== row.title && !(await confirmDuplicate(title, row.keyword_id, row.id))) return;
  savingId.value = row.id;
  try {
    const updated = await titlesApi.update(row.id, { title, style: editStyle.value });
    Object.assign(row, updated);
    ElMessage.success(t("common.saved"));
    cancelEdit();
  } catch {
    /* 已提示 */
  } finally {
    savingId.value = 0;
  }
}

// ---------- 人工分 ----------
const scoreDrafts = reactive<Record<number, number | null>>({});

function syncScoreDrafts() {
  for (const key of Object.keys(scoreDrafts)) delete scoreDrafts[Number(key)];
  for (const r of rows.value) scoreDrafts[r.id] = r.manual_score;
}

async function saveScore(row: Title) {
  const value = scoreDrafts[row.id] ?? null;
  if (value === row.manual_score) return;
  if (value !== null && (value < 0 || value > TITLE_LIMITS.manualScoreMax || Math.round(value * 2) !== value * 2)) {
    ElMessage.warning(t("titles.scoreRule"));
    scoreDrafts[row.id] = row.manual_score;
    return;
  }
  try {
    const updated = await titlesApi.score(row.id, value);
    Object.assign(row, updated);
    scoreDrafts[row.id] = updated.manual_score;
    ElMessage.success(t("titles.scored"));
  } catch {
    scoreDrafts[row.id] = row.manual_score;
  }
}

// ---------- 状态动作 ----------
const acting = ref(0);

async function act(row: Title, action: AdoptAction) {
  acting.value = row.id;
  try {
    const updated = await titlesApi[action](row.id);
    Object.assign(row, updated);
    ElMessage.success(t(`titles.actionDone.${action}`));
    if (filters.status) void loadTitles();
  } catch {
    /* 409（关键词未采用等）已提示 */
  } finally {
    acting.value = 0;
  }
}

const batchActing = ref(false);

async function batchAct(action: AdoptAction) {
  const ids = selection.value.map((r) => r.id);
  if (!ids.length) return;
  if (ids.length > BATCH_IDS_MAX) {
    ElMessage.warning(t("keywords.batchTooMany", { max: BATCH_IDS_MAX }));
    return;
  }
  batchActing.value = true;
  try {
    const res = await titlesApi.batchStatus(ids, action);
    const msg = t("keywords.batchResult", { updated: res.updated, skipped: res.skipped.length });
    if (res.skipped.length) ElMessage.warning(msg);
    else ElMessage.success(msg);
    void loadTitles();
  } catch {
    /* 已提示 */
  } finally {
    batchActing.value = false;
  }
}

async function removeTitle(row: Title) {
  try {
    await ElMessageBox.confirm(t("titles.deleteConfirm", { title: row.title }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await titlesApi.remove(row.id, { silent: true });
    ElMessage.success(t("titles.deleted"));
    void loadTitles();
    void loadKeywords();
  } catch (err) {
    if (isApiError(err)) ElMessage.error(err.code === 409 ? t("titles.deleteInUse") : err.message);
  } finally {
    acting.value = 0;
  }
}

// ---------- 手工新增 ----------
const createVisible = ref(false);
const createSaving = ref(false);
const createForm = reactive({ keyword_id: 0, title: "", style: "news" as ContentStyle });
const createError = ref("");

async function openCreate() {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  await loadRuntime();
  createForm.keyword_id = selectedKeywordId.value || 0;
  createForm.title = "";
  createForm.style = defaultStyle();
  createError.value = "";
  createVisible.value = true;
}

async function submitCreate() {
  const title = createForm.title.trim().replace(/\s+/g, " ");
  if (!createForm.keyword_id) {
    createError.value = t("titles.keywordRequired");
    return;
  }
  if (!title) {
    createError.value = t("titles.titleRequired");
    return;
  }
  if (title.length > TITLE_LIMITS.title) {
    createError.value = t("titles.titleTooLong", { max: TITLE_LIMITS.title });
    return;
  }
  createError.value = "";
  // 同关键词已有标题：当前视图已加载的 + 不在当前视图时拉取一页
  let extra: Title[] = [];
  if (createForm.keyword_id !== selectedKeywordId.value) {
    try {
      extra = (await titlesApi.list({ project_id: projectId.value, keyword_id: createForm.keyword_id, page: 1, page_size: MAX_PAGE_SIZE }, { silent: true })).items;
    } catch {
      extra = [];
    }
  }
  if (!(await confirmDuplicate(title, createForm.keyword_id, undefined, extra))) return;
  createSaving.value = true;
  try {
    await titlesApi.create({ keyword_id: createForm.keyword_id, title, style: createForm.style });
    ElMessage.success(t("titles.created"));
    createVisible.value = false;
    void loadTitles();
    void loadKeywords();
  } catch {
    /* 已提示 */
  } finally {
    createSaving.value = false;
  }
}

// ---------- 生成标题 ----------
const guard = useGenerateGuard();
const genVisible = ref(false);
const genBatchId = ref(0);
const genForm = reactive({ keyword_ids: [] as number[], count: 5, style: "news" as ContentStyle, template_id: null as number | null, model: null as string | null });
const genError = ref<Record<string, string>>({});
const genTargetLabels = ref<Record<number, string>>({});

async function openGenerate(keywordIds?: number[]) {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  const gen = await loadRuntime();
  guard.reset();
  genBatchId.value = 0;
  genError.value = {};
  const ids = keywordIds ?? (selectedKeywordId.value ? [selectedKeywordId.value] : []);
  genForm.keyword_ids = ids.filter((id) => keywordMap.value.get(id)?.status !== "discarded").slice(0, TITLE_LIMITS.keywordIds);
  genForm.count = gen.title.default_count;
  genForm.style = defaultStyle();
  genForm.template_id = null;
  genForm.model = null;
  genVisible.value = true;
}

const genKeywordOptions = computed(() => keywords.value.filter((k) => k.status !== "discarded"));

async function submitGenerate() {
  const errs: Record<string, string> = {};
  if (!genForm.keyword_ids.length) errs.keyword_ids = t("titles.gen.keywordsRequired");
  else if (genForm.keyword_ids.length > TITLE_LIMITS.keywordIds) errs.keyword_ids = t("titles.gen.keywordsTooMany", { max: TITLE_LIMITS.keywordIds });
  const max = generation.value.title.max_count;
  if (!genForm.count || genForm.count < 1 || genForm.count > max) errs.count = t("titles.gen.countRange", { max });
  genError.value = errs;
  if (Object.keys(errs).length) return;
  const labels: Record<number, string> = {};
  for (const id of genForm.keyword_ids) labels[id] = keywordText(id);
  const res = await guard.run(() =>
    titlesApi.generate(
      {
        project_id: projectId.value,
        keyword_ids: [...genForm.keyword_ids],
        count: genForm.count,
        style: genForm.style,
        ...(has("content.prompt_templates.view") && genForm.template_id ? { template_id: genForm.template_id } : {}),
        ...(has("ai.models.view") && genForm.model ? { model: genForm.model } : {}),
      },
      { silent: true },
    ),
  );
  if (!res) return;
  genTargetLabels.value = labels;
  genBatchId.value = res.batch_id;
  ElMessage.success(t("titles.gen.queued", { id: res.batch_id }));
}

const genFieldErrors = computed(() => ({ ...guard.fields.value, ...genError.value }));

function onBatchFinished(batch: GenerationBatch) {
  if (batch.status === "succeeded" || batch.status === "partial") {
    void loadTitles();
    void loadKeywords();
  }
}

function showGenBatch() {
  selectedKeywordId.value = 0;
  filters.batch_id = genBatchId.value;
  filters.status = undefined;
  filters.style = undefined;
  genVisible.value = false;
  search();
}

// ---------- 生成内容 ----------
const contentVisible = ref(false);
const contentTitles = ref<Title[]>([]);

function openGenerateContent(list: Title[]) {
  const adopted = list.filter((x) => x.status === "adopted");
  if (!adopted.length) {
    ElMessage.warning(t("titles.adoptFirst"));
    return;
  }
  if (adopted.length < list.length) ElMessage.warning(t("titles.nonAdoptedFiltered", { count: list.length - adopted.length }));
  contentTitles.value = adopted;
  contentVisible.value = true;
}

function onContentCreated(res: ContentGenerateResult) {
  void router.push({ path: "/contents", query: { batch_id: String(res.batch_id) } });
}

// ---------- 进入页面 / 切换项目 ----------
async function init() {
  const kwId = Number(route.query.keyword_id);
  const batchId = Number(route.query.batch_id);
  selectedKeywordId.value = Number.isInteger(kwId) && kwId > 0 ? kwId : 0;
  if (Number.isInteger(batchId) && batchId > 0) filters.batch_id = batchId;
  await loadRuntime();
  void loadKeywords(selectedKeywordId.value);
  void loadTitles();
}

watch(projectId, () => {
  selectedKeywordId.value = 0;
  filters.batch_id = undefined;
  selection.value = [];
  kwSearch.value = "";
  void loadKeywords();
  search();
});

onMounted(() => {
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void init();
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.titles") }}</h2>
        <div v-if="projectId" class="header-actions">
          <el-button v-if="canGenerate" type="primary" :icon="MagicStick" @click="openGenerate()">{{ t("titles.generate") }}</el-button>
          <el-button v-if="canCreate" :icon="Plus" @click="openCreate">{{ t("titles.create") }}</el-button>
        </div>
      </div>
    </template>

    <el-empty v-if="!projectId" :description="t('generation.noProject')">
      <router-link v-if="has('content.projects.view')" to="/projects">
        <el-button type="primary">{{ t("generation.gotoProjects") }}</el-button>
      </router-link>
    </el-empty>

    <div v-else class="titles-layout">
      <!-- 关键词选择 -->
      <aside class="kw-panel">
        <el-input v-model="kwSearch" :placeholder="t('titles.kwSearch')" clearable :prefix-icon="Search" @keyup.enter="loadKeywords()" @clear="loadKeywords()" />
        <div v-loading="kwLoading" class="kw-list">
          <div class="kw-item" :class="{ 'is-active': selectedKeywordId === 0 }" @click="selectKeyword(0)">
            <span class="kw-item__text">{{ t("titles.allTitles") }}</span>
          </div>
          <div v-for="kw in keywords" :key="kw.id" class="kw-item" :class="{ 'is-active': selectedKeywordId === kw.id }" @click="selectKeyword(kw.id)">
            <span class="kw-item__text" :title="kw.keyword">{{ kw.keyword }}</span>
            <span class="kw-item__meta">
              <StatusTag kind="keyword_status" :value="kw.status" />
              <span class="kw-item__count">{{ kw.title_count }}</span>
            </span>
          </div>
          <el-empty v-if="!kwLoading && !keywords.length" :image-size="60" :description="t('titles.noKeywords')" />
        </div>
        <div v-if="kwTruncated" class="form-hint">{{ t("titles.kwTruncated", { max: MAX_PAGE_SIZE }) }}</div>
      </aside>

      <!-- 标题 -->
      <section class="title-panel">
        <div v-if="selectedKeyword" class="kw-head">
          <span class="kw-head__name">{{ selectedKeyword.keyword }}</span>
          <StatusTag kind="keyword_status" :value="selectedKeyword.status" />
          <StatusTag kind="keyword_intent" :value="selectedKeyword.intent" effect="plain" />
          <span class="spacer" />
          <template v-if="selectedKeyword.status === 'candidate'">
            <span class="text-secondary hint">{{ t("titles.keywordNotAdopted") }}</span>
            <el-button v-permission="'content.keywords.status'" size="small" type="success" plain :loading="kwActing" @click="adoptKeyword">
              {{ t("titles.adoptKeyword") }}
            </el-button>
          </template>
        </div>

        <div class="toolbar">
          <ToolbarSelect v-model="filters.status" enum-name="title_status" :placeholder="t('common.status')" width="120px" @change="search" />
          <ToolbarSelect v-model="filters.style" enum-name="content_style" :placeholder="t('titles.style')" width="120px" @change="search" />
          <el-input-number v-if="!selectedKeywordId" v-model="filters.batch_id" :min="1" :controls="false" :placeholder="t('keywords.batchId')" style="width: 110px" @change="search" />
          <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
          <span class="spacer" />
          <el-button :icon="Refresh" @click="loadTitles">{{ t("common.refresh") }}</el-button>
        </div>

        <div v-if="selection.length" class="batch-bar">
          <span>{{ t("keywords.selected", { count: selection.length }) }}</span>
          <template v-if="canStatus">
            <el-button size="small" type="success" plain :loading="batchActing" @click="batchAct('adopt')">{{ t("keywords.batchAdopt") }}</el-button>
            <el-button size="small" type="warning" plain :loading="batchActing" @click="batchAct('discard')">{{ t("keywords.batchDiscard") }}</el-button>
            <el-button size="small" plain :loading="batchActing" @click="batchAct('restore')">{{ t("keywords.batchRestore") }}</el-button>
          </template>
          <el-button v-if="canGenerateContent" size="small" type="primary" plain :icon="Document" @click="openGenerateContent(selection)">
            {{ t("titles.generateContent") }}
          </el-button>
        </div>

        <el-table v-loading="loading" :data="rows" row-key="id" stripe @selection-change="(v: Title[]) => (selection = v)">
          <el-table-column type="selection" width="44" />
          <el-table-column :label="t('titles.title')" min-width="280">
            <template #default="{ row }">
              <div v-if="editingId === row.id" class="inline-edit">
                <el-input v-model="editText" size="small" :maxlength="TITLE_LIMITS.title" @keyup.enter="saveEdit(row)" @keyup.esc="cancelEdit" />
                <el-select v-model="editStyle" size="small" style="width: 100px">
                  <el-option v-for="s in CONTENT_STYLE" :key="s" :value="s" :label="t(`status.content_style.${s}`)" />
                </el-select>
                <el-button size="small" type="primary" :icon="Check" :loading="savingId === row.id" @click="saveEdit(row)" />
                <el-button size="small" :icon="Close" @click="cancelEdit" />
              </div>
              <div v-else class="title-cell">
                <span class="title-cell__text">{{ row.title }}</span>
                <el-tooltip v-if="row.is_edited" :content="t('titles.original', { title: row.original_title || '-' })" placement="top">
                  <el-tag size="small" type="warning" effect="plain">{{ t("titles.edited") }}</el-tag>
                </el-tooltip>
                <el-button v-if="canUpdate" link type="primary" :icon="EditPen" class="title-cell__edit" @click="startEdit(row)" />
              </div>
              <div v-if="!selectedKeywordId" class="text-secondary sub">{{ keywordText(row.keyword_id) }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="t('titles.style')" width="80">
            <template #default="{ row }"><StatusTag kind="content_style" :value="row.style" effect="plain" /></template>
          </el-table-column>
          <el-table-column prop="ai_score" :label="t('titles.aiScore')" width="90" align="right" sortable>
            <template #default="{ row }">{{ row.ai_score ?? "-" }}</template>
          </el-table-column>
          <el-table-column prop="manual_score" :label="t('titles.manualScore')" width="130" sortable>
            <template #default="{ row }">
              <el-input-number
                v-if="canUpdate"
                v-model="scoreDrafts[row.id]"
                size="small"
                :min="0"
                :max="TITLE_LIMITS.manualScoreMax"
                :step="TITLE_LIMITS.manualScoreStep"
                :precision="1"
                :value-on-clear="null"
                controls-position="right"
                style="width: 100px"
                @change="saveScore(row)"
              />
              <span v-else>{{ row.manual_score ?? "-" }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('common.status')" width="90">
            <template #default="{ row }"><StatusTag kind="title_status" :value="row.status" /></template>
          </el-table-column>
          <el-table-column :label="t('titles.contentCount')" width="80" align="right">
            <template #default="{ row }">
              <router-link v-if="row.content_count" :to="`/contents?title_id=${row.id}`">{{ row.content_count }}</router-link>
              <span v-else>0</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('common.createdAt')" width="160">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('common.actions')" width="230" fixed="right">
            <template #default="{ row }">
              <template v-if="canStatus">
                <el-button v-if="row.status === 'candidate'" link type="success" :loading="acting === row.id" @click="act(row, 'adopt')">{{ t("keywords.adopt") }}</el-button>
                <el-button v-if="row.status !== 'discarded'" link type="warning" :loading="acting === row.id" @click="act(row, 'discard')">{{ t("keywords.discard") }}</el-button>
                <el-button v-else link type="primary" :loading="acting === row.id" @click="act(row, 'restore')">{{ t("keywords.restore") }}</el-button>
              </template>
              <el-tooltip v-if="canGenerateContent" :content="t('titles.adoptFirst')" :disabled="row.status === 'adopted'" placement="top">
                <span>
                  <el-button link type="primary" :disabled="row.status !== 'adopted'" @click="openGenerateContent([row])">{{ t("titles.generateContent") }}</el-button>
                </span>
              </el-tooltip>
              <span v-permission="'content.titles.delete'">
                <el-tooltip :content="t('titles.deleteTip')" :disabled="!row.content_count" placement="top">
                  <el-button link type="danger" :icon="Delete" :disabled="!!row.content_count" @click="removeTitle(row)" />
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
            @current-change="loadTitles"
            @size-change="onPageSizeChange"
          />
        </div>
      </section>
    </div>

    <!-- 生成标题 -->
    <el-drawer v-model="genVisible" :title="t('titles.gen.title')" size="min(560px, 96vw)" :close-on-click-modal="false">
      <GenerateNotice :notice="guard.notice.value" :quota-warning="guard.quotaWarning.value" @close="guard.notice.value = null" />
      <el-form v-if="!genBatchId" label-position="top" @submit.prevent="submitGenerate">
        <el-form-item :label="t('titles.gen.keywords')" :error="genFieldErrors.keyword_ids" required>
          <el-select
            v-model="genForm.keyword_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :max-collapse-tags="6"
            :multiple-limit="TITLE_LIMITS.keywordIds"
            style="width: 100%"
            :placeholder="t('titles.gen.keywordsPlaceholder')"
          >
            <el-option v-for="kw in genKeywordOptions" :key="kw.id" :value="kw.id" :label="kw.keyword">
              <span class="opt-row">
                <span>{{ kw.keyword }}</span>
                <StatusTag kind="keyword_status" :value="kw.status" />
              </span>
            </el-option>
          </el-select>
          <div class="form-hint">{{ t("titles.gen.keywordsHint", { max: TITLE_LIMITS.keywordIds }) }}</div>
        </el-form-item>
        <el-form-item :label="t('titles.gen.count')" :error="genFieldErrors.count" required>
          <el-input-number v-model="genForm.count" :min="1" :max="generation.title.max_count" controls-position="right" />
          <span class="form-hint inline">{{ t("titles.gen.countHint", { max: generation.title.max_count }) }}</span>
        </el-form-item>
        <el-form-item :label="t('titles.style')" :error="genFieldErrors.style" required>
          <el-select v-model="genForm.style" style="width: 200px">
            <el-option v-for="s in CONTENT_STYLE" :key="s" :value="s" :label="t(`status.content_style.${s}`)" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="has('content.prompt_templates.view')" :label="t('generation.template')" :error="genFieldErrors.template_id">
          <TemplateSelect v-model="genForm.template_id" :kinds="['title']" :project-id="projectId" :project-defaults="project?.default_templates" />
        </el-form-item>
        <el-form-item v-if="has('ai.models.view')" :label="t('generation.model')" :error="genFieldErrors.model">
          <ModelSelect v-model="genForm.model" modality="text" allow-empty />
        </el-form-item>
      </el-form>
      <BatchProgress v-else :batch-id="genBatchId" :target-labels="genTargetLabels" @finished="onBatchFinished" />
      <template #footer>
        <template v-if="!genBatchId">
          <el-button @click="genVisible = false">{{ t("common.cancel") }}</el-button>
          <el-button type="primary" :loading="guard.submitting.value" :disabled="guard.cooldown.value > 0" @click="submitGenerate">
            {{ guard.cooldown.value > 0 ? t("generation.retryIn", { seconds: guard.cooldown.value }) : t("titles.gen.submit") }}
          </el-button>
        </template>
        <template v-else>
          <el-button @click="openGenerate(genForm.keyword_ids)">{{ t("generation.again") }}</el-button>
          <el-button type="primary" @click="showGenBatch">{{ t("titles.gen.showBatch") }}</el-button>
        </template>
      </template>
    </el-drawer>

    <!-- 手工新增 -->
    <el-dialog v-model="createVisible" :title="t('titles.create')" width="min(560px, 96vw)" :close-on-click-modal="false">
      <el-alert v-if="createError" type="error" :title="createError" :closable="false" show-icon class="mb" />
      <el-form label-width="80px" @submit.prevent="submitCreate">
        <el-form-item :label="t('titles.keyword')" required>
          <el-select v-model="createForm.keyword_id" filterable style="width: 100%" :placeholder="t('titles.gen.keywordsPlaceholder')">
            <el-option v-for="kw in genKeywordOptions" :key="kw.id" :value="kw.id" :label="kw.keyword" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('titles.title')" required>
          <el-input v-model="createForm.title" :maxlength="TITLE_LIMITS.title" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('titles.style')" required>
          <el-select v-model="createForm.style" style="width: 200px">
            <el-option v-for="s in CONTENT_STYLE" :key="s" :value="s" :label="t(`status.content_style.${s}`)" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="createSaving" @click="submitCreate">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>

    <ContentGenerateDialog v-model="contentVisible" :project-id="projectId" :project="project" :titles="contentTitles" @success="onContentCreated" />
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
.titles-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.kw-panel {
  flex: 0 0 260px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.kw-list {
  max-height: calc(100vh - 260px);
  min-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.kw-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 7px 10px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  font-size: 13px;
}
.kw-item:hover {
  background: var(--el-fill-color-light);
}
.kw-item.is-active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
.kw-item__text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kw-item__meta {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: none;
}
.kw-item__count {
  min-width: 18px;
  text-align: right;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.title-panel {
  flex: 1;
  min-width: 0;
}
.kw-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.kw-head__name {
  font-weight: 600;
  font-size: 15px;
}
.kw-head .spacer {
  flex: 1;
}
.kw-head .hint {
  font-size: 12px;
}
.batch-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-size: 13px;
}
.batch-bar > .el-button {
  margin-left: 0;
}
.inline-edit {
  display: flex;
  align-items: center;
  gap: 4px;
}
.inline-edit .el-button {
  margin-left: 0;
}
.title-cell {
  display: flex;
  align-items: center;
  gap: 6px;
}
.title-cell__text {
  word-break: break-all;
}
.title-cell__edit {
  visibility: hidden;
}
.el-table__row:hover .title-cell__edit {
  visibility: visible;
}
.sub {
  font-size: 12px;
}
.opt-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
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
.mb {
  margin-bottom: 12px;
}
@media (max-width: 1100px) {
  .titles-layout {
    flex-direction: column;
  }
  .kw-panel {
    flex: none;
    width: 100%;
  }
  .kw-list {
    max-height: 240px;
  }
}
</style>
