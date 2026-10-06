<script setup lang="ts">
// 删除检测记录页（docs/11 §11.6；docs/04 §6.18、§7.12）：
// - 顶部概览（MonitoringOverview focus=link）：到期数、队列长度、今日检测数、最近执行时间、日上限使用量、monitor_worker 心跳；
// - 表格：时间、链接（link.url + platform_code，跳转链接详情）、check_type、result_status、previous_status → applied_status、http_status、
//   matched_rule、redirect_count、hamming_distance、耗时、触发人；行点击打开 EvidenceDrawer（GET /link-checks/{id} 取完整 evidence）；
// - 筛选：顶栏项目（0 = 全部可见项目）、平台、结果、检测类型、链接 ID、时间区间；路由 query 可预置 link_id / result_status / check_type / platform_id；
// - 工具栏「批量触发」：MonitoringRunDialog（monitoring.link_checks.run），用户视角下请求自动附加 owner_id。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { Refresh, Search, VideoPlay } from "@element-plus/icons-vue";
import { CHECK_TYPE, DEFAULT_PAGE_SIZE, LINK_CHECK_RESULT, type CheckType, type LinkCheck, type LinkCheckResult } from "@aicreat/shared";
import * as monitoringApi from "@/api/monitoring";
import EvidenceDrawer from "@/components/EvidenceDrawer.vue";
import MonitoringOverview from "@/components/MonitoringOverview.vue";
import MonitoringRunDialog from "@/components/MonitoringRunDialog.vue";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect, { type ToolbarOption } from "@/components/ToolbarSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { usePlatforms } from "@/composables/usePlatforms";
import { useProject } from "@/composables/useProject";
import { useAuthStore } from "@/store/auth";
import { formatDateTime, formatDuration, toUtcIso } from "@/utils/format";
import { platformLabel, shortUrl } from "@/utils/links";

const { t } = useI18n();
const route = useRoute();
const { has } = usePermission();
const auth = useAuthStore();
const { projectId, store: projectStore } = useProject();
const { platforms, load: loadPlatforms } = usePlatforms();

const canRun = computed(() => has("monitoring.link_checks.run"));
const canViewLinks = computed(() => has("publish.links.view"));

// ---------- 筛选与列表 ----------
const filters = reactive({
  platform_id: undefined as number | undefined,
  result_status: undefined as LinkCheckResult | undefined,
  check_type: undefined as CheckType | undefined,
  link_id: undefined as number | undefined,
  range: null as [Date, Date] | null,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<LinkCheck[]>([]);
const loading = ref(false);
let loadSeq = 0;

const platformOptions = computed<ToolbarOption[]>(() => platforms.value.map((p) => ({ value: p.id, label: platformLabel(p) })));

function queryParams() {
  const [start, end] = filters.range ?? [null, null];
  return {
    project_id: projectId.value || undefined,
    platform_id: filters.platform_id || undefined,
    result_status: filters.result_status,
    check_type: filters.check_type,
    link_id: filters.link_id || undefined,
    start: start ? toUtcIso(start) : undefined,
    end: end ? toUtcIso(end) : undefined,
  };
}

async function load() {
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const res = await monitoringApi.listLinkChecks({ ...queryParams(), page: page.value, page_size: pageSize.value });
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

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.platform_id = undefined;
  filters.result_status = undefined;
  filters.check_type = undefined;
  filters.link_id = undefined;
  filters.range = null;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

watch(projectId, () => search());
watch(
  () => projectStore.ownerId,
  () => search(),
);

// ---------- 行展示 ----------
function platformText(row: LinkCheck): string {
  const code = row.link?.platform_code;
  if (!code) return "-";
  const p = platforms.value.find((x) => x.code === code);
  return p ? platformLabel(p) : code;
}

function triggeredBy(row: LinkCheck): string {
  if (!row.triggered_by) return t("evidence.system");
  if (auth.admin?.id === row.triggered_by) return t("monitoring.me");
  return `#${row.triggered_by}`;
}

// ---------- 证据抽屉 ----------
const evidenceVisible = ref(false);
const evidenceRecord = ref<LinkCheck | null>(null);
const evidenceLoading = ref(false);
let evidenceSeq = 0;

async function openEvidence(row: LinkCheck) {
  const seq = ++evidenceSeq;
  evidenceRecord.value = row;
  evidenceVisible.value = true;
  evidenceLoading.value = true;
  try {
    const detail = await monitoringApi.getLinkCheck(row.id, { silent: true });
    if (seq === evidenceSeq) evidenceRecord.value = { ...detail, link: detail.link ?? row.link };
  } catch {
    /* 列表行已含 evidence，详情失败时沿用列表数据 */
  } finally {
    if (seq === evidenceSeq) evidenceLoading.value = false;
  }
}

function onRowClick(row: LinkCheck, _column: unknown, event: MouseEvent) {
  // 点击链接 / 按钮时不打开抽屉
  if ((event.target as HTMLElement | null)?.closest("a,button")) return;
  void openEvidence(row);
}

// ---------- 批量触发 ----------
const runVisible = ref(false);
const overviewRef = ref<InstanceType<typeof MonitoringOverview> | null>(null);

function onRunDone() {
  overviewRef.value?.reload();
}

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
  filters.link_id = qInt("link_id");
  filters.platform_id = qInt("platform_id");
  filters.result_status = qEnum("result_status", LINK_CHECK_RESULT);
  filters.check_type = qEnum("check_type", CHECK_TYPE);
  void loadPlatforms();
  void load();
});

</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.linkChecks") }}</h2>
        <div class="header-actions">
          <el-button v-if="canRun" type="primary" :icon="VideoPlay" @click="runVisible = true">{{ t("monitoring.run.button") }}</el-button>
        </div>
      </div>
    </template>

    <MonitoringOverview ref="overviewRef" focus="link" />

    <div class="toolbar">
      <ToolbarSelect v-model="filters.platform_id" :options="platformOptions" :placeholder="t('links.platform')" filterable width="140px" @change="search" />
      <ToolbarSelect v-model="filters.result_status" enum-name="link_check_result" :placeholder="t('monitoring.resultStatus')" width="130px" @change="search" />
      <ToolbarSelect v-model="filters.check_type" enum-name="check_type" :placeholder="t('monitoring.checkType')" width="120px" @change="search" />
      <el-input-number v-model="filters.link_id" :min="1" :controls="false" :placeholder="t('monitoring.linkId')" style="width: 110px" @change="search" />
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
      <el-button :icon="Refresh" @click="load()">{{ t("common.refresh") }}</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe class="clickable-table" @row-click="onRowClick">
      <el-table-column :label="t('evidence.checkedAt')" width="160">
        <template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('links.link')" min-width="220">
        <template #default="{ row }">
          <div class="link-cell">
            <router-link v-if="canViewLinks" :to="`/links/${row.link_id}`" class="link-id">#{{ row.link_id }}</router-link>
            <span v-else class="link-id">#{{ row.link_id }}</span>
            <el-tag size="small" effect="plain" disable-transitions>{{ platformText(row) }}</el-tag>
          </div>
          <a v-if="row.link?.url" :href="row.link.url" target="_blank" rel="noopener noreferrer nofollow" class="link-url" :title="row.link.url">{{ shortUrl(row.link.url, 60) }}</a>
        </template>
      </el-table-column>
      <el-table-column :label="t('monitoring.checkType')" width="90">
        <template #default="{ row }"><StatusTag kind="check_type" :value="row.check_type" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('monitoring.resultStatus')" width="110">
        <template #default="{ row }"><StatusTag kind="link_check_result" :value="row.result_status" /></template>
      </el-table-column>
      <el-table-column :label="t('evidence.transition')" min-width="190">
        <template #default="{ row }">
          <span class="transition">
            <StatusTag kind="link_alive_status" :value="row.previous_status" effect="plain" />
            <span class="arrow">→</span>
            <StatusTag kind="link_alive_status" :value="row.applied_status" />
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('monitoring.http')" width="70" align="center">
        <template #default="{ row }">{{ row.http_status ?? "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('evidence.matchedRule')" min-width="130">
        <template #default="{ row }"><StatusTag v-if="row.matched_rule" kind="link_check_rule" :value="row.matched_rule" /><span v-else>-</span></template>
      </el-table-column>
      <el-table-column :label="t('monitoring.redirects')" width="70" align="center">
        <template #default="{ row }">{{ row.redirect_count ?? 0 }}</template>
      </el-table-column>
      <el-table-column :label="t('evidence.hamming')" width="80" align="center">
        <template #default="{ row }">{{ row.hamming_distance ?? "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('evidence.duration')" width="80">
        <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
      </el-table-column>
      <el-table-column :label="t('evidence.triggeredBy')" width="80">
        <template #default="{ row }">{{ triggeredBy(row) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="70" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="openEvidence(row)">{{ t("evidence.evidence") }}</el-button>
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

    <EvidenceDrawer v-model="evidenceVisible" kind="link" :record="evidenceRecord" :loading="evidenceLoading" />
    <MonitoringRunDialog v-model="runVisible" kind="link" :platform-id="filters.platform_id ?? null" @done="onRunDone" />
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.range-picker {
  width: 340px;
  max-width: 100%;
}
.clickable-table :deep(.el-table__row) {
  cursor: pointer;
}
.link-cell {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.link-id {
  font-weight: 500;
  color: var(--el-color-primary);
  text-decoration: none;
}
.link-url {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  text-decoration: none;
}
.link-url:hover {
  text-decoration: underline;
}
.transition {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.arrow {
  color: var(--el-text-color-secondary);
}
</style>
