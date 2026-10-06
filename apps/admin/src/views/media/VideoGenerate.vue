<script setup lang="ts">
// 占位页：由后续阶段替换为完整实现（路径与路由保持不变）
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";

const route = useRoute();
const { t } = useI18n();
const title = computed(() => t(route.meta.title ?? "menu.mediaVideos"));
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <h2 class="page-title">{{ title }}</h2>
    </template>
    <el-empty :description="t('common.underConstruction')" />
  </el-card>
</template>
