// 告警摘要（docs/11 §10.4、§11.7；docs/07 §9.2；docs/13 §11）：仅有 monitoring.alerts.view 时每 60s 调 GET /admin/alerts/summary。
// - openCount：未处理（open）告警数，顶栏铃铛的徽标数字；criticalCount > 0 时铃铛高亮为严重；
// - 计数按数据范围：普通用户只含本人项目的告警，总后台用户视角下由 client.ts 附加 owner_id；
// - 告警中心处理告警后调用 refresh() 立即同步铃铛。
import { defineStore } from "pinia";
import { ALERT_SEVERITY, type AlertSeverity, type AlertSummary, type SeverityCounts } from "@aicreat/shared";
import * as alertsApi from "@/api/alerts";
import { useAuthStore } from "@/store/auth";

const INTERVAL = 60_000;
let timer: ReturnType<typeof setInterval> | null = null;
/** 登录态 / 视角变化时递增，丢弃变化前发出的摘要请求结果 */
let generation = 0;

function sum(counts: Partial<SeverityCounts> | undefined): number {
  return counts ? ALERT_SEVERITY.reduce((acc, s) => acc + (Number(counts[s]) || 0), 0) : 0;
}

export const useAlertsStore = defineStore("alerts", {
  state: () => ({
    summary: null as AlertSummary | null,
    /** 最近一次成功刷新的时间（ms） */
    updatedAt: 0,
    running: false,
    loading: false,
  }),
  getters: {
    /** 未处理（open）告警数 */
    openCount: (s): number => sum(s.summary?.open),
    /** 已确认未解决（acknowledged）告警数 */
    acknowledgedCount: (s): number => sum(s.summary?.acknowledged),
    /** 未处理的严重告警数：> 0 时铃铛高亮 */
    criticalCount: (s): number => Number(s.summary?.open?.critical) || 0,
    /** 未处理告警中最高的严重级别 */
    topSeverity: (s): AlertSeverity | null => {
      const open = s.summary?.open;
      if (!open) return null;
      for (const sev of [...ALERT_SEVERITY].reverse()) if ((Number(open[sev]) || 0) > 0) return sev;
      return null;
    },
  },
  actions: {
    async refresh() {
      const auth = useAuthStore();
      if (!auth.token || !auth.hasPermission("monitoring.alerts.view")) {
        this.stop();
        return;
      }
      const gen = generation;
      this.loading = true;
      try {
        // silent：暂时失败时不打扰用户，保留上次计数
        const summary = await alertsApi.summary({ silent: true });
        if (gen !== generation) return;
        this.summary = summary;
        this.updatedAt = Date.now();
      } catch {
        /* ignore */
      } finally {
        if (gen === generation) this.loading = false;
      }
    },
    start() {
      const auth = useAuthStore();
      if (!auth.hasPermission("monitoring.alerts.view")) {
        this.stop();
        return;
      }
      if (timer) return;
      this.running = true;
      void this.refresh();
      timer = setInterval(() => void this.refresh(), INTERVAL);
    },
    stop() {
      if (timer) clearInterval(timer);
      timer = null;
      this.running = false;
    },
    reset() {
      generation += 1;
      this.stop();
      this.summary = null;
      this.updatedAt = 0;
      this.loading = false;
    },
  },
});
