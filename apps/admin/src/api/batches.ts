// /admin/generation-batches（docs/04 §6.12、§7.4；docs/09 §9、§10.7）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type { BatchKind, BatchStatus, GenerationBatch, Page, SystemHealth } from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { get as httpGet, isApiError, post } from "./client";

export interface ListBatchesParams {
  page?: number;
  page_size?: number;
  project_id?: number;
  kind?: BatchKind;
  status?: BatchStatus;
  created_by?: number;
}

function clean(params: object): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
}

/** 分页列表 */
export function list(params: ListBatchesParams = {}, config: AxiosRequestConfig = {}): Promise<Page<GenerationBatch>> {
  return httpGet<Page<GenerationBatch>>("/admin/generation-batches", clean(params), config);
}

/** 详情 + `tasks[]`（根任务摘要：尝试行数、最后错误、`model_override`）；关键词 / 标题批次的轮询入口 */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<GenerationBatch> {
  return httpGet<GenerationBatch>(`/admin/generation-batches/${id}`, undefined, config);
}

/** `queued`/`running → cancelled`：未开始的根任务置 `cancelled`，运行中的不打断 */
export function cancel(id: number, config: AxiosRequestConfig = {}): Promise<GenerationBatch> {
  return post<GenerationBatch>(`/admin/generation-batches/${id}/cancel`, undefined, config);
}

/** `partial`/`failed → running`：只重跑尚无重试子任务的 `failed` 根任务；不可重试 409 `current_status` */
export function retry(id: number, config: AxiosRequestConfig = {}): Promise<GenerationBatch> {
  return post<GenerationBatch>(`/admin/generation-batches/${id}/retry`, undefined, config);
}

/**
 * 公开健康检查 `GET /api/v1/health`（docs/04 §6.21）：批次页据 `workers.worker.alive` 显示「worker 未在线」，
 * 不依赖 `ai.routes.view`（docs/09 §10.7）。db / redis 不可用时 HTTP 503 但 `data` 结构相同，此处一并返回。
 */
export async function systemHealth(config: AxiosRequestConfig = {}): Promise<SystemHealth | null> {
  try {
    return await httpGet<SystemHealth>("/health", undefined, { silent: true, ...config });
  } catch (err) {
    if (isApiError(err) && err.data && typeof err.data === "object" && "workers" in (err.data as object)) return err.data as SystemHealth;
    return null;
  }
}
