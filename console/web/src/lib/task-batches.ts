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
  let status: number | null = null;
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
    status = response.status;
    let data;
    if (status !== 204) {
      try {
        data = await response.json();
      } catch {
        if (response.ok) {
          throw new TaskApiError(
            controller.signal.aborted ? "timeout" : "invalid_response",
            status,
          );
        }
        // Proxies may return HTML or an empty body. Keep the HTTP status,
        // without exposing the response body or an exception's raw detail.
      }
    }
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
      controller.signal.aborted
        ? "timeout"
        : status === null
          ? "connection_failed"
          : "invalid_response",
      status,
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
    connection_failed: [
      "无法连接 Console。请检查主机服务、网络与反向代理连接。",
      "Could not connect to Console. Check the host service, network and reverse proxy connection.",
    ],
    invalid_response: [
      "Console 返回了无法读取的响应。请检查后端服务与 /api 代理路由。",
      "Console returned an unreadable response. Check the backend service and /api proxy routing.",
    ],
    origin_rejected: [
      "Console 拒绝了当前页面来源。请使用同一 Console 地址，并检查反向代理的来源转发设置。",
      "Console rejected this page's origin. Use the same Console address and check origin forwarding in the reverse proxy.",
    ],
    invalid_request: [
      "提交参数无效，请检查目标格式与并行数量后重试。",
      "The submitted parameters are invalid. Check the targets and concurrency values, then try again.",
    ],
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
  const message = messages[code]?.[en ? 1 : 0];
  if (message) return message;
  const status = error instanceof TaskApiError ? error.status : null;
  const httpMessages: Record<number, [string, string]> = {
    400: messages.invalid_request,
    401: [
      "Console 需要身份验证（HTTP 401），请检查访问凭证或重新登录。",
      "Console requires authentication (HTTP 401). Check your access credentials or sign in again.",
    ],
    403: [
      "Console 拒绝了此操作（HTTP 403），请检查访问权限与页面来源设置。",
      "Console denied this operation (HTTP 403). Check access permissions and page origin settings.",
    ],
    404: [
      "此功能的 API 无法使用（HTTP 404）。请确认前后端版本一致、重启 Console，并检查 /api 代理转发。",
      "This feature's API is unavailable (HTTP 404). Confirm matching frontend and backend versions, restart Console, and check /api proxy forwarding.",
    ],
    405: [
      "此功能的 API 不接受当前请求方式（HTTP 405）。请确认前后端版本一致、重启 Console，并检查 /api 代理转发。",
      "This feature's API rejects the request method (HTTP 405). Confirm matching frontend and backend versions, restart Console, and check /api proxy forwarding.",
    ],
    422: messages.invalid_request,
    429: [
      "Console 请求频率受限（HTTP 429），请稍后重试。",
      "Console rate-limited this request (HTTP 429). Try again later.",
    ],
  };
  if (status && httpMessages[status]) return httpMessages[status][en ? 1 : 0];
  if (status && status >= 500) {
    return en
      ? `Console encountered a server error (HTTP ${status}). Check the Console service logs and try again.`
      : `Console 服务发生错误（HTTP ${status}），请检查 Console 服务日志后重试。`;
  }
  return en
    ? "The operation could not be completed. Check the Console service status and try again."
    : "无法完成操作，请检查 Console 服务状态后重试。";
}
