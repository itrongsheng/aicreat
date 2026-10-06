<script lang="ts">
// 参考素材输入（docs/10 §6.5、§7.1、§7.2、§7.4；docs/04 §6.14；docs/13 §7.4、§12.2）：
// - 上传到 POST /admin/uploads/image|video（需 system.upload.create，无权限时只显示 URL 输入框），返回 {asset_id,url,public}；
// - 粘贴公网 URL；可从素材库选择已上传的参考素材（AssetPicker，usage_type=reference&status=ready，不带 project_id）；
// - 真实模式下 public=false（或本地判断为非公网地址、或 4222 返回的 urls）标红并提示「zhiqiapi 无法读取该地址」；Mock 模式恒为公网；
// - 总后台处于用户视角（ownerId > 0）时提示上传的素材归属到本人（13 §12.2）。
// v-model：multiple=true 时为 string[]，否则为 string | null。kind=audio 只接受 URL 输入（本系统无音频上传）。

/** 上传接口返回的 public 标志（url → public），跨组件共享 */
const publicFlags = new Map<string, boolean>();

export function rememberPublic(url: string, isPublic: boolean): void {
  publicFlags.set(url, isPublic);
}

const PRIVATE_V4 = [/^127\./, /^10\./, /^192\.168\./, /^172\.(1[6-9]|2\d|3[01])\./, /^169\.254\./, /^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\./, /^0\./];

/** 本地可判断的非公网迹象（真实模式下提示用；以后端 4222 为准） */
export function looksNonPublic(raw: string): boolean {
  const known = publicFlags.get(raw);
  if (known !== undefined) return !known;
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return true;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return true;
  if (url.username || url.password) return true;
  if (url.port && url.port !== "80" && url.port !== "443") return true;
  const host = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (!host || host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local") || host.endsWith(".internal")) return true;
  if (/^[\d.]+$/.test(host)) return PRIVATE_V4.some((re) => re.test(host));
  if (host.includes(":")) return host === "::1" || host.startsWith("fc") || host.startsWith("fd") || host.startsWith("fe80");
  // 单标签主机名（如 compose 服务名 server）不可从公网解析
  return !host.includes(".");
}

/** 合法的 http(s) URL（长度 ≤ 1000，docs/10 §11.1） */
export function isHttpUrl(raw: string): boolean {
  if (!raw || raw.length > 1000) return false;
  try {
    const url = new URL(raw);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}
</script>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, type UploadRawFile, type UploadRequestOptions } from "element-plus";
import { Delete, DocumentCopy, FolderOpened, Headset, Upload, VideoCamera } from "@element-plus/icons-vue";
import { UPLOAD_LIMITS, type MediaAsset, type UploadResult } from "@aicreat/shared";
import * as uploadsApi from "@/api/uploads";
import AssetPicker from "@/components/AssetPicker.vue";
import { copyText } from "@/composables/useAssetActions";
import { usePermission } from "@/composables/usePermission";
import { useAuthStore } from "@/store/auth";
import { useProjectStore } from "@/store/project";

const props = withDefaults(
  defineProps<{
    modelValue: string[] | string | null | undefined;
    kind?: "image" | "video" | "audio";
    multiple?: boolean;
    /** 最多条数（multiple 时） */
    max?: number;
    /** 真实模式（undefined 时取登录态的 zhiqi_mode） */
    live?: boolean;
    /** 后端 4222 返回的非公网 URL（标红） */
    invalidUrls?: string[];
    allowUpload?: boolean;
    allowPicker?: boolean;
    disabled?: boolean;
    placeholder?: string;
  }>(),
  {
    kind: "image",
    multiple: true,
    max: 9,
    live: undefined,
    invalidUrls: () => [],
    allowUpload: true,
    allowPicker: true,
    disabled: false,
    placeholder: "",
  },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: string[] | string | null): void;
  (e: "uploaded", result: UploadResult): void;
}>();

const { t } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();
const projectStore = useProjectStore();

const urls = computed<string[]>(() => {
  const v = props.modelValue;
  if (Array.isArray(v)) return v.filter(Boolean);
  return v ? [v] : [];
});
const limit = computed(() => (props.multiple ? Math.max(1, props.max) : 1));
const full = computed(() => urls.value.length >= limit.value);
const isLive = computed(() => (props.live === undefined ? auth.zhiqiMode === "live" : props.live));

const canUpload = computed(() => props.allowUpload && props.kind !== "audio" && has("system.upload.create"));
const canPick = computed(() => props.allowPicker && props.kind !== "audio" && has("media.assets.view"));
const userViewHint = computed(() => canUpload.value && auth.isAllScope && projectStore.ownerId > 0);

function setUrls(next: string[]) {
  if (props.multiple) emit("update:modelValue", next);
  else emit("update:modelValue", next[0] ?? null);
}

function addUrls(list: string[]): number {
  const current = [...urls.value];
  let added = 0;
  for (const u of list) {
    if (!u || current.includes(u)) continue;
    if (current.length >= limit.value) {
      if (props.multiple) {
        ElMessage.warning(t("media.upload.maxReached", { max: limit.value }));
        break;
      }
      current.splice(0, current.length);
    }
    current.push(u);
    added += 1;
  }
  setUrls(current);
  return added;
}

function removeUrl(u: string) {
  setUrls(urls.value.filter((x) => x !== u));
}

function warnOf(u: string): "invalid" | "nonPublic" | null {
  if (props.invalidUrls.includes(u)) return "invalid";
  if (isLive.value && looksNonPublic(u)) return "nonPublic";
  return null;
}

// ---------- 粘贴 URL ----------
const urlInput = ref("");
function addTyped() {
  const raw = urlInput.value.trim();
  if (!raw) return;
  const parts = raw.split(/[\s,]+/).filter(Boolean);
  const bad = parts.filter((p) => !isHttpUrl(p));
  if (bad.length) {
    ElMessage.error(t("media.upload.invalidUrl"));
    return;
  }
  addUrls(parts);
  urlInput.value = "";
}

// ---------- 上传 ----------
const uploading = ref(false);
const percent = ref(0);

const EXTS: Record<"image" | "video", string[]> = {
  image: ["jpg", "jpeg", "png", "webp", "gif"],
  video: ["mp4", "mov"],
};
const accept = computed(() => (props.kind === "video" ? ".mp4,.mov,video/mp4,video/quicktime" : ".jpg,.jpeg,.png,.webp,.gif,image/*"));
const maxMb = computed(() => (props.kind === "video" ? UPLOAD_LIMITS.video_mb : UPLOAD_LIMITS.image_mb));

function beforeUpload(file: UploadRawFile): boolean {
  const kind = props.kind === "video" ? "video" : "image";
  const ext = (file.name.split(".").pop() ?? "").toLowerCase();
  if (!EXTS[kind].includes(ext)) {
    ElMessage.error(t("media.upload.badType", { types: EXTS[kind].join(" / ") }));
    return false;
  }
  if (file.size > maxMb.value * 1024 * 1024) {
    ElMessage.error(t("media.upload.tooLarge", { mb: maxMb.value }));
    return false;
  }
  if (full.value && props.multiple) {
    ElMessage.warning(t("media.upload.maxReached", { max: limit.value }));
    return false;
  }
  return true;
}

async function doUpload(options: UploadRequestOptions) {
  uploading.value = true;
  percent.value = 0;
  try {
    const fn = props.kind === "video" ? uploadsApi.uploadVideo : uploadsApi.uploadImage;
    const res = await fn(options.file, { onProgress: (p) => (percent.value = p) });
    rememberPublic(res.url, res.public);
    addUrls([res.url]);
    emit("uploaded", res);
    if (!res.public && isLive.value) ElMessage.warning(t("media.upload.notPublic"));
    else ElMessage.success(t("media.upload.uploaded", { id: res.asset_id }));
  } catch {
    /* 拦截器已提示（413 / 400 魔数不符 / 403） */
  } finally {
    uploading.value = false;
  }
}

// ---------- 从素材库选择 ----------
const pickerVisible = ref(false);
function onPicked(assets: MediaAsset[]) {
  addUrls(assets.map((a) => a.url).filter((u): u is string => !!u));
}

const iconOf = computed(() => (props.kind === "video" ? VideoCamera : Headset));
</script>

<template>
  <div class="image-upload">
    <el-alert v-if="userViewHint" type="info" :closable="false" show-icon :title="t('media.upload.userViewHint')" class="image-upload__hint" />

    <div v-if="urls.length" class="image-upload__list">
      <div v-for="u in urls" :key="u" class="image-upload__item" :class="{ 'is-warn': warnOf(u) }">
        <el-image v-if="kind === 'image'" :src="u" fit="cover" class="image-upload__thumb" :preview-src-list="[u]" preview-teleported lazy>
          <template #error><div class="image-upload__thumb image-upload__thumb--empty">?</div></template>
        </el-image>
        <div v-else class="image-upload__thumb image-upload__thumb--empty">
          <el-icon><component :is="iconOf" /></el-icon>
        </div>
        <div class="image-upload__meta">
          <el-tooltip :content="u" placement="top" :show-after="400">
            <span class="image-upload__url mono">{{ u }}</span>
          </el-tooltip>
          <span v-if="warnOf(u) === 'invalid'" class="image-upload__warn">{{ t("media.upload.rejected") }}</span>
          <span v-else-if="warnOf(u) === 'nonPublic'" class="image-upload__warn">{{ t("media.upload.notPublic") }}</span>
        </div>
        <div class="image-upload__ops">
          <el-button link :icon="DocumentCopy" :title="t('common.copy')" @click="copyText(u)" />
          <el-button link type="danger" :icon="Delete" :disabled="disabled" :title="t('common.delete')" @click="removeUrl(u)" />
        </div>
      </div>
    </div>

    <div v-if="!full || !multiple" class="image-upload__bar">
      <el-input
        v-model="urlInput"
        :disabled="disabled"
        clearable
        :placeholder="placeholder || t(`media.upload.urlPlaceholder.${kind}`)"
        class="image-upload__input"
        @keyup.enter="addTyped"
      >
        <template #append>
          <el-button :disabled="disabled || !urlInput.trim()" @click="addTyped">{{ t("media.upload.addUrl") }}</el-button>
        </template>
      </el-input>
      <el-upload
        v-if="canUpload"
        :show-file-list="false"
        :accept="accept"
        :multiple="false"
        :disabled="disabled || uploading"
        :before-upload="beforeUpload"
        :http-request="doUpload"
      >
        <el-button :icon="Upload" :loading="uploading" :disabled="disabled">
          {{ uploading ? `${percent}%` : t(kind === "video" ? "media.upload.uploadVideo" : "media.upload.uploadImage") }}
        </el-button>
      </el-upload>
      <el-button v-if="canPick" :icon="FolderOpened" :disabled="disabled" @click="pickerVisible = true">{{ t("media.upload.pick") }}</el-button>
    </div>
    <div class="image-upload__foot text-secondary">
      <span v-if="multiple">{{ t("media.upload.count", { n: urls.length, max: limit }) }}</span>
      <span v-if="canUpload">{{ t("media.upload.limits", { types: (kind === "video" ? EXTS.video : EXTS.image).join(" / "), mb: maxMb }) }}</span>
      <span v-else-if="kind !== 'audio' && allowUpload">{{ t("media.upload.noUploadPermission") }}</span>
      <span v-if="kind === 'audio'">{{ t("media.upload.audioUrlOnly") }}</span>
    </div>

    <AssetPicker
      v-if="canPick"
      v-model="pickerVisible"
      mode="reference"
      :kind="kind === 'video' ? 'video' : 'image'"
      :multiple="multiple"
      :max="multiple ? Math.max(1, limit - urls.length) : 1"
      @select="onPicked"
    />
  </div>
</template>

<style scoped>
.image-upload {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
  min-width: 0;
}
.image-upload__hint {
  padding: 4px 10px;
}
.image-upload__list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.image-upload__item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  min-width: 0;
}
.image-upload__item.is-warn {
  border-color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.image-upload__thumb {
  width: 44px;
  height: 44px;
  flex: none;
  border-radius: 4px;
  overflow: hidden;
}
.image-upload__thumb--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 18px;
}
.image-upload__meta {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
  line-height: 1.4;
}
.image-upload__url {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.image-upload__warn {
  color: var(--el-color-danger);
  font-size: 12px;
}
.image-upload__ops {
  display: flex;
  flex: none;
}
.image-upload__bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.image-upload__input {
  flex: 1;
  min-width: 220px;
}
.image-upload__foot {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  line-height: 1.4;
}
</style>
