<script setup lang="ts">
// 报表页「明细榜」Tab（docs/12 §6.5、§9.4）：GET /admin/stats/rankings。
// 五种榜单各自的表列；条数 10 / 20 / 50，不选时不传 limit（后端取 stats_config.rankings_limit）；top_cost_projects 忽略项目筛选；导出 CSV。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Download } from "@element-plus/icons-vue";
import { RANKING_TYPE, type FastestIndexedRankingRow, type MetricRankingRow, type RankingType } from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import { useLatestRequest } from "@/composables/useLatestRequest";
import { usePermission } from "@/composables/usePermission";
import { formatCny, formatDateTime, formatNumber, formatPercent } from "@/utils/format";
import { exportCsv } from "./exportCsv";
import { RANKING_LIMITS, type RankingState, type ReportFilters } from "./types";

const props = defineProps<{ filters: ReportFilters; active: boolean; refreshKey?: number }>();
const state = defineModel<RankingState>("state", { required: true });
const emit = defineEmits<{ (e: "project-gone"): void }>();

const { t } = useI18n();
const { has } = usePermission();

const projectIgnored = computed(() => state.value.type === "top_cost_projects");
const canViewContents = computed(() => has("content.contents.view"));
const canViewLinks = computed(() => has("publish.links.view"));

const request = useLatestRequest();
const rows = ref<(FastestIndexedRankingRow | MetricRankingRow)[]>([]);
const loadedType = ref<RankingType>(state.value.type);
const error = ref<string | null>(null);

function queryParams(): statsApi.RankingsParams {
  return {
    type: state.value.type,
    limit: state.value.limit,
    start: props.filters.start,
    end: props.filters.end,
    project_id: projectIgnored.value ? 0 : props.filters.projectId,
  };
}

async function load() {
  const params = queryParams();
  const type = state.value.type;
  const res = await request.run((signal) => statsApi.rankings(params, { silent: true, signal }));
  if (!res) return;
  if (res.ok) {
    rows.value = res.value as (FastestIndexedRankingRow | MetricRankingRow)[];
    loadedType.value = type;
    error.value = null;
    return;
  }
  if (statsApi.isProjectGone(res.error) && props.filters.projectId > 0) {
    emit("project-gone");
    return;
  }
  error.value = res.error instanceof Error ? res.error.message : t("common.requestFailed");
}

const queryKey = computed(() => JSON.stringify([props.active, state.value.type, state.value.limit, props.filters, projectIgnored.value, props.refreshKey]));
watch(
  queryKey,
  () => {
    if (props.active) request.debounce(() => void load());
    else request.abort();
  },
  { immediate: true },
);

const fastestRows = computed(() => (loadedType.value === "fastest_indexed" ? (rows.value as FastestIndexedRankingRow[]) : []));
const metricRows = computed(() => (loadedType.value !== "fastest_indexed" ? (rows.value as MetricRankingRow[]) : []));

function extra(row: MetricRankingRow, key: string): number | null {
  const v = row.extra?.[key];
  return v === undefined ? null : v;
}

function failRate(row: MetricRankingRow): string {
  const calls = Number(extra(row, "ai_calls"));
  if (!calls) return "--";
  return formatPercent(Number(row.value) / calls, 2);
}

function pct(v: number | null): string {
  return v === null ? "--" : formatPercent(v, 2);
}

function cny(v: number | null): string {
  return v === null ? "--" : formatCny(v);
}

// ---------- 导出 ----------
const exporting = ref(false);
async function onExport() {
  exporting.value = true;
  try {
    await exportCsv({ report: "rankings", ...queryParams() });
  } finally {
    exporting.value = false;
  }
}
</script>

<template>
  <div class="rankings-tab">
    <div class="toolbar">
      <span class="filter-label">{{ t("stats.filters.type") }}</span>
      <el-select v-model="state.type" style="width: 200px">
        <el-option v-for="type in RANKING_TYPE" :key="type" :value="type" :label="t(`status.ranking_type.${type}`)" />
      </el-select>
      <span class="filter-label">{{ t("stats.filters.limit") }}</span>
      <el-select v-model="state.limit" clearable :placeholder="t('stats.filters.limitDefault')" style="width: 130px">
        <el-option v-for="n in RANKING_LIMITS" :key="n" :value="n" :label="t('stats.filters.limitOption', { n })" />
      </el-select>
      <span class="spacer" />
      <el-button v-permission="'stats.reports.export'" :icon="Download" :loading="exporting" @click="onExport">{{ t("stats.export.button") }}</el-button>
    </div>

    <el-alert v-if="projectIgnored && filters.projectId > 0" type="info" :closable="false" show-icon :title="t('stats.filters.projectIgnored')" class="tab-alert" />
    <el-alert v-if="state.type === 'fastest_indexed'" type="info" :closable="false" :title="t('stats.rankings.fastestHint')" class="tab-alert" />
    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error" class="tab-alert" />

    <!-- 收录最快 -->
    <el-table v-if="loadedType === 'fastest_indexed'" v-loading="request.loading.value" :data="fastestRows" size="small" border class="rank-table">
      <el-table-column :label="t('stats.table.rank')" prop="rank" width="70" align="center" />
      <el-table-column :label="t('stats.rankings.title')" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">
          <router-link v-if="canViewContents" :to="{ name: 'content-edit', params: { id: row.content_id } }" class="rank-link">{{ row.title }}</router-link>
          <span v-else>{{ row.title }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('stats.rankings.url')" min-width="240" show-overflow-tooltip>
        <template #default="{ row }">
          <router-link v-if="canViewLinks" :to="{ name: 'link-detail', params: { id: row.link_id } }" class="rank-link mono">{{ row.url }}</router-link>
          <span v-else class="mono">{{ row.url }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('stats.rankings.platform')" min-width="100">
        <template #default="{ row }">{{ row.platform_name || row.platform_code }}</template>
      </el-table-column>
      <el-table-column :label="t('stats.rankings.project')" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ row.project_name || `#${row.project_id}` }}</template>
      </el-table-column>
      <el-table-column :label="t('stats.rankings.publishedAt')" min-width="160">
        <template #default="{ row }">{{ formatDateTime(row.published_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('stats.rankings.firstIndexedAt')" min-width="160">
        <template #default="{ row }">{{ formatDateTime(row.first_indexed_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('stats.rankings.hours')" align="right" min-width="120">
        <template #default="{ row }">{{ formatNumber(row.hours) }}</template>
      </el-table-column>
    </el-table>

    <!-- 其它四类：{rank, key, label, value, extra} -->
    <el-table v-else v-loading="request.loading.value" :data="metricRows" size="small" border class="rank-table">
      <el-table-column :label="t('stats.table.rank')" prop="rank" width="70" align="center" />
      <template v-if="loadedType === 'most_deleted_platforms'">
        <el-table-column :label="t('stats.rankings.platform')" min-width="160">
          <template #default="{ row }">{{ row.label || row.key }} <span class="mono text-secondary">{{ row.key }}</span></template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.linksDeleted')" align="right" min-width="120">
          <template #default="{ row }">{{ formatNumber(row.value) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.linksTotal')" align="right" min-width="120">
          <template #default="{ row }">{{ formatNumber(extra(row, "links_total")) }}</template>
        </el-table-column>
      </template>
      <template v-else-if="loadedType === 'top_cost_models'">
        <el-table-column :label="t('stats.rankings.model')" min-width="200">
          <template #default="{ row }"><span class="mono">{{ row.label || row.key }}</span></template>
        </el-table-column>
        <el-table-column align="right" min-width="120">
          <template #header>
            <el-tooltip :content="t('stats.costHelp')" placement="top"><span class="th-help">{{ t("stats.rankings.cost") }}</span></el-tooltip>
          </template>
          <template #default="{ row }">{{ formatCny(row.value) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.calls')" align="right" min-width="100">
          <template #default="{ row }">{{ formatNumber(extra(row, "ai_calls")) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.successRate')" align="right" min-width="100">
          <template #default="{ row }">{{ pct(extra(row, "ai_success_rate")) }}</template>
        </el-table-column>
      </template>
      <template v-else-if="loadedType === 'top_cost_projects'">
        <el-table-column :label="t('stats.rankings.project')" min-width="200">
          <template #default="{ row }">{{ row.label || `#${row.key}` }}</template>
        </el-table-column>
        <el-table-column align="right" min-width="120">
          <template #header>
            <el-tooltip :content="t('stats.costHelp')" placement="top"><span class="th-help">{{ t("stats.rankings.cost") }}</span></el-tooltip>
          </template>
          <template #default="{ row }">{{ formatCny(row.value) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.contentsCreated')" align="right" min-width="110">
          <template #default="{ row }">{{ formatNumber(extra(row, "contents_created")) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.costPerContent')" align="right" min-width="130">
          <template #default="{ row }">{{ cny(extra(row, "cost_cny_per_content")) }}</template>
        </el-table-column>
      </template>
      <template v-else>
        <el-table-column :label="t('stats.rankings.model')" min-width="200">
          <template #default="{ row }"><span class="mono">{{ row.label || row.key }}</span></template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.failed')" align="right" min-width="110">
          <template #default="{ row }">{{ formatNumber(row.value) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.calls')" align="right" min-width="100">
          <template #default="{ row }">{{ formatNumber(extra(row, "ai_calls")) }}</template>
        </el-table-column>
        <el-table-column :label="t('stats.rankings.failRate')" align="right" min-width="100">
          <template #default="{ row }">{{ failRate(row) }}</template>
        </el-table-column>
      </template>
    </el-table>
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
.rank-table {
  width: 100%;
}
.rank-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
.rank-link:hover {
  text-decoration: underline;
}
.th-help {
  border-bottom: 1px dashed var(--el-text-color-placeholder);
  cursor: help;
}
</style>
