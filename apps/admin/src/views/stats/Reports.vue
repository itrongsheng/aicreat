<script setup lang="ts">
// 报表页（docs/12 §6、§7、§8、§11；docs/13 §12.3）：四个 Tab —— 趋势 / 分解 / 明细榜 / AI 消耗。
// - 公共筛选：时间范围（统计时区，快捷项最近 7 / 30 / 90 天、本月、上月，默认最近 30 天，跨度 ≤ 731 天）、项目（默认跟随全局）；
// - 筛选条件同步到路由 query（?tab=trends&start=…&end=…&project_id=… 及当前 Tab 的参数），刷新 / 分享可还原；
// - 跨度 > 92 天时把「日」粒度默认切到「周」；分解表「查看趋势」切到趋势 Tab 并预填维度 / 维度键 / 指标；
// - 统计接口 404（所选项目已不可见）时项目重置为 0 后重新请求；重算按钮（stats.reports.recompute）+ 二次确认。
import { computed, reactive, ref, watch } from "vue";
import { useRoute, useRouter, type LocationQueryRaw } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Refresh, RefreshRight } from "@element-plus/icons-vue";
import {
  BREAKDOWN_DIMENSION,
  RANKING_TYPE,
  STATS_DIMENSION,
  STATS_GRANULARITY,
  type RankingType,
} from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import ProjectSelect from "@/components/ProjectSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { useStatsRuntime } from "@/composables/useRuntimeSettings";
import { useProjectStore } from "@/store/project";
import { addDays, isDateStr, monthEnd, monthStart, spanDays, todayIn } from "@/utils/stats";
import AiCostTab from "./AiCostTab.vue";
import BreakdownTab from "./BreakdownTab.vue";
import RankingsTab from "./RankingsTab.vue";
import RecomputeDialog from "./RecomputeDialog.vue";
import TrendsTab from "./TrendsTab.vue";
import {
  RANKING_LIMITS,
  REPORT_TABS,
  type AiState,
  type BreakdownState,
  type RankingState,
  type ReportFilters,
  type ReportTab,
  type TrendState,
  type ViewTrendPayload,
} from "./types";

const DEFAULT_SPAN = 30;
const WEEK_THRESHOLD_DAYS = 92;

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { has, isAllScope } = usePermission();
const projectStore = useProjectStore();
const statsRuntime = useStatsRuntime();

// ---------- 路由 query 解析 ----------
function q(key: string): string | undefined {
  const v = route.query[key];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === "string" && s !== "" ? s : undefined;
}

function qEnum<T extends string>(key: string, values: readonly T[]): T | undefined {
  const v = q(key);
  return v && (values as readonly string[]).includes(v) ? (v as T) : undefined;
}

function qList(key: string): string[] {
  const v = q(key);
  if (!v) return [];
  return [...new Set(v.split(",").map((s) => s.trim()))].filter((m) => m in statsApi.METRIC_SPEC_MAP).slice(0, statsApi.MAX_METRICS);
}

const initialTab = qEnum("tab", REPORT_TABS) ?? "trends";
const tab = ref<ReportTab>(initialTab);

const filters = reactive<ReportFilters>({ start: "", end: "", projectId: 0 });
{
  const pid = Number(q("project_id"));
  filters.projectId = q("project_id") !== undefined && Number.isInteger(pid) && pid >= 0 ? pid : projectStore.currentId;
}

const trendState = reactive<TrendState>({
  granularity: (initialTab === "trends" && qEnum("granularity", STATS_GRANULARITY)) || "day",
  dimension: (initialTab === "trends" && qEnum("dimension", STATS_DIMENSION)) || "total",
  dimension_key: (initialTab === "trends" && q("dimension_key")) || "",
  metrics: initialTab === "trends" ? qList("metrics") : [],
  chartType: initialTab === "trends" && q("chart") === "bar" ? "bar" : "line",
});
const breakdownState = reactive<BreakdownState>({
  dimension: (initialTab === "breakdown" && qEnum("dimension", BREAKDOWN_DIMENSION)) || "project",
  metrics: initialTab === "breakdown" ? qList("metric") : [],
});
const rankingState = reactive<RankingState>({
  type: (initialTab === "rankings" && qEnum<RankingType>("type", RANKING_TYPE)) || "fastest_indexed",
  limit: (() => {
    const n = Number(q("limit"));
    return initialTab === "rankings" && (RANKING_LIMITS as readonly number[]).includes(n) ? n : undefined;
  })(),
});
const aiState = reactive<AiState>({ granularity: (initialTab === "ai" && qEnum("granularity", STATS_GRANULARITY)) || "day" });

// ---------- 统计时区与日期 ----------
const ready = ref(false);
const today = ref(todayIn(null));

function applyDefaultRange() {
  filters.end = today.value;
  filters.start = addDays(today.value, -(DEFAULT_SPAN - 1));
}

async function init() {
  const tz = await statsRuntime.load();
  today.value = todayIn(tz);
  const start = q("start");
  const end = q("end");
  if (isDateStr(start) && isDateStr(end) && start <= end && spanDays(start, end) <= statsApi.MAX_SPAN_DAYS) {
    filters.start = start;
    filters.end = end;
  } else {
    applyDefaultRange();
  }
  ready.value = true;
}
void init();

const dateRange = computed<[string, string] | null>({
  get: (): [string, string] | null => (filters.start && filters.end ? [filters.start, filters.end] : null),
  set: (v: [string, string] | null) => {
    if (!v || !isDateStr(v[0]) || !isDateStr(v[1])) {
      applyDefaultRange();
      return;
    }
    const [start, end]: [string, string] = v[0] <= v[1] ? [v[0], v[1]] : [v[1], v[0]];
    if (spanDays(start, end) > statsApi.MAX_SPAN_DAYS) {
      ElMessage.warning(t("stats.filters.spanTooLong", { max: statsApi.MAX_SPAN_DAYS }));
      return;
    }
    filters.start = start;
    filters.end = end;
    if (spanDays(start, end) > WEEK_THRESHOLD_DAYS) {
      if (trendState.granularity === "day") trendState.granularity = "week";
      if (aiState.granularity === "day") aiState.granularity = "week";
    }
  },
});

function toLocalDate(date: string): Date {
  const [y, m, d] = date.split("-").map(Number);
  return new Date(y, m - 1, d);
}

const shortcuts = computed(() => {
  const end = today.value;
  const lastMonthEnd = addDays(monthStart(end), -1);
  const make = (start: string, stop: string) => () => [toLocalDate(start), toLocalDate(stop)];
  return [
    { text: t("stats.filters.shortcuts.last7"), value: make(addDays(end, -6), end) },
    { text: t("stats.filters.shortcuts.last30"), value: make(addDays(end, -29), end) },
    { text: t("stats.filters.shortcuts.last90"), value: make(addDays(end, -89), end) },
    { text: t("stats.filters.shortcuts.thisMonth"), value: make(monthStart(end), end) },
    { text: t("stats.filters.shortcuts.lastMonth"), value: make(monthStart(lastMonthEnd), monthEnd(lastMonthEnd)) },
  ];
});

function disabledDate(date: Date): boolean {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` > today.value;
}

// ---------- 项目 ----------
/** 跟随全局项目切换（顶栏 ProjectSelect） */
watch(
  () => projectStore.currentId,
  (id) => {
    filters.projectId = id;
  },
);

/** 统计接口 404：项目已不可见 → 重置为 0 后重新请求（docs/13 §12.2） */
function onProjectGone() {
  if (filters.projectId > 0 && projectStore.currentId === filters.projectId) projectStore.setCurrent(0);
  filters.projectId = 0;
}

// ---------- query 同步 ----------
function buildQuery(): LocationQueryRaw {
  const query: LocationQueryRaw = { tab: tab.value, start: filters.start, end: filters.end, project_id: String(filters.projectId) };
  if (tab.value === "trends") {
    query.granularity = trendState.granularity;
    query.dimension = trendState.dimension;
    if (trendState.dimension !== "total" && trendState.dimension_key) query.dimension_key = trendState.dimension_key;
    if (trendState.metrics.length) query.metrics = trendState.metrics.join(",");
    if (trendState.chartType === "bar") query.chart = "bar";
  } else if (tab.value === "breakdown") {
    query.dimension = breakdownState.dimension;
    if (breakdownState.metrics.length) query.metric = breakdownState.metrics.join(",");
  } else if (tab.value === "rankings") {
    query.type = rankingState.type;
    if (rankingState.limit) query.limit = String(rankingState.limit);
  } else {
    query.granularity = aiState.granularity;
  }
  return query;
}

watch(
  () => [ready.value, tab.value, { ...filters }, { ...trendState }, { ...breakdownState }, { ...rankingState }, { ...aiState }],
  () => {
    if (!ready.value) return;
    const next = buildQuery();
    const cur = route.query;
    const same = Object.keys(next).length === Object.keys(cur).length && Object.entries(next).every(([k, v]) => cur[k] === v);
    if (!same) void router.replace({ query: next });
  },
  { deep: true },
);

// ---------- 联动 ----------
function onViewTrend(payload: ViewTrendPayload) {
  trendState.dimension = payload.dimension;
  trendState.dimension_key = payload.dimension_key;
  if (payload.metric && statsApi.metricSupports(payload.metric, payload.dimension)) trendState.metrics = [payload.metric];
  if (payload.projectId !== undefined) filters.projectId = payload.projectId;
  tab.value = "trends";
}

// ---------- 刷新与重算 ----------
const refreshKey = ref(0);
function refresh() {
  refreshKey.value += 1;
}

const recomputeVisible = ref(false);
const canRecompute = computed(() => has("stats.reports.recompute"));

const scopeLabel = computed(() => {
  if (!isAllScope.value) return t("dashboard.scopeOwn");
  if (projectStore.ownerId > 0) return t("dashboard.scopeOwner", { name: projectStore.ownerName });
  return t("dashboard.scopeAll");
});
</script>

<template>
  <el-card shadow="never" class="page-card reports">
    <template #header>
      <div class="page-header reports-header">
        <div class="reports-title">
          <h2 class="page-title">{{ t("menu.statsReports") }}</h2>
          <el-tag :type="isAllScope && projectStore.ownerId === 0 ? 'primary' : 'info'" effect="plain" disable-transitions>{{ scopeLabel }}</el-tag>
        </div>
        <div class="reports-actions">
          <span class="text-secondary small">{{ t("stats.timezoneNote", { tz: statsRuntime.timezone.value }) }}</span>
          <el-button :icon="Refresh" @click="refresh">{{ t("common.refresh") }}</el-button>
          <el-button v-if="canRecompute" v-permission="'stats.reports.recompute'" :icon="RefreshRight" type="warning" plain @click="recomputeVisible = true">
            {{ t("stats.recompute.button") }}
          </el-button>
        </div>
      </div>
    </template>

    <div class="toolbar">
      <span class="filter-label">{{ t("stats.filters.dateRange") }}</span>
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        format="YYYY-MM-DD"
        unlink-panels
        :clearable="false"
        :shortcuts="shortcuts"
        :disabled-date="disabledDate"
        :start-placeholder="t('stats.filters.start')"
        :end-placeholder="t('stats.filters.end')"
        class="date-range"
        style="width: 280px; flex: none"
      />
      <span class="filter-label">{{ t("stats.filters.project") }}</span>
      <ProjectSelect v-model="filters.projectId" allow-all width="200px" />
    </div>

    <el-tabs v-model="tab" class="reports-tabs">
      <el-tab-pane :label="t('stats.tabs.trends')" name="trends" lazy>
        <TrendsTab
          v-if="ready"
          v-model:state="trendState"
          :filters="filters"
          :active="tab === 'trends'"
          :refresh-key="refreshKey"
          @project-gone="onProjectGone"
        />
      </el-tab-pane>
      <el-tab-pane :label="t('stats.tabs.breakdown')" name="breakdown" lazy>
        <BreakdownTab
          v-if="ready"
          v-model:state="breakdownState"
          :filters="filters"
          :active="tab === 'breakdown'"
          :refresh-key="refreshKey"
          @project-gone="onProjectGone"
          @view-trend="onViewTrend"
        />
      </el-tab-pane>
      <el-tab-pane :label="t('stats.tabs.rankings')" name="rankings" lazy>
        <RankingsTab
          v-if="ready"
          v-model:state="rankingState"
          :filters="filters"
          :active="tab === 'rankings'"
          :refresh-key="refreshKey"
          @project-gone="onProjectGone"
        />
      </el-tab-pane>
      <el-tab-pane :label="t('stats.tabs.ai')" name="ai" lazy>
        <AiCostTab v-if="ready" v-model:state="aiState" :filters="filters" :active="tab === 'ai'" :refresh-key="refreshKey" @project-gone="onProjectGone" />
      </el-tab-pane>
    </el-tabs>

    <RecomputeDialog v-if="canRecompute" v-model="recomputeVisible" :today="today" @done="refresh" />
  </el-card>
</template>

<style scoped>
.reports-header {
  flex-wrap: wrap;
}
.reports-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.reports-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.filter-label {
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.small {
  font-size: 12px;
}
.reports-tabs :deep(.el-tabs__content) {
  overflow: visible;
}
@media (max-width: 768px) {
  .date-range {
    width: 100% !important;
  }
}
</style>
