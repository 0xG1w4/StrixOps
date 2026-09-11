import { HintRequestError, sendHint, type Hint, type RunDetail } from "@/lib/api";

interface Attempt {
  id: string;
  message: string;
  status: "sending" | "uncertain" | "rejected";
  code?: string;
}
export interface HintDraft {
  text: string;
  attempt: Attempt | null;
  notice: "accepted" | "failed" | null;
}
const EMPTY: HintDraft = { text: "", attempt: null, notice: null };
const drafts = new Map<string, HintDraft>();
const listeners = new Set<() => void>();
const acceptedListeners = new Set<(name: string, hint: Hint) => void>();
export const subscribeAcceptedHints = (listener: (name: string, hint: Hint) => void) => {
  acceptedListeners.add(listener);
  return () => { acceptedListeners.delete(listener); };
};
export const hintDraftKey = (name: string, agent: string) => JSON.stringify([name, agent]);
export const hintDraftSnapshot = (key: string) => drafts.get(key) || EMPTY;
export const hintDraftSubscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
const change = (key: string, value: HintDraft) => { drafts.set(key, value); listeners.forEach(listener => listener()); };
export function editHintDraft(key: string, text: string) {
  change(key, { ...hintDraftSnapshot(key), text, notice: null });
}

export function hintEligibility(run: RunDetail | null, agentId: string): "run_closed" | "unknown" | "agent_closed" | null {
  if (!run) return "unknown";
  const status = (run.status || "").toLowerCase();
  const closed = ["completed", "failed", "crashed", "stopped", "timeout", "cancelled", "finishing"];
  if (status !== "running") return closed.includes(status) ? "run_closed" : "unknown";
  if (!run.live) return "run_closed";
  const rootStatus = run.agents?.root?.status?.toLowerCase();
  if (!rootStatus || !["running", "waiting"].includes(rootStatus)) {
    return rootStatus && closed.includes(rootStatus) ? "run_closed" : "unknown";
  }
  const agent = run.agents?.[agentId];
  if (!agent || !agent.status || agent.status === "unknown") return "unknown";
  const agentStatus = agent.status.toLowerCase();
  return ["running", "waiting"].includes(agentStatus) ? null : closed.includes(agentStatus) ? "agent_closed" : "unknown";
}

function requestId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The captured key/payload owns the result even after the user switches views. */
export async function submitHintDraft(name: string, agentId: string, retry = false): Promise<Hint | null> {
  const key = hintDraftKey(name, agentId);
  const draft = hintDraftSnapshot(key);
  if (draft.attempt?.status === "sending") return null;
  const message = retry ? draft.attempt?.message : draft.text.trim();
  if (!message || (!retry && draft.attempt?.status === "uncertain")) return null;
  const previous = draft.attempt;
  const id = previous && previous.message === message ? previous.id : requestId();
  change(key, { ...draft, attempt: { id, message, status: "sending" }, notice: null });
  try {
    const result = await sendHint(name, { message, agent_id: agentId, client_request_id: id });
    const current = hintDraftSnapshot(key);
    if (current.attempt?.id === id) {
      change(key, {
        text: current.text.trim() === message ? "" : current.text,
        attempt: null,
        notice: result.status === "failed" ? "failed" : "accepted",
      });
    }
    const hint: Hint = {
      message_id: result.message_id,
      hint_token: result.hint_token,
      agent_id: result.agent_id || agentId,
      message,
      status: result.status || "queued",
      created_at: new Date().toISOString(),
      failure_code: result.failure_code,
      failure_reason: result.failure_reason,
    };
    acceptedListeners.forEach(listener => listener(name, hint));
    return hint;
  } catch (error) {
    const current = hintDraftSnapshot(key);
    if (current.attempt?.id === id) {
      change(key, {
        ...current,
        attempt: { id, message, status: error instanceof HintRequestError && !error.uncertain ? "rejected" : "uncertain", code: error instanceof HintRequestError ? error.code : "delivery_unknown" },
      });
    }
    return null;
  }
}
