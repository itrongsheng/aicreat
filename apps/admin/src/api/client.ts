// axios 实例与统一请求封装（docs/02 §3.4、docs/07 §9.6、docs/13 §12.2）。
// - 请求：Authorization: Bearer、lang（查询参数 + Accept-Language）、总后台用户视角 owner_id（GET 与两个检测 run 接口）
// - 响应：解包 {code, message, data}；code != 0 抛 ApiError(code, message, data)
// - 401 清登录态并跳转登录页（登录请求本身除外）；403 按 data.permission 区分；429 按 retry_after 提示
import axios, { AxiosError, type AxiosRequestConfig, type AxiosResponse, type InternalAxiosRequestConfig } from "axios";
import { ElMessage } from "element-plus";
import { API_PREFIX, type ApiResponse, type ValidationErrorItem } from "@aicreat/shared";
import { getLocale, t } from "@/i18n";
import { useAuthStore } from "@/store/auth";
import { useProjectStore } from "@/store/project";
import { router } from "@/router";

export class ApiError<D = unknown> extends Error {
  readonly code: number;
  readonly data: D;
  readonly status: number;
  readonly requestId: string | null;

  constructor(code: number, message: string, data: D, status = 0, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.data = data;
    this.status = status;
    this.requestId = requestId;
  }
}

export function isApiError(err: unknown): err is ApiError {
  return err instanceof ApiError;
}

/** 400 校验错误列表（04 §5.1）；非该结构时返回空数组 */
export function validationErrors(err: unknown): ValidationErrorItem[] {
  if (!isApiError(err) || !Array.isArray(err.data)) return [];
  return (err.data as unknown[]).filter(
    (item): item is ValidationErrorItem => !!item && typeof item === "object" && "loc" in item && "msg" in item,
  );
}

/** 把校验错误按字段名（`loc` 中 body / query / path 之后的第一段）聚合，供 el-form-item 的 :error 使用 */
export function fieldErrors(err: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of validationErrors(err)) {
    const field = item.loc.length > 1 ? String(item.loc[1]) : String(item.loc[0] ?? "");
    if (field && !out[field]) out[field] = item.msg;
  }
  return out;
}

const LOGIN_URL = "/admin/auth/login";
/** 登出请求的 401（令牌已失效）不再走「登录已失效」流程：登录态本就在清除 */
const LOGOUT_URL = "/admin/auth/logout";
/** 用户视角下除 GET 外也附加 owner_id 的写接口（13 §12.2） */
const OWNER_SCOPED_POSTS = new Set(["/admin/monitoring/link-checks/run", "/admin/monitoring/index-checks/run"]);

export const http = axios.create({ baseURL: API_PREFIX, timeout: 30_000 });

function pathOf(config: AxiosRequestConfig): string {
  const url = config.url ?? "";
  const noQuery = url.split("?")[0];
  return noQuery.startsWith(API_PREFIX) ? noQuery.slice(API_PREFIX.length) : noQuery;
}

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  // 调用方显式携带的 Authorization（如登出时传入清空前的令牌）不覆盖
  if (auth.token && !config.headers.has("Authorization")) config.headers.set("Authorization", `Bearer ${auth.token}`);
  const locale = getLocale();
  config.headers.set("Accept-Language", locale);
  const params: Record<string, unknown> = { ...(config.params ?? {}) };
  if (!("lang" in params)) params.lang = locale;

  const method = (config.method ?? "get").toLowerCase();
  const project = useProjectStore();
  if (auth.isAllScope && project.ownerId > 0 && (method === "get" || (method === "post" && OWNER_SCOPED_POSTS.has(pathOf(config))))) {
    // 请求已显式携带 owner_id（params 中含该键，含显式 null 表示不附加；或 URL 查询串已带）时不覆盖
    const inUrl = /[?&]owner_id=/.test(config.url ?? "");
    if (!inUrl && !Object.prototype.hasOwnProperty.call(params, "owner_id")) params.owner_id = project.ownerId;
  }
  config.params = params;
  return config;
});

let sessionExpiredNotified = false;

/** 登录成功后调用：重新允许「登录已失效」提示 */
export function resetSessionNotice(): void {
  sessionExpiredNotified = false;
}

function notify(config: AxiosRequestConfig | undefined, message: string): void {
  if (config?.silent) return;
  ElMessage.error({ message, grouping: true });
}

async function readErrorBody(response: AxiosResponse): Promise<Partial<ApiResponse> | null> {
  const raw = response.data as unknown;
  if (raw instanceof Blob) {
    try {
      return JSON.parse(await raw.text()) as Partial<ApiResponse>;
    } catch {
      return null;
    }
  }
  if (raw && typeof raw === "object") return raw as Partial<ApiResponse>;
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw) as Partial<ApiResponse>;
    } catch {
      return null;
    }
  }
  return null;
}

function describeValidation(message: string, data: unknown): string {
  if (!Array.isArray(data)) return message;
  const msgs = (data as ValidationErrorItem[])
    .filter((item) => item && typeof item.msg === "string")
    .slice(0, 3)
    .map((item) => item.msg);
  return msgs.length ? `${message}：${msgs.join("；")}` : message;
}

async function handleUnauthorized(): Promise<void> {
  const auth = useAuthStore();
  auth.logout(false);
  if (!sessionExpiredNotified) {
    sessionExpiredNotified = true;
    ElMessage.warning({ message: t("common.sessionExpired"), grouping: true });
  }
  const current = router.currentRoute.value;
  // 首次导航（START_LOCATION，matched 为空）期间由路由守卫负责跳转，避免重复导航
  if (current.matched.length > 0 && current.name !== "login") {
    await router.replace({ name: "login", query: current.fullPath && current.fullPath !== "/" ? { redirect: current.fullPath } : {} });
  }
}

async function handleForbidden(config: AxiosRequestConfig | undefined, message: string, data: unknown): Promise<void> {
  const permission = data && typeof data === "object" ? (data as { permission?: unknown }).permission : undefined;
  if (typeof permission === "string" && permission) {
    notify(config, t("common.forbiddenAction"));
    const auth = useAuthStore();
    await auth.refreshPermissions();
    const routePermission = router.currentRoute.value.meta.permission;
    if (routePermission && !auth.hasPermission(routePermission)) {
      await router.replace({ path: "/403", query: { from: router.currentRoute.value.fullPath } });
    }
    return;
  }
  // 安全规则拒绝（data = null）：直接展示后端 message，不刷新权限、不跳转
  notify(config, message || t("common.forbiddenAction"));
}

async function toApiError(error: AxiosError): Promise<ApiError> {
  const config = error.config;
  const response = error.response;
  if (!response) {
    if (axios.isCancel(error)) return new ApiError(0, "canceled", null);
    notify(config, t("common.networkError"));
    return new ApiError(0, t("common.networkError"), null);
  }
  const status = response.status;
  const body = await readErrorBody(response);
  const code = typeof body?.code === "number" ? body.code : status;
  const message = (typeof body?.message === "string" && body.message) || t("common.requestFailed");
  const data = body?.data ?? null;
  const requestId = (response.headers?.["x-request-id"] as string | undefined) ?? null;
  const apiError = new ApiError(code, message, data, status, requestId);
  const path = pathOf(config ?? {});
  const isLogin = path === LOGIN_URL;

  if (status === 401) {
    if (!isLogin && path !== LOGOUT_URL) await handleUnauthorized();
    return apiError;
  }
  if (status === 403) {
    if (!isLogin) await handleForbidden(config, message, data);
    return apiError;
  }
  if (isLogin) return apiError; // 登录页自行展示 401 / 403 / 429
  if (status === 429) {
    const retryAfter = data && typeof data === "object" ? Number((data as { retry_after?: unknown }).retry_after) : NaN;
    notify(config, Number.isFinite(retryAfter) && retryAfter > 0 ? t("common.rateLimited", { seconds: Math.ceil(retryAfter) }) : message);
    return apiError;
  }
  if (status >= 500) {
    const base = body?.message ? message : t("common.serverError");
    notify(config, requestId ? `${base}（${t("common.requestId")} ${requestId}）` : base);
    return apiError;
  }
  notify(config, code === 400 ? describeValidation(message, data) : message);
  return apiError;
}

http.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => Promise.reject(await toApiError(error)),
);

/** 统一请求：返回解包后的 data；业务码非 0 抛 ApiError */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await http.request<ApiResponse<T>>(config);
  const body = response.data;
  if (!body || typeof body !== "object" || typeof body.code !== "number") {
    return body as unknown as T;
  }
  if (body.code !== 0) {
    const requestId = (response.headers?.["x-request-id"] as string | undefined) ?? null;
    notify(config, body.message || t("common.requestFailed"));
    throw new ApiError(body.code, body.message, body.data, response.status, requestId);
  }
  return body.data;
}

type Params = Record<string, unknown>;

export function get<T>(url: string, params?: Params, config: AxiosRequestConfig = {}): Promise<T> {
  return request<T>({ ...config, method: "get", url, params });
}

export function post<T>(url: string, data?: unknown, config: AxiosRequestConfig = {}): Promise<T> {
  return request<T>({ ...config, method: "post", url, data });
}

export function put<T>(url: string, data?: unknown, config: AxiosRequestConfig = {}): Promise<T> {
  return request<T>({ ...config, method: "put", url, data });
}

export function patch<T>(url: string, data?: unknown, config: AxiosRequestConfig = {}): Promise<T> {
  return request<T>({ ...config, method: "patch", url, data });
}

export function del<T = null>(url: string, params?: Params, config: AxiosRequestConfig = {}): Promise<T> {
  return request<T>({ ...config, method: "delete", url, params });
}

export interface DownloadResult {
  blob: Blob;
  filename: string | null;
}

function filenameFromDisposition(header: string | undefined): string | null {
  if (!header) return null;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : null;
}

/** 文件下载（CSV 导出、内容导出）：responseType blob；失败时后端仍返回 JSON 外壳，由拦截器解析 */
export async function requestBlob(config: AxiosRequestConfig): Promise<DownloadResult> {
  const response = await http.request<Blob>({ ...config, responseType: "blob" });
  const blob = response.data;
  if (blob.type.includes("application/json")) {
    // 可能是业务外壳（失败）也可能是 JSON 格式的导出文件本身：只有外壳且 code != 0 时按错误处理
    let body: Partial<ApiResponse> | null = null;
    try {
      body = JSON.parse(await blob.text()) as Partial<ApiResponse>;
    } catch {
      body = null;
    }
    if (body && typeof body.code === "number" && typeof body.message === "string" && body.code !== 0) {
      notify(config, body.message);
      throw new ApiError(body.code, body.message, body.data ?? null, response.status);
    }
  }
  const disposition = response.headers?.["content-disposition"] as string | undefined;
  return { blob, filename: filenameFromDisposition(disposition) };
}
