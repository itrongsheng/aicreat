<script setup lang="ts">
// 明暗主题切换：html.dark + Element Plus 暗色 CSS 变量
import { useI18n } from "vue-i18n";
import { Moon, Sunny } from "@element-plus/icons-vue";
import { useThemeStore } from "@/store/theme";

const theme = useThemeStore();
const { t } = useI18n();
</script>

<template>
  <el-tooltip :content="theme.isDark ? t('common.lightMode') : t('common.darkMode')" placement="bottom">
    <el-button text circle class="theme-switch" @click="theme.toggle()">
      <el-icon :size="18"><Sunny v-if="theme.isDark" /><Moon v-else /></el-icon>
    </el-button>
  </el-tooltip>
</template>
