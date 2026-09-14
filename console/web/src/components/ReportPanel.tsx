"use client";

/* ============================================================================
   ReportPanel — renders the run's penetration_test_report.md.

   Markdown via react-markdown + remark-gfm inside .prose-report. While the
   run is live or finalizing the panel refreshes any saved report so the
   synthesized report can replace the initial saved version.
   ========================================================================= */

import * as React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download, FileText, RefreshCw } from "lucide-react";
import { EmptyState } from "@/components/ui";
import MermaidDiagram from "@/components/MermaidDiagram";
import { apiURL, getJSON } from "@/lib/api";
import type { ReportPage, RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const REPORT_POLL_MS = 5000;
const FINALIZATION_PHASE_KEYS: Record<string, string> = {
  agents: "report.finalizing.agents",
  sandbox_quiesce: "report.finalizing.sandboxQuiesce",
  evidence: "report.finalizing.evidence",
  report: "report.finalizing.report",
  sandbox_delete: "report.finalizing.sandboxDelete",
  gateway: "report.finalizing.gateway",
};

type LoadOutcome = "ready" | "missing" | "error" | "stale";

/* A fenced ```mermaid block arrives as <pre><code class="language-mermaid">.
 * Intercept it at the <pre> level (inline code has no pre) and hand the chart
 * to the diagram renderer; every other block falls through unchanged. */
function mermaidSource(children: React.ReactNode): string | null {
  const child = Array.isArray(children) ? children[0] : children;
  if (!React.isValidElement(child)) return null;
  const props = child.props as { className?: unknown; children?: unknown };
  const className = typeof props.className === "string" ? props.className : "";
  if (!className.split(/\s+/).includes("language-mermaid")) return null;
  const raw = props.children;
  const text = Array.isArray(raw) ? raw.join("") : String(raw ?? "");
  const chart = text.trim();
  return chart || null;
}

export default function ReportPanel({ name, run }: { name: string; run: RunDetail | null }) {
  const { t, locale } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "missing" | "error">("loading");
  const [error, setError] = React.useState("");
  const [markdown, setMarkdown] = React.useState("");
  const [reportSynthesized, setReportSynthesized] = React.useState<boolean | undefined>(undefined);
  const live = Boolean(run?.live);
  const reporting = (run?.status || "").toLowerCase() === "reporting";
  const finalizing = run?.cleanup?.status === "in_progress";
  const pending = live || reporting || finalizing;
  const requestIdRef = React.useRef(0);
  const hasReportRef = React.useRef(false);
  // Task polling rerenders this panel. Stable renderer identities preserve the
  // table/code DOM, keyboard focus and horizontal reading position.
  const markdownComponents = React.useMemo<Components>(() => ({
    pre: ({ children }) => {
      const chart = mermaidSource(children);
      return chart ? <MermaidDiagram chart={chart} /> : (
        <pre tabIndex={0} aria-label={locale === "en" ? "Code block" : "程式碼區塊"}>{children}</pre>
      );
    },
    table: ({ children }) => (
      <div className="report-table-scroll" role="region" tabIndex={0}
        aria-label={locale === "en" ? "Report table, scroll horizontally when needed" : "報告表格，可左右捲動"}>
        <table>{children}</table>
      </div>
    ),
  }), [locale]);

  const load = React.useCallback(async (): Promise<LoadOutcome> => {
    const requestId = ++requestIdRef.current;
    try {
      const page = await getJSON<ReportPage>(`/api/runs/${encodeURIComponent(name)}/report`);
      if (requestId !== requestIdRef.current) return "stale";
      hasReportRef.current = true;
      setMarkdown(page.markdown ?? "");
      setReportSynthesized(
        typeof page.report_synthesized === "boolean" ? page.report_synthesized : undefined,
      );
      setError("");
      setPhase("ready");
      return "ready";
    } catch (e) {
      if (requestId !== requestIdRef.current) return "stale";
      const message = e instanceof Error ? e.message : String(e);
      setError(message);
      if (message.startsWith("404")) {
        if (!hasReportRef.current) setPhase("missing");
        return "missing";
      }
      if (!hasReportRef.current) setPhase("error");
      return "error";
    }
  }, [name]);

  React.useEffect(() => {
    // Changing runs clears the document; lifecycle transitions and temporary
    // refresh failures must keep the current run's saved report readable.
    hasReportRef.current = false;
    setMarkdown("");
    setReportSynthesized(undefined);
    setError("");
    setPhase("loading");
  }, [name]);

  // Refresh immediately when the process exits, even if cleanup keeps the
  // run pending, so the final file is not missed between polling intervals.
  React.useEffect(() => {
    let disposed = false;
    let retryTimer: number | undefined;

    requestIdRef.current += 1;

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
      if (pending || outcome === "error") {
        scheduleRetry();
      }
    };

    void attempt();

    return () => {
      disposed = true;
      requestIdRef.current += 1;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [load, live, pending, run?.report_synthesized]);

  const finalizationNotice = finalizing ? (
    <p className="text-sm text-fg-muted" role="status">
      {t(FINALIZATION_PHASE_KEYS[run?.cleanup?.phase ?? ""] ?? "report.finalizing")}
    </p>
  ) : null;

  if (phase === "loading") {
    return (
      <div className="space-y-3 p-4">
        {finalizationNotice}
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
      <div className="space-y-3 p-4">
        {finalizationNotice}
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
        {finalizationNotice}
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
      {finalizationNotice}
      {reportSynthesized === false && (
        <div className="space-y-1 text-sm text-fg-muted" role="status">
          <p className="font-semibold">{t("report.draft.title")}</p>
          <p>{t(pending ? "report.draft.pending" : "report.draft.unfinished")}</p>
        </div>
      )}
      {reportSynthesized === true && (
        <p className="text-sm text-fg-muted">{t("report.modelFinal")}</p>
      )}
      {error && <p className="text-sm text-fg-muted" role="status">{t("report.refreshError")}</p>}
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
      <div className="prose-report report-document">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={markdownComponents}
        >
          {markdown}
        </ReactMarkdown>
      </div>
    </div>
  );
}
