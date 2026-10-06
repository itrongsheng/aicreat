<script setup lang="ts">
// 编辑回填链接（docs/11 §4.5；docs/04 §6.17 PUT /admin/links/{id}）：可改平台 / 发布账号 / 发布时间 / 备注，URL 不可改；
// 发布时间范围同回填（[now−3650 天, now+5 分钟]，允许早于内容创建时间）；只提交变化的字段。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { LINK_LIMITS, type LinkUpdateBody, type PublishLink } from "@aicreat/shared";
import { fieldErrors, isApiError } from "@/api/client";
import * as linksApi from "@/api/links";
import { usePlatforms } from "@/composables/usePlatforms";
import { toUtcIso } from "@/utils/format";
import { checkPublishedAt, disabledPublishDate, platformLabel } from "@/utils/links";

const props = defineProps<{ modelValue: boolean; link: PublishLink | null }>();
const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "saved", link: PublishLink): void;
}>();

const { t } = useI18n();
const { platforms, load: loadPlatforms } = usePlatforms();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const form = ref({ platform_id: 0, publish_account: "", published_at: null as Date | null, note: "" });
const errors = ref<Record<string, string>>({});
const saving = ref(false);

interface PlatformOption {
  id: number;
  code: string;
  name: string;
  name_en?: string | null;
  is_active: boolean;
}

/** 可选平台：启用中的平台 + 当前平台（即使已停用也保留；无平台查看权限时用详情附带的 platform 摘要） */
const platformOptions = computed<PlatformOption[]>(() => {
  const link = props.link;
  const list: PlatformOption[] = platforms.value.filter((p) => p.is_active || p.id === link?.platform_id);
  if (link && !list.some((p) => p.id === link.platform_id)) {
    list.unshift({
      id: link.platform_id,
      code: link.platform?.code ?? String(link.platform_id),
      name: link.platform?.name ?? `#${link.platform_id}`,
      name_en: link.platform?.name_en ?? null,
      is_active: true,
    });
  }
  return list;
});

watch(
  () => [props.modelValue, props.link] as const,
  ([open, link]) => {
    if (!open || !link) return;
    form.value = {
      platform_id: link.platform_id,
      publish_account: link.publish_account ?? "",
      published_at: link.published_at ? new Date(link.published_at) : null,
      note: link.note ?? "",
    };
    errors.value = {};
    void loadPlatforms();
  },
  { immediate: true },
);

async function submit() {
  const link = props.link;
  if (!link) return;
  const errs: Record<string, string> = {};
  if (!form.value.published_at) errs.published_at = t("links.validate.publishedAtRequired");
  else {
    const e = checkPublishedAt(form.value.published_at);
    if (e) errs.published_at = e;
  }
  if (form.value.publish_account.trim().length > LINK_LIMITS.publishAccount) {
    errs.publish_account = t("links.validate.accountTooLong", { max: LINK_LIMITS.publishAccount });
  }
  if (form.value.note.trim().length > LINK_LIMITS.note) errs.note = t("links.validate.noteTooLong", { max: LINK_LIMITS.note });
  errors.value = errs;
  if (Object.keys(errs).length) return;

  const body: LinkUpdateBody = {};
  if (form.value.platform_id && form.value.platform_id !== link.platform_id) body.platform_id = form.value.platform_id;
  const account = form.value.publish_account.trim() || null;
  if (account !== (link.publish_account ?? null)) body.publish_account = account;
  const note = form.value.note.trim() || null;
  if (note !== (link.note ?? null)) body.note = note;
  const published = toUtcIso(form.value.published_at);
  if (published && published !== toUtcIso(link.published_at)) body.published_at = published;
  if (!Object.keys(body).length) {
    visible.value = false;
    return;
  }
  saving.value = true;
  try {
    const updated = await linksApi.update(link.id, body, { silent: true });
    ElMessage.success(t("common.saved"));
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
  <el-dialog v-model="visible" :title="t('links.edit.title')" width="min(560px, 96vw)" :close-on-click-modal="false" append-to-body>
    <el-form v-if="link" label-width="100px" @submit.prevent="submit">
      <el-form-item :label="t('links.url')">
        <span class="mono url">{{ link.url }}</span>
        <div class="form-hint">{{ t("links.edit.urlImmutable") }}</div>
      </el-form-item>
      <el-form-item :label="t('links.platform')" :error="errors.platform_id">
        <el-select v-model="form.platform_id" filterable style="width: 100%">
          <el-option v-for="p in platformOptions" :key="p.id" :value="p.id" :label="`${platformLabel(p)}（${p.code}）`" :disabled="!p.is_active" />
        </el-select>
        <div class="form-hint">{{ t("links.edit.platformHint") }}</div>
      </el-form-item>
      <el-form-item :label="t('links.publishAccount')" :error="errors.publish_account">
        <el-input v-model="form.publish_account" :maxlength="LINK_LIMITS.publishAccount" show-word-limit />
      </el-form-item>
      <el-form-item :label="t('links.publishedAt')" :error="errors.published_at" required>
        <el-date-picker v-model="form.published_at" type="datetime" :disabled-date="disabledPublishDate" :clearable="false" style="width: 100%" />
        <div class="form-hint">{{ t("links.edit.publishedAtHint") }}</div>
      </el-form-item>
      <el-form-item :label="t('links.note')" :error="errors.note">
        <el-input v-model="form.note" type="textarea" :rows="3" :maxlength="LINK_LIMITS.note" show-word-limit />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="saving" @click="submit">{{ t("common.save") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.url {
  word-break: break-all;
  line-height: 1.5;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
</style>
