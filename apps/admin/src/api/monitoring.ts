// /admin/monitoring（docs/04 §6.18、§7.11、§7.12；docs/11 §11.6；docs/13 §6.2、§7.5）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
// 总后台处于用户视角（store/project.ownerId > 0）时，client.ts 为 GET 与两个 run 接口自动附加查询参数 owner_id。
import type {
  CheckType,
  EnqueueResult,
  IndexCheck,
  IndexKind,
  LinkCheck,
  LinkCheckResult,
  MonitoringIndexChecksRunBody,
  MonitoringLinkChecksRunBody,
  MonitoringOverview,
  Page,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { get, post } from "./client";

export interface ListLinkChecksParams {
  page?: number;
  page_size?: number;
  project_id?: number;
  platform_id?: number;
  result_status?: LinkCheckResult;
  check_type?: CheckType;
  link_id?: number;
  /** ISO 8601 UTC，作用于 checked_at */
  start?: string;
  end?: string;
}

/** `GET /admin/monitoring/index-checks` 的筛选（04 §6.18：kind / engine / provider / result_status / project_id / platform_id / link_id / start / end） */
export interface ListIndexChecksParams {
  page?: number;
  page_size?: number;
  kind?: IndexKind;
  engine?: string;
  provider?: string;
  result_status?: string;
  project_id?: number;
  platform_id?: number;
  link_id?: number;
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

/**
 * 概览：`due` / `today` / `last_run_at` 按调用者可见的链接计算，`queued` / `workers` / `daily_limits` 为平台值。
 * 需要 `monitoring.link_checks.view`。
 */
export function overview(config: AxiosRequestConfig = {}): Promise<MonitoringOverview> {
  return get<MonitoringOverview>("/admin/monitoring/overview", undefined, config);
}

/** 全部删除检测记录分页；每条附 `link{id,url,platform_code}`（`project_id` 为 0 / 缺省时为全部可见项目） */
export function listLinkChecks(params: ListLinkChecksParams = {}, config: AxiosRequestConfig = {}): Promise<Page<LinkCheck>> {
  return get<Page<LinkCheck>>("/admin/monitoring/link-checks", clean({ ...params, project_id: params.project_id || undefined }), config);
}

/** 单条删除检测记录（含 `evidence`） */
export function getLinkCheck(id: number, config: AxiosRequestConfig = {}): Promise<LinkCheck> {
  return get<LinkCheck>(`/admin/monitoring/link-checks/${id}`, undefined, config);
}

/** 收录检测记录分页（列表不附链接信息） */
export function listIndexChecks(params: ListIndexChecksParams = {}, config: AxiosRequestConfig = {}): Promise<Page<IndexCheck>> {
  return get<Page<IndexCheck>>("/admin/monitoring/index-checks", clean({ ...params, project_id: params.project_id || undefined }), config);
}

/** 单条收录检测记录（含 `evidence`） */
export function getIndexCheck(id: number, config: AxiosRequestConfig = {}): Promise<IndexCheck> {
  return get<IndexCheck>(`/admin/monitoring/index-checks/${id}`, undefined, config);
}

/**
 * 批量入队 `manual` 删除检测 → `{enqueued, skipped}`：已在队列 / 超出 `link_check.daily_limit` / 不可见的 `link_ids` 计入 `skipped`；
 * `project_id` 与 `link_ids` 都未给出时，用户视角（或普通用户）只对其负责项目下的链接入队。
 */
export function runLinkChecks(body: MonitoringLinkChecksRunBody, config: AxiosRequestConfig = {}): Promise<EnqueueResult> {
  return post<EnqueueResult>("/admin/monitoring/link-checks/run", body, config);
}

/** 批量入队 `manual` 收录检测 → `{enqueued, skipped}`；`engines` 省略时取 `kinds` 下全部启用引擎，传入未启用引擎 400 */
export function runIndexChecks(body: MonitoringIndexChecksRunBody, config: AxiosRequestConfig = {}): Promise<EnqueueResult> {
  return post<EnqueueResult>("/admin/monitoring/index-checks/run", body, config);
}
