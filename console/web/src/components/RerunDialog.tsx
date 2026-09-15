"use client";

import { authFetch } from "@/lib/auth";


/* ============================================================================
   RerunDialog — create an independent task, optionally using a final report.

   Pre-fills from the run's instruction.md artifact and POSTs /api/scans with
   the run's own target / scan_type / scan_mode / crypto / socks5 / gsocket, so the new
   run preserves its scope plus whatever the operator changes. The backend
   validates and freezes the selected final report; report text is never sent
   back as an operator instruction. Live mode uses the active Settings profile.
   ========================================================================= */

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { CircleAlert, CircleCheck, RotateCw } from "lucide-react";
import { Spinner } from "@/components/ui";
import RunTargetList from "@/components/RunTargetList";
import ScanModeSelector from "@/components/scan/ScanModeSelector";
import { apiURL, getJSON, getSettings, postJSON, runTargetLabel, runTargets } from "@/lib/api";
import type { ModelProfile, RerunContext, RunDetail, ScanLaunchResult } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";
import { scanMode, type ScanMode } from "@/lib/scan-mode";

const INSTRUCTION_SOFT_LIMIT = 4000;

const CONTEXT_REASON_KEYS: Record<string, string> = {
  task_active: "rerun.report.active",
  report_not_final: "rerun.report.notFinal",
  report_missing: "rerun.report.missing",
  report_empty: "rerun.report.empty",
  report_unreadable: "rerun.report.unreadable",
  report_changing: "rerun.report.changing",
  report_changed: "rerun.report.changed",
  context_budget_exceeded: "rerun.report.inputBudget",
  report_too_large: "rerun.report.tooLarge",
  invalid_continuation: "rerun.report.invalid",
};

function parseLaunchError(e: unknown, t: (key: string) => string): string {
  const raw = e instanceof Error ? e.message : String(e);
  try {
    const parsed = JSON.parse(raw.replace(/^\d{3}:\s*/, "")) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
    if (parsed.detail && typeof parsed.detail === "object") {
      const detail = parsed.detail as { error_code?: unknown; code?: unknown; message?: unknown };
      const code = detail.error_code ?? detail.code;
      if (typeof code === "string" && CONTEXT_REASON_KEYS[code]) {
        return t(CONTEXT_REASON_KEYS[code]);
      }
      if (typeof detail.message === "string") return detail.message;
    }
  } catch {
    /* not JSON — return raw */
  }
  return raw;
}

export default function RerunDialog({
  run,
  onCancel,
  onLaunched,
}: {
  run: RunDetail;
  onCancel: () => void;
  onLaunched: (result: ScanLaunchResult) => void;
}) {
  const { t, locale } = useI18n();
  const [instruction, setInstruction] = React.useState("");
  const [mode, setMode] = React.useState<"new" | "continue">("new");
  const [additionalInstruction, setAdditionalInstruction] = React.useState("");
  const [reportContext, setReportContext] = React.useState<RerunContext | null>(null);
  const [contextLoading, setContextLoading] = React.useState(true);
  const [contextFailed, setContextFailed] = React.useState(false);
  const [contextRevision, setContextRevision] = React.useState(0);
  const [previewOpen, setPreviewOpen] = React.useState(false);
  const [depth, setDepth] = React.useState<ScanMode>(() => scanMode(run.scan_mode));
  const [instructionLoaded, setInstructionLoaded] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [profile, setProfile] = React.useState<ModelProfile | null>(null);
  const cancelRef = React.useRef<HTMLButtonElement | null>(null);

  /* the active model profile backs live reruns */
  React.useEffect(() => {
    let disposed = false;
    getSettings()
      .then((page) => {
        if (disposed) return;
        setProfile(
          page.profiles.find((p) => p.id === page.active_profile_id) ??
            page.profiles[0] ??
            null
        );
      })
      .catch(() => {
        if (!disposed) setProfile(null);
      });
    return () => {
      disposed = true;
    };
  }, []);

  /* pre-fill from the run's instruction.md artifact */
  React.useEffect(() => {
    let disposed = false;
    authFetch(
      apiURL(`/api/runs/${encodeURIComponent(run.name)}/artifacts/instruction.md`)
    )
      .then(async (res) => (res.ok ? res.text() : ""))
      .then((text) => {
        if (disposed) return;
        setInstruction(text && text.trim() !== "# (no instruction)" ? text : "");
        setInstructionLoaded(true);
      })
      .catch(() => {
        if (!disposed) setInstructionLoaded(true);
      });
    return () => {
      disposed = true;
    };
  }, [run.name]);

  React.useEffect(() => {
    let disposed = false;
    setContextLoading(true);
    setContextFailed(false);
    getJSON<RerunContext>(`/api/runs/${encodeURIComponent(run.name)}/rerun-context`)
      .then((context) => {
        if (!disposed) setReportContext(context);
      })
      .catch(() => {
        if (!disposed) {
          setReportContext(null);
          setContextFailed(true);
        }
      })
      .finally(() => {
        if (!disposed) setContextLoading(false);
      });
    return () => { disposed = true; };
  }, [run.name, contextRevision]);

  const profileModel = profile
    ? (run.scan_type === "internal"
        ? profile.model_internal || profile.model_web
        : profile.model_web || profile.model_internal)
    : "";
  const liveBlocked = !profile;
  const targets = runTargets(run);
  const canContinue = !contextLoading && Boolean(
    reportContext?.can_continue && reportContext.report_sha256 &&
    reportContext.source_run === run.name && reportContext.markdown?.trim()
  );
  const continuationBlocked = mode === "continue" && !canContinue;
  const contextMessage = contextLoading
    ? t("rerun.report.loading")
    : contextFailed
      ? t("rerun.report.loadFailed")
      : !canContinue
        ? t(CONTEXT_REASON_KEYS[reportContext?.reason ?? ""] ?? "rerun.report.unavailable")
        : t("rerun.report.available");

  const relaunch = async () => {
    if (busy || liveBlocked || !instructionLoaded || targets.length === 0 || continuationBlocked) return;
    setBusy(true);
    setError("");
    try {
      const res = await postJSON<ScanLaunchResult>("/api/scans", {
        target: run.target,
        ...(targets.length > 1 ? { targets } : {}),
        scan_type: run.scan_type || "web",
        scan_mode: depth,
        crypto: Boolean(run.crypto),
        socks5: run.socks5 || "",
        gsocket: run.gsocket || "",
        instruction,
        rerun_mode: mode,
        source_run: run.name,
        ...(mode === "continue" ? {
          source_report_sha256: reportContext?.report_sha256,
          additional_instruction: additionalInstruction,
        } : {}),
        profile_id: profile?.id ?? "",
        // Preserve project ownership so reruns remain visible in the same
        // workspace and are checked against its current scope server-side.
        project_id: run.project_id || "",
      });
      if ("batch_id" in res) {
        if (res.kind !== "batch" || !res.batch_id) throw new Error(t("rerun.batchMissing"));
      } else if (!res.run_name) {
        throw new Error(t("scan.launch.missingRunName"));
      }
      onLaunched(res);
    } catch (e) {
      setError(parseLaunchError(e, t));
      setBusy(false);
    }
  };

  const target = runTargetLabel(run);

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open && !busy) onCancel(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[80] bg-black/60" />
        <Dialog.Content
          className="panel max-w-2xl"
          style={{
            position: "fixed",
            left: "50%",
            top: "50%",
            transform: "translate(-50%, -50%)",
            zIndex: 81,
            width: "calc(100% - 2rem)",
            maxHeight: "calc(100dvh - 2rem)",
            overflowY: "auto",
            background: "var(--bg-panel-solid)",
          }}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelRef.current?.focus({ preventScroll: true });
          }}
        >
          <div className="border-b border-line/6 px-5 py-4">
            <div className="eyebrow">&gt; {t("rerun.path")}</div>
            <Dialog.Title
              className="mt-2 font-mono text-base font-bold uppercase tracking-[0.06em]"
              style={{ color: "var(--text-heading)" }}
            >
              {t("rerun.title")}
            </Dialog.Title>
          </div>

          <div className="space-y-4 px-5 py-5">
            <Dialog.Description className="text-sm leading-relaxed text-fg-2">
              {t("rerun.hint")}
            </Dialog.Description>

            <fieldset disabled={busy} className="min-w-0 space-y-2">
              <legend className="field-label mb-2">{t("rerun.mode")}</legend>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {(["new", "continue"] as const).map((option) => {
                  const disabled = option === "continue" && !canContinue;
                  return (
                    <label key={option} className={`flex min-w-0 items-start gap-3 border p-3 ${
                      mode === option ? "border-accent/60 bg-accent/5" : "border-line/10 bg-surface-deep/40"
                    } ${disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"}`}>
                      <input
                        type="radio"
                        name="rerun-mode"
                        value={option}
                        aria-label={t(`rerun.mode.${option}`)}
                        checked={mode === option}
                        onChange={() => { setMode(option); setError(""); }}
                        disabled={disabled}
                        aria-describedby={`rerun-${option}-hint${option === "continue" ? " rerun-report-status" : ""}`}
                        className="mt-0.5 shrink-0 accent-[var(--accent)]"
                      />
                      <span className="min-w-0">
                        <span className="block text-sm font-semibold text-fg">{t(`rerun.mode.${option}`)}</span>
                        <span id={`rerun-${option}-hint`} className="mt-1 block text-xs leading-relaxed text-fg-muted">
                          {t(`rerun.mode.${option}.hint`)}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p id="rerun-report-status" role="status" className="text-xs leading-relaxed text-fg-muted">
                  {contextMessage}
                </p>
                <button type="button" className="text-xs text-accent hover:underline disabled:opacity-50"
                  disabled={contextLoading || busy} onClick={() => { setContextRevision((value) => value + 1); setError(""); }}>
                  {t("rerun.report.refresh")}
                </button>
              </div>
            </fieldset>

            {mode === "continue" && canContinue && reportContext && (
              <div className="min-w-0 space-y-3 border border-accent/20 bg-accent/5 p-4">
                <div className="space-y-1 text-xs text-fg-2">
                  <p className="break-words font-medium">{t("rerun.source", { name: reportContext.source_run })}</p>
                  {reportContext.report_generated_at && (
                    <p>{t("rerun.report.generatedAt", { time: fmtTime(reportContext.report_generated_at, locale) })}</p>
                  )}
                  <p className="text-fg-muted">{t("rerun.report.referenceHint")}</p>
                </div>
                <details open={previewOpen} onToggle={(event) => setPreviewOpen(event.currentTarget.open)}>
                  <summary className="cursor-pointer text-xs font-semibold text-accent">{t("rerun.report.preview")}</summary>
                  {previewOpen && (
                    <pre className="mt-3 max-h-72 overflow-y-auto whitespace-pre-wrap break-words border border-line/10 bg-surface-deep/60 p-3 font-mono text-xs leading-relaxed text-fg-2">
                      {reportContext.markdown}
                    </pre>
                  )}
                </details>
              </div>
            )}

            {/* run context */}
            <div className="divide-y divide-line/6 border border-line/6 bg-surface-deep/40 px-4 py-1">
              <div className="flex items-center justify-between gap-4 py-2.5 text-xs">
                <span className="shrink-0 text-fg-muted">{t("scan.target")}</span>
                <span className="truncate font-mono text-fg">{target}</span>
              </div>
              <div className="flex items-center justify-between gap-4 py-2.5 text-xs">
                <span className="shrink-0 text-fg-muted">{t("scan.mode")}</span>
                <span className="truncate font-mono text-fg">
                  {t(
                    run.scan_type === "internal"
                      ? "scan.mode.internal"
                      : "scan.mode.web"
                  )}
                  {run.crypto ? ` · ${t("scan.crypto")}` : ""}
                </span>
              </div>
              <div className="flex items-center justify-between gap-4 py-2.5 text-xs">
                <span className="shrink-0 text-fg-muted">{t("scan.route")}</span>
                <span className="truncate font-mono text-fg">
                  {run.socks5
                    ? `socks5 · ${run.socks5.replace(/^socks5h?:\/\//i, "")}`
                    : run.gsocket
                      ? t("scan.transport.gsocket")
                      : t("scan.transport.direct")}
                </span>
              </div>
            </div>

            <RunTargetList targets={targets} />
            <ScanModeSelector id="rerun-depth" value={depth} onChange={setDepth} disabled={busy} />

            {/* instruction */}
            <div>
              <label htmlFor="rerun-instruction" className="field-label">
                {t("scan.instruction")}
              </label>
              <textarea
                id="rerun-instruction"
                className="textarea-shell mt-2 min-h-40 font-mono text-[0.82rem] leading-relaxed"
                placeholder={
                  instructionLoaded
                    ? t("scan.instruction.placeholder")
                    : t("rerun.instruction.loading")
                }
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                spellCheck={false}
                disabled={!instructionLoaded || busy}
              />
              <div className="mt-1.5 flex items-center justify-between gap-3">
                <span className="text-[10px] uppercase tracking-[0.14em] text-fg-muted">
                  {t("rerun.instruction.hint")}
                </span>
                <span
                  className={`font-mono text-[10px] uppercase tracking-[0.14em] ${
                    instruction.length > INSTRUCTION_SOFT_LIMIT ? "text-warning" : "text-fg-muted"
                  }`}
                >
                  {t("scan.instruction.chars", {
                    n: instruction.length.toLocaleString(),
                  })}
                </span>
              </div>
            </div>

            {mode === "continue" && (
              <div>
                <label htmlFor="rerun-additional-instruction" className="field-label">{t("rerun.additional")}</label>
                <textarea id="rerun-additional-instruction"
                  className="textarea-shell mt-2 min-h-28 font-mono text-[0.82rem] leading-relaxed"
                  placeholder={t("rerun.additional.placeholder")}
                  value={additionalInstruction} onChange={(event) => setAdditionalInstruction(event.target.value)}
                  spellCheck={false} disabled={busy} />
                <p className="mt-1.5 text-xs leading-relaxed text-fg-muted">{t("rerun.additional.hint")}</p>
              </div>
            )}

            {/* live-mode route notice */}
            <div className="flex items-center justify-between gap-4 border border-line/6 bg-surface-deep/40 px-4 py-3.5">
              <div className="min-w-0">
                <div
                  className="font-mono text-sm font-bold uppercase tracking-[0.1em]"
                  style={{ color: "var(--text-heading)" }}
                >
                  {t("scan.liveMode")}
                </div>
                <p className="mt-1 text-xs leading-relaxed text-fg-muted">
                  {t("rerun.liveNotice", {
                    model: profileModel || t("scan.live.modelFallback"),
                    profile: profile?.name ?? t("scan.live.profileFallback"),
                  })}
                </p>
              </div>
            </div>

            {liveBlocked && (
              <div className="alert-warning">
                {t("rerun.profileRequired")}
              </div>
            )}
            {targets.length === 0 && (
              <div className="alert-warning" role="alert">
                {locale === "zh-CN"
                  ? "无法读取此任务的完整目标范围，请在新增任务中重新填写目标。"
                  : "This task's complete scope could not be read. Enter the targets in a new task."}
              </div>
            )}
            {error && (
              <div className="alert-error" role="alert">
                <div className="flex items-start gap-2">
                  <CircleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" strokeWidth={2} />
                  <div className="min-w-0">
                    <span className="font-mono text-[10px] font-bold uppercase tracking-[0.16em]">
                      {t("rerun.failed")}
                    </span>
                    <p className="mt-1 break-words">{error}</p>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line/6 px-5 py-4">
            <span className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-muted">
              {busy ? (
                <>
                  <Spinner className="h-3 w-3" />
                  {t("scan.launching")}
                </>
              ) : liveBlocked ? (
                <>
                  <CircleAlert className="h-3.5 w-3.5 text-warning" strokeWidth={2} />
                  {t("rerun.routeIncomplete")}
                </>
              ) : (
                <>
                  <CircleCheck className="h-3.5 w-3.5 text-success" strokeWidth={2} />
                  {t("rerun.liveReady")}
                </>
              )}
            </span>
            <div className="flex items-center gap-2">
              <button
                ref={cancelRef}
                type="button"
                className="button-secondary button-compact"
                onClick={onCancel}
                disabled={busy}
              >
                {t("common.cancel")}
              </button>
              <button
                type="button"
                className="button-primary button-compact"
                onClick={() => void relaunch()}
                disabled={busy || liveBlocked || !instructionLoaded || targets.length === 0 || continuationBlocked}
                aria-busy={busy}
              >
                {busy ? (
                  <Spinner className="h-3.5 w-3.5" />
                ) : (
                  <RotateCw className="h-3.5 w-3.5" />
                )}
                {t(busy ? "scan.launching" : mode === "continue" ? "rerun.launchContinue" : "rerun.launch")}
              </button>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
