<script setup lang="ts">
// 回填链接弹窗（docs/11 §4.1、§4.3、§11.2、§11.8；docs/04 §6.17、§7.10；docs/13 §8、§12.3）：
// - 单条：内容（ContentSelect，只可选 approved / published；content 预设时锁定）、URL（语法预检，失焦调用 POST /platforms/detect 预填平台，
//   手动改选后不再覆盖）、平台（启用中的平台，含 website / other）、发布账号、发布时间（[now−3650 天, now+5 分钟]，缺省为当前时间）、备注；
//   409 existing_id → 「该链接已回填」并可跳转详情；409 reason=owned_by_other → 「该链接已由其他用户回填」（不跳转）；
// - 批量：粘贴多行「URL[,平台 code][,账号][,发布时间]」（≤ 100 行，含 Tab 的行按 Tab 分列）→ POST /links/batch，
//   顶部 created / failed 计数，逐条结果 index / ok / link_id / queued / code / message；code=409 且 link_id 非空可跳转，
//   link_id=null（owned_by_other）只显示提示、不跳转。
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage } from "element-plus";
import { LINK_LIMITS, type ConflictData, type Content, type LinkBatchResult, type LinkBatchResultItem, type PublishLink } from "@aicreat/shared";
import { fieldErrors, isApiError, validationErrors } from "@/api/client";
import * as linksApi from "@/api/links";
import * as platformsApi from "@/api/platforms";
import ContentSelect from "@/components/ContentSelect.vue";
import { usePermission } from "@/composables/usePermission";
import { usePlatforms } from "@/composables/usePlatforms";
import { toUtcIso } from "@/utils/format";
import { checkPublicUrl, checkPublishedAt, disabledPublishDate, parseBatchLines, platformLabel, type BatchParseError, type ParsedBatchLine } from "@/utils/links";

type Mode = "single" | "batch";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    /** 预设内容（内容编辑器链接面板）：锁定内容选择 */
    contentId?: number | null;
    contentTitle?: string | null;
    /** 内容下拉限定项目（0 / null 为全部可见项目） */
    projectId?: number | null;
    initialMode?: Mode;
  }>(),
  { contentId: null, contentTitle: null, projectId: null, initialMode: "single" },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  /** 至少新建了一条链接：单条为新链接，批量为成功条数 */
  (e: "created", payload: { link: PublishLink | null; count: number }): void;
}>();

const { t } = useI18n();
const router = useRouter();
const { has } = usePermission();
const { activePlatforms, platforms, load: loadPlatforms } = usePlatforms();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const ALLOWED_CONTENT_STATUSES = ["approved", "published"] as const;
const contentLocked = computed(() => !!props.contentId);
const canPickContent = computed(() => has("content.contents.view"));
const canViewLink = computed(() => has("publish.links.view"));
const canDetect = computed(() => has("publish.platforms.view"));

const mode = ref<Mode>("single");
const contentId = ref<number | null>(null);
const contentError = ref("");

// ---------- 单条 ----------
const form = ref({
  url: "",
  platform_id: null as number | null,
  publish_account: "",
  published_at: null as Date | null,
  note: "",
});
const platformTouched = ref(false);
const detecting = ref(false);
const detectedCode = ref("");
const errors = ref<Record<string, string>>({});
const conflict = ref<{ existingId: number | null; message: string } | null>(null);
const submitting = ref(false);
let detectSeq = 0;

// ---------- 批量 ----------
const batchText = ref("");
const batchErrors = ref<BatchParseError[]>([]);
const batchError = ref("");
const batchResult = ref<LinkBatchResult | null>(null);
const batchLines = ref<ParsedBatchLine[]>([]);
const batchSubmitting = ref(false);

const batchCount = computed(() => batchText.value.split(/\r?\n/).filter((l) => l.trim()).length);

function reset() {
  mode.value = props.initialMode;
  contentId.value = props.contentId ?? null;
  contentError.value = "";
  form.value = { url: "", platform_id: null, publish_account: "", published_at: null, note: "" };
  platformTouched.value = false;
  detectedCode.value = "";
  errors.value = {};
  conflict.value = null;
  batchText.value = "";
  batchErrors.value = [];
  batchError.value = "";
  batchResult.value = null;
  batchLines.value = [];
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return;
    reset();
    void loadPlatforms();
  },
  { immediate: true },
);

function onContentChange(_id: number | null, content: Content | null) {
  contentError.value = "";
  if (content && !(ALLOWED_CONTENT_STATUSES as readonly string[]).includes(content.status)) {
    contentError.value = t("links.backfill.contentStatus");
  }
}

function checkContent(): boolean {
  if (!contentId.value || contentId.value <= 0) {
    contentError.value = t("links.backfill.contentRequired");
    return false;
  }
  return !contentError.value;
}

async function onUrlBlur() {
  const url = form.value.url.trim();
  const err = url ? checkPublicUrl(url) : null;
  errors.value = { ...errors.value, url: err ?? "" };
  conflict.value = null;
  if (!url || err || !canDetect.value) return;
  const seq = ++detectSeq;
  detecting.value = true;
  try {
    const res = await platformsApi.detect(url, { silent: true });
    if (seq !== detectSeq) return;
    detectedCode.value = res.code;
    if (!platformTouched.value) form.value.platform_id = res.platform_id;
  } catch {
    if (seq === detectSeq) detectedCode.value = "";
  } finally {
    if (seq === detectSeq) detecting.value = false;
  }
}

function onPlatformChange() {
  platformTouched.value = true;
}

const detectedLabel = computed(() => {
  if (!detectedCode.value) return "";
  const p = platforms.value.find((x) => x.code === detectedCode.value);
  return p ? platformLabel(p) : detectedCode.value;
});

function trimOrNull(v: string): string | null {
  const s = v.trim();
  return s ? s : null;
}

function validateSingle(): boolean {
  const errs: Record<string, string> = {};
  const urlErr = checkPublicUrl(form.value.url);
  if (urlErr) errs.url = urlErr;
  const pubErr = checkPublishedAt(form.value.published_at);
  if (pubErr) errs.published_at = pubErr;
  if (form.value.publish_account.trim().length > LINK_LIMITS.publishAccount) {
    errs.publish_account = t("links.validate.accountTooLong", { max: LINK_LIMITS.publishAccount });
  }
  if (form.value.note.trim().length > LINK_LIMITS.note) errs.note = t("links.validate.noteTooLong", { max: LINK_LIMITS.note });
  errors.value = errs;
  const contentOk = checkContent();
  return contentOk && !Object.keys(errs).length;
}

/** 409 / 404 / 400 → 弹窗内提示 */
function applyError(err: unknown) {
  if (!isApiError(err)) return;
  if (err.code === 409) {
    const data = (err.data ?? {}) as ConflictData;
    if (data.reason === "owned_by_other") {
      conflict.value = { existingId: null, message: t("links.ownedByOther") };
      return;
    }
    if (data.existing_id) {
      conflict.value = { existingId: data.existing_id, message: t("links.backfill.duplicate") };
      return;
    }
    if (data.current_status) {
      contentError.value = `${err.message}（${t(`status.content_status.${data.current_status}`)}）`;
      return;
    }
    ElMessage.error(err.message);
    return;
  }
  if (err.code === 400) {
    const fe = fieldErrors(err);
    if (Object.keys(fe).length) {
      errors.value = { ...errors.value, ...fe };
      if (fe.content_id) contentError.value = fe.content_id;
      return;
    }
  }
  ElMessage.error(err.message);
}

async function submitSingle() {
  conflict.value = null;
  if (!validateSingle()) return;
  submitting.value = true;
  try {
    const res = await linksApi.backfill(
      {
        content_id: contentId.value as number,
        url: form.value.url.trim(),
        platform_id: form.value.platform_id ?? null,
        publish_account: trimOrNull(form.value.publish_account),
        published_at: form.value.published_at ? (toUtcIso(form.value.published_at) ?? null) : null,
        note: trimOrNull(form.value.note),
      },
      { silent: true },
    );
    ElMessage.success(res.queued ? t("links.backfill.createdQueued") : t("links.backfill.created"));
    emit("created", { link: res.link, count: 1 });
    visible.value = false;
  } catch (err) {
    applyError(err);
  } finally {
    submitting.value = false;
  }
}

async function submitBatch() {
  batchError.value = "";
  batchResult.value = null;
  if (!checkContent()) return;
  const { items, errors: parseErrors } = parseBatchLines(batchText.value, platforms.value);
  batchErrors.value = parseErrors;
  if (parseErrors.length) {
    batchError.value = t("links.batch.fixErrors", { n: parseErrors.length });
    return;
  }
  if (!items.length) {
    batchError.value = t("links.batch.empty");
    return;
  }
  if (items.length > LINK_LIMITS.batchMax) {
    batchError.value = t("links.batch.tooMany", { max: LINK_LIMITS.batchMax });
    return;
  }
  batchLines.value = items;
  batchSubmitting.value = true;
  try {
    const res = await linksApi.batchBackfill(
      items.map((it) => ({ content_id: contentId.value as number, ...it.body })),
      { silent: true },
    );
    batchResult.value = res;
    if (res.created > 0) emit("created", { link: null, count: res.created });
    if (res.failed) ElMessage.warning(t("links.batch.done", { created: res.created, failed: res.failed }));
    else ElMessage.success(t("links.batch.done", { created: res.created, failed: res.failed }));
  } catch (err) {
    if (isApiError(err)) {
      if (err.code === 404 || (err.code === 409 && (err.data as ConflictData | null)?.current_status)) {
        applyError(err);
        if (!contentError.value) batchError.value = err.message;
        return;
      }
      const msgs = validationErrors(err).slice(0, 5).map((i) => `${i.loc.slice(1).join(".")}: ${i.msg}`);
      batchError.value = msgs.length ? `${err.message}：${msgs.join("；")}` : err.message;
    }
  } finally {
    batchSubmitting.value = false;
  }
}

function resultUrl(row: LinkBatchResultItem): string {
  return batchLines.value[row.index]?.body.url ?? "-";
}

function resultLine(row: LinkBatchResultItem): number {
  return batchLines.value[row.index]?.line ?? row.index + 1;
}

function resultMessage(row: LinkBatchResultItem): string {
  if (row.reason === "owned_by_other") return t("links.ownedByOther");
  if (row.ok) return row.queued ? t("links.batch.msgOkQueued") : t("links.batch.msgOk");
  return row.message;
}

function gotoLink(id: number) {
  visible.value = false;
  void router.push(`/links/${id}`);
}

function continueBatch() {
  batchResult.value = null;
  batchText.value = "";
  batchErrors.value = [];
  batchError.value = "";
}

function submit() {
  if (mode.value === "single") void submitSingle();
  else void submitBatch();
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('links.backfill.title')" width="min(760px, 96vw)" :close-on-click-modal="false" append-to-body>
    <el-form label-width="100px" class="backfill-form" @submit.prevent>
      <el-form-item :label="t('links.content')" :error="contentError" required>
        <div v-if="contentLocked" class="locked-content">
          <span class="text-secondary">#{{ contentId }}</span>
          <span class="locked-content__title">{{ contentTitle || "" }}</span>
        </div>
        <ContentSelect
          v-else-if="canPickContent"
          v-model="contentId"
          :project-id="projectId || null"
          :allowed-statuses="[...ALLOWED_CONTENT_STATUSES]"
          :placeholder="t('links.backfill.contentPlaceholder')"
          @change="onContentChange"
        />
        <el-input-number v-else v-model="contentId" :min="1" :controls="false" :placeholder="t('links.backfill.contentIdPlaceholder')" style="width: 200px" />
        <div class="form-hint">{{ t("links.backfill.contentHint") }}</div>
      </el-form-item>
    </el-form>

    <el-tabs v-model="mode">
      <!-- 单条 -->
      <el-tab-pane name="single" :label="t('links.backfill.single')">
        <el-form label-width="100px" class="backfill-form" @submit.prevent="submitSingle">
          <el-form-item :label="t('links.url')" :error="errors.url" required>
            <el-input v-model="form.url" :placeholder="t('links.backfill.urlPlaceholder')" clearable @blur="onUrlBlur" @change="conflict = null" />
          </el-form-item>
          <el-alert v-if="conflict" type="warning" :closable="false" show-icon class="conflict">
            <template #title>
              {{ conflict.message }}
              <el-link v-if="conflict.existingId && canViewLink" type="primary" underline="never" class="conflict__jump" @click="gotoLink(conflict.existingId)">
                {{ t("links.backfill.viewExisting", { id: conflict.existingId }) }}
              </el-link>
            </template>
          </el-alert>
          <el-form-item :label="t('links.platform')" :error="errors.platform_id">
            <el-select
              v-model="form.platform_id"
              v-loading="detecting"
              clearable
              filterable
              :placeholder="t('links.backfill.platformAuto')"
              style="width: 100%"
              @change="onPlatformChange"
            >
              <el-option v-for="p in activePlatforms" :key="p.id" :value="p.id" :label="`${platformLabel(p)}（${p.code}）`" />
            </el-select>
            <div class="form-hint">
              <template v-if="detectedCode">{{ t("links.backfill.detected", { name: detectedLabel }) }}</template>
              <template v-else>{{ t("links.backfill.platformHint") }}</template>
            </div>
          </el-form-item>
          <el-form-item :label="t('links.publishAccount')" :error="errors.publish_account">
            <el-input v-model="form.publish_account" :maxlength="LINK_LIMITS.publishAccount" show-word-limit :placeholder="t('links.backfill.accountPlaceholder')" />
          </el-form-item>
          <el-form-item :label="t('links.publishedAt')" :error="errors.published_at">
            <el-date-picker
              v-model="form.published_at"
              type="datetime"
              :disabled-date="disabledPublishDate"
              :placeholder="t('links.backfill.publishedAtPlaceholder')"
              style="width: 100%"
            />
            <div class="form-hint">{{ t("links.backfill.publishedAtHint", { days: LINK_LIMITS.publishedAtPastDays }) }}</div>
          </el-form-item>
          <el-form-item :label="t('links.note')" :error="errors.note">
            <el-input v-model="form.note" type="textarea" :rows="2" :maxlength="LINK_LIMITS.note" show-word-limit />
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <!-- 批量 -->
      <el-tab-pane name="batch" :label="t('links.backfill.batch')">
        <template v-if="!batchResult">
          <el-input v-model="batchText" type="textarea" :rows="10" :placeholder="t('links.batch.placeholder')" spellcheck="false" class="batch-input" />
          <div class="form-hint">{{ t("links.batch.hint", { count: batchCount, max: LINK_LIMITS.batchMax }) }}</div>
          <el-alert v-if="batchError" type="error" :title="batchError" :closable="false" show-icon class="mt" />
          <el-table v-if="batchErrors.length" :data="batchErrors" size="small" max-height="220" class="mt">
            <el-table-column prop="line" :label="t('links.batch.line')" width="70" />
            <el-table-column prop="text" :label="t('links.batch.content')" min-width="220" show-overflow-tooltip />
            <el-table-column prop="message" :label="t('links.batch.reason')" min-width="180" />
          </el-table>
        </template>
        <template v-else>
          <div class="batch-stats">
            <el-tag type="success" size="large">{{ t("links.batch.created", { n: batchResult.created }) }}</el-tag>
            <el-tag :type="batchResult.failed ? 'danger' : 'info'" size="large">{{ t("links.batch.failed", { n: batchResult.failed }) }}</el-tag>
          </div>
          <el-table :data="batchResult.results" size="small" max-height="360" border>
            <el-table-column :label="t('links.batch.index')" width="80">
              <template #default="{ row }">
                <div>{{ row.index }}</div>
                <div class="text-secondary line-no">{{ t("links.batch.lineNo", { n: resultLine(row) }) }}</div>
              </template>
            </el-table-column>
            <el-table-column :label="t('links.url')" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ resultUrl(row) }}</template>
            </el-table-column>
            <el-table-column :label="t('links.batch.ok')" width="64">
              <template #default="{ row }">
                <el-tag :type="row.ok ? 'success' : 'danger'" size="small" disable-transitions>{{ row.ok ? t("common.yes") : t("common.no") }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="link_id" width="76">
              <template #default="{ row }">
                <el-link v-if="row.link_id && canViewLink" type="primary" underline="never" @click="gotoLink(row.link_id)">#{{ row.link_id }}</el-link>
                <span v-else-if="row.link_id">#{{ row.link_id }}</span>
                <span v-else class="text-secondary">-</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('links.batch.queued')" width="70">
              <template #default="{ row }">{{ row.queued ? t("common.yes") : t("common.no") }}</template>
            </el-table-column>
            <el-table-column prop="code" label="code" width="60" />
            <el-table-column :label="t('links.batch.message')" min-width="180">
              <template #default="{ row }">
                <span :class="{ 'text-danger': !row.ok }">{{ resultMessage(row) }}</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <el-button @click="visible = false">{{ t("common.close") }}</el-button>
      <template v-if="mode === 'batch' && batchResult">
        <el-button type="primary" @click="continueBatch">{{ t("links.batch.continue") }}</el-button>
      </template>
      <el-button v-else type="primary" :loading="submitting || batchSubmitting" @click="submit">
        {{ mode === "single" ? t("links.backfill.submit") : t("links.batch.submit") }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.backfill-form :deep(.el-form-item) {
  margin-bottom: 16px;
}
.locked-content {
  display: flex;
  gap: 6px;
  min-width: 0;
  line-height: 32px;
}
.locked-content__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.form-hint {
  width: 100%;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.conflict {
  margin: -6px 0 14px 100px;
  width: auto;
}
.conflict__jump {
  margin-left: 8px;
  vertical-align: baseline;
}
.batch-input :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
}
.batch-stats {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}
.mt {
  margin-top: 10px;
}
.line-no {
  font-size: 12px;
}
.text-danger {
  color: var(--el-color-danger);
}
@media (max-width: 640px) {
  .conflict {
    margin-left: 0;
  }
}
</style>
