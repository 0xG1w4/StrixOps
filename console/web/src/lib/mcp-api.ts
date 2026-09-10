"use client";

export type McpTaskStatus = "idle" | "capturing" | "stopped" | "ended" | "error" | "starting" | "stopping" | "ending" | "deleting" | "delete_failed";
export type McpFlowKind = "page" | "api" | "asset" | "other";
export type McpFlowSource = "user" | "replay" | "agent";
export type McpTestStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "blocked";

export interface McpAgentConfig {
  profile_id?: string | null;
  skills?: string[];
  instruction?: string;
  max_requests?: number;
  max_seconds?: number;
}

export interface McpSession {
  id: string;
  status: string;
  proxy_host?: string;
  proxy_port?: number;
  ca_ready?: boolean;
  ca_shared?: boolean;
  error?: string | null;
  ingest_error?: string | null;
  capture_status?: { state?: string; error?: string; message?: string };
}

export interface McpTask {
  id: string;
  name: string;
  status: McpTaskStatus;
  allow_hosts: string[];
  exclude_hosts: string[];
  scope_revision?: number;
  agent_config?: McpAgentConfig;
  created_at: string;
  updated_at: string;
  session?: McpSession | null;
  counts?: { flows?: number; pages?: number; apis?: number; tests?: number; findings?: number; reports?: number };
  error?: string | null;
}

export interface McpMessage {
  headers: Array<[string, string]>;
  url?: string;
  path?: string;
  body_text?: string | null;
  body_base64?: string | null;
  body_size?: number;
  truncated?: boolean;
  http_version?: string;
  binary?: boolean;
}

export interface McpFlow {
  id: string;
  session_id: string;
  method: string;
  url: string;
  host: string;
  path: string;
  status_code?: number | null;
  content_type?: string | null;
  duration_ms?: number | null;
  source: McpFlowSource;
  kind: McpFlowKind;
  error?: string | null;
  truncated?: boolean;
  created_at: string;
  stage?: string;
  request?: McpMessage;
  response?: McpMessage | null;
  parent_flow_id?: string | null;
  test_id?: string | null;
}

export interface McpEndpoint {
  id?: string;
  method: string;
  host: string;
  path: string;
  kind: McpFlowKind;
  count?: number;
  flow_count?: number;
  last_seen?: string;
  latest_flow_id?: string;
  inferred?: boolean;
}

export interface McpFinding {
  id?: string;
  title?: string;
  severity?: string;
  description?: string;
  observation?: string;
  evidence_flow_ids?: string[];
  evidence?: unknown;
  [key: string]: unknown;
}

export interface McpTest {
  id: string;
  status: McpTestStatus;
  flow_ids: string[];
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  config?: McpAgentConfig;
  result?: { summary?: string; findings?: McpFinding[]; coverage?: unknown[]; [key: string]: unknown } | null;
  error?: string | null;
  events?: Array<{ timestamp?: string; message?: string; type?: string; [key: string]: unknown }>;
}

export interface McpReport {
  id: string;
  created_at: string;
  markdown?: string;
  [key: string]: unknown;
}

export interface McpCatalog {
  skills: Array<{ id: string; description?: string; name?: string }>;
  profiles: Array<{ id: string; name: string }>;
  prompt_template?: string;
  defaults?: McpAgentConfig;
  [key: string]: unknown;
}

export interface McpFlowPage {
  flows: McpFlow[];
  total: number;
  next_cursor?: string | null;
}

export interface McpTaskInput {
  name: string;
  allow_hosts: string[];
  exclude_hosts: string[];
  agent_config?: McpAgentConfig;
}

export interface McpReplayPatch {
  method?: string;
  url?: string;
  headers?: Record<string, string>;
  body?: string;
}

export interface McpCaInfo {
  sha256: string;
  not_after?: string;
  source?: string;
  [key: string]: unknown;
}

export class McpApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); this.name = "McpApiError"; }
}

async function mcpRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  // MCP's access boundary requires same-origin browser requests in dev and production.
  const response = await fetch(`/api/mcp${path}`, { cache: "no-store", ...init });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const text = await response.text();
      if (text) {
        try {
          const payload: { detail?: unknown; error?: unknown } = JSON.parse(text);
          const detail = payload.detail ?? payload.error ?? payload;
          message = typeof detail === "string" ? detail : JSON.stringify(detail);
        } catch { message = text; }
      }
    } catch { /* Preserve the response status when its body is unavailable. */ }
    throw new McpApiError(message.slice(0, 1800), response.status);
  }
  return await response.json() as T;
}

const taskPath = (id: string) => `/tasks/${encodeURIComponent(id)}`;
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const mcpApi = {
  catalog: (signal?: AbortSignal) => mcpRequest<McpCatalog>("/catalog", { signal }),
  tasks: (signal?: AbortSignal) => mcpRequest<{ tasks: McpTask[] }>("/tasks", { signal }),
  createTask: (body: McpTaskInput) => mcpRequest<{ task: McpTask }>("/tasks", json("POST", body)),
  task: (id: string, signal?: AbortSignal) => mcpRequest<{ task: McpTask }>(taskPath(id), { signal }),
  updateTask: (id: string, body: McpTaskInput) => mcpRequest<{ task: McpTask }>(taskPath(id), json("PATCH", body)),
  deleteTask: (id: string) => mcpRequest<{ deleted: boolean; task_id: string }>(taskPath(id), { method: "DELETE" }),
  transition: (id: string, action: "start" | "stop" | "end") => mcpRequest<{ task: McpTask; session?: McpSession }>(`${taskPath(id)}/${action}`, json("POST", {})),
  flows: (id: string, filters: { q?: string; kind?: string; source?: string; after?: string; limit?: number }, signal?: AbortSignal) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value !== undefined && value !== "") query.set(key, String(value));
    return mcpRequest<McpFlowPage>(`${taskPath(id)}/flows?${query}`, { signal });
  },
  flow: (id: string, flowId: string, signal?: AbortSignal, reveal = false) => mcpRequest<{ flow: McpFlow }>(`${taskPath(id)}/flows/${encodeURIComponent(flowId)}?reveal=${reveal}`, { signal }),
  replay: (id: string, flowId: string, modifications: McpReplayPatch) => mcpRequest<{ flow: McpFlow }>(`${taskPath(id)}/flows/${encodeURIComponent(flowId)}/replay`, json("POST", { modifications })),
  endpoints: (id: string, signal?: AbortSignal) => mcpRequest<{ endpoints: McpEndpoint[] }>(`${taskPath(id)}/endpoints`, { signal }),
  tests: (id: string, signal?: AbortSignal) => mcpRequest<{ tests: McpTest[] }>(`${taskPath(id)}/tests`, { signal }),
  test: (id: string, testId: string, signal?: AbortSignal) => mcpRequest<{ test: McpTest & { prompt_snapshot?: unknown } }>(`${taskPath(id)}/tests/${encodeURIComponent(testId)}`, { signal }),
  createTest: (id: string, body: McpAgentConfig & { flow_ids: string[] }) => mcpRequest<{ test: McpTest }>(`${taskPath(id)}/tests`, json("POST", body)),
  cancelTest: (id: string, testId: string) => mcpRequest<{ test: McpTest }>(`${taskPath(id)}/tests/${encodeURIComponent(testId)}/cancel`, json("POST", {})),
  reports: (id: string, signal?: AbortSignal) => mcpRequest<{ reports: McpReport[] }>(`${taskPath(id)}/reports`, { signal }),
  createReport: (id: string) => mcpRequest<{ report: McpReport }>(`${taskPath(id)}/reports`, json("POST", {})),
  report: (id: string, reportId: string, signal?: AbortSignal) => mcpRequest<{ report: McpReport }>(`${taskPath(id)}/reports/${encodeURIComponent(reportId)}`, { signal }),
  caURL: (id: string) => `/api/mcp${taskPath(id)}/ca`,
  sharedCaURL: () => "/api/mcp/ca",
  sharedCaInfo: (signal?: AbortSignal) => mcpRequest<McpCaInfo>("/ca/info", { signal }),
};

export function mcpError(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function isMcpTaskLive(task: McpTask | null | undefined): boolean {
  return Boolean(task && ["capturing", "starting", "stopping", "ending", "deleting"].includes(task.status));
}

export function isMcpTestLive(test: McpTest): boolean {
  return test.status === "queued" || test.status === "running";
}
