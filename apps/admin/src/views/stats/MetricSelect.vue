<script setup lang="ts">
// 报表指标多选（docs/12 §6.1、§6.3）：候选按 §3.2 分组（内容生产 / 链接与存活 / 收录 / AI 调用与消耗 / 媒体 / 告警），
// 并按 §4.2 维度 × 列矩阵过滤掉当前维度不支持的指标；最多 max 个（默认 8）；存量口径引擎指标带「存量口径」标识。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import type { BreakdownDimension, StatsDimension } from "@aicreat/shared";
import { MAX_METRICS, METRIC_GROUP_ORDER, metricsFor } from "@/api/stats";

const props = withDefaults(
  defineProps<{
    modelValue: string[];
    dimension: StatsDimension | BreakdownDimension;
    max?: number;
    width?: string;
    placeholder?: string;
  }>(),
  { max: MAX_METRICS, width: "360px", placeholder: "" },
);

const emit = defineEmits<{ (e: "update:modelValue", value: string[]): void }>();

const { t } = useI18n();

const STOCK = new Set(["seo_index_rate_by_engine", "geo_cite_rate_by_engine"]);

const groups = computed(() => {
  const specs = metricsFor(props.dimension);
  return METRIC_GROUP_ORDER.map((g) => ({ group: g, items: specs.filter((s) => s.group === g) })).filter((g) => g.items.length);
});

const value = computed<string[]>({
  get: () => props.modelValue,
  set: (v) => {
    if (v.length > props.max) {
      ElMessage.warning(t("stats.filters.maxMetrics", { max: props.max }));
      return;
    }
    emit("update:modelValue", v);
  },
});
</script>

<template>
  <el-select
    v-model="value"
    multiple
    filterable
    collapse-tags
    collapse-tags-tooltip
    :max-collapse-tags="3"
    :placeholder="placeholder || t('stats.filters.metrics')"
    :style="{ width }"
  >
    <el-option-group v-for="g in groups" :key="g.group" :label="t(`stats.groups.${g.group}`)">
      <el-option
        v-for="item in g.items"
        :key="item.key"
        :value="item.key"
        :label="t(`stats.metrics.${item.key}`)"
        :disabled="!modelValue.includes(item.key) && modelValue.length >= max"
      >
        <span>{{ t(`stats.metrics.${item.key}`) }}</span>
        <el-tag v-if="STOCK.has(item.key)" size="small" type="info" effect="plain" class="metric-tag" disable-transitions>{{ t("stats.caliber.stock") }}</el-tag>
        <span class="metric-key mono">{{ item.key }}</span>
      </el-option>
    </el-option-group>
  </el-select>
</template>

<style scoped>
.metric-tag {
  margin-left: 6px;
}
.metric-key {
  float: right;
  margin-left: 12px;
  color: var(--el-text-color-placeholder);
}
</style>
