<script setup lang="ts">
// 检测证据抽屉（docs/11 §11.3、§11.6；docs/04 §7.12）：
// - kind=link：删除检测记录（结果 / 前后状态 / HTTP / 规则 / 指纹）+ evidence（marker / context / redirects / headers / title / text_excerpt）；
// - kind=index：收录检测记录（引擎 / 提供器 / 匹配方式 / 置信度 / 模型 / request_id / ai_task_id → AI 任务页）+ evidence.citations、matched、answer_excerpt；
// - 底部为只读 JSON（JsonEditor readonly）展示完整 evidence。抓取内容与模型回答只以文本插值渲染。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { CopyDocument } from "@element-plus/icons-vue";
import type { IndexCheck, IndexCheckEvidence, LinkCheck } from "@aicreat/shared";
import JsonEditor from "@/components/JsonEditor.vue";
import LinkEvidencePanel from "@/components/LinkEvidencePanel.vue";
import StatusTag from "@/components/StatusTag.vue";
import { copyText } from "@/composables/useAssetActions";
import { engineLabel } from "@/composables/useIndexEngines";
import { useNarrow } from "@/composables/useNarrow";
import { usePermission } from "@/composables/usePermission";
import { formatDateTime, formatDuration, formatNumber } from "@/utils/format";
import { simhashHex } from "@/utils/links";

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    kind: "link" | "index";
    record: LinkCheck | IndexCheck | null;
    loading?: boolean;
    /** 显示记录所属链接（监控页全局记录附带 `link`）与跳转链接详情 */
    showLink?: boolean;
  }>(),
  { loading: false, showLink: true },
);

const emit = defineEmits<{ (e: "update:modelValue", value: boolean): void }>();

const { t } = useI18n();
const { has } = usePermission();
const narrow = useNarrow();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const linkCheck = computed(() => (props.kind === "link" ? (props.record as LinkCheck | null) : null));
const indexCheck = computed(() => (props.kind === "index" ? (props.record as IndexCheck | null) : null));

const title = computed(() => {
  const id = props.record?.id;
  const base = props.kind === "link" ? t("evidence.linkTitle") : t("evidence.indexTitle");
  return id ? `${base} #${id}` : base;
});

const indexEvidence = computed<IndexCheckEvidence | null>(() => indexCheck.value?.evidence ?? null);
const indexEnum = computed(() => (indexCheck.value?.kind === "geo" ? "geo_cite_status" : "seo_index_status") as "geo_cite_status" | "seo_index_status");

interface Citation {
  url: string;
  title: string | null;
  snippet: string | null;
  source: string | null;
  matched: boolean;
}

const matched = computed(() => {
  const m = indexEvidence.value?.matched;
  if (!m || typeof m !== "object") return null;
  const obj = m as Record<string, unknown>;
  return { mode: typeof obj.mode === "string" ? obj.mode : null, candidate: typeof obj.candidate === "string" ? obj.candidate : null };
});

const citations = computed<Citation[]>(() => {
  const list = indexEvidence.value?.citations;
  if (!Array.isArray(list)) return [];
  const candidate = matched.value?.candidate ?? indexCheck.value?.evidence_url ?? null;
  return list
    .filter((c) => c && typeof c.url === "string")
    .map((c) => ({
      url: c.url,
      title: c.title ?? null,
      snippet: c.snippet ?? null,
      source: c.source ?? null,
      matched: !!candidate && c.url === candidate,
    }));
});

const parsed = computed(() => {
  const p = indexEvidence.value?.parsed;
  return p === undefined || p === null ? null : p;
});

function flag(key: string): boolean | null {
  const v = indexEvidence.value?.[key];
  return typeof v === "boolean" ? v : null;
}

const titleSimilarity = computed(() => {
  const v = indexEvidence.value?.title_similarity;
  return typeof v === "number" ? v : null;
});

const linkInfo = computed(() => props.record?.link ?? null);

function aiTaskRoute(id: number) {
  return { path: "/ai/tasks", query: { open: String(id), row_kind: "attempt" } };
}

function citationRowClass({ row }: { row: Citation }): string {
  return row.matched ? "is-matched" : "";
}
</script>

<template>
  <el-drawer v-model="visible" :title="title" size="min(720px, 96vw)" destroy-on-close>
    <div v-loading="loading" class="evidence-drawer">
      <template v-if="record">
        <div v-if="showLink && linkInfo" class="evidence-link">
          <span class="text-secondary">{{ t("evidence.link") }}</span>
          <router-link :to="`/links/${linkInfo.id}`">#{{ linkInfo.id }}</router-link>
          <span v-if="linkInfo.platform_code" class="text-secondary">{{ linkInfo.platform_code }}</span>
          <a :href="linkInfo.url" target="_blank" rel="noopener noreferrer nofollow" class="evidence-link__url">{{ linkInfo.url }}</a>
        </div>

        <!-- 删除检测 -->
        <template v-if="linkCheck">
          <el-descriptions :column="narrow ? 1 : 2" size="small" border class="evidence-block">
            <el-descriptions-item :label="t('evidence.checkType')"><StatusTag kind="check_type" :value="linkCheck.check_type" effect="plain" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.checkedAt')">{{ formatDateTime(linkCheck.checked_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.result')"><StatusTag kind="link_check_result" :value="linkCheck.result_status" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.transition')">
              <StatusTag kind="link_alive_status" :value="linkCheck.previous_status" effect="plain" />
              <span class="arrow">→</span>
              <StatusTag kind="link_alive_status" :value="linkCheck.applied_status" />
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.httpStatus')">{{ linkCheck.http_status ?? "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.matchedRule')"><StatusTag kind="link_check_rule" :value="linkCheck.matched_rule" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.finalUrl')" :span="2">
              <span class="mono break">{{ linkCheck.final_url || "-" }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.redirectCount')">{{ linkCheck.redirect_count ?? 0 }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.duration')">{{ formatDuration(linkCheck.duration_ms) }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.pageTitle')" :span="2">{{ linkCheck.title || "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.simhash')">
              <span class="mono">{{ simhashHex(linkCheck.simhash) }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.hamming')">{{ linkCheck.hamming_distance ?? "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.responseBytes')">{{ formatNumber(linkCheck.response_bytes) }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.triggeredBy')">{{ linkCheck.triggered_by ? `#${linkCheck.triggered_by}` : t("evidence.system") }}</el-descriptions-item>
            <el-descriptions-item v-if="linkCheck.error_message" :label="t('evidence.errorMessage')" :span="2">
              <span class="error-text">{{ linkCheck.error_message }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="evidence-section">{{ t("evidence.evidence") }}</div>
          <LinkEvidencePanel :evidence="linkCheck.evidence" />
        </template>

        <!-- 收录检测 -->
        <template v-if="indexCheck">
          <el-descriptions :column="narrow ? 1 : 2" size="small" border class="evidence-block">
            <el-descriptions-item :label="t('evidence.kindEngine')">
              <StatusTag kind="index_kind" :value="indexCheck.kind" effect="plain" />
              <span class="engine">{{ engineLabel(indexCheck.kind, indexCheck.engine) }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.checkedAt')">{{ formatDateTime(indexCheck.checked_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.result')"><StatusTag :kind="indexEnum" :value="indexCheck.result_status" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.previousStatus')"><StatusTag :kind="indexEnum" :value="indexCheck.previous_status" effect="plain" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.provider')">
              <StatusTag :kind="indexCheck.kind === 'geo' ? 'geo_provider' : 'seo_provider'" :value="indexCheck.provider" effect="plain" />
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.checkType')"><StatusTag kind="check_type" :value="indexCheck.check_type" effect="plain" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.matchMode')"><StatusTag kind="index_match_mode" :value="indexCheck.match_mode" effect="plain" /></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.confidence')">{{ indexCheck.confidence == null ? "-" : formatNumber(indexCheck.confidence, 2) }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.model')"><span class="mono">{{ indexCheck.model || "-" }}</span></el-descriptions-item>
            <el-descriptions-item :label="t('evidence.duration')">{{ formatDuration(indexCheck.duration_ms) }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.requestId')">
              <template v-if="indexCheck.request_id">
                <span class="mono">{{ indexCheck.request_id }}</span>
                <el-button link size="small" :icon="CopyDocument" :title="t('common.copy')" @click="copyText(indexCheck.request_id)" />
              </template>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.aiTask')">
              <template v-if="indexCheck.ai_task_id">
                <router-link v-if="has('ai.tasks.view')" :to="aiTaskRoute(indexCheck.ai_task_id)">#{{ indexCheck.ai_task_id }}</router-link>
                <span v-else>#{{ indexCheck.ai_task_id }}</span>
              </template>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.triggeredBy')">{{ indexCheck.triggered_by ? `#${indexCheck.triggered_by}` : t("evidence.system") }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.errorCategory')"><StatusTag kind="error_category" :value="indexCheck.error_category" /></el-descriptions-item>
            <el-descriptions-item v-if="indexCheck.error_message" :label="t('evidence.errorMessage')" :span="2">
              <span class="error-text">{{ indexCheck.error_message }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.queryText')" :span="2">
              <div class="evidence-text">{{ indexCheck.query_text || "-" }}</div>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.evidenceTitle')" :span="2">{{ indexCheck.evidence_title || "-" }}</el-descriptions-item>
            <el-descriptions-item :label="t('evidence.evidenceSnippet')" :span="2">
              <div class="evidence-text">{{ indexCheck.evidence_snippet || "-" }}</div>
            </el-descriptions-item>
            <el-descriptions-item :label="t('evidence.evidenceUrl')" :span="2">
              <a v-if="indexCheck.evidence_url" :href="indexCheck.evidence_url" target="_blank" rel="noopener noreferrer nofollow" class="break">{{ indexCheck.evidence_url }}</a>
              <span v-else>-</span>
            </el-descriptions-item>
          </el-descriptions>

          <div class="evidence-section">{{ t("evidence.evidence") }}</div>
          <template v-if="indexEvidence">
            <el-descriptions :column="narrow ? 1 : 2" size="small" border class="evidence-block">
              <el-descriptions-item :label="t('evidence.source')">{{ indexEvidence.source || "-" }}</el-descriptions-item>
              <el-descriptions-item :label="t('evidence.matched')">
                <template v-if="matched && (matched.mode || matched.candidate)">
                  <StatusTag v-if="matched.mode" kind="index_match_mode" :value="matched.mode" effect="plain" />
                  <span class="mono break candidate">{{ matched.candidate || "" }}</span>
                </template>
                <span v-else>-</span>
              </el-descriptions-item>
              <el-descriptions-item v-if="flag('dropped') !== null" :label="t('evidence.dropped')">
                <el-tag v-if="flag('dropped')" type="warning" size="small" disable-transitions>{{ t("common.yes") }}</el-tag>
                <span v-else>{{ t("common.no") }}</span>
              </el-descriptions-item>
              <el-descriptions-item v-if="flag('domain_match_ignored') !== null" :label="t('evidence.domainMatchIgnored')">
                {{ flag("domain_match_ignored") ? t("common.yes") : t("common.no") }}
              </el-descriptions-item>
              <el-descriptions-item v-if="titleSimilarity !== null" :label="t('evidence.titleSimilarity')">{{ formatNumber(titleSimilarity, 3) }}</el-descriptions-item>
            </el-descriptions>

            <div class="evidence-sub">{{ t("evidence.citations", { n: citations.length }) }}</div>
            <el-table v-if="citations.length" :data="citations" size="small" border :row-class-name="citationRowClass" class="evidence-block">
              <el-table-column type="index" width="44" />
              <el-table-column :label="t('evidence.citationUrl')" min-width="220">
                <template #default="{ row }">
                  <a :href="row.url" target="_blank" rel="noopener noreferrer nofollow" class="break">{{ row.url }}</a>
                  <el-tag v-if="row.matched" type="success" size="small" class="matched-tag" disable-transitions>{{ t("evidence.hit") }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column :label="t('evidence.citationTitle')" min-width="140">
                <template #default="{ row }">{{ row.title || "-" }}</template>
              </el-table-column>
              <el-table-column :label="t('evidence.citationSnippet')" min-width="160">
                <template #default="{ row }"><span class="snippet">{{ row.snippet || "-" }}</span></template>
              </el-table-column>
              <el-table-column :label="t('evidence.citationSource')" width="110">
                <template #default="{ row }">{{ row.source || "-" }}</template>
              </el-table-column>
            </el-table>
            <span v-else class="text-secondary">{{ t("evidence.noCitations") }}</span>

            <template v-if="indexEvidence.answer_excerpt">
              <div class="evidence-sub">{{ t("evidence.answerExcerpt") }}</div>
              <pre class="evidence-pre">{{ indexEvidence.answer_excerpt }}</pre>
            </template>
            <template v-if="parsed !== null">
              <div class="evidence-sub">{{ t("evidence.parsed") }}</div>
              <pre class="evidence-pre">{{ JSON.stringify(parsed, null, 2) }}</pre>
            </template>
          </template>
          <el-empty v-else :description="t('evidence.none')" :image-size="60" />
        </template>

        <div class="evidence-section">{{ t("evidence.rawJson") }}</div>
        <JsonEditor :model-value="record.evidence ?? null" readonly :rows="12" />
      </template>
      <el-empty v-else-if="!loading" :description="t('common.noData')" />
    </div>
  </el-drawer>
</template>

<style scoped>
.evidence-drawer {
  min-height: 120px;
}
.evidence-link {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 13px;
}
.evidence-link__url {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.evidence-block {
  margin-bottom: 12px;
}
.evidence-block :deep(.el-descriptions__label) {
  width: 110px;
}
.evidence-section {
  margin: 16px 0 8px;
  font-size: 14px;
  font-weight: 600;
}
.evidence-sub {
  margin: 12px 0 6px;
  font-size: 13px;
  font-weight: 600;
}
.evidence-text {
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.6;
  max-height: 200px;
  overflow: auto;
}
.evidence-pre {
  margin: 0;
  padding: 8px 10px;
  max-height: 260px;
  overflow: auto;
  border-radius: 4px;
  background: var(--el-fill-color-light);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
.arrow {
  margin: 0 6px;
  color: var(--el-text-color-secondary);
}
.engine {
  margin-left: 6px;
}
.break {
  word-break: break-all;
}
.snippet {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
}
.candidate {
  margin-left: 6px;
}
.matched-tag {
  margin-left: 6px;
}
.error-text {
  color: var(--el-color-danger);
  word-break: break-all;
}
.evidence-drawer :deep(.el-table .is-matched) {
  --el-table-tr-bg-color: var(--el-color-success-light-9);
}
</style>
