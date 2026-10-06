// v-permission="'content.keywords.generate'"；数组表示任一满足。无权限时隐藏元素（docs/07 §9.3）
import type { Directive } from "vue";
import { useAuthStore } from "@/store/auth";

function apply(el: HTMLElement, value: string | string[] | undefined) {
  const auth = useAuthStore();
  const codes = Array.isArray(value) ? value : value ? [value] : [];
  el.style.display = codes.length === 0 || codes.some((c) => auth.hasPermission(c)) ? "" : "none";
}

export const permissionDirective: Directive<HTMLElement, string | string[]> = {
  mounted: (el, binding) => apply(el, binding.value),
  updated: (el, binding) => apply(el, binding.value),
};
