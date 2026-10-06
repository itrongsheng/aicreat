<script setup lang="ts">
// 回填链接列表（docs/11 §11.2；docs/04 §6.17；docs/13 §12.3）：
// - 筛选：顶栏项目（0 = 全部可见项目）、content_id、平台、存活状态、SEO 已收录、GEO 已引用、监控中、关键词（标题 / URL）、发布时间区间；分页；
// - 列：标题快照 / 内容（跳转编辑器）、平台、链接（domain + 缩略 normalized_url，复制，新窗口打开原始 url）、发布账号 / 时间、存活状态（hover
//   最近检测 / HTTP / 下次检测）、SEO / GEO 按引擎徽标、监控开关（pause / resume 二次确认）、操作（详情 / 立即检测 / 收录检测 / 人工标记 /
//   重建基线 / 编辑 / 删除）；
// - 工具栏：回填、批量回填（LinkBackfillDialog，逐条结果表）、导出 CSV；存在 pending 链接时每 3s 刷新直到无 pending；
// - 路由参数 content_id 预置筛选，backfill=1 时按该内容打开回填弹窗（内容编辑器兼容入口）。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ArrowDown, CopyDocument, Download, Plus, Refresh, Search, Upload } from "@element-plus/icons-vue";
import { DEFAULT_PAGE_SIZE, LINK_ALIVE_STATUS, type LinkAliveStatus, type PublishLink } from "@aicreat/shared";
import * as contentsApi from "@/api/contents";
import * as linksApi from "@/api/links";
import LinkBackfillDialog from "@/components/LinkBackfillDialog.vue";
import LinkEditDialog from "@/components/LinkEditDialog.vue";
import LinkIndexBadges from "@/components/LinkIndexBadges.vue";
import LinkIndexCheckDialog from "@/components/LinkIndexCheckDialog.vue";
import LinkMarkIndexDialog from "@/components/LinkMarkIndexDialog.vue";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect, { type ToolbarOption } from "@/components/ToolbarSelect.vue";
import { copyText } from "@/composables/useAssetActions";
import { useIndexEngines } from "@/composables/useIndexEngines";
import { useLinkActions } from "@/composables/useLinkActions";
import { usePermission } from "@/composables/usePermission";
import { usePlatforms } from "@/composables/usePlatforms";
import { usePolling } from "@/composables/usePolling";
import { useProject } from "@/composables/useProject";
import { datedFilename, downloadBlob } from "@/utils/download";
import { formatDateTime, toUtcIso } from "@/utils/format";
import { isIndexOverdue, platformLabel, shortUrl } from "@/utils/links";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { has } = usePermission();
const { projectId, store: projectStore } = useProject();
const { platforms, byId: platformById, load: loadPlatforms } = usePlatforms();
const engineCatalog = useIndexEngines();
const actions = useLinkActions();

const canCreate = computed(() => has("publish.links.create"));
const canUpdate = computed(() => has("publish.links.update"));
const canCheck = computed(() => has("publish.links.check"));
const canMark = computed(() => has("publish.links.mark"));
const canDelete = computed(() => has("publish.links.delete"));
const canViewContent = computed(() => has("content.contents.view"));

// ---------- 筛选与列表 ----------
type BoolFilter = "1" | "0" | undefined;

const filters = reactive({
  content_id: undefined as number | undefined,
  platform_id: undefined as number | undefined,
  alive_status: undefined as LinkAliveStatus | undefined,
  seo_indexed_any: undefined as BoolFilter,
  geo_cited_any: undefined as BoolFilter,
  is_monitoring: undefined as BoolFilter,
  keyword: "",
  published: null as [Date, Date] | null,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<PublishLink[]>([]);
const loading = ref(false);
let loadSeq = 0;

const boolOptions = computed<ToolbarOption[]>(() => [
  { value: "1", label: t("common.yes") },
  { value: "0", label: t("common.no") },
]);
const platformOptions = computed<ToolbarOption[]>(() => platforms.value.map((p) => ({ value: p.id, label: platformLabel(p) })));

function boolParam(v: BoolFilter): boolean | undefined {
  return v === undefined ? undefined : v === "1";
}

function queryParams() {
  const [start, end] = filters.published ?? [null, null];
  return {
    project_id: projectId.value || undefined,
    content_id: filters.content_id || undefined,
    platform_id: filters.platform_id || undefined,
    alive_status: filters.alive_status,
    seo_indexed_any: boolParam(filters.seo_indexed_any),
    geo_cited_any: boolParam(filters.geo_cited_any),
    is_monitoring: boolParam(filters.is_monitoring),
    keyword: filters.keyword.trim() || undefined,
    published_start: start ? toUtcIso(new Date(start.getFullYear(), start.getMonth(), start.getDate())) : undefined,
    published_end: end ? toUtcIso(new Date(end.getFullYear(), end.getMonth(), end.getDate(), 23, 59, 59)) : undefined,
  };
}

async function load(silent = false) {
  const seq = ++loadSeq;
  if (!silent) loading.value = true;
  try {
    const res = await linksApi.list({ ...queryParams(), page: page.value, page_size: pageSize.value }, { silent });
    if (seq !== loadSeq) return;
    rows.value = res.items;
    total.value = res.total;
  } catch (err) {
    if (seq !== loadSeq) return;
    if (!silent) {
      rows.value = [];
      total.value = 0;
    }
    if (silent) throw err;
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.content_id = undefined;
  filters.platform_id = undefined;
  filters.alive_status = undefined;
  filters.seo_indexed_any = undefined;
  filters.geo_cited_any = undefined;
  filters.is_monitoring = undefined;
  filters.keyword = "";
  filters.published = null;
  if (route.query.content_id) void router.replace({ query: { ...route.query, content_id: undefined } });
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

watch(projectId, () => search());

// pending 链接存在时每 3s 刷新（页面不可见暂停），直到无 pending
const polling = usePolling(() => load(true), { interval: 3000, immediate: false });
const hasPending = computed(() => rows.value.some((r) => r.alive_status === "pending"));
watch(hasPending, (pending) => (pending ? polling.start() : polling.stop()));

// ---------- 行展示 ----------
function platformOf(row: PublishLink) {
  return platformById.value.get(row.platform_id) ?? row.platform ?? null;
}

function platformName(row: PublishLink): string {
  return platformLabel(platformOf(row), row.platform_id);
}

function platformIcon(row: PublishLink): string | null {
  const icon = platformById.value.get(row.platform_id)?.icon ?? null;
  return icon && /^https?:\/\//i.test(icon) ? icon : null;
}

function engineLabel(kind: "seo" | "geo", code: string): string {
  return engineCatalog.label(kind, code);
}

function gotoDetail(row: PublishLink) {
  void router.push(`/links/${row.id}`);
}

// ---------- 动作 ----------
async function toggleMonitoring(row: PublishLink, on: boolean) {
  const updated = await actions.setMonitoring(row, on);
  if (updated) Object.assign(row, updated);
}

async function runCheck(row: PublishLink) {
  await actions.check(row);
}

async function runRebaseline(row: PublishLink) {
  if (await actions.rebaseline(row)) void load(true).catch(() => undefined);
}

async function removeRow(row: PublishLink) {
  if (await actions.remove(row)) {
    if (rows.value.length === 1 && page.value > 1) page.value -= 1;
    void load();
  }
}

const indexCheckVisible = ref(false);
const markVisible = ref(false);
const editVisible = ref(false);
const activeRow = ref<PublishLink | null>(null);

function openIndexCheck(row: PublishLink) {
  activeRow.value = row;
  indexCheckVisible.value = true;
}

function openMark(row: PublishLink) {
  activeRow.value = row;
  markVisible.value = true;
}

function openEdit(row: PublishLink) {
  activeRow.value = row;
  editVisible.value = true;
}

function onRowUpdated(updated: PublishLink) {
  const row = rows.value.find((r) => r.id === updated.id);
  if (row) Object.assign(row, updated);
}

function onCommand(row: PublishLink, command: string) {
  if (command === "indexCheck") openIndexCheck(row);
  else if (command === "mark") openMark(row);
  else if (command === "rebaseline") void runRebaseline(row);
  else if (command === "edit") openEdit(row);
  else if (command === "delete") void removeRow(row);
}

const hasMoreActions = computed(() => canMark.value || canCheck.value || canUpdate.value || canDelete.value);

// ---------- 回填 ----------
const backfillVisible = ref(false);
const backfillMode = ref<"single" | "batch">("single");
const backfillContent = ref<{ id: number; title: string } | null>(null);

function openBackfill(mode: "single" | "batch") {
  backfillContent.value = null;
  backfillMode.value = mode;
  backfillVisible.value = true;
}

function onBackfilled() {
  page.value = 1;
  void load();
}

// ---------- 导出 ----------
const exporting = ref(false);

async function exportCsv() {
  exporting.value = true;
  try {
    const { blob, filename } = await linksApi.exportLinks(queryParams());
    downloadBlob(blob, filename || datedFilename("links", "csv"));
  } catch {
    /* 已提示 */
  } finally {
    exporting.value = false;
  }
}

// ---------- 进入页面 ----------
function qInt(key: string): number | undefined {
  const v = route.query[key];
  const n = Number(Array.isArray(v) ? v[0] : v);
  return Number.isInteger(n) && n > 0 ? n : undefined;
}

function qEnum<T extends string>(key: string, values: readonly T[]): T | undefined {
  const v = route.query[key];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === "string" && (values as readonly string[]).includes(s) ? (s as T) : undefined;
}

async function openBackfillFromQuery(contentId: number) {
  let title = "";
  if (canViewContent.value) {
    try {
      title = (await contentsApi.get(contentId, { silent: true })).title;
    } catch {
      title = "";
    }
  }
  backfillContent.value = { id: contentId, title };
  backfillMode.value = "single";
  backfillVisible.value = true;
}

onMounted(() => {
  filters.content_id = qInt("content_id");
  filters.platform_id = qInt("platform_id");
  filters.alive_status = qEnum("alive_status", LINK_ALIVE_STATUS);
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void loadPlatforms();
  void engineCatalog.load();
  void load();
  const backfillContentId = qInt("content_id");
  if (route.query.backfill === "1" && backfillContentId && canCreate.value) {
    void router.replace({ query: { ...route.query, backfill: undefined } });
    void openBackfillFromQuery(backfillContentId);
  }
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.links") }}</h2>
        <div class="header-actions">
          <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openBackfill('single')">{{ t("links.backfillBtn") }}</el-button>
          <el-button v-if="canCreate" :icon="Upload" @click="openBackfill('batch')">{{ t("links.batchBackfillBtn") }}</el-button>
          <el-button :icon="Download" :loading="exporting" @click="exportCsv">{{ t("links.exportCsv") }}</el-button>
        </div>
      </div>
    </template>

    <div class="toolbar">
      <el-input v-model="filters.keyword" :placeholder="t('links.keywordPlaceholder')" clearable style="width: 220px" @keyup.enter="search" @clear="search" />
      <el-input-number v-model="filters.content_id" :min="1" :controls="false" :placeholder="t('links.contentId')" style="width: 110px" @change="search" />
      <ToolbarSelect v-model="filters.platform_id" :options="platformOptions" :placeholder="t('links.platform')" filterable width="140px" @change="search" />
      <ToolbarSelect v-model="filters.alive_status" enum-name="link_alive_status" :placeholder="t('links.aliveStatus')" width="130px" @change="search" />
      <ToolbarSelect v-model="filters.seo_indexed_any" :options="boolOptions" :placeholder="t('links.filterSeoIndexed')" width="120px" @change="search" />
      <ToolbarSelect v-model="filters.geo_cited_any" :options="boolOptions" :placeholder="t('links.filterGeoCited')" width="120px" @change="search" />
      <ToolbarSelect v-model="filters.is_monitoring" :options="boolOptions" :placeholder="t('links.filterMonitoring')" width="110px" @change="search" />
      <el-date-picker
        v-model="filters.published"
        type="daterange"
        :start-placeholder="t('links.publishedStart')"
        :end-placeholder="t('links.publishedEnd')"
        style="width: 250px"
        @change="search"
      />
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <el-button :icon="Refresh" @click="load()">{{ t("common.refresh") }}</el-button>
    </div>

    <el-alert v-if="hasPending" type="info" :title="t('links.pendingPolling')" :closable="false" show-icon class="pending-alert" />

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column :label="t('links.titleContent')" min-width="200">
        <template #default="{ row }">
          <router-link v-if="canViewContent" :to="`/contents/${row.content_id}`" class="title-link">{{ row.title_snapshot || `#${row.content_id}` }}</router-link>
          <span v-else class="title-text">{{ row.title_snapshot || `#${row.content_id}` }}</span>
          <div class="sub-line">
            <span class="text-secondary">{{ t("links.contentNo", { id: row.content_id }) }}</span>
            <el-tag v-if="isIndexOverdue(row, engineCatalog.overdueDays.value)" type="warning" size="small" effect="plain" disable-transitions>
              {{ t("links.overdue") }}
            </el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('links.platform')" width="100">
        <template #default="{ row }">
          <span class="platform-cell">
            <img v-if="platformIcon(row)" :src="platformIcon(row)!" alt="" class="platform-icon" referrerpolicy="no-referrer" />
            <span>{{ platformName(row) }}</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('links.link')" min-width="210">
        <template #default="{ row }">
          <div class="link-cell">
            <span class="domain">{{ row.domain }}</span>
            <a :href="row.url" target="_blank" rel="noopener noreferrer" class="link-url" :title="row.url">{{ shortUrl(row.normalized_url, 56) }}</a>
            <el-button link size="small" :icon="CopyDocument" :title="t('common.copy')" @click="copyText(row.url)" />
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('links.accountPublished')" width="145">
        <template #default="{ row }">
          <div class="ellipsis">{{ row.publish_account || "-" }}</div>
          <div class="text-secondary small">{{ formatDateTime(row.published_at, false) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('links.aliveStatus')" width="100">
        <template #default="{ row }">
          <el-tooltip placement="top" :show-after="200">
            <template #content>
              <div>{{ t("links.lastCheckedAt") }}：{{ formatDateTime(row.last_checked_at) }}</div>
              <div>{{ t("links.lastHttpStatus") }}：{{ row.last_http_status ?? "-" }}</div>
              <div>{{ t("links.nextCheckAt") }}：{{ formatDateTime(row.next_check_at) }}</div>
            </template>
            <span><StatusTag kind="link_alive_status" :value="row.alive_status" /></span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('links.seoIndex')" min-width="130">
        <template #default="{ row }"><LinkIndexBadges :link="row" kind="seo" :labeler="engineLabel" /></template>
      </el-table-column>
      <el-table-column :label="t('links.geoCite')" min-width="130">
        <template #default="{ row }"><LinkIndexBadges :link="row" kind="geo" :labeler="engineLabel" /></template>
      </el-table-column>
      <el-table-column :label="t('links.monitoring')" width="70" align="center">
        <template #default="{ row }">
          <el-switch
            :model-value="row.is_monitoring"
            :disabled="!canUpdate"
            :loading="actions.acting.value === row.id"
            size="small"
            @change="(v: string | number | boolean) => toggleMonitoring(row, !!v)"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="190" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="gotoDetail(row)">{{ t("links.detail") }}</el-button>
          <el-button v-if="canCheck" link type="primary" :loading="actions.acting.value === row.id" @click="runCheck(row)">{{ t("links.actions.check") }}</el-button>
          <el-dropdown v-if="hasMoreActions" trigger="click" @command="(cmd: string) => onCommand(row, cmd)">
            <el-button link type="primary">{{ t("common.more") }}<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="canCheck" command="indexCheck" :disabled="row.alive_status === 'deleted' || !row.is_monitoring">
                  {{ t("links.actions.indexCheck") }}
                </el-dropdown-item>
                <el-dropdown-item v-if="canMark" command="mark">{{ t("links.actions.mark") }}</el-dropdown-item>
                <el-dropdown-item v-if="canCheck" command="rebaseline">{{ t("links.actions.rebaseline") }}</el-dropdown-item>
                <el-dropdown-item v-if="canUpdate" command="edit">{{ t("common.edit") }}</el-dropdown-item>
                <el-dropdown-item v-if="canDelete" command="delete" divided class="danger-item">{{ t("common.delete") }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
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

    <LinkBackfillDialog
      v-model="backfillVisible"
      :initial-mode="backfillMode"
      :content-id="backfillContent?.id ?? null"
      :content-title="backfillContent?.title ?? null"
      :project-id="projectId || null"
      @created="onBackfilled"
    />
    <LinkIndexCheckDialog v-model="indexCheckVisible" :link="activeRow" />
    <LinkMarkIndexDialog v-model="markVisible" :link="activeRow" @saved="onRowUpdated" />
    <LinkEditDialog v-model="editVisible" :link="activeRow" @saved="onRowUpdated" />
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.header-actions > * {
  margin-left: 0 !important;
}
.pending-alert {
  margin-bottom: 10px;
}
.title-link,
.title-text {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  font-weight: 500;
  line-height: 1.4;
}
.title-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
.sub-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  font-size: 12px;
}
.platform-cell {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.platform-icon {
  width: 16px;
  height: 16px;
  border-radius: 3px;
  object-fit: contain;
}
.link-cell {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 6px;
  min-width: 0;
}
.domain {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.link-url {
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--el-color-primary);
  font-size: 13px;
  text-decoration: none;
}
.link-url:hover {
  text-decoration: underline;
}
.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.small {
  font-size: 12px;
}
:deep(.danger-item) {
  color: var(--el-color-danger);
}
</style>
