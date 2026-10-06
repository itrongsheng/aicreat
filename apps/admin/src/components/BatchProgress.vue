<script lang="ts">
// 生成批次进度（docs/09 §9.3、§10.3、§10.4、§10.7；docs/04 §7.4）：`usePolling` 每 3s 调 `GET /admin/generation-batches/{id}`，
// 页面不可见时暂停，`status` 进入 succeeded/partial/failed/cancelled 后停止并发出 finished；
// 展示根任务计数、产出数、`error_summary` 分项与各根任务的 `TaskProgress`，「查看任务」链接仅 has('ai.tasks.view') 时显示。
import { BATCH_APPLY_COUNT_KEYS, BATCH_TERMINAL_STATUSES, type BatchStatus } from "@aicreat/shared";
import { t, te } from "@/i18n";
import { statusLabel } from "@/components/StatusTag.vue";

export function isTerminalBatch(status: BatchStatus | null | undefined): boolean {
  return !!status && BATCH_TERMINAL_STATUSES.includes(status);
}

export interface SummaryItem {
  key: string;
  count: number;
  label: string;
  /** 分项计数（去重 / 无效 / 空输出等）为 info，失败分类为 danger */
  kind: "apply" | "failure";
}

/** 解析 `error_summary`（`duplicates=2;too_long=1;content_blocked=1`） */
export function parseErrorSummary(summary: string | null | undefined): SummaryItem[] {
  if (!summary) return [];
  const out: SummaryItem[] = [];
  for (const part of summary.split(";")) {
    const [rawKey, rawCount] = part.split("=");
    const key = (rawKey ?? "").trim();
    if (!key) continue;
    const count = Number(rawCount);
    const apply = (BATCH_APPLY_COUNT_KEYS as readonly string[]).includes(key);
    let label: string;
    if (te(`batches.summary.${key}`)) label = t(`batches.summary.${key}`);
    else label = statusLabel("error_category", key);
    out.push({ key, count: Number.isFinite(count) ? count : 0, label, kind: apply ? "apply" : "failure" });
  }
  return out;
}
</script>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { BatchTaskSummary, GenerationBatch } from "@aicreat/shared";
import * as batchesApi from "@/api/batches";
import StatusTag from "@/components/StatusTag.vue";
import TaskProgress from "@/components/TaskProgress.vue";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { formatDateTime, formatNumber } from "@/utils/format";

const props = withDefaults(
  defineProps<{
    batchId: number;
    /** 根任务目标的显示名（标题批次为关键词文本，内容批次为标题） */
    targetLabels?: Record<number, string>;
    /** 是否列出根任务 */
    showTasks?: boolean;
    /** 已加载的批次对象（列表行）作为初始值 */
    initial?: GenerationBatch | null;
  }>(),
  { targetLabels: () => ({}), showTasks: true, initial: null },
);

const emit = defineEmits<{
  (e: "update", batch: GenerationBatch): void;
  (e: "finished", batch: GenerationBatch): void;
}>();

const { t: tt } = useI18n();
const { has } = usePermission();

const batch = ref<GenerationBatch | null>(props.initial);
const loadFailed = ref(false);
let finishedFor = 0;

async function fetchBatch() {
  const id = props.batchId;
  try {
    const res = await batchesApi.get(id, { silent: true });
    if (id !== props.batchId) return;
    batch.value = res;
    loadFailed.value = false;
    emit("update", res);
    if (isTerminalBatch(res.status)) {
      polling.stop();
      if (finishedFor !== id) {
        finishedFor = id;
        emit("finished", res);
      }
    }
  } catch (err) {
    loadFailed.value = true;
    throw err;
  }
}

const polling = usePolling(fetchBatch, { interval: 3000 });

function restart() {
  polling.stop();
  finishedFor = 0;
  if (props.batchId > 0) polling.start();
}

watch(
  () => props.batchId,
  (id, old) => {
    if (id !== old) {
      batch.value = props.initial && props.initial.id === id ? props.initial : null;
      restart();
    }
  },
);
onMounted(restart);

const percent = computed(() => {
  const b = batch.value;
  if (!b || !b.task_total) return 0;
  if (b.status === "succeeded") return 100;
  return Math.min(100, Math.round(((b.task_done + b.task_failed) / b.task_total) * 100));
});

const progressStatus = computed<"" | "success" | "exception" | "warning">(() => {
  switch (batch.value?.status) {
    case "succeeded":
      return "success";
    case "failed":
      return "exception";
    case "partial":
    case "cancelled":
      return "warning";
    default:
      return "";
  }
});

const summary = computed(() => parseErrorSummary(batch.value?.error_summary));

const hints = computed<string[]>(() => {
  const b = batch.value;
  if (!b || !isTerminalBatch(b.status)) return [];
  const out: string[] = [];
  const keys = new Set(summary.value.map((s) => s.key));
  if (keys.has("empty_output")) out.push(tt("batches.hints.emptyOutput"));
  if (b.kind !== "content" && b.produced_count === 0 && keys.has("duplicates")) out.push(tt("batches.hints.allDuplicates"));
  if (keys.has("content_blocked")) out.push(tt("batches.hints.contentBlocked"));
  return out;
});

function targetLabel(task: BatchTaskSummary): string {
  if (task.target_id && props.targetLabels[task.target_id]) return props.targetLabels[task.target_id];
  if (task.target_type && task.target_id) return `${statusLabel("ai_task_target_type", task.target_type)} #${task.target_id}`;
  return `#${task.id}`;
}

function targetRoute(task: BatchTaskSummary): string | null {
  if (!task.target_id) return null;
  if (task.target_type === "content") return `/contents/${task.target_id}`;
  if (task.target_type === "keyword") return `/titles?keyword_id=${task.target_id}`;
  return null;
}

defineExpose({ refresh: () => fetchBatch().catch(() => undefined), restart, batch });
</script>

<template>
  <div class="batch-progress">
    <el-skeleton v-if="!batch && !loadFailed" :rows="3" animated />
    <el-alert v-else-if="!batch" type="error" :title="tt('batches.loadFailed')" :closable="false" show-icon />
    <template v-else>
      <div class="batch-progress__head">
        <span class="mono">#{{ batch.id }}</span>
        <StatusTag kind="batch_kind" :value="batch.kind" effect="plain" />
        <StatusTag kind="batch_status" :value="batch.status" />
        <span class="spacer" />
        <router-link v-if="has('ai.tasks.view')" :to="`/ai/tasks?batch_id=${batch.id}`" class="batch-progress__link">{{ tt("batches.viewTasks") }}</router-link>
      </div>
      <el-progress
        :percentage="percent"
        :status="progressStatus || undefined"
        :indeterminate="!isTerminalBatch(batch.status) && percent === 0"
        :duration="2"
        :stroke-width="10"
      />
      <div class="batch-progress__stats">
        <span>{{ tt("batches.requested") }} {{ formatNumber(batch.requested_count) }}</span>
        <span>{{ tt("batches.produced") }} <b>{{ formatNumber(batch.produced_count) }}</b></span>
        <span>{{ tt("batches.tasksDone", { done: batch.task_done, failed: batch.task_failed, total: batch.task_total }) }}</span>
        <span v-if="batch.finished_at" class="text-secondary">{{ tt("batches.finishedAt") }} {{ formatDateTime(batch.finished_at) }}</span>
      </div>
      <div v-if="summary.length" class="batch-progress__summary">
        <el-tag v-for="s in summary" :key="s.key" :type="s.kind === 'failure' ? 'danger' : 'info'" size="small" effect="plain">
          {{ s.label }} × {{ s.count }}
        </el-tag>
      </div>
      <el-alert v-for="(h, i) in hints" :key="i" type="warning" :title="h" :closable="false" show-icon class="batch-progress__hint" />
      <div v-if="showTasks && batch.tasks?.length" class="batch-progress__tasks">
        <div v-for="task in batch.tasks" :key="task.id" class="batch-progress__task">
          <div class="batch-progress__task-target">
            <router-link v-if="targetRoute(task)" :to="targetRoute(task)!">{{ targetLabel(task) }}</router-link>
            <span v-else>{{ targetLabel(task) }}</span>
            <span class="text-secondary mono">{{ tt("batches.rootTask") }} #{{ task.id }}</span>
          </div>
          <TaskProgress :task="task" />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.batch-progress {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.batch-progress__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.batch-progress__head .spacer {
  flex: 1;
}
.batch-progress__link {
  color: var(--el-color-primary);
  font-size: 13px;
}
.batch-progress__stats {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  font-size: 13px;
}
.batch-progress__summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.batch-progress__hint {
  padding: 4px 10px;
}
.batch-progress__tasks {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 420px;
  overflow-y: auto;
}
.batch-progress__task {
  padding: 8px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.batch-progress__task-target {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 13px;
}
</style>
