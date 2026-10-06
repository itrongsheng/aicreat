<script setup lang="ts">
// 界面语言切换（zh-CN / en-US），持久化；生成内容语言由项目 language 决定，不受此影响
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { Locale } from "@aicreat/shared";
import { setLocale } from "@/i18n";

const { t, locale } = useI18n();

const LABELS: Record<Locale, string> = { "zh-CN": "简体中文", "en-US": "English" };
const current = computed(() => LABELS[locale.value as Locale] ?? locale.value);

function onCommand(value: Locale) {
  setLocale(value);
}
</script>

<template>
  <el-dropdown trigger="click" @command="onCommand">
    <el-button text class="lang-switch" :title="t('common.language')">
      <svg class="lang-icon" viewBox="0 0 24 24" aria-hidden="true">
        <path
          fill="currentColor"
          d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm6.9 6h-2.95a15.7 15.7 0 0 0-1.38-3.56A8.03 8.03 0 0 1 18.9 8ZM12 4.04c.83 1.2 1.48 2.53 1.91 3.96h-3.82c.43-1.43 1.08-2.76 1.91-3.96ZM4.26 14a8.2 8.2 0 0 1 0-4h3.38a16.5 16.5 0 0 0 0 4H4.26Zm.84 2h2.95c.32 1.25.78 2.45 1.38 3.56A8 8 0 0 1 5.1 16Zm2.95-8H5.1a8 8 0 0 1 4.33-3.56A15.7 15.7 0 0 0 8.05 8ZM12 19.96A14.1 14.1 0 0 1 10.09 16h3.82A14.1 14.1 0 0 1 12 19.96ZM14.34 14H9.66a14.7 14.7 0 0 1 0-4h4.68a14.7 14.7 0 0 1 0 4Zm.25 5.56c.6-1.11 1.06-2.31 1.38-3.56h2.95a8.03 8.03 0 0 1-4.33 3.56ZM16.36 14a16.5 16.5 0 0 0 0-4h3.38a8.2 8.2 0 0 1 0 4h-3.38Z"
        />
      </svg>
      <span class="lang-label">{{ current }}</span>
    </el-button>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item v-for="(label, code) in LABELS" :key="code" :command="code" :disabled="code === locale">{{ label }}</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style scoped>
.lang-switch {
  padding: 0 6px;
}
.lang-icon {
  width: 16px;
  height: 16px;
  margin-right: 4px;
}
.lang-label {
  font-size: 13px;
}
</style>
