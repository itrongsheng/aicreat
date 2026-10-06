// /admin/ai/*（docs/04 §6.15、§7.15~§7.17；docs/08 §7.7、§10.7、§11.5、§12.3）
// 模型目录 / 能力路由与健康探测 / AI 任务 / 用量对账。路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  AiHealthSnapshot,
  AiModel,
  AiModelOption,
  AiTask,
  AiTaskOperation,
  AiTaskRetryResult,
  AiTaskRowKind,
  AiTaskStatus,
  AiTaskTargetType,
  AiTaskTriggerType,
  AiUsageLog,
  AiUsageSummaryRow,
  Capability,
  CapabilityRoute,
  CapabilityRouteCreateBody,
  CapabilityRouteUpdateBody,
  ErrorCategory,
  HealthResult,
  Modality,
  ModelSyncResult,
  Page,
  Protocol,
  ReconcileResult,
  ResetBreakerResult,
  RouteTestResultItem,
  UsageLastPull,
  UsageLogType,
  UsageSummaryGroupBy,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get, post, put, requestBlob, type DownloadResult } from "./client";

/** 同步上游 / 一键测试 / 对账可能较慢（一键测试总超时 35s，docs/08 §12.3） */
const SLOW_TIMEOUT = 120_000;

type Query = Record<string, unknown>;

/** 去掉 undefined / 空串 / null，布尔转 1 / 0（后端查询参数约定） */
function clean(params: object): Query {
  const out: Query = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = typeof v === "boolean" ? (v ? 1 : 0) : v;
  }
  return out;
}

// ---------------------------------------------------------------------
// 模型目录（/ai/models，权限 ai.models.view / ai.models.sync）
// ---------------------------------------------------------------------

export interface ListModelsParams {
  page?: number;
  page_size?: number;
  modality?: Modality;
  vendor_id?: number;
  is_available?: boolean;
  keyword?: string;
  /** 显示 `is_available=0 AND last_seen_at < now − hide_unavailable_after_days` 的模型 */
  include_hidden?: boolean;
}

/** 分页；含价格快照与健康状态 */
export function listModels(params: ListModelsParams = {}): Promise<Page<AiModel>> {
  return get<Page<AiModel>>("/admin/ai/models", clean(params));
}

/** 不分页模型选项（`ModelSelect.vue`）；`is_available` 缺省 true（只返回可用模型） */
export function listModelOptions(modality?: Modality, isAvailable = true, config: AxiosRequestConfig = {}): Promise<AiModelOption[]> {
  return get<AiModelOption[]>("/admin/ai/models/options", clean({ modality, is_available: isAvailable }), config);
}

/** 详情（含 `raw_pricing`） */
export function getModel(id: number): Promise<AiModel> {
  return get<AiModel>(`/admin/ai/models/${id}`);
}

/** 立即同步 `/v1/models` + `/api/pricing_new`；同步中 409，上游失败 5021 */
export function syncModels(config: AxiosRequestConfig = {}): Promise<ModelSyncResult> {
  return post<ModelSyncResult>("/admin/ai/models/sync", undefined, { timeout: SLOW_TIMEOUT, ...config });
}

// ---------------------------------------------------------------------
// 能力路由与健康（/ai，权限 ai.routes.*）
// ---------------------------------------------------------------------

export interface ListRoutesParams {
  /** 含该项目的覆盖行；项目不可见时只返回全局行 */
  project_id?: number;
}

/** 不分页：全局路由 + 可见项目的覆盖行，每条附熔断与主 / 备模型健康 */
export function listRoutes(params: ListRoutesParams = {}): Promise<CapabilityRoute[]> {
  return get<CapabilityRoute[]>("/admin/ai/routes", clean({ project_id: params.project_id && params.project_id > 0 ? params.project_id : undefined }));
}

export function getRoute(id: number): Promise<CapabilityRoute> {
  return get<CapabilityRoute>(`/admin/ai/routes/${id}`);
}

/** 新建项目覆盖路由；返回路由对象并附 `warnings[]`（`is_available=0` 的主 / 备模型） */
export function createRoute(body: CapabilityRouteCreateBody, config: AxiosRequestConfig = {}): Promise<CapabilityRoute> {
  return post<CapabilityRoute>("/admin/ai/routes", body, config);
}

/** 编辑路由（`capability` / `project_id` 不可改）；返回同 createRoute */
export function updateRoute(id: number, body: CapabilityRouteUpdateBody, config: AxiosRequestConfig = {}): Promise<CapabilityRoute> {
  return put<CapabilityRoute>(`/admin/ai/routes/${id}`, body, config);
}

/** 删除项目覆盖路由（全局路由 409） */
export function deleteRoute(id: number): Promise<null> {
  return del<null>(`/admin/ai/routes/${id}`);
}

/** 一键测试：主模型与每个备选模型各探测一次；image / video 仅 `probe_media=true` 时真实提交 */
export function testRoute(id: number, probeMedia = false): Promise<RouteTestResultItem[]> {
  return post<RouteTestResultItem[]>(`/admin/ai/routes/${id}/test`, { probe_media: probeMedia }, { timeout: SLOW_TIMEOUT });
}

/** 重置该路由主 / 备模型熔断并删除 `ai:paused:*` */
export function resetBreaker(id: number): Promise<ResetBreakerResult> {
  return post<ResetBreakerResult>(`/admin/ai/routes/${id}/reset-breaker`);
}

/** 健康快照 `{zhiqi_mode, base_url, paused, models[], workers[]}` */
export function health(config: AxiosRequestConfig = {}): Promise<AiHealthSnapshot> {
  return get<AiHealthSnapshot>("/admin/ai/health", undefined, config);
}

export interface ProbeBody {
  capability: Capability;
  model: string;
  protocol?: Protocol;
  probe_media?: boolean;
}

/** 单模型探测（GEO / SEO 引擎配置页「测试模型」）；失败以 `status="down"` 返回，不抛业务码 */
export function probe(body: ProbeBody): Promise<HealthResult> {
  return post<HealthResult>("/admin/ai/health/probe", body, { timeout: SLOW_TIMEOUT });
}

// ---------------------------------------------------------------------
// AI 任务（/ai/tasks，权限 ai.tasks.view / retry / cancel）
// ---------------------------------------------------------------------

export interface ListTasksParams {
  page?: number;
  page_size?: number;
  /** 缺省 root */
  row_kind?: AiTaskRowKind;
  project_id?: number;
  capability?: Capability;
  operation?: AiTaskOperation;
  model?: string;
  status?: AiTaskStatus;
  error_category?: ErrorCategory;
  trigger_type?: AiTaskTriggerType;
  batch_id?: number;
  root_task_id?: number;
  target_type?: AiTaskTargetType;
  target_id?: number;
  request_id?: string;
  /** ISO 8601 UTC，左闭右开 */
  start?: string;
  end?: string;
}

export type ExportTasksParams = Omit<ListTasksParams, "page" | "page_size">;

export function listTasks(params: ListTasksParams = {}): Promise<Page<AiTask>> {
  return get<Page<AiTask>>("/admin/ai/tasks", clean(params));
}

/** 详情：`input`、脱敏 `request_payload`、`response_meta`、对账信息；根任务附 `attempts[]` */
export function getTask(id: number, config: AxiosRequestConfig = {}): Promise<AiTask> {
  return get<AiTask>(`/admin/ai/tasks/${id}`, undefined, config);
}

/** 重试失败 / 过期的文本根任务 → `{task_id}`；已有重试根任务 409 `existing_id`，媒体 / 收录检测根任务 409 `hint` */
export function retryTask(id: number, config: AxiosRequestConfig = {}): Promise<AiTaskRetryResult> {
  return post<AiTaskRetryResult>(`/admin/ai/tasks/${id}/retry`, undefined, config);
}

/** 取消 `queued` / `polling` 根任务 → 最新根任务；`running` 与同步执行的根任务 409 */
export function cancelTask(id: number, config: AxiosRequestConfig = {}): Promise<AiTask> {
  return post<AiTask>(`/admin/ai/tasks/${id}/cancel`, undefined, config);
}

/** CSV 导出（同列表筛选，后端缺省 `row_kind=attempt`；最多 50,000 行，超出 400） */
export function exportTasks(params: ExportTasksParams = {}): Promise<DownloadResult> {
  return requestBlob({ method: "get", url: "/admin/ai/tasks/export", params: { format: "csv", ...clean(params) }, timeout: SLOW_TIMEOUT });
}

// ---------------------------------------------------------------------
// 用量对账（/ai/usage，权限 ai.usage.view / ai.usage.reconcile）
// ---------------------------------------------------------------------

export interface UsageLogsParams {
  page?: number;
  page_size?: number;
  model_name?: string;
  log_type?: UsageLogType;
  /** true：已匹配到本地尝试行（`ai_task_id` 非空） */
  matched?: boolean;
  request_id?: string;
  /** ISO 8601 UTC，左闭右开 */
  start?: string;
  end?: string;
}

export function usageLogs(params: UsageLogsParams = {}): Promise<Page<AiUsageLog>> {
  return get<Page<AiUsageLog>>("/admin/ai/usage/logs", clean(params));
}

/** 立即拉取 `/api/log/token` 对账；占用中 409，上游失败 5021 */
export function reconcile(config: AxiosRequestConfig = {}): Promise<ReconcileResult> {
  return post<ReconcileResult>("/admin/ai/usage/reconcile", undefined, { timeout: SLOW_TIMEOUT, ...config });
}

export interface UsageSummaryParams {
  group_by: UsageSummaryGroupBy;
  /** YYYY-MM-DD，闭区间；省略时为最近 30 天 */
  start?: string;
  end?: string;
}

/** 按终态尝试行汇总（`trigger_type != health_probe`） */
export function usageSummary(params: UsageSummaryParams): Promise<AiUsageSummaryRow[]> {
  return get<AiUsageSummaryRow[]>("/admin/ai/usage/summary", clean(params));
}

/** 最近一次对账拉取摘要；键不存在时为 null（own 范围只含 pulled_at / window_overflow） */
export function usageLastPull(config: AxiosRequestConfig = {}): Promise<UsageLastPull | null> {
  return get<UsageLastPull | null>("/admin/ai/usage/last-pull", undefined, config);
}
