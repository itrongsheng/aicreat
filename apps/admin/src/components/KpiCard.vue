<script setup lang="ts">
// KPI 卡片（docs/12 §5.3）：主值（按 format 格式化，null 显示 `--`）、副值、环比（delta_rate > 0 绿色上箭头、< 0 红色下箭头、
// null 显示 `--`；invert 时颜色反转，用于「越低越好」的指标）、SVG 迷你趋势（sparkline）、口径说明 tooltip、点击跳转（无 to 时不可点击）。
import { computed } from "vue";
import { useRouter, type RouteLocationRaw } from "vue-router";
import { useI18n } from "vue-i18n";
import { CaretBottom, CaretTop, QuestionFilled } from "@element-plus/icons-vue";
import { formatCny, formatDuration, formatNumber, formatPercent, formatQuota, formatTokens } from "@/utils/format";

export type KpiFormat = "number" | "percent" | "currency" | "duration" | "quota" | "tokens";

export interface KpiCompare {
  previous: number | null;
  delta: number | null;
  delta_rate: number | null;
}

const props = withDefaults(
  defineProps<{
    title: string;
    value: number | null | undefined;
    format?: KpiFormat;
    /** 副值（已格式化） */
    sub?: string;
    compare?: KpiCompare | null;
    /** 迷你趋势；无则不渲染 */
    sparkline?: number[];
    /** 口径说明（el-tooltip） */
    help?: string;
    /** 点击跳转；调用方在无对应权限时不传 */
    to?: RouteLocationRaw;
    loading?: boolean;
    /** 越低越好：环比颜色反转 */
    invert?: boolean;
    /** 卡片角标（如「检测口径」） */
    badge?: string;
  }>(),
  { format: "number", sub: "", compare: undefined, sparkline: undefined, help: "", to: undefined, loading: false, invert: false, badge: "" },
);

const { t } = useI18n();
const router = useRouter();

const display = computed(() => {
  const v = props.value;
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "--";
  switch (props.format) {
    case "percent":
      return formatPercent(v, 2);
    case "currency":
      return formatCny(v);
    case "duration":
      return formatDuration(v);
    case "quota":
      return formatQuota(v);
    case "tokens":
      return formatTokens(v);
    default:
      return formatNumber(v, Number.isInteger(Number(v)) ? 0 : 2);
  }
});

/** 显示环比：compare 为 undefined（指标无环比）时不渲染；有 compare 但 delta_rate 为 null 时显示 `--` */
const showCompare = computed(() => props.compare !== undefined);
const rate = computed(() => props.compare?.delta_rate ?? null);
const direction = computed<"up" | "down" | "flat" | "none">(() => {
  const r = rate.value;
  if (r === null || !Number.isFinite(r)) return "none";
  if (r > 0) return "up";
  if (r < 0) return "down";
  return "flat";
});
const tone = computed(() => {
  if (direction.value === "up") return props.invert ? "bad" : "good";
  if (direction.value === "down") return props.invert ? "good" : "bad";
  return "neutral";
});
const rateText = computed(() => (rate.value === null ? "--" : `${(Math.abs(rate.value) * 100).toFixed(1)}%`));
const compareTitle = computed(() => {
  const c = props.compare;
  if (!c) return "";
  const prev = c.previous === null ? "--" : formatPlain(c.previous);
  return t("dashboard.comparePrevious", { value: prev });
});

function formatPlain(v: number): string {
  if (props.format === "percent") return formatPercent(v, 2);
  if (props.format === "currency") return formatCny(v);
  if (props.format === "duration") return formatDuration(v);
  if (props.format === "tokens") return formatTokens(v);
  return formatNumber(v, Number.isInteger(v) ? 0 : 2);
}

// ---------- 迷你趋势（SVG） ----------
const W = 120;
const H = 32;
const sparkPoints = computed(() => {
  const data = (props.sparkline ?? []).map((v) => (Number.isFinite(Number(v)) ? Number(v) : 0));
  if (data.length < 2) return "";
  const min = Math.min(...data);
  const max = Math.max(...data);
  const span = max - min || 1;
  const step = W / (data.length - 1);
  return data.map((v, i) => `${(i * step).toFixed(1)},${(H - 2 - ((v - min) / span) * (H - 4)).toFixed(1)}`).join(" ");
});
const sparkArea = computed(() => (sparkPoints.value ? `0,${H} ${sparkPoints.value} ${W},${H}` : ""));

const clickable = computed(() => !!props.to && !props.loading);

function onClick() {
  if (clickable.value && props.to) void router.push(props.to);
}
</script>

<template>
  <el-card
    shadow="never"
    class="kpi-card"
    :class="{ 'is-clickable': clickable }"
    :body-style="{ padding: '14px 16px' }"
    :tabindex="clickable ? 0 : undefined"
    :role="clickable ? 'link' : undefined"
    @click="onClick"
    @keydown.enter="onClick"
  >
    <el-skeleton v-if="loading" animated :rows="2" />
    <template v-else>
      <div class="kpi-head">
        <span class="kpi-title" :title="title">{{ title }}</span>
        <el-tag v-if="badge" size="small" type="info" effect="plain" disable-transitions class="kpi-badge">{{ badge }}</el-tag>
        <el-tooltip v-if="help" :content="help" placement="top" :show-after="200">
          <el-icon class="kpi-help" @click.stop><QuestionFilled /></el-icon>
        </el-tooltip>
      </div>
      <div class="kpi-main">
        <span class="kpi-value">{{ display }}</span>
        <span v-if="showCompare" class="kpi-compare" :class="`is-${tone}`" :title="compareTitle">
          <el-icon v-if="direction === 'up'"><CaretTop /></el-icon>
          <el-icon v-else-if="direction === 'down'"><CaretBottom /></el-icon>
          {{ rateText }}
        </span>
      </div>
      <div class="kpi-foot">
        <span class="kpi-sub" :title="sub">{{ sub }}</span>
        <svg v-if="sparkPoints" class="kpi-spark" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" aria-hidden="true">
          <polygon :points="sparkArea" class="kpi-spark-area" />
          <polyline :points="sparkPoints" class="kpi-spark-line" />
        </svg>
      </div>
    </template>
  </el-card>
</template>

<style scoped>
.kpi-card {
  height: 100%;
  min-height: 112px;
  transition: border-color 0.15s;
}
.kpi-card.is-clickable {
  cursor: pointer;
}
.kpi-card.is-clickable:hover,
.kpi-card.is-clickable:focus-visible {
  border-color: var(--el-color-primary-light-5);
  outline: none;
}
.kpi-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.kpi-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kpi-badge {
  flex: none;
}
.kpi-help {
  flex: none;
  cursor: help;
  color: var(--el-text-color-placeholder);
}
.kpi-main {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 10px;
  margin: 6px 0 4px;
}
.kpi-value {
  font-size: 24px;
  font-weight: 600;
  line-height: 1.2;
  color: var(--el-text-color-primary);
  font-variant-numeric: tabular-nums;
}
.kpi-compare {
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.kpi-compare.is-good {
  color: var(--el-color-success);
}
.kpi-compare.is-bad {
  color: var(--el-color-danger);
}
.kpi-compare.is-neutral {
  color: var(--el-text-color-secondary);
}
.kpi-foot {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 8px;
  min-height: 32px;
}
.kpi-sub {
  min-width: 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
}
.kpi-spark {
  flex: none;
  width: 96px;
  height: 32px;
}
.kpi-spark-line {
  fill: none;
  stroke: var(--el-color-primary);
  stroke-width: 1.5;
  vector-effect: non-scaling-stroke;
}
.kpi-spark-area {
  fill: var(--el-color-primary-light-8);
  opacity: 0.6;
  stroke: none;
}
</style>
