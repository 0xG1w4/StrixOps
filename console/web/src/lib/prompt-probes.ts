"use client";

import { apiURL } from "@/lib/api";

export type PromptProbeKind = "prompt" | "skill";
export type PromptProbeStatus = "responded" | "refused" | "blocked" | "error" | "inconclusive";

export interface PromptProbeItem {
  kind: PromptProbeKind;
  name: string;
  sha256: string;
  size: number;
}

export interface PromptProbeRoute {
  profile_id: string;
  profile_name: string;
  scan_type: "web" | "internal";
  model: string;
  api_mode: string;
  reasoning_effort: string;
  route_fingerprint: string;
}

export interface PromptProbeCatalog {
  test_kind: "task_response";
  items: PromptProbeItem[];
  routes: PromptProbeRoute[];
  active_profile_id: string | null;
  max_task_chars: number;
  probe_version: string;
}

export interface PromptProbeRequest {
  kind: PromptProbeKind;
  name: string;
  sha256: string;
  profile_id: string;
  scan_type: "web" | "internal";
  route_fingerprint: string;
  test_task: string;
}

export interface PromptProbeResult extends PromptProbeRequest {
  test_kind: "task_response";
  task_sha256: string;
  model: string;
  api_mode: string;
  reasoning_effort: string;
  status: PromptProbeStatus;
  code: string;
  message: string;
  response_excerpt: string;
  response_text: string;
  response_truncated: boolean;
  diagnostics: string;
  checked_at: string;
  duration_ms: number;
  probe_version: string;
}

export class PromptProbeError extends Error {
  constructor(public code: string, message: string, public status: number = 0) {
    super(message);
    this.name = "PromptProbeError";
  }
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const nonempty = (value: unknown): value is string => typeof value === "string" && value.length > 0;
const kind = (value: unknown): value is PromptProbeKind => value === "prompt" || value === "skill";
const scanType = (value: unknown) => value === "web" || value === "internal";
const hash = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/i.test(value);
const nonnegative = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0;
const invalidResponse = () => new PromptProbeError("console_invalid_response", "The console returned an invalid prompt-test response.");

async function request(path: string, signal: AbortSignal, body?: PromptProbeRequest): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(apiURL(path), {
      method: body ? "POST" : "GET",
      cache: "no-store",
      signal,
      ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new PromptProbeError("console_connection", "Cannot connect to the console backend.");
  }

  let data: unknown;
  try {
    data = await response.json();
  } catch (error) {
    if (signal.aborted) throw error;
    if (!response.ok) {
      throw new PromptProbeError(response.status === 404 ? "endpoint_missing" : "console_http_error", `Console request failed (HTTP ${response.status}).`, response.status);
    }
    throw invalidResponse();
  }
  if (!response.ok) {
    const detail = record(data) && record(data.detail) ? data.detail : null;
    const code = detail && nonempty(detail.code) ? detail.code.slice(0, 100) : "console_http_error";
    const message = detail && nonempty(detail.message) ? detail.message.slice(0, 1200) : `Console request failed (HTTP ${response.status}).`;
    throw new PromptProbeError(code, message, response.status);
  }
  return data;
}

export async function fetchPromptProbeCatalog(signal: AbortSignal): Promise<PromptProbeCatalog> {
  const value = await request("/api/prompt-probes/catalog", signal);
  if (!record(value) || !Array.isArray(value.items) || !Array.isArray(value.routes)
    || value.test_kind !== "task_response" || value.probe_version !== "3"
    || typeof value.max_task_chars !== "number" || !Number.isSafeInteger(value.max_task_chars) || value.max_task_chars <= 0
    || !(value.active_profile_id === null || typeof value.active_profile_id === "string")) throw invalidResponse();
  const itemKeys = new Set<string>();
  for (const item of value.items) {
    if (!record(item) || !kind(item.kind) || !nonempty(item.name) || !hash(item.sha256) || !nonnegative(item.size)) throw invalidResponse();
    const key = `${item.kind}:${item.name}`;
    if (itemKeys.has(key)) throw invalidResponse();
    itemKeys.add(key);
  }
  const routeKeys = new Set<string>();
  for (const route of value.routes) {
    if (!record(route) || !nonempty(route.profile_id) || !nonempty(route.profile_name)
      || !scanType(route.scan_type) || !nonempty(route.model) || !nonempty(route.api_mode)
      || !nonempty(route.reasoning_effort) || !nonempty(route.route_fingerprint)) throw invalidResponse();
    const key = `${route.profile_id}:${route.scan_type}`;
    if (routeKeys.has(key)) throw invalidResponse();
    routeKeys.add(key);
  }
  return value as unknown as PromptProbeCatalog;
}

export async function runPromptProbe(body: PromptProbeRequest, signal: AbortSignal): Promise<PromptProbeResult> {
  const value = await request("/api/prompt-probes/run", signal, body);
  if (!record(value) || !["responded", "refused", "blocked", "error", "inconclusive"].includes(String(value.status))
    || value.test_kind !== "task_response" || value.probe_version !== "3" || !hash(value.task_sha256)
    || typeof value.response_truncated !== "boolean"
    || !["code", "message", "response_excerpt", "response_text", "diagnostics", "checked_at", "model", "api_mode", "reasoning_effort"].every((key) => typeof value[key] === "string")
    || !nonnegative(value.duration_ms) || !Number.isFinite(Date.parse(value.checked_at as string))
    || !Object.entries(body).every(([key, expected]) => value[key] === expected)) throw invalidResponse();
  return value as unknown as PromptProbeResult;
}
