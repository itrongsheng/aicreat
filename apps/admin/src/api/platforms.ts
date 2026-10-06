// /admin/platforms（docs/04 §6.16；docs/11 §4.3、§5）。路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type { Platform, PlatformCreateBody, PlatformDetectResult, PlatformTestResult, PlatformUpdateBody } from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put } from "./client";

/** 不分页；`is_active` 筛选；每项含 `link_count`（只统计调用者可见的链接） */
export function list(params: { is_active?: boolean } = {}, config: AxiosRequestConfig = {}): Promise<Platform[]> {
  const query: Record<string, unknown> = {};
  if (params.is_active !== undefined) query.is_active = params.is_active ? 1 : 0;
  return httpGet<Platform[]>("/admin/platforms", query, config);
}

/** 详情（含规则 JSON） */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<Platform> {
  return httpGet<Platform>(`/admin/platforms/${id}`, undefined, config);
}

/** 新建；`code` 唯一且创建后不可改；正则 / 文案 / `fetch_config` 校验失败 400（`loc` 指向具体条目） */
export function create(body: PlatformCreateBody, config: AxiosRequestConfig = {}): Promise<Platform> {
  return post<Platform>("/admin/platforms", body, config);
}

/** 编辑规则 / 状态（停用：`{is_active:false}`）；`fetch_config.headers` 白名单校验失败 400 */
export function update(id: number, body: PlatformUpdateBody, config: AxiosRequestConfig = {}): Promise<Platform> {
  return put<Platform>(`/admin/platforms/${id}`, body, config);
}

/** 删除：非系统平台且无链接引用，否则 409 `reason=in_use` */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/platforms/${id}`, undefined, config);
}

/** 规则测试：实时抓取一次并按该平台规则判定（不写库，计入 `limit:link_checks:{date}`）；同步抓取，超时放宽到 60s */
export function testRule(id: number, url: string, config: AxiosRequestConfig = {}): Promise<PlatformTestResult> {
  return post<PlatformTestResult>(`/admin/platforms/${id}/test`, { url }, { timeout: 60_000, ...config });
}

/** 按 `url_patterns` 识别平台（未命中返回 `website`）；无副作用、不记审计 */
export function detect(url: string, config: AxiosRequestConfig = {}): Promise<PlatformDetectResult> {
  return post<PlatformDetectResult>("/admin/platforms/detect", { url }, config);
}
