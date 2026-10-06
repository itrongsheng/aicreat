<script setup lang="ts">
// 检测记录页「批量触发」（docs/11 §11.6；docs/04 §6.18、§7.11；docs/13 §7.5、§12.2）：
// - kind=link：POST /admin/monitoring/link-checks/run {project_id?, platform_id?, link_ids?, only_due}；
// - kind=index：POST /admin/monitoring/index-checks/run，另含 kinds（必选）与 engines（不选 = 所选 kinds 下全部启用引擎）；
// - 返回 {enqueued, skipped}（已在队列 / 日上限 / 不可见 / deleted、暂停监控的链接计入 skipped）；
// - 总后台处于用户视角时 client.ts 自动附加 owner_id：未选项目且未填链接 ID 时只对该用户负责项目下的链接入队（弹窗内提示）。
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { BATCH_IDS_MAX, type EnqueueResult, type IndexKind } from "@aicreat/shared";
import * as monitoringApi from "@/api/monitoring";
import ProjectSelect from "@/components/ProjectSelect.vue";
import { useIndexEngines } from "@/composables/useIndexEngines";
import { usePermission } from "@/composables/usePermission";
import { usePlatforms } from "@/composables/usePlatforms";
import { useProjectStore } from "@/store/project";
import { platformLabel } from "@/utils/links";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    kind: "link" | "index";
    /** 预置项目（默认取顶栏当前项目） */
    projectId?: number | null;
    /** 预置平台（来自列表筛选） */
    platformId?: number | null;
  }>(),
  { projectId: null, platformId: null },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "done", result: EnqueueResult): void;
}>();

const { t } = useI18n();
const { isAllScope } = usePermission();
const projectStore = useProjectStore();
const { platforms, load: loadPlatforms } = usePlatforms();
const engineCatalog = useIndexEngines();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const form = reactive({
  project_id: 0,
  platform_id: undefined as number | undefined,
  linkIdsText: "",
  only_due: true,
  kinds: ["seo", "geo"] as IndexKind[],
  engines: [] as string[],
});
const errors = reactive({ link_ids: "", kinds: "" });
const submitting = ref(false);
const result = ref<EnqueueResult | null>(null);

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    form.project_id = props.projectId ?? projectStore.currentId ?? 0;
    form.platform_id = props.platformId ?? undefined;
    form.linkIdsText = "";
    form.only_due = true;
    form.kinds = ["seo", "geo"];
    form.engines = [];
    errors.link_ids = "";
    errors.kinds = "";
    result.value = null;
    void loadPlatforms();
    if (props.kind === "index") void engineCatalog.load();
  },
  { immediate: true },
);

const viewingOwner = computed(() => isAllScope.value && projectStore.ownerId > 0);

const platformOptions = computed(() => platforms.value.map((p) => ({ value: p.id, label: platformLabel(p) })));

const engineOptions = computed(() =>
  engineCatalog.enabledEngines.value
    .filter((e) => form.kinds.includes(e.kind))
    .map((e) => ({ value: e.code, kind: e.kind, label: `${engineCatalog.label(e.kind, e.code)}（${e.kind.toUpperCase()}）` })),
);
const noEnabled = computed(() => props.kind === "index" && engineCatalog.loaded.value && !engineOptions.value.length);

watch(
  () => form.kinds.slice(),
  () => {
    const allowed = new Set(engineOptions.value.map((o) => o.value));
    form.engines = form.engines.filter((e) => allowed.has(e));
  },
);

/** 解析链接 ID：逗号 / 空白 / 换行分隔的正整数，去重 */
function parseLinkIds(text: string): { ids: number[]; invalid: string[] } {
  const ids: number[] = [];
  const invalid: string[] = [];
  const seen = new Set<number>();
  for (const part of text.split(/[\s,，;；]+/)) {
    const s = part.trim();
    if (!s) continue;
    const n = Number(s.replace(/^#/, ""));
    if (!Number.isInteger(n) || n <= 0) invalid.push(s);
    else if (!seen.has(n)) {
      seen.add(n);
      ids.push(n);
    }
  }
  return { ids, invalid };
}

const parsedIds = computed(() => parseLinkIds(form.linkIdsText));

const scopeDescription = computed(() => {
  if (parsedIds.value.ids.length) return t("monitoring.run.scopeLinks", { n: parsedIds.value.ids.length });
  if (form.project_id) return t("monitoring.run.scopeProject", { name: projectStore.projects.find((p) => p.id === form.project_id)?.name ?? `#${form.project_id}` });
  if (viewingOwner.value) return t("monitoring.run.scopeOwner", { name: projectStore.ownerName });
  return isAllScope.value ? t("monitoring.run.scopeAll") : t("monitoring.run.scopeOwn");
});

async function submit() {
  errors.link_ids = "";
  errors.kinds = "";
  const { ids, invalid } = parsedIds.value;
  if (invalid.length) {
    errors.link_ids = t("monitoring.run.invalidIds", { ids: invalid.slice(0, 5).join(", ") });
    return;
  }
  if (ids.length > BATCH_IDS_MAX) {
    errors.link_ids = t("monitoring.run.tooManyIds", { max: BATCH_IDS_MAX });
    return;
  }
  if (props.kind === "index" && !form.kinds.length) {
    errors.kinds = t("links.indexCheck.kindsRequired");
    return;
  }
  const base = {
    project_id: form.project_id || null,
    platform_id: form.platform_id ?? null,
    link_ids: ids.length ? ids : null,
    only_due: form.only_due,
  };
  submitting.value = true;
  try {
    const res =
      props.kind === "link"
        ? await monitoringApi.runLinkChecks(base)
        : await monitoringApi.runIndexChecks({ ...base, kinds: form.kinds, engines: form.engines.length ? form.engines : null });
    result.value = res;
    if (res.enqueued > 0) ElMessage.success(t("monitoring.run.result", { enqueued: res.enqueued, skipped: res.skipped }));
    else ElMessage.warning(t("monitoring.run.result", { enqueued: res.enqueued, skipped: res.skipped }));
    emit("done", res);
  } catch {
    /* 拦截器已提示（400 未启用引擎 / 校验错误、403） */
  } finally {
    submitting.value = false;
  }
}

const title = computed(() => (props.kind === "link" ? t("monitoring.run.titleLink") : t("monitoring.run.titleIndex")));
</script>

<template>
  <el-dialog v-model="visible" :title="title" width="min(560px, 96vw)" :close-on-click-modal="false" append-to-body>
    <el-alert v-if="viewingOwner" type="warning" :closable="false" show-icon class="mb" :title="t('monitoring.run.ownerTip', { name: projectStore.ownerName })" />
    <el-form label-width="110px" class="run-form" @submit.prevent="submit">
      <el-form-item :label="t('monitoring.run.project')">
        <ProjectSelect v-model="form.project_id" allow-all width="100%" />
      </el-form-item>
      <el-form-item :label="t('monitoring.run.platform')">
        <el-select v-model="form.platform_id" clearable filterable :placeholder="t('monitoring.run.allPlatforms')" style="width: 100%">
          <el-option v-for="o in platformOptions" :key="o.value" :value="o.value" :label="o.label" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('monitoring.run.linkIds')" :error="errors.link_ids">
        <el-input v-model="form.linkIdsText" type="textarea" :rows="2" :placeholder="t('monitoring.run.linkIdsPlaceholder')" />
        <div class="form-hint">{{ t("monitoring.run.linkIdsHint", { max: BATCH_IDS_MAX }) }}</div>
      </el-form-item>
      <el-form-item :label="t('monitoring.run.onlyDue')">
        <el-switch v-model="form.only_due" />
        <div class="form-hint">{{ kind === "link" ? t("monitoring.run.onlyDueLinkHint") : t("monitoring.run.onlyDueIndexHint") }}</div>
      </el-form-item>
      <template v-if="kind === 'index'">
        <el-form-item :label="t('links.indexCheck.kinds')" :error="errors.kinds">
          <el-checkbox-group v-model="form.kinds">
            <el-checkbox value="seo">{{ t("status.index_kind.seo") }}</el-checkbox>
            <el-checkbox value="geo">{{ t("status.index_kind.geo") }}</el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item :label="t('links.indexCheck.engines')">
          <el-select v-model="form.engines" multiple clearable collapse-tags-tooltip :placeholder="t('links.indexCheck.enginesAll')" style="width: 100%">
            <el-option v-for="o in engineOptions" :key="`${o.kind}:${o.value}`" :value="o.value" :label="o.label" />
          </el-select>
          <div class="form-hint">{{ noEnabled ? t("links.indexCheck.noEnabled") : t("links.indexCheck.enginesHint") }}</div>
        </el-form-item>
      </template>
      <el-form-item :label="t('monitoring.run.scope')">
        <span class="scope-text">{{ scopeDescription }}</span>
        <div class="form-hint">{{ kind === "link" ? t("monitoring.run.skipHintLink") : t("monitoring.run.skipHintIndex") }}</div>
      </el-form-item>
    </el-form>
    <el-alert
      v-if="result"
      :type="result.enqueued > 0 ? 'success' : 'warning'"
      :closable="false"
      show-icon
      :title="t('monitoring.run.result', { enqueued: result.enqueued, skipped: result.skipped })"
    />
    <template #footer>
      <el-button @click="visible = false">{{ result ? t("common.close") : t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="submitting" :disabled="noEnabled" @click="submit">{{ t("monitoring.run.submit") }}</el-button>
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
.scope-text {
  font-size: 13px;
}
@media (max-width: 600px) {
  .run-form :deep(.el-form-item) {
    flex-direction: column;
    align-items: stretch;
  }
  .run-form :deep(.el-form-item__label) {
    justify-content: flex-start;
    width: auto !important;
    height: auto;
    line-height: 1.5;
    margin-bottom: 4px;
  }
}
</style>
