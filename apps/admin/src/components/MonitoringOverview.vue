<script setup lang="ts">
// 检测记录页顶部概览（docs/11 §11.6；docs/04 §6.18）：GET /admin/monitoring/overview（需 monitoring.link_checks.view）。
// - focus 指定的检测类型：到期数（is_monitoring=1 且 next_*_at ≤ now）、队列长度、今日检测数、最近执行时间、日上限使用量；
// - monitor_worker 副本心跳（alive 标记）；无存活副本时警示「检测暂停」；
// - due / today / last_run_at 按调用者可见的链接计算（用户视角下附加 owner_id），queued / workers / daily_limits 为平台值；
// - 每 30s 静默刷新（页面不可见时暂停）；无权限时不请求、不渲染。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh } from "@element-plus/icons-vue";
import type { MonitoringOverview } from "@aicreat/shared";
import * as monitoringApi from "@/api/monitoring";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useProjectStore } from "@/store/project";
import { formatDateTime, formatNumber } from "@/utils/format";

const props = withDefaults(defineProps<{ focus: "link" | "index" }>(), {});

const { t } = useI18n();
const { has } = usePermission();
const projectStore = useProjectStore();

const canView = computed(() => has("monitoring.link_checks.view"));
const data = ref<MonitoringOverview | null>(null);
const loading = ref(false);
const failed = ref(false);

async function load(silent = false) {
  if (!canView.value) return;
  if (!silent) loading.value = true;
  try {
    data.value = await monitoringApi.overview({ silent: true });
    failed.value = false;
  } catch (err) {
    failed.value = true;
    if (silent) throw err;
  } finally {
    loading.value = false;
  }
}

const polling = usePolling(() => load(true), { interval: 30_000, immediate: false });
watch(
  canView,
  (ok) => {
    if (ok) {
      void load();
      polling.start();
    } else {
      polling.stop();
    }
  },
  { immediate: true },
);
// 用户视角切换后 due / today 口径变化
watch(
  () => projectStore.ownerId,
  () => void load(),
);

const section = computed(() => (props.focus === "link" ? data.value?.link_checks : data.value?.index_checks) ?? null);
const limit = computed(() => (props.focus === "link" ? data.value?.daily_limits?.link_checks : data.value?.daily_limits?.index_checks) ?? null);
const limitPercent = computed(() => {
  const l = limit.value;
  if (!l || !l.limit) return 0;
  return Math.min(100, Math.round((l.used / l.limit) * 100));
});
const limitStatus = computed(() => (limitPercent.value >= 100 ? "exception" : limitPercent.value >= 80 ? "warning" : "success"));

const workers = computed(() => (data.value?.workers ?? []).filter((w) => !w.name || w.name === "monitor_worker"));
const aliveCount = computed(() => workers.value.filter((w) => w.alive).length);

defineExpose({ reload: () => load() });
</script>

<template>
  <div v-if="canView" v-loading="loading && !data" class="mon-overview">
    <div class="mon-head">
      <span class="mon-title">{{ t("monitoring.overview.title") }}</span>
      <span class="text-secondary small">{{ t("monitoring.overview.scopeHint") }}</span>
      <span class="spacer" />
      <el-button size="small" text :icon="Refresh" :loading="loading" @click="load()">{{ t("common.refresh") }}</el-button>
    </div>
    <el-alert v-if="failed && !data" type="warning" :closable="false" show-icon :title="t('monitoring.overview.loadFailed')" />
    <template v-if="data && section">
      <div class="kpi-grid">
        <div class="kpi">
          <div class="kpi-label">{{ t("monitoring.overview.due") }}</div>
          <div class="kpi-value" :class="{ 'is-warn': section.due > 0 }">{{ formatNumber(section.due) }}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{{ t("monitoring.overview.queued") }}</div>
          <div class="kpi-value">{{ formatNumber(section.queued) }}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{{ t("monitoring.overview.today") }}</div>
          <div class="kpi-value">{{ formatNumber(section.today) }}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">{{ t("monitoring.overview.lastRunAt") }}</div>
          <div class="kpi-value kpi-time">{{ formatDateTime(section.last_run_at) }}</div>
        </div>
        <div class="kpi kpi-wide">
          <div class="kpi-label">
            {{ t("monitoring.overview.dailyLimit") }}
            <span class="text-secondary">{{ limit ? `${formatNumber(limit.used)} / ${formatNumber(limit.limit)}` : "-" }}</span>
          </div>
          <el-progress v-if="limit" :percentage="limitPercent" :status="limitStatus" :stroke-width="10" class="kpi-progress" />
          <div v-if="limit && limitPercent >= 100" class="kpi-note">{{ t(focus === "link" ? "monitoring.overview.limitReachedLink" : "monitoring.overview.limitReachedIndex") }}</div>
        </div>
        <div class="kpi kpi-wide">
          <div class="kpi-label">
            {{ t("monitoring.overview.workers") }}
            <span class="text-secondary">{{ t("monitoring.overview.aliveCount", { alive: aliveCount, total: workers.length }) }}</span>
          </div>
          <div v-if="workers.length" class="workers">
            <el-tooltip v-for="w in workers" :key="`${w.hostname}:${w.pid}`" placement="top" :show-after="150">
              <template #content>
                <div>{{ w.name || "monitor_worker" }} · {{ w.hostname }}:{{ w.pid }}</div>
                <div>{{ t("monitoring.overview.heartbeatAt") }}：{{ formatDateTime(w.heartbeat_at) }}</div>
              </template>
              <el-tag :type="w.alive ? 'success' : 'danger'" size="small" effect="plain" disable-transitions class="worker-tag">
                <span class="dot" :class="w.alive ? 'is-alive' : 'is-dead'" />
                {{ w.hostname }}:{{ w.pid }}
              </el-tag>
            </el-tooltip>
          </div>
          <div v-if="!aliveCount" class="kpi-note is-danger">{{ t("monitoring.overview.noWorker") }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.mon-overview {
  margin-bottom: 14px;
}
.mon-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 10px;
  margin-bottom: 8px;
}
.mon-title {
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.small {
  font-size: 12px;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
}
.kpi {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  background: var(--el-fill-color-blank);
  min-width: 0;
}
.kpi-wide {
  grid-column: span 2;
}
.kpi-label {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 4px 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.kpi-value {
  margin-top: 4px;
  font-size: 20px;
  font-weight: 600;
}
.kpi-value.is-warn {
  color: var(--el-color-warning);
}
.kpi-time {
  font-size: 13px;
  line-height: 1.6;
  padding-top: 4px;
}
.kpi-progress {
  margin-top: 8px;
}
.kpi-note {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-color-warning);
}
.kpi-note.is-danger {
  color: var(--el-color-danger);
}
.workers {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.worker-tag {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}
.dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 4px;
  border-radius: 50%;
  vertical-align: middle;
}
.dot.is-alive {
  background: var(--el-color-success);
}
.dot.is-dead {
  background: var(--el-color-danger);
}
@media (max-width: 480px) {
  .kpi-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .kpi-wide {
    grid-column: span 2;
  }
}
</style>
