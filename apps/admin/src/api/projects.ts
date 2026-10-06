// /admin/projects（docs/04 §6.7、docs/13 §6.4）。本文件先提供顶栏选择器所需的 list / ownerOptions，CRUD 等由项目模块补齐。
import type { OwnerOption, Page, Project, ProjectStatus } from "@aicreat/shared";
import { get } from "./client";

export interface ListProjectsParams {
  page?: number;
  page_size?: number;
  keyword?: string;
  status?: ProjectStatus;
  /** 总后台按负责人筛选；省略时由 client.ts 按用户视角自动附加 */
  owner_id?: number | null;
}

function clean<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([k, v]) => v !== undefined && v !== "" && (v !== null || k === "owner_id")));
}

/** 分页；只含可见项目，每项附 counts 与 owner 摘要 */
export function list(params: ListProjectsParams = {}): Promise<Page<Project>> {
  return get<Page<Project>>("/admin/projects", clean(params));
}

/** 负责人候选（不分页）：总后台返回全部启用用户与仍负责项目的已禁用用户，普通用户只返回本人 */
export function ownerOptions(): Promise<OwnerOption[]> {
  return get<OwnerOption[]>("/admin/projects/owner-options");
}
