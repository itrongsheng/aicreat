<script setup lang="ts">
// 「AI 网关 → 能力路由」（docs/08 §6.7、§12.3、§12.5；docs/04 §6.15、§7.16；docs/13 §6.3）：
// - 顶部状态条：GET /admin/ai/health（zhiqi_mode、base_url、paused 红色告警条、worker 心跳、模型健康快照），可见时 30s 轮询；
// - 表格：全局路由 + 可见项目的覆盖行，主 / 备模型显示健康、是否在目录与熔断状态；
// - 行操作：一键测试（ai.routes.test，image / video 提供 probe_media 复选框）、重置熔断（ai.routes.reset_breaker）、
//   编辑（ai.routes.update）、删除（仅项目行，ai.routes.delete）；新建项目覆盖路由（ai.routes.create）；
// - 编辑弹窗：ModelSelect 按 MODALITY_OF[capability] 过滤、备选链拖拽 / 上下移动排序、params 用 JsonEditor 并按能力给出字段提示；
//   保存响应含 warnings[]（主 / 备模型 is_available=0）时弹出黄色警示。全局重试 / 熔断 / 回退 / 超时参数不在本页（系统配置 → AI 路由）。
import { computed, h, onMounted, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowDown, ArrowUp, Delete, Plus, Rank, Refresh, Setting } from "@element-plus/icons-vue";
import {
  CAPABILITIES,
  MODALITY_OF,
  TEXT_CAPABILITIES,
  type AiHealthSnapshot,
  type BreakerReason,
  type BreakerState,
  type Capability,
  type CapabilityRoute,
  type CapabilityRouteCreateBody,
  type HealthStatus,
  type Protocol,
  type RouteTestResultItem,
  type ValidationErrorItem,
} from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import { isApiError, validationErrors } from "@/api/client";
import JsonEditor from "@/components/JsonEditor.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import ProjectSelect from "@/components/ProjectSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useProjectStore } from "@/store/project";
import { formatDateTime, formatDuration } from "@/utils/format";

const { t } = useI18n();
const { has } = usePermission();
const router = useRouter();
const projectStore = useProjectStore();

const TEXT_PROTOCOLS: Protocol[] = ["openai_chat", "openai_responses", "anthropic_messages"];

/** docs/08 §6.2 seed 表：各能力的 params 初值 */
const PARAMS_SEED: Record<Capability, Record<string, unknown>> = {
  keyword: { temperature: 0.7, max_tokens: 2048 },
  title: { temperature: 0.8, max_tokens: 1024 },
  content: { temperature: 0.7, max_tokens: 4096 },
  rewrite: { temperature: 0.7, max_tokens: 4096 },
  image: { resolution: "1080p", aspect_ratio: "16:9" },
  video: { resolution: "720p", duration: 5, aspect_ratio: "16:9", generate_audio: false },
  geo_check: { temperature: 0.2, max_tokens: 1024 },
  seo_check: { temperature: 0.2, max_tokens: 1024 },
};

function isText(capability: Capability): boolean {
  return (TEXT_CAPABILITIES as readonly string[]).includes(capability);
}

function protocolsOf(capability: Capability): Protocol[] {
  if (capability === "image") return ["image_async"];
  if (capability === "video") return ["video"];
  return TEXT_PROTOCOLS;
}

function capabilityLabel(capability: Capability): string {
  return t(`status.capability.${capability}`);
}

function projectLabel(projectId: number): string {
  if (!projectId) return t("aiRoutes.global");
  const p = projectStore.projects.find((x) => x.id === projectId);
  return p ? p.name : `#${projectId}`;
}

// ---------- 健康快照（30s 轮询） ----------
const healthSnap = ref<AiHealthSnapshot | null>(null);
const healthUpdatedAt = ref<string | null>(null);
const healthLoading = ref(false);

async function loadHealth() {
  healthLoading.value = true;
  try {
    healthSnap.value = await aiApi.health({ silent: true });
    healthUpdatedAt.value = new Date().toISOString();
  } finally {
    healthLoading.value = false;
  }
}

const healthPolling = usePolling(loadHealth, { interval: 30_000 });

const pausedReasons = computed(() => {
  const p = healthSnap.value?.paused;
  if (!p) return [];
  const out: { reason: "quota_exceeded" | "auth_failed"; text: string }[] = [];
  if (p.quota_exceeded) out.push({ reason: "quota_exceeded", text: t("aiRoutes.pausedQuota") });
  if (p.auth_failed) out.push({ reason: "auth_failed", text: t("aiRoutes.pausedAuth") });
  return out;
});

// ---------- 路由列表 ----------
const projectFilter = ref(0);
const routes = ref<CapabilityRoute[]>([]);
const loading = ref(false);

const sortedRoutes = computed(() =>
  [...routes.value].sort((a, b) => {
    const ca = CAPABILITIES.indexOf(a.capability);
    const cb = CAPABILITIES.indexOf(b.capability);
    return ca !== cb ? ca - cb : a.project_id - b.project_id;
  }),
);

async function loadRoutes() {
  loading.value = true;
  try {
    routes.value = await aiApi.listRoutes({ project_id: projectFilter.value || undefined });
  } catch {
    routes.value = [];
  } finally {
    loading.value = false;
  }
}

watch(projectFilter, () => void loadRoutes());

function refreshAll() {
  void loadRoutes();
  void loadHealth().catch(() => undefined);
}

onMounted(() => {
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void loadRoutes();
  healthPolling.start();
});

interface ModelChip {
  model: string;
  index: number;
  health: HealthStatus;
  isAvailable: boolean;
  breakerState: BreakerState;
  breakerReason: BreakerReason | null;
}

/** 主 / 备模型状态：优先取路由自带的 models[]，其次健康快照，最后以路由级 breaker_state（主模型）兜底 */
function chipOf(route: CapabilityRoute, model: string, index: number): ModelChip {
  const fromRoute = route.models?.find((m) => m.model === model && m.candidate_index === index) ?? route.models?.find((m) => m.model === model);
  if (fromRoute) {
    return {
      model,
      index,
      health: fromRoute.health,
      isAvailable: fromRoute.is_available,
      breakerState: fromRoute.breaker_state,
      breakerReason: fromRoute.breaker_reason,
    };
  }
  const snap = healthSnap.value?.models.find((m) => m.capability === route.capability && m.model === model);
  return {
    model,
    index,
    health: snap?.status ?? "unknown",
    isAvailable: snap?.is_available ?? true,
    breakerState: snap?.breaker_state ?? (index === 0 ? (route.breaker_state ?? "closed") : "closed"),
    breakerReason: snap?.breaker_reason ?? (index === 0 ? (route.breaker_reason ?? null) : null),
  };
}

function chipsOf(route: CapabilityRoute): { primary: ModelChip; fallbacks: ModelChip[] } {
  return {
    primary: chipOf(route, route.primary_model, 0),
    fallbacks: (route.fallback_models ?? []).map((m, i) => chipOf(route, m, i + 1)),
  };
}

function paramsText(params: Record<string, unknown> | null | undefined): string {
  if (!params || !Object.keys(params).length) return "{}";
  try {
    return JSON.stringify(params);
  } catch {
    return "-";
  }
}

// ---------- 编辑 / 新建 ----------
interface FallbackRow {
  key: number;
  model: string | null;
}

let rowKey = 0;
const editVisible = ref(false);
const editSaving = ref(false);
const editing = ref<CapabilityRoute | null>(null);
const form = reactive({
  capability: "keyword" as Capability,
  project_id: 0,
  protocol: "openai_chat" as Protocol,
  primary_model: null as string | null,
  fallbacks: [] as FallbackRow[],
  params: {} as Record<string, unknown>,
  timeout_seconds: undefined as number | undefined,
  max_attempts: 3,
  is_enabled: true,
  note: "",
});
const paramsValid = ref(true);
const paramsEditor = ref<InstanceType<typeof JsonEditor>>();
const serverErrors = ref<ValidationErrorItem[]>([]);
const localErrors = reactive<{ project: string; capability: string; primary: string; fallbacks: Record<number, string> }>({
  project: "",
  capability: "",
  primary: "",
  fallbacks: {},
});

const editModality = computed(() => MODALITY_OF[form.capability]);
const protocolOptions = computed(() => protocolsOf(form.capability));
const paramFieldsHint = computed(() => t(`aiRoutes.paramFields.${editModality.value}`));

function globalRouteOf(capability: Capability): CapabilityRoute | undefined {
  return routes.value.find((r) => r.capability === capability && r.project_id === 0);
}

function clearErrors() {
  serverErrors.value = [];
  localErrors.project = "";
  localErrors.capability = "";
  localErrors.primary = "";
  localErrors.fallbacks = {};
}

function fillForm(route: CapabilityRoute | null, capability: Capability, projectId: number) {
  const base = route ?? globalRouteOf(capability);
  form.capability = capability;
  form.project_id = projectId;
  const protocols = protocolsOf(capability);
  form.protocol = base && protocols.includes(base.protocol) ? base.protocol : protocols[0];
  form.primary_model = route ? route.primary_model : null;
  form.fallbacks = (route?.fallback_models ?? []).map((m) => ({ key: ++rowKey, model: m }));
  form.params = base?.params && typeof base.params === "object" ? JSON.parse(JSON.stringify(base.params)) : { ...PARAMS_SEED[capability] };
  form.timeout_seconds = base?.timeout_seconds ?? undefined;
  form.max_attempts = base?.max_attempts ?? 3;
  form.is_enabled = base?.is_enabled ?? true;
  form.note = route?.note ?? "";
  paramsValid.value = true;
  clearErrors();
}

function openCreate() {
  editing.value = null;
  fillForm(null, "keyword", projectFilter.value > 0 ? projectFilter.value : projectStore.currentId);
  editVisible.value = true;
}

function openEdit(route: CapabilityRoute) {
  editing.value = route;
  fillForm(route, route.capability, route.project_id);
  editVisible.value = true;
}

function onCapabilityChange(capability: Capability) {
  // 新建时切换能力：协议、params、超时、尝试次数、启用状态从同能力全局行复制（与 PUT /projects/{id}/routes 插入规则一致）
  const keepPrimary = form.primary_model;
  const keepFallbacks = form.fallbacks;
  const sameModality = MODALITY_OF[capability] === editModality.value;
  fillForm(null, capability, form.project_id);
  if (sameModality) {
    form.primary_model = keepPrimary;
    form.fallbacks = keepFallbacks;
  }
}

function seedParams() {
  form.params = { ...PARAMS_SEED[form.capability] };
}

function addFallback() {
  form.fallbacks.push({ key: ++rowKey, model: null });
}

function removeFallback(index: number) {
  form.fallbacks.splice(index, 1);
  localErrors.fallbacks = {};
}

function moveFallback(from: number, to: number) {
  if (to < 0 || to >= form.fallbacks.length || from === to) return;
  const [item] = form.fallbacks.splice(from, 1);
  form.fallbacks.splice(to, 0, item);
  localErrors.fallbacks = {};
  serverErrors.value = serverErrors.value.filter((e) => e.loc[1] !== "fallback_models");
}

// 拖拽排序（HTML5 drag & drop）
const dragIndex = ref<number | null>(null);
const dragOverIndex = ref<number | null>(null);

function onDragStart(index: number, event: DragEvent) {
  dragIndex.value = index;
  event.dataTransfer?.setData("text/plain", String(index));
  if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
}

function onDragOver(index: number, event: DragEvent) {
  if (dragIndex.value === null) return;
  event.preventDefault();
  dragOverIndex.value = index;
}

function onDrop(index: number) {
  if (dragIndex.value !== null) moveFallback(dragIndex.value, index);
  dragIndex.value = null;
  dragOverIndex.value = null;
}

function onDragEnd() {
  dragIndex.value = null;
  dragOverIndex.value = null;
}

/** 后端 400 校验错误按字段归位 */
const fieldError = computed(() => {
  const out: Record<string, string> = {};
  const fallbacks: Record<number, string> = {};
  for (const item of serverErrors.value) {
    const field = String(item.loc[1] ?? item.loc[0] ?? "");
    if (field === "fallback_models" && typeof item.loc[2] === "number") {
      if (!fallbacks[item.loc[2]]) fallbacks[item.loc[2]] = item.msg;
      continue;
    }
    if (field && !out[field]) out[field] = item.msg;
  }
  return { fields: out, fallbacks };
});

const paramsErrors = computed(() => serverErrors.value.filter((e) => e.loc[1] === "params"));

function validateLocal(): boolean {
  clearErrors();
  let ok = true;
  if (!editing.value) {
    if (!form.project_id || form.project_id <= 0) {
      localErrors.project = t("aiRoutes.form.projectRequired");
      ok = false;
    }
    if (!form.capability) {
      localErrors.capability = t("aiRoutes.form.capabilityRequired");
      ok = false;
    }
  }
  if (!form.primary_model) {
    localErrors.primary = t("aiRoutes.form.primaryRequired");
    ok = false;
  }
  const seen = new Set<string>(form.primary_model ? [form.primary_model] : []);
  form.fallbacks.forEach((row, idx) => {
    if (!row.model) {
      localErrors.fallbacks[idx] = t("aiRoutes.form.fallbackEmpty");
      ok = false;
    } else if (seen.has(row.model)) {
      localErrors.fallbacks[idx] = t("aiRoutes.form.duplicateModel");
      ok = false;
    } else {
      seen.add(row.model);
    }
  });
  if (!paramsEditor.value?.validate() || !paramsValid.value) ok = false;
  return ok;
}

function showWarnings(warnings: ValidationErrorItem[] | undefined) {
  if (!warnings?.length) return;
  void ElMessageBox.alert(
    h("div", [
      h("p", { style: "margin:0 0 6px" }, t("aiRoutes.warningsTitle")),
      h(
        "ul",
        { style: "margin:0;padding-left:18px" },
        warnings.map((w) => h("li", [h("code", String(w.input ?? "")), ` ${w.msg}`])),
      ),
    ]),
    t("common.warning"),
    { type: "warning", confirmButtonText: t("common.confirm") },
  ).catch(() => undefined);
}

async function submitEdit() {
  if (!validateLocal()) return;
  editSaving.value = true;
  const payload = {
    protocol: form.protocol,
    primary_model: form.primary_model as string,
    fallback_models: form.fallbacks.map((r) => r.model as string),
    params: form.params ?? {},
    timeout_seconds: typeof form.timeout_seconds === "number" && form.timeout_seconds > 0 ? form.timeout_seconds : null,
    max_attempts: form.max_attempts,
    is_enabled: form.is_enabled,
    note: form.note.trim() || null,
  };
  try {
    let saved: CapabilityRoute;
    if (editing.value) {
      saved = await aiApi.updateRoute(editing.value.id, payload, { silent: true });
    } else {
      const body: CapabilityRouteCreateBody = { capability: form.capability, project_id: form.project_id, ...payload };
      saved = await aiApi.createRoute(body, { silent: true });
    }
    editVisible.value = false;
    ElMessage.success(t("aiRoutes.saved"));
    showWarnings(saved.warnings);
    await loadRoutes();
  } catch (err) {
    if (isApiError(err)) {
      const items = validationErrors(err);
      serverErrors.value = items;
      if (err.code === 409) ElMessage.error(editing.value ? err.message : t("aiRoutes.form.exists"));
      else if (items.length) ElMessage.error(`${err.message}：${items.map((e) => e.msg).slice(0, 3).join("；")}`);
      else ElMessage.error(err.message);
    }
  } finally {
    editSaving.value = false;
  }
}

/** 主模型 + 其它备选（排除自身），用于避免重复选择 */
function excludeFor(index: number): string[] {
  const others = form.fallbacks.filter((_, i) => i !== index).map((r) => r.model ?? "");
  return [form.primary_model ?? "", ...others].filter(Boolean);
}

// ---------- 删除 ----------
async function removeRoute(route: CapabilityRoute) {
  try {
    await ElMessageBox.confirm(
      t("aiRoutes.deleteConfirm", { project: projectLabel(route.project_id), capability: capabilityLabel(route.capability) }),
      t("common.tip"),
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    await aiApi.deleteRoute(route.id);
    ElMessage.success(t("aiRoutes.deleted"));
    await loadRoutes();
  } catch {
    /* 拦截器已提示 */
  }
}

// ---------- 重置熔断 ----------
const resetting = ref<number | null>(null);

async function resetRoute(route: CapabilityRoute) {
  try {
    await ElMessageBox.confirm(t("aiRoutes.resetConfirm", { capability: capabilityLabel(route.capability) }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  resetting.value = route.id;
  try {
    const res = await aiApi.resetBreaker(route.id);
    const parts = [t("aiRoutes.resetDone", { count: res.reset_models.length })];
    if (res.paused_cleared) parts.push(t("aiRoutes.resetPausedCleared"));
    ElMessage.success(parts.join("；"));
    refreshAll();
  } catch {
    /* 拦截器已提示 */
  } finally {
    resetting.value = null;
  }
}

// ---------- 一键测试 ----------
const testVisible = ref(false);
const testRouteRow = ref<CapabilityRoute | null>(null);
const testProbeMedia = ref(false);
const testRunning = ref(false);
const testResults = ref<RouteTestResultItem[] | null>(null);
const testIsMedia = computed(() => !!testRouteRow.value && !isText(testRouteRow.value.capability));

function openTest(route: CapabilityRoute) {
  testRouteRow.value = route;
  testProbeMedia.value = false;
  testResults.value = null;
  testVisible.value = true;
}

async function runTest() {
  const route = testRouteRow.value;
  if (!route) return;
  testRunning.value = true;
  try {
    testResults.value = await aiApi.testRoute(route.id, testIsMedia.value && testProbeMedia.value);
    refreshAll();
  } catch {
    /* 拦截器已提示 */
  } finally {
    testRunning.value = false;
  }
}

function candidateTag(index: number): string {
  return index === 0 ? t("aiRoutes.primaryTag") : t("aiRoutes.fallbackTag", { index });
}

function testCandidateIndex(model: string): number {
  const route = testRouteRow.value;
  if (!route) return 0;
  if (route.primary_model === model) return 0;
  const idx = route.fallback_models.indexOf(model);
  return idx >= 0 ? idx + 1 : 0;
}

const canGotoSettings = computed(() => has("system.settings.view"));
function gotoSettings() {
  void router.push({ path: "/settings", query: { tab: "ai_routing_config" } });
}
</script>

<template>
  <!-- 顶部状态条：GET /admin/ai/health -->
  <el-card v-loading="healthLoading && !healthSnap" shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("aiRoutes.gateway") }}</h2>
        <div class="header-actions">
          <span v-if="healthUpdatedAt" class="text-secondary small">
            {{ t("aiRoutes.updatedAt", { time: formatDateTime(healthUpdatedAt) }) }} · {{ t("aiRoutes.autoRefresh") }}
          </span>
          <el-button :icon="Refresh" :loading="healthLoading" @click="loadHealth().catch(() => undefined)">{{ t("common.refresh") }}</el-button>
        </div>
      </div>
    </template>

    <el-alert v-for="p in pausedReasons" :key="p.reason" type="error" :closable="false" show-icon effect="dark" :title="p.text" class="paused-alert" />

    <div v-if="healthSnap" class="gateway">
      <el-descriptions :column="3" border size="small" class="gateway-desc">
        <el-descriptions-item :label="t('aiRoutes.mode')"><StatusTag kind="zhiqi_mode" :value="healthSnap.zhiqi_mode" /></el-descriptions-item>
        <el-descriptions-item :label="t('aiRoutes.baseUrl')"><span class="mono">{{ healthSnap.base_url }}</span></el-descriptions-item>
        <el-descriptions-item :label="t('aiRoutes.paused')">
          <span class="tag-list">
            <el-tag v-if="healthSnap.paused.quota_exceeded" type="danger" size="small" effect="dark">{{ t("status.paused_reason.quota_exceeded") }}</el-tag>
            <el-tag v-if="healthSnap.paused.auth_failed" type="danger" size="small" effect="dark">{{ t("status.paused_reason.auth_failed") }}</el-tag>
            <el-tag v-if="!healthSnap.paused.quota_exceeded && !healthSnap.paused.auth_failed" type="success" size="small">{{ t("aiRoutes.notPaused") }}</el-tag>
          </span>
        </el-descriptions-item>
        <el-descriptions-item :label="t('aiRoutes.workers')" :span="3">
          <div v-if="healthSnap.workers.length" class="workers">
            <el-tooltip v-for="w in healthSnap.workers" :key="`${w.name}-${w.hostname}-${w.pid}`" :content="formatDateTime(w.heartbeat_at)" placement="top">
              <el-tag :type="w.alive ? 'success' : 'danger'" size="small" effect="plain">
                {{ w.name }} · {{ w.hostname }}:{{ w.pid }} · {{ w.alive ? t("aiRoutes.workerAlive") : t("aiRoutes.workerDead") }}
              </el-tag>
            </el-tooltip>
          </div>
          <span v-else class="text-secondary">{{ t("aiRoutes.noWorkers") }}</span>
        </el-descriptions-item>
      </el-descriptions>

      <el-collapse class="health-collapse">
        <el-collapse-item name="models" :title="t('aiRoutes.modelHealth', { count: healthSnap.models.length })">
          <el-table :data="healthSnap.models" size="small" border max-height="360">
            <el-table-column :label="t('aiRoutes.capability')" width="120">
              <template #default="{ row }"><StatusTag kind="capability" :value="row.capability" effect="plain" /></template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.model')" min-width="180">
              <template #default="{ row }"><span class="mono">{{ row.model }}</span></template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.status')" width="90">
              <template #default="{ row }"><StatusTag kind="health_status" :value="row.status" /></template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.latency')" width="90" align="right">
              <template #default="{ row }">{{ formatDuration(row.latency_ms) }}</template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.checkedAt')" width="165">
              <template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.breaker')" width="90">
              <template #default="{ row }"><StatusTag kind="breaker_state" :value="row.breaker_state" /></template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.breakerReason')" width="110">
              <template #default="{ row }"><StatusTag kind="breaker_reason" :value="row.breaker_reason" effect="plain" /></template>
            </el-table-column>
            <el-table-column :label="t('aiRoutes.availableCol')" width="80">
              <template #default="{ row }">
                <el-tag :type="row.is_available ? 'success' : 'danger'" size="small" effect="plain">{{ row.is_available ? t("common.yes") : t("common.no") }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-card>

  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("aiRoutes.title") }}</h2>
        <div class="header-actions">
          <el-button v-if="canGotoSettings" :icon="Setting" @click="gotoSettings">{{ t("aiRoutes.gotoSettings") }}</el-button>
          <el-button v-permission="'ai.routes.create'" type="primary" :icon="Plus" @click="openCreate">{{ t("aiRoutes.create") }}</el-button>
        </div>
      </div>
    </template>

    <el-alert type="info" :closable="false" show-icon :title="t('aiRoutes.hint')" class="hint-alert" />

    <div class="toolbar">
      <ProjectSelect v-model="projectFilter" allow-all :placeholder="t('aiRoutes.projectFilter')" width="220px" />
      <el-button :icon="Refresh" @click="refreshAll">{{ t("common.refresh") }}</el-button>
    </div>

    <el-table v-loading="loading" :data="sortedRoutes" row-key="id" stripe>
      <el-table-column :label="t('aiRoutes.capability')" width="110" fixed="left">
        <template #default="{ row }"><StatusTag kind="capability" :value="row.capability" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.project')" min-width="120">
        <template #default="{ row }">
          <el-tag v-if="row.project_id === 0" size="small" type="info">{{ t("aiRoutes.global") }}</el-tag>
          <span v-else>{{ projectLabel(row.project_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.protocol')" width="150">
        <template #default="{ row }"><StatusTag kind="protocol" :value="row.protocol" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.primary')" min-width="230">
        <template #default="{ row }">
          <div class="chip">
            <span class="mono chip-model">{{ row.primary_model || "-" }}</span>
            <span class="chip-tags">
              <StatusTag kind="health_status" :value="chipsOf(row).primary.health" />
              <el-tooltip :disabled="!chipsOf(row).primary.breakerReason" :content="t(`status.breaker_reason.${chipsOf(row).primary.breakerReason}`)" placement="top">
                <StatusTag kind="breaker_state" :value="chipsOf(row).primary.breakerState" effect="plain" />
              </el-tooltip>
              <el-tag v-if="!chipsOf(row).primary.isAvailable" type="danger" size="small" effect="plain">{{ t("aiRoutes.notInCatalog") }}</el-tag>
            </span>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.fallbacks')" min-width="260">
        <template #default="{ row }">
          <div v-if="row.fallback_models?.length" class="chips">
            <div v-for="chip in chipsOf(row).fallbacks" :key="`${chip.index}-${chip.model}`" class="chip">
              <span class="chip-index">{{ candidateTag(chip.index) }}</span>
              <span class="mono chip-model">{{ chip.model }}</span>
              <span class="chip-tags">
                <StatusTag kind="health_status" :value="chip.health" />
                <el-tooltip :disabled="!chip.breakerReason" :content="t(`status.breaker_reason.${chip.breakerReason}`)" placement="top">
                  <StatusTag kind="breaker_state" :value="chip.breakerState" effect="plain" />
                </el-tooltip>
                <el-tag v-if="!chip.isAvailable" type="danger" size="small" effect="plain">{{ t("aiRoutes.notInCatalog") }}</el-tag>
              </span>
            </div>
          </div>
          <span v-else class="text-secondary">{{ t("aiRoutes.noFallbacks") }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.params')" min-width="180">
        <template #default="{ row }">
          <el-tooltip :content="paramsText(row.params)" placement="top">
            <span class="mono params-cell">{{ paramsText(row.params) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.timeout')" width="100" align="right">
        <template #default="{ row }">{{ row.timeout_seconds ?? "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.maxAttempts')" width="90" align="right">
        <template #default="{ row }">{{ row.max_attempts }}</template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.enabled')" width="80">
        <template #default="{ row }"><StatusTag kind="active" :value="String(row.is_enabled)" /></template>
      </el-table-column>
      <el-table-column :label="t('aiRoutes.updatedBy')" width="90">
        <template #default="{ row }">{{ row.updated_by ? `#${row.updated_by}` : "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="290" fixed="right">
        <template #default="{ row }">
          <el-button v-permission="'ai.routes.test'" link type="primary" @click="openTest(row)">{{ t("aiRoutes.test") }}</el-button>
          <el-button v-permission="'ai.routes.reset_breaker'" link type="warning" :loading="resetting === row.id" @click="resetRoute(row)">
            {{ t("aiRoutes.resetBreaker") }}
          </el-button>
          <el-button v-permission="'ai.routes.update'" link type="primary" @click="openEdit(row)">{{ t("common.edit") }}</el-button>
          <span v-permission="'ai.routes.delete'">
            <el-tooltip :content="t('aiRoutes.deleteGlobalTip')" :disabled="row.project_id > 0" placement="top">
              <el-button link type="danger" :disabled="row.project_id === 0" @click="removeRoute(row)">{{ t("common.delete") }}</el-button>
            </el-tooltip>
          </span>
        </template>
      </el-table-column>
    </el-table>

    <!-- 编辑 / 新建 -->
    <el-dialog
      v-model="editVisible"
      :title="editing ? t('aiRoutes.editTitle', { capability: capabilityLabel(editing.capability), project: projectLabel(editing.project_id) }) : t('aiRoutes.createTitle')"
      width="min(760px, 96vw)"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-form label-width="150px" @submit.prevent="submitEdit">
        <el-form-item :label="t('aiRoutes.form.capability')" required :error="localErrors.capability || fieldError.fields.capability">
          <el-select v-if="!editing" v-model="form.capability" style="width: 220px" @change="onCapabilityChange">
            <el-option v-for="c in CAPABILITIES" :key="c" :label="capabilityLabel(c)" :value="c" />
          </el-select>
          <StatusTag v-else kind="capability" :value="form.capability" effect="plain" />
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.project')" required :error="localErrors.project || fieldError.fields.project_id">
          <ProjectSelect v-if="!editing" v-model="form.project_id" :allow-all="false" width="260px" />
          <span v-else>{{ projectLabel(form.project_id) }}</span>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.protocol')" :error="fieldError.fields.protocol">
          <el-select v-if="protocolOptions.length > 1" v-model="form.protocol" style="width: 260px">
            <el-option v-for="p in protocolOptions" :key="p" :label="t(`status.protocol.${p}`)" :value="p" />
          </el-select>
          <StatusTag v-else kind="protocol" :value="form.protocol" effect="plain" />
          <div class="form-hint">
            {{ protocolOptions.length > 1 ? t("aiRoutes.form.protocolHint") : t("aiRoutes.form.protocolFixed", { protocol: form.protocol }) }}
          </div>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.primary')" required :error="localErrors.primary || fieldError.fields.primary_model">
          <ModelSelect
            v-model="form.primary_model"
            :modality="editModality"
            include-unavailable
            :exclude="form.fallbacks.map((r) => r.model ?? '')"
            width="420px"
          />
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.fallbacks')" :error="fieldError.fields.fallback_models">
          <div class="fallbacks">
            <div
              v-for="(row, idx) in form.fallbacks"
              :key="row.key"
              class="fallback-row"
              :class="{ 'is-drag-over': dragOverIndex === idx && dragIndex !== idx, 'is-dragging': dragIndex === idx }"
              draggable="true"
              @dragstart="onDragStart(idx, $event)"
              @dragover="onDragOver(idx, $event)"
              @drop.prevent="onDrop(idx)"
              @dragend="onDragEnd"
            >
              <el-icon class="drag-handle"><Rank /></el-icon>
              <span class="fallback-index">{{ idx + 1 }}</span>
              <div class="fallback-select">
                <ModelSelect v-model="row.model" :modality="editModality" include-unavailable :exclude="excludeFor(idx)" width="100%" />
                <div v-if="localErrors.fallbacks[idx] || fieldError.fallbacks[idx]" class="row-error">
                  {{ localErrors.fallbacks[idx] || fieldError.fallbacks[idx] }}
                </div>
              </div>
              <el-tooltip :content="t('aiRoutes.form.moveUp')" placement="top">
                <el-button :icon="ArrowUp" circle size="small" :disabled="idx === 0" @click="moveFallback(idx, idx - 1)" />
              </el-tooltip>
              <el-tooltip :content="t('aiRoutes.form.moveDown')" placement="top">
                <el-button :icon="ArrowDown" circle size="small" :disabled="idx === form.fallbacks.length - 1" @click="moveFallback(idx, idx + 1)" />
              </el-tooltip>
              <el-tooltip :content="t('aiRoutes.form.remove')" placement="top">
                <el-button :icon="Delete" circle size="small" type="danger" plain @click="removeFallback(idx)" />
              </el-tooltip>
            </div>
            <el-button :icon="Plus" size="small" @click="addFallback">{{ t("aiRoutes.form.addFallback") }}</el-button>
            <div class="form-hint">{{ t("aiRoutes.form.dragTip") }}</div>
          </div>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.params')" :error="paramsErrors.length ? undefined : fieldError.fields.params">
          <div class="params-box">
            <div class="params-hint">
              <span class="form-hint">{{ t("aiRoutes.form.paramsHint", { fields: paramFieldsHint }) }}</span>
              <el-button link type="primary" size="small" @click="seedParams">{{ t("aiRoutes.form.paramsSeed") }}</el-button>
            </div>
            <JsonEditor ref="paramsEditor" v-model="form.params" object-only :rows="6" :errors="paramsErrors" @validity="(v: boolean) => (paramsValid = v)" />
          </div>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.timeout')" :error="fieldError.fields.timeout_seconds">
          <el-input-number v-model="form.timeout_seconds" :min="1" :max="3600" :step="10" controls-position="right" style="width: 180px" />
          <div class="form-hint">{{ t("aiRoutes.form.timeoutHint") }}</div>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.maxAttempts')" :error="fieldError.fields.max_attempts">
          <el-input-number v-model="form.max_attempts" :min="1" :max="10" controls-position="right" style="width: 180px" />
          <div class="form-hint">{{ t("aiRoutes.form.maxAttemptsHint") }}</div>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.enabled')" :error="fieldError.fields.is_enabled">
          <el-switch v-model="form.is_enabled" />
          <span v-if="!form.is_enabled" class="form-hint inline warn">{{ t("aiRoutes.form.disabledHint") }}</span>
        </el-form-item>
        <el-form-item :label="t('aiRoutes.form.note')" :error="fieldError.fields.note">
          <el-input v-model="form.note" maxlength="255" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="editSaving" :disabled="!paramsValid" @click="submitEdit">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>

    <!-- 一键测试 -->
    <el-dialog
      v-model="testVisible"
      :title="testRouteRow ? t('aiRoutes.testTitle', { capability: capabilityLabel(testRouteRow.capability), project: projectLabel(testRouteRow.project_id) }) : ''"
      width="min(760px, 96vw)"
      destroy-on-close
    >
      <div class="test-head">
        <template v-if="testIsMedia">
          <el-checkbox v-model="testProbeMedia">{{ t("aiRoutes.probeMedia") }}</el-checkbox>
          <span class="form-hint inline">{{ t("aiRoutes.probeMediaTip") }}</span>
        </template>
        <span class="spacer" />
        <el-button type="primary" :loading="testRunning" @click="runTest">{{ t("aiRoutes.runTest") }}</el-button>
      </div>
      <el-table v-if="testResults" :data="testResults" size="small" border>
        <el-table-column width="70">
          <template #default="{ row }"><el-tag size="small" effect="plain">{{ candidateTag(testCandidateIndex(row.model)) }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="t('aiRoutes.model')" min-width="180">
          <template #default="{ row }"><span class="mono">{{ row.model }}</span></template>
        </el-table-column>
        <el-table-column :label="t('aiRoutes.status')" width="90">
          <template #default="{ row }"><StatusTag kind="health_status" :value="row.status" /></template>
        </el-table-column>
        <el-table-column :label="t('aiRoutes.latency')" width="90" align="right">
          <template #default="{ row }">{{ formatDuration(row.latency_ms) }}</template>
        </el-table-column>
        <el-table-column :label="t('aiRoutes.requestId')" min-width="160">
          <template #default="{ row }"><span class="mono">{{ row.request_id || "-" }}</span></template>
        </el-table-column>
        <el-table-column :label="t('aiRoutes.errorCategory')" width="130">
          <template #default="{ row }"><StatusTag kind="error_category" :value="row.error_category" /></template>
        </el-table-column>
      </el-table>
      <el-empty v-else :description="t('aiRoutes.testEmpty')" :image-size="60" />
    </el-dialog>
  </el-card>
</template>

<style scoped>
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.small {
  font-size: 12px;
}
.paused-alert {
  margin-bottom: 10px;
}
.gateway-desc {
  margin-bottom: 8px;
}
.workers,
.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.health-collapse {
  border-top: none;
}
.hint-alert {
  margin-bottom: 12px;
}
.chips {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.chip {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
}
.chip-model {
  margin-right: 2px;
  word-break: break-all;
}
.chip-index {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.chip-tags {
  display: inline-flex;
  gap: 4px;
}
.params-cell {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.form-hint.inline {
  width: auto;
  margin-left: 8px;
}
.form-hint.warn {
  color: var(--el-color-warning);
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
  padding: 4px;
  border: 1px dashed transparent;
  border-radius: 4px;
}
.fallback-row.is-drag-over {
  border-color: var(--el-color-primary);
}
.fallback-row.is-dragging {
  opacity: 0.5;
}
.drag-handle {
  cursor: move;
  margin-top: 9px;
  color: var(--el-text-color-secondary);
}
.fallback-index {
  width: 18px;
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  text-align: right;
}
.fallback-select {
  flex: 1;
  min-width: 0;
}
.fallback-row .el-button {
  margin-top: 4px;
  margin-left: 0;
}
.row-error {
  color: var(--el-color-danger);
  font-size: 12px;
  line-height: 1.4;
}
.params-box {
  width: 100%;
}
.params-hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.test-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.test-head .spacer {
  flex: 1;
}
</style>
