<script setup lang="ts">
// 手工创建内容草稿（docs/09 §10.5；docs/04 §6.11 `POST /admin/contents`）：标题（必填）、关联标题 / 关键词（可选）、格式、风格、正文（可空）。
// 选择关联标题时带出标题文本、关键词与风格；带正文时后端建版本 `source=manual`。成功后发出 created（调用方跳转编辑器）。
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { CONTENT_FORMAT, CONTENT_STYLE, MAX_PAGE_SIZE, type Content, type ContentFormat, type ContentStyle, type Keyword, type Project, type Title } from "@aicreat/shared";
import { fieldErrors, isApiError } from "@/api/client";
import * as contentsApi from "@/api/contents";
import * as keywordsApi from "@/api/keywords";
import * as titlesApi from "@/api/titles";
import MarkdownEditor from "@/components/MarkdownEditor.vue";
import { useRuntimeSettings } from "@/composables/useRuntimeSettings";

const TITLE_MAX = 200;

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    projectId: number;
    project?: Project | null;
  }>(),
  { project: null },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "created", content: Content): void;
  (e: "cancel"): void;
}>();

const { t } = useI18n();
const { load: loadRuntime } = useRuntimeSettings();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const form = reactive({
  title: "",
  title_id: null as number | null,
  keyword_id: null as number | null,
  format: "markdown" as ContentFormat,
  style: "news" as ContentStyle,
  body: "",
});
const errors = ref<Record<string, string>>({});
const saving = ref(false);
const titles = ref<Title[]>([]);
const keywords = ref<Keyword[]>([]);
/** 本次打开是否已创建成功（关闭时据此区分取消） */
let createdOk = false;

async function loadOptions() {
  if (!props.projectId) return;
  try {
    const [t1, k1, k2] = await Promise.all([
      titlesApi.list({ project_id: props.projectId, status: "adopted", page: 1, page_size: MAX_PAGE_SIZE }, { silent: true }),
      keywordsApi.list({ project_id: props.projectId, status: "adopted", sort: "score", page: 1, page_size: MAX_PAGE_SIZE }, { silent: true }),
      keywordsApi.list({ project_id: props.projectId, status: "candidate", sort: "score", page: 1, page_size: MAX_PAGE_SIZE }, { silent: true }),
    ]);
    titles.value = t1.items;
    keywords.value = [...k1.items, ...k2.items];
  } catch {
    /* 关联项为可选，失败时只能手填 */
  }
}

async function init() {
  createdOk = false;
  const gen = await loadRuntime();
  form.title = "";
  form.title_id = null;
  form.keyword_id = null;
  form.format = props.project?.default_format ?? gen.content.default_format;
  form.style = props.project?.default_style ?? gen.title.default_style;
  form.body = "";
  errors.value = {};
  void loadOptions();
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) void init();
  },
  { immediate: true },
);

function onTitlePicked(id: number | null) {
  const picked = titles.value.find((x) => x.id === id);
  if (!picked) return;
  form.title = picked.title;
  form.keyword_id = picked.keyword_id;
  form.style = picked.style;
}

async function submit() {
  const title = form.title.trim();
  errors.value = {};
  if (!title) errors.value.title = t("contents.create.titleRequired");
  else if (title.length > TITLE_MAX) errors.value.title = t("contents.create.titleTooLong", { max: TITLE_MAX });
  if (Object.keys(errors.value).length) return;
  saving.value = true;
  try {
    const created = await contentsApi.create(
      {
        project_id: props.projectId,
        title,
        title_id: form.title_id || null,
        keyword_id: form.keyword_id || null,
        format: form.format,
        style: form.style,
        body: form.body.trim() ? form.body : null,
      },
      { silent: true },
    );
    ElMessage.success(t("contents.create.created"));
    createdOk = true;
    visible.value = false;
    emit("created", created);
  } catch (err) {
    if (isApiError(err)) {
      errors.value = fieldErrors(err);
      ElMessage.error(err.message);
    }
  } finally {
    saving.value = false;
  }
}

function cancel() {
  visible.value = false;
}

function onClosed() {
  if (!createdOk) emit("cancel");
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('contents.create.title')" width="min(860px, 96vw)" :close-on-click-modal="false" append-to-body @closed="onClosed">
    <el-form label-width="96px" @submit.prevent="submit">
      <el-form-item :label="t('contents.create.linkTitle')" :error="errors.title_id">
        <el-select v-model="form.title_id" filterable clearable :placeholder="t('contents.create.linkTitlePlaceholder')" style="width: 100%" @change="onTitlePicked">
          <el-option v-for="opt in titles" :key="opt.id" :value="opt.id" :label="opt.title" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('contents.create.contentTitle')" :error="errors.title" required>
        <el-input v-model="form.title" :maxlength="TITLE_MAX" show-word-limit />
      </el-form-item>
      <el-form-item :label="t('contents.create.linkKeyword')" :error="errors.keyword_id">
        <el-select v-model="form.keyword_id" filterable clearable :placeholder="t('contents.create.linkKeywordPlaceholder')" style="width: 100%">
          <el-option v-for="kw in keywords" :key="kw.id" :value="kw.id" :label="kw.keyword" />
        </el-select>
      </el-form-item>
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item :label="t('contents.format')" :error="errors.format">
            <el-radio-group v-model="form.format">
              <el-radio-button v-for="f in CONTENT_FORMAT" :key="f" :value="f">{{ t(`status.content_format.${f}`) }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="t('contents.style')" :error="errors.style">
            <el-select v-model="form.style" style="width: 100%">
              <el-option v-for="s in CONTENT_STYLE" :key="s" :value="s" :label="t(`status.content_style.${s}`)" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item :label="t('contents.create.body')" :error="errors.body">
        <MarkdownEditor v-model="form.body" :format="form.format" :rows="12" :placeholder="t('contents.create.bodyPlaceholder')" style="width: 100%" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="cancel">{{ t("common.cancel") }}</el-button>
      <el-button type="primary" :loading="saving" @click="submit">{{ t("contents.create.submit") }}</el-button>
    </template>
  </el-dialog>
</template>
