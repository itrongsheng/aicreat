<script setup lang="ts">
// 绑定素材到内容（AssetPicker 的反向流程，docs/10 §4.9、§7.1、§7.3）：选择内容 + 用途（视频只提供 inline），
// POST /admin/contents/{id}/assets/{asset_id}/attach {usage_type, sort}；sort 缺省取该内容现有绑定素材 MAX(sort)+1。
// 素材 project_id 为空（上传素材）时可选任一可见内容（attach 时写入内容的项目）；否则只列同项目内容。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import type { MediaAsset } from "@aicreat/shared";
import * as contentsApi from "@/api/contents";
import ContentSelect from "@/components/ContentSelect.vue";
import { describeAttachError } from "@/composables/useAssetActions";
import { useProjectStore } from "@/store/project";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    asset: MediaAsset | null;
    /** 预选内容 */
    contentId?: number | null;
  }>(),
  { contentId: null },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "attached", asset: MediaAsset, contentId: number): void;
}>();

const { t } = useI18n();
const projectStore = useProjectStore();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const form = ref({ content_id: null as number | null, usage_type: "inline" as "inline" | "cover", sort: undefined as number | undefined });
const saving = ref(false);
const contentError = ref("");

const projectId = computed(() => props.asset?.project_id ?? (projectStore.currentId || null));
const isVideo = computed(() => props.asset?.kind === "video");

watch(visible, (v) => {
  if (!v) return;
  form.value = {
    content_id: props.contentId ?? props.asset?.content_id ?? null,
    usage_type: "inline",
    sort: undefined,
  };
  contentError.value = "";
});

async function nextSort(contentId: number): Promise<number> {
  try {
    const list = await contentsApi.listAssets(contentId, { silent: true });
    return list.reduce((m, x) => Math.max(m, x.sort ?? 0), 0) + 1;
  } catch {
    return 1;
  }
}

async function submit() {
  const asset = props.asset;
  const contentId = form.value.content_id;
  if (!asset) return;
  if (!contentId) {
    contentError.value = t("media.attach.contentRequired");
    return;
  }
  saving.value = true;
  try {
    const usage = isVideo.value ? "inline" : form.value.usage_type;
    const sort = form.value.sort ?? (asset.content_id === contentId ? asset.sort : await nextSort(contentId));
    const res = await contentsApi.attach(contentId, asset.id, { usage_type: usage, sort }, { silent: true });
    ElMessage.success(usage === "cover" ? t("editor.assets.coverSet") : t("editor.assets.attached"));
    emit("attached", res, contentId);
    visible.value = false;
  } catch (err) {
    const msg = describeAttachError(err, asset);
    if (msg) ElMessage.error(msg);
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('media.attach.title', { id: asset?.id ?? '' })" width="480px" append-to-body>
    <el-form label-width="90px" @submit.prevent>
      <el-form-item :label="t('media.fields.content')" :error="contentError" required>
        <ContentSelect v-model="form.content_id" :project-id="projectId" @change="contentError = ''" />
      </el-form-item>
      <el-form-item :label="t('media.fields.usage')">
        <el-radio-group v-model="form.usage_type">
          <el-radio value="inline">{{ t("status.media_usage_type.inline") }}</el-radio>
          <el-radio v-if="!isVideo" value="cover">{{ t("status.media_usage_type.cover") }}</el-radio>
        </el-radio-group>
        <div v-if="isVideo" class="text-secondary attach-tip">{{ t("media.attach.videoInlineOnly") }}</div>
        <div v-else-if="form.usage_type === 'cover'" class="text-secondary attach-tip">{{ t("media.attach.coverTip") }}</div>
      </el-form-item>
      <el-form-item :label="t('media.fields.sort')">
        <el-input-number v-model="form.sort" :min="0" :controls="false" :placeholder="t('media.attach.sortAuto')" style="width: 160px" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="saving" @click="submit">{{ t("media.actions.attach") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.attach-tip {
  width: 100%;
  font-size: 12px;
  line-height: 1.5;
}
</style>
