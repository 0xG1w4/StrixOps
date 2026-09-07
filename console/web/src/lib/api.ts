"use client";

/* ============================================================================
   API client — the console backend contract.

   In dev the Next rewrites proxy /api to the FastAPI server; in production
   (static export) same-origin serves both. `request` throws `status: body`
   on failure so callers can parse FastAPI detail JSON.
   ========================================================================= */

export const API = process.env.NODE_ENV === "production" ? "" : "http://127.0.0.1:8300";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store", ...init });
  if (!res.ok) {
    let detail = "";
    try {
      const text = (await res.text()).trim();
      if (text) detail = text.length > 300 ? `${text.slice(0, 300)}…` : text;
    } catch {
      /* body unreadable — keep the status line */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function getJSON<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function postJSON<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function putJSON<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function putText<T>(path: string, content: string): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

export function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

export function apiURL(path: string): string {
  return `${API}${path}`;
}

export function streamURL(path: string): string {
  return `${API}${path}`;
}

/* ------------------------------------------------------------------ health */

/** GET /api/health */
export interface Health {
  ok: boolean;
  runs_root: string;
  live_runs: number;
}

/* -------------------------------------------------------------------- runs */

export interface SeverityCounts {
  [severity: string]: number;
}

export interface RunTotals {
  severity: SeverityCounts;
  runs: number;
  live: number;
}

export interface RunSummary {
  name: string;
  target: string;
  scan_type: string;
  project_id: string;
  /** Whether the target still matches the project's current launch scope. */
  scope_match?: boolean;
  status: string;
  live: boolean;
  stale: boolean;
  start_time: string;
  end_time: string | null;
  duration_seconds: number | null;
  vulnerability_count: number;
  internal_finding_count: number;
  severity: SeverityCounts;
  failure_reason?: string;
  engine_exit_code?: number;
  /** Egress transport recorded in the run's scan_config (raw, for Rerun). */
  socks5?: string;
  gsocket?: string;
  crypto?: boolean;
  /** Model id the run used (engine scan_config or console launch sidecar). */
  model?: string;
  /** Execution mode; undefined when unrecorded (runs older than the field). */
  dry_run?: boolean;
  /** Run-level token accounting (present once the first model turn lands). */
  llm_usage?: {
    requests: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
  };
}

/** GET /api/runs */
export interface RunsPage {
  runs: RunSummary[];
  totals: RunTotals;
}

export interface AgentEntry {
  name: string;
  task: string;
  status: string;
  parent_id: string | null;
}

export interface HintsSummary {
  total: number;
  delivered: number;
}

/** GET /api/runs/{name} */
export type RunDetail = RunSummary & {
  agents: Record<string, AgentEntry>;
  hints_summary: HintsSummary;
};

export interface Message {
  /** event-index as string */
  id: string;
  type: string;
  timestamp: string;
  agent_id: string;
  agent_name: string;
  content?: string;
  args?: Record<string, unknown>;
  title?: string;
  result?: unknown;
  severity?: string;
  report_id?: string;
  finding_type?: string;
  hint_status?: string;
}

export interface ConversationPage {
  messages: Message[];
  cursor: number;
  live: boolean;
}

/* ---------------------------------------------------------------- findings */

export interface Vulnerability {
  id: string;
  title: string;
  severity: string;
  timestamp?: string;
  target?: string;
  description?: string;
  impact?: string;
  technical_analysis?: string;
  poc_description?: string;
  poc_script_code?: string;
  remediation_steps?: string;
  evidence?: string;
  cvss?: number | null;
  cvss_vector?: string;
  endpoint?: string;
  method?: string;
  cve?: string;
  cwe?: string;
  confidence?: string;
  counterevidence?: string;
  assumptions?: string;
}

export interface InternalFinding {
  id: string;
  title?: string;
  finding_type?: string;
  severity?: string;
  host?: string;
  source_file?: string;
}

export interface FindingsPage {
  vulnerabilities: Vulnerability[];
  internal: InternalFinding[];
}

/* ------------------------------------------------------------------ report */

/** GET /api/runs/{name}/report */
export interface ReportPage {
  markdown: string;
}

/* -------------------------------------------------------------------- hints */

export type HintStatus = "queued" | "delivered" | "acked";

export interface Hint {
  message_id: string;
  agent_id: string;
  agent_name?: string;
  message: string;
  status: HintStatus;
  hint_token: string;
  created_at: string;
}

export interface HintsPage {
  hints: Hint[];
}

export interface HintSendResult {
  ok: boolean;
  message_id: string;
  hint_token: string;
}

/* --------------------------------------------------------------------- log */

/** GET /api/runs/{name}/log */
export interface LogPage {
  text: string;
}

/* --------------------------------------------------------------- artifacts */

export interface ArtifactFile {
  path: string;
  size: number;
  mtime?: number;
  is_dir?: boolean;
}

export interface ArtifactsIndexPage {
  files: ArtifactFile[];
}

/** Download URL for the run-dir zip bundle (GET /api/runs/{name}/archive). */
export function archiveURL(name: string): string {
  return apiURL(`/api/runs/${encodeURIComponent(name)}/archive`);
}

/* ------------------------------------------------------------------ launch */

/** POST /api/scans */
export interface ScanRequest {
  target: string;
  scan_type: string;
  project_id?: string | null;
  crypto?: boolean;
  socks5?: string | null;
  gsocket?: string | null;
  instruction?: string;
  dry_run?: boolean;
  profile_id?: string;
  language?: string;
  llm_api_base?: string;
  llm_api_key?: string;
  strix_llm?: string;
}

/** POST /api/scans response */
export interface ScanLaunched {
  ok: boolean;
  scan_id: string;
  run_name: string;
  pid: number;
}

/* ------------------------------------------------------------------ settings */

/** GET /api/settings profile entry (API key always masked server-side). */
export interface ModelProfile {
  id: string;
  name: string;
  route_type: "custom" | "openrouter";
  llm_api_base: string;
  llm_api_key: string;
  llm_api_key_set: boolean;
  model_web: string;
  model_internal: string;
  created_at: string;
  updated_at: string;
}

/** GET /api/settings */
export interface SettingsPage {
  profiles: ModelProfile[];
  active_profile_id: string | null;
}

/** Profile write body (masked/empty key = keep stored key on update). */
export interface ProfileWrite {
  name: string;
  route_type: "custom" | "openrouter";
  llm_api_base?: string;
  llm_api_key?: string;
  model_web?: string;
  model_internal?: string;
}

export interface CatalogModel {
  id: string;
  name: string;
}

export class ModelCatalogError extends Error {
  constructor(public readonly code: string) {
    super(code);
    this.name = "ModelCatalogError";
  }
}

function isAbortError(error: unknown): boolean {
  return !!error && typeof error === "object" && "name" in error && error.name === "AbortError";
}

/** Fetch through the console so stored keys never have to reach the browser. */
export async function fetchModelCatalog(
  body: Pick<ProfileWrite, "route_type" | "llm_api_base" | "llm_api_key"> & { profile_id?: string | null },
  signal?: AbortSignal,
): Promise<{ models: CatalogModel[]; count: number }> {
  let response: Response;
  try {
    response = await fetch(`${API}/api/settings/models`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (signal?.aborted || isAbortError(error)) throw error;
    throw new ModelCatalogError("console_connection");
  }

  let result;
  try {
    result = await response.json();
  } catch (error) {
    if (signal?.aborted || isAbortError(error)) throw error;
    if (error instanceof TypeError) throw new ModelCatalogError("console_connection");
  }
  if (!response.ok) {
    // A provider/profile failure has a structured code. A plain 404/405 means
    // this console endpoint is missing, commonly from a backend not restarted.
    const code = result?.detail?.code;
    if (typeof code === "string" && code) throw new ModelCatalogError(code);
    if (response.status === 404 || response.status === 405) {
      throw new ModelCatalogError("console_endpoint_missing");
    }
    throw new ModelCatalogError("console_http_error");
  }
  if (!Array.isArray(result?.models)) throw new ModelCatalogError("console_invalid_response");
  return result;
}

export async function getSettings(): Promise<SettingsPage> {
  return getJSON<SettingsPage>("/api/settings");
}

interface ModelProfileWrapped {
  ok: boolean;
  profile: ModelProfile;
}

export async function createProfile(body: ProfileWrite): Promise<ModelProfile> {
  const res = await postJSON<ModelProfileWrapped>("/api/settings/profiles", body);
  return res.profile;
}

export async function updateProfile(id: string, body: ProfileWrite): Promise<ModelProfile> {
  const res = await request<ModelProfileWrapped>(
    `/api/settings/profiles/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }
  );
  return res.profile;
}

export async function deleteProfile(id: string): Promise<void> {
  await del(`/api/settings/profiles/${encodeURIComponent(id)}`);
}

export async function activateProfile(id: string): Promise<void> {
  await postJSON("/api/settings/activate", { profile_id: id });
}


/* ---------------------------------------------------------------- projects */

export type ProjectScopeRule =
  | { kind: "any"; value: "*" }
  | { kind: "domain"; value: string; include_subdomains: boolean }
  | { kind: "ip"; value: string }
  | { kind: "cidr"; value: string };

/** GET /api/projects entry */
export interface ProjectSummary {
  id: string;
  name: string;
  description: string;
  color: string;
  created_at: string;
  updated_at: string;
  run_count: number;
  live_count: number;
  vulnerability_count: number;
  internal_finding_count: number;
  severity: { [sev: string]: number };
  last_run_at: string;
  scope_rules: ProjectScopeRule[];
  scope_revision: number;
}

export interface ProjectsPage {
  projects: ProjectSummary[];
  unassigned_count: number;
}

export interface ProjectDetail {
  project: ProjectSummary;
  runs: RunSummary[];
}

export interface ProjectFindings {
  vulnerabilities: (Vulnerability & { source_run: string })[];
  internal: (InternalFinding & { source_run: string })[];
  by_run: { [runName: string]: { vulns: number; internal: number } };
}

export interface ProjectSkillEntry {
  skill: string;
  category: string;
  hits: number;
  runs: number;
}

export interface ProjectSkillAnalytics {
  top_skills: ProjectSkillEntry[];
  skills: ProjectSkillEntry[];
  categories: Array<{ category: string; hits: number; loads?: number }>;
  by_run: Array<{
    run: string;
    target: string;
    status: string;
    start_time: string;
    skills: Record<string, number>;
    hits: number;
    total: number;
  }>;
  runs: Array<{
    run: string;
    target: string;
    status: string;
    start_time: string;
    skills: Record<string, number>;
    total: number;
  }>;
  totals: {
    distinct_skills: number;
    total_hits: number;
    runs_with_skills: number;
    project_runs: number;
  };
}

export interface ProjectReportVersion {
  project_id: string;
  project_name: string;
  version: number;
  version_id: string;
  status: "ready" | "failed" | "generating" | "queued" | "corrupt";
  ready: boolean;
  language: "zh-CN" | "en";
  generated_at: string;
  source_snapshot_hash: string;
  source_runs: Array<Record<string, unknown>>;
  included_run_count: number;
  excluded_runs: Array<{ run: string; reason: string }>;
  total_run_count: number;
  stale?: boolean;
  content?: string;
  integrity_ok?: boolean;
  stale_reasons?: Array<{ code: string; runs?: string[] }>;
  stats?: {
    unique_vulnerability_count: number;
    vulnerability_occurrence_count: number;
    unique_internal_finding_count: number;
    severity: Record<string, number>;
    highest_severity: string;
    target_count: number;
  };
  snapshot_summary?: {
    key_findings: Array<{
      fingerprint: string;
      title: string;
      severity: string;
      target: string;
      endpoint: string;
      occurrences: number;
      source_runs: string[];
    }>;
  };
}

export async function getProjects(): Promise<ProjectsPage> {
  return getJSON<ProjectsPage>("/api/projects");
}

export async function createProject(body: { name: string; description?: string; color?: string }): Promise<ProjectSummary> {
  const res = await postJSON<{ ok: boolean; project: ProjectSummary }>("/api/projects", body);
  return res.project;
}

export async function updateProject(id: string, body: { name?: string; description?: string; color?: string }): Promise<ProjectSummary> {
  const res = await request<{ ok: boolean; project: ProjectSummary }>(`/api/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.project;
}

export async function deleteProject(id: string): Promise<void> {
  await del(`/api/projects/${encodeURIComponent(id)}`);
}

export async function getProjectRuns(id: string): Promise<ProjectDetail> {
  return getJSON<ProjectDetail>(`/api/projects/${encodeURIComponent(id)}/runs`);
}

export async function getProjectFindings(id: string): Promise<ProjectFindings> {
  return getJSON<ProjectFindings>(`/api/projects/${encodeURIComponent(id)}/findings`);
}

export async function getProjectReport(id: string): Promise<{ markdown: string }> {
  return getJSON<{ markdown: string }>(`/api/projects/${encodeURIComponent(id)}/report`);
}

export async function updateProjectScope(
  id: string,
  scopeRules: ProjectScopeRule[]
): Promise<{ project: ProjectSummary; historical_out_of_scope: string[] }> {
  const res = await putJSON<{
    ok: boolean;
    project: ProjectSummary;
    historical_out_of_scope: string[];
  }>(
    `/api/projects/${encodeURIComponent(id)}/scope`,
    { scope_rules: scopeRules }
  );
  return {
    project: res.project,
    historical_out_of_scope: res.historical_out_of_scope ?? [],
  };
}

export async function getProjectSkillAnalytics(id: string): Promise<ProjectSkillAnalytics> {
  return getJSON<ProjectSkillAnalytics>(
    `/api/projects/${encodeURIComponent(id)}/analytics/skills`
  );
}

export async function getProjectReports(
  id: string
): Promise<{ reports: ProjectReportVersion[]; stale: boolean }> {
  return getJSON<{ reports: ProjectReportVersion[]; stale: boolean }>(
    `/api/projects/${encodeURIComponent(id)}/reports`
  );
}

export async function generateProjectReport(
  id: string,
  language: "zh-CN" | "en"
): Promise<ProjectReportVersion> {
  const res = await postJSON<{ ok: boolean; report: ProjectReportVersion }>(
    `/api/projects/${encodeURIComponent(id)}/reports`,
    { language }
  );
  return res.report;
}

export async function assignRun(runName: string, projectId: string): Promise<void> {
  await postJSON(`/api/runs/${encodeURIComponent(runName)}/assign`, { project_id: projectId });
}


/* ---------------------------------------------------------------- evidence */

export interface EvidenceEntry {
  filename: string;
  size: number;
  size_human: string;
  sha256?: string;
  sha256_short?: string;
  category: string;
  category_label: string;
  collected_at: string;
  oversize: boolean;
  download_url: string;
}

export interface EvidencePage {
  evidence: EvidenceEntry[];
  totals: { count: number; total_bytes: number; total_human?: string };
}

export async function getEvidence(runName: string): Promise<EvidencePage> {
  return getJSON<EvidencePage>(`/api/runs/${encodeURIComponent(runName)}/evidence`);
}

/* ------------------------------------------------------------------- misc */

/** POST /api/runs/{name}/stop, DELETE /api/runs/{name}, POST /api/runs/{name}/hints */
export interface OkResult {
  ok: boolean;
}
