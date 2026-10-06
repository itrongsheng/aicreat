<script setup lang="ts">
// 无权限落点（docs/07 §8.6）
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { resolveHomePath } from "@/router";

const router = useRouter();
const { t } = useI18n();

function goBack() {
  if (window.history.length > 1) router.back();
  else void router.replace(resolveHomePath());
}

function goHome() {
  const home = resolveHomePath();
  void router.replace(home === "/403" ? "/403" : home);
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <el-result icon="warning" :title="t('forbidden.title')" :sub-title="t('forbidden.message')">
      <template #extra>
        <el-button @click="goBack">{{ t("common.back") }}</el-button>
        <el-button type="primary" @click="goHome">{{ t("common.home") }}</el-button>
      </template>
    </el-result>
  </el-card>
</template>
