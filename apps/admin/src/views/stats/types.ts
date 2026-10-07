// 报表页（docs/12 §6）各 Tab 的筛选状态：由 Reports.vue 持有并同步到路由 query，子组件通过 v-model 读写。
import type { BreakdownDimension, RankingType, StatsDimension, StatsGranularity } from "@aicreat/shared";

export type ReportTab = "trends" | "breakdown" | "rankings" | "ai";
export const REPORT_TABS: readonly ReportTab[] = ["trends", "breakdown", "rankings", "ai"];

/** 各 Tab 共用：时间范围（统计时区，闭区间）与项目（0 = 全部） */
export interface ReportFilters {
  start: string;
  end: string;
  projectId: number;
}

export interface TrendState {
  granularity: StatsGranularity;
  dimension: StatsDimension;
  dimension_key: string;
  metrics: string[];
  chartType: "line" | "bar";
}

export interface BreakdownState {
  dimension: BreakdownDimension;
  /** 第一个为主指标 */
  metrics: string[];
}

export interface RankingState {
  type: RankingType;
  /** undefined = 由后端按 stats_config.rankings_limit 取默认值 */
  limit: number | undefined;
}

export interface AiState {
  granularity: StatsGranularity;
}

/** 分解表「查看趋势」联动 */
export interface ViewTrendPayload {
  dimension: StatsDimension;
  dimension_key: string;
  metric: string;
  projectId?: number;
}

export const RANKING_LIMITS = [10, 20, 50] as const;
