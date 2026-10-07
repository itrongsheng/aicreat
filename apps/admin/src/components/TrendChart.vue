<script setup lang="ts">
// echarts 折线 / 柱状封装（docs/12 §5.4、§10.5）：
// - `import('echarts')` 动态加载（只在首次渲染时下载），加载失败显示错误占位；
// - ResizeObserver 自适应宽度；主题跟随 store/theme（明暗切换时 dispose 后按新主题重建）；
// - 按单位分组，最多两个 Y 轴（第一组左轴、第二组右轴）；超过两组时 console.warn 并只画前两组（调用方应拆图）；
// - null 断线不连；图例可点击隐藏；tooltip 显示日期与各序列格式化值；
// - 数据点（日期数 × 序列数）超过 400 时启用 dataZoom；incompleteEdges 为首尾周期加「不完整」角标。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { ECharts, EChartsOption } from "echarts";
import { useThemeStore } from "@/store/theme";
import { formatMetricValue } from "@/utils/stats";
import type { MetricUnit } from "@/api/stats";

export type TrendUnit = "count" | "percent" | "currency" | "duration" | "quota" | "tokens" | "hours";

export interface TrendSeries {
  key: string;
  name: string;
  data: (number | null)[];
  unit: TrendUnit;
  type?: "line" | "bar";
}

const props = withDefaults(
  defineProps<{
    dates: string[];
    series: TrendSeries[];
    height?: number;
    loading?: boolean;
    /** 首尾周期不完整：true = 两端都标；对象可分别指定 */
    incompleteEdges?: boolean | { start: boolean; end: boolean };
    /** 横向柱状 / 分类轴不旋转标签等场景：分类轴标签最大字符数（超出省略） */
    labelMaxLength?: number;
  }>(),
  { height: 320, loading: false, incompleteEdges: false, labelMaxLength: 0 },
);

const MAX_POINTS = 400;

const { t } = useI18n();
const theme = useThemeStore();
const el = ref<HTMLDivElement | null>(null);
const chart = shallowRef<ECharts | null>(null);
const loadFailed = ref(false);
let echartsModule: typeof import("echarts") | null = null;
let modulePromise: Promise<typeof import("echarts")> | null = null;
let observer: ResizeObserver | null = null;
let disposed = false;

function loadEcharts(): Promise<typeof import("echarts")> {
  if (!modulePromise) {
    modulePromise = import("echarts");
    modulePromise.catch(() => {
      modulePromise = null;
    });
  }
  return modulePromise;
}

/** 单位分组（出现顺序），只取前两组 */
const unitGroups = computed<TrendUnit[]>(() => {
  const units: TrendUnit[] = [];
  for (const s of props.series) if (!units.includes(s.unit)) units.push(s.unit);
  return units;
});

watch(
  () => unitGroups.value.length,
  (n) => {
    if (n > 2) console.warn(`[TrendChart] ${n} unit groups given; only the first two (${unitGroups.value.slice(0, 2).join(", ")}) are drawn.`);
  },
  { immediate: true },
);

const drawnSeries = computed(() => props.series.filter((s) => unitGroups.value.indexOf(s.unit) < 2));

const edges = computed(() => {
  const v = props.incompleteEdges;
  if (typeof v === "boolean") return { start: v, end: v };
  return v ?? { start: false, end: false };
});

function isIncomplete(index: number): boolean {
  const last = props.dates.length - 1;
  return (index === 0 && edges.value.start) || (index === last && edges.value.end);
}

const isEmpty = computed(() => !props.dates.length || !drawnSeries.value.length);

function axisFormatter(unit: TrendUnit) {
  return (value: number) => {
    if (unit === "percent") return `${Math.round(value * 1000) / 10}%`;
    if (unit === "currency") return `¥${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
    if (unit === "duration") return value >= 1000 ? `${Math.round(value / 100) / 10}s` : `${value}ms`;
    if (unit === "hours") return `${value}h`;
    if (Math.abs(value) >= 1_000_000) return `${Math.round(value / 100_000) / 10}M`;
    if (Math.abs(value) >= 10_000) return `${Math.round(value / 100) / 10}K`;
    return String(value);
  };
}

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function buildOption(): EChartsOption {
  const groups = unitGroups.value.slice(0, 2);
  const totalPoints = props.dates.length * drawnSeries.value.length;
  const zoom = totalPoints > MAX_POINTS;
  const textColor = cssVar("--el-text-color-secondary", "#909399");
  const warnColor = cssVar("--el-color-warning", "#e6a23c");
  const incompleteLabel = t("stats.incomplete");
  const maxLen = props.labelMaxLength;
  return {
    backgroundColor: "transparent",
    animationDuration: 300,
    // 单 Y 轴时右侧无轴标签撑开，留出空间避免最后一个日期标签（boundaryGap=false 时居中于边缘）被裁掉
    grid: { left: 12, right: groups.length > 1 ? 12 : 40, top: 40, bottom: zoom ? 56 : 12, containLabel: true },
    legend: { type: "scroll", top: 0, textStyle: { color: textColor } },
    tooltip: {
      trigger: "axis",
      confine: true,
      axisPointer: { type: drawnSeries.value.some((s) => s.type === "bar") ? "shadow" : "line" },
      formatter: (raw: unknown) => {
        const items = (Array.isArray(raw) ? raw : [raw]) as { dataIndex: number; seriesIndex: number; marker: string; seriesName: string; value: unknown }[];
        if (!items.length) return "";
        const idx = items[0].dataIndex;
        const head = `${escapeHtml(props.dates[idx] ?? "")}${isIncomplete(idx) ? ` <span style="color:${warnColor}">(${escapeHtml(incompleteLabel)})</span>` : ""}`;
        const lines = items.map((it) => {
          const s = drawnSeries.value[it.seriesIndex];
          const v = Array.isArray(it.value) ? it.value[1] : it.value;
          const text = formatMetricValue(v === undefined || v === "-" ? null : (v as number | null), (s?.unit ?? "count") as MetricUnit);
          return `${it.marker}${escapeHtml(it.seriesName)}<span style="float:right;margin-left:16px;font-weight:600">${escapeHtml(text)}</span>`;
        });
        return [head, ...lines].join("<br/>");
      },
    },
    xAxis: {
      type: "category",
      data: props.dates,
      boundaryGap: drawnSeries.value.some((s) => s.type === "bar"),
      axisLabel: {
        color: textColor,
        hideOverlap: true,
        formatter: (value: string, index: number) => {
          const label = maxLen > 0 && value.length > maxLen ? `${value.slice(0, maxLen)}…` : value;
          return isIncomplete(index) ? `${label}\n{inc|${incompleteLabel}}` : label;
        },
        rich: { inc: { color: warnColor, fontSize: 10, padding: [2, 0, 0, 0] } },
      },
    },
    yAxis: groups.map((unit, i) => ({
      type: "value",
      position: i === 0 ? "left" : "right",
      axisLabel: { color: textColor, formatter: axisFormatter(unit) },
      splitLine: { show: i === 0 },
      ...(unit === "percent" ? { min: 0 } : {}),
    })),
    dataZoom: zoom
      ? [
          { type: "inside", start: 0, end: 100 },
          { type: "slider", start: 0, end: 100, height: 18, bottom: 8 },
        ]
      : [],
    series: drawnSeries.value.map((s) => ({
      name: s.name,
      type: s.type ?? "line",
      yAxisIndex: Math.max(0, groups.indexOf(s.unit)),
      data: s.data.map((v) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? null : Number(v))),
      connectNulls: false,
      showSymbol: props.dates.length <= 60,
      symbolSize: 5,
      smooth: false,
      barMaxWidth: 28,
      emphasis: { focus: "series" },
    })),
  } as EChartsOption;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}

async function ensureChart(): Promise<ECharts | null> {
  if (chart.value) return chart.value;
  if (!el.value) return null;
  try {
    echartsModule = echartsModule ?? (await loadEcharts());
  } catch {
    loadFailed.value = true;
    return null;
  }
  if (disposed || !el.value) return null;
  loadFailed.value = false;
  chart.value = echartsModule.init(el.value, theme.isDark ? "dark" : undefined, { renderer: "canvas" });
  return chart.value;
}

async function render() {
  if (isEmpty.value) {
    chart.value?.clear();
    return;
  }
  await nextTick();
  const instance = await ensureChart();
  if (!instance) return;
  instance.setOption(buildOption(), { notMerge: true });
}

function rebuild() {
  chart.value?.dispose();
  chart.value = null;
  void render();
}

watch(() => [props.dates, props.series, props.incompleteEdges, props.labelMaxLength], () => void render(), { deep: true });
watch(() => theme.isDark, () => rebuild());
watch(
  () => props.height,
  () => void nextTick(() => chart.value?.resize()),
);

onMounted(() => {
  void render();
  if (typeof ResizeObserver !== "undefined" && el.value) {
    observer = new ResizeObserver(() => chart.value?.resize());
    observer.observe(el.value);
  }
});

onBeforeUnmount(() => {
  disposed = true;
  observer?.disconnect();
  observer = null;
  chart.value?.dispose();
  chart.value = null;
});

function retry() {
  loadFailed.value = false;
  void render();
}

defineExpose({ resize: () => chart.value?.resize() });
</script>

<template>
  <div class="trend-chart" :style="{ height: `${height}px` }">
    <div v-show="!loadFailed && !isEmpty" ref="el" class="trend-canvas" />
    <div v-if="loadFailed" class="trend-placeholder">
      <span>{{ t("stats.chartLoadFailed") }}</span>
      <el-button link type="primary" @click="retry">{{ t("common.reload") }}</el-button>
    </div>
    <div v-else-if="isEmpty && !loading" class="trend-placeholder">
      <el-empty :image-size="60" :description="t('common.noData')" />
    </div>
    <div v-if="loading" v-loading="true" class="trend-loading" />
  </div>
</template>

<style scoped>
.trend-chart {
  position: relative;
  width: 100%;
  min-width: 0;
}
.trend-canvas {
  width: 100%;
  height: 100%;
}
.trend-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.trend-loading {
  position: absolute;
  inset: 0;
  pointer-events: none;
}
</style>
