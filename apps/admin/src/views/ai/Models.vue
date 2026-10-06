<script setup lang="ts">
// 模型目录（docs/08 §11.5、docs/04 §6.15 / §7.15）：筛选 modality / vendor_id / is_available / keyword / include_hidden；
// 顶部「同步目录」（ai.models.sync）显示上次 synced_at 与返回的 request_ids；行点击抽屉展示详情与 raw_pricing（JsonEditor 只读）。
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Refresh, Search } from "@element-plus/icons-vue";
import { DEFAULT_PAGE_SIZE, MODALITY, type AiModel, type Modality, type ModelSyncResult } from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import { isApiError } from "@/api/client";
import JsonEditor from "@/components/JsonEditor.vue";
import { invalidateModelOptions } from "@/components/ModelSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import { formatDateTime, formatNumber } from "@/utils/format";

const { t } = useI18n();

// ---------- 列表 ----------
const filters = reactive<{ keyword: string; modality: Modality | undefined; vendor_id: number | undefined; is_available: boolean | undefined; include_hidden: boolean }>({
  keyword: "",
  modality: undefined,
  vendor_id: undefined,
  is_available: undefined,
  include_hidden: false,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<AiModel[]>([]);
const loading = ref(false);

/** 供应商选项：从已加载的行累积（无独立供应商接口） */
const vendors = reactive(new Map<number, string>());
const vendorOptions = computed(() => [...vendors.entries()].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name)));

/** 已加载行中最近的 synced_at（同步后以返回值为准） */
const lastSyncedAt = ref<string | null>(null);

async function load() {
  loading.value = true;
  try {
    const res = await aiApi.listModels({
      page: page.value,
      page_size: pageSize.value,
      keyword: filters.keyword.trim() || undefined,
      modality: filters.modality,
      vendor_id: filters.vendor_id,
      is_available: filters.is_available,
      include_hidden: filters.include_hidden || undefined,
    });
    rows.value = res.items;
    total.value = res.total;
    for (const m of res.items) {
      if (m.vendor_id !== null && m.vendor_id !== undefined) vendors.set(m.vendor_id, m.vendor_name || `#${m.vendor_id}`);
      if (m.synced_at && (!lastSyncedAt.value || m.synced_at > lastSyncedAt.value)) lastSyncedAt.value = m.synced_at;
    }
  } catch {
    rows.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.keyword = "";
  filters.modality = undefined;
  filters.vendor_id = undefined;
  filters.is_available = undefined;
  filters.include_hidden = false;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

onMounted(load);

// ---------- 同步 ----------
const syncing = ref(false);
const syncResult = ref<ModelSyncResult | null>(null);

async function sync() {
  syncing.value = true;
  try {
    const res = await aiApi.syncModels({ silent: true });
    syncResult.value = res;
    lastSyncedAt.value = res.synced_at;
    invalidateModelOptions();
    ElMessage.success(t("aiModels.syncDone", { total: res.total, added: res.added, updated: res.updated, unavailable: res.unavailable }));
    await load();
  } catch (err) {
    if (isApiError(err) && err.code === 409) ElMessage.warning(t("aiModels.syncBusy"));
    else if (isApiError(err)) {
      const data = err.data as { error_category?: string; request_id?: string } | null;
      const extra = data?.request_id ? `（${t("aiModels.requestIds")} ${data.request_id}）` : "";
      ElMessage.error(`${err.message}${extra}`);
    }
  } finally {
    syncing.value = false;
  }
}

// ---------- 详情抽屉 ----------
const detailVisible = ref(false);
const detailLoading = ref(false);
const detail = ref<AiModel | null>(null);

async function openDetail(row: AiModel) {
  detail.value = row;
  detailVisible.value = true;
  detailLoading.value = true;
  try {
    detail.value = await aiApi.getModel(row.id);
  } catch {
    /* 拦截器已提示；保留列表行数据 */
  } finally {
    detailLoading.value = false;
  }
}

function ratio(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return String(Number(value));
}

function quotaTypeLabel(value: number | null | undefined): string {
  return value === 1 ? t("aiModels.quotaType1") : value === 0 ? t("aiModels.quotaType0") : "-";
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("aiModels.title") }}</h2>
        <div class="sync-box">
          <div class="sync-meta">
            <span class="text-secondary">{{ t("aiModels.lastSynced") }}：</span>
            <span>{{ lastSyncedAt ? formatDateTime(lastSyncedAt) : t("aiModels.neverSynced") }}</span>
            <template v-if="syncResult">
              <span class="text-secondary sync-rid">{{ t("aiModels.requestIds") }}：</span>
              <span class="mono">{{ t("aiModels.requestIdModels") }} {{ syncResult.request_ids.models || "-" }}</span>
              <span class="mono">· {{ t("aiModels.requestIdPricing") }} {{ syncResult.request_ids.pricing || "-" }}</span>
            </template>
          </div>
          <el-button v-permission="'ai.models.sync'" type="primary" :icon="Refresh" :loading="syncing" @click="sync">{{ t("aiModels.sync") }}</el-button>
        </div>
      </div>
    </template>

    <el-alert
      v-if="syncResult"
      type="success"
      :closable="true"
      show-icon
      class="sync-alert"
      :title="t('aiModels.syncDone', { total: syncResult.total, added: syncResult.added, updated: syncResult.updated, unavailable: syncResult.unavailable })"
      @close="syncResult = null"
    />

    <div class="toolbar">
      <el-input v-model="filters.keyword" :placeholder="t('aiModels.keywordPlaceholder')" clearable style="width: 220px" @keyup.enter="search" />
      <el-select v-model="filters.modality" :placeholder="t('aiModels.modality')" clearable style="width: 130px" @change="search">
        <el-option v-for="m in MODALITY" :key="m" :label="t(`status.modality.${m}`)" :value="m" />
      </el-select>
      <el-select v-model="filters.vendor_id" :placeholder="t('aiModels.vendor')" clearable filterable style="width: 160px" @change="search">
        <el-option v-for="v in vendorOptions" :key="v.id" :label="v.name" :value="v.id" />
      </el-select>
      <el-select v-model="filters.is_available" :placeholder="t('aiModels.availability')" clearable style="width: 130px" @change="search">
        <el-option :label="t('aiModels.available')" :value="true" />
        <el-option :label="t('aiModels.unavailable')" :value="false" />
      </el-select>
      <el-tooltip :content="t('aiModels.includeHiddenTip')" placement="top">
        <el-checkbox v-model="filters.include_hidden" @change="search">{{ t("aiModels.includeHidden") }}</el-checkbox>
      </el-tooltip>
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button :icon="Refresh" @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <span class="text-secondary hint">{{ t("aiModels.rowClickTip") }}</span>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe class="clickable-table" @row-click="openDetail">
      <el-table-column :label="t('aiModels.modelId')" min-width="200" fixed="left">
        <template #default="{ row }"><span class="mono">{{ row.model_id }}</span></template>
      </el-table-column>
      <el-table-column :label="t('aiModels.vendor')" min-width="110">
        <template #default="{ row }">{{ row.vendor_name || "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('aiModels.modalities')" min-width="130">
        <template #default="{ row }">
          <span class="tag-list">
            <StatusTag v-for="m in row.modalities" :key="m" kind="modality" :value="m" effect="plain" />
            <span v-if="!row.modalities?.length" class="text-secondary">-</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiModels.endpoints')" min-width="220">
        <template #default="{ row }">
          <span class="tag-list">
            <el-tag v-for="e in row.supported_endpoint_types" :key="e" size="small" type="info" effect="plain" class="mono">{{ e }}</el-tag>
            <span v-if="!row.supported_endpoint_types?.length" class="text-secondary">-</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiModels.quotaType')" width="90">
        <template #default="{ row }">{{ quotaTypeLabel(row.quota_type) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiModels.modelRatio')" width="100" align="right">
        <template #default="{ row }">{{ ratio(row.model_ratio) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiModels.completionRatio')" width="100" align="right">
        <template #default="{ row }">{{ ratio(row.completion_ratio) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiModels.modelPrice')" width="100" align="right">
        <template #default="{ row }">{{ ratio(row.model_price) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiModels.isAvailable')" width="90">
        <template #default="{ row }">
          <el-tag :type="row.is_available ? 'success' : 'danger'" size="small">{{ row.is_available ? t("aiModels.available") : t("aiModels.unavailable") }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiModels.health')" width="90">
        <template #default="{ row }"><StatusTag kind="health_status" :value="row.last_health_status" /></template>
      </el-table-column>
      <el-table-column :label="t('aiModels.lastSeen')" width="165">
        <template #default="{ row }">{{ formatDateTime(row.last_seen_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('aiModels.syncedAt')" width="165">
        <template #default="{ row }">{{ formatDateTime(row.synced_at) }}</template>
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
        @current-change="load"
        @size-change="onPageSizeChange"
      />
    </div>

    <el-drawer v-model="detailVisible" :title="detail ? `${t('aiModels.detail')} - ${detail.model_id}` : t('aiModels.detail')" size="min(640px, 100%)" destroy-on-close>
      <div v-if="detail" v-loading="detailLoading" class="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item :label="t('aiModels.modelId')" :span="2"><span class="mono">{{ detail.model_id }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.vendor')">{{ detail.vendor_name || "-" }}<span v-if="detail.vendor_id !== null" class="text-secondary"> (#{{ detail.vendor_id }})</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.ownedBy')">{{ detail.owned_by || "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.description')" :span="2">{{ detail.description || "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.modalities')">
            <span class="tag-list">
              <StatusTag v-for="m in detail.modalities" :key="m" kind="modality" :value="m" effect="plain" />
              <span v-if="!detail.modalities?.length">-</span>
            </span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.tags')">{{ detail.tags?.length ? detail.tags.join(", ") : "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.endpoints')" :span="2">
            <span class="tag-list">
              <el-tag v-for="e in detail.supported_endpoint_types" :key="e" size="small" type="info" effect="plain" class="mono">{{ e }}</el-tag>
              <span v-if="!detail.supported_endpoint_types?.length">-</span>
            </span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.quotaType')">{{ quotaTypeLabel(detail.quota_type) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.modelPrice')">{{ ratio(detail.model_price) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.modelRatio')">{{ ratio(detail.model_ratio) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.completionRatio')">{{ ratio(detail.completion_ratio) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.cacheRatio')">{{ ratio(detail.cache_ratio) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.createCacheRatio')">{{ ratio(detail.create_cache_ratio) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.billingMode')">{{ detail.billing_mode || "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.modelPriceType')">{{ detail.model_price_type || "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.billingExpr')" :span="2"><span class="mono">{{ detail.billing_expr || "-" }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.enableGroups')">{{ detail.enable_groups?.length ? detail.enable_groups.join(", ") : "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.sortOrder')">{{ detail.sort_order }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.isAvailable')">
            <el-tag :type="detail.is_available ? 'success' : 'danger'" size="small">{{ detail.is_available ? t("aiModels.available") : t("aiModels.unavailable") }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.health')"><StatusTag kind="health_status" :value="detail.last_health_status" /></el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.healthLatency')">{{ detail.last_health_latency_ms !== null ? `${formatNumber(detail.last_health_latency_ms)} ms` : "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.lastHealthAt')">{{ formatDateTime(detail.last_health_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.lastSeen')">{{ formatDateTime(detail.last_seen_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('aiModels.syncedAt')">{{ formatDateTime(detail.synced_at) }}</el-descriptions-item>
        </el-descriptions>

        <h4 class="section-title">{{ t("aiModels.rawPricing") }}</h4>
        <JsonEditor v-if="detail.raw_pricing" :model-value="detail.raw_pricing" readonly :rows="16" />
        <el-empty v-else :description="t('aiModels.noRawPricing')" :image-size="60" />
      </div>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.sync-box {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.sync-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  font-size: 13px;
}
.sync-rid {
  margin-left: 8px;
}
.sync-alert {
  margin-bottom: 12px;
}
.hint {
  font-size: 12px;
}
.tag-list {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
}
.clickable-table :deep(.el-table__row) {
  cursor: pointer;
}
.section-title {
  margin: 16px 0 8px;
  font-size: 14px;
}
</style>
