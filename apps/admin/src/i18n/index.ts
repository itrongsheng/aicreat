import { createI18n } from "vue-i18n";
import { SUPPORTED_LOCALES, type Locale } from "@aicreat/shared";
import zhCN, { type MessageSchema } from "./locales/zh-CN";
import enUS from "./locales/en-US";

const STORAGE_KEY = "aicreat.locale";

function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

/** 语言检测：localStorage → 浏览器语言（zh* → zh-CN，其它 → en-US） */
function detectLocale(): Locale {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (isLocale(stored)) return stored;
  } catch {
    /* localStorage 不可用时按浏览器语言 */
  }
  const nav = typeof navigator !== "undefined" ? navigator.language || "" : "";
  return nav.toLowerCase().startsWith("zh") ? "zh-CN" : "en-US";
}

const initial = detectLocale();

export const i18n = createI18n<[MessageSchema], Locale, false>({
  legacy: false,
  locale: initial,
  fallbackLocale: "zh-CN",
  messages: { "zh-CN": zhCN, "en-US": enUS },
  missingWarn: false,
  fallbackWarn: false,
});

if (typeof document !== "undefined") document.documentElement.lang = initial;

export function getLocale(): Locale {
  return i18n.global.locale.value as Locale;
}

/** 切换界面语言并持久化（请求的 lang 参数随之变化） */
export function setLocale(locale: Locale): void {
  if (!isLocale(locale)) return;
  i18n.global.locale.value = locale;
  try {
    localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    /* ignore */
  }
  document.documentElement.lang = locale;
}

/** 组件外（store、api、router）使用的翻译函数 */
export function t(key: string, named?: Record<string, unknown>): string {
  return named ? i18n.global.t(key, named) : i18n.global.t(key);
}

/** 词条是否存在（StatusTag 等按枚举值动态取文案时使用） */
export function te(key: string): boolean {
  return i18n.global.te(key);
}
