// 素材操作的统一流程（docs/10 §4.10「人工入口」、§6.4、§7.1、§7.3；docs/04 §5.2、§5.3、§6.13）：
// - 重试：确认框按资产状态区分「先复查上游任务（不重复计费）」/「重新提交并重新计费」，`failed(transfer_failed)` 额外建议优先「转存」；
//   409 `current_status`、5021（复查旧上游任务失败，资产状态不变）、4291（重新提交分支额度不足）、5031 / 429 统一提示；
// - 转存：`failed(transfer_failed/timeout)` 且有上游 URL；
// - 取消：`POST /admin/ai/tasks/{id}/cancel`（仅本地放弃，不调用上游取消、费用不退）；根任务 `running` → 409「提交中，请稍后再试」；
// - 删除：确认框显示引用计数（详情实时计算）与「正文中的引用将失效」；409 `reason=in_use` / `current_status`；
// - 插入标记：markdown `![alt](url)`、html `<img src alt>`、视频两种格式均 `<video src controls></video>`（§4.9）。
import { h } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type {
  ConflictData,
  ContentFormat,
  MediaAsset,
  MediaRetryResult,
  MediaTransferResult,
  PublicUrlRequiredData,
  UpstreamErrorData,
} from "@aicreat/shared";
import * as aiApi from "@/api/ai";
import * as mediaApi from "@/api/media";
import { isApiError, validationErrors } from "@/api/client";
import { statusLabel } from "@/components/StatusTag.vue";
import { describeGenerateError, type GenerateNoticeInfo } from "@/composables/useGenerateGuard";
import { t } from "@/i18n";

function obj<T>(data: unknown): Partial<T> {
  return data && typeof data === "object" && !Array.isArray(data) ? (data as Partial<T>) : {};
}

// ---------- 可用性判断 ----------

export function canRetryAsset(a: MediaAsset): boolean {
  return a.status === "failed" || a.status === "expired";
}

export function canTransferAsset(a: MediaAsset): boolean {
  return a.status === "failed" && (a.error_category === "transfer_failed" || a.error_category === "timeout") && !!a.upstream_url;
}

export function canCancelAsset(a: MediaAsset): boolean {
  return !!a.ai_task_id && (a.status === "pending" || a.status === "submitted" || a.status === "generating");
}

export function canDeleteAsset(a: MediaAsset): boolean {
  return a.status === "ready" || a.status === "failed" || a.status === "expired";
}

/** 重试会先同步复查旧上游任务（docs/10 §4.10）：有上游任务 ID 且资产 `expired` 或 `failed(timeout)` */
export function retryChecksUpstream(a: MediaAsset): boolean {
  return !!a.upstream_task_id && (a.status === "expired" || (a.status === "failed" && a.error_category === "timeout"));
}

// ---------- 插入标记 ----------

function escapeAttr(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** `alt` 默认取资产 `prompt` 前 50 字符，无则取文章标题（docs/10 §4.9） */
export function assetAlt(a: MediaAsset, fallbackTitle = ""): string {
  const prompt = (a.prompt ?? "").replace(/[\r\n[\]]+/g, " ").trim().slice(0, 50).trim();
  return prompt || fallbackTitle.replace(/[\r\n[\]]+/g, " ").trim();
}

export function assetMarkup(a: MediaAsset, format: ContentFormat, fallbackTitle = ""): string {
  const url = a.url ?? "";
  if (a.kind === "video") return `<video src="${escapeAttr(url)}" controls></video>`;
  const alt = assetAlt(a, fallbackTitle);
  if (format === "html") return `<img src="${escapeAttr(url)}" alt="${escapeAttr(alt)}">`;
  return `![${alt}](${url})`;
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (!ok) throw new Error("copy failed");
    }
    ElMessage.success(t("common.copied"));
    return true;
  } catch {
    ElMessage.error(t("common.copyFailed"));
    return false;
  }
}

// ---------- 错误提示 ----------

/** 素材操作（重试 / 转存 / 取消 / 删除）错误 → 提示文案 */
export function describeAssetError(err: unknown): string | null {
  if (!isApiError(err)) return t("common.requestFailed");
  if (err.code === 0 && err.message === "canceled") return null;
  if (err.code === 409) {
    const data = obj<ConflictData>(err.data);
    if (data.reason === "in_use") return t("media.errors.inUse");
    if (data.hint) return t("media.errors.hint", { hint: data.hint });
    if (data.current_status === "running") return t("media.errors.cancelRunning");
    if (data.current_status) {
      const label = statusLabel("media_status", data.current_status);
      if (data.current_status === "downloading") return t("media.errors.downloadingConflict", { status: label });
      if (data.current_status === "pending" || data.current_status === "submitted" || data.current_status === "generating") {
        return t("media.errors.activeConflict", { status: label });
      }
      return t("media.errors.statusConflict", { status: label });
    }
    return err.message || t("common.requestFailed");
  }
  if (err.code === 5021) {
    const data = obj<UpstreamErrorData>(err.data);
    const category = data.error_category ? statusLabel("error_category", data.error_category) : "-";
    return t("media.errors.upstreamRecheck", { category, requestId: data.request_id || "-" });
  }
  if (err.code === 404) return t("media.errors.notFound");
  const { notice } = describeGenerateError(err);
  if (!notice) return null;
  return notice.description ? `${notice.title}：${notice.description}` : notice.title;
}

/**
 * 内容绑定（attach / detach）错误 → 提示文案（docs/10 §4.9；docs/03 B.12）：
 * 409 `current_status` 先由内容 `generating` 触发、其次是素材未 `ready`（两者都可能为 `generating`，按本地素材状态区分）；
 * 400（`kind_mismatch` 封面只能是图片 / `project_mismatch` 素材不属于该项目）取校验条目的 `msg`；404 素材不存在或未绑定在该内容。
 */
export function describeAttachError(err: unknown, asset?: Pick<MediaAsset, "status"> | null): string | null {
  if (!isApiError(err)) return t("common.requestFailed");
  if (err.code === 0 && err.message === "canceled") return null;
  if (err.code === 409) {
    const status = obj<ConflictData>(err.data).current_status;
    if (status === "generating" && (!asset || asset.status === "ready")) return t("media.attach.contentGenerating");
    if (status) return t("media.attach.assetNotReady", { status: statusLabel("media_status", status) });
    return err.message || t("common.requestFailed");
  }
  if (err.code === 400) {
    const msgs = validationErrors(err).map((i) => i.msg).filter(Boolean);
    return msgs.length ? msgs.join("；") : err.message || t("common.validationFailed");
  }
  if (err.code === 404) return t("media.errors.notFound");
  return err.message || t("common.requestFailed");
}

function showError(err: unknown) {
  const msg = describeAssetError(err);
  if (msg) ElMessage.error({ message: msg, grouping: true });
}

async function confirm(message: string, title: string, type: "warning" | "info" = "warning", confirmText?: string): Promise<boolean> {
  try {
    await ElMessageBox.confirm(h("div", { style: "white-space: pre-line; word-break: break-word" }, message), title, {
      type,
      confirmButtonText: confirmText ?? t("common.confirm"),
      cancelButtonText: t("common.cancel"),
    });
    return true;
  } catch {
    return false;
  }
}

// ---------- 操作流程 ----------

/** 重试：返回 `{asset, task_id, resumed}`；用户取消或失败返回 null（失败已提示） */
export async function retryAssetFlow(a: MediaAsset): Promise<MediaRetryResult | null> {
  let message: string;
  if (retryChecksUpstream(a)) message = t("media.actions.retryRecheckConfirm");
  else if (a.status === "failed" && a.error_category === "transfer_failed" && a.upstream_url) message = t("media.actions.retryTransferFailedConfirm");
  else message = t("media.actions.retryResubmitConfirm");
  if (!(await confirm(message, t("media.actions.retryTitle", { id: a.id })))) return null;
  try {
    const res = await mediaApi.retryAsset(a.id, { silent: true });
    ElMessage.success(res.resumed ? t("media.actions.retryResumed", { id: res.task_id }) : t("media.actions.retryResubmitted", { id: res.task_id }));
    return res;
  } catch (err) {
    showError(err);
    return null;
  }
}

/** 重新转存（不新建根任务、不计费） */
export async function transferAssetFlow(a: MediaAsset): Promise<MediaTransferResult | null> {
  try {
    const res = await mediaApi.transferAsset(a.id, { silent: true });
    ElMessage.success(t("media.actions.transferQueued"));
    return res;
  } catch (err) {
    showError(err);
    return null;
  }
}

/** 取消当前根任务（二次确认，不退费）；成功返回 true */
export async function cancelAssetFlow(a: MediaAsset): Promise<boolean> {
  const taskId = a.ai_task_id;
  if (!taskId) return false;
  const message = a.kind === "video" ? t("media.actions.cancelVideoConfirm") : t("media.actions.cancelConfirm");
  if (!(await confirm(message, t("media.actions.cancelTitle", { id: a.id }), "warning", t("media.actions.cancel")))) return false;
  try {
    await aiApi.cancelTask(taskId, { silent: true });
    ElMessage.success(t("media.actions.cancelled"));
    return true;
  } catch (err) {
    showError(err);
    return false;
  }
}

/** 删除：先取详情计算引用（列表不含 references），确认后删除；成功返回 true */
export async function deleteAssetFlow(a: MediaAsset): Promise<boolean> {
  let detail = a;
  if (!a.references) {
    try {
      detail = await mediaApi.getAsset(a.id, { silent: true });
    } catch (err) {
      showError(err);
      return false;
    }
  }
  if (!canDeleteAsset(detail)) {
    ElMessage.warning(t("media.errors.statusConflict", { status: statusLabel("media_status", detail.status) }));
    return false;
  }
  const refs = detail.references;
  const lines: string[] = [t("media.actions.deleteConfirm", { id: detail.id })];
  if (refs && refs.count > 0) {
    lines.push(t("media.actions.deleteRefs", { count: refs.count }));
    if (refs.cover_of) lines.push(`· ${t("media.references.coverOf", { id: refs.cover_of })}`);
    if (refs.bound_content_id) lines.push(`· ${t("media.references.boundContent", { id: refs.bound_content_id })}`);
    if (refs.referenced_by_asset_ids.length) {
      lines.push(`· ${t("media.references.referencedBy", { ids: refs.referenced_by_asset_ids.map((r) => `#${r.id}`).join(", ") })}`);
    }
  }
  if (detail.status === "ready") lines.push(t("media.actions.deleteInlineHint"));
  if (!(await confirm(lines.join("\n"), t("media.actions.deleteTitle"), "warning", t("common.delete")))) return false;
  try {
    await mediaApi.deleteAsset(detail.id, { silent: true });
    ElMessage.success(t("media.actions.deleted"));
    return true;
  } catch (err) {
    showError(err);
    return false;
  }
}

// ---------- 生成接口错误（docs/10 §7.1「错误提示映射」） ----------

export interface MediaGenerateErrorInfo {
  notice: GenerateNoticeInfo | null;
  /** 按字段名的错误（prompt / negative_prompt / model / content_id / count …），供 el-form-item :error */
  fields: Record<string, string>;
  /** 4222 返回的非公网 URL（高亮对应输入） */
  invalidUrls: string[];
}

/**
 * 图片 / 视频生成接口的错误映射：
 * 400 `type=banned_words` 按 `loc` 高亮提示词 / 负向提示词；400 `data.model` 高亮模型下拉；4222 高亮非公网 URL；
 * 4291 按 `scope` 提示日上限或额度；5031 显示暂停原因 / 不可用模型 / 熔断（`hint=model_override` 提示更换模型）；429 显示 `retry_after` 倒计时；409 显示状态。
 */
export function describeMediaGenerateError(err: unknown, modality: "image" | "video"): MediaGenerateErrorInfo {
  if (isApiError(err) && err.code === 4222) {
    const urls = Array.isArray(obj<PublicUrlRequiredData>(err.data).urls) ? (obj<PublicUrlRequiredData>(err.data).urls as string[]) : [];
    return {
      notice: { type: "error", title: err.message || t("media.errors.publicUrlRequired"), description: t("media.errors.publicUrlHint", { urls: urls.join("、") || "-" }) },
      fields: {},
      invalidUrls: urls,
    };
  }
  if (isApiError(err) && err.code === 400 && !Array.isArray(err.data) && obj<{ model: string }>(err.data).model !== undefined) {
    const msg = t("media.errors.modelInvalid", { model: String(obj<{ model: string }>(err.data).model ?? ""), modality: t(`status.media_kind.${modality}`) });
    return { notice: { type: "error", title: msg }, fields: { model: msg }, invalidUrls: [] };
  }
  const { notice, fields } = describeGenerateError(err);
  return { notice, fields, invalidUrls: [] };
}
