"use client";

/* ============================================================================
   ReportPanel — renders the run's penetration_test_report.md.

   Markdown via react-markdown + remark-gfm inside .prose-report. While the
   run is live the panel polls for the report and stops once it exists.
   ========================================================================= */

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download, FileText, RefreshCw } from "lucide-react";
import { EmptyState } from "@/components/ui";
import { apiURL, getJSON } from "@/lib/api";
import type { ReportPage, RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const REPORT_POLL_MS = 5000;

type LoadOutcome = "ready" | "missing" | "error" | "stale";

export default function ReportPanel({ name, run }: { name: string; run: RunDetail | null }) {
  const { t } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "missing" | "error">("loading");
  const [error, setError] = React.useState("");
  const [markdown, setMarkdown] = React.useState("");
  const live = Boolean(run?.live);
  const reporting = (run?.status || "").toLowerCase() === "reporting";
  const pending = live || reporting;
  const requestIdRef = React.useRef(0);

  const load = React.useCallback(async (): Promise<LoadOutcome> => {
    const requestId = ++requestIdRef.current;
    try {
      const page = await getJSON<ReportPage>(`/api/runs/${encodeURIComponent(name)}/report`);
      if (requestId !== requestIdRef.current) return "stale";
      setMarkdown(page.markdown ?? "");
      setError("");
      setPhase("ready");
      return "ready";
    } catch (e) {
      if (requestId !== requestIdRef.current) return "stale";
      const message = e instanceof Error ? e.message : String(e);
      if (message.startsWith("404")) {
        setPhase("missing");
        return "missing";
      }
      setError(message);
      setPhase("error");
      return "error";
    }
  }, [name]);

  React.useEffect(() => {
    /* While the run is live the report cannot exist yet (it is written at
     * scan end) — skip fetching entirely so visiting the tab early does not
     * hammer the API with guaranteed 404s. The effect re-runs the moment
     * the cockpit poll flips `live` off. */
    let disposed = false;
    let retryTimer: number | undefined;

    requestIdRef.current += 1;
    setMarkdown("");
    setError("");
    setPhase(live ? "missing" : "loading");

    const scheduleRetry = () => {
      retryTimer = window.setTimeout(() => {
        if (disposed) return;
        if (document.hidden) {
          scheduleRetry();
          return;
        }
        void attempt();
      }, REPORT_POLL_MS);
    };

    const attempt = async () => {
      const outcome = await load();
      if (disposed) return;
      if (outcome === "error" || (outcome === "missing" && reporting)) {
        scheduleRetry();
      }
    };

    if (!live) void attempt();

    return () => {
      disposed = true;
      requestIdRef.current += 1;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [load, live, name, reporting]);

  if (phase === "loading") {
    return (
      <div className="space-y-3 p-4">
        <div className="skeleton-line w-2/5 h-5" />
        <div className="skeleton-line w-full" />
        <div className="skeleton-line w-5/6" />
        <div className="skeleton-line w-4/6" />
        <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
          {t("common.loading")}
        </p>
      </div>
    );
  }

  if (phase === "missing") {
    return (
      <div className="p-4">
        <EmptyState
          title={t(pending ? "report.pending" : "report.empty")}
          hint={
            t(pending ? "report.pending.hint" : "report.empty.hint")
          }
          action={
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
          }
        />
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="space-y-3 p-4">
        <div className="alert-error" role="alert">
          {t("report.error", { error })}
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
    );
  }

  const sizeKb = (new Blob([markdown]).size / 1024).toFixed(1);

  return (
    <div className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="micro-label">
          <FileText className="h-3.5 w-3.5" />
          penetration_test_report.md · {sizeKb} KB
        </span>
        <a
          className="button-secondary button-compact"
          href={apiURL(`/api/runs/${encodeURIComponent(name)}/artifacts/penetration_test_report.md`)}
          download={`${name}-penetration_test_report.md`}
        >
          <Download className="h-3.5 w-3.5" />
          {t("report.download")}
        </a>
      </div>
      <div className="prose-report">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
      </div>
    </div>
  );
}
