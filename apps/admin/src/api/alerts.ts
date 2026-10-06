// /admin/alerts（docs/04 §6.19、§7.13；docs/11 §10、§11.7；docs/13 §11）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。用户视角下 GET 请求由 client.ts 附加 owner_id。
import type {
  Alert,
  AlertBatchResolveBody,
  AlertSeverity,
  AlertStatus,
  AlertSummary,
  AlertTargetType,
  AlertType,
  BatchUpdateResult,
  Page,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { get as httpGet, post } from "./client";

export interface ListAlertsParams {
  page?: number;
  page_size?: number;
  status?: AlertStatus;
  severity?: AlertSeverity;
  alert_type?: AlertType;
  project_id?: number;
  /** 按对象筛选时与 `target_id` 同用（如 `target_type=publish_link&target_id={id}`）；`ai_model` / `worker` / `system` 只按类型筛选 */
  target_type?: AlertTargetType;
  target_id?: number;
  /** ISO 8601 UTC，作用于 last_triggered_at */
  start?: string;
  end?: string;
}

function clean(params: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = v;
  }
  return out;
}

/** 分页（按 `last_triggered_at` 倒序）；不传 `status` 时不按状态过滤（含已解决） */
export function list(params: ListAlertsParams = {}, config: AxiosRequestConfig = {}): Promise<Page<Alert>> {
  return httpGet<Page<Alert>>("/admin/alerts", clean({ ...params, project_id: params.project_id || undefined }), config);
}

/** 摘要：按 severity 的 open / acknowledged 数与今日新增 / 解决（全部计数按数据范围） */
export function summary(config: AxiosRequestConfig = {}): Promise<AlertSummary> {
  return httpGet<AlertSummary>("/admin/alerts/summary", undefined, config);
}

/** 详情（含 `payload`） */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<Alert> {
  return httpGet<Alert>(`/admin/alerts/${id}`, undefined, config);
}

/** `open → acknowledged`；其它状态 409 `current_status` */
export function acknowledge(id: number, config: AxiosRequestConfig = {}): Promise<Alert> {
  return post<Alert>(`/admin/alerts/${id}/acknowledge`, {}, config);
}

/** `open` / `acknowledged → resolved`，`{note?}` */
export function resolve(id: number, note?: string | null, config: AxiosRequestConfig = {}): Promise<Alert> {
  return post<Alert>(`/admin/alerts/${id}/resolve`, note ? { note } : {}, config);
}

/** `open` / `acknowledged → ignored`（04 §6.19 未定义请求体） */
export function ignore(id: number, config: AxiosRequestConfig = {}): Promise<Alert> {
  return post<Alert>(`/admin/alerts/${id}/ignore`, {}, config);
}

/** 批量解决 → `{updated, skipped[{id, reason}]}`（终态 `invalid_transition`、不可见 / 不存在 `not_found`） */
export function batchResolve(body: AlertBatchResolveBody, config: AxiosRequestConfig = {}): Promise<BatchUpdateResult> {
  return post<BatchUpdateResult>("/admin/alerts/batch-resolve", body, config);
}
