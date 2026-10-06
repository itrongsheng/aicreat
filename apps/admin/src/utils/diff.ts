// 文本差异（VersionDiff.vue）：Myers O(ND) 差异算法，先裁掉公共前后缀；行级对比 + 修改行内的词级高亮。

export type DiffKind = "equal" | "insert" | "delete";

export interface DiffOp<T> {
  kind: DiffKind;
  items: T[];
}

/** 超过该编辑距离时放弃精细对比，按「整段删除 + 整段插入」处理（避免超长文本卡住页面） */
const MAX_EDIT_DISTANCE = 2000;

function push<T>(ops: DiffOp<T>[], kind: DiffKind, item: T): void {
  const last = ops[ops.length - 1];
  if (last && last.kind === kind) last.items.push(item);
  else ops.push({ kind, items: [item] });
}

/** Myers 差异：返回把 a 变成 b 的操作序列（相邻同类操作已合并） */
export function diffSequence<T>(a: readonly T[], b: readonly T[], equals: (x: T, y: T) => boolean = Object.is): DiffOp<T>[] {
  let start = 0;
  while (start < a.length && start < b.length && equals(a[start], b[start])) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && equals(a[endA - 1], b[endB - 1])) {
    endA -= 1;
    endB -= 1;
  }
  const ops: DiffOp<T>[] = [];
  for (let i = 0; i < start; i += 1) push(ops, "equal", a[i]);
  middle(a.slice(start, endA), b.slice(start, endB), equals).forEach((op) => op.items.forEach((item) => push(ops, op.kind, item)));
  for (let i = endA; i < a.length; i += 1) push(ops, "equal", a[i]);
  return ops;
}

function middle<T>(a: T[], b: T[], equals: (x: T, y: T) => boolean): DiffOp<T>[] {
  const n = a.length;
  const m = b.length;
  if (n === 0 && m === 0) return [];
  if (n === 0) return [{ kind: "insert", items: [...b] }];
  if (m === 0) return [{ kind: "delete", items: [...a] }];

  const max = Math.min(n + m, MAX_EDIT_DISTANCE);
  const offset = max + 1;
  const v = new Int32Array(2 * max + 3);
  // 每轮只保存 k ∈ [-d-1, d+1] 的快照（下标 k + d + 1），回溯时读取
  const trace: Int32Array[] = [];
  let found = false;
  outer: for (let d = 0; d <= max; d += 1) {
    trace.push(v.slice(offset - d - 1, offset + d + 2));
    for (let k = -d; k <= d; k += 2) {
      let x: number;
      if (k === -d || (k !== d && v[offset + k - 1] < v[offset + k + 1])) x = v[offset + k + 1];
      else x = v[offset + k - 1] + 1;
      let y = x - k;
      while (x < n && y < m && equals(a[x], b[y])) {
        x += 1;
        y += 1;
      }
      v[offset + k] = x;
      if (x >= n && y >= m) {
        found = true;
        break outer;
      }
    }
  }
  if (!found) return [{ kind: "delete", items: [...a] }, { kind: "insert", items: [...b] }];

  // 回溯
  const reversed: { kind: DiffKind; item: T }[] = [];
  let x = n;
  let y = m;
  for (let d = trace.length - 1; d >= 0 && (x > 0 || y > 0); d -= 1) {
    const vd = trace[d];
    const base = d + 1;
    const k = x - y;
    let prevK: number;
    if (k === -d || (k !== d && vd[base + k - 1] < vd[base + k + 1])) prevK = k + 1;
    else prevK = k - 1;
    const prevX = d === 0 ? 0 : vd[base + prevK];
    const prevY = prevX - prevK;
    while (x > prevX && y > prevY) {
      reversed.push({ kind: "equal", item: a[x - 1] });
      x -= 1;
      y -= 1;
    }
    if (d === 0) break;
    if (x === prevX) {
      reversed.push({ kind: "insert", item: b[y - 1] });
      y -= 1;
    } else {
      reversed.push({ kind: "delete", item: a[x - 1] });
      x -= 1;
    }
  }
  while (x > 0 && y > 0) {
    reversed.push({ kind: "equal", item: a[x - 1] });
    x -= 1;
    y -= 1;
  }
  const ops: DiffOp<T>[] = [];
  for (let i = reversed.length - 1; i >= 0; i -= 1) push(ops, reversed[i].kind, reversed[i].item);
  return ops;
}

/** 词级切分：连续的拉丁字母 / 数字为一个词，CJK 等其它字符逐字，空白按段 */
export function tokenize(text: string): string[] {
  return text.match(/[A-Za-z0-9_]+|\s+|[\s\S]/gu) ?? [];
}

export interface InlineSegment {
  text: string;
  changed: boolean;
}

/** 一对修改行的词级差异：返回旧行与新行各自的分段（changed 为被删 / 新增部分） */
export function diffInline(oldLine: string, newLine: string): { old: InlineSegment[]; new: InlineSegment[] } {
  const ops = diffSequence(tokenize(oldLine), tokenize(newLine));
  const oldSegs: InlineSegment[] = [];
  const newSegs: InlineSegment[] = [];
  const add = (segs: InlineSegment[], text: string, changed: boolean) => {
    const last = segs[segs.length - 1];
    if (last && last.changed === changed) last.text += text;
    else segs.push({ text, changed });
  };
  for (const op of ops) {
    const text = op.items.join("");
    if (op.kind === "equal") {
      add(oldSegs, text, false);
      add(newSegs, text, false);
    } else if (op.kind === "delete") add(oldSegs, text, true);
    else add(newSegs, text, true);
  }
  return { old: oldSegs, new: newSegs };
}

export interface DiffRow {
  kind: DiffKind | "replace";
  oldNo: number | null;
  newNo: number | null;
  oldText: string | null;
  newText: string | null;
  /** replace 行的词级分段 */
  oldSegments?: InlineSegment[];
  newSegments?: InlineSegment[];
}

/** 行级对比，相邻的「删除 + 插入」块逐行配对为 replace 行（并排视图） */
export function diffLines(oldText: string, newText: string): DiffRow[] {
  const a = (oldText ?? "").replace(/\r\n?/g, "\n").split("\n");
  const b = (newText ?? "").replace(/\r\n?/g, "\n").split("\n");
  const ops = diffSequence(a, b);
  const rows: DiffRow[] = [];
  let oldNo = 1;
  let newNo = 1;
  for (let i = 0; i < ops.length; i += 1) {
    const op = ops[i];
    if (op.kind === "equal") {
      for (const line of op.items) rows.push({ kind: "equal", oldNo: oldNo++, newNo: newNo++, oldText: line, newText: line });
      continue;
    }
    if (op.kind === "delete" && ops[i + 1]?.kind === "insert") {
      const dels = op.items;
      const ins = ops[i + 1].items;
      const pairs = Math.min(dels.length, ins.length);
      for (let j = 0; j < pairs; j += 1) {
        const inline = diffInline(dels[j], ins[j]);
        rows.push({ kind: "replace", oldNo: oldNo++, newNo: newNo++, oldText: dels[j], newText: ins[j], oldSegments: inline.old, newSegments: inline.new });
      }
      for (let j = pairs; j < dels.length; j += 1) rows.push({ kind: "delete", oldNo: oldNo++, newNo: null, oldText: dels[j], newText: null });
      for (let j = pairs; j < ins.length; j += 1) rows.push({ kind: "insert", oldNo: null, newNo: newNo++, oldText: null, newText: ins[j] });
      i += 1;
      continue;
    }
    if (op.kind === "delete") {
      for (const line of op.items) rows.push({ kind: "delete", oldNo: oldNo++, newNo: null, oldText: line, newText: null });
    } else {
      for (const line of op.items) rows.push({ kind: "insert", oldNo: null, newNo: newNo++, oldText: null, newText: line });
    }
  }
  return rows;
}

/** 统计：新增 / 删除行数（replace 计为各 1） */
export function diffStats(rows: DiffRow[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const r of rows) {
    if (r.kind === "insert") added += 1;
    else if (r.kind === "delete") removed += 1;
    else if (r.kind === "replace") {
      added += 1;
      removed += 1;
    }
  }
  return { added, removed };
}
