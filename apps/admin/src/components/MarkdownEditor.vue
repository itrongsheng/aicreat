<script setup lang="ts">
// 正文 / 提示词编辑器（docs/02 §3.6、docs/09 §10.2、§10.6）：原生 textarea + 格式工具条（Markdown 或 HTML 源码），
// 快捷键 Ctrl/⌘+B / I / K，Tab 缩进；暴露 insert / wrap / focus / getSelection 供父组件在光标处插入素材、变量等片段。
import { computed, nextTick, ref, type Component } from "vue";
import { Grid, Link, Minus, Picture } from "@element-plus/icons-vue";
import { useI18n } from "vue-i18n";
import type { ContentFormat } from "@aicreat/shared";
import { countWords } from "@/utils/markdown";

type Action = "h2" | "h3" | "bold" | "italic" | "quote" | "ul" | "ol" | "link" | "image" | "code" | "codeBlock" | "table" | "hr";

const props = withDefaults(
  defineProps<{
    modelValue: string | null | undefined;
    /** markdown：Markdown 工具条；html：插入 HTML 标签；text：纯文本（无工具条，如 Prompt 正文） */
    format?: ContentFormat | "text";
    rows?: number;
    /** 自适应高度上限（行），0 表示固定 rows */
    maxRows?: number;
    readonly?: boolean;
    disabled?: boolean;
    placeholder?: string;
    maxlength?: number;
    toolbar?: boolean;
    /** 底部字数统计（Markdown / HTML 按 count_words 口径，text 为字符数） */
    showCount?: boolean;
    /** 等宽字体（Prompt、HTML 源码） */
    monospace?: boolean;
    invalid?: boolean;
  }>(),
  {
    format: "markdown",
    rows: 16,
    maxRows: 0,
    readonly: false,
    disabled: false,
    placeholder: "",
    maxlength: undefined,
    toolbar: true,
    showCount: true,
    monospace: false,
    invalid: false,
  },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: string): void;
  (e: "change", value: string): void;
  (e: "blur", event: FocusEvent): void;
}>();

const { t } = useI18n();
const textarea = ref<HTMLTextAreaElement>();
const value = computed(() => props.modelValue ?? "");
const editable = computed(() => !props.readonly && !props.disabled);
const showToolbar = computed(() => props.toolbar && props.format !== "text" && editable.value);

const count = computed(() => {
  if (props.format === "text") return value.value.length;
  return countWords(value.value, props.format);
});

const style = computed(() => {
  if (!props.maxRows) return undefined;
  return { maxHeight: `${props.maxRows * 1.6 + 1.2}em` };
});

function onInput(event: Event) {
  const next = (event.target as HTMLTextAreaElement).value;
  emit("update:modelValue", next);
}

function onChange(event: Event) {
  emit("change", (event.target as HTMLTextAreaElement).value);
}

/** 用 next 替换 [start, end) 并把选区设置为 [selStart, selEnd)（相对插入起点） */
async function replaceRange(start: number, end: number, next: string, selStart = next.length, selEnd = selStart) {
  const text = value.value;
  const merged = text.slice(0, start) + next + text.slice(end);
  if (props.maxlength && merged.length > props.maxlength) return;
  emit("update:modelValue", merged);
  emit("change", merged);
  await nextTick();
  const el = textarea.value;
  if (!el) return;
  el.focus();
  el.setSelectionRange(start + selStart, start + selEnd);
}

function selection(): { start: number; end: number; text: string } {
  const el = textarea.value;
  const start = el?.selectionStart ?? value.value.length;
  const end = el?.selectionEnd ?? start;
  return { start, end, text: value.value.slice(start, end) };
}

/** 在光标处插入文本（替换当前选区）；块级片段自动补齐前后空行 */
async function insert(snippet: string, block = false): Promise<void> {
  if (!editable.value) return;
  const { start, end } = selection();
  let text = snippet;
  if (block) {
    const before = value.value.slice(0, start);
    const after = value.value.slice(end);
    if (before && !before.endsWith("\n\n")) text = (before.endsWith("\n") ? "\n" : "\n\n") + text;
    if (!after.startsWith("\n")) text += "\n\n";
    else if (!after.startsWith("\n\n")) text += "\n";
  }
  await replaceRange(start, end, text);
}

/** 用前后缀包裹选区；无选区时插入占位文字并选中 */
async function wrap(prefix: string, suffix: string, placeholder: string): Promise<void> {
  if (!editable.value) return;
  const { start, end, text } = selection();
  const inner = text || placeholder;
  await replaceRange(start, end, `${prefix}${inner}${suffix}`, prefix.length, prefix.length + inner.length);
}

/** 对选区所在的每一行加前缀（标题、引用、列表） */
async function prefixLines(make: (index: number) => string, placeholder: string): Promise<void> {
  if (!editable.value) return;
  const text = value.value;
  const sel = selection();
  const lineStart = text.lastIndexOf("\n", sel.start - 1) + 1;
  let lineEnd = text.indexOf("\n", sel.end);
  if (lineEnd < 0) lineEnd = text.length;
  const block = text.slice(lineStart, lineEnd) || placeholder;
  const next = block
    .split("\n")
    .map((line, i) => make(i) + line.replace(/^(#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+)/, ""))
    .join("\n");
  await replaceRange(lineStart, lineEnd, next, 0, next.length);
}

async function apply(action: Action) {
  const html = props.format === "html";
  switch (action) {
    case "h2":
      return html ? wrap("<h2>", "</h2>", t("markdown.heading")) : prefixLines(() => "## ", t("markdown.heading"));
    case "h3":
      return html ? wrap("<h3>", "</h3>", t("markdown.heading")) : prefixLines(() => "### ", t("markdown.heading"));
    case "bold":
      return html ? wrap("<strong>", "</strong>", t("markdown.bold")) : wrap("**", "**", t("markdown.bold"));
    case "italic":
      return html ? wrap("<em>", "</em>", t("markdown.italic")) : wrap("*", "*", t("markdown.italic"));
    case "quote":
      return html ? wrap("<blockquote>", "</blockquote>", t("markdown.quote")) : prefixLines(() => "> ", t("markdown.quote"));
    case "ul":
      return html ? wrap("<ul>\n  <li>", "</li>\n</ul>", t("markdown.listItem")) : prefixLines(() => "- ", t("markdown.listItem"));
    case "ol":
      return html ? wrap("<ol>\n  <li>", "</li>\n</ol>", t("markdown.listItem")) : prefixLines((i) => `${i + 1}. `, t("markdown.listItem"));
    case "link":
      return html ? wrap('<a href="https://">', "</a>", t("markdown.linkText")) : wrap("[", "](https://)", t("markdown.linkText"));
    case "image":
      return insert(html ? `<img src="https://" alt="${t("markdown.imageAlt")}" />` : `![${t("markdown.imageAlt")}](https://)`, true);
    case "code":
      return html ? wrap("<code>", "</code>", "code") : wrap("`", "`", "code");
    case "codeBlock":
      return html ? wrap("<pre><code>", "</code></pre>", "code") : wrap("```\n", "\n```", "code");
    case "table":
      return insert(
        html
          ? "<table>\n  <thead><tr><th>A</th><th>B</th></tr></thead>\n  <tbody><tr><td>1</td><td>2</td></tr></tbody>\n</table>"
          : "| A | B |\n| --- | --- |\n| 1 | 2 |",
        true,
      );
    case "hr":
      return insert(html ? "<hr />" : "---", true);
  }
}

function onKeydown(event: KeyboardEvent) {
  if (!editable.value) return;
  const mod = event.ctrlKey || event.metaKey;
  if (mod && props.format !== "text") {
    const key = event.key.toLowerCase();
    const map: Partial<Record<string, Action>> = { b: "bold", i: "italic", k: "link" };
    const action = map[key];
    if (action) {
      event.preventDefault();
      void apply(action);
      return;
    }
  }
  if (event.key === "Tab" && !mod && !event.altKey) {
    const { start, end, text } = selection();
    if (event.shiftKey) {
      // 反缩进：移除选区各行行首两个空格
      const full = value.value;
      const lineStart = full.lastIndexOf("\n", start - 1) + 1;
      const block = full.slice(lineStart, end);
      const next = block.replace(/^ {1,2}/gm, "");
      if (next !== block) {
        event.preventDefault();
        void replaceRange(lineStart, end, next, 0, next.length);
      }
      return;
    }
    event.preventDefault();
    if (text.includes("\n")) {
      const full = value.value;
      const lineStart = full.lastIndexOf("\n", start - 1) + 1;
      const block = full.slice(lineStart, end);
      const next = block.replace(/^/gm, "  ");
      void replaceRange(lineStart, end, next, 0, next.length);
    } else {
      void replaceRange(start, end, "  ");
    }
  }
}

function focus() {
  textarea.value?.focus();
}

defineExpose({ insert, wrap, focus, getSelection: selection });

const tools: { action: Action; label: string; text?: string; icon?: Component }[][] = [
  [
    { action: "h2", label: "markdown.h2", text: "H2" },
    { action: "h3", label: "markdown.h3", text: "H3" },
  ],
  [
    { action: "bold", label: "markdown.bold", text: "B" },
    { action: "italic", label: "markdown.italic", text: "I" },
    { action: "code", label: "markdown.code", text: "</>" },
  ],
  [
    { action: "quote", label: "markdown.quote", text: "“”" },
    { action: "ul", label: "markdown.ul", text: "•" },
    { action: "ol", label: "markdown.ol", text: "1." },
  ],
  [
    { action: "link", label: "markdown.link", icon: Link },
    { action: "image", label: "markdown.image", icon: Picture },
    { action: "table", label: "markdown.table", icon: Grid },
    { action: "codeBlock", label: "markdown.codeBlock", text: "{ }" },
    { action: "hr", label: "markdown.hr", icon: Minus },
  ],
];
</script>

<template>
  <div class="md-editor" :class="{ 'is-disabled': disabled, 'is-readonly': readonly, 'is-invalid': invalid }">
    <div v-if="showToolbar" class="md-editor__toolbar">
      <template v-for="(group, gi) in tools" :key="gi">
        <span v-if="gi > 0" class="md-editor__sep" />
        <el-tooltip v-for="tool in group" :key="tool.action" :content="t(tool.label)" placement="top" :show-after="400">
          <button type="button" class="md-editor__tool" :class="`is-${tool.action}`" @mousedown.prevent @click="apply(tool.action)">
            <el-icon v-if="tool.icon"><component :is="tool.icon" /></el-icon>
            <template v-else>{{ tool.text }}</template>
          </button>
        </el-tooltip>
      </template>
      <span class="md-editor__spacer" />
      <slot name="toolbar-extra" />
    </div>
    <textarea
      ref="textarea"
      class="md-editor__input"
      :class="{ 'is-mono': monospace || format === 'html' }"
      :style="style"
      :value="value"
      :rows="rows"
      :readonly="readonly"
      :disabled="disabled"
      :placeholder="placeholder"
      :maxlength="maxlength"
      spellcheck="false"
      @input="onInput"
      @change="onChange"
      @keydown="onKeydown"
      @blur="emit('blur', $event)"
    />
    <div v-if="showCount" class="md-editor__footer">
      <slot name="footer" />
      <span class="md-editor__spacer" />
      <span>{{ format === "text" ? t("markdown.chars", { count }) : t("markdown.words", { count }) }}</span>
      <span v-if="maxlength">&nbsp;/ {{ maxlength }}</span>
    </div>
  </div>
</template>

<style scoped>
.md-editor {
  display: flex;
  flex-direction: column;
  width: 100%;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  background: var(--el-bg-color);
  transition: border-color 0.2s;
}
.md-editor:focus-within {
  border-color: var(--el-color-primary);
}
.md-editor.is-invalid {
  border-color: var(--el-color-danger);
}
.md-editor.is-disabled {
  background: var(--el-disabled-bg-color);
}
.md-editor__toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px;
  padding: 4px 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.md-editor__tool {
  min-width: 28px;
  height: 26px;
  padding: 0 6px;
  border: none;
  border-radius: 3px;
  background: transparent;
  color: var(--el-text-color-regular);
  font-size: 13px;
  cursor: pointer;
}
.md-editor__tool {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.md-editor__tool:hover {
  background: var(--el-fill-color);
  color: var(--el-color-primary);
}
.md-editor__tool.is-bold {
  font-weight: 700;
}
.md-editor__tool.is-italic {
  font-style: italic;
}
.md-editor__sep {
  width: 1px;
  height: 16px;
  margin: 0 4px;
  background: var(--el-border-color-lighter);
}
.md-editor__spacer {
  flex: 1;
}
.md-editor__input {
  flex: 1;
  width: 100%;
  box-sizing: border-box;
  min-height: 6em;
  padding: 8px 12px;
  border: none;
  outline: none;
  resize: vertical;
  background: transparent;
  color: var(--el-text-color-primary);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
}
.md-editor__input.is-mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 13px;
}
.md-editor__input::placeholder {
  color: var(--el-text-color-placeholder);
}
.md-editor__input:disabled {
  cursor: not-allowed;
  color: var(--el-text-color-disabled);
}
.md-editor__footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 2px 10px 4px;
  border-top: 1px solid var(--el-border-color-extra-light);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
