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
