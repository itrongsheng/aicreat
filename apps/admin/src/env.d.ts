/// <reference types="vite/client" />

import "vue-router";

declare module "vue-router" {
  interface RouteMeta {
    /** 需要登录（挂在 Layout 根路由上，子路由继承） */
    requiresAuth?: boolean;
    /** 页面绑定的 *.view 权限码（与 Layout.vue 菜单项一致） */
    permission?: string;
    /** 页面标题 i18n 键 */
    title?: string;
    /** 详情 / 编辑页高亮的菜单路径 */
    activeMenu?: string;
  }
}

declare module "axios" {
  interface AxiosRequestConfig {
    /** 为 true 时拦截器不弹出错误提示（401 / 403 的登录态与权限处理照常执行） */
    silent?: boolean;
  }
}

export {};
