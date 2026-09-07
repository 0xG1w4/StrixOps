"use client";

/* ============================================================================
   RerunDialog — relaunch a finished run with an edited instruction.

   Pre-fills from the run's instruction.md artifact and POSTs /api/scans with
   the run's own target / scan_type / crypto / socks5 / gsocket, so the new
   run is a faithful child of the old one plus whatever the operator changes.
   Live mode routes through the server-side model profile store (Settings);
   the active profile is used unless the run recorded an explicit one.
   ========================================================================= */

import * as React from "react";
import { CircleAlert, CircleCheck, RotateCw } from "lucide-react";
import { Spinner } from "@/components/ui";
import { apiURL, getSettings, postJSON } from "@/lib/api";
import type { ModelProfile, RunDetail, ScanLaunched } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const INSTRUCTION_SOFT_LIMIT = 4000;

function parseLaunchError(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e);
  try {
    const parsed = JSON.parse(raw.replace(/^\d{3}:\s*/, "")) as { detail?: unknown };
    if (typeof parsed.detail === "string") return parsed.detail;
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
  onLaunched: (runName: string) => void;
}) {
  const { t } = useI18n();
  const [instruction, setInstruction] = React.useState("");
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
    fetch(
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
    cancelRef.current?.focus();
  }, []);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  const profileModel = profile
    ? (run.scan_type === "internal"
        ? profile.model_internal || profile.model_web
        : profile.model_web || profile.model_internal)
    : "";
  const liveBlocked = !profile;

  const relaunch = async () => {
    if (busy || liveBlocked) return;
    setBusy(true);
    setError("");
    try {
      const res = await postJSON<ScanLaunched>("/api/scans", {
        target: run.target,
        scan_type: run.scan_type || "web",
        crypto: Boolean(run.crypto),
        socks5: run.socks5 || "",
        gsocket: run.gsocket || "",
        instruction,
        profile_id: profile?.id ?? "",
        // Preserve project ownership so reruns remain visible in the same
        // workspace and are checked against its current scope server-side.
        project_id: run.project_id || "",
      });
      if (!res.run_name) throw new Error(t("scan.launch.missingRunName"));
      onLaunched(res.run_name);
    } catch (e) {
      setError(parseLaunchError(e));
      setBusy(false);
    }
  };

  const target = run.target || run.name;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 sm:p-6"
      onClick={onCancel}
    >
      <div
        className="panel w-full max-w-2xl animate-enter"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rerun-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-line/6 px-5 py-4">
          <div className="eyebrow">&gt; {t("rerun.path")}</div>
          <h3
            id="rerun-title"
            className="mt-2 font-mono text-base font-bold uppercase tracking-[0.06em]"
            style={{ color: "var(--text-heading)" }}
          >
            {t("rerun.title")}
          </h3>
        </div>

        <div className="space-y-4 px-5 py-5">
          <p className="text-sm leading-relaxed text-fg-2">
            {t("rerun.hint")}
          </p>

          {/* run context */}
          <div className="divide-y divide-line/6 border border-line/6 bg-surface-deep/40 px-4 py-1">
            <div className="flex items-center justify-between gap-4 py-2.5 text-xs">
              <span className="text-fg-muted">{t("scan.target")}</span>
              <span className="truncate font-mono text-fg">{target}</span>
            </div>
            <div className="flex items-center justify-between gap-4 py-2.5 text-xs">
              <span className="text-fg-muted">{t("scan.mode")}</span>
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
              <span className="text-fg-muted">{t("scan.route")}</span>
              <span className="truncate font-mono text-fg">
                {run.socks5
                  ? `socks5 · ${run.socks5.replace(/^socks5h?:\/\//i, "")}`
                  : run.gsocket
                    ? t("scan.transport.gsocket")
                    : t("scan.transport.direct")}
              </span>
            </div>
          </div>

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
              disabled={busy || liveBlocked || !instructionLoaded}
              aria-busy={busy}
            >
              {busy ? (
                <Spinner className="h-3.5 w-3.5" />
              ) : (
                <RotateCw className="h-3.5 w-3.5" />
              )}
              {t(busy ? "scan.launching" : "rerun.launch")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
