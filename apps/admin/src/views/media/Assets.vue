<script setup lang="ts">
// 素材库（docs/10 §6.1~§6.4、§7.3；docs/04 §6.13；docs/13 §4.2）：
// - 筛选：项目（默认顶栏当前项目，可选「全部项目」查看独立素材）、内容 ID、kind / status / usage_type / source、创建人（我创建的）；
//   网格 / 列表切换（本机记忆）；分页 20 / 50 / 100；路由参数 id（打开详情）/ kind / status / usage_type / source / content_id / project_id / created_by 预置；
// - 进行中（pending / submitted / generating / downloading）的素材经 useAssetTracker(5s) 轮询并就地更新；
// - 详情抽屉四个 Tab：基本信息（url 复制、file_hash）、参数（params、reference_asset_ids 可点击跳转）、
//   任务（/task 摘要；有 ai.tasks.view 时另取 GET /admin/ai/tasks/{id} 的轮询记录 response_meta.poll、下载记录 response_meta.download 与尝试行）、
//   引用（references：cover_of / bound_content_id / referenced_by_asset_ids / count）；
// - 操作：重试（failed / expired；transfer_failed 走重新提交，确认框建议优先「转存」）、转存、取消、删除（确认框显示引用计数，409 in_use / current_status）、
//   绑定到内容（选择内容 + 用途，视频只有 inline）。首版无批量删除。上游临时 URL 不展示。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Grid, Refresh, Search, Tickets, VideoCamera, Picture } from "@element-plus/icons-vue";
import {
  DEFAULT_PAGE_SIZE,
  MEDIA_KIND,
  MEDIA_SOURCE,
  MEDIA_STATUS,
  MEDIA_USAGE_TYPE,
  type AiTask,
  type MediaAsset,
  type MediaKind,
  type MediaSource,
  type MediaStatus,
  type MediaUsageType,
} from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import * as mediaApi from "@/api/media";
import { isApiError } from "@/api/client";
import AssetAttachDialog from "@/components/AssetAttachDialog.vue";
import AssetCard from "@/components/AssetCard.vue";
import ProjectSelect from "@/components/ProjectSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import TaskProgress from "@/components/TaskProgress.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import {
  canCancelAsset,
  canDeleteAsset,
  canRetryAsset,
  canTransferAsset,
  cancelAssetFlow,
  copyText,
  deleteAssetFlow,
  retryAssetFlow,
  retryChecksUpstream,
  transferAssetFlow,
} from "@/composables/useAssetActions";
import { isMediaActive, useAssetTracker } from "@/composables/useAssetTracker";
import { usePermission } from "@/composables/usePermission";
import { useProject } from "@/composables/useProject";
import { useAuthStore } from "@/store/auth";
import { formatBytes, formatDateTime, formatDuration } from "@/utils/format";

const VIEW_KEY = "aicreat.media.view";

const { t } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const { projectId: currentProjectId } = useProject();

// ---------- 路由参数 ----------
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

// ---------- 筛选与列表 ----------
const filters = reactive({
  project_id: (q("project_id") === "0" ? 0 : (qInt("project_id") ?? currentProjectId.value)) as number,
  content_id: qInt("content_id") as number | undefined,
  kind: qEnum<MediaKind>("kind", MEDIA_KIND) as MediaKind | undefined,
  status: qEnum<MediaStatus>("status", MEDIA_STATUS) as MediaStatus | undefined,
  usage_type: qEnum<MediaUsageType>("usage_type", MEDIA_USAGE_TYPE) as MediaUsageType | undefined,
  source: qEnum<MediaSource>("source", MEDIA_SOURCE) as MediaSource | undefined,
  created_by: qInt("created_by") as number | undefined,
  mine: false,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<MediaAsset[]>([]);
const loading = ref(false);
let seq = 0;

function readView(): "grid" | "list" {
  try {
    return localStorage.getItem(VIEW_KEY) === "list" ? "list" : "grid";
  } catch {
    return "grid";
  }
}
const view = ref<"grid" | "list">(readView());
watch(view, (v) => {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    /* ignore */
  }
});

const createdBy = computed(() => (filters.mine ? auth.admin?.id : filters.created_by));

const tracker = useAssetTracker({
  interval: 5000,
  onUpdate: (asset) => mergeAsset(asset),
});

function mergeAsset(asset: MediaAsset) {
  const idx = rows.value.findIndex((r) => r.id === asset.id);
  if (idx >= 0) rows.value[idx] = { ...rows.value[idx], ...asset };
  if (detail.value?.id === asset.id) detail.value = { ...detail.value, ...asset };
}

function trackActive() {
  tracker.clear();
  for (const r of rows.value) if (isMediaActive(r.status)) tracker.add(r);
  if (detail.value && isMediaActive(detail.value.status)) tracker.add(detail.value);
}

async function load(silent = false) {
  const my = ++seq;
  if (!silent) loading.value = true;
  try {
    const res = await mediaApi.listAssets({
      page: page.value,
      page_size: pageSize.value,
      project_id: filters.project_id || undefined,
      content_id: filters.content_id,
      kind: filters.kind,
      status: filters.status,
      usage_type: filters.usage_type,
      source: filters.source,
      created_by: createdBy.value,
    });
    if (my !== seq) return;
    rows.value = res.items;
    total.value = res.total;
    trackActive();
  } catch {
    if (my !== seq) return;
    rows.value = [];
    total.value = 0;
  } finally {
    if (my === seq) loading.value = false;
  }
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.project_id = currentProjectId.value;
  filters.content_id = undefined;
  filters.kind = undefined;
  filters.status = undefined;
  filters.usage_type = undefined;
  filters.source = undefined;
  filters.created_by = undefined;
  filters.mine = false;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

// 顶栏切换项目时跟随（「全部项目」= 0 时同样跟随）
watch(currentProjectId, (id) => {
  filters.project_id = id;
  search();
});

// ---------- 行操作 ----------
const acting = ref<number | null>(null);

async function afterChange(id: number) {
  try {
    const fresh = await mediaApi.getAsset(id, { silent: true });
    mergeAsset(fresh);
    if (isMediaActive(fresh.status)) tracker.add(fresh);
  } catch (err) {
    if (isApiError(err) && err.code === 404) void load(true);
  }
}

async function doRetry(a: MediaAsset) {
  acting.value = a.id;
  try {
    const res = await retryAssetFlow(a);
    if (res) await afterChange(a.id);
  } finally {
    acting.value = null;
  }
}

async function doTransfer(a: MediaAsset) {
  acting.value = a.id;
  try {
    if (await transferAssetFlow(a)) await afterChange(a.id);
  } finally {
    acting.value = null;
  }
}

async function doCancel(a: MediaAsset) {
  acting.value = a.id;
  try {
    if (await cancelAssetFlow(a)) await afterChange(a.id);
  } finally {
    acting.value = null;
  }
}

async function doDelete(a: MediaAsset) {
  acting.value = a.id;
  try {
    if (await deleteAssetFlow(detail.value?.id === a.id ? detail.value : a)) {
      if (detail.value?.id === a.id) drawerVisible.value = false;
      tracker.remove(a.id);
      await load(true);
    }
  } finally {
    acting.value = null;
  }
}

function onCardChanged(id: number) {
  void afterChange(id);
}

async function onCardDeleted(id: number) {
  if (detail.value?.id === id) drawerVisible.value = false;
  tracker.remove(id);
  await load(true);
}

// 绑定到内容
const attachVisible = ref(false);
const attachTarget = ref<MediaAsset | null>(null);
function openAttach(a: MediaAsset) {
  attachTarget.value = a;
  attachVisible.value = true;
}
function onAttached(asset: MediaAsset) {
  void afterChange(asset.id);
}

// ---------- 详情抽屉 ----------
const drawerVisible = ref(false);
const detail = ref<MediaAsset | null>(null);
const detailLoading = ref(false);
const detailTab = ref<"basic" | "params" | "task" | "references">("basic");
const taskDetail = ref<AiTask | null>(null);
const taskLoading = ref(false);
const taskError = ref("");

async function openDetail(id: number) {
  drawerVisible.value = true;
  detailTab.value = "basic";
  taskDetail.value = null;
  taskError.value = "";
  detailLoading.value = true;
  try {
    detail.value = await mediaApi.getAsset(id, { silent: true });
    if (isMediaActive(detail.value.status)) tracker.add(detail.value);
    if (q("id") !== String(id)) void router.replace({ query: { ...route.query, id: String(id) } });
  } catch (err) {
    detail.value = null;
    drawerVisible.value = false;
    if (isApiError(err) && err.code === 404) ElMessage.error(t("media.errors.notFound"));
  } finally {
    detailLoading.value = false;
  }
}

watch(drawerVisible, (v) => {
  if (!v && q("id")) {
    const { id: _id, ...rest } = route.query;
    void router.replace({ query: rest });
  }
});

async function loadTaskDetail(force = false) {
  const a = detail.value;
  if (!a?.ai_task_id || !has("ai.tasks.view")) return;
  if (taskDetail.value && taskDetail.value.id === a.ai_task_id && !force) return;
  taskLoading.value = true;
  taskError.value = "";
  try {
    taskDetail.value = await aiApi.getTask(a.ai_task_id, { silent: true });
  } catch (err) {
    taskDetail.value = null;
    taskError.value = isApiError(err) ? err.message : t("common.requestFailed");
  } finally {
    taskLoading.value = false;
  }
}

watch(detailTab, (tab) => {
  if (tab === "task") void loadTaskDetail();
});
watch(
  () => detail.value?.ai_task_id,
  () => {
    if (detailTab.value === "task") void loadTaskDetail(true);
  },
);

const poll = computed(() => taskDetail.value?.response_meta?.poll ?? null);
const download = computed(() => taskDetail.value?.response_meta?.download ?? null);
const paramEntries = computed(() => Object.entries(detail.value?.params ?? {}));

function paramValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "-";
  if (Array.isArray(v)) return v.length ? v.join("\n") : "-";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function tasksLink(a: MediaAsset) {
  return { path: "/ai/tasks", query: { project_id: "0", target_type: "media_asset", target_id: String(a.id) } };
}

onMounted(async () => {
  await load();
  const id = qInt("id");
  if (id) await openDetail(id);
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.mediaAssets") }}</h2>
        <div class="header-actions">
          <router-link v-if="has('media.images.view')" to="/media/images"><el-button type="primary" :icon="Picture">{{ t("menu.mediaImages") }}</el-button></router-link>
          <router-link v-if="has('media.videos.view')" to="/media/videos"><el-button :icon="VideoCamera">{{ t("menu.mediaVideos") }}</el-button></router-link>
        </div>
      </div>
    </template>

    <div class="toolbar">
      <ProjectSelect v-model="filters.project_id" allow-all width="180px" @change="search" />
      <el-input-number v-model="filters.content_id" :min="1" :controls="false" :placeholder="t('media.fields.contentId')" style="width: 100px" @change="search" />
      <ToolbarSelect v-model="filters.kind" enum-name="media_kind" :placeholder="t('media.fields.kind')" width="100px" @change="search" />
      <ToolbarSelect v-model="filters.status" enum-name="media_status" :placeholder="t('common.status')" width="110px" @change="search" />
      <ToolbarSelect v-model="filters.usage_type" enum-name="media_usage_type" :placeholder="t('media.fields.usage')" width="120px" @change="search" />
      <ToolbarSelect v-model="filters.source" enum-name="media_source" :placeholder="t('media.fields.source')" width="110px" @change="search" />
      <el-checkbox v-model="filters.mine" @change="search">{{ t("media.assets.mine") }}</el-checkbox>
      <el-tag v-if="filters.created_by && !filters.mine" closable @close="(filters.created_by = undefined), search()">{{ t("media.assets.createdBy", { id: filters.created_by }) }}</el-tag>
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <el-radio-group v-model="view" size="small">
        <el-radio-button value="grid"><el-icon><Grid /></el-icon></el-radio-button>
        <el-radio-button value="list"><el-icon><Tickets /></el-icon></el-radio-button>
      </el-radio-group>
      <el-button :icon="Refresh" @click="load()">{{ t("common.refresh") }}</el-button>
    </div>
    <div v-if="!filters.project_id" class="text-secondary scope-tip">{{ t("media.assets.allProjectsTip") }}</div>

    <div v-if="view === 'grid'" v-loading="loading" class="asset-grid">
      <AssetCard
        v-for="row in rows"
        :key="row.id"
        :asset="row"
        :task="tracker.get(row.id)?.taskKnown ? tracker.get(row.id)?.task : undefined"
        compact
        show-detail
        @detail="(a: MediaAsset) => openDetail(a.id)"
        @changed="onCardChanged"
        @deleted="onCardDeleted"
        @attach="openAttach"
      />
      <el-empty v-if="!loading && !rows.length" :description="t('media.assets.empty')" class="asset-grid__empty" />
    </div>

    <el-table v-else v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column :label="t('media.fields.preview')" width="96">
        <template #default="{ row }">
          <el-image
            v-if="row.kind === 'image' && row.status === 'ready' && (row.thumbnail_url || row.url)"
            :src="row.thumbnail_url || row.url"
            fit="cover"
            lazy
            class="row-thumb"
            :preview-src-list="row.url ? [row.url] : []"
            preview-teleported
          />
          <video v-else-if="row.kind === 'video' && row.status === 'ready' && row.url" :src="row.url" preload="metadata" muted class="row-thumb" />
          <div v-else class="row-thumb row-thumb--empty"><el-icon><component :is="row.kind === 'video' ? VideoCamera : Picture" /></el-icon></div>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.id')" width="80">
        <template #default="{ row }"><el-button link type="primary" @click="openDetail(row.id)">#{{ row.id }}</el-button></template>
      </el-table-column>
      <el-table-column :label="t('media.fields.kind')" width="70">
        <template #default="{ row }"><StatusTag kind="media_kind" :value="row.kind" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('media.fields.usage')" width="96">
        <template #default="{ row }"><StatusTag kind="media_usage_type" :value="row.usage_type" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('media.fields.source')" width="80">
        <template #default="{ row }"><StatusTag kind="media_source" :value="row.source" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('common.status')" min-width="170">
        <template #default="{ row }">
          <StatusTag kind="media_status" :value="row.status" />
          <StatusTag v-if="row.error_category" kind="error_category" :value="row.error_category" effect="plain" class="ml4" />
          <div v-if="isMediaActive(row.status)" class="row-task">
            <TaskProgress v-if="tracker.get(row.id)?.task" :task="tracker.get(row.id)!.task" compact :show-hint="false" />
            <el-progress v-else :percentage="row.progress" :indeterminate="!row.progress" :stroke-width="4" :show-text="false" />
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('media.fields.spec')" min-width="120">
        <template #default="{ row }">
          <div v-if="row.width && row.height">{{ row.width }}×{{ row.height }}</div>
          <div class="text-secondary">{{ row.size_bytes ? formatBytes(row.size_bytes) : "-" }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('media.fields.model')" min-width="120" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.model || "-" }}</span></template>
      </el-table-column>
      <el-table-column :label="t('media.fields.content')" width="90">
        <template #default="{ row }">
          <router-link v-if="row.content_id" :to="`/contents/${row.content_id}`">#{{ row.content_id }}</router-link>
          <span v-else class="text-secondary">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.createdAt')" width="160">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="230" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row.id)">{{ t("media.actions.detail") }}</el-button>
          <el-button v-if="has('content.contents.update') && row.status === 'ready'" link type="primary" @click="openAttach(row)">{{ t("media.actions.attach") }}</el-button>
          <el-button v-if="has('media.assets.retry') && canTransferAsset(row)" link type="primary" :loading="acting === row.id" @click="doTransfer(row)">{{ t("media.actions.transfer") }}</el-button>
          <el-button v-if="has('media.assets.retry') && canRetryAsset(row)" link type="warning" :loading="acting === row.id" @click="doRetry(row)">
            {{ retryChecksUpstream(row) ? t("media.actions.retryRecheck") : t("media.actions.retry") }}
          </el-button>
          <el-button v-if="has('ai.tasks.cancel') && canCancelAsset(row)" link type="warning" :loading="acting === row.id" @click="doCancel(row)">{{ t("media.actions.cancel") }}</el-button>
          <el-button v-if="has('media.assets.delete') && canDeleteAsset(row)" link type="danger" :loading="acting === row.id" @click="doDelete(row)">{{ t("common.delete") }}</el-button>
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

    <!-- 详情抽屉 -->
    <el-drawer v-model="drawerVisible" :title="detail ? t('media.detail.title', { id: detail.id }) : ''" size="640px" append-to-body>
      <div v-loading="detailLoading" class="detail">
        <template v-if="detail">
          <div class="detail__head">
            <StatusTag kind="media_status" :value="detail.status" size="default" />
            <StatusTag kind="media_kind" :value="detail.kind" effect="plain" />
            <StatusTag kind="media_usage_type" :value="detail.usage_type" effect="plain" />
            <StatusTag kind="media_source" :value="detail.source" effect="plain" />
            <span class="spacer" />
            <el-button v-if="has('content.contents.update') && detail.status === 'ready'" size="small" @click="openAttach(detail)">{{ t("media.actions.attach") }}</el-button>
            <el-button v-if="has('media.assets.retry') && canTransferAsset(detail)" size="small" type="primary" plain :loading="acting === detail.id" @click="doTransfer(detail)">
              {{ t("media.actions.transfer") }}
            </el-button>
            <el-button v-if="has('media.assets.retry') && canRetryAsset(detail)" size="small" type="warning" plain :loading="acting === detail.id" @click="doRetry(detail)">
              {{ retryChecksUpstream(detail) ? t("media.actions.retryRecheck") : t("media.actions.retry") }}
            </el-button>
            <el-button v-if="has('ai.tasks.cancel') && canCancelAsset(detail)" size="small" type="warning" plain :loading="acting === detail.id" @click="doCancel(detail)">
              {{ t("media.actions.cancel") }}
            </el-button>
            <el-button v-if="has('media.assets.delete') && canDeleteAsset(detail)" size="small" type="danger" plain :loading="acting === detail.id" @click="doDelete(detail)">
              {{ t("common.delete") }}
            </el-button>
          </div>

          <div class="detail__preview">
            <el-image
              v-if="detail.kind === 'image' && detail.status === 'ready' && detail.url"
              :src="detail.url"
              fit="contain"
              :preview-src-list="[detail.url]"
              preview-teleported
              class="detail__media"
            />
            <video v-else-if="detail.kind === 'video' && detail.status === 'ready' && detail.url" :src="detail.url" controls preload="metadata" class="detail__media" />
            <div v-else class="detail__media detail__media--empty">
              <el-icon :size="32"><component :is="detail.kind === 'video' ? VideoCamera : Picture" /></el-icon>
              <span v-if="isMediaActive(detail.status)">{{ detail.progress }}%</span>
            </div>
          </div>
          <TaskProgress v-if="isMediaActive(detail.status) && detail.task" :task="detail.task" :show-hint="false" class="detail__progress" />

          <el-tabs v-model="detailTab">
            <!-- 基本信息 -->
            <el-tab-pane name="basic" :label="t('media.detail.tabs.basic')">
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item :label="t('media.fields.url')" :span="2">
                  <template v-if="detail.url">
                    <a :href="detail.url" target="_blank" rel="noopener noreferrer" class="mono break">{{ detail.url }}</a>
                    <el-button link type="primary" size="small" @click="copyText(detail.url)">{{ t("common.copy") }}</el-button>
                  </template>
                  <span v-else class="text-secondary">{{ t("media.detail.noUrl") }}</span>
                </el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.storageKey')" :span="2"><span class="mono break">{{ detail.storage_key || "-" }}</span></el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.fileHash')" :span="2">
                  <template v-if="detail.file_hash">
                    <span class="mono break">{{ detail.file_hash }}</span>
                    <el-button link type="primary" size="small" @click="copyText(detail.file_hash)">{{ t("common.copy") }}</el-button>
                  </template>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.project')">
                  <router-link v-if="detail.project_id && has('content.projects.view')" :to="`/projects/${detail.project_id}`">#{{ detail.project_id }}</router-link>
                  <span v-else-if="detail.project_id">#{{ detail.project_id }}</span>
                  <span v-else class="text-secondary">{{ t("media.detail.noProject") }}</span>
                </el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.content')">
                  <router-link v-if="detail.content_id" :to="`/contents/${detail.content_id}`">#{{ detail.content_id }}</router-link>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.mime')">{{ detail.mime_type || "-" }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.size')">{{ detail.size_bytes ? formatBytes(detail.size_bytes) : "-" }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.dimensions')">{{ detail.width && detail.height ? `${detail.width}×${detail.height}` : "-" }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.durationSeconds')">{{ detail.duration_seconds ? t("media.card.seconds", { n: detail.duration_seconds }) : "-" }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.sort')">{{ detail.sort }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.transferAttempts')">{{ detail.transfer_attempts }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.readyAt')">{{ formatDateTime(detail.ready_at) }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.failedAt')">{{ formatDateTime(detail.failed_at) }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.nextTransferAt')">{{ formatDateTime(detail.next_transfer_at) }}</el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.createdBy')">{{ detail.created_by ? `#${detail.created_by}` : "-" }}</el-descriptions-item>
                <el-descriptions-item :label="t('common.createdAt')">{{ formatDateTime(detail.created_at) }}</el-descriptions-item>
                <el-descriptions-item :label="t('common.updatedAt')">{{ formatDateTime(detail.updated_at) }}</el-descriptions-item>
                <el-descriptions-item v-if="detail.error_category || detail.error_message" :label="t('media.fields.error')" :span="2">
                  <StatusTag v-if="detail.error_category" kind="error_category" :value="detail.error_category" />
                  <div class="errmsg">{{ detail.error_message || "" }}</div>
                </el-descriptions-item>
              </el-descriptions>
            </el-tab-pane>

            <!-- 参数 -->
            <el-tab-pane name="params" :label="t('media.detail.tabs.params')">
              <el-descriptions :column="1" border size="small">
                <el-descriptions-item :label="t('media.fields.prompt')">
                  <div class="pre">{{ detail.prompt || "-" }}</div>
                  <el-button v-if="detail.prompt" link type="primary" size="small" @click="copyText(detail.prompt)">{{ t("common.copy") }}</el-button>
                </el-descriptions-item>
                <el-descriptions-item v-if="detail.negative_prompt" :label="t('media.fields.negativePrompt')"><div class="pre">{{ detail.negative_prompt }}</div></el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.model')"><span class="mono">{{ detail.model || "-" }}</span></el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.upstreamTaskId')"><span class="mono">{{ detail.upstream_task_id || "-" }}</span></el-descriptions-item>
                <el-descriptions-item v-for="[k, v] in paramEntries" :key="k" :label="k">
                  <div class="pre mono">{{ paramValue(v) }}</div>
                </el-descriptions-item>
                <el-descriptions-item :label="t('media.fields.referenceAssets')">
                  <template v-if="detail.reference_asset_ids?.length">
                    <el-button v-for="rid in detail.reference_asset_ids" :key="rid" link type="primary" @click="openDetail(rid)">#{{ rid }}</el-button>
                  </template>
                  <span v-else>-</span>
                </el-descriptions-item>
              </el-descriptions>
            </el-tab-pane>

            <!-- 任务 -->
            <el-tab-pane name="task" :label="t('media.detail.tabs.task')">
              <template v-if="detail.task">
                <TaskProgress :task="detail.task" />
                <el-descriptions :column="2" border size="small" class="section">
                  <el-descriptions-item :label="t('media.detail.taskId')">#{{ detail.task.task_id }}</el-descriptions-item>
                  <el-descriptions-item :label="t('aiTasks.operation')">{{ t(`status.ai_task_operation.${detail.task.operation}`) }}</el-descriptions-item>
                  <el-descriptions-item :label="t('taskProgress.modelOverride')"><span class="mono">{{ detail.task.model_override || "-" }}</span></el-descriptions-item>
                  <el-descriptions-item :label="t('aiTasks.fields.finishedAt')">{{ formatDateTime(detail.task.finished_at) }}</el-descriptions-item>
                </el-descriptions>
              </template>
              <el-empty v-else :description="t('media.detail.noTask')" :image-size="70" />

              <template v-if="detail.ai_task_id && has('ai.tasks.view')">
                <div v-loading="taskLoading" class="section">
                  <div class="section-head">
                    <h4 class="section-title">{{ t("media.detail.rootTask") }}</h4>
                    <el-button link type="primary" size="small" @click="loadTaskDetail(true)">{{ t("common.refresh") }}</el-button>
                    <router-link :to="tasksLink(detail)"><el-button link type="primary" size="small">{{ t("media.detail.viewInTasks") }}</el-button></router-link>
                  </div>
                  <el-alert v-if="taskError" type="error" :title="taskError" :closable="false" />
                  <template v-if="taskDetail">
                    <el-descriptions :column="2" border size="small">
                      <el-descriptions-item :label="t('aiTasks.requestId')"><span class="mono">{{ taskDetail.request_id || "-" }}</span></el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.fields.upstreamTaskId')"><span class="mono">{{ taskDetail.upstream_task_id || "-" }}</span></el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.fields.parentTaskId')">{{ taskDetail.parent_task_id ? `#${taskDetail.parent_task_id}` : "-" }}</el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.triggerType')"><StatusTag kind="ai_task_trigger_type" :value="taskDetail.trigger_type" effect="plain" /></el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.fields.pollCount')">{{ taskDetail.poll_count }}</el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.fields.nextPollAt')">{{ formatDateTime(taskDetail.next_poll_at) }}</el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.fields.deadlineAt')">{{ formatDateTime(taskDetail.deadline_at) }}</el-descriptions-item>
                      <el-descriptions-item :label="t('aiTasks.duration')">{{ formatDuration(taskDetail.duration_ms) }}</el-descriptions-item>
                      <el-descriptions-item v-if="taskDetail.error_category || taskDetail.error_message" :label="t('aiTasks.errorCategory')" :span="2">
                        <StatusTag kind="error_category" :value="taskDetail.error_category" />
                        <div class="errmsg">{{ taskDetail.error_message || "" }}</div>
                      </el-descriptions-item>
                    </el-descriptions>

                    <template v-if="poll">
                      <h4 class="section-title">{{ t("aiTasks.sections.poll") }}</h4>
                      <el-descriptions :column="2" border size="small">
                        <el-descriptions-item :label="t('aiTasks.fields.lastRequestId')"><span class="mono">{{ poll.last_request_id || "-" }}</span></el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.fields.lastHttpStatus')">{{ poll.last_http_status ?? "-" }}</el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.fields.errorCode')"><span class="mono">{{ poll.error_code || "-" }}</span></el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.fields.consecutive404')">{{ poll.consecutive_404 ?? 0 }}</el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.fields.errorMessage')" :span="2">{{ poll.error_message || "-" }}</el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.fields.requestIds')" :span="2">
                          <div class="mono break">{{ poll.request_ids?.length ? poll.request_ids.join(", ") : "-" }}</div>
                        </el-descriptions-item>
                      </el-descriptions>
                    </template>

                    <template v-if="download">
                      <h4 class="section-title">{{ t("aiTasks.sections.download") }}</h4>
                      <el-descriptions :column="2" border size="small">
                        <el-descriptions-item :label="t('aiTasks.fields.source')">{{ t(`aiTasks.downloadSource.${download.source}`) }} ({{ download.source }})</el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.httpStatus')">{{ download.http_status ?? "-" }}</el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.requestId')" :span="2"><span class="mono">{{ download.request_id || "-" }}</span></el-descriptions-item>
                        <el-descriptions-item :label="t('aiTasks.fields.requestIds')" :span="2">
                          <div class="mono break">{{ download.request_ids?.length ? download.request_ids.join(", ") : "-" }}</div>
                        </el-descriptions-item>
                      </el-descriptions>
                    </template>

                    <h4 class="section-title">{{ t("aiTasks.attempts") }}（{{ taskDetail.attempts?.length ?? 0 }}）</h4>
                    <el-table :data="taskDetail.attempts ?? []" size="small" border :empty-text="t('aiTasks.noAttempts')">
                      <el-table-column :label="t('common.id')" width="80" prop="id" />
                      <el-table-column :label="t('aiTasks.model')" min-width="120" show-overflow-tooltip>
                        <template #default="{ row: at }"><span class="mono">{{ at.model || "-" }}</span></template>
                      </el-table-column>
                      <el-table-column :label="t('aiTasks.protocol')" width="100" prop="protocol" />
                      <el-table-column :label="t('aiTasks.status')" width="90">
                        <template #default="{ row: at }"><StatusTag kind="ai_task_status" :value="at.status" /></template>
                      </el-table-column>
                      <el-table-column :label="t('aiTasks.errorCategory')" width="120">
                        <template #default="{ row: at }"><StatusTag kind="error_category" :value="at.error_category" effect="plain" /></template>
                      </el-table-column>
                      <el-table-column :label="t('aiTasks.httpStatus')" width="64" prop="http_status" />
                      <el-table-column :label="t('aiTasks.requestId')" min-width="140" show-overflow-tooltip>
                        <template #default="{ row: at }"><span class="mono">{{ at.request_id || "-" }}</span></template>
                      </el-table-column>
                    </el-table>
                  </template>
                </div>
              </template>
              <div v-else-if="detail.ai_task_id" class="text-secondary section">{{ t("media.detail.noTaskPermission") }}</div>
            </el-tab-pane>

            <!-- 引用 -->
            <el-tab-pane name="references" :label="t('media.detail.tabs.references', { n: detail.references?.count ?? 0 })">
              <template v-if="detail.references">
                <el-descriptions :column="1" border size="small">
                  <el-descriptions-item :label="t('media.references.coverOfLabel')">
                    <router-link v-if="detail.references.cover_of" :to="`/contents/${detail.references.cover_of}`">#{{ detail.references.cover_of }}</router-link>
                    <span v-else>-</span>
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('media.references.boundLabel')">
                    <router-link v-if="detail.references.bound_content_id" :to="`/contents/${detail.references.bound_content_id}`">#{{ detail.references.bound_content_id }}</router-link>
                    <span v-else>-</span>
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('media.references.referencedByLabel')">
                    <template v-if="detail.references.referenced_by_asset_ids.length">
                      <span v-for="r in detail.references.referenced_by_asset_ids" :key="r.id" class="ref-item">
                        <el-button link type="primary" @click="openDetail(r.id)">#{{ r.id }}</el-button>
                        <StatusTag kind="media_status" :value="r.status" />
                      </span>
                    </template>
                    <span v-else>-</span>
                  </el-descriptions-item>
                  <el-descriptions-item :label="t('media.references.count')">{{ detail.references.count }}</el-descriptions-item>
                </el-descriptions>
                <p class="text-secondary ref-tip">{{ t("media.references.tip") }}</p>
              </template>
              <el-empty v-else :image-size="70" />
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </el-drawer>

    <AssetAttachDialog v-model="attachVisible" :asset="attachTarget" @attached="onAttached" />
  </el-card>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.header-actions {
  display: flex;
  gap: 8px;
}
.scope-tip {
  margin: -4px 0 10px;
  font-size: 12px;
}
.asset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
  min-height: 120px;
}
.asset-grid__empty {
  grid-column: 1 / -1;
}
.row-thumb {
  width: 72px;
  height: 48px;
  border-radius: 4px;
  object-fit: cover;
  display: block;
}
.row-thumb--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
}
.row-task {
  margin-top: 4px;
  max-width: 260px;
}
.ml4 {
  margin-left: 4px;
}
.detail {
  min-height: 200px;
}
.detail__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
}
.detail__head .spacer {
  flex: 1;
}
.detail__head .el-button + .el-button {
  margin-left: 0;
}
.detail__preview {
  margin-bottom: 12px;
  border-radius: 6px;
  overflow: hidden;
  background: var(--el-fill-color-light);
}
.detail__media {
  width: 100%;
  max-height: 320px;
  display: block;
}
.detail__media--empty {
  height: 160px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
}
.detail__progress {
  margin-bottom: 12px;
}
.section {
  margin-top: 12px;
}
.section-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-title {
  margin: 14px 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.section-head .section-title {
  margin-right: auto;
}
.break {
  word-break: break-all;
}
.pre {
  white-space: pre-wrap;
  word-break: break-word;
}
.errmsg {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
  margin-top: 4px;
}
.ref-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-right: 12px;
}
.ref-tip {
  font-size: 12px;
  line-height: 1.6;
}
</style>
