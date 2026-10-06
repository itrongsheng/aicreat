<script setup lang="ts">
// 操作日志（docs/07 §8.5）：只读列表；数据范围 own 的用户只看到本人的记录（后端过滤）
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { CopyDocument, Refresh, Search } from "@element-plus/icons-vue";
import { DEFAULT_PAGE_SIZE, OPERATION_ACTION, type AdminItem, type AdminOperationLogItem, type OperationAction } from "@aicreat/shared";
import * as adminsApi from "@/api/admins";
import StatusTag, { statusLabel } from "@/components/StatusTag.vue";
import { usePermission } from "@/composables/usePermission";
import { formatDateTime, toUtcIso } from "@/utils/format";

const { t, locale } = useI18n();
const { has } = usePermission();

/** MODULE_NAMES 九个模块 + auth（07 §6.3、§8.5） */
const MODULES = ["dashboard", "content", "media", "ai", "publish", "monitoring", "stats", "system", "security", "auth"] as const;

const canPickAdmin = computed(() => has("security.admins.view"));

const filters = reactive<{
  admin_id: number | undefined;
  module: string | undefined;
  action: OperationAction | undefined;
  target_type: string;
  range: [Date, Date] | null;
}>({ admin_id: undefined, module: undefined, action: undefined, target_type: "", range: null });

const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<AdminOperationLogItem[]>([]);
const loading = ref(false);

const moduleOptions = computed(() => {
  void locale.value;
  return MODULES.map((m) => ({ value: m, label: t(`logs.modules.${m}`) }));
});
const actionOptions = computed(() => {
  void locale.value;
  return OPERATION_ACTION.map((a) => ({ value: a, label: statusLabel("operation_action", a) }));
});

function moduleOf(code: string): string {
  const prefix = (code || "").split(".")[0];
  return (MODULES as readonly string[]).includes(prefix) ? t(`logs.modules.${prefix}`) : prefix || "-";
}

// ---------- 用户下拉（有 security.admins.view 时远程搜索） ----------
const adminOptions = ref<AdminItem[]>([]);
const adminSearching = ref(false);

async function searchAdmins(keyword = "") {
  if (!canPickAdmin.value) return;
  adminSearching.value = true;
  try {
    const res = await adminsApi.listAdmins({ keyword: keyword.trim() || undefined, page: 1, page_size: 50 });
    adminOptions.value = res.items;
  } catch {
    adminOptions.value = [];
  } finally {
    adminSearching.value = false;
  }
}

async function load() {
  loading.value = true;
  try {
    const res = await adminsApi.listOperationLogs({
      page: page.value,
      page_size: pageSize.value,
      admin_id: filters.admin_id || undefined,
      module: filters.module,
      action: filters.action,
      target_type: filters.target_type.trim() || undefined,
      start: filters.range ? toUtcIso(filters.range[0]) : undefined,
      end: filters.range ? toUtcIso(filters.range[1]) : undefined,
    });
    rows.value = res.items;
    total.value = res.total;
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
  filters.admin_id = undefined;
  filters.module = undefined;
  filters.action = undefined;
  filters.target_type = "";
  filters.range = null;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

async function copy(text: string | null) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    ElMessage.success(t("common.copied"));
  } catch {
    ElMessage.warning(t("common.copyFailed"));
  }
}

onMounted(() => {
  void searchAdmins();
  void load();
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <h2 class="page-title">{{ t("logs.title") }}</h2>
    </template>

    <div class="toolbar">
      <el-select
        v-if="canPickAdmin"
        v-model="filters.admin_id"
        filterable
        remote
        clearable
        :remote-method="searchAdmins"
        :loading="adminSearching"
        :placeholder="t('logs.adminPlaceholder')"
        style="width: 200px"
      >
        <el-option v-for="a in adminOptions" :key="a.id" :label="a.display_name ? `${a.display_name}（${a.username}）` : a.username" :value="a.id" />
      </el-select>
      <el-input-number v-else v-model="filters.admin_id" :min="1" :controls="false" :placeholder="t('logs.adminIdPlaceholder')" style="width: 140px" />
      <el-select v-model="filters.module" clearable :placeholder="t('logs.module')" style="width: 140px">
        <el-option v-for="m in moduleOptions" :key="m.value" :label="m.label" :value="m.value" />
      </el-select>
      <el-select v-model="filters.action" clearable :placeholder="t('logs.action')" style="width: 140px">
        <el-option v-for="a in actionOptions" :key="a.value" :label="a.label" :value="a.value" />
      </el-select>
      <el-input v-model="filters.target_type" clearable :placeholder="t('logs.targetType')" style="width: 160px" @keyup.enter="search" />
      <el-date-picker
        v-model="filters.range"
        type="datetimerange"
        :start-placeholder="t('logs.startTime')"
        :end-placeholder="t('logs.endTime')"
        style="width: 360px"
      />
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button :icon="Refresh" @click="resetFilters">{{ t("common.reset") }}</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column :label="t('logs.time')" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('logs.admin')" min-width="140">
        <template #default="{ row }">
          <template v-if="row.admin">
            <div>{{ row.admin.display_name || row.admin.username }}</div>
            <div v-if="row.admin.display_name" class="text-secondary mono">{{ row.admin.username }}</div>
          </template>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('logs.group')" min-width="110">
        <template #default="{ row }">{{ row.group_name || "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('logs.module')" width="100">
        <template #default="{ row }">{{ moduleOf(row.permission_code) }}</template>
      </el-table-column>
      <el-table-column :label="t('logs.permissionCode')" min-width="190">
        <template #default="{ row }"><span class="mono">{{ row.permission_code || "-" }}</span></template>
      </el-table-column>
      <el-table-column :label="t('logs.action')" width="100">
        <template #default="{ row }"><StatusTag kind="operation_action" :value="row.action" /></template>
      </el-table-column>
      <el-table-column :label="t('logs.target')" min-width="140">
        <template #default="{ row }">
          <span class="mono">{{ row.target_type || "-" }}{{ row.target_id ? ` #${row.target_id}` : "" }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="summary" :label="t('logs.summary')" min-width="220" show-overflow-tooltip />
      <el-table-column :label="t('logs.ip')" width="130">
        <template #default="{ row }">{{ row.ip || "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('logs.requestId')" width="140">
        <template #default="{ row }">
          <el-tooltip v-if="row.request_id" :content="row.request_id" placement="top">
            <el-button link size="small" :icon="CopyDocument" @click="copy(row.request_id)">{{ row.request_id.slice(0, 8) }}</el-button>
          </el-tooltip>
          <span v-else>-</span>
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
        @current-change="load"
        @size-change="onPageSizeChange"
      />
    </div>
  </el-card>
</template>
