"use client";

/* ============================================================================
   AgentsPanel — the agent tree of the run cockpit.

   Rows build a parent_id tree, each with a StatusPill, mono name + id, the
   current task truncated, a per-agent message count (lightweight
   /conversation tally), and a per-agent "Send hint" shortcut that hops to
   the Hints tab with the agent preselected.
   ========================================================================= */

import * as React from "react";
import { CornerDownRight, FileText, MessageSquarePlus, RefreshCw } from "lucide-react";
import { EmptyState, StatusPill } from "@/components/ui";
import { apiURL, getJSON } from "@/lib/api";
import type { ConversationPage, RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const AGENT_POLL_MS = 10000;

function AgentSkeleton() {
  const { t } = useI18n();
  return (
    <div className="space-y-2 p-4">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-3"
        >
          <div className="flex items-center gap-2.5">
            <div className="skeleton-line h-4 w-16" />
            <div className="skeleton-line w-28" />
            <div className="skeleton-line ml-auto w-14" />
          </div>
          <div className="skeleton-line mt-2.5 w-3/4" />
        </div>
      ))}
      <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
        {t("common.loading")}
      </p>
    </div>
  );
}

export default function AgentsPanel({
  name,
  run,
  onSendHint,
}: {
  name: string;
  run: RunDetail | null;
  /** switch to the Hints tab with this agent preselected */
  onSendHint: (agentId: string, agentName: string) => void;
}) {
  const { t } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = React.useState("");
  const [counts, setCounts] = React.useState<Record<string, number>>({});
  const live = Boolean(run?.live);

  const refreshCounts = React.useCallback(async () => {
    try {
      const data = await getJSON<ConversationPage>(
        `/api/runs/${encodeURIComponent(name)}/conversation?after=-1`
      );
      const tally: Record<string, number> = {};
      for (const m of data.messages) {
        const key = m.agent_id || `name:${(m.agent_name || "").toLowerCase()}`;
        if (!key) continue;
        tally[key] = (tally[key] ?? 0) + 1;
      }
      setCounts(tally);
      setPhase("ready");
    } catch (e) {
      setPhase((prev) => (prev === "ready" ? prev : "error"));
      setError(String(e));
    }
  }, [name]);

  React.useEffect(() => {
    void refreshCounts();
    if (!live) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshCounts();
    }, AGENT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [refreshCounts, live]);

  const entries = React.useMemo(
    () => (run?.agents ? Object.entries(run.agents) : []),
    [run]
  );

  const messageCount = React.useCallback(
    (id: string, entryName: string) => {
      const byId = counts[id];
      if (byId !== undefined) return byId;
      return counts[`name:${(entryName || "").toLowerCase()}`] ?? 0;
    },
    [counts]
  );

  if (phase === "loading") return <AgentSkeleton />;

  if (phase === "error") {
    return (
      <div className="space-y-3 p-4">
        <div className="alert-error" role="alert">
          {t("agents.error", { error })}
        </div>
        <button
          type="button"
          className="button-secondary button-compact"
          onClick={() => {
            setPhase("loading");
            void refreshCounts();
          }}
        >
          <RefreshCw className="h-3.5 w-3.5" />
          {t("common.retry")}
        </button>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="p-4">
        <EmptyState
          title={t(live ? "agents.waiting" : "agents.empty")}
          hint={
            t(live ? "agents.waiting.hint" : "agents.empty.hint")
          }
        />
      </div>
    );
  }

  const byParent = new Map<string, string[]>();
  const known = new Set(entries.map(([id]) => id));
  for (const [id, entry] of entries) {
    const parent = entry.parent_id && known.has(entry.parent_id) ? entry.parent_id : "";
    const list = byParent.get(parent) || [];
    list.push(id);
    byParent.set(parent, list);
  }

  const renderRow = (id: string, depth: number): React.ReactNode => {
    const entry = run?.agents?.[id];
    if (!entry) return null;
    const count = messageCount(id, entry.name);
    const status = entry.status || "unknown";
    const statusKey = `status.${status.toLowerCase()}`;
    const translatedStatus = t(statusKey);
    return (
      <div key={id} className={depth > 0 ? "ml-4 border-l border-line/6 pl-3" : ""}>
        <div className="group rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-3 transition-colors hover:border-line/12 hover:bg-raised/4">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
            {depth > 0 && <CornerDownRight className="h-3.5 w-3.5 shrink-0 text-fg-faint" />}
            <StatusPill
              status={status}
              label={translatedStatus === statusKey ? status : translatedStatus}
            />
            <span className="min-w-0 truncate font-mono text-sm font-semibold text-fg">
              {entry.name || id}
            </span>
            <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-faint">
              {id}
            </span>
            <span className="ml-auto flex shrink-0 items-center gap-2">
              {count > 0 && (
                <span className="micro-label text-[9px]">
                  {t("agents.messages", { n: count })}
                </span>
              )}
              <button
                type="button"
                className="button-ghost min-h-8 px-2 text-[10px] uppercase tracking-[0.12em] group-hover:text-accent"
                onClick={() => onSendHint(id, entry.name || id)}
                disabled={!live}
                title={
                  live
                    ? t("agents.hint.title", { name: entry.name || id })
                    : t("agents.hint.unavailable")
                }
              >
                <MessageSquarePlus className="h-3.5 w-3.5" />
                {t("agents.hint")}
              </button>
              <a
                href={apiURL(
                  `/api/runs/${encodeURIComponent(name)}/prompts/${encodeURIComponent(`prompt_${(entry.name || id).replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 40)}.md`)}`
                )}
                target="_blank"
                rel="noreferrer"
                className="button-ghost min-h-8 px-2 text-[10px] uppercase tracking-[0.12em]"
                title={t("agents.prompt.title")}
              >
                <FileText className="h-3.5 w-3.5" />
              </a>
            </span>
          </div>
          {entry.task && (
            <p className="mt-1.5 line-clamp-2 break-words text-xs leading-relaxed text-fg-muted">
              {entry.task}
            </p>
          )}
        </div>
        {(byParent.get(id) || []).map((child) => renderRow(child, depth + 1))}
      </div>
    );
  };

  return (
    <div className="space-y-2.5 p-4">
      <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
        {t("agents.summary", { n: entries.length })}
      </p>
      {(byParent.get("") || []).map((id) => renderRow(id, 0))}
    </div>
  );
}
