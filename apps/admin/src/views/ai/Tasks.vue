<script setup lang="ts">
// AI 任务（docs/08 §7.4、§7.7；docs/04 §6.15、§7.17；docs/13 §6.3）：
// - 顶部 row_kind 切换（根任务 / 尝试行 / 全部）与全部筛选（project_id、capability、operation、model、status、error_category、
//   trigger_type、batch_id、root_task_id、target_type、target_id、request_id、start、end）；URL 查询参数可预置筛选（批次页「查看任务」、用量页跳转）；
// - 根任务行可展开 attempts[]；行操作「重试」（ai.tasks.retry）与「取消」（ai.tasks.cancel）按状态条件禁用，409 按 existing_id / hint / current_status 提示；
// - 详情抽屉以 JsonEditor 只读展示 input / request_payload / response_meta，媒体根任务另列 poll 与 download 记录；
// - 「导出 CSV」沿用 ai.tasks.view；列表含进行中的根任务时由 usePolling 可见时 3s 刷新。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowLeft, Download, Refresh, Search } from "@element-plus/icons-vue";
import {
  AI_TASK_OPERATION,
  AI_TASK_ROW_KIND,
  AI_TASK_STATUS,
  AI_TASK_TARGET_TYPE,
  AI_TASK_TRIGGER_TYPE,
  CAPABILITIES,
  DEFAULT_PAGE_SIZE,
  ERROR_CATEGORY,
  MEDIA_KIND,
  type AiTask,
  type AiTaskOperation,
  type AiTaskRowKind,
  type AiTaskStats,
  type AiTaskStatus,
  type AiTaskTargetType,
  type AiTaskTriggerType,
  type Capability,
  type ConflictData,
  type ErrorCategory,
  type MediaKind,
} from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import { isApiError } from "@/api/client";
import JsonEditor from "@/components/JsonEditor.vue";
import ProjectSelect from "@/components/ProjectSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import TaskProgress, { isTerminalTask } from "@/components/TaskProgress.vue";
import { useNarrow } from "@/composables/useNarrow";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useProjectStore } from "@/store/project";
import { datedFilename, downloadBlob } from "@/utils/download";
import { formatCny, formatDateTime, formatDuration, formatNumber, formatQuota, toUtcIso } from "@/utils/format";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const projectStore = useProjectStore();
const { has } = usePermission();

// ---------- 筛选（可由 URL 查询参数预置） ----------
interface Filters {
  row_kind: AiTaskRowKind;
  project_id: number;
  capability: Capability | undefined;
  operation: AiTaskOperation | undefined;
  model: string;
  status: AiTaskStatus | undefined;
  error_category: ErrorCategory | undefined;
  trigger_type: AiTaskTriggerType | undefined;
  batch_id: number | undefined;
  root_task_id: number | undefined;
  target_type: AiTaskTargetType | undefined;
  target_id: number | undefined;
  request_id: string;
  range: [Date, Date] | null;
}

function q(key: string): string | undefined {
  const v = route.query[key];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === "string" && s !== "" ? s : undefined;
}

function qInt(key: string): number | undefined {
  const n = Number(q(key));
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

function qEnum<T extends string>(key: string, values: readonly T[]): T | undefined {
  const v = q(key);
  return v && (values as readonly string[]).includes(v) ? (v as T) : undefined;
}

function qRange(): [Date, Date] | null {
  const s = q("start");
  const e = q("end");
  if (!s || !e) return null;
  const sd = new Date(s);
  const ed = new Date(e);
  return Number.isNaN(sd.getTime()) || Number.isNaN(ed.getTime()) ? null : [sd, ed];
}

function initialFilters(): Filters {
  return {
    row_kind: qEnum("row_kind", AI_TASK_ROW_KIND) ?? "root",
    // project_id=0 显式表示全部项目（用量页跳转）；未带时默认顶栏当前项目
    project_id: q("project_id") === "0" ? 0 : (qInt("project_id") ?? projectStore.currentId),
    capability: qEnum("capability", CAPABILITIES),
    operation: qEnum("operation", AI_TASK_OPERATION),
    model: q("model") ?? "",
    status: qEnum("status", AI_TASK_STATUS),
    error_category: qEnum("error_category", ERROR_CATEGORY),
    trigger_type: qEnum("trigger_type", AI_TASK_TRIGGER_TYPE),
    batch_id: qInt("batch_id"),
    root_task_id: qInt("root_task_id"),
    target_type: qEnum("target_type", AI_TASK_TARGET_TYPE),
    target_id: qInt("target_id"),
    request_id: q("request_id") ?? "",
    range: qRange(),
  };
}

const filters = reactive<Filters>(initialFilters());
const showMore = ref(
  !!(filters.trigger_type || filters.batch_id || filters.root_task_id || filters.target_type || filters.target_id || filters.request_id || filters.range),
);

function queryParams(): aiApi.ExportTasksParams {
  return {
    row_kind: filters.row_kind,
    project_id: filters.project_id > 0 ? filters.project_id : undefined,
    capability: filters.capability,
    operation: filters.operation,
    model: filters.model.trim() || undefined,
    status: filters.status,
    error_category: filters.error_category,
    trigger_type: filters.trigger_type,
    batch_id: filters.batch_id,
    root_task_id: filters.root_task_id,
    target_type: filters.target_type,
    target_id: filters.target_id,
    request_id: filters.request_id.trim() || undefined,
    start: filters.range ? toUtcIso(filters.range[0]) : undefined,
    end: filters.range ? toUtcIso(filters.range[1]) : undefined,
  };
}

// ---------- 列表 ----------
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<AiTask[]>([]);
const loading = ref(false);
let loadSeq = 0;

async function load(silent = false) {
  const seq = ++loadSeq;
  if (!silent) loading.value = true;
  try {
    const res = await aiApi.listTasks({ ...queryParams(), page: page.value, page_size: pageSize.value });
    if (seq !== loadSeq) return;
    rows.value = res.items;
    total.value = res.total;
    // 已展开的根任务刷新其尝试行
    for (const id of Object.keys(attemptsMap)) {
      if (!res.items.some((r) => r.id === Number(id))) delete attemptsMap[Number(id)];
    }
    if (silent) for (const id of expandedIds) void loadAttempts(id, true);
  } catch (err) {
    if (seq !== loadSeq) return;
    if (!silent) {
      rows.value = [];
      total.value = 0;
    }
    throw err;
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

function reload() {
  load().catch(() => undefined);
}

function search() {
  page.value = 1;
  reload();
  refreshStats();
}

// ---------- 尝试行统计：P95 耗时 / 失败分类（ai_p95_duration_ms、ai_failures_by_category，docs/12 §3.2 仅详情页） ----------
// 按当前筛选的项目 / 能力 / 模型 / 时间范围（缺省最近 7 天）实时查询；展开时才请求
const statsOpen = ref<string[]>([]);
const narrow = useNarrow(767);
const stats = ref<AiTaskStats | null>(null);
const statsLoading = ref(false);
let statsSeq = 0;

async function loadStats() {
  const seq = ++statsSeq;
  statsLoading.value = true;
  const p = queryParams();
  try {
    const res = await aiApi.getTaskStats({ project_id: p.project_id, capability: p.capability, model: p.model, start: p.start, end: p.end });
    if (seq === statsSeq) stats.value = res;
  } catch {
    if (seq === statsSeq) stats.value = null;
  } finally {
    if (seq === statsSeq) statsLoading.value = false;
  }
}

function refreshStats() {
  if (statsOpen.value.length) void loadStats();
  else stats.value = null;
}

watch(statsOpen, (open) => {
  if (open.length && !stats.value && !statsLoading.value) void loadStats();
});

function resetFilters() {
  Object.assign(filters, {
    row_kind: "root",
    project_id: projectStore.currentId,
    capability: undefined,
    operation: undefined,
    model: "",
    status: undefined,
    error_category: undefined,
    trigger_type: undefined,
    batch_id: undefined,
    root_task_id: undefined,
    target_type: undefined,
    target_id: undefined,
    request_id: "",
    range: null,
  } satisfies Filters);
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

watch(() => filters.row_kind, search);

const isRoot = (row: AiTask) => row.root_task_id === null || row.root_task_id === undefined;

// 进行中的根任务：可见时 3s 刷新
const hasActiveRoots = computed(() => rows.value.some((r) => isRoot(r) && !isTerminalTask(r.status)));
const polling = usePolling(
  async () => {
    await load(true);
    if (detailVisible.value && detail.value && !isTerminalTask(detail.value.status)) await refreshDetail(true);
  },
  { interval: 3000, immediate: false },
);
watch(hasActiveRoots, (active) => (active ? polling.start() : polling.stop()));

// ---------- 展开尝试行 ----------
const attemptsMap = reactive<Record<number, AiTask[]>>({});
const attemptsLoading = reactive<Record<number, boolean>>({});
const expandedIds = new Set<number>();

async function loadAttempts(id: number, silent = false) {
  if (!silent) attemptsLoading[id] = true;
  try {
    const task = await aiApi.getTask(id, { silent });
    attemptsMap[id] = task.attempts ?? [];
  } catch {
    if (!silent) attemptsMap[id] = [];
  } finally {
    attemptsLoading[id] = false;
  }
}

function onExpandChange(row: AiTask, expanded: AiTask[]) {
  const open = expanded.some((r) => r.id === row.id);
  if (open) {
    expandedIds.add(row.id);
    if (isRoot(row) && !attemptsMap[row.id]) void loadAttempts(row.id);
  } else {
    expandedIds.delete(row.id);
  }
}

// ---------- 展示辅助 ----------
function projectLabel(id: number | null | undefined): string {
  if (!id) return "-";
  const p = projectStore.projects.find((x) => x.id === id);
  return p ? p.name : `#${id}`;
}

function tokens(row: AiTask): string {
  return `${formatNumber(row.prompt_tokens)} / ${formatNumber(row.completion_tokens)}`;
}

function quota(row: AiTask): string {
  return `${formatQuota(row.quota_estimated)} / ${row.quota_actual === null || row.quota_actual === undefined ? "-" : formatQuota(row.quota_actual)}`;
}

function candidateLabel(index: number | null | undefined): string {
  if (index === null || index === undefined) return "-";
  return index === 0 ? t("aiTasks.candidatePrimary") : String(index);
}

function usageLogTypeLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  const key = `aiUsage.logTypes.${value}`;
  return `${t(key)} (${value})`;
}

// ---------- 重试 / 取消 ----------
const RETRYABLE: readonly AiTaskStatus[] = ["failed", "expired"];
const CANCELLABLE: readonly AiTaskStatus[] = ["queued", "polling"];
const canRetry = (row: AiTask) => isRoot(row) && RETRYABLE.includes(row.status);
const canCancel = (row: AiTask) => isRoot(row) && CANCELLABLE.includes(row.status);
const acting = ref<number | null>(null);

function hintTarget(hint: string): { kind: "media" | "link" | null; id: number | null } {
  const media = /\/admin\/media\/assets\/(\d+)\//.exec(hint);
  if (media) return { kind: "media", id: Number(media[1]) };
  const link = /\/admin\/links\/(\d+)\//.exec(hint);
  if (link) return { kind: "link", id: Number(link[1]) };
  return { kind: null, id: null };
}

async function handleConflict(err: unknown, action: "retry" | "cancel") {
  if (!isApiError(err)) return;
  if (err.code !== 409) {
    ElMessage.error(err.message);
    return;
  }
  const data = (err.data && typeof err.data === "object" ? err.data : {}) as ConflictData;
  if (typeof data.existing_id === "number" && data.existing_id > 0) {
    const existing = data.existing_id;
    try {
      await ElMessageBox.confirm(t("aiTasks.retryExisting", { id: existing }), t("common.tip"), {
        type: "warning",
        confirmButtonText: t("aiTasks.viewExisting"),
        cancelButtonText: t("common.close"),
      });
      void openDetail(existing);
    } catch {
      /* 关闭 */
    }
    return;
  }
  if (typeof data.hint === "string" && data.hint) {
    const target = hintTarget(data.hint);
    const message = target.kind === "link" ? t("aiTasks.retryIndexHint", { hint: data.hint }) : t("aiTasks.retryMediaHint", { hint: data.hint });
    try {
      await ElMessageBox.confirm(message, t("common.tip"), {
        type: "info",
        confirmButtonText: target.kind === "link" ? t("aiTasks.gotoLinks") : t("aiTasks.gotoAssets"),
        cancelButtonText: t("common.close"),
      });
      if (target.kind === "link" && target.id) void router.push(`/links/${target.id}`);
      else void router.push("/media/assets");
    } catch {
      /* 关闭 */
    }
    return;
  }
  if (action === "cancel" && data.current_status === "running") {
    ElMessage.warning(t("aiTasks.cancelRunning"));
    return;
  }
  if (data.current_status) {
    ElMessage.warning(t("aiTasks.conflict", { status: t(`status.ai_task_status.${data.current_status}`) }));
    return;
  }
  ElMessage.warning(err.message || t("aiTasks.notAllowed"));
}

async function retryTask(row: AiTask) {
  try {
    await ElMessageBox.confirm(t("aiTasks.retryConfirm", { id: row.id }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    const res = await aiApi.retryTask(row.id, { silent: true });
    ElMessage.success(t("aiTasks.retryDone", { id: res.task_id }));
    reload();
    if (detailVisible.value && detail.value?.id === row.id) void refreshDetail(true);
  } catch (err) {
    await handleConflict(err, "retry");
  } finally {
    acting.value = null;
  }
}

async function cancelTask(row: AiTask) {
  try {
    await ElMessageBox.confirm(t("aiTasks.cancelConfirm", { id: row.id }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    const updated = await aiApi.cancelTask(row.id, { silent: true });
    ElMessage.success(t("aiTasks.cancelDone"));
    const idx = rows.value.findIndex((r) => r.id === row.id);
    if (idx >= 0) rows.value[idx] = { ...rows.value[idx], ...updated };
    reload();
    if (detailVisible.value && detail.value?.id === row.id) void refreshDetail(true);
  } catch (err) {
    await handleConflict(err, "cancel");
  } finally {
    acting.value = null;
  }
}

// ---------- 导出 ----------
const exporting = ref(false);

async function exportCsv() {
  exporting.value = true;
  try {
    const { blob, filename } = await aiApi.exportTasks(queryParams());
    downloadBlob(blob, filename || datedFilename("ai-tasks", "csv"));
  } catch {
    /* 拦截器已提示（超过 50,000 行为 400） */
  } finally {
    exporting.value = false;
  }
}

// ---------- 详情抽屉 ----------
const detailVisible = ref(false);
const detailLoading = ref(false);
const detail = ref<AiTask | null>(null);
const detailHistory = ref<number[]>([]);

/** push=true：从当前详情跳转（可返回）；否则重新打开并清空返回栈 */
async function openDetail(id: number, push = false) {
  if (push && detail.value && detail.value.id !== id) detailHistory.value.push(detail.value.id);
  if (!push) detailHistory.value = [];
  detailVisible.value = true;
  detailLoading.value = true;
  try {
    detail.value = await aiApi.getTask(id);
  } catch {
    if (!detail.value || detail.value.id !== id) detailVisible.value = false;
  } finally {
    detailLoading.value = false;
  }
}

async function refreshDetail(silent = false) {
  if (!detail.value) return;
  try {
    detail.value = await aiApi.getTask(detail.value.id, { silent });
  } catch {
    /* 忽略 */
  }
}

async function backDetail() {
  const prev = detailHistory.value.pop();
  if (!prev) return;
  detailLoading.value = true;
  try {
    detail.value = await aiApi.getTask(prev);
  } catch {
    /* 拦截器已提示 */
  } finally {
    detailLoading.value = false;
  }
}

function viewAttempts(row: AiTask) {
  filters.row_kind = "attempt";
  filters.root_task_id = row.id;
  showMore.value = true;
  detailVisible.value = false;
}

const meta = computed(() => detail.value?.response_meta ?? null);
const poll = computed(() => meta.value?.poll ?? null);
const download = computed(() => meta.value?.download ?? null);
const detailIsRoot = computed(() => !!detail.value && isRoot(detail.value));
// 媒体任务的 output_excerpt 是上游临时 URL：后端已返回 null，这里再兜底不展示（docs/10 §2.2、§13 第 1 条）；改为链到素材详情（转存后的 url）
const detailIsMedia = computed(() => !!detail.value && MEDIA_KIND.includes(detail.value.capability as MediaKind));
function openAsset(id: number) {
  detailVisible.value = false;
  void router.push({ path: "/media/assets", query: { id: String(id) } });
}

// URL 中带 open=<id> 时直接打开该任务详情（用量对账页跳转）
onMounted(() => {
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  reload();
  const openId = qInt("open");
  if (openId) void openDetail(openId);
});

const rowKindOptions = computed(() => AI_TASK_ROW_KIND.map((k) => ({ value: k, label: t(`aiTasks.rowKind.${k}`) })));
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("aiTasks.title") }}</h2>
        <div class="header-actions">
          <el-radio-group v-model="filters.row_kind">
            <el-radio-button v-for="opt in rowKindOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</el-radio-button>
          </el-radio-group>
          <el-tooltip :content="t('aiTasks.exportTip')" placement="top">
            <el-button v-permission="'ai.tasks.view'" :icon="Download" :loading="exporting" @click="exportCsv">{{ t("aiTasks.export") }}</el-button>
          </el-tooltip>
        </div>
      </div>
    </template>

    <div class="toolbar">
      <ProjectSelect v-model="filters.project_id" allow-all width="180px" />
      <el-select v-model="filters.capability" :placeholder="t('aiTasks.capability')" clearable style="width: 130px">
        <el-option v-for="c in CAPABILITIES" :key="c" :label="t(`status.capability.${c}`)" :value="c" />
      </el-select>
      <el-select v-model="filters.operation" :placeholder="t('aiTasks.operation')" clearable filterable style="width: 150px">
        <el-option v-for="o in AI_TASK_OPERATION" :key="o" :label="t(`status.ai_task_operation.${o}`)" :value="o" />
      </el-select>
      <el-select v-model="filters.status" :placeholder="t('aiTasks.status')" clearable style="width: 120px">
        <el-option v-for="s in AI_TASK_STATUS" :key="s" :label="t(`status.ai_task_status.${s}`)" :value="s" />
      </el-select>
      <el-select v-model="filters.error_category" :placeholder="t('aiTasks.errorCategory')" clearable filterable style="width: 150px">
        <el-option v-for="c in ERROR_CATEGORY" :key="c" :label="t(`status.error_category.${c}`)" :value="c" />
      </el-select>
      <el-input v-model="filters.model" :placeholder="t('aiTasks.modelPlaceholder')" clearable style="width: 160px" @keyup.enter="search" />
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button :icon="Refresh" @click="resetFilters">{{ t("common.reset") }}</el-button>
      <el-button link type="primary" @click="showMore = !showMore">{{ showMore ? t("aiTasks.lessFilters") : t("aiTasks.moreFilters") }}</el-button>
    </div>
    <div v-show="showMore" class="toolbar">
      <el-select v-model="filters.trigger_type" :placeholder="t('aiTasks.triggerType')" clearable style="width: 130px">
        <el-option v-for="tt in AI_TASK_TRIGGER_TYPE" :key="tt" :label="t(`status.ai_task_trigger_type.${tt}`)" :value="tt" />
      </el-select>
      <el-select v-model="filters.target_type" :placeholder="t('aiTasks.targetType')" clearable style="width: 130px">
        <el-option v-for="tt in AI_TASK_TARGET_TYPE" :key="tt" :label="t(`status.ai_task_target_type.${tt}`)" :value="tt" />
      </el-select>
      <el-input-number v-model="filters.target_id" :placeholder="t('aiTasks.targetId')" :min="1" :controls="false" style="width: 110px" />
      <el-input-number v-model="filters.batch_id" :placeholder="t('aiTasks.batchId')" :min="1" :controls="false" style="width: 110px" />
      <el-input-number v-model="filters.root_task_id" :placeholder="t('aiTasks.rootTaskId')" :min="1" :controls="false" style="width: 120px" />
      <el-input v-model="filters.request_id" :placeholder="t('aiTasks.requestId')" clearable style="width: 220px" @keyup.enter="search" />
      <el-date-picker
        v-model="filters.range"
        type="datetimerange"
        :start-placeholder="t('aiTasks.startTime')"
        :end-placeholder="t('aiTasks.endTime')"
        :range-separator="'~'"
        style="width: 360px"
      />
    </div>
    <div v-if="hasActiveRoots" class="auto-refresh text-secondary">{{ t("aiTasks.autoRefresh") }}</div>

    <el-collapse v-model="statsOpen" class="stats-collapse">
      <el-collapse-item name="stats">
        <template #title>
          <span class="stats-title">{{ t("aiTasks.stats.title") }}</span>
          <span v-if="stats" class="text-secondary stats-range">{{ formatDateTime(stats.start, false) }} ~ {{ formatDateTime(stats.end, false) }}</span>
        </template>
        <div v-loading="statsLoading" class="stats-body">
          <template v-if="stats">
            <div class="text-secondary stats-hint">{{ t("aiTasks.stats.hint") }}</div>
            <el-descriptions :column="narrow ? 1 : 4" border size="small">
              <el-descriptions-item :label="t('aiTasks.stats.attempts')">{{ formatNumber(stats.attempts) }}</el-descriptions-item>
              <el-descriptions-item :label="t('aiTasks.stats.succeeded')">{{ formatNumber(stats.succeeded) }}</el-descriptions-item>
              <el-descriptions-item :label="t('aiTasks.stats.failed')">{{ formatNumber(stats.failed) }}</el-descriptions-item>
              <el-descriptions-item :label="t('aiTasks.stats.p95')">{{ stats.p95_duration_ms === null ? "-" : formatDuration(stats.p95_duration_ms) }}</el-descriptions-item>
              <el-descriptions-item :label="t('aiTasks.stats.failuresByCategory')" :span="narrow ? 1 : 4">
                <span v-if="!stats.failures_by_category.length">-</span>
                <span v-else class="tag-list">
                  <el-tag v-for="c in stats.failures_by_category" :key="c.error_category" type="danger" effect="plain" size="small">
                    {{ t(`status.error_category.${c.error_category}`) }} × {{ formatNumber(c.count) }}
                  </el-tag>
                </span>
              </el-descriptions-item>
            </el-descriptions>
            <el-alert v-if="stats.warnings.includes('p95_sampled')" type="info" :closable="false" show-icon :title="t('aiTasks.stats.sampled')" class="stats-warning" />
            <el-table :data="stats.by_model" size="small" border class="stats-table" :empty-text="t('aiTasks.stats.empty')">
              <el-table-column :label="t('aiTasks.capability')" width="110">
                <template #default="{ row: g }">{{ t(`status.capability.${g.capability}`) }}</template>
              </el-table-column>
              <el-table-column prop="model" :label="t('aiTasks.model')" min-width="160" show-overflow-tooltip />
              <el-table-column :label="t('aiTasks.stats.attempts')" width="90" align="right">
                <template #default="{ row: g }">{{ formatNumber(g.attempts) }}</template>
              </el-table-column>
              <el-table-column :label="t('aiTasks.stats.failed')" width="90" align="right">
                <template #default="{ row: g }">{{ formatNumber(g.failed) }}</template>
              </el-table-column>
              <el-table-column :label="t('aiTasks.stats.p95')" width="120" align="right">
                <template #default="{ row: g }">{{ g.p95_duration_ms === null ? "-" : formatDuration(g.p95_duration_ms) }}</template>
              </el-table-column>
              <el-table-column :label="t('aiTasks.stats.failuresByCategory')" min-width="220">
                <template #default="{ row: g }">
                  <span v-if="!g.failures_by_category.length">-</span>
                  <span v-else class="tag-list">
                    <el-tag v-for="c in g.failures_by_category" :key="c.error_category" type="danger" effect="plain" size="small">
                      {{ t(`status.error_category.${c.error_category}`) }} × {{ formatNumber(c.count) }}
                    </el-tag>
                  </span>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </div>
      </el-collapse-item>
    </el-collapse>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe @expand-change="onExpandChange">
      <el-table-column type="expand" width="40">
        <template #default="{ row }">
          <div class="expand">
            <template v-if="isRoot(row)">
              <TaskProgress :task="row" class="expand-progress" />
              <el-table v-loading="attemptsLoading[row.id]" :data="attemptsMap[row.id] ?? []" size="small" border :empty-text="t('aiTasks.noAttempts')">
                <el-table-column :label="t('common.id')" width="90">
                  <template #default="{ row: a }"><el-button link type="primary" @click="openDetail(a.id)">#{{ a.id }}</el-button></template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.candidateIndex')" width="70">
                  <template #default="{ row: a }">{{ candidateLabel(a.candidate_index) }}</template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.attempt')" width="70" prop="attempt" />
                <el-table-column :label="t('aiTasks.segmentIndex')" width="70">
                  <template #default="{ row: a }">{{ a.segment_index ?? "-" }}</template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.model')" min-width="150">
                  <template #default="{ row: a }"><span class="mono">{{ a.model || "-" }}</span></template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.protocol')" width="140">
                  <template #default="{ row: a }"><StatusTag kind="protocol" :value="a.protocol" effect="plain" /></template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.status')" width="90">
                  <template #default="{ row: a }"><StatusTag kind="ai_task_status" :value="a.status" /></template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.requestId')" min-width="170">
                  <template #default="{ row: a }"><span class="mono">{{ a.request_id || "-" }}</span></template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.httpStatus')" width="70">
                  <template #default="{ row: a }">{{ a.http_status ?? "-" }}</template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.errorCategory')" width="130">
                  <template #default="{ row: a }"><StatusTag kind="error_category" :value="a.error_category" /></template>
                </el-table-column>
                <el-table-column :label="t('aiTasks.upstreamLatency')" width="100" align="right">
                  <template #default="{ row: a }">{{ formatDuration(a.upstream_latency_ms) }}</template>
                </el-table-column>
              </el-table>
            </template>
            <span v-else class="text-secondary">{{ t("aiTasks.attemptOf", { id: row.root_task_id }) }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.id')" width="100">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row.id)">#{{ row.id }}</el-button>
          <div v-if="!isRoot(row)" class="sub text-secondary">↳ #{{ row.root_task_id }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.capability')" width="100">
        <template #default="{ row }"><StatusTag kind="capability" :value="row.capability" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.operation')" min-width="120">
        <template #default="{ row }">{{ t(`status.ai_task_operation.${row.operation}`) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.model')" min-width="150">
        <template #default="{ row }"><span class="mono">{{ row.model || "-" }}</span></template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.protocol')" width="140">
        <template #default="{ row }"><StatusTag kind="protocol" :value="row.protocol" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.status')" width="110">
        <template #default="{ row }">
          <StatusTag kind="ai_task_status" :value="row.status" />
          <el-progress
            v-if="isRoot(row) && row.status === 'polling'"
            :percentage="Math.max(0, Math.min(100, row.progress || 0))"
            :stroke-width="4"
            :show-text="false"
            class="row-progress"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.errorCategory')" width="130">
        <template #default="{ row }">
          <el-tooltip :disabled="!row.error_message" :content="row.error_message || ''" placement="top">
            <StatusTag kind="error_category" :value="row.error_category" />
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.requestId')" min-width="170">
        <template #default="{ row }"><span class="mono">{{ row.request_id || "-" }}</span></template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.tokens')" width="130" align="right">
        <template #default="{ row }">{{ tokens(row) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.quota')" width="150" align="right">
        <template #default="{ row }">{{ quota(row) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.cost')" width="100" align="right">
        <template #default="{ row }">{{ formatCny(row.cost_cny) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.duration')" width="90" align="right">
        <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiTasks.project')" min-width="110">
        <template #default="{ row }">{{ projectLabel(row.project_id) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.createdAt')" width="165">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="170" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row.id)">{{ t("aiTasks.detailAction") }}</el-button>
          <template v-if="isRoot(row)">
            <span v-permission="'ai.tasks.retry'">
              <el-tooltip :content="t('aiTasks.retryDisabledTip')" :disabled="canRetry(row)" placement="top">
                <el-button link type="warning" :disabled="!canRetry(row)" :loading="acting === row.id" @click="retryTask(row)">{{ t("aiTasks.retry") }}</el-button>
              </el-tooltip>
            </span>
            <span v-permission="'ai.tasks.cancel'">
              <el-tooltip :content="t('aiTasks.cancelDisabledTip')" :disabled="canCancel(row)" placement="top">
                <el-button link type="danger" :disabled="!canCancel(row)" :loading="acting === row.id" @click="cancelTask(row)">{{ t("aiTasks.cancel") }}</el-button>
              </el-tooltip>
            </span>
          </template>
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
        @current-change="reload"
        @size-change="onPageSizeChange"
      />
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" size="min(760px, 100%)" :title="detail ? t('aiTasks.detail', { id: detail.id }) : ''" destroy-on-close>
      <div v-if="detail" v-loading="detailLoading" class="detail">
        <div class="detail-actions">
          <el-button v-if="detailHistory.length" :icon="ArrowLeft" size="small" @click="backDetail">{{ t("common.back") }}</el-button>
          <el-button :icon="Refresh" size="small" @click="refreshDetail()">{{ t("common.refresh") }}</el-button>
          <span class="spacer" />
          <template v-if="detailIsRoot">
            <el-button v-permission="'ai.tasks.retry'" size="small" type="warning" plain :disabled="!canRetry(detail)" @click="retryTask(detail)">{{ t("aiTasks.retry") }}</el-button>
            <el-button v-permission="'ai.tasks.cancel'" size="small" type="danger" plain :disabled="!canCancel(detail)" @click="cancelTask(detail)">{{ t("aiTasks.cancel") }}</el-button>
          </template>
        </div>

        <template v-if="detailIsRoot">
          <h4 class="section-title">{{ t("aiTasks.sections.progress") }}</h4>
          <TaskProgress :task="detail" />
        </template>

        <h4 class="section-title">{{ t("aiTasks.sections.basic") }}</h4>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item :label="t('aiTasks.kind')">
            <template v-if="detailIsRoot">{{ t("aiTasks.rootRow") }}</template>
            <el-button v-else link type="primary" @click="openDetail(detail.root_task_id as number, true)">
              {{ t("aiTasks.attemptOf", { id: detail.root_task_id }) }}
            </el-button>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.status')"><StatusTag kind="ai_task_status" :value="detail.status" /></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.capability')"><StatusTag kind="capability" :value="detail.capability" effect="plain" /></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.operation')">{{ t(`status.ai_task_operation.${detail.operation}`) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.model')"><span class="mono">{{ detail.model || "-" }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.protocol')"><StatusTag kind="protocol" :value="detail.protocol" effect="plain" /></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.candidateIndex')">{{ candidateLabel(detail.candidate_index) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.attempt')">{{ detail.attempt }}<template v-if="detail.segment_index"> · {{ t("aiTasks.segmentIndex") }} {{ detail.segment_index }}</template></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.triggerType')"><StatusTag kind="ai_task_trigger_type" :value="detail.trigger_type" effect="plain" /></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.project')">{{ projectLabel(detail.project_id) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.target')">
            <el-button
              v-if="detail.target_type === 'media_asset' && detail.target_id && has('media.assets.view')"
              link
              type="primary"
              @click="openAsset(detail.target_id)"
            >
              {{ t(`status.ai_task_target_type.${detail.target_type}`) }} #{{ detail.target_id }}
            </el-button>
            <template v-else-if="detail.target_type">{{ t(`status.ai_task_target_type.${detail.target_type}`) }} #{{ detail.target_id ?? "-" }}</template>
            <template v-else>-</template>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.batchId')">{{ detail.batch_id ?? "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.routeId')">{{ detail.route_id ?? "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.templateId')">{{ detail.template_id ?? "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.parentTaskId')">
            <el-button v-if="detail.parent_task_id" link type="primary" @click="openDetail(detail.parent_task_id, true)">#{{ detail.parent_task_id }}</el-button>
            <template v-else>-</template>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.requestId')"><span class="mono">{{ detail.request_id || "-" }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.upstreamTaskId')"><span class="mono">{{ detail.upstream_task_id || "-" }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.httpStatus')">{{ detail.http_status ?? "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.duration')">{{ formatDuration(detail.duration_ms) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.upstreamLatency')">{{ formatDuration(detail.upstream_latency_ms) }}</el-descriptions-item>
          <el-descriptions-item v-if="detailIsRoot" :label="t('aiTasks.fields.pauseCount')">{{ detail.pause_count }}</el-descriptions-item>
          <template v-if="detailIsRoot && (detail.upstream_task_id || detail.poll_count)">
            <el-descriptions-item :label="t('aiTasks.fields.progress')">{{ detail.progress }}%</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.pollCount')">{{ detail.poll_count }}</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.nextPollAt')">{{ formatDateTime(detail.next_poll_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.deadlineAt')">{{ formatDateTime(detail.deadline_at) }}</el-descriptions-item>
          </template>
          <el-descriptions-item :label="t('aiTasks.fields.lockedBy')"><span class="mono">{{ detail.locked_by || "-" }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.heartbeatAt')">{{ formatDateTime(detail.heartbeat_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.startedAt')">{{ formatDateTime(detail.started_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.finishedAt')">{{ formatDateTime(detail.finished_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.createdBy')">{{ detail.created_by ? `#${detail.created_by}` : "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('common.createdAt')">{{ formatDateTime(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.output_excerpt && !detailIsMedia" :label="t('aiTasks.fields.outputExcerpt')" :span="2">
            <div class="excerpt">{{ detail.output_excerpt }}</div>
          </el-descriptions-item>
        </el-descriptions>

        <template v-if="detail.error_category || detail.error_message">
          <h4 class="section-title">{{ t("aiTasks.sections.error") }}</h4>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item :label="t('aiTasks.errorCategory')"><StatusTag kind="error_category" :value="detail.error_category" /></el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.errorMessage')"><div class="excerpt">{{ detail.error_message || "-" }}</div></el-descriptions-item>
          </el-descriptions>
        </template>

        <h4 class="section-title">{{ t("aiTasks.sections.usage") }}</h4>
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item :label="t('aiTasks.fields.promptTokens')">{{ formatNumber(detail.prompt_tokens) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.completionTokens')">{{ formatNumber(detail.completion_tokens) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.cacheTokens')">{{ formatNumber(detail.cache_tokens) }}</el-descriptions-item>
          <el-descriptions-item v-if="detailIsRoot" :label="t('aiTasks.fields.quotaReserved')">{{ formatQuota(detail.quota_reserved) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.quotaEstimated')">{{ formatQuota(detail.quota_estimated) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.quotaActual')">
            <template v-if="detail.quota_actual !== null && detail.quota_actual !== undefined">{{ formatQuota(detail.quota_actual) }}</template>
            <el-tag v-else size="small" type="info">{{ t("aiTasks.fields.notReconciled") }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.cost')">{{ formatCny(detail.cost_cny) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.reconciledAt')">{{ formatDateTime(detail.reconciled_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiTasks.fields.usageLogType')">{{ usageLogTypeLabel(detail.usage_log_type) }}</el-descriptions-item>
        </el-descriptions>

        <!-- response_meta 要点 -->
        <template v-if="meta">
          <el-descriptions
            v-if="meta.finish_reason || meta.usage_missing || meta.degraded_params?.length || meta.fallback_from || meta.retry_request_ids?.length || meta.stale_after_submit || meta.apply_counts"
            :column="1"
            border
            size="small"
            class="meta-desc"
          >
            <el-descriptions-item v-if="meta.finish_reason" :label="t('aiTasks.fields.finishReason')">{{ meta.finish_reason }}</el-descriptions-item>
            <el-descriptions-item v-if="meta.usage_missing" :label="t('aiTasks.fields.usageMissing')"><el-tag size="small" type="warning">{{ t("common.yes") }}</el-tag></el-descriptions-item>
            <el-descriptions-item v-if="meta.degraded_params?.length" :label="t('aiTasks.fields.degradedParams')">
              <span class="tag-list"><el-tag v-for="p in meta.degraded_params" :key="p" size="small" type="warning" effect="plain" class="mono">{{ p }}</el-tag></span>
            </el-descriptions-item>
            <el-descriptions-item v-if="meta.fallback_from" :label="t('aiTasks.fields.fallbackFrom')">
              <el-button link type="primary" @click="openDetail(meta.fallback_from as number, true)">#{{ meta.fallback_from }}</el-button>
            </el-descriptions-item>
            <el-descriptions-item v-if="meta.retry_request_ids?.length" :label="t('aiTasks.fields.retryRequestIds')">
              <div class="mono id-list">{{ meta.retry_request_ids.join(", ") }}</div>
            </el-descriptions-item>
            <el-descriptions-item v-if="meta.stale_after_submit" :label="t('aiTasks.fields.staleAfterSubmit')"><el-tag size="small" type="danger">{{ t("common.yes") }}</el-tag></el-descriptions-item>
            <el-descriptions-item v-if="meta.apply_counts" :label="t('aiTasks.fields.applyCounts')">
              <span class="mono">{{ Object.entries(meta.apply_counts).map(([k, v]) => `${k}=${v}`).join(", ") }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </template>

        <template v-if="poll">
          <h4 class="section-title">{{ t("aiTasks.sections.poll") }}</h4>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item :label="t('aiTasks.fields.lastRequestId')"><span class="mono">{{ poll.last_request_id || "-" }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.lastHttpStatus')">{{ poll.last_http_status ?? "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.errorCode')"><span class="mono">{{ poll.error_code || "-" }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.consecutive404')">{{ poll.consecutive_404 ?? 0 }}</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.errorMessage')" :span="2">{{ poll.error_message || "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.requestIds')" :span="2">
              <div class="mono id-list">{{ poll.request_ids?.length ? poll.request_ids.join(", ") : "-" }}</div>
            </el-descriptions-item>
          </el-descriptions>
        </template>

        <template v-if="download">
          <h4 class="section-title">{{ t("aiTasks.sections.download") }}</h4>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item :label="t('aiTasks.fields.source')">{{ t(`aiTasks.downloadSource.${download.source}`) }} ({{ download.source }})</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.httpStatus')">{{ download.http_status ?? "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.requestId')" :span="2"><span class="mono">{{ download.request_id || "-" }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('aiTasks.fields.requestIds')" :span="2">
              <div class="mono id-list">{{ download.request_ids?.length ? download.request_ids.join(", ") : "-" }}</div>
            </el-descriptions-item>
          </el-descriptions>
        </template>

        <template v-if="detailIsRoot">
          <div class="section-head">
            <h4 class="section-title">{{ t("aiTasks.attempts") }}（{{ detail.attempts?.length ?? 0 }}）</h4>
            <el-button link type="primary" size="small" @click="viewAttempts(detail)">{{ t("aiTasks.rowKind.attempt") }} →</el-button>
          </div>
          <el-table :data="detail.attempts ?? []" size="small" border :empty-text="t('aiTasks.noAttempts')">
            <el-table-column :label="t('common.id')" width="90">
              <template #default="{ row: a }"><el-button link type="primary" @click="openDetail(a.id, true)">#{{ a.id }}</el-button></template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.candidateIndex')" width="64">
              <template #default="{ row: a }">{{ candidateLabel(a.candidate_index) }}</template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.attempt')" width="64" prop="attempt" />
            <el-table-column :label="t('aiTasks.segmentIndex')" width="64">
              <template #default="{ row: a }">{{ a.segment_index ?? "-" }}</template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.protocol')" width="130">
              <template #default="{ row: a }"><StatusTag kind="protocol" :value="a.protocol" effect="plain" /></template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.requestId')" min-width="150">
              <template #default="{ row: a }"><span class="mono">{{ a.request_id || "-" }}</span></template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.httpStatus')" width="64">
              <template #default="{ row: a }">{{ a.http_status ?? "-" }}</template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.errorCategory')" width="120">
              <template #default="{ row: a }"><StatusTag kind="error_category" :value="a.error_category" /></template>
            </el-table-column>
            <el-table-column :label="t('aiTasks.upstreamLatency')" width="90" align="right">
              <template #default="{ row: a }">{{ formatDuration(a.upstream_latency_ms) }}</template>
            </el-table-column>
          </el-table>
        </template>

        <h4 class="section-title">{{ t("aiTasks.sections.input") }}</h4>
        <JsonEditor :model-value="detail.input ?? null" readonly :rows="8" />
        <h4 class="section-title">{{ t("aiTasks.sections.requestPayload") }}</h4>
        <JsonEditor :model-value="detail.request_payload ?? null" readonly :rows="10" />
        <h4 class="section-title">{{ t("aiTasks.sections.responseMeta") }}</h4>
        <JsonEditor :model-value="detail.response_meta ?? null" readonly :rows="10" />
      </div>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.auto-refresh {
  font-size: 12px;
  margin: -4px 0 8px;
}
.sub {
  font-size: 12px;
}
.row-progress {
  margin-top: 4px;
}
.expand {
  padding: 4px 16px 8px 48px;
}
.expand-progress {
  margin-bottom: 8px;
  max-width: 640px;
}
.detail-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.detail-actions .spacer {
  flex: 1;
}
.section-title {
  margin: 16px 0 8px;
  font-size: 14px;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.meta-desc {
  margin-top: 8px;
}
.excerpt {
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 160px;
  overflow: auto;
}
.id-list {
  word-break: break-all;
}
.tag-list {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
.stats-collapse {
  margin-bottom: 12px;
}
.stats-collapse :deep(.el-collapse-item__header) {
  height: auto;
  min-height: 48px;
  line-height: 1.4;
  padding: 8px 0;
  flex-wrap: wrap;
}
.stats-title {
  font-weight: 600;
}
.stats-range {
  margin-left: 12px;
  font-size: 12px;
}
.stats-body {
  min-height: 48px;
}
.stats-hint {
  font-size: 12px;
  margin-bottom: 8px;
}
.stats-warning,
.stats-table {
  margin-top: 8px;
}
</style>
