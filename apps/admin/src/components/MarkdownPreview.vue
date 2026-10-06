<script setup lang="ts">
// Markdown / HTML 预览（docs/02 §3.6、docs/09 §8.5、§10.6）：markdown-it 渲染 + DOMPurify 白名单清理（utils/markdown.ts）；
// HTML 格式只做清理。所有输出都经 sanitizeHtml，模型输出与业务数据一律视为不可信。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { ContentFormat } from "@aicreat/shared";
import { renderMarkdown, sanitizeHtml } from "@/utils/markdown";

const props = withDefaults(
  defineProps<{
    source: string | null | undefined;
    format?: ContentFormat;
    /** 最大高度（超出滚动），如 "480px"；不传则随内容增高 */
    maxHeight?: string;
    /** 空内容时的提示 */
    emptyText?: string;
    bordered?: boolean;
  }>(),
  { format: "markdown", maxHeight: undefined, emptyText: "", bordered: false },
);

const { t } = useI18n();

const html = computed(() => {
  const src = props.source ?? "";
  if (!src.trim()) return "";
  return props.format === "html" ? sanitizeHtml(src) : renderMarkdown(src);
});
</script>

<template>
  <div class="md-preview" :class="{ 'is-bordered': bordered }" :style="maxHeight ? { maxHeight, overflowY: 'auto' } : undefined">
    <!-- eslint-disable-next-line vue/no-v-html -- 已经 DOMPurify 白名单清理 -->
    <div v-if="html" class="md-preview__body" v-html="html" />
    <div v-else class="md-preview__empty">{{ emptyText || t("markdown.empty") }}</div>
  </div>
</template>

<style scoped>
.md-preview {
  min-width: 0;
  color: var(--el-text-color-primary);
  font-size: 14px;
  line-height: 1.75;
  word-break: break-word;
}
.md-preview.is-bordered {
  padding: 12px 16px;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--el-bg-color);
}
.md-preview__empty {
  padding: 24px 0;
  color: var(--el-text-color-placeholder);
  text-align: center;
}
.md-preview__body :deep(h1),
.md-preview__body :deep(h2),
.md-preview__body :deep(h3),
.md-preview__body :deep(h4) {
  margin: 1.2em 0 0.6em;
  line-height: 1.4;
  font-weight: 600;
}
.md-preview__body :deep(h1) {
  font-size: 1.6em;
}
.md-preview__body :deep(h2) {
  font-size: 1.35em;
  padding-bottom: 0.3em;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.md-preview__body :deep(h3) {
  font-size: 1.15em;
}
.md-preview__body :deep(:first-child) {
  margin-top: 0;
}
.md-preview__body :deep(p) {
  margin: 0.6em 0;
}
.md-preview__body :deep(ul),
.md-preview__body :deep(ol) {
  margin: 0.6em 0;
  padding-left: 1.6em;
}
.md-preview__body :deep(blockquote) {
  margin: 0.8em 0;
  padding: 0.2em 1em;
  border-left: 4px solid var(--el-border-color);
  color: var(--el-text-color-secondary);
  background: var(--el-fill-color-lighter);
}
.md-preview__body :deep(code) {
  padding: 0.1em 0.4em;
  border-radius: 3px;
  background: var(--el-fill-color);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 0.9em;
}
.md-preview__body :deep(pre) {
  overflow-x: auto;
  padding: 12px;
  border-radius: 4px;
  background: var(--el-fill-color);
}
.md-preview__body :deep(pre code) {
  padding: 0;
  background: transparent;
}
.md-preview__body :deep(table) {
  border-collapse: collapse;
  margin: 0.8em 0;
  max-width: 100%;
  display: block;
  overflow-x: auto;
}
.md-preview__body :deep(th),
.md-preview__body :deep(td) {
  padding: 6px 12px;
  border: 1px solid var(--el-border-color);
}
.md-preview__body :deep(th) {
  background: var(--el-fill-color-light);
}
.md-preview__body :deep(img),
.md-preview__body :deep(video) {
  max-width: 100%;
  height: auto;
  border-radius: 4px;
}
.md-preview__body :deep(a) {
  color: var(--el-color-primary);
}
.md-preview__body :deep(hr) {
  border: none;
  border-top: 1px solid var(--el-border-color);
  margin: 1.2em 0;
}
</style>
