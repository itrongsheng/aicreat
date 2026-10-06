// 媒体资产进度跟踪（docs/10 §7.1、§7.2、§7.4；docs/04 §8）：
// - 根任务未终态时轮询 GET /admin/media/assets/{id}/task；根任务进入终态后改查 GET /admin/media/assets/{id}，
//   直到资产 status ∈ ready/failed/expired（或已 deleted）才停止 —— 「根任务终态且资产落定」双条件；
// - 任务状态 / 进度变化（如 queued → polling、progress 0 → 50、polling → succeeded）时顺带刷新一次资产详情，使卡片状态标签与进度同步；
// - 统一一个 usePolling（图片 5s、视频 15s），页面不可见时暂停，连续 3 次网络错误退避 10s；全部落定后停止。
import { computed, reactive } from "vue";
import type { AiTaskSummary, MediaAsset, MediaStatus } from "@aicreat/shared";
import * as mediaApi from "@/api/media";
import { isApiError } from "@/api/client";
import { isTerminalTask } from "@/components/TaskProgress.vue";
import { usePolling } from "@/composables/usePolling";

/** 资产落定（可展示 / 可人工处理）的状态 */
export const SETTLED_MEDIA_STATUSES: readonly MediaStatus[] = ["ready", "failed", "expired", "deleted"];
/** 进行中（需要轮询）的状态 */
export const ACTIVE_MEDIA_STATUSES: readonly MediaStatus[] = ["pending", "submitted", "generating", "downloading"];

export function isMediaSettled(status: MediaStatus | null | undefined): boolean {
  return !!status && SETTLED_MEDIA_STATUSES.includes(status);
}

export function isMediaActive(status: MediaStatus | null | undefined): boolean {
  return !!status && ACTIVE_MEDIA_STATUSES.includes(status);
}

export interface TrackedAsset {
  id: number;
  asset: MediaAsset | null;
  task: AiTaskSummary | null;
  /** 已至少取到一次 /task（上传素材等无根任务时为 null） */
  taskKnown: boolean;
  /** 资产已不可见 / 不存在（404） */
  missing: boolean;
}

export interface AssetTrackerOptions {
  /** 轮询间隔：图片 5000、视频 15000 */
  interval: number;
  /** 某资产落定时回调（ready / failed / expired / deleted） */
  onSettled?: (asset: MediaAsset) => void;
  /** 每次资产详情更新时回调 */
  onUpdate?: (asset: MediaAsset) => void;
}

/** 单个条目是否落定：资产落定且（无根任务或根任务终态） */
export function isTrackedSettled(entry: TrackedAsset): boolean {
  if (entry.missing) return true;
  if (!entry.asset || !isMediaSettled(entry.asset.status)) return false;
  // 资产落定时根任务必然已终态（docs/10 §4.6 对应表）；尚未取过 /task 的列表行直接按资产状态判断
  if (!entry.taskKnown) return true;
  return !entry.task || isTerminalTask(entry.task.status);
}

export function useAssetTracker(options: AssetTrackerOptions) {
  /** 按加入顺序保存（新提交的放在最前） */
  const order = reactive<number[]>([]);
  const entries = reactive<Record<number, TrackedAsset>>({});

  const list = computed<TrackedAsset[]>(() => order.map((id) => entries[id]).filter((e): e is TrackedAsset => !!e));
  const pending = computed(() => list.value.filter((e) => !isTrackedSettled(e)));

  function applyAsset(entry: TrackedAsset, asset: MediaAsset) {
    const wasSettled = isTrackedSettled(entry);
    entry.asset = asset;
    if (asset.task !== undefined) {
      entry.task = asset.task ?? null;
      entry.taskKnown = true;
    }
    options.onUpdate?.(asset);
    if (!wasSettled && isTrackedSettled(entry)) options.onSettled?.(asset);
  }

  async function fetchAsset(entry: TrackedAsset) {
    try {
      applyAsset(entry, await mediaApi.getAsset(entry.id, { silent: true }));
    } catch (err) {
      if (isApiError(err) && err.code === 404) {
        entry.missing = true;
        return;
      }
      throw err;
    }
  }

  async function step(entry: TrackedAsset) {
    if (isTrackedSettled(entry)) return;
    const taskActive = !entry.taskKnown || (entry.task !== null && !isTerminalTask(entry.task.status));
    if (!taskActive || !entry.asset) {
      await fetchAsset(entry);
      return;
    }
    let task: AiTaskSummary | null;
    try {
      task = await mediaApi.getAssetTask(entry.id, { silent: true });
    } catch (err) {
      if (isApiError(err) && err.code === 404) {
        entry.missing = true;
        return;
      }
      throw err;
    }
    const prev = entry.task;
    entry.task = task;
    entry.taskKnown = true;
    if (entry.asset && task && task.task_id === entry.asset.ai_task_id) {
      // /task 不含资产状态：进度两处同步（docs/10 §4.6），状态标签在状态变化时刷新详情获得
      entry.asset = { ...entry.asset, progress: Math.max(entry.asset.progress ?? 0, task.progress ?? 0) };
    }
    // 资产状态（submitted → generating 等）只能从详情获得：任务状态、根任务或进度变化时刷新一次详情
    const changed = !prev || !task || prev.status !== task.status || prev.task_id !== task.task_id || prev.progress !== task.progress;
    if (changed || !task || isTerminalTask(task.status)) await fetchAsset(entry);
  }

  async function tick() {
    const targets = pending.value;
    if (!targets.length) {
      polling.stop();
      return;
    }
    const results = await Promise.allSettled(targets.map((e) => step(e)));
    if (!pending.value.length) polling.stop();
    if (results.some((r) => r.status === "rejected")) throw new Error("asset polling failed");
  }

  const polling = usePolling(tick, { interval: options.interval, immediate: false });

  function ensurePolling() {
    if (pending.value.length && !polling.running.value) polling.start();
  }

  function upsert(id: number, front: boolean): TrackedAsset {
    let entry = entries[id];
    if (!entry) {
      entries[id] = { id, asset: null, task: null, taskKnown: false, missing: false };
      entry = entries[id];
      if (front) order.unshift(id);
      else order.push(id);
    }
    return entry;
  }

  /** 加入已有详情的资产（如列表行）；`front` 放到最前 */
  function add(asset: MediaAsset, front = false) {
    const entry = upsert(asset.id, front);
    entry.missing = false;
    entry.asset = asset;
    if (asset.task !== undefined) {
      entry.task = asset.task ?? null;
      entry.taskKnown = true;
    }
    ensurePolling();
  }

  /** 按 ID 加入（先取一次详情，再按需轮询） */
  async function addById(ids: number[], front = true): Promise<void> {
    const list = front ? [...ids].reverse() : ids;
    const created = list.map((id) => upsert(id, front));
    await Promise.allSettled(created.map((e) => fetchAsset(e)));
    ensurePolling();
  }

  /** 资产被重试 / 转存 / 取消等改变后：重新取详情并恢复轮询 */
  async function refresh(id: number): Promise<void> {
    const entry = entries[id];
    if (!entry) return;
    entry.taskKnown = false;
    entry.task = null;
    entry.missing = false;
    try {
      await fetchAsset(entry);
    } catch {
      /* 由下一轮轮询重试 */
    }
    ensurePolling();
  }

  /** 用接口返回的新资产（如 retry 的 `asset`）覆盖并恢复轮询 */
  function replace(asset: MediaAsset) {
    const entry = entries[asset.id];
    if (!entry) return add(asset);
    entry.asset = { ...(entry.asset ?? {}), ...asset } as MediaAsset;
    entry.taskKnown = false;
    entry.task = null;
    entry.missing = false;
    ensurePolling();
  }

  function remove(id: number) {
    const idx = order.indexOf(id);
    if (idx >= 0) order.splice(idx, 1);
    delete entries[id];
    if (!pending.value.length) polling.stop();
  }

  function clear() {
    order.splice(0, order.length);
    for (const key of Object.keys(entries)) delete entries[Number(key)];
    polling.stop();
  }

  function get(id: number): TrackedAsset | undefined {
    return entries[id];
  }

  return { list, pending, entries, add, addById, refresh, replace, remove, clear, get, polling };
}
