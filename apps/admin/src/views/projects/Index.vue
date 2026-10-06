<script setup lang="ts">
// 项目列表与新建 / 编辑弹窗（docs/09 §4.3、docs/04 §6.7、§7.3、docs/13 §7.2、§12.3）：
// - 列表筛选 keyword / status，总后台另有「负责人」列与筛选（owner_id）；每行附 counts 与负责人摘要；
// - 弹窗字段：名称、slug、行业、受众、品牌名、品牌信息、说明、语言、默认风格、默认格式、负责人（仅总后台，缺省为顶栏所选用户或本人；
//   编辑时修改即转移负责人并二次确认）、常用平台；名称与 slug 在同一负责人下唯一（409 existing_id）；
// - 行操作：详情、编辑、归档 / 恢复（content.projects.status）、删除（仅 archived，409 in_use 时提示）。
// 路由查询 `?edit={id}` 打开编辑弹窗（项目详情页的「编辑基本信息」入口）。
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import { Plus, Refresh, Search } from "@element-plus/icons-vue";
import {
  CONTENT_FORMAT,
  CONTENT_STYLE,
  DEFAULT_PAGE_SIZE,
  SUPPORTED_LOCALES,
  type ConflictData,
  type ContentFormat,
  type ContentStyle,
  type Platform,
  type Project,
  type ProjectBody,
  type ProjectStatus,
} from "@aicreat/shared";
import { fieldErrors, isApiError } from "@/api/client";
import * as platformsApi from "@/api/platforms";
import * as projectsApi from "@/api/projects";
import * as settingsApi from "@/api/settings";
import OwnerSelect from "@/components/OwnerSelect.vue";
import StatusTag from "@/components/StatusTag.vue";
import ToolbarSelect from "@/components/ToolbarSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { useAuthStore } from "@/store/auth";
import { useProjectStore } from "@/store/project";
import { formatDateTime, formatNumber } from "@/utils/format";

const SLUG_RE = /^[a-z0-9][a-z0-9_-]*$/;

const { t } = useI18n();
const { has, isAllScope } = usePermission();
const auth = useAuthStore();
const projectStore = useProjectStore();
const route = useRoute();
const router = useRouter();

// ---------- 列表 ----------
const filters = reactive<{ keyword: string; status: ProjectStatus | undefined; owner_id: number }>({
  keyword: "",
  status: undefined,
  // 总后台：缺省跟随顶栏用户视角
  owner_id: projectStore.ownerId,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<Project[]>([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const res = await projectsApi.list({
      page: page.value,
      page_size: pageSize.value,
      keyword: filters.keyword.trim() || undefined,
      status: filters.status,
      // 总后台显式传 owner_id（0 = 全部用户 → null 不附加），普通用户由后端限定本人
      owner_id: isAllScope.value ? filters.owner_id || null : undefined,
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
  filters.keyword = "";
  filters.status = undefined;
  filters.owner_id = projectStore.ownerId;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

/** 列表、顶栏项目选择器与负责人候选（project_count）一起刷新 */
function refreshAll() {
  void load();
  projectStore.load().catch(() => undefined);
  if (isAllScope.value) projectStore.loadOwners(true).catch(() => undefined);
}

function ownerLabel(row: Project): string {
  if (row.owner) return row.owner.display_name || row.owner.username;
  const o = projectStore.owners.find((x) => x.id === row.owner_id);
  return o ? o.display_name || o.username : `#${row.owner_id}`;
}

function ownerNameById(id: number | undefined): string {
  if (!id) return "-";
  const o = projectStore.owners.find((x) => x.id === id);
  if (o) return o.display_name || o.username;
  if (id === auth.admin?.id) return auth.displayName;
  return `#${id}`;
}

// ---------- 常用平台（需 publish.platforms.view） ----------
const canViewPlatforms = computed(() => has("publish.platforms.view"));
const platforms = ref<Platform[]>([]);
const platformsLoaded = ref(false);

async function loadPlatforms() {
  if (!canViewPlatforms.value || platformsLoaded.value) return;
  try {
    platforms.value = await platformsApi.list({ is_active: true }, { silent: true });
    platformsLoaded.value = true;
  } catch {
    platforms.value = [];
  }
}

function platformLabel(p: Platform): string {
  return p.name || p.code;
}

// ---------- 运行时默认值（新建表单预填） ----------
const runtimeDefaults = reactive<{ style: ContentStyle; format: ContentFormat }>({ style: "news", format: "markdown" });
let runtimeLoaded = false;

async function loadRuntime() {
  if (runtimeLoaded) return;
  try {
    const rt = await settingsApi.runtime();
    runtimeDefaults.style = rt.generation_config.title.default_style ?? "news";
    runtimeDefaults.format = rt.generation_config.content.default_format ?? "markdown";
    runtimeLoaded = true;
  } catch {
    /* 使用内置缺省 */
  }
}

// ---------- 新建 / 编辑 ----------
const editVisible = ref(false);
const editSaving = ref(false);
const editing = ref<Project | null>(null);
const formRef = ref<FormInstance>();
const serverErrors = ref<Record<string, string>>({});
const form = reactive({
  name: "",
  slug: "",
  industry: "",
  audience: "",
  brand_name: "",
  brand_info: "",
  description: "",
  language: "zh-CN" as string,
  default_style: "news" as ContentStyle,
  default_format: "markdown" as ContentFormat,
  owner_id: 0,
  default_platform_ids: [] as number[],
});

const rules = computed<FormRules>(() => ({
  name: [
    { required: true, message: t("projects.form.nameRequired"), trigger: "blur" },
    { max: 100, message: t("projects.form.maxLength", { max: 100 }), trigger: "blur" },
  ],
  slug: [
    { required: true, message: t("projects.form.slugRequired"), trigger: "blur" },
    { max: 80, message: t("projects.form.maxLength", { max: 80 }), trigger: "blur" },
    { pattern: SLUG_RE, message: t("projects.form.slugRule"), trigger: "blur" },
  ],
  industry: [{ max: 80, message: t("projects.form.maxLength", { max: 80 }), trigger: "blur" }],
  audience: [{ max: 255, message: t("projects.form.maxLength", { max: 255 }), trigger: "blur" }],
  brand_name: [{ max: 100, message: t("projects.form.maxLength", { max: 100 }), trigger: "blur" }],
  description: [{ max: 500, message: t("projects.form.maxLength", { max: 500 }), trigger: "blur" }],
  owner_id: isAllScope.value ? [{ required: true, type: "number", min: 1, message: t("projects.form.ownerRequired"), trigger: "change" }] : [],
}));

/** 已选平台中不在启用列表里的（停用 / 无权限查看）也保留显示 */
const platformOptions = computed(() => {
  const known = new Set(platforms.value.map((p) => p.id));
  const extra = form.default_platform_ids.filter((id) => !known.has(id)).map((id) => ({ id, label: `#${id}` }));
  return [...platforms.value.map((p) => ({ id: p.id, label: platformLabel(p) })), ...extra];
});

function resetForm() {
  form.name = "";
  form.slug = "";
  form.industry = "";
  form.audience = "";
  form.brand_name = "";
  form.brand_info = "";
  form.description = "";
  form.language = "zh-CN";
  form.default_style = runtimeDefaults.style;
  form.default_format = runtimeDefaults.format;
  form.owner_id = projectStore.ownerId || auth.admin?.id || 0;
  form.default_platform_ids = [];
  serverErrors.value = {};
}

async function openCreate() {
  editing.value = null;
  await loadRuntime();
  resetForm();
  void loadPlatforms();
  editVisible.value = true;
  formRef.value?.clearValidate();
}

function fillFrom(p: Project) {
  form.name = p.name;
  form.slug = p.slug;
  form.industry = p.industry ?? "";
  form.audience = p.audience ?? "";
  form.brand_name = p.brand_name ?? "";
  form.brand_info = p.brand_info ?? "";
  form.description = p.description ?? "";
  form.language = p.language || "zh-CN";
  form.default_style = p.default_style;
  form.default_format = p.default_format;
  form.owner_id = p.owner_id;
  form.default_platform_ids = [...(p.default_platform_ids ?? [])];
  serverErrors.value = {};
}

function openEdit(row: Project) {
  editing.value = row;
  fillFrom(row);
  void loadPlatforms();
  editVisible.value = true;
  formRef.value?.clearValidate();
}

/** `?edit={id}`：从详情页进入编辑 */
async function openEditById(id: number) {
  try {
    const p = await projectsApi.get(id);
    openEdit(p);
  } catch {
    /* 404：拦截器已提示 */
  }
}

const trimOrNull = (v: string) => (v.trim() ? v.trim() : null);

function buildBody(): ProjectBody {
  const body: ProjectBody = {
    name: form.name.trim(),
    slug: form.slug.trim(),
    industry: trimOrNull(form.industry),
    audience: trimOrNull(form.audience),
    brand_name: trimOrNull(form.brand_name),
    brand_info: trimOrNull(form.brand_info),
    description: trimOrNull(form.description),
    language: form.language,
    default_style: form.default_style,
    default_format: form.default_format,
    default_platform_ids: [...form.default_platform_ids],
  };
  if (editing.value) body.default_templates = { ...(editing.value.default_templates ?? {}) };
  // 普通用户不传 owner_id（负责人恒为本人）
  if (isAllScope.value && form.owner_id > 0) body.owner_id = form.owner_id;
  return body;
}

function applyConflict(err: unknown): boolean {
  if (!isApiError(err) || err.code !== 409) return false;
  const data = (err.data ?? {}) as ConflictData;
  const msg = data.existing_id ? t("projects.form.duplicateWithId", { id: data.existing_id }) : t("projects.form.duplicate");
  serverErrors.value = { name: msg, slug: msg };
  return true;
}

async function submit() {
  if (!formRef.value) return;
  try {
    await formRef.value.validate();
  } catch {
    return;
  }
  const current = editing.value;
  // 转移负责人二次确认（13 §12.3）
  if (current && isAllScope.value && form.owner_id > 0 && form.owner_id !== current.owner_id) {
    try {
      await ElMessageBox.confirm(t("projects.transferConfirm", { name: ownerNameById(form.owner_id) }), t("projects.transferTitle"), {
        type: "warning",
        confirmButtonText: t("projects.transferOk"),
      });
    } catch {
      return;
    }
  }
  editSaving.value = true;
  serverErrors.value = {};
  try {
    const body = buildBody();
    if (current) {
      await projectsApi.update(current.id, body, { silent: true });
      ElMessage.success(t("common.saved"));
    } else {
      await projectsApi.create(body, { silent: true });
      ElMessage.success(t("projects.created"));
    }
    editVisible.value = false;
    refreshAll();
  } catch (err) {
    if (applyConflict(err)) {
      ElMessage.error(t("projects.form.duplicate"));
    } else if (isApiError(err)) {
      serverErrors.value = fieldErrors(err);
      ElMessage.error(Object.keys(serverErrors.value).length ? t("common.validationFailed") : err.message);
    }
  } finally {
    editSaving.value = false;
  }
}

const ownerChanged = computed(() => !!editing.value && isAllScope.value && form.owner_id > 0 && form.owner_id !== editing.value.owner_id);

// ---------- 归档 / 恢复 / 删除 ----------
const acting = ref<number | null>(null);

async function toggleArchive(row: Project) {
  const archiving = row.status === "active";
  try {
    await ElMessageBox.confirm(
      archiving ? t("projects.archiveConfirm", { name: row.name }) : t("projects.unarchiveConfirm", { name: row.name }),
      t("common.tip"),
      { type: archiving ? "warning" : "info" },
    );
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    if (archiving) await projectsApi.archive(row.id);
    else await projectsApi.unarchive(row.id);
    ElMessage.success(archiving ? t("projects.archived") : t("projects.unarchived"));
    refreshAll();
  } catch {
    /* 拦截器已提示（409 current_status 等） */
  } finally {
    acting.value = null;
  }
}

async function removeProject(row: Project) {
  try {
    await ElMessageBox.confirm(t("projects.deleteConfirm", { name: row.name }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = row.id;
  try {
    await projectsApi.remove(row.id, { silent: true });
    ElMessage.success(t("projects.deleted"));
    if (projectStore.currentId === row.id) projectStore.setCurrent(0);
    refreshAll();
  } catch (err) {
    if (isApiError(err)) {
      const data = (err.data ?? {}) as ConflictData;
      if (err.code === 409 && data.reason === "in_use") ElMessage.error(t("projects.deleteInUse"));
      else if (err.code === 409 && data.current_status) ElMessage.error(t("projects.deleteNeedArchive"));
      else ElMessage.error(err.message);
    }
  } finally {
    acting.value = null;
  }
}

function gotoDetail(row: Project) {
  void router.push({ name: "project-detail", params: { id: row.id } });
}

onMounted(async () => {
  void load();
  if (isAllScope.value && !projectStore.ownersLoaded) projectStore.loadOwners().catch(() => undefined);
  const editId = Number(route.query.edit);
  if (Number.isInteger(editId) && editId > 0 && has("content.projects.update")) {
    await openEditById(editId);
    void router.replace({ query: { ...route.query, edit: undefined } });
  }
});
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.projects") }}</h2>
        <el-button v-permission="'content.projects.create'" type="primary" :icon="Plus" @click="openCreate">{{ t("projects.create") }}</el-button>
      </div>
    </template>

    <div class="toolbar">
      <el-input v-model="filters.keyword" :placeholder="t('projects.keywordPlaceholder')" clearable style="width: 220px" @keyup.enter="search" />
      <ToolbarSelect v-model="filters.status" enum-name="project_status" :placeholder="t('projects.status')" width="140px" @change="search" />
      <OwnerSelect v-if="isAllScope" v-model="filters.owner_id" allow-all width="180px" @change="search" />
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button @click="resetFilters">{{ t("common.reset") }}</el-button>
      <span class="spacer" />
      <el-button :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column prop="id" :label="t('common.id')" width="70" />
      <el-table-column :label="t('projects.name')" min-width="180">
        <template #default="{ row }">
          <el-link type="primary" :underline="false" @click="gotoDetail(row)">{{ row.name }}</el-link>
          <div class="text-secondary mono">{{ row.slug }}</div>
        </template>
      </el-table-column>
      <el-table-column v-if="isAllScope" :label="t('projects.owner')" min-width="120">
        <template #default="{ row }">{{ ownerLabel(row) }}</template>
      </el-table-column>
      <el-table-column :label="t('projects.industry')" min-width="110">
        <template #default="{ row }">{{ row.industry || "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('projects.language')" width="90">
        <template #default="{ row }">{{ row.language }}</template>
      </el-table-column>
      <el-table-column :label="t('projects.defaultStyle')" width="100">
        <template #default="{ row }"><StatusTag kind="content_style" :value="row.default_style" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('projects.defaultFormat')" width="100">
        <template #default="{ row }"><StatusTag kind="content_format" :value="row.default_format" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('projects.counts')" min-width="170">
        <template #default="{ row }">
          <span class="counts">
            <span>{{ t("projects.countKeywords") }} {{ formatNumber(row.counts?.keywords ?? 0) }}</span>
            <span>{{ t("projects.countContents") }} {{ formatNumber(row.counts?.contents ?? 0) }}</span>
            <span>{{ t("projects.countLinks") }} {{ formatNumber(row.counts?.links ?? 0) }}</span>
          </span>
        </template>
      </el-table-column>
      <el-table-column :label="t('projects.status')" width="90">
        <template #default="{ row }"><StatusTag kind="project_status" :value="row.status" /></template>
      </el-table-column>
      <el-table-column :label="t('common.updatedAt')" width="165">
        <template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="230" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="gotoDetail(row)">{{ t("projects.detail") }}</el-button>
          <el-button v-permission="'content.projects.update'" link type="primary" @click="openEdit(row)">{{ t("common.edit") }}</el-button>
          <el-button v-permission="'content.projects.status'" link :type="row.status === 'active' ? 'warning' : 'success'" :loading="acting === row.id" @click="toggleArchive(row)">
            {{ row.status === "active" ? t("projects.archive") : t("projects.unarchive") }}
          </el-button>
          <span v-permission="'content.projects.delete'">
            <el-tooltip :content="t('projects.deleteTip')" :disabled="row.status === 'archived'" placement="top">
              <el-button link type="danger" :disabled="row.status !== 'archived'" @click="removeProject(row)">{{ t("common.delete") }}</el-button>
            </el-tooltip>
          </span>
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

    <!-- 新建 / 编辑 -->
    <el-dialog
      v-model="editVisible"
      :title="editing ? t('projects.editTitle', { name: editing.name }) : t('projects.createTitle')"
      width="min(720px, 96vw)"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px" @submit.prevent="submit">
        <el-form-item :label="t('projects.name')" prop="name" :error="serverErrors.name">
          <el-input v-model="form.name" maxlength="100" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('projects.slug')" prop="slug" :error="serverErrors.slug">
          <el-input v-model="form.slug" maxlength="80" class="mono-input" :placeholder="t('projects.form.slugPlaceholder')" />
          <div class="form-hint">{{ t("projects.form.uniqueHint") }}</div>
        </el-form-item>
        <el-form-item v-if="isAllScope" :label="t('projects.owner')" prop="owner_id" :error="serverErrors.owner_id">
          <OwnerSelect v-model="form.owner_id" :allow-all="false" :active-only="!editing" width="260px" />
          <div v-if="ownerChanged" class="form-hint warn">{{ t("projects.form.transferHint", { name: ownerNameById(form.owner_id) }) }}</div>
          <div v-else-if="!editing" class="form-hint">{{ t("projects.form.ownerHint") }}</div>
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item :label="t('projects.industry')" prop="industry" :error="serverErrors.industry">
              <el-input v-model="form.industry" maxlength="80" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="t('projects.brandName')" prop="brand_name" :error="serverErrors.brand_name">
              <el-input v-model="form.brand_name" maxlength="100" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item :label="t('projects.audience')" prop="audience" :error="serverErrors.audience">
          <el-input v-model="form.audience" maxlength="255" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('projects.brandInfo')" prop="brand_info" :error="serverErrors.brand_info">
          <el-input v-model="form.brand_info" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" maxlength="5000" :placeholder="t('projects.form.brandInfoPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('projects.description')" prop="description" :error="serverErrors.description">
          <el-input v-model="form.description" type="textarea" :autosize="{ minRows: 2, maxRows: 4 }" maxlength="500" show-word-limit />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item :label="t('projects.language')" prop="language" :error="serverErrors.language">
              <el-select v-model="form.language" style="width: 100%">
                <el-option v-for="l in SUPPORTED_LOCALES" :key="l" :label="l" :value="l" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item :label="t('projects.defaultStyle')" prop="default_style" :error="serverErrors.default_style">
              <el-select v-model="form.default_style" style="width: 100%">
                <el-option v-for="s in CONTENT_STYLE" :key="s" :label="t(`status.content_style.${s}`)" :value="s" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item :label="t('projects.defaultFormat')" prop="default_format" :error="serverErrors.default_format">
              <el-select v-model="form.default_format" style="width: 100%">
                <el-option v-for="f in CONTENT_FORMAT" :key="f" :label="t(`status.content_format.${f}`)" :value="f" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item :label="t('projects.platforms')" :error="serverErrors.default_platform_ids">
          <el-select
            v-if="canViewPlatforms"
            v-model="form.default_platform_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :max-collapse-tags="4"
            :placeholder="t('projects.form.platformsPlaceholder')"
            style="width: 100%"
          >
            <el-option v-for="p in platformOptions" :key="p.id" :label="p.label" :value="p.id" />
          </el-select>
          <span v-else class="text-secondary">
            {{ form.default_platform_ids.length ? form.default_platform_ids.map((id) => `#${id}`).join("、") : t("projects.form.platformsNoPermission") }}
          </span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="editSaving" @click="submit">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.counts {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.form-hint.warn {
  color: var(--el-color-warning);
}
.mono-input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
}
</style>
