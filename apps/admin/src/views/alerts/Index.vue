<script setup lang="ts">
// 告警中心（docs/11 §10、§11.7；docs/04 §6.19、§7.13；docs/13 §11）：
// - 摘要卡片（GET /alerts/summary）：按 severity 的 open / acknowledged 数、今日新增 / 解决；点击卡片按该级别筛选未处理告警；
// - 列表：severity、类型、标题（+ message）、目标（target_type + 跳转：publish_link → 链接详情、content → 内容编辑器、ai_task → AI 任务、
//   ai_model / capability_route → 能力路由页、media_asset → 素材库）、项目、trigger_count、首次 / 最近触发、状态、处理人；
// - 筛选：status / severity / alert_type / 项目 / target_type / target_id / 时间区间（last_triggered_at）；路由 query 可预置
//   （链接详情时间线带 target_type=publish_link&target_id=…，铃铛带 status=open[&severity=critical]）；
// - 操作（monitoring.alerts.handle）：确认、解决（填 note）、忽略（二次确认）、批量解决（勾选 open / acknowledged 行，填 note）；
//   处理后刷新列表、摘要与顶栏铃铛（store/alerts）；
// - 数据范围：普通用户只含本人项目的告警，系统告警（project_id 为空）只对总后台可见；用户视角下 GET 由 client.ts 附加 owner_id。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type TableInstance } from "element-plus";
import { Check, CircleClose, Finished, Refresh, Search } from "@element-plus/icons-vue";
import {
  ALERT_SEVERITY,
  ALERT_STATUS,
  ALERT_TARGET_TYPE,
  ALERT_TYPE,
  BATCH_IDS_MAX,
  DEFAULT_PAGE_SIZE,
  type Alert,
  type AlertSeverity,
  type AlertStatus,
  type AlertSummary,
  type AlertTargetType,
  type AlertType,
  type BatchUpdateResult,
} from "@aicreat/shared";
import * as alertsApi from "@/api/alerts";
import JsonEditor from "@/components/JsonEditor.vue";
import ProjectSelect from "@/components/ProjectSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import { useNarrow } from "@/composables/useNarrow";
import { usePermission } from "@/composables/usePermission";
import { useProject } from "@/composables/useProject";
import { useAlertsStore } from "@/store/alerts";
import { useAuthStore } from "@/store/auth";
import { formatDateTime, formatNumber, toUtcIso } from "@/utils/format";

const MAX_NOTE = 500;
const ACTIVE: AlertStatus[] = ["open", "acknowledged"];

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { has } = usePermission();
const auth = useAuthStore();
const { projectId: globalProjectId, store: projectStore } = useProject();
const alertsStore = useAlertsStore();
const narrow = useNarrow();

const canHandle = computed(() => has("monitoring.alerts.handle"));

// ---------- 摘要 ----------
const summary = ref<AlertSummary | null>(null);
const summaryLoading = ref(false);

async function loadSummary() {
  summaryLoading.value = true;
  try {
    summary.value = await alertsApi.summary({ silent: true });
  } catch {
    /* 摘要失败不阻塞列表 */
  } finally {
    summaryLoading.value = false;
  }
}

const severityCards = computed(() =>
  [...ALERT_SEVERITY].reverse().map((sev) => ({
    sev,
    open: Number(summary.value?.open?.[sev]) || 0,
    acknowledged: Number(summary.value?.acknowledged?.[sev]) || 0,
  })),
);

function filterBySeverity(sev: AlertSeverity) {
  filters.status = "open";
  filters.severity = sev;
  search();
}

// ---------- 筛选与列表 ----------
const filters = reactive({
  status: undefined as AlertStatus | undefined,
  severity: undefined as AlertSeverity | undefined,
  alert_type: undefined as AlertType | undefined,
  project_id: 0,
  target_type: undefined as AlertTargetType | undefined,
  target_id: undefined as number | undefined,
  range: null as [Date, Date] | null,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<Alert[]>([]);
const loading = ref(false);
let loadSeq = 0;

function queryParams(): alertsApi.ListAlertsParams {
  const [start, end] = filters.range ?? [null, null];
  return {
    status: filters.status,
    severity: filters.severity,
    alert_type: filters.alert_type,
    project_id: filters.project_id || undefined,
    target_type: filters.target_type,
    target_id: filters.target_id || undefined,
    start: start ? toUtcIso(start) : undefined,
    end: end ? toUtcIso(end) : undefined,
  };
}

async function load() {
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const res = await alertsApi.list({ ...queryParams(), page: page.value, page_size: pageSize.value });
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

/** 列表 + 摘要 + 顶栏铃铛 */
function reloadAll() {
  void load();
  void loadSummary();
  void alertsStore.refresh();
}

function syncQuery() {
  const query: Record<string, string> = {};
  if (filters.status) query.status = filters.status;
  if (filters.severity) query.severity = filters.severity;
  if (filters.alert_type) query.alert_type = filters.alert_type;
  if (filters.project_id) query.project_id = String(filters.project_id);
  if (filters.target_type) query.target_type = filters.target_type;
  if (filters.target_id) query.target_id = String(filters.target_id);
  void router.replace({ query });
}

function search() {
  page.value = 1;
  syncQuery();
  void load();
}

function resetFilters() {
  filters.status = undefined;
  filters.severity = undefined;
  filters.alert_type = undefined;
  filters.project_id = globalProjectId.value || 0;
  filters.target_type = undefined;
  filters.target_id = undefined;
  filters.range = null;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

// 顶栏项目切换：同步列表的项目筛选
watch(globalProjectId, (id) => {
  filters.project_id = id || 0;
  search();
});
// 用户视角切换：摘要与列表口径变化
watch(
  () => projectStore.ownerId,
  () => {
    void loadSummary();
    search();
  },
);

// ---------- 行展示 ----------
function projectName(id: number | null): string {
  if (!id) return t("alerts.systemAlert");
  return projectStore.projects.find((p) => p.id === id)?.name ?? `#${id}`;
}

function adminName(id: number | null): string {
  if (!id) return "";
  return auth.admin?.id === id ? t("monitoring.me") : `#${id}`;
}

function handler(row: Alert): string {
  if (row.status === "resolved") return row.resolved_by ? adminName(row.resolved_by) : t("alerts.autoResolved");
  if (row.acknowledged_by) return adminName(row.acknowledged_by);
  return "-";
}

interface TargetLink {
  to: string | { path: string; query?: Record<string, string> } | null;
  text: string;
}

/** 目标跳转（docs/11 §11.7）：无对应页面权限时只显示文本 */
function targetLink(row: Alert): TargetLink {
  const id = row.target_id;
  const key = row.target_key || (id ? String(id) : "");
  switch (row.target_type) {
    case "publish_link":
      return { to: id && has("publish.links.view") ? `/links/${id}` : null, text: id ? `#${id}` : key };
    case "content":
      return { to: id && has("content.contents.view") ? `/contents/${id}` : null, text: id ? `#${id}` : key };
    case "ai_task":
      return { to: id && has("ai.tasks.view") ? { path: "/ai/tasks", query: { open: String(id) } } : null, text: id ? `#${id}` : key };
    case "ai_model":
    case "capability_route":
      return { to: has("ai.routes.view") ? "/ai/routes" : null, text: key || (id ? `#${id}` : "-") };
    case "media_asset":
      return { to: id && has("media.assets.view") ? { path: "/media/assets", query: { id: String(id) } } : null, text: id ? `#${id}` : key };
    default:
      return { to: null, text: key || "-" };
  }
}

function isActive(row: Alert): boolean {
  return ACTIVE.includes(row.status);
}

function rowClass({ row }: { row: Alert }): string {
  return row.severity === "critical" && isActive(row) ? "is-critical-row" : "";
}

// ---------- 单条操作 ----------
const acting = ref<number | null>(null);

function replaceRow(updated: Alert) {
  const idx = rows.value.findIndex((r) => r.id === updated.id);
  if (idx >= 0) rows.value.splice(idx, 1, updated);
  if (detail.value?.id === updated.id) detail.value = updated;
}

async function acknowledge(row: Alert) {
  acting.value = row.id;
  try {
    replaceRow(await alertsApi.acknowledge(row.id));
    ElMessage.success(t("alerts.acknowledged"));
    reloadAll();
  } catch {
    reloadAll(); // 409（状态已变化）时同步最新状态
  } finally {
    acting.value = null;
  }
}

async function ignore(row: Alert) {
  try {
    await ElMessageBox.confirm(t("alerts.ignoreConfirm", { title: row.title }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    replaceRow(await alertsApi.ignore(row.id));
    ElMessage.success(t("alerts.ignored"));
    reloadAll();
  } catch {
    reloadAll();
  } finally {
    acting.value = null;
  }
}

// ---------- 解决 / 批量解决（填 note） ----------
const resolveVisible = ref(false);
const resolveTargets = ref<Alert[]>([]);
const resolveNote = ref("");
const resolving = ref(false);
const batchResult = ref<BatchUpdateResult | null>(null);

function openResolve(targets: Alert[]) {
  resolveTargets.value = targets;
  resolveNote.value = "";
  batchResult.value = null;
  resolveVisible.value = true;
}

const isBatch = computed(() => resolveTargets.value.length > 1);

async function submitResolve() {
  const note = resolveNote.value.trim() || null;
  resolving.value = true;
  try {
    if (!isBatch.value) {
      const target = resolveTargets.value[0];
      if (!target) return;
      replaceRow(await alertsApi.resolve(target.id, note));
      ElMessage.success(t("alerts.resolved"));
      resolveVisible.value = false;
    } else {
      const res = await alertsApi.batchResolve({ ids: resolveTargets.value.map((r) => r.id), note });
      batchResult.value = res;
      if (!res.skipped.length) {
        ElMessage.success(t("alerts.batchResult", { updated: res.updated, skipped: 0 }));
        resolveVisible.value = false;
      } else {
        ElMessage.warning(t("alerts.batchResult", { updated: res.updated, skipped: res.skipped.length }));
      }
      tableRef.value?.clearSelection();
    }
    reloadAll();
  } catch {
    reloadAll();
  } finally {
    resolving.value = false;
  }
}

// ---------- 勾选 ----------
const tableRef = ref<TableInstance>();
const selected = ref<Alert[]>([]);

function onSelectionChange(list: Alert[]) {
  selected.value = list;
}

function selectable(row: Alert): boolean {
  return isActive(row);
}

function openBatchResolve() {
  if (!selected.value.length) return;
  if (selected.value.length > BATCH_IDS_MAX) {
    ElMessage.warning(t("alerts.tooMany", { max: BATCH_IDS_MAX }));
    return;
  }
  openResolve([...selected.value]);
}

// ---------- 详情抽屉 ----------
const detailVisible = ref(false);
const detail = ref<Alert | null>(null);
const detailLoading = ref(false);
let detailSeq = 0;

async function openDetail(row: Alert) {
  const seq = ++detailSeq;
  detail.value = row;
  detailVisible.value = true;
  detailLoading.value = true;
  try {
    const res = await alertsApi.get(row.id, { silent: true });
    if (seq === detailSeq) detail.value = res;
  } catch {
    /* 沿用列表数据 */
  } finally {
    if (seq === detailSeq) detailLoading.value = false;
  }
}

function onRowClick(row: Alert, column: { type?: string } | undefined, event: MouseEvent) {
  if (column?.type === "selection") return;
  if ((event.target as HTMLElement | null)?.closest("a,button,.el-checkbox")) return;
  void openDetail(row);
}

const skippedText = computed(() =>
  (batchResult.value?.skipped ?? []).map((s) => `#${s.id}（${t(`alerts.skipReason.${s.reason === "invalid_transition" ? "invalid_transition" : "not_found"}`)}）`).join("、"),
);

// ---------- 进入页面 ----------
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

onMounted(() => {
  filters.status = qEnum("status", ALERT_STATUS);
  filters.severity = qEnum("severity", ALERT_SEVERITY);
  filters.alert_type = qEnum("alert_type", ALERT_TYPE);
  filters.target_type = qEnum("target_type", ALERT_TARGET_TYPE);
  filters.target_id = qInt("target_id");
  // 按对象跳转进入（链接详情时间线）时不叠加顶栏项目，避免对象所属项目与顶栏不一致导致空结果
  const qProject = q("project_id");
  filters.project_id = qProject !== undefined ? (qInt("project_id") ?? 0) : filters.target_id ? 0 : globalProjectId.value || 0;
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void load();
  void loadSummary();
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.alerts") }}</h2>
        <div class="header-actions">
          <el-button v-if="canHandle" type="primary" :icon="Finished" :disabled="!selected.length" @click="openBatchResolve">
            {{ t("alerts.batchResolve") }}<template v-if="selected.length">（{{ selected.length }}）</template>
          </el-button>
        </div>
      </div>
    </template>

    <!-- 摘要卡片 -->
    <div v-loading="summaryLoading && !summary" class="summary-grid">
      <button
        v-for="card in severityCards"
        :key="card.sev"
        type="button"
        class="summary-card"
        :class="[`is-${card.sev}`, { 'is-hot': card.sev === 'critical' && card.open > 0, 'is-active': filters.status === 'open' && filters.severity === card.sev }]"
        @click="filterBySeverity(card.sev)"
      >
        <div class="summary-label"><StatusTag kind="alert_severity" :value="card.sev" /></div>
        <div class="summary-value">{{ formatNumber(card.open) }}</div>
        <div class="summary-sub">{{ t("alerts.summary.acknowledged", { n: card.acknowledged }) }}</div>
      </button>
      <div class="summary-card is-plain">
        <div class="summary-label">{{ t("alerts.summary.todayOpened") }}</div>
        <div class="summary-value">{{ formatNumber(summary?.today_opened ?? 0) }}</div>
      </div>
      <div class="summary-card is-plain">
        <div class="summary-label">{{ t("alerts.summary.todayResolved") }}</div>
        <div class="summary-value">{{ formatNumber(summary?.today_resolved ?? 0) }}</div>
      </div>
    </div>

    <div class="toolbar">
      <ToolbarSelect v-model="filters.status" enum-name="alert_status" :placeholder="t('common.status')" width="110px" @change="search" />
      <ToolbarSelect v-model="filters.severity" enum-name="alert_severity" :placeholder="t('alerts.severity')" width="100px" @change="search" />
      <ToolbarSelect v-model="filters.alert_type" enum-name="alert_type" :placeholder="t('alerts.type')" filterable width="160px" @change="search" />
      <ProjectSelect v-model="filters.project_id" allow-all width="160px" @change="search" />
      <ToolbarSelect v-model="filters.target_type" enum-name="alert_target_type" :placeholder="t('alerts.targetType')" width="130px" @change="search" />
      <el-input-number v-model="filters.target_id" :min="1" :controls="false" :placeholder="t('alerts.targetId')" style="width: 110px" @change="search" />
      <el-date-picker
        v-model="filters.range"
        type="datetimerange"
        :start-placeholder="t('monitoring.start')"
        :end-placeholder="t('monitoring.end')"
        class="range-picker"
        @change="search"
      />
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <el-button :icon="Refresh" @click="reloadAll">{{ t("common.refresh") }}</el-button>
    </div>

    <el-table
      ref="tableRef"
      v-loading="loading"
      :data="rows"
      row-key="id"
      :row-class-name="rowClass"
      class="clickable-table"
      @selection-change="onSelectionChange"
      @row-click="onRowClick"
    >
      <el-table-column v-if="canHandle" type="selection" width="40" :selectable="selectable" reserve-selection />
      <el-table-column :label="t('alerts.severity')" width="80">
        <template #default="{ row }"><StatusTag kind="alert_severity" :value="row.severity" :effect="row.severity === 'critical' ? 'dark' : 'light'" /></template>
      </el-table-column>
      <el-table-column :label="t('alerts.type')" width="130">
        <template #default="{ row }"><StatusTag kind="alert_type" :value="row.alert_type" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('alerts.titleCol')" min-width="240">
        <template #default="{ row }">
          <div class="alert-title" :title="row.title">{{ row.title }}</div>
          <div class="alert-message" :title="row.message">{{ row.message }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('alerts.target')" min-width="150">
        <template #default="{ row }">
          <StatusTag kind="alert_target_type" :value="row.target_type" effect="plain" />
          <router-link v-if="targetLink(row).to" :to="targetLink(row).to!" class="target-link">{{ targetLink(row).text }}</router-link>
          <span v-else class="target-text mono">{{ targetLink(row).text }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('alerts.project')" min-width="110">
        <template #default="{ row }"><span :class="{ 'text-secondary': !row.project_id }">{{ projectName(row.project_id) }}</span></template>
      </el-table-column>
      <el-table-column :label="t('alerts.triggerCount')" width="70" align="center">
        <template #default="{ row }">{{ row.trigger_count }}</template>
      </el-table-column>
      <el-table-column :label="t('alerts.triggeredAt')" width="165">
        <template #default="{ row }">
          <div class="small">{{ formatDateTime(row.first_triggered_at) }}</div>
          <div v-if="row.last_triggered_at !== row.first_triggered_at" class="small text-secondary">{{ formatDateTime(row.last_triggered_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.status')" width="90">
        <template #default="{ row }"><StatusTag kind="alert_status" :value="row.status" /></template>
      </el-table-column>
      <el-table-column :label="t('alerts.handler')" width="90">
        <template #default="{ row }">
          <el-tooltip v-if="row.resolution_note" :content="row.resolution_note" placement="top">
            <span class="handler">{{ handler(row) }}</span>
          </el-tooltip>
          <span v-else>{{ handler(row) }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canHandle" :label="t('common.actions')" width="170" fixed="right">
        <template #default="{ row }">
          <template v-if="isActive(row)">
            <el-button v-if="row.status === 'open'" link type="primary" :loading="acting === row.id" @click.stop="acknowledge(row)">{{ t("alerts.acknowledge") }}</el-button>
            <el-button link type="success" @click.stop="openResolve([row])">{{ t("alerts.resolve") }}</el-button>
            <el-button link type="info" :disabled="acting === row.id" @click.stop="ignore(row)">{{ t("alerts.ignore") }}</el-button>
          </template>
          <span v-else class="text-secondary small">-</span>
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

    <!-- 解决 / 批量解决 -->
    <el-dialog v-model="resolveVisible" :title="isBatch ? t('alerts.batchResolveTitle', { n: resolveTargets.length }) : t('alerts.resolveTitle')" width="min(520px, 96vw)" append-to-body>
      <p v-if="!isBatch && resolveTargets[0]" class="resolve-target">{{ resolveTargets[0].title }}</p>
      <el-form label-position="top" @submit.prevent="submitResolve">
        <el-form-item :label="t('alerts.note')">
          <el-input v-model="resolveNote" type="textarea" :rows="3" :maxlength="MAX_NOTE" show-word-limit :placeholder="t('alerts.notePlaceholder')" />
        </el-form-item>
      </el-form>
      <el-alert
        v-if="batchResult && batchResult.skipped.length"
        type="warning"
        :closable="false"
        show-icon
        :title="t('alerts.batchResult', { updated: batchResult.updated, skipped: batchResult.skipped.length })"
        :description="skippedText"
      />
      <template #footer>
        <el-button @click="resolveVisible = false">{{ batchResult ? t("common.close") : t("common.cancel") }}</el-button>
        <el-button v-if="!batchResult" type="primary" :loading="resolving" @click="submitResolve">{{ t("alerts.resolve") }}</el-button>
      </template>
    </el-dialog>

    <!-- 详情 -->
    <el-drawer v-model="detailVisible" :title="detail ? `${t('alerts.detailTitle')} #${detail.id}` : t('alerts.detailTitle')" size="min(640px, 96vw)" destroy-on-close>
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-head">
            <StatusTag kind="alert_severity" :value="detail.severity" />
            <StatusTag kind="alert_type" :value="detail.alert_type" effect="plain" />
            <StatusTag kind="alert_status" :value="detail.status" />
          </div>
          <h3 class="detail-title">{{ detail.title }}</h3>
          <p class="detail-message">{{ detail.message }}</p>
          <el-descriptions :column="narrow ? 1 : 2" size="small" border class="detail-desc">
            <el-descriptions-item :label="t('alerts.target')">
              <StatusTag kind="alert_target_type" :value="detail.target_type" effect="plain" />
              <router-link v-if="targetLink(detail).to" :to="targetLink(detail).to!" class="target-link">{{ targetLink(detail).text }}</router-link>
              <span v-else class="target-text mono">{{ targetLink(detail).text }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('alerts.project')">{{ projectName(detail.project_id) }}</el-descriptions-item>
            <el-descriptions-item :label="t('alerts.dedupeKey')" :span="2"><span class="mono break">{{ detail.dedupe_key }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('alerts.triggerCount')">{{ detail.trigger_count }}</el-descriptions-item>
            <el-descriptions-item :label="t('alerts.channels')">
              <span v-if="!detail.notified_channels?.length">-</span>
              <StatusTag v-for="c in detail.notified_channels" :key="c" kind="alert_channel" :value="c" effect="plain" class="channel-tag" />
            </el-descriptions-item>
            <el-descriptions-item :label="t('alerts.firstTriggeredAt')">{{ formatDateTime(detail.first_triggered_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('alerts.lastTriggeredAt')">{{ formatDateTime(detail.last_triggered_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('alerts.acknowledgedAt')">
              {{ formatDateTime(detail.acknowledged_at) }}<template v-if="detail.acknowledged_by"> · {{ adminName(detail.acknowledged_by) }}</template>
            </el-descriptions-item>
            <el-descriptions-item :label="t('alerts.resolvedAt')">
              {{ formatDateTime(detail.resolved_at) }}
              <template v-if="detail.status === 'resolved'"> · {{ detail.resolved_by ? adminName(detail.resolved_by) : t("alerts.autoResolved") }}</template>
            </el-descriptions-item>
            <el-descriptions-item :label="t('alerts.note')" :span="2">{{ detail.resolution_note || "-" }}</el-descriptions-item>
          </el-descriptions>
          <div class="detail-section">{{ t("alerts.payload") }}</div>
          <JsonEditor :model-value="detail.payload ?? {}" readonly :rows="10" />
          <div v-if="canHandle && isActive(detail)" class="detail-actions">
            <el-button v-if="detail.status === 'open'" :icon="Check" :loading="acting === detail.id" @click="acknowledge(detail)">{{ t("alerts.acknowledge") }}</el-button>
            <el-button type="success" :icon="Finished" @click="openResolve([detail])">{{ t("alerts.resolve") }}</el-button>
            <el-button :icon="CircleClose" @click="ignore(detail)">{{ t("alerts.ignore") }}</el-button>
          </div>
        </template>
      </div>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
  margin-bottom: 14px;
}
.summary-card {
  display: block;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
  color: inherit;
  font: inherit;
  text-align: left;
  min-width: 0;
}
button.summary-card {
  cursor: pointer;
  transition: border-color 0.15s;
}
button.summary-card:hover,
button.summary-card.is-active {
  border-color: var(--el-color-primary);
}
.summary-card.is-critical {
  border-left: 3px solid var(--el-color-danger);
}
.summary-card.is-warning {
  border-left: 3px solid var(--el-color-warning);
}
.summary-card.is-info {
  border-left: 3px solid var(--el-color-info);
}
.summary-card.is-hot {
  background: var(--el-color-danger-light-9);
}
.summary-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.summary-value {
  margin-top: 4px;
  font-size: 22px;
  font-weight: 600;
}
.summary-card.is-hot .summary-value {
  color: var(--el-color-danger);
}
.summary-sub {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.range-picker {
  width: 340px;
  max-width: 100%;
}
.clickable-table :deep(.el-table__row) {
  cursor: pointer;
}
.clickable-table :deep(.is-critical-row > td.el-table__cell:first-child) {
  box-shadow: inset 3px 0 0 var(--el-color-danger);
}
.alert-title {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.alert-message {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.target-link {
  margin-left: 6px;
  color: var(--el-color-primary);
  text-decoration: none;
  word-break: break-all;
}
.target-text {
  margin-left: 6px;
  word-break: break-all;
}
.handler {
  border-bottom: 1px dashed var(--el-border-color);
}
.small {
  font-size: 12px;
}
.resolve-target {
  margin: 0 0 10px;
  font-weight: 500;
}
.detail-head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.detail-title {
  margin: 10px 0 4px;
  font-size: 15px;
  word-break: break-word;
}
.detail-message {
  margin: 0 0 12px;
  color: var(--el-text-color-regular);
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-desc {
  margin-bottom: 12px;
}
.detail-section {
  margin: 8px 0 6px;
  font-weight: 600;
}
.detail-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.channel-tag {
  margin-right: 4px;
}
.break {
  word-break: break-all;
}
</style>
