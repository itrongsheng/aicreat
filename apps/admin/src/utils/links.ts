// 回填链接与平台规则的前端工具（docs/11 §4.1、§4.2、§5.2、§5.3、§11）：URL 语法预检、发布时间范围、批量粘贴解析、
// 平台显示名、SimHash 十六进制、超期未收录判定、平台 fetch_config 白名单校验。均为交互预检，以服务端校验为准。
import {
  LINK_LIMITS,
  PLATFORM_ALLOWED_HEADERS,
  PLATFORM_FETCH_CONFIG_KEYS,
  PLATFORM_FORBIDDEN_HEADERS,
  PLATFORM_LIMITS,
  type LinkCreateBody,
  type Platform,
  type PublishLink,
} from "@aicreat/shared";
import { getLocale, t } from "@/i18n";
import { toUtcIso } from "@/utils/format";

type PlatformLike = Pick<Platform, "code" | "name"> & { name_en?: string | null };

/** 平台显示名：英文界面优先 `name_en` */
export function platformLabel(p: PlatformLike | null | undefined, fallbackId?: number | null): string {
  if (!p) return fallbackId ? `#${fallbackId}` : "-";
  if (getLocale() === "en-US" && p.name_en) return p.name_en;
  return p.name || p.code;
}

/** 去掉 scheme 的缩略 URL（列表展示） */
export function shortUrl(url: string | null | undefined, max = 60): string {
  if (!url) return "-";
  const s = url.replace(/^https?:\/\//i, "");
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** 回填 URL 语法预检（与 safe_fetch.normalize_public_url 的语法级规则一致，不做 DNS）；通过返回 null，否则返回错误文案 */
export function checkPublicUrl(value: string): string | null {
  const raw = value.trim();
  if (!raw) return t("links.validate.urlRequired");
  if (raw.length > 2000) return t("links.validate.urlTooLong");
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return t("links.validate.urlInvalid");
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return t("links.validate.urlScheme");
  if (u.username || u.password) return t("links.validate.urlUserinfo");
  if (u.port && u.port !== "80" && u.port !== "443") return t("links.validate.urlPort");
  if (!u.hostname) return t("links.validate.urlInvalid");
  return null;
}

/** `published_at` 允许范围：[now − 3650 天, now + 5 分钟] */
export function publishedAtBounds(now = Date.now()): { min: number; max: number } {
  return {
    min: now - LINK_LIMITS.publishedAtPastDays * 86_400_000,
    max: now + LINK_LIMITS.publishedAtFutureMinutes * 60_000,
  };
}

/** 发布时间范围校验；空值允许（服务端取当前时间） */
export function checkPublishedAt(value: Date | string | number | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return t("links.validate.publishedAtInvalid");
  const { min, max } = publishedAtBounds();
  if (d.getTime() > max || d.getTime() < min) return t("links.validate.publishedAtRange", { days: LINK_LIMITS.publishedAtPastDays });
  return null;
}

/** el-date-picker 的 disabled-date：晚于今天或早于 3650 天前的日期不可选 */
export function disabledPublishDate(date: Date): boolean {
  const { min, max } = publishedAtBounds();
  const dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const dayEnd = dayStart + 86_400_000 - 1;
  return dayStart > max || dayEnd < min;
}

/** 解析粘贴的发布时间：ISO（带时区）按原值；`YYYY-MM-DD[ HH:mm[:ss]]`（`-` 或 `/`）按本地时间 */
export function parsePublishedAt(text: string): Date | null {
  const s = text.trim();
  if (!s) return null;
  const local = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/.exec(s);
  if (local) {
    const [, y, mo, d, h, mi, se] = local;
    const date = new Date(Number(y), Number(mo) - 1, Number(d), Number(h ?? 0), Number(mi ?? 0), Number(se ?? 0));
    if (date.getMonth() !== Number(mo) - 1 || date.getDate() !== Number(d)) return null;
    return Number.isNaN(date.getTime()) ? null : date;
  }
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/i.test(s)) {
    const date = new Date(s);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  return null;
}

export interface ParsedBatchLine {
  /** 原文行号（从 1 开始） */
  line: number;
  body: Omit<LinkCreateBody, "content_id">;
}

export interface BatchParseError {
  line: number;
  text: string;
  message: string;
}

/**
 * 批量粘贴解析：每行「URL[,平台 code][,账号][,发布时间]」；含 Tab 的行按 Tab 分列（便于从表格粘贴、URL 含逗号时使用），
 * 否则按半角 / 全角逗号分列。平台 code 留空则由服务端自动识别；空行忽略。
 */
export function parseBatchLines(text: string, platforms: Platform[]): { items: ParsedBatchLine[]; errors: BatchParseError[] } {
  const byCode = new Map(platforms.map((p) => [p.code.toLowerCase(), p]));
  const items: ParsedBatchLine[] = [];
  const errors: BatchParseError[] = [];
  text.split(/\r?\n/).forEach((rawLine, idx) => {
    const lineText = rawLine.trim();
    if (!lineText) return;
    const line = idx + 1;
    const cols = (lineText.includes("\t") ? lineText.split("\t") : lineText.split(/[,，]/)).map((c) => c.trim());
    const [url = "", code = "", account = "", published = ""] = cols;
    const fail = (message: string) => errors.push({ line, text: lineText, message });
    if (cols.length > 4) return fail(t("links.batch.tooManyColumns"));
    const urlError = checkPublicUrl(url);
    if (urlError) return fail(urlError);
    const body: ParsedBatchLine["body"] = { url };
    if (code) {
      const platform = byCode.get(code.toLowerCase());
      if (!platform) return fail(t("links.batch.unknownPlatform", { code }));
      if (!platform.is_active) return fail(t("links.batch.inactivePlatform", { code }));
      body.platform_id = platform.id;
    }
    if (account) {
      if (account.length > LINK_LIMITS.publishAccount) return fail(t("links.validate.accountTooLong", { max: LINK_LIMITS.publishAccount }));
      body.publish_account = account;
    }
    if (published) {
      const date = parsePublishedAt(published);
      if (!date) return fail(t("links.validate.publishedAtInvalid"));
      const rangeError = checkPublishedAt(date);
      if (rangeError) return fail(rangeError);
      body.published_at = toUtcIso(date) ?? null;
    }
    items.push({ line, body });
  });
  return { items, errors };
}

/** 有符号 64 位 SimHash → 16 位十六进制（JSON number 超过 2^53 时已有精度损失，仅用于展示） */
export function simhashHex(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  try {
    let n = BigInt(typeof value === "number" ? Math.trunc(value).toLocaleString("en-US", { useGrouping: false }) : String(value).trim());
    if (n < BigInt(0)) n += BigInt("18446744073709551616");
    return n.toString(16).padStart(16, "0");
  } catch {
    return String(value);
  }
}

/** 「超期未收录」标记（优先取接口的 index_overdue）：发布超过 overdueDays 天、任一 SEO 引擎都未收录、链接仍存活（docs/11 §13.1 `index_check.overdue_days`） */
export function isIndexOverdue(
  link: Pick<PublishLink, "published_at" | "seo_indexed_any" | "alive_status" | "index_overdue">,
  overdueDays: number = LINK_LIMITS.overdueDays,
): boolean {
  // 服务端已给出标记时以服务端为准（按 monitoring_config.index_check.overdue_days 计算）
  if (typeof link.index_overdue === "boolean") return link.index_overdue;
  if (link.seo_indexed_any) return false;
  if (link.alive_status !== "alive" && link.alive_status !== "changed") return false;
  const published = new Date(link.published_at).getTime();
  if (Number.isNaN(published)) return false;
  return Date.now() - published >= overdueDays * 86_400_000;
}

// ---------- 平台规则（docs/11 §5.2、§5.3） ----------

/** 平台 code：小写字母开头，小写字母 / 数字 / 下划线，≤ 32 字符 */
export const PLATFORM_CODE_PATTERN = /^[a-z][a-z0-9_]{1,31}$/;

/** 把 Python `re` 专有语法转换成 JS 等价写法，用于前端即时编译校验（服务端以 `re.compile` 为准） */
function pythonToJsRegex(pattern: string): { source: string; flags: string } {
  let source = pattern;
  let flags = "";
  const inline = /^\(\?([aiLmsux]+)\)/.exec(source);
  if (inline) {
    source = source.slice(inline[0].length);
    if (inline[1].includes("i")) flags += "i";
    if (inline[1].includes("m")) flags += "m";
    if (inline[1].includes("s")) flags += "s";
  }
  source = source
    .replace(/\(\?P<([A-Za-z_][A-Za-z0-9_]*)>/g, "(?<$1>")
    .replace(/\(\?P=([A-Za-z_][A-Za-z0-9_]*)\)/g, "\\k<$1>")
    .replace(/\\A/g, "^")
    .replace(/\\Z/g, "$");
  return { source, flags };
}

/** 正则编译校验：通过返回 null，否则返回「正则无法编译：…」 */
export function checkRegex(pattern: string): string | null {
  const p = pattern.trim();
  if (!p) return t("platforms.validate.emptyItem");
  if (p.length > PLATFORM_LIMITS.patternMax) return t("platforms.validate.patternTooLong", { max: PLATFORM_LIMITS.patternMax });
  try {
    const { source, flags } = pythonToJsRegex(p);
    new RegExp(source, flags.includes("i") ? flags : `${flags}i`);
    return null;
  } catch (err) {
    return t("platforms.validate.regexInvalid", { message: err instanceof Error ? err.message : String(err) });
  }
}

/** 删除特征文案：纯文本，单条 4~100 字符 */
export function checkMarker(marker: string): string | null {
  const m = marker.trim();
  if (!m) return t("platforms.validate.emptyItem");
  if (m.length < PLATFORM_LIMITS.markerMin || m.length > PLATFORM_LIMITS.markerMax) {
    return t("platforms.validate.markerLength", { min: PLATFORM_LIMITS.markerMin, max: PLATFORM_LIMITS.markerMax });
  }
  return null;
}

/** `fetch_config` 白名单校验（docs/11 §5.3）；返回错误文案列表（空数组表示通过） */
export function checkFetchConfig(value: unknown): string[] {
  const errors: string[] = [];
  if (value === null || value === undefined) return errors;
  if (typeof value !== "object" || Array.isArray(value)) return [t("platforms.validate.fetchConfigObject")];
  const cfg = value as Record<string, unknown>;
  const allowedKeys = PLATFORM_FETCH_CONFIG_KEYS as readonly string[];
  for (const key of Object.keys(cfg)) {
    if (!allowedKeys.includes(key)) errors.push(t("platforms.validate.fetchConfigKey", { key, keys: allowedKeys.join(" / ") }));
  }
  if ("user_agent" in cfg) {
    const ua = cfg.user_agent;
    if (ua !== null && typeof ua !== "string") errors.push(t("platforms.validate.userAgentType"));
    else if (typeof ua === "string" && ua !== "") {
      if (ua.length > PLATFORM_LIMITS.userAgentMax) errors.push(t("platforms.validate.userAgentTooLong", { max: PLATFORM_LIMITS.userAgentMax }));
      if (!ua.includes(PLATFORM_LIMITS.userAgentMarker)) errors.push(t("platforms.validate.userAgentMarker", { marker: PLATFORM_LIMITS.userAgentMarker }));
      if (/[\r\n]/.test(ua)) errors.push(t("platforms.validate.userAgentNewline"));
    }
  }
  if ("headers" in cfg && cfg.headers !== null) {
    const headers = cfg.headers;
    if (typeof headers !== "object" || Array.isArray(headers)) errors.push(t("platforms.validate.headersObject"));
    else {
      const entries = Object.entries(headers as Record<string, unknown>);
      if (entries.length > PLATFORM_LIMITS.headersMax) errors.push(t("platforms.validate.headersTooMany", { max: PLATFORM_LIMITS.headersMax }));
      const seen = new Set<string>();
      for (const [name, v] of entries) {
        const lower = name.trim().toLowerCase();
        if ((PLATFORM_FORBIDDEN_HEADERS as readonly string[]).includes(lower)) errors.push(t("platforms.validate.headerForbidden", { name }));
        else if (
          !/^[A-Za-z0-9-]+$/.test(name.trim()) ||
          (!(PLATFORM_ALLOWED_HEADERS as readonly string[]).includes(lower) && !(lower.startsWith("x-") && lower.length > 2))
        ) {
          errors.push(t("platforms.validate.headerNotAllowed", { name }));
        }
        if (seen.has(lower)) errors.push(t("platforms.validate.headerDuplicate", { name }));
        seen.add(lower);
        if (typeof v !== "string" || v.length > PLATFORM_LIMITS.headerValueMax || /[\r\n]/.test(v)) {
          errors.push(t("platforms.validate.headerValue", { name, max: PLATFORM_LIMITS.headerValueMax }));
        }
      }
    }
  }
  if ("timeout_seconds" in cfg && cfg.timeout_seconds !== null) {
    const n = Number(cfg.timeout_seconds);
    if (typeof cfg.timeout_seconds !== "number" || !Number.isFinite(n) || n < PLATFORM_LIMITS.timeoutMin || n > PLATFORM_LIMITS.timeoutMax) {
      errors.push(t("platforms.validate.timeout", { min: PLATFORM_LIMITS.timeoutMin, max: PLATFORM_LIMITS.timeoutMax }));
    }
  }
  for (const key of ["respect_robots", "allow_http"]) {
    if (key in cfg && cfg[key] !== null && typeof cfg[key] !== "boolean") errors.push(t("platforms.validate.boolean", { key }));
  }
  return errors;
}
