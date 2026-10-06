<script setup lang="ts">
// 顶栏告警铃铛（docs/11 §11.7）：读 store/alerts 的摘要（60s 轮询），徽标为未处理（open）告警数；
// 存在未处理的严重（critical）告警时徽标与铃铛高亮为危险色并轻微脉动。仅有 monitoring.alerts.view 时由 Layout 渲染。
// 点击跳转告警中心：有严重告警时预置 severity=critical，否则只筛选未处理。
import { computed } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { Bell } from "@element-plus/icons-vue";
import { ALERT_SEVERITY } from "@aicreat/shared";
import { useAlertsStore } from "@/store/alerts";

const alerts = useAlertsStore();
const router = useRouter();
const { t } = useI18n();

const critical = computed(() => alerts.criticalCount > 0);
const badgeType = computed(() => (critical.value ? "danger" : alerts.topSeverity === "warning" ? "warning" : "primary"));

const breakdown = computed(() =>
  [...ALERT_SEVERITY].reverse().map((sev) => ({ sev, count: Number(alerts.summary?.open?.[sev]) || 0 })),
);

function open() {
  void router.push({ path: "/alerts", query: critical.value ? { status: "open", severity: "critical" } : { status: "open" } });
}
</script>

<template>
  <el-tooltip placement="bottom" :show-after="150">
    <template #content>
      <div class="alert-tip-title">{{ t("alerts.badgeTitle") }}：{{ alerts.openCount }}</div>
      <div v-for="item in breakdown" :key="item.sev" class="alert-tip-row">
        <span>{{ t(`status.alert_severity.${item.sev}`) }}</span>
        <span>{{ item.count }}</span>
      </div>
      <div v-if="alerts.acknowledgedCount" class="alert-tip-row">
        <span>{{ t("status.alert_status.acknowledged") }}</span>
        <span>{{ alerts.acknowledgedCount }}</span>
      </div>
    </template>
    <el-badge :value="alerts.openCount" :max="99" :hidden="alerts.openCount <= 0" :type="badgeType" class="alert-badge" :class="{ 'is-critical': critical }">
      <el-button text circle :aria-label="t('alerts.badgeTitle')" @click="open">
        <el-icon :size="18" :class="{ 'bell-critical': critical }"><Bell /></el-icon>
      </el-button>
    </el-badge>
  </el-tooltip>
</template>

<style scoped>
.alert-badge :deep(.el-badge__content.is-fixed) {
  top: 6px;
  right: 10px;
}
.alert-badge.is-critical :deep(.el-badge__content) {
  animation: alert-pulse 1.6s ease-in-out infinite;
}
.bell-critical {
  color: var(--el-color-danger);
}
.alert-tip-title {
  font-weight: 600;
  margin-bottom: 4px;
}
.alert-tip-row {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  font-size: 12px;
  line-height: 1.6;
}
@keyframes alert-pulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(245, 108, 108, 0.55);
  }
  50% {
    box-shadow: 0 0 0 5px rgba(245, 108, 108, 0);
  }
}
@media (prefers-reduced-motion: reduce) {
  .alert-badge.is-critical :deep(.el-badge__content) {
    animation: none;
  }
}
</style>
