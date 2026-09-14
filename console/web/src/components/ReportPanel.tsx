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
import { Download, FileText, LoaderCircle, RefreshCw } from "lucide-react";
import { EmptyState } from "@/components/ui";
import MermaidDiagram from "@/components/MermaidDiagram";
import { apiURL, generateReport, getJSON, getReportGeneration } from "@/lib/api";
import type { ReportGeneration, ReportPage, RunDetail } from "@/lib/api";
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
const GENERATION_ERROR_KEYS: Record<string, string> = {
  interrupted: "report.generate.interrupted",
  timeout: "report.generate.timeout",
  timed_out: "report.generate.timeout",
  invalid_output: "report.generate.invalidOutput",
  unusable_output: "report.generate.invalidOutput",
  model_required: "report.generate.modelRequired",
  missing_model: "report.generate.modelRequired",
  model_unavailable: "report.generate.modelRequired",
  authentication: "report.generate.authentication",
  input_budget: "report.generate.inputBudget",
  context_limit: "report.generate.inputBudget",
  output_limit: "report.generate.outputLimit",
  rate_limit: "report.generate.rateLimit",
  upstream_policy: "report.generate.policy",
  model_refusal: "report.generate.refusal",
  incomplete_output: "report.generate.invalidOutput",
  empty_output: "report.generate.invalidOutput",
  model_error: "report.generate.failedGeneric",
  source_error: "report.generate.sourceError",
  storage_error: "report.generate.storageError",
  scan_active: "report.generate.active",
  cancelled: "report.generate.interrupted",
  disabled: "report.generate.disabled",
  dry_run: "report.generate.dryRun",
};
const REPORT_GENERATION_TERMINAL_STATUSES = new Set([
  "completed", "failed", "interrupted", "cancelled", "aborted", "stopped",
]);

function generationRequestErrorKey(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  try {
    const payload = JSON.parse(message.replace(/^\d+:\s*/, ""));
    const code = payload?.detail?.code;
    if (typeof code === "string" && Object.hasOwn(GENERATION_ERROR_KEYS, code)) {
      return GENERATION_ERROR_KEYS[code];
    }
  } catch { /* Only recognized error codes are presented to the user. */ }
  return message.startsWith("409") ? "report.generate.active" : "report.generate.unavailable";
}

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
  const [generation, setGeneration] = React.useState<ReportGeneration | null>(null);
  const [generationRequestError, setGenerationRequestError] = React.useState("");
  const [generationStatusError, setGenerationStatusError] = React.useState(false);
  const [submittingGeneration, setSubmittingGeneration] = React.useState(false);
  const [generationRefresh, setGenerationRefresh] = React.useState(0);
  const live = Boolean(run?.live);
  const runStatus = (run?.status || "").toLowerCase();
  const reporting = runStatus === "reporting";
  const finalizing = run?.cleanup?.status === "in_progress";
  const pending = live || reporting || finalizing;
  const generationBlocked = !run || pending || !REPORT_GENERATION_TERMINAL_STATUSES.has(runStatus);
  const generating = submittingGeneration || generation?.status === "running";
  const requestIdRef = React.useRef(0);
  const hasReportRef = React.useRef(false);
  const generationRequestIdRef = React.useRef(0);
  const generationActionIdRef = React.useRef(0);
  const generationSubmittingRef = React.useRef(false);
  const loadedGenerationRef = React.useRef("");
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
    generationActionIdRef.current += 1;
    generationSubmittingRef.current = false;
    loadedGenerationRef.current = "";
    setGeneration(null);
    setGenerationRequestError("");
    setGenerationStatusError(false);
    setSubmittingGeneration(false);
    return () => { generationActionIdRef.current += 1; };
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

  React.useEffect(() => {
    let disposed = false;
    let retryTimer: number | undefined;
    const requestId = ++generationRequestIdRef.current;
    const current = () => !disposed && requestId === generationRequestIdRef.current;
    const schedule = () => {
      retryTimer = window.setTimeout(() => { void poll(); }, REPORT_POLL_MS);
    };
    const poll = async () => {
      try {
        const page = await getReportGeneration(name);
        if (!current()) return;
        setGeneration(page.generation);
        setGenerationStatusError(false);
        if (page.generation.status === "completed") {
          const identity = page.generation.job_id || page.generation.completed_at || "completed";
          if (loadedGenerationRef.current !== identity) {
            // A regenerated final report still has report_synthesized=true.
            // Refresh by job identity, including after navigating back here.
            const outcome = await load();
            if (!current()) return;
            if (outcome === "ready") loadedGenerationRef.current = identity;
            else schedule();
          }
        } else if (page.generation.status === "running") {
          schedule();
        }
      } catch {
        if (!current()) return;
        setGenerationStatusError(true);
        schedule();
      }
    };
    void poll();
    return () => {
      disposed = true;
      generationRequestIdRef.current += 1;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
    };
  }, [name, load, pending, generationRefresh]);

  const startGeneration = async () => {
    if (generationBlocked || generating || generationSubmittingRef.current) return;
    generationSubmittingRef.current = true;
    generationRequestIdRef.current += 1;
    const actionId = generationActionIdRef.current;
    setSubmittingGeneration(true);
    setGenerationRequestError("");
    try {
      const page = await generateReport(name);
      if (actionId !== generationActionIdRef.current) return;
      setGeneration(page.generation);
      setGenerationStatusError(false);
    } catch (e) {
      if (actionId !== generationActionIdRef.current) return;
      // Provider errors can contain credentials. API status and the backend's
      // persisted safe failure fields are enough to explain what to do next.
      setGenerationRequestError(generationRequestErrorKey(e));
    } finally {
      if (actionId === generationActionIdRef.current) {
        generationSubmittingRef.current = false;
        setSubmittingGeneration(false);
        setGenerationRefresh((value) => value + 1);
      }
    }
  };

  const finalizationNotice = finalizing ? (
    <p className="text-sm text-fg-muted" role="status">
      {t(FINALIZATION_PHASE_KEYS[run?.cleanup?.phase ?? ""] ?? "report.finalizing")}
    </p>
  ) : null;

  const generationFailure = generation?.status !== "running" && generation?.status !== "completed"
    && (generation?.status === "failed" || generation?.error || generation?.code);
  const generationSkipped = generation?.status === "idle"
    && ["disabled", "dry_run"].includes(generation.code ?? "");
  const generationFailureText = generation?.code && Object.hasOwn(GENERATION_ERROR_KEYS, generation.code)
    ? t(GENERATION_ERROR_KEYS[generation.code])
    : generation?.error || t("report.generate.failedGeneric");
  const sizeKb = (new Blob([markdown]).size / 1024).toFixed(1);

  return (
    <div className="space-y-3 p-4">
      {finalizationNotice}
      {generating && (
        <p className="text-sm text-fg-muted" role="status">{t("report.generate.running")}</p>
      )}
      {!generating && generationRequestError && (
        <p className="alert-error" role="alert">{t(generationRequestError)}</p>
      )}
      {!generating && !generationRequestError && generationFailure && (
        <p className={generationSkipped ? "text-sm text-fg-muted" : "alert-error"}
          role={generationSkipped ? "status" : "alert"}>
          {generationSkipped ? generationFailureText : t("report.generate.failed", { error: generationFailureText })}
        </p>
      )}
      {!generating && !generationRequestError && generation?.status === "completed" && (
        <p className="text-sm text-fg-muted" role="status">{t("report.generate.completed")}</p>
      )}
      {generationStatusError && (
        <p className="text-sm text-fg-muted" role="status">{t("report.generate.statusError")}</p>
      )}
      {reportSynthesized === false && (
        <div className="space-y-1 text-sm text-fg-muted" role="status">
          <p className="font-semibold">{t("report.draft.title")}</p>
          <p>{t(pending || generating ? "report.draft.pending" : "report.draft.unfinished")}</p>
        </div>
      )}
      {reportSynthesized === true && (
        <p className="text-sm text-fg-muted">{t("report.modelFinal")}</p>
      )}
      {phase === "ready" && error && (
        <p className="text-sm text-fg-muted" role="status">{t("report.refreshError")}</p>
      )}
      <div className="flex flex-wrap items-center justify-between gap-2">
        {phase === "ready" ? (
          <span className="micro-label">
            <FileText className="h-3.5 w-3.5" />
            penetration_test_report.md · {sizeKb} KB
          </span>
        ) : <span />}
        <div className="flex flex-wrap items-center gap-2">
          {phase === "ready" && (
            <a
              className="button-secondary button-compact"
              href={apiURL(`/api/runs/${encodeURIComponent(name)}/artifacts/penetration_test_report.md`)}
              download={`${name}-penetration_test_report.md`}
            >
              <Download className="h-3.5 w-3.5" />
              {t("report.download")}
            </a>
          )}
          <button
            type="button"
            className="button-secondary button-compact"
            disabled={generationBlocked || generating}
            title={generationBlocked ? t("report.generate.pending") : undefined}
            onClick={() => { void startGeneration(); }}
          >
            {generating
              ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
              : <RefreshCw className="h-3.5 w-3.5" />}
            {t(generating ? "report.generating" : "report.generate")}
          </button>
        </div>
      </div>
      {phase === "loading" && (
        <div className="space-y-3">
          <div className="skeleton-line w-2/5 h-5" />
          <div className="skeleton-line w-full" />
          <div className="skeleton-line w-5/6" />
          <div className="skeleton-line w-4/6" />
          <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
            {t("common.loading")}
          </p>
        </div>
      )}
      {phase === "missing" && (
        <EmptyState
          title={t(pending || generating ? "report.pending" : "report.empty")}
          hint={t(generating ? "report.generate.running" : pending ? "report.pending.hint" : "report.empty.hint")}
          action={(
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
          )}
        />
      )}
      {phase === "error" && (
        <div className="space-y-3">
          <div className="alert-error" role="alert">{t("report.error", { error })}</div>
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
      )}
      {phase === "ready" && (
        <div className="prose-report report-document">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {markdown}
          </ReactMarkdown>
        </div>
      )}
    </div>
  );
}
