// 路由表（docs/02 §3.3）+ 登录守卫 + meta.permission 权限守卫 + 403（docs/07 §9.4）
import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import Layout, { menuGroups } from "@/layouts/Layout.vue"; // menuGroups 由 Layout.vue 的普通 <script lang="ts"> 块导出
import { t } from "@/i18n";
import { useAuthStore } from "@/store/auth";

/** 按 Layout 菜单配置顺序取第一个有权限的菜单路径；没有任何菜单权限时为 /403 */
export function resolveHomePath(): string {
  const auth = useAuthStore();
  const item = menuGroups.flatMap((g) => g.items).find((m) => auth.hasPermission(m.permission));
  return item?.path ?? "/403";
}

const routes: RouteRecordRaw[] = [
  { path: "/login", name: "login", component: () => import("@/views/Login.vue"), meta: { title: "menu.login" } },
  {
    path: "/",
    component: Layout,
    meta: { requiresAuth: true },
    children: [
      { path: "", name: "home", redirect: () => resolveHomePath() },
      // 控制台
      { path: "dashboard", name: "dashboard", component: () => import("@/views/Dashboard.vue"), meta: { permission: "dashboard.view", title: "menu.dashboard" } },
      // 内容生产
      { path: "projects", name: "projects", component: () => import("@/views/projects/Index.vue"), meta: { permission: "content.projects.view", title: "menu.projects" } },
      {
        path: "projects/:id",
        name: "project-detail",
        component: () => import("@/views/projects/Detail.vue"),
        meta: { permission: "content.projects.view", title: "menu.projectDetail", activeMenu: "/projects" },
      },
      {
        path: "prompt-templates",
        name: "prompt-templates",
        component: () => import("@/views/prompt-templates/Index.vue"),
        meta: { permission: "content.prompt_templates.view", title: "menu.promptTemplates" },
      },
      {
        path: "prompt-templates/new",
        name: "prompt-template-new",
        component: () => import("@/views/prompt-templates/Editor.vue"),
        meta: { permission: "content.prompt_templates.view", title: "menu.promptTemplateNew", activeMenu: "/prompt-templates" },
      },
      {
        path: "prompt-templates/:id",
        name: "prompt-template-edit",
        component: () => import("@/views/prompt-templates/Editor.vue"),
        meta: { permission: "content.prompt_templates.view", title: "menu.promptTemplateEdit", activeMenu: "/prompt-templates" },
      },
      { path: "keywords", name: "keywords", component: () => import("@/views/keywords/Index.vue"), meta: { permission: "content.keywords.view", title: "menu.keywords" } },
      { path: "titles", name: "titles", component: () => import("@/views/titles/Index.vue"), meta: { permission: "content.titles.view", title: "menu.titles" } },
      { path: "contents", name: "contents", component: () => import("@/views/contents/Index.vue"), meta: { permission: "content.contents.view", title: "menu.contents" } },
      {
        path: "contents/new",
        name: "content-new",
        component: () => import("@/views/contents/Editor.vue"),
        meta: { permission: "content.contents.view", title: "menu.contentNew", activeMenu: "/contents" },
      },
      {
        path: "contents/:id",
        name: "content-edit",
        component: () => import("@/views/contents/Editor.vue"),
        meta: { permission: "content.contents.view", title: "menu.contentEdit", activeMenu: "/contents" },
      },
      {
        path: "generation-batches",
        name: "generation-batches",
        component: () => import("@/views/generation-batches/Index.vue"),
        meta: { permission: "content.batches.view", title: "menu.generationBatches" },
      },
      // 媒体
      { path: "media/assets", name: "media-assets", component: () => import("@/views/media/Assets.vue"), meta: { permission: "media.assets.view", title: "menu.mediaAssets" } },
      { path: "media/images", name: "media-images", component: () => import("@/views/media/ImageGenerate.vue"), meta: { permission: "media.images.view", title: "menu.mediaImages" } },
      { path: "media/videos", name: "media-videos", component: () => import("@/views/media/VideoGenerate.vue"), meta: { permission: "media.videos.view", title: "menu.mediaVideos" } },
      // AI 网关
      { path: "ai/models", name: "ai-models", component: () => import("@/views/ai/Models.vue"), meta: { permission: "ai.models.view", title: "menu.aiModels" } },
      { path: "ai/routes", name: "ai-routes", component: () => import("@/views/ai/Routes.vue"), meta: { permission: "ai.routes.view", title: "menu.aiRoutes" } },
      { path: "ai/tasks", name: "ai-tasks", component: () => import("@/views/ai/Tasks.vue"), meta: { permission: "ai.tasks.view", title: "menu.aiTasks" } },
      { path: "ai/usage", name: "ai-usage", component: () => import("@/views/ai/Usage.vue"), meta: { permission: "ai.usage.view", title: "menu.aiUsage" } },
      // 发布与监控
      { path: "platforms", name: "platforms", component: () => import("@/views/platforms/Index.vue"), meta: { permission: "publish.platforms.view", title: "menu.platforms" } },
      { path: "links", name: "links", component: () => import("@/views/links/Index.vue"), meta: { permission: "publish.links.view", title: "menu.links" } },
      {
        path: "links/:id",
        name: "link-detail",
        component: () => import("@/views/links/Detail.vue"),
        meta: { permission: "publish.links.view", title: "menu.linkDetail", activeMenu: "/links" },
      },
      {
        path: "monitoring/link-checks",
        name: "monitoring-link-checks",
        component: () => import("@/views/monitoring/LinkChecks.vue"),
        meta: { permission: "monitoring.link_checks.view", title: "menu.linkChecks" },
      },
      {
        path: "monitoring/index-checks",
        name: "monitoring-index-checks",
        component: () => import("@/views/monitoring/IndexChecks.vue"),
        meta: { permission: "monitoring.index_checks.view", title: "menu.indexChecks" },
      },
      { path: "alerts", name: "alerts", component: () => import("@/views/alerts/Index.vue"), meta: { permission: "monitoring.alerts.view", title: "menu.alerts" } },
      // 报表
      { path: "stats/reports", name: "stats-reports", component: () => import("@/views/stats/Reports.vue"), meta: { permission: "stats.reports.view", title: "menu.statsReports" } },
      // 系统
      { path: "settings", name: "settings", component: () => import("@/views/settings/Index.vue"), meta: { permission: "system.settings.view", title: "menu.settings" } },
      { path: "admins", name: "admins", component: () => import("@/views/admins/Index.vue"), meta: { permission: "security.admins.view", title: "menu.admins" } },
      { path: "admin-groups", name: "admin-groups", component: () => import("@/views/admin-groups/Index.vue"), meta: { permission: "security.groups.view", title: "menu.adminGroups" } },
      {
        path: "admin-operation-logs",
        name: "admin-operation-logs",
        component: () => import("@/views/admin-operation-logs/Index.vue"),
        meta: { permission: "security.audit.view", title: "menu.operationLogs" },
      },
      { path: "403", name: "forbidden", component: () => import("@/views/Forbidden.vue"), meta: { title: "menu.forbidden" } },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

// History 模式；Nginx 对 /admin/ 以 try_files 回退 index.html（docs/05）
export const router = createRouter({
  history: createWebHistory("/admin/"),
  routes,
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  const requiresAuth = to.matched.some((r) => r.meta.requiresAuth);
  // redirect 记录原始地址：`/` 的 redirect 在未登录时会求值为 /403，登录后应回到 `/` 重新计算首页
  const loginRedirect = { name: "login", query: { redirect: (to.redirectedFrom ?? to).fullPath } };
  if (!auth.token) return requiresAuth ? loginRedirect : true;
  if (!auth.hydrated) {
    // 有 token 但本次会话尚未用 /auth/me 校验（刷新页面、新开标签）
    try {
      await auth.fetchMe();
    } catch {
      auth.logout(false);
      return requiresAuth ? loginRedirect : true;
    }
  }
  if (to.name === "login") return resolveHomePath(); // 已登录访问登录页：按最新权限落到首页
  const permission = to.meta.permission;
  // `/`（及兜底路由）的 redirect 在守卫之前求值：若当时权限尚未经 /auth/me 校验（无本地缓存或缓存过期），
  // 落点可能是 /403 或已失去权限的菜单，此处按最新权限重新计算首页落点
  if (to.redirectedFrom && (to.path === "/403" || (permission && !auth.hasPermission(permission)))) {
    const home = resolveHomePath();
    if (home !== to.path) return home;
  }
  if (permission && !auth.hasPermission(permission)) return { path: "/403", query: { from: to.fullPath } };
  return true;
});

router.afterEach((to) => {
  const key = [...to.matched].reverse().find((r) => r.meta.title)?.meta.title;
  const app = t("common.appName");
  document.title = key ? `${t(key)} - ${app}` : app;
});

export default router;
