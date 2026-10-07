<script setup lang="ts">
// 报表页「AI 消耗」Tab（docs/12 §6.6、§7）：数据全部来自 /admin/stats/trends 与 /admin/stats/breakdown，不依赖 ai.* 权限。
// - 汇总卡片：一次 trends（8 个指标，比率的分子分母列由后端自动附带）→ 前端对流量列求和、比率按分子分母重算；
// - 趋势图：quota_estimated / quota_actual 左轴，cost_cny 右轴，按粒度；
// - 分解表：按能力 / 按模型 / 按项目（project_id=0 时）/ 按人员 / 按用户（仅总后台且 project_id=0 时），见 AiBreakdownTable.vue；
// - 「说明」折叠面板：对账率 < 100% 的常见原因（§7.3 原文）；具备 ai.usage.view 时提供「查看对账日志」。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { STATS_GRANULARITY, type TrendPoint } from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import KpiCard, { type KpiFormat } from "@/components/KpiCard.vue";
import TrendChart, { type TrendSeries } from "@/components/TrendChart.vue";
import { useLatestRequest } from "@/composables/useLatestRequest";
import { usePermission } from "@/composables/usePermission";
import { aggregateMetric, incompleteEdges } from "@/utils/stats";
import AiBreakdownTable from "./AiBreakdownTable.vue";
import type { AiState, ReportFilters } from "./types";

const props = defineProps<{ filters: ReportFilters; active: boolean; refreshKey?: number }>();
const state = defineModel<AiState>("state", { required: true });
const emit = defineEmits<{ (e: "project-gone"): void }>();

const { t } = useI18n();
const { has, isAllScope } = usePermission();

const SUMMARY_METRICS = [
  "ai_calls",
  "ai_success_rate",
  "tokens_total",
  "quota_estimated",
  "quota_actual",
  "quota_reconciled_rate",
  "cost_cny",
  "cost_cny_per_content",
];

const request = useLatestRequest();
const rows = ref<TrendPoint[]>([]);
const error = ref<string | null>(null);

async function load() {
  const res = await request.run((signal) =>
    statsApi.trends(
      {
        metrics: SUMMARY_METRICS,
        granularity: state.value.granularity,
        start: props.filters.start,
        end: props.filters.end,
        project_id: props.filters.projectId,
        dimension: "total",
      },
      { silent: true, signal },
    ),
  );
  if (!res) return;
  if (res.ok) {
    rows.value = res.value;
    error.value = null;
    return;
  }
  if (statsApi.isProjectGone(res.error) && props.filters.projectId > 0) {
    emit("project-gone");
    return;
  }
  error.value = res.error instanceof Error ? res.error.message : t("common.requestFailed");
}

const queryKey = computed(() => JSON.stringify([props.active, state.value.granularity, props.filters, props.refreshKey]));
watch(
  queryKey,
  () => {
    if (props.active) request.debounce(() => void load());
    else request.abort();
  },
  { immediate: true },
);

// ---------- 汇总卡片 ----------
const CARDS: { metric: string; format: KpiFormat; help?: string }[] = [
  { metric: "ai_calls", format: "number" },
  { metric: "ai_success_rate", format: "percent" },
  { metric: "tokens_total", format: "tokens" },
  { metric: "quota_estimated", format: "quota", help: "stats.quotaHelp" },
  { metric: "quota_actual", format: "quota", help: "stats.quotaHelp" },
  { metric: "quota_reconciled_rate", format: "percent" },
  { metric: "cost_cny", format: "currency", help: "stats.costHelp" },
  { metric: "cost_cny_per_content", format: "currency", help: "stats.costHelp" },
];

const summary = computed(() =>
  CARDS.map((c) => ({ ...c, title: t(`stats.metrics.${c.metric}`), value: rows.value.length ? aggregateMetric(rows.value, c.metric) : null })),
);

// ---------- 趋势图 ----------
const dates = computed(() => rows.value.map((r) => String(r.date)));
function seriesOf(metric: string): (number | null)[] {
  return rows.value.map((r) => {
    const v = r[metric];
    return v === null || v === undefined || v === "" ? null : Number(v);
  });
}
const chartSeries = computed<TrendSeries[]>(() => [
  { key: "quota_estimated", name: t("stats.metrics.quota_estimated"), unit: "quota", data: seriesOf("quota_estimated") },
  { key: "quota_actual", name: t("stats.metrics.quota_actual"), unit: "quota", data: seriesOf("quota_actual") },
  { key: "cost_cny", name: t("stats.metrics.cost_cny"), unit: "currency", data: seriesOf("cost_cny") },
]);
const edges = computed(() => incompleteEdges(props.filters.start, props.filters.end, state.value.granularity));

// ---------- 分解表 ----------
const showProject = computed(() => props.filters.projectId === 0);
const showOwner = computed(() => isAllScope.value && props.filters.projectId === 0);

const REASONS = ["overflow", "pending", "noHeader", "noHttp", "failed"] as const;
const reasonRows = computed(() =>
  REASONS.map((k) => ({
    reason: t(`stats.ai.reasons.${k}.reason`),
    description: t(`stats.ai.reasons.${k}.description`),
    handling: t(`stats.ai.reasons.${k}.handling`),
  })),
);
const explainOpen = ref<string[]>([]);
</script>

<template>
  <div class="ai-tab">
    <div class="toolbar">
      <span class="filter-label">{{ t("stats.filters.granularity") }}</span>
      <el-radio-group v-model="state.granularity">
        <el-radio-button v-for="g in STATS_GRANULARITY" :key="g" :value="g">{{ t(`status.stats_granularity.${g}`) }}</el-radio-button>
      </el-radio-group>
      <span class="spacer" />
      <router-link v-if="has('ai.usage.view')" :to="{ name: 'ai-usage' }" class="usage-link">{{ t("stats.ai.viewUsageLogs") }}</router-link>
    </div>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="tab-alert" />

    <el-row :gutter="12">
      <el-col v-for="c in summary" :key="c.metric" :xs="12" :sm="8" :md="6" :lg="6" :xl="3" class="sum-col">
        <KpiCard
          :title="c.title"
          :value="c.value"
          :format="c.format"
          :help="c.help ? t(c.help) : ''"
          :loading="request.loading.value && !rows.length"
        />
      </el-col>
    </el-row>

    <el-card shadow="never" class="ai-chart-card">
      <template #header>
        <span class="section-title">{{ t("stats.ai.trendTitle") }}</span>
      </template>
      <TrendChart :dates="dates" :series="chartSeries" :loading="request.loading.value" :incomplete-edges="edges" :height="300" />
    </el-card>

    <AiBreakdownTable :refresh-key="refreshKey" dimension="capability" :title="t('stats.ai.byCapability')" :filters="filters" :active="active" @project-gone="emit('project-gone')" />
    <AiBreakdownTable :refresh-key="refreshKey" dimension="model" :title="t('stats.ai.byModel')" :filters="filters" :active="active" @project-gone="emit('project-gone')" />
    <AiBreakdownTable v-if="showProject" :refresh-key="refreshKey" dimension="project" :title="t('stats.ai.byProject')" :filters="filters" :active="active" />
    <AiBreakdownTable :refresh-key="refreshKey" dimension="admin" :title="t('stats.ai.byAdmin')" :filters="filters" :active="active" @project-gone="emit('project-gone')" />
    <AiBreakdownTable v-if="showOwner" :refresh-key="refreshKey" dimension="owner" :title="t('stats.ai.byOwner')" :filters="filters" :active="active" />

    <el-collapse v-model="explainOpen" class="ai-explain">
      <el-collapse-item name="reasons" :title="t('stats.ai.explain')">
        <el-table :data="reasonRows" size="small" border>
          <el-table-column prop="reason" :label="t('stats.ai.reason')" min-width="140" />
          <el-table-column prop="description" :label="t('stats.ai.description')" min-width="280" />
          <el-table-column prop="handling" :label="t('stats.ai.handling')" min-width="280" />
        </el-table>
        <p class="text-secondary small explain-note">{{ t("stats.ai.diffRateWarn") }}</p>
      </el-collapse-item>
    </el-collapse>
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
.sum-col {
  margin-bottom: 12px;
}
.ai-chart-card {
  margin-bottom: 12px;
}
.section-title {
  font-weight: 600;
}
.usage-link {
  color: var(--el-color-primary);
  font-size: 13px;
  text-decoration: none;
}
.ai-explain {
  margin-top: 12px;
}
.explain-note {
  margin: 8px 0 0;
}
.small {
  font-size: 12px;
}
</style>
