// /admin/contents（docs/04 §6.11、§7.6、§7.7、§8；docs/09 §8）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  AiTaskSummary,
  Content,
  ContentAttachBody,
  ContentBodyBody,
  ContentCreateBody,
  ContentExportFormat,
  ContentGenerateBody,
  ContentGenerateResult,
  ContentOutlineBody,
  ContentRewriteBody,
  ContentSeoBody,
  ContentStatus,
  ContentUpdateBody,
  ContentVersion,
  MediaAsset,
  Page,
  PublishLink,
  TaskCreatedResult,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put, requestBlob, type DownloadResult } from "./client";

export interface ListContentsParams {
  page?: number;
  page_size?: number;
  project_id?: number;
  status?: ContentStatus;
  keyword_id?: number;
  title_id?: number;
  batch_id?: number;
  /** 标题搜索 */
  keyword?: string;
  created_by?: number;
  has_links?: boolean;
}

function clean(params: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = typeof v === "boolean" ? (v ? 1 : 0) : v;
  }
  return out;
}

/** 分页列表（不含 `body`） */
export function list(params: ListContentsParams = {}, config: AxiosRequestConfig = {}): Promise<Page<Content>> {
  return httpGet<Page<Content>>("/admin/contents", clean(params), config);
}

/** 详情（含当前版本、`outline`、SEO 要素、`assets[]`、`link_count`、`active_task_id`、`pending_tasks[]`） */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<Content> {
  return httpGet<Content>(`/admin/contents/${id}`, undefined, config);
}

/** 手工创建草稿；带 `body` 时建版本 `source=manual` */
export function create(body: ContentCreateBody, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>("/admin/contents", body, config);
}

/**
 * 人工编辑 → 新版本 `source=manual`（哈希未变 `version_created=false`）；
 * `current_version_id` 与服务端不一致 → 409 `data={"current_version_id":…}`；`generating` → 409 `current_status`。
 */
export function update(id: number, body: ContentUpdateBody, config: AxiosRequestConfig = {}): Promise<Content> {
  return put<Content>(`/admin/contents/${id}`, body, config);
}

/** 删除：仅 `draft`/`archived` 且 `link_count=0` */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/contents/${id}`, undefined, config);
}

/** 每个已采用标题创建一条 `generating` 内容 + 根任务；返回 `{batch_id, content_ids[], quota_warning?}` */
export function generate(body: ContentGenerateBody, config: AxiosRequestConfig = {}): Promise<ContentGenerateResult> {
  return post<ContentGenerateResult>("/admin/contents/generate", body, config);
}

/** 生成大纲（`content_outline`，不改状态） */
export function generateOutline(id: number, body: ContentOutlineBody = {}, config: AxiosRequestConfig = {}): Promise<TaskCreatedResult> {
  return post<TaskCreatedResult>(`/admin/contents/${id}/generate-outline`, body, config);
}

/** 生成正文（`content_body`，`draft`/`ready`/`rejected → generating`） */
export function generateBody(id: number, body: ContentBodyBody = {}, config: AxiosRequestConfig = {}): Promise<TaskCreatedResult> {
  return post<TaskCreatedResult>(`/admin/contents/${id}/generate-body`, body, config);
}

/** 重写 / 扩写 / 缩写 / 改风格（`content_rewrite`） */
export function rewrite(id: number, body: ContentRewriteBody, config: AxiosRequestConfig = {}): Promise<TaskCreatedResult> {
  return post<TaskCreatedResult>(`/admin/contents/${id}/rewrite`, body, config);
}

/** 生成 SEO 要素与 FAQ（`content_seo`，不改状态；正文为空 409） */
export function generateSeo(id: number, body: ContentSeoBody = {}, config: AxiosRequestConfig = {}): Promise<TaskCreatedResult> {
  return post<TaskCreatedResult>(`/admin/contents/${id}/generate-seo`, body, config);
}

/** `active_task_id` 对应根任务摘要；无进行中任务返回最近一个 `content_*` 根任务，从未生成过为 `null` */
export function getTask(id: number, config: AxiosRequestConfig = {}): Promise<AiTaskSummary | null> {
  return httpGet<AiTaskSummary | null>(`/admin/contents/${id}/task`, undefined, config);
}

/** `ready → reviewing`（`review_required=false` 时直接 `approved`）；质量阻断 409 `reason=quality_blocked` */
export function submitReview(id: number, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>(`/admin/contents/${id}/submit-review`, undefined, config);
}

/** `reviewing → approved` */
export function approve(id: number, note?: string | null, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>(`/admin/contents/${id}/approve`, { note: note || null }, config);
}

/** `reviewing → rejected`（`note` 必填） */
export function reject(id: number, note: string, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>(`/admin/contents/${id}/reject`, { note }, config);
}

/** `draft`/`ready`/`rejected`/`approved`/`published → archived`；`generating`/`reviewing` 409 */
export function archive(id: number, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>(`/admin/contents/${id}/archive`, undefined, config);
}

/** `archived → prev_status` */
export function unarchive(id: number, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>(`/admin/contents/${id}/unarchive`, undefined, config);
}

/** 版本列表（不分页；默认不含 `body`，`withBody=true` 含） */
export function versions(id: number, withBody = false, config: AxiosRequestConfig = {}): Promise<ContentVersion[]> {
  return httpGet<ContentVersion[]>(`/admin/contents/${id}/versions`, withBody ? { with_body: 1 } : undefined, config);
}

/** 版本详情（含全部版本化字段） */
export function getVersion(id: number, versionId: number, config: AxiosRequestConfig = {}): Promise<ContentVersion> {
  return httpGet<ContentVersion>(`/admin/contents/${id}/versions/${versionId}`, undefined, config);
}

/** 以该版本新建版本 `source=restore`；与当前哈希相同 `version_created=false`；`generating` 409 */
export function restoreVersion(id: number, versionId: number, config: AxiosRequestConfig = {}): Promise<Content> {
  return post<Content>(`/admin/contents/${id}/versions/${versionId}/restore`, undefined, config);
}

/** 删除历史版本；当前版本 409 */
export function deleteVersion(id: number, versionId: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/contents/${id}/versions/${versionId}`, undefined, config);
}

/** 绑定素材列表（不分页，按 `sort`） */
export function listAssets(id: number, config: AxiosRequestConfig = {}): Promise<MediaAsset[]> {
  return httpGet<MediaAsset[]>(`/admin/contents/${id}/assets`, undefined, config);
}

/** 绑定素材：`cover` 要求 `kind=image` 且 `status=ready` 并写 `cover_asset_id`；`inline` 要求 `status=ready` */
export function attach(id: number, assetId: number, body: ContentAttachBody, config: AxiosRequestConfig = {}): Promise<MediaAsset> {
  return post<MediaAsset>(`/admin/contents/${id}/assets/${assetId}/attach`, body, config);
}

/** 解绑（封面则清空 `cover_asset_id`），不删除素材 */
export function detach(id: number, assetId: number, config: AxiosRequestConfig = {}): Promise<MediaAsset> {
  return post<MediaAsset>(`/admin/contents/${id}/assets/${assetId}/detach`, undefined, config);
}

/** docs/10 §7 的命名（与 `attach` / `detach` 等价） */
export const attachAsset = attach;
export const detachAsset = detach;

/** 该内容的回填链接（不分页） */
export function listLinks(id: number, config: AxiosRequestConfig = {}): Promise<PublishLink[]> {
  return httpGet<PublishLink[]>(`/admin/contents/${id}/links`, undefined, config);
}

/** 下载当前版本：`md` → text/markdown、`html` → text/html、`json` → application/json */
export function exportContent(id: number, format: ContentExportFormat, config: AxiosRequestConfig = {}): Promise<DownloadResult> {
  return requestBlob({ ...config, method: "get", url: `/admin/contents/${id}/export`, params: { format }, timeout: 60_000 });
}
