<script setup lang="ts">
// 生成表单顶部提示（docs/09 §9.6、§10.3、§12）：额度预警黄色横幅（`quota_warning`）+ 错误提示（429 / 4291 / 5031 / 4221 / 409 / 404 / 400）。
// 与 composables/useGenerateGuard.ts 配合使用。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { QuotaWarning } from "@aicreat/shared";
import type { GenerateNoticeInfo } from "@/composables/useGenerateGuard";
import { generateCooldown } from "@/composables/useGenerateGuard";
import { formatQuota } from "@/utils/format";

const props = defineProps<{
  notice?: GenerateNoticeInfo | null;
  quotaWarning?: QuotaWarning | null;
}>();

const emit = defineEmits<{ (e: "close"): void }>();

const { t } = useI18n();

const warningTitle = computed(() => {
  const w = props.quotaWarning;
  if (!w) return "";
  return t("generation.quotaWarning", {
    scope: t(`generation.quotaScope.${w.scope}`),
    percent: Math.round(Number(w.percent) || 0),
    used: formatQuota(w.used),
    limit: formatQuota(w.limit),
  });
});
</script>

<template>
  <div v-if="quotaWarning || notice" class="generate-notice">
    <el-alert v-if="quotaWarning" type="warning" :title="warningTitle" :closable="false" show-icon />
    <el-alert v-if="notice" :type="notice.type" :closable="true" show-icon @close="emit('close')">
      <template #title>
        <span>{{ notice.title }}</span>
        <span v-if="generateCooldown > 0 && notice.type === 'warning'" class="generate-notice__countdown">
          {{ t("generation.retryIn", { seconds: generateCooldown }) }}
        </span>
      </template>
      <div v-if="notice.description || notice.link" class="generate-notice__desc">
        <span v-if="notice.description">{{ notice.description }}</span>
        <router-link v-if="notice.link" :to="notice.link.to" class="generate-notice__link">{{ notice.link.label }}</router-link>
      </div>
    </el-alert>
  </div>
</template>

<style scoped>
.generate-notice {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}
.generate-notice__countdown {
  margin-left: 8px;
  font-weight: 600;
}
.generate-notice__desc {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  line-height: 1.5;
}
.generate-notice__link {
  color: var(--el-color-primary);
}
</style>
