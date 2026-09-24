"use client";

/* ============================================================================
   FindingsPanel — vulnerabilities + internal findings of the run cockpit.

   Header: counts, severity filter chips (gold active state), text search.
   Rows open a FLOATING detail modal (Esc/overlay close): vulnerabilities
   render their full structured fields with the PoC in a terminal
   block; internal findings fetch and render their .md artifact. Client-side
   CSV export mirrors the engine's vulnerabilities.csv columns.
   ========================================================================= */

import * as React from "react";
import { ChevronDown, Download, RefreshCw, Search } from "lucide-react";
import { EmptyState, SeverityChip } from "@/components/ui";
import { getJSON } from "@/lib/api";
import type { FindingsPage, InternalFinding, RunDetail, Vulnerability } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { FindingDetailDialog, type ActiveFinding } from "@/components/FindingDetailDialog";

const FINDINGS_POLL_MS = 6000;
const SEV_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"] as const;
type SevKey = (typeof SEV_ORDER)[number];

function sevKeyOf(severity: string | undefined): SevKey {
  const key = (severity || "INFO").toUpperCase();
  return (SEV_ORDER as readonly string[]).includes(key) ? (key as SevKey) : "INFO";
}

/* ---------------------------------------------------------------- CSV export */

function csvCell(value: string): string {
  const guarded = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return `"${guarded.replace(/"/g, '""')}"`;
}

function exportCsv(name: string, vulnerabilities: Vulnerability[]): void {
  const header = "id,title,severity,timestamp,file";
  const rows = vulnerabilities.map((v) =>
    [
      csvCell(v.id ?? ""),
      csvCell(v.title ?? ""),
      csvCell(String(v.severity ?? "info").toUpperCase()),
      csvCell(v.timestamp ?? ""),
      csvCell(`vulnerabilities/${v.id}.md`),
    ].join(",")
  );
  const blob = new Blob([[header, ...rows].join("\r\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${name}-vulnerabilities.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function detailValue(value: unknown): string {
  if (value == null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/* ------------------------------------------------------------ vulnerability */

function VulnerabilityRow({ vuln, onOpen }: { vuln: Vulnerability; onOpen: () => void }) {
  const sev = sevKeyOf(vuln.severity);
  return (
    <button
      type="button"
      className="flex w-full items-center gap-2.5 rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-3 text-left transition-colors hover:border-line/12 hover:bg-raised/4"
      onClick={(event) => { event.currentTarget.focus({ preventScroll: true }); onOpen(); }}
    >
      <SeverityChip severity={sev} />
      <span className="min-w-0 flex-1 truncate text-sm font-medium text-fg">
        {vuln.title || vuln.id}
      </span>
      {typeof vuln.cvss === "number" && (
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-muted">
          CVSS {vuln.cvss.toFixed(1)}
        </span>
      )}
      {(vuln.endpoint || vuln.target) && (
        <span className="hidden max-w-[16rem] shrink-0 truncate font-mono text-[10px] text-fg-faint md:inline">
          {vuln.method ? `${vuln.method} ` : ""}
          {vuln.endpoint || vuln.target}
        </span>
      )}
      <ChevronDown className="h-4 w-4 shrink-0 -rotate-90 text-fg-muted" />
    </button>
  );
}

/* -------------------------------------------------------------------- panel */

export default function FindingsPanel({ name, run }: { name: string; run: RunDetail | null }) {
  const { t } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = React.useState("");
  const [data, setData] = React.useState<FindingsPage | null>(null);
  const [sevFilter, setSevFilter] = React.useState<SevKey | "ALL">("ALL");
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState<ActiveFinding | null>(null);
  const live = Boolean(run?.live);

  React.useEffect(() => { setActive(null); }, [name]);

  const load = React.useCallback(async () => {
    try {
      const page = await getJSON<FindingsPage & { read_warnings?: string[] }>(`/api/runs/${encodeURIComponent(name)}/findings`);
      setData(page);
      setError(Array.isArray(page.read_warnings) ? page.read_warnings.filter((warning) => typeof warning === "string").join(" ") : "");
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
    }, FINDINGS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, live]);

  /* ---- derived data (hooks must run before any early return) ---- */
  const vulnerabilities = data?.vulnerabilities ?? [];
  const internal = data?.internal ?? [];
  const q = query.trim().toLowerCase();

  const counts = React.useMemo(() => {
    const tally: Record<string, number> = { ALL: vulnerabilities.length };
    for (const v of vulnerabilities) {
      const key = sevKeyOf(v.severity);
      tally[key] = (tally[key] ?? 0) + 1;
    }
    return tally;
  }, [vulnerabilities]);

  const filteredVulns = React.useMemo(
    () =>
      vulnerabilities.filter((v) => {
        if (sevFilter !== "ALL" && sevKeyOf(v.severity) !== sevFilter) return false;
        if (!q) return true;
        return (
          (v.title || "").toLowerCase().includes(q) ||
          (v.id || "").toLowerCase().includes(q) ||
          (v.endpoint || "").toLowerCase().includes(q) ||
          (v.target || "").toLowerCase().includes(q) ||
          (v.cve || "").toLowerCase().includes(q) ||
          (v.finding_class || "").toLowerCase().includes(q) ||
          detailValue(v.dependency_metadata).toLowerCase().includes(q)
        );
      }),
    [vulnerabilities, sevFilter, q]
  );

  const groups = React.useMemo(() => {
    const matching = q
      ? internal.filter((f) =>
          `${f.title ?? ""} ${f.finding_type ?? ""} ${f.host ?? ""} ${f.id}`
            .toLowerCase()
            .includes(q)
        )
      : internal;
    const map = new Map<string, InternalFinding[]>();
    for (const f of matching) {
      const key = f.finding_type || "general";
      const list = map.get(key) || [];
      list.push(f);
      map.set(key, list);
    }
    return [...map.entries()];
  }, [internal, q]);

  if (phase === "loading") {
    return (
      <div className="space-y-2 p-4">
        {[0, 1, 2].map((i) => (
          <div key={i} className="rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-3">
            <div className="flex items-center gap-2.5">
              <div className="skeleton-line h-3.5 w-16" />
              <div className="skeleton-line flex-1" />
              <div className="skeleton-line w-12" />
            </div>
          </div>
        ))}
        <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
          {t("findings.loading")}
        </p>
      </div>
    );
  }

  if (phase === "error" || (error && vulnerabilities.length === 0 && internal.length === 0)) {
    return (
      <div className="space-y-3 p-4">
        <div className="alert-error" role="alert">
          {t("findings.error", { error })}
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

  if (vulnerabilities.length === 0 && internal.length === 0) {
    return (
      <div className="p-4">
        <EmptyState
          title={live ? t("findings.empty.live.title") : t("findings.empty.done.title")}
          hint={
            live
              ? t("findings.empty.live.hint")
              : t("findings.empty.done.hint")
          }
        />
      </div>
    );
  }

  return (
    <div className="space-y-4 p-4">
      {error && <div className="alert-error" role="alert">{error}</div>}
      {/* header: counts + filters + search */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="micro-label">
          {t("findings.summary", {
            vulnerabilities: vulnerabilities.length,
            internal: internal.length,
          })}
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          {SEV_ORDER.map((sev) =>
            counts[sev] ? (
              <button
                key={sev}
                type="button"
                className={`filter-chip min-h-8 px-3 text-[10px] ${
                  sevFilter === sev ? "filter-chip-active" : ""
                }`}
                onClick={() => setSevFilter(sevFilter === sev ? "ALL" : sev)}
              >
                {sev} · {counts[sev]}
              </button>
            ) : null
          )}
          {sevFilter !== "ALL" && (
            <button
              type="button"
              className="button-ghost min-h-8 px-2 text-[10px] uppercase tracking-[0.14em]"
              onClick={() => setSevFilter("ALL")}
            >
              {t("findings.clearFilters")}
            </button>
          )}
        </div>
        <div className="relative ml-auto w-full max-w-xs sm:w-64">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted" />
          <input
            className="input-shell min-h-9 pl-8 py-1.5 text-xs"
            placeholder={t("findings.search")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={t("findings.search")}
          />
        </div>
        <button
          type="button"
          className="button-secondary button-compact"
          onClick={() => exportCsv(name, vulnerabilities)}
          disabled={vulnerabilities.length === 0}
        >
          <Download className="h-3.5 w-3.5" />
          {t("findings.exportCsv")}
        </button>
      </div>

      {/* vulnerabilities */}
      {filteredVulns.length > 0 ? (
        <div className="space-y-2">
          {filteredVulns.map((v) => (
            <VulnerabilityRow key={v.id} vuln={v} onOpen={() => setActive({ kind: "vuln", v })} />
          ))}
        </div>
      ) : vulnerabilities.length > 0 ? (
        <EmptyState
          title={t("findings.filtered.empty.title")}
          hint={t("findings.filtered.empty.hint")}
        />
      ) : null}

      {/* internal findings grouped by type */}
      {groups.length > 0 && (
        <div className="space-y-3">
          <div className="section-divider" />
          {groups.map(([type, items]) => (
            <div key={type} className="space-y-2">
              <div className="flex items-center gap-2">
                <span className="mono-chip chip-warning py-0.5 text-[9px]">{type}</span>
                <span className="micro-label text-[9px]">
                  {t("findings.internal.count", { n: items.length })}
                </span>
              </div>
              {items.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className="flex w-full items-center gap-2.5 rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-2.5 text-left transition-colors hover:border-warning/24 hover:bg-warning/6"
                  onClick={(event) => { event.currentTarget.focus({ preventScroll: true }); setActive({ kind: "internal", f }); }}
                >
                  {f.severity ? (
                    <SeverityChip severity={f.severity} />
                  ) : (
                    <span className="mono-chip py-px text-[9px]">
                      {t("findings.internal")}
                    </span>
                  )}
                  <span className="min-w-0 flex-1 truncate text-sm text-fg">
                    {f.title || f.id}
                  </span>
                  {f.host && (
                    <span className="shrink-0 font-mono text-[10px] text-fg-faint">{f.host}</span>
                  )}
                  <ChevronDown className="h-3.5 w-3.5 shrink-0 -rotate-90 text-fg-faint" />
                </button>
              ))}
            </div>
          ))}
        </div>
      )}

      {q && filteredVulns.length === 0 && groups.length === 0 && (
        <EmptyState
          title={t("findings.search.empty.title")}
          hint={t("findings.search.empty.hint", { query })}
        />
      )}

      {/* floating detail window */}
      {active && (
        <FindingDetailDialog
          active={active.kind === "vuln" ? { kind: "vuln", v: vulnerabilities.find((v) => v.id === active.v.id) ?? active.v } : active}
          name={name}
          onClose={() => setActive(null)}
        />
      )}
    </div>
  );
}
