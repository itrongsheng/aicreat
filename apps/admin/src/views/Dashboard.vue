<script setup lang="ts">
// 控制台占位：标题按数据范围显示「总览」/「我的数据」；KPI 与趋势由报表阶段实现（GET /admin/stats/overview）
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { usePermission } from "@/composables/usePermission";

const { t } = useI18n();
const { isAllScope } = usePermission();
const title = computed(() => (isAllScope.value ? t("dashboard.titleAll") : t("dashboard.titleOwn")));
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ title }}</h2>
        <el-tag :type="isAllScope ? 'primary' : 'info'" effect="plain">{{ isAllScope ? t("scope.all") : t("scope.own") }}</el-tag>
      </div>
    </template>
    <el-empty :description="t('common.underConstruction')" />
  </el-card>
</template>
