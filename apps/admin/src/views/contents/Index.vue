<script setup lang="ts">
// 内容列表（docs/09 §8、§10.5；docs/04 §6.11）：
// - 状态 Tab：全部 / 草稿 / 生成中 / 可提审 / 待审核 / 已审核 / 已驳回 / 已发布 / 已归档；
//   筛选 keyword_id / title_id / batch_id / created_by（「我创建的」）/ has_links / 标题搜索（路由参数 keyword_id、title_id、batch_id 预置）；
// - 列：标题、主关键词、风格、格式、字数、质量分（< 60 标红，悬停显示 risk_flags）、状态、版本数、链接数、更新时间、
//   操作（编辑、提审 / 审核、归档 / 恢复、导出、删除）；
// - generating 行显示 TaskProgress：页面级 usePolling 每 3s 对当前页的生成中行批量调 GET /contents/{id}/task，任一终态后刷新列表；
// - 「新建内容」手工创建（ContentCreateDialog）后进入编辑器；「生成内容」从已采用标题生成（ContentGenerateDialog）。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowDown, MagicStick, Plus, Refresh, Search } from "@element-plus/icons-vue";
import {
  CONTENT_ACTIONS,
  CONTENT_LIMITS,
  CONTENT_STATUS,
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  type AiTaskSummary,
  type ConflictData,
  type Content,
  type ContentAction,
  type ContentExportFormat,
  type ContentGenerateResult,
  type ContentStatus,
  type Keyword,
} from "@aicreat/shared";
import { isApiError } from "@/api/client";
import * as contentsApi from "@/api/contents";
import * as keywordsApi from "@/api/keywords";
import ContentCreateDialog from "@/components/ContentCreateDialog.vue";
import ContentGenerateDialog from "@/components/ContentGenerateDialog.vue";
import StatusTag from "@/components/StatusTag.vue";
import TaskProgress, { isTerminalTask } from "@/components/TaskProgress.vue";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useProject } from "@/composables/useProject";
import { useAuthStore } from "@/store/auth";
import { downloadBlob } from "@/utils/download";
import { formatDateTime, formatNumber } from "@/utils/format";

type StatusTab = "all" | ContentStatus;

const { t, te } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();
const { projectId, project, store: projectStore } = useProject();
const route = useRoute();
const router = useRouter();

const canGenerate = computed(() => has("content.contents.generate") && has("content.batches.view"));

// ---------- 筛选与列表 ----------
const statusTab = ref<StatusTab>("all");
const filters = reactive({
  keyword: "",
  keyword_id: undefined as number | undefined,
  title_id: undefined as number | undefined,
  batch_id: undefined as number | undefined,
  mine: false,
  has_links: undefined as boolean | undefined,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<Content[]>([]);
const loading = ref(false);
let seq = 0;

async function load(silent = false) {
  if (!projectId.value) {
    rows.value = [];
    total.value = 0;
    syncPolling();
    return;
  }
  const my = ++seq;
  if (!silent) loading.value = true;
  try {
    const res = await contentsApi.list(
      {
        project_id: projectId.value,
        status: statusTab.value === "all" ? undefined : statusTab.value,
        keyword: filters.keyword.trim() || undefined,
        keyword_id: filters.keyword_id,
        title_id: filters.title_id,
        batch_id: filters.batch_id,
        created_by: filters.mine ? auth.admin?.id : undefined,
        has_links: filters.has_links,
        page: page.value,
        page_size: pageSize.value,
      },
      { silent },
    );
    if (my !== seq) return;
    rows.value = res.items;
    total.value = res.total;
    void resolveKeywordNames();
  } catch {
    if (my !== seq || silent) return;
    rows.value = [];
    total.value = 0;
  } finally {
    if (my === seq) loading.value = false;
  }
  syncPolling();
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.keyword = "";
  filters.keyword_id = undefined;
  filters.title_id = undefined;
  filters.batch_id = undefined;
  filters.mine = false;
  filters.has_links = undefined;
  statusTab.value = "all";
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

watch(projectId, () => {
  filters.keyword_id = undefined;
  filters.title_id = undefined;
  filters.batch_id = undefined;
  void loadKeywordOptions();
  search();
});

// ---------- 关键词：筛选选项与主关键词文本 ----------
const keywordOptions = ref<Keyword[]>([]);
const keywordNames = reactive<Record<number, string>>({});

async function loadKeywordOptions() {
  keywordOptions.value = [];
  if (!projectId.value || !has("content.keywords.view")) return;
  try {
    const res = await keywordsApi.list({ project_id: projectId.value, status: "adopted", sort: "score", page: 1, page_size: MAX_PAGE_SIZE }, { silent: true });
    keywordOptions.value = res.items;
    for (const k of res.items) keywordNames[k.id] = k.keyword;
  } catch {
    keywordOptions.value = [];
  }
}

async function resolveKeywordNames() {
  if (!has("content.keywords.view")) return;
  const missing = [...new Set(rows.value.map((r) => r.keyword_id).filter((id): id is number => !!id && !(id in keywordNames)))];
  await Promise.all(
    missing.map(async (id) => {
      try {
        keywordNames[id] = (await keywordsApi.get(id, { silent: true })).keyword;
      } catch {
        keywordNames[id] = `#${id}`;
      }
    }),
  );
}

// ---------- 生成中行的任务轮询 ----------
const tasks = reactive<Record<number, AiTaskSummary | null>>({});

/** 有进行中任务的行：generating，或 approved / published 下重写中（active_task_id 非空） */
function isBusy(row: Content): boolean {
  return row.status === "generating" || !!row.active_task_id;
}

async function pollTasks() {
  const generating = rows.value.filter(isBusy);
  if (!generating.length) return;
  let finished = false;
  await Promise.all(
    generating.map(async (row) => {
      try {
        const task = await contentsApi.getTask(row.id, { silent: true });
        tasks[row.id] = task;
        if (task && isTerminalTask(task.status)) finished = true;
      } catch {
        /* 下次再试 */
      }
    }),
  );
  if (finished) await load(true);
}

const polling = usePolling(pollTasks, { interval: 3000 });

function syncPolling() {
  const active = rows.value.some(isBusy);
  if (active && !polling.running.value) polling.start();
  else if (!active && polling.running.value) polling.stop();
}

// ---------- 行动作 ----------
function allowed(row: Content, action: ContentAction): boolean {
  return CONTENT_ACTIONS[row.status]?.includes(action) ?? false;
}

function riskLabel(flag: string): string {
  return te(`contents.riskFlags.${flag}`) ? t(`contents.riskFlags.${flag}`) : flag;
}

const acting = ref(0);

function applyRow(row: Content, updated: Content) {
  Object.assign(row, updated);
  if (statusTab.value !== "all" && updated.status !== statusTab.value) void load(true);
}

async function submitReview(row: Content) {
  acting.value = row.id;
  try {
    const updated = await contentsApi.submitReview(row.id, { silent: true });
    applyRow(row, updated);
    ElMessage.success(updated.status === "approved" ? t("contents.review.autoApproved") : t("contents.review.submitted"));
  } catch (err) {
    if (isApiError(err)) {
      const data = (err.data ?? {}) as ConflictData;
      if (err.code === 409 && data.reason === "quality_blocked") {
        ElMessage.error(t("contents.review.qualityBlocked", { flags: (data.flags ?? []).map(riskLabel).join("、") }));
      } else ElMessage.error(err.message);
    }
  } finally {
    acting.value = 0;
  }
}

async function approve(row: Content) {
  let note = "";
  try {
    const res = await ElMessageBox.prompt(t("contents.review.approveNote"), t("contents.review.approve"), {
      inputPlaceholder: t("contents.review.notePlaceholder"),
      inputValidator: (v: string) => (v ?? "").length <= 500 || t("contents.review.noteTooLong", { max: 500 }),
      confirmButtonText: t("contents.review.approve"),
    });
    note = (res as { value: string }).value ?? "";
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    applyRow(row, await contentsApi.approve(row.id, note.trim() || null));
    ElMessage.success(t("contents.review.approved"));
  } catch {
    /* 已提示 */
  } finally {
    acting.value = 0;
  }
}

async function reject(row: Content) {
  let note = "";
  try {
    const res = await ElMessageBox.prompt(t("contents.review.rejectNote"), t("contents.review.reject"), {
      inputType: "textarea",
      inputPlaceholder: t("contents.review.rejectPlaceholder"),
      inputValidator: (v: string) => {
        const s = (v ?? "").trim();
        if (!s) return t("contents.review.noteRequired");
        return s.length <= 500 || t("contents.review.noteTooLong", { max: 500 });
      },
      confirmButtonText: t("contents.review.reject"),
      confirmButtonClass: "el-button--danger",
    });
    note = (res as { value: string }).value.trim();
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    applyRow(row, await contentsApi.reject(row.id, note));
    ElMessage.success(t("contents.review.rejected"));
  } catch {
    /* 已提示 */
  } finally {
    acting.value = 0;
  }
}

async function toggleArchive(row: Content) {
  const archiving = row.status !== "archived";
  try {
    await ElMessageBox.confirm(
      archiving ? t("contents.archiveConfirm", { title: row.title }) : t("contents.unarchiveConfirm", { title: row.title }),
      t("common.tip"),
      { type: archiving ? "warning" : "info" },
    );
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    applyRow(row, archiving ? await contentsApi.archive(row.id) : await contentsApi.unarchive(row.id));
    ElMessage.success(archiving ? t("contents.archived") : t("contents.unarchived"));
  } catch {
    /* 409 已提示 */
  } finally {
    acting.value = 0;
  }
}

async function removeContent(row: Content) {
  try {
    await ElMessageBox.confirm(t("contents.deleteConfirm", { title: row.title }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await contentsApi.remove(row.id);
    ElMessage.success(t("contents.deleted"));
    void load();
  } catch {
    /* 已提示 */
  } finally {
    acting.value = 0;
  }
}

async function exportOne(row: Content, format: ContentExportFormat) {
  try {
    const { blob, filename } = await contentsApi.exportContent(row.id, format);
    downloadBlob(blob, filename || `content-${row.id}.${format}`);
  } catch {
    /* 已提示 */
  }
}

function openEditor(row: Content) {
  void router.push({ name: "content-edit", params: { id: row.id } });
}

// ---------- 新建 / 生成 ----------
const createVisible = ref(false);
const generateVisible = ref(false);

function openCreate() {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  createVisible.value = true;
}

function openGenerate() {
  if (!projectId.value) {
    ElMessage.warning(t("common.selectProjectFirst"));
    return;
  }
  generateVisible.value = true;
}

function onCreated(content: Content) {
  void router.push({ name: "content-edit", params: { id: content.id } });
}

function onGenerated(res: ContentGenerateResult) {
  filters.batch_id = res.batch_id;
  statusTab.value = "all";
  search();
}

// ---------- 进入页面 ----------
function queryInt(key: string): number | undefined {
  const n = Number(route.query[key]);
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

onMounted(() => {
  filters.keyword_id = queryInt("keyword_id");
  filters.title_id = queryInt("title_id");
  filters.batch_id = queryInt("batch_id");
  const status = route.query.status;
  if (typeof status === "string" && (CONTENT_STATUS as readonly string[]).includes(status)) statusTab.value = status as ContentStatus;
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void loadKeywordOptions();
  void load();
});

const tabs: StatusTab[] = ["all", ...CONTENT_STATUS];
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.contents") }}</h2>
        <div v-if="projectId" class="header-actions">
          <el-button v-if="canGenerate" type="primary" :icon="MagicStick" @click="openGenerate">{{ t("contents.generate") }}</el-button>
          <el-button v-permission="'content.contents.create'" :icon="Plus" @click="openCreate">{{ t("contents.create.title") }}</el-button>
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
        <el-tab-pane v-for="tab in tabs" :key="tab" :name="tab" :label="tab === 'all' ? t('common.all') : t(`contents.tabs.${tab}`)" />
      </el-tabs>

      <div class="toolbar">
        <el-input v-model="filters.keyword" :placeholder="t('contents.searchPlaceholder')" clearable style="width: 200px" @keyup.enter="search" @clear="search" />
        <el-select v-model="filters.keyword_id" filterable clearable :placeholder="t('contents.mainKeyword')" style="width: 170px" @change="search">
          <el-option v-for="kw in keywordOptions" :key="kw.id" :value="kw.id" :label="kw.keyword" />
          <el-option v-if="filters.keyword_id && !keywordOptions.some((k) => k.id === filters.keyword_id)" :value="filters.keyword_id" :label="keywordNames[filters.keyword_id] || `#${filters.keyword_id}`" />
        </el-select>
        <el-input-number v-model="filters.title_id" :min="1" :controls="false" :placeholder="t('contents.titleId')" style="width: 100px" @change="search" />
        <el-input-number v-model="filters.batch_id" :min="1" :controls="false" :placeholder="t('keywords.batchId')" style="width: 100px" @change="search" />
        <el-select v-model="filters.has_links" clearable :placeholder="t('contents.hasLinks')" style="width: 120px" @change="search">
          <el-option :value="true" :label="t('contents.withLinks')" />
          <el-option :value="false" :label="t('contents.withoutLinks')" />
        </el-select>
        <el-checkbox v-model="filters.mine" @change="search">{{ t("contents.mine") }}</el-checkbox>
        <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
        <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
        <span class="spacer" />
        <el-button :icon="Refresh" @click="load()">{{ t("common.refresh") }}</el-button>
      </div>

      <el-table v-loading="loading" :data="rows" row-key="id" stripe>
        <el-table-column :label="t('contents.titleCol')" min-width="260">
          <template #default="{ row }">
            <el-link type="primary" :underline="false" class="title-link" @click="openEditor(row)">{{ row.title }}</el-link>
            <div v-if="isBusy(row)" class="row-task">
              <TaskProgress v-if="tasks[row.id]" :task="tasks[row.id]" compact :show-hint="false" />
              <el-progress v-else :percentage="0" indeterminate :stroke-width="4" :show-text="false" />
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('contents.mainKeyword')" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.keyword_id ? keywordNames[row.keyword_id] || `#${row.keyword_id}` : "-" }}</template>
        </el-table-column>
        <el-table-column :label="t('contents.style')" width="80">
          <template #default="{ row }"><StatusTag kind="content_style" :value="row.style" effect="plain" /></template>
        </el-table-column>
        <el-table-column :label="t('contents.format')" width="95">
          <template #default="{ row }">{{ t(`status.content_format.${row.format}`) }}</template>
        </el-table-column>
        <el-table-column :label="t('contents.wordCount')" width="80" align="right">
          <template #default="{ row }">{{ formatNumber(row.word_count) }}</template>
        </el-table-column>
        <el-table-column :label="t('contents.quality')" width="80" align="right">
          <template #default="{ row }">
            <el-tooltip v-if="row.quality_score != null" :disabled="!row.risk_flags?.length" placement="top">
              <template #content>
                <div v-for="f in row.risk_flags" :key="f">{{ riskLabel(f) }}</div>
              </template>
              <span :class="{ 'quality-bad': row.quality_score < CONTENT_LIMITS.qualityWarn, 'has-flags': row.risk_flags?.length }">{{ row.quality_score }}</span>
            </el-tooltip>
            <span v-else class="text-secondary">-</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('common.status')" width="95">
          <template #default="{ row }"><StatusTag kind="content_status" :value="row.status" /></template>
        </el-table-column>
        <el-table-column :label="t('contents.versionCount')" width="70" align="right" prop="version_count" />
        <el-table-column :label="t('contents.linkCount')" width="70" align="right" prop="link_count" />
        <el-table-column :label="t('common.updatedAt')" width="160">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="250" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEditor(row)">{{ row.status === "generating" ? t("contents.view") : t("common.edit") }}</el-button>
            <el-button v-if="allowed(row, 'submit_review') && has('content.contents.update')" link type="primary" :loading="acting === row.id" @click="submitReview(row)">
              {{ t("contents.review.submit") }}
            </el-button>
            <template v-if="allowed(row, 'approve') && has('content.contents.review')">
              <el-button link type="success" :loading="acting === row.id" @click="approve(row)">{{ t("contents.review.approve") }}</el-button>
              <el-button link type="danger" :loading="acting === row.id" @click="reject(row)">{{ t("contents.review.reject") }}</el-button>
            </template>
            <template v-if="has('content.contents.status')">
              <el-button v-if="allowed(row, 'archive')" link type="warning" :loading="acting === row.id" @click="toggleArchive(row)">{{ t("contents.archive") }}</el-button>
              <el-button v-else-if="allowed(row, 'unarchive')" link type="success" :loading="acting === row.id" @click="toggleArchive(row)">{{ t("contents.unarchive") }}</el-button>
            </template>
            <el-dropdown v-if="has('content.contents.export')" trigger="click" @command="(f: ContentExportFormat) => exportOne(row, f)">
              <el-button link type="primary" class="dropdown-btn">
                {{ t("contents.export") }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="md">Markdown</el-dropdown-item>
                  <el-dropdown-item command="html">HTML</el-dropdown-item>
                  <el-dropdown-item command="json">JSON</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button
              v-if="has('content.contents.delete') && allowed(row, 'delete')"
              link
              type="danger"
              :disabled="row.link_count > 0"
              :loading="acting === row.id"
              @click="removeContent(row)"
            >
              {{ t("common.delete") }}
            </el-button>
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
          @current-change="load()"
          @size-change="onPageSizeChange"
        />
      </div>
    </template>

    <ContentCreateDialog v-model="createVisible" :project-id="projectId" :project="project" @created="onCreated" />
    <ContentGenerateDialog v-model="generateVisible" :project-id="projectId" :project="project" selectable @success="onGenerated" />
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
.title-link {
  font-weight: 500;
  word-break: break-all;
}
.row-task {
  margin-top: 4px;
  max-width: 360px;
}
.quality-bad {
  color: var(--el-color-danger);
  font-weight: 600;
}
.has-flags {
  text-decoration: underline dotted;
  cursor: help;
}
.dropdown-btn {
  margin-left: 12px;
  vertical-align: middle;
}
</style>
