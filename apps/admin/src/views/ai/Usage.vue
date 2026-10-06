<script setup lang="ts">
// 用量对账（docs/08 §10.3~§10.7；docs/04 §6.15、§7.17；docs/13 §4.3、§12.3）：
// - 顶部最近一次对账摘要（GET /admin/ai/usage/last-pull，源自 ai:usage:last_pull）：总后台（isAllScope 且未处于用户视角）显示拉取时间、
//   条数、匹配 / 未匹配与 request_ids；own 范围或用户视角只显示拉取时间与 window_overflow 警示；window_overflow=true 时红色提示；
// - 「立即对账」（ai.usage.reconcile）；
// - 汇总 Tab：group_by=model|capability|project|day、start/end（YYYY-MM-DD），含 reconciled_rate；
// - 日志 Tab：model_name / log_type / matched / request_id / start / end 筛选，点击跳转 ai/Tasks.vue 对应尝试行。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Refresh, Search } from "@element-plus/icons-vue";
import {
  DEFAULT_PAGE_SIZE,
  USAGE_LOG_TYPE,
  USAGE_SUMMARY_GROUP_BY,
  type AiUsageLog,
  type AiUsageSummaryRow,
  type ReconcileResult,
  type UsageLastPull,
  type UsageLogType,
  type UsageSummaryGroupBy,
} from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import { isApiError } from "@/api/client";
import JsonEditor from "@/components/JsonEditor.vue";
import { usePermission } from "@/composables/usePermission";
import { useProjectStore } from "@/store/project";
import { formatCny, formatDate, formatDateTime, formatNumber, formatPercent, formatQuota, toUtcIso } from "@/utils/format";

const { t, te } = useI18n();
const router = useRouter();
const { isAllScope } = usePermission();
const projectStore = useProjectStore();

/** 计数与 request_ids 只在总后台且未处于用户视角时显示（13 §12.3） */
const showCounts = computed(() => isAllScope.value && projectStore.ownerId === 0);

// ---------- 最近一次对账 ----------
const lastPull = ref<UsageLastPull | null>(null);
const lastPullLoaded = ref(false);
const lastPullLoading = ref(false);

async function loadLastPull() {
  lastPullLoading.value = true;
  try {
    lastPull.value = await aiApi.usageLastPull();
  } catch {
    lastPull.value = null;
  } finally {
    lastPullLoaded.value = true;
    lastPullLoading.value = false;
  }
}

const hasCounts = computed(() => showCounts.value && !!lastPull.value && typeof lastPull.value.pulled === "number");

// ---------- 立即对账 ----------
const reconciling = ref(false);
const reconcileResult = ref<ReconcileResult | null>(null);

async function reconcile() {
  reconciling.value = true;
  try {
    const res = await aiApi.reconcile({ silent: true });
    reconcileResult.value = res;
    ElMessage.success(t("aiUsage.reconcileDone", { pulled: res.pulled, new: res.new, matched: res.matched, unmatched: res.unmatched }));
    await loadLastPull();
    if (activeTab.value === "summary") void loadSummary();
    else void loadLogs();
  } catch (err) {
    if (!isApiError(err)) return;
    if (err.code === 409) ElMessage.warning(t("aiUsage.reconcileBusy"));
    else {
      const data = err.data as { request_id?: string } | null;
      ElMessage.error(data?.request_id ? `${err.message}（${t("aiUsage.requestId")} ${data.request_id}）` : err.message);
    }
  } finally {
    reconciling.value = false;
  }
}

// ---------- Tab ----------
const activeTab = ref<"summary" | "logs">("summary");
watch(activeTab, (tab) => {
  if (tab === "logs" && !logsLoaded.value) void loadLogs();
});

// ---------- 汇总 ----------
const groupBy = ref<UsageSummaryGroupBy>("model");
const dateRange = ref<[string, string] | null>(null);
const summaryRows = ref<AiUsageSummaryRow[]>([]);
const summaryLoading = ref(false);

async function loadSummary() {
  summaryLoading.value = true;
  try {
    summaryRows.value = await aiApi.usageSummary({
      group_by: groupBy.value,
      start: dateRange.value?.[0],
      end: dateRange.value?.[1],
    });
  } catch {
    summaryRows.value = [];
  } finally {
    summaryLoading.value = false;
  }
}

watch([groupBy, dateRange], () => void loadSummary());

function keyLabel(key: string | number | null): string {
  if (key === null || key === undefined || key === "") return "-";
  const k = String(key);
  switch (groupBy.value) {
    case "capability":
      return te(`status.capability.${k}`) ? t(`status.capability.${k}`) : k;
    case "project": {
      const id = Number(k);
      if (!id) return k;
      const p = projectStore.projects.find((x) => x.id === id);
      return p ? p.name : t("aiUsage.projectName", { id });
    }
    case "day":
      return formatDate(k);
    default:
      return k;
  }
}

/** 合计行：reconciled_rate 按调用数加权 */
function summaryTotals(param: { columns: { property?: string }[]; data: AiUsageSummaryRow[] }): string[] {
  const data = param.data;
  const sum = (f: (r: AiUsageSummaryRow) => number) => data.reduce((acc, r) => acc + (Number(f(r)) || 0), 0);
  const calls = sum((r) => r.calls);
  const reconciled = sum((r) => (r.reconciled_rate ?? 0) * r.calls);
  return param.columns.map((col, idx) => {
    if (idx === 0) return t("aiUsage.total");
    switch (col.property) {
      case "calls":
        return formatNumber(calls);
      case "prompt_tokens":
        return formatNumber(sum((r) => r.prompt_tokens));
      case "completion_tokens":
        return formatNumber(sum((r) => r.completion_tokens));
      case "quota_estimated":
        return formatQuota(sum((r) => r.quota_estimated));
      case "quota_actual":
        return formatQuota(sum((r) => r.quota_actual));
      case "cost_cny":
        return formatCny(sum((r) => r.cost_cny));
      case "reconciled_rate":
        return calls > 0 ? formatPercent(reconciled / calls) : "-";
      default:
        return "";
    }
  });
}

// ---------- 日志 ----------
const logFilters = reactive<{ model_name: string; log_type: UsageLogType | undefined; matched: boolean | undefined; request_id: string; range: [Date, Date] | null }>({
  model_name: "",
  log_type: undefined,
  matched: undefined,
  request_id: "",
  range: null,
});
const logPage = ref(1);
const logPageSize = ref(DEFAULT_PAGE_SIZE);
const logTotal = ref(0);
const logs = ref<AiUsageLog[]>([]);
const logsLoading = ref(false);
const logsLoaded = ref(false);

async function loadLogs() {
  logsLoading.value = true;
  try {
    const res = await aiApi.usageLogs({
      page: logPage.value,
      page_size: logPageSize.value,
      model_name: logFilters.model_name.trim() || undefined,
      log_type: logFilters.log_type,
      matched: logFilters.matched,
      request_id: logFilters.request_id.trim() || undefined,
      start: logFilters.range ? toUtcIso(logFilters.range[0]) : undefined,
      end: logFilters.range ? toUtcIso(logFilters.range[1]) : undefined,
    });
    logs.value = res.items;
    logTotal.value = res.total;
  } catch {
    logs.value = [];
    logTotal.value = 0;
  } finally {
    logsLoaded.value = true;
    logsLoading.value = false;
  }
}

function searchLogs() {
  logPage.value = 1;
  void loadLogs();
}

function resetLogFilters() {
  logFilters.model_name = "";
  logFilters.log_type = undefined;
  logFilters.matched = undefined;
  logFilters.request_id = "";
  logFilters.range = null;
  searchLogs();
}

function onLogPageSizeChange(size: number) {
  logPageSize.value = size;
  searchLogs();
}

function logTypeLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  const key = `aiUsage.logTypes.${value}`;
  return te(key) ? t(key) : String(value);
}

function logTypeTag(value: number): "success" | "danger" | "warning" | "info" {
  return value === 2 ? "success" : value === 5 ? "danger" : value === 6 ? "warning" : "info";
}

/** 跳转到 AI 任务页对应尝试行（按 request_id 过滤并打开详情） */
function gotoTask(log: AiUsageLog) {
  if (!log.ai_task_id) return;
  const query: Record<string, string> = { row_kind: "attempt", project_id: "0", open: String(log.ai_task_id) };
  if (log.request_id) query.request_id = log.request_id;
  void router.push({ path: "/ai/tasks", query });
}

const rawVisible = ref(false);
const rawLog = ref<AiUsageLog | null>(null);
function viewRaw(log: AiUsageLog) {
  rawLog.value = log;
  rawVisible.value = true;
}

// 用户视角切换时刷新（计数可见性与可见日志随之变化）
watch(
  () => projectStore.ownerId,
  () => {
    void loadLastPull();
    void loadSummary();
    if (logsLoaded.value) void loadLogs();
  },
);

onMounted(() => {
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void loadLastPull();
  void loadSummary();
});
</script>

<template>
  <!-- 最近一次对账摘要 -->
  <el-card v-loading="lastPullLoading && !lastPullLoaded" shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("aiUsage.title") }}</h2>
        <div class="header-actions">
          <el-button :icon="Refresh" @click="loadLastPull">{{ t("common.refresh") }}</el-button>
          <el-button v-permission="'ai.usage.reconcile'" type="primary" :loading="reconciling" @click="reconcile">{{ t("aiUsage.reconcile") }}</el-button>
        </div>
      </div>
    </template>

    <el-alert v-if="lastPull?.window_overflow" type="error" effect="dark" :closable="false" show-icon :title="t('aiUsage.overflow')" class="overflow-alert" />

    <div class="last-pull">
      <span class="last-pull__title">{{ t("aiUsage.lastPull") }}</span>
      <template v-if="lastPull">
        <span class="last-pull__item">
          <span class="text-secondary">{{ t("aiUsage.pulledAt") }}：</span>{{ formatDateTime(lastPull.pulled_at) }}
        </span>
        <template v-if="hasCounts">
          <span class="last-pull__item"><span class="text-secondary">{{ t("aiUsage.pulled") }}：</span>{{ formatNumber(lastPull.pulled) }}</span>
          <span class="last-pull__item"><span class="text-secondary">{{ t("aiUsage.new") }}：</span>{{ formatNumber(lastPull.new) }}</span>
          <span class="last-pull__item"><span class="text-secondary">{{ t("aiUsage.matched") }}：</span>{{ formatNumber(lastPull.matched) }}</span>
          <span class="last-pull__item"><span class="text-secondary">{{ t("aiUsage.unmatched") }}：</span>{{ formatNumber(lastPull.unmatched) }}</span>
        </template>
        <el-tag :type="lastPull.window_overflow ? 'danger' : 'success'" size="small" effect="plain">
          {{ lastPull.window_overflow ? t("aiUsage.overflowTag") : t("aiUsage.windowOk") }}
        </el-tag>
        <div v-if="hasCounts && lastPull.request_ids?.length" class="last-pull__rids">
          <span class="text-secondary">{{ t("aiUsage.requestIds") }}：</span>
          <span class="mono">{{ lastPull.request_ids.join(", ") }}</span>
        </div>
      </template>
      <span v-else-if="lastPullLoaded" class="text-secondary">{{ t("aiUsage.neverPulled") }}</span>
    </div>

    <el-alert
      v-if="reconcileResult"
      type="success"
      show-icon
      class="result-alert"
      :title="t('aiUsage.reconcileDone', { pulled: reconcileResult.pulled, new: reconcileResult.new, matched: reconcileResult.matched, unmatched: reconcileResult.unmatched })"
      @close="reconcileResult = null"
    />
  </el-card>

  <el-card shadow="never" class="page-card">
    <el-tabs v-model="activeTab">
      <!-- 汇总 -->
      <el-tab-pane name="summary" :label="t('aiUsage.tabs.summary')">
        <div class="toolbar">
          <span class="text-secondary">{{ t("aiUsage.groupBy") }}</span>
          <el-radio-group v-model="groupBy">
            <el-radio-button v-for="g in USAGE_SUMMARY_GROUP_BY" :key="g" :value="g">{{ t(`aiUsage.groupByOptions.${g}`) }}</el-radio-button>
          </el-radio-group>
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            :start-placeholder="t('aiUsage.startDate')"
            :end-placeholder="t('aiUsage.endDate')"
            :range-separator="'~'"
            style="width: 280px"
          />
          <el-button :icon="Refresh" @click="loadSummary">{{ t("common.refresh") }}</el-button>
          <span class="spacer" />
          <span class="text-secondary hint">{{ dateRange ? t("aiUsage.summaryNote") : `${t("aiUsage.defaultRange")}；${t("aiUsage.summaryNote")}` }}</span>
        </div>
        <el-table v-loading="summaryLoading" :data="summaryRows" stripe show-summary :summary-method="summaryTotals">
          <el-table-column :label="t(`aiUsage.groupByOptions.${groupBy}`)" min-width="180" prop="key">
            <template #default="{ row }">
              <span :class="{ mono: groupBy === 'model' }">{{ keyLabel(row.key) }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.calls')" prop="calls" width="100" align="right">
            <template #default="{ row }">{{ formatNumber(row.calls) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.promptTokens')" prop="prompt_tokens" width="130" align="right">
            <template #default="{ row }">{{ formatNumber(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.completionTokens')" prop="completion_tokens" width="130" align="right">
            <template #default="{ row }">{{ formatNumber(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.quotaEstimated')" prop="quota_estimated" width="140" align="right">
            <template #default="{ row }">{{ formatQuota(row.quota_estimated) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.quotaActual')" prop="quota_actual" width="140" align="right">
            <template #default="{ row }">{{ formatQuota(row.quota_actual) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.cost')" prop="cost_cny" width="120" align="right">
            <template #default="{ row }">{{ formatCny(row.cost_cny) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.reconciledRate')" prop="reconciled_rate" width="150">
            <template #default="{ row }">
              <el-progress
                v-if="row.reconciled_rate !== null && row.reconciled_rate !== undefined"
                :percentage="Math.round(Number(row.reconciled_rate) * 1000) / 10"
                :status="Number(row.reconciled_rate) >= 1 ? 'success' : Number(row.reconciled_rate) < 0.8 ? 'warning' : undefined"
                :stroke-width="6"
              />
              <span v-else>-</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 日志 -->
      <el-tab-pane name="logs" :label="t('aiUsage.tabs.logs')">
        <div class="toolbar">
          <el-input v-model="logFilters.model_name" :placeholder="t('aiUsage.modelName')" clearable style="width: 160px" @keyup.enter="searchLogs" />
          <el-select v-model="logFilters.log_type" :placeholder="t('aiUsage.logType')" clearable style="width: 120px">
            <el-option v-for="lt in USAGE_LOG_TYPE" :key="lt" :label="`${logTypeLabel(lt)} (${lt})`" :value="lt" />
          </el-select>
          <el-select v-model="logFilters.matched" :placeholder="t('aiUsage.matchState')" clearable style="width: 120px">
            <el-option :label="t('aiUsage.matchedYes')" :value="true" />
            <el-option :label="t('aiUsage.matchedNo')" :value="false" />
          </el-select>
          <el-input v-model="logFilters.request_id" :placeholder="t('aiUsage.requestId')" clearable style="width: 220px" @keyup.enter="searchLogs" />
          <el-date-picker
            v-model="logFilters.range"
            type="datetimerange"
            :start-placeholder="t('aiUsage.timeRange')"
            :end-placeholder="t('aiUsage.timeRange')"
            :range-separator="'~'"
            style="width: 360px"
          />
          <el-button type="primary" :icon="Search" @click="searchLogs">{{ t("common.search") }}</el-button>
          <el-button :icon="Refresh" @click="resetLogFilters">{{ t("common.reset") }}</el-button>
        </div>
        <el-table v-loading="logsLoading" :data="logs" row-key="id" stripe>
          <el-table-column :label="t('common.id')" prop="id" width="80" />
          <el-table-column :label="t('aiUsage.requestId')" min-width="180">
            <template #default="{ row }"><span class="mono">{{ row.request_id || "-" }}</span></template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.logType')" width="90">
            <template #default="{ row }"><el-tag :type="logTypeTag(row.log_type)" size="small">{{ logTypeLabel(row.log_type) }}</el-tag></template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.modelName')" min-width="150">
            <template #default="{ row }"><span class="mono">{{ row.model_name || "-" }}</span></template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.group')" width="100">
            <template #default="{ row }">{{ row.group_name || "-" }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.quota')" width="120" align="right">
            <template #default="{ row }">{{ formatQuota(row.quota) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.tokens')" width="140" align="right">
            <template #default="{ row }">{{ formatNumber(row.prompt_tokens) }} / {{ formatNumber(row.completion_tokens) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.cacheTokens')" width="100" align="right">
            <template #default="{ row }">{{ formatNumber(row.cache_tokens) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.upstreamTaskId')" min-width="150">
            <template #default="{ row }"><span class="mono">{{ row.upstream_task_id || "-" }}</span></template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.task')" width="130">
            <template #default="{ row }">
              <el-button v-if="row.ai_task_id" link type="primary" @click="gotoTask(row)">#{{ row.ai_task_id }}</el-button>
              <el-tag v-else size="small" type="info" effect="plain">{{ t("aiUsage.matchedNo") }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.upstreamCreatedAt')" width="165">
            <template #default="{ row }">{{ formatDateTime(row.upstream_created_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.matchedAt')" width="165">
            <template #default="{ row }">{{ formatDateTime(row.matched_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('aiUsage.pulledAt')" width="165">
            <template #default="{ row }">{{ formatDateTime(row.pulled_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('common.actions')" width="140" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="viewRaw(row)">{{ t("aiUsage.viewRaw") }}</el-button>
              <el-button v-if="row.ai_task_id" link type="primary" @click="gotoTask(row)">{{ t("aiUsage.gotoTask") }}</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="pagination">
          <el-pagination
            v-model:current-page="logPage"
            :page-size="logPageSize"
            :page-sizes="[20, 50, 100]"
            :total="logTotal"
            layout="total, sizes, prev, pager, next"
            background
            @current-change="loadLogs"
            @size-change="onLogPageSizeChange"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="rawVisible" :title="rawLog ? t('aiUsage.rawTitle', { id: rawLog.id }) : ''" width="min(680px, 96vw)" destroy-on-close>
      <JsonEditor v-if="rawLog" :model-value="rawLog.raw" readonly :rows="18" />
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.overflow-alert {
  margin-bottom: 12px;
}
.last-pull {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  font-size: 14px;
}
.last-pull__title {
  font-weight: 600;
}
.last-pull__rids {
  width: 100%;
  font-size: 12px;
  word-break: break-all;
}
.result-alert {
  margin-top: 12px;
}
.hint {
  font-size: 12px;
}
</style>
