<script setup lang="ts">
// 报表页「趋势」Tab（docs/12 §6.1~§6.3；docs/04 §7.14）：GET /admin/stats/trends。
// - 维度（stats_dimension）+ 维度键（候选来自同维度、同范围、同项目的 GET /admin/stats/breakdown，§6.2）；
// - 指标按 §4.2 矩阵过滤（切换维度时剔除不支持的指标）；粒度 日 / 周 / 月，周 / 月首尾不完整周期显示角标；
// - TrendChart（折线 / 柱状切换）+ 原始行表格（表头带单位）；导出 CSV（stats.reports.export）。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Download, QuestionFilled } from "@element-plus/icons-vue";
import { STATS_DIMENSION, STATS_GRANULARITY, type BreakdownRow, type StatsDimension, type TrendPoint } from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import TrendChart, { type TrendSeries } from "@/components/TrendChart.vue";
import { useLatestRequest } from "@/composables/useLatestRequest";
import { formatMetric, incompleteEdges, unitOf } from "@/utils/stats";
import { exportCsv } from "./exportCsv";
import MetricSelect from "./MetricSelect.vue";
import type { ReportFilters, TrendState } from "./types";

const props = defineProps<{ filters: ReportFilters; active: boolean; refreshKey?: number }>();
const state = defineModel<TrendState>("state", { required: true });
const emit = defineEmits<{ (e: "project-gone"): void }>();

const { t, te } = useI18n();

// ---------- 维度与维度键 ----------
const dimensionOptions = computed(() => STATS_DIMENSION.map((d) => ({ value: d, label: t(`stats.dimension.${d}`) })));

function onDimensionChange(dim: StatsDimension) {
  state.value.dimension_key = "";
  normalizeMetrics(dim);
}

function normalizeMetrics(dim: StatsDimension) {
  const kept = state.value.metrics.filter((m) => statsApi.metricSupports(m, dim));
  state.value.metrics = kept.length ? kept : [...statsApi.DEFAULT_DIMENSION_METRICS[dim]];
}

watch(
  () => state.value.dimension,
  (dim) => normalizeMetrics(dim),
  { immediate: true },
);

const keyRequest = useLatestRequest();
const keyOptions = ref<{ value: string; label: string }[]>([]);

function keyLabel(dim: StatsDimension, row: BreakdownRow): string {
  if (dim === "capability" && te(`stats.capability.${row.key}`)) return t(`stats.capability.${row.key}`);
  return row.label || row.key;
}

async function loadKeys() {
  const dim = state.value.dimension;
  if (dim === "total") {
    keyOptions.value = [];
    return;
  }
  const res = await keyRequest.run((signal) =>
    statsApi.breakdown(
      { dimension: dim, metric: statsApi.DIMENSION_KEY_METRIC[dim], start: props.filters.start, end: props.filters.end, project_id: props.filters.projectId },
      { silent: true, signal },
    ),
  );
  if (!res) return;
  if (!res.ok) {
    keyOptions.value = [];
    if (statsApi.isProjectGone(res.error) && props.filters.projectId > 0) emit("project-gone");
    return;
  }
  keyOptions.value = res.value.map((row) => ({ value: row.key, label: keyLabel(dim, row) }));
  if (!state.value.dimension_key && keyOptions.value.length) state.value.dimension_key = keyOptions.value[0].value;
}

const keyParams = computed(() => [props.active, state.value.dimension, props.filters.start, props.filters.end, props.filters.projectId, props.refreshKey] as const);
watch(
  keyParams,
  ([active]) => {
    if (active) keyRequest.debounce(() => void loadKeys());
  },
  { immediate: true },
);

/** 维度键可能来自路由 / 「查看趋势」，候选尚未包含时补一项 */
const keySelectOptions = computed(() => {
  const k = state.value.dimension_key;
  if (k && !keyOptions.value.some((o) => o.value === k)) return [{ value: k, label: k }, ...keyOptions.value];
  return keyOptions.value;
});

// ---------- 趋势数据 ----------
const request = useLatestRequest();
const rows = ref<TrendPoint[]>([]);
const loadedMetrics = ref<string[]>([]);
const error = ref<string | null>(null);

const ready = computed(() => state.value.metrics.length > 0 && (state.value.dimension === "total" || !!state.value.dimension_key));

function queryParams(): statsApi.TrendsParams {
  return {
    metrics: state.value.metrics,
    granularity: state.value.granularity,
    start: props.filters.start,
    end: props.filters.end,
    project_id: props.filters.projectId,
    dimension: state.value.dimension,
    dimension_key: state.value.dimension === "total" ? undefined : state.value.dimension_key,
  };
}

async function load() {
  if (!ready.value) {
    rows.value = [];
    return;
  }
  const params = queryParams();
  const metrics = [...state.value.metrics];
  const res = await request.run((signal) => statsApi.trends(params, { silent: true, signal }));
  if (!res) return;
  if (res.ok) {
    rows.value = res.value;
    loadedMetrics.value = metrics;
    error.value = null;
    return;
  }
  if (statsApi.isProjectGone(res.error) && props.filters.projectId > 0) {
    emit("project-gone");
    return;
  }
  error.value = res.error instanceof Error ? res.error.message : t("common.requestFailed");
}

const queryKey = computed(() =>
  JSON.stringify([props.active, state.value.metrics, state.value.granularity, state.value.dimension, state.value.dimension_key, props.filters, props.refreshKey]),
);
watch(
  queryKey,
  () => {
    if (props.active) request.debounce(() => void load());
    else request.abort();
  },
  { immediate: true },
);

// ---------- 图表与表格 ----------
const edges = computed(() => incompleteEdges(props.filters.start, props.filters.end, state.value.granularity));
const dates = computed(() => rows.value.map((r) => String(r.date)));

const chartSeries = computed<TrendSeries[]>(() =>
  loadedMetrics.value.map((m) => ({
    key: m,
    name: t(`stats.metrics.${m}`),
    unit: unitOf(m),
    type: state.value.chartType,
    data: rows.value.map((r) => {
      const v = r[m];
      return v === null || v === undefined || v === "" ? null : Number(v);
    }),
  })),
);

/**
 * TrendChart 最多两个 Y 轴（两个单位组），超过时按单位出现顺序每两组拆成一张图（docs/12 §5.4「调用方应拆成多张图」）。
 */
const chartGroups = computed<TrendSeries[][]>(() => {
  const units: string[] = [];
  for (const s of chartSeries.value) if (!units.includes(s.unit)) units.push(s.unit);
  const groups: TrendSeries[][] = [];
  for (let i = 0; i < units.length; i += 2) {
    const pair = units.slice(i, i + 2);
    groups.push(chartSeries.value.filter((s) => pair.includes(s.unit)));
  }
  return groups.length ? groups : [[]];
});

function isEdgeRow(index: number): boolean {
  const last = rows.value.length - 1;
  return (index === 0 && edges.value.start) || (index === last && edges.value.end);
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
  if (!ready.value) return;
  exporting.value = true;
  try {
    await exportCsv({ report: "trends", ...queryParams() });
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <div class="trends-tab">
    <div class="toolbar">
      <span class="filter-label">{{ t("stats.filters.granularity") }}</span>
      <el-radio-group v-model="state.granularity" size="default">
        <el-radio-button v-for="g in STATS_GRANULARITY" :key="g" :value="g">{{ t(`status.stats_granularity.${g}`) }}</el-radio-button>
      </el-radio-group>
      <span class="filter-label">{{ t("stats.filters.dimension") }}</span>
      <el-select v-model="state.dimension" style="width: 140px" @change="onDimensionChange">
        <el-option v-for="opt in dimensionOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
      </el-select>
      <el-tooltip :content="t('stats.filters.dimensionTip')" placement="top">
        <el-icon class="tip-icon"><QuestionFilled /></el-icon>
      </el-tooltip>
      <el-select
        v-if="state.dimension !== 'total'"
        v-model="state.dimension_key"
        filterable
        :loading="keyRequest.loading.value"
        :placeholder="t('stats.filters.selectDimensionKey')"
        style="width: 200px"
      >
        <el-option v-for="opt in keySelectOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
      </el-select>
    </div>
    <div class="toolbar">
      <span class="filter-label">{{ t("stats.filters.metrics") }}</span>
      <MetricSelect v-model="state.metrics" :dimension="state.dimension" width="min(520px, 100%)" />
      <el-radio-group v-model="state.chartType" size="default">
        <el-radio-button value="line">{{ t("stats.chartType.line") }}</el-radio-button>
        <el-radio-button value="bar">{{ t("stats.chartType.bar") }}</el-radio-button>
      </el-radio-group>
      <span class="spacer" />
      <el-button v-permission="'stats.reports.export'" :icon="Download" :loading="exporting" :disabled="!ready" @click="onExport">
        {{ t("stats.export.button") }}
      </el-button>
    </div>

    <el-alert v-if="!state.metrics.length" type="warning" :closable="false" show-icon :title="t('stats.filters.metricsRequired')" class="tab-alert" />
    <el-alert
      v-else-if="state.dimension !== 'total' && !state.dimension_key"
      type="warning"
      :closable="false"
      show-icon
      :title="t('stats.filters.dimensionKeyRequired')"
      class="tab-alert"
    />
    <el-alert v-if="chartGroups.length > 1" type="info" :closable="false" show-icon :title="t('stats.chartsSplit', { n: chartGroups.length })" class="tab-alert" />
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="tab-alert" />

    <TrendChart
      v-for="(group, i) in chartGroups"
      :key="group.map((s) => s.key).join(',') || i"
      :dates="dates"
      :series="group"
      :loading="request.loading.value"
      :incomplete-edges="edges"
      :height="chartGroups.length > 1 ? 280 : 340"
      class="trend-chart"
    />

    <el-table v-loading="request.loading.value" :data="rows" size="small" border class="trend-table" max-height="480">
      <el-table-column :label="t('stats.table.date')" min-width="130" fixed>
        <template #default="{ row, $index }">
          <span class="mono">{{ row.date }}</span>
          <el-tag v-if="isEdgeRow($index)" size="small" type="warning" effect="plain" class="edge-tag" disable-transitions>{{ t("stats.incomplete") }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column v-for="m in loadedMetrics" :key="m" :label="`${t(`stats.metrics.${m}`)}${unitSuffix(m)}`" align="right" min-width="140">
        <template #default="{ row }">{{ formatMetric(m, row[m]) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.filter-label {
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.tip-icon {
  color: var(--el-text-color-placeholder);
  cursor: help;
}
.tab-alert {
  margin-bottom: 12px;
}
.trend-chart + .trend-chart {
  margin-top: 12px;
}
.trend-table {
  margin-top: 12px;
  width: 100%;
}
.edge-tag {
  margin-left: 6px;
}
</style>
