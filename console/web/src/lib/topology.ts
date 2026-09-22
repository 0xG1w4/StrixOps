import { authFetch } from "@/lib/auth";

export type TopologyWarning = { code: string; run?: string };
export type TopologySource = {
  run: string; source: string; evidence: string; agent_id?: string; observed_at?: string;
};
export type TopologyRecord = Record<string, unknown>;
export type TopologyCredential = {
  id: string; source_run: string; host: string; username: string; secret_type: string;
  validation_status: string; validation_evidence?: string; has_password: boolean; has_hash: boolean;
};
export type TopologyNode = {
  id: string; address: string; ip: string; hostname: string; network_context: string;
  subnet: string; os: string; role: string; status: "observed" | "reachable";
  sources: TopologySource[]; first_seen: string; last_seen: string; severity: string;
  backfilled: boolean; vulnerabilities: TopologyRecord[]; findings: TopologyRecord[];
  credentials: TopologyCredential[];
};
export type TopologyEdge = {
  id: string; source: string; target: string; relation_type: string; verified: boolean;
  evidence: string; source_run: string;
};
export type TopologySnapshot = {
  version: number; generated_at: string; source_fingerprint: string; run_names: string[];
  nodes: TopologyNode[]; edges: TopologyEdge[]; warnings: TopologyWarning[]; partial: boolean;
};
export type ProjectTopologyResponse = {
  snapshot: TopologySnapshot | null; eligible_runs: number; stale: boolean;
  warnings: TopologyWarning[]; source_status: "available" | "partial" | "missing";
};
export type TopologySecret = { password: string; hash: string };
export type ProjectTopologyStatus = Omit<ProjectTopologyResponse, "snapshot">;

async function request<T>(projectId: string, action: "read" | "generate" | "secret", signal: AbortSignal, body?: unknown): Promise<T> {
  const url = `/api/projects/${encodeURIComponent(projectId)}/topology${action === "secret" ? "/credentials" : ""}`;
  const response = await authFetch(url, {
    method: action === "read" ? "GET" : "POST", signal,
    ...(action === "read" ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) }),
  });
  // Raw errors may contain source content. Keep them out of the interface.
  if (!response.ok) throw new Error(`topology_request_${response.status}`);
  return await response.json() as T;
}

export const getProjectTopology = (projectId: string, signal: AbortSignal) =>
  request<ProjectTopologyResponse>(projectId, "read", signal);
export async function getProjectTopologyStatus(projectId: string, fingerprint: string, signal: AbortSignal): Promise<ProjectTopologyStatus> {
  const response = await authFetch(`/api/projects/${encodeURIComponent(projectId)}/topology/status?source_fingerprint=${encodeURIComponent(fingerprint)}`, { signal });
  if (!response.ok) throw new Error(`topology_request_${response.status}`);
  return await response.json() as ProjectTopologyStatus;
}
export const generateProjectTopology = (projectId: string, signal: AbortSignal) =>
  request<ProjectTopologyResponse>(projectId, "generate", signal);
export const revealTopologyCredential = (projectId: string, nodeId: string, credential: TopologyCredential, version: number, signal: AbortSignal) =>
  request<TopologySecret>(projectId, "secret", signal, {
    node_id: nodeId, credential_id: credential.id, source_run: credential.source_run, version,
  });
