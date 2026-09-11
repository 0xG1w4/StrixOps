import { apiURL } from "./api";

export class TaskApiError extends Error {
  constructor(
    public code: string,
    public status: number | null = null,
  ) {
    super(code);
  }
}

export async function taskRequest<T>(
  path: string,
  options: {
    method?: string;
    body?: unknown;
    signal?: AbortSignal;
    timeout?: number;
  } = {},
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = window.setTimeout(abort, options.timeout ?? 30000);
  try {
    const response = await fetch(apiURL(path), {
      method: options.method ?? "GET",
      cache: "no-store",
      signal: controller.signal,
      ...(options.body === undefined
        ? {}
        : {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(options.body),
          }),
    });
    const data = response.status === 204 ? undefined : await response.json();
    if (!response.ok) {
      const code =
        data?.error_code ??
        data?.code ??
        data?.detail?.error_code ??
        data?.detail?.code;
      throw new TaskApiError(
        typeof code === "string" && /^[a-z_]{1,80}$/.test(code)
          ? code
          : "request_failed",
        response.status,
      );
    }
    return data as T;
  } catch (error) {
    if (error instanceof TaskApiError) throw error;
    throw new TaskApiError(
      controller.signal.aborted ? "timeout" : "request_failed",
    );
  } finally {
    window.clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
  }
}

export type BatchItemStatus =
  | "queued"
  | "starting"
  | "waiting_capacity"
  | "running"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled"
  | "blocked";
export interface BatchItem {
  id: string;
  target: string;
  scan_type: string;
  status: BatchItemStatus;
  run_name: string | null;
  error_code: string | null;
  error: string | null;
  report_ready: boolean;
}
export interface ScanBatch {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  status: string;
  max_concurrent: number;
  target_count: number;
  counts: {
    queued: number;
    active: number;
    completed: number;
    failed: number;
    cancelled: number;
    blocked: number;
  };
  items?: BatchItem[];
  source?: Record<string, unknown>;
}
export interface QueueSettings {
  max_active_targets: number;
  active_targets: number;
  waiting_targets: number;
  blocked_targets: number;
}
export interface BatchCreated {
  ok: boolean;
  kind: "batch";
  batch_id: string;
  target_count: number;
}

export const batchActive = (status: string) =>
  ["queued", "running"].includes(status);
export const itemActive = (status: string) =>
  ["queued", "starting", "waiting_capacity", "running", "cancelling"].includes(
    status,
  );
export const queueStatus = (status: string, en: boolean) =>
  ({
    queued: ["排队中", "Queued"],
    starting: ["正在启动", "Starting"],
    waiting_capacity: ["等待主机空位", "Waiting for capacity"],
    running: ["执行中", "Running"],
    cancelling: ["正在取消", "Cancelling"],
    completed: ["已完成", "Completed"],
    failed: ["失败", "Failed"],
    cancelled: ["已取消", "Cancelled"],
    blocked: ["受阻", "Blocked"],
    partial: ["部分完成", "Partially completed"],
  })[status]?.[en ? 1 : 0] ?? (en ? "Unknown" : "状态未知");
export const taskDate = (value: string | number | null | undefined) => {
  const timestamp =
    typeof value === "number"
      ? value * 1000
      : typeof value === "string"
        ? Date.parse(value)
        : NaN;
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString()
    : "—";
};
export function taskError(error: unknown, en: boolean): string {
  const code =
    error instanceof TaskApiError
      ? error.code
      : typeof error === "string"
        ? error
        : "request_failed";
  const messages: Record<string, [string, string]> = {
    timeout: ["请求逾时，请重试。", "The request timed out. Try again."],
    queue_not_found: [
      "此批次或目标已不存在。",
      "This batch or target no longer exists.",
    ],
    queue_invalid: [
      "队列设置无效，请检查后重试。",
      "Check the queue settings and try again.",
    ],
    queue_busy: [
      "批次仍有执行、排队或受阻目标，暂时无法删除。",
      "The batch has active, queued or blocked targets and cannot be deleted.",
    ],
    queue_cancelled: [
      "此目标在开始执行前已取消。",
      "This target was cancelled before it started.",
    ],
    queue_launch_failed: [
      "无法启动此目标任务。",
      "This target could not be started.",
    ],
    queue_launch_uncertain: [
      "无法确认启动状态，请先确认资源清理。",
      "Startup could not be confirmed. Verify resource cleanup.",
    ],
    queue_cleanup_unconfirmed: [
      "无法确认资源已清理。",
      "Resource cleanup could not be confirmed.",
    ],
    queue_process_unconfirmed: [
      "无法确认原任务进程。",
      "The original task process could not be verified.",
    ],
    queue_scan_failed: [
      "此目标的测试未成功完成。",
      "This target assessment did not complete successfully.",
    ],
    queue_scope_rejected: [
      "目标已不在项目授权范围内。",
      "This target is no longer in the project's authorized scope.",
    ],
    queue_snapshot_invalid: [
      "保存的启动配置无法读取或已失效。",
      "The saved launch configuration is unavailable or invalid.",
    ],
    queue_stop_failed: [
      "无法确认目标任务已停止。",
      "Stopping this target could not be confirmed.",
    ],
    invalid_snapshot: [
      "保存的启动配置已失效。",
      "The saved launch configuration is invalid.",
    ],
    snapshot_unavailable: [
      "保存的启动配置无法读取。",
      "The saved launch configuration is unavailable.",
    ],
    different_runs_root: [
      "此批次属于另一个任务目录。",
      "This batch belongs to another task directory.",
    ],
    batch_not_found: ["此批次已不存在。", "This batch no longer exists."],
    scope_changed: [
      "项目范围已变更，请重新检查目标。",
      "Project scope changed. Check the targets again.",
    ],
    scope_violation: [
      "部分目标不在项目范围内。",
      "Some targets are outside the project scope.",
    ],
    queue_full: [
      "队列已满，请稍后重试。",
      "The queue is full. Try again later.",
    ],
    active_batch: [
      "批次尚未结束，无法删除。",
      "This batch is still active and cannot be deleted.",
    ],
  };
  return (
    messages[code]?.[en ? 1 : 0] ??
    (en
      ? "The operation could not be completed. Refresh and try again."
      : "无法完成操作，请更新后重试。")
  );
}
