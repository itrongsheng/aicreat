<script setup lang="ts">
// 用户组权限（docs/07 §8.4、docs/13 §12.3）：左侧用户组列表，右侧基本信息（含数据范围单选）+ 权限树。
// 权限树 check-strictly 并自定义联动：勾选 action 自动勾选其 view；取消 view 取消其全部 action；
// 保存前按 07 §4.3 在本地补齐同资源 view 与跨资源依赖并高亮「自动勾选」节点，以响应 permission_codes 回显。
import { computed, nextTick, onMounted, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules, type TreeInstance } from "element-plus";
import { Plus } from "@element-plus/icons-vue";
import type { AdminGroupItem, DataScope, PermissionTreeModule } from "@aicreat/shared";
import { fieldErrors } from "@/api/client";
import * as groupsApi from "@/api/groups";
import { usePermission } from "@/composables/usePermission";
import { useAuthStore } from "@/store/auth";

const { t } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();

/** 跨资源依赖（与后端 PERMISSION_DEPENDENCIES 一致，07 §4.3） */
const PERMISSION_DEPENDENCIES: Record<string, string[]> = {
  "content.keywords.generate": ["content.batches.view"],
  "content.titles.generate": ["content.batches.view"],
  "content.contents.generate": ["content.batches.view"],
  "media.images.generate": ["media.assets.view"],
  "media.videos.generate": ["media.assets.view"],
};

/** 总后台职能权限：不受数据范围限制，只应授予 all 范围的组（13 §4.3） */
const ALL_SCOPE_ONLY = new Set([
  "ai.models.sync",
  "ai.routes.create",
  "ai.routes.update",
  "ai.routes.delete",
  "ai.routes.test",
  "ai.routes.reset_breaker",
  "ai.usage.reconcile",
  "publish.platforms.create",
  "publish.platforms.update",
  "publish.platforms.delete",
  "publish.platforms.test",
  "stats.reports.recompute",
]);
const isAllScopeOnly = (code: string) => code.startsWith("security.") || code.startsWith("system.settings.") || ALL_SCOPE_ONLY.has(code);

type NodeType = "module" | "menu" | "action";
interface PermNode {
  code: string;
  name: string;
  type: NodeType;
  module: string;
  parent: string | null;
  children?: PermNode[];
}

const canCreate = computed(() => has("security.groups.create"));
const canUpdate = computed(() => has("security.groups.update"));
const canAssign = computed(() => has("security.groups.assign"));
const canDelete = computed(() => has("security.groups.delete"));

// ---------- 用户组列表 ----------
const groups = ref<AdminGroupItem[]>([]);
const listLoading = ref(false);
const selectedId = ref<number | null>(null);
const detail = ref<AdminGroupItem | null>(null);
const detailLoading = ref(false);

const isSuperAdminGroup = computed(() => detail.value?.code === "super_admin");

async function loadGroups(selectId?: number | null) {
  listLoading.value = true;
  try {
    groups.value = await groupsApi.listGroups();
  } catch {
    groups.value = [];
  } finally {
    listLoading.value = false;
  }
  const target = selectId ?? selectedId.value;
  const next = groups.value.find((g) => g.id === target) ?? groups.value[0] ?? null;
  if (next && next.id !== detail.value?.id) await selectGroup(next.id);
  else if (!next) {
    selectedId.value = null;
    detail.value = null;
  }
}

function scopeLabel(scope: DataScope): string {
  return scope === "all" ? t("scope.adminAll") : t("scope.adminOwn");
}

// ---------- 权限树 ----------
const treeRef = ref<TreeInstance>();
const tree = ref<PermNode[]>([]);
const treeLoaded = ref(false);
const parentOf = new Map<string, string>();
const nameOf = new Map<string, string>();
const allCodes = ref<string[]>([]);

async function loadTree() {
  if (treeLoaded.value) return;
  const modules: PermissionTreeModule[] = await groupsApi.permissionTree();
  const codes: string[] = [];
  tree.value = modules.map((m) => ({
    code: `module:${m.module}`,
    name: m.name,
    type: "module" as const,
    module: m.module,
    parent: null,
    children: m.items.map((menu) => {
      codes.push(menu.code);
      nameOf.set(menu.code, menu.name);
      return {
        code: menu.code,
        name: menu.name,
        type: "menu" as const,
        module: m.module,
        parent: null,
        children: (menu.children ?? []).map((action) => {
          codes.push(action.code);
          parentOf.set(action.code, menu.code);
          nameOf.set(action.code, action.name);
          return { code: action.code, name: action.name, type: "action" as const, module: m.module, parent: menu.code };
        }),
      };
    }),
  }));
  allCodes.value = codes;
  treeLoaded.value = true;
}

const permReadonly = computed(() => !detail.value || isSuperAdminGroup.value || !canAssign.value);

const treeProps = {
  label: "name",
  children: "children",
  disabled: (data: Record<string, unknown>) => data.type === "module" || permReadonly.value,
  class: (data: Record<string, unknown>) => `perm-node--${String(data.type)}`,
};

const checked = ref<Set<string>>(new Set());
const savedCodes = ref<Set<string>>(new Set());
const autoAdded = ref<Set<string>>(new Set());

const dirty = computed(() => {
  if (checked.value.size !== savedCodes.value.size) return true;
  for (const c of checked.value) if (!savedCodes.value.has(c)) return true;
  return false;
});

function applyChecked(keys: Iterable<string>) {
  const valid = new Set(allCodes.value);
  checked.value = new Set([...keys].filter((k) => valid.has(k)));
  for (const k of [...autoAdded.value]) if (!checked.value.has(k)) autoAdded.value.delete(k);
  void nextTick(() => treeRef.value?.setCheckedKeys([...checked.value]));
}

function onCheck(data: Record<string, unknown>, state: { checkedKeys: (string | number)[] }) {
  const node = data as unknown as PermNode;
  const keys = new Set(state.checkedKeys.map(String));
  const isChecked = keys.has(node.code);
  if (node.type === "action" && isChecked && node.parent) keys.add(node.parent);
  if (node.type === "menu" && !isChecked) for (const child of node.children ?? []) keys.delete(child.code);
  applyChecked(keys);
}

function moduleCodes(node: PermNode): string[] {
  return (node.children ?? []).flatMap((menu) => [menu.code, ...(menu.children ?? []).map((a) => a.code)]);
}

function selectModule(node: PermNode, select: boolean) {
  const keys = new Set(checked.value);
  for (const code of moduleCodes(node)) {
    if (select) keys.add(code);
    else keys.delete(code);
  }
  applyChecked(keys);
}

function expandModule(node: PermNode, expand: boolean) {
  const treeNode = treeRef.value?.getNode(node.code);
  if (!treeNode) return;
  if (expand) {
    treeNode.expand();
    for (const child of treeNode.childNodes) child.expand();
  } else {
    treeNode.collapse();
  }
}

/** 07 §4.3：同资源补齐 view + 跨资源依赖 */
function normalize(codes: Set<string>): Set<string> {
  const out = new Set(codes);
  for (const code of codes) {
    const parent = parentOf.get(code);
    if (parent) out.add(parent);
  }
  for (const code of [...out]) for (const dep of PERMISSION_DEPENDENCIES[code] ?? []) out.add(dep);
  return out;
}

const ownScopeConflicts = computed(() => {
  if (basicForm.data_scope !== "own") return [];
  return [...checked.value].filter(isAllScopeOnly).sort();
});

// ---------- 选中组 ----------
const basicFormRef = ref<FormInstance>();
const basicErrors = ref<Record<string, string>>({});
const basicForm = reactive({ name: "", description: "", is_active: true, data_scope: "own" as DataScope });
const basicSaving = ref(false);
const permSaving = ref(false);

const basicRules = computed<FormRules>(() => ({
  name: [
    { required: true, message: t("groups.nameRequired"), trigger: "blur" },
    { min: 1, max: 50, message: t("groups.nameTooLong"), trigger: "blur" },
  ],
  description: [{ max: 255, message: t("groups.descriptionTooLong"), trigger: "blur" }],
}));

function fillBasic(group: AdminGroupItem) {
  basicForm.name = group.name;
  basicForm.description = group.description ?? "";
  basicForm.is_active = group.is_active;
  basicForm.data_scope = group.data_scope;
  basicErrors.value = {};
  basicFormRef.value?.clearValidate();
}

async function selectGroup(id: number) {
  if (dirty.value && detail.value && detail.value.id !== id && !permReadonly.value) {
    try {
      await ElMessageBox.confirm(t("groups.dirtyTip"), t("common.tip"), { type: "warning" });
    } catch {
      return;
    }
  }
  selectedId.value = id;
  detailLoading.value = true;
  try {
    await loadTree();
    const group = await groupsApi.getGroup(id);
    detail.value = group;
    fillBasic(group);
    const codes = group.code === "super_admin" ? allCodes.value : (group.permission_codes ?? []);
    savedCodes.value = new Set(codes);
    autoAdded.value = new Set();
    applyChecked(codes);
  } catch {
    /* 拦截器已提示 */
  } finally {
    detailLoading.value = false;
  }
}

watch(
  () => isSuperAdminGroup.value,
  (sa) => {
    if (sa) applyChecked(allCodes.value);
  },
);

async function saveBasic() {
  const group = detail.value;
  if (!group) return;
  basicErrors.value = {};
  const valid = await basicFormRef.value?.validate().catch(() => false);
  if (!valid) return;
  const body: groupsApi.GroupUpdateBody = {};
  const name = basicForm.name.trim();
  const description = basicForm.description.trim() || null;
  if (name !== group.name) body.name = name;
  if (description !== (group.description ?? null)) body.description = description;
  if (!group.is_system && basicForm.is_active !== group.is_active) body.is_active = basicForm.is_active;
  if (group.code !== "super_admin" && basicForm.data_scope !== group.data_scope) body.data_scope = basicForm.data_scope;
  if (Object.keys(body).length === 0) {
    ElMessage.success(t("common.saved"));
    return;
  }
  basicSaving.value = true;
  try {
    const updated = await groupsApi.updateGroup(group.id, body);
    detail.value = { ...group, ...updated, permission_codes: group.permission_codes };
    fillBasic(detail.value);
    ElMessage.success(t("common.saved"));
    if (auth.admin?.group?.id === group.id) void auth.fetchMe().catch(() => undefined);
    await loadGroups(group.id);
  } catch (err) {
    basicErrors.value = fieldErrors(err);
  } finally {
    basicSaving.value = false;
  }
}

async function savePermissions() {
  const group = detail.value;
  if (!group || permReadonly.value) return;
  const normalized = normalize(checked.value);
  const added = [...normalized].filter((c) => !checked.value.has(c));
  if (group.is_system && normalized.size === 0) {
    ElMessage.warning(t("groups.emptySystemGroup"));
    return;
  }
  if (added.length) {
    autoAdded.value = new Set(added);
    applyChecked(normalized);
    ElMessage.info(t("groups.autoChecked"));
  }
  permSaving.value = true;
  try {
    const updated = await groupsApi.saveGroupPermissions(group.id, [...normalized].sort());
    const codes = updated.permission_codes ?? [...normalized];
    const serverAdded = codes.filter((c) => !normalized.has(c));
    if (serverAdded.length) autoAdded.value = new Set([...autoAdded.value, ...serverAdded]);
    savedCodes.value = new Set(codes);
    applyChecked(codes);
    detail.value = { ...group, ...updated };
    ElMessage.success(t("common.saved"));
    if (auth.admin?.group?.id === group.id) void auth.fetchMe().catch(() => undefined);
  } catch {
    /* 拦截器已提示（400 无效权限码时该组原权限不变） */
  } finally {
    permSaving.value = false;
  }
}

function resetPermissions() {
  autoAdded.value = new Set();
  applyChecked(savedCodes.value);
}

// ---------- 删除 ----------
const deleteDisabledReason = computed(() => {
  const group = detail.value;
  if (!group) return "";
  if (group.is_system) return t("groups.deleteDisabledSystem");
  if (group.admin_count > 0) return t("groups.deleteDisabledMembers");
  return "";
});

async function removeGroup() {
  const group = detail.value;
  if (!group || deleteDisabledReason.value) return;
  try {
    await ElMessageBox.confirm(t("groups.deleteConfirm", { name: group.name }), t("common.warning"), { type: "warning" });
  } catch {
    return;
  }
  try {
    await groupsApi.deleteGroup(group.id);
    ElMessage.success(t("common.success"));
    detail.value = null;
    selectedId.value = null;
    savedCodes.value = new Set();
    checked.value = new Set();
    await loadGroups();
  } catch {
    /* 拦截器已提示 */
  }
}

// ---------- 新增 ----------
const createVisible = ref(false);
const createSaving = ref(false);
const createFormRef = ref<FormInstance>();
const createErrors = ref<Record<string, string>>({});
const createForm = reactive({ name: "", description: "", is_active: true, data_scope: "own" as DataScope });

function openCreate() {
  Object.assign(createForm, { name: "", description: "", is_active: true, data_scope: "own" });
  createErrors.value = {};
  createVisible.value = true;
  createFormRef.value?.clearValidate();
}

async function submitCreate() {
  createErrors.value = {};
  const valid = await createFormRef.value?.validate().catch(() => false);
  if (!valid) return;
  createSaving.value = true;
  try {
    const created = await groupsApi.createGroup({
      name: createForm.name.trim(),
      description: createForm.description.trim() || null,
      is_active: createForm.is_active,
      data_scope: createForm.data_scope,
    });
    createVisible.value = false;
    ElMessage.success(t("groups.createdTip"));
    savedCodes.value = new Set();
    checked.value = new Set();
    await loadGroups(created.id);
  } catch (err) {
    createErrors.value = fieldErrors(err);
  } finally {
    createSaving.value = false;
  }
}

onMounted(() => {
  void loadGroups();
});
</script>

<template>
  <div class="groups-page">
    <el-card shadow="never" class="groups-list" body-class="groups-list-body">
      <template #header>
        <div class="page-header">
          <h2 class="page-title">{{ t("groups.title") }}</h2>
          <el-button v-if="canCreate" type="primary" size="small" :icon="Plus" @click="openCreate">{{ t("groups.create") }}</el-button>
        </div>
      </template>
      <el-scrollbar v-loading="listLoading">
        <ul class="group-items">
          <li v-for="g in groups" :key="g.id" class="group-item" :class="{ active: g.id === selectedId }" @click="selectGroup(g.id)">
            <div class="group-item-main">
              <span class="group-item-name">{{ g.name }}</span>
              <span class="group-item-count">{{ t("groups.adminCount", { count: g.admin_count }) }}</span>
            </div>
            <div class="group-item-tags">
              <el-tag v-if="g.is_system" size="small" type="warning" effect="plain">{{ t("groups.system") }}</el-tag>
              <el-tag size="small" :type="g.data_scope === 'all' ? 'primary' : 'info'" effect="plain">{{ scopeLabel(g.data_scope) }}</el-tag>
              <el-tag v-if="!g.is_active" size="small" type="info">{{ t("common.disabled") }}</el-tag>
            </div>
          </li>
        </ul>
        <el-empty v-if="!listLoading && groups.length === 0" :description="t('common.noData')" :image-size="60" />
      </el-scrollbar>
    </el-card>

    <div v-loading="detailLoading" class="groups-detail">
      <el-card v-if="!detail" shadow="never">
        <el-empty :description="t('groups.selectGroup')" />
      </el-card>
      <template v-else>
        <el-card shadow="never" class="page-card">
          <template #header>
            <div class="page-header">
              <span class="detail-title">
                {{ t("groups.basicInfo") }}
                <span class="detail-code mono">{{ detail.code }}</span>
              </span>
              <div>
                <el-tooltip v-if="canDelete" :content="deleteDisabledReason" :disabled="!deleteDisabledReason" placement="top">
                  <span>
                    <el-button type="danger" plain size="small" :disabled="!!deleteDisabledReason" @click="removeGroup">{{ t("common.delete") }}</el-button>
                  </span>
                </el-tooltip>
              </div>
            </div>
          </template>
          <el-form ref="basicFormRef" :model="basicForm" :rules="basicRules" label-width="100px" :disabled="!canUpdate" @submit.prevent="saveBasic">
            <el-form-item :label="t('groups.name')" prop="name" :error="basicErrors.name">
              <el-input v-model="basicForm.name" maxlength="50" show-word-limit style="max-width: 420px" />
            </el-form-item>
            <el-form-item :label="t('groups.description')" prop="description" :error="basicErrors.description">
              <el-input v-model="basicForm.description" type="textarea" :rows="2" maxlength="255" show-word-limit style="max-width: 560px" />
            </el-form-item>
            <el-form-item :label="t('groups.status')" prop="is_active" :error="basicErrors.is_active">
              <el-switch v-model="basicForm.is_active" :disabled="detail.is_system" :active-text="t('common.enabled')" :inactive-text="t('common.disabled')" />
              <span v-if="detail.is_system" class="form-hint inline">{{ t("groups.systemStatusLocked") }}</span>
            </el-form-item>
            <el-form-item :label="t('groups.dataScope')" prop="data_scope" :error="basicErrors.data_scope">
              <div>
                <el-radio-group v-model="basicForm.data_scope" :disabled="isSuperAdminGroup">
                  <el-radio value="all">{{ t("scope.allData") }}</el-radio>
                  <el-radio value="own">{{ t("scope.ownData") }}</el-radio>
                </el-radio-group>
                <div class="form-hint">{{ isSuperAdminGroup ? t("groups.superAdminScopeLocked") : t("scope.ownHint") }}</div>
              </div>
            </el-form-item>
            <el-form-item v-if="canUpdate">
              <el-button type="primary" :loading="basicSaving" @click="saveBasic">{{ t("common.save") }}</el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" class="page-card">
          <template #header>
            <div class="page-header">
              <span class="detail-title">{{ t("groups.permissions") }}</span>
              <div v-if="!permReadonly" class="perm-actions">
                <span v-if="dirty" class="form-hint inline warn">{{ t("groups.dirtyTip") }}</span>
                <el-button size="small" :disabled="!dirty" @click="resetPermissions">{{ t("common.reset") }}</el-button>
                <el-button type="primary" size="small" :loading="permSaving" @click="savePermissions">{{ t("groups.savePermissions") }}</el-button>
              </div>
            </div>
          </template>
          <el-alert v-if="isSuperAdminGroup" type="info" :closable="false" show-icon :title="t('groups.superAdminReadonly')" class="perm-alert" />
          <el-alert v-if="ownScopeConflicts.length" type="warning" :closable="false" show-icon :title="t('scope.ownScopeWarning')" class="perm-alert">
            <div class="perm-conflicts">
              <el-tag v-for="code in ownScopeConflicts" :key="code" size="small" type="warning" effect="plain">{{ nameOf.get(code) || code }}</el-tag>
            </div>
          </el-alert>
          <el-tree
            ref="treeRef"
            :data="tree"
            node-key="code"
            show-checkbox
            check-strictly
            default-expand-all
            :expand-on-click-node="false"
            :props="treeProps"
            class="perm-tree"
            @check="onCheck"
          >
            <template #default="{ data }">
              <span class="perm-node" :class="{ 'is-auto': autoAdded.has(data.code) }">
                <span class="perm-node-name">{{ data.name }}</span>
                <span v-if="data.type !== 'module'" class="perm-node-code mono">{{ data.code }}</span>
                <el-tag v-if="autoAdded.has(data.code)" size="small" type="success" effect="plain">{{ t("groups.autoChecked") }}</el-tag>
                <span v-if="data.type === 'module'" class="perm-module-actions" @click.stop>
                  <template v-if="!permReadonly">
                    <el-button link type="primary" size="small" @click="selectModule(data, true)">{{ t("groups.selectAll") }}</el-button>
                    <el-button link size="small" @click="selectModule(data, false)">{{ t("groups.unselectAll") }}</el-button>
                  </template>
                  <el-button link size="small" @click="expandModule(data, true)">{{ t("groups.expand") }}</el-button>
                  <el-button link size="small" @click="expandModule(data, false)">{{ t("groups.collapse") }}</el-button>
                </span>
              </span>
            </template>
          </el-tree>
        </el-card>
      </template>
    </div>

    <el-dialog v-model="createVisible" :title="t('groups.create')" width="520px" :close-on-click-modal="false">
      <el-form ref="createFormRef" :model="createForm" :rules="basicRules" label-width="100px" @submit.prevent="submitCreate">
        <el-form-item :label="t('groups.name')" prop="name" :error="createErrors.name">
          <el-input v-model="createForm.name" maxlength="50" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('groups.description')" prop="description" :error="createErrors.description">
          <el-input v-model="createForm.description" type="textarea" :rows="2" maxlength="255" show-word-limit />
        </el-form-item>
        <el-form-item :label="t('groups.status')" prop="is_active">
          <el-switch v-model="createForm.is_active" :active-text="t('common.enabled')" :inactive-text="t('common.disabled')" />
        </el-form-item>
        <el-form-item :label="t('groups.dataScope')" prop="data_scope" :error="createErrors.data_scope">
          <div>
            <el-radio-group v-model="createForm.data_scope">
              <el-radio value="all">{{ t("scope.allData") }}</el-radio>
              <el-radio value="own">{{ t("scope.ownData") }}</el-radio>
            </el-radio-group>
            <div class="form-hint">{{ t("scope.ownHint") }}</div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="createSaving" @click="submitCreate">{{ t("common.save") }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.groups-page {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}
.groups-list {
  width: 300px;
  flex-shrink: 0;
  position: sticky;
  top: 0;
}
.groups-list :deep(.groups-list-body) {
  padding: 8px;
  max-height: calc(100vh - 200px);
  overflow: hidden;
  display: flex;
}
.group-items {
  list-style: none;
  margin: 0;
  padding: 0;
}
.group-item {
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}
.group-item + .group-item {
  margin-top: 4px;
}
.group-item:hover {
  background: var(--el-fill-color-light);
}
.group-item.active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.group-item-main {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.group-item-name {
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.group-item-count {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  flex-shrink: 0;
}
.group-item-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}
.groups-detail {
  flex: 1;
  min-width: 0;
}
.detail-title {
  font-weight: 600;
}
.detail-code {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-weight: normal;
}
.form-hint {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}
.form-hint.inline {
  margin: 0 0 0 12px;
}
.form-hint.warn {
  color: var(--el-color-warning);
}
.perm-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.perm-alert {
  margin-bottom: 12px;
}
.perm-conflicts {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}
.perm-tree :deep(.perm-node--module > .el-tree-node__content > .el-checkbox) {
  display: none;
}
.perm-tree :deep(.perm-node--module > .el-tree-node__content) {
  height: 34px;
  font-weight: 600;
}
.perm-node {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  padding-right: 8px;
}
.perm-node.is-auto .perm-node-name {
  color: var(--el-color-success);
  font-weight: 600;
}
.perm-node-code {
  color: var(--el-text-color-placeholder);
}
.perm-module-actions {
  margin-left: auto;
  display: inline-flex;
  gap: 2px;
}
</style>
