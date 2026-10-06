// /admin/uploads（docs/04 §6.14；docs/10 §6.5）：multipart `file`，落 `media_assets(source=uploaded, usage_type=reference, status=ready)`，
// 返回 `{asset_id, url, public}`；`public=false` 表示真实模式下 zhiqiapi 无法读取该地址。
import type { UploadResult } from "@aicreat/shared";
import type { AxiosProgressEvent, AxiosRequestConfig } from "axios";
import { post } from "./client";

export interface UploadOptions extends AxiosRequestConfig {
  /** 上传进度 0~100 */
  onProgress?: (percent: number) => void;
}

function upload(url: string, file: File | Blob, options: UploadOptions, timeout: number): Promise<UploadResult> {
  const { onProgress, ...config } = options;
  const form = new FormData();
  form.append("file", file, file instanceof File ? file.name : "upload");
  return post<UploadResult>(url, form, {
    timeout,
    ...config,
    onUploadProgress: (event: AxiosProgressEvent) => {
      if (onProgress && event.total) onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      config.onUploadProgress?.(event);
    },
  });
}

/** 参考图：jpg/png/webp/gif，≤ `MAX_IMAGE_SIZE_MB`（默认 10） */
export function uploadImage(file: File | Blob, options: UploadOptions = {}): Promise<UploadResult> {
  return upload("/admin/uploads/image", file, options, 120_000);
}

/** 参考视频：mp4/mov，≤ `MAX_VIDEO_SIZE_MB`（默认 200） */
export function uploadVideo(file: File | Blob, options: UploadOptions = {}): Promise<UploadResult> {
  return upload("/admin/uploads/video", file, options, 600_000);
}
