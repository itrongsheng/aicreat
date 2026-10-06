// 窄屏判定（matchMedia，默认 ≤ 768px）：描述列表等在移动端改为单列，避免横向溢出
import { getCurrentScope, onScopeDispose, ref } from "vue";

export function useNarrow(maxWidth = 768) {
  const query = typeof window !== "undefined" && window.matchMedia ? window.matchMedia(`(max-width: ${maxWidth}px)`) : null;
  const narrow = ref(query?.matches ?? false);
  const onChange = (e: MediaQueryListEvent) => (narrow.value = e.matches);
  query?.addEventListener("change", onChange);
  if (getCurrentScope()) onScopeDispose(() => query?.removeEventListener("change", onChange));
  return narrow;
}
