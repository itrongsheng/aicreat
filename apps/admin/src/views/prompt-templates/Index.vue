<script setup lang="ts">
// Prompt 模板列表（docs/09 §5.6、§10.2、docs/04 §6.8、docs/13 §7.3、§12.2、§12.3）：
// - 筛选 kind / status / 范围（全局 / 项目）/ 关键词，「全部版本」开关（all_versions=1）；
// - 列：code、名称、kind、语言、范围、版本、状态、系统标记、更新人 / 时间；
// - 操作：编辑（published 时提示将创建新版本草稿）、发布、归档（409 last_published）、复制（409 owned_by_other）、
//   删除（仅 draft 且非系统）、版本历史；
// - 普通用户新建全局模板提示「草稿仅自己可见，需由总后台发布」；总后台处于用户视角时提示新建 / 编辑全局模板将归属到本人。
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import { Plus, Refresh, Search } from "@element-plus/icons-vue";
import {
  DEFAULT_PAGE_SIZE,
  PROMPT_CODE_PATTERN,
  SYSTEM_TEMPLATE_PREFIX,
  type ConflictData,
  type PromptKind,
  type PromptStatus,
  type PromptTemplate,
} from "@aicreat/shared";
import { fieldErrors, isApiError, validationErrors } from "@/api/client";
import * as promptTemplatesApi from "@/api/promptTemplates";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { useProjectStore } from "@/store/project";
import { formatDateTime } from "@/utils/format";

const { t } = useI18n();
const { has, isAllScope } = usePermission();
const router = useRouter();
const projectStore = useProjectStore();

/** 范围筛选：undefined 全部；0 全局；>0 项目 */
const filters = reactive<{ kind: PromptKind | undefined; status: PromptStatus | undefined; project_id: number | undefined; keyword: string; all_versions: boolean }>({
  kind: undefined,
  status: undefined,
  project_id: undefined,
  keyword: "",
  all_versions: false,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<PromptTemplate[]>([]);
const loading = ref(false);

const scopeOptions = computed(() => [
  { label: t("promptTemplates.scopeGlobal"), value: 0 },
  ...projectStore.projects.map((p) => ({ label: p.name, value: p.id })),
]);

async function load() {
  loading.value = true;
  try {
    const res = await promptTemplatesApi.list({
      page: page.value,
      page_size: pageSize.value,
      kind: filters.kind,
      status: filters.status,
      project_id: filters.project_id,
      keyword: filters.keyword.trim() || undefined,
      all_versions: filters.all_versions || undefined,
    });
    rows.value = res.items;
    total.value = res.total;
  } catch {
    rows.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

function search() {
  page.value = 1;
  void load();
}

function resetFilters() {
  filters.kind = undefined;
  filters.status = undefined;
  filters.project_id = undefined;
  filters.keyword = "";
  filters.all_versions = false;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

function projectName(id: number): string {
  const p = projectStore.projects.find((x) => x.id === id);
  return p ? p.name : `#${id}`;
}

/** 总后台处于用户视角：新建 / 编辑全局模板将归属到本人（13 §12.2） */
const viewingOwner = computed(() => isAllScope.value && projectStore.ownerId > 0);

// ---------- 新建 / 编辑 ----------
function gotoCreate() {
  void router.push({ name: "prompt-template-new", query: filters.kind ? { kind: filters.kind } : {} });
}

async function gotoEdit(row: PromptTemplate) {
  if (row.status === "published" && has("content.prompt_templates.update")) {
    try {
      await ElMessageBox.confirm(t("promptTemplates.editPublishedConfirm"), t("common.tip"), {
        type: "info",
        confirmButtonText: t("promptTemplates.continueEdit"),
      });
    } catch {
      return;
    }
  }
  void router.push({ name: "prompt-template-edit", params: { id: row.id } });
}

// ---------- 发布 / 归档 / 删除 ----------
const acting = ref<number | null>(null);

function describeUnknownVariables(err: unknown): string | null {
  const items = validationErrors(err).filter((e) => e.type === "unknown_variable");
  if (!items.length) return null;
  return t("promptTemplates.unknownVariables", { names: items.map((e) => String(e.input ?? "")).join("、") });
}

async function publish(row: PromptTemplate) {
  try {
    await ElMessageBox.confirm(t("promptTemplates.publishConfirm", { code: row.code, version: row.version }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await promptTemplatesApi.publish(row.id, { silent: true });
    ElMessage.success(t("promptTemplates.published"));
    void load();
  } catch (err) {
    if (isApiError(err)) {
      const unknown = describeUnknownVariables(err);
      const items = validationErrors(err);
      ElMessage.error(unknown ?? (items.length ? `${err.message}：${items.map((e) => e.msg).slice(0, 3).join("；")}` : err.message));
    }
  } finally {
    acting.value = null;
  }
}

async function archive(row: PromptTemplate) {
  try {
    await ElMessageBox.confirm(t("promptTemplates.archiveConfirm", { code: row.code, version: row.version }), t("common.tip"), { type: "warning" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await promptTemplatesApi.archive(row.id, { silent: true });
    ElMessage.success(t("promptTemplates.archived"));
    void load();
  } catch (err) {
    if (isApiError(err)) {
      const data = (err.data ?? {}) as ConflictData;
      ElMessage.error(err.code === 409 && data.reason === "last_published" ? t("promptTemplates.lastPublished") : err.message);
    }
  } finally {
    acting.value = null;
  }
}

async function remove(row: PromptTemplate) {
  try {
    await ElMessageBox.confirm(t("promptTemplates.deleteConfirm", { code: row.code, version: row.version }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await promptTemplatesApi.remove(row.id);
    ElMessage.success(t("promptTemplates.deleted"));
    if (rows.value.length === 1 && page.value > 1) page.value -= 1;
    void load();
  } catch {
    /* 拦截器已提示 */
  } finally {
    acting.value = null;
  }
}

// ---------- 复制 ----------
const dupVisible = ref(false);
const dupSaving = ref(false);
const dupSource = ref<PromptTemplate | null>(null);
const dupFormRef = ref<FormInstance>();
const dupErrors = ref<Record<string, string>>({});
const dupForm = reactive({ code: "", name: "", project_id: 0 });

const codeRules = computed(() => [
  { required: true, message: t("promptTemplates.form.codeRequired"), trigger: "blur" },
  {
    validator: (_rule: unknown, value: string, cb: (e?: Error) => void) => {
      if (value && value.startsWith(SYSTEM_TEMPLATE_PREFIX)) cb(new Error(t("promptTemplates.form.codeSysPrefix")));
      else if (value && !PROMPT_CODE_PATTERN.test(value)) cb(new Error(t("promptTemplates.form.codeRule")));
      else cb();
    },
    trigger: "blur",
  },
]);

const dupRules = computed<FormRules>(() => ({
  code: codeRules.value,
  name: [
    { required: true, message: t("promptTemplates.form.nameRequired"), trigger: "blur" },
    { max: 100, message: t("promptTemplates.form.maxLength", { max: 100 }), trigger: "blur" },
  ],
}));

function openDuplicate(row: PromptTemplate) {
  dupSource.value = row;
  const base = row.code.startsWith(SYSTEM_TEMPLATE_PREFIX) ? row.code.slice(SYSTEM_TEMPLATE_PREFIX.length) : row.code;
  dupForm.code = `${base}_copy`.slice(0, 80);
  dupForm.name = `${row.name} ${t("promptTemplates.copySuffix")}`.slice(0, 100);
  // 从全局 / 系统模板派生时缺省复制到顶栏当前项目，否则沿用源模板范围
  dupForm.project_id = row.project_id > 0 ? row.project_id : projectStore.currentId;
  dupErrors.value = {};
  dupVisible.value = true;
  dupFormRef.value?.clearValidate();
}

async function submitDuplicate() {
  const src = dupSource.value;
  if (!src || !dupFormRef.value) return;
  try {
    await dupFormRef.value.validate();
  } catch {
    return;
  }
  dupSaving.value = true;
  dupErrors.value = {};
  try {
    const created = await promptTemplatesApi.duplicate(src.id, { code: dupForm.code.trim(), name: dupForm.name.trim(), project_id: dupForm.project_id }, { silent: true });
    dupVisible.value = false;
    ElMessage.success(t("promptTemplates.duplicated", { code: created.code }));
    // 新草稿若为全局模板，用户视角下不可见：先回到全部用户（13 §12.2）
    if (created.project_id === 0 && viewingOwner.value) await projectStore.setOwner(0);
    void router.push({ name: "prompt-template-edit", params: { id: created.id } });
  } catch (err) {
    if (!isApiError(err)) return;
    if (err.code === 409) {
      const data = (err.data ?? {}) as ConflictData;
      dupErrors.value = { code: data.reason === "owned_by_other" ? t("promptTemplates.codeOwnedByOther") : t("promptTemplates.codeExists") };
    } else {
      dupErrors.value = fieldErrors(err);
      if (!Object.keys(dupErrors.value).length) ElMessage.error(err.message);
    }
  } finally {
    dupSaving.value = false;
  }
}

// ---------- 版本历史 ----------
const versionsVisible = ref(false);
const versionsLoading = ref(false);
const versionsOf = ref<PromptTemplate | null>(null);
const versionRows = ref<PromptTemplate[]>([]);

async function openVersions(row: PromptTemplate) {
  versionsOf.value = row;
  versionRows.value = [];
  versionsVisible.value = true;
  versionsLoading.value = true;
  try {
    versionRows.value = [...(await promptTemplatesApi.versions(row.id))].sort((a, b) => b.version - a.version);
  } catch {
    versionRows.value = [];
  } finally {
    versionsLoading.value = false;
  }
}

function openVersion(row: PromptTemplate) {
  versionsVisible.value = false;
  void router.push({ name: "prompt-template-edit", params: { id: row.id } });
}

onMounted(() => {
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void load();
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.promptTemplates") }}</h2>
        <el-button v-permission="'content.prompt_templates.create'" type="primary" :icon="Plus" @click="gotoCreate">{{ t("promptTemplates.create") }}</el-button>
      </div>
    </template>

    <el-alert v-if="viewingOwner" type="warning" :closable="false" show-icon :title="t('promptTemplates.ownerViewHint')" class="hint-alert" />
    <el-alert v-else-if="!isAllScope && has('content.prompt_templates.create')" type="info" :closable="false" show-icon :title="t('promptTemplates.ownDraftHint')" class="hint-alert" />

    <div class="toolbar">
      <el-input v-model="filters.keyword" :placeholder="t('promptTemplates.keywordPlaceholder')" clearable style="width: 220px" @keyup.enter="search" />
      <ToolbarSelect v-model="filters.kind" enum-name="prompt_kind" :placeholder="t('promptTemplates.kind')" width="140px" filterable @change="search" />
      <ToolbarSelect v-model="filters.status" enum-name="prompt_status" :placeholder="t('promptTemplates.status')" width="120px" @change="search" />
      <ToolbarSelect v-model="filters.project_id" :options="scopeOptions" :placeholder="t('promptTemplates.scope')" width="180px" filterable @change="search" />
      <el-checkbox v-model="filters.all_versions" @change="search">{{ t("promptTemplates.allVersions") }}</el-checkbox>
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <el-button :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column :label="t('promptTemplates.code')" min-width="170">
        <template #default="{ row }">
          <el-link type="primary" underline="never" class="mono" @click="gotoEdit(row)">{{ row.code }}</el-link>
        </template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.name')" min-width="160" show-overflow-tooltip>
        <template #default="{ row }">{{ row.name }}</template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.kind')" width="110">
        <template #default="{ row }"><StatusTag kind="prompt_kind" :value="row.kind" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.language')" width="80">
        <template #default="{ row }">{{ row.language }}</template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.scope')" min-width="120">
        <template #default="{ row }">
          <el-tag v-if="row.project_id === 0" size="small" type="info">{{ t("promptTemplates.scopeGlobal") }}</el-tag>
          <span v-else>{{ projectName(row.project_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.version')" width="70" align="center">
        <template #default="{ row }">v{{ row.version }}</template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.status')" width="90">
        <template #default="{ row }"><StatusTag kind="prompt_status" :value="row.status" /></template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.system')" width="70" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.is_system" size="small" type="warning" effect="plain">{{ t("promptTemplates.systemTag") }}</el-tag>
          <span v-else class="text-secondary">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('promptTemplates.updated')" width="175">
        <template #default="{ row }">
          <div>{{ formatDateTime(row.updated_at) }}</div>
          <div class="text-secondary small">{{ row.updated_by ? `#${row.updated_by}` : "-" }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="300" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="gotoEdit(row)">
            {{ row.status === "draft" && has("content.prompt_templates.update") ? t("common.edit") : t("promptTemplates.view") }}
          </el-button>
          <el-button v-if="row.status === 'draft'" v-permission="'content.prompt_templates.publish'" link type="success" :loading="acting === row.id" @click="publish(row)">
            {{ t("promptTemplates.publish") }}
          </el-button>
          <el-button v-if="row.status === 'published'" v-permission="'content.prompt_templates.publish'" link type="warning" :loading="acting === row.id" @click="archive(row)">
            {{ t("promptTemplates.archive") }}
          </el-button>
          <el-button v-permission="'content.prompt_templates.create'" link type="primary" @click="openDuplicate(row)">{{ t("promptTemplates.duplicate") }}</el-button>
          <el-button link type="primary" @click="openVersions(row)">{{ t("promptTemplates.versions") }}</el-button>
          <el-button
            v-if="row.status === 'draft' && !row.is_system"
            v-permission="'content.prompt_templates.delete'"
            link
            type="danger"
            :loading="acting === row.id"
            @click="remove(row)"
          >
            {{ t("common.delete") }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pagination">
      <el-pagination
        v-model:current-page="page"
        :page-size="pageSize"
        :page-sizes="[20, 50, 100]"
        :total="total"
        layout="total, sizes, prev, pager, next"
        background
        @current-change="load"
        @size-change="onPageSizeChange"
      />
    </div>

    <!-- 复制 -->
    <el-dialog v-model="dupVisible" :title="t('promptTemplates.duplicateTitle', { code: dupSource?.code ?? '' })" width="min(520px, 96vw)" :close-on-click-modal="false">
      <el-alert v-if="viewingOwner && dupForm.project_id === 0" type="warning" :closable="false" show-icon :title="t('promptTemplates.ownerViewHint')" class="hint-alert" />
      <el-form ref="dupFormRef" :model="dupForm" :rules="dupRules" label-width="90px" @submit.prevent="submitDuplicate">
        <el-form-item :label="t('promptTemplates.code')" prop="code" :error="dupErrors.code">
          <el-input v-model="dupForm.code" maxlength="80" class="mono-input" />
          <div class="form-hint">{{ t("promptTemplates.form.codeHint") }}</div>
        </el-form-item>
        <el-form-item :label="t('promptTemplates.name')" prop="name" :error="dupErrors.name">
          <el-input v-model="dupForm.name" maxlength="100" />
        </el-form-item>
        <el-form-item :label="t('promptTemplates.scope')" :error="dupErrors.project_id">
          <el-select v-model="dupForm.project_id" filterable style="width: 100%">
            <el-option v-for="o in scopeOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <div class="form-hint">{{ t("promptTemplates.duplicateHint") }}</div>
      </el-form>
      <template #footer>
        <el-button @click="dupVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="dupSaving" @click="submitDuplicate">{{ t("common.confirm") }}</el-button>
      </template>
    </el-dialog>

    <!-- 版本历史 -->
    <el-dialog v-model="versionsVisible" :title="t('promptTemplates.versionsTitle', { code: versionsOf?.code ?? '' })" width="min(720px, 96vw)">
      <el-table v-loading="versionsLoading" :data="versionRows" size="small" border max-height="420">
        <el-table-column :label="t('promptTemplates.version')" width="80">
          <template #default="{ row }">v{{ row.version }}</template>
        </el-table-column>
        <el-table-column :label="t('promptTemplates.name')" min-width="160" show-overflow-tooltip prop="name" />
        <el-table-column :label="t('promptTemplates.status')" width="90">
          <template #default="{ row }"><StatusTag kind="prompt_status" :value="row.status" /></template>
        </el-table-column>
        <el-table-column :label="t('promptTemplates.publishedAt')" width="165">
          <template #default="{ row }">{{ formatDateTime(row.published_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.updatedAt')" width="165">
          <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('common.actions')" width="80">
          <template #default="{ row }">
            <el-button link type="primary" @click="openVersion(row)">{{ t("promptTemplates.open") }}</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.hint-alert {
  margin-bottom: 12px;
}
.small {
  font-size: 12px;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
</style>
