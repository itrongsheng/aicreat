// 未处理告警数：仅有 monitoring.alerts.view 时每 60s 调 GET /admin/alerts/summary（docs/07 §9.2）
import { defineStore } from "pinia";
import type { AlertSummary } from "@aicreat/shared";
import { get } from "@/api/client";
import { useAuthStore } from "@/store/auth";

const INTERVAL = 60_000;
let timer: ReturnType<typeof setInterval> | null = null;

function sum(counts: Record<string, number> | undefined): number {
  return counts ? Object.values(counts).reduce((acc, n) => acc + (Number(n) || 0), 0) : 0;
}

export const useAlertsStore = defineStore("alerts", {
  state: () => ({
    openCount: 0,
    summary: null as AlertSummary | null,
    running: false,
  }),
  actions: {
    async refresh() {
      const auth = useAuthStore();
      if (!auth.token || !auth.hasPermission("monitoring.alerts.view")) {
        this.stop();
        return;
      }
      try {
        // silent：接口未就绪（404）或暂时失败时不打扰用户，保留上次计数
        const summary = await get<AlertSummary>("/admin/alerts/summary", undefined, { silent: true });
        this.summary = summary;
        this.openCount = sum(summary?.open);
      } catch {
        /* ignore */
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
      this.stop();
      this.openCount = 0;
      this.summary = null;
    },
  },
});
