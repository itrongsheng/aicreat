// /admin/media（docs/04 §6.13、§7.8、§7.9、§8；docs/10 §7、§8）。
// 路径与参数以 04 为准；生成接口建议以 `{ silent: true }` 发出，由页面统一映射 400 / 4222 / 4291 / 429 / 5031 提示。
import type {
  AiTaskSummary,
  ImageGenerateBody,
  ImageGenerateResult,
  MediaAsset,
  MediaKind,
  MediaRetryResult,
  MediaSource,
  MediaStatus,
  MediaTransferResult,
  MediaUsageType,
  Page,
  VideoGenerateBody,
  VideoGenerateResult,
} from "@aicreat/shared";
import type { AxiosRequestConfig } from "axios";
import { del, get, post } from "./client";

export interface ListAssetsParams {
  page?: number;
  page_size?: number;
  /** 不传 = 全部可见项目（含 `project_id` 为空的独立 / 上传素材） */
  project_id?: number;
  content_id?: number;
  kind?: MediaKind;
  status?: MediaStatus;
  usage_type?: MediaUsageType;
  source?: MediaSource;
  created_by?: number;
}

function clean(params: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    out[k] = v;
  }
  return out;
}

/** 素材列表（按 `created_at DESC`；列表不含 `references`） */
export function listAssets(params: ListAssetsParams = {}, config: AxiosRequestConfig = {}): Promise<Page<MediaAsset>> {
  return get<Page<MediaAsset>>("/admin/media/assets", clean(params), config);
}

/** 素材详情：含根任务摘要 `task`、`params`、`reference_asset_ids` 与实时计算的 `references` */
export function getAsset(id: number, config: AxiosRequestConfig = {}): Promise<MediaAsset> {
  return get<MediaAsset>(`/admin/media/assets/${id}`, undefined, config);
}

/** 当前根任务摘要（前端轮询；备选回退 / 重试后自动指向新根任务；上传素材无根任务为 null） */
export function getAssetTask(id: number, config: AxiosRequestConfig = {}): Promise<AiTaskSummary | null> {
  return get<AiTaskSummary | null>(`/admin/media/assets/${id}/task`, undefined, config);
}

/** 文生图 / 图生图：每张一条资产 + 一个根任务；返回 `{asset_ids[], task_ids[], quota_warning?}` */
export function generateImages(body: ImageGenerateBody, config: AxiosRequestConfig = {}): Promise<ImageGenerateResult> {
  return post<ImageGenerateResult>("/admin/media/images/generate", body, config);
}

/** docs/02 §3.4 的命名（与 docs/10 §7 的 `generateImages` 等价） */
export const generateImage = generateImages;

/** 文生视频 / 图生视频 / 首尾帧 / 参考素材：返回 `{asset_id, task_id, quota_warning?}` */
export function generateVideo(body: VideoGenerateBody, config: AxiosRequestConfig = {}): Promise<VideoGenerateResult> {
  return post<VideoGenerateResult>("/admin/media/videos/generate", body, config);
}

/**
 * `failed`/`expired` 重试：满足条件时先同步复查旧上游任务（`resumed=true` 表示未重新提交、不重复计费）；
 * 其它状态 409 `current_status`；复查遇非 404 上游错误 5021（资产状态不变）；重新提交分支可能 4291。
 */
export function retryAsset(id: number, config: AxiosRequestConfig = {}): Promise<MediaRetryResult> {
  return post<MediaRetryResult>(`/admin/media/assets/${id}/retry`, undefined, config);
}

/** `failed(transfer_failed/timeout)` 且有上游 URL → `downloading`，由 worker 重新转存；其它 409 `current_status` */
export function transferAsset(id: number, config: AxiosRequestConfig = {}): Promise<MediaTransferResult> {
  return post<MediaTransferResult>(`/admin/media/assets/${id}/transfer`, undefined, config);
}

/** 删除（仅 `ready`/`failed`/`expired`）：其它状态 409 `current_status`；被待提交任务引用的参考素材 409 `reason=in_use` */
export function deleteAsset(id: number, config: AxiosRequestConfig = {}): Promise<null> {
  return del<null>(`/admin/media/assets/${id}`, undefined, config);
}
