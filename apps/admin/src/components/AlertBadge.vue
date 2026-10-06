<script setup lang="ts">
// 顶栏告警铃铛（读 store/alerts）；仅有 monitoring.alerts.view 时由 Layout 渲染
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { Bell } from "@element-plus/icons-vue";
import { useAlertsStore } from "@/store/alerts";

const alerts = useAlertsStore();
const router = useRouter();
const { t } = useI18n();

function open() {
  void router.push({ path: "/alerts", query: { status: "open" } });
}
</script>

<template>
  <el-tooltip :content="t('alerts.badgeTitle')" placement="bottom">
    <el-badge :value="alerts.openCount" :max="99" :hidden="alerts.openCount <= 0" class="alert-badge">
      <el-button text circle @click="open">
        <el-icon :size="18"><Bell /></el-icon>
      </el-button>
    </el-badge>
  </el-tooltip>
</template>

<style scoped>
.alert-badge :deep(.el-badge__content.is-fixed) {
  top: 6px;
  right: 10px;
}
</style>
