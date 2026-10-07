// 「只保留最新一次」的请求封装（docs/12 §10.5）：新请求发出时用 AbortController 取消未完成的旧请求，
// 被取消或被后续请求取代的结果一律丢弃；组件卸载时自动取消。配合 debounce() 实现筛选变化 300 ms 防抖。
import { getCurrentScope, onScopeDispose, ref } from "vue";
import { isCanceled } from "@/api/stats";

export type LatestResult<T> = { ok: true; value: T } | { ok: false; error: unknown };

export function useLatestRequest() {
  const loading = ref(false);
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function abort() {
    if (timer) clearTimeout(timer);
    timer = null;
    controller?.abort();
    controller = null;
    loading.value = false;
  }

  /** 执行请求；被取消 / 被取代时返回 null */
  async function run<T>(fn: (signal: AbortSignal) => Promise<T>): Promise<LatestResult<T> | null> {
    controller?.abort();
    const ctrl = new AbortController();
    controller = ctrl;
    loading.value = true;
    try {
      const value = await fn(ctrl.signal);
      return ctrl === controller ? { ok: true, value } : null;
    } catch (error) {
      if (ctrl !== controller || isCanceled(error)) return null;
      return { ok: false, error };
    } finally {
      if (ctrl === controller) {
        loading.value = false;
        controller = null;
      }
    }
  }

  /** 防抖执行（默认 300 ms） */
  function debounce(fn: () => void, wait = 300) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      fn();
    }, wait);
  }

  if (getCurrentScope()) onScopeDispose(abort);

  return { loading, run, abort, debounce };
}
