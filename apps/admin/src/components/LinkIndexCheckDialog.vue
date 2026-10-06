<script setup lang="ts">
// 立即收录检测（docs/11 §7.5、§9；docs/04 §6.17、§7.11 POST /admin/links/{id}/index-check）：
// 选择 kinds（SEO / GEO）与引擎（只列启用的引擎；不选 = 所选 kinds 下全部启用引擎）；
// 返回 queued=false 时按 reason 提示（already_queued / daily_limit）；deleted / 暂停监控的链接 409，前端预先禁用并说明。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import type { IndexKind, PublishLink } from "@aicreat/shared";
import * as linksApi from "@/api/links";
import { useIndexEngines } from "@/composables/useIndexEngines";

const props = defineProps<{ modelValue: boolean; link: Pick<PublishLink, "id" | "alive_status" | "is_monitoring"> | null }>();
const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "queued"): void;
}>();

const { t } = useI18n();
const engineCatalog = useIndexEngines();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const kinds = ref<IndexKind[]>(["seo", "geo"]);
const engines = ref<string[]>([]);
const submitting = ref(false);
const error = ref("");

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    kinds.value = ["seo", "geo"];
    engines.value = [];
    error.value = "";
    void engineCatalog.load();
  },
  { immediate: true },
);

const blockedReason = computed(() => {
  const link = props.link;
  if (!link) return "";
  if (link.alive_status === "deleted") return t("links.indexCheck.blockedDeleted");
  if (!link.is_monitoring) return t("links.indexCheck.blockedPaused");
  return "";
});

const engineOptions = computed(() =>
  engineCatalog.enabledEngines.value
    .filter((e) => kinds.value.includes(e.kind))
    .map((e) => ({ value: e.code, kind: e.kind, label: `${engineCatalog.label(e.kind, e.code)}（${e.kind.toUpperCase()}）` })),
);

const noEnabled = computed(() => engineCatalog.loaded.value && !engineOptions.value.length);

watch(kinds, () => {
  const allowed = new Set(engineOptions.value.map((o) => o.value));
  engines.value = engines.value.filter((e) => allowed.has(e));
});

async function submit() {
  const link = props.link;
  if (!link || blockedReason.value) return;
  error.value = "";
  if (!kinds.value.length) {
    error.value = t("links.indexCheck.kindsRequired");
    return;
  }
  submitting.value = true;
  try {
    const res = await linksApi.indexCheck(link.id, { kinds: kinds.value, ...(engines.value.length ? { engines: engines.value } : {}) });
    if (res.queued) {
      ElMessage.success(t("links.indexCheck.queued"));
      emit("queued");
      visible.value = false;
    } else if (res.reason === "daily_limit") {
      ElMessage.warning(t("links.reason.daily_limit"));
    } else {
      ElMessage.warning(t("links.reason.already_queued"));
      visible.value = false;
    }
  } catch {
    /* 拦截器已提示（400 未启用引擎 / 409 deleted、暂停监控 / 429 频控） */
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('links.indexCheck.title')" width="min(520px, 96vw)" :close-on-click-modal="false" append-to-body>
    <el-alert v-if="blockedReason" type="warning" :title="blockedReason" :closable="false" show-icon class="mb" />
    <el-form label-width="90px" @submit.prevent="submit">
      <el-form-item :label="t('links.indexCheck.kinds')" :error="error">
        <el-checkbox-group v-model="kinds">
          <el-checkbox value="seo">{{ t("status.index_kind.seo") }}</el-checkbox>
          <el-checkbox value="geo">{{ t("status.index_kind.geo") }}</el-checkbox>
        </el-checkbox-group>
      </el-form-item>
      <el-form-item :label="t('links.indexCheck.engines')">
        <el-select v-model="engines" multiple clearable collapse-tags-tooltip :placeholder="t('links.indexCheck.enginesAll')" style="width: 100%">
          <el-option v-for="o in engineOptions" :key="`${o.kind}:${o.value}`" :value="o.value" :label="o.label" />
        </el-select>
        <div class="form-hint">{{ noEnabled ? t("links.indexCheck.noEnabled") : t("links.indexCheck.enginesHint") }}</div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!!blockedReason" @click="submit">{{ t("links.indexCheck.submit") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.mb {
  margin-bottom: 12px;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
</style>
