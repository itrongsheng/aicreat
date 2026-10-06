// 登录态与权限（docs/07 §9.1、docs/13 §12.1）
import { defineStore } from "pinia";
import type { AdminProfile, ZhiqiMode } from "@aicreat/shared";
import { authApi, type LoginBody } from "@/api/auth";
import { resetSessionNotice } from "@/api/client";
import { useProjectStore } from "@/store/project";

const TOKEN_KEY = "admin_token";
const PROFILE_KEY = "admin_profile";

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function readProfile(): AdminProfile | null {
  try {
    return JSON.parse(localStorage.getItem(PROFILE_KEY) || "null") as AdminProfile | null;
  } catch {
    return null; // 本地缓存损坏时按未命中处理
  }
}

const cachedProfile = readProfile();

export const useAuthStore = defineStore("adminAuth", {
  state: () => ({
    token: readToken(),
    admin: cachedProfile,
    zhiqiMode: (cachedProfile?.zhiqi_mode ?? "mock") as ZhiqiMode,
    hydrated: false, // 本次会话是否已用 /auth/me 校验过
    lastRefreshAt: 0,
  }),
  getters: {
    permissions: (s): string[] => s.admin?.permissions ?? [],
    hasPermission(): (code: string) => boolean {
      const set = new Set(this.permissions);
      return (code: string) => set.has(code);
    },
    isSuperAdmin: (s): boolean => s.admin?.group?.code === "super_admin",
    /** 总后台视角（数据范围 all），见 13 §12 */
    isAllScope: (s): boolean => s.admin?.data_scope === "all",
    displayName: (s): string => s.admin?.display_name || s.admin?.username || "",
  },
  actions: {
    setAdmin(admin: AdminProfile) {
      this.admin = admin;
      this.hydrated = true;
      if (admin.zhiqi_mode) this.zhiqiMode = admin.zhiqi_mode;
      try {
        localStorage.setItem(PROFILE_KEY, JSON.stringify(admin));
      } catch {
        /* ignore */
      }
    },
    async login(body: LoginBody) {
      const data = await authApi.login(body);
      useProjectStore().resetScope(); // 清空上一个账号的用户视角与当前项目（13 §12.2）
      this.token = data.token;
      try {
        localStorage.setItem(TOKEN_KEY, data.token);
      } catch {
        /* ignore */
      }
      this.setAdmin(data.admin);
      this.lastRefreshAt = Date.now();
      resetSessionNotice();
      // 登录响应不含 zhiqi_mode：后台补取一次 /auth/me（失败不影响登录）
      this.fetchMe().catch(() => undefined);
    },
    async fetchMe() {
      const me = await authApi.me(); // 失败（401）由拦截器清登录态
      this.setAdmin(me);
      this.lastRefreshAt = Date.now();
    },
    /** 收到带 data.permission 的 403 后节流刷新（30s 内最多一次） */
    async refreshPermissions() {
      if (Date.now() - this.lastRefreshAt > 30_000) await this.fetchMe().catch(() => undefined);
    },
    logout(callApi = true) {
      if (callApi && this.token) authApi.logout(this.token).catch(() => undefined); // 先取令牌再清空，见 api/auth.ts
      this.token = null;
      this.admin = null;
      this.hydrated = false;
      useProjectStore().resetScope();
      try {
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(PROFILE_KEY);
      } catch {
        /* ignore */
      }
    },
  },
});
