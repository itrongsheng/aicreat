// Markdown 渲染（markdown-it）与 HTML 白名单清理（DOMPurify）
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

const md = new MarkdownIt({ html: true, linkify: true, breaks: false, typographer: false });

const ALLOWED_TAGS = [
  "a", "abbr", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl", "dt", "em", "figcaption", "figure",
  "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "s", "section",
  "small", "span", "strong", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul", "video", "source",
];
const ALLOWED_ATTR = [
  "href", "title", "alt", "src", "width", "height", "align", "colspan", "rowspan", "class", "id", "start", "type",
  "controls", "poster", "preload", "target", "rel", "loading",
];

let hooked = false;
function ensureHooks() {
  if (hooked) return;
  hooked = true;
  // 外链统一新窗口打开并加 rel
  DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A" && node.getAttribute("href")) {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer nofollow");
    }
  });
}

/** HTML 白名单清理（仅允许 http(s) / mailto / 相对地址与 data:image） */
export function sanitizeHtml(html: string): string {
  ensureHooks();
  return DOMPurify.sanitize(html ?? "", {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|data:image\/(?:png|jpe?g|gif|webp);|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i,
  });
}

/** Markdown → 安全 HTML */
export function renderMarkdown(source: string | null | undefined): string {
  return sanitizeHtml(md.render(source ?? ""));
}

const CJK_RE = /[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]/g;

/** 去掉 Markdown 标记语法（标题符号、强调符号、链接保留文字、图片整体移除、代码围栏符号） */
function stripMarkdown(source: string): string {
  return source
    .replace(/^\s*(```|~~~).*$/gm, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s*([-*+]|\d+\.)\s+/gm, "")
    .replace(/[*_~`]+/g, "");
}

/** 去 HTML 标签与常见实体 */
function stripHtml(source: string): string {
  return source
    .replace(/<(script|style)[\s\S]*?<\/\1>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&[a-z]+;|&#\d+;/gi, " ");
}

/**
 * 字数（与后端 `content_service.count_words` 口径一致，docs/09 §8.9）：
 * Markdown 去标记 / HTML 去标签后，CJK 字符每个计 1，其它按空白切分的词每个计 1。
 */
export function countWords(body: string | null | undefined, format: "markdown" | "html" = "markdown"): number {
  if (!body) return 0;
  const text = format === "html" ? stripHtml(body) : stripMarkdown(body);
  const cjk = text.match(CJK_RE)?.length ?? 0;
  const rest = text.replace(CJK_RE, " ").split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)).length;
  return cjk + rest;
}
