<script lang="ts">
// ai_tasks / 素材任务进度（docs/02 §3.6、docs/08 §9.3、docs/10 §4 页面）：状态标签 + 进度条 + 错误分类文案映射。
// 接收任一任务摘要：`AiTaskSummary`（/contents/{id}/task、/media/assets/{id}/task）、`BatchTaskSummary`（批次 tasks[]）或 `AiTask`（/ai/tasks）。
// 异步任务的 error_message 不附 hint 后缀：model_override 非空 → 使用了覆盖模型、未切换备选；error_category=content_blocked → 上游拦截（提示修改提示词）。
import type { AiTask, AiTaskOperation, AiTaskStatus, AiTaskSummary, BatchTaskSummary, ErrorCategory } from "@aicreat/shared";

export type TaskLike = AiTaskSummary | BatchTaskSummary | AiTask;

export interface NormalizedTask {
  id: number;
  operation: AiTaskOperation | null;
  status: AiTaskStatus;
  progress: number;
  errorCategory: ErrorCategory | null;
  errorMessage: string | null;
  modelOverride: string | null;
  model: string | null;
  requestId: string | null;
  attemptCount: number | null;
}

/** 终态（docs/08 §7.5） */
export const TERMINAL_TASK_STATUSES: readonly AiTaskStatus[] = ["succeeded", "failed", "cancelled", "expired"];

export function isTerminalTask(status: AiTaskStatus | null | undefined): boolean {
  return !!status && TERMINAL_TASK_STATUSES.includes(status);
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export function normalizeTask(task: TaskLike): NormalizedTask {
  const raw = task as unknown as Record<string, unknown>;
  const id = Number(raw.task_id ?? raw.id ?? 0);
  // AiTask 没有 model_override：取根任务 input.model（详情才返回 input）
  const input = raw.input && typeof raw.input === "object" ? (raw.input as Record<string, unknown>) : null;
  return {
    id,
    operation: (str(raw.operation) as AiTaskOperation | null) ?? null,
    status: raw.status as AiTaskStatus,
    progress: Number(raw.progress ?? 0) || 0,
    errorCategory: (str(raw.error_category) ?? str(raw.last_error_category)) as ErrorCategory | null,
    errorMessage: str(raw.error_message) ?? str(raw.last_error_message),
    modelOverride: "model_override" in raw ? str(raw.model_override) : str(input?.model),
    model: str(raw.model),
    requestId: str(raw.request_id),
    attemptCount: typeof raw.attempt_count === "number" ? raw.attempt_count : null,
  };
}
</script>

<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import StatusTag from "@/components/StatusTag.vue";

const props = withDefaults(
  defineProps<{
    task: TaskLike | null | undefined;
    /** 显示上游请求号（有时） */
    showRequestId?: boolean;
    /** 显示错误文案与处置提示 */
    showHint?: boolean;
    /** 紧凑模式：单行状态 + 细进度条（表格行内） */
    compact?: boolean;
  }>(),
  { showRequestId: true, showHint: true, compact: false },
);

const { t, te } = useI18n();

const info = computed(() => (props.task ? normalizeTask(props.task) : null));

const MEDIA_OPERATIONS: readonly AiTaskOperation[] = ["image_generate", "video_generate"];

const percentage = computed(() => {
  const task = info.value;
  if (!task) return 0;
  if (task.status === "succeeded") return 100;
  return Math.max(0, Math.min(100, Math.round(task.progress)));
});

/** 执行中 / 排队中且无上游进度时显示不确定进度条 */
const indeterminate = computed(() => {
  const task = info.value;
  return !!task && (task.status === "queued" || task.status === "running" || (task.status === "polling" && task.progress <= 0));
});

const progressStatus = computed<"" | "success" | "exception" | "warning">(() => {
  switch (info.value?.status) {
    case "succeeded":
      return "success";
    case "failed":
      return "exception";
    case "expired":
    case "cancelled":
      return "warning";
    default:
      return "";
  }
});

const failed = computed(() => !!info.value && (info.value.status === "failed" || info.value.status === "expired"));

/** 处置提示：分类提示 + 媒体超时复查提示 + 覆盖模型提示 */
const hints = computed<string[]>(() => {
  const task = info.value;
  if (!task || !props.showHint) return [];
  const out: string[] = [];
  const category = task.errorCategory;
  if (category && (failed.value || task.status === "cancelled")) {
    if (category === "timeout" && task.operation && MEDIA_OPERATIONS.includes(task.operation)) out.push(t("taskProgress.mediaTimeout"));
    else if (te(`taskProgress.hints.${category}`)) out.push(t(`taskProgress.hints.${category}`));
  }
  if (failed.value && task.modelOverride && category !== "content_blocked" && category !== "cancelled") {
    out.push(t("taskProgress.modelOverrideFailed", { model: task.modelOverride }));
  }
  return out;
});

const hintType = computed(() => (info.value?.errorCategory === "content_blocked" ? "error" : "warning"));
</script>

<template>
  <div v-if="info" class="task-progress" :class="{ 'is-compact': compact }">
    <div class="task-progress__head">
      <StatusTag kind="ai_task_status" :value="info.status" />
      <StatusTag v-if="info.errorCategory" kind="error_category" :value="info.errorCategory" effect="plain" />
      <span v-if="!compact && info.operation" class="task-progress__op">{{ t(`status.ai_task_operation.${info.operation}`) }}</span>
      <el-tooltip v-if="info.modelOverride" :content="t('taskProgress.modelOverrideTip')" placement="top">
        <el-tag size="small" type="warning" effect="plain" class="task-progress__override">
          {{ t("taskProgress.modelOverride") }}: <span class="mono">{{ info.modelOverride }}</span>
        </el-tag>
      </el-tooltip>
      <span v-else-if="!compact && info.model" class="task-progress__model">
        {{ t("taskProgress.model") }}: <span class="mono">{{ info.model }}</span>
      </span>
      <span v-if="!compact && info.attemptCount" class="task-progress__attempts">{{ t("taskProgress.attempts", { count: info.attemptCount }) }}</span>
    </div>
    <el-progress
      v-if="!isTerminalTask(info.status) || !compact"
      :percentage="percentage"
      :status="progressStatus || undefined"
      :indeterminate="indeterminate"
      :duration="2"
      :stroke-width="compact ? 4 : 8"
      :show-text="!compact"
      class="task-progress__bar"
    />
    <template v-if="!compact">
      <div v-if="info.errorMessage && (failed || info.status === 'cancelled')" class="task-progress__error">{{ info.errorMessage }}</div>
      <el-alert v-for="(hint, idx) in hints" :key="idx" :type="hintType" :closable="false" show-icon :title="hint" class="task-progress__hint" />
      <div v-if="showRequestId && info.requestId" class="task-progress__rid">
        {{ t("taskProgress.requestId") }}: <span class="mono">{{ info.requestId }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.task-progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.task-progress.is-compact {
  gap: 2px;
}
.task-progress__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.task-progress__op,
.task-progress__model,
.task-progress__attempts {
  color: var(--el-text-color-secondary);
}
.task-progress__error {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
}
.task-progress__hint {
  padding: 4px 10px;
}
.task-progress__rid {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
