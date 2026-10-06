<script setup lang="ts">
// 内容编辑器（docs/09 §8、§10.6；docs/04 §6.11、§7.6、§7.7、§8）：
// - 进入页面 GET /admin/contents/{id}；active_task_id 非空时 usePolling(getTask, 3s)（页面不可见暂停），编辑区只读并显示遮罩，
//   终态后自动刷新内容并提示「已生成新版本」/ 错误原因；pending_tasks[]（大纲 / SEO）非空时以同样间隔轮询详情直到清空；
// - 工具条：保存（PUT，脏检查，固定携带进入页面时的 current_version_id，409 冲突提示刷新）、生成大纲 / 正文（segmented 开关与模型）/
//   SEO 要素（含 include_faq）、重写（模式受 rewrite.modes 限制、范围全文 / 指定小节（按 outline 顺序）、改风格、补充要求、模板、模型）、
//   提审 / 通过 / 驳回（审核意见）、归档 / 恢复、导出 md / html / json、版本抽屉、删除；可用动作由 CONTENT_ACTIONS（状态 → 动作）决定；
// - 面板：任务（TaskProgress）、大纲（OutlineEditor，增删改排后随保存提交）、SEO（字数提示、关键词 Tag、FAQ）、素材（插入到光标 / 设为封面 / 解绑，
//   AssetPicker 选择已有素材绑定为配图 / 封面、上移 / 下移、「生成配图 / 生成封面」跳转图片工作台并预选本内容，docs/10 §7.5）、链接（GET /contents/{id}/links，平台 / 存活 / 收录徽标，LinkBackfillDialog 预填本内容回填，docs/11 §11.8）、信息；
// - 模板下拉仅 has('content.prompt_templates.view')、模型下拉仅 has('ai.models.view') 时显示，否则请求不带 template_id / model（§10.8）；
// - 路由 /contents/new：打开手工创建对话框，创建后进入该内容的编辑器。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import { ArrowDown, ArrowLeft, Bottom, Clock, Delete, DocumentChecked, EditPen, FolderOpened, MagicStick, Picture, Refresh, Top, VideoCamera } from "@element-plus/icons-vue";
import {
  CONTENT_ACTIONS,
  CONTENT_LIMITS,
  CONTENT_STYLE,
  REVIEW_BLOCKING_FLAGS,
  type AiTaskSummary,
  type ConflictData,
  type Content,
  type ContentAction,
  type ContentExportFormat,
  type ContentStyle,
  type ContentUpdateBody,
  type FaqItem,
  type MediaAsset,
  type OutlineItem,
  type PromptKind,
  type PublishLink,
  type RewriteMode,
  type RewriteScope,
  type TaskCreatedResult,
} from "@aicreat/shared";
import { isApiError } from "@/api/client";
import * as contentsApi from "@/api/contents";
import AssetPicker from "@/components/AssetPicker.vue";
import ContentCreateDialog from "@/components/ContentCreateDialog.vue";
import ContentVersionsDrawer from "@/components/ContentVersionsDrawer.vue";
import FaqEditor from "@/components/FaqEditor.vue";
import LinkBackfillDialog from "@/components/LinkBackfillDialog.vue";
import LinkIndexBadges from "@/components/LinkIndexBadges.vue";
import GenerateNotice from "@/components/GenerateNotice.vue";
import MarkdownEditor from "@/components/MarkdownEditor.vue";
import MarkdownPreview from "@/components/MarkdownPreview.vue";
import ModelSelect from "@/components/ModelSelect.vue";
import OutlineEditor, { normalizeOutline, validateOutline } from "@/components/OutlineEditor.vue";
import StatusTag from "@/components/StatusTag.vue";
import TaskProgress, { isTerminalTask } from "@/components/TaskProgress.vue";
import TemplateSelect from "@/components/TemplateSelect.vue";
import { assetMarkup, describeAttachError } from "@/composables/useAssetActions";
import { useGenerateGuard } from "@/composables/useGenerateGuard";
import { usePermission } from "@/composables/usePermission";
import { usePolling } from "@/composables/usePolling";
import { useProject } from "@/composables/useProject";
import { useRuntimeSettings } from "@/composables/useRuntimeSettings";
import { useProjectStore } from "@/store/project";
import { downloadBlob } from "@/utils/download";
import { formatDateTime, formatNumber } from "@/utils/format";
import { platformLabel } from "@/utils/links";
import { countWords } from "@/utils/markdown";

const { t, te } = useI18n();
const { has } = usePermission();
const route = useRoute();
const router = useRouter();
const projectStore = useProjectStore();
const { projectId: currentProjectId, project: currentProject } = useProject();
const { generation, load: loadRuntime } = useRuntimeSettings();

const isNew = computed(() => route.name === "content-new");
const contentId = computed(() => {
  const n = Number(route.params.id);
  return Number.isInteger(n) && n > 0 ? n : 0;
});

// ---------- 内容与表单 ----------
const content = ref<Content | null>(null);
const loading = ref(false);
const notFound = ref(false);
const baseVersionId = ref<number | null>(null);

const form = reactive({
  title: "",
  body: "",
  outline: [] as OutlineItem[],
  summary: "",
  seo_title: "",
  seo_description: "",
  seo_keywords: [] as string[],
  faq: [] as FaqItem[],
});
const baseline = ref("");

function snapshot(): string {
  return JSON.stringify({
    title: form.title.trim(),
    body: form.body,
    outline: normalizeOutline(form.outline),
    summary: form.summary.trim(),
    seo_title: form.seo_title.trim(),
    seo_description: form.seo_description.trim(),
    seo_keywords: form.seo_keywords.map((k) => k.trim()).filter(Boolean),
    faq: form.faq.map((f) => ({ q: f.q.trim(), a: f.a.trim() })),
  });
}

const dirty = computed(() => !!content.value && snapshot() !== baseline.value);

function fillForm(c: Content) {
  form.title = c.title ?? "";
  form.body = c.body ?? "";
  form.outline = (c.outline ?? []).map((it) => ({ heading: it.heading, level: it.level, points: [...(it.points ?? [])] }));
  form.summary = c.summary ?? "";
  form.seo_title = c.seo_title ?? "";
  form.seo_description = c.seo_description ?? "";
  form.seo_keywords = [...(c.seo_keywords ?? [])];
  form.faq = (c.faq ?? []).map((f) => ({ q: f.q, a: f.a }));
  baseline.value = snapshot();
  baseVersionId.value = c.current_version_id;
}

/** 应用服务端内容；resetForm=false 时只更新元信息（保留本地未保存修改） */
function applyContent(c: Content, resetForm = true) {
  content.value = c;
  if (resetForm) {
    fillForm(c);
    staleVersion.value = false;
  }
}

const format = computed(() => content.value?.format ?? "markdown");
const status = computed(() => content.value?.status ?? "draft");
const liveWordCount = computed(() => countWords(form.body, format.value));
const hasBody = computed(() => !!(content.value?.body ?? "").trim());

function allowed(action: ContentAction): boolean {
  return CONTENT_ACTIONS[status.value]?.includes(action) ?? false;
}

// ---------- 任务轮询 ----------
const activeTask = ref<AiTaskSummary | null>(null);
const lastTask = ref<AiTaskSummary | null>(null);
/** 本地有未保存修改时服务端产生了新版本（大纲 / SEO 任务完成） */
const staleVersion = ref(false);

const locked = computed(() => status.value === "generating" || !!content.value?.active_task_id);
const canSave = computed(() => has("content.contents.update") && allowed("save") && !locked.value);
const editable = computed(() => canSave.value);

async function pollActive() {
  const id = contentId.value;
  if (!id) return;
  const task = await contentsApi.getTask(id, { silent: true });
  if (id !== contentId.value) return;
  activeTask.value = task;
  if (!task || isTerminalTask(task.status)) {
    activePolling.stop();
    lastTask.value = task;
    activeTask.value = null;
    await reload(true);
    if (task?.status === "succeeded") ElMessage.success(t("editor.task.succeeded"));
    else if (task?.status === "failed") ElMessage.error(t("editor.task.failed", { reason: taskReason(task) }));
    else if (task?.status === "cancelled") ElMessage.warning(t("editor.task.cancelled"));
  }
}

const activePolling = usePolling(pollActive, { interval: 3000 });

let pendingIds = new Set<number>();

async function pollPending() {
  const id = contentId.value;
  if (!id) return;
  const c = await contentsApi.get(id, { silent: true });
  if (id !== contentId.value) return;
  const prevVersion = content.value?.version_count ?? 0;
  const nextIds = new Set((c.pending_tasks ?? []).map((p) => p.task_id));
  const finished = [...pendingIds].filter((x) => !nextIds.has(x));
  pendingIds = nextIds;
  const versionChanged = c.current_version_id !== content.value?.current_version_id;
  if (versionChanged && dirty.value) {
    applyContent(c, false);
    staleVersion.value = true;
  } else {
    applyContent(c, versionChanged || !dirty.value);
  }
  if (finished.length) {
    try {
      const task = await contentsApi.getTask(id, { silent: true });
      if (task) lastTask.value = task;
      if (task && finished.includes(task.task_id) && task.status === "failed") ElMessage.error(t("editor.task.failed", { reason: taskReason(task) }));
      else if (c.version_count > prevVersion) ElMessage.success(t("editor.task.succeeded"));
    } catch {
      /* ignore */
    }
  }
  syncPolling();
}

const pendingPolling = usePolling(pollPending, { interval: 3000, immediate: false });

function syncPolling() {
  const c = content.value;
  if (!c) {
    activePolling.stop();
    pendingPolling.stop();
    return;
  }
  if (c.active_task_id) activePolling.start();
  const needDetail = !!c.pending_tasks?.length || (c.status === "generating" && !c.active_task_id);
  if (needDetail) {
    if (!pendingPolling.running.value) {
      pendingIds = new Set((c.pending_tasks ?? []).map((p) => p.task_id));
      pendingPolling.start();
    }
  } else if (pendingPolling.running.value) pendingPolling.stop();
}

function taskReason(task: AiTaskSummary | null): string {
  if (!task) return "-";
  if (task.error_category) {
    const key = `taskProgress.hints.${task.error_category}`;
    return te(key) ? t(key) : task.error_category;
  }
  return task.error_message || "-";
}

// ---------- 加载 ----------
async function reload(resetForm = true) {
  const id = contentId.value;
  if (!id) return;
  try {
    const c = await contentsApi.get(id, { silent: true });
    if (id !== contentId.value) return;
    applyContent(c, resetForm);
    notFound.value = false;
  } catch (err) {
    if (isApiError(err) && err.code === 404) notFound.value = true;
    else if (isApiError(err)) ElMessage.error(err.message);
  }
  syncPolling();
}

async function init() {
  activePolling.stop();
  pendingPolling.stop();
  content.value = null;
  activeTask.value = null;
  lastTask.value = null;
  assets.value = [];
  links.value = [];
  notFound.value = false;
  if (isNew.value || !contentId.value) {
    notFound.value = !isNew.value;
    return;
  }
  loading.value = true;
  try {
    await reload(true);
  } finally {
    loading.value = false;
  }
  void loadRuntime();
  // content 在 reload 中被赋值（TS 无法感知异步赋值，显式标注类型）
  const loaded = content.value as Content | null;
  if (loaded) {
    if (!loaded.active_task_id) {
      contentsApi
        .getTask(contentId.value, { silent: true })
        .then((task) => (lastTask.value = task))
        .catch(() => undefined);
    }
    void loadAssets();
    void loadLinks();
  }
}

watch(() => route.fullPath, (to, from) => {
  if (to !== from && (route.name === "content-edit" || route.name === "content-new")) void init();
});

// ---------- 保存 ----------
const saving = ref(false);
const saveErrors = ref<Record<string, string>>({});

function trimOrNull(v: string): string | null {
  const s = v.trim();
  return s ? s : null;
}

function validateForm(): string | null {
  if (!form.title.trim()) return t("editor.titleRequired");
  if (form.title.trim().length > 200) return t("contents.create.titleTooLong", { max: 200 });
  const outlineError = validateOutline(form.outline);
  if (outlineError) return outlineError;
  const L = CONTENT_LIMITS;
  if (form.summary.trim().length > L.summary) return t("seo.tooLong", { field: t("seo.summary"), max: L.summary });
  if (form.seo_title.trim().length > L.seoTitle) return t("seo.tooLong", { field: t("seo.seoTitle"), max: L.seoTitle });
  if (form.seo_description.trim().length > L.seoDescription) return t("seo.tooLong", { field: t("seo.seoDescription"), max: L.seoDescription });
  const kws = form.seo_keywords.map((k) => k.trim()).filter(Boolean);
  if (kws.length > L.seoKeywords || kws.some((k) => k.length > L.seoKeyword)) return t("seo.keywordsRule", { max: L.seoKeywords, len: L.seoKeyword });
  for (let i = 0; i < form.faq.length; i += 1) {
    const f = form.faq[i];
    if (!f.q.trim() || !f.a.trim()) return t("faq.incomplete", { index: i + 1 });
    if (f.q.trim().length > L.faqQuestion || f.a.trim().length > L.faqAnswer) return t("faq.tooLong", { index: i + 1, q: L.faqQuestion, a: L.faqAnswer });
  }
  return null;
}

/** 只提交相对进入页面（或上次保存）时有变化的版本化字段，并固定携带 current_version_id（并发冲突检查） */
function changedFields(): ContentUpdateBody {
  const base = JSON.parse(baseline.value) as Record<string, unknown>;
  const cur = JSON.parse(snapshot()) as Record<string, unknown>;
  const body: ContentUpdateBody = { current_version_id: baseVersionId.value };
  const changed = (key: string) => JSON.stringify(base[key]) !== JSON.stringify(cur[key]);
  if (changed("title")) body.title = form.title.trim();
  if (changed("body")) body.body = form.body;
  if (changed("outline")) {
    const outline = normalizeOutline(form.outline);
    body.outline = outline.length ? outline : null;
  }
  if (changed("summary")) body.summary = trimOrNull(form.summary);
  if (changed("seo_title")) body.seo_title = trimOrNull(form.seo_title);
  if (changed("seo_description")) body.seo_description = trimOrNull(form.seo_description);
  if (changed("seo_keywords")) body.seo_keywords = form.seo_keywords.map((k) => k.trim()).filter(Boolean);
  if (changed("faq")) body.faq = form.faq.map((f) => ({ q: f.q.trim(), a: f.a.trim() }));
  return body;
}

async function save(): Promise<boolean> {
  const c = content.value;
  if (!c || !canSave.value) return false;
  const error = validateForm();
  if (error) {
    ElMessage.warning(error);
    return false;
  }
  saving.value = true;
  saveErrors.value = {};
  try {
    const updated = await contentsApi.update(c.id, changedFields(), { silent: true });
    applyContent(updated, true);
    if (updated.version_created === false) ElMessage.info(t("editor.noChange"));
    else ElMessage.success(t("editor.saved", { no: updated.current_version?.version_no ?? updated.version_count }));
    syncPolling();
    return true;
  } catch (err) {
    await handleSaveError(err);
    return false;
  } finally {
    saving.value = false;
  }
}

async function handleSaveError(err: unknown) {
  if (!isApiError(err)) return;
  const data = (err.data ?? {}) as ConflictData;
  if (err.code === 409 && data.current_version_id !== undefined) {
    try {
      await ElMessageBox.confirm(t("editor.versionConflict", { id: data.current_version_id }), t("editor.versionConflictTitle"), {
        type: "warning",
        confirmButtonText: t("editor.reloadDiscard"),
        cancelButtonText: t("editor.keepEditing"),
      });
      await reload(true);
    } catch {
      /* 保留本地修改，用户可复制后刷新 */
    }
    return;
  }
  if (err.code === 409 && data.current_status) {
    ElMessage.error(t("editor.statusConflict", { status: t(`status.content_status.${data.current_status}`) }));
    await reload(false);
    return;
  }
  if (err.code === 400 && Array.isArray(err.data)) {
    const msgs = (err.data as { msg: string }[]).slice(0, 3).map((i) => i.msg);
    ElMessage.error(msgs.length ? `${err.message}：${msgs.join("；")}` : err.message);
    return;
  }
  ElMessage.error(err.message);
}

/** 生成类动作前：有未保存修改时询问「保存并继续」 */
async function ensureSaved(): Promise<boolean> {
  if (!dirty.value) return true;
  try {
    await ElMessageBox.confirm(t("editor.saveBeforeGenerate"), t("common.tip"), {
      type: "warning",
      confirmButtonText: t("editor.saveAndContinue"),
    });
  } catch {
    return false;
  }
  return save();
}

// ---------- 生成（大纲 / 正文 / SEO） ----------
type GenKind = "outline" | "body" | "seo";
const guard = useGenerateGuard();
const genVisible = ref(false);
const genKind = ref<GenKind>("outline");
const genForm = reactive({
  segmented: true,
  include_faq: true,
  template_id: null as number | null,
  model: null as string | null,
});

const genTemplateKinds = computed<PromptKind[]>(() => {
  if (genKind.value === "outline") return ["outline"];
  if (genKind.value === "seo") return ["seo_meta"];
  return ["content", "section"];
});

async function openGenerate(kind: GenKind) {
  if (!(await ensureSaved())) return;
  const gen = await loadRuntime();
  guard.reset();
  genKind.value = kind;
  const params = content.value?.generation_params ?? {};
  genForm.segmented = typeof params.segmented === "boolean" ? params.segmented : gen.content.segmented;
  genForm.include_faq = typeof params.include_faq === "boolean" ? params.include_faq : gen.content.include_faq;
  genForm.template_id = null;
  genForm.model = null;
  genVisible.value = true;
}

function optional() {
  return {
    ...(has("content.prompt_templates.view") && genForm.template_id ? { template_id: genForm.template_id } : {}),
    ...(has("ai.models.view") && genForm.model ? { model: genForm.model } : {}),
  };
}

async function afterTaskCreated(res: TaskCreatedResult, message: string) {
  ElMessage.success(message);
  if (res.quota_warning) {
    ElMessage.warning({
      message: t("generation.quotaWarning", {
        scope: t(`generation.quotaScope.${res.quota_warning.scope}`),
        percent: Math.round(res.quota_warning.percent),
        used: formatNumber(res.quota_warning.used),
        limit: formatNumber(res.quota_warning.limit),
      }),
      duration: 6000,
    });
  }
  await reload(true);
}

async function submitGenerate() {
  const c = content.value;
  if (!c) return;
  const kind = genKind.value;
  const res = await guard.run(() => {
    if (kind === "outline") return contentsApi.generateOutline(c.id, optional(), { silent: true });
    if (kind === "seo") return contentsApi.generateSeo(c.id, { include_faq: genForm.include_faq, ...optional() }, { silent: true });
    return contentsApi.generateBody(c.id, { segmented: genForm.segmented, ...optional() }, { silent: true });
  });
  if (!res) return;
  genVisible.value = false;
  await afterTaskCreated(res, t("editor.gen.queued", { id: res.task_id }));
}

// ---------- 重写 ----------
const rewriteVisible = ref(false);
const rewriteGuard = useGenerateGuard();
const rewriteForm = reactive({
  mode: "rewrite" as RewriteMode,
  scope: "full" as RewriteScope,
  section_index: null as number | null,
  style: "news" as ContentStyle,
  instruction: "",
  template_id: null as number | null,
  model: null as string | null,
});

const rewriteModes = computed(() => generation.value.rewrite.modes as RewriteMode[]);
const savedOutline = computed(() => content.value?.outline ?? []);

async function openRewrite() {
  if (!(await ensureSaved())) return;
  const gen = await loadRuntime();
  rewriteGuard.reset();
  const modes = gen.rewrite.modes as RewriteMode[];
  rewriteForm.mode = modes.includes(rewriteForm.mode) ? rewriteForm.mode : (modes[0] ?? "rewrite");
  rewriteForm.scope = "full";
  rewriteForm.section_index = null;
  rewriteForm.style = content.value?.style ?? "news";
  rewriteForm.instruction = "";
  rewriteForm.template_id = null;
  rewriteForm.model = null;
  rewriteVisible.value = true;
}

watch(
  () => rewriteForm.mode,
  () => {
    rewriteForm.template_id = null;
  },
);

async function submitRewrite() {
  const c = content.value;
  if (!c) return;
  const f = rewriteForm;
  const errs: Record<string, string> = {};
  if (f.scope === "section" && !f.section_index) errs.section_index = t("editor.rewrite.sectionRequired");
  if (f.mode === "restyle" && !f.style) errs.style = t("editor.rewrite.styleRequired");
  if (f.instruction.length > CONTENT_LIMITS.instruction) errs.instruction = t("editor.rewrite.instructionTooLong", { max: CONTENT_LIMITS.instruction });
  if (Object.keys(errs).length) {
    rewriteGuard.fields.value = errs;
    return;
  }
  const res = await rewriteGuard.run(() =>
    contentsApi.rewrite(
      c.id,
      {
        mode: f.mode,
        scope: f.scope,
        section_index: f.scope === "section" ? f.section_index : null,
        style: f.mode === "restyle" ? f.style : null,
        instruction: f.instruction.trim() || null,
        ...(has("content.prompt_templates.view") && f.template_id ? { template_id: f.template_id } : {}),
        ...(has("ai.models.view") && f.model ? { model: f.model } : {}),
      },
      { silent: true },
    ),
  );
  if (!res) return;
  rewriteVisible.value = false;
  await afterTaskCreated(res, t("editor.rewrite.queued", { id: res.task_id }));
}

// ---------- 审核 / 归档 / 删除 ----------
const acting = ref(false);
const reviewVisible = ref(false);
const reviewType = ref<"approve" | "reject">("approve");
const reviewNote = ref("");
const reviewError = ref("");

function riskLabel(flag: string): string {
  return te(`contents.riskFlags.${flag}`) ? t(`contents.riskFlags.${flag}`) : flag;
}

async function submitReview() {
  const c = content.value;
  if (!c) return;
  if (!(await ensureSaved())) return;
  acting.value = true;
  try {
    const updated = await contentsApi.submitReview(c.id, { silent: true });
    applyContent(updated, !dirty.value);
    ElMessage.success(updated.status === "approved" ? t("contents.review.autoApproved") : t("contents.review.submitted"));
  } catch (err) {
    if (isApiError(err)) {
      const data = (err.data ?? {}) as ConflictData;
      if (err.code === 409 && data.reason === "quality_blocked") {
        await ElMessageBox.alert(
          t("contents.review.qualityBlocked", { flags: (data.flags ?? []).map(riskLabel).join("、") }),
          t("contents.review.qualityBlockedTitle"),
          { type: "error" },
        ).catch(() => undefined);
      } else ElMessage.error(err.message);
    }
  } finally {
    acting.value = false;
  }
}

function openReview(type: "approve" | "reject") {
  reviewType.value = type;
  reviewNote.value = "";
  reviewError.value = "";
  reviewVisible.value = true;
}

async function confirmReview() {
  const c = content.value;
  if (!c) return;
  const note = reviewNote.value.trim();
  if (reviewType.value === "reject" && !note) {
    reviewError.value = t("contents.review.noteRequired");
    return;
  }
  if (note.length > 500) {
    reviewError.value = t("contents.review.noteTooLong", { max: 500 });
    return;
  }
  acting.value = true;
  try {
    const updated = reviewType.value === "approve" ? await contentsApi.approve(c.id, note || null) : await contentsApi.reject(c.id, note);
    applyContent(updated, !dirty.value);
    reviewVisible.value = false;
    ElMessage.success(reviewType.value === "approve" ? t("contents.review.approved") : t("contents.review.rejected"));
  } catch {
    /* 已提示 */
  } finally {
    acting.value = false;
  }
}

async function toggleArchive() {
  const c = content.value;
  if (!c) return;
  const archiving = c.status !== "archived";
  try {
    await ElMessageBox.confirm(
      archiving ? t("contents.archiveConfirm", { title: c.title }) : t("contents.unarchiveConfirm", { title: c.title }),
      t("common.tip"),
      { type: archiving ? "warning" : "info" },
    );
  } catch {
    return;
  }
  acting.value = true;
  try {
    const updated = archiving ? await contentsApi.archive(c.id) : await contentsApi.unarchive(c.id);
    applyContent(updated, !dirty.value);
    ElMessage.success(archiving ? t("contents.archived") : t("contents.unarchived"));
  } catch {
    /* 已提示 */
  } finally {
    acting.value = false;
  }
}

async function removeContent() {
  const c = content.value;
  if (!c) return;
  try {
    await ElMessageBox.confirm(t("contents.deleteConfirm", { title: c.title }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = true;
  try {
    await contentsApi.remove(c.id);
    ElMessage.success(t("contents.deleted"));
    baseline.value = snapshot(); // 不再提示未保存
    void router.push("/contents");
  } catch {
    /* 已提示 */
  } finally {
    acting.value = false;
  }
}

async function exportAs(fmt: ContentExportFormat) {
  const c = content.value;
  if (!c) return;
  if (dirty.value) ElMessage.info(t("editor.exportSavedOnly"));
  try {
    const { blob, filename } = await contentsApi.exportContent(c.id, fmt);
    downloadBlob(blob, filename || `content-${c.id}.${fmt}`);
  } catch {
    /* 已提示 */
  }
}

// ---------- 版本 ----------
const versionsVisible = ref(false);

function onVersionRestored(c: Content) {
  applyContent(c, true);
  syncPolling();
}

// ---------- 素材（docs/10 §4.9、§7.5） ----------
const editorRef = ref<InstanceType<typeof MarkdownEditor>>();
const assets = ref<MediaAsset[]>([]);
const assetsLoading = ref(false);
const attaching = ref(false);
const pickerVisible = ref(false);

/** 绑定素材按 sort, id 排序（与 GET /contents/{id}/assets 一致） */
const sortedAssets = computed(() => [...assets.value].sort((x, y) => (x.sort ?? 0) - (y.sort ?? 0) || x.id - y.id));
const coverAsset = computed(() => {
  const id = content.value?.cover_asset_id;
  return id ? (assets.value.find((a) => a.id === id) ?? null) : null;
});
const boundIds = computed(() => assets.value.map((a) => a.id));

async function loadAssets() {
  const id = contentId.value;
  if (!id) return;
  assetsLoading.value = true;
  try {
    assets.value = await contentsApi.listAssets(id, { silent: true });
  } catch {
    assets.value = [];
  } finally {
    assetsLoading.value = false;
  }
}

/** 插入标记：markdown `![alt](url)`、html `<img src alt>`、视频 `<video src controls></video>`；alt 取 prompt 前 50 字符，无则取标题 */
async function insertAsset(a: MediaAsset) {
  if (!a.url || a.status !== "ready") return;
  if (!editable.value) {
    ElMessage.warning(t("editor.assets.readonly"));
    return;
  }
  await editorRef.value?.insert(assetMarkup(a, format.value, form.title || content.value?.title || ""), true);
}

function usageOf(a: MediaAsset): "cover" | "inline" {
  return content.value?.cover_asset_id === a.id ? "cover" : "inline";
}

async function attachAsset(assetId: number, usage: "inline" | "cover", sort?: number) {
  const c = content.value;
  if (!c) return;
  attaching.value = true;
  try {
    const maxSort = assets.value.reduce((m, a) => Math.max(m, a.sort ?? 0), 0);
    const existing = assets.value.find((a) => a.id === assetId);
    await contentsApi.attach(c.id, assetId, { usage_type: usage, sort: sort ?? existing?.sort ?? maxSort + 1 }, { silent: true });
    ElMessage.success(usage === "cover" ? t("editor.assets.coverSet") : t("editor.assets.attached"));
    await Promise.all([loadAssets(), reload(!dirty.value)]);
  } catch (err) {
    const msg = describeAttachError(err, assets.value.find((a) => a.id === assetId));
    if (msg) ElMessage.error(msg);
  } finally {
    attaching.value = false;
  }
}

/** AssetPicker 选择后批量绑定：cover 只取单张图片，其余按顺序追加为 inline */
async function onAssetsPicked(picked: MediaAsset[], usage: "cover" | "inline") {
  const c = content.value;
  if (!c || !picked.length) return;
  attaching.value = true;
  let ok = 0;
  try {
    let next = assets.value.reduce((m, a) => Math.max(m, a.sort ?? 0), 0) + 1;
    for (const a of picked) {
      const u = usage === "cover" && a.kind === "image" && picked.length === 1 ? "cover" : "inline";
      try {
        await contentsApi.attach(c.id, a.id, { usage_type: u, sort: next }, { silent: true });
        next += 1;
        ok += 1;
      } catch (err) {
        const msg = describeAttachError(err, a);
        if (msg) ElMessage.error(`#${a.id}：${msg}`);
      }
    }
    if (ok) ElMessage.success(usage === "cover" && ok === 1 ? t("editor.assets.coverSet") : t("editor.assets.attachedN", { n: ok }));
    await Promise.all([loadAssets(), reload(!dirty.value)]);
  } finally {
    attaching.value = false;
  }
}

/** 上移 / 下移：按新顺序重排 sort（1 起），只对 sort 变化的项调用 attach（用途不变） */
async function moveAsset(a: MediaAsset, delta: -1 | 1) {
  const c = content.value;
  if (!c) return;
  const list = [...sortedAssets.value];
  const idx = list.findIndex((x) => x.id === a.id);
  const target = idx + delta;
  if (idx < 0 || target < 0 || target >= list.length) return;
  [list[idx], list[target]] = [list[target], list[idx]];
  attaching.value = true;
  try {
    for (let i = 0; i < list.length; i += 1) {
      const item = list[i];
      // attach 只接受 ready 素材（docs/10 §4.9）：进行中的素材（如生成中的封面）保持原 sort，避免改写其用途
      if ((item.sort ?? 0) === i + 1 || item.status !== "ready") continue;
      await contentsApi.attach(c.id, item.id, { usage_type: usageOf(item), sort: i + 1 }, { silent: true });
    }
    await loadAssets();
  } catch (err) {
    const msg = describeAttachError(err, a);
    if (msg) ElMessage.error(msg);
    await loadAssets();
  } finally {
    attaching.value = false;
  }
}

async function detachAsset(a: MediaAsset) {
  const c = content.value;
  if (!c) return;
  try {
    await ElMessageBox.confirm(
      c.cover_asset_id === a.id ? t("editor.assets.detachCoverConfirm") : t("editor.assets.detachConfirm"),
      t("common.tip"),
      { type: "warning" },
    );
  } catch {
    return;
  }
  try {
    await contentsApi.detach(c.id, a.id, { silent: true });
    ElMessage.success(t("editor.assets.detached"));
    await Promise.all([loadAssets(), reload(!dirty.value)]);
  } catch (err) {
    const msg = describeAttachError(err, a);
    if (msg) ElMessage.error(msg);
  }
}

/** 「生成配图 / 生成封面」：跳转图片工作台并预选本内容（from_content_prompt 默认开启，docs/10 §7.5） */
function gotoImageGenerate(usage: "inline" | "cover" = "inline") {
  const c = content.value;
  if (!c) return;
  void router.push({
    path: "/media/images",
    query: { content_id: String(c.id), project_id: String(c.project_id), usage_type: usage, from_content_prompt: "1" },
  });
}

const canGenerateImage = computed(() => has("media.images.view") && has("media.images.generate") && has("content.contents.update"));

// ---------- 链接 ----------
const links = ref<PublishLink[]>([]);
const linksLoading = ref(false);

async function loadLinks() {
  const id = contentId.value;
  if (!id) return;
  linksLoading.value = true;
  try {
    links.value = await contentsApi.listLinks(id, { silent: true });
  } catch {
    links.value = [];
  } finally {
    linksLoading.value = false;
  }
}

// 回填链接弹窗（LinkBackfillDialog，content_id 预填并锁定，docs/11 §11.8）
const backfillVisible = ref(false);

function openBackfill() {
  if (!content.value) return;
  backfillVisible.value = true;
}

/** 回填成功：刷新链接列表与内容（link_count 变化使 approved → published） */
function onBackfilled() {
  void loadLinks();
  void reload(false);
}

// ---------- 视图 ----------
const viewMode = ref<"split" | "edit" | "preview">("split");
const panels = ref<string[]>(["task", "outline", "seo"]);

const sectionOptions = computed(() =>
  savedOutline.value.map((it, i) => ({ value: i + 1, label: `${i + 1}. ${it.level === 3 ? "　" : ""}${it.heading}` })),
);

const seoTitleWarn = computed(() => form.seo_title.trim().length > CONTENT_LIMITS.seoTitleSuggest);
const seoDescLen = computed(() => form.seo_description.trim().length);
const seoDescWarn = computed(() => seoDescLen.value > 0 && (seoDescLen.value < CONTENT_LIMITS.seoDescriptionMin || seoDescLen.value > CONTENT_LIMITS.seoDescriptionMax));

const blockingFlags = computed(() => (content.value?.risk_flags ?? []).filter((f) => (REVIEW_BLOCKING_FLAGS as readonly string[]).includes(f)));

const projectName = computed(() => {
  const pid = content.value?.project_id;
  return projectStore.projects.find((p) => p.id === pid)?.name ?? (pid ? `#${pid}` : "-");
});

const canGenerate = computed(() => has("content.contents.generate") && !locked.value);

// ---------- 新建 ----------
const createVisible = ref(true);

function onCreated(c: Content) {
  void router.replace({ name: "content-edit", params: { id: c.id } });
}

function onCreateCancel() {
  if (isNew.value) void router.push("/contents");
}

// ---------- 离开保护 ----------
async function confirmLeave(): Promise<boolean> {
  if (!dirty.value) return true;
  try {
    await ElMessageBox.confirm(t("editor.leaveConfirm"), t("common.warning"), { type: "warning" });
    return true;
  } catch {
    return false;
  }
}

onBeforeRouteLeave(confirmLeave);
onBeforeRouteUpdate((to, from) => (to.params.id !== from.params.id ? confirmLeave() : true));

function onBeforeUnload(e: BeforeUnloadEvent) {
  if (dirty.value) {
    e.preventDefault();
    e.returnValue = "";
  }
}

function onKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    if (dirty.value && canSave.value && !saving.value) void save();
  }
}

onMounted(() => {
  window.addEventListener("beforeunload", onBeforeUnload);
  window.addEventListener("keydown", onKeydown);
  if (!projectStore.projectsLoaded && !projectStore.loadingProjects) projectStore.load().catch(() => undefined);
  void init();
});

onBeforeUnmount(() => {
  window.removeEventListener("beforeunload", onBeforeUnload);
  window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <!-- 新建 -->
  <el-card v-if="isNew" shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("menu.contentNew") }}</h2>
      </div>
    </template>
    <el-empty v-if="!currentProjectId" :description="t('generation.noProject')" />
    <el-empty v-else-if="!has('content.contents.create')" :description="t('common.forbiddenAction')" />
    <template v-else>
      <el-empty :description="t('editor.creating')">
        <el-button type="primary" @click="createVisible = true">{{ t("contents.create.title") }}</el-button>
      </el-empty>
      <ContentCreateDialog v-model="createVisible" :project-id="currentProjectId" :project="currentProject" @created="onCreated" @cancel="onCreateCancel" />
    </template>
  </el-card>

  <el-card v-else-if="notFound" shadow="never" class="page-card">
    <el-result icon="warning" :title="t('editor.notFound')">
      <template #extra>
        <el-button type="primary" @click="router.push('/contents')">{{ t("editor.backToList") }}</el-button>
      </template>
    </el-result>
  </el-card>

  <div v-else v-loading="loading" class="editor-page">
    <template v-if="content">
      <!-- 顶部工具条 -->
      <el-card shadow="never" class="page-card toolbar-card">
        <div class="editor-head">
          <el-button :icon="ArrowLeft" text @click="router.push('/contents')" />
          <el-input v-model="form.title" class="editor-title" :readonly="!editable" maxlength="200" :placeholder="t('editor.titlePlaceholder')" />
          <StatusTag kind="content_status" :value="content.status" size="default" />
          <el-tooltip v-if="content.quality_score != null" placement="bottom" :disabled="!content.risk_flags?.length">
            <template #content>
              <div v-for="f in content.risk_flags" :key="f">{{ riskLabel(f) }}</div>
            </template>
            <el-tag :type="content.quality_score < CONTENT_LIMITS.qualityWarn ? 'danger' : content.risk_flags?.length ? 'warning' : 'success'" effect="plain">
              {{ t("editor.quality", { score: content.quality_score }) }}
            </el-tag>
          </el-tooltip>
          <span class="text-secondary meta">
            v{{ content.current_version?.version_no ?? content.version_count }} · {{ t("editor.words", { n: formatNumber(liveWordCount) }) }}
          </span>
          <el-tag v-if="dirty" type="warning" size="small" effect="dark">{{ t("editor.unsaved") }}</el-tag>
        </div>

        <div class="editor-actions">
          <el-button v-if="has('content.contents.update') && allowed('save')" type="primary" :icon="DocumentChecked" :loading="saving" :disabled="!dirty || locked" @click="save">
            {{ t("common.save") }}
          </el-button>
          <el-dropdown
            v-if="has('content.contents.generate') && (allowed('generate_outline') || allowed('generate_body') || allowed('generate_seo'))"
            trigger="click"
            :disabled="!canGenerate"
            @command="(k: 'outline' | 'body' | 'seo') => openGenerate(k)"
          >
            <el-button :icon="MagicStick" :disabled="!canGenerate">
              {{ t("editor.generate") }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item v-if="allowed('generate_outline')" command="outline">{{ t("editor.gen.outline") }}</el-dropdown-item>
                <el-dropdown-item v-if="allowed('generate_body')" command="body">{{ t("editor.gen.body") }}</el-dropdown-item>
                <el-dropdown-item v-if="allowed('generate_seo')" command="seo" :disabled="!hasBody">{{ t("editor.gen.seo") }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-tooltip v-if="has('content.contents.generate') && allowed('rewrite')" :content="t('editor.rewrite.needBody')" :disabled="hasBody" placement="top">
            <span>
              <el-button :icon="EditPen" :disabled="!canGenerate || !hasBody" @click="openRewrite">{{ t("editor.rewrite.button") }}</el-button>
            </span>
          </el-tooltip>
          <el-tooltip v-if="has('content.contents.update') && allowed('submit_review')" :disabled="!blockingFlags.length" placement="top">
            <template #content>{{ t("contents.review.qualityBlocked", { flags: blockingFlags.map(riskLabel).join("、") }) }}</template>
            <span>
              <el-button type="success" plain :loading="acting" @click="submitReview">{{ t("contents.review.submit") }}</el-button>
            </span>
          </el-tooltip>
          <template v-if="has('content.contents.review') && allowed('approve')">
            <el-button type="success" :loading="acting" @click="openReview('approve')">{{ t("contents.review.approve") }}</el-button>
            <el-button type="danger" plain :loading="acting" @click="openReview('reject')">{{ t("contents.review.reject") }}</el-button>
          </template>
          <template v-if="has('content.contents.status')">
            <el-button v-if="allowed('archive')" plain :loading="acting" @click="toggleArchive">{{ t("contents.archive") }}</el-button>
            <el-button v-else-if="allowed('unarchive')" type="success" plain :loading="acting" @click="toggleArchive">{{ t("contents.unarchive") }}</el-button>
          </template>
          <el-dropdown v-if="has('content.contents.export')" trigger="click" @command="(f: ContentExportFormat) => exportAs(f)">
            <el-button>
              {{ t("contents.export") }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="md">Markdown (.md)</el-dropdown-item>
                <el-dropdown-item command="html">HTML (.html)</el-dropdown-item>
                <el-dropdown-item command="json">JSON (.json)</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-button :icon="Clock" @click="versionsVisible = true">{{ t("editor.versions", { n: content.version_count }) }}</el-button>
          <el-button
            v-if="has('content.contents.delete') && allowed('delete')"
            type="danger"
            text
            :icon="Delete"
            :disabled="content.link_count > 0"
            :loading="acting"
            @click="removeContent"
          />
          <span class="spacer" />
          <el-radio-group v-model="viewMode" size="small">
            <el-radio-button value="edit">{{ t("editor.view.edit") }}</el-radio-button>
            <el-radio-button value="split">{{ t("editor.view.split") }}</el-radio-button>
            <el-radio-button value="preview">{{ t("editor.view.preview") }}</el-radio-button>
          </el-radio-group>
          <el-button :icon="Refresh" text @click="reload(!dirty)" />
        </div>

        <el-alert v-if="staleVersion" type="warning" :closable="false" show-icon class="stale-alert">
          <template #title>
            {{ t("editor.staleVersion") }}
            <el-link type="primary" :underline="false" @click="reload(true)">{{ t("editor.loadNewVersion") }}</el-link>
          </template>
        </el-alert>
        <el-alert
          v-if="content.status === 'rejected' && content.review_note"
          type="error"
          :title="t('editor.rejectedNote', { note: content.review_note })"
          :closable="false"
          show-icon
          class="stale-alert"
        />
      </el-card>

      <div class="editor-body">
        <!-- 编辑 / 预览 -->
        <el-card shadow="never" class="page-card main-card">
          <div class="edit-area" :class="`is-${viewMode}`">
            <div v-if="viewMode !== 'preview'" class="edit-area__editor">
              <MarkdownEditor
                ref="editorRef"
                v-model="form.body"
                :format="format"
                :readonly="!editable"
                :monospace="format === 'html'"
                :rows="28"
                :placeholder="t('editor.bodyPlaceholder')"
              />
            </div>
            <div v-if="viewMode !== 'edit'" class="edit-area__preview">
              <MarkdownPreview :source="form.body" :format="format" max-height="calc(100vh - 290px)" bordered />
            </div>
            <div v-if="locked" class="edit-mask">
              <div class="edit-mask__box">
                <div class="edit-mask__title">{{ t("editor.generatingMask") }}</div>
                <TaskProgress v-if="activeTask" :task="activeTask" />
                <el-progress v-else :percentage="0" indeterminate :show-text="false" />
              </div>
            </div>
          </div>
        </el-card>

        <!-- 右侧面板 -->
        <el-card shadow="never" class="page-card side-card">
          <el-collapse v-model="panels">
            <el-collapse-item name="task" :title="t('editor.panels.task')">
              <div class="panel">
                <TaskProgress v-if="activeTask" :task="activeTask" />
                <div v-for="p in content.pending_tasks ?? []" :key="p.task_id" class="pending-task">
                  <TaskProgress :task="p" />
                </div>
                <template v-if="!activeTask && !content.pending_tasks?.length">
                  <TaskProgress v-if="lastTask" :task="lastTask" />
                  <span v-else class="text-secondary">{{ t("editor.task.none") }}</span>
                </template>
                <el-alert v-if="lastTask?.error_category === 'content_blocked' && !activeTask" type="error" :title="t('editor.task.blockedHint')" :closable="false" show-icon />
                <router-link v-if="has('ai.tasks.view')" :to="`/ai/tasks?target_type=content&target_id=${content.id}`" class="panel-link">
                  {{ t("editor.task.viewAll") }}
                </router-link>
              </div>
            </el-collapse-item>

            <el-collapse-item name="outline" :title="t('editor.panels.outline', { n: form.outline.length })">
              <div class="panel">
                <OutlineEditor v-model="form.outline" :readonly="!editable" />
                <el-button
                  v-if="has('content.contents.generate') && allowed('generate_body')"
                  size="small"
                  type="primary"
                  plain
                  :disabled="!canGenerate || !form.outline.length"
                  @click="openGenerate('body')"
                >
                  {{ t("editor.regenerateByOutline") }}
                </el-button>
              </div>
            </el-collapse-item>

            <el-collapse-item name="seo" :title="t('editor.panels.seo')">
              <el-form label-position="top" size="small" class="panel">
                <el-form-item :label="t('seo.summary')">
                  <el-input v-model="form.summary" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :maxlength="CONTENT_LIMITS.summary" show-word-limit :readonly="!editable" />
                </el-form-item>
                <el-form-item :label="t('seo.seoTitle')">
                  <el-input v-model="form.seo_title" :maxlength="CONTENT_LIMITS.seoTitle" :readonly="!editable" />
                  <div class="count" :class="{ warn: seoTitleWarn }">
                    {{ t("seo.titleCount", { n: form.seo_title.trim().length, max: CONTENT_LIMITS.seoTitleSuggest }) }}
                  </div>
                </el-form-item>
                <el-form-item :label="t('seo.seoDescription')">
                  <el-input v-model="form.seo_description" type="textarea" :autosize="{ minRows: 2, maxRows: 5 }" :maxlength="CONTENT_LIMITS.seoDescription" :readonly="!editable" />
                  <div class="count" :class="{ warn: seoDescWarn }">
                    {{ t("seo.descCount", { n: seoDescLen, min: CONTENT_LIMITS.seoDescriptionMin, max: CONTENT_LIMITS.seoDescriptionMax }) }}
                  </div>
                </el-form-item>
                <el-form-item :label="t('seo.seoKeywords')">
                  <el-input-tag
                    v-model="form.seo_keywords"
                    :max="CONTENT_LIMITS.seoKeywords"
                    :maxlength="CONTENT_LIMITS.seoKeyword"
                    :readonly="!editable"
                    trigger="Enter"
                    delimiter=","
                    clearable
                    :placeholder="t('seo.keywordsPlaceholder')"
                  />
                </el-form-item>
                <el-form-item :label="t('seo.faq', { n: form.faq.length })">
                  <FaqEditor v-model="form.faq" :readonly="!editable" />
                </el-form-item>
              </el-form>
            </el-collapse-item>

            <el-collapse-item name="assets" :title="t('editor.panels.assets', { n: assets.length })">
              <div v-loading="assetsLoading || attaching" class="panel">
                <!-- 封面区：cover_asset_id 对应资产 -->
                <div class="cover-box">
                  <span class="cover-box__label">{{ t("editor.assets.cover") }}</span>
                  <el-image
                    v-if="coverAsset && (coverAsset.thumbnail_url || coverAsset.url)"
                    :src="coverAsset.thumbnail_url || coverAsset.url || ''"
                    fit="cover"
                    class="cover-box__img"
                    :preview-src-list="coverAsset.url ? [coverAsset.url] : []"
                    preview-teleported
                  >
                    <template #error><div class="asset__broken">{{ t("editor.assets.broken") }}</div></template>
                  </el-image>
                  <span v-else-if="content.cover_asset_id" class="text-secondary">#{{ content.cover_asset_id }}</span>
                  <span v-else class="text-secondary">{{ t("editor.assets.noCover") }}</span>
                  <el-button v-if="canGenerateImage" size="small" link type="primary" :icon="MagicStick" @click="gotoImageGenerate('cover')">
                    {{ t("editor.assets.generateCover") }}
                  </el-button>
                </div>

                <div v-for="(a, idx) in sortedAssets" :key="a.id" class="asset">
                  <el-image
                    v-if="a.kind === 'image' && (a.thumbnail_url || a.url)"
                    :src="a.thumbnail_url || a.url || ''"
                    fit="cover"
                    class="asset__thumb"
                    :preview-src-list="a.url ? [a.url] : []"
                    preview-teleported
                  >
                    <template #error><div class="asset__broken">{{ t("editor.assets.broken") }}</div></template>
                  </el-image>
                  <video v-else-if="a.kind === 'video' && a.url" :src="a.url" preload="metadata" muted class="asset__thumb asset__video" />
                  <div v-else class="asset__thumb asset__placeholder"><el-icon><component :is="a.kind === 'video' ? VideoCamera : Picture" /></el-icon></div>
                  <div class="asset__info">
                    <div class="asset__line">
                      <span class="mono">#{{ a.id }}</span>
                      <el-tag v-if="content.cover_asset_id === a.id" size="small" type="success">{{ t("editor.assets.cover") }}</el-tag>
                      <StatusTag v-else kind="media_usage_type" :value="a.usage_type" effect="plain" />
                      <StatusTag v-if="a.kind === 'video'" kind="media_kind" :value="a.kind" effect="plain" />
                      <StatusTag v-if="a.status !== 'ready'" kind="media_status" :value="a.status" />
                    </div>
                    <div class="asset__prompt text-secondary" :title="a.prompt ?? ''">{{ a.prompt || "-" }}</div>
                    <div class="asset__ops">
                      <el-button link type="primary" size="small" :disabled="!editable || a.status !== 'ready' || !a.url" @click="insertAsset(a)">
                        {{ t("editor.assets.insert") }}
                      </el-button>
                      <template v-if="has('content.contents.update')">
                        <el-button
                          v-if="a.kind === 'image' && content.cover_asset_id !== a.id"
                          link
                          type="primary"
                          size="small"
                          :disabled="a.status !== 'ready' || attaching"
                          @click="attachAsset(a.id, 'cover')"
                        >
                          {{ t("editor.assets.setCover") }}
                        </el-button>
                        <el-button link size="small" :icon="Top" :disabled="idx === 0 || attaching || a.status !== 'ready'" :title="t('editor.assets.moveUp')" @click="moveAsset(a, -1)" />
                        <el-button link size="small" :icon="Bottom" :disabled="idx === sortedAssets.length - 1 || attaching || a.status !== 'ready'" :title="t('editor.assets.moveDown')" @click="moveAsset(a, 1)" />
                        <el-button link type="danger" size="small" :disabled="attaching" @click="detachAsset(a)">{{ t("editor.assets.detach") }}</el-button>
                      </template>
                    </div>
                  </div>
                </div>
                <span v-if="!assetsLoading && !assets.length" class="text-secondary">{{ t("editor.assets.empty") }}</span>
                <div class="attach-row">
                  <el-button v-if="has('content.contents.update') && has('media.assets.view')" size="small" type="primary" plain :icon="FolderOpened" @click="pickerVisible = true">
                    {{ t("editor.assets.pick") }}
                  </el-button>
                  <el-button v-if="canGenerateImage" size="small" :icon="Picture" @click="gotoImageGenerate('inline')">
                    {{ t("editor.assets.generateImage") }}
                  </el-button>
                  <el-button size="small" text :icon="Refresh" @click="loadAssets" />
                </div>
                <AssetPicker
                  v-if="has('content.contents.update') && has('media.assets.view')"
                  v-model="pickerVisible"
                  mode="content"
                  :project-id="content.project_id"
                  multiple
                  :max="20"
                  with-usage
                  :exclude-ids="boundIds"
                  @select="onAssetsPicked"
                />
              </div>
            </el-collapse-item>

            <el-collapse-item name="links" :title="t('editor.panels.links', { n: content.link_count })">
              <div v-loading="linksLoading" class="panel">
                <div v-for="l in links" :key="l.id" class="link-item">
                  <a :href="l.url" target="_blank" rel="noopener noreferrer nofollow" class="link-item__url">{{ l.url }}</a>
                  <div class="link-item__meta">
                    <span>{{ platformLabel(l.platform ?? null, l.platform_id) }}</span>
                    <StatusTag kind="link_alive_status" :value="l.alive_status" />
                    <span class="text-secondary">{{ formatDateTime(l.published_at, false) }}</span>
                    <router-link v-if="has('publish.links.view')" :to="`/links/${l.id}`">{{ t("editor.links.detail") }}</router-link>
                  </div>
                  <div class="link-item__index">
                    <span class="text-secondary">SEO</span>
                    <LinkIndexBadges :link="l" kind="seo" />
                    <span class="text-secondary">GEO</span>
                    <LinkIndexBadges :link="l" kind="geo" />
                  </div>
                </div>
                <span v-if="!linksLoading && !links.length" class="text-secondary">{{ t("editor.links.empty") }}</span>
                <div v-if="links.length && has('publish.links.view')" class="link-panel-more">
                  <router-link :to="{ path: '/links', query: { content_id: String(content.id) } }">{{ t("editor.links.viewAll") }}</router-link>
                </div>
                <el-button v-if="allowed('backfill') && has('publish.links.create')" size="small" type="primary" plain @click="openBackfill">
                  {{ t("editor.links.backfill") }}
                </el-button>
                <span v-else-if="has('publish.links.create')" class="text-secondary hint">{{ t("editor.links.backfillHint") }}</span>
              </div>
            </el-collapse-item>

            <el-collapse-item name="info" :title="t('editor.panels.info')">
              <el-descriptions :column="1" size="small" border class="panel">
                <el-descriptions-item :label="t('editor.info.project')">{{ projectName }}</el-descriptions-item>
                <el-descriptions-item :label="t('contents.mainKeyword')">
                  <router-link v-if="content.keyword_id" :to="`/titles?keyword_id=${content.keyword_id}`">#{{ content.keyword_id }}</router-link>
                  <span v-else>-</span>
                </el-descriptions-item>
                <el-descriptions-item :label="t('editor.info.title')">{{ content.title_id ? `#${content.title_id}` : "-" }}</el-descriptions-item>
                <el-descriptions-item :label="t('contents.format')">{{ t(`status.content_format.${content.format}`) }}</el-descriptions-item>
                <el-descriptions-item :label="t('contents.style')"><StatusTag kind="content_style" :value="content.style" effect="plain" /></el-descriptions-item>
                <el-descriptions-item :label="t('editor.info.language')">{{ content.language }}</el-descriptions-item>
                <el-descriptions-item v-if="content.generation_params" :label="t('editor.info.params')">
                  <span class="mono">
                    {{ t("editor.info.paramsText", {
                      words: content.generation_params.target_word_count ?? "-",
                      outline: content.generation_params.outline_first ? t("common.yes") : t("common.no"),
                      segmented: content.generation_params.segmented ? t("common.yes") : t("common.no"),
                    }) }}
                  </span>
                </el-descriptions-item>
                <el-descriptions-item :label="t('editor.info.batch')">
                  <router-link v-if="content.batch_id && has('content.batches.view')" to="/generation-batches">#{{ content.batch_id }}</router-link>
                  <span v-else>{{ content.batch_id ? `#${content.batch_id}` : "-" }}</span>
                </el-descriptions-item>
                <el-descriptions-item v-if="content.review_result" :label="t('editor.info.review')">
                  <StatusTag kind="review_result" :value="content.review_result" />
                  <span class="text-secondary"> {{ formatDateTime(content.reviewed_at) }}</span>
                  <div v-if="content.review_note" class="review-note">{{ content.review_note }}</div>
                </el-descriptions-item>
                <el-descriptions-item :label="t('common.createdAt')">{{ formatDateTime(content.created_at) }}</el-descriptions-item>
                <el-descriptions-item :label="t('common.updatedAt')">{{ formatDateTime(content.updated_at) }}</el-descriptions-item>
              </el-descriptions>
            </el-collapse-item>
          </el-collapse>
        </el-card>
      </div>

      <!-- 生成对话框 -->
      <el-dialog v-model="genVisible" :title="t(`editor.gen.${genKind}`)" width="min(520px, 96vw)" :close-on-click-modal="false">
        <GenerateNotice :notice="guard.notice.value" :quota-warning="guard.quotaWarning.value" @close="guard.notice.value = null" />
        <el-form label-width="110px" @submit.prevent="submitGenerate">
          <template v-if="genKind === 'body'">
            <el-form-item :label="t('editor.gen.segmented')">
              <el-switch v-model="genForm.segmented" />
            </el-form-item>
            <el-alert
              v-if="genForm.segmented && !savedOutline.length"
              type="info"
              :title="t('editor.gen.noOutlineHint')"
              :closable="false"
              show-icon
              class="mb"
            />
            <el-alert v-if="hasBody" type="warning" :title="t('editor.gen.overwriteHint')" :closable="false" show-icon class="mb" />
          </template>
          <el-form-item v-if="genKind === 'seo'" :label="t('editor.gen.includeFaq')">
            <el-switch v-model="genForm.include_faq" />
          </el-form-item>
          <el-form-item v-if="has('content.prompt_templates.view')" :label="t('generation.template')" :error="guard.fields.value.template_id">
            <TemplateSelect v-model="genForm.template_id" :kinds="genTemplateKinds" :project-id="content.project_id" />
          </el-form-item>
          <el-form-item v-if="has('ai.models.view')" :label="t('generation.model')" :error="guard.fields.value.model">
            <ModelSelect v-model="genForm.model" modality="text" allow-empty />
          </el-form-item>
          <div class="form-hint">{{ t(`editor.gen.${genKind}Hint`) }}</div>
        </el-form>
        <template #footer>
          <el-button @click="genVisible = false">{{ t("common.cancel") }}</el-button>
          <el-button type="primary" :loading="guard.submitting.value" :disabled="guard.cooldown.value > 0" @click="submitGenerate">
            {{ guard.cooldown.value > 0 ? t("generation.retryIn", { seconds: guard.cooldown.value }) : t("editor.gen.submit") }}
          </el-button>
        </template>
      </el-dialog>

      <!-- 重写对话框 -->
      <el-dialog v-model="rewriteVisible" :title="t('editor.rewrite.title')" width="min(600px, 96vw)" :close-on-click-modal="false">
        <GenerateNotice :notice="rewriteGuard.notice.value" :quota-warning="rewriteGuard.quotaWarning.value" @close="rewriteGuard.notice.value = null" />
        <el-form label-width="100px" @submit.prevent="submitRewrite">
          <el-form-item :label="t('editor.rewrite.mode')" :error="rewriteGuard.fields.value.mode">
            <el-radio-group v-model="rewriteForm.mode">
              <el-radio-button v-for="m in rewriteModes" :key="m" :value="m">{{ t(`status.rewrite_mode.${m}`) }}</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item :label="t('editor.rewrite.scope')" :error="rewriteGuard.fields.value.scope">
            <el-radio-group v-model="rewriteForm.scope">
              <el-radio value="full">{{ t("status.rewrite_scope.full") }}</el-radio>
              <el-radio value="section" :disabled="!savedOutline.length">{{ t("status.rewrite_scope.section") }}</el-radio>
            </el-radio-group>
            <span v-if="!savedOutline.length" class="form-hint inline">{{ t("editor.rewrite.noOutline") }}</span>
          </el-form-item>
          <el-form-item v-if="rewriteForm.scope === 'section'" :label="t('editor.rewrite.section')" :error="rewriteGuard.fields.value.section_index" required>
            <el-select v-model="rewriteForm.section_index" style="width: 100%" :placeholder="t('editor.rewrite.sectionPlaceholder')">
              <el-option v-for="opt in sectionOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
            </el-select>
          </el-form-item>
          <el-form-item v-if="rewriteForm.mode === 'restyle'" :label="t('contents.style')" :error="rewriteGuard.fields.value.style" required>
            <el-select v-model="rewriteForm.style" style="width: 200px">
              <el-option v-for="s in CONTENT_STYLE" :key="s" :value="s" :label="t(`status.content_style.${s}`)" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('editor.rewrite.instruction')" :error="rewriteGuard.fields.value.instruction">
            <el-input
              v-model="rewriteForm.instruction"
              type="textarea"
              :autosize="{ minRows: 2, maxRows: 5 }"
              :maxlength="CONTENT_LIMITS.instruction"
              show-word-limit
              :placeholder="t('editor.rewrite.instructionPlaceholder')"
            />
          </el-form-item>
          <el-form-item v-if="has('content.prompt_templates.view')" :label="t('generation.template')" :error="rewriteGuard.fields.value.template_id">
            <TemplateSelect v-model="rewriteForm.template_id" :kinds="[rewriteForm.mode]" :project-id="content.project_id" />
          </el-form-item>
          <el-form-item v-if="has('ai.models.view')" :label="t('generation.model')" :error="rewriteGuard.fields.value.model">
            <ModelSelect v-model="rewriteForm.model" modality="text" allow-empty />
          </el-form-item>
          <div class="form-hint">
            {{ ["approved", "published"].includes(content.status) ? t("editor.rewrite.keepStatusHint") : t("editor.rewrite.statusHint") }}
          </div>
        </el-form>
        <template #footer>
          <el-button @click="rewriteVisible = false">{{ t("common.cancel") }}</el-button>
          <el-button type="primary" :loading="rewriteGuard.submitting.value" :disabled="rewriteGuard.cooldown.value > 0" @click="submitRewrite">
            {{ rewriteGuard.cooldown.value > 0 ? t("generation.retryIn", { seconds: rewriteGuard.cooldown.value }) : t("editor.rewrite.submit") }}
          </el-button>
        </template>
      </el-dialog>

      <!-- 审核意见 -->
      <el-dialog v-model="reviewVisible" :title="reviewType === 'approve' ? t('contents.review.approve') : t('contents.review.reject')" width="min(480px, 96vw)">
        <el-input
          v-model="reviewNote"
          type="textarea"
          :rows="4"
          maxlength="500"
          show-word-limit
          :placeholder="reviewType === 'approve' ? t('contents.review.notePlaceholder') : t('contents.review.rejectPlaceholder')"
        />
        <div v-if="reviewError" class="review-error">{{ reviewError }}</div>
        <template #footer>
          <el-button @click="reviewVisible = false">{{ t("common.cancel") }}</el-button>
          <el-button :type="reviewType === 'approve' ? 'success' : 'danger'" :loading="acting" @click="confirmReview">
            {{ reviewType === "approve" ? t("contents.review.approve") : t("contents.review.reject") }}
          </el-button>
        </template>
      </el-dialog>

      <ContentVersionsDrawer
        v-model="versionsVisible"
        :content-id="content.id"
        :current-version-id="content.current_version_id"
        :format="content.format"
        :locked="locked"
        :dirty="dirty"
        @restored="onVersionRestored"
      />

      <LinkBackfillDialog
        v-model="backfillVisible"
        :content-id="content.id"
        :content-title="content.title"
        :project-id="content.project_id"
        @created="onBackfilled"
      />
    </template>
  </div>
</template>

<style scoped>
.editor-page {
  min-height: 300px;
}
.toolbar-card :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.editor-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.editor-title {
  flex: 1;
  min-width: 240px;
}
.editor-title :deep(.el-input__inner) {
  font-size: 16px;
  font-weight: 600;
}
.editor-head .meta {
  font-size: 12px;
}
.editor-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.editor-actions > .el-button,
.editor-actions > span > .el-button {
  margin-left: 0;
}
.editor-actions .spacer {
  flex: 1;
}
.stale-alert {
  padding: 6px 12px;
}
.editor-body {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-top: 12px;
}
.editor-body .page-card + .page-card {
  margin-top: 0;
}
.main-card {
  flex: 1;
  min-width: 0;
}
.side-card {
  flex: 0 0 380px;
  max-width: 380px;
  max-height: calc(100vh - 200px);
  overflow-y: auto;
}
.edit-area {
  position: relative;
  display: grid;
  gap: 12px;
  min-height: 420px;
}
.edit-area.is-split {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}
.edit-area.is-edit,
.edit-area.is-preview {
  grid-template-columns: minmax(0, 1fr);
}
.edit-area__editor,
.edit-area__preview {
  min-width: 0;
}
.edit-mask {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
  background: color-mix(in srgb, var(--el-bg-color) 70%, transparent);
  backdrop-filter: blur(1px);
}
.edit-mask__box {
  width: min(420px, 90%);
  padding: 16px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  background: var(--el-bg-color);
  box-shadow: var(--el-box-shadow-light);
}
.edit-mask__title {
  margin-bottom: 10px;
  font-weight: 600;
}
.panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.panel > .el-button {
  align-self: flex-start;
  margin-left: 0;
}
.panel-link {
  color: var(--el-color-primary);
  font-size: 13px;
}
.pending-task + .pending-task {
  margin-top: 6px;
}
.count {
  width: 100%;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.count.warn {
  color: var(--el-color-warning);
}
.asset {
  display: flex;
  gap: 8px;
  padding: 6px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}
.asset__thumb {
  flex: none;
  width: 72px;
  height: 72px;
  border-radius: 4px;
  overflow: hidden;
}
.asset__placeholder,
.asset__broken {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.asset__info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.asset__line {
  display: flex;
  align-items: center;
  gap: 4px;
}
.asset__prompt {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.asset__ops .el-button + .el-button {
  margin-left: 8px;
}
.attach-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.asset__thumb.asset__placeholder {
  width: 72px;
  height: 72px;
}
.asset__video {
  object-fit: cover;
  background: #000;
}
.cover-box {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 6px;
  border: 1px dashed var(--el-border-color);
  border-radius: 6px;
}
.cover-box__label {
  font-size: 12px;
  font-weight: 600;
}
.cover-box__img {
  width: 120px;
  height: 68px;
  border-radius: 4px;
  overflow: hidden;
}
.attach-row .el-button {
  margin-left: 0;
}
.link-item {
  padding: 6px 0;
  border-bottom: 1px solid var(--el-border-color-extra-light);
  font-size: 13px;
}
.link-item__url {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--el-color-primary);
}
.link-item__index {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 6px;
  margin-top: 4px;
  font-size: 12px;
}
.link-panel-more {
  margin: 6px 0;
  font-size: 12px;
}
.link-item__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
  font-size: 12px;
}
.hint {
  font-size: 12px;
}
.review-note {
  margin-top: 4px;
  white-space: pre-wrap;
}
.review-error {
  margin-top: 6px;
  color: var(--el-color-danger);
  font-size: 12px;
}
.form-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.form-hint.inline {
  margin-left: 8px;
}
.mb {
  margin-bottom: 12px;
}
@media (max-width: 1200px) {
  .editor-body {
    flex-direction: column;
  }
  .side-card {
    flex: none;
    width: 100%;
    max-width: none;
    max-height: none;
  }
  .edit-area.is-split {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
