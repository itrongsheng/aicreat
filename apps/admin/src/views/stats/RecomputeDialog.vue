<script setup lang="ts">
// 重算统计（docs/12 §4.5、§9.6、§11）：POST /admin/stats/recompute（stats.reports.recompute，前端超时 300s）。
// - 校验 start_date ≤ end_date ≤ 今日（统计时区）且跨度 ≤ 31 天；提交前二次确认（显示日期跨度与同步 / 异步提示）；
// - ≤ 7 天同步：显示天数、写入行数、耗时，skipped[] 非空时提示「以下日期正在被 worker 聚合，稍后自动完成」；
// - 8~31 天返回 202 入队：提示「已加入队列」；请求超时：提示「仍在后台执行」。完成后通知父组件刷新。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { ElMessage, ElMessageBox } from "element-plus";
import type { RecomputeResult } from "@aicreat/shared";
import * as statsApi from "@/api/stats";
import { isApiError } from "@/api/client";
import { formatDuration, formatNumber } from "@/utils/format";
import { addDays, isDateStr, spanDays } from "@/utils/stats";

const props = defineProps<{ modelValue: boolean; today: string }>();
const emit = defineEmits<{ (e: "update:modelValue", value: boolean): void; (e: "done"): void }>();

const { t } = useI18n();

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit("update:modelValue", v),
});

const range = ref<[string, string] | null>(null);
const submitting = ref(false);
const result = ref<RecomputeResult | null>(null);
const timedOut = ref(false);

watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      const yesterday = addDays(props.today, -1);
      range.value = [yesterday, yesterday];
      result.value = null;
      timedOut.value = false;
    }
  },
);

const days = computed(() => (range.value && isDateStr(range.value[0]) && isDateStr(range.value[1]) ? spanDays(range.value[0], range.value[1]) : 0));
const isSync = computed(() => days.value > 0 && days.value <= statsApi.SYNC_RECOMPUTE_DAYS);

const validation = computed<string | null>(() => {
  if (!range.value) return null;
  const end = range.value[1];
  if (end > props.today) return t("stats.recompute.futureDate", { today: props.today });
  if (days.value > statsApi.MAX_RECOMPUTE_DAYS) return t("stats.recompute.spanTooLong", { max: statsApi.MAX_RECOMPUTE_DAYS });
  return null;
});

function disabledDate(date: Date): boolean {
  const pad = (n: number) => String(n).padStart(2, "0");
  const s = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return s > props.today;
}

const skippedText = computed(() => {
  const r = result.value;
  return r && !statsApi.isQueuedRecompute(r) && r.skipped.length ? t("stats.recompute.skipped", { dates: r.skipped.join("、") }) : "";
});

async function submit() {
  if (!range.value || validation.value) return;
  const [start, end] = range.value;
  try {
    await ElMessageBox.confirm(
      t("stats.recompute.confirm", {
        start,
        end,
        days: days.value,
        mode: isSync.value ? t("stats.recompute.modeSync") : t("stats.recompute.modeAsync"),
      }),
      t("stats.recompute.title"),
      { type: "warning", confirmButtonText: t("stats.recompute.submit"), cancelButtonText: t("common.cancel") },
    );
  } catch {
    return;
  }
  submitting.value = true;
  result.value = null;
  timedOut.value = false;
  const startedAt = Date.now();
  try {
    const res = await statsApi.recompute({ start_date: start, end_date: end }, { silent: true });
    result.value = res;
    if (statsApi.isQueuedRecompute(res)) {
      ElMessage.success(t("stats.recompute.queued", { days: res.days }));
    } else {
      ElMessage.success(
        t("stats.recompute.done", { days: res.days, rows: formatNumber(res.rows_upserted), duration: formatDuration(res.duration_ms) }),
      );
    }
    emit("done");
  } catch (err) {
    // 网络层失败（无响应）且已接近 300s 超时：服务端仍在执行（docs/12 §12）
    if (isApiError(err) && err.status === 0 && Date.now() - startedAt >= statsApi.RECOMPUTE_TIMEOUT_MS - 5_000) {
      timedOut.value = true;
      ElMessage.warning(t("stats.recompute.timeout"));
    } else {
      ElMessage.error(err instanceof Error && err.message ? err.message : t("common.requestFailed"));
    }
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <el-dialog v-model="visible" :title="t('stats.recompute.title')" width="520px" :close-on-click-modal="!submitting" :close-on-press-escape="!submitting" :show-close="!submitting" append-to-body>
    <el-form label-width="100px" @submit.prevent>
      <el-form-item :label="t('stats.recompute.range')" :error="validation ?? undefined">
        <el-date-picker
          v-model="range"
          type="daterange"
          value-format="YYYY-MM-DD"
          format="YYYY-MM-DD"
          :disabled-date="disabledDate"
          :clearable="false"
          :disabled="submitting"
          style="width: 100%"
        />
      </el-form-item>
    </el-form>
    <el-alert
      type="info"
      :closable="false"
      show-icon
      :title="isSync ? t('stats.recompute.syncHint') : t('stats.recompute.asyncHint')"
      class="rc-alert"
    />
    <el-alert v-if="submitting" type="warning" :closable="false" show-icon :title="t('stats.recompute.running')" class="rc-alert" />
    <template v-if="result">
      <el-alert
        v-if="statsApi.isQueuedRecompute(result)"
        type="success"
        :closable="false"
        show-icon
        :title="t('stats.recompute.queued', { days: result.days })"
        class="rc-alert"
      />
      <template v-else>
        <el-alert
          type="success"
          :closable="false"
          show-icon
          :title="t('stats.recompute.done', { days: result.days, rows: formatNumber(result.rows_upserted), duration: formatDuration(result.duration_ms) })"
          class="rc-alert"
        />
        <el-alert v-if="skippedText" type="warning" :closable="false" show-icon :title="skippedText" class="rc-alert" />
      </template>
    </template>
    <el-alert v-if="timedOut" type="warning" :closable="false" show-icon :title="t('stats.recompute.timeout')" class="rc-alert" />
    <template #footer>
      <el-button :disabled="submitting" @click="visible = false">{{ t("common.close") }}</el-button>
      <el-button type="primary" :loading="submitting" :disabled="!range || !!validation" @click="submit">{{ t("stats.recompute.submit") }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.rc-alert + .rc-alert {
  margin-top: 8px;
}
</style>
