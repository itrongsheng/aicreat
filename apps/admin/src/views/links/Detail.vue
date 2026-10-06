<script setup lang="ts">
// 链接详情与时间线（docs/11 §11.3；docs/04 §6.17、§6.19、§7.11~§7.13）：
// 1. 头部：标题快照（跳转内容编辑器）、内容摘要、平台、原始 / 规范化 URL、发布账号 / 时间、回填人、alive_status、监控开关、操作按钮（同列表）；
// 2. 基线：baseline_title / excerpt / captured_at / simhash（十六进制），重建基线；
// 3. 收录状态表：每行一个引擎（seo_status / geo_status 的键，另补启用但尚未检测的引擎），表头显示 next_index_check_at；
// 4. 时间线：合并 GET /links/{id}/checks、GET /links/{id}/index-checks 与 GET /alerts?target_type=publish_link&target_id={id}
//    （仅 has('monitoring.alerts.view') 时请求告警）按时间倒序；点击检测记录打开 EvidenceDrawer，点击告警跳转告警中心并带同一组筛选；
// 5. Tab「删除检测历史」「收录检测历史」：分页表格（kind / engine 筛选）。
// alive_status=pending 时每 3s 刷新详情直到基线检测完成。
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ArrowLeft, Bell, CopyDocument, EditPen, Monitor, Refresh, Search } from "@element-plus/icons-vue";
import {
  DEFAULT_PAGE_SIZE,
  type Alert,
  type IndexCheck,
  type IndexKind,
  type LinkCheck,
  type PublishLink,
} from "@aicreat/shared";
import * as alertsApi from "@/api/alerts";
import { isApiError } from "@/api/client";
import * as linksApi from "@/api/links";
import EvidenceDrawer from "@/components/EvidenceDrawer.vue";
import LinkEditDialog from "@/components/LinkEditDialog.vue";
import LinkIndexCheckDialog from "@/components/LinkIndexCheckDialog.vue";
import LinkMarkIndexDialog from "@/components/LinkMarkIndexDialog.vue";
import StatusTag, { statusTagType } from "@/components/StatusTag.vue";
import ToolbarSelect, { type ToolbarOption } from "@/components/ToolbarSelect.vue";
import { copyText } from "@/composables/useAssetActions";
import { useIndexEngines } from "@/composables/useIndexEngines";
import { useLinkActions } from "@/composables/useLinkActions";
import { usePermission } from "@/composables/usePermission";
import { useNarrow } from "@/composables/useNarrow";
import { usePlatforms } from "@/composables/usePlatforms";
import { usePolling } from "@/composables/usePolling";
import { formatDateTime, formatDuration } from "@/utils/format";
import { isIndexOverdue, platformLabel, simhashHex } from "@/utils/links";

/** 时间线每类最多取的条数 */
const TIMELINE_LIMIT = 50;

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
const { has } = usePermission();
const engineCatalog = useIndexEngines();
const { byId: platformById, load: loadPlatforms } = usePlatforms();
const actions = useLinkActions();
const narrow = useNarrow();

const linkId = computed(() => {
  const n = Number(route.params.id);
  return Number.isInteger(n) && n > 0 ? n : 0;
});

const canUpdate = computed(() => has("publish.links.update"));
const canCheck = computed(() => has("publish.links.check"));
const canMark = computed(() => has("publish.links.mark"));
const canDelete = computed(() => has("publish.links.delete"));
const canViewAlerts = computed(() => has("monitoring.alerts.view"));
const canViewContent = computed(() => has("content.contents.view"));

const link = ref<PublishLink | null>(null);
const loading = ref(false);
const notFound = ref(false);

async function loadLink(silent = false) {
  const id = linkId.value;
  if (!id) {
    notFound.value = true;
    return;
  }
  if (!silent) loading.value = true;
  try {
    const res = await linksApi.get(id, { silent: true });
    if (id !== linkId.value) return;
    link.value = res;
    notFound.value = false;
  } catch (err) {
    if (isApiError(err) && err.code === 404) {
      notFound.value = true;
      link.value = null;
    } else if (silent) throw err;
  } finally {
    loading.value = false;
  }
}

const polling = usePolling(
  async () => {
    await loadLink(true);
    if (link.value && link.value.alive_status !== "pending") {
      polling.stop();
      void loadTimeline();
      void loadChecks();
    }
  },
  { interval: 3000, immediate: false },
);

watch(
  () => link.value?.alive_status,
  (status) => (status === "pending" ? polling.start() : polling.stop()),
);

// ---------- 头部 ----------
const platform = computed(() => {
  const l = link.value;
  if (!l) return null;
  return platformById.value.get(l.platform_id) ?? l.platform ?? null;
});
const overdue = computed(() => !!link.value && isIndexOverdue(link.value, engineCatalog.overdueDays.value));

async function toggleMonitoring(on: boolean) {
  const l = link.value;
  if (!l) return;
  const updated = await actions.setMonitoring(l, on);
  if (updated) link.value = { ...l, ...updated };
}

async function runCheck() {
  if (link.value && (await actions.check(link.value))) void reloadAll();
}

async function runRebaseline() {
  if (link.value && (await actions.rebaseline(link.value))) void reloadAll();
}

async function removeLink() {
  if (link.value && (await actions.remove(link.value))) void router.replace("/links");
}

const indexCheckVisible = ref(false);
const editVisible = ref(false);
const markVisible = ref(false);
const markPreset = ref<{ kind: IndexKind | null; engine: string | null }>({ kind: null, engine: null });

function openMark(kind: IndexKind | null = null, engine: string | null = null) {
  markPreset.value = { kind, engine };
  markVisible.value = true;
}

function onLinkUpdated(updated: PublishLink) {
  link.value = { ...(link.value ?? updated), ...updated };
  void loadTimeline();
  void loadIndexChecks();
}

// ---------- 收录状态表 ----------
interface EngineRow {
  kind: IndexKind;
  code: string;
  label: string;
  status: string | null;
  checkedAt: string | null;
  firstAt: string | null;
  checkCount: number | null;
  provider: string;
  enabled: boolean;
}

const engineRows = computed<EngineRow[]>(() => {
  const l = link.value;
  if (!l) return [];
  const rows: EngineRow[] = [];
  for (const kind of ["seo", "geo"] as IndexKind[]) {
    const map = ((kind === "seo" ? l.seo_status : l.geo_status) ?? {}) as Record<
      string,
      { status: string; checked_at: string | null; first_indexed_at?: string | null; first_cited_at?: string | null; check_count: number } | undefined
    >;
    const codes = new Set(Object.keys(map));
    for (const e of engineCatalog.engines.value) if (e.kind === kind && e.enabled) codes.add(e.code);
    for (const code of codes) {
      const st = map[code];
      const info = engineCatalog.find(kind, code);
      rows.push({
        kind,
        code,
        label: engineCatalog.label(kind, code),
        status: st?.status ?? null,
        checkedAt: st?.checked_at ?? null,
        firstAt: (kind === "seo" ? st?.first_indexed_at : st?.first_cited_at) ?? null,
        checkCount: st ? (st.check_count ?? 0) : null,
        provider: info?.provider ?? (kind === "geo" ? "zhiqi_model" : ""),
        enabled: !!info?.enabled,
      });
    }
  }
  return rows;
});

// ---------- 时间线 ----------
type TimelineItem =
  | { key: string; type: "link_check"; at: string; record: LinkCheck }
  | { key: string; type: "index_check"; at: string; record: IndexCheck }
  | { key: string; type: "alert"; at: string; record: Alert };

const timelineChecks = ref<LinkCheck[]>([]);
const timelineIndexChecks = ref<IndexCheck[]>([]);
const timelineAlerts = ref<Alert[]>([]);
const timelineLoading = ref(false);
const timelineTruncated = ref(false);

async function loadTimeline() {
  const id = linkId.value;
  if (!id) return;
  timelineLoading.value = true;
  const opts = { silent: true };
  const [checks, indexChecks, alerts] = await Promise.allSettled([
    linksApi.listChecks(id, { page: 1, page_size: TIMELINE_LIMIT }, opts),
    linksApi.listIndexChecks(id, { page: 1, page_size: TIMELINE_LIMIT }, opts),
    canViewAlerts.value
      ? alertsApi.list({ target_type: "publish_link", target_id: id, page: 1, page_size: TIMELINE_LIMIT }, opts)
      : Promise.resolve(null),
  ]);
  if (id !== linkId.value) return;
  timelineChecks.value = checks.status === "fulfilled" ? checks.value.items : [];
  timelineIndexChecks.value = indexChecks.status === "fulfilled" ? indexChecks.value.items : [];
  timelineAlerts.value = alerts.status === "fulfilled" && alerts.value ? alerts.value.items : [];
  timelineTruncated.value = [checks, indexChecks, alerts].some((r) => r.status === "fulfilled" && r.value && r.value.total > TIMELINE_LIMIT);
  timelineLoading.value = false;
}

const timeline = computed<TimelineItem[]>(() => {
  const items: TimelineItem[] = [
    ...timelineChecks.value.map((r) => ({ key: `c${r.id}`, type: "link_check" as const, at: r.checked_at, record: r })),
    ...timelineIndexChecks.value.map((r) => ({ key: `i${r.id}`, type: "index_check" as const, at: r.checked_at, record: r })),
    ...timelineAlerts.value.map((r) => ({ key: `a${r.id}`, type: "alert" as const, at: r.first_triggered_at, record: r })),
  ];
  return items.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime() || b.key.localeCompare(a.key));
});

const TAG_COLORS: Record<string, string> = {
  success: "var(--el-color-success)",
  warning: "var(--el-color-warning)",
  danger: "var(--el-color-danger)",
  primary: "var(--el-color-primary)",
  info: "var(--el-color-info)",
};

function itemColor(item: TimelineItem): string {
  if (item.type === "link_check") return TAG_COLORS[statusTagType("link_check_result", item.record.result_status)];
  if (item.type === "index_check") {
    return TAG_COLORS[statusTagType(item.record.kind === "geo" ? "geo_cite_status" : "seo_index_status", item.record.result_status)];
  }
  return TAG_COLORS[statusTagType("alert_type", item.record.alert_type)];
}

function onTimelineClick(item: TimelineItem) {
  if (item.type === "alert") {
    void router.push({ path: "/alerts", query: { target_type: "publish_link", target_id: String(linkId.value) } });
    return;
  }
  openEvidence(item.type === "link_check" ? "link" : "index", item.record);
}

// ---------- 证据抽屉 ----------
const evidenceVisible = ref(false);
const evidenceKind = ref<"link" | "index">("link");
const evidenceRecord = ref<LinkCheck | IndexCheck | null>(null);

function openEvidence(kind: "link" | "index", record: LinkCheck | IndexCheck) {
  evidenceKind.value = kind;
  evidenceRecord.value = record;
  evidenceVisible.value = true;
}

// ---------- 历史 Tab ----------
const tab = ref<"timeline" | "checks" | "index">("timeline");

const checks = ref<LinkCheck[]>([]);
const checksTotal = ref(0);
const checksPage = ref(1);
const checksPageSize = ref(DEFAULT_PAGE_SIZE);
const checksLoading = ref(false);

async function loadChecks() {
  const id = linkId.value;
  if (!id) return;
  checksLoading.value = true;
  try {
    const res = await linksApi.listChecks(id, { page: checksPage.value, page_size: checksPageSize.value });
    if (id !== linkId.value) return;
    checks.value = res.items;
    checksTotal.value = res.total;
  } catch {
    checks.value = [];
    checksTotal.value = 0;
  } finally {
    checksLoading.value = false;
  }
}

const indexChecks = ref<IndexCheck[]>([]);
const indexTotal = ref(0);
const indexPage = ref(1);
const indexPageSize = ref(DEFAULT_PAGE_SIZE);
const indexLoading = ref(false);
const indexFilters = ref({ kind: undefined as IndexKind | undefined, engine: undefined as string | undefined });

const engineFilterOptions = computed<ToolbarOption[]>(() =>
  engineCatalog.engines.value
    .filter((e) => !indexFilters.value.kind || e.kind === indexFilters.value.kind)
    .map((e) => ({ value: e.code, label: `${engineCatalog.label(e.kind, e.code)}（${e.kind.toUpperCase()}）` })),
);

async function loadIndexChecks() {
  const id = linkId.value;
  if (!id) return;
  indexLoading.value = true;
  try {
    const res = await linksApi.listIndexChecks(id, {
      page: indexPage.value,
      page_size: indexPageSize.value,
      kind: indexFilters.value.kind,
      engine: indexFilters.value.engine,
    });
    if (id !== linkId.value) return;
    indexChecks.value = res.items;
    indexTotal.value = res.total;
  } catch {
    indexChecks.value = [];
    indexTotal.value = 0;
  } finally {
    indexLoading.value = false;
  }
}

function searchIndex() {
  indexPage.value = 1;
  void loadIndexChecks();
}

function onIndexKindChange() {
  if (indexFilters.value.engine && !engineFilterOptions.value.some((o) => o.value === indexFilters.value.engine)) indexFilters.value.engine = undefined;
  searchIndex();
}

function indexEnum(kind: IndexKind) {
  return kind === "geo" ? "geo_cite_status" : "seo_index_status";
}

// ---------- 加载 ----------
async function reloadAll() {
  await loadLink(true).catch(() => undefined);
  void loadTimeline();
  void loadChecks();
  void loadIndexChecks();
}

async function init() {
  link.value = null;
  notFound.value = false;
  timelineChecks.value = [];
  timelineIndexChecks.value = [];
  timelineAlerts.value = [];
  checksPage.value = 1;
  indexPage.value = 1;
  await loadLink();
  if (!link.value) return;
  void loadTimeline();
  void loadChecks();
  void loadIndexChecks();
}

watch(linkId, (id, old) => {
  if (id && id !== old) void init();
});

onMounted(() => {
  void loadPlatforms();
  void engineCatalog.load();
  void init();
});

function back() {
  if (window.history.state?.back) router.back();
  else void router.push("/links");
}
</script>

<template>
  <div v-loading="loading && !link" class="link-detail">
    <el-result v-if="notFound" icon="warning" :title="t('links.detailPage.notFound')">
      <template #extra>
        <el-button type="primary" @click="router.push('/links')">{{ t("links.detailPage.backToList") }}</el-button>
      </template>
    </el-result>

    <template v-else-if="link">
      <!-- 头部 -->
      <el-card shadow="never" class="page-card">
        <template #header>
          <div class="page-header">
            <div class="title-wrap">
              <el-button link :icon="ArrowLeft" @click="back" />
              <h2 class="page-title">{{ t("menu.linkDetail") }} #{{ link.id }}</h2>
              <StatusTag kind="link_alive_status" :value="link.alive_status" size="default" />
              <el-tag v-if="overdue" type="warning" effect="plain" disable-transitions>{{ t("links.overdue") }}</el-tag>
            </div>
            <div class="header-actions">
              <span class="monitor-switch">
                <span class="text-secondary">{{ t("links.monitoring") }}</span>
                <el-switch
                  :model-value="link.is_monitoring"
                  :disabled="!canUpdate"
                  :loading="actions.acting.value === link.id"
                  @change="(v: string | number | boolean) => toggleMonitoring(!!v)"
                />
              </span>
              <el-button v-if="canCheck" :icon="Monitor" :loading="actions.acting.value === link.id" @click="runCheck">{{ t("links.actions.check") }}</el-button>
              <el-button v-if="canCheck" :icon="Search" :disabled="link.alive_status === 'deleted' || !link.is_monitoring" @click="indexCheckVisible = true">
                {{ t("links.actions.indexCheck") }}
              </el-button>
              <el-button v-if="canMark" @click="openMark()">{{ t("links.actions.mark") }}</el-button>
              <el-button v-if="canUpdate" :icon="EditPen" @click="editVisible = true">{{ t("common.edit") }}</el-button>
              <el-button v-if="canDelete" type="danger" plain @click="removeLink">{{ t("common.delete") }}</el-button>
              <el-button :icon="Refresh" @click="reloadAll">{{ t("common.refresh") }}</el-button>
            </div>
          </div>
        </template>

        <el-descriptions :column="narrow ? 1 : 2" border size="small" class="desc">
          <el-descriptions-item :label="t('links.titleSnapshot')" :span="2">
            <router-link v-if="canViewContent" :to="`/contents/${link.content_id}`" class="plain-link">{{ link.title_snapshot || `#${link.content_id}` }}</router-link>
            <span v-else>{{ link.title_snapshot || `#${link.content_id}` }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.content')">
            <span class="text-secondary">#{{ link.content_id }}</span>
            <span v-if="link.content" class="content-title">{{ link.content.title }}</span>
            <StatusTag v-if="link.content" kind="content_status" :value="link.content.status" />
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.platform')">{{ platformLabel(platform, link.platform_id) }}<span v-if="platform" class="text-secondary">（{{ platform.code }}）</span></el-descriptions-item>
          <el-descriptions-item :label="t('links.originalUrl')" :span="2">
            <a :href="link.url" target="_blank" rel="noopener noreferrer" class="break">{{ link.url }}</a>
            <el-button link size="small" :icon="CopyDocument" :title="t('common.copy')" @click="copyText(link.url)" />
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.normalizedUrl')" :span="2">
            <span class="mono break">{{ link.normalized_url }}</span>
            <el-button link size="small" :icon="CopyDocument" :title="t('common.copy')" @click="copyText(link.normalized_url)" />
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.domain')">{{ link.domain }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.publishAccount')">{{ link.publish_account || "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.publishedAt')">{{ formatDateTime(link.published_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.backfilledBy')">{{ link.backfilled_by ? `#${link.backfilled_by}` : "-" }} · {{ formatDateTime(link.created_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.aliveChangedAt')">{{ formatDateTime(link.alive_changed_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.lastCheckedAt')">
            {{ formatDateTime(link.last_checked_at) }}
            <span class="text-secondary">（{{ t("links.lastHttpStatus") }} {{ link.last_http_status ?? "-" }}）</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.nextCheckAt')">{{ formatDateTime(link.next_check_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.checkCounters')">
            {{ t("links.checkCountersText", { total: link.check_count, unknown: link.consecutive_unknown, suspected: link.consecutive_suspected }) }}
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.lastCheck')" :span="2">
            <template v-if="link.last_check">
              <StatusTag kind="link_check_result" :value="link.last_check.result_status" />
              <StatusTag kind="link_check_rule" :value="link.last_check.matched_rule" effect="plain" class="ml" />
              <span class="text-secondary ml">{{ formatDateTime(link.last_check.checked_at) }}</span>
              <el-button link type="primary" size="small" class="ml" @click="openEvidence('link', link.last_check)">{{ t("links.viewEvidence") }}</el-button>
            </template>
            <span v-else class="text-secondary">{{ t("links.noCheckYet") }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="t('links.note')" :span="2">
            <span class="pre">{{ link.note || "-" }}</span>
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <!-- 基线 -->
      <el-card shadow="never" class="page-card">
        <template #header>
          <div class="page-header">
            <h3 class="card-title">{{ t("links.baseline.title") }}</h3>
            <el-button v-if="canCheck" size="small" :loading="actions.acting.value === link.id" @click="runRebaseline">{{ t("links.actions.rebaseline") }}</el-button>
          </div>
        </template>
        <el-descriptions v-if="link.baseline_captured_at" :column="narrow ? 1 : 2" border size="small" class="desc">
          <el-descriptions-item :label="t('links.baseline.capturedAt')">{{ formatDateTime(link.baseline_captured_at) }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.baseline.simhash')"><span class="mono">{{ simhashHex(link.baseline_simhash) }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('links.baseline.pageTitle')" :span="2">{{ link.baseline_title || "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('links.baseline.excerpt')" :span="2">
            <div class="excerpt">{{ link.baseline_excerpt || "-" }}</div>
          </el-descriptions-item>
        </el-descriptions>
        <el-empty v-else :description="link.alive_status === 'pending' ? t('links.baseline.pending') : t('links.baseline.empty')" :image-size="60" />
      </el-card>

      <!-- 收录状态 -->
      <el-card shadow="never" class="page-card">
        <template #header>
          <div class="page-header">
            <h3 class="card-title">{{ t("links.indexStatus.title") }}</h3>
            <div class="index-meta text-secondary">
              <span>{{ t("links.indexStatus.next") }}：{{ formatDateTime(link.next_index_check_at) }}</span>
              <span>{{ t("links.indexStatus.last") }}：{{ formatDateTime(link.last_index_checked_at) }}</span>
              <span>{{ t("links.indexStatus.rounds", { count: link.index_check_count, done: link.index_checks_done }) }}</span>
              <span>{{ t("links.firstIndexedAt") }}：{{ formatDateTime(link.first_indexed_at) }}</span>
              <span>{{ t("links.firstCitedAt") }}：{{ formatDateTime(link.first_cited_at) }}</span>
            </div>
          </div>
        </template>
        <el-table :data="engineRows" size="small" stripe :empty-text="t('links.indexStatus.empty')">
          <el-table-column :label="t('links.indexStatus.kind')" width="80">
            <template #default="{ row }"><StatusTag kind="index_kind" :value="row.kind" effect="plain" /></template>
          </el-table-column>
          <el-table-column :label="t('links.indexStatus.engine')" min-width="120">
            <template #default="{ row }">
              {{ row.label }}
              <el-tag v-if="!row.enabled" size="small" type="info" effect="plain" disable-transitions class="ml">{{ t("links.indexStatus.disabled") }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('common.status')" width="100">
            <template #default="{ row }">
              <StatusTag v-if="row.status" :kind="indexEnum(row.kind)" :value="row.status" />
              <span v-else class="text-secondary">{{ t("links.indexStatus.notChecked") }}</span>
            </template>
          </el-table-column>
          <el-table-column :label="t('links.checkedAt')" width="160">
            <template #default="{ row }">{{ formatDateTime(row.checkedAt) }}</template>
          </el-table-column>
          <el-table-column :label="t('links.indexStatus.firstAt')" width="160">
            <template #default="{ row }">{{ formatDateTime(row.firstAt) }}</template>
          </el-table-column>
          <el-table-column :label="t('links.engineCheckCount')" width="90" align="right">
            <template #default="{ row }">{{ row.checkCount ?? "-" }}</template>
          </el-table-column>
          <el-table-column :label="t('links.indexStatus.provider')" min-width="130">
            <template #default="{ row }">
              <StatusTag v-if="row.provider" :kind="row.kind === 'geo' ? 'geo_provider' : 'seo_provider'" :value="row.provider" effect="plain" />
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column v-if="canMark" :label="t('common.actions')" width="90">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openMark(row.kind, row.code)">{{ t("links.actions.markShort") }}</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <!-- 时间线与历史 -->
      <el-card shadow="never" class="page-card">
        <el-tabs v-model="tab">
          <el-tab-pane name="timeline" :label="t('links.timeline.title')">
            <div v-loading="timelineLoading" class="timeline-wrap">
              <el-alert v-if="!canViewAlerts" type="info" :title="t('links.timeline.noAlertPermission')" :closable="false" show-icon class="mb" />
              <el-alert v-if="timelineTruncated" type="info" :title="t('links.timeline.truncated', { n: TIMELINE_LIMIT })" :closable="false" show-icon class="mb" />
              <el-timeline v-if="timeline.length">
                <el-timeline-item v-for="item in timeline" :key="item.key" :timestamp="formatDateTime(item.at)" :color="itemColor(item)" placement="top">
                  <div class="tl-item" role="button" tabindex="0" @click="onTimelineClick(item)" @keyup.enter="onTimelineClick(item)">
                    <template v-if="item.type === 'link_check'">
                      <div class="tl-head">
                        <el-icon><Monitor /></el-icon>
                        <span class="tl-type">{{ t("links.timeline.linkCheck") }}</span>
                        <StatusTag kind="check_type" :value="item.record.check_type" effect="plain" />
                        <StatusTag kind="link_check_result" :value="item.record.result_status" />
                        <StatusTag kind="link_check_rule" :value="item.record.matched_rule" effect="plain" />
                      </div>
                      <div class="tl-body text-secondary">
                        <span>{{ t(`status.link_alive_status.${item.record.previous_status}`) }} → {{ t(`status.link_alive_status.${item.record.applied_status}`) }}</span>
                        <span>HTTP {{ item.record.http_status ?? "-" }}</span>
                        <span>{{ formatDuration(item.record.duration_ms) }}</span>
                        <span v-if="item.record.error_message" class="tl-error">{{ item.record.error_message }}</span>
                      </div>
                    </template>
                    <template v-else-if="item.type === 'index_check'">
                      <div class="tl-head">
                        <el-icon><Search /></el-icon>
                        <span class="tl-type">{{ t("links.timeline.indexCheck") }}</span>
                        <StatusTag kind="index_kind" :value="item.record.kind" effect="plain" />
                        <span>{{ engineCatalog.label(item.record.kind, item.record.engine) }}</span>
                        <StatusTag :kind="indexEnum(item.record.kind)" :value="item.record.result_status" />
                      </div>
                      <div class="tl-body text-secondary">
                        <StatusTag :kind="item.record.kind === 'geo' ? 'geo_provider' : 'seo_provider'" :value="item.record.provider" effect="plain" />
                        <StatusTag kind="check_type" :value="item.record.check_type" effect="plain" />
                        <span>{{ formatDuration(item.record.duration_ms) }}</span>
                        <span v-if="item.record.error_message" class="tl-error">{{ item.record.error_message }}</span>
                      </div>
                    </template>
                    <template v-else>
                      <div class="tl-head">
                        <el-icon><Bell /></el-icon>
                        <span class="tl-type">{{ t("links.timeline.alert") }}</span>
                        <StatusTag kind="alert_type" :value="item.record.alert_type" />
                        <StatusTag kind="alert_severity" :value="item.record.severity" effect="plain" />
                        <StatusTag kind="alert_status" :value="item.record.status" />
                      </div>
                      <div class="tl-body text-secondary">
                        <span>{{ item.record.title }}</span>
                        <span>{{ t("links.timeline.triggerCount", { n: item.record.trigger_count }) }}</span>
                      </div>
                    </template>
                  </div>
                </el-timeline-item>
              </el-timeline>
              <el-empty v-else-if="!timelineLoading" :description="t('links.timeline.empty')" :image-size="60" />
            </div>
          </el-tab-pane>

          <el-tab-pane name="checks" :label="t('links.history.checks')">
            <el-table v-loading="checksLoading" :data="checks" size="small" stripe class="clickable" @row-click="(row: LinkCheck) => openEvidence('link', row)">
              <el-table-column :label="t('links.checkedAt')" width="160">
                <template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template>
              </el-table-column>
              <el-table-column :label="t('links.history.checkType')" width="80">
                <template #default="{ row }"><StatusTag kind="check_type" :value="row.check_type" effect="plain" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.result')" width="100">
                <template #default="{ row }"><StatusTag kind="link_check_result" :value="row.result_status" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.transition')" min-width="170">
                <template #default="{ row }">
                  <StatusTag kind="link_alive_status" :value="row.previous_status" effect="plain" />
                  <span class="arrow">→</span>
                  <StatusTag kind="link_alive_status" :value="row.applied_status" />
                </template>
              </el-table-column>
              <el-table-column label="HTTP" width="70">
                <template #default="{ row }">{{ row.http_status ?? "-" }}</template>
              </el-table-column>
              <el-table-column :label="t('links.history.rule')" min-width="130">
                <template #default="{ row }"><StatusTag kind="link_check_rule" :value="row.matched_rule" effect="plain" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.redirects')" width="80" align="right">
                <template #default="{ row }">{{ row.redirect_count ?? 0 }}</template>
              </el-table-column>
              <el-table-column :label="t('links.history.hamming')" width="90" align="right">
                <template #default="{ row }">{{ row.hamming_distance ?? "-" }}</template>
              </el-table-column>
              <el-table-column :label="t('links.history.duration')" width="90" align="right">
                <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
              </el-table-column>
              <el-table-column :label="t('links.history.triggeredBy')" width="90">
                <template #default="{ row }">{{ row.triggered_by ? `#${row.triggered_by}` : t("evidence.system") }}</template>
              </el-table-column>
            </el-table>
            <div class="pagination">
              <el-pagination
                v-model:current-page="checksPage"
                v-model:page-size="checksPageSize"
                :page-sizes="[20, 50, 100]"
                :total="checksTotal"
                layout="total, sizes, prev, pager, next"
                background
                @current-change="loadChecks"
                @size-change="
                  checksPage = 1;
                  loadChecks();
                "
              />
            </div>
          </el-tab-pane>

          <el-tab-pane name="index" :label="t('links.history.indexChecks')">
            <div class="toolbar">
              <ToolbarSelect v-model="indexFilters.kind" enum-name="index_kind" :placeholder="t('links.indexStatus.kind')" width="110px" @change="onIndexKindChange" />
              <ToolbarSelect v-model="indexFilters.engine" :options="engineFilterOptions" :placeholder="t('links.indexStatus.engine')" width="170px" @change="searchIndex" />
            </div>
            <el-table v-loading="indexLoading" :data="indexChecks" size="small" stripe class="clickable" @row-click="(row: IndexCheck) => openEvidence('index', row)">
              <el-table-column :label="t('links.checkedAt')" width="160">
                <template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template>
              </el-table-column>
              <el-table-column :label="t('links.indexStatus.kind')" width="70">
                <template #default="{ row }"><StatusTag kind="index_kind" :value="row.kind" effect="plain" /></template>
              </el-table-column>
              <el-table-column :label="t('links.indexStatus.engine')" width="120">
                <template #default="{ row }">{{ engineCatalog.label(row.kind, row.engine) }}</template>
              </el-table-column>
              <el-table-column :label="t('links.indexStatus.provider')" width="140">
                <template #default="{ row }"><StatusTag :kind="row.kind === 'geo' ? 'geo_provider' : 'seo_provider'" :value="row.provider" effect="plain" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.result')" width="90">
                <template #default="{ row }"><StatusTag :kind="indexEnum(row.kind)" :value="row.result_status" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.matchMode')" width="100">
                <template #default="{ row }"><StatusTag kind="index_match_mode" :value="row.match_mode" effect="plain" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.confidence')" width="80" align="right">
                <template #default="{ row }">{{ row.confidence == null ? "-" : Number(row.confidence).toFixed(2) }}</template>
              </el-table-column>
              <el-table-column :label="t('links.history.model')" min-width="120" show-overflow-tooltip>
                <template #default="{ row }"><span class="mono">{{ row.model || "-" }}</span></template>
              </el-table-column>
              <el-table-column :label="t('links.history.errorCategory')" width="120">
                <template #default="{ row }"><StatusTag kind="error_category" :value="row.error_category" /></template>
              </el-table-column>
              <el-table-column :label="t('links.history.duration')" width="90" align="right">
                <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
              </el-table-column>
            </el-table>
            <div class="pagination">
              <el-pagination
                v-model:current-page="indexPage"
                v-model:page-size="indexPageSize"
                :page-sizes="[20, 50, 100]"
                :total="indexTotal"
                layout="total, sizes, prev, pager, next"
                background
                @current-change="loadIndexChecks"
                @size-change="searchIndex"
              />
            </div>
          </el-tab-pane>
        </el-tabs>
      </el-card>

      <EvidenceDrawer v-model="evidenceVisible" :kind="evidenceKind" :record="evidenceRecord" :show-link="false" />
      <LinkIndexCheckDialog v-model="indexCheckVisible" :link="link" @queued="loadTimeline" />
      <LinkMarkIndexDialog v-model="markVisible" :link="link" :preset-kind="markPreset.kind" :preset-engine="markPreset.engine" @saved="onLinkUpdated" />
      <LinkEditDialog v-model="editVisible" :link="link" @saved="onLinkUpdated" />
    </template>
  </div>
</template>

<style scoped>
.link-detail {
  min-height: 200px;
}
.title-wrap {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.page-header {
  flex-wrap: wrap;
}
.header-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.header-actions > * {
  margin-left: 0 !important;
}
.monitor-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-right: 4px;
  font-size: 13px;
}
.card-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.desc :deep(.el-descriptions__label) {
  width: 120px;
}
@media (max-width: 768px) {
  .desc :deep(.el-descriptions__label) {
    width: 84px;
  }
}
.plain-link {
  color: var(--el-color-primary);
  text-decoration: none;
}
.content-title {
  margin: 0 6px;
}
.break {
  word-break: break-all;
}
.pre {
  white-space: pre-wrap;
}
.excerpt {
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.6;
}
.ml {
  margin-left: 6px;
}
.mb {
  margin-bottom: 10px;
}
.index-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  font-size: 12px;
}
.timeline-wrap {
  min-height: 80px;
  padding-top: 4px;
}
.tl-item {
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
}
.tl-item:hover,
.tl-item:focus-visible {
  background: var(--el-fill-color-light);
  outline: none;
}
.tl-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.tl-type {
  font-weight: 600;
}
.tl-body {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 12px;
  margin-top: 4px;
  font-size: 12px;
}
.tl-error {
  color: var(--el-color-danger);
  word-break: break-all;
}
.arrow {
  margin: 0 4px;
  color: var(--el-text-color-secondary);
}
.clickable :deep(.el-table__row) {
  cursor: pointer;
}
</style>
