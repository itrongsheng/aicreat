<script setup lang="ts">
// 发布平台规则页（docs/11 §5、§11.4；docs/04 §6.16）：
// - 列表（不分页）：code、名称、启用（开关 → PUT is_active，停用二次确认）、链接数、url_patterns / 删除文案 / 跳转规则条数、排序、系统标记、
//   操作（编辑 / 规则测试 / 删除：系统平台或有链接引用不可删，服务端 409 兜底）；
// - 「识别 URL」工具条：POST /platforms/detect → 命中的平台；
// - 编辑弹窗：基本信息 + url_patterns / deleted_markers / redirect_markers（可编辑列表，正则即时编译校验、文案 4~100 字符）+
//   fetch_config（JsonEditor，提交前按 §5.3 白名单校验：headers 只允许 Accept-Language / Referer / X-*，拒绝 cookie / authorization /
//   proxy-authorization；user_agent ≤ 200 且须含 aicreat）；服务端 400 的 loc 回显到对应条目；
// - 规则测试弹窗：输入 URL → POST /platforms/{id}/test → http_status / final_url / result_status / matched_rule / evidence.marker /
//   evidence.context，并显示 title 与 evidence.text_excerpt 供核对实际抓取内容（不写库）。
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { Aim, Delete, EditPen, Plus, Refresh } from "@element-plus/icons-vue";
import {
  PLATFORM_LIMITS,
  type Platform,
  type PlatformCreateBody,
  type PlatformDetectResult,
  type PlatformFetchConfig,
  type PlatformTestResult,
  type ValidationErrorItem,
} from "@aicreat/shared";
import { isApiError, validationErrors } from "@/api/client";
import * as platformsApi from "@/api/platforms";
import JsonEditor from "@/components/JsonEditor.vue";
import LinkEvidencePanel from "@/components/LinkEvidencePanel.vue";
import RuleListEditor from "@/components/RuleListEditor.vue";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect, { type ToolbarOption } from "@/components/ToolbarSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { invalidatePlatforms } from "@/composables/usePlatforms";
import { formatDuration } from "@/utils/format";
import { PLATFORM_CODE_PATTERN, checkFetchConfig, checkMarker, checkPublicUrl, checkRegex, platformLabel } from "@/utils/links";

type ListField = "url_patterns" | "deleted_markers" | "redirect_markers";

const { t } = useI18n();
const { has } = usePermission();

const canCreate = computed(() => has("publish.platforms.create"));
const canUpdate = computed(() => has("publish.platforms.update"));
const canDelete = computed(() => has("publish.platforms.delete"));
const canTest = computed(() => has("publish.platforms.test"));

// ---------- 列表 ----------
const activeFilter = ref<"1" | "0" | undefined>(undefined);
const rows = ref<Platform[]>([]);
const loading = ref(false);
const acting = ref<number | null>(null);

const activeOptions = computed<ToolbarOption[]>(() => [
  { value: "1", label: t("common.enabled") },
  { value: "0", label: t("common.disabled") },
]);

async function load() {
  loading.value = true;
  try {
    const list = await platformsApi.list(activeFilter.value === undefined ? {} : { is_active: activeFilter.value === "1" });
    rows.value = [...list].sort((a, b) => a.sort - b.sort || a.id - b.id);
  } catch {
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

function afterWrite() {
  invalidatePlatforms();
  void load();
}

async function toggleActive(row: Platform, on: boolean) {
  if (!on) {
    try {
      await ElMessageBox.confirm(t("platforms.deactivateConfirm", { name: platformLabel(row) }), t("common.tip"), { type: "warning" });
    } catch {
      return;
    }
  }
  acting.value = row.id;
  try {
    const updated = await platformsApi.update(row.id, { is_active: on });
    Object.assign(row, updated);
    invalidatePlatforms();
    ElMessage.success(on ? t("platforms.activated") : t("platforms.deactivated"));
  } catch {
    /* 已提示 */
  } finally {
    acting.value = null;
  }
}

function deleteBlocked(row: Platform): boolean {
  return row.is_system || (row.link_count ?? 0) > 0;
}

async function removeRow(row: Platform) {
  try {
    await ElMessageBox.confirm(t("platforms.deleteConfirm", { name: platformLabel(row), code: row.code }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await platformsApi.remove(row.id, { silent: true });
    ElMessage.success(t("platforms.deleted"));
    afterWrite();
  } catch (err) {
    if (isApiError(err)) ElMessage.error(err.code === 409 ? t("platforms.deleteInUse") : err.message);
  } finally {
    acting.value = null;
  }
}

function iconUrl(row: Platform): string | null {
  return row.icon && /^https?:\/\//i.test(row.icon) ? row.icon : null;
}

// ---------- 识别 URL ----------
const detectUrl = ref("");
const detecting = ref(false);
const detectResult = ref<PlatformDetectResult | null>(null);
const detectError = ref("");

const detectedPlatform = computed(() => (detectResult.value ? (rows.value.find((p) => p.id === detectResult.value!.platform_id) ?? null) : null));

async function runDetect() {
  detectResult.value = null;
  detectError.value = checkPublicUrl(detectUrl.value) ?? "";
  if (detectError.value) return;
  detecting.value = true;
  try {
    detectResult.value = await platformsApi.detect(detectUrl.value.trim(), { silent: true });
  } catch (err) {
    if (isApiError(err)) detectError.value = validationErrors(err)[0]?.msg ?? err.message;
  } finally {
    detecting.value = false;
  }
}

// ---------- 新建 / 编辑 ----------
interface PlatformForm {
  code: string;
  name: string;
  name_en: string;
  icon: string;
  home_url: string;
  url_patterns: string[];
  deleted_markers: string[];
  redirect_markers: string[];
  fetch_config: PlatformFetchConfig;
  is_active: boolean;
  sort: number;
}

const editVisible = ref(false);
const editing = ref<Platform | null>(null);
const saving = ref(false);
const form = ref<PlatformForm>(emptyForm());
const formErrors = ref<Record<string, string>>({});
const listServerErrors = ref<Record<ListField, Record<number, string>>>({ url_patterns: {}, deleted_markers: {}, redirect_markers: {} });
const fetchServerErrors = ref<ValidationErrorItem[]>([]);
const fetchJsonValid = ref(true);
const jsonEditorRef = ref<InstanceType<typeof JsonEditor>>();

function emptyForm(): PlatformForm {
  return {
    code: "",
    name: "",
    name_en: "",
    icon: "",
    home_url: "",
    url_patterns: [],
    deleted_markers: [],
    redirect_markers: [],
    fetch_config: {},
    is_active: true,
    sort: 0,
  };
}

function resetErrors() {
  formErrors.value = {};
  listServerErrors.value = { url_patterns: {}, deleted_markers: {}, redirect_markers: {} };
  fetchServerErrors.value = [];
  fetchJsonValid.value = true;
}

function openCreate() {
  editing.value = null;
  form.value = emptyForm();
  resetErrors();
  editVisible.value = true;
}

async function openEdit(row: Platform) {
  editing.value = row;
  resetErrors();
  // 列表项已含规则 JSON；再取一次详情确保最新
  let p = row;
  try {
    p = await platformsApi.get(row.id, { silent: true });
  } catch {
    p = row;
  }
  form.value = {
    code: p.code,
    name: p.name,
    name_en: p.name_en ?? "",
    icon: p.icon ?? "",
    home_url: p.home_url ?? "",
    url_patterns: [...(p.url_patterns ?? [])],
    deleted_markers: [...(p.deleted_markers ?? [])],
    redirect_markers: [...(p.redirect_markers ?? [])],
    fetch_config: { ...(p.fetch_config ?? {}) },
    is_active: p.is_active,
    sort: p.sort,
  };
  editVisible.value = true;
}

const fetchConfigErrors = computed(() => checkFetchConfig(form.value.fetch_config));

function cleanList(values: string[]): string[] {
  return values.map((v) => v.trim()).filter(Boolean);
}

function validateForm(): boolean {
  const errs: Record<string, string> = {};
  const f = form.value;
  if (!editing.value && !PLATFORM_CODE_PATTERN.test(f.code.trim())) errs.code = t("platforms.validate.code");
  if (!f.name.trim()) errs.name = t("platforms.validate.nameRequired");
  else if (f.name.trim().length > PLATFORM_LIMITS.name) errs.name = t("platforms.validate.tooLong", { max: PLATFORM_LIMITS.name });
  if (!f.name_en.trim()) errs.name_en = t("platforms.validate.nameRequired");
  else if (f.name_en.trim().length > PLATFORM_LIMITS.nameEn) errs.name_en = t("platforms.validate.tooLong", { max: PLATFORM_LIMITS.nameEn });
  if (f.icon.trim().length > PLATFORM_LIMITS.icon) errs.icon = t("platforms.validate.tooLong", { max: PLATFORM_LIMITS.icon });
  if (f.home_url.trim().length > PLATFORM_LIMITS.homeUrl) errs.home_url = t("platforms.validate.tooLong", { max: PLATFORM_LIMITS.homeUrl });
  const lists: [ListField, (v: string) => string | null, number][] = [
    ["url_patterns", checkRegex, PLATFORM_LIMITS.patternsMax],
    ["redirect_markers", checkRegex, PLATFORM_LIMITS.patternsMax],
    ["deleted_markers", checkMarker, PLATFORM_LIMITS.markersMax],
  ];
  for (const [field, check, max] of lists) {
    const values = cleanList(f[field]);
    if (values.length > max) errs[field] = t("platforms.validate.tooMany", { max });
    else if (values.some((v) => check(v))) errs[field] = t("platforms.validate.fixItems");
  }
  const jsonOk = jsonEditorRef.value?.validate() ?? true;
  if (!jsonOk || !fetchJsonValid.value) errs.fetch_config = t("platforms.validate.fetchConfigJson");
  else if (fetchConfigErrors.value.length) errs.fetch_config = t("platforms.validate.fetchConfigRules");
  formErrors.value = errs;
  return !Object.keys(errs).length;
}

function applyServerErrors(err: unknown) {
  if (!isApiError(err)) return;
  const items = validationErrors(err);
  if (!items.length) {
    if (err.code === 409) formErrors.value = { code: err.message };
    else ElMessage.error(err.message);
    return;
  }
  const lists: Record<ListField, Record<number, string>> = { url_patterns: {}, deleted_markers: {}, redirect_markers: {} };
  const fetchErrs: ValidationErrorItem[] = [];
  const rest: ValidationErrorItem[] = [];
  for (const item of items) {
    const field = String(item.loc[1] ?? "");
    const idx = item.loc[2];
    if ((field === "url_patterns" || field === "deleted_markers" || field === "redirect_markers") && typeof idx === "number") {
      // 提交时已去掉空条目：服务端序号对应提交数组，回显到编辑列表中第 idx 个非空条目
      const visibleIdx = nthNonEmpty(form.value[field], idx);
      lists[field][visibleIdx] = item.msg;
    } else if (field === "fetch_config") fetchErrs.push(item);
    else rest.push(item);
  }
  listServerErrors.value = lists;
  fetchServerErrors.value = fetchErrs;
  formErrors.value = fieldErrorsOf(rest);
  for (const field of ["url_patterns", "deleted_markers", "redirect_markers"] as ListField[]) {
    if (Object.keys(lists[field]).length) formErrors.value[field] = t("platforms.validate.fixItems");
  }
  if (fetchErrs.length) formErrors.value.fetch_config = t("platforms.validate.fetchConfigRules");
}

function fieldErrorsOf(items: ValidationErrorItem[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of items) {
    const field = item.loc.length > 1 ? String(item.loc[1]) : String(item.loc[0] ?? "");
    if (field && !out[field]) out[field] = item.msg;
  }
  return out;
}

function nthNonEmpty(values: string[], n: number): number {
  let count = -1;
  for (let i = 0; i < values.length; i += 1) {
    if (values[i].trim()) count += 1;
    if (count === n) return i;
  }
  return n;
}

function buildBody(): PlatformCreateBody {
  const f = form.value;
  return {
    code: f.code.trim(),
    name: f.name.trim(),
    name_en: f.name_en.trim(),
    icon: f.icon.trim() || null,
    home_url: f.home_url.trim() || null,
    url_patterns: cleanList(f.url_patterns),
    deleted_markers: cleanList(f.deleted_markers),
    redirect_markers: cleanList(f.redirect_markers),
    fetch_config: f.fetch_config ?? {},
    is_active: f.is_active,
    sort: Number(f.sort) || 0,
  };
}

async function submit() {
  listServerErrors.value = { url_patterns: {}, deleted_markers: {}, redirect_markers: {} };
  fetchServerErrors.value = [];
  if (!validateForm()) return;
  const body = buildBody();
  saving.value = true;
  try {
    if (editing.value) {
      const { code: _code, ...rest } = body;
      await platformsApi.update(editing.value.id, rest, { silent: true });
    } else {
      await platformsApi.create(body, { silent: true });
    }
    ElMessage.success(t("common.saved"));
    editVisible.value = false;
    afterWrite();
  } catch (err) {
    applyServerErrors(err);
  } finally {
    saving.value = false;
  }
}

// ---------- 规则测试 ----------
const testVisible = ref(false);
const testPlatform = ref<Platform | null>(null);
const testUrl = ref("");
const testing = ref(false);
const testError = ref("");
const testResult = ref<PlatformTestResult | null>(null);

function openTest(row: Platform) {
  testPlatform.value = row;
  testUrl.value = "";
  testError.value = "";
  testResult.value = null;
  testVisible.value = true;
}

async function runTest() {
  const p = testPlatform.value;
  if (!p) return;
  testResult.value = null;
  testError.value = checkPublicUrl(testUrl.value) ?? "";
  if (testError.value) return;
  testing.value = true;
  try {
    testResult.value = await platformsApi.testRule(p.id, testUrl.value.trim(), { silent: true });
  } catch (err) {
    if (isApiError(err)) testError.value = validationErrors(err)[0]?.msg ?? err.message;
  } finally {
    testing.value = false;
  }
}

onMounted(() => void load());
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.platforms") }}</h2>
        <el-button v-if="canCreate" type="primary" :icon="Plus" @click="openCreate">{{ t("platforms.create") }}</el-button>
      </div>
    </template>

    <div class="toolbar">
      <ToolbarSelect v-model="activeFilter" :options="activeOptions" :placeholder="t('common.status')" width="120px" @change="load" />
      <el-button :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
      <span class="spacer" />
      <div class="detect-bar">
        <el-input v-model="detectUrl" :placeholder="t('platforms.detect.placeholder')" clearable style="width: 300px" @keyup.enter="runDetect" @clear="detectResult = null" />
        <el-button :icon="Aim" :loading="detecting" @click="runDetect">{{ t("platforms.detect.button") }}</el-button>
      </div>
    </div>
    <div v-if="detectResult || detectError" class="detect-result">
      <span v-if="detectError" class="error-text">{{ detectError }}</span>
      <template v-else-if="detectResult">
        {{ t("platforms.detect.result") }}
        <b>{{ detectedPlatform ? platformLabel(detectedPlatform) : detectResult.code }}</b>
        <span class="text-secondary">（{{ detectResult.code }} · #{{ detectResult.platform_id }}）</span>
        <span v-if="detectResult.code === 'website'" class="text-secondary">{{ t("platforms.detect.fallback") }}</span>
      </template>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column prop="code" label="code" width="120">
        <template #default="{ row }"><span class="mono">{{ row.code }}</span></template>
      </el-table-column>
      <el-table-column :label="t('platforms.name')" min-width="160">
        <template #default="{ row }">
          <span class="name-cell">
            <img v-if="iconUrl(row)" :src="iconUrl(row)!" alt="" class="platform-icon" referrerpolicy="no-referrer" />
            <span>{{ row.name }}</span>
            <span class="text-secondary small">{{ row.name_en }}</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.enabled')" width="80" align="center">
        <template #default="{ row }">
          <el-switch
            :model-value="row.is_active"
            :disabled="!canUpdate"
            :loading="acting === row.id"
            size="small"
            @change="(v: string | number | boolean) => toggleActive(row, !!v)"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('platforms.linkCount')" width="80" align="right">
        <template #default="{ row }">{{ row.link_count ?? 0 }}</template>
      </el-table-column>
      <el-table-column :label="t('platforms.urlPatterns')" width="90" align="right">
        <template #default="{ row }">{{ row.url_patterns?.length ?? 0 }}</template>
      </el-table-column>
      <el-table-column :label="t('platforms.deletedMarkers')" width="90" align="right">
        <template #default="{ row }">{{ row.deleted_markers?.length ?? 0 }}</template>
      </el-table-column>
      <el-table-column :label="t('platforms.redirectMarkers')" width="90" align="right">
        <template #default="{ row }">{{ row.redirect_markers?.length ?? 0 }}</template>
      </el-table-column>
      <el-table-column :label="t('platforms.sort')" width="70" align="right" prop="sort" />
      <el-table-column :label="t('platforms.system')" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.is_system" size="small" type="info" disable-transitions>{{ t("platforms.systemTag") }}</el-tag>
          <span v-else class="text-secondary">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="200" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canUpdate" link type="primary" :icon="EditPen" @click="openEdit(row)">{{ t("common.edit") }}</el-button>
          <el-button v-if="canTest" link type="primary" @click="openTest(row)">{{ t("platforms.test.button") }}</el-button>
          <span v-if="canDelete">
            <el-tooltip :content="row.is_system ? t('platforms.deleteSystemTip') : t('platforms.deleteLinkedTip')" :disabled="!deleteBlocked(row)" placement="top">
              <el-button link type="danger" :icon="Delete" :disabled="deleteBlocked(row)" :loading="acting === row.id" @click="removeRow(row)" />
            </el-tooltip>
          </span>
        </template>
      </el-table-column>
    </el-table>

    <!-- 新建 / 编辑 -->
    <el-dialog
      v-model="editVisible"
      :title="editing ? t('platforms.editTitle', { code: editing.code }) : t('platforms.create')"
      width="min(820px, 96vw)"
      :close-on-click-modal="false"
      top="5vh"
    >
      <el-form label-width="130px" class="platform-form" @submit.prevent="submit">
        <div class="form-section">{{ t("platforms.form.basic") }}</div>
        <el-row :gutter="12">
          <el-col :xs="24" :sm="12">
            <el-form-item label="code" :error="formErrors.code" required>
              <el-input v-model="form.code" :disabled="!!editing" :maxlength="PLATFORM_LIMITS.code" class="mono" />
              <div class="form-hint">{{ editing ? t("platforms.form.codeImmutable") : t("platforms.form.codeHint") }}</div>
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('platforms.sort')" :error="formErrors.sort">
              <el-input-number v-model="form.sort" :min="-100000" :max="100000" :precision="0" controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('platforms.form.name')" :error="formErrors.name" required>
              <el-input v-model="form.name" :maxlength="PLATFORM_LIMITS.name" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('platforms.form.nameEn')" :error="formErrors.name_en" required>
              <el-input v-model="form.name_en" :maxlength="PLATFORM_LIMITS.nameEn" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('platforms.form.homeUrl')" :error="formErrors.home_url">
              <el-input v-model="form.home_url" :maxlength="PLATFORM_LIMITS.homeUrl" placeholder="https://" />
            </el-form-item>
          </el-col>
          <el-col :xs="24" :sm="12">
            <el-form-item :label="t('platforms.form.icon')" :error="formErrors.icon">
              <el-input v-model="form.icon" :maxlength="PLATFORM_LIMITS.icon" placeholder="https://…/favicon.ico" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item :label="t('common.enabled')">
              <el-switch v-model="form.is_active" />
              <span class="form-hint inline">{{ t("platforms.form.activeHint") }}</span>
            </el-form-item>
          </el-col>
        </el-row>

        <div class="form-section">{{ t("platforms.form.rules") }}</div>
        <el-form-item :label="t('platforms.urlPatterns')" :error="formErrors.url_patterns">
          <RuleListEditor
            v-model="form.url_patterns"
            :validator="checkRegex"
            :max="PLATFORM_LIMITS.patternsMax"
            :server-errors="listServerErrors.url_patterns"
            placeholder="^https?://(www\.)?example\.com/"
            mono
          />
          <div class="form-hint">{{ t("platforms.form.urlPatternsHint") }}</div>
        </el-form-item>
        <el-form-item :label="t('platforms.deletedMarkers')" :error="formErrors.deleted_markers">
          <RuleListEditor
            v-model="form.deleted_markers"
            :validator="checkMarker"
            :max="PLATFORM_LIMITS.markersMax"
            :server-errors="listServerErrors.deleted_markers"
            :placeholder="t('platforms.form.markerPlaceholder')"
          />
          <div class="form-hint">{{ t("platforms.form.deletedMarkersHint", { min: PLATFORM_LIMITS.markerMin, max: PLATFORM_LIMITS.markerMax }) }}</div>
        </el-form-item>
        <el-form-item :label="t('platforms.redirectMarkers')" :error="formErrors.redirect_markers">
          <RuleListEditor
            v-model="form.redirect_markers"
            :validator="checkRegex"
            :max="PLATFORM_LIMITS.patternsMax"
            :server-errors="listServerErrors.redirect_markers"
            placeholder="^https?://www\.example\.com/login"
            mono
          />
          <div class="form-hint">{{ t("platforms.form.redirectMarkersHint") }}</div>
        </el-form-item>

        <div class="form-section">{{ t("platforms.form.fetchConfig") }}</div>
        <el-form-item label="fetch_config" :error="formErrors.fetch_config">
          <JsonEditor
            ref="jsonEditorRef"
            v-model="form.fetch_config"
            object-only
            :rows="8"
            :errors="fetchServerErrors"
            placeholder="{}"
            @validity="(v: boolean) => (fetchJsonValid = v)"
          />
          <el-alert v-if="fetchConfigErrors.length" type="warning" :closable="false" show-icon class="fetch-alert">
            <ul class="fetch-errors">
              <li v-for="(msg, i) in fetchConfigErrors" :key="i">{{ msg }}</li>
            </ul>
          </el-alert>
          <div class="form-hint">{{ t("platforms.form.fetchConfigHint", { marker: PLATFORM_LIMITS.userAgentMarker, max: PLATFORM_LIMITS.userAgentMax }) }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>

    <!-- 规则测试 -->
    <el-dialog v-model="testVisible" :title="t('platforms.test.title', { name: testPlatform ? platformLabel(testPlatform) : '' })" width="min(760px, 96vw)" top="5vh">
      <div class="test-bar">
        <el-input v-model="testUrl" :placeholder="t('platforms.test.placeholder')" clearable @keyup.enter="runTest" />
        <el-button type="primary" :loading="testing" @click="runTest">{{ t("platforms.test.run") }}</el-button>
      </div>
      <div class="form-hint">{{ t("platforms.test.hint") }}</div>
      <el-alert v-if="testError" type="error" :title="testError" :closable="false" show-icon class="test-alert" />
      <div v-if="testResult" class="test-result">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item :label="t('evidence.result')"><StatusTag kind="link_check_result" :value="testResult.result_status" size="default" /></el-descriptions-item>
          <el-descriptions-item :label="t('evidence.matchedRule')"><StatusTag kind="link_check_rule" :value="testResult.matched_rule" size="default" /></el-descriptions-item>
          <el-descriptions-item :label="t('evidence.httpStatus')">{{ testResult.http_status ?? "-" }}</el-descriptions-item>
          <el-descriptions-item :label="t('evidence.duration')">{{ formatDuration(testResult.duration_ms) }}</el-descriptions-item>
          <el-descriptions-item :label="t('evidence.finalUrl')" :span="2"><span class="mono break">{{ testResult.final_url || "-" }}</span></el-descriptions-item>
          <el-descriptions-item :label="t('evidence.redirectCount')">{{ testResult.redirect_count ?? 0 }}</el-descriptions-item>
          <el-descriptions-item :label="t('evidence.pageTitle')">{{ testResult.title || "-" }}</el-descriptions-item>
        </el-descriptions>
        <div class="test-section">{{ t("evidence.evidence") }}</div>
        <LinkEvidencePanel :evidence="testResult.evidence" />
      </div>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.detect-bar {
  display: flex;
  gap: 6px;
  max-width: 100%;
}
.detect-bar .el-input {
  max-width: calc(100vw - 140px);
}
.detect-result {
  margin: -4px 0 12px;
  font-size: 13px;
}
.error-text {
  color: var(--el-color-danger);
}
.name-cell {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.platform-icon {
  width: 16px;
  height: 16px;
  border-radius: 3px;
  object-fit: contain;
}
.small {
  font-size: 12px;
}
.break {
  word-break: break-all;
}
.form-section {
  margin: 4px 0 12px;
  padding-left: 8px;
  border-left: 3px solid var(--el-color-primary);
  font-size: 14px;
  font-weight: 600;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.form-hint.inline {
  width: auto;
  margin-left: 8px;
}
.platform-form :deep(.json-editor) {
  width: 100%;
}
.fetch-alert {
  margin-top: 8px;
  width: 100%;
}
.fetch-alert :deep(.el-alert__description) {
  font-size: 12px;
}
.fetch-errors {
  margin: 0;
  padding-left: 16px;
}
.test-bar {
  display: flex;
  gap: 8px;
}
.test-alert {
  margin-top: 10px;
}
.test-result {
  margin-top: 12px;
}
.test-section {
  margin: 14px 0 8px;
  font-size: 14px;
  font-weight: 600;
}
@media (max-width: 640px) {
  .platform-form :deep(.el-form-item) {
    display: block;
  }
  .platform-form :deep(.el-form-item__label) {
    justify-content: flex-start;
    width: auto !important;
  }
}
</style>
