// 任务 / 批次状态轮询（docs/04 §8）：页面不可见时暂停；连续 3 次失败退避到 10s，成功后回到原间隔；
// 到终态由调用方 stop()；组件卸载时自动停止。
import { getCurrentScope, onScopeDispose, ref } from "vue";

export interface PollingOptions {
  /** 轮询间隔（毫秒），默认 3000；图片 5000、视频 15000 */
  interval?: number;
  /** start() 时是否立即执行一次，默认 true */
  immediate?: boolean;
  /** 连续失败多少次后退避，默认 3 */
  maxErrors?: number;
  /** 退避间隔，默认 10000 */
  backoffInterval?: number;
}

export function usePolling(fn: () => unknown | Promise<unknown>, options: PollingOptions = {}) {
  const interval = options.interval ?? 3000;
  const immediate = options.immediate ?? true;
  const maxErrors = options.maxErrors ?? 3;
  const backoffInterval = options.backoffInterval ?? 10_000;

  const running = ref(false);
  const errorCount = ref(0);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight = false;

  const visible = () => typeof document === "undefined" || document.visibilityState === "visible";

  function clearTimer() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  function schedule(delay: number) {
    clearTimer();
    if (!running.value) return;
    timer = setTimeout(tick, delay);
  }

  async function tick() {
    timer = null;
    if (!running.value) return;
    if (!visible()) return; // 不可见：暂停，等 visibilitychange 恢复
    if (inFlight) return schedule(interval);
    inFlight = true;
    try {
      await fn();
      errorCount.value = 0;
    } catch {
      errorCount.value += 1;
    } finally {
      inFlight = false;
    }
    schedule(errorCount.value >= maxErrors ? backoffInterval : interval);
  }

  function onVisibilityChange() {
    if (!running.value) return;
    if (visible()) {
      if (!timer && !inFlight) void tick();
    } else {
      clearTimer();
    }
  }

  function start() {
    if (running.value) return;
    running.value = true;
    errorCount.value = 0;
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibilityChange);
    if (immediate) void tick();
    else schedule(interval);
  }

  function stop() {
    running.value = false;
    clearTimer();
    if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibilityChange);
  }

  if (getCurrentScope()) onScopeDispose(stop);

  return { start, stop, running, errorCount };
}
