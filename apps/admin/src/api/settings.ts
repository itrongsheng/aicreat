// /admin/settings（docs/04 §6.6、§7.18）
import type { RuntimeSettings, Setting, SettingLocale } from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { get, put } from "./client";

/** 配置键（docs/00 §「配置」） */
export const SETTING_KEYS = [
  "generation_config",
  "media_config",
  "monitoring_config",
  "geo_engines",
  "seo_providers",
  "alert_config",
  "ai_routing_config",
  "stats_config",
  "system_info",
] as const;
export type SettingKey = (typeof SETTING_KEYS)[number];

export interface SettingItemBody<V = unknown> {
  key: string;
  locale: SettingLocale;
  value: V;
}

/** 全部配置 [{key, locale, value}]，已与默认值深合并 */
export function listSettings(): Promise<Setting[]> {
  return get<Setting[]>("/admin/settings");
}

/** 单键；`system_info` 传 zh-CN / en-US，其它键默认 `*` */
export function getSetting<V = Record<string, unknown>>(key: string, locale: SettingLocale = "*"): Promise<Setting<V>> {
  return get<Setting<V>>(`/admin/settings/${key}`, { locale });
}

/** 单键保存，返回合并后的完整值；校验失败 400（data 为校验错误列表） */
export function saveSetting<V = Record<string, unknown>>(
  key: string,
  value: V,
  locale: SettingLocale = "*",
  config: AxiosRequestConfig = {},
): Promise<Setting<V>> {
  return put<Setting<V>>(`/admin/settings/${key}`, { locale, value }, config);
}

/** 批量保存，任一失败整体 400 */
export function saveSettings(items: SettingItemBody[], config: AxiosRequestConfig = {}): Promise<Setting[]> {
  return put<Setting[]>("/admin/settings", { items }, config);
}

/** 已登录即可读的非敏感运行时子集 */
export function runtime(): Promise<RuntimeSettings> {
  return get<RuntimeSettings>("/admin/settings/runtime");
}
