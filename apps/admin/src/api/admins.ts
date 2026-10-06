// /admin/admins、/admin/admin-operation-logs（docs/07 §6.2、§6.4）
import type { AdminItem, AdminOperationLogItem, OperationAction, Page } from "@aicreat/shared";
import { get, patch, post, put } from "./client";

export interface ListAdminsParams {
  page?: number;
  page_size?: number;
  keyword?: string;
  group_id?: number;
  is_active?: boolean;
}

export interface AdminCreateBody {
  username: string;
  display_name: string | null;
  password: string;
  group_id: number;
  is_active: boolean;
}

export interface AdminUpdateBody {
  display_name?: string | null;
  group_id?: number;
}

export interface ListOperationLogsParams {
  page?: number;
  page_size?: number;
  admin_id?: number;
  module?: string;
  action?: OperationAction;
  target_type?: string;
  /** ISO 8601 UTC */
  start?: string;
  /** ISO 8601 UTC */
  end?: string;
}

function clean<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
}

export function listAdmins(params: ListAdminsParams = {}): Promise<Page<AdminItem>> {
  return get<Page<AdminItem>>("/admin/admins", clean(params));
}

export function getAdmin(id: number): Promise<AdminItem> {
  return get<AdminItem>(`/admin/admins/${id}`);
}

export function createAdmin(body: AdminCreateBody): Promise<AdminItem> {
  return post<AdminItem>("/admin/admins", body);
}

export function updateAdmin(id: number, body: AdminUpdateBody): Promise<AdminItem> {
  return put<AdminItem>(`/admin/admins/${id}`, body);
}

export function setAdminStatus(id: number, is_active: boolean): Promise<AdminItem> {
  return patch<AdminItem>(`/admin/admins/${id}/status`, { is_active });
}

export function resetPassword(id: number, password: string): Promise<null> {
  return post<null>(`/admin/admins/${id}/reset-password`, { password });
}

export function listOperationLogs(params: ListOperationLogsParams = {}): Promise<Page<AdminOperationLogItem>> {
  return get<Page<AdminOperationLogItem>>("/admin/admin-operation-logs", clean(params));
}
