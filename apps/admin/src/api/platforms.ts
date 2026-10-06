// /admin/platforms（docs/04 §6.16）。本文件先提供项目表单「常用平台」所需的 list，其余（CRUD、testRule、detect）由平台模块补齐。
import type { Platform } from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { get } from "./client";

/** 不分页；`is_active` 筛选；每项含 `link_count` */
export function list(params: { is_active?: boolean } = {}, config: AxiosRequestConfig = {}): Promise<Platform[]> {
  const query: Record<string, unknown> = {};
  if (params.is_active !== undefined) query.is_active = params.is_active ? 1 : 0;
  return get<Platform[]>("/admin/platforms", query, config);
}
