"use client";

/* ============================================================================
   HintsPanel — operator-hint composer + delivery ledger.

   Composer targets the selected agent (or auto-routes to root). The sent
   list polls while the run is live; each hint shows its delivery status
   chip (queued → delivered → acked), the engine's echo token, and relative
   time. Acked hints (the agent echoed [hint:token]) get gold highlighting.
   ========================================================================= */

import * as React from "react";
import { RefreshCw, Send } from "lucide-react";
import { EmptyState, Spinner } from "@/components/ui";
import { Select } from "@/components/Select";
import { getJSON, postJSON } from "@/lib/api";
import type { Hint, HintSendResult, HintsPage, RunDetail } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

const HINTS_POLL_MS = 3000;

const HINT_STATUS: Record<string, { tone: string; key: string }> = {
  queued: { tone: "accent", key: "queued" },
  delivered: { tone: "success", key: "delivered" },
  acked: { tone: "warning", key: "acked" },
};

function statusOf(status: string): { tone: string; key: string } {
  return HINT_STATUS[(status || "queued").toLowerCase()] ?? HINT_STATUS.queued;
}

export default function HintsPanel({
  name,
  run,
  target,
}: {
  name: string;
  run: RunDetail | null;
  /** agent preselected from elsewhere (AgentsPanel "Send hint") */
  target: { id: string; name: string } | null;
}) {
  const { t, locale } = useI18n();
  const [text, setText] = React.useState("");
  const [agentId, setAgentId] = React.useState(target?.id ?? "");
  const [sending, setSending] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const [noticeTone, setNoticeTone] = React.useState<"success" | "error">("success");
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = React.useState("");
  const [hints, setHints] = React.useState<Hint[]>([]);
  const live = Boolean(run?.live);

  /* adopt an externally preselected target */
  React.useEffect(() => {
    if (target) setAgentId(target.id);
  }, [target]);

  const load = React.useCallback(async () => {
    try {
      const page = await getJSON<HintsPage>(`/api/runs/${encodeURIComponent(name)}/hints`);
      setHints([...page.hints].reverse()); /* newest first */
      setPhase("ready");
    } catch (e) {
      setPhase((prev) => (prev === "ready" ? prev : "error"));
      setError(String(e));
    }
  }, [name]);

  React.useEffect(() => {
    void load();
    if (!live) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, HINTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, live]);

  const agents = React.useMemo(
    () => (run?.agents ? Object.entries(run.agents) : []),
    [run]
  );
  const selectedAgent = agents.find(([id]) => id === agentId);

  const flash = (tone: "success" | "error", message: string) => {
    setNoticeTone(tone);
    setNotice(message);
    window.setTimeout(() => setNotice(""), 6000);
  };

  const send = async () => {
    const message = text.trim();
    if (!message || sending) return;
    setSending(true);
    try {
      const result = await postJSON<HintSendResult>(
        `/api/runs/${encodeURIComponent(name)}/hints`,
        { message, agent_id: agentId }
      );
      setText("");
      flash("success", t("hints.queuedNotice", { token: result.hint_token }));
      await load();
    } catch (e) {
      flash("error", t("hints.failed", { error: String(e) }));
    } finally {
      setSending(false);
    }
  };

  const placeholder = !live
    ? t("hints.placeholder.closed")
    : selectedAgent
      ? t("hints.placeholder.agent", {
          name: selectedAgent[1].name || selectedAgent[0],
        })
      : t("hints.placeholder.root");

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* composer */}
      <div className="flex-shrink-0 space-y-2 border-b border-line/6 bg-surface/96 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-fg-muted">
          <span>
            {selectedAgent
              ? t("hints.target.agent", {
                  name: selectedAgent[1].name || selectedAgent[0],
                  id: selectedAgent[0],
                })
              : t("hints.target.root")}
          </span>
          <Select
            className="select-shell min-h-8 w-auto py-0.5 text-[11px]"
            value={agentId}
            onValueChange={setAgentId}
            aria-label={t("hints.target.label")}
            options={[
              { value: "", label: t("hints.root") },
              ...agents.map(([id, entry]) => ({ value: id, label: `${entry.name || id} (${id})` })),
            ]}
          />
        </div>
        <div className="flex items-end gap-2">
          <textarea
            className="min-h-[56px] flex-1 resize-none rounded-[1rem] border border-line/8 bg-surface-deep/78 px-3 py-2 text-sm text-fg outline-none transition focus:border-accent/40 disabled:opacity-50"
            placeholder={placeholder}
            value={text}
            disabled={!live || sending}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
            }}
            aria-label={t("hints.message.label")}
          />
          <button
            type="button"
            className="button-primary min-h-[56px] px-4"
            disabled={!live || sending || !text.trim()}
            onClick={() => void send()}
          >
            {sending ? <Spinner className="border-line/30 border-t-dark-900" /> : <Send className="h-4 w-4" />}
            {t(sending ? "hints.sending" : "hints.send")}
          </button>
        </div>
        {notice && (
          <div
            className={`rounded-[0.9rem] border px-3 py-2 text-xs leading-relaxed ${
              noticeTone === "success"
                ? "border-success/25 bg-success/10 text-success"
                : "border-danger/25 bg-danger/10 text-danger"
            }`}
            role="status"
          >
            {notice}
          </div>
        )}
        {!live && (
          <p className="text-[11px] text-fg-faint">
            {t("hints.closed")}
          </p>
        )}
      </div>

      {/* sent list */}
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {phase === "loading" ? (
          <div className="space-y-2">
            {[0, 1].map((i) => (
              <div key={i} className="rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-3">
                <div className="flex items-center gap-2">
                  <div className="skeleton-line h-3.5 w-16" />
                  <div className="skeleton-line w-24" />
                  <div className="skeleton-line ml-auto w-14" />
                </div>
                <div className="skeleton-line mt-2 w-3/4" />
              </div>
            ))}
            <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
              {t("common.loading")}
            </p>
          </div>
        ) : phase === "error" ? (
          <div className="space-y-3">
            <div className="alert-error" role="alert">
              {t("hints.error", { error })}
            </div>
            <button
              type="button"
              className="button-secondary button-compact"
              onClick={() => {
                setPhase("loading");
                void load();
              }}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              {t("common.retry")}
            </button>
          </div>
        ) : hints.length === 0 ? (
          <EmptyState
            title={t("hints.empty")}
            hint={
              t(live ? "hints.empty.liveHint" : "hints.empty.hint")
            }
          />
        ) : (
          <div className="space-y-2">
            <p className="pb-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
              {t("hints.summary", { n: hints.length })}
              {live ? ` · ${t("hints.polling", { seconds: 3 })}` : ""}
            </p>
            {hints.map((hint) => {
              const status = statusOf(hint.status);
              const acked = hint.status === "acked";
              return (
                <div
                  key={hint.message_id}
                  className={`rounded-[1rem] border px-3.5 py-3 transition-colors ${
                    acked
                      ? "border-warning/24 bg-warning/8 shadow-[0_0_14px_rgba(212,175,55,0.08)]"
                      : hint.status === "delivered"
                        ? "border-success/16 bg-success/5"
                        : "border-line/6 bg-surface/42"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`status-pill status-${status.tone} status-subtle`}>
                      {t(`hints.status.${status.key}`)}
                    </span>
                    <span className="truncate font-mono text-[10px] uppercase tracking-[0.14em] text-fg-muted">
                      {hint.agent_name || hint.agent_id || t("hints.root.short")}
                    </span>
                    <span className="ml-auto shrink-0 font-mono text-[10px] text-fg-faint">
                      {relTime(hint.created_at, locale)}
                    </span>
                  </div>
                  <p className="mt-1.5 whitespace-pre-wrap break-words text-sm leading-relaxed text-fg">
                    {hint.message}
                  </p>
                  <div className="mt-1.5 flex items-center gap-2">
                    {hint.hint_token && (
                      <span className="mono-chip py-0.5 text-[9px]">
                        {t("hints.token", { token: hint.hint_token })}
                      </span>
                    )}
                    {acked && (
                      <span className="font-mono text-[9px] uppercase tracking-[0.14em] text-[#f0d060]">
                        {t("hints.acked", { token: hint.hint_token })}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
