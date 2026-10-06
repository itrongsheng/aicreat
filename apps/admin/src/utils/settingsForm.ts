// 系统配置专用表单的公共工具（views/settings/*Form.vue）：深拷贝、按路径读写、以默认值补齐缺失字段、
// 后端 400 校验错误（loc 形如 ["body","value","link_check","max_redirects"]）按字段路径归位。
import type { ValidationErrorItem } from "@aicreat/shared";

export type Json = Record<string, unknown>;
export type Path = (string | number)[];

export function isObject(value: unknown): value is Json {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value ?? null)) as T;
}

/** 以默认值递归补齐缺失的键（不覆盖已有值、不删除未知键；数组整体视为值，不逐项补齐） */
export function fillDefaults(value: unknown, defaults: Json): Json {
  const out = clone(isObject(value) ? value : {});
  for (const [k, v] of Object.entries(defaults)) {
    if (!(k in out)) out[k] = clone(v);
    else if (isObject(v) && isObject(out[k])) out[k] = fillDefaults(out[k], v);
  }
  return out;
}

export function getAt(root: unknown, path: Path): unknown {
  let cur: unknown = root;
  for (const key of path) {
    if (Array.isArray(cur) && typeof key === "number") cur = cur[key];
    else if (isObject(cur)) cur = cur[String(key)];
    else return undefined;
  }
  return cur;
}

export function setAt(root: Json, path: Path, value: unknown): void {
  let cur: unknown = root;
  for (let i = 0; i < path.length - 1; i += 1) {
    const key = path[i];
    const nextKey = path[i + 1];
    const container = cur as Record<string | number, unknown>;
    if (!isObject(container[key]) && !Array.isArray(container[key])) container[key] = typeof nextKey === "number" ? [] : {};
    cur = container[key];
  }
  (cur as Record<string | number, unknown>)[path[path.length - 1]] = value;
}

/** loc 去掉 body / value 前缀后的相对路径（字符串化） */
export function relLoc(item: ValidationErrorItem): string[] {
  const loc = item.loc.map(String);
  let i = 0;
  if (loc[i] === "body") i += 1;
  if (loc[i] === "value") i += 1;
  return loc.slice(i);
}

export function pathKey(path: Path): string {
  return path.map(String).join(".");
}

/** loc 是否落在 path 上或其下（path 为 loc 的前缀） */
export function locUnder(item: ValidationErrorItem, path: Path): boolean {
  const rel = relLoc(item);
  return path.length <= rel.length && path.every((p, idx) => rel[idx] === String(p));
}

/** loc 恰好等于 path（模型级校验错误，如引擎对象本身的 model_required） */
export function locExact(item: ValidationErrorItem, path: Path): boolean {
  const rel = relLoc(item);
  return rel.length === path.length && path.every((p, idx) => rel[idx] === String(p));
}

/** 取落在 path 上或其下的第一条错误消息 */
export function errorAt(errors: ValidationErrorItem[], path: Path): string | undefined {
  return errors.find((e) => locUnder(e, path))?.msg;
}

/** 解析整数数组输入（el-select allow-create 产生字符串）：去重、过滤范围外的值、升序 */
export function toSortedInts(values: unknown[], min: number, max: number): number[] {
  const set = new Set<number>();
  for (const v of values) {
    const n = Number(String(v).trim());
    if (Number.isInteger(n) && n >= min && n <= max) set.add(n);
  }
  return [...set].sort((a, b) => a - b);
}
