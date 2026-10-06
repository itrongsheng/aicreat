// 回填链接的对象级动作（docs/11 §4.5、§6.7、§6.8；docs/04 §6.17、§7.11）：立即检测、重建基线、暂停 / 恢复监控、删除。
// rebaseline / pause / resume / 删除均二次确认（docs/11 §16.3）；queued=false 时按 reason 提示。链接列表与详情共用。
import { ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import type { PublishLink } from "@aicreat/shared";
import * as linksApi from "@/api/links";
import { t } from "@/i18n";
import { shortUrl } from "@/utils/links";

type LinkRef = Pick<PublishLink, "id" | "url" | "normalized_url" | "is_monitoring" | "alive_status">;

async function confirm(message: string, type: "warning" | "error" = "warning"): Promise<boolean> {
  try {
    await ElMessageBox.confirm(message, type === "error" ? t("common.warning") : t("common.tip"), { type });
    return true;
  } catch {
    return false;
  }
}

function queuedMessage(res: { queued: boolean; reason?: string }, okKey: string): void {
  if (res.queued) ElMessage.success(t(okKey));
  else ElMessage.warning(t(`links.reason.${res.reason ?? "already_queued"}`));
}

export function useLinkActions() {
  /** 正在执行动作的链接 ID（按钮 loading） */
  const acting = ref<number | null>(null);

  async function run<T>(id: number, fn: () => Promise<T>): Promise<T | null> {
    acting.value = id;
    try {
      return await fn();
    } catch {
      return null; // 拦截器已提示
    } finally {
      if (acting.value === id) acting.value = null;
    }
  }

  /** 立即删除检测（manual 插队，不受日上限拦截） */
  async function check(link: LinkRef): Promise<boolean> {
    const res = await run(link.id, () => linksApi.check(link.id));
    if (!res) return false;
    queuedMessage(res, "links.actions.checkQueued");
    return res.queued;
  }

  /** 重建基线：清空 baseline_* 并入队 manual 检测 */
  async function rebaseline(link: LinkRef): Promise<boolean> {
    if (!(await confirm(t("links.actions.rebaselineConfirm")))) return false;
    const res = await run(link.id, () => linksApi.rebaseline(link.id));
    if (!res) return false;
    queuedMessage(res, "links.actions.rebaselineQueued");
    return true;
  }

  /** 暂停 / 恢复监控；返回更新后的链接 */
  async function setMonitoring(link: LinkRef, on: boolean): Promise<PublishLink | null> {
    if (on === link.is_monitoring) return null;
    const ok = await confirm(on ? t("links.actions.resumeConfirm") : t("links.actions.pauseConfirm"));
    if (!ok) return null;
    const updated = await run(link.id, () => (on ? linksApi.resume(link.id) : linksApi.pause(link.id)));
    if (updated) ElMessage.success(on ? t("links.actions.resumed") : t("links.actions.paused"));
    return updated;
  }

  /** 删除链接（级联检测记录） */
  async function remove(link: LinkRef): Promise<boolean> {
    const ok = await confirm(t("links.actions.deleteConfirm", { url: shortUrl(link.normalized_url || link.url, 80) }), "error");
    if (!ok) return false;
    const res = await run(link.id, () => linksApi.remove(link.id).then(() => true));
    if (res) ElMessage.success(t("links.actions.deleted"));
    return !!res;
  }

  return { acting, check, rebaseline, setMonitoring, remove };
}
