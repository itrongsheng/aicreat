<script setup lang="ts">
// 控制台总览（docs/12 §5；docs/04 §7.14；docs/13 §12.3）。页面只调用 GET /admin/stats/overview：
// - 工具栏：全局 ProjectSelect、今日 / 7 天 / 30 天（localStorage `aicreat.dashboard.range`，默认 7d）、刷新（meta.cached 时提示「缓存数据」）、
//   数据时间 meta.computed_at（本地时间）与 today_source 徽标、统计时区标注、范围徽标（meta.scope）；
// - 16 张 KPI 卡（环比箭头、越低越好反色、迷你趋势取 series、按权限可点击跳转）；趋势图（ai_calls / links_backfilled / seo_newly_indexed 左轴，
//   cost_cny 右轴）；告警摘要（仅 monitoring.alerts.view，未处理数取 breakdowns.alerts_open，今日新增 / 解决取 store/alerts）；四张分解表；
// - 首次加载骨架、刷新只在工具栏 loading、失败 el-result + 重试并保留上次数据、空数据引导；页面可见时每 60s 自动刷新，隐藏时暂停；
// - 统计接口 404（所选项目已不可见）时把当前项目重置为 0 后重新请求（docs/13 §12.2）。
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { RouteLocationRaw } from "vue-router";
import { Refresh } from "@element-plus/icons-vue";
import {
  ALERT_SEVERITY,
  CONTENT_STATUS,
  KEYWORD_STATUS,
  LINK_ALIVE_STATUS,
  STATS_RANGE,
  TITLE_STATUS,
  type CostByCapabilityRow,
  type CostByModelRow,
  type StatsOverview,
  type StatsOverviewKpis,
  type StatsRange,
} from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import KpiCard, { type KpiCompare, type KpiFormat } from "@/components/KpiCard.vue";
import ProjectSelect from "@/components/ProjectSelect.vue";
import StatusTag, { type StatusEnumName } from "@/components/StatusTag.vue";
import TrendChart, { type TrendSeries } from "@/components/TrendChart.vue";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useAlertsStore } from "@/store/alerts";
import { useProjectStore } from "@/store/project";
import { formatCny, formatDateTime, formatDuration, formatHours, formatNumber, formatPercent, formatQuota, formatTokens } from "@/utils/format";

const RANGE_KEY = "aicreat.dashboard.range";
const REFRESH_MS = 60_000;

const { t, te } = useI18n();
const { has, isAllScope } = usePermission();
const projectStore = useProjectStore();
const alertsStore = useAlertsStore();

const title = computed(() => (isAllScope.value ? t("dashboard.titleAll") : t("dashboard.titleOwn")));

// ---------- 工具栏状态 ----------
function readRange(): StatsRange {
  try {
    const v = localStorage.getItem(RANGE_KEY);
    if (v && (STATS_RANGE as readonly string[]).includes(v)) return v as StatsRange;
  } catch {
    /* ignore */
  }
  return "7d";
}

const range = ref<StatsRange>(readRange());
watch(range, (v) => {
  try {
    localStorage.setItem(RANGE_KEY, v);
  } catch {
    /* ignore */
  }
});

// ---------- 数据 ----------
const data = ref<StatsOverview | null>(null);
const initialLoading = ref(true);
const refreshing = ref(false);
const error = ref<string | null>(null);
let controller: AbortController | null = null;

async function load(background = false): Promise<void> {
  controller?.abort();
  const ctrl = new AbortController();
  controller = ctrl;
  if (!background) refreshing.value = true;
  const projectId = projectStore.currentId;
  try {
    const res = await statsApi.overview({ project_id: projectId, range: range.value }, { silent: true, signal: ctrl.signal });
    if (ctrl !== controller) return;
    data.value = res;
    error.value = null;
  } catch (err) {
    if (ctrl !== controller || statsApi.isCanceled(err)) return;
    if (statsApi.isProjectGone(err) && projectId > 0) {
      // 所选项目已不可见：重置为全部项目后重新请求（watch currentId 触发）
      projectStore.setCurrent(0);
      return;
    }
    error.value = err instanceof Error && err.message ? err.message : t("common.requestFailed");
    if (background) throw err;
  } finally {
    if (ctrl === controller) {
      refreshing.value = false;
      initialLoading.value = false;
    }
  }
}

let debounce: ReturnType<typeof setTimeout> | null = null;
function scheduleLoad() {
  if (debounce) clearTimeout(debounce);
  debounce = setTimeout(() => void load(), 300);
}

watch([range, () => projectStore.currentId], scheduleLoad);

const polling = usePolling(() => load(true), { interval: REFRESH_MS, immediate: false });

onMounted(() => {
  void load();
  polling.start();
  if (has("monitoring.alerts.view")) alertsStore.start();
});

onBeforeUnmount(() => {
  if (debounce) clearTimeout(debounce);
  controller?.abort();
});

// ---------- meta ----------
const meta = computed(() => data.value?.meta ?? null);
const kpis = computed<Partial<StatsOverviewKpis>>(() => data.value?.kpis ?? {});
const compare = computed(() => data.value?.compare ?? {});
const breakdowns = computed(() => data.value?.breakdowns ?? null);

const todaySourceType = computed(() => {
  const s = meta.value?.today_source;
  if (s === "daily_stats") return "success";
  if (s === "realtime") return "primary";
  return "warning";
});

const scopeBadge = computed(() => {
  const m = meta.value;
  if (!m) return isAllScope.value ? t("dashboard.scopeAll") : t("dashboard.scopeOwn");
  if (m.scope === "all") return t("dashboard.scopeAll");
  if (!isAllScope.value) return t("dashboard.scopeOwn");
  const name = projectStore.currentOwner?.id === m.owner_id ? projectStore.ownerName : m.owner_id ? `#${m.owner_id}` : "";
  return t("dashboard.scopeOwner", { name });
});

const warnings = computed(() => (meta.value?.warnings ?? []).map((w) => (te(`dashboard.warnings.${w}`) ? t(`dashboard.warnings.${w}`) : w)));

/** 所有 KPI 为 0 且无今日数据：引导确认 monitor-worker 已启动 */
const isEmptyData = computed(() => {
  const d = data.value;
  if (!d || d.meta.today_source !== "none") return false;
  return Object.values(d.kpis).every((v) => v === null || v === 0);
});

// ---------- KPI 卡片 ----------
type CardGroup = "content" | "links" | "ai" | "media";

interface CardDef {
  key: string;
  group: CardGroup;
  title: string;
  value: number | null | undefined;
  format: KpiFormat;
  sub: string;
  compare?: KpiCompare | null;
  sparkline?: number[];
  help: string;
  to?: RouteLocationRaw;
  invert?: boolean;
  badge?: string;
}

function n(key: keyof StatsOverviewKpis): number | null {
  const v = kpis.value[key];
  return v === undefined ? null : (v as number | null);
}

function cmp(key: string): KpiCompare | null {
  return (compare.value as Record<string, KpiCompare | undefined>)[key] ?? null;
}

function linkIf(permission: string, to: RouteLocationRaw): RouteLocationRaw | undefined {
  return has(permission) ? to : undefined;
}

function capabilityDuration(cap: "image" | "video"): string {
  const row = breakdowns.value?.cost_by_capability?.find((r) => r.capability === cap);
  return dash(formatDuration(row?.task_avg_duration_ms ?? null));
}

function dash(s: string): string {
  return s === "-" ? "--" : s;
}

const alertsOpenTotal = computed(() => {
  const open = breakdowns.value?.alerts_open;
  return open ? ALERT_SEVERITY.reduce((acc, s) => acc + (Number(open[s]) || 0), 0) : 0;
});

const series = computed(() => data.value?.series ?? null);

const cards = computed<CardDef[]>(() => [
  {
    key: "keywords_created",
    group: "content",
    title: t("dashboard.cards.keywordsCreated"),
    value: n("keywords_created"),
    format: "number",
    sub: t("dashboard.sub.keywords", { total: formatNumber(n("keywords_total")), rate: dash(formatPercent(n("keyword_adopt_rate"))) }),
    compare: cmp("keywords_created"),
    help: t("dashboard.help.keywordsCreated"),
    to: linkIf("content.keywords.view", { name: "keywords" }),
  },
  {
    key: "titles_created",
    group: "content",
    title: t("dashboard.cards.titlesCreated"),
    value: n("titles_created"),
    format: "number",
    sub: t("dashboard.sub.titles", { total: formatNumber(n("titles_total")) }),
    compare: cmp("titles_created"),
    help: t("dashboard.help.titlesCreated"),
    to: linkIf("content.titles.view", { name: "titles" }),
  },
  {
    key: "contents_created",
    group: "content",
    title: t("dashboard.cards.contentsCreated"),
    value: n("contents_created"),
    format: "number",
    sub: t("dashboard.sub.contentsCreated", { approved: formatNumber(n("contents_approved")) }),
    compare: cmp("contents_created"),
    help: t("dashboard.help.contentsCreated"),
    to: linkIf("content.contents.view", { name: "contents" }),
  },
  {
    key: "contents_published",
    group: "content",
    title: t("dashboard.cards.contentsPublished"),
    value: n("contents_published"),
    format: "number",
    sub: t("dashboard.sub.contentsPublished", { total: formatNumber(n("contents_total")) }),
    compare: cmp("contents_published"),
    help: t("dashboard.help.contentsPublished"),
    to: linkIf("content.contents.view", { name: "contents", query: { status: "published" } }),
  },
  {
    key: "links_backfilled",
    group: "links",
    title: t("dashboard.cards.linksBackfilled"),
    value: n("links_backfilled"),
    format: "number",
    sub: t("dashboard.sub.links", { total: formatNumber(n("links_total")), checked: formatNumber(n("links_checked")) }),
    compare: cmp("links_backfilled"),
    sparkline: series.value?.links_backfilled,
    help: t("dashboard.help.linksBackfilled"),
    to: linkIf("publish.links.view", { name: "links" }),
  },
  {
    key: "link_alive_rate",
    group: "links",
    title: t("dashboard.cards.linkAliveRate"),
    value: n("link_alive_rate"),
    format: "percent",
    sub: t("dashboard.sub.alive", { alive: formatNumber(n("links_alive")), deleted: formatNumber(n("links_deleted")) }),
    help: t("dashboard.help.linkAliveRate"),
    to: linkIf("publish.links.view", { name: "links", query: { alive_status: "deleted" } }),
  },
  {
    key: "seo_index_rate",
    group: "links",
    title: t("dashboard.cards.seoIndexRate"),
    value: n("seo_index_rate"),
    format: "percent",
    sub: t("dashboard.sub.seo", { count: formatNumber(n("seo_newly_indexed")), hours: dash(formatHours(n("time_to_index_hours_avg"))) }),
    compare: cmp("seo_newly_indexed"),
    sparkline: series.value?.seo_newly_indexed,
    help: t("dashboard.help.seoIndexRate"),
    badge: t("stats.caliber.detect"),
    to: linkIf("publish.links.view", { name: "links", query: { seo_indexed_any: "0" } }),
  },
  {
    key: "geo_cite_rate",
    group: "links",
    title: t("dashboard.cards.geoCiteRate"),
    value: n("geo_cite_rate"),
    format: "percent",
    sub: t("dashboard.sub.geo", { count: formatNumber(n("geo_newly_cited")) }),
    compare: cmp("geo_newly_cited"),
    help: t("dashboard.help.geoCiteRate"),
    badge: t("stats.caliber.detect"),
    to: linkIf("publish.links.view", { name: "links", query: { geo_cited_any: "0" } }),
  },
  {
    key: "ai_calls",
    group: "ai",
    title: t("dashboard.cards.aiCalls"),
    value: n("ai_calls"),
    format: "number",
    sub: t("dashboard.sub.ai", {
      attempt: dash(formatPercent(n("ai_success_rate"))),
      task: dash(formatPercent(n("task_success_rate"))),
      duration: dash(formatDuration(n("ai_avg_duration_ms"))),
    }),
    compare: cmp("ai_calls"),
    sparkline: series.value?.ai_calls,
    help: t("dashboard.help.aiCalls"),
    to: linkIf("ai.tasks.view", { name: "ai-tasks" }),
  },
  {
    key: "cost_cny",
    group: "ai",
    title: t("dashboard.cards.costCny"),
    value: n("cost_cny"),
    format: "currency",
    sub: t("dashboard.sub.cost", { value: dash(formatCny(n("cost_cny_per_content"))) }),
    compare: cmp("cost_cny"),
    sparkline: series.value?.cost_cny,
    help: t("dashboard.help.costCny"),
    to: linkIf("stats.reports.view", { name: "stats-reports", query: { tab: "ai" } }),
  },
  {
    key: "tokens_total",
    group: "ai",
    title: t("dashboard.cards.tokens"),
    value: n("tokens_total"),
    format: "tokens",
    sub: t("dashboard.sub.tokens", { prompt: formatTokens(n("prompt_tokens")), completion: formatTokens(n("completion_tokens")) }),
    compare: cmp("tokens_total"),
    help: t("dashboard.help.tokens"),
    to: linkIf("stats.reports.view", { name: "stats-reports", query: { tab: "ai" } }),
  },
  {
    key: "quota_reconciled_rate",
    group: "ai",
    title: t("dashboard.cards.reconciledRate"),
    value: n("quota_reconciled_rate"),
    format: "percent",
    sub: t("dashboard.sub.reconciled", { actual: formatQuota(n("quota_actual")), estimated: formatQuota(n("quota_estimated")) }),
    compare: cmp("quota_reconciled_rate"),
    help: t("dashboard.help.reconciledRate"),
    to: linkIf("ai.usage.view", { name: "ai-usage" }),
  },
  {
    key: "images_generated",
    group: "media",
    title: t("dashboard.cards.imagesGenerated"),
    value: n("images_generated"),
    format: "number",
    sub: "",
    compare: cmp("images_generated"),
    help: t("dashboard.help.mediaGenerated"),
    to: linkIf("media.assets.view", { name: "media-assets", query: { kind: "image" } }),
  },
  {
    key: "videos_generated",
    group: "media",
    title: t("dashboard.cards.videosGenerated"),
    value: n("videos_generated"),
    format: "number",
    sub: "",
    compare: cmp("videos_generated"),
    help: t("dashboard.help.mediaGenerated"),
    to: linkIf("media.assets.view", { name: "media-assets", query: { kind: "video" } }),
  },
  {
    key: "media_success_rate",
    group: "media",
    title: t("dashboard.cards.mediaSuccessRate"),
    value: n("media_success_rate"),
    format: "percent",
    sub: t("dashboard.sub.media", { failed: formatNumber(n("media_failed")), image: capabilityDuration("image"), video: capabilityDuration("video") }),
    compare: cmp("media_success_rate"),
    help: t("dashboard.help.mediaSuccessRate"),
    to: linkIf("media.assets.view", { name: "media-assets", query: { status: "failed" } }),
  },
  {
    key: "alerts_opened",
    group: "media",
    title: t("dashboard.cards.alertsOpened"),
    value: n("alerts_opened"),
    format: "number",
    sub: t("dashboard.sub.alerts", { resolved: formatNumber(n("alerts_resolved")), open: formatNumber(alertsOpenTotal.value) }),
    compare: cmp("alerts_opened"),
    help: t("dashboard.help.alertsOpened"),
    invert: statsApi.LOWER_IS_BETTER.has("alerts_opened"),
    to: linkIf("monitoring.alerts.view", { name: "alerts" }),
  },
]);

const CARD_GROUPS: CardGroup[] = ["content", "links", "ai", "media"];
const groupedCards = computed(() => CARD_GROUPS.map((g) => ({ group: g, cards: cards.value.filter((c) => c.group === g) })));

// ---------- 趋势 ----------
const trendSeries = computed<TrendSeries[]>(() => {
  const s = series.value;
  if (!s) return [];
  return [
    { key: "ai_calls", name: t("stats.metrics.ai_calls"), data: s.ai_calls ?? [], unit: "count" },
    { key: "links_backfilled", name: t("stats.metrics.links_backfilled"), data: s.links_backfilled ?? [], unit: "count" },
    { key: "seo_newly_indexed", name: t("stats.metrics.seo_newly_indexed"), data: s.seo_newly_indexed ?? [], unit: "count" },
    { key: "cost_cny", name: t("stats.metrics.cost_cny"), data: s.cost_cny ?? [], unit: "currency" },
  ];
});

// ---------- 告警摘要 ----------
const canViewAlerts = computed(() => has("monitoring.alerts.view"));
const alertsOpen = computed(() => breakdowns.value?.alerts_open ?? null);
const criticalOpen = computed(() => Number(alertsOpen.value?.critical) || 0);

// ---------- 分解表 ----------
type StatusSource = "contents" | "keywords" | "titles";
const statusSource = ref<StatusSource>("contents");

interface StatusRow {
  status: string;
  count: number;
  ratio: number | null;
}

function toStatusRows(values: readonly string[], counts: Record<string, number> | undefined): StatusRow[] {
  const map = counts ?? {};
  const keys = [...values, ...Object.keys(map).filter((k) => !values.includes(k))];
  const total = keys.reduce((acc, k) => acc + (Number(map[k]) || 0), 0);
  return keys.map((k) => {
    const count = Number(map[k]) || 0;
    return { status: k, count, ratio: total > 0 ? count / total : null };
  });
}

const statusKind = computed<StatusEnumName>(() =>
  statusSource.value === "contents" ? "content_status" : statusSource.value === "keywords" ? "keyword_status" : "title_status",
);
const statusRows = computed<StatusRow[]>(() => {
  const b = breakdowns.value;
  if (!b) return [];
  if (statusSource.value === "keywords") return toStatusRows(KEYWORD_STATUS, b.keywords_by_status);
  if (statusSource.value === "titles") return toStatusRows(TITLE_STATUS, b.titles_by_status);
  return toStatusRows(CONTENT_STATUS, b.contents_by_status);
});
const linkRows = computed<StatusRow[]>(() => toStatusRows(LINK_ALIVE_STATUS, breakdowns.value?.links_by_status));
const canViewLinks = computed(() => has("publish.links.view"));

function linkRowClass({ row }: { row: StatusRow }): string {
  return row.status === "deleted" && canViewLinks.value ? "is-clickable-row" : "";
}

type CostSource = "capability" | "model";
const costSource = ref<CostSource>("capability");
const costRows = computed<(CostByCapabilityRow | CostByModelRow)[]>(() => {
  const b = breakdowns.value;
  if (!b) return [];
  return (costSource.value === "capability" ? b.cost_by_capability : b.cost_by_model) ?? [];
});

function costKey(row: CostByCapabilityRow | CostByModelRow): string {
  if ("capability" in row) return te(`stats.capability.${row.capability}`) ? t(`stats.capability.${row.capability}`) : row.capability;
  return row.model;
}

interface EngineRow {
  engine: string;
  kind: "seo" | "geo";
  hit: number | null;
  total: number | null;
  rate: number | null;
}

const engineRows = computed<EngineRow[]>(() => {
  const b = breakdowns.value;
  if (!b) return [];
  const rows: EngineRow[] = [];
  for (const [engine, v] of Object.entries(b.seo_index_rate_by_engine ?? {})) {
    if (v) rows.push({ engine, kind: "seo", hit: v.hit, total: v.total, rate: v.rate });
  }
  for (const [engine, v] of Object.entries(b.geo_cite_rate_by_engine ?? {})) {
    if (v) rows.push({ engine, kind: "geo", hit: v.hit, total: v.total, rate: v.rate });
  }
  return rows;
});

function engineLabel(row: EngineRow): string {
  const key = `status.${row.kind === "seo" ? "seo_engine" : "geo_engine"}.${row.engine}`;
  return te(key) ? t(key) : row.engine;
}

function pct(v: number | null | undefined): string {
  return dash(formatPercent(v ?? null, 1));
}
</script>

<template>
  <div class="dashboard">
    <el-card shadow="never" class="page-card">
      <template #header>
        <div class="page-header dash-header">
          <div class="dash-title">
            <h2 class="page-title">{{ title }}</h2>
            <el-tag :type="meta?.scope === 'owner' || !isAllScope ? 'info' : 'primary'" effect="plain" disable-transitions>{{ scopeBadge }}</el-tag>
          </div>
          <div class="dash-toolbar">
            <ProjectSelect width="180px" />
            <el-radio-group v-model="range">
              <el-radio-button v-for="r in STATS_RANGE" :key="r" :value="r">{{ t(`status.stats_range.${r}`) }}</el-radio-button>
            </el-radio-group>
            <el-button :icon="Refresh" :loading="refreshing" @click="load()">{{ t("common.refresh") }}</el-button>
            <el-tag v-if="meta?.cached" type="info" size="small" effect="plain" disable-transitions>{{ t("dashboard.cached") }}</el-tag>
            <el-tooltip :content="t('dashboard.dataTimeHelp')" placement="bottom">
              <el-tag type="info" effect="plain" disable-transitions>
                {{ meta?.computed_at ? t("dashboard.dataTime", { time: formatDateTime(meta.computed_at) }) : t("dashboard.notAggregated") }}
              </el-tag>
            </el-tooltip>
            <el-tooltip v-if="meta" :content="t('dashboard.todaySourceHelp')" placement="bottom">
              <el-tag :type="todaySourceType" disable-transitions>{{ t(`dashboard.todaySource.${meta.today_source}`) }}</el-tag>
            </el-tooltip>
          </div>
        </div>
      </template>

      <div v-if="meta" class="dash-meta text-secondary">
        <span>{{ t("dashboard.period", { start: meta.start_date, end: meta.end_date }) }}</span>
        <span v-if="meta.snapshot_date">{{ t("dashboard.snapshotDate", { date: meta.snapshot_date }) }}</span>
        <span>{{ t("stats.timezoneNote", { tz: meta.timezone }) }}</span>
        <span>{{ t("dashboard.autoRefresh") }}</span>
      </div>

      <el-alert v-for="w in warnings" :key="w" type="warning" :closable="false" show-icon :title="w" class="dash-alert" />
      <el-alert v-if="error && data" type="error" :closable="false" show-icon class="dash-alert">
        <template #title>
          {{ t("dashboard.loadFailed") }}：{{ error }}
          <el-button link type="primary" @click="load()">{{ t("dashboard.retry") }}</el-button>
        </template>
      </el-alert>
      <el-alert v-if="isEmptyData" type="info" :closable="false" show-icon :title="t('dashboard.empty')" class="dash-alert" />

      <!-- 首次加载：16 块骨架 -->
      <el-row v-if="initialLoading && !data" :gutter="16">
        <el-col v-for="i in 16" :key="i" :xs="24" :sm="12" :md="8" :lg="6" class="kpi-col">
          <KpiCard title="" :value="null" loading />
        </el-col>
      </el-row>

      <!-- 加载失败且无数据 -->
      <el-result v-else-if="!data && error" icon="error" :title="t('dashboard.loadFailed')" :sub-title="error">
        <template #extra>
          <el-button type="primary" :icon="Refresh" @click="load()">{{ t("dashboard.retry") }}</el-button>
        </template>
      </el-result>

      <template v-else-if="data">
        <section v-for="g in groupedCards" :key="g.group" class="kpi-group">
          <div class="kpi-group-title">{{ t(`dashboard.groups.${g.group}`) }}</div>
          <el-row :gutter="16">
            <el-col v-for="card in g.cards" :key="card.key" :xs="24" :sm="12" :md="8" :lg="6" class="kpi-col">
              <KpiCard
                :title="card.title"
                :value="card.value"
                :format="card.format"
                :sub="card.sub"
                :compare="card.compare"
                :sparkline="card.sparkline"
                :help="card.help"
                :to="card.to"
                :invert="card.invert"
                :badge="card.badge"
              />
            </el-col>
          </el-row>
        </section>
      </template>
    </el-card>

    <template v-if="data">
      <el-row :gutter="16" class="dash-row">
        <el-col :xs="24" :lg="canViewAlerts ? 16 : 24" class="dash-col">
          <el-card shadow="never" class="section-card">
            <template #header>
              <div class="section-header">
                <span class="section-title">{{ t("dashboard.trendTitle") }}</span>
                <span class="text-secondary small">{{ t("dashboard.trendHint") }}</span>
              </div>
            </template>
            <TrendChart :dates="data.series?.dates ?? []" :series="trendSeries" :height="300" />
          </el-card>
        </el-col>
        <el-col v-if="canViewAlerts" :xs="24" :lg="8" class="dash-col">
          <el-card shadow="never" class="section-card alert-card" :class="{ 'is-critical': criticalOpen > 0 }">
            <template #header>
              <div class="section-header">
                <span class="section-title">{{ t("dashboard.alertSummary") }}</span>
              </div>
            </template>
            <div class="alert-sub text-secondary">{{ t("dashboard.alertOpenBySeverity") }}</div>
            <div class="alert-severities">
              <div v-for="sev in [...ALERT_SEVERITY].reverse()" :key="sev" class="alert-sev" :class="`sev-${sev}`">
                <div class="alert-sev-count">{{ formatNumber(Number(alertsOpen?.[sev]) || 0) }}</div>
                <StatusTag kind="alert_severity" :value="sev" size="small" />
              </div>
            </div>
            <el-divider />
            <div class="alert-today">
              <div>
                <div class="text-secondary small">{{ t("dashboard.todayOpened") }}</div>
                <div class="alert-today-value">{{ alertsStore.summary ? formatNumber(alertsStore.summary.today_opened) : "--" }}</div>
              </div>
              <div>
                <div class="text-secondary small">{{ t("dashboard.todayResolved") }}</div>
                <div class="alert-today-value">{{ alertsStore.summary ? formatNumber(alertsStore.summary.today_resolved) : "--" }}</div>
              </div>
            </div>
            <div class="text-secondary small alert-note">{{ t("dashboard.alertTodayNote") }}</div>
            <el-button type="primary" plain class="alert-goto" @click="$router.push({ name: 'alerts', query: { status: 'open' } })">
              {{ t("dashboard.gotoAlerts") }}
            </el-button>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="dash-row">
        <el-col :xs="24" :lg="12" class="dash-col">
          <el-card shadow="never" class="section-card">
            <template #header>
              <div class="section-header">
                <span class="section-title">{{ t("dashboard.tables.contentStatus") }}</span>
                <el-radio-group v-model="statusSource" size="small">
                  <el-radio-button value="contents">{{ t("dashboard.tables.contents") }}</el-radio-button>
                  <el-radio-button value="keywords">{{ t("dashboard.tables.keywords") }}</el-radio-button>
                  <el-radio-button value="titles">{{ t("dashboard.tables.titles") }}</el-radio-button>
                </el-radio-group>
              </div>
            </template>
            <el-table :data="statusRows" size="small" class="dash-table">
              <el-table-column :label="t('dashboard.tables.status')" min-width="120">
                <template #default="{ row }"><StatusTag :kind="statusKind" :value="row.status" /></template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.count')" align="right" min-width="90">
                <template #default="{ row }">{{ formatNumber(row.count) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.ratio')" align="right" min-width="90">
                <template #default="{ row }">{{ pct(row.ratio) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="12" class="dash-col">
          <el-card shadow="never" class="section-card">
            <template #header>
              <div class="section-header">
                <span class="section-title">{{ t("dashboard.tables.linkStatus") }}</span>
                <span class="text-secondary small">{{ t("dashboard.tables.linkStatusHint") }}</span>
              </div>
            </template>
            <el-table
              :data="linkRows"
              size="small"
              class="dash-table"
              :row-class-name="linkRowClass"
              @row-click="(row: StatusRow) => row.status === 'deleted' && canViewLinks && $router.push({ name: 'links', query: { alive_status: 'deleted' } })"
            >
              <el-table-column :label="t('dashboard.tables.status')" min-width="120">
                <template #default="{ row }"><StatusTag kind="link_alive_status" :value="row.status" /></template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.count')" align="right" min-width="90">
                <template #default="{ row }">{{ formatNumber(row.count) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.ratio')" align="right" min-width="90">
                <template #default="{ row }">{{ pct(row.ratio) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
      </el-row>

      <el-row :gutter="16" class="dash-row">
        <el-col :xs="24" :lg="12" class="dash-col">
          <el-card shadow="never" class="section-card">
            <template #header>
              <div class="section-header">
                <span class="section-title">{{ t("dashboard.tables.aiCost") }}</span>
                <el-radio-group v-model="costSource" size="small">
                  <el-radio-button value="capability">{{ t("dashboard.tables.byCapability") }}</el-radio-button>
                  <el-radio-button value="model">{{ t("dashboard.tables.byModel") }}</el-radio-button>
                </el-radio-group>
              </div>
            </template>
            <el-table :data="costRows" size="small" class="dash-table">
              <el-table-column :label="costSource === 'capability' ? t('dashboard.tables.capability') : t('dashboard.tables.model')" min-width="130" fixed>
                <template #default="{ row }"><span :class="{ mono: costSource === 'model' }">{{ costKey(row) }}</span></template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.calls')" align="right" min-width="80">
                <template #default="{ row }">{{ formatNumber(row.ai_calls) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.successRate')" align="right" min-width="80">
                <template #default="{ row }">{{ pct(row.ai_success_rate) }}</template>
              </el-table-column>
              <el-table-column label="tokens" align="right" min-width="90">
                <template #default="{ row }">{{ formatTokens(row.tokens_total) }}</template>
              </el-table-column>
              <el-table-column align="right" min-width="110">
                <template #header>
                  <el-tooltip :content="t('stats.quotaHelp')" placement="top"><span class="th-help">{{ t("dashboard.tables.quotaEstimated") }}</span></el-tooltip>
                </template>
                <template #default="{ row }">{{ formatQuota(row.quota_estimated) }}</template>
              </el-table-column>
              <el-table-column align="right" min-width="110">
                <template #header>
                  <el-tooltip :content="t('stats.quotaHelp')" placement="top"><span class="th-help">{{ t("dashboard.tables.quotaActual") }}</span></el-tooltip>
                </template>
                <template #default="{ row }">{{ formatQuota(row.quota_actual) }}</template>
              </el-table-column>
              <el-table-column align="right" min-width="100">
                <template #header>
                  <el-tooltip :content="t('stats.costHelp')" placement="top"><span class="th-help">{{ t("dashboard.tables.cost") }}</span></el-tooltip>
                </template>
                <template #default="{ row }">{{ formatCny(row.cost_cny) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.costShare')" align="right" min-width="90">
                <template #default="{ row }">{{ pct(row.share) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
        <el-col :xs="24" :lg="12" class="dash-col">
          <el-card shadow="never" class="section-card">
            <template #header>
              <div class="section-header">
                <span class="section-title">{{ t("dashboard.tables.engineRates") }}</span>
                <el-tag size="small" type="info" effect="plain" disable-transitions>{{ t("stats.caliber.stock") }}</el-tag>
              </div>
            </template>
            <el-table :data="engineRows" size="small" class="dash-table">
              <el-table-column :label="t('dashboard.tables.engine')" min-width="120">
                <template #default="{ row }">{{ engineLabel(row) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.kind')" min-width="70">
                <template #default="{ row }">{{ row.kind === "seo" ? "SEO" : "GEO" }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.hit')" align="right" min-width="80">
                <template #default="{ row }">{{ row.hit === null ? "--" : formatNumber(row.hit) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.total')" align="right" min-width="90">
                <template #default="{ row }">{{ row.total === null ? "--" : formatNumber(row.total) }}</template>
              </el-table-column>
              <el-table-column :label="t('dashboard.tables.rate')" align="right" min-width="90">
                <template #default="{ row }">
                  <el-tooltip v-if="row.rate === null" :content="t('dashboard.tables.engineUnavailable')" placement="top">
                    <span class="text-secondary">--</span>
                  </el-tooltip>
                  <span v-else>{{ pct(row.rate) }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<style scoped>
.dash-header {
  flex-wrap: wrap;
}
.dash-title {
  display: flex;
  align-items: center;
  gap: 10px;
}
.dash-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.dash-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  font-size: 12px;
  margin-bottom: 12px;
}
.dash-alert {
  margin-bottom: 12px;
}
.kpi-group + .kpi-group {
  margin-top: 4px;
}
.kpi-group-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-regular);
  margin-bottom: 8px;
}
.kpi-col {
  margin-bottom: 16px;
}
.dash-row {
  margin-top: 12px;
}
.dash-col {
  margin-bottom: 4px;
}
.dash-row > .dash-col + .dash-col {
  margin-top: 0;
}
.section-card {
  height: 100%;
}
.section-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.section-title {
  font-weight: 600;
}
.small {
  font-size: 12px;
}
.dash-table {
  width: 100%;
}
.dash-table :deep(.is-clickable-row) {
  cursor: pointer;
}
.th-help {
  border-bottom: 1px dashed var(--el-text-color-placeholder);
  cursor: help;
}
.alert-card.is-critical {
  border-color: var(--el-color-danger);
}
.alert-sub {
  font-size: 12px;
  margin-bottom: 10px;
}
.alert-severities {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.alert-sev {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 10px 4px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
}
.alert-sev-count {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.sev-critical .alert-sev-count {
  color: var(--el-color-danger);
}
.sev-warning .alert-sev-count {
  color: var(--el-color-warning);
}
.alert-today {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.alert-today-value {
  font-size: 20px;
  font-weight: 600;
  margin-top: 2px;
  font-variant-numeric: tabular-nums;
}
.alert-note {
  margin-top: 8px;
}
.alert-goto {
  margin-top: 12px;
  width: 100%;
}
@media (max-width: 1199px) {
  .dash-col + .dash-col {
    margin-top: 12px;
  }
}
@media (max-width: 768px) {
  .dash-toolbar {
    width: 100%;
  }
}
</style>
