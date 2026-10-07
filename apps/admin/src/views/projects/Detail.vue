<script setup lang="ts">
// 项目详情（docs/09 §4.2、§4.3、docs/04 §6.7、§7.3、docs/13 §7.1）三个 Tab：
// - 概览：项目基本信息 + GET /admin/projects/{id}/overview（结构同 /admin/stats/overview）的 KPI 卡片（KpiCard：环比、迷你趋势）与状态分布；
// - 默认模板：每个 prompt_kind 一个下拉，只列该 kind、project_id ∈ {0, 该项目} 的 published 模板，未选即按系统缺省 code 回退；
//   保存走 PUT /admin/projects/{id}（400 loc=["body","default_templates","<kind>"] 定位到对应行）；
// - 默认模型：keyword / title / content / rewrite 四个文本能力各一行 ModelSelect（modality=text）+ 备选链 + params，
//   image / video / geo_check / seo_check 同页展示；保存走 PUT /admin/projects/{id}/routes（未开启覆盖的能力删除覆盖行）。
// 不可见 / 不存在的项目统一 404 → 显示空态。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowDown, ArrowLeft, ArrowUp, Delete, Edit, Plus, Refresh } from "@element-plus/icons-vue";
import {
  CAPABILITIES,
  MODALITY_OF,
  PROMPT_DEFAULT_CODES,
  PROMPT_KIND,
  STATS_RANGE,
  TEXT_CAPABILITIES,
  type Capability,
  type CapabilityRoute,
  type Project,
  type ProjectBody,
  type ProjectRouteInput,
  type PromptKind,
  type PromptTemplate,
  type Protocol,
  type StatsOverview,
  type StatsOverviewKpis,
  type StatsRange,
  type ValidationErrorItem,
} from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import { isApiError, validationErrors } from "@/api/client";
import * as projectsApi from "@/api/projects";
import * as promptTemplatesApi from "@/api/promptTemplates";
import JsonEditor from "@/components/JsonEditor.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import KpiCard, { type KpiFormat } from "@/components/KpiCard.vue";
import StatusTag from "@/components/StatusTag.vue";
import { usePermission } from "@/composables/usePermission";
import { useAuthStore } from "@/store/auth";
import { useProjectStore } from "@/store/project";
import { formatCny, formatDateTime, formatNumber } from "@/utils/format";

type TabName = "overview" | "templates" | "models";
const TABS: TabName[] = ["overview", "templates", "models"];

const { t } = useI18n();
const { has, isAllScope } = usePermission();
const auth = useAuthStore();
const projectStore = useProjectStore();
const route = useRoute();
const router = useRouter();

const projectId = computed(() => Number(route.params.id));
const project = ref<Project | null>(null);
const loading = ref(false);
const notFound = ref(false);

const tab = ref<TabName>(TABS.includes(route.query.tab as TabName) ? (route.query.tab as TabName) : "overview");
watch(tab, (v) => void router.replace({ query: { ...route.query, tab: v === "overview" ? undefined : v } }));

const canUpdate = computed(() => has("content.projects.update"));

async function loadProject() {
  const id = projectId.value;
  if (!Number.isInteger(id) || id <= 0) {
    notFound.value = true;
    return;
  }
  loading.value = true;
  try {
    project.value = await projectsApi.get(id, { silent: true });
    notFound.value = false;
    initTemplateSelection();
    initRouteRows();
  } catch (err) {
    if (isApiError(err) && err.code === 404) notFound.value = true;
    else if (isApiError(err)) ElMessage.error(err.message);
  } finally {
    loading.value = false;
  }
}

function ownerLabel(p: Project): string {
  if (p.owner) return p.owner.display_name || p.owner.username;
  const o = projectStore.owners.find((x) => x.id === p.owner_id);
  if (o) return o.display_name || o.username;
  if (p.owner_id === auth.admin?.id) return auth.displayName;
  return `#${p.owner_id}`;
}

function gotoEdit() {
  if (!project.value) return;
  void router.push({ name: "projects", query: { edit: String(project.value.id) } });
}

/** 保存项目时提交完整字段（PUT 语义），只改变 default_templates */
function bodyOf(p: Project, defaultTemplates: Partial<Record<PromptKind, number>>): ProjectBody {
  const body: ProjectBody = {
    name: p.name,
    slug: p.slug,
    industry: p.industry,
    audience: p.audience,
    brand_name: p.brand_name,
    brand_info: p.brand_info,
    description: p.description,
    language: p.language,
    default_style: p.default_style,
    default_format: p.default_format,
    default_templates: defaultTemplates,
    default_platform_ids: [...(p.default_platform_ids ?? [])],
  };
  if (isAllScope.value) body.owner_id = p.owner_id;
  return body;
}

// =====================================================================
// 概览
// =====================================================================
const range = ref<StatsRange>("7d");
const overview = ref<StatsOverview | null>(null);
const overviewLoading = ref(false);
const overviewFailed = ref(false);

async function loadOverview() {
  if (!project.value) return;
  overviewLoading.value = true;
  overviewFailed.value = false;
  try {
    overview.value = await projectsApi.overview(project.value.id, { range: range.value }, { silent: true });
  } catch {
    overview.value = null;
    overviewFailed.value = true;
  } finally {
    overviewLoading.value = false;
  }
}

watch(range, () => void loadOverview());

/** 项目 KPI 卡（KpiCard，结构同总览）：主值、环比（compare 有该指标时）、迷你趋势（series）与当前值副值 */
interface ProjectKpi {
  key: keyof StatsOverviewKpis;
  format: KpiFormat;
  /** 环比所用指标（当前值指标无环比） */
  compareKey?: keyof StatsOverviewKpis;
  spark?: "ai_calls" | "cost_cny" | "links_backfilled" | "seo_newly_indexed";
  sub?: () => string;
}

function kpi(key: keyof StatsOverviewKpis): number | null {
  const v = overview.value?.kpis?.[key];
  return v === undefined ? null : (v as number | null);
}

const KPI_ITEMS: ProjectKpi[] = [
  { key: "keywords_total", format: "number", sub: () => `${t("stats.metrics.keywords_created")} ${formatNumber(kpi("keywords_created"))}`, compareKey: "keywords_created" },
  { key: "keyword_adopt_rate", format: "percent", compareKey: "keyword_adopt_rate" },
  { key: "titles_total", format: "number", sub: () => `${t("stats.metrics.titles_created")} ${formatNumber(kpi("titles_created"))}`, compareKey: "titles_created" },
  { key: "contents_total", format: "number", sub: () => `${t("stats.metrics.contents_created")} ${formatNumber(kpi("contents_created"))}`, compareKey: "contents_created" },
  { key: "contents_approved", format: "number", compareKey: "contents_approved" },
  { key: "contents_published", format: "number", compareKey: "contents_published" },
  {
    key: "links_total",
    format: "number",
    sub: () => `${t("stats.metrics.links_backfilled")} ${formatNumber(kpi("links_backfilled"))}`,
    compareKey: "links_backfilled",
    spark: "links_backfilled",
  },
  {
    key: "link_alive_rate",
    format: "percent",
    sub: () => t("dashboard.sub.alive", { alive: formatNumber(kpi("links_alive")), deleted: formatNumber(kpi("links_deleted")) }),
  },
  {
    key: "seo_index_rate",
    format: "percent",
    sub: () => `${t("stats.metrics.seo_newly_indexed")} ${formatNumber(kpi("seo_newly_indexed"))}`,
    compareKey: "seo_newly_indexed",
    spark: "seo_newly_indexed",
  },
  { key: "geo_cite_rate", format: "percent", sub: () => `${t("stats.metrics.geo_newly_cited")} ${formatNumber(kpi("geo_newly_cited"))}`, compareKey: "geo_newly_cited" },
  { key: "ai_calls", format: "number", compareKey: "ai_calls", spark: "ai_calls" },
  { key: "task_success_rate", format: "percent", compareKey: "task_success_rate" },
  { key: "tokens_total", format: "tokens", compareKey: "tokens_total" },
  {
    key: "cost_cny",
    format: "currency",
    sub: () => `${t("stats.metrics.cost_cny_per_content")} ${formatCny(kpi("cost_cny_per_content"))}`,
    compareKey: "cost_cny",
    spark: "cost_cny",
  },
];

/** 只展示接口实际返回的指标 */
const visibleKpis = computed(() => {
  const kpis = overview.value?.kpis;
  if (!kpis) return [];
  return KPI_ITEMS.filter((item) => item.key in kpis).map((item) => ({
    ...item,
    value: kpi(item.key),
    compare: item.compareKey ? (overview.value?.compare?.[item.compareKey] ?? null) : undefined,
    sparkline: item.spark ? overview.value?.series?.[item.spark] : undefined,
    subText: item.sub ? item.sub() : "",
  }));
});

const statusBreakdowns = computed(() => {
  const b = overview.value?.breakdowns;
  if (!b) return [];
  return [
    { label: t("projects.overview.keywordsByStatus"), kind: "keyword_status" as const, data: b.keywords_by_status },
    { label: t("projects.overview.titlesByStatus"), kind: "title_status" as const, data: b.titles_by_status },
    { label: t("projects.overview.contentsByStatus"), kind: "content_status" as const, data: b.contents_by_status },
    { label: t("projects.overview.linksByStatus"), kind: "link_alive_status" as const, data: b.links_by_status },
  ].filter((g) => g.data && Object.keys(g.data).length);
});

// =====================================================================
// 默认模板
// =====================================================================
const canViewTemplates = computed(() => has("content.prompt_templates.view"));
const templates = ref<PromptTemplate[]>([]);
const templatesLoading = ref(false);
const templatesLoaded = ref(false);
/** 已配置但不在 published 选项里的模板（因同 code 发布新版本而归档等） */
const extraTemplates = reactive<Record<number, PromptTemplate | null>>({});
const selection = reactive<Partial<Record<PromptKind, number | undefined>>>({});
const templateErrors = ref<Partial<Record<PromptKind, string>>>({});
const templatesSaving = ref(false);

function initTemplateSelection() {
  const current = project.value?.default_templates ?? {};
  for (const kind of PROMPT_KIND) selection[kind] = current[kind] ?? undefined;
  templateErrors.value = {};
}

async function loadTemplates() {
  if (!canViewTemplates.value || !project.value) return;
  templatesLoading.value = true;
  try {
    const all: PromptTemplate[] = [];
    for (let page = 1; page <= 20; page += 1) {
      // 同 code 只有一个 published 版本：all_versions + status=published 恰好得到全部已发布模板
      const res = await promptTemplatesApi.list({ status: "published", all_versions: true, page, page_size: 100 }, { silent: true });
      all.push(...res.items);
      if (res.items.length < 100 || all.length >= res.total) break;
    }
    const pid = project.value.id;
    templates.value = all.filter((tpl) => tpl.project_id === 0 || tpl.project_id === pid);
    templatesLoaded.value = true;
    await loadExtraTemplates();
  } catch {
    templates.value = [];
  } finally {
    templatesLoading.value = false;
  }
}

async function loadExtraTemplates() {
  const known = new Set(templates.value.map((x) => x.id));
  const ids = Object.values(project.value?.default_templates ?? {}).filter((id): id is number => typeof id === "number" && !known.has(id) && !(id in extraTemplates));
  await Promise.all(
    ids.map(async (id) => {
      try {
        extraTemplates[id] = await promptTemplatesApi.get(id, { silent: true });
      } catch {
        extraTemplates[id] = null;
      }
    }),
  );
}

function optionsOf(kind: PromptKind): PromptTemplate[] {
  return templates.value
    .filter((x) => x.kind === kind)
    .sort((a, b) => Number(b.project_id > 0) - Number(a.project_id > 0) || a.code.localeCompare(b.code));
}

function templateLabel(tpl: PromptTemplate): string {
  return `${tpl.name}（${tpl.code} v${tpl.version}）`;
}

/** 当前选中值不在选项中时的显示文案 */
function missingLabel(kind: PromptKind): string | null {
  const id = selection[kind];
  if (!id || optionsOf(kind).some((x) => x.id === id)) return null;
  const extra = extraTemplates[id];
  if (extra) return t("projects.templates.notPublishedOption", { label: templateLabel(extra), status: t(`status.prompt_status.${extra.status}`) });
  return t("projects.templates.unknownOption", { id });
}

const templatesDirty = computed(() => {
  const current = project.value?.default_templates ?? {};
  return PROMPT_KIND.some((k) => (selection[k] ?? undefined) !== (current[k] ?? undefined));
});

async function saveTemplates() {
  const p = project.value;
  if (!p) return;
  const defaults: Partial<Record<PromptKind, number>> = {};
  for (const kind of PROMPT_KIND) {
    const id = selection[kind];
    if (typeof id === "number" && id > 0) defaults[kind] = id;
  }
  templatesSaving.value = true;
  templateErrors.value = {};
  try {
    const saved = await projectsApi.update(p.id, bodyOf(p, defaults), { silent: true });
    project.value = { ...p, ...saved, routes: saved.routes ?? p.routes };
    initTemplateSelection();
    ElMessage.success(t("projects.templates.saved"));
  } catch (err) {
    if (!isApiError(err)) return;
    const items = validationErrors(err);
    const errs: Partial<Record<PromptKind, string>> = {};
    for (const item of items) {
      if (item.loc[1] === "default_templates" && typeof item.loc[2] === "string") errs[item.loc[2] as PromptKind] = item.msg;
    }
    templateErrors.value = errs;
    if (items.length) ElMessage.error(t("common.validationFailed"));
    else ElMessage.error(err.message);
  } finally {
    templatesSaving.value = false;
  }
}

function resetTemplates() {
  initTemplateSelection();
}

// =====================================================================
// 默认模型（项目级 capability_routes 覆盖行）
// =====================================================================
const TEXT_PROTOCOLS: Protocol[] = ["openai_chat", "openai_responses", "anthropic_messages"];
const PRIMARY_CAPABILITIES: Capability[] = ["keyword", "title", "content", "rewrite"];
const OTHER_CAPABILITIES: Capability[] = CAPABILITIES.filter((c) => !PRIMARY_CAPABILITIES.includes(c));

interface FallbackRow {
  key: number;
  model: string | null;
}

interface RouteRow {
  capability: Capability;
  enabled: boolean;
  primary: string | null;
  fallbacks: FallbackRow[];
  params: Record<string, unknown>;
  paramsValid: boolean;
  protocol: Protocol | undefined;
  showParams: boolean;
}

interface RowErrors {
  primary?: string;
  fallbacks: Record<number, string>;
  params?: string;
  paramsItems: ValidationErrorItem[];
  protocol?: string;
  general?: string;
}

let rowKey = 0;
const routeRows = reactive<Record<string, RouteRow>>({});
const routeErrors = reactive<Record<string, RowErrors>>({});
const routesSaving = ref(false);
const canPickModels = computed(() => has("ai.models.view"));
const canEditRoutes = computed(() => canUpdate.value && canPickModels.value);
const globalRoutes = ref<CapabilityRoute[]>([]);

function isText(c: Capability): boolean {
  return (TEXT_CAPABILITIES as readonly string[]).includes(c);
}

function emptyErrors(): RowErrors {
  return { fallbacks: {}, paramsItems: [] };
}

function snapshotOf(r: RouteRow): string {
  if (!r.enabled) return `${r.capability}:off`;
  return JSON.stringify([r.capability, r.primary, r.fallbacks.map((f) => f.model), r.params, r.protocol ?? null]);
}

const routesBaseline = ref<Record<string, string>>({});

function initRouteRows() {
  const existing = project.value?.routes ?? [];
  for (const capability of CAPABILITIES) {
    const r = existing.find((x) => x.capability === capability);
    routeRows[capability] = {
      capability,
      enabled: !!r,
      primary: r?.primary_model ?? null,
      fallbacks: (r?.fallback_models ?? []).map((m) => ({ key: ++rowKey, model: m })),
      params: r?.params && typeof r.params === "object" ? JSON.parse(JSON.stringify(r.params)) : {},
      paramsValid: true,
      protocol: r && isText(capability) ? r.protocol : undefined,
      showParams: !!r?.params && Object.keys(r.params).length > 0,
    };
    routeErrors[capability] = emptyErrors();
  }
  routesBaseline.value = Object.fromEntries(CAPABILITIES.map((c) => [c, snapshotOf(routeRows[c])]));
}

const routesDirty = computed(() => CAPABILITIES.some((c) => routeRows[c] && snapshotOf(routeRows[c]) !== routesBaseline.value[c]));

async function loadGlobalRoutes() {
  if (!has("ai.routes.view")) return;
  try {
    globalRoutes.value = (await aiApi.listRoutes()).filter((r) => r.project_id === 0);
  } catch {
    globalRoutes.value = [];
  }
}

function globalOf(c: Capability): CapabilityRoute | undefined {
  return globalRoutes.value.find((r) => r.capability === c);
}

function globalChain(c: Capability): string {
  const g = globalOf(c);
  if (!g) return "";
  return [g.primary_model, ...(g.fallback_models ?? [])].filter(Boolean).join(" → ");
}

function toggleOverride(row: RouteRow, value: boolean) {
  row.enabled = value;
  routeErrors[row.capability] = emptyErrors();
  if (value && !row.primary) {
    // 以全局行作为起点，便于在其上调整
    const g = globalOf(row.capability);
    if (g) {
      row.primary = g.primary_model || null;
      row.fallbacks = (g.fallback_models ?? []).map((m) => ({ key: ++rowKey, model: m }));
      // 新覆盖行不带 params 时后端取全局行的 params：在表单中显式带出，所见即所存
      if (!row.params || !Object.keys(row.params).length) {
        row.params = g.params && typeof g.params === "object" ? JSON.parse(JSON.stringify(g.params)) : {};
        row.showParams = Object.keys(row.params).length > 0;
      }
    }
  }
}

function addFallback(row: RouteRow) {
  row.fallbacks.push({ key: ++rowKey, model: null });
}

function removeFallback(row: RouteRow, index: number) {
  row.fallbacks.splice(index, 1);
  routeErrors[row.capability].fallbacks = {};
}

function moveFallback(row: RouteRow, from: number, to: number) {
  if (to < 0 || to >= row.fallbacks.length) return;
  const [item] = row.fallbacks.splice(from, 1);
  row.fallbacks.splice(to, 0, item);
  routeErrors[row.capability].fallbacks = {};
}

function excludeFor(row: RouteRow, index: number): string[] {
  return [row.primary ?? "", ...row.fallbacks.filter((_, i) => i !== index).map((f) => f.model ?? "")].filter(Boolean);
}

function validateRoutes(): boolean {
  let ok = true;
  for (const c of CAPABILITIES) {
    const row = routeRows[c];
    const errs = emptyErrors();
    routeErrors[c] = errs;
    if (!row.enabled) continue;
    if (!row.primary) {
      errs.primary = t("projects.models.primaryRequired");
      ok = false;
    }
    const seen = new Set<string>(row.primary ? [row.primary] : []);
    row.fallbacks.forEach((f, i) => {
      if (!f.model) {
        errs.fallbacks[i] = t("projects.models.fallbackEmpty");
        ok = false;
      } else if (seen.has(f.model)) {
        errs.fallbacks[i] = t("projects.models.duplicateModel");
        ok = false;
      } else seen.add(f.model);
    });
    if (!row.paramsValid) {
      errs.params = t("projects.models.paramsInvalid");
      row.showParams = true;
      ok = false;
    }
  }
  return ok;
}

async function saveRoutes() {
  const p = project.value;
  if (!p || !validateRoutes()) return;
  const payload: ProjectRouteInput[] = [];
  const indexToCapability: Capability[] = [];
  const existingCapabilities = new Set((p.routes ?? []).map((r) => r.capability));
  for (const c of CAPABILITIES) {
    const row = routeRows[c];
    if (!row.enabled) continue;
    const item: ProjectRouteInput = {
      capability: c,
      primary_model: row.primary as string,
      fallback_models: row.fallbacks.map((f) => f.model as string),
    };
    // 既有覆盖行省略 params 时后端保留原值：始终显式提交（清空即 {}）；新行为空时省略，由后端取全局行的 params
    const params = row.params && typeof row.params === "object" ? row.params : {};
    if (Object.keys(params).length || existingCapabilities.has(c)) item.params = params;
    if (row.protocol && isText(c)) item.protocol = row.protocol;
    payload.push(item);
    indexToCapability.push(c);
  }
  if (!payload.length && (p.routes?.length ?? 0) > 0) {
    try {
      await ElMessageBox.confirm(t("projects.models.clearConfirm"), t("common.tip"), { type: "warning" });
    } catch {
      return;
    }
  }
  routesSaving.value = true;
  try {
    const saved = await projectsApi.saveRoutes(p.id, payload, { silent: true });
    project.value = { ...p, routes: saved };
    initRouteRows();
    ElMessage.success(t("projects.models.saved"));
  } catch (err) {
    if (!isApiError(err)) return;
    const items = validationErrors(err);
    for (const item of items) {
      const idx = item.loc[2];
      const capability = typeof idx === "number" ? indexToCapability[idx] : undefined;
      if (!capability) continue;
      const errs = routeErrors[capability];
      const field = item.loc[3];
      if (field === "primary_model") errs.primary = item.msg;
      else if (field === "fallback_models" && typeof item.loc[4] === "number") errs.fallbacks[item.loc[4]] = item.msg;
      else if (field === "params") {
        errs.paramsItems = [...errs.paramsItems, item];
        routeRows[capability].showParams = true;
      } else if (field === "protocol") errs.protocol = item.msg;
      else errs.general = item.msg;
    }
    ElMessage.error(items.length ? t("common.validationFailed") : err.message);
  } finally {
    routesSaving.value = false;
  }
}

function resetRoutes() {
  initRouteRows();
}

// =====================================================================
// 生命周期
// =====================================================================
onMounted(async () => {
  if (isAllScope.value && !projectStore.ownersLoaded) projectStore.loadOwners().catch(() => undefined);
  await loadProject();
  if (!project.value) return;
  void loadOverview();
  void loadTemplates();
  void loadGlobalRoutes();
});

watch(projectId, async () => {
  overview.value = null;
  templatesLoaded.value = false;
  await loadProject();
  if (!project.value) return;
  void loadOverview();
  void loadTemplates();
});
</script>

<template>
  <!-- 不存在 / 不可见 -->
  <el-card v-if="notFound" shadow="never" class="page-card">
    <el-result icon="warning" :title="t('projects.notFound')" :sub-title="t('projects.notFoundHint')">
      <template #extra>
        <el-button type="primary" :icon="ArrowLeft" @click="router.push({ name: 'projects' })">{{ t("projects.backToList") }}</el-button>
      </template>
    </el-result>
  </el-card>

  <template v-else>
    <el-card v-loading="loading && !project" shadow="never" class="page-card">
      <template #header>
        <div class="page-header">
          <div class="title-line">
            <el-button :icon="ArrowLeft" circle size="small" @click="router.push({ name: 'projects' })" />
            <h2 class="page-title">{{ project?.name ?? t("menu.projectDetail") }}</h2>
            <StatusTag v-if="project" kind="project_status" :value="project.status" />
            <span v-if="project" class="mono text-secondary">{{ project.slug }}</span>
          </div>
          <div class="header-actions">
            <el-button :icon="Refresh" @click="loadProject">{{ t("common.refresh") }}</el-button>
            <el-button v-if="project && canUpdate" :icon="Edit" @click="gotoEdit">{{ t("projects.editBasic") }}</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="project?.status === 'archived'" type="info" :closable="false" show-icon :title="t('projects.archivedHint')" class="block-gap" />

      <el-tabs v-if="project" v-model="tab">
        <!-- ============ 概览 ============ -->
        <el-tab-pane :label="t('projects.tabs.overview')" name="overview">
          <el-descriptions :column="3" border size="small" class="block-gap">
            <el-descriptions-item :label="t('projects.owner')">{{ ownerLabel(project) }}</el-descriptions-item>
            <el-descriptions-item :label="t('projects.industry')">{{ project.industry || "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('projects.language')">{{ project.language }}</el-descriptions-item>
            <el-descriptions-item :label="t('projects.brandName')">{{ project.brand_name || "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('projects.defaultStyle')"><StatusTag kind="content_style" :value="project.default_style" effect="plain" /></el-descriptions-item>
            <el-descriptions-item :label="t('projects.defaultFormat')"><StatusTag kind="content_format" :value="project.default_format" effect="plain" /></el-descriptions-item>
            <el-descriptions-item :label="t('projects.audience')" :span="3">{{ project.audience || "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('projects.brandInfo')" :span="3"><span class="pre-wrap">{{ project.brand_info || "-" }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('projects.description')" :span="3"><span class="pre-wrap">{{ project.description || "-" }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('projects.platforms')">
              {{ project.default_platform_ids?.length ? project.default_platform_ids.map((id) => `#${id}`).join("、") : "-" }}
            </el-descriptions-item>
            <el-descriptions-item :label="t('common.createdAt')">{{ formatDateTime(project.created_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('common.updatedAt')">{{ formatDateTime(project.updated_at) }}</el-descriptions-item>
          </el-descriptions>

          <div class="section-head">
            <h3 class="section-title">{{ t("projects.overview.kpis") }}</h3>
            <div class="header-actions">
              <el-radio-group v-model="range" size="small">
                <el-radio-button v-for="r in STATS_RANGE" :key="r" :value="r">{{ t(`status.stats_range.${r}`) }}</el-radio-button>
              </el-radio-group>
              <el-button :icon="Refresh" size="small" :loading="overviewLoading" @click="loadOverview">{{ t("common.refresh") }}</el-button>
            </div>
          </div>
          <div v-loading="overviewLoading">
            <el-empty v-if="!overview" :image-size="60" :description="overviewFailed ? t('projects.overview.unavailable') : t('common.noData')" />
            <template v-else>
              <el-row :gutter="12">
                <el-col v-for="item in visibleKpis" :key="item.key" :xs="24" :sm="12" :md="8" :lg="6" class="kpi-col">
                  <KpiCard
                    :title="t(`projects.overview.kpi.${item.key}`)"
                    :value="item.value"
                    :format="item.format"
                    :sub="item.subText"
                    :compare="item.compare"
                    :sparkline="item.sparkline"
                  />
                </el-col>
              </el-row>
              <div v-if="overview.meta?.start_date" class="text-secondary small meta-line">
                {{ t("projects.overview.period", { start: overview.meta.start_date, end: overview.meta.end_date }) }}
                <template v-if="overview.meta.computed_at"> · {{ t("projects.overview.computedAt", { time: formatDateTime(overview.meta.computed_at) }) }}</template>
              </div>
              <div v-if="statusBreakdowns.length" class="breakdowns">
                <div v-for="group in statusBreakdowns" :key="group.kind" class="breakdown">
                  <div class="breakdown-title">{{ group.label }}</div>
                  <div class="breakdown-tags">
                    <span v-for="(count, status) in group.data" :key="String(status)" class="breakdown-item">
                      <StatusTag :kind="group.kind" :value="String(status)" />
                      <span class="breakdown-count">{{ formatNumber(count as number) }}</span>
                    </span>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </el-tab-pane>

        <!-- ============ 默认模板 ============ -->
        <el-tab-pane :label="t('projects.tabs.templates')" name="templates">
          <el-alert type="info" :closable="false" show-icon :title="t('projects.templates.hint')" class="block-gap" />
          <el-alert v-if="!canViewTemplates" type="warning" :closable="false" show-icon :title="t('projects.templates.noPermission')" class="block-gap" />
          <el-table v-loading="templatesLoading" :data="[...PROMPT_KIND]" size="small" border>
            <el-table-column :label="t('projects.templates.kind')" width="150">
              <template #default="{ row }">
                <StatusTag kind="prompt_kind" :value="row" effect="plain" />
                <div class="mono text-secondary small">{{ row }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('projects.templates.template')" min-width="320">
              <template #default="{ row }">
                <el-select
                  v-if="canViewTemplates"
                  v-model="selection[row as PromptKind]"
                  clearable
                  filterable
                  :disabled="!canUpdate"
                  :placeholder="t('projects.templates.useDefault', { code: PROMPT_DEFAULT_CODES[row as PromptKind] })"
                  style="width: 100%"
                >
                  <el-option v-if="missingLabel(row as PromptKind)" :value="selection[row as PromptKind] as number" :label="missingLabel(row as PromptKind) as string" />
                  <el-option v-for="tpl in optionsOf(row as PromptKind)" :key="tpl.id" :value="tpl.id" :label="templateLabel(tpl)">
                    <div class="tpl-option">
                      <span>{{ templateLabel(tpl) }}</span>
                      <el-tag size="small" :type="tpl.project_id > 0 ? 'primary' : 'info'" effect="plain">
                        {{ tpl.project_id > 0 ? t("projects.templates.projectScope") : t("projects.templates.globalScope") }}
                      </el-tag>
                    </div>
                  </el-option>
                </el-select>
                <span v-else class="mono">{{ selection[row as PromptKind] ? `#${selection[row as PromptKind]}` : t("projects.templates.useDefault", { code: PROMPT_DEFAULT_CODES[row as PromptKind] }) }}</span>
                <div v-if="templateErrors[row as PromptKind]" class="row-error">{{ templateErrors[row as PromptKind] }}</div>
                <div v-else-if="missingLabel(row as PromptKind) && extraTemplates[selection[row as PromptKind] as number]?.status === 'archived'" class="form-hint">
                  {{ t("projects.templates.archivedHint") }}
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('projects.templates.fallback')" width="200">
              <template #default="{ row }"><span class="mono text-secondary">{{ PROMPT_DEFAULT_CODES[row as PromptKind] }}</span></template>
            </el-table-column>
          </el-table>
          <div v-if="canUpdate && canViewTemplates" class="footer-actions">
            <el-button :disabled="!templatesDirty" @click="resetTemplates">{{ t("common.reset") }}</el-button>
            <el-button type="primary" :loading="templatesSaving" :disabled="!templatesDirty" @click="saveTemplates">{{ t("common.save") }}</el-button>
          </div>
        </el-tab-pane>

        <!-- ============ 默认模型 ============ -->
        <el-tab-pane :label="t('projects.tabs.models')" name="models">
          <el-alert type="info" :closable="false" show-icon :title="t('projects.models.hint')" class="block-gap" />
          <el-alert v-if="canUpdate && !canPickModels" type="warning" :closable="false" show-icon :title="t('projects.models.noModelPermission')" class="block-gap" />

          <template v-for="(group, gi) in [PRIMARY_CAPABILITIES, OTHER_CAPABILITIES]" :key="gi">
            <div v-if="gi === 1" class="section-head">
              <h3 class="section-title">{{ t("projects.models.otherCapabilities") }}</h3>
              <span class="text-secondary small">{{ t("projects.models.otherHint") }}</span>
            </div>
            <div v-for="c in group" :key="c" class="route-row" :class="{ 'is-on': routeRows[c]?.enabled }">
              <template v-if="routeRows[c]">
                <div class="route-head">
                  <StatusTag kind="capability" :value="c" effect="plain" />
                  <el-tag size="small" type="info" effect="plain">{{ t(`status.modality.${MODALITY_OF[c]}`) }}</el-tag>
                  <el-switch
                    :model-value="routeRows[c].enabled"
                    :disabled="!canEditRoutes"
                    :active-text="t('projects.models.override')"
                    :inactive-text="t('projects.models.followGlobal')"
                    inline-prompt
                    style="--el-switch-on-color: var(--el-color-primary)"
                    @update:model-value="(v: string | number | boolean) => toggleOverride(routeRows[c], !!v)"
                  />
                  <span v-if="globalChain(c)" class="text-secondary small global-chain">
                    {{ t("projects.models.globalChain") }}<span class="mono">{{ globalChain(c) }}</span>
                  </span>
                </div>

                <div v-if="routeRows[c].enabled" class="route-body">
                  <div v-if="routeErrors[c].general" class="row-error">{{ routeErrors[c].general }}</div>
                  <el-form label-width="96px" size="small" @submit.prevent>
                    <el-form-item :label="t('projects.models.primary')" required :error="routeErrors[c].primary">
                      <ModelSelect
                        v-if="canPickModels"
                        v-model="routeRows[c].primary"
                        :modality="MODALITY_OF[c]"
                        :exclude="routeRows[c].fallbacks.map((f) => f.model ?? '')"
                        :disabled="!canEditRoutes"
                        size="small"
                        width="420px"
                      />
                      <span v-else class="mono">{{ routeRows[c].primary || "-" }}</span>
                    </el-form-item>
                    <el-form-item :label="t('projects.models.fallbacks')">
                      <div class="fallbacks">
                        <div v-for="(f, idx) in routeRows[c].fallbacks" :key="f.key" class="fallback-row">
                          <span class="fallback-index">{{ idx + 1 }}</span>
                          <div class="fallback-select">
                            <ModelSelect
                              v-if="canPickModels"
                              v-model="f.model"
                              :modality="MODALITY_OF[c]"
                              :exclude="excludeFor(routeRows[c], idx)"
                              :disabled="!canEditRoutes"
                              size="small"
                              width="100%"
                            />
                            <span v-else class="mono">{{ f.model }}</span>
                            <div v-if="routeErrors[c].fallbacks[idx]" class="row-error">{{ routeErrors[c].fallbacks[idx] }}</div>
                          </div>
                          <template v-if="canEditRoutes">
                            <el-button :icon="ArrowUp" circle size="small" :disabled="idx === 0" @click="moveFallback(routeRows[c], idx, idx - 1)" />
                            <el-button :icon="ArrowDown" circle size="small" :disabled="idx === routeRows[c].fallbacks.length - 1" @click="moveFallback(routeRows[c], idx, idx + 1)" />
                            <el-button :icon="Delete" circle size="small" type="danger" plain @click="removeFallback(routeRows[c], idx)" />
                          </template>
                        </div>
                        <div v-if="canEditRoutes">
                          <el-button :icon="Plus" size="small" @click="addFallback(routeRows[c])">{{ t("projects.models.addFallback") }}</el-button>
                        </div>
                        <span v-else-if="!routeRows[c].fallbacks.length" class="text-secondary">{{ t("projects.models.noFallbacks") }}</span>
                      </div>
                    </el-form-item>
                    <el-form-item v-if="isText(c)" :label="t('projects.models.protocol')" :error="routeErrors[c].protocol">
                      <el-select v-model="routeRows[c].protocol" clearable :disabled="!canEditRoutes" :placeholder="t('projects.models.protocolGlobal')" style="width: 240px">
                        <el-option v-for="p in TEXT_PROTOCOLS" :key="p" :label="t(`status.protocol.${p}`)" :value="p" />
                      </el-select>
                    </el-form-item>
                    <el-form-item :label="t('projects.models.params')" :error="routeErrors[c].params">
                      <div class="params-box">
                        <el-button link type="primary" size="small" @click="routeRows[c].showParams = !routeRows[c].showParams">
                          {{ routeRows[c].showParams ? t("projects.models.hideParams") : t("projects.models.editParams") }}
                          <span v-if="Object.keys(routeRows[c].params || {}).length" class="mono text-secondary">&nbsp;{{ JSON.stringify(routeRows[c].params) }}</span>
                        </el-button>
                        <JsonEditor
                          v-if="routeRows[c].showParams"
                          v-model="routeRows[c].params"
                          object-only
                          nullable
                          :rows="4"
                          :readonly="!canEditRoutes"
                          :errors="routeErrors[c].paramsItems"
                          @validity="(v: boolean) => (routeRows[c].paramsValid = v)"
                        />
                        <div v-if="routeRows[c].showParams" class="form-hint">{{ t(`projects.models.paramsHint.${MODALITY_OF[c]}`) }}</div>
                      </div>
                    </el-form-item>
                  </el-form>
                </div>
              </template>
            </div>
          </template>

          <div v-if="canEditRoutes" class="footer-actions">
            <el-button :disabled="!routesDirty" @click="resetRoutes">{{ t("common.reset") }}</el-button>
            <el-button type="primary" :loading="routesSaving" :disabled="!routesDirty" @click="saveRoutes">{{ t("common.save") }}</el-button>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </template>
</template>

<style scoped>
.title-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.block-gap {
  margin-bottom: 12px;
}
.small {
  font-size: 12px;
}
.pre-wrap {
  white-space: pre-wrap;
}
.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  margin: 16px 0 10px;
}
.section-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.kpi-col {
  margin-bottom: 12px;
}
.meta-line {
  margin-top: 8px;
}
.breakdowns {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.breakdown {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
}
.breakdown-title {
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 600;
}
.breakdown-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 12px;
}
.breakdown-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.breakdown-count {
  font-variant-numeric: tabular-nums;
}
.tpl-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.row-error {
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.4;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.footer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
.route-row {
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  margin-bottom: 8px;
}
.route-row.is-on {
  border-color: var(--el-color-primary-light-5);
}
.route-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.global-chain {
  margin-left: 4px;
  word-break: break-all;
}
.route-body {
  margin-top: 10px;
}
.fallbacks {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
}
.fallback-row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
.fallback-row .el-button {
  margin-left: 0;
}
.fallback-index {
  width: 18px;
  margin-top: 4px;
  color: var(--el-text-color-secondary);
  text-align: right;
}
.fallback-select {
  flex: 1;
  min-width: 0;
  max-width: 460px;
}
.params-box {
  width: 100%;
}
</style>
