// 报表通用工具（docs/12 §3.1、§3.4、§7）：按单位格式化指标、统计时区下的日期运算、周 / 月首尾周期是否完整、
// 多行指标汇总（流量列求和、比率由分子分母求和后相除，缺分子列时按「比率 × 分母」还原）。
import type { MetricUnit } from "@/api/stats";
import { METRIC_SPEC_MAP } from "@/api/stats";
import { formatCny, formatDuration, formatHours, formatNumber, formatPercent, formatQuota, formatTokens } from "@/utils/format";

const EMPTY = "--";

/** 指标单位（未知指标按计数） */
export function unitOf(metric: string): MetricUnit {
  return METRIC_SPEC_MAP[metric]?.unit ?? "count";
}

/** 按单位格式化；null / 非数值显示 `--`（docs/12 §3.1） */
export function formatMetricValue(value: number | string | null | undefined, unit: MetricUnit): string {
  if (value === null || value === undefined || value === "") return EMPTY;
  const n = Number(value);
  if (!Number.isFinite(n)) return EMPTY;
  switch (unit) {
    case "percent":
      return formatPercent(n, 2);
    case "currency":
      return formatCny(n);
    case "duration":
      return formatDuration(n);
    case "quota":
      return formatQuota(n);
    case "tokens":
      return formatTokens(n);
    case "hours":
      return formatHours(n);
    default:
      return formatNumber(n, Number.isInteger(n) ? 0 : 2);
  }
}

export function formatMetric(metric: string, value: number | string | null | undefined): string {
  return formatMetricValue(value, unitOf(metric));
}

// ---------- 日期（统计时区，YYYY-MM-DD） ----------

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

export function isDateStr(value: unknown): value is string {
  return typeof value === "string" && DATE_RE.test(value) && !Number.isNaN(Date.parse(`${value}T00:00:00Z`));
}

/** 指定 IANA 时区的今天（`YYYY-MM-DD`）；时区非法时退回浏览器本地日期 */
export function todayIn(timezone?: string | null): string {
  const now = new Date();
  try {
    if (timezone) {
      return new Intl.DateTimeFormat("en-CA", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).format(now);
    }
  } catch {
    /* fall through */
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function toUtc(date: string): Date {
  return new Date(`${date}T00:00:00Z`);
}

function fromUtc(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addDays(date: string, days: number): string {
  const d = toUtc(date);
  d.setUTCDate(d.getUTCDate() + days);
  return fromUtc(d);
}

/** 闭区间天数 */
export function spanDays(start: string, end: string): number {
  return Math.round((toUtc(end).getTime() - toUtc(start).getTime()) / 86_400_000) + 1;
}

export function monthStart(date: string): string {
  return `${date.slice(0, 7)}-01`;
}

export function monthEnd(date: string): string {
  const d = toUtc(monthStart(date));
  d.setUTCMonth(d.getUTCMonth() + 1);
  d.setUTCDate(0);
  return fromUtc(d);
}

/** ISO 周一 = 1 … 周日 = 7 */
function isoWeekday(date: string): number {
  const w = toUtc(date).getUTCDay();
  return w === 0 ? 7 : w;
}

/** 周 / 月粒度下 start / end 所在周期是否被截断（docs/12 §3.4「不完整」角标） */
export function incompleteEdges(start: string, end: string, granularity: "day" | "week" | "month"): { start: boolean; end: boolean } {
  if (granularity === "week") return { start: isoWeekday(start) !== 1, end: isoWeekday(end) !== 7 };
  if (granularity === "month") return { start: start !== monthStart(start), end: end !== monthEnd(end) };
  return { start: false, end: false };
}

// ---------- 汇总 ----------

export type ValueRow = Record<string, number | string | null | undefined>;

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** 流量列求和（null 视为 0；全部缺失返回 null） */
export function sumColumn(rows: ValueRow[], key: string): number | null {
  let seen = false;
  let total = 0;
  for (const row of rows) {
    const v = num(row[key]);
    if (v === null) continue;
    seen = true;
    total += v;
  }
  return seen ? total : null;
}

interface RatioRule {
  /** 分子列（全部存在时直接求和） */
  num: string[];
  /** 分母列（求和） */
  den: string[];
  /** 分母为「分子 + 其它列」时的额外列 */
  denExtra?: string[];
}

/** 比率指标的分子 / 分母（docs/12 §3.2；分母为 0 时 null） */
const RATIO_RULES: Record<string, RatioRule> = {
  keyword_adopt_rate: { num: ["keywords_adopted"], den: ["keywords_created"] },
  ai_success_rate: { num: ["ai_succeeded"], den: ["ai_calls"] },
  ai_avg_duration_ms: { num: ["ai_duration_ms_sum"], den: ["ai_succeeded"] },
  task_success_rate: { num: ["tasks_succeeded"], den: ["tasks_succeeded", "tasks_failed"] },
  task_avg_duration_ms: { num: ["task_duration_ms_sum"], den: ["tasks_succeeded"] },
  quota_reconciled_rate: { num: ["quota_reconciled_calls"], den: ["ai_calls"] },
  media_success_rate: { num: ["images_generated", "videos_generated"], den: ["images_generated", "videos_generated", "media_failed"] },
  time_to_index_hours_avg: { num: ["index_hours_sum"], den: ["index_hours_links"] },
  cost_cny_per_content: { num: [], den: ["contents_created"] },
};

/**
 * 多行汇总一个比率指标：每行优先用分子列；缺分子列时用「该行比率 × 该行分母」还原（如 cost_cny_per_content）；
 * 任一行既无分子也无法还原、或分母缺失时返回 null。
 */
export function ratioFromRows(rows: ValueRow[], metric: string): number | null {
  if (metric === "quota_diff_rate") {
    const diff = diffFromRows(rows);
    const base = sumColumn(rows, "quota_estimated_reconciled");
    return diff === null || !base ? null : diff / base;
  }
  const rule = RATIO_RULES[metric];
  if (!rule) return null;
  let numerator = 0;
  let denominator = 0;
  for (const row of rows) {
    let den = 0;
    for (const k of rule.den) {
      const v = num(row[k]);
      if (v === null) return null;
      den += v;
    }
    let part: number | null = 0;
    if (rule.num.length && rule.num.every((k) => num(row[k]) !== null)) {
      part = rule.num.reduce((acc, k) => acc + (num(row[k]) ?? 0), 0);
    } else {
      const rate = num(row[metric]);
      part = rate === null ? (den === 0 ? 0 : null) : rate * den;
    }
    if (part === null) return null;
    numerator += part;
    denominator += den;
  }
  return denominator === 0 ? null : numerator / denominator;
}

/** quota_diff = Σ quota_actual − Σ quota_estimated_reconciled（docs/12 §7.3） */
export function diffFromRows(rows: ValueRow[]): number | null {
  const actual = sumColumn(rows, "quota_actual");
  const base = sumColumn(rows, "quota_estimated_reconciled");
  return actual === null || base === null ? null : actual - base;
}

/** 汇总一个指标：流量 / 快照列求和、tokens_total 由两列或自身求和、比率按分子分母、对账差异按 §7.3 */
export function aggregateMetric(rows: ValueRow[], metric: string): number | null {
  if (!rows.length) return null;
  if (metric === "quota_diff") return diffFromRows(rows);
  const spec = METRIC_SPEC_MAP[metric];
  if (spec?.ratio || metric in RATIO_RULES || metric === "quota_diff_rate") return ratioFromRows(rows, metric);
  return sumColumn(rows, metric);
}
