<script setup lang="ts">
// 收录检测记录页（docs/11 §11.6；docs/04 §6.18、§7.12）：
// - 顶部概览（MonitoringOverview focus=index，需 monitoring.link_checks.view，无该权限时不渲染）；
// - 表格：时间、link_id（列表接口不附链接信息，点击跳转链接详情）、kind、engine、provider、result_status、match_mode、confidence、model、
//   request_id、error_category、耗时；行点击打开 EvidenceDrawer（GET /index-checks/{id} 取完整 evidence）；
// - 筛选：kind / engine / provider / result_status / 顶栏项目 / 平台 / 链接 ID / 时间区间；路由 query 可预置这些筛选；
// - 工具栏「批量触发」：MonitoringRunDialog（monitoring.index_checks.run，kinds / engines），用户视角下请求自动附加 owner_id。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { CopyDocument, Refresh, Search, VideoPlay } from "@element-plus/icons-vue";
import {
  DEFAULT_PAGE_SIZE,
  GEO_CITE_STATUS,
  GEO_PROVIDER,
  INDEX_KIND,
  SEO_INDEX_STATUS,
  SEO_PROVIDER,
  type IndexCheck,
  type IndexKind,
} from "@aicreat/shared";
import * as monitoringApi from "@/api/monitoring";
import EvidenceDrawer from "@/components/EvidenceDrawer.vue";
import MonitoringOverview from "@/components/MonitoringOverview.vue";
import MonitoringRunDialog from "@/components/MonitoringRunDialog.vue";
import StatusTag, { statusLabel } from "@/components/StatusTag.vue";
import ToolbarSelect, { type ToolbarOption } from "@/components/ToolbarSelect.vue";
import { copyText } from "@/composables/useAssetActions";
import { useIndexEngines } from "@/composables/useIndexEngines";
import { usePermission } from "@/composables/usePermission";
import { usePlatforms } from "@/composables/usePlatforms";
import { useProject } from "@/composables/useProject";
import { formatDateTime, formatDuration, formatNumber, toUtcIso } from "@/utils/format";
import { platformLabel } from "@/utils/links";

const { t } = useI18n();
const route = useRoute();
const { has } = usePermission();
const { projectId, store: projectStore } = useProject();
const { platforms, load: loadPlatforms } = usePlatforms();
const engineCatalog = useIndexEngines();

const canRun = computed(() => has("monitoring.index_checks.run"));
const canViewLinks = computed(() => has("publish.links.view"));

// ---------- 筛选与列表 ----------
const filters = reactive({
  kind: undefined as IndexKind | undefined,
  engine: undefined as string | undefined,
  provider: undefined as string | undefined,
  result_status: undefined as string | undefined,
  platform_id: undefined as number | undefined,
  link_id: undefined as number | undefined,
  range: null as [Date, Date] | null,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<IndexCheck[]>([]);
const loading = ref(false);
let loadSeq = 0;

const kindOptions = computed<ToolbarOption[]>(() => INDEX_KIND.map((k) => ({ value: k, label: statusLabel("index_kind", k) })));
const engineOptions = computed<ToolbarOption[]>(() =>
  engineCatalog.engines.value
    .filter((e) => !filters.kind || e.kind === filters.kind)
    .map((e) => ({ value: e.code, label: `${engineCatalog.label(e.kind, e.code)}${filters.kind ? "" : `（${e.kind.toUpperCase()}）`}` })),
);
const providerOptions = computed<ToolbarOption[]>(() => {
  const seo = SEO_PROVIDER.map((p) => ({ value: p, label: statusLabel("seo_provider", p) }));
  const geo = GEO_PROVIDER.filter((p) => p !== "manual").map((p) => ({ value: p, label: statusLabel("geo_provider", p) }));
  if (filters.kind === "seo") return seo;
  if (filters.kind === "geo") return GEO_PROVIDER.map((p) => ({ value: p, label: statusLabel("geo_provider", p) }));
  return [...seo, ...geo];
});
const resultOptions = computed<ToolbarOption[]>(() => {
  const seo = SEO_INDEX_STATUS.map((s) => ({ value: s, label: statusLabel("seo_index_status", s) }));
  const geo = GEO_CITE_STATUS.map((s) => ({ value: s, label: statusLabel("geo_cite_status", s) }));
  if (filters.kind === "seo") return seo;
  if (filters.kind === "geo") return geo;
  return [...seo, ...geo.filter((g) => g.value !== "unknown")];
});
const platformOptions = computed<ToolbarOption[]>(() => platforms.value.map((p) => ({ value: p.id, label: platformLabel(p) })));

/** kind 变化后清除不再适用的 engine / provider / result_status */
function onKindChange() {
  if (filters.engine && !engineOptions.value.some((o) => o.value === filters.engine)) filters.engine = undefined;
  if (filters.provider && !providerOptions.value.some((o) => o.value === filters.provider)) filters.provider = undefined;
  if (filters.result_status && !resultOptions.value.some((o) => o.value === filters.result_status)) filters.result_status = undefined;
  search();
}

function queryParams() {
  const [start, end] = filters.range ?? [null, null];
  return {
    kind: filters.kind,
    engine: filters.engine,
    provider: filters.provider,
    result_status: filters.result_status,
    project_id: projectId.value || undefined,
    platform_id: filters.platform_id || undefined,
    link_id: filters.link_id || undefined,
    start: start ? toUtcIso(start) : undefined,
    end: end ? toUtcIso(end) : undefined,
  };
}

async function load() {
  const seq = ++loadSeq;
  loading.value = true;
  try {
    const res = await monitoringApi.listIndexChecks({ ...queryParams(), page: page.value, page_size: pageSize.value });
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
  filters.kind = undefined;
  filters.engine = undefined;
  filters.provider = undefined;
  filters.result_status = undefined;
  filters.platform_id = undefined;
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
function resultEnum(row: IndexCheck) {
  return row.kind === "geo" ? "geo_cite_status" : "seo_index_status";
}

function providerEnum(row: IndexCheck) {
  return row.kind === "geo" ? "geo_provider" : "seo_provider";
}

// ---------- 证据抽屉 ----------
const evidenceVisible = ref(false);
const evidenceRecord = ref<IndexCheck | null>(null);
const evidenceLoading = ref(false);
let evidenceSeq = 0;

async function openEvidence(row: IndexCheck) {
  const seq = ++evidenceSeq;
  evidenceRecord.value = row;
  evidenceVisible.value = true;
  evidenceLoading.value = true;
  try {
    const detail = await monitoringApi.getIndexCheck(row.id, { silent: true });
    if (seq === evidenceSeq) evidenceRecord.value = detail;
  } catch {
    /* 列表行已含 evidence，详情失败时沿用列表数据 */
  } finally {
    if (seq === evidenceSeq) evidenceLoading.value = false;
  }
}

function onRowClick(row: IndexCheck, _column: unknown, event: MouseEvent) {
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

onMounted(() => {
  const kind = q("kind");
  filters.kind = kind && (INDEX_KIND as readonly string[]).includes(kind) ? (kind as IndexKind) : undefined;
  filters.engine = q("engine");
  filters.provider = q("provider");
  filters.result_status = q("result_status");
  filters.platform_id = qInt("platform_id");
  filters.link_id = qInt("link_id");
  void loadPlatforms();
  void engineCatalog.load();
  void load();
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.indexChecks") }}</h2>
        <div class="header-actions">
          <el-button v-if="canRun" type="primary" :icon="VideoPlay" @click="runVisible = true">{{ t("monitoring.run.button") }}</el-button>
        </div>
      </div>
    </template>

    <MonitoringOverview ref="overviewRef" focus="index" />

    <div class="toolbar">
      <ToolbarSelect v-model="filters.kind" :options="kindOptions" :placeholder="t('monitoring.kind')" width="100px" @change="onKindChange" />
      <ToolbarSelect v-model="filters.engine" :options="engineOptions" :placeholder="t('monitoring.engine')" filterable width="150px" @change="search" />
      <ToolbarSelect v-model="filters.provider" :options="providerOptions" :placeholder="t('evidence.provider')" width="150px" @change="search" />
      <ToolbarSelect v-model="filters.result_status" :options="resultOptions" :placeholder="t('monitoring.resultStatus')" width="120px" @change="search" />
      <ToolbarSelect v-model="filters.platform_id" :options="platformOptions" :placeholder="t('links.platform')" filterable width="130px" @change="search" />
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
        <template #default="{ row }">
          <div>{{ formatDateTime(row.checked_at) }}</div>
          <StatusTag kind="check_type" :value="row.check_type" effect="plain" />
        </template>
      </el-table-column>
      <el-table-column :label="t('monitoring.linkId')" width="90">
        <template #default="{ row }">
          <router-link v-if="canViewLinks" :to="`/links/${row.link_id}`" class="link-id">#{{ row.link_id }}</router-link>
          <span v-else>#{{ row.link_id }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('evidence.kindEngine')" min-width="140">
        <template #default="{ row }">
          <StatusTag kind="index_kind" :value="row.kind" effect="plain" />
          <span class="engine">{{ engineCatalog.label(row.kind, row.engine) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('evidence.provider')" min-width="130">
        <template #default="{ row }"><StatusTag :kind="providerEnum(row)" :value="row.provider" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('monitoring.resultStatus')" width="100">
        <template #default="{ row }"><StatusTag :kind="resultEnum(row)" :value="row.result_status" /></template>
      </el-table-column>
      <el-table-column :label="t('evidence.matchMode')" width="100">
        <template #default="{ row }"><StatusTag kind="index_match_mode" :value="row.match_mode" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('evidence.confidence')" width="80" align="center">
        <template #default="{ row }">{{ row.confidence == null ? "-" : formatNumber(row.confidence, 2) }}</template>
      </el-table-column>
      <el-table-column :label="t('evidence.model')" min-width="130">
        <template #default="{ row }"><span class="mono ellipsis" :title="row.model || ''">{{ row.model || "-" }}</span></template>
      </el-table-column>
      <el-table-column :label="t('evidence.requestId')" min-width="140">
        <template #default="{ row }">
          <span v-if="row.request_id" class="request-id">
            <span class="mono ellipsis" :title="row.request_id">{{ row.request_id }}</span>
            <el-button link size="small" :icon="CopyDocument" :title="t('common.copy')" @click.stop="copyText(row.request_id)" />
          </span>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('evidence.errorCategory')" min-width="120">
        <template #default="{ row }">
          <StatusTag v-if="row.error_category" kind="error_category" :value="row.error_category" />
          <div v-if="row.error_message" class="error-msg" :title="row.error_message">{{ row.error_message }}</div>
          <span v-if="!row.error_category && !row.error_message">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('evidence.duration')" width="80">
        <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
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

    <EvidenceDrawer v-model="evidenceVisible" kind="index" :record="evidenceRecord" :loading="evidenceLoading" :show-link="false" />
    <MonitoringRunDialog v-model="runVisible" kind="index" :platform-id="filters.platform_id ?? null" @done="onRunDone" />
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
.link-id {
  font-weight: 500;
  color: var(--el-color-primary);
  text-decoration: none;
}
.engine {
  margin-left: 6px;
}
.ellipsis {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
.request-id {
  display: inline-flex;
  align-items: center;
  max-width: 100%;
  min-width: 0;
}
.request-id .ellipsis {
  max-width: calc(100% - 24px);
}
.error-msg {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
