// /admin/admin-groups、/admin/admin-permissions（docs/07 §6.3）
import type { AdminGroupItem, AdminPermissionItem, DataScope, PermissionTreeModule } from "@aicreat/shared";
import { del, get, post, put } from "./client";

export interface ListGroupsParams {
  is_active?: boolean;
}

export interface GroupCreateBody {
  name: string;
  description: string | null;
  is_active: boolean;
  data_scope?: DataScope;
}

export interface GroupUpdateBody {
  name?: string;
  description?: string | null;
  is_active?: boolean;
  data_scope?: DataScope;
}

/** 不分页，data 为数组 */
export function listGroups(params: ListGroupsParams = {}): Promise<AdminGroupItem[]> {
  const query = params.is_active === undefined ? {} : { is_active: params.is_active };
  return get<AdminGroupItem[]>("/admin/admin-groups", query);
}

/** 详情含 permission_codes[] */
export function getGroup(id: number): Promise<AdminGroupItem> {
  return get<AdminGroupItem>(`/admin/admin-groups/${id}`);
}

export function createGroup(body: GroupCreateBody): Promise<AdminGroupItem> {
  return post<AdminGroupItem>("/admin/admin-groups", body);
}

export function updateGroup(id: number, body: GroupUpdateBody): Promise<AdminGroupItem> {
  return put<AdminGroupItem>(`/admin/admin-groups/${id}`, body);
}

export function deleteGroup(id: number): Promise<null> {
  return del<null>(`/admin/admin-groups/${id}`);
}

/** 覆盖保存；响应为补齐依赖后的组详情（含 permission_codes） */
export function saveGroupPermissions(id: number, codes: string[]): Promise<AdminGroupItem> {
  return put<AdminGroupItem>(`/admin/admin-groups/${id}/permissions`, { permission_codes: codes });
}

export function listPermissions(): Promise<AdminPermissionItem[]> {
  return get<AdminPermissionItem[]>("/admin/admin-permissions");
}

export function permissionTree(): Promise<PermissionTreeModule[]> {
  return get<PermissionTreeModule[]>("/admin/admin-permissions/tree");
}
