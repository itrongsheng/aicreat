<script setup lang="ts">
// 用户管理（docs/07 §8.3、docs/13 §12.3）：列表含数据范围列（取所属用户组）；新增 / 编辑 / 启用禁用 / 重置密码
import { computed, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import { Plus, Refresh, Search } from "@element-plus/icons-vue";
import { DEFAULT_PAGE_SIZE, type AdminGroupItem, type AdminItem, type DataScope } from "@aicreat/shared";
import * as adminsApi from "@/api/admins";
import { fieldErrors } from "@/api/client";
import * as groupsApi from "@/api/groups";
import StatusTag from "@/components/StatusTag.vue";
import { usePermission } from "@/composables/usePermission";
import { useAuthStore } from "@/store/auth";
import { formatDateTime } from "@/utils/format";

const { t } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();

const USERNAME_RE = /^[A-Za-z0-9_.-]{3,50}$/;
const PASSWORD_RE = /^(?=.*[A-Za-z])(?=.*\d).{8,}$/;
const byteLength = (value: string) => new TextEncoder().encode(value).length;

const canViewGroups = computed(() => has("security.groups.view"));

// ---------- 用户组 ----------
const groups = ref<AdminGroupItem[]>([]);
const activeGroups = computed(() => groups.value.filter((g) => g.is_active));

async function loadGroups() {
  if (!canViewGroups.value) return;
  try {
    groups.value = await groupsApi.listGroups();
  } catch {
    groups.value = [];
  }
}

function scopeLabel(scope: DataScope | undefined | null): string {
  if (scope === "all") return t("scope.adminAll");
  if (scope === "own") return t("scope.adminOwn");
  return "-";
}

// ---------- 列表 ----------
const filters = reactive<{ keyword: string; group_id: number | undefined; is_active: boolean | undefined }>({
  keyword: "",
  group_id: undefined,
  is_active: undefined,
});
const page = ref(1);
const pageSize = ref(DEFAULT_PAGE_SIZE);
const total = ref(0);
const rows = ref<AdminItem[]>([]);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const res = await adminsApi.listAdmins({
      page: page.value,
      page_size: pageSize.value,
      keyword: filters.keyword.trim() || undefined,
      group_id: filters.group_id,
      is_active: filters.is_active,
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
  filters.group_id = undefined;
  filters.is_active = undefined;
  search();
}

function onPageSizeChange(size: number) {
  pageSize.value = size;
  search();
}

onMounted(() => {
  void loadGroups();
  void load();
});

const isSelf = (row: AdminItem) => row.id === auth.admin?.id;

// ---------- 新增 / 编辑 ----------
const editVisible = ref(false);
const editSaving = ref(false);
const editing = ref<AdminItem | null>(null);
const editFormRef = ref<FormInstance>();
const editErrors = ref<Record<string, string>>({});
const editForm = reactive({
  username: "",
  display_name: "",
  group_id: undefined as number | undefined,
  password: "",
  confirm: "",
  is_active: true,
});

const editRules = computed<FormRules>(() => {
  const rules: FormRules = {
    display_name: [{ max: 50, message: t("admins.displayNameRule"), trigger: "blur" }],
    group_id: [{ required: true, message: t("admins.groupRequired"), trigger: "change" }],
  };
  if (!editing.value) {
    rules.username = [
      { required: true, message: t("common.required"), trigger: "blur" },
      { pattern: USERNAME_RE, message: t("admins.usernameRule"), trigger: "blur" },
    ];
    rules.password = [
      { required: true, message: t("common.required"), trigger: "blur" },
      {
        validator: (_rule, value: string, callback) => {
          if (!PASSWORD_RE.test(value)) callback(new Error(t("auth.passwordPolicy")));
          else if (byteLength(value) > 72) callback(new Error(t("auth.passwordTooLong")));
          else callback();
        },
        trigger: "blur",
      },
    ];
    rules.confirm = [
      { required: true, message: t("common.required"), trigger: "blur" },
      {
        validator: (_rule, value: string, callback) => (value !== editForm.password ? callback(new Error(t("auth.passwordMismatch"))) : callback()),
        trigger: "blur",
      },
    ];
  }
  return rules;
});

/** 编辑弹窗的用户组下拉：仅启用组；当前所属组即使已停用也保留以便回显 */
const groupOptions = computed(() => {
  const list = [...activeGroups.value];
  const currentId = editing.value?.group?.id;
  if (currentId && !list.some((g) => g.id === currentId)) {
    const current = groups.value.find((g) => g.id === currentId);
    if (current) list.push(current);
  }
  return list;
});

const selectedGroup = computed(() => groups.value.find((g) => g.id === editForm.group_id) ?? null);
const groupChanged = computed(() => !!editing.value && editForm.group_id !== editing.value.group?.id);

function openCreate() {
  editing.value = null;
  editErrors.value = {};
  Object.assign(editForm, { username: "", display_name: "", group_id: undefined, password: "", confirm: "", is_active: true });
  editVisible.value = true;
  editFormRef.value?.clearValidate();
}

function openEdit(row: AdminItem) {
  editing.value = row;
  editErrors.value = {};
  Object.assign(editForm, {
    username: row.username,
    display_name: row.display_name ?? "",
    group_id: row.group?.id,
    password: "",
    confirm: "",
    is_active: row.is_active,
  });
  editVisible.value = true;
  editFormRef.value?.clearValidate();
}

async function submitEdit() {
  editErrors.value = {};
  const valid = await editFormRef.value?.validate().catch(() => false);
  if (!valid) return;
  const displayName = editForm.display_name.trim() || null;
  editSaving.value = true;
  try {
    if (editing.value) {
      const body: adminsApi.AdminUpdateBody = {};
      if (displayName !== (editing.value.display_name ?? null)) body.display_name = displayName;
      if (!isSelf(editing.value) && editForm.group_id !== undefined && editForm.group_id !== editing.value.group?.id) body.group_id = editForm.group_id;
      if (Object.keys(body).length > 0) {
        await adminsApi.updateAdmin(editing.value.id, body);
        ElMessage.success(t("common.saved"));
      }
    } else {
      await adminsApi.createAdmin({
        username: editForm.username.trim(),
        display_name: displayName,
        password: editForm.password,
        group_id: editForm.group_id as number,
        is_active: editForm.is_active,
      });
      ElMessage.success(t("common.success"));
    }
    editVisible.value = false;
    if (editing.value && isSelf(editing.value)) void auth.fetchMe().catch(() => undefined);
    void load();
    void loadGroups();
  } catch (err) {
    editErrors.value = fieldErrors(err);
  } finally {
    editSaving.value = false;
  }
}

// ---------- 启用 / 禁用 ----------
async function toggleStatus(row: AdminItem) {
  const next = !row.is_active;
  const name = row.display_name || row.username;
  try {
    await ElMessageBox.confirm(next ? t("admins.enableConfirm", { name }) : t("admins.disableConfirm", { name }), t("common.tip"), {
      type: next ? "info" : "warning",
    });
  } catch {
    return;
  }
  try {
    await adminsApi.setAdminStatus(row.id, next);
    ElMessage.success(next ? t("admins.enabledTip") : t("admins.disabledTip"));
    void load();
    void loadGroups();
  } catch {
    /* 拦截器已提示 */
  }
}

// ---------- 重置密码 ----------
const resetVisible = ref(false);
const resetSaving = ref(false);
const resetTarget = ref<AdminItem | null>(null);
const resetFormRef = ref<FormInstance>();
const resetErrors = ref<Record<string, string>>({});
const resetForm = reactive({ password: "", confirm: "" });

const resetRules = computed<FormRules>(() => ({
  password: [
    { required: true, message: t("common.required"), trigger: "blur" },
    {
      validator: (_rule, value: string, callback) => {
        if (!PASSWORD_RE.test(value)) callback(new Error(t("auth.passwordPolicy")));
        else if (byteLength(value) > 72) callback(new Error(t("auth.passwordTooLong")));
        else callback();
      },
      trigger: "blur",
    },
  ],
  confirm: [
    { required: true, message: t("common.required"), trigger: "blur" },
    {
      validator: (_rule, value: string, callback) => (value !== resetForm.password ? callback(new Error(t("auth.passwordMismatch"))) : callback()),
      trigger: "blur",
    },
  ],
}));

function openReset(row: AdminItem) {
  resetTarget.value = row;
  resetForm.password = "";
  resetForm.confirm = "";
  resetErrors.value = {};
  resetVisible.value = true;
  resetFormRef.value?.clearValidate();
}

async function submitReset() {
  if (!resetTarget.value) return;
  resetErrors.value = {};
  const valid = await resetFormRef.value?.validate().catch(() => false);
  if (!valid) return;
  resetSaving.value = true;
  try {
    await adminsApi.resetPassword(resetTarget.value.id, resetForm.password);
    resetVisible.value = false;
    resetForm.password = "";
    resetForm.confirm = "";
    ElMessage.success(t("admins.resetDone"));
  } catch (err) {
    resetErrors.value = fieldErrors(err);
  } finally {
    resetSaving.value = false;
  }
}
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("admins.title") }}</h2>
        <el-button v-permission="'security.admins.create'" type="primary" :icon="Plus" @click="openCreate">{{ t("admins.create") }}</el-button>
      </div>
    </template>

    <div class="toolbar">
      <el-input v-model="filters.keyword" :placeholder="t('admins.keywordPlaceholder')" clearable style="width: 220px" @keyup.enter="search" />
      <el-select v-if="canViewGroups" v-model="filters.group_id" :placeholder="t('admins.group')" clearable filterable style="width: 200px">
        <el-option v-for="g in groups" :key="g.id" :label="g.name" :value="g.id" />
      </el-select>
      <el-select v-model="filters.is_active" :placeholder="t('admins.status')" clearable style="width: 120px">
        <el-option :label="t('common.active')" :value="true" />
        <el-option :label="t('common.inactive')" :value="false" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search">{{ t("common.search") }}</el-button>
      <el-button :icon="Refresh" @click="resetFilters">{{ t("common.reset") }}</el-button>
    </div>

    <el-table v-loading="loading" :data="rows" row-key="id" stripe>
      <el-table-column prop="id" :label="t('common.id')" width="80" />
      <el-table-column prop="username" :label="t('admins.username')" min-width="140" />
      <el-table-column :label="t('admins.displayName')" min-width="140">
        <template #default="{ row }">{{ row.display_name || "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('admins.group')" min-width="140">
        <template #default="{ row }">{{ row.group?.name ?? "-" }}</template>
      </el-table-column>
      <el-table-column :label="t('admins.dataScope')" width="110">
        <template #default="{ row }">
          <el-tag :type="row.data_scope === 'all' ? 'primary' : 'info'" size="small" effect="plain">{{ scopeLabel(row.data_scope) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('admins.status')" width="90">
        <template #default="{ row }"><StatusTag kind="active" :value="String(row.is_active)" /></template>
      </el-table-column>
      <el-table-column :label="t('admins.lastLogin')" width="170">
        <template #default="{ row }">{{ formatDateTime(row.last_login_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.createdAt')" width="170">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="230" fixed="right">
        <template #default="{ row }">
          <el-button v-permission="'security.admins.update'" link type="primary" @click="openEdit(row)">{{ t("common.edit") }}</el-button>
          <span v-permission="'security.admins.status'">
            <el-tooltip :content="t('admins.selfStatusLocked')" :disabled="!isSelf(row)" placement="top">
              <el-button link :type="row.is_active ? 'danger' : 'success'" :disabled="isSelf(row)" @click="toggleStatus(row)">
                {{ row.is_active ? t("admins.disable") : t("admins.enable") }}
              </el-button>
            </el-tooltip>
          </span>
          <el-button v-permission="'security.admins.reset_password'" link type="warning" @click="openReset(row)">{{ t("admins.resetPassword") }}</el-button>
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

    <!-- 新增 / 编辑 -->
    <el-dialog
      v-model="editVisible"
      :title="editing ? `${t('admins.edit')} - ${editing.username}` : t('admins.create')"
      width="520px"
      :close-on-click-modal="false"
    >
      <el-form ref="editFormRef" :model="editForm" :rules="editRules" label-width="110px" @submit.prevent="submitEdit">
        <!-- 编辑弹窗只含显示名称与所属用户组（07 §8.3） -->
        <el-form-item v-if="!editing" :label="t('admins.username')" prop="username" :error="editErrors.username">
          <el-input v-model="editForm.username" :placeholder="t('admins.usernameRule')" autocomplete="off" />
        </el-form-item>
        <el-form-item :label="t('admins.displayName')" prop="display_name" :error="editErrors.display_name">
          <el-input v-model="editForm.display_name" maxlength="50" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('admins.group')" prop="group_id" :error="editErrors.group_id">
          <template v-if="canViewGroups">
            <el-select v-model="editForm.group_id" filterable :disabled="!!editing && isSelf(editing)" style="width: 100%">
              <el-option v-for="g in groupOptions" :key="g.id" :label="g.name" :value="g.id" :disabled="!g.is_active">
                <div class="group-option">
                  <span>{{ g.name }}</span>
                  <el-tag size="small" :type="g.data_scope === 'all' ? 'primary' : 'info'" effect="plain">{{ scopeLabel(g.data_scope) }}</el-tag>
                </div>
              </el-option>
            </el-select>
          </template>
          <template v-else>
            <el-input-number v-model="editForm.group_id" :min="1" :controls="false" :disabled="!!editing && isSelf(editing)" :placeholder="t('admins.groupId')" />
            <div class="form-hint">{{ t("admins.noGroupPermission") }}</div>
          </template>
          <div v-if="editing && isSelf(editing)" class="form-hint">{{ t("admins.selfGroupLocked") }}</div>
          <div v-else-if="groupChanged" class="form-hint warn">{{ t("admins.groupChangedTip") }}</div>
          <div v-if="selectedGroup" class="form-hint">{{ t("admins.dataScope") }}：{{ scopeLabel(selectedGroup.data_scope) }}</div>
        </el-form-item>
        <template v-if="!editing">
          <el-form-item :label="t('admins.initialPassword')" prop="password" :error="editErrors.password">
            <el-input v-model="editForm.password" type="password" show-password autocomplete="new-password" :placeholder="t('auth.passwordPolicy')" />
          </el-form-item>
          <el-form-item :label="t('admins.confirmPassword')" prop="confirm">
            <el-input v-model="editForm.confirm" type="password" show-password autocomplete="new-password" />
          </el-form-item>
          <el-form-item :label="t('admins.status')" prop="is_active">
            <el-switch v-model="editForm.is_active" :active-text="t('common.active')" :inactive-text="t('common.inactive')" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="editSaving" @click="submitEdit">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetVisible" :title="`${t('admins.resetPassword')} - ${resetTarget?.display_name || resetTarget?.username || ''}`" width="440px" :close-on-click-modal="false">
      <el-form ref="resetFormRef" :model="resetForm" :rules="resetRules" label-width="100px" @submit.prevent="submitReset">
        <el-form-item :label="t('admins.newPassword')" prop="password" :error="resetErrors.password">
          <el-input v-model="resetForm.password" type="password" show-password autocomplete="new-password" :placeholder="t('auth.passwordPolicy')" />
        </el-form-item>
        <el-form-item :label="t('admins.confirmPassword')" prop="confirm">
          <el-input v-model="resetForm.confirm" type="password" show-password autocomplete="new-password" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="resetSaving" @click="submitReset">{{ t("common.confirm") }}</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.group-option {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.form-hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.form-hint.warn {
  color: var(--el-color-warning);
}
</style>
