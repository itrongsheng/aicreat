<script setup lang="ts">
// 素材卡片（docs/10 §7.1~§7.4）：预览（图片 thumbnail_url 懒加载、点击看原图；视频 <video preload="metadata">）、
// 状态标签（media_status / kind / usage_type / source）、尺寸 / 大小 / 时长 / 模型 / 创建时间、进度（TaskProgress，接收 /task 摘要）、
// 视频长任务的已用时 / 预算与 progress ≥ 99 提示、失败原因与处置提示、操作按钮（按权限码显示）：
// 绑定到内容（content.contents.update）、复制 Markdown / HTML 标记、重试 / 转存（media.assets.retry）、取消（ai.tasks.cancel）、删除（media.assets.delete）、查看提示词。
// 上游临时 URL（upstream_url）只用于判断能否「转存」，不在界面展示（docs/10 §13 第 1 条）。
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ArrowDown, Picture, VideoCamera } from "@element-plus/icons-vue";
import type { AiTaskSummary, ContentFormat, MediaAsset, MediaRetryResult } from "@aicreat/shared";
import StatusTag from "@/components/StatusTag.vue";
import TaskProgress from "@/components/TaskProgress.vue";
import {
  assetMarkup,
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
import { isMediaActive } from "@/composables/useAssetTracker";
import { usePermission } from "@/composables/usePermission";
import { formatBytes, formatDateTime } from "@/utils/format";

const props = withDefaults(
  defineProps<{
    asset: MediaAsset;
    /** 当前根任务摘要（/task）；不传时用 asset.task */
    task?: AiTaskSummary | null;
    /** 轮询预算（秒）：传入时进行中的卡片显示「已用时 / 预算」（视频工作台，docs/10 §5.4） */
    budgetSeconds?: number | null;
    /** 显示操作按钮 */
    actions?: boolean;
    /** 显示「绑定到内容」 */
    showAttach?: boolean;
    /** 显示「详情」 */
    showDetail?: boolean;
    /** 紧凑模式（素材库网格） */
    compact?: boolean;
    /** 插入标记 alt 的兜底（文章标题） */
    contentTitle?: string;
  }>(),
  { task: undefined, budgetSeconds: null, actions: true, showAttach: true, showDetail: false, compact: false, contentTitle: "" },
);

const emit = defineEmits<{
  /** 重试 / 转存 / 取消后：父组件刷新该资产并恢复轮询 */
  (e: "changed", id: number): void;
  (e: "retried", result: MediaRetryResult): void;
  (e: "deleted", id: number): void;
  (e: "attach", asset: MediaAsset): void;
  (e: "detail", asset: MediaAsset): void;
}>();

const { t, te } = useI18n();
const { has } = usePermission();

const a = computed(() => props.asset);
const task = computed<AiTaskSummary | null>(() => (props.task !== undefined ? props.task : (props.asset.task ?? null)));
const active = computed(() => isMediaActive(a.value.status));
const ready = computed(() => a.value.status === "ready" && !!a.value.url);
const failed = computed(() => a.value.status === "failed" || a.value.status === "expired");
const progress = computed(() => Math.max(a.value.progress ?? 0, task.value?.progress ?? 0));

// ---------- 已用时 / 预算 ----------
const now = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | null = null;
function syncTicker() {
  const need = active.value && !!props.budgetSeconds;
  if (need && !ticker) ticker = setInterval(() => (now.value = Date.now()), 1000);
  if (!need && ticker) {
    clearInterval(ticker);
    ticker = null;
  }
}
watch([active, () => props.budgetSeconds], syncTicker, { immediate: true });
onBeforeUnmount(() => ticker && clearInterval(ticker));

function mmss(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
}
const elapsed = computed(() => {
  const created = Date.parse(a.value.created_at);
  return Number.isFinite(created) ? (now.value - created) / 1000 : 0;
});
const overBudget = computed(() => !!props.budgetSeconds && elapsed.value > props.budgetSeconds);

// ---------- 失败处置提示 ----------
const failureHints = computed<string[]>(() => {
  if (!failed.value) return [];
  const out: string[] = [];
  const category = a.value.error_category;
  if (a.value.status === "expired") out.push(t("media.card.expiredHint"));
  else if (category === "timeout" && a.value.upstream_task_id) out.push(t("taskProgress.mediaTimeout"));
  else if (category && te(`taskProgress.hints.${category}`)) out.push(t(`taskProgress.hints.${category}`));
  const override = task.value?.model_override;
  if (override && category !== "content_blocked" && category !== "cancelled") out.push(t("taskProgress.modelOverrideFailed", { model: override }));
  return out;
});

// ---------- 操作 ----------
const busy = ref<"" | "retry" | "transfer" | "cancel" | "delete">("");

async function onRetry() {
  busy.value = "retry";
  try {
    const res = await retryAssetFlow(a.value);
    if (res) {
      emit("retried", res);
      emit("changed", a.value.id);
    }
  } finally {
    busy.value = "";
  }
}

async function onTransfer() {
  busy.value = "transfer";
  try {
    if (await transferAssetFlow(a.value)) emit("changed", a.value.id);
  } finally {
    busy.value = "";
  }
}

async function onCancel() {
  busy.value = "cancel";
  try {
    if (await cancelAssetFlow(a.value)) emit("changed", a.value.id);
  } finally {
    busy.value = "";
  }
}

async function onDelete() {
  busy.value = "delete";
  try {
    if (await deleteAssetFlow(a.value)) emit("deleted", a.value.id);
  } finally {
    busy.value = "";
  }
}

function copyMarkup(format: ContentFormat) {
  void copyText(assetMarkup(a.value, format, props.contentTitle));
}

const retryLabel = computed(() => (retryChecksUpstream(a.value) ? t("media.actions.retryRecheck") : t("media.actions.retry")));
const showRetry = computed(() => has("media.assets.retry") && canRetryAsset(a.value));
const showTransfer = computed(() => has("media.assets.retry") && canTransferAsset(a.value));
const showCancel = computed(() => has("ai.tasks.cancel") && canCancelAsset(a.value));
const showDelete = computed(() => has("media.assets.delete") && canDeleteAsset(a.value));
const showAttachBtn = computed(() => props.showAttach && ready.value && has("content.contents.update"));
</script>

<template>
  <div class="asset-card" :class="{ 'is-compact': compact, 'is-failed': failed }">
    <div class="asset-card__preview">
      <template v-if="ready">
        <el-image
          v-if="a.kind === 'image'"
          :src="a.thumbnail_url || a.url || ''"
          fit="cover"
          lazy
          class="asset-card__media"
          :preview-src-list="a.url ? [a.url] : []"
          preview-teleported
        >
          <template #error><div class="asset-card__media asset-card__placeholder">{{ t("media.card.broken") }}</div></template>
        </el-image>
        <video v-else :src="a.url || ''" preload="metadata" controls class="asset-card__media" />
      </template>
      <div v-else class="asset-card__media asset-card__placeholder">
        <el-icon :size="28"><component :is="a.kind === 'video' ? VideoCamera : Picture" /></el-icon>
        <span v-if="active" class="asset-card__pct">{{ progress }}%</span>
        <StatusTag v-else kind="media_status" :value="a.status" />
      </div>
    </div>

    <div class="asset-card__body">
      <div class="asset-card__line">
        <el-button v-if="showDetail" link type="primary" class="mono" @click="emit('detail', a)">#{{ a.id }}</el-button>
        <span v-else class="mono">#{{ a.id }}</span>
        <StatusTag kind="media_status" :value="a.status" />
        <StatusTag kind="media_kind" :value="a.kind" effect="plain" />
        <StatusTag kind="media_usage_type" :value="a.usage_type" effect="plain" />
        <StatusTag v-if="!compact" kind="media_source" :value="a.source" effect="plain" />
      </div>
      <div class="asset-card__meta text-secondary">
        <span v-if="a.width && a.height">{{ a.width }}×{{ a.height }}</span>
        <span v-if="a.size_bytes">{{ formatBytes(a.size_bytes) }}</span>
        <span v-if="a.duration_seconds">{{ t("media.card.seconds", { n: a.duration_seconds }) }}</span>
        <span v-if="a.model" class="mono">{{ a.model }}</span>
        <span>{{ formatDateTime(a.created_at, false) }}</span>
        <router-link v-if="a.content_id && !compact" :to="`/contents/${a.content_id}`">{{ t("media.card.content", { id: a.content_id }) }}</router-link>
      </div>

      <!-- 进行中：任务进度 -->
      <div v-if="active" class="asset-card__progress">
        <TaskProgress v-if="task" :task="task" :compact="compact" :show-hint="false" :show-request-id="false" />
        <el-progress v-else :percentage="progress" :indeterminate="progress <= 0" :stroke-width="6" :show-text="!compact" />
        <div v-if="a.status === 'downloading'" class="asset-card__note text-secondary">{{ t("media.card.downloading") }}</div>
        <div v-if="budgetSeconds" class="asset-card__note" :class="{ 'is-over': overBudget }">
          {{ t("media.card.elapsed", { elapsed: mmss(elapsed), budget: mmss(budgetSeconds) }) }}
        </div>
        <div v-if="a.kind === 'video' && progress >= 99 && a.status !== 'downloading'" class="asset-card__note text-secondary">{{ t("media.card.upstreamProcessing") }}</div>
      </div>

      <!-- 失败 / 过期：原因与处置 -->
      <div v-if="failed" class="asset-card__error">
        <div class="asset-card__line">
          <StatusTag v-if="a.error_category" kind="error_category" :value="a.error_category" effect="plain" />
          <el-tag v-if="task?.model_override" size="small" type="warning" effect="plain">{{ t("taskProgress.modelOverride") }}: {{ task.model_override }}</el-tag>
        </div>
        <div v-if="a.error_message && !compact" class="asset-card__errmsg">{{ a.error_message }}</div>
        <div v-for="(hint, idx) in failureHints" :key="idx" class="asset-card__hint">{{ hint }}</div>
      </div>

      <div v-if="actions" class="asset-card__ops">
        <el-popover v-if="a.prompt" placement="top" :width="360" trigger="click">
          <template #reference>
            <el-button link type="primary" size="small">{{ t("media.card.viewPrompt") }}</el-button>
          </template>
          <div class="asset-card__prompt">{{ a.prompt }}</div>
          <div v-if="a.negative_prompt" class="asset-card__prompt text-secondary">{{ t("media.fields.negativePrompt") }}：{{ a.negative_prompt }}</div>
          <el-button link type="primary" size="small" @click="copyText(a.prompt)">{{ t("common.copy") }}</el-button>
        </el-popover>
        <el-button v-if="showAttachBtn" link type="primary" size="small" @click="emit('attach', a)">{{ t("media.actions.attach") }}</el-button>
        <el-dropdown v-if="ready" trigger="click" @command="copyMarkup">
          <el-button link type="primary" size="small" class="asset-card__dropdown">
            {{ t("media.actions.copyMarkup") }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="markdown">Markdown</el-dropdown-item>
              <el-dropdown-item command="html">HTML</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button v-if="ready" link type="primary" size="small" @click="copyText(a.url || '')">{{ t("media.actions.copyUrl") }}</el-button>
        <el-button v-if="showTransfer" link type="primary" size="small" :loading="busy === 'transfer'" @click="onTransfer">{{ t("media.actions.transfer") }}</el-button>
        <el-button v-if="showRetry" link type="warning" size="small" :loading="busy === 'retry'" @click="onRetry">{{ retryLabel }}</el-button>
        <el-button v-if="showCancel" link type="warning" size="small" :loading="busy === 'cancel'" @click="onCancel">{{ t("media.actions.cancel") }}</el-button>
        <el-button v-if="showDelete" link type="danger" size="small" :loading="busy === 'delete'" @click="onDelete">{{ t("common.delete") }}</el-button>
        <el-button v-if="showDetail" link size="small" @click="emit('detail', a)">{{ t("media.actions.detail") }}</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.asset-card {
  display: flex;
  flex-direction: column;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  overflow: hidden;
  background: var(--el-bg-color);
  min-width: 0;
}
.asset-card.is-failed {
  border-color: var(--el-color-danger-light-5);
}
.asset-card__preview {
  aspect-ratio: 16 / 10;
  background: var(--el-fill-color-light);
}
.asset-card__media {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}
video.asset-card__media {
  object-fit: contain;
  background: #000;
}
.asset-card__placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.asset-card__pct {
  font-size: 16px;
  font-weight: 600;
}
.asset-card__body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px 10px;
  min-width: 0;
}
.asset-card__line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.asset-card__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 12px;
}
.asset-card__meta a {
  color: var(--el-color-primary);
  text-decoration: none;
}
.asset-card__progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.asset-card__note {
  font-size: 12px;
}
.asset-card__note.is-over {
  color: var(--el-color-warning);
}
.asset-card__error {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.asset-card__errmsg {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-all;
}
.asset-card__hint {
  color: var(--el-color-warning-dark-2);
  font-size: 12px;
  line-height: 1.5;
}
.asset-card__ops {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 10px;
}
.asset-card__ops .el-button + .el-button {
  margin-left: 0;
}
.asset-card__dropdown {
  vertical-align: middle;
}
.asset-card__prompt {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  margin-bottom: 6px;
}
.is-compact .asset-card__body {
  padding: 6px 8px 8px;
  gap: 4px;
}
</style>
