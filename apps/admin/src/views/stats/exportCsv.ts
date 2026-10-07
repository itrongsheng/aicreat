// 报表 CSV 导出（docs/12 §8）：浏览器下载流，不在前端拼 CSV；文件名取 Content-Disposition（stats-{report}-{YYYYMMDD}.csv）。
import { ElMessage } from "element-plus";
import { t } from "@/i18n";
import * as statsApi from "@/api/stats";
import { datedFilename, downloadBlob } from "@/utils/download";

export async function exportCsv(params: statsApi.ExportParams): Promise<boolean> {
  try {
    const { blob, filename } = await statsApi.exportStats(params);
    downloadBlob(blob, filename || datedFilename(`stats-${params.report}`, "csv"));
    ElMessage.success(t("stats.export.done"));
    return true;
  } catch {
    // 400（行数超限等）与 403 已由拦截器提示
    return false;
  }
}
