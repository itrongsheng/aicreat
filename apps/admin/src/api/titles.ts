// /admin/titles（docs/04 §6.10、§7.5；docs/09 §7）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  AdoptAction,
  BatchCreatedResult,
  BatchUpdateResult,
  ContentStyle,
  Page,
  Title,
  TitleCreateBody,
  TitleGenerateBody,
  TitleStatus,
  TitleUpdateBody,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put } from "./client";

export interface ListTitlesParams {
  page?: number;
  page_size?: number;
  project_id?: number;
  keyword_id?: number;
  status?: TitleStatus;
  style?: ContentStyle;
  batch_id?: number;
}

function clean(params: object): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
}

/** 分页列表（不提供 `sort`，按 `created_at DESC, id DESC`） */
export function list(params: ListTitlesParams = {}, config: AxiosRequestConfig = {}): Promise<Page<Title>> {
  return httpGet<Page<Title>>("/admin/titles", clean(params), config);
}

export function get(id: number, config: AxiosRequestConfig = {}): Promise<Title> {
  return httpGet<Title>(`/admin/titles/${id}`, undefined, config);
}

/** 手工新增（`source=manual`）；同关键词重复不拦截（前端提交前比对并确认） */
export function create(body: TitleCreateBody, config: AxiosRequestConfig = {}): Promise<Title> {
  return post<Title>("/admin/titles", body, config);
}

/** 编辑：首次编辑把原文写入 `original_title`、`is_edited=true` */
export function update(id: number, body: TitleUpdateBody, config: AxiosRequestConfig = {}): Promise<Title> {
  return put<Title>(`/admin/titles/${id}`, body, config);
}

/** 删除：仅 `content_count=0`，否则 409 `reason=in_use` */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/titles/${id}`, undefined, config);
}

/** 每个关键词一个根任务（`operation=title_generate`）；返回 `{batch_id, quota_warning?}` */
export function generate(body: TitleGenerateBody, config: AxiosRequestConfig = {}): Promise<BatchCreatedResult> {
  return post<BatchCreatedResult>("/admin/titles/generate", body, config);
}

/** 人工打分 0~10（步长 0.5） */
export function score(id: number, manualScore: number | null, config: AxiosRequestConfig = {}): Promise<Title> {
  return post<Title>(`/admin/titles/${id}/score`, { manual_score: manualScore }, config);
}

/** `→ adopted`：要求关键词已采用，否则 409 `current_status=candidate`（「关键词未采用」） */
export function adopt(id: number, config: AxiosRequestConfig = {}): Promise<Title> {
  return post<Title>(`/admin/titles/${id}/adopt`, undefined, config);
}

export function discard(id: number, config: AxiosRequestConfig = {}): Promise<Title> {
  return post<Title>(`/admin/titles/${id}/discard`, undefined, config);
}

/** `discarded → candidate` */
export function restore(id: number, config: AxiosRequestConfig = {}): Promise<Title> {
  return post<Title>(`/admin/titles/${id}/restore`, undefined, config);
}

/** 批量状态动作：`ids` ≤ 500；返回 `{updated, skipped[{id, reason}]}` */
export function batchStatus(ids: number[], action: AdoptAction, config: AxiosRequestConfig = {}): Promise<BatchUpdateResult> {
  return post<BatchUpdateResult>("/admin/titles/batch-status", { ids, action }, config);
}
