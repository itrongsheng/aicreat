// /admin/keywords（docs/04 §6.9、§7.4；docs/09 §6）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  AdoptAction,
  BatchCreatedResult,
  BatchUpdateResult,
  Keyword,
  KeywordCreateBody,
  KeywordGenerateBody,
  KeywordImportItem,
  KeywordImportResult,
  KeywordIntent,
  KeywordStatus,
  KeywordType,
  KeywordUpdateBody,
  Page,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put, requestBlob, type DownloadResult } from "./client";

export interface ListKeywordsParams {
  page?: number;
  page_size?: number;
  /** 必填 */
  project_id: number;
  status?: KeywordStatus;
  intent?: KeywordIntent;
  keyword_type?: KeywordType;
  /** 关键词搜索 */
  keyword?: string;
  batch_id?: number;
  /** `score` → `score DESC, created_at DESC`；`created_at` 与默认相同 */
  sort?: "score" | "created_at";
  order?: "asc" | "desc";
}

export type ExportKeywordsParams = Omit<ListKeywordsParams, "page" | "page_size">;

function clean(params: object): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
}

/** 分页列表 */
export function list(params: ListKeywordsParams, config: AxiosRequestConfig = {}): Promise<Page<Keyword>> {
  return httpGet<Page<Keyword>>("/admin/keywords", clean(params), config);
}

/** 详情（含 `title_count`/`content_count`）；不可见返回 404 */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<Keyword> {
  return httpGet<Keyword>(`/admin/keywords/${id}`, undefined, config);
}

/** 手工新增（`source=manual`）；`normalized_keyword` 重复 409 `existing_id`；项目已归档 409 `current_status=archived` */
export function create(body: KeywordCreateBody, config: AxiosRequestConfig = {}): Promise<Keyword> {
  return post<Keyword>("/admin/keywords", body, config);
}

/** 编辑；改 `keyword` 时重新归一化，冲突 409 `existing_id` */
export function update(id: number, body: KeywordUpdateBody, config: AxiosRequestConfig = {}): Promise<Keyword> {
  return put<Keyword>(`/admin/keywords/${id}`, body, config);
}

/** 删除：仅 `title_count=0 AND content_count=0`，否则 409 `reason=in_use` */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/keywords/${id}`, undefined, config);
}

/**
 * 创建关键词批次（1 个根任务）并入队，返回 `{batch_id, quota_warning?}`；
 * 全局暂停 / 无可用模型 5031、频控 429、额度 4291、模板变量缺失 4221、覆盖模型不可用 400 `data={"model":…}`。
 */
export function generate(body: KeywordGenerateBody, config: AxiosRequestConfig = {}): Promise<BatchCreatedResult> {
  return post<BatchCreatedResult>("/admin/keywords/generate", body, config);
}

/** JSON 导入：`items` 1~5,000 条；返回 `{created, skipped, errors[]}` */
export function importKeywords(projectId: number, items: KeywordImportItem[], config: AxiosRequestConfig = {}): Promise<KeywordImportResult> {
  return post<KeywordImportResult>("/admin/keywords/import", { project_id: projectId, items }, { timeout: 120_000, ...config });
}

/** CSV 导入（multipart `file` + 表单 `project_id`）：UTF-8（允许 BOM），表头 `keyword,intent,keyword_type`，≤ 2 MB 且 ≤ 5,000 行 */
export function importFile(projectId: number, file: File | Blob, config: AxiosRequestConfig = {}): Promise<KeywordImportResult> {
  const form = new FormData();
  form.append("project_id", String(projectId));
  form.append("file", file, file instanceof File ? file.name : "keywords.csv");
  return post<KeywordImportResult>("/admin/keywords/import-file", form, { timeout: 120_000, ...config });
}

/** `candidate → adopted`；重复采用 409 `current_status=adopted` */
export function adopt(id: number, config: AxiosRequestConfig = {}): Promise<Keyword> {
  return post<Keyword>(`/admin/keywords/${id}/adopt`, undefined, config);
}

/** `candidate`/`adopted → discarded`，同事务弃用其 `candidate` 标题 */
export function discard(id: number, config: AxiosRequestConfig = {}): Promise<Keyword> {
  return post<Keyword>(`/admin/keywords/${id}/discard`, undefined, config);
}

/** `discarded → candidate`（不恢复被级联弃用的标题） */
export function restore(id: number, config: AxiosRequestConfig = {}): Promise<Keyword> {
  return post<Keyword>(`/admin/keywords/${id}/restore`, undefined, config);
}

/** 批量状态动作：`ids` ≤ 500；逐条判定，返回 `{updated, skipped[{id, reason}]}` */
export function batchStatus(ids: number[], action: AdoptAction, config: AxiosRequestConfig = {}): Promise<BatchUpdateResult> {
  return post<BatchUpdateResult>("/admin/keywords/batch-status", { ids, action }, config);
}

/** CSV 导出（同列表筛选，UTF-8 BOM，≤ 50,000 行） */
export function exportKeywords(params: ExportKeywordsParams, config: AxiosRequestConfig = {}): Promise<DownloadResult> {
  return requestBlob({ ...config, method: "get", url: "/admin/keywords/export", params: { ...clean(params), format: "csv" }, timeout: 120_000 });
}
