"use client";

import * as React from "react";
import Link from "next/link";
import { ChevronLeft, ChevronRight, Plus, RotateCw, Search, WifiOff, X } from "lucide-react";
import {
  apiURL,
  del,
  getJSON,
  runTargetLabel,
  runTargets,
  type OkResult,
  type RunSummary,
  type RunsPage,
} from "@/lib/api";
import { fmtDuration } from "@/lib/format";
import { useI18n, type Locale } from "@/lib/i18n";
import { PAGE_SIZE_KEY, readStorage, writeStorage } from "@/lib/storage";
import { Select } from "@/components/Select";
import {
  Chip,
  ConfirmButton,
  EmptyState,
  MetricCard,
  MicroLabel,
  Panel,
  SeverityChip,
  Spinner,
  StatusPill,
} from "@/components/ui";

/* ============================================================================
   Constants + helpers
   ========================================================================= */

const FAST_POLL_MS = 4000;
const SLOW_POLL_MS = 10000;

const SEV_KEYS = ["critical", "high", "medium", "low", "info"] as const;
type SevKey = (typeof SEV_KEYS)[number];

const TYPE_FILTERS = ["all", "web", "internal"] as const;
type TypeFilter = (typeof TYPE_FILTERS)[number];

const PAGE_SIZES = [30, 50, 100] as const;
const DEFAULT_PAGE_SIZE = 50;

/** Display status: an operator Stop (failed/"interrupted") shows as stopped. */
function pillStatusOf(run: RunSummary): string {
  if (run.live) return "live";
  const status = (run.status || "unknown").toLowerCase();
  if (status === "failed" && (run.failure_reason || "").toLowerCase() === "interrupted") {
    return "stopped";
  }
  return run.status || "unknown";
}

function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Case-insensitive severity lookup — engine keys are lowercase, chips uppercase. */
function sevCount(map: Record<string, number> | undefined, key: SevKey): number {
  if (!map) return 0;
  for (const k of Object.keys(map)) {
    if (k.toLowerCase() === key) return map[k] ?? 0;
  }
  return 0;
}

/** Duration for a row: recorded value, or a ticking clock while live. */
function runDuration(run: RunSummary, nowMs: number): string {
  if (run.duration_seconds != null) return fmtDuration(run.duration_seconds);
  if (run.live && run.start_time) {
    const t = new Date(run.start_time).getTime();
    if (!Number.isNaN(t)) return fmtDuration(Math.max(0, (nowMs - t) / 1000));
  }
  return "—";
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function localeTime(value: string | number | null | undefined, locale: Locale): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function localeRelativeTime(value: string | number | null | undefined, locale: Locale): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const delta = date.getTime() - Date.now();
  const absoluteSeconds = Math.abs(delta) / 1000;
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (absoluteSeconds < 45) return formatter.format(0, "second");
  if (absoluteSeconds < 3600) return formatter.format(Math.round(delta / 60000), "minute");
  if (absoluteSeconds < 86400) return formatter.format(Math.round(delta / 3600000), "hour");
  if (absoluteSeconds < 1209600) return formatter.format(Math.round(delta / 86400000), "day");
  return localeTime(value, locale);
}

/* ============================================================================
   Skeletons — same geometry as the real surfaces
   ========================================================================= */

function MetricSkeleton() {
  return (
    <div className="metric-card">
      <div className="skeleton-line w-24" />
      <div className="skeleton-line mt-3 h-7 w-16" />
      <div className="skeleton-line mt-3 w-36 opacity-60" />
    </div>
  );
}

function SevCellSkeleton() {
  return (
    <div className="risk-legend-item">
      <div className="skeleton-line w-16" />
      <div className="skeleton-line w-8" />
    </div>
  );
}

function RowSkeleton() {
  return (
    <div className="run-ledger-row">
      <div className="skeleton-line w-40" />
      <div className="skeleton-line mt-2.5 w-72 opacity-60" />
      <div className="skeleton-line mt-2.5 w-48 opacity-40" />
    </div>
  );
}

/* ============================================================================
   Sync indicator — pulses green while polling, red while offline
   ========================================================================= */

function SyncIndicator({
  offline,
  lastSync,
  cadenceMs,
}: {
  offline: boolean;
  lastSync: number | null;
  cadenceMs: number;
}) {
  const { t, locale } = useI18n();
  if (offline) {
    return (
      <span className="sync-indicator sync-indicator-offline">
        <span aria-hidden="true" />
        {t("dashboard.offlineRetrying")}
      </span>
    );
  }
  return (
    <span className="sync-indicator">
      <span aria-hidden="true" />
      {t("dashboard.synced", {
        time: lastSync ? localeRelativeTime(lastSync, locale) : "—",
        seconds: Math.round(cadenceMs / 1000),
      })}
    </span>
  );
}

/* ============================================================================
   Dashboard page
   ========================================================================= */

export default function DashboardPage() {
  /* ---------------------------------------------------------------- state */
  const { t, locale } = useI18n();
  const [data, setData] = React.useState<RunsPage | null>(null);
  const [offline, setOffline] = React.useState(false);
  const [fetchError, setFetchError] = React.useState<string | null>(null);
  const [syncing, setSyncing] = React.useState(false);
  const [lastSync, setLastSync] = React.useState<number | null>(null);
  const [cadenceMs, setCadenceMs] = React.useState(FAST_POLL_MS);

  const [search, setSearch] = React.useState("");
  const [typeFilter, setTypeFilter] = React.useState<TypeFilter>("all");

  /* pagination — page size persisted across visits */
  const [pageSize, setPageSize] = React.useState<number>(DEFAULT_PAGE_SIZE);
  const [pageNum, setPageNum] = React.useState(0);
  const [pageSizeHydrated, setPageSizeHydrated] = React.useState(false);

  React.useEffect(() => {
    const saved = Number.parseInt(readStorage(PAGE_SIZE_KEY) ?? "", 10);
    if (PAGE_SIZES.includes(saved as (typeof PAGE_SIZES)[number])) {
      setPageSize(saved);
    }
    setPageSizeHydrated(true);
  }, []);

  React.useEffect(() => {
    if (pageSizeHydrated) writeStorage(PAGE_SIZE_KEY, String(pageSize));
  }, [pageSize, pageSizeHydrated]);

  const [deleting, setDeleting] = React.useState<string | null>(null);
  const [actionError, setActionError] = React.useState<string | null>(null);

  /* 1s tick — only armed while at least one run is live, so durations breathe. */
  const [nowMs, setNowMs] = React.useState(() => Date.now());

  const signatureRef = React.useRef("");
  const fastRef = React.useRef(true);
  const inFlight = React.useRef(false);
  const refreshRef = React.useRef<(() => void) | undefined>(undefined);

  /* ----------------------------------------------------------------- load */
  const load = React.useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSyncing(true);
    try {
      const page = await getJSON<RunsPage>("/api/runs");
      const signature = JSON.stringify(page);
      const changed = signature !== signatureRef.current;
      signatureRef.current = signature;
      /* Stay on the fast cadence while anything is live, else back off. */
      fastRef.current = changed || page.runs.some((r) => r.live);
      setCadenceMs(fastRef.current ? FAST_POLL_MS : SLOW_POLL_MS);
      setData(page);
      setOffline(false);
      setFetchError(null);
      setLastSync(Date.now());
    } catch (e) {
      setOffline(true);
      setFetchError(errMsg(e));
    } finally {
      inFlight.current = false;
      setSyncing(false);
    }
  }, []);

  /* -------------------------------------------- poll: 4s / 10s + visibility */
  React.useEffect(() => {
    let stopped = false;
    let timer = 0;

    const pump = () => {
      if (stopped) return;
      timer = window.setTimeout(() => void run(), fastRef.current ? FAST_POLL_MS : SLOW_POLL_MS);
    };

    const run = async () => {
      if (stopped) return;
      /* Hidden tab: skip the fetch, keep the chain alive until visible again. */
      if (document.visibilityState === "hidden") {
        pump();
        return;
      }
      await load();
      pump();
    };

    refreshRef.current = () => {
      window.clearTimeout(timer);
      void run();
    };

    void run();

    const onVisibility = () => {
      if (document.visibilityState === "visible" && !stopped) {
        window.clearTimeout(timer);
        void run();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      refreshRef.current = undefined;
    };
  }, [load]);

  const refresh = React.useCallback(() => refreshRef.current?.(), []);

  /* -------------------------------------------------------- live 1s ticker */
  const anyLive = data?.runs.some((r) => r.live) ?? false;
  React.useEffect(() => {
    if (!anyLive) return;
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [anyLive]);

  /* ------------------------------------------------------- action error ttl */
  React.useEffect(() => {
    if (!actionError) return;
    const id = window.setTimeout(() => setActionError(null), 10000);
    return () => window.clearTimeout(id);
  }, [actionError]);

  /* ---------------------------------------------------------------- delete */
  const handleDelete = async (name: string) => {
    setDeleting(name);
    try {
      await del<OkResult>(`/api/runs/${encodeURIComponent(name)}`);
      setActionError(null);
      await load();
    } catch (e) {
      setActionError(t("dashboard.deleteFailed", { name, error: errMsg(e) }));
    } finally {
      setDeleting(null);
    }
  };

  /* -------------------------------------------------------------- derived */
  const runs = data?.runs ?? [];
  const totals = data?.totals;

  const sorted = React.useMemo(
    () =>
      [...runs].sort((a, b) =>
        (b.start_time || "").localeCompare(a.start_time || "")
      ),
    [runs]
  );
  const activeRuns = sorted.filter((run) => run.live).slice(0, 3);

  const liveCount = totals?.live ?? runs.filter((r) => r.live).length;
  const completedCount = runs.filter((r) => (r.status || "").toLowerCase() === "completed").length;
  const attentionCount = runs.filter((r) =>
    ["failed", "crashed"].includes((r.status || "").toLowerCase())
  ).length;

  const findingsTotal = totals
    ? SEV_KEYS.reduce((sum, k) => sum + sevCount(totals.severity, k), 0)
    : 0;
  const criticalCount = totals ? sevCount(totals.severity, "critical") : 0;
  const highCount = totals ? sevCount(totals.severity, "high") : 0;

  const statusLabel = (status: string) => {
    const key = `status.${(status || "unknown").toLowerCase()}`;
    const translated = t(key);
    return translated === key ? status : translated;
  };

  const q = search.trim().toLowerCase();
  const typeOf = (r: RunSummary) => (r.scan_type || "").toLowerCase();
  const filtered = sorted.filter((r) => {
    if (typeFilter !== "all" && typeOf(r) !== typeFilter) return false;
    if (!q) return true;
    return (
      runTargets(r).some((target) => target.toLowerCase().includes(q)) ||
      (r.name || "").toLowerCase().includes(q)
    );
  });

  const typeCounts = React.useMemo(
    () => ({
      all: sorted.length,
      web: sorted.filter((r) => typeOf(r) === "web").length,
      internal: sorted.filter((r) => typeOf(r) === "internal").length,
    }),
    [sorted]
  );

  const booting = data === null && !offline;
  const filtersActive = q !== "" || typeFilter !== "all";
  const clearFilters = () => {
    setSearch("");
    setTypeFilter("all");
  };

  /* ------------------------------------------------------------- pagination */
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(pageNum, pageCount - 1);
  const pageStart = safePage * pageSize;
  const pageEnd = Math.min(pageStart + pageSize, filtered.length);
  const pageRows = filtered.slice(pageStart, pageEnd);
  React.useEffect(() => {
    setPageNum(0); /* any filter/size change rewinds to the first page */
  }, [q, typeFilter, pageSize]);

  /* =================================================================== jsx */

  return (
    <div className="dashboard-page">
      <section className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">&gt; {t("dashboard.path")}</div>
          <h1 className="page-title">{t("dashboard.title")}</h1>
        </div>
        <div className="hero-actions">
          <button
            type="button"
            className="button-secondary button-compact"
            onClick={refresh}
            disabled={syncing}
            title={t("dashboard.sync.hint")}
          >
            {syncing ? (
              <Spinner className="h-3.5 w-3.5" />
            ) : (
              <RotateCw className="h-3.5 w-3.5" strokeWidth={1.8} />
            )}
            <span>{t("dashboard.sync")}</span>
          </button>
          <Link href="/scan" className="button-primary button-compact">
            <Plus className="h-3.5 w-3.5" strokeWidth={2} />
            {t("dashboard.newScan")}
          </Link>
        </div>
      </section>

      <div className="dashboard-metrics">
        {booting ? (
          <>
            <MetricSkeleton />
            <MetricSkeleton />
            <MetricSkeleton />
            <MetricSkeleton />
          </>
        ) : (
          <>
            <MetricCard
              code="RUN"
              label={t("dashboard.active")}
              tone="accent"
              value={data ? String(liveCount).padStart(2, "0") : "—"}
              matrix
              hint={data ? t("dashboard.active.hint", { n: liveCount }) : t("dashboard.waiting")}
            />
            <MetricCard
              code="DONE"
              label={t("dashboard.completed")}
              tone="success"
              value={data ? String(completedCount).padStart(2, "0") : "—"}
              matrix
              hint={data ? t("dashboard.completed.hint", { done: completedCount, total: runs.length }) : t("dashboard.waiting")}
            />
            <MetricCard
              code="ACT"
              label={t("dashboard.attention")}
              tone="danger"
              value={data ? String(attentionCount).padStart(2, "0") : "—"}
              matrix
              hint={data ? t("dashboard.attention.metricHint", { n: attentionCount }) : t("dashboard.waiting")}
            />
            <MetricCard
              code="FND"
              label={t("dashboard.findings")}
              tone="violet"
              value={data ? String(findingsTotal).padStart(2, "0") : "—"}
              matrix
              hint={data ? t("dashboard.findings.hint", { critical: criticalCount, high: highCount }) : t("dashboard.waiting")}
            />
          </>
        )}
      </div>

      <div className="dashboard-overview-grid">
        <Panel
          code="RUNS"
          title={t("dashboard.currentRuns")}
          actions={
            <MicroLabel>
              {t("dashboard.currentMeta", {
                n: liveCount,
                seconds: Math.round(cadenceMs / 1000),
              })}
            </MicroLabel>
          }
        >
          {booting ? (
            <div className="active-run-list">
              <RowSkeleton />
              <RowSkeleton />
            </div>
          ) : activeRuns.length > 0 ? (
            <div className="active-run-list">
              {activeRuns.map((run) => {
                const runFindings = run.vulnerability_count + run.internal_finding_count;
                const pillStatus = pillStatusOf(run);
                return (
                  <Link
                    key={run.name}
                    href={`/run?name=${encodeURIComponent(run.name)}`}
                    className="active-run-row"
                  >
                    <span className="active-run-signal" aria-hidden="true" />
                    <span className="active-run-identity">
                      <strong title={runTargets(run).join("\n") || run.name}>{runTargetLabel(run)}</strong>
                      <span>{run.name} · {runDuration(run, nowMs)}</span>
                    </span>
                    <StatusPill status={pillStatus} label={statusLabel(pillStatus)} live />
                    <span className="active-run-findings">
                      <b>{runFindings}</b>
                      <span>{t("dashboard.findings")}</span>
                    </span>
                    <ChevronRight className="active-run-open" aria-hidden="true" />
                  </Link>
                );
              })}
            </div>
          ) : (
            <div className="compact-empty">
              <span className="compact-empty-code">[IDLE]</span>
              <span>
                <strong>{t("dashboard.noActive")}</strong>
                <small>{data ? t("dashboard.noActive.hint") : t("dashboard.waiting")}</small>
              </span>
              <Link href="/scan" className="button-secondary button-compact">
                {t("dashboard.newScan")}
              </Link>
            </div>
          )}
        </Panel>

        <Panel
          code="RISK"
          title={t("dashboard.threat")}
          actions={<MicroLabel>{t("dashboard.riskMeta", { n: findingsTotal })}</MicroLabel>}
        >
          <div className="risk-overview">
            <div className="severity-spectrum" role="img" aria-label={t("dashboard.threat")}>
              {SEV_KEYS.map((key) => {
                const count = totals ? sevCount(totals.severity, key) : 0;
                return (
                  <span
                    key={key}
                    className={`severity-segment severity-segment-${key}`}
                    style={{ flexGrow: Math.max(count, findingsTotal === 0 ? 1 : 0.18) }}
                    title={`${key.toUpperCase()} ${count}`}
                  />
                );
              })}
            </div>
            <div className="risk-legend">
              {booting
                ? SEV_KEYS.map((key) => <SevCellSkeleton key={key} />)
                : SEV_KEYS.map((key) => (
                    <div key={key} className="risk-legend-item">
                      <SeverityChip severity={key.toUpperCase()} />
                      <strong>{totals ? sevCount(totals.severity, key) : 0}</strong>
                    </div>
                  ))}
            </div>
          </div>
        </Panel>
      </div>

      <Panel
        code="LEDGER"
        title={t("dashboard.queue")}
        actions={
          <div className="ledger-search">
            <Search
              className="ledger-search-icon"
              strokeWidth={1.8}
            />
            <input
              type="text"
              className="input-shell"
              placeholder={t("dashboard.searchPlaceholder")}
              aria-label={t("dashboard.searchLabel")}
              autoComplete="off"
              spellCheck={false}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        }
      >
        <div className="ledger-toolbar">
          {TYPE_FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              className={cn(
                "filter-chip",
                typeFilter === f && "filter-chip-active"
              )}
              aria-pressed={typeFilter === f}
              onClick={() => setTypeFilter(f)}
            >
              {f === "all" ? t("common.all") : t(`dashboard.filter.${f}`)}
              <span>{typeCounts[f]}</span>
            </button>
          ))}

          <div className="ledger-toolbar-meta">
            {data && (
              <span className="ledger-count">
                {filtered.length} / {sorted.length} {t("dashboard.runs")}
              </span>
            )}
            <SyncIndicator
              offline={offline}
              lastSync={lastSync}
              cadenceMs={cadenceMs}
            />
          </div>
        </div>

        {offline && data && (
          <div className="alert-warning ledger-alert">
            <WifiOff className="h-4 w-4 shrink-0" strokeWidth={1.8} />
            <span className="min-w-0">
              {t("dashboard.connectionLost", {
                time: lastSync ? localeTime(lastSync, locale) : "—",
              })}
            </span>
            <button
              type="button"
              className="button-secondary button-compact ml-auto"
              onClick={refresh}
            >
              <RotateCw className="h-3.5 w-3.5" strokeWidth={1.8} />
              {t("common.retryNow")}
            </button>
          </div>
        )}

        {actionError && (
          <div className="alert-error ledger-alert">
            <span className="min-w-0 break-words">{actionError}</span>
            <button
              type="button"
              aria-label={t("common.dismissError")}
              className="button-ghost button-compact shrink-0 px-2"
              onClick={() => setActionError(null)}
            >
              <X className="h-3.5 w-3.5" strokeWidth={1.8} />
            </button>
          </div>
        )}

        {booting ? (
          <div className="run-ledger">
            <RowSkeleton />
            <RowSkeleton />
            <RowSkeleton />
            <RowSkeleton />
          </div>
        ) : data === null ? (
          <div className="ledger-state-stack">
            <div className="alert-error ledger-alert">
              <WifiOff className="h-4 w-4 shrink-0" strokeWidth={1.8} />
              <span className="min-w-0 break-words">
                <strong>{t("dashboard.engineUnavailable")}</strong>{" "}
                {fetchError ?? t("common.error")} · {t("dashboard.polling", { endpoint: apiURL("/api/runs") })}
              </span>
              <button
                type="button"
                className="button-secondary button-compact ml-auto"
                onClick={refresh}
              >
                <RotateCw className="h-3.5 w-3.5" strokeWidth={1.8} />
                {t("common.retry")}
              </button>
            </div>
            <EmptyState
              title={t("dashboard.waiting")}
              hint={t("dashboard.waiting.hint")}
            />
          </div>
        ) : sorted.length === 0 ? (
          <EmptyState
            title={t("dashboard.noRuns")}
            hint={t("dashboard.noRuns.hint")}
            action={
              <Link href="/scan" className="button-primary button-compact">
                <Plus className="h-3.5 w-3.5" strokeWidth={2} />
                {t("dashboard.newScan")}
              </Link>
            }
          />
        ) : filtered.length === 0 ? (
          <EmptyState
            title={t("dashboard.noMatch")}
            hint={q ? t("dashboard.noMatch.hint") : t("dashboard.noTypeMatch.hint")}
            action={
              filtersActive ? (
                <button
                  type="button"
                  className="button-ghost button-compact"
                  onClick={clearFilters}
                >
                  {t("dashboard.clearFilters")}
                </button>
              ) : undefined
            }
          />
        ) : (
          <div className="run-ledger">
            {pageRows.map((run) => {
              const type = typeOf(run);
              const runSevTotal = SEV_KEYS.reduce(
                (s, k) => s + sevCount(run.severity, k),
                0
              );
              const isDeleting = deleting === run.name;
              const interrupted =
                !run.live &&
                (run.status || "").toLowerCase() === "failed" &&
                (run.failure_reason || "").toLowerCase() === "interrupted";
              const pillStatus = pillStatusOf(run);
              return (
                <article
                  key={run.name}
                  className="run-ledger-row"
                >
                  <Link
                    href={`/run?name=${encodeURIComponent(run.name)}`}
                    className="run-ledger-main"
                  >
                    <div className="run-ledger-status">
                      <StatusPill
                        status={pillStatus}
                        label={statusLabel(pillStatus)}
                        live={run.live}
                      />
                      {run.stale && <Chip tone="warning">{statusLabel("stale")}</Chip>}
                    </div>

                    <div className="run-ledger-identity">
                      <div className="run-ledger-target">
                        <strong title={runTargets(run).join("\n") || run.name}>{runTargetLabel(run)}</strong>
                        <Chip
                          tone={
                            type === "internal"
                              ? "violet"
                              : type === "web"
                                ? "accent"
                            : "neutral"
                          }
                        >
                          {type === "web" || type === "internal" ? t(`dashboard.filter.${type}`) : run.scan_type || statusLabel("unknown")}
                        </Chip>
                      </div>
                      <div className="run-ledger-meta">
                        <MicroLabel>{run.name}</MicroLabel>
                        <span>{t("dashboard.started", { time: localeRelativeTime(run.start_time, locale) })}</span>
                        <span>{t("dashboard.duration", { duration: runDuration(run, nowMs) })}</span>
                      </div>
                      {run.failure_reason && (
                        <div className={cn("run-failure", interrupted && "run-failure-stopped")}
                          title={run.failure_reason}
                        >
                          {interrupted
                            ? t("dashboard.stoppedSignal")
                            : t("dashboard.failedReason", { reason: run.failure_reason })}
                        </div>
                      )}
                    </div>

                    <div className="run-ledger-severity">
                      {SEV_KEYS.filter((k) => sevCount(run.severity, k) > 0).map(
                        (k) => (
                          <span key={k}>
                            <SeverityChip severity={k.toUpperCase()} />
                            <b>{sevCount(run.severity, k)}</b>
                          </span>
                        )
                      )}
                      {run.internal_finding_count > 0 && (
                        <Chip tone="neutral">
                          {t("dashboard.internalFindings", { n: run.internal_finding_count })}
                        </Chip>
                      )}
                      {runSevTotal === 0 && run.internal_finding_count === 0 && (
                        <span className="run-no-findings">{t("dashboard.noFindings")}</span>
                      )}
                    </div>
                    <ChevronRight className="run-ledger-chevron" aria-hidden="true" />
                  </Link>

                  <div className="run-ledger-actions">
                    <Link
                      href={`/run?name=${encodeURIComponent(run.name)}`}
                      className="button-secondary button-compact"
                    >
                      {t("dashboard.open")}
                    </Link>
                    {run.live ? (
                      <button
                        type="button"
                        className="button-danger button-compact"
                        disabled
                        title={t("dashboard.live.protect")}
                      >
                        {t("common.delete")}
                      </button>
                    ) : isDeleting ? (
                      <span className="button-danger button-compact run-deleting">
                        <Spinner className="h-3 w-3" />
                        {t("dashboard.deleting")}
                      </span>
                    ) : (
                      <span className="run-confirm-delete">
                        <ConfirmButton
                          label={t("common.delete")}
                          confirmLabel={t("common.confirmDelete")}
                          danger
                          onConfirm={() => void handleDelete(run.name)}
                        />
                      </span>
                    )}
                  </div>
                </article>
              );
            })}

            <div className="ledger-pagination">
              <span className="ledger-pagination-summary">
                {t("dashboard.showing", { start: pageStart + 1, end: pageEnd, total: filtered.length })}
              </span>
              <div className="ledger-pagination-controls">
                <label>
                  {t("common.rows")}
                  <Select
                    className="select-shell"
                    value={String(pageSize)}
                    onValueChange={(value) => setPageSize(Number(value))}
                    aria-label={t("common.rows")}
                    options={PAGE_SIZES.map((size) => ({ value: String(size), label: size }))}
                  />
                </label>
                <span className="ledger-page-count">
                  {t("dashboard.page", { current: safePage + 1, total: pageCount })}
                </span>
                <button
                  type="button"
                  className="button-secondary button-compact"
                  disabled={safePage === 0}
                  onClick={() => setPageNum(safePage - 1)}
                  aria-label={t("common.previous")}
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                  <span>{t("common.previous")}</span>
                </button>
                <button
                  type="button"
                  className="button-secondary button-compact"
                  disabled={safePage >= pageCount - 1}
                  onClick={() => setPageNum(safePage + 1)}
                  aria-label={t("common.next")}
                >
                  <span>{t("common.next")}</span>
                  <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
