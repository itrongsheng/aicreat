<script setup lang="ts">
// 图片工作台（docs/10 §4.1、§4.2、§4.11、§7.1；docs/04 §6.13、§7.8、§8）：
// - 左侧表单：项目（顶栏 ProjectSelect，只读显示）、关联内容（可搜索，用途 standalone 时禁用并清空）、用途 cover / inline / standalone
//   （决定推荐比例：封面 16:9、配图 4:3）、提示词（≤ 4000）+「由文章生成提示词」（from_content_prompt，须选内容，standalone 时禁用）、
//   张数 1~max_count_per_request、分辨率 / 比例（media_config.image.allowed_*，比例旁等比预览框，2k / 4k 成本提示）、
//   参考图（ImageUpload：上传 / 粘贴 URL / AssetPicker，≤ max_reference_images，真实模式非公网标红）、模型覆盖（ModelSelect modality=image）；
// - 提交 POST /admin/media/images/generate → {asset_ids[], task_ids[], quota_warning?}；quota_warning 在表单顶部黄色提示；
//   错误映射：400 banned_words（按 loc 高亮）/ 400 data.model / 4222 / 4291 / 429 / 5031 / 409；
// - 右侧结果：每个资产一张 AssetCard，useAssetTracker(5s) 先轮询 /task，根任务终态后轮询资产详情直到 ready / failed / expired；
// - 路由参数 content_id / project_id / usage_type / from_content_prompt 预填（内容编辑器「生成配图 / 生成封面」入口，§7.5）。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Delete, MagicStick } from "@element-plus/icons-vue";
import type { AspectRatio, ImageGenerateBody, ImageResolution, MediaAsset } from "@aicreat/shared";
import * as mediaApi from "@/api/media";
import AssetAttachDialog from "@/components/AssetAttachDialog.vue";
import AssetCard from "@/components/AssetCard.vue";
import ContentSelect from "@/components/ContentSelect.vue";
import GenerateNotice from "@/components/GenerateNotice.vue";
import ImageUpload from "@/components/ImageUpload.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import { describeMediaGenerateError } from "@/composables/useAssetActions";
import { useAssetTracker } from "@/composables/useAssetTracker";
import { useGenerateGuard } from "@/composables/useGenerateGuard";
import { usePermission } from "@/composables/usePermission";
import { useProject } from "@/composables/useProject";
import { useMediaRuntime } from "@/composables/useRuntimeSettings";

type Usage = ImageGenerateBody["usage_type"];

const RECENT_KEY = "aicreat.media.recent.image";
const RECENT_MAX = 20;
/** 按用途的推荐比例（docs/10 §4.2） */
const RECOMMENDED_RATIO: Partial<Record<Usage, AspectRatio>> = { cover: "16:9", inline: "4:3" };

const { t } = useI18n();
const route = useRoute();
const { has } = usePermission();
const { projectId, project, store: projectStore } = useProject();
const { media, load: loadRuntime } = useMediaRuntime();
const guard = useGenerateGuard();

const form = reactive({
  content_id: null as number | null,
  usage_type: "standalone" as Usage,
  prompt: "",
  from_content_prompt: false,
  count: 1,
  resolution: "1080p" as ImageResolution,
  aspect_ratio: "16:9" as AspectRatio,
  reference_image_urls: [] as string[],
  model: null as string | null,
});
const localErrors = ref<Record<string, string>>({});
const invalidUrls = ref<string[]>([]);
/** 用户手动改过比例后，切换用途不再覆盖 */
let ratioTouched = false;
let applyingQuery = false;

const errors = computed<Record<string, string>>(() => ({ ...guard.fields.value, ...localErrors.value }));
const canGenerate = computed(() => has("media.images.generate"));
const showModel = computed(() => has("ai.models.view"));
const needContent = computed(() => form.usage_type !== "standalone");
const costHint = computed(() => form.resolution === "2k" || form.resolution === "4k");

function applyRecommendedRatio() {
  const rec = RECOMMENDED_RATIO[form.usage_type];
  if (rec && media.value.image.allowed_aspect_ratios.includes(rec)) form.aspect_ratio = rec;
}

watch(
  () => form.usage_type,
  (usage) => {
    if (usage === "standalone") {
      form.content_id = null;
      form.from_content_prompt = false;
    }
    if (!ratioTouched && !applyingQuery) applyRecommendedRatio();
    localErrors.value = {};
  },
);

watch(
  () => form.from_content_prompt,
  () => {
    const { prompt: _p, content_id: _c, ...rest } = localErrors.value;
    localErrors.value = rest;
  },
);

// 切换顶栏项目：关联内容属于旧项目，清空
watch(projectId, () => {
  if (!applyingQuery) form.content_id = null;
});

/** 比例预览框（最长边 44px） */
const ratioBox = computed(() => {
  const [w, h] = form.aspect_ratio.split(":").map(Number);
  const scale = 44 / Math.max(w || 1, h || 1);
  return { width: `${Math.round((w || 1) * scale)}px`, height: `${Math.round((h || 1) * scale)}px` };
});

function onRatioChange() {
  ratioTouched = true;
}

// ---------- 结果区 ----------
function readRecent(): number[] {
  try {
    const raw = JSON.parse(sessionStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((n) => Number.isInteger(n) && n > 0).slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

function saveRecent() {
  try {
    sessionStorage.setItem(RECENT_KEY, JSON.stringify(tracker.list.value.map((e) => e.id).slice(0, RECENT_MAX)));
  } catch {
    /* ignore */
  }
}

const tracker = useAssetTracker({ interval: 5000 });
const cards = computed(() => tracker.list.value.filter((e) => e.asset && !e.missing && e.asset.status !== "deleted"));

function onDeleted(id: number) {
  tracker.remove(id);
  saveRecent();
}

function clearResults() {
  tracker.clear();
  saveRecent();
}

// 绑定到内容
const attachVisible = ref(false);
const attachAsset = ref<MediaAsset | null>(null);
function openAttach(a: MediaAsset) {
  attachAsset.value = a;
  attachVisible.value = true;
}
function onAttached(_a: MediaAsset) {
  if (attachAsset.value) void tracker.refresh(attachAsset.value.id);
}

// ---------- 提交 ----------
function validate(): boolean {
  const errs: Record<string, string> = {};
  if (needContent.value && !form.content_id) errs.content_id = t("media.form.contentRequired");
  if (form.from_content_prompt && !form.content_id) errs.content_id = t("media.form.contentRequiredForPrompt");
  if (!form.from_content_prompt && !form.prompt.trim()) errs.prompt = t("media.form.promptRequired");
  if (form.prompt.length > 4000) errs.prompt = t("media.form.promptTooLong", { max: 4000 });
  if (form.reference_image_urls.length > media.value.image.max_reference_images) {
    errs.reference_image_urls = t("media.form.tooManyRefs", { max: media.value.image.max_reference_images });
  }
  localErrors.value = errs;
  return Object.keys(errs).length === 0;
}

async function submit() {
  if (!projectId.value || guard.cooldown.value > 0) return;
  guard.fields.value = {};
  invalidUrls.value = [];
  if (!validate()) return;
  const body: ImageGenerateBody = {
    project_id: projectId.value,
    content_id: needContent.value ? form.content_id : null,
    usage_type: form.usage_type,
    prompt: form.prompt.trim() || null,
    from_content_prompt: form.from_content_prompt,
    count: form.count,
    resolution: form.resolution,
    aspect_ratio: form.aspect_ratio,
    reference_image_urls: [...form.reference_image_urls],
    model: showModel.value ? form.model || null : null,
  };
  guard.submitting.value = true;
  guard.notice.value = null;
  try {
    const res = await mediaApi.generateImages(body, { silent: true });
    guard.quotaWarning.value = res.quota_warning ?? null;
    ElMessage.success(t("media.image.created", { n: res.asset_ids.length }));
    await tracker.addById(res.asset_ids, true);
    saveRecent();
  } catch (err) {
    const info = describeMediaGenerateError(err, "image");
    guard.notice.value = info.notice;
    guard.fields.value = info.fields;
    invalidUrls.value = info.invalidUrls;
  } finally {
    guard.submitting.value = false;
  }
}

function resetForm() {
  form.prompt = "";
  form.count = 1;
  form.reference_image_urls = [];
  form.model = null;
  form.resolution = media.value.image.default_resolution;
  ratioTouched = false;
  form.aspect_ratio = media.value.image.default_aspect_ratio;
  applyRecommendedRatio();
  localErrors.value = {};
  guard.reset();
  invalidUrls.value = [];
}

// ---------- 路由预填 ----------
function q(key: string): string | undefined {
  const v = route.query[key];
  const s = Array.isArray(v) ? v[0] : v;
  return typeof s === "string" && s !== "" ? s : undefined;
}

function applyQuery() {
  applyingQuery = true;
  const pid = Number(q("project_id"));
  if (Number.isInteger(pid) && pid > 0 && pid !== projectStore.currentId) projectStore.setCurrent(pid);
  const cid = Number(q("content_id"));
  const usage = q("usage_type");
  if (usage === "cover" || usage === "inline" || usage === "standalone") form.usage_type = usage;
  if (Number.isInteger(cid) && cid > 0) {
    if (form.usage_type === "standalone") form.usage_type = usage === "cover" ? "cover" : "inline";
    form.content_id = cid;
    // 内容编辑器入口默认开启「由文章生成提示词」（docs/10 §7.5）
    const fcp = q("from_content_prompt");
    form.from_content_prompt = fcp === undefined ? true : fcp === "1" || fcp === "true";
  }
  applyRecommendedRatio();
  // 让 watch 回调在本轮之后执行时仍看到 applyingQuery
  setTimeout(() => (applyingQuery = false), 0);
}

onMounted(async () => {
  const rt = await loadRuntime();
  form.resolution = rt.image.default_resolution;
  form.aspect_ratio = rt.image.default_aspect_ratio;
  form.count = Math.min(form.count, rt.image.max_count_per_request);
  applyQuery();
  const recent = readRecent();
  if (recent.length) await tracker.addById(recent, false);
});

watch(
  () => route.query,
  (q2, old) => {
    if (route.name === "media-images" && JSON.stringify(q2) !== JSON.stringify(old)) applyQuery();
  },
);
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.mediaImages") }}</h2>
        <el-tag v-if="media.zhiqi_mode === 'mock'" type="info" size="small">{{ t("common.mockMode") }}</el-tag>
      </div>
    </template>

    <el-empty v-if="!projectId" :description="t('generation.noProject')">
      <router-link v-if="has('content.projects.view')" to="/projects">
        <el-button type="primary">{{ t("generation.gotoProjects") }}</el-button>
      </router-link>
    </el-empty>

    <el-row v-else :gutter="20">
      <el-col :xs="24" :lg="10">
        <GenerateNotice :notice="guard.notice.value" :quota-warning="guard.quotaWarning.value" @close="guard.notice.value = null" />
        <el-form label-width="104px" class="media-form" @submit.prevent>
          <el-form-item :label="t('media.fields.project')">
            <el-tag type="primary" effect="plain">{{ project?.name || `#${projectId}` }}</el-tag>
            <span class="text-secondary form-tip-inline">{{ t("media.form.projectTip") }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.usage')" required>
            <el-radio-group v-model="form.usage_type">
              <el-radio-button value="cover">{{ t("status.media_usage_type.cover") }}</el-radio-button>
              <el-radio-button value="inline">{{ t("status.media_usage_type.inline") }}</el-radio-button>
              <el-radio-button value="standalone">{{ t("status.media_usage_type.standalone") }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="t('media.fields.content')" :required="needContent" :error="errors.content_id">
            <ContentSelect v-model="form.content_id" :project-id="projectId" :disabled="!needContent" :placeholder="needContent ? '' : t('media.form.standaloneNoContent')" />
          </el-form-item>
          <el-form-item :label="t('media.fields.prompt')" :required="!form.from_content_prompt" :error="errors.prompt">
            <el-input
              v-model="form.prompt"
              type="textarea"
              :rows="5"
              maxlength="4000"
              show-word-limit
              :placeholder="form.from_content_prompt ? t('media.form.promptFromContent') : t('media.form.promptPlaceholder')"
            />
            <div class="switch-row">
              <el-switch v-model="form.from_content_prompt" :disabled="form.usage_type === 'standalone'" />
              <span>{{ t("media.fields.fromContentPrompt") }}</span>
              <span class="text-secondary form-tip-inline">{{ form.usage_type === "standalone" ? t("media.form.fromContentDisabled") : t("media.form.fromContentTip") }}</span>
            </div>
          </el-form-item>
          <el-form-item :label="t('media.fields.count')" :error="errors.count">
            <el-input-number v-model="form.count" :min="1" :max="media.image.max_count_per_request" :step="1" step-strictly />
            <span class="text-secondary form-tip-inline">{{ t("media.form.countTip", { max: media.image.max_count_per_request }) }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.resolution')" :error="errors.resolution">
            <el-select v-model="form.resolution" style="width: 140px">
              <el-option v-for="r in media.image.allowed_resolutions" :key="r" :value="r" :label="r" />
            </el-select>
            <span v-if="costHint" class="cost-hint">{{ t("media.form.imageCostHint") }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.aspectRatio')" :error="errors.aspect_ratio">
            <el-select v-model="form.aspect_ratio" style="width: 140px" @change="onRatioChange">
              <el-option v-for="r in media.image.allowed_aspect_ratios" :key="r" :value="r" :label="r" />
            </el-select>
            <span class="ratio-box-wrap"><span class="ratio-box" :style="ratioBox" /></span>
            <span class="text-secondary form-tip-inline">{{ t("media.form.ratioTip") }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.referenceImages')" :error="errors.reference_image_urls">
            <ImageUpload
              v-model="form.reference_image_urls"
              kind="image"
              :max="media.image.max_reference_images"
              :live="media.zhiqi_mode === 'live'"
              :invalid-urls="invalidUrls"
            />
          </el-form-item>
          <el-form-item v-if="showModel" :label="t('media.fields.model')" :error="errors.model">
            <ModelSelect v-model="form.model" modality="image" allow-empty />
            <div class="text-secondary form-tip">{{ t("media.form.modelTip") }}</div>
          </el-form-item>
          <el-form-item>
            <el-button
              v-if="canGenerate"
              type="primary"
              :icon="MagicStick"
              :loading="guard.submitting.value"
              :disabled="guard.cooldown.value > 0"
              @click="submit"
            >
              {{ guard.cooldown.value > 0 ? t("generation.retryIn", { seconds: guard.cooldown.value }) : t("media.image.submit") }}
            </el-button>
            <el-button @click="resetForm">{{ t("common.reset") }}</el-button>
            <span v-if="!canGenerate" class="text-secondary form-tip-inline">{{ t("media.form.noGeneratePermission") }}</span>
          </el-form-item>
        </el-form>
      </el-col>

      <el-col :xs="24" :lg="14">
        <div class="results-head">
          <h3 class="results-title">{{ t("media.results.title", { n: cards.length }) }}</h3>
          <span class="text-secondary results-tip">{{ t("media.results.imageTip") }}</span>
          <span class="spacer" />
          <router-link v-if="has('media.assets.view')" to="/media/assets?kind=image">
            <el-button link type="primary">{{ t("media.results.gotoAssets") }}</el-button>
          </router-link>
          <el-button v-if="cards.length" link :icon="Delete" @click="clearResults">{{ t("media.results.clear") }}</el-button>
        </div>
        <el-empty v-if="!cards.length" :description="t('media.results.empty')" :image-size="90" />
        <div v-else class="results-grid">
          <AssetCard
            v-for="e in cards"
            :key="e.id"
            :asset="e.asset!"
            :task="e.taskKnown ? e.task : undefined"
            @changed="(id: number) => tracker.refresh(id)"
            @deleted="onDeleted"
            @attach="openAttach"
          />
        </div>
      </el-col>
    </el-row>

    <AssetAttachDialog v-model="attachVisible" :asset="attachAsset" :content-id="form.content_id" @attached="onAttached" />
  </el-card>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  gap: 8px;
}
.media-form :deep(.el-form-item__content) {
  flex-wrap: wrap;
  gap: 6px 0;
}
.form-tip-inline {
  margin-left: 10px;
  font-size: 12px;
  line-height: 1.5;
}
.form-tip {
  width: 100%;
  font-size: 12px;
  line-height: 1.5;
}
.switch-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  width: 100%;
  margin-top: 6px;
}
.switch-row .form-tip-inline {
  margin-left: 0;
}
.cost-hint {
  margin-left: 10px;
  color: var(--el-color-warning);
  font-size: 12px;
}
.ratio-box-wrap {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 48px;
  margin-left: 10px;
}
.ratio-box {
  display: inline-block;
  border: 2px solid var(--el-color-primary);
  border-radius: 3px;
  background: var(--el-color-primary-light-9);
}
.results-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.results-head .spacer {
  flex: 1;
}
.results-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
}
.results-tip {
  font-size: 12px;
}
.results-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}
</style>
