// 标题去重键（docs/09 §7.3 第 2 步、§7.4）：与服务端 `title_service.title_dedup_key` 结果一致——
// Unicode NFKC → 去掉标点（Unicode 类别 P*）与空白 → 小写；不剥离站点后缀
// （「智能门锁选购指南 - 新手必看」与「智能门锁选购指南 - 避坑大全」不算重复）。
const PUNCT_OR_SPACE = /[\p{P}\s]/gu;

export function titleDedupKey(title: string | null | undefined): string {
  return (title ?? "").normalize("NFKC").replace(PUNCT_OR_SPACE, "").toLowerCase();
}

/** 在候选标题中找与 `title` 去重键相同的项（排除 `excludeId`） */
export function findDuplicateTitle<T extends { id: number; title: string }>(title: string, candidates: readonly T[], excludeId?: number): T | null {
  const key = titleDedupKey(title);
  if (!key) return null;
  return candidates.find((c) => c.id !== excludeId && titleDedupKey(c.title) === key) ?? null;
}
