<script setup lang="ts">
// 链接按引擎的 SEO 收录 / GEO 引用小徽标（docs/11 §11.2）：键为引擎 code，颜色与 StatusTag 的 seo_index_status / geo_cite_status 一致；
// `seo_indexed_any` / `geo_cited_any` 为真时整组高亮；hover 显示 checked_at 与 first_indexed_at / first_cited_at。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { CircleCheckFilled } from "@element-plus/icons-vue";
import type { GeoEngineStatus, IndexKind, PublishLink, SeoEngineStatus } from "@aicreat/shared";
import { statusLabel, statusTagType } from "@/components/StatusTag.vue";
import { engineLabel } from "@/composables/useIndexEngines";
import { formatDateTime } from "@/utils/format";

const props = withDefaults(
  defineProps<{
    link: Pick<PublishLink, "seo_status" | "geo_status" | "seo_indexed_any" | "geo_cited_any">;
    kind: IndexKind;
    /** 自定义引擎显示名（如 useIndexEngines().label） */
    labeler?: (kind: IndexKind, code: string) => string;
  }>(),
  { labeler: undefined },
);

const { t, locale } = useI18n();

interface Badge {
  code: string;
  label: string;
  status: string;
  checkedAt: string | null;
  firstAt: string | null;
  checkCount: number;
}

const enumName = computed(() => (props.kind === "seo" ? "seo_index_status" : "geo_cite_status") as "seo_index_status" | "geo_cite_status");
const any = computed(() => (props.kind === "seo" ? props.link.seo_indexed_any : props.link.geo_cited_any));

const badges = computed<Badge[]>(() => {
  void locale.value;
  const map = (props.kind === "seo" ? props.link.seo_status : props.link.geo_status) ?? {};
  return Object.entries(map as Record<string, SeoEngineStatus | GeoEngineStatus | undefined>)
    .filter((entry): entry is [string, SeoEngineStatus | GeoEngineStatus] => !!entry[1])
    .map(([code, st]) => ({
      code,
      label: props.labeler ? props.labeler(props.kind, code) : engineLabel(props.kind, code),
      status: st.status || "unknown",
      checkedAt: st.checked_at ?? null,
      firstAt: "first_indexed_at" in st ? st.first_indexed_at : "first_cited_at" in st ? st.first_cited_at : null,
      checkCount: st.check_count ?? 0,
    }))
    .sort((a, b) => a.code.localeCompare(b.code));
});
</script>

<template>
  <div class="index-badges" :class="{ 'is-any': any }">
    <el-icon v-if="any" class="index-badges__any" :title="kind === 'seo' ? t('links.indexedAny') : t('links.citedAny')"><CircleCheckFilled /></el-icon>
    <template v-if="badges.length">
      <el-tooltip v-for="b in badges" :key="b.code" placement="top" :show-after="200">
        <template #content>
          <div>{{ b.label }}：{{ statusLabel(enumName, b.status) }}</div>
          <div>{{ t("links.checkedAt") }}：{{ formatDateTime(b.checkedAt) }}</div>
          <div>{{ kind === "seo" ? t("links.firstIndexedAt") : t("links.firstCitedAt") }}：{{ formatDateTime(b.firstAt) }}</div>
          <div>{{ t("links.engineCheckCount") }}：{{ b.checkCount }}</div>
        </template>
        <el-tag :type="statusTagType(enumName, b.status)" size="small" effect="light" disable-transitions class="index-badges__tag">{{ b.label }}</el-tag>
      </el-tooltip>
    </template>
    <span v-else class="index-badges__empty">-</span>
  </div>
</template>

<style scoped>
.index-badges {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.index-badges__any {
  color: var(--el-color-success);
}
.index-badges__tag {
  cursor: default;
}
.index-badges__empty {
  color: var(--el-text-color-placeholder);
}
</style>
