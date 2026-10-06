// /admin/prompt-templates（docs/04 §6.8；docs/09 §5.6、§10.2；docs/13 §7.3）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  Page,
  PromptKind,
  PromptPreviewResult,
  PromptStatus,
  PromptTemplate,
  PromptTemplateCreateBody,
  PromptTemplateDuplicateBody,
  PromptTemplateUpdateBody,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put } from "./client";

export interface ListPromptTemplatesParams {
  page?: number;
  page_size?: number;
  kind?: PromptKind;
  status?: PromptStatus;
  /** 0 = 全局模板；>0 = 该项目的专属模板 */
  project_id?: number;
  keyword?: string;
  /** 默认每个 code 只返回最新可见版本；true 返回全部可见版本 */
  all_versions?: boolean;
}

function clean(params: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = typeof v === "boolean" ? (v ? 1 : 0) : v;
  }
  return out;
}

/** 分页；全局草稿只对创建人与总后台可见 */
export function list(params: ListPromptTemplatesParams = {}, config: AxiosRequestConfig = {}): Promise<Page<PromptTemplate>> {
  return httpGet<Page<PromptTemplate>>("/admin/prompt-templates", clean(params), config);
}

export function get(id: number, config: AxiosRequestConfig = {}): Promise<PromptTemplate> {
  return httpGet<PromptTemplate>(`/admin/prompt-templates/${id}`, undefined, config);
}

/** 新建 draft（version=1）；`code` 已存在 409（被不可见模板占用时 `existing_id=null`、`reason=owned_by_other`） */
export function create(body: PromptTemplateCreateBody, config: AxiosRequestConfig = {}): Promise<PromptTemplate> {
  return post<PromptTemplate>("/admin/prompt-templates", body, config);
}

/** 仅 draft 原地修改；对 published 调用则复制为同 code 新版本 draft 并返回新对象（`id` 不同） */
export function update(id: number, body: PromptTemplateUpdateBody, config: AxiosRequestConfig = {}): Promise<PromptTemplate> {
  return put<PromptTemplate>(`/admin/prompt-templates/${id}`, body, config);
}

/** 仅 draft 且非系统模板 */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/prompt-templates/${id}`, undefined, config);
}

/** `draft → published`，同 code 旧 published → archived；未知变量 400（`loc=["body","user_prompt"|"system_prompt"]`，`type=unknown_variable`） */
export function publish(id: number, config: AxiosRequestConfig = {}): Promise<PromptTemplate> {
  return post<PromptTemplate>(`/admin/prompt-templates/${id}/publish`, undefined, config);
}

/** `published → archived`；最后一个 published 且（系统模板或被项目默认引用）→ 409 `reason=last_published` */
export function archive(id: number, config: AxiosRequestConfig = {}): Promise<PromptTemplate> {
  return post<PromptTemplate>(`/admin/prompt-templates/${id}/archive`, undefined, config);
}

/** 复制为新 code 的 draft */
export function duplicate(id: number, body: PromptTemplateDuplicateBody, config: AxiosRequestConfig = {}): Promise<PromptTemplate> {
  return post<PromptTemplate>(`/admin/prompt-templates/${id}/duplicate`, body, config);
}

/** 渲染预览（不调用模型）：`variables` 覆盖内置示例值；必填变量缺失 4221 `data.missing[]` */
export function preview(id: number, variables: Record<string, string>, config: AxiosRequestConfig = {}): Promise<PromptPreviewResult> {
  return post<PromptPreviewResult>(`/admin/prompt-templates/${id}/preview`, { variables }, config);
}

/** 同 code 的全部可见版本（不分页） */
export function versions(id: number, config: AxiosRequestConfig = {}): Promise<PromptTemplate[]> {
  return httpGet<PromptTemplate[]>(`/admin/prompt-templates/${id}/versions`, undefined, config);
}
