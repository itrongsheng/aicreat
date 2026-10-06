<script setup lang="ts">
// 人工标记收录 / 引用（docs/11 §7.4、§8.7；docs/04 §6.17、§7.11 POST /admin/links/{id}/mark-index）：
// status 只接受确定结论：SEO 为 indexed / not_indexed，GEO 为 cited / not_cited（不接受 unknown）；
// 写 index_checks(provider=manual) 并回写引擎状态，不改排程计数。成功后返回更新后的链接。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import type { IndexKind, LinkMarkIndexBody, PublishLink } from "@aicreat/shared";
import { fieldErrors, isApiError } from "@/api/client";
import * as linksApi from "@/api/links";
import { useIndexEngines } from "@/composables/useIndexEngines";

const NOTE_MAX = 500;
const EVIDENCE_URL_MAX = 1000;

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    link: Pick<PublishLink, "id"> | null;
    /** 预选（如详情收录状态表的行） */
    presetKind?: IndexKind | null;
    presetEngine?: string | null;
  }>(),
  { presetKind: null, presetEngine: null },
);
const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "saved", link: PublishLink): void;
}>();

const { t } = useI18n();
const engineCatalog = useIndexEngines();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const STATUS_BY_KIND: Record<IndexKind, LinkMarkIndexBody["status"][]> = {
  seo: ["indexed", "not_indexed"],
  geo: ["cited", "not_cited"],
};

const form = ref({ kind: "seo" as IndexKind, engine: "", status: "indexed" as LinkMarkIndexBody["status"], note: "", evidence_url: "" });
const errors = ref<Record<string, string>>({});
const saving = ref(false);

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    const kind = props.presetKind ?? "seo";
    form.value = { kind, engine: props.presetEngine ?? "", status: STATUS_BY_KIND[kind][0], note: "", evidence_url: "" };
    errors.value = {};
    void engineCatalog.load();
  },
  { immediate: true },
);

const engineOptions = computed(() =>
  (form.value.kind === "seo" ? engineCatalog.seoEngines.value : engineCatalog.geoEngines.value).map((e) => ({
    value: e.code,
    label: engineCatalog.label(e.kind, e.code),
    enabled: e.enabled,
  })),
);

function onKindChange(kind: IndexKind) {
  form.value.status = STATUS_BY_KIND[kind][0];
  if (!engineOptions.value.some((o) => o.value === form.value.engine)) form.value.engine = "";
}

const statusEnum = computed(() => (form.value.kind === "seo" ? "seo_index_status" : "geo_cite_status"));

async function submit() {
  const link = props.link;
  if (!link) return;
  const errs: Record<string, string> = {};
  if (!form.value.engine) errs.engine = t("links.mark.engineRequired");
  const url = form.value.evidence_url.trim();
  if (url) {
    if (url.length > EVIDENCE_URL_MAX) errs.evidence_url = t("links.mark.evidenceUrlTooLong", { max: EVIDENCE_URL_MAX });
    else if (!/^https?:\/\/\S+$/i.test(url)) errs.evidence_url = t("links.mark.evidenceUrlInvalid");
  }
  if (form.value.note.trim().length > NOTE_MAX) errs.note = t("links.validate.noteTooLong", { max: NOTE_MAX });
  errors.value = errs;
  if (Object.keys(errs).length) return;
  saving.value = true;
  try {
    const updated = await linksApi.markIndex(
      link.id,
      {
        kind: form.value.kind,
        engine: form.value.engine,
        status: form.value.status,
        note: form.value.note.trim() || null,
        evidence_url: url || null,
      },
      { silent: true },
    );
    ElMessage.success(t("links.mark.saved"));
    emit("saved", updated);
    visible.value = false;
  } catch (err) {
    if (!isApiError(err)) return;
    const fe = fieldErrors(err);
    if (Object.keys(fe).length) errors.value = fe;
    else ElMessage.error(err.message);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('links.mark.title')" width="min(520px, 96vw)" :close-on-click-modal="false" append-to-body>
    <el-form label-width="90px" @submit.prevent="submit">
      <el-form-item :label="t('links.mark.kind')" :error="errors.kind">
        <el-radio-group v-model="form.kind" @change="(v: string | number | boolean | undefined) => onKindChange(v as IndexKind)">
          <el-radio-button value="seo">{{ t("status.index_kind.seo") }}</el-radio-button>
          <el-radio-button value="geo">{{ t("status.index_kind.geo") }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item :label="t('links.mark.engine')" :error="errors.engine" required>
        <el-select v-model="form.engine" filterable style="width: 100%">
          <el-option v-for="o in engineOptions" :key="o.value" :value="o.value" :label="o.enabled ? o.label : `${o.label}（${t('links.mark.engineDisabled')}）`" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('links.mark.status')" :error="errors.status" required>
        <el-radio-group v-model="form.status">
          <el-radio v-for="s in STATUS_BY_KIND[form.kind]" :key="s" :value="s">{{ t(`status.${statusEnum}.${s}`) }}</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item :label="t('links.mark.evidenceUrl')" :error="errors.evidence_url">
        <el-input v-model="form.evidence_url" :maxlength="EVIDENCE_URL_MAX" placeholder="https://…" clearable />
      </el-form-item>
      <el-form-item :label="t('links.note')" :error="errors.note">
        <el-input v-model="form.note" type="textarea" :rows="2" :maxlength="NOTE_MAX" show-word-limit :placeholder="t('links.mark.notePlaceholder')" />
      </el-form-item>
      <div class="form-hint">{{ t("links.mark.hint") }}</div>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="saving" @click="submit">{{ t("common.save") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.form-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
</style>
