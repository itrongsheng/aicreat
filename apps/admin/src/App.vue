<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import en from "element-plus/es/locale/lang/en";

const { locale } = useI18n();
const elLocale = computed(() => (locale.value === "en-US" ? en : zhCn));
</script>

<template>
  <el-config-provider :locale="elLocale">
    <router-view />
  </el-config-provider>
</template>

<style>
html,
body,
#app {
  height: 100%;
  margin: 0;
}
body {
  font-family:
    -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
  background: var(--el-bg-color-page);
  color: var(--el-text-color-primary);
  -webkit-font-smoothing: antialiased;
}
/* 页面通用：工具栏、表格卡片、分页 */
.page-card + .page-card {
  margin-top: 12px;
}
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.page-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.toolbar .spacer {
  flex: 1;
}
.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
.text-secondary {
  color: var(--el-text-color-secondary);
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
}
</style>
