// /admin/auth（docs/07 §6.1、docs/04 §6.1）
import type { AdminProfile, Locale, LoginResult, SiteInfo } from "@aicreat/shared";
import { get, post } from "./client";

export interface LoginBody {
  username: string;
  password: string;
}

export interface ChangePasswordBody {
  old_password: string;
  new_password: string;
}

/** 登录：错误由登录页自行展示（silent），401 不触发「登录已失效」流程 */
export function login(body: LoginBody): Promise<LoginResult> {
  return post<LoginResult>("/admin/auth/login", body, { silent: true });
}

export function me(): Promise<AdminProfile> {
  return get<AdminProfile>("/admin/auth/me");
}

export function logout(): Promise<null> {
  return post<null>("/admin/auth/logout", undefined, { silent: true });
}

export function changePassword(body: ChangePasswordBody): Promise<null> {
  return post<null>("/admin/auth/change-password", body);
}

/** 公开：登录前渲染站点信息 */
export function siteInfo(locale: Locale): Promise<SiteInfo> {
  return get<SiteInfo>("/admin/auth/site-info", { locale }, { silent: true });
}
