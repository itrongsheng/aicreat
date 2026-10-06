// /admin/links（docs/04 §6.17、§7.10~§7.12；docs/11 §4、§6.7、§6.8、§7.4）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  IndexCheck,
  IndexKind,
  LinkAliveStatus,
  LinkBatchResult,
  LinkCheck,
  LinkCreateBody,
  LinkCreateResult,
  LinkIndexCheckBody,
  LinkMarkIndexBody,
  LinkQueuedResult,
  LinkUpdateBody,
  Page,
  PublishLink,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put, requestBlob, type DownloadResult } from "./client";

export interface ListLinksParams {
  page?: number;
  page_size?: number;
  project_id?: number;
  content_id?: number;
  platform_id?: number;
  alive_status?: LinkAliveStatus;
  seo_indexed_any?: boolean;
  geo_cited_any?: boolean;
  is_monitoring?: boolean;
  /** URL / 标题模糊 */
  keyword?: string;
  /** ISO 8601 UTC */
  published_start?: string;
  published_end?: string;
}

export interface ListIndexChecksParams {
  page?: number;
  page_size?: number;
  kind?: IndexKind;
  engine?: string;
}

function clean(params: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = typeof v === "boolean" ? (v ? 1 : 0) : v;
  }
  return out;
}

/** 分页列表（`project_id` 为 0 / 缺省时为全部可见项目） */
export function list(params: ListLinksParams = {}, config: AxiosRequestConfig = {}): Promise<Page<PublishLink>> {
  return httpGet<Page<PublishLink>>("/admin/links", clean({ ...params, project_id: params.project_id || undefined }), config);
}

/** 详情（含 `platform`、`content` 摘要、基线、按引擎收录状态、`last_check`） */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<PublishLink> {
  return httpGet<PublishLink>(`/admin/links/${id}`, undefined, config);
}

/**
 * 单条回填 → `{link, queued}`。URL 预校验 / `published_at` 范围失败 400（`loc=["body","url"|"published_at"]`）；
 * `url_hash` 重复 409 `data.existing_id`（不可见时 `existing_id=null, reason=owned_by_other`）；内容非 approved/published 409 `current_status`。
 */
export function backfill(body: LinkCreateBody, config: AxiosRequestConfig = {}): Promise<LinkCreateResult> {
  return post<LinkCreateResult>("/admin/links", body, config);
}

/** 批量回填（≤ 100 条，逐条独立事务，整体 200）→ `{created, failed, results[]}` */
export function batchBackfill(items: LinkCreateBody[], config: AxiosRequestConfig = {}): Promise<LinkBatchResult> {
  return post<LinkBatchResult>("/admin/links/batch", { items }, { timeout: 120_000, ...config });
}

/** 编辑 `platform_id` / `publish_account` / `published_at` / `note`（URL 不可改） */
export function update(id: number, body: LinkUpdateBody, config: AxiosRequestConfig = {}): Promise<PublishLink> {
  return put<PublishLink>(`/admin/links/${id}`, body, config);
}

/** 删除（级联检测记录；内容 `link_count` 归零时 `published → approved`） */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/links/${id}`, undefined, config);
}

/** 立即删除检测（`manual` 插队）→ `{queued:true}` / `{queued:false, reason:"already_queued"}` */
export function check(id: number, config: AxiosRequestConfig = {}): Promise<LinkQueuedResult> {
  return post<LinkQueuedResult>(`/admin/links/${id}/check`, undefined, config);
}

/**
 * 立即收录检测 → `{queued}` / `{queued:false, reason:"already_queued"|"daily_limit"}`；
 * 未启用引擎 400；`deleted` / 暂停监控 409；每管理员 30/hour → 429。
 */
export function indexCheck(id: number, body: LinkIndexCheckBody, config: AxiosRequestConfig = {}): Promise<LinkQueuedResult> {
  return post<LinkQueuedResult>(`/admin/links/${id}/index-check`, body, config);
}

/** 人工标记收录 / 引用（`provider=manual`），返回更新后的链接 */
export function markIndex(id: number, body: LinkMarkIndexBody, config: AxiosRequestConfig = {}): Promise<PublishLink> {
  return post<PublishLink>(`/admin/links/${id}/mark-index`, body, config);
}

/** 清空基线并入队 `manual` 检测（下次 200 重建基线并写 `alive`） */
export function rebaseline(id: number, config: AxiosRequestConfig = {}): Promise<LinkQueuedResult> {
  return post<LinkQueuedResult>(`/admin/links/${id}/rebaseline`, undefined, config);
}

/** 暂停监控：`is_monitoring=0`，`next_check_at` / `next_index_check_at` 置空 */
export function pause(id: number, config: AxiosRequestConfig = {}): Promise<PublishLink> {
  return post<PublishLink>(`/admin/links/${id}/pause`, undefined, config);
}

/** 恢复监控并重算 `next_check_at` / `next_index_check_at` */
export function resume(id: number, config: AxiosRequestConfig = {}): Promise<PublishLink> {
  return post<PublishLink>(`/admin/links/${id}/resume`, undefined, config);
}

/** 删除检测历史（分页，`checked_at` 倒序） */
export function listChecks(id: number, params: { page?: number; page_size?: number } = {}, config: AxiosRequestConfig = {}): Promise<Page<LinkCheck>> {
  return httpGet<Page<LinkCheck>>(`/admin/links/${id}/checks`, clean(params), config);
}

/** 收录检测历史（分页；`kind` / `engine`） */
export function listIndexChecks(id: number, params: ListIndexChecksParams = {}, config: AxiosRequestConfig = {}): Promise<Page<IndexCheck>> {
  return httpGet<Page<IndexCheck>>(`/admin/links/${id}/index-checks`, clean(params), config);
}

/** 导出 CSV（同列表筛选；含按引擎收录状态与最近检测） */
export function exportLinks(params: Omit<ListLinksParams, "page" | "page_size"> = {}, config: AxiosRequestConfig = {}): Promise<DownloadResult> {
  return requestBlob({
    ...config,
    method: "get",
    url: "/admin/links/export",
    params: clean({ ...params, project_id: params.project_id || undefined }),
    timeout: 120_000,
  });
}
