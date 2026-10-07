// /admin/stats（docs/04 §6.20、§7.14；docs/12 §3、§4.2、§8、§9；docs/13 §10）。
// - 查询接口：overview / trends / breakdown / rankings；导出 exportStats（= exportReport）；重算 recompute（timeout 300000）
// - 指标分组常量 METRIC_GROUPS 与规格表 METRIC_SPECS：指标 key 即 API 字段名与 i18n 键 `stats.metrics.<key>`；
//   `metricSupports()` 按 docs/12 §4.2「维度 × 列矩阵」过滤（派生指标按其分子、分母列判断），与后端 400 校验一致。
// 用户视角下的 GET 请求由 client.ts 附加 owner_id；路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  BreakdownDimension,
  BreakdownRow,
  RankingRowOf,
  RankingType,
  RecomputeResult,
  StatsDimension,
  StatsGranularity,
  StatsOverview,
  StatsRange,
  TrendPoint,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { get, isApiError, post, requestBlob, type DownloadResult } from "./client";

// =====================================================================
// 指标规格（docs/12 §3.2；docs/04 §7.14 metrics 合法取值表）
// =====================================================================

/** 指标分组：报表页指标下拉的分组（docs/12 §6.3） */
export type MetricGroup = "content" | "links" | "index" | "ai" | "media" | "alerts";
/** flow 流量列（周期求和）/ snapshot 快照列（取周期末值）/ ratio 派生比率（分子分母求和后相除）/ diff 对账差异 */
export type MetricKind = "flow" | "snapshot" | "ratio" | "diff";
/** 显示单位：count 计数、percent 0~1 比率、currency 人民币元、duration 毫秒、quota 额度、tokens、hours 小时 */
export type MetricUnit = "count" | "percent" | "currency" | "duration" | "quota" | "tokens" | "hours";

export interface MetricSpec {
  key: string;
  group: MetricGroup;
  kind: MetricKind;
  unit: MetricUnit;
  /** 计算依赖的 `daily_stats` 列（列本身为 [key]）；维度矩阵按这些列判断 */
  deps: string[];
  /** 是否比率类（分解 `share` 为 null、汇总时按分子分母重算） */
  ratio: boolean;
  /** 越低越好（环比颜色反转，docs/12 §5.3） */
  lowerIsBetter?: boolean;
  /** 只允许的维度（`*_by_engine` 仅引擎维度；`cost_cny_per_content` 分子分母分属不同行，仅 total 行口径） */
  only?: readonly (StatsDimension | BreakdownDimension)[];
}

function col(key: string, group: MetricGroup, unit: MetricUnit = "count", extra: Partial<MetricSpec> = {}): MetricSpec {
  return { key, group, kind: "flow", unit, deps: [key], ratio: false, ...extra };
}

function snap(key: string, group: MetricGroup): MetricSpec {
  return { key, group, kind: "snapshot", unit: "count", deps: [key], ratio: false };
}

function ratio(key: string, group: MetricGroup, deps: string[], unit: MetricUnit = "percent", extra: Partial<MetricSpec> = {}): MetricSpec {
  return { key, group, kind: "ratio", unit, deps, ratio: true, ...extra };
}

/** 全部可用于 `trends.metrics` / `breakdown.metric` 的指标（顺序即下拉顺序） */
export const METRIC_SPECS: readonly MetricSpec[] = [
  // 内容生产
  col("keywords_created", "content"),
  col("keywords_adopted", "content"),
  ratio("keyword_adopt_rate", "content", ["keywords_adopted", "keywords_created"]),
  col("titles_created", "content"),
  col("titles_adopted", "content"),
  col("contents_created", "content"),
  col("contents_approved", "content"),
  col("contents_published", "content"),
  // 链接与存活
  col("links_backfilled", "links"),
  col("links_checked", "links"),
  col("links_deleted", "links", "count", { lowerIsBetter: true }),
  col("links_changed", "links"),
  col("links_restored", "links"),
  snap("links_alive_snapshot", "links"),
  snap("links_total_snapshot", "links"),
  // 收录
  col("seo_checks", "index"),
  col("seo_newly_indexed", "index"),
  snap("seo_indexed_snapshot", "index"),
  col("geo_checks", "index"),
  col("geo_newly_cited", "index"),
  snap("geo_cited_snapshot", "index"),
  col("index_hours_sum", "index", "hours"),
  col("index_hours_links", "index"),
  ratio("time_to_index_hours_avg", "index", ["index_hours_sum", "index_hours_links"], "hours"),
  ratio("seo_index_rate_by_engine", "index", ["seo_indexed_snapshot"], "percent", { only: ["seo_engine"] }),
  ratio("geo_cite_rate_by_engine", "index", ["geo_cited_snapshot"], "percent", { only: ["geo_engine"] }),
  // AI 调用与消耗
  col("ai_calls", "ai"),
  col("ai_succeeded", "ai"),
  col("ai_failed", "ai", "count", { lowerIsBetter: true }),
  ratio("ai_success_rate", "ai", ["ai_succeeded", "ai_calls"]),
  col("ai_duration_ms_sum", "ai", "duration"),
  ratio("ai_avg_duration_ms", "ai", ["ai_duration_ms_sum", "ai_succeeded"], "duration"),
  col("tasks_succeeded", "ai"),
  col("tasks_failed", "ai", "count", { lowerIsBetter: true }),
  ratio("task_success_rate", "ai", ["tasks_succeeded", "tasks_failed"]),
  col("task_duration_ms_sum", "ai", "duration"),
  ratio("task_avg_duration_ms", "ai", ["task_duration_ms_sum", "tasks_succeeded"], "duration"),
  col("prompt_tokens", "ai", "tokens"),
  col("completion_tokens", "ai", "tokens"),
  { key: "tokens_total", group: "ai", kind: "flow", unit: "tokens", deps: ["prompt_tokens", "completion_tokens"], ratio: false },
  col("quota_estimated", "ai", "quota"),
  col("quota_actual", "ai", "quota"),
  col("quota_reconciled_calls", "ai"),
  ratio("quota_reconciled_rate", "ai", ["quota_reconciled_calls", "ai_calls"]),
  { key: "quota_estimated_reconciled", group: "ai", kind: "diff", unit: "quota", deps: ["quota_estimated_reconciled"], ratio: false },
  { key: "quota_diff", group: "ai", kind: "diff", unit: "quota", deps: ["quota_actual", "quota_estimated_reconciled"], ratio: false },
  { key: "quota_diff_rate", group: "ai", kind: "diff", unit: "percent", deps: ["quota_actual", "quota_estimated_reconciled"], ratio: true },
  col("cost_cny", "ai", "currency"),
  ratio("cost_cny_per_content", "ai", ["cost_cny", "contents_created"], "currency", { only: ["total", "project", "owner"] }),
  // 媒体
  col("images_generated", "media"),
  col("videos_generated", "media"),
  col("media_failed", "media", "count", { lowerIsBetter: true }),
  ratio("media_success_rate", "media", ["images_generated", "videos_generated", "media_failed"]),
  // 告警
  col("alerts_opened", "alerts", "count", { lowerIsBetter: true }),
  col("alerts_resolved", "alerts"),
];

export const METRIC_SPEC_MAP: Readonly<Record<string, MetricSpec>> = Object.fromEntries(METRIC_SPECS.map((s) => [s.key, s]));

/** 分组顺序（docs/12 §6.3：内容生产、链接与存活、收录、AI 调用与消耗、媒体、告警） */
export const METRIC_GROUP_ORDER: readonly MetricGroup[] = ["content", "links", "index", "ai", "media", "alerts"];

/** 指标分组常量：group → 指标 key 列表 */
export const METRIC_GROUPS: Readonly<Record<MetricGroup, string[]>> = METRIC_GROUP_ORDER.reduce(
  (acc, g) => {
    acc[g] = METRIC_SPECS.filter((s) => s.group === g).map((s) => s.key);
    return acc;
  },
  {} as Record<MetricGroup, string[]>,
);

/** 「越低越好」的指标（KPI 环比颜色反转，docs/12 §5.3） */
export const LOWER_IS_BETTER: ReadonlySet<string> = new Set([
  ...METRIC_SPECS.filter((s) => s.lowerIsBetter).map((s) => s.key),
  "link_deleted_rate",
]);

/** `trends.metrics` / `breakdown.metric` 的上限（docs/12 §9.2、§9.3） */
export const MAX_METRICS = 8;
/** `start`~`end` 跨度上限（天，含首尾） */
export const MAX_SPAN_DAYS = 731;
/** 手动重算跨度上限（天）与同步执行阈值 */
export const MAX_RECOMPUTE_DAYS = 31;
export const SYNC_RECOMPUTE_DAYS = 7;
/** 重算请求超时（docs/12 §4.5、§10.5） */
export const RECOMPUTE_TIMEOUT_MS = 300_000;

// ---------- 维度 × 列矩阵（docs/12 §4.2） ----------

const PLATFORM_COLUMNS = [
  "links_backfilled",
  "links_checked",
  "links_deleted",
  "links_changed",
  "links_restored",
  "links_alive_snapshot",
  "links_total_snapshot",
  "seo_checks",
  "seo_newly_indexed",
  "seo_indexed_snapshot",
  "geo_checks",
  "geo_newly_cited",
  "geo_cited_snapshot",
  "index_hours_sum",
  "index_hours_links",
];
const AI_COLUMNS = [
  "ai_calls",
  "ai_succeeded",
  "ai_failed",
  "ai_duration_ms_sum",
  "prompt_tokens",
  "completion_tokens",
  "quota_estimated",
  "quota_actual",
  "quota_reconciled_calls",
  "cost_cny",
  "tasks_succeeded",
  "tasks_failed",
  "task_duration_ms_sum",
  "images_generated",
  "videos_generated",
  "media_failed",
  "quota_estimated_reconciled",
];
const ADMIN_COLUMNS = [
  "keywords_created",
  "keywords_adopted",
  "titles_created",
  "titles_adopted",
  "contents_created",
  "contents_approved",
  "links_backfilled",
  "images_generated",
  "videos_generated",
  "media_failed",
  "ai_calls",
  "ai_succeeded",
  "ai_failed",
  "prompt_tokens",
  "completion_tokens",
  "quota_estimated",
  "quota_actual",
  "cost_cny",
  "tasks_succeeded",
  "tasks_failed",
  "task_duration_ms_sum",
  "quota_estimated_reconciled",
];

/** 各存储维度填写的列；`total`（及分解的 `project` / `owner`，取 total 行）为全部列 */
export const DIMENSION_COLUMNS: Readonly<Record<Exclude<StatsDimension, "total">, ReadonlySet<string>>> = {
  platform: new Set(PLATFORM_COLUMNS),
  capability: new Set(AI_COLUMNS),
  model: new Set(AI_COLUMNS),
  admin: new Set(ADMIN_COLUMNS),
  seo_engine: new Set(["seo_checks", "seo_newly_indexed", "seo_indexed_snapshot", "index_hours_sum", "index_hours_links"]),
  geo_engine: new Set(["geo_checks", "geo_newly_cited", "geo_cited_snapshot"]),
};

/** 指标是否可用于该维度（趋势的 `dimension` 或分解的 `dimension`） */
export function metricSupports(metric: string, dimension: StatsDimension | BreakdownDimension): boolean {
  const spec = METRIC_SPEC_MAP[metric];
  if (!spec) return false;
  if (spec.only) return spec.only.includes(dimension);
  if (dimension === "total" || dimension === "project" || dimension === "owner") return true;
  const cols = DIMENSION_COLUMNS[dimension];
  return spec.deps.every((d) => cols.has(d));
}

/** 该维度可选的指标（保持 METRIC_SPECS 顺序） */
export function metricsFor(dimension: StatsDimension | BreakdownDimension): MetricSpec[] {
  return METRIC_SPECS.filter((s) => metricSupports(s.key, dimension));
}

/** 维度键候选请求所用的指标（docs/12 §6.2） */
export const DIMENSION_KEY_METRIC: Readonly<Record<Exclude<StatsDimension, "total"> | "owner", string>> = {
  platform: "links_total_snapshot",
  capability: "ai_calls",
  model: "ai_calls",
  admin: "ai_calls",
  owner: "contents_created",
  seo_engine: "seo_checks",
  geo_engine: "geo_checks",
};

/** 维度切换后的缺省指标（须在该维度矩阵内） */
export const DEFAULT_DIMENSION_METRICS: Readonly<Record<StatsDimension | BreakdownDimension, string[]>> = {
  total: ["ai_calls", "links_backfilled", "seo_newly_indexed"],
  project: ["cost_cny", "ai_calls", "contents_created"],
  owner: ["cost_cny", "ai_calls", "contents_created"],
  platform: ["links_backfilled", "links_deleted"],
  capability: ["ai_calls", "cost_cny"],
  model: ["ai_calls", "cost_cny"],
  admin: ["ai_calls", "contents_created"],
  seo_engine: ["seo_checks", "seo_newly_indexed"],
  geo_engine: ["geo_checks", "geo_newly_cited"],
};

// =====================================================================
// 接口
// =====================================================================

export interface OverviewParams {
  /** 0 = 全部（按用户统计时为该用户的全部项目） */
  project_id?: number;
  range?: StatsRange;
}

export interface TrendsParams {
  metrics: string[] | string;
  granularity?: StatsGranularity;
  start: string;
  end: string;
  project_id?: number;
  dimension?: StatsDimension;
  dimension_key?: string;
}

export interface BreakdownParams {
  dimension: BreakdownDimension;
  /** 第一个为主指标（排序与 share 依据） */
  metric: string[] | string;
  start: string;
  end: string;
  project_id?: number;
}

export interface RankingsParams {
  type: RankingType;
  /** 不传时由后端取 `stats_config.rankings_limit` */
  limit?: number;
  start: string;
  end: string;
  project_id?: number;
}

export type ExportParams =
  | ({ report: "trends" } & TrendsParams)
  | ({ report: "breakdown" } & BreakdownParams)
  | ({ report: "rankings" } & RankingsParams);

export interface RecomputeBody {
  start_date: string;
  end_date: string;
}

function joinList(value: string[] | string): string {
  return Array.isArray(value) ? value.map((v) => v.trim()).filter(Boolean).join(",") : value;
}

function clean(params: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = Array.isArray(v) ? joinList(v as string[]) : v;
  }
  return out;
}

function trendQuery(params: TrendsParams): Record<string, unknown> {
  const dimension = params.dimension ?? "total";
  return clean({
    metrics: params.metrics,
    granularity: params.granularity ?? "day",
    start: params.start,
    end: params.end,
    project_id: params.project_id ?? 0,
    dimension,
    dimension_key: dimension === "total" ? undefined : params.dimension_key,
  });
}

function breakdownQuery(params: BreakdownParams): Record<string, unknown> {
  return clean({
    dimension: params.dimension,
    metric: params.metric,
    start: params.start,
    end: params.end,
    project_id: params.project_id ?? 0,
  });
}

function rankingsQuery(params: RankingsParams): Record<string, unknown> {
  return clean({ type: params.type, limit: params.limit, start: params.start, end: params.end, project_id: params.project_id ?? 0 });
}

/** 总览：KPI + 环比 + 分解 + 四条轻量序列（`dashboard.view`） */
export function overview(params: OverviewParams = {}, config: AxiosRequestConfig = {}): Promise<StatsOverview> {
  return get<StatsOverview>("/admin/stats/overview", clean({ project_id: params.project_id ?? 0, range: params.range ?? "7d" }), config);
}

/** 趋势 `[{date, <metric>…}]`；比率指标的分子分母列由后端自动附带（`stats.reports.view`） */
export function trends(params: TrendsParams, config: AxiosRequestConfig = {}): Promise<TrendPoint[]> {
  return get<TrendPoint[]>("/admin/stats/trends", trendQuery(params), config);
}

/** 分解 `[{key, label, value, share, values}]`，含 value=0 的键（亦为维度键候选来源） */
export function breakdown(params: BreakdownParams, config: AxiosRequestConfig = {}): Promise<BreakdownRow[]> {
  return get<BreakdownRow[]>("/admin/stats/breakdown", breakdownQuery(params), config);
}

/** 榜单 */
export function rankings<T extends RankingType>(params: RankingsParams & { type: T }, config: AxiosRequestConfig = {}): Promise<RankingRowOf<T>[]> {
  return get<RankingRowOf<T>[]>("/admin/stats/rankings", rankingsQuery(params), config);
}

/** CSV 导出（`stats.reports.export`）：参数与对应查询接口完全相同；返回 blob 与文件名 `stats-{report}-{YYYYMMDD}.csv` */
export function exportStats(params: ExportParams, config: AxiosRequestConfig = {}): Promise<DownloadResult> {
  let query: Record<string, unknown>;
  if (params.report === "trends") query = trendQuery(params);
  else if (params.report === "breakdown") query = breakdownQuery(params);
  else query = rankingsQuery(params);
  return requestBlob({ timeout: 120_000, ...config, method: "get", url: "/admin/stats/export", params: { report: params.report, ...query } });
}

/** docs/12 §8 的命名（与 exportStats 相同） */
export const exportReport = exportStats;

/** 重算 `daily_stats`：≤ 7 天同步返回 `{days, rows_upserted, skipped, duration_ms}`；8~31 天入队返回 202 `{queued, days}` */
export function recompute(body: RecomputeBody, config: AxiosRequestConfig = {}): Promise<RecomputeResult> {
  return post<RecomputeResult>("/admin/stats/recompute", body, { ...config, timeout: RECOMPUTE_TIMEOUT_MS });
}

export function isQueuedRecompute(result: RecomputeResult): result is { queued: true; days: number } {
  return "queued" in result && result.queued === true;
}

/** 请求被 AbortController 取消 */
export function isCanceled(err: unknown): boolean {
  return isApiError(err) && err.code === 0 && err.message === "canceled";
}

/** 统计接口 404：所选项目已不可见（docs/13 §12.2，调用方应把项目重置为 0 后重新请求） */
export function isProjectGone(err: unknown): boolean {
  return isApiError(err) && (err.status === 404 || err.code === 404);
}
