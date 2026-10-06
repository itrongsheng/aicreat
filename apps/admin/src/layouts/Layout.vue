<script lang="ts">
// 侧边菜单为显式配置：每项绑定一个 *.view 权限码并按其过滤（docs/07 §4.4、§9.5）；
// menuGroups 同时供 router/index.ts 的 resolveHomePath() 使用。key 对应 i18n 词条 menu.<key>，分组为 menu.groups.<key>。
export interface MenuItem {
  path: string;
  key: string;
  permission: string;
}

export interface MenuGroup {
  key: string;
  items: MenuItem[];
}

export const menuGroups: MenuGroup[] = [
  { key: "console", items: [{ path: "/dashboard", key: "dashboard", permission: "dashboard.view" }] },
  {
    key: "content",
    items: [
      { path: "/projects", key: "projects", permission: "content.projects.view" },
      { path: "/prompt-templates", key: "promptTemplates", permission: "content.prompt_templates.view" },
      { path: "/keywords", key: "keywords", permission: "content.keywords.view" },
      { path: "/titles", key: "titles", permission: "content.titles.view" },
      { path: "/contents", key: "contents", permission: "content.contents.view" },
      { path: "/generation-batches", key: "generationBatches", permission: "content.batches.view" },
    ],
  },
  {
    key: "media",
    items: [
      { path: "/media/assets", key: "mediaAssets", permission: "media.assets.view" },
      { path: "/media/images", key: "mediaImages", permission: "media.images.view" },
      { path: "/media/videos", key: "mediaVideos", permission: "media.videos.view" },
    ],
  },
  {
    key: "ai",
    items: [
      { path: "/ai/models", key: "aiModels", permission: "ai.models.view" },
      { path: "/ai/routes", key: "aiRoutes", permission: "ai.routes.view" },
      { path: "/ai/tasks", key: "aiTasks", permission: "ai.tasks.view" },
      { path: "/ai/usage", key: "aiUsage", permission: "ai.usage.view" },
    ],
  },
  {
    key: "publish",
    items: [
      { path: "/platforms", key: "platforms", permission: "publish.platforms.view" },
      { path: "/links", key: "links", permission: "publish.links.view" },
      { path: "/monitoring/link-checks", key: "linkChecks", permission: "monitoring.link_checks.view" },
      { path: "/monitoring/index-checks", key: "indexChecks", permission: "monitoring.index_checks.view" },
      { path: "/alerts", key: "alerts", permission: "monitoring.alerts.view" },
    ],
  },
  { key: "stats", items: [{ path: "/stats/reports", key: "statsReports", permission: "stats.reports.view" }] },
  {
    key: "system",
    items: [
      { path: "/settings", key: "settings", permission: "system.settings.view" },
      { path: "/admins", key: "admins", permission: "security.admins.view" },
      { path: "/admin-groups", key: "adminGroups", permission: "security.groups.view" },
      { path: "/admin-operation-logs", key: "operationLogs", permission: "security.audit.view" },
    ],
  },
];
</script>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch, type Component } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus";
import {
  ArrowDown,
  Cpu,
  Document,
  Expand,
  Fold,
  Lock,
  Odometer,
  Picture,
  Promotion,
  Setting,
  SwitchButton,
  TrendCharts,
} from "@element-plus/icons-vue";
import * as authApi from "@/api/auth";
import { fieldErrors } from "@/api/client";
import AlertBadge from "@/components/AlertBadge.vue";
import LangSwitch from "@/components/LangSwitch.vue";
import OwnerSelect from "@/components/OwnerSelect.vue";
import ProjectSelect from "@/components/ProjectSelect.vue";
import ThemeSwitch from "@/components/ThemeSwitch.vue";
import { usePermission } from "@/composables/usePermission";
import { useAlertsStore } from "@/store/alerts";
import { useAuthStore } from "@/store/auth";
import { useProjectStore } from "@/store/project";

const auth = useAuthStore();
const project = useProjectStore();
const alerts = useAlertsStore();
const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const { has, isAllScope } = usePermission();

const logoUrl = `${import.meta.env.BASE_URL}favicon.svg`;

const GROUP_ICONS: Record<string, Component> = {
  console: Odometer,
  content: Document,
  media: Picture,
  ai: Cpu,
  publish: Promotion,
  stats: TrendCharts,
  system: Setting,
};

const COLLAPSE_KEY = "aicreat.menu_collapsed";
function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === "1";
  } catch {
    return false;
  }
}
const collapsed = ref(readCollapsed());
function toggleCollapse() {
  collapsed.value = !collapsed.value;
  try {
    localStorage.setItem(COLLAPSE_KEY, collapsed.value ? "1" : "0");
  } catch {
    /* ignore */
  }
}

const visibleMenuGroups = computed(() =>
  menuGroups.map((g) => ({ ...g, items: g.items.filter((m) => auth.hasPermission(m.permission)) })).filter((g) => g.items.length > 0),
);

const activeMenu = computed(() => route.meta.activeMenu ?? route.path);

const activeEntry = computed(() => {
  for (const group of visibleMenuGroups.value) {
    const item = group.items.find((m) => m.path === activeMenu.value);
    if (item) return { group, item };
  }
  return null;
});

const defaultOpeneds = computed(() => (activeEntry.value ? [activeEntry.value.group.key] : []));

const breadcrumbs = computed(() => {
  const crumbs: { label: string; to?: string }[] = [];
  const entry = activeEntry.value;
  if (entry) {
    crumbs.push({ label: t(`menu.groups.${entry.group.key}`) });
    const isDetail = route.path !== entry.item.path;
    crumbs.push({ label: t(`menu.${entry.item.key}`), to: isDetail ? entry.item.path : undefined });
    if (isDetail && route.meta.title) crumbs.push({ label: t(route.meta.title) });
  } else if (route.meta.title) {
    crumbs.push({ label: t(route.meta.title) });
  }
  return crumbs;
});

const showOwnerSelect = computed(() => isAllScope.value && has("content.projects.view"));
const showProjectSelect = computed(() => has("content.projects.view"));
const showAlertBadge = computed(() => has("monitoring.alerts.view"));
const viewingOwner = computed(() => isAllScope.value && project.ownerId > 0);
const scopeLabel = computed(() => (isAllScope.value ? t("scope.all") : t("scope.own")));

function syncScopeData() {
  if (showAlertBadge.value) alerts.start();
  else alerts.reset();
  project.loadOwners().catch(() => undefined);
  project.load().catch(() => undefined);
}

onMounted(syncScopeData);
onBeforeUnmount(() => alerts.stop());

// 权限 / 数据范围变化（刷新权限后）时重新判定轮询与选择器数据
watch(
  () => [auth.permissions.join(","), auth.admin?.data_scope],
  () => syncScopeData(),
);
// 切换用户视角后刷新告警摘要（summary 接受 owner_id）
watch(
  () => project.ownerId,
  () => {
    if (showAlertBadge.value) void alerts.refresh();
  },
);

async function backToAllUsers() {
  await project.setOwner(0);
}

// ---------- 用户菜单 ----------
async function onUserCommand(command: string) {
  if (command === "password") {
    openPasswordDialog();
    return;
  }
  if (command === "logout") {
    try {
      await ElMessageBox.confirm(t("auth.logoutConfirm"), t("common.tip"), { type: "warning" });
    } catch {
      return;
    }
    alerts.reset();
    auth.logout();
    await router.replace({ name: "login" });
  }
}

// ---------- 修改密码 ----------
const PASSWORD_RE = /^(?=.*[A-Za-z])(?=.*\d).{8,}$/;
const passwordVisible = ref(false);
const passwordSaving = ref(false);
const passwordFormRef = ref<FormInstance>();
const passwordForm = reactive({ old_password: "", new_password: "", confirm: "" });
const passwordErrors = ref<Record<string, string>>({});

function byteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

const passwordRules = computed<FormRules>(() => ({
  old_password: [{ required: true, message: t("common.required"), trigger: "blur" }],
  new_password: [
    { required: true, message: t("common.required"), trigger: "blur" },
    {
      validator: (_rule, value: string, callback) => {
        if (!PASSWORD_RE.test(value)) callback(new Error(t("auth.passwordPolicy")));
        else if (byteLength(value) > 72) callback(new Error(t("auth.passwordTooLong")));
        else if (value === passwordForm.old_password) callback(new Error(t("auth.sameAsOld")));
        else callback();
      },
      trigger: "blur",
    },
  ],
  confirm: [
    { required: true, message: t("common.required"), trigger: "blur" },
    {
      validator: (_rule, value: string, callback) => (value !== passwordForm.new_password ? callback(new Error(t("auth.passwordMismatch"))) : callback()),
      trigger: "blur",
    },
  ],
}));

function openPasswordDialog() {
  passwordForm.old_password = "";
  passwordForm.new_password = "";
  passwordForm.confirm = "";
  passwordErrors.value = {};
  passwordVisible.value = true;
}

async function submitPassword() {
  passwordErrors.value = {};
  const valid = await passwordFormRef.value?.validate().catch(() => false);
  if (!valid) return;
  passwordSaving.value = true;
  try {
    await authApi.changePassword({ old_password: passwordForm.old_password, new_password: passwordForm.new_password });
    passwordVisible.value = false;
    alerts.reset();
    auth.logout(false); // 令牌已失效，不再请求 logout 接口
    ElMessage.success(t("auth.passwordChanged"));
    await router.replace({ name: "login" });
  } catch (err) {
    passwordErrors.value = fieldErrors(err);
  } finally {
    passwordSaving.value = false;
  }
}
</script>

<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : '220px'" class="layout-aside">
      <div class="layout-brand" @click="router.push('/')">
        <img :src="logoUrl" alt="" class="layout-logo" />
        <span v-show="!collapsed" class="layout-brand-text">aicreat</span>
      </div>
      <el-scrollbar class="layout-menu-scroll">
        <el-menu :default-active="activeMenu" :default-openeds="defaultOpeneds" :collapse="collapsed" :collapse-transition="false" router class="layout-menu">
          <template v-for="group in visibleMenuGroups" :key="group.key">
            <el-menu-item v-if="group.key === 'console'" :index="group.items[0].path">
              <el-icon><component :is="GROUP_ICONS[group.key]" /></el-icon>
              <template #title>{{ t(`menu.${group.items[0].key}`) }}</template>
            </el-menu-item>
            <el-sub-menu v-else :index="group.key">
              <template #title>
                <el-icon><component :is="GROUP_ICONS[group.key]" /></el-icon>
                <span>{{ t(`menu.groups.${group.key}`) }}</span>
              </template>
              <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">{{ t(`menu.${item.key}`) }}</el-menu-item>
            </el-sub-menu>
          </template>
        </el-menu>
      </el-scrollbar>
    </el-aside>

    <el-container class="layout-body">
      <el-header class="layout-header" height="56px">
        <div class="layout-header-left">
          <el-button text circle :title="collapsed ? t('common.expand') : t('common.collapse')" @click="toggleCollapse">
            <el-icon :size="18"><Expand v-if="collapsed" /><Fold v-else /></el-icon>
          </el-button>
          <el-breadcrumb separator="/" class="layout-breadcrumb">
            <el-breadcrumb-item v-for="(crumb, idx) in breadcrumbs" :key="idx" :to="crumb.to">{{ crumb.label }}</el-breadcrumb-item>
          </el-breadcrumb>
        </div>
        <div class="layout-header-right">
          <OwnerSelect v-if="showOwnerSelect" width="160px" />
          <ProjectSelect v-if="showProjectSelect" width="180px" />
          <el-tag v-if="auth.zhiqiMode === 'mock'" type="warning" effect="plain" size="small">{{ t("common.mockMode") }}</el-tag>
          <AlertBadge v-if="showAlertBadge" />
          <LangSwitch />
          <ThemeSwitch />
          <el-dropdown trigger="click" @command="onUserCommand">
            <span class="layout-user">
              <span class="layout-user-name">{{ auth.displayName }}</span>
              <span v-if="auth.admin?.group" class="layout-user-group">{{ auth.admin.group.name }}</span>
              <el-tag size="small" :type="isAllScope ? 'primary' : 'info'" effect="plain">{{ scopeLabel }}</el-tag>
              <el-icon><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item disabled>
                  <div class="layout-user-meta">
                    <div>{{ auth.admin?.username }}</div>
                    <div v-if="auth.admin?.group">{{ t("auth.group") }}：{{ auth.admin.group.name }}</div>
                  </div>
                </el-dropdown-item>
                <el-dropdown-item divided command="password" :icon="Lock">{{ t("auth.changePassword") }}</el-dropdown-item>
                <el-dropdown-item command="logout" :icon="SwitchButton">{{ t("auth.logout") }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="layout-main">
        <el-alert v-if="viewingOwner" type="warning" :closable="false" show-icon class="layout-owner-banner">
          <template #title>
            <span>{{ t("scope.viewingOwner", { name: project.ownerName }) }}</span>
            <el-button link type="primary" class="layout-owner-back" @click="backToAllUsers">{{ t("scope.backToAll") }}</el-button>
          </template>
        </el-alert>
        <router-view v-slot="{ Component: page }">
          <component :is="page" :key="project.ownerId" />
        </router-view>
      </el-main>
    </el-container>

    <el-dialog v-model="passwordVisible" :title="t('auth.changePassword')" width="440px" :close-on-click-modal="false" append-to-body>
      <el-form ref="passwordFormRef" :model="passwordForm" :rules="passwordRules" label-width="110px" @submit.prevent="submitPassword">
        <el-form-item :label="t('auth.oldPassword')" prop="old_password" :error="passwordErrors.old_password">
          <el-input v-model="passwordForm.old_password" type="password" show-password autocomplete="current-password" />
        </el-form-item>
        <el-form-item :label="t('auth.newPassword')" prop="new_password" :error="passwordErrors.new_password">
          <el-input v-model="passwordForm.new_password" type="password" show-password autocomplete="new-password" :placeholder="t('auth.passwordPolicy')" />
        </el-form-item>
        <el-form-item :label="t('auth.confirmPassword')" prop="confirm">
          <el-input v-model="passwordForm.confirm" type="password" show-password autocomplete="new-password" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="passwordVisible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :loading="passwordSaving" @click="submitPassword">{{ t("common.confirm") }}</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
  overflow: hidden;
}
.layout-aside {
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--el-border-color-light);
  background: var(--el-bg-color);
  transition: width 0.2s;
  overflow: hidden;
}
.layout-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 18px;
  cursor: pointer;
  border-bottom: 1px solid var(--el-border-color-light);
  flex-shrink: 0;
}
.layout-logo {
  width: 28px;
  height: 28px;
}
.layout-brand-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.layout-menu-scroll {
  flex: 1;
}
.layout-menu {
  border-right: none;
}
.layout-body {
  min-width: 0;
}
.layout-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: var(--el-bg-color);
  padding: 0 16px;
}
.layout-header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.layout-breadcrumb {
  white-space: nowrap;
}
.layout-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
}
.layout-user {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--el-text-color-primary);
  outline: none;
}
.layout-user-name {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.layout-user-group {
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.layout-user-meta {
  line-height: 1.6;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.layout-main {
  background: var(--el-bg-color-page);
  overflow: auto;
}
.layout-owner-banner {
  margin-bottom: 12px;
}
.layout-owner-back {
  margin-left: 12px;
  vertical-align: baseline;
}
</style>
