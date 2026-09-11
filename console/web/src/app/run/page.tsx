"use client";

/* ============================================================================
   /run?name=X — the RUN COCKPIT.

   Header carries the live status pill, target, scan-type chip, timeline
   tiles, failure diagnostics (reason + engine.log tail), and the lifecycle
   actions (Stop / Delete / CSV export / artifacts path). Below it, a single
   tabbed console panel: Conversation · Agents · Findings · Report · Hints.

   Desktop (≥1360×760) switches to a full-height console layout where the
   transcript and ledger panels scroll internally and the page never scrolls.
   ========================================================================= */

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  ArrowLeft,
  ChevronDown,
  Download,
  FolderOpen,
  RotateCw,
  Square,
} from "lucide-react";
import { Chip, ConfirmButton, EmptyState, StatusPill } from "@/components/ui";
import AgentsPanel from "@/components/AgentsPanel";
import AssessmentPanel from "@/components/AssessmentPanel";
import ArtifactsBrowser from "@/components/ArtifactsBrowser";
import EvidencePanel from "@/components/EvidencePanel";
import ConversationView from "@/components/ConversationView";
import FindingsPanel from "@/components/FindingsPanel";
import HintsPanel from "@/components/HintsPanel";
import NotesPanel from "@/components/NotesPanel";
import ProxyStatusPanel from "@/components/ProxyStatusPanel";
import WebSearchDiagnostics from "@/components/WebSearchDiagnostics";
import ReportPanel from "@/components/ReportPanel";
import RerunDialog from "@/components/RerunDialog";
import RunTargetList from "@/components/RunTargetList";
import { apiURL, getJSON, postJSON, del, runTargetLabel, runTargets } from "@/lib/api";
import type { Health, LogPage, RunDetail } from "@/lib/api";
import { fmtDuration, fmtTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

/** 12345 -> "12.3k" — compact token counts for the info tile. */
function fmtTokens(n: number | undefined): string {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

const TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "timeout",
  "cancelled",
  "stopped",
  "crashed",
]);

type TabKey = "conversation" | "agents" | "findings" | "evidence" | "assessment" | "notes" | "report" | "hints" | "artifacts";

const FAILED_STATUSES = new Set(["failed", "crashed"]);

/** How long a never-seen run may keep 404ing before we call it not-found
 *  (covers externally spawned engines that take a few seconds to mkdir). */
const STARTUP_GRACE_MS = 15000;
/** Poll cadence while the run record is unsettled (no status yet). */
const SETTLING_POLL_MS = 1500;

function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export default function RunPage() {
  const { t } = useI18n();
  return (
    <Suspense
      fallback={
        <div className="space-y-3">
          <div className="skeleton-line h-8 w-2/5" />
          <div className="skeleton-line w-full" />
          <div className="skeleton-line w-5/6" />
          <p className="font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
            {t("run.opening")}
          </p>
        </div>
      }
    >
      <Cockpit />
    </Suspense>
  );
}

/* ============================================================================
   Loading / not-found shells
   ========================================================================= */

function LoadingPanel({ offline, starting }: { offline: boolean; starting?: boolean }) {
  const { t } = useI18n();
  return (
    <div className="panel panel-hairline">
      <div className="space-y-3 p-5">
        {offline && (
          <div className="alert-warning" role="status">
            {t("run.loading.offline")}
          </div>
        )}
        {starting && (
          <div className="alert-warning" role="status">
            {t("run.loading.starting")}
          </div>
        )}
        <div className="skeleton-line h-7 w-2/5" />
        <div className="skeleton-line w-full" />
        <div className="skeleton-line w-3/4" />
        <div className="skeleton-line w-2/3" />
        <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
          {offline ? t("dashboard.waiting") : starting ? t("run.starting") : t("run.loading")}
        </p>
      </div>
    </div>
  );
}

function NotFoundPanel({ name }: { name: string }) {
  const { t } = useI18n();
  return (
    <div className="panel panel-hairline">
      <EmptyState
        title={t("run.notFound")}
        hint={t("run.notFound.hint", { name })}
        action={
          <Link href="/" className="button-primary button-compact">
            <ArrowLeft className="h-3.5 w-3.5" />
            {t("run.backToRuns")}
          </Link>
        }
      />
    </div>
  );
}

function NoNamePanel() {
  const { t } = useI18n();
  return (
    <div className="panel panel-hairline">
      <EmptyState
        title={t("run.noSelection")}
        hint={t("run.noSelection.hint")}
        action={
          <Link href="/" className="button-primary button-compact">
            <ArrowLeft className="h-3.5 w-3.5" />
            {t("run.backToRuns")}
          </Link>
        }
      />
    </div>
  );
}

/* ============================================================================
   Cockpit
   ========================================================================= */

const TAB_KEYS: readonly TabKey[] = [
  "conversation",
  "agents",
  "findings",
  "evidence",
  "assessment",
  "notes",
  "report",
  "hints",
  "artifacts",
];

function parseTab(value: string | null): TabKey {
  return TAB_KEYS.includes(value as TabKey) ? (value as TabKey) : "conversation";
}

function Cockpit() {
  const { t, locale } = useI18n();
  const params = useSearchParams();
  const name = params.get("name") || "";
  const router = useRouter();

  const [run, setRun] = React.useState<RunDetail | null>(null);
  const [notFound, setNotFound] = React.useState(false);
  const [offline, setOffline] = React.useState(false);
  const [starting, setStarting] = React.useState(false);
  const [rerunOpen, setRerunOpen] = React.useState(false);
  const [tab, setTab] = React.useState<TabKey>(() => parseTab(params.get("tab")));
  const [hintTarget, setHintTarget] = React.useState<{ id: string; name: string } | null>(null);
  const [notice, setNotice] = React.useState("");
  const [noticeTone, setNoticeTone] = React.useState<"success" | "error">("success");
  const [logText, setLogText] = React.useState<string | null>(null);
  const [logOpen, setLogOpen] = React.useState(true);
  const [runsRoot, setRunsRoot] = React.useState("");
  const [now, setNow] = React.useState(() => Date.now());
  const [viewport, setViewport] = React.useState({ width: 0, height: 0 });

  const requestedTab = params.get("tab");
  React.useEffect(() => {
    setTab(parseTab(requestedTab));
  }, [requestedTab]);

  const selectTab = React.useCallback(
    (nextTab: TabKey) => {
      setTab(nextTab);
      const nextParams = new URLSearchParams(params.toString());
      nextParams.set("tab", nextTab);
      router.replace(`/run?${nextParams.toString()}`, { scroll: false });
    },
    [params, router]
  );

  /* Latest run record + first-404 timestamp, readable inside poll closures. */
  const runRef = React.useRef<RunDetail | null>(null);
  const firstMissRef = React.useRef<number | null>(null);

  /* ---- viewport tracking (desktop console layout ≥1360×760) ---- */
  React.useEffect(() => {
    const sync = () => setViewport({ width: window.innerWidth, height: window.innerHeight });
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);
  const desktop = viewport.width >= 1360 && viewport.height >= 760;

  /* ---- run record poll ----
     1.5s while the record is unsettled (just launched, no status yet, or the
     engine has not written run.json), 3s while live, 12s heartbeat after. */
  const live = Boolean(run?.live);
  const unsettled = !run || !run.status || run.status === "unknown";
  React.useEffect(() => {
    if (!name || notFound) return;
    let disposed = false;
    const load = async () => {
      if (document.hidden) return;
      try {
        const data = await getJSON<RunDetail>(`/api/runs/${encodeURIComponent(name)}`);
        if (disposed) return;
        runRef.current = data;
        firstMissRef.current = null;
        setStarting(false);
        setRun(data);
        setOffline(false);
      } catch (e) {
        if (disposed) return;
        const message = e instanceof Error ? e.message : String(e);
        if (message.startsWith("404")) {
          if (runRef.current !== null) {
            /* existed before, gone now — deleted underneath us */
            setNotFound(true);
            return;
          }
          /* Never seen: an externally spawned engine may simply not have
           * created the run dir yet. Give it a grace window, then declare
           * the run not found. */
          if (firstMissRef.current === null) firstMissRef.current = Date.now();
          if (Date.now() - firstMissRef.current > STARTUP_GRACE_MS) {
            setNotFound(true);
            return;
          }
          setStarting(true);
          setOffline(false);
          return;
        }
        setOffline(true);
      }
    };
    void load();
    const timer = window.setInterval(load, unsettled ? SETTLING_POLL_MS : live ? 3000 : 12000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [name, notFound, live, unsettled]);

  /* ---- artifacts root hint ---- */
  React.useEffect(() => {
    getJSON<Health>("/api/health")
      .then((h) => setRunsRoot(h.runs_root ?? ""))
      .catch(() => undefined);
  }, []);

  /* ---- ticking clock for the live duration tile ---- */
  React.useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);

  /* ---- failure diagnostics: engine.log tail ----
     An operator-initiated Stop lands as failed/"interrupted" — tracked
     separately so a deliberate stop never renders as a failure. */
  const interrupted =
    (run?.status || "").toLowerCase() === "failed" &&
    (run?.failure_reason || "").toLowerCase() === "interrupted";
  const failed = Boolean(
    run &&
      !interrupted &&
      (run.failure_reason || FAILED_STATUSES.has((run.status || "").toLowerCase()))
  );
  React.useEffect(() => {
    if (!failed || logText !== null) return;
    getJSON<LogPage>(`/api/runs/${encodeURIComponent(name)}/log`)
      .then((page) => setLogText(page.text ?? ""))
      .catch(() => setLogText(""));
  }, [failed, logText, name]);

  const flash = (tone: "success" | "error", message: string) => {
    setNoticeTone(tone);
    setNotice(message);
    window.setTimeout(() => setNotice(""), 7000);
  };

  const stopRun = async () => {
    if (!run) return;
    try {
      const result = await postJSON<{ ok: boolean; detail?: string }>(
        `/api/runs/${encodeURIComponent(name)}/stop`,
        {}
      );
      if (result.ok) {
        flash("success", t("run.notice.stopSent"));
      } else {
        flash("error", t("run.notice.stopRefused", { detail: result.detail ?? t("run.notice.signalRejected") }));
      }
    } catch (e) {
      flash("error", t("run.notice.stopFailed", { error: String(e) }));
    }
  };

  const deleteRun = async () => {
    if (!run) return;
    try {
      await del(`/api/runs/${encodeURIComponent(name)}`);
      router.push("/");
    } catch (e) {
      flash("error", t("run.notice.deleteFailed", { error: String(e) }));
    }
  };

  if (!name) return <NoNamePanel />;
  if (notFound) return <NotFoundPanel name={name} />;
  if (!run) return <LoadingPanel offline={offline} starting={starting} />;

  const rawStatus = run.status || "unknown";
  /* Operator Stop surfaces as failed/"interrupted" — display it as the
   * deliberate stop it is (the pill + copy below use this mapping). */
  const status = interrupted ? "stopped" : rawStatus;
  const statusLower = status.toLowerCase();
  const terminal = !live && TERMINAL_STATUSES.has(statusLower);
  const stale = Boolean(run.stale);
  const agents = run.agents ? Object.keys(run.agents).length : 0;
  const findingsTotal = (run.vulnerability_count ?? 0) + (run.internal_finding_count ?? 0);
  const hintsTotal = run.hints_summary?.total ?? 0;

  const startMs = run.start_time ? Date.parse(run.start_time) : NaN;
  const liveDuration =
    live && !Number.isNaN(startMs) ? Math.max(0, (now - startMs) / 1000) : null;
  const duration = liveDuration ?? run.duration_seconds ?? null;

  const displayStatus = (value: string) => {
    const key = `status.${value.toLowerCase()}`;
    const translated = t(key);
    return translated === key ? value : translated;
  };

  const statusLine = live
    ? t("run.summary.live")
    : status === "completed"
      ? t("run.summary.completed", {
          n: run.vulnerability_count ?? 0,
          duration: fmtDuration(run.duration_seconds),
        })
      : interrupted
        ? t("run.summary.stopped")
        : failed
          ? run.failure_reason || t("run.summary.failed")
          : t("run.summary.status", { status: displayStatus(status) });

  const tabs: Array<{ key: TabKey; label: string; count?: number }> = [
    { key: "conversation", label: t("run.tab.conversation") },
    { key: "agents", label: t("run.tab.agents"), count: agents },
    { key: "findings", label: t("run.tab.findings"), count: findingsTotal },
    { key: "evidence", label: t("run.tab.evidence") },
    { key: "assessment", label: locale === "en" ? "Assessment" : "评估" },
    { key: "notes", label: t("run.tab.notes") },
    { key: "report", label: t("run.tab.report") },
    { key: "hints", label: t("run.tab.hints"), count: hintsTotal },
    { key: "artifacts", label: t("run.tab.artifacts") },
  ];

  const csvHref = apiURL(`/api/runs/${encodeURIComponent(name)}/artifacts/vulnerabilities.csv`);
  const artifactsPath = runsRoot ? `${runsRoot}/${name}` : name;

  const tabContent = (() => {
    switch (tab) {
      case "conversation":
        return <ConversationView name={name} run={run} />;
      case "agents":
        return (
          <AgentsPanel
            name={name}
            run={run}
            onSendHint={(agentId, agentName) => {
              setHintTarget({ id: agentId, name: agentName });
              selectTab("hints");
            }}
          />
        );
      case "findings":
        return <FindingsPanel name={name} run={run} />;
      case "evidence":
        return <EvidencePanel name={name} run={run} />;
      case "assessment":
        return <AssessmentPanel key={name} name={name} run={run} />;
      case "notes":
        return <NotesPanel key={name} runName={name} live={live} />;
      case "report":
        return <ReportPanel name={name} run={run} />;
      case "hints":
        return <HintsPanel name={name} run={run} target={hintTarget} />;
      case "artifacts":
        return <ArtifactsBrowser name={name} live={live} />;
    }
  })();

  const internalScroll = tab === "conversation" || tab === "hints" || tab === "artifacts";

  return (
    <div
      className={cn(
        "mx-auto flex w-full max-w-[1720px] flex-col",
        desktop
          ? "h-[calc(100dvh-7.6rem)] gap-3 overflow-hidden" /* main padding + status strip + ticker */
          : "max-w-[1440px] gap-4"
      )}
    >
      {/* ============================ header ============================ */}
      <div className={cn("flex-shrink-0", desktop && "xl:[&_.hero-panel]:px-5 xl:[&_.hero-panel]:py-4")}>
        <div className="hero-panel mb-0">
          <div className="hero-copy min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-3">
              <span className="eyebrow">&gt; {t("run.path")}</span>
              <Link
                href="/"
                className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted transition-colors hover:text-accent"
              >
                <ArrowLeft className="h-3 w-3" />
                {t("run.backToRuns")}
              </Link>
            </div>
            <h1 className="page-title truncate" title={runTargets(run).join("\n") || name}>
              {runTargetLabel(run)}
            </h1>
            <RunTargetList targets={runTargets(run)} />
            {run.target_error && (
              <p className="alert-warning mt-3" role="alert">
                {locale === "zh-CN"
                  ? "任务的完整目标范围记录无法读取，请检查 run.json。"
                  : "The task's complete target scope could not be read. Check run.json."}
              </p>
            )}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <StatusPill status={live ? "live" : status} label={displayStatus(live ? "live" : status)} live={live} />
              {stale && <StatusPill status="stale" label={displayStatus("stale")} />}
              <Chip tone={run.scan_type === "internal" ? "success" : "accent"}>
                {t("run.scope", { type: run.scan_type || "web" })}
              </Chip>
              {run.dry_run !== undefined && (
                <Chip tone={run.dry_run ? "accent" : "warning"}>
                  {run.dry_run ? t("run.dryRun") : t("run.liveLlm")}
                </Chip>
              )}
              {run.model && (
                <span className="mono-chip" title={t("run.modelRoute", { model: run.model })}>
                  model · {run.model}
                </span>
              )}
              {run.socks5 && (
                <span className="mono-chip" title={`SOCKS5 egress: ${run.socks5}`}>
                  socks5 · {run.socks5.replace(/^socks5h?:\/\//i, "")}
                </span>
              )}
              {run.gsocket && (
                <span
                  className="mono-chip"
                  title={`GSocket reverse tunnel (secret key •••${run.gsocket.slice(-4)})`}
                >
                  gsocket · •••{run.gsocket.slice(-4)}
                </span>
              )}
              {run.crypto && <Chip tone="warning">crypto</Chip>}
              <span className="mono-chip" title={name}>
                {name}
              </span>
            </div>
            <p className="page-copy">{statusLine}</p>
          </div>

          <div className="hero-actions min-w-0 max-w-full flex-shrink-0 flex-col items-end">
            <div className="flex min-w-0 max-w-full flex-wrap items-center justify-end gap-2">
              <Chip tone="danger">{t("run.vulns", { n: run.vulnerability_count ?? 0 })}</Chip>
              {(run.internal_finding_count ?? 0) > 0 && (
                <Chip tone="warning">{t("run.internal", { n: run.internal_finding_count })}</Chip>
              )}
              {hintsTotal > 0 && (
                <Chip tone="violet">
                  {t("run.hintsDelivered", { delivered: run.hints_summary?.delivered ?? 0, total: hintsTotal })}
                </Chip>
              )}
            </div>
            <div className="flex min-w-0 max-w-full flex-wrap items-center justify-end gap-2">
              {live && (
                <ConfirmButton
                  label={t("run.stop")}
                  confirmLabel={t("run.confirmStop")}
                  danger
                  onConfirm={() => void stopRun()}
                />
              )}
              {terminal && (
                <button
                  type="button"
                  className="button-secondary button-compact"
                  onClick={() => setRerunOpen(true)}
                  title={t("run.rerun.hint")}
                >
                  <RotateCw className="h-3.5 w-3.5" strokeWidth={1.8} />
                  {t("run.rerun")}
                </button>
              )}
              {terminal && (
                <ConfirmButton
                  label={t("run.delete")}
                  confirmLabel={t("run.confirmDelete")}
                  danger
                  onConfirm={() => void deleteRun()}
                />
              )}
              <a
                className="button-secondary button-compact"
                href={csvHref}
                download={`${name}-vulnerabilities.csv`}
                title={t("run.csv.hint")}
              >
                <Download className="h-3.5 w-3.5" />
                {t("run.csv")}
              </a>
            </div>
            {runsRoot && (
              <div className="flex min-w-0 max-w-full justify-end">
                <span
                  className="mono-chip max-w-full truncate text-[9px]"
                  title={t("run.artifactsPath", { path: artifactsPath })}
                >
                  <FolderOpen className="h-3 w-3 shrink-0" />
                  <span className="truncate">{artifactsPath}</span>
                </span>
              </div>
            )}
          </div>
        </div>

        {/* action notice */}
        {notice && (
          <div
            className={cn(
              "mt-3 rounded-[1rem] border px-4 py-2.5 text-xs leading-relaxed",
              noticeTone === "success"
                ? "border-success/25 bg-success/10 text-success"
                : "border-danger/25 bg-danger/10 text-danger"
            )}
            role="status"
          >
            {notice}
          </div>
        )}

        {/* timeline tiles */}
        <div className="mt-3 grid gap-2 md:grid-cols-5">
          <div className="info-tile">
            <div className="info-label">{t("run.started")}</div>
            <div className="info-value text-sm">{fmtTime(run.start_time, locale)}</div>
          </div>
          <div className="info-tile">
            <div className="info-label">{live ? t("run.ends") : t("run.finished")}</div>
            <div className="info-value text-sm">
              {live ? (
                <span className="text-fg-muted">
                  {t("run.inProgress")}<span className="ml-1 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent align-middle" />
                </span>
              ) : (
                fmtTime(run.end_time, locale)
              )}
            </div>
          </div>
          <div className="info-tile">
            <div className="info-label">{t("run.duration")}</div>
            <div className="info-value text-sm">{fmtDuration(duration)}</div>
          </div>
          <div className="info-tile">
            <div className="info-label">{t("run.tokens")}</div>
            <div className="info-value text-sm" title={t("run.tokens.hint")}>
              {run.llm_usage ? (
                <>
                  {t("run.tokens.in", { n: fmtTokens(run.llm_usage.input_tokens) })}
                  <span className="mx-1 text-fg-faint">/</span>
                  {t("run.tokens.out", { n: fmtTokens(run.llm_usage.output_tokens) })}
                </>
              ) : (
                <span className="text-fg-faint">—</span>
              )}
            </div>
          </div>
          <div className="info-tile">
            <div className="info-label">{t("run.findings")}</div>
            <div className="info-value text-sm">
              {t("run.vulns", { n: run.vulnerability_count ?? 0 })}
              {(run.internal_finding_count ?? 0) > 0 && (
                <span className="ml-1.5 font-normal text-fg-muted">
                  +{t("run.internal", { n: run.internal_finding_count })}
                </span>
              )}
            </div>
          </div>
        </div>

        {/* failure / stop diagnostics */}
        {(failed || interrupted) && (
          <div className="mt-3 space-y-2">
            <div className={interrupted ? "alert-warning" : "alert-error"} role={interrupted ? "status" : "alert"}>
              <span className="font-mono text-[10px] uppercase tracking-[0.16em] opacity-80">
                {interrupted ? t("run.stopped") : t("run.failed")}
                {run.engine_exit_code !== undefined ? ` · ${t("run.exitCode", { code: run.engine_exit_code })}` : ""}
              </span>
              <div className="mt-1 break-words">
                {interrupted
                  ? t("run.stopped.detail")
                  : run.failure_reason ||
                    t("run.failed.detail")}
              </div>
            </div>
            <div className="terminal">
              <button
                type="button"
                className="flex w-full items-center gap-2 px-3.5 py-2 text-left"
                onClick={() => setLogOpen((v) => !v)}
                aria-expanded={logOpen}
              >
                <ChevronDown
                  className={`h-3.5 w-3.5 text-fg-muted transition-transform ${logOpen ? "rotate-180" : ""}`}
                />
                <span className="terminal-bar border-0 px-0 py-0">
                  <span className="terminal-bar-dot" aria-hidden />
                  {t("run.logTail")}
                </span>
              </button>
              {logOpen && (
                <pre className="terminal-body max-h-64 border-t border-line/6">
                  {logText === null
                    ? t("run.logLoading")
                    : logText.trim()
                      ? logText
                      : t("run.logEmpty")}
                </pre>
              )}
            </div>
          </div>
        )}
      </div>

      <ProxyStatusPanel key={name} runName={name} live={live} enabled={run.scan_type === "web" && !run.dry_run} />
      <WebSearchDiagnostics key={`search-${name}`} runName={name} live={live} />

      {/* ============================ console ============================ */}
      <section
        className={cn(
          "panel panel-hairline flex flex-col overflow-hidden",
          desktop ? "min-h-0 flex-1" : ""
        )}
      >
        <div className="tabs-bar flex-shrink-0" role="tablist">
          {tabs.map((t) => (
            <button
              key={t.key}
              type="button"
              role="tab"
              aria-selected={tab === t.key}
              className={cn("tab-item", tab === t.key && "tab-item-active")}
              onClick={() => selectTab(t.key)}
            >
              {t.label}
              {t.count !== undefined && t.count > 0 && (
                <span className="ml-1 text-fg-faint">({t.count})</span>
              )}
            </button>
          ))}
        </div>
        <div
          className={cn(
            "min-h-0 flex-1",
            internalScroll
              ? cn(
                  "flex flex-col overflow-hidden",
                  !desktop && tab === "conversation" && "h-[72dvh] max-h-[44rem] min-h-[26rem]",
                  !desktop && tab === "hints" && "h-[64dvh] max-h-[40rem] min-h-[24rem]",
                  !desktop && tab === "artifacts" && "h-[76dvh] max-h-[52rem] min-h-[26rem]"
                )
              : desktop
                ? "overflow-y-auto"
                : ""
          )}
        >
          {tabContent}
        </div>
      </section>

      {/* footer hint strip */}
      <div className="flex flex-shrink-0 flex-wrap items-center justify-between gap-2 px-1 text-[10px] text-fg-faint">
        <span className="font-mono uppercase tracking-[0.16em]">
          {run.scan_type || "web"} · {name}
        </span>
        <span className="font-mono uppercase tracking-[0.16em]">
          {live ? (
            <span className="inline-flex items-center gap-1.5 text-accent">
              <Square className="h-2.5 w-2.5 animate-pulse fill-current" />
              {t("run.engineLive")}
            </span>
          ) : (
            <>
              {t("run.footer", { status: displayStatus(status), path: artifactsPath })}
            </>
          )}
        </span>
      </div>

      {/* rerun dialog — pre-filled from this run's instruction.md */}
      {rerunOpen && (
        <RerunDialog
          run={run}
          onCancel={() => setRerunOpen(false)}
          onLaunched={(runName) => {
            setRerunOpen(false);
            router.push(`/run?name=${encodeURIComponent(runName)}`);
          }}
        />
      )}
    </div>
  );
}
