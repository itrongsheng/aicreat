// /admin/projects（docs/04 §6.7、§7.3；docs/09 §4；docs/13 §6.4、§7.2）。
// 路径与参数以 04 为准，不得自行扩展后端不存在的路径。
import type {
  CapabilityRoute,
  OwnerOption,
  Page,
  Project,
  ProjectBody,
  ProjectRouteInput,
  ProjectStatus,
  StatsOverview,
  StatsRange,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get as httpGet, post, put } from "./client";

export interface ListProjectsParams {
  page?: number;
  page_size?: number;
  keyword?: string;
  status?: ProjectStatus;
  /** 总后台按负责人筛选；省略时由 client.ts 按用户视角自动附加，显式 null 表示不附加 */
  owner_id?: number | null;
}

function clean<T extends object>(params: T): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([k, v]) => v !== undefined && v !== "" && (v !== null || k === "owner_id")));
}

/** 分页；只含可见项目，每项附 counts 与 owner 摘要 */
export function list(params: ListProjectsParams = {}, config: AxiosRequestConfig = {}): Promise<Page<Project>> {
  return httpGet<Page<Project>>("/admin/projects", clean(params), config);
}

/** 负责人候选（不分页）：总后台返回全部启用用户与仍负责项目的已禁用用户，普通用户只返回本人 */
export function ownerOptions(config: AxiosRequestConfig = {}): Promise<OwnerOption[]> {
  return httpGet<OwnerOption[]>("/admin/projects/owner-options", undefined, config);
}

/** 详情 + `routes[]`（项目级 capability_routes 覆盖行）；不可见返回 404 */
export function get(id: number, config: AxiosRequestConfig = {}): Promise<Project> {
  return httpGet<Project>(`/admin/projects/${id}`, undefined, config);
}

/** 新建：`name`/`slug` 在同一负责人下唯一（409 `existing_id`）；`owner_id` 越权 400 `owner_forbidden` / `owner_unavailable` */
export function create(body: ProjectBody, config: AxiosRequestConfig = {}): Promise<Project> {
  return post<Project>("/admin/projects", body, config);
}

/** 编辑；`owner_id` 变化即转移负责人（仅总后台）；`default_templates` 校验失败 400 `loc=["body","default_templates","<kind>"]` */
export function update(id: number, body: ProjectBody, config: AxiosRequestConfig = {}): Promise<Project> {
  return put<Project>(`/admin/projects/${id}`, body, config);
}

/** 删除：仅 `archived` 且无下游数据，否则 409 `reason=in_use` */
export function remove(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/projects/${id}`, undefined, config);
}

/** `active → archived` */
export function archive(id: number, config: AxiosRequestConfig = {}): Promise<Project> {
  return post<Project>(`/admin/projects/${id}/archive`, undefined, config);
}

/** `archived → active` */
export function unarchive(id: number, config: AxiosRequestConfig = {}): Promise<Project> {
  return post<Project>(`/admin/projects/${id}/unarchive`, undefined, config);
}

/** 项目 KPI，结构同 `GET /admin/stats/overview?project_id={id}`（04 §7.14） */
export function overview(id: number, params: { range?: StatsRange } = {}, config: AxiosRequestConfig = {}): Promise<StatsOverview> {
  return httpGet<StatsOverview>(`/admin/projects/${id}/overview`, clean(params), config);
}

/**
 * 项目级默认模型：对 `capability_routes(project_id=id)` upsert，未出现的能力删除覆盖行、空数组删除全部；
 * 主 / 备模型须存在、模态匹配且可用，否则 400（`loc=["body","routes",i,"primary_model"]`，`type=invalid_model`/`model_unavailable`）。
 * 返回更新后的 `routes[]`。
 */
export function saveRoutes(id: number, routes: ProjectRouteInput[], config: AxiosRequestConfig = {}): Promise<CapabilityRoute[]> {
  return put<CapabilityRoute[]>(`/admin/projects/${id}/routes`, { routes }, config);
}
