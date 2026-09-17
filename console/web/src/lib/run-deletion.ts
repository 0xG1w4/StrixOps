export type RunDeletionResult = {
  deleted: string[];
  failed: { name: string; error: string }[];
};

/** Render the API's deletion reason without exposing its JSON envelope. */
export function describeDeletionError(message: string, locale: "zh-CN" | "en"): string {
  const match = message.match(/^(\d{3}):\s*(.*)$/s);
  if (!match) return message;
  if (match[1] === "404") {
    return locale === "en" ? "This task no longer exists. It may have been deleted elsewhere." : "该任务已不存在，可能已在其他页面删除。";
  }
  let detail = match[2];
  try {
    const payload = JSON.parse(detail);
    if (typeof payload.detail === "string") detail = payload.detail;
  } catch { /* Keep plain-text responses readable. */ }
  const translations: Record<string, string> = {
    "report generation is running — wait for it to finish": "报告正在生成，请等待完成后再删除。",
    "run is active or finalizing — stop or finish it first": "任务正在运行或收尾，请停止任务或等待完成后再删除。",
    "run still has queued work or pending cleanup": "任务仍有排队工作或资源清理尚未完成，请处理后重试。",
    "queue activity could not be verified": "无法确认排队任务的状态，请检查队列后重试。",
    "run cleanup has not been verified": "尚未确认任务资源已清理，请检查任务状态后重试。",
    "invalid run directory": "任务目录无效，无法删除。",
    "invalid run name": "任务名称无效，无法删除。",
  };
  return (locale === "zh-CN" ? translations[detail] : undefined) ?? (detail || message);
}

/** Delete the confirmed selection in order, retaining individual failures. */
export async function deleteRuns(
  names: readonly string[],
  remove: (name: string) => Promise<unknown>,
  onProgress?: (done: number, total: number) => void,
): Promise<RunDeletionResult> {
  // Snapshot before the first await: selection changes must not expand a deletion.
  const selected = [...new Set(names)];
  const result: RunDeletionResult = { deleted: [], failed: [] };
  onProgress?.(0, selected.length);
  for (const name of selected) {
    try {
      await remove(name);
      result.deleted.push(name);
    } catch (error) {
      result.failed.push({
        name,
        error: error instanceof Error ? error.message : String(error),
      });
    }
    onProgress?.(result.deleted.length + result.failed.length, selected.length);
  }
  return result;
}
