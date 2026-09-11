import { TaskApiError } from "./task-batches";
export interface FofaSettings {
  enabled: boolean;
  email: string;
  key_set: boolean;
  key_masked: string;
}
export interface FofaTest {
  success: boolean;
  code: string;
  duration_seconds: number;
  account: {
    is_vip: boolean;
    vip_level: number | null;
    remaining_queries: number | null;
    remaining_data: number | null;
  } | null;
}
export interface FofaSearch {
  search_id: string;
  query: string;
  status: "queued" | "running" | "completed" | "partial" | "failed";
  created_at: string;
  updated_at: string;
  max_results: number;
  loaded_count: number;
  total_available: number | null;
  pages_fetched: number;
  error_code: string | null;
  limited: boolean;
}
export interface FofaRow {
  result_id: string;
  position: number;
  host: string;
  ip: string;
  port: number | null;
  protocol: string;
  domain: string;
  title: string;
  country: string;
  region: string;
  city: string;
  server: string;
  lastupdatetime: string;
  web_target: string | null;
  selectable: boolean;
  selection_reason: "unsupported_protocol" | "invalid_target" | null;
}
export interface FofaResults {
  results: FofaRow[];
  total: number;
  loaded_count: number;
  limit: number;
  offset: number;
  has_more: boolean;
}
export interface FofaHistory {
  searches: FofaSearch[];
  total: number;
  limit: number;
  offset: number;
}
export interface FofaDraft {
  draft_id: string;
  created_at: string;
  search_id: string;
  target_count: number;
  targets: Array<{
    target: string;
    source: { kind: "fofa"; search_id: string; result_id: string };
  }>;
}
export type FofaFilter = {
  q: string;
  protocol: string;
  country: string;
  port: string;
  host: string;
  ip: string;
  title: string;
  domain: string;
  server: string;
  region: string;
  city: string;
  lastupdatetime: string;
  web_only: boolean;
};
export const EMPTY_FOFA_FILTER: FofaFilter = {
  q: "",
  protocol: "",
  country: "",
  port: "",
  host: "",
  ip: "",
  title: "",
  domain: "",
  server: "",
  region: "",
  city: "",
  lastupdatetime: "",
  web_only: false,
};
export function fofaParams(filters: FofaFilter): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters))
    if (value !== "") params.set(key, String(value));
  return params;
}
export function fofaStatus(status: string, en: boolean): string {
  return (
    {
      queued: ["排队中", "Queued"],
      running: ["查询中", "Searching"],
      completed: ["已完成", "Completed"],
      partial: ["部分结果", "Partial results"],
      failed: ["查询失败", "Search failed"],
    }[status]?.[en ? 1 : 0] ?? (en ? "Unknown" : "状态未知")
  );
}
export function fofaError(error: unknown, en: boolean): string {
  const code =
    error instanceof TaskApiError
      ? error.code
      : typeof error === "string"
        ? error
        : "provider_error";
  const messages: Record<string, [string, string]> = {
    interrupted: [
      "查询已中止；已取得的结果会保留，不会自动重新查询。",
      "The search was interrupted. Retrieved results are retained; the query is not automatically restarted.",
    ],
    disabled: [
      "FOFA 已停用，请在设置中启用。",
      "FOFA is disabled. Enable it in Settings.",
    ],
    not_configured: [
      "请先保存 FOFA API Key。",
      "Save your FOFA API key first.",
    ],
    unauthorized: [
      "FOFA 认证失败，请检查 Email 与 API Key。",
      "FOFA authentication failed. Check the email and API key.",
    ],
    quota_exceeded: [
      "FOFA 额度不足，请检查账号额度。",
      "FOFA quota is insufficient. Check your account allowance.",
    ],
    rate_limited: [
      "FOFA 请求频率受限，请稍后重试。",
      "FOFA rate-limited this request. Try again later.",
    ],
    invalid_request: [
      "输入格式无效，请检查查询或筛选条件。",
      "Check the query or filter format.",
    ],
    invalid_response: [
      "FOFA 返回了无法使用的结果，请重试。",
      "FOFA returned an unusable response. Try again.",
    ],
    timeout: [
      "FOFA 请求逾时，请重试。",
      "The FOFA request timed out. Try again.",
    ],
    response_too_large: [
      "返回结果过大，请缩小查询范围。",
      "The response is too large. Narrow the query.",
    ],
    storage_unavailable: [
      "无法读取或保存记录，请稍后重试。",
      "Records could not be read or saved. Try again later.",
    ],
    not_found: [
      "此查询或草稿已不存在。",
      "This search or draft no longer exists.",
    ],
    search_running: [
      "已有 FOFA 查询进行中，请等待完成或删除该查询。",
      "A FOFA search is already running. Wait for it to finish or delete it.",
    ],
    selection_limit: [
      "一次最多选择 100 个不同目标，请减少选取。",
      "Select at most 100 distinct targets.",
    ],
    unsupported_targets: [
      "选择中含有无法直接建立 Web 任务的资产，请先检查目标。",
      "Some selected assets cannot launch a Web task. Review the targets.",
    ],
    origin_rejected: [
      "此页面来源无法提交操作，请使用同一 Console 地址。",
      "Use the same Console origin to submit this operation.",
    ],
  };
  return (
    messages[code]?.[en ? 1 : 0] ??
    (en
      ? "FOFA could not complete this operation. Try again."
      : "FOFA 暂时无法完成此操作，请重试。")
  );
}
