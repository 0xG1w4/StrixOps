"use client";

/* ============================================================================
   ConversationView — the live transcript panel of the run cockpit.

   Transport: SSE (/api/runs/:name/stream) as primary, cursor polling
   (/api/runs/:conversation?after=) as fallback + initial load. Both share the
   event-line index space, so messages dedupe by id across the two paths.
   Raw SSE data lines are classified into message shapes client-side — the
   same argument-shape classification the engine's parser applies.
   ========================================================================= */

import * as React from "react";
import { ArrowUp, Check, ChevronDown, PanelLeft, Radio, RotateCcw } from "lucide-react";
import { EmptyState, SeverityChip } from "@/components/ui";
import ConversationAgents from "@/components/ConversationAgents";
import HintComposer from "@/components/HintComposer";
import { subscribeAcceptedHints } from "./hint-state";
import styles from "./ConversationControls.module.css";
import { apiURL, streamURL } from "@/lib/api";
import type { ConversationPage, Hint, HintsPage, Message, RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const DOM_WINDOW = 600;
const POLL_INTERVAL_MS = 5000;
const HINTS_POLL_MS = 4000;

const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "timeout",
  "cancelled",
  "stopped",
  "crashed",
]);

/* ============================================================================
   Raw event → message classification (port of the engine parser)
   ========================================================================= */

interface RawEvent {
  timestamp?: string;
  event_type?: string;
  actor?: { agent_id?: string; agent_name?: string } | null;
  payload?: Record<string, unknown> | null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function inferToolKind(args: Record<string, unknown>): {
  kind?: string;
  title?: string;
  name?: string;
} {
  if ("command" in args || "cmd" in args || "chars" in args || "session_id" in args) {
    const command = args.command ?? args.cmd ?? args.chars ?? "";
    return { kind: "shell", title: String(command ?? "") };
  }
  if ("todos" in args || "todo_ids" in args || "updates" in args) {
    return { kind: "todo" };
  }
  if (
    "executive_summary" in args &&
    "methodology" in args &&
    "technical_analysis" in args &&
    "recommendations" in args
  ) {
    return { kind: "finish" };
  }
  const vulnKeys = ["description", "impact", "technical_analysis", "endpoint", "target"];
  if ("title" in args && vulnKeys.some((k) => k in args)) {
    return { kind: "vulnerability_report", title: String(args.title ?? "") };
  }
  if ("result_summary" in args) return { kind: "agent_finish" };
  if ("name" in args && "task" in args) return { kind: "dispatch", name: String(args.name ?? "") };
  if ("thought" in args) return { kind: "thinking" };
  if ("finding_type" in args && "title" in args) {
    return { kind: "internal_finding", title: String(args.title ?? "") };
  }
  return {};
}

function eventToMessage(event: RawEvent, id: string): Message | null {
  const etype = event.event_type ?? "";
  const actor = event.actor ?? {};
  const payload = asRecord(event.payload);
  const base = {
    id,
    timestamp: event.timestamp ?? "",
    agent_id: actor.agent_id ?? "",
    agent_name: actor.agent_name ?? "",
  };

  if (etype === "chat.message") {
    return { ...base, type: "message", content: String(payload.content ?? "") };
  }
  if (etype === "tool.execution.started") {
    const args = asRecord(payload.args);
    const ctx = inferToolKind(args);
    return {
      ...base,
      type: ctx.kind || "tool",
      args,
      title: ctx.title ?? ctx.name ?? "",
    };
  }
  if (etype === "tool.execution.updated") {
    return { ...base, type: "output", result: payload.result };
  }
  if (etype === "vulnerability.found") {
    const finding = asRecord(payload.finding);
    return {
      ...base,
      type: "report",
      title: String(finding.title ?? payload.report_id ?? "finding"),
      severity: String(finding.severity ?? "").toUpperCase(),
      report_id: String(payload.report_id ?? ""),
      args: finding,
    };
  }
  if (etype === "finding.internal_created") {
    const finding = asRecord(payload.finding);
    return {
      ...base,
      type: "internal_finding",
      title: String(finding.title ?? finding.id ?? "internal finding"),
      severity: String(finding.severity ?? "").toUpperCase(),
      finding_type: String(finding.finding_type ?? ""),
    };
  }
  if (etype === "agent.created") {
    return {
      ...base,
      type: "system",
      content: `agent '${actor.agent_name ?? base.agent_id}' created`,
    };
  }
  if (etype === "agent.status.updated") {
    return { ...base, type: "system", content: `status → ${String(payload.status ?? "")}` };
  }
  if (etype === "run.configured") {
    return { ...base, type: "system", content: "run configured" };
  }
  if (etype === "run.completed") {
    return {
      ...base,
      type: "system",
      content: `run completed — ${String(payload.vulnerability_count ?? 0)} finding(s), ${String(
        payload.duration_seconds ?? 0
      )}s`,
    };
  }
  return null;
}

/* ============================================================================
   Small helpers
   ========================================================================= */

function clockOf(timestamp: string): string {
  if (!timestamp) return "";
  const d = new Date(timestamp);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour12: false });
}

function argStr(message: Message, key: string): string {
  const value = asRecord(message.args)[key];
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : String(value);
}

function argList(message: Message, key: string): string[] {
  const value = asRecord(message.args)[key];
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string");
}

function resultText(result: unknown): string {
  if (result === undefined || result === null) return "";
  if (typeof result === "string") return result;
  try {
    return JSON.stringify(result, null, 2);
  } catch {
    return String(result);
  }
}

function stringifyContent(content: unknown): string {
  if (content === undefined || content === null) return "";
  if (typeof content === "string") return content;
  try {
    return JSON.stringify(content, null, 2);
  } catch {
    return String(content);
  }
}

interface TodoItem {
  title: string;
  description: string;
  status: string;
}

function parseTodoEntries(value: unknown): TodoItem[] {
  let arr: unknown = value;
  if (typeof value === "string") {
    try {
      arr = JSON.parse(value);
    } catch {
      return value.trim() ? [{ title: value, description: "", status: "" }] : [];
    }
  }
  if (!Array.isArray(arr)) {
    const record = asRecord(arr);
    arr = Array.isArray(record.todos) ? record.todos : [];
  }
  if (!Array.isArray(arr)) return [];
  const items: TodoItem[] = [];
  for (const entry of arr) {
    if (typeof entry === "string") {
      if (entry.trim()) items.push({ title: entry, description: "", status: "" });
      continue;
    }
    const record = asRecord(entry);
    const title = String(record.title ?? record.task ?? record.content ?? record.activeForm ?? "");
    if (!title) continue;
    items.push({
      title,
      description: String(record.description ?? ""),
      status: String(record.status ?? "").toLowerCase(),
    });
  }
  return items;
}

/* ============================================================================
   Frame + chrome — .conversation-message rebuilt on Tailwind utilities
   ========================================================================= */

function MessageFrame({
  variant,
  className,
  children,
}: {
  variant?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`relative overflow-hidden rounded-[1rem] border border-[rgba(212,175,55,0.1)] bg-[rgba(10,12,24,0.72)] px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.03)] ${
        variant ?? ""
      } ${className ?? ""}`}
    >
      {children}
      <span
        aria-hidden
        className="pointer-events-none absolute inset-x-4 bottom-0.5 h-px bg-[linear-gradient(90deg,transparent,rgba(0,229,255,0.18),transparent)] opacity-65"
      />
    </div>
  );
}

function Chrome({
  label,
  tone,
  agent,
  time,
  agentClass,
}: {
  label: string;
  tone: string;
  agent: string;
  time: string;
  agentClass?: string;
}) {
  return (
    <div className="mb-2 flex items-center gap-2">
      <span className={`status-pill status-${tone} status-subtle`}>{label}</span>
      {agent && (
        <span
          className={`truncate font-mono text-[10px] uppercase tracking-[0.16em] ${
            agentClass ?? "text-fg-muted"
          }`}
        >
          {agent}
        </span>
      )}
      {time && <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-faint">{time}</span>}
    </div>
  );
}

/* ============================================================================
   [hint:token] echo highlight inside chat messages
   ========================================================================= */

/* Matches the server's ack regex: the full token alphabet the writers
 * produce — hex tokens and mixed alphanumerics like the scripted fixture's
 * "echo12345" — so echoed tokens always highlight. */
const HINT_TOKEN_SPLIT = /(\[hint:[0-9a-zA-Z_-]+\])/g;

/** Interleave order: timestamp first, event index as the tiebreaker. */
function messageOrder(a: Message, b: Message): number {
  const ta = Date.parse(a.timestamp || "");
  const tb = Date.parse(b.timestamp || "");
  if (!Number.isNaN(ta) && !Number.isNaN(tb) && ta !== tb) return ta - tb;
  const ia = Number(a.id);
  const ib = Number(b.id);
  const na = Number.isFinite(ia) ? ia : Number.MAX_SAFE_INTEGER;
  const nb = Number.isFinite(ib) ? ib : Number.MAX_SAFE_INTEGER;
  return na - nb;
}

function MessageBody({ text }: { text: string }) {
  const parts = text.split(HINT_TOKEN_SPLIT);
  return (
    <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-fg">
      {parts.map((part, i) =>
        part.startsWith("[hint:") && part.endsWith("]") ? (
          <code
            key={i}
            className="mx-0.5 rounded-[0.45rem] border border-warning/25 bg-warning/10 px-1 py-px font-mono text-[11px] tracking-[0.04em] text-[#f0d060]"
          >
            {part}
          </code>
        ) : (
          part
        )
      )}
    </pre>
  );
}

/* ============================================================================
   Collapsible output block
   ========================================================================= */

function OutputBlock({ text, time }: { text: string; time: string }) {
  const { t } = useI18n();
  const [expanded, setExpanded] = React.useState(false);
  const long = text.length > 600 || text.split("\n").length > 14;
  if (!text) return null;
  return (
    <div className="my-0.5">
      <MessageFrame
        variant={`border-line/6 bg-surface-deep/56 ${expanded ? "" : "max-h-56 overflow-hidden"}`}
      >
        <Chrome label={t("conversation.event.output")} tone="neutral" agent="" time={time} />
        <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-snug text-fg-muted">
          {text}
        </pre>
      </MessageFrame>
      {long && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-1 ml-2 inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-muted transition-colors hover:text-accent"
        >
          <ChevronDown
            className={`h-3 w-3 transition-transform ${expanded ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
          {expanded
            ? t("conversation.event.collapse")
            : t("conversation.event.expand", { n: text.split("\n").length })}
        </button>
      )}
    </div>
  );
}

/* ============================================================================
   Todo checklist
   ========================================================================= */

function TodoGlyph({ status }: { status: string }) {
  if (status === "done") {
    return <Check className="mt-0.5 h-3 w-3 shrink-0 text-success" strokeWidth={2.5} />;
  }
  if (status === "in_progress") {
    return (
      <span
        aria-hidden
        className="mt-[5px] h-2 w-2 shrink-0 animate-spin rounded-full border-[1.5px] border-warning/25 border-t-warning"
      />
    );
  }
  return <span aria-hidden className="mt-[3px] text-[10px] leading-3 text-fg-faint">○</span>;
}

function TodoList({ items }: { items: TodoItem[] }) {
  return (
    <div className="space-y-1">
      {items.map((item, i) => (
        <div key={i} className="flex items-start gap-2 text-xs">
          <span className="select-none pt-px font-mono text-[10px] text-fg-muted">{i + 1}.</span>
          <TodoGlyph status={item.status} />
          <div className="min-w-0">
            <div
              className={`font-medium leading-snug ${
                item.status === "done" ? "text-fg-muted line-through decoration-dark-600" : "text-fg"
              }`}
            >
              {item.title}
            </div>
            {item.description && (
              <div className="mt-0.5 text-[11px] leading-snug text-fg-muted">{item.description}</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ============================================================================
   Message card — one per event, memoized (up to 600 in the DOM)
   ========================================================================= */

const MessageCard = React.memo(function MessageCard({ message }: { message: Message }) {
  const { t, locale } = useI18n();
  const agent = message.agent_name || message.agent_id || "";
  const time = clockOf(message.timestamp);
  const args = asRecord(message.args);

  switch (message.type) {
    case "message":
      return (
        <MessageFrame variant="border-accent/12 bg-raised/2">
          <div className="mb-1 flex items-center gap-2">
            <span className="truncate font-mono text-[10px] uppercase tracking-[0.16em] text-[#f0d060]">
              {agent || t("conversation.agent")}
            </span>
            {time && (
              <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-faint">{time}</span>
            )}
          </div>
          <MessageBody text={String(message.content ?? "")} />
        </MessageFrame>
      );

    case "thinking":
      return (
        <MessageFrame variant="border-violet/10 bg-raised/2">
          <Chrome label={t("conversation.event.thinking")} tone="violet" agent={agent} time={time} />
          <div className="whitespace-pre-wrap break-words text-sm italic leading-relaxed text-fg-2">
            {argStr(message, "thought") || String(message.content ?? "")}
          </div>
        </MessageFrame>
      );

    case "shell":
    case "terminal":
      return (
        <MessageFrame
          variant="my-1 border-line/6 bg-surface-deep/78 shadow-[inset_0_1px_0_rgba(255,255,255,0.04)]"
        >
          <Chrome label={t("conversation.event.command")} tone="accent" agent={agent} time={time} />
          <pre className="whitespace-pre-wrap break-all font-mono text-xs leading-snug text-fg">
            <span className="mr-1.5 select-none text-accent">$</span>
            {message.title || argStr(message, "command")}
          </pre>
        </MessageFrame>
      );

    case "output":
      return <OutputBlock text={resultText(message.result)} time={time} />;

    case "todo": {
      const items = parseTodoEntries(args.todos ?? args.updates ?? args.todo_ids ?? args);
      const action =
        "todos" in args
          ? t("conversation.event.plan")
          : "updates" in args
            ? t("conversation.event.planUpdate")
            : t("conversation.event.planMarked");
      return (
        <MessageFrame variant="border-warning/16 bg-warning/6">
          <Chrome label={action} tone="warning" agent={agent} time={time} />
          {items.length ? (
            <TodoList items={items} />
          ) : (
            <div className="text-xs text-fg-muted">{t("conversation.event.todoUpdated")}</div>
          )}
        </MessageFrame>
      );
    }

    case "dispatch":
    case "spawn": {
      const childName = argStr(message, "name") || message.title;
      const task = argStr(message, "task");
      const skills = argList(message, "skills");
      return (
        <MessageFrame variant="my-1 border-accent/16 bg-accent/6">
          <div className="mb-1 flex items-center gap-2">
            <span className="status-pill status-accent status-subtle">
              {t("conversation.event.dispatch")}
            </span>
            {agent && <span className="truncate text-xs text-fg-2">{agent}</span>}
            <span className="text-[10px] text-fg-muted">→</span>
            <span className="truncate font-mono text-xs font-medium text-accent/90">{childName}</span>
            {time && (
              <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-faint">{time}</span>
            )}
          </div>
          {task && <div className="break-words text-xs leading-relaxed text-fg-2">{task}</div>}
          {skills.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {skills.map((s) => (
                <span
                  key={s}
                  className="rounded-full border border-line/6 bg-surface/60 px-2 py-0.5 text-[9px] text-fg-muted"
                >
                  {s}
                </span>
              ))}
            </div>
          )}
        </MessageFrame>
      );
    }

    case "report":
    case "vulnerability_report": {
      const severity = message.severity || argStr(message, "severity");
      const cvss = args.cvss;
      return (
        <MessageFrame variant="my-1 border-danger/24 bg-[rgba(255,91,91,0.06)]">
          <Chrome label={t("conversation.event.vulnerability")} tone="danger" agent={agent} time={time} />
          <div className="break-words text-sm font-semibold leading-snug text-fg">
            {message.title || t("projects.findings")}
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-2">
            {severity && <SeverityChip severity={severity} />}
            {typeof cvss === "number" && (
              <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-muted">
                CVSS {cvss.toFixed(1)}
              </span>
            )}
            {message.report_id && (
              <span className="truncate font-mono text-[10px] text-fg-faint">{message.report_id}</span>
            )}
          </div>
        </MessageFrame>
      );
    }

    case "agent_finish":
      return (
        <MessageFrame variant="border-success/18 bg-success/6">
          <Chrome label={t("conversation.event.agentFinished")} tone="success" agent={agent} time={time} />
          <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-fg">
            {argStr(message, "result_summary") || t("conversation.event.agentFinished")}
          </div>
        </MessageFrame>
      );

    case "finish":
      return (
        <MessageFrame variant="my-1 border-success/20 bg-success/6">
          <Chrome label={t("conversation.event.executiveReport")} tone="success" agent={agent} time={time} />
          <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-fg">
            {argStr(message, "executive_summary") || t("conversation.event.scanFinished")}
          </div>
        </MessageFrame>
      );

    case "internal_finding":
      return (
        <MessageFrame variant="border-warning/20 bg-warning/6">
          <Chrome label={t("projects.internal")} tone="warning" agent={agent} time={time} />
          <div className="flex flex-wrap items-center gap-2">
            {message.finding_type && (
              <span className="mono-chip chip-warning py-0.5 text-[9px]">{message.finding_type}</span>
            )}
            <span className="min-w-0 flex-1 break-words text-sm font-semibold leading-snug text-fg">
              {message.title}
            </span>
          </div>
        </MessageFrame>
      );

    case "operator_hint":
    case "hints": {
      const status = String(message.hint_status || "queued").toLowerCase();
      const tone = status === "failed" ? "danger" : status === "acked" ? "warning" : status === "delivered" ? "success" : "accent";
      const labels: Record<string, [string, string]> = {
        queued: ["等待交付", "Queued"], delivered: ["已交付", "Delivered"],
        acked: ["已確認收到", "Receipt confirmed"], failed: ["交付失敗", "Delivery failed"],
      };
      const label = labels[status]?.[locale === "en" ? 1 : 0] || (locale === "en" ? "Unknown status" : "狀態未知");
      return (
        <MessageFrame variant="ml-auto max-w-[85%] border-accent/16 bg-accent/7">
          <div className="mb-1 flex items-center gap-2">
            <span className={`status-pill status-${tone} status-subtle`}>{label}</span>
            <span className="truncate font-mono text-[10px] text-fg-muted">
              {t("conversation.you")} → {agent || "root"}
            </span>
            {time && (
              <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-faint">{time}</span>
            )}
          </div>
          <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-fg">
            {String(message.content ?? message.title ?? "")}
          </div>
          {status === "acked" && <p className="mt-2 text-[10px] text-fg-muted">{locale === "en" ? "The agent echoed the receipt token. This does not mean the requested work is complete." : "代理已回覆確認 token；這不代表指令要求的工作已完成。"}</p>}
          {status === "failed" && <p className={styles.failure}>{argStr(message, "failure_reason") || (locale === "en" ? "The hint could not be delivered." : "指令無法交付。")}</p>}
        </MessageFrame>
      );
    }

    case "system":
      return (
        <div className="py-2 text-center">
          <span className="status-pill status-neutral status-subtle">
            {t("conversation.event.system")}
            {agent ? ` · ${agent}` : ""}
            {" · "}
            {String(message.content ?? "")}
          </span>
        </div>
      );

    default: {
      const body =
        stringifyContent(message.args && Object.keys(message.args).length ? message.args : message.content) ||
        message.title ||
        "";
      return (
        <MessageFrame variant="border-line/8 bg-surface-deep/40">
          <Chrome
            label={t("conversation.event.other", { type: message.type || "—" })}
            tone="neutral"
            agent={agent}
            time={time}
          />
          {body && (
            <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-snug text-fg-muted">
              {body}
            </pre>
          )}
        </MessageFrame>
      );
    }
  }
});

/* ============================================================================
   ConversationView
   ========================================================================= */

type ConnState = "connecting" | "stream" | "reconnecting" | "closed";

export default function ConversationView({
  name,
  run,
  selectedAgentId,
  onSelectAgent,
}: {
  name: string;
  run: RunDetail | null;
  selectedAgentId?: string;
  onSelectAgent?: (id: string) => void;
}) {
  const { t, locale } = useI18n();
  const c = (zh: string, en: string) => locale === "en" ? en : zh;
  const [messages, setMessages] = React.useState<Message[]>([]);
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [conn, setConn] = React.useState<ConnState>("connecting");
  const [closedStatus, setClosedStatus] = React.useState<string | null>(null);
  const [follow, setFollow] = React.useState(true);
  const [localAgent, setLocalAgent] = React.useState("");
  const agentFilter = selectedAgentId ?? localAgent;
  const [agentsOpen, setAgentsOpen] = React.useState(false);
  const [focusVersion, setFocusVersion] = React.useState(0);
  const agentToggle = React.useRef<HTMLButtonElement>(null);
  const chooseAgent = (id: string) => {
    if (onSelectAgent) onSelectAgent(id); else setLocalAgent(id);
    setFocusVersion(value => value + 1);
    if (window.innerWidth <= 900) setAgentsOpen(false);
  };
  const closeAgents = () => { setAgentsOpen(false); agentToggle.current?.focus(); };
  const [windowSize, setWindowSize] = React.useState(DOM_WINDOW);

  const transportEpoch = React.useRef(0);
  const requests = React.useRef(new Set<AbortController>());
  const cursorRef = React.useRef(-1);
  const idsRef = React.useRef<Set<string>>(new Set());
  const esRef = React.useRef<EventSource | null>(null);
  const closedRef = React.useRef<string | null>(null);
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const followRef = React.useRef(true);
  followRef.current = follow;

  /* ---- operator-hint ledger — merged into the transcript timeline ----
     The engine emits no hint event; the ledger (/api/runs/:name/hints) is
     the source of truth for what the operator sent and its delivery state. */
  const [hints, setHints] = React.useState<Hint[]>([]);
  const [hintError, setHintError] = React.useState(false);
  const [hintRevision, setHintRevision] = React.useState(0);
  const runLive = Boolean(run?.live);
  React.useEffect(() => subscribeAcceptedHints((runName, hint) => {
    if (runName !== name) return;
    setHints(previous => [...previous.filter(item => item.message_id !== hint.message_id), hint]);
    setHintRevision(value => value + 1);
  }), [name]);
  React.useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let resume = false;
    const loadHints = async () => {
      if (disposed || document.hidden || controller) return;
      const current = new AbortController();
      controller = current;
      let timedOut = false;
      const deadline = window.setTimeout(() => { timedOut = true; current.abort(); }, 10_000);
      try {
        const response = await fetch(apiURL(`/api/runs/${encodeURIComponent(name)}/hints`), { cache: "no-store", signal: current.signal });
        if (!response.ok) throw new Error("unavailable");
        const page: HintsPage = await response.json();
        if (!Array.isArray(page.hints)) throw new Error("unavailable");
        if (!disposed && !current.signal.aborted) { setHints(page.hints); setHintError(false); }
      } catch {
        if (!disposed && (!current.signal.aborted || timedOut)) setHintError(true);
      } finally {
        window.clearTimeout(deadline);
        controller = null;
        if (!disposed && !document.hidden) {
          if (resume) { resume = false; void loadHints(); }
          else if (runLive) timer = window.setTimeout(() => void loadHints(), HINTS_POLL_MS);
        }
      }
    };
    const visibility = () => {
      window.clearTimeout(timer);
      if (document.hidden) { resume = false; controller?.abort(); }
      else if (controller) resume = true;
      else void loadHints();
    };
    void loadHints();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [name, runLive, hintRevision]);

  const append = React.useCallback((incoming: Message[]) => {
    if (!incoming.length) return;
    setMessages((prev) => {
      const fresh = incoming.filter((m) => !idsRef.current.has(m.id));
      if (!fresh.length) return prev;
      for (const m of fresh) idsRef.current.add(m.id);
      return [...prev, ...fresh].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
    });
  }, []);

  const fetchPage = React.useCallback(
    async (after: number, replace: boolean) => {
      const epoch = transportEpoch.current;
      const controller = new AbortController();
      requests.current.add(controller);
      const deadline = window.setTimeout(() => controller.abort(), 12_000);
      let data: ConversationPage;
      try {
        const response = await fetch(apiURL(`/api/runs/${encodeURIComponent(name)}/conversation?after=${after}`), { cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error("unavailable");
        data = await response.json();
      } finally {
        window.clearTimeout(deadline);
        requests.current.delete(controller);
      }
      if (epoch !== transportEpoch.current || controller.signal.aborted) return;

      if (replace) {
        const sorted = [...data.messages].sort((a, b) => (Number(a.id) || 0) - (Number(b.id) || 0));
        idsRef.current = new Set(sorted.map((m) => m.id));
        setMessages(sorted);
      } else {
        append(data.messages);
      }
      cursorRef.current = Math.max(cursorRef.current, data.cursor);
    },
    [name, append]
  );

  const closeStream = React.useCallback((status: string) => {
    if (closedRef.current) return;
    closedRef.current = status;
    setClosedStatus(status);
    setConn("closed");
    esRef.current?.close();
    esRef.current = null;
    /* drain any tail lines the stream never delivered */
    if (!document.hidden) void fetchPage(cursorRef.current, false).catch(() => undefined);
  }, [fetchPage]);

  /* ---- initial load, SSE, fallback poll, visibility pause ---- */
  React.useEffect(() => {
    let disposed = false;
    let pollTimer: number | undefined;
    transportEpoch.current += 1;
    cursorRef.current = -1;
    idsRef.current = new Set();
    closedRef.current = null;
    setMessages([]);
    setConn("connecting");
    setClosedStatus(null);
    setPhase("loading");

    const openStream = () => {
      if (disposed || document.hidden || closedRef.current || esRef.current) return;
      let es: EventSource;
      try {
        es = new EventSource(
          streamURL(`/api/runs/${encodeURIComponent(name)}/stream?after=${cursorRef.current}`)
        );
      } catch {
        return;
      }
      esRef.current = es;
      es.onmessage = (ev: MessageEvent<string>) => {
        if (disposed || document.hidden) return;
        if (ev.lastEventId) {
          const idx = Number(ev.lastEventId);
          if (!Number.isNaN(idx)) cursorRef.current = Math.max(cursorRef.current, idx);
        }
        try {
          const message = eventToMessage(JSON.parse(ev.data) as RawEvent, ev.lastEventId || "");
          if (message) append([message]);
        } catch {
          /* malformed data line — index already advanced via lastEventId */
        }
      };
      es.addEventListener("run_closed", (ev) => {
        if (disposed) return;
        try {
          const data = JSON.parse((ev as MessageEvent<string>).data) as { status?: string };
          closeStream(String(data.status ?? "closed"));
        } catch {
          closeStream("closed");
        }
      });
      es.onerror = () => {
        if (disposed) return;
        /* EventSource auto-reconnects with Last-Event-ID; polling covers gaps */
        setConn((prev) => (closedRef.current || prev === "closed" ? prev : "reconnecting"));
      };
      setConn("stream");
    };

    const start = async () => {
      try {
        if (!document.hidden) await fetchPage(-1, true);
        if (disposed) return;
        setPhase("ready");
        requestAnimationFrame(() => {
          scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
        });
        openStream();
      } catch {
        if (!disposed) {
          setPhase("error");
        }
      }
      if (disposed) return;
      pollTimer = window.setInterval(() => {
        if (disposed || document.hidden || closedRef.current || requests.current.size > 0) return;
        void fetchPage(cursorRef.current, false)
          .then(() => {
            if (!disposed && !closedRef.current && !esRef.current) {
              setPhase("ready");
              openStream();
            }
          })
          .catch(() => undefined);
      }, POLL_INTERVAL_MS);
    };
    void start();

    const onVisibility = () => {
      if (document.hidden) {
        requests.current.forEach(controller => controller.abort());
        esRef.current?.close();
        esRef.current = null;
      } else {
        void fetchPage(cursorRef.current, false)
          .then(() => {
            if (!disposed) { setPhase("ready"); openStream(); }
          })
          .catch(() => undefined);
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      disposed = true;
      transportEpoch.current += 1;
      requests.current.forEach(controller => controller.abort());
      requests.current.clear();
      if (pollTimer !== undefined) window.clearInterval(pollTimer);
      document.removeEventListener("visibilitychange", onVisibility);
      esRef.current?.close();
      esRef.current = null;
    };
  }, [name, fetchPage, append, closeStream]);

  /* ---- engine already finished (page poll noticed before the stream) ---- */
  React.useEffect(() => {
    if (!run || closedRef.current) return;
    if (!run.live && TERMINAL_STATUSES.has((run.status || "").toLowerCase())) {
      closeStream(run.status);
    }
  }, [run, closeStream]);

  /* ---- agent filter ---- */
  const agents = React.useMemo(
    () => (run?.agents ? Object.entries(run.agents) : []),
    [run]
  );
  const filterEntry = agentFilter && run?.agents ? run.agents[agentFilter] : undefined;
  const hintMessages = React.useMemo<Message[]>(
    () =>
      hints.map((hint) => ({
        id: `hint:${hint.message_id}`,
        type: "operator_hint",
        timestamp: hint.created_at,
        agent_id: hint.agent_id,
        agent_name: hint.agent_name || run?.agents?.[hint.agent_id || "root"]?.name || hint.agent_id || "root",
        content: hint.message,
        hint_status: hint.status,
        args: { failure_reason: hint.failure_reason || "", failure_code: hint.failure_code || "" },
      })),
    [hints, run?.agents]
  );
  const filtered = React.useMemo(() => {
    const nameLower = (filterEntry?.name || "").toLowerCase();
    const transcript = messages.filter(message => message.type !== "operator_hint" && message.type !== "hints");
    const events = agentFilter
      ? transcript.filter(
          (m) =>
            m.agent_id === agentFilter ||
            (!m.agent_id && nameLower && agents.filter(([, agent]) => agent.name.toLowerCase() === nameLower).length === 1 && (m.agent_name || "").toLowerCase() === nameLower)
        )
      : transcript;
    /* Hints route to the target agent ("" means root) — under an agent
     * filter keep only that agent's hints; otherwise show them all. */
    const ownHints = agentFilter
      ? hintMessages.filter((m) => (m.agent_id || "root") === agentFilter)
      : hintMessages;
    if (ownHints.length === 0) return events;
    return [...events, ...ownHints].sort(messageOrder);
  }, [messages, hintMessages, agentFilter, filterEntry, agents]);
  const seenRef = React.useRef(0);
  const seenAgent = React.useRef(agentFilter);
  React.useEffect(() => {
    if ((filtered.length > seenRef.current || seenAgent.current !== agentFilter) && followRef.current) {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
    }
    seenRef.current = filtered.length;
    seenAgent.current = agentFilter;
  }, [filtered, agentFilter]);
  const visible = filtered.slice(-windowSize);
  const hidden = filtered.length - visible.length;
  const live = Boolean(run?.live) && !closedStatus;
  const statusLabel = (status: string) => {
    const normalized = (status || "unknown").toLowerCase();
    if (normalized === "closed") return t("conversation.connection.closed");
    const key = `status.${normalized}`;
    const translated = t(key);
    return translated === key ? status : translated;
  };
  const connectionLabel =
    conn === "stream"
      ? t("conversation.connection.stream")
      : conn === "reconnecting"
        ? t("conversation.connection.reconnecting")
        : conn === "closed"
          ? t("conversation.connection.closed")
          : t("conversation.connection.connecting");

  return (
    <section
      className={`${styles.conversation} panel panel-hairline flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-surface/92`}
      aria-label={c("任務對話", "Task conversation")}
      onKeyDown={event => { if (event.key === "Escape" && agentsOpen) closeAgents(); }}
    >
      {/* header bar */}
      <div className={styles.header}>
        <div className={styles.transcriptInfo}>
          <span className={styles.transcriptTitle}>
            {t("run.tab.conversation")}
            <span className={styles.eventCount}>{t("conversation.events", { n: filtered.length })}</span>
          </span>
          {live && !agentFilter && (
            <span
              aria-hidden
              className="ml-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-success"
            />
          )}
          <span
            className={`${styles.connection} font-mono text-[9px] ${
              conn === "stream"
                ? "text-accent"
                : conn === "reconnecting"
                  ? "text-warning"
                  : conn === "closed"
                    ? "text-fg-muted"
                    : "text-fg-muted"
            }`}
          >
            {conn === "stream" ? (
              <Radio className="h-3 w-3 animate-pulse" aria-hidden="true" />
            ) : conn === "reconnecting" ? (
              <RotateCcw className="h-3 w-3 animate-spin" aria-hidden="true" />
            ) : null}
            {connectionLabel}
            {conn === "closed" && closedStatus && closedStatus !== "closed"
              ? ` · ${statusLabel(closedStatus)}`
              : ""}
          </span>
        </div>
        <div className={styles.controls} role="group" aria-label={c("對話檢視設定", "Conversation view controls")}>
          <button
            ref={agentToggle}
            type="button"
            className={styles.toggle}
            aria-expanded={agentsOpen}
            onClick={() => setAgentsOpen(value => !value)}
          >
            <PanelLeft size={14} aria-hidden="true" />
            <span>{c("代理", "Agents")}</span>
            <span className={styles.agentCount}>{agents.length}</span>
          </button>
          <div className={styles.selectWrap}>
            <select
              className={styles.agentSelect}
              aria-label={t("conversation.filterAgent")}
              title={agentFilter ? `${filterEntry?.name || agentFilter} (${agentFilter})` : t("conversation.allAgents")}
              value={agentFilter}
              onChange={event => chooseAgent(event.target.value)}
            >
              <option value="">{t("conversation.allAgents")}</option>
              {agentFilter && !run?.agents?.[agentFilter] && <option value={agentFilter}>{agentFilter} · {c("狀態未知", "Unknown")}</option>}
              {agents.map(([id, entry]) => <option key={id} value={id}>{entry.name || id} ({id})</option>)}
            </select>
            <ChevronDown size={14} className={styles.selectChevron} aria-hidden="true" />
          </div>
          <label className={styles.follow} data-enabled={follow}>
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            {t("conversation.follow")}
          </label>
        </div>
      </div>

      {hintError && (
        <div className={styles.ledgerError} role="status">
          <span>{c("指令交付狀態暫時無法更新。", "Hint delivery status could not be refreshed.")}</span>
          <button type="button" onClick={() => setHintRevision(value => value + 1)}>{t("common.retry")}</button>
        </div>
      )}
      <div className={styles.workspace}>
        {agentsOpen && <ConversationAgents name={name} run={run} selected={agentFilter} onSelect={chooseAgent} onClose={closeAgents} />}
        <div className={styles.main}>
      {/* scroll area */}
      <div ref={scrollRef} aria-label={c("對話時間軸", "Conversation timeline")} className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-surface-deep/32 p-4">
        {phase === "loading" && messages.length === 0 ? (
          <div className="space-y-3 p-2">
            <div className="skeleton-line w-5/6" />
            <div className="skeleton-line w-2/3" />
            <div className="skeleton-line w-full" />
            <div className="skeleton-line w-4/6" />
            <p className="pt-2 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
              {t("common.loading")}
            </p>
          </div>
        ) : phase === "error" && messages.length === 0 ? (
          <div className="space-y-3 p-2">
            <div className="alert-error" role="alert">
              {t("conversation.unavailable")}
            </div>
            <button
              type="button"
              className="button-secondary button-compact"
              onClick={() => {
                const epoch = transportEpoch.current;
                setPhase("loading");
                void fetchPage(-1, true)
                  .then(() => { if (epoch === transportEpoch.current) setPhase("ready"); })
                  .catch(() => { if (epoch === transportEpoch.current) setPhase("error"); });
              }}
            >
              {t("common.retry")}
            </button>
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            title={
              agentFilter
                ? t("conversation.noAgentEvents")
                : live
                  ? t("conversation.waiting")
                  : t("conversation.empty")
            }
            hint={
              agentFilter
                ? t("conversation.noAgentEventsHint")
                : live
                  ? t("conversation.waitingHint")
                  : t("conversation.emptyHint")
            }
          />
        ) : (
          <>
            {hidden > 0 && (
              <div className="flex justify-center pb-1">
                <button
                  type="button"
                  className="button-secondary button-compact"
                  onClick={() => setWindowSize((w) => w + DOM_WINDOW)}
                >
                  <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
                  {t("conversation.loadEarlier", { n: hidden })}
                </button>
              </div>
            )}
            {visible.map((m) => (
              <MessageCard key={m.id} message={m} />
            ))}
          </>
        )}
      </div>

      {/* footer strip */}
      <div className="flex flex-shrink-0 flex-wrap items-center justify-between gap-2 border-t border-line/6 bg-surface/96 px-4 py-2 text-[11px] text-fg-muted">
        <span className="truncate">
          {closedStatus
            ? t("conversation.footer.closed", {
                status: statusLabel(closedStatus),
                n: filtered.length,
              })
            : live
              ? t("conversation.footer.live", {
                  n: filtered.length,
                  time:
                    clockOf(filtered.length ? filtered[filtered.length - 1].timestamp : "") || "—",
                })
              : t("conversation.footer.recorded", { n: filtered.length })}
        </span>
        {filtered.length > DOM_WINDOW && (
          <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-fg-faint">
            {t("conversation.footer.window", { n: visible.length })}
          </span>
        )}
      </div>
      <HintComposer name={name} run={run} selectedAgentId={agentFilter} focusVersion={focusVersion} />
        </div>
      </div>
    </section>
  );
}
