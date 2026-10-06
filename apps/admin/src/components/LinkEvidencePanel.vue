<script setup lang="ts">
// 删除检测证据展示（docs/11 §6.9；docs/04 §7.12）：固定键 marker / context / redirects / headers 与补充键 title / text_excerpt / short_text。
// 用于 EvidenceDrawer（link_checks.evidence）与平台规则测试结果（POST /platforms/{id}/test 的 evidence，同构）。
// 抓取内容视为不可信输入：只以文本插值渲染，不使用 v-html。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { LinkCheckEvidence } from "@aicreat/shared";

const props = defineProps<{ evidence: LinkCheckEvidence | null | undefined }>();

const { t } = useI18n();

/** 把上下文按命中文案（忽略大小写）切段，命中段用 <mark> 高亮 */
const contextSegments = computed(() => {
  const ctx = props.evidence?.context ?? "";
  const marker = props.evidence?.marker ?? "";
  if (!ctx) return [];
  if (!marker) return [{ text: ctx, hit: false }];
  const out: { text: string; hit: boolean }[] = [];
  const lower = ctx.toLowerCase();
  const needle = marker.toLowerCase();
  let pos = 0;
  while (pos < ctx.length) {
    const idx = lower.indexOf(needle, pos);
    if (idx < 0) {
      out.push({ text: ctx.slice(pos), hit: false });
      break;
    }
    if (idx > pos) out.push({ text: ctx.slice(pos, idx), hit: false });
    out.push({ text: ctx.slice(idx, idx + marker.length), hit: true });
    pos = idx + marker.length;
  }
  return out;
});

const headers = computed(() => Object.entries(props.evidence?.headers ?? {}).map(([name, value]) => ({ name, value: String(value) })));
const redirects = computed(() => (Array.isArray(props.evidence?.redirects) ? props.evidence!.redirects : []));
</script>

<template>
  <div v-if="evidence" class="evidence-panel">
    <el-descriptions :column="1" size="small" border>
      <el-descriptions-item :label="t('evidence.marker')">
        <el-tag v-if="evidence.marker" type="danger" size="small" disable-transitions>{{ evidence.marker }}</el-tag>
        <span v-else class="text-secondary">-</span>
      </el-descriptions-item>
      <el-descriptions-item :label="t('evidence.context')">
        <div v-if="contextSegments.length" class="evidence-text">
          <template v-for="(seg, i) in contextSegments" :key="i"><mark v-if="seg.hit">{{ seg.text }}</mark><template v-else>{{ seg.text }}</template></template>
        </div>
        <span v-else class="text-secondary">-</span>
      </el-descriptions-item>
      <el-descriptions-item :label="t('evidence.pageTitle')">{{ evidence.title || "-" }}</el-descriptions-item>
      <el-descriptions-item :label="t('evidence.textExcerpt')">
        <div v-if="evidence.text_excerpt" class="evidence-text">{{ evidence.text_excerpt }}</div>
        <span v-else class="text-secondary">-</span>
      </el-descriptions-item>
      <el-descriptions-item :label="t('evidence.shortText')">
        <el-tag v-if="evidence.short_text" type="warning" size="small" disable-transitions>{{ t("evidence.shortTextYes") }}</el-tag>
        <span v-else>{{ evidence.short_text === false ? t("common.no") : "-" }}</span>
      </el-descriptions-item>
      <el-descriptions-item :label="t('evidence.redirects')">
        <ol v-if="redirects.length" class="evidence-redirects">
          <li v-for="(u, i) in redirects" :key="i" class="mono">{{ u }}</li>
        </ol>
        <span v-else class="text-secondary">{{ t("evidence.noRedirects") }}</span>
      </el-descriptions-item>
    </el-descriptions>
    <div v-if="headers.length" class="evidence-headers">
      <div class="evidence-sub">{{ t("evidence.headers") }}</div>
      <el-table :data="headers" size="small" border>
        <el-table-column prop="name" :label="t('evidence.headerName')" width="180" />
        <el-table-column prop="value" :label="t('evidence.headerValue')" min-width="200">
          <template #default="{ row }"><span class="mono break">{{ row.value }}</span></template>
        </el-table-column>
      </el-table>
    </div>
  </div>
  <el-empty v-else :description="t('evidence.none')" :image-size="60" />
</template>

<style scoped>
.evidence-panel :deep(.el-descriptions__label) {
  width: 110px;
}
.evidence-text {
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.6;
  max-height: 220px;
  overflow: auto;
}
.evidence-text mark {
  padding: 0 2px;
  border-radius: 2px;
  background: var(--el-color-danger-light-7);
  color: inherit;
}
.evidence-redirects {
  margin: 0;
  padding-left: 18px;
  word-break: break-all;
}
.evidence-headers {
  margin-top: 12px;
}
.evidence-sub {
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
}
.break {
  word-break: break-all;
}
</style>
