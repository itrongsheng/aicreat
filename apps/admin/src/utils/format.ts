// 时间（UTC ISO → 本地）、额度、金额、时长格式化
const EMPTY = "-";

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(value)) {
    // 无时区后缀的时间按 UTC 解析（API 约定 UTC）
    value = `${value}Z`;
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** UTC ISO 8601 → 本地 `YYYY-MM-DD HH:mm:ss` */
export function formatDateTime(value: string | number | Date | null | undefined, withSeconds = true): string {
  const d = toDate(value);
  if (!d) return EMPTY;
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}${withSeconds ? `:${pad(d.getSeconds())}` : ""}`;
  return `${date} ${time}`;
}

/** UTC ISO 8601 → 本地 `YYYY-MM-DD`；`YYYY-MM-DD` 日期串原样返回 */
export function formatDate(value: string | number | Date | null | undefined): string {
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const d = toDate(value);
  if (!d) return EMPTY;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 本地时间 → ISO 8601 UTC（秒级，`Z` 结尾），供 start / end 筛选参数 */
export function toUtcIso(value: string | number | Date | null | undefined): string | undefined {
  const d = toDate(value);
  if (!d) return undefined;
  return d.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** 整数千分位 */
export function formatNumber(value: number | string | null | undefined, digits = 0): string {
  if (value === null || value === undefined || value === "") return EMPTY;
  const n = Number(value);
  if (!Number.isFinite(n)) return EMPTY;
  return n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** zhiqiapi 额度：整数千分位 */
export function formatQuota(value: number | string | null | undefined): string {
  return formatNumber(value === null || value === undefined || value === "" ? value : Math.round(Number(value)));
}

/** 金额（人民币）：两位小数 */
export function formatCny(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY;
  const n = Number(value);
  if (!Number.isFinite(n)) return EMPTY;
  return `¥${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** 比率（0~1）→ 百分比 */
export function formatPercent(value: number | string | null | undefined, digits = 1): string {
  if (value === null || value === undefined || value === "") return EMPTY;
  const n = Number(value);
  if (!Number.isFinite(n)) return EMPTY;
  return `${(n * 100).toFixed(digits)}%`;
}

/** 时长（毫秒）→ `850ms` / `12.3s` / `3m 05s` / `2h 03m` */
export function formatDuration(ms: number | string | null | undefined): string {
  if (ms === null || ms === undefined || ms === "") return EMPTY;
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return EMPTY;
  if (n < 1000) return `${Math.round(n)}ms`;
  const seconds = n / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const totalSec = Math.round(seconds);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  return h > 0 ? `${h}h ${pad(m)}m` : `${m}m ${pad(s)}s`;
}

/** 文件大小（字节） */
export function formatBytes(bytes: number | string | null | undefined): string {
  if (bytes === null || bytes === undefined || bytes === "") return EMPTY;
  let n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return EMPTY;
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i === 0 ? n : n.toFixed(1)} ${units[i]}`;
}
