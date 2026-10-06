// 生成类请求的统一结果处理（docs/09 §10.3、§10.8、§12；docs/04 §5.2、§5.3）：
// - 成功：记录可选的 `quota_warning`（黄色横幅）；
// - 429：按 `retry_after` 倒计时禁用生成按钮（`rate:generate:{admin_id}` 按管理员计数，倒计时在各页面共享）；
// - 4291 额度上限、5031 能力不可用 / 全局暂停 / 覆盖模型熔断、4221 模板变量缺失、409 项目归档 / 生成中 / 同类任务、
//   404 模板解析失败、400 校验错误（列表定位字段；`{"model":…}` 为覆盖模型不可用）转为可读提示。
// 生成请求应以 `{ silent: true }` 发出，由本组合式函数统一展示，避免拦截器重复提示。
import { computed, ref } from "vue";
import type {
  CapabilityUnavailableData,
  ConflictData,
  QuotaLimitData,
  QuotaWarning,
  ValidationErrorItem,
} from "@aicreat/shared";
import { t } from "@/i18n";
import { fieldErrors as toFieldErrors, isApiError, validationErrors } from "@/api/client";
import { statusLabel } from "@/components/StatusTag.vue";
import { useAuthStore } from "@/store/auth";
import { formatQuota } from "@/utils/format";

export interface GenerateNoticeInfo {
  type: "error" | "warning" | "info";
  title: string;
  description?: string;
  /** 处置入口（如「AI 网关 → 能力路由」） */
  link?: { to: string; label: string };
  /** 4221 缺失的模板变量 */
  missing?: string[];
}

// ---------- 频控倒计时（模块级共享） ----------
const cooldownUntil = ref(0);
const now = ref(Date.now());
let ticker: ReturnType<typeof setInterval> | null = null;

function startCooldown(seconds: number) {
  const until = Date.now() + Math.max(1, Math.ceil(seconds)) * 1000;
  if (until > cooldownUntil.value) cooldownUntil.value = until;
  now.value = Date.now();
  if (ticker) return;
  ticker = setInterval(() => {
    now.value = Date.now();
    if (now.value >= cooldownUntil.value && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  }, 1000);
}

/** 剩余冷却秒数（0 = 可提交） */
export const generateCooldown = computed(() => Math.max(0, Math.ceil((cooldownUntil.value - now.value) / 1000)));

function obj<T>(data: unknown): Partial<T> {
  return data && typeof data === "object" && !Array.isArray(data) ? (data as Partial<T>) : {};
}

/** 把生成接口的错误转为提示；返回提示与字段错误 */
export function describeGenerateError(err: unknown): { notice: GenerateNoticeInfo | null; fields: Record<string, string> } {
  if (!isApiError(err)) return { notice: { type: "error", title: t("common.requestFailed") }, fields: {} };
  if (err.code === 0 && err.message === "canceled") return { notice: null, fields: {} };
  const auth = useAuthStore();
  const code = err.code;
  const fields: Record<string, string> = {};

  if (code === 429) {
    const retryAfter = Number(obj<{ retry_after: number }>(err.data).retry_after);
    if (Number.isFinite(retryAfter) && retryAfter > 0) startCooldown(retryAfter);
    return {
      notice: { type: "warning", title: t("generation.rateLimited"), description: t("generation.rateLimitedHint") },
      fields,
    };
  }
  if (code === 4291) {
    const data = obj<QuotaLimitData>(err.data);
    const scope = data.scope ?? "daily";
    return {
      notice: {
        type: "error",
        title: t(`generation.quotaLimit.${scope}`),
        description: t("generation.quotaUsage", { used: formatQuota(data.used ?? 0), limit: formatQuota(data.limit ?? 0) }),
      },
      fields,
    };
  }
  if (code === 5031) {
    const data = obj<CapabilityUnavailableData>(err.data);
    const routesLink = auth.hasPermission("ai.routes.view") ? { to: "/ai/routes", label: t("generation.gotoRoutes") } : undefined;
    if (data.hint === "model_override") {
      fields.model = t("generation.overrideBreakerOpen");
      return { notice: { type: "error", title: t("generation.overrideBreakerOpen"), description: t("generation.overrideHint") }, fields };
    }
    if (data.paused_reason) {
      return {
        notice: { type: "error", title: t(`generation.paused.${data.paused_reason}`), description: t("generation.pausedHint"), link: routesLink },
        fields,
      };
    }
    if (data.breaker_open?.length) {
      return {
        notice: {
          type: "error",
          title: t("generation.breakerOpen"),
          description: t("generation.breakerOpenModels", { models: data.breaker_open.join(", ") }),
          link: routesLink,
        },
        fields,
      };
    }
    const models = data.unavailable_models ?? [];
    return {
      notice: {
        type: "error",
        title: t("generation.capabilityUnavailable"),
        description: models.length ? t("generation.unavailableModels", { models: models.join(", ") }) : t("generation.capabilityUnavailableHint"),
        link: routesLink,
      },
      fields,
    };
  }
  if (code === 4221) {
    const missing = Array.isArray(obj<{ missing: string[] }>(err.data).missing) ? (obj<{ missing: string[] }>(err.data).missing as string[]) : [];
    fields.template_id = t("generation.templateVarMissing", { names: missing.join("、") });
    return {
      notice: { type: "error", title: t("generation.templateVarMissing", { names: missing.join("、") }), description: t("generation.templateVarMissingHint"), missing },
      fields,
    };
  }
  if (code === 409) {
    const data = obj<ConflictData>(err.data);
    if (data.current_status === "archived" && !data.reason) {
      return { notice: { type: "error", title: err.message || t("generation.projectArchived") }, fields };
    }
    if (data.existing_id) {
      return { notice: { type: "warning", title: t("generation.taskExists", { id: data.existing_id }) }, fields };
    }
    if (data.current_status) {
      return {
        notice: { type: "error", title: err.message, description: t("generation.currentStatus", { status: statusLabel("content_status", data.current_status) }) },
        fields,
      };
    }
    return { notice: { type: "error", title: err.message }, fields };
  }
  if (code === 404) {
    const kind = obj<{ kind: string }>(err.data).kind;
    if (kind) return { notice: { type: "error", title: t("generation.templateNotResolved", { kind: statusLabel("prompt_kind", kind) }) }, fields };
    return { notice: { type: "error", title: err.message || t("generation.notFound") }, fields };
  }
  if (code === 400) {
    const modelData = obj<{ model: string }>(err.data);
    if (!Array.isArray(err.data) && modelData.model !== undefined) {
      fields.model = t("generation.modelInvalid", { model: String(modelData.model ?? "") });
      return { notice: { type: "error", title: fields.model }, fields };
    }
    const items: ValidationErrorItem[] = validationErrors(err);
    Object.assign(fields, toFieldErrors(err));
    return {
      notice: { type: "error", title: err.message || t("common.validationFailed"), description: items.slice(0, 5).map((i) => i.msg).join("；") || undefined },
      fields,
    };
  }
  if (code === 403) {
    return { notice: { type: "error", title: t("common.forbiddenAction") }, fields };
  }
  const rid = err.requestId ? `（${t("common.requestId")} ${err.requestId}）` : "";
  return { notice: { type: "error", title: `${err.message || t("common.requestFailed")}${rid}` }, fields };
}

/** 每个生成表单一份：提示、额度预警与字段错误 */
export function useGenerateGuard() {
  const notice = ref<GenerateNoticeInfo | null>(null);
  const quotaWarning = ref<QuotaWarning | null>(null);
  const fields = ref<Record<string, string>>({});
  const submitting = ref(false);

  function reset() {
    notice.value = null;
    quotaWarning.value = null;
    fields.value = {};
  }

  /** 执行生成请求：成功返回结果（并记录 quota_warning），失败返回 null 并设置提示 */
  async function run<T extends { quota_warning?: QuotaWarning }>(fn: () => Promise<T>): Promise<T | null> {
    if (generateCooldown.value > 0) return null;
    submitting.value = true;
    notice.value = null;
    fields.value = {};
    try {
      const res = await fn();
      quotaWarning.value = res?.quota_warning ?? null;
      return res;
    } catch (err) {
      const { notice: n, fields: f } = describeGenerateError(err);
      notice.value = n;
      fields.value = f;
      return null;
    } finally {
      submitting.value = false;
    }
  }

  return { notice, quotaWarning, fields, submitting, cooldown: generateCooldown, reset, run };
}
