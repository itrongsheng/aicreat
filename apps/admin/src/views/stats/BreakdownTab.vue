<script setup lang="ts">
// 报表页「分解」Tab（docs/12 §6.4；docs/13 §10.3、§12.3）：GET /admin/stats/breakdown。
// - 维度：project / owner（仅总后台）/ platform / capability / model / admin / seo_engine / geo_engine；project / owner 忽略项目筛选；
// - 多指标（最多 8 个，第一个为主指标：排序、占比与柱状图依据）；左侧柱状图前 10 项，其余合并为「其它」（比率类主指标不合并）；
// - 右侧表格：键、标签、各指标、占比（比率类主指标显示 --）、操作「查看趋势」（切到趋势 Tab 并预填维度 / 维度键 / 指标）；
//   dimension=owner 时点击用户行切换到该用户视角（键 0「已删除项目」不可点击）；导出 CSV。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Download } from "@element-plus/icons-vue";
import { BREAKDOWN_DIMENSION, STATS_DIMENSION, type BreakdownDimension, type BreakdownRow, type StatsDimension } from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import TrendChart, { type TrendSeries } from "@/components/TrendChart.vue";
import { useLatestRequest } from "@/composables/useLatestRequest";
import { usePermission } from "@/composables/usePermission";
import { useProjectStore } from "@/store/project";
import { formatPercent } from "@/utils/format";
import { formatMetric, unitOf } from "@/utils/stats";
import { exportCsv } from "./exportCsv";
import MetricSelect from "./MetricSelect.vue";
import type { BreakdownState, ReportFilters, ViewTrendPayload } from "./types";

const props = defineProps<{ filters: ReportFilters; active: boolean; refreshKey?: number }>();
const state = defineModel<BreakdownState>("state", { required: true });
const emit = defineEmits<{ (e: "project-gone"): void; (e: "view-trend", payload: ViewTrendPayload): void }>();

const { t, te } = useI18n();
const { isAllScope } = usePermission();
const projectStore = useProjectStore();

const TOP_N = 10;

const dimensionOptions = computed(() =>
  BREAKDOWN_DIMENSION.filter((d) => d !== "owner" || isAllScope.value).map((d) => ({ value: d, label: t(`stats.dimension.${d}`) })),
);

/** 非总后台不允许 owner 维度（路由 query 带入时回退到 project） */
watch(
  [() => state.value.dimension, isAllScope],
  ([dim, all]) => {
    if (dim === "owner" && !all) state.value.dimension = "project";
  },
  { immediate: true },
);

function normalizeMetrics(dim: BreakdownDimension) {
  const kept = state.value.metrics.filter((m) => statsApi.metricSupports(m, dim));
  state.value.metrics = kept.length ? kept : [...statsApi.DEFAULT_DIMENSION_METRICS[dim]];
}

watch(
  () => state.value.dimension,
  (dim) => normalizeMetrics(dim),
  { immediate: true },
);

const projectIgnored = computed(() => state.value.dimension === "project" || state.value.dimension === "owner");
const primary = computed(() => state.value.metrics[0] ?? "");

// ---------- 数据 ----------
const request = useLatestRequest();
const rows = ref<BreakdownRow[]>([]);
const loadedMetrics = ref<string[]>([]);
const loadedDimension = ref<BreakdownDimension>(state.value.dimension);
const error = ref<string | null>(null);

function queryParams(): statsApi.BreakdownParams {
  return {
    dimension: state.value.dimension,
    metric: state.value.metrics,
    start: props.filters.start,
    end: props.filters.end,
    project_id: projectIgnored.value ? 0 : props.filters.projectId,
  };
}

async function load() {
  if (!state.value.metrics.length) {
    rows.value = [];
    return;
  }
  const params = queryParams();
  const metrics = [...state.value.metrics];
  const dimension = state.value.dimension;
  const res = await request.run((signal) => statsApi.breakdown(params, { silent: true, signal }));
  if (!res) return;
  if (res.ok) {
    rows.value = res.value;
    loadedMetrics.value = metrics;
    loadedDimension.value = dimension;
    error.value = null;
    return;
  }
  if (statsApi.isProjectGone(res.error) && props.filters.projectId > 0) {
    emit("project-gone");
    return;
  }
  error.value = res.error instanceof Error ? res.error.message : t("common.requestFailed");
}

const queryKey = computed(() => JSON.stringify([props.active, state.value.dimension, state.value.metrics, props.filters, projectIgnored.value, props.refreshKey]));
watch(
  queryKey,
  () => {
    if (props.active) request.debounce(() => void load());
    else request.abort();
  },
  { immediate: true },
);

// ---------- 展示 ----------
function rowLabel(row: BreakdownRow): string {
  const dim = loadedDimension.value;
  if (dim === "capability" && te(`stats.capability.${row.key}`)) return t(`stats.capability.${row.key}`);
  if (dim === "owner" && row.key === "0") return row.label || t("stats.deletedProject");
  return row.label || row.key;
}

function metricValue(row: BreakdownRow, metric: string): number | null {
  const v = row.values?.[metric];
  if (v !== undefined) return v;
  return metric === loadedMetrics.value[0] ? row.value : null;
}

const chartData = computed(() => {
  const main = loadedMetrics.value[0];
  if (!main) return { dates: [] as string[], series: [] as TrendSeries[] };
  const list = rows.value;
  const top = list.slice(0, TOP_N);
  const labels = top.map(rowLabel);
  const values = top.map((r) => metricValue(r, main));
  const isRatio = !!statsApi.METRIC_SPEC_MAP[main]?.ratio;
  if (!isRatio && list.length > TOP_N) {
    labels.push(t("stats.table.other"));
    values.push(list.slice(TOP_N).reduce((acc, r) => acc + (Number(metricValue(r, main)) || 0), 0));
  }
  return {
    dates: labels,
    series: [{ key: main, name: t(`stats.metrics.${main}`), unit: unitOf(main), type: "bar" as const, data: values }],
  };
});

const loadedPrimaryIsRatio = computed(() => !!statsApi.METRIC_SPEC_MAP[loadedMetrics.value[0] ?? ""]?.ratio);

function shareText(row: BreakdownRow): string {
  if (loadedPrimaryIsRatio.value || row.share === null || row.share === undefined) return "--";
  return formatPercent(row.share, 2);
}

function canViewTrend(row: BreakdownRow): boolean {
  const dim = loadedDimension.value;
  if (dim === "owner") return false;
  if (dim === "project") return Number(row.key) > 0;
  return (STATS_DIMENSION as readonly string[]).includes(dim);
}

function viewTrend(row: BreakdownRow) {
  const dim = loadedDimension.value;
  const metric = loadedMetrics.value[0];
  if (dim === "project") {
    emit("view-trend", { dimension: "total", dimension_key: "", metric, projectId: Number(row.key) });
    return;
  }
  emit("view-trend", { dimension: dim as StatsDimension, dimension_key: row.key, metric });
}

/** dimension=owner：点击用户行切换顶栏用户视角（键 0「已删除项目」不可点击） */
function ownerClickable(row: BreakdownRow): boolean {
  return loadedDimension.value === "owner" && isAllScope.value && Number(row.key) > 0;
}

function onRowClick(row: BreakdownRow) {
  if (ownerClickable(row)) void projectStore.setOwner(Number(row.key));
}

function rowClass({ row }: { row: BreakdownRow }): string {
  return ownerClickable(row) ? "is-clickable-row" : "";
}

/** 表头单位：指标名已含单位（如「费用（元）」）时不重复 */
function unitSuffix(metric: string): string {
  const unit = unitOf(metric);
  const label = t(`stats.metrics.${metric}`);
  if (unit === "count" || /[（(]/.test(label)) return "";
  return `（${t(`stats.units.${unit}`)}）`;
}

// ---------- 导出 ----------
const exporting = ref(false);
async function onExport() {
  if (!state.value.metrics.length) return;
  exporting.value = true;
  try {
    await exportCsv({ report: "breakdown", ...queryParams() });
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <div class="breakdown-tab">
    <div class="toolbar">
      <span class="filter-label">{{ t("stats.filters.dimension") }}</span>
      <el-select v-model="state.dimension" style="width: 140px">
        <el-option v-for="opt in dimensionOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
      </el-select>
      <span class="filter-label">{{ t("stats.filters.metrics") }}</span>
      <MetricSelect v-model="state.metrics" :dimension="state.dimension" width="min(520px, 100%)" />
      <el-tag v-if="primary" type="info" effect="plain" disable-transitions>{{ t("stats.table.primary") }}：{{ t(`stats.metrics.${primary}`) }}</el-tag>
      <span class="spacer" />
      <el-button v-permission="'stats.reports.export'" :icon="Download" :loading="exporting" :disabled="!state.metrics.length" @click="onExport">
        {{ t("stats.export.button") }}
      </el-button>
    </div>

    <el-alert v-if="projectIgnored && filters.projectId > 0" type="info" :closable="false" show-icon :title="t('stats.filters.projectIgnored')" class="tab-alert" />
    <el-alert v-if="!state.metrics.length" type="warning" :closable="false" show-icon :title="t('stats.filters.metricsRequired')" class="tab-alert" />
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="tab-alert" />

    <el-row :gutter="16">
      <el-col :xs="24" :lg="10" class="bd-col">
        <TrendChart :dates="chartData.dates" :series="chartData.series" :loading="request.loading.value" :height="360" :label-max-length="10" />
      </el-col>
      <el-col :xs="24" :lg="14" class="bd-col">
        <el-table
          v-loading="request.loading.value"
          :data="rows"
          size="small"
          border
          max-height="520"
          class="bd-table"
          :row-class-name="rowClass"
          @row-click="onRowClick"
        >
          <el-table-column :label="t('stats.table.key')" min-width="110">
            <template #default="{ row }"><span class="mono">{{ row.key }}</span></template>
          </el-table-column>
          <el-table-column :label="t('stats.table.label')" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">
              <el-tooltip v-if="ownerClickable(row)" :content="t('stats.table.switchOwner')" placement="top">
                <el-link type="primary" underline="never">{{ rowLabel(row) }}</el-link>
              </el-tooltip>
              <span v-else>{{ rowLabel(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-for="m in loadedMetrics" :key="m" :label="`${t(`stats.metrics.${m}`)}${unitSuffix(m)}`" align="right" min-width="130">
            <template #default="{ row }">{{ formatMetric(m, metricValue(row, m)) }}</template>
          </el-table-column>
          <el-table-column :label="t('stats.table.share')" align="right" min-width="90">
            <template #default="{ row }">{{ shareText(row) }}</template>
          </el-table-column>
          <el-table-column :label="t('stats.table.actions')" width="100" fixed="right">
            <template #default="{ row }">
              <el-button v-if="canViewTrend(row)" link type="primary" @click.stop="viewTrend(row)">{{ t("stats.table.viewTrend") }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.filter-label {
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.tab-alert {
  margin-bottom: 12px;
}
.bd-col {
  margin-bottom: 12px;
}
.bd-table {
  width: 100%;
}
.bd-table :deep(.is-clickable-row) {
  cursor: pointer;
}
</style>
