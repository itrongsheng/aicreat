<script lang="ts">
// 统一状态 → el-tag 颜色 / i18n 文案映射（读 @aicreat/shared 枚举；文案键 status.<enum>.<value>）
import {
  ADMIN_GROUP_CODE,
  AI_TASK_OPERATION,
  AI_TASK_STATUS,
  AI_TASK_TARGET_TYPE,
  AI_TASK_TRIGGER_TYPE,
  ALERT_CHANNEL,
  ALERT_SEVERITY,
  ALERT_STATUS,
  ALERT_TARGET_TYPE,
  ALERT_TYPE,
  BATCH_KIND,
  BATCH_STATUS,
  BREAKDOWN_DIMENSION,
  BREAKER_REASON,
  BREAKER_STATE,
  CAPABILITIES,
  CHECK_TYPE,
  CONTENT_FORMAT,
  CONTENT_STATUS,
  CONTENT_STYLE,
  DATA_SCOPE,
  ERROR_CATEGORY,
  GEO_CITE_STATUS,
  GEO_ENGINE,
  GEO_PROVIDER,
  HEALTH_STATUS,
  INDEX_KIND,
  INDEX_MATCH_MODE,
  KEYWORD_INTENT,
  KEYWORD_SOURCE,
  KEYWORD_STATUS,
  KEYWORD_TYPE,
  LINK_ALIVE_STATUS,
  LINK_CHECK_MARKER_PREFIX,
  LINK_CHECK_RESULT,
  LINK_CHECK_RULE,
  MEDIA_KIND,
  MEDIA_SOURCE,
  MEDIA_STATUS,
  MEDIA_USAGE_TYPE,
  MODALITY,
  OPERATION_ACTION,
  PAUSED_REASON,
  PERMISSION_TYPE,
  PROJECT_STATUS,
  PROMPT_KIND,
  PROMPT_OUTPUT_FORMAT,
  PROMPT_STATUS,
  PROTOCOL,
  RANKING_TYPE,
  REVIEW_RESULT,
  REWRITE_MODE,
  REWRITE_SCOPE,
  SEO_ENGINE,
  SEO_INDEX_STATUS,
  SEO_PROVIDER,
  STATS_DIMENSION,
  STATS_GRANULARITY,
  STATS_RANGE,
  TITLE_SOURCE,
  TITLE_STATUS,
  UPSTREAM_TASK_STATUS,
  VERSION_SOURCE,
  ZHIQI_MODE,
} from "@aicreat/shared";
import { t, te } from "@/i18n";

export type TagType = "primary" | "success" | "warning" | "danger" | "info";

/** 枚举名（snake_case，与 i18n status.<enum> 一致）→ 取值集合 */
export const STATUS_ENUMS = {
  project_status: PROJECT_STATUS,
  content_style: CONTENT_STYLE,
  content_format: CONTENT_FORMAT,
  prompt_kind: PROMPT_KIND,
  prompt_status: PROMPT_STATUS,
  prompt_output_format: PROMPT_OUTPUT_FORMAT,
  batch_kind: BATCH_KIND,
  batch_status: BATCH_STATUS,
  keyword_status: KEYWORD_STATUS,
  keyword_intent: KEYWORD_INTENT,
  keyword_type: KEYWORD_TYPE,
  keyword_source: KEYWORD_SOURCE,
  title_source: TITLE_SOURCE,
  title_status: TITLE_STATUS,
  content_status: CONTENT_STATUS,
  review_result: REVIEW_RESULT,
  version_source: VERSION_SOURCE,
  rewrite_mode: REWRITE_MODE,
  rewrite_scope: REWRITE_SCOPE,
  media_kind: MEDIA_KIND,
  media_usage_type: MEDIA_USAGE_TYPE,
  media_source: MEDIA_SOURCE,
  media_status: MEDIA_STATUS,
  upstream_task_status: UPSTREAM_TASK_STATUS,
  modality: MODALITY,
  protocol: PROTOCOL,
  capability: CAPABILITIES,
  ai_task_status: AI_TASK_STATUS,
  ai_task_operation: AI_TASK_OPERATION,
  ai_task_trigger_type: AI_TASK_TRIGGER_TYPE,
  ai_task_target_type: AI_TASK_TARGET_TYPE,
  error_category: ERROR_CATEGORY,
  health_status: HEALTH_STATUS,
  breaker_state: BREAKER_STATE,
  breaker_reason: BREAKER_REASON,
  paused_reason: PAUSED_REASON,
  zhiqi_mode: ZHIQI_MODE,
  link_alive_status: LINK_ALIVE_STATUS,
  link_check_result: LINK_CHECK_RESULT,
  link_check_rule: LINK_CHECK_RULE,
  check_type: CHECK_TYPE,
  index_kind: INDEX_KIND,
  seo_engine: SEO_ENGINE,
  geo_engine: GEO_ENGINE,
  seo_provider: SEO_PROVIDER,
  geo_provider: GEO_PROVIDER,
  seo_index_status: SEO_INDEX_STATUS,
  geo_cite_status: GEO_CITE_STATUS,
  index_match_mode: INDEX_MATCH_MODE,
  alert_type: ALERT_TYPE,
  alert_severity: ALERT_SEVERITY,
  alert_status: ALERT_STATUS,
  alert_target_type: ALERT_TARGET_TYPE,
  alert_channel: ALERT_CHANNEL,
  stats_dimension: STATS_DIMENSION,
  breakdown_dimension: BREAKDOWN_DIMENSION,
  stats_granularity: STATS_GRANULARITY,
  stats_range: STATS_RANGE,
  ranking_type: RANKING_TYPE,
  admin_group_code: ADMIN_GROUP_CODE,
  data_scope: DATA_SCOPE,
  permission_type: PERMISSION_TYPE,
  operation_action: OPERATION_ACTION,
  active: ["true", "false"] as const,
} as const;

export type StatusEnumName = keyof typeof STATUS_ENUMS;

const S: TagType = "success";
const W: TagType = "warning";
const D: TagType = "danger";
const I: TagType = "info";
const P: TagType = "primary";

/** 颜色映射；未列出的取值为 info */
const TAG_TYPES: Partial<Record<StatusEnumName, Record<string, TagType>>> = {
  project_status: { active: S, archived: I },
  prompt_status: { draft: W, published: S, archived: I },
  batch_status: { queued: I, running: P, succeeded: S, partial: W, failed: D, cancelled: I },
  keyword_status: { candidate: P, adopted: S, discarded: I },
  title_status: { candidate: P, adopted: S, discarded: I },
  content_status: { draft: I, generating: P, ready: P, reviewing: W, approved: S, rejected: D, published: S, archived: I },
  review_result: { approved: S, rejected: D },
  media_status: { pending: I, submitted: P, generating: P, downloading: P, ready: S, failed: D, expired: W, deleted: I },
  upstream_task_status: { queued: I, in_progress: P, succeeded: S, failed: D, expired: W },
  ai_task_status: { queued: I, running: P, polling: P, succeeded: S, failed: D, cancelled: I, expired: W },
  error_category: {
    unsupported_parameter: W,
    route_missing: W,
    model_unrouted: W,
    upstream_unavailable: D,
    rate_limited: W,
    timeout: W,
    quota_exceeded: D,
    auth_failed: D,
    content_blocked: D,
    media_storage: D,
    transfer_failed: D,
    invalid_response: W,
    breaker_open: W,
    cancelled: I,
    unknown: I,
  },
  health_status: { healthy: S, degraded: W, down: D, unknown: I },
  breaker_state: { closed: S, open: D, half_open: W },
  paused_reason: { quota_exceeded: D, auth_failed: D },
  zhiqi_mode: { mock: W, live: S },
  link_alive_status: { pending: I, alive: S, changed: W, suspected_deleted: W, deleted: D, unknown: I },
  link_check_result: { alive: S, changed: W, suspected_deleted: W, deleted: D, unknown: I },
  link_check_rule: {
    http_404: D,
    http_410: D,
    http_451: D,
    redirect_home: W,
    redirect_login: W,
    title_changed: W,
    body_changed: W,
    network_error: I,
    blocked_by_robots: I,
    ssrf_blocked: D,
    ok: S,
  },
  seo_index_status: { indexed: S, not_indexed: W, unknown: I },
  geo_cite_status: { cited: S, not_cited: W, unknown: I },
  alert_type: {
    link_deleted: D,
    link_restored: S,
    link_changed: W,
    index_overdue: W,
    ai_task_failures: D,
    ai_breaker_open: W,
    ai_quota_exceeded: D,
    ai_auth_failed: D,
    ai_upstream_unavailable: D,
    media_task_failed: W,
    worker_stale: D,
  },
  alert_severity: { info: I, warning: W, critical: D },
  alert_status: { open: D, acknowledged: W, resolved: S, ignored: I },
  data_scope: { all: P, own: I },
  admin_group_code: { super_admin: D, operator: P, reviewer: W, read_only: I },
  permission_type: { menu: P, action: I },
  operation_action: { create: S, update: P, update_status: W, delete: D, execute: P, login: I, logout: I, reset_password: W },
  active: { true: S, false: I },
};

function normalize(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

/** 枚举值的 el-tag 类型 */
export function statusTagType(kind: StatusEnumName, value: unknown): TagType {
  const v = normalize(value);
  if (kind === "link_check_rule" && v.startsWith(LINK_CHECK_MARKER_PREFIX)) return D;
  return TAG_TYPES[kind]?.[v] ?? I;
}

/** 枚举值的界面文案；无词条时原样返回取值（`marker:<文案>` 显示为文案本身） */
export function statusLabel(kind: StatusEnumName, value: unknown): string {
  const v = normalize(value);
  if (!v) return "-";
  if (kind === "link_check_rule" && v.startsWith(LINK_CHECK_MARKER_PREFIX)) return v.slice(LINK_CHECK_MARKER_PREFIX.length);
  const key = `status.${kind}.${v}`;
  return te(key) ? t(key) : v;
}

/** 下拉选项（ToolbarSelect 等） */
export function statusOptions(kind: StatusEnumName): { label: string; value: string }[] {
  return (STATUS_ENUMS[kind] as readonly (string | number)[]).map((v) => ({ label: statusLabel(kind, v), value: String(v) }));
}
</script>

<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";

const props = withDefaults(
  defineProps<{
    /** 枚举名，如 content_status / link_alive_status */
    kind: StatusEnumName;
    value: string | number | boolean | null | undefined;
    size?: "large" | "default" | "small";
    effect?: "dark" | "light" | "plain";
    round?: boolean;
  }>(),
  { size: "small", effect: "light", round: false },
);

const { locale } = useI18n();

const type = computed(() => statusTagType(props.kind, props.value));
// 依赖 locale 以便切换语言时重新计算文案
const label = computed(() => {
  void locale.value;
  return statusLabel(props.kind, props.value);
});
</script>

<template>
  <el-tag v-if="value !== null && value !== undefined && value !== ''" :type="type" :size="size" :effect="effect" :round="round" disable-transitions>
    {{ label }}
  </el-tag>
  <span v-else class="status-tag-empty">-</span>
</template>

<style scoped>
.status-tag-empty {
  color: var(--el-text-color-placeholder);
}
</style>
