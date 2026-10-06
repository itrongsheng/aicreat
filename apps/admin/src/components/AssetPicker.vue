<script setup lang="ts">
// 素材选择弹窗（docs/10 §7.4、§7.5；docs/13 §7.4）：只列 status=ready 的素材。
// - mode=content（内容编辑器）：默认 GET /admin/media/assets?project_id=&kind=&status=ready；可切到「上传的参考素材」
//   （usage_type=reference&status=ready，不带 project_id——上传素材 project_id 为空，attach 时写入内容的项目）；
//   withUsage 时底部选择用途（cover 仅图片单选，视频只能 inline）；
// - mode=reference（参考图 / 参考视频）：usage_type=reference&status=ready，不带 project_id；普通用户只看到自己上传的素材。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { Refresh, VideoCamera } from "@element-plus/icons-vue";
import type { MediaAsset, MediaKind } from "@aicreat/shared";
import * as mediaApi from "@/api/media";
import StatusTag from "@/components/StatusTag.vue";
import { formatBytes, formatDateTime } from "@/utils/format";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    mode?: "content" | "reference";
    /** content 模式的项目 */
    projectId?: number | null;
    /** 固定素材类型；不传时可筛选 */
    kind?: MediaKind | null;
    multiple?: boolean;
    max?: number;
    /** content 模式：底部选择用途 cover / inline */
    withUsage?: boolean;
    defaultUsage?: "cover" | "inline";
    /** 不可选的素材（如已绑定到当前内容） */
    excludeIds?: number[];
    title?: string;
  }>(),
  {
    mode: "reference",
    projectId: null,
    kind: null,
    multiple: false,
    max: 9,
    withUsage: false,
    defaultUsage: "inline",
    excludeIds: () => [],
    title: "",
  },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "select", assets: MediaAsset[], usage: "cover" | "inline"): void;
}>();

const { t } = useI18n();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const scope = ref<"project" | "uploads">("project");
const kindFilter = ref<MediaKind | undefined>(undefined);
const page = ref(1);
const pageSize = 20;
const total = ref(0);
const rows = ref<MediaAsset[]>([]);
const loading = ref(false);
const picked = ref<MediaAsset[]>([]);
const usage = ref<"cover" | "inline">(props.defaultUsage);
let seq = 0;

const effectiveKind = computed<MediaKind | undefined>(() => props.kind ?? kindFilter.value);
const referenceScope = computed(() => props.mode === "reference" || scope.value === "uploads" || !props.projectId);

async function load() {
  const my = ++seq;
  loading.value = true;
  try {
    const res = await mediaApi.listAssets(
      referenceScope.value
        ? { usage_type: "reference", status: "ready", kind: effectiveKind.value, page: page.value, page_size: pageSize }
        : { project_id: props.projectId ?? undefined, status: "ready", kind: effectiveKind.value, page: page.value, page_size: pageSize },
      { silent: true },
    );
    if (my !== seq) return;
    rows.value = res.items;
    total.value = res.total;
  } catch {
    if (my !== seq) return;
    rows.value = [];
    total.value = 0;
  } finally {
    if (my === seq) loading.value = false;
  }
}

function reload() {
  page.value = 1;
  void load();
}

watch(visible, (v) => {
  if (!v) return;
  picked.value = [];
  usage.value = props.defaultUsage;
  scope.value = props.mode === "content" && props.projectId ? "project" : "uploads";
  kindFilter.value = undefined;
  reload();
});

const excluded = computed(() => new Set(props.excludeIds));

function isPicked(a: MediaAsset): boolean {
  return picked.value.some((p) => p.id === a.id);
}

function toggle(a: MediaAsset) {
  if (excluded.value.has(a.id) || !a.url) return;
  if (isPicked(a)) {
    picked.value = picked.value.filter((p) => p.id !== a.id);
    return;
  }
  if (!props.multiple) {
    picked.value = [a];
    return;
  }
  if (picked.value.length >= props.max) return;
  picked.value = [...picked.value, a];
}

/** 封面只能是单张图片 */
const coverAllowed = computed(() => picked.value.length <= 1 && picked.value.every((a) => a.kind === "image"));
watch(coverAllowed, (ok) => {
  if (!ok && usage.value === "cover") usage.value = "inline";
});

function confirm() {
  if (!picked.value.length) return;
  emit("select", [...picked.value], props.withUsage ? usage.value : "inline");
  visible.value = false;
}

const dialogTitle = computed(() => props.title || t(props.mode === "content" ? "media.picker.titleContent" : "media.picker.titleReference"));
</script>

<template>
  <el-dialog v-model="visible" :title="dialogTitle" width="860px" append-to-body destroy-on-close class="asset-picker">
    <div class="toolbar asset-picker__toolbar">
      <el-radio-group v-if="mode === 'content' && projectId" v-model="scope" size="small" @change="reload">
        <el-radio-button value="project">{{ t("media.picker.scopeProject") }}</el-radio-button>
        <el-radio-button value="uploads">{{ t("media.picker.scopeUploads") }}</el-radio-button>
      </el-radio-group>
      <el-select v-if="!kind" v-model="kindFilter" clearable size="small" :placeholder="t('media.fields.kind')" style="width: 110px" @change="reload">
        <el-option value="image" :label="t('status.media_kind.image')" />
        <el-option value="video" :label="t('status.media_kind.video')" />
      </el-select>
      <span class="text-secondary asset-picker__tip">{{ referenceScope ? t("media.picker.referenceTip") : t("media.picker.projectTip") }}</span>
      <span class="spacer" />
      <el-button size="small" :icon="Refresh" @click="load">{{ t("common.refresh") }}</el-button>
    </div>

    <div v-loading="loading" class="asset-picker__grid">
      <div
        v-for="a in rows"
        :key="a.id"
        class="asset-picker__cell"
        :class="{ 'is-picked': isPicked(a), 'is-disabled': excluded.has(a.id) || !a.url }"
        @click="toggle(a)"
      >
        <div class="asset-picker__preview">
          <el-image v-if="a.kind === 'image'" :src="a.thumbnail_url || a.url || ''" fit="cover" lazy class="asset-picker__media" />
          <video v-else-if="a.url" :src="a.url" preload="metadata" muted class="asset-picker__media" />
          <div v-else class="asset-picker__media asset-picker__empty"><el-icon><VideoCamera /></el-icon></div>
          <el-checkbox :model-value="isPicked(a)" class="asset-picker__check" @click.stop="toggle(a)" />
        </div>
        <div class="asset-picker__meta">
          <span class="mono">#{{ a.id }}</span>
          <StatusTag kind="media_usage_type" :value="a.usage_type" effect="plain" />
          <el-tag v-if="excluded.has(a.id)" size="small" type="info">{{ t("media.picker.alreadyBound") }}</el-tag>
          <el-tag v-else-if="a.content_id" size="small" type="warning" effect="plain">{{ t("media.picker.boundTo", { id: a.content_id }) }}</el-tag>
        </div>
        <div class="asset-picker__sub text-secondary">
          <span v-if="a.width && a.height">{{ a.width }}×{{ a.height }}</span>
          <span>{{ formatBytes(a.size_bytes) }}</span>
          <span>{{ formatDateTime(a.created_at, false) }}</span>
        </div>
      </div>
      <el-empty v-if="!loading && !rows.length" :description="t('media.picker.empty')" :image-size="80" class="asset-picker__none" />
    </div>

    <div class="pagination">
      <el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" layout="total, prev, pager, next" size="small" background @current-change="load" />
    </div>

    <template #footer>
      <div class="asset-picker__footer">
        <span class="text-secondary">{{ multiple ? t("media.picker.pickedMulti", { n: picked.length, max }) : t("media.picker.picked", { n: picked.length }) }}</span>
        <el-radio-group v-if="withUsage" v-model="usage" size="small">
          <el-radio-button value="inline">{{ t("status.media_usage_type.inline") }}</el-radio-button>
          <el-radio-button value="cover" :disabled="!coverAllowed">{{ t("status.media_usage_type.cover") }}</el-radio-button>
        </el-radio-group>
        <span class="spacer" />
        <el-button @click="visible = false">{{ t("common.cancel") }}</el-button>
        <el-button type="primary" :disabled="!picked.length" @click="confirm">{{ t("common.confirm") }}</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.asset-picker__toolbar {
  margin-bottom: 8px;
}
.asset-picker__tip {
  font-size: 12px;
}
.asset-picker__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
  min-height: 160px;
  max-height: 56vh;
  overflow-y: auto;
  padding: 2px;
}
.asset-picker__none {
  grid-column: 1 / -1;
}
.asset-picker__cell {
  border: 2px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 4px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.asset-picker__cell.is-picked {
  border-color: var(--el-color-primary);
}
.asset-picker__cell.is-disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.asset-picker__preview {
  position: relative;
  aspect-ratio: 4 / 3;
  background: var(--el-fill-color-light);
  border-radius: 4px;
  overflow: hidden;
}
.asset-picker__media {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.asset-picker__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  color: var(--el-text-color-secondary);
}
.asset-picker__check {
  position: absolute;
  top: 2px;
  right: 6px;
}
.asset-picker__meta,
.asset-picker__sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px;
  font-size: 12px;
}
.asset-picker__footer {
  display: flex;
  align-items: center;
  gap: 12px;
}
.asset-picker__footer .spacer {
  flex: 1;
}
</style>
