<script setup lang="ts">
// 「生成内容」对话框（docs/09 §8.1、§10.4、§10.5；docs/04 §6.11、§7.6）：只对已采用（adopted）标题可用，
// 传入的标题中未采用的被过滤并提示「请先采用标题」；`selectable` 时可从当前项目的已采用标题中挑选（≤ 20）。
// 字段 outline_first / target_word_count / include_faq / include_seo_meta / format 按 runtime 配置与项目默认预填；
// 模板下拉仅 has('content.prompt_templates.view')、模型下拉仅 has('ai.models.view') 时显示，否则请求不带 template_id / model（§10.8）。
// 提交 `POST /admin/contents/generate`，成功后发出 success（调用方跳转内容列表并按 batch_id 筛选）。
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { CONTENT_FORMAT, CONTENT_LIMITS, MAX_PAGE_SIZE, type ContentFormat, type ContentGenerateResult, type Project, type Title } from "@aicreat/shared";
import * as contentsApi from "@/api/contents";
import * as titlesApi from "@/api/titles";
import GenerateNotice from "@/components/GenerateNotice.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import TemplateSelect from "@/components/TemplateSelect.vue";
import { useGenerateGuard } from "@/composables/useGenerateGuard";
import { usePermission } from "@/composables/usePermission";
import { useRuntimeSettings } from "@/composables/useRuntimeSettings";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    projectId: number;
    project?: Project | null;
    /** 预选标题（标题页多选 / 行内「生成内容」） */
    titles?: Title[];
    /** 允许在对话框内挑选已采用标题（内容列表页） */
    selectable?: boolean;
  }>(),
  { project: null, titles: () => [], selectable: false },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "success", result: ContentGenerateResult): void;
}>();

const { t } = useI18n();
const { has } = usePermission();
const { generation, load: loadRuntime } = useRuntimeSettings();
const guard = useGenerateGuard();

const canTemplate = computed(() => has("content.prompt_templates.view"));
const canModel = computed(() => has("ai.models.view"));

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const form = reactive({
  title_ids: [] as number[],
  outline_first: true,
  target_word_count: 1500,
  include_faq: true,
  include_seo_meta: true,
  format: "markdown" as ContentFormat,
  template_id: null as number | null,
  model: null as string | null,
});

/** 可选标题（已采用）：传入的 + 挑选列表 */
const titleOptions = ref<Title[]>([]);
const loadingTitles = ref(false);
const skippedCount = ref(0);

async function loadAdoptedTitles() {
  if (!props.selectable || !props.projectId) return;
  loadingTitles.value = true;
  try {
    const res = await titlesApi.list({ project_id: props.projectId, status: "adopted", page: 1, page_size: MAX_PAGE_SIZE }, { silent: true });
    const known = new Map(titleOptions.value.map((x) => [x.id, x]));
    for (const item of res.items) known.set(item.id, item);
    titleOptions.value = [...known.values()];
  } catch {
    /* 保留已有选项 */
  } finally {
    loadingTitles.value = false;
  }
}

async function init() {
  guard.reset();
  const gen = await loadRuntime();
  const adopted = props.titles.filter((x) => x.status === "adopted");
  skippedCount.value = props.titles.length - adopted.length;
  titleOptions.value = [...adopted];
  form.title_ids = adopted.slice(0, CONTENT_LIMITS.titleIds).map((x) => x.id);
  form.outline_first = gen.content.outline_first;
  form.target_word_count = gen.content.target_word_count;
  form.include_faq = gen.content.include_faq;
  form.include_seo_meta = gen.content.include_seo_meta;
  form.format = props.project?.default_format ?? gen.content.default_format;
  form.template_id = null;
  form.model = null;
  void loadAdoptedTitles();
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) void init();
  },
);

const minWords = computed(() => generation.value.content.min_word_count);
const maxWords = computed(() => generation.value.content.max_word_count);

function titleLabel(id: number): string {
  return titleOptions.value.find((x) => x.id === id)?.title ?? `#${id}`;
}

async function submit() {
  if (!form.title_ids.length) {
    ElMessage.warning(t("contentGenerate.needTitles"));
    return;
  }
  if (form.title_ids.length > CONTENT_LIMITS.titleIds) {
    ElMessage.warning(t("contentGenerate.tooManyTitles", { max: CONTENT_LIMITS.titleIds }));
    return;
  }
  const res = await guard.run(() =>
    contentsApi.generate(
      {
        project_id: props.projectId,
        title_ids: [...form.title_ids],
        outline_first: form.outline_first,
        target_word_count: form.target_word_count,
        include_faq: form.include_faq,
        include_seo_meta: form.include_seo_meta,
        format: form.format,
        // 无权限时不传（§10.8）
        ...(canTemplate.value && form.template_id ? { template_id: form.template_id } : {}),
        ...(canModel.value && form.model ? { model: form.model } : {}),
      },
      { silent: true },
    ),
  );
  if (!res) return;
  ElMessage.success(t("contentGenerate.created", { count: res.content_ids.length }));
  if (res.quota_warning) {
    ElMessage.warning({
      message: t("generation.quotaWarning", {
        scope: t(`generation.quotaScope.${res.quota_warning.scope}`),
        percent: Math.round(res.quota_warning.percent),
        used: res.quota_warning.used,
        limit: res.quota_warning.limit,
      }),
      duration: 6000,
    });
  }
  visible.value = false;
  emit("success", res);
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('contentGenerate.title')" width="min(640px, 96vw)" :close-on-click-modal="false" append-to-body>
    <GenerateNotice :notice="guard.notice.value" :quota-warning="guard.quotaWarning.value" @close="guard.notice.value = null" />
    <el-alert v-if="skippedCount > 0" type="warning" :title="t('contentGenerate.adoptFirst', { count: skippedCount })" :closable="false" show-icon class="mb" />
    <el-form label-width="120px" @submit.prevent="submit">
      <el-form-item :label="t('contentGenerate.titles')" :error="guard.fields.value.title_ids" required>
        <el-select
          v-if="selectable"
          v-model="form.title_ids"
          multiple
          filterable
          :multiple-limit="CONTENT_LIMITS.titleIds"
          :loading="loadingTitles"
          :placeholder="t('contentGenerate.titlesPlaceholder')"
          style="width: 100%"
        >
          <el-option v-for="opt in titleOptions" :key="opt.id" :label="opt.title" :value="opt.id" />
        </el-select>
        <div v-else class="title-tags">
          <el-tag v-for="id in form.title_ids" :key="id" closable @close="form.title_ids = form.title_ids.filter((x) => x !== id)">
            {{ titleLabel(id) }}
          </el-tag>
          <span v-if="!form.title_ids.length" class="text-secondary">{{ t("contentGenerate.noAdopted") }}</span>
        </div>
        <div class="form-hint">{{ t("contentGenerate.titlesHint", { max: CONTENT_LIMITS.titleIds }) }}</div>
      </el-form-item>
      <el-form-item :label="t('contentGenerate.outlineFirst')">
        <el-switch v-model="form.outline_first" />
      </el-form-item>
      <el-form-item :label="t('contentGenerate.targetWordCount')" :error="guard.fields.value.target_word_count">
        <el-input-number v-model="form.target_word_count" :min="minWords" :max="maxWords" :step="100" controls-position="right" />
        <span class="form-hint inline">{{ t("contentGenerate.wordRange", { min: minWords, max: maxWords }) }}</span>
      </el-form-item>
      <el-form-item :label="t('contentGenerate.includeSeoMeta')">
        <el-switch v-model="form.include_seo_meta" />
      </el-form-item>
      <el-form-item :label="t('contentGenerate.includeFaq')">
        <el-switch v-model="form.include_faq" />
      </el-form-item>
      <el-form-item :label="t('contentGenerate.format')" :error="guard.fields.value.format">
        <el-radio-group v-model="form.format">
          <el-radio-button v-for="f in CONTENT_FORMAT" :key="f" :value="f">{{ t(`status.content_format.${f}`) }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="canTemplate" :label="t('generation.template')" :error="guard.fields.value.template_id">
        <TemplateSelect v-model="form.template_id" :kinds="['content', 'section']" :project-id="projectId" :project-defaults="project?.default_templates" />
        <div class="form-hint">{{ t("contentGenerate.templateHint") }}</div>
      </el-form-item>
      <el-form-item v-if="canModel" :label="t('generation.model')" :error="guard.fields.value.model">
        <ModelSelect v-model="form.model" modality="text" allow-empty />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="guard.submitting.value" :disabled="guard.cooldown.value > 0 || !form.title_ids.length" @click="submit">
        {{ guard.cooldown.value > 0 ? t("generation.retryIn", { seconds: guard.cooldown.value }) : t("contentGenerate.submit") }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.mb {
  margin-bottom: 12px;
}
.title-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  width: 100%;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.5;
}
.form-hint.inline {
  width: auto;
  margin-left: 8px;
}
</style>
