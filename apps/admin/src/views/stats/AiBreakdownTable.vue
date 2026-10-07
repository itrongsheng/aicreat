<script setup lang="ts">
// AI 消耗 Tab 的分解表（docs/12 §7.1、§7.3）：每张表两次 GET /admin/stats/breakdown（metric 均 ≤ 8 个）：
// ① ai_calls,ai_success_rate,tokens_total,quota_estimated,quota_estimated_reconciled,quota_actual,quota_reconciled_rate,cost_cny
//    （dimension=admin 时去掉 quota_reconciled_rate，该列显示 --）决定行集合、排序与占比；
// ② task_success_rate,task_avg_duration_ms 的 values 按 key 并入同一行；
// 对账差异 quota_diff / quota_diff_rate 由前端用同一行的 quota_actual 与 quota_estimated_reconciled 计算；表尾合计行。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { QuestionFilled } from "@element-plus/icons-vue";
import type { BreakdownDimension, BreakdownRow } from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import { useLatestRequest } from "@/composables/useLatestRequest";
import { aggregateMetric, formatMetric, type ValueRow } from "@/utils/stats";
import type { ReportFilters } from "./types";

const props = defineProps<{
  dimension: BreakdownDimension;
  title: string;
  filters: ReportFilters;
  active: boolean;
  /** 递增时强制重新请求（报表页「刷新」） */
  refreshKey?: number;
}>();
const emit = defineEmits<{ (e: "project-gone"): void }>();

const { t, te } = useI18n();

const PRIMARY_METRICS = [
  "ai_calls",
  "ai_success_rate",
  "tokens_total",
  "quota_estimated",
  "quota_estimated_reconciled",
  "quota_actual",
  "quota_reconciled_rate",
  "cost_cny",
];
const TASK_METRICS = ["task_success_rate", "task_avg_duration_ms"];

const firstMetrics = computed(() => PRIMARY_METRICS.filter((m) => statsApi.metricSupports(m, props.dimension)));
const projectIgnored = computed(() => props.dimension === "project" || props.dimension === "owner");

interface AiRow extends ValueRow {
  key: string;
  label: string;
  share: number | null;
}

const request = useLatestRequest();
const rows = ref<AiRow[]>([]);
const error = ref<string | null>(null);

function rowLabel(row: BreakdownRow): string {
  if (props.dimension === "capability" && te(`stats.capability.${row.key}`)) return t(`stats.capability.${row.key}`);
  if (props.dimension === "owner" && row.key === "0") return row.label || t("stats.deletedProject");
  return row.label || row.key;
}

async function load() {
  const base = {
    dimension: props.dimension,
    start: props.filters.start,
    end: props.filters.end,
    project_id: projectIgnored.value ? 0 : props.filters.projectId,
  };
  const res = await request.run((signal) =>
    Promise.all([
      statsApi.breakdown({ ...base, metric: firstMetrics.value }, { silent: true, signal }),
      statsApi.breakdown({ ...base, metric: TASK_METRICS }, { silent: true, signal }),
    ]),
  );
  if (!res) return;
  if (!res.ok) {
    if (statsApi.isProjectGone(res.error) && props.filters.projectId > 0) {
      emit("project-gone");
      return;
    }
    error.value = res.error instanceof Error ? res.error.message : t("common.requestFailed");
    return;
  }
  const [main, tasks] = res.value;
  const taskByKey = new Map(tasks.map((r) => [r.key, r.values ?? {}]));
  rows.value = main.map((r) => {
    const values: Record<string, number | null> = { ...(r.values ?? {}), ...(taskByKey.get(r.key) ?? {}) };
    if (values.ai_calls === undefined) values.ai_calls = r.value;
    const actual = values.quota_actual;
    const base2 = values.quota_estimated_reconciled;
    values.quota_diff = actual === null || actual === undefined || base2 === null || base2 === undefined ? null : actual - base2;
    values.quota_diff_rate = values.quota_diff === null || !base2 ? null : (values.quota_diff as number) / base2;
    return { key: r.key, label: rowLabel(r), share: r.share, ...values };
  });
  error.value = null;
}

const queryKey = computed(() => JSON.stringify([props.active, props.dimension, props.filters, projectIgnored.value, props.refreshKey]));
watch(
  queryKey,
  () => {
    if (props.active) request.debounce(() => void load());
    else request.abort();
  },
  { immediate: true },
);

interface Column {
  metric: string;
  help?: string;
  minWidth: number;
}

const columns = computed<Column[]>(() => [
  { metric: "ai_calls", minWidth: 90 },
  { metric: "ai_success_rate", minWidth: 100 },
  { metric: "task_success_rate", minWidth: 100 },
  { metric: "task_avg_duration_ms", minWidth: 120 },
  { metric: "tokens_total", minWidth: 110 },
  { metric: "quota_estimated", minWidth: 120, help: t("stats.quotaHelp") },
  { metric: "quota_actual", minWidth: 120, help: t("stats.quotaHelp") },
  { metric: "quota_reconciled_rate", minWidth: 100, help: props.dimension === "admin" ? t("stats.ai.adminNoReconciled") : undefined },
  { metric: "quota_diff", minWidth: 120, help: t("stats.diffHelp") },
  { metric: "quota_diff_rate", minWidth: 110, help: t("stats.diffHelp") },
  { metric: "cost_cny", minWidth: 110, help: t("stats.costHelp") },
]);

function cell(row: AiRow, metric: string): string {
  if (metric === "quota_reconciled_rate" && props.dimension === "admin") return "--";
  return formatMetric(metric, row[metric] as number | null | undefined);
}

/** 表尾合计：流量列求和，比率按分子分母（或「比率 × 分母」）重算，对账差异按 §7.3 */
function summaryMethod({ columns: cols }: { columns: { property?: string; label?: string }[] }): string[] {
  return cols.map((c, idx) => {
    if (idx === 0) return t("stats.table.total");
    const metric = c.property;
    if (!metric) return "";
    if (metric === "share") return rows.value.length ? "100%" : "--";
    if (metric === "quota_reconciled_rate" && props.dimension === "admin") return "--";
    return formatMetric(metric, aggregateMetric(rows.value, metric));
  });
}

function shareText(row: AiRow): string {
  return formatMetric("ai_success_rate", row.share);
}
</script>

<template>
  <el-card shadow="never" class="ai-bd-card">
    <template #header>
      <div class="ai-bd-header">
        <span class="ai-bd-title">{{ title }}</span>
        <span v-if="dimension === 'admin'" class="text-secondary small">{{ t("stats.ai.adminNoReconciled") }}</span>
      </div>
    </template>
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="ai-bd-alert" />
    <el-table v-loading="request.loading.value" :data="rows" size="small" border show-summary :summary-method="summaryMethod" max-height="420" class="ai-bd-table">
      <el-table-column :label="t(`stats.dimension.${dimension}`)" min-width="150" fixed show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.label }}</span>
          <span v-if="row.label !== row.key" class="mono text-secondary key-suffix">{{ row.key }}</span>
        </template>
      </el-table-column>
      <el-table-column v-for="c in columns" :key="c.metric" :prop="c.metric" align="right" :min-width="c.minWidth">
        <template #header>
          <el-tooltip v-if="c.help" :content="c.help" placement="top">
            <span class="th-help">{{ t(`stats.metrics.${c.metric}`) }} <el-icon><QuestionFilled /></el-icon></span>
          </el-tooltip>
          <span v-else>{{ t(`stats.metrics.${c.metric}`) }}</span>
        </template>
        <template #default="{ row }">{{ cell(row, c.metric) }}</template>
      </el-table-column>
      <el-table-column prop="share" :label="t('stats.table.share')" align="right" min-width="90">
        <template #default="{ row }">{{ shareText(row) }}</template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<style scoped>
.ai-bd-card + .ai-bd-card {
  margin-top: 12px;
}
.ai-bd-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.ai-bd-title {
  font-weight: 600;
}
.ai-bd-alert {
  margin-bottom: 8px;
}
.ai-bd-table {
  width: 100%;
}
.key-suffix {
  margin-left: 6px;
}
.th-help {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  cursor: help;
}
.small {
  font-size: 12px;
}
</style>
