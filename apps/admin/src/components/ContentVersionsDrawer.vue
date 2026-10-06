<script setup lang="ts">
// 内容版本抽屉（docs/09 §8.8、§10.6；docs/04 §6.11）：版本列表（版本号、来源、模型、字数、操作人、变更说明、时间）、
// VersionDiff 逐行对比任意两个版本的 body（按需拉取版本详情）、「恢复到此版本」（新建 source=restore 版本，generating 时禁用）、
// 「删除」（当前版本不可删）；版本的 ai_task_id 可跳转 ai/Tasks.vue（仅 has('ai.tasks.view')）。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import type { Content, ContentFormat, ContentVersion } from "@aicreat/shared";
import * as contentsApi from "@/api/contents";
import MarkdownPreview from "@/components/MarkdownPreview.vue";
import StatusTag from "@/components/StatusTag.vue";
import VersionDiff from "@/components/VersionDiff.vue";
import { usePermission } from "@/composables/usePermission";
import { useAuthStore } from "@/store/auth";
import { formatDateTime, formatNumber } from "@/utils/format";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    contentId: number;
    currentVersionId: number | null;
    format?: ContentFormat;
    /** 内容生成中：恢复 / 删除不可用 */
    locked?: boolean;
    /** 本地有未保存修改：恢复前需确认放弃 */
    dirty?: boolean;
  }>(),
  { format: "markdown", locked: false, dirty: false },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: boolean): void;
  (e: "restored", content: Content): void;
}>();

const { t } = useI18n();
const { has } = usePermission();
const auth = useAuthStore();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const versions = ref<ContentVersion[]>([]);
const loading = ref(false);
const details = new Map<number, Promise<ContentVersion>>();

const oldId = ref(0);
const newId = ref(0);
const oldVersion = ref<ContentVersion | null>(null);
const newVersion = ref<ContentVersion | null>(null);
const diffLoading = ref(false);
const view = ref<"diff" | "preview">("diff");

async function load() {
  loading.value = true;
  try {
    const list = await contentsApi.versions(props.contentId);
    versions.value = [...list].sort((a, b) => b.version_no - a.version_no);
    const current = props.currentVersionId ?? versions.value[0]?.id ?? 0;
    newId.value = current;
    oldId.value = versions.value.find((v) => v.id !== current)?.id ?? current;
    void loadDiff();
  } catch {
    versions.value = [];
  } finally {
    loading.value = false;
  }
}

function detail(id: number): Promise<ContentVersion> {
  let p = details.get(id);
  if (!p) {
    p = contentsApi.getVersion(props.contentId, id, { silent: true });
    details.set(id, p);
    p.catch(() => details.delete(id));
  }
  return p;
}

async function loadDiff() {
  if (!oldId.value || !newId.value) {
    oldVersion.value = null;
    newVersion.value = null;
    return;
  }
  diffLoading.value = true;
  try {
    const [a, b] = await Promise.all([detail(oldId.value), detail(newId.value)]);
    oldVersion.value = a;
    newVersion.value = b;
  } catch {
    ElMessage.error(t("versions.loadFailed"));
  } finally {
    diffLoading.value = false;
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      details.clear();
      void load();
    }
  },
);
watch([oldId, newId], () => void loadDiff());

function label(v: ContentVersion | null): string {
  if (!v) return "";
  return `v${v.version_no} · ${t(`status.version_source.${v.source}`)}`;
}

function creator(id: number | null): string {
  if (!id) return "-";
  return id === auth.admin?.id ? auth.displayName : `#${id}`;
}

function compareWithCurrent(v: ContentVersion) {
  oldId.value = v.id;
  newId.value = props.currentVersionId ?? v.id;
  view.value = "diff";
}

const acting = ref(0);

async function restore(v: ContentVersion) {
  try {
    await ElMessageBox.confirm(
      props.dirty ? t("versions.restoreConfirmDirty", { no: v.version_no }) : t("versions.restoreConfirm", { no: v.version_no }),
      t("common.tip"),
      { type: "warning" },
    );
  } catch {
    return;
  }
  acting.value = v.id;
  try {
    const content = await contentsApi.restoreVersion(props.contentId, v.id);
    if (content.version_created === false) ElMessage.info(t("versions.sameAsCurrent"));
    else ElMessage.success(t("versions.restored", { no: v.version_no }));
    emit("restored", content);
    details.clear();
    await load();
  } catch {
    /* 已提示 */
  } finally {
    acting.value = 0;
  }
}

async function removeVersion(v: ContentVersion) {
  try {
    await ElMessageBox.confirm(t("versions.deleteConfirm", { no: v.version_no }), t("common.warning"), { type: "error" });
  } catch {
    return;
  }
  acting.value = v.id;
  try {
    await contentsApi.deleteVersion(props.contentId, v.id);
    ElMessage.success(t("versions.deleted"));
    details.delete(v.id);
    await load();
  } catch {
    /* 已提示 */
  } finally {
    acting.value = 0;
  }
}
</script>

<template>
  <el-drawer v-model="visible" :title="t('versions.title')" size="min(1100px, 96vw)" append-to-body>
    <el-table v-loading="loading" :data="versions" row-key="id" size="small" max-height="300" highlight-current-row>
      <el-table-column :label="t('versions.no')" width="90">
        <template #default="{ row }">
          <span class="mono">v{{ row.version_no }}</span>
          <el-tag v-if="row.id === currentVersionId" size="small" type="success" class="ml">{{ t("versions.current") }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('versions.source')" width="90">
        <template #default="{ row }"><StatusTag kind="version_source" :value="row.source" effect="plain" /></template>
      </el-table-column>
      <el-table-column :label="t('versions.model')" min-width="120" show-overflow-tooltip>
        <template #default="{ row }"><span class="mono">{{ row.model || "-" }}</span></template>
      </el-table-column>
      <el-table-column :label="t('contents.wordCount')" width="80" align="right">
        <template #default="{ row }">{{ formatNumber(row.word_count) }}</template>
      </el-table-column>
      <el-table-column :label="t('versions.operator')" width="110" show-overflow-tooltip>
        <template #default="{ row }">{{ creator(row.created_by) }}</template>
      </el-table-column>
      <el-table-column :label="t('versions.changeSummary')" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">
          <span>{{ row.change_summary || "-" }}</span>
          <span v-if="row.restored_from_version_id" class="text-secondary"> · {{ t("versions.restoredFrom", { id: row.restored_from_version_id }) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.createdAt')" width="150">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.actions')" width="220" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :disabled="row.id === currentVersionId" @click="compareWithCurrent(row)">{{ t("versions.compare") }}</el-button>
          <template v-if="has('content.contents.update')">
            <el-button link type="primary" :disabled="locked || row.id === currentVersionId" :loading="acting === row.id" @click="restore(row)">
              {{ t("versions.restore") }}
            </el-button>
            <el-button link type="danger" :disabled="row.id === currentVersionId" :loading="acting === row.id" @click="removeVersion(row)">
              {{ t("common.delete") }}
            </el-button>
          </template>
          <router-link v-if="has('ai.tasks.view') && row.ai_task_id" :to="`/ai/tasks?target_type=content&target_id=${contentId}`" class="task-link">
            {{ t("versions.task") }}
          </router-link>
        </template>
      </el-table-column>
    </el-table>

    <div class="compare">
      <div class="compare__bar">
        <span>{{ t("versions.old") }}</span>
        <el-select v-model="oldId" size="small" style="width: 200px">
          <el-option v-for="v in versions" :key="v.id" :value="v.id" :label="label(v)" />
        </el-select>
        <span>{{ t("versions.new") }}</span>
        <el-select v-model="newId" size="small" style="width: 200px">
          <el-option v-for="v in versions" :key="v.id" :value="v.id" :label="label(v)" />
        </el-select>
        <span class="spacer" />
        <el-radio-group v-model="view" size="small">
          <el-radio-button value="diff">{{ t("versions.diffView") }}</el-radio-button>
          <el-radio-button value="preview">{{ t("versions.previewView") }}</el-radio-button>
        </el-radio-group>
      </div>
      <div v-loading="diffLoading" class="compare__body">
        <template v-if="oldVersion && newVersion">
          <div v-if="oldVersion.title !== newVersion.title" class="title-change">
            <span class="text-secondary">{{ t("versions.titleChanged") }}</span>
            <del>{{ oldVersion.title }}</del> → <b>{{ newVersion.title }}</b>
          </div>
          <VersionDiff
            v-if="view === 'diff'"
            :old-text="oldVersion.body ?? ''"
            :new-text="newVersion.body ?? ''"
            :old-label="label(oldVersion)"
            :new-label="label(newVersion)"
            max-height="calc(100vh - 520px)"
          />
          <div v-else class="preview-grid">
            <div>
              <div class="preview-label">{{ label(oldVersion) }}</div>
              <MarkdownPreview :source="oldVersion.body" :format="format" max-height="calc(100vh - 520px)" bordered />
            </div>
            <div>
              <div class="preview-label">{{ label(newVersion) }}</div>
              <MarkdownPreview :source="newVersion.body" :format="format" max-height="calc(100vh - 520px)" bordered />
            </div>
          </div>
        </template>
        <el-empty v-else-if="!diffLoading" :image-size="60" :description="t('versions.pick')" />
      </div>
    </div>
  </el-drawer>
</template>

<style scoped>
.ml {
  margin-left: 6px;
}
.task-link {
  margin-left: 8px;
  color: var(--el-color-primary);
  font-size: 13px;
}
.compare {
  margin-top: 14px;
}
.compare__bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 13px;
}
.compare__bar .spacer {
  flex: 1;
}
.compare__body {
  min-height: 160px;
}
.title-change {
  margin-bottom: 8px;
  font-size: 13px;
}
.preview-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.preview-label {
  margin-bottom: 4px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
