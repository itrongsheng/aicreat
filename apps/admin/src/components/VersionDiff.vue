<script setup lang="ts">
// 两个版本的文本差异对比（docs/02 §3.6、docs/09 §8.8、§10.2）：行级 Myers 差异 + 修改行内词级高亮；
// 并排 / 合并两种视图，长段未变化内容折叠（可展开）。用于内容版本抽屉与模板编辑器的版本面板。
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { diffLines, diffStats, type DiffRow, type InlineSegment } from "@/utils/diff";

const props = withDefaults(
  defineProps<{
    oldText: string | null | undefined;
    newText: string | null | undefined;
    oldLabel?: string;
    newLabel?: string;
    /** 初始视图 */
    mode?: "split" | "unified";
    /** 变化前后保留的上下文行数；超出部分折叠。-1 表示不折叠 */
    context?: number;
    /** 显示视图切换与统计 */
    header?: boolean;
    maxHeight?: string;
  }>(),
  { oldLabel: "", newLabel: "", mode: "split", context: 3, header: true, maxHeight: undefined },
);

const { t } = useI18n();
const view = ref<"split" | "unified">(props.mode);
watch(
  () => props.mode,
  (m) => (view.value = m),
);

const rows = computed<DiffRow[]>(() => diffLines(props.oldText ?? "", props.newText ?? ""));
const stats = computed(() => diffStats(rows.value));
const identical = computed(() => stats.value.added === 0 && stats.value.removed === 0);

/** 展开过的折叠块（按起始行下标） */
const expanded = ref(new Set<number>());
watch(rows, () => (expanded.value = new Set()));

type Block = { type: "rows"; rows: DiffRow[] } | { type: "fold"; start: number; rows: DiffRow[] };

const blocks = computed<Block[]>(() => {
  const all = rows.value;
  const ctx = props.context;
  if (ctx < 0) return [{ type: "rows", rows: all }];
  const keep = new Array<boolean>(all.length).fill(false);
  all.forEach((r, i) => {
    if (r.kind === "equal") return;
    for (let j = Math.max(0, i - ctx); j <= Math.min(all.length - 1, i + ctx); j += 1) keep[j] = true;
  });
  const out: Block[] = [];
  let i = 0;
  while (i < all.length) {
    if (keep[i]) {
      const chunk: DiffRow[] = [];
      while (i < all.length && keep[i]) chunk.push(all[i++]);
      out.push({ type: "rows", rows: chunk });
    } else {
      const start = i;
      const chunk: DiffRow[] = [];
      while (i < all.length && !keep[i]) chunk.push(all[i++]);
      // 太短的折叠没有意义
      if (chunk.length <= 2 || expanded.value.has(start)) out.push({ type: "rows", rows: chunk });
      else out.push({ type: "fold", start, rows: chunk });
    }
  }
  return out;
});

function expand(start: number) {
  const next = new Set(expanded.value);
  next.add(start);
  expanded.value = next;
}

/** 合并视图：replace 行拆为「删除行 + 新增行」 */
interface UnifiedLine {
  kind: "equal" | "insert" | "delete";
  oldNo: number | null;
  newNo: number | null;
  text: string;
  segments?: InlineSegment[];
}

function unified(list: DiffRow[]): UnifiedLine[] {
  const out: UnifiedLine[] = [];
  const pendingIns: UnifiedLine[] = [];
  const flush = () => {
    out.push(...pendingIns);
    pendingIns.length = 0;
  };
  for (const r of list) {
    if (r.kind === "replace") {
      out.push({ kind: "delete", oldNo: r.oldNo, newNo: null, text: r.oldText ?? "", segments: r.oldSegments });
      pendingIns.push({ kind: "insert", oldNo: null, newNo: r.newNo, text: r.newText ?? "", segments: r.newSegments });
      continue;
    }
    flush();
    if (r.kind === "equal") out.push({ kind: "equal", oldNo: r.oldNo, newNo: r.newNo, text: r.oldText ?? "" });
    else if (r.kind === "delete") out.push({ kind: "delete", oldNo: r.oldNo, newNo: null, text: r.oldText ?? "" });
    else out.push({ kind: "insert", oldNo: null, newNo: r.newNo, text: r.newText ?? "" });
  }
  flush();
  return out;
}

function sideClass(row: DiffRow, side: "old" | "new"): string {
  if (row.kind === "equal") return "";
  if (row.kind === "replace") return side === "old" ? "is-del" : "is-ins";
  if (row.kind === "delete") return side === "old" ? "is-del" : "is-empty";
  return side === "new" ? "is-ins" : "is-empty";
}
</script>

<template>
  <div class="version-diff">
    <div v-if="header" class="version-diff__header">
      <div class="version-diff__labels">
        <span v-if="oldLabel" class="version-diff__label is-old">{{ oldLabel }}</span>
        <span v-if="oldLabel || newLabel" class="text-secondary">→</span>
        <span v-if="newLabel" class="version-diff__label is-new">{{ newLabel }}</span>
      </div>
      <div class="version-diff__stats">
        <span class="is-added">+{{ stats.added }}</span>
        <span class="is-removed">-{{ stats.removed }}</span>
        <el-radio-group v-model="view" size="small">
          <el-radio-button value="split">{{ t("versionDiff.split") }}</el-radio-button>
          <el-radio-button value="unified">{{ t("versionDiff.unified") }}</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <div class="version-diff__body" :style="maxHeight ? { maxHeight, overflow: 'auto' } : undefined">
      <div v-if="identical" class="version-diff__identical">{{ t("versionDiff.identical") }}</div>

      <!-- 并排 -->
      <table v-else-if="view === 'split'" class="version-diff__table is-split">
        <colgroup>
          <col class="col-no" />
          <col />
          <col class="col-no" />
          <col />
        </colgroup>
        <tbody>
          <template v-for="(block, bi) in blocks" :key="bi">
            <tr v-if="block.type === 'fold'" class="version-diff__fold">
              <td colspan="4">
                <el-button link type="primary" size="small" @click="expand(block.start)">
                  {{ t("versionDiff.expand", { count: block.rows.length }) }}
                </el-button>
              </td>
            </tr>
            <template v-else>
            <tr v-for="(row, ri) in block.rows" :key="`${bi}-${ri}`">
              <td class="no" :class="sideClass(row, 'old')">{{ row.oldNo ?? "" }}</td>
              <td class="code" :class="sideClass(row, 'old')">
                <template v-if="row.kind === 'replace' && row.oldSegments">
                  <span v-for="(seg, si) in row.oldSegments" :key="si" :class="{ 'hl-del': seg.changed }">{{ seg.text }}</span>
                </template>
                <template v-else>{{ row.oldText ?? "" }}</template>
              </td>
              <td class="no" :class="sideClass(row, 'new')">{{ row.newNo ?? "" }}</td>
              <td class="code" :class="sideClass(row, 'new')">
                <template v-if="row.kind === 'replace' && row.newSegments">
                  <span v-for="(seg, si) in row.newSegments" :key="si" :class="{ 'hl-ins': seg.changed }">{{ seg.text }}</span>
                </template>
                <template v-else>{{ row.newText ?? "" }}</template>
              </td>
            </tr>
            </template>
          </template>
        </tbody>
      </table>

      <!-- 合并 -->
      <table v-else class="version-diff__table is-unified">
        <colgroup>
          <col class="col-no" />
          <col class="col-no" />
          <col class="col-sign" />
          <col />
        </colgroup>
        <tbody>
          <template v-for="(block, bi) in blocks" :key="bi">
            <tr v-if="block.type === 'fold'" class="version-diff__fold">
              <td colspan="4">
                <el-button link type="primary" size="small" @click="expand(block.start)">
                  {{ t("versionDiff.expand", { count: block.rows.length }) }}
                </el-button>
              </td>
            </tr>
            <template v-else>
            <tr
              v-for="(line, li) in unified(block.rows)"
              :key="`${bi}-${li}`"
              :class="{ 'is-del': line.kind === 'delete', 'is-ins': line.kind === 'insert' }"
            >
              <td class="no">{{ line.oldNo ?? "" }}</td>
              <td class="no">{{ line.newNo ?? "" }}</td>
              <td class="sign">{{ line.kind === "insert" ? "+" : line.kind === "delete" ? "-" : "" }}</td>
              <td class="code">
                <template v-if="line.segments">
                  <span
                    v-for="(seg, si) in line.segments"
                    :key="si"
                    :class="{ 'hl-del': seg.changed && line.kind === 'delete', 'hl-ins': seg.changed && line.kind === 'insert' }"
                  >{{ seg.text }}</span>
                </template>
                <template v-else>{{ line.text }}</template>
              </td>
            </tr>
            </template>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.version-diff {
  width: 100%;
  min-width: 0;
}
.version-diff__header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.version-diff__labels {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 13px;
}
.version-diff__label {
  padding: 1px 6px;
  border-radius: 3px;
}
.version-diff__label.is-old {
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}
.version-diff__label.is-new {
  background: var(--el-color-success-light-9);
  color: var(--el-color-success);
}
.version-diff__stats {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}
.version-diff__stats .is-added {
  color: var(--el-color-success);
}
.version-diff__stats .is-removed {
  color: var(--el-color-danger);
}
.version-diff__body {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 4px;
  overflow-x: auto;
}
.version-diff__identical {
  padding: 24px;
  color: var(--el-text-color-secondary);
  text-align: center;
}
.version-diff__table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12px;
  line-height: 1.6;
}
.version-diff__table .col-no {
  width: 44px;
}
.version-diff__table .col-sign {
  width: 18px;
}
.version-diff__table td {
  padding: 0 6px;
  vertical-align: top;
}
.version-diff__table td.no {
  color: var(--el-text-color-placeholder);
  text-align: right;
  user-select: none;
  border-right: 1px solid var(--el-border-color-extra-light);
}
.version-diff__table td.sign {
  color: var(--el-text-color-secondary);
  user-select: none;
}
.version-diff__table td.code {
  white-space: pre-wrap;
  word-break: break-word;
}
.version-diff__table td.is-del,
.version-diff__table tr.is-del td {
  background: var(--el-color-danger-light-9);
}
.version-diff__table td.is-ins,
.version-diff__table tr.is-ins td {
  background: var(--el-color-success-light-9);
}
.version-diff__table td.is-empty {
  background: var(--el-fill-color-lighter);
}
.hl-del {
  background: var(--el-color-danger-light-7);
  border-radius: 2px;
}
.hl-ins {
  background: var(--el-color-success-light-7);
  border-radius: 2px;
}
.version-diff__fold td {
  background: var(--el-fill-color-light);
  text-align: center;
}
</style>
