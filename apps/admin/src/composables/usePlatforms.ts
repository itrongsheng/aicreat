// 发布平台目录（GET /admin/platforms，不分页）的模块级缓存：回填弹窗、链接列表筛选、编辑弹窗共用；
// 平台页写操作后调用 invalidatePlatforms()。无 publish.platforms.view 权限时不请求，列表为空（链接详情自带 platform 摘要）。
import { computed, ref } from "vue";
import type { Platform } from "@aicreat/shared";
import * as platformsApi from "@/api/platforms";
import { useAuthStore } from "@/store/auth";
import { platformLabel } from "@/utils/links";

const CACHE_TTL_MS = 60_000;
const platforms = ref<Platform[]>([]);
const loading = ref(false);
let loadedAt = 0;
let pending: Promise<Platform[]> | null = null;

/** 平台增删改后清空缓存（下次 load 重新请求） */
export function invalidatePlatforms(): void {
  loadedAt = 0;
}

export function usePlatforms() {
  const auth = useAuthStore();

  async function load(force = false): Promise<Platform[]> {
    if (!auth.hasPermission("publish.platforms.view")) {
      platforms.value = [];
      return platforms.value;
    }
    if (!force && loadedAt && Date.now() - loadedAt < CACHE_TTL_MS) return platforms.value;
    if (pending) return pending;
    loading.value = true;
    pending = platformsApi
      .list({}, { silent: true })
      .then((list) => {
        platforms.value = [...list].sort((a, b) => a.sort - b.sort || a.id - b.id);
        loadedAt = Date.now();
        return platforms.value;
      })
      .catch(() => platforms.value)
      .finally(() => {
        pending = null;
        loading.value = false;
      });
    return pending;
  }

  const byId = computed(() => new Map(platforms.value.map((p) => [p.id, p])));
  const activePlatforms = computed(() => platforms.value.filter((p) => p.is_active));

  function labelOf(id: number | null | undefined): string {
    if (!id) return "-";
    return platformLabel(byId.value.get(id), id);
  }

  return { platforms, activePlatforms, byId, loading, load, labelOf };
}
