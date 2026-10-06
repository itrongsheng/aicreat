<script setup lang="ts">
// 视频工作台（docs/10 §5.1、§5.2、§5.4、§7.2；docs/04 §6.13、§7.9、§8）：
// - 模式 Tab：文生视频 / 图生视频（input_reference）/ 首尾帧（first_frame_image_url + 可选 last_frame_image_url）/
//   参考素材（reference_image_urls / reference_video_urls / reference_audio_urls 至少一项）；只发送当前模式的字段；
// - 用途 inline / standalone（视频不能作封面）；inline 须选关联内容，standalone 时禁用并清空；
// - 通用参数：时长滑块（1 ~ max_duration，默认 default_duration）、分辨率（allowed_resolutions）、比例或尺寸（二选一）、
//   负向提示词（≤ 2000）、generate_audio、模型（modality=video）；1080p / 4k 或时长 > 10s 显示成本提示，所选模型按次计费时追加「按次计费」；
// - 提交返回 {asset_id, task_id, quota_warning?}；长任务卡片 useAssetTracker(15s)：进度、已用时 / 预算（now − created_at / poll_budget_seconds）、
//   progress ≥ 99「上游处理中」、expired「重试（先复查上游任务）」、取消二次确认（不退费）；完成后 <video controls> 播放；
// - 离开页面不影响任务，再次进入从 sessionStorage 恢复最近的卡片，也可在素材库按 kind=video 查看。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { Delete, VideoCamera } from "@element-plus/icons-vue";
import { ASPECT_RATIOS, type AiModelOption, type MediaAsset, type VideoGenerateBody, type VideoResolution } from "@aicreat/shared";
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

type Mode = "text" | "image" | "frames" | "reference";

const MODES: Mode[] = ["text", "image", "frames", "reference"];
const RECENT_KEY = "aicreat.media.recent.video";
const RECENT_MAX = 10;
const MAX_REFERENCE_VIDEOS = 3;
const MAX_REFERENCE_AUDIOS = 1;

const { t } = useI18n();
const route = useRoute();
const { has } = usePermission();
const { projectId, project, store: projectStore } = useProject();
const { media, load: loadRuntime } = useMediaRuntime();
const guard = useGenerateGuard();

const mode = ref<Mode>("text");
const sizeMode = ref<"ratio" | "size">("ratio");
const form = reactive({
  usage_type: "standalone" as VideoGenerateBody["usage_type"],
  content_id: null as number | null,
  prompt: "",
  negative_prompt: "",
  duration: 5,
  resolution: "720p" as VideoResolution,
  aspect_ratio: "16:9",
  size: "",
  input_reference: null as string | null,
  first_frame_image_url: null as string | null,
  last_frame_image_url: null as string | null,
  reference_image_urls: [] as string[],
  reference_video_urls: [] as string[],
  reference_audio_urls: [] as string[],
  generate_audio: false,
  model: null as string | null,
});
const modelOption = ref<AiModelOption | null>(null);
const localErrors = ref<Record<string, string>>({});
const invalidUrls = ref<string[]>([]);

const errors = computed<Record<string, string>>(() => ({ ...guard.fields.value, ...localErrors.value }));
const canGenerate = computed(() => has("media.videos.generate"));
const showModel = computed(() => has("ai.models.view"));
const needContent = computed(() => form.usage_type === "inline");
const ratioOptions = computed(() => {
  const list: string[] = [...ASPECT_RATIOS];
  if (!list.includes(media.value.video.default_aspect_ratio)) list.unshift(media.value.video.default_aspect_ratio);
  return list;
});
const costHint = computed(() => form.resolution === "1080p" || form.resolution === "4k" || form.duration > 10);

watch(
  () => form.usage_type,
  (usage) => {
    if (usage === "standalone") form.content_id = null;
    localErrors.value = {};
  },
);
watch(projectId, () => {
  if (!applyingQuery) form.content_id = null;
});
watch(mode, () => {
  localErrors.value = {};
  invalidUrls.value = [];
});

function onModelChange(_v: string | null, option: AiModelOption | null) {
  modelOption.value = option;
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

const tracker = useAssetTracker({ interval: 15000 });
const cards = computed(() => tracker.list.value.filter((e) => e.asset && !e.missing && e.asset.status !== "deleted"));

function saveRecent() {
  try {
    sessionStorage.setItem(RECENT_KEY, JSON.stringify(tracker.list.value.map((e) => e.id).slice(0, RECENT_MAX)));
  } catch {
    /* ignore */
  }
}

function onDeleted(id: number) {
  tracker.remove(id);
  saveRecent();
}

function clearResults() {
  tracker.clear();
  saveRecent();
}

const attachVisible = ref(false);
const attachAsset = ref<MediaAsset | null>(null);
function openAttach(a: MediaAsset) {
  attachAsset.value = a;
  attachVisible.value = true;
}
function onAttached() {
  if (attachAsset.value) void tracker.refresh(attachAsset.value.id);
}

// ---------- 提交 ----------
function validate(): boolean {
  const errs: Record<string, string> = {};
  if (needContent.value && !form.content_id) errs.content_id = t("media.form.contentRequired");
  if (!form.prompt.trim()) errs.prompt = t("media.form.promptRequired");
  if (form.prompt.length > 4000) errs.prompt = t("media.form.promptTooLong", { max: 4000 });
  if (form.negative_prompt.length > 2000) errs.negative_prompt = t("media.form.promptTooLong", { max: 2000 });
  if (sizeMode.value === "ratio" && !/^\d+:\d+$/.test(form.aspect_ratio.trim())) errs.aspect_ratio = t("media.form.ratioFormat");
  if (sizeMode.value === "size" && !/^\d+x\d+$/.test(form.size.trim())) errs.size = t("media.form.sizeFormat");
  if (mode.value === "image" && !form.input_reference) errs.input_reference = t("media.video.inputReferenceRequired");
  if (mode.value === "frames" && !form.first_frame_image_url) errs.first_frame_image_url = t("media.video.firstFrameRequired");
  if (
    mode.value === "reference" &&
    !form.reference_image_urls.length &&
    !form.reference_video_urls.length &&
    !form.reference_audio_urls.length
  ) {
    errs.reference_image_urls = t("media.video.referenceRequired");
  }
  localErrors.value = errs;
  return Object.keys(errs).length === 0;
}

function buildBody(): VideoGenerateBody {
  const body: VideoGenerateBody = {
    project_id: projectId.value,
    content_id: needContent.value ? form.content_id : null,
    usage_type: form.usage_type,
    prompt: form.prompt.trim(),
    negative_prompt: form.negative_prompt.trim() || null,
    duration: form.duration,
    resolution: form.resolution,
    generate_audio: form.generate_audio,
    model: showModel.value ? form.model || null : null,
  };
  if (sizeMode.value === "ratio") body.aspect_ratio = form.aspect_ratio.trim();
  else body.size = form.size.trim();
  if (mode.value === "image") body.input_reference = form.input_reference;
  if (mode.value === "frames") {
    body.first_frame_image_url = form.first_frame_image_url;
    if (form.last_frame_image_url) body.last_frame_image_url = form.last_frame_image_url;
  }
  if (mode.value === "reference") {
    if (form.reference_image_urls.length) body.reference_image_urls = [...form.reference_image_urls];
    if (form.reference_video_urls.length) body.reference_video_urls = [...form.reference_video_urls];
    if (form.reference_audio_urls.length) body.reference_audio_urls = [...form.reference_audio_urls];
  }
  return body;
}

async function submit() {
  if (!projectId.value || guard.cooldown.value > 0) return;
  guard.fields.value = {};
  invalidUrls.value = [];
  if (!validate()) return;
  guard.submitting.value = true;
  guard.notice.value = null;
  try {
    const res = await mediaApi.generateVideo(buildBody(), { silent: true });
    guard.quotaWarning.value = res.quota_warning ?? null;
    ElMessage.success(t("media.video.created", { id: res.asset_id }));
    await tracker.addById([res.asset_id], true);
    saveRecent();
  } catch (err) {
    const info = describeMediaGenerateError(err, "video");
    guard.notice.value = info.notice;
    guard.fields.value = info.fields;
    invalidUrls.value = info.invalidUrls;
  } finally {
    guard.submitting.value = false;
  }
}

function resetForm() {
  const v = media.value.video;
  form.prompt = "";
  form.negative_prompt = "";
  form.duration = v.default_duration;
  form.resolution = v.default_resolution;
  form.aspect_ratio = v.default_aspect_ratio;
  form.size = "";
  sizeMode.value = "ratio";
  form.input_reference = null;
  form.first_frame_image_url = null;
  form.last_frame_image_url = null;
  form.reference_image_urls = [];
  form.reference_video_urls = [];
  form.reference_audio_urls = [];
  form.generate_audio = v.generate_audio_default;
  form.model = null;
  modelOption.value = null;
  localErrors.value = {};
  invalidUrls.value = [];
  guard.reset();
}

// ---------- 路由预填 ----------
let applyingQuery = false;
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
  if (Number.isInteger(cid) && cid > 0) {
    form.usage_type = "inline";
    form.content_id = cid;
  }
  const m = q("mode");
  if (m && (MODES as string[]).includes(m)) mode.value = m as Mode;
  setTimeout(() => (applyingQuery = false), 0);
}

onMounted(async () => {
  await loadRuntime();
  resetForm();
  applyQuery();
  const recent = readRecent();
  if (recent.length) await tracker.addById(recent, false);
});

watch(
  () => route.query,
  (q2, old) => {
    if (route.name === "media-videos" && JSON.stringify(q2) !== JSON.stringify(old)) applyQuery();
  },
);
</script>

<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.mediaVideos") }}</h2>
        <el-tag v-if="media.zhiqi_mode === 'mock'" type="info" size="small">{{ t("common.mockMode") }}</el-tag>
      </div>
    </template>

    <el-empty v-if="!projectId" :description="t('generation.noProject')">
      <router-link v-if="has('content.projects.view')" to="/projects">
        <el-button type="primary">{{ t("generation.gotoProjects") }}</el-button>
      </router-link>
    </el-empty>

    <el-row v-else :gutter="20">
      <el-col :xs="24" :lg="11">
        <GenerateNotice :notice="guard.notice.value" :quota-warning="guard.quotaWarning.value" @close="guard.notice.value = null" />
        <el-tabs v-model="mode" class="mode-tabs">
          <el-tab-pane v-for="m in MODES" :key="m" :name="m" :label="t(`media.video.modes.${m}`)" />
        </el-tabs>
        <p class="text-secondary mode-desc">{{ t(`media.video.modeDesc.${mode}`) }}</p>

        <el-form label-width="112px" class="media-form" @submit.prevent>
          <el-form-item :label="t('media.fields.project')">
            <el-tag type="primary" effect="plain">{{ project?.name || `#${projectId}` }}</el-tag>
            <span class="text-secondary form-tip-inline">{{ t("media.form.projectTip") }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.usage')" required>
            <el-radio-group v-model="form.usage_type">
              <el-radio-button value="inline">{{ t("status.media_usage_type.inline") }}</el-radio-button>
              <el-radio-button value="standalone">{{ t("status.media_usage_type.standalone") }}</el-radio-button>
            </el-radio-group>
            <span class="text-secondary form-tip-inline">{{ t("media.video.noCover") }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.content')" :required="needContent" :error="errors.content_id">
            <ContentSelect v-model="form.content_id" :project-id="projectId" :disabled="!needContent" :placeholder="needContent ? '' : t('media.form.standaloneNoContent')" />
          </el-form-item>
          <el-form-item :label="t('media.fields.prompt')" required :error="errors.prompt">
            <el-input v-model="form.prompt" type="textarea" :rows="4" maxlength="4000" show-word-limit :placeholder="t('media.video.promptPlaceholder')" />
          </el-form-item>

          <!-- 模式专属 -->
          <el-form-item v-if="mode === 'image'" :label="t('media.fields.inputReference')" required :error="errors.input_reference">
            <ImageUpload v-model="form.input_reference" kind="image" :multiple="false" :live="media.zhiqi_mode === 'live'" :invalid-urls="invalidUrls" />
          </el-form-item>
          <template v-if="mode === 'frames'">
            <el-form-item :label="t('media.fields.firstFrame')" required :error="errors.first_frame_image_url">
              <ImageUpload v-model="form.first_frame_image_url" kind="image" :multiple="false" :live="media.zhiqi_mode === 'live'" :invalid-urls="invalidUrls" />
            </el-form-item>
            <el-form-item :label="t('media.fields.lastFrame')" :error="errors.last_frame_image_url">
              <ImageUpload v-model="form.last_frame_image_url" kind="image" :multiple="false" :live="media.zhiqi_mode === 'live'" :invalid-urls="invalidUrls" />
            </el-form-item>
          </template>
          <template v-if="mode === 'reference'">
            <el-form-item :label="t('media.fields.referenceImages')" :error="errors.reference_image_urls">
              <ImageUpload
                v-model="form.reference_image_urls"
                kind="image"
                :max="media.image.max_reference_images"
                :live="media.zhiqi_mode === 'live'"
                :invalid-urls="invalidUrls"
              />
            </el-form-item>
            <el-form-item :label="t('media.fields.referenceVideos')" :error="errors.reference_video_urls">
              <ImageUpload v-model="form.reference_video_urls" kind="video" :max="MAX_REFERENCE_VIDEOS" :live="media.zhiqi_mode === 'live'" :invalid-urls="invalidUrls" />
            </el-form-item>
            <el-form-item :label="t('media.fields.referenceAudios')" :error="errors.reference_audio_urls">
              <ImageUpload v-model="form.reference_audio_urls" kind="audio" :max="MAX_REFERENCE_AUDIOS" :live="media.zhiqi_mode === 'live'" :invalid-urls="invalidUrls" />
            </el-form-item>
          </template>

          <!-- 通用参数 -->
          <el-form-item :label="t('media.fields.duration')" :error="errors.duration">
            <el-slider v-model="form.duration" :min="1" :max="media.video.max_duration" :step="1" show-input :format-tooltip="(v: number) => t('media.card.seconds', { n: v })" class="duration-slider" />
          </el-form-item>
          <el-form-item :label="t('media.fields.resolution')" :error="errors.resolution">
            <el-select v-model="form.resolution" style="width: 140px">
              <el-option v-for="r in media.video.allowed_resolutions" :key="r" :value="r" :label="r" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('media.fields.frameSize')" :error="sizeMode === 'ratio' ? errors.aspect_ratio : errors.size">
            <el-radio-group v-model="sizeMode" size="small" class="size-mode">
              <el-radio-button value="ratio">{{ t("media.fields.aspectRatio") }}</el-radio-button>
              <el-radio-button value="size">{{ t("media.fields.size") }}</el-radio-button>
            </el-radio-group>
            <el-select v-if="sizeMode === 'ratio'" v-model="form.aspect_ratio" filterable allow-create default-first-option style="width: 140px">
              <el-option v-for="r in ratioOptions" :key="r" :value="r" :label="r" />
            </el-select>
            <el-input v-else v-model="form.size" :placeholder="t('media.video.sizePlaceholder')" style="width: 160px" />
            <span class="text-secondary form-tip-inline">{{ t("media.video.ratioOrSize") }}</span>
          </el-form-item>
          <el-form-item :label="t('media.fields.negativePrompt')" :error="errors.negative_prompt">
            <el-input v-model="form.negative_prompt" type="textarea" :rows="2" maxlength="2000" show-word-limit :placeholder="t('media.video.negativePlaceholder')" />
          </el-form-item>
          <el-form-item :label="t('media.fields.generateAudio')">
            <el-switch v-model="form.generate_audio" />
            <span class="text-secondary form-tip-inline">{{ t("media.video.audioTip") }}</span>
          </el-form-item>
          <el-form-item v-if="showModel" :label="t('media.fields.model')" :error="errors.model">
            <ModelSelect v-model="form.model" modality="video" allow-empty @change="onModelChange" />
            <div class="text-secondary form-tip">{{ t("media.form.modelTip") }}</div>
          </el-form-item>
          <el-form-item v-if="costHint || modelOption?.quota_type === 1">
            <el-alert type="warning" :closable="false" show-icon class="cost-alert">
              <template #title>
                {{ costHint ? t("media.video.costHint") : t("media.video.costHintBase") }}
                <template v-if="modelOption?.quota_type === 1">{{ t("media.video.perCall") }}</template>
              </template>
            </el-alert>
          </el-form-item>
          <el-form-item>
            <el-button
              v-if="canGenerate"
              type="primary"
              :icon="VideoCamera"
              :loading="guard.submitting.value"
              :disabled="guard.cooldown.value > 0"
              @click="submit"
            >
              {{ guard.cooldown.value > 0 ? t("generation.retryIn", { seconds: guard.cooldown.value }) : t("media.video.submit") }}
            </el-button>
            <el-button @click="resetForm">{{ t("common.reset") }}</el-button>
            <span v-if="!canGenerate" class="text-secondary form-tip-inline">{{ t("media.form.noGeneratePermission") }}</span>
          </el-form-item>
        </el-form>
      </el-col>

      <el-col :xs="24" :lg="13">
        <div class="results-head">
          <h3 class="results-title">{{ t("media.results.title", { n: cards.length }) }}</h3>
          <span class="text-secondary results-tip">{{ t("media.results.videoTip", { minutes: Math.round(media.video.poll_budget_seconds / 60) }) }}</span>
          <span class="spacer" />
          <router-link v-if="has('media.assets.view')" to="/media/assets?kind=video">
            <el-button link type="primary">{{ t("media.results.gotoAssets") }}</el-button>
          </router-link>
          <el-button v-if="cards.length" link :icon="Delete" @click="clearResults">{{ t("media.results.clear") }}</el-button>
        </div>
        <el-empty v-if="!cards.length" :description="t('media.results.empty')" :image-size="90" />
        <div v-else class="results-list">
          <AssetCard
            v-for="e in cards"
            :key="e.id"
            :asset="e.asset!"
            :task="e.taskKnown ? e.task : undefined"
            :budget-seconds="media.video.poll_budget_seconds"
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
.mode-tabs {
  margin-top: -8px;
}
.mode-desc {
  margin: 0 0 12px;
  font-size: 12px;
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
.size-mode {
  margin-right: 10px;
}
.duration-slider {
  width: 100%;
  max-width: 420px;
}
.cost-alert {
  padding: 4px 10px;
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
.results-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
</style>
