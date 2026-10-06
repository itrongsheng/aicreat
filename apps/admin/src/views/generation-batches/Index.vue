<script setup lang="ts">
// 生成批次（docs/09 §9、§10.7；docs/04 §6.12）：
// - 筛选 kind / status / created_by（「我发起的」）；默认按顶栏当前项目筛选（未选项目时列出全部可见批次）；
// - 列：批次号、类型、项目、请求 / 产出数量、根任务 总 / 成功 / 失败、状态、发起人、开始 / 结束时间、error_summary；
// - 详情抽屉：BatchProgress（tasks[] 各根任务 TaskProgress：目标对象链接、状态、模型、尝试行数、最后错误分类、request_id）；
// - 操作：取消（queued / running）、重试（partial / failed）、AI 任务页（?batch_id=，仅 has('ai.tasks.view')）；
// - 页面可见时对非终态批次 usePolling 3s；顶部读公开 GET /api/v1/health 的 workers.worker.alive，为 false 时显示「worker 未在线」
//   （不调用需要 ai.routes.view 的 /admin/ai/health，reviewer 等用户组也能正常打开本页）。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { DEFAULT_PAGE_SIZE, type BatchKind, type BatchStatus, type GenerationBatch, type SystemHealth } from "@aicreat/shared";
import { isApiError } from "@/api/client";
import * as batchesApi from "@/api/batches";
import BatchProgress, { isTerminalBatch, parseErrorSummary } from "@/components/BatchProgress.vue";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useAuthStore } from "@/store/auth";
import { useProjectStore } from "@/store/project";
import { formatDateTime, formatNumber } from "@/utils/format";

const { t } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();
const projectStore = useProjectStore();

// ---------- worker 在线状态 ----------
const health = ref<SystemHealth | null>(null);

async function loadHealth() {
  health.value = await batchesApi.systemHealth();
}

const workerOffline = computed(() => !!health.value && health.value.workers?.worker?.alive === false);

// ---------- 列表 ----------
const filters = reactive({
  kind: undefined as BatchKind | undefined,
  status: undefined as BatchStatus | undefined,
  mine: false,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<GenerationBatch[]>([]);
const loading = ref(false);
let seq = 0;

async function load(silent = false) {
  const my = ++seq;
  if (!silent) loading.value = true;
  try {
    const res = await batchesApi.list(
      {
        project_id: projectStore.currentId || undefined,
        kind: filters.kind,
        status: filters.status,
        created_by: filters.mine ? auth.admin?.id : undefined,
        page: page.value,
        page_size: pageSize.value,
      },
      { silent },
    );
    if (my !== seq) return;
    rows.value = res.items;
    total.value = res.total;
  } catch {
    if (my !== seq || silent) return;
    rows.value = [];
    total.value = 0;
  } finally {
    if (my === seq) loading.value = false;
  }
  syncPolling();
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.kind = undefined;
  filters.status = undefined;
  filters.mine = false;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

watch(() => projectStore.currentId, search);

// 非终态批次轮询：只刷新列表（静默）
const polling = usePolling(() => load(true), { interval: 3000, immediate: false });

function syncPolling() {
  const active = rows.value.some((r) => !isTerminalBatch(r.status));
  if (active && !polling.running.value) polling.start();
  else if (!active && polling.running.value) polling.stop();
}

function projectName(id: number): string {
  return projectStore.projects.find((p) => p.id === id)?.name ?? `#${id}`;
}

function creatorLabel(id: number | null): string {
  if (!id) return "-";
  if (id === auth.admin?.id) return `${auth.displayName}（${t("batches.me")}）`;
  return `#${id}`;
}

function batchTarget(row: GenerationBatch): string {
  if (row.kind === "keyword") return `/keywords?batch_id=${row.id}`;
  if (row.kind === "title") return `/titles?batch_id=${row.id}`;
  return `/contents?batch_id=${row.id}`;
}

// ---------- 详情抽屉 ----------
const detailVisible = ref(false);
const detailId = ref(0);
const detailRow = ref<GenerationBatch | null>(null);
const detailRef = ref<InstanceType<typeof BatchProgress>>();

function openDetail(row: GenerationBatch) {
  detailRow.value = row;
  detailId.value = row.id;
  detailVisible.value = true;
}

function onDetailUpdate(batch: GenerationBatch) {
  detailRow.value = batch;
  const row = rows.value.find((r) => r.id === batch.id);
  if (row) Object.assign(row, { ...batch, tasks: undefined });
}

// ---------- 取消 / 重试 ----------
const acting = ref(0);

async function cancelBatch(row: GenerationBatch) {
  try {
    await ElMessageBox.confirm(t("batches.cancelConfirm", { id: row.id }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    const updated = await batchesApi.cancel(row.id);
    Object.assign(row, updated);
    ElMessage.success(t("batches.cancelled"));
    afterAction(row.id);
  } catch {
    /* 已提示 */
  } finally {
    acting.value = 0;
  }
}

async function retryBatch(row: GenerationBatch) {
  acting.value = row.id;
  try {
    const updated = await batchesApi.retry(row.id, { silent: true });
    Object.assign(row, updated);
    ElMessage.success(t("batches.retried"));
    afterAction(row.id);
  } catch (err) {
    if (isApiError(err)) ElMessage.error(err.code === 409 ? t("batches.retryConflict") : err.message);
  } finally {
    acting.value = 0;
  }
}

function afterAction(id: number) {
  void load(true);
  if (detailVisible.value && detailId.value === id) detailRef.value?.restart();
}

const canCancel = (row: GenerationBatch) => has("content.batches.cancel") && (row.status === "queued" || row.status === "running");
const canRetry = (row: GenerationBatch) => has("content.batches.retry") && (row.status === "partial" || row.status === "failed");

onMounted(() => {
  void loadHealth();
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void load();
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.generationBatches") }}</h2>
        <span v-if="projectStore.currentProject" class="text-secondary">{{ t("batches.projectScope", { name: projectStore.currentProject.name }) }}</span>
      </div>
    </template>

    <el-alert v-if="workerOffline" type="error" :title="t('batches.workerOffline')" :description="t('batches.workerOfflineHint')" :closable="false" show-icon class="mb" />

    <div class="toolbar">
      <ToolbarSelect v-model="filters.kind" enum-name="batch_kind" :placeholder="t('batches.kind')" width="120px" @change="search" />
      <ToolbarSelect v-model="filters.status" enum-name="batch_status" :placeholder="t('common.status')" width="130px" @change="search" />
      <el-checkbox v-model="filters.mine" @change="search">{{ t("batches.mine") }}</el-checkbox>
      <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <el-button
        :icon="Refresh"
        @click="
          load();
          loadHealth();
        "
      >
        {{ t("common.refresh") }}
      </el-button>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column :label="t('batches.id')" width="90">
        <template #default="{ row }">
          <el-link type="primary" :underline="false" class="mono" @click="openDetail(row)">#{{ row.id }}</el-link>
        </template>
      </el-table-column>
      <el-table-column :label="t('batches.kind')" width="80">
        <template #default="{ row }"><StatusTag kind="batch_kind" :value="row.kind" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('batches.project')" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ projectName(row.project_id) }}</template>
      </el-table-column>
      <el-table-column :label="t('batches.requestedProduced')" width="110" align="right">
        <template #default="{ row }">
          <router-link :to="batchTarget(row)">{{ formatNumber(row.requested_count) }} / {{ formatNumber(row.produced_count) }}</router-link>
        </template>
      </el-table-column>
      <el-table-column :label="t('batches.taskCounts')" width="130" align="center">
        <template #default="{ row }">
          <span>{{ row.task_total }}</span> /
          <span class="ok">{{ row.task_done }}</span> /
          <span :class="{ bad: row.task_failed > 0 }">{{ row.task_failed }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.status')" width="100">
        <template #default="{ row }"><StatusTag kind="batch_status" :value="row.status" /></template>
      </el-table-column>
      <el-table-column :label="t('batches.creator')" min-width="110" show-overflow-tooltip>
        <template #default="{ row }">{{ creatorLabel(row.created_by) }}</template>
      </el-table-column>
      <el-table-column :label="t('batches.startedFinished')" width="170">
        <template #default="{ row }">
          <div>{{ formatDateTime(row.started_at) }}</div>
          <div class="text-secondary">{{ formatDateTime(row.finished_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('batches.errorSummary')" min-width="170">
        <template #default="{ row }">
          <div v-if="row.error_summary" class="summary">
            <el-tag v-for="s in parseErrorSummary(row.error_summary)" :key="s.key" size="small" :type="s.kind === 'failure' ? 'danger' : 'info'" effect="plain">
              {{ s.label }} × {{ s.count }}
            </el-tag>
          </div>
          <span v-else class="text-secondary">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openDetail(row)">{{ t("batches.detail") }}</el-button>
          <el-button v-if="canCancel(row)" link type="warning" :loading="acting === row.id" @click="cancelBatch(row)">{{ t("batches.cancel") }}</el-button>
          <el-button v-if="canRetry(row)" link type="primary" :loading="acting === row.id" @click="retryBatch(row)">{{ t("batches.retry") }}</el-button>
          <router-link v-if="has('ai.tasks.view')" :to="`/ai/tasks?batch_id=${row.id}`" class="task-link">{{ t("batches.aiTasks") }}</router-link>
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

    <el-drawer v-model="detailVisible" :title="t('batches.detailTitle', { id: detailId })" size="min(640px, 96vw)" destroy-on-close>
      <template v-if="detailRow">
        <el-descriptions :column="2" size="small" border class="mb">
          <el-descriptions-item :label="t('batches.project')">{{ projectName(detailRow.project_id) }}</el-descriptions-item>
          <el-descriptions-item :label="t('batches.creator')">{{ creatorLabel(detailRow.created_by) }}</el-descriptions-item>
          <el-descriptions-item :label="t('batches.template')">
            {{ detailRow.template_id ? `#${detailRow.template_id} v${detailRow.template_version ?? "-"}` : "-" }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('batches.modelOverride')">
            <span class="mono">{{ (detailRow.input?.model as string) || t("modelSelect.byRoute") }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('common.createdAt')">{{ formatDateTime(detailRow.created_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('batches.heartbeat')">{{ formatDateTime(detailRow.heartbeat_at) }}</el-descriptions-item>
        </el-descriptions>
        <BatchProgress ref="detailRef" :batch-id="detailId" :initial="detailRow" @update="onDetailUpdate" />
      </template>
      <template #footer>
        <template v-if="detailRow">
          <el-button v-if="canCancel(detailRow)" type="warning" plain :loading="acting === detailRow.id" @click="cancelBatch(detailRow)">{{ t("batches.cancel") }}</el-button>
          <el-button v-if="canRetry(detailRow)" type="primary" plain :loading="acting === detailRow.id" @click="retryBatch(detailRow)">{{ t("batches.retry") }}</el-button>
          <router-link :to="batchTarget(detailRow)" class="footer-link">
            <el-button>{{ t("batches.viewResults") }}</el-button>
          </router-link>
        </template>
      </template>
    </el-drawer>
  </el-card>
</template>

<style scoped>
.mb {
  margin-bottom: 12px;
}
.ok {
  color: var(--el-color-success);
}
.bad {
  color: var(--el-color-danger);
  font-weight: 600;
}
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.task-link {
  margin-left: 8px;
  color: var(--el-color-primary);
  font-size: 14px;
}
.footer-link {
  margin-left: 12px;
}
</style>
