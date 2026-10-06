// 权限判断（docs/07 §9.2）；前端控制只用于交互体验，安全判断以后端为准
import { computed } from "vue";
import { useAuthStore } from "@/store/auth";

export function usePermission() {
  const auth = useAuthStore();
  const has = (code: string) => auth.hasPermission(code);
  const hasAny = (codes: string[]) => codes.some(has);
  const hasAll = (codes: string[]) => codes.every(has);
  return {
    has,
    hasAny,
    hasAll,
    permissions: computed(() => auth.permissions),
    isSuperAdmin: computed(() => auth.isSuperAdmin),
    isAllScope: computed(() => auth.isAllScope),
  };
}
