export type PerplexityModel = "sonar" | "sonar-reasoning-pro";

export interface WebSearchIntegrationSettings {
  revision: number;
  perplexity_api_key_set: boolean;
  perplexity_api_key_masked: string;
  perplexity_enabled: boolean;
  perplexity_model: PerplexityModel;
  perplexity_timeout_seconds: number;
  perplexity_key_source: "saved" | "environment" | "none";
  perplexity_error_code?: string | null;
}

export interface WebSearchUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  cost_usd: number | null;
}

export interface WebSearchConnectionTest {
  success: boolean;
  status: "success" | "error" | "skipped";
  code: string;
  model: PerplexityModel;
  duration_seconds: number | null;
  usage: WebSearchUsage;
}

export interface WebSearchStats {
  available: boolean;
  code: "ok" | "not_recorded" | "invalid_stats";
  calls: number | null;
  requests: number | null;
  successes: number | null;
  failures: number | null;
  skipped: number | null;
  duration_seconds: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  reported_cost_usd: number | null;
  partial_usage: boolean;
  circuit_code: string | null;
  recent: Array<{
    timestamp: string | null;
    agent_id: string | null;
    model: string | null;
    status: "success" | "error" | "skipped" | "cancelled" | null;
    code: string | null;
    duration_seconds: number | null;
    attempts: number | null;
    input_tokens: number | null;
    output_tokens: number | null;
    total_tokens: number | null;
    cost_usd: number | null;
  }>;
}

export function webSearchStatus(code: string | null | undefined, en: boolean): string {
  const names: Record<string, [string, string]> = {
    ok: ["成功", "Success"], disabled: ["未启用", "Disabled"], not_configured: ["未设置 API Key", "API key not set"],
    invalid_configuration: ["设置需要修正", "Check the settings"], invalid_query: ["查询内容无效", "Invalid query"],
    unauthorized: ["认证失败", "Authentication failed"], quota_exceeded: ["额度不足", "Insufficient quota"],
    forbidden: ["访问被拒绝", "Access denied"], rate_limited: ["请求频率受限", "Rate limited"],
    cooldown: ["暂停重试中", "Retry cooldown"], timeout: ["请求超时", "Request timed out"],
    network_error: ["网络连接失败", "Connection failed"], http_error: ["搜索服务异常", "Search service error"],
    upstream_error: ["搜索服务异常", "Search service error"], service_error: ["搜索服务异常", "Search service error"],
    invalid_response: ["搜索服务响应无效", "Invalid service response"], empty_response: ["搜索服务未返回结果", "No search results returned"],
    incomplete_response: ["搜索响应不完整", "Search response incomplete"], cancelled: ["已取消", "Cancelled"],
  };
  return names[code || ""]?.[en ? 1 : 0] || (en ? "Search service unavailable" : "搜索服务暂时无法使用");
}

export const knownNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
export const searchCount = (value?: number | null) => knownNumber(value) && Number.isInteger(value) ? value.toLocaleString() : "—";
export const searchDuration = (value?: number | null) => knownNumber(value) ? `${Math.round(value * 10) / 10} s` : "—";
export const searchCost = (value?: number | null) => knownNumber(value) ? `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 6 })}` : "—";
