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
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronDown, Download, ExternalLink, RefreshCw, Search, X } from "lucide-react";
import { EmptyState, SeverityChip, Spinner } from "@/components/ui";
import { apiURL, getJSON } from "@/lib/api";
import type { FindingsPage, InternalFinding, RunDetail, Vulnerability } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

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

/* ---------------------------------------------------------- field subsection */

function Field({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  if (!value) return null;
  return (
    <div>
      <div className="micro-label text-[9px]">{label}</div>
      <p
        className={`mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-fg-2 ${
          mono ? "font-mono" : ""
        }`}
      >
        {value}
      </p>
    </div>
  );
}

function MetaChip({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <span className="mono-chip min-w-0 max-w-full text-[9px]" title={`${label}: ${value}`}>
      <span className="shrink-0 text-fg-faint">{label}</span>
      <span className="truncate">{value}</span>
    </span>
  );
}

function detailValue(value: unknown): string {
  if (value == null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/** Preserve optional structured fields without collapsing objects to [object Object]. */
function DetailRecord({ value }: { value: Record<string, unknown> }) {
  if (!value || typeof value !== "object") return <p className="text-xs text-fg-2">{detailValue(value)}</p>;
  return (
    <dl className="grid gap-2 text-xs sm:grid-cols-[minmax(7rem,auto)_1fr]">
      {Object.entries(value).filter(([, item]) => item != null && item !== "").map(([key, item]) => (
        <React.Fragment key={key}>
          <dt className="break-words font-mono text-[10px] text-fg-muted">{key.replaceAll("_", " ")}</dt>
          <dd className="min-w-0 whitespace-pre-wrap break-words font-mono text-fg-2">{detailValue(item)}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}

/* ------------------------------------------------------------ vulnerability */

function VulnerabilityRow({ vuln, onOpen }: { vuln: Vulnerability; onOpen: () => void }) {
  const sev = sevKeyOf(vuln.severity);
  return (
    <button
      type="button"
      className="flex w-full items-center gap-2.5 rounded-[1rem] border border-line/6 bg-surface/42 px-3.5 py-3 text-left transition-colors hover:border-line/12 hover:bg-raised/4"
      onClick={onOpen}
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

/* -------------------------------------------------------------------- modal */

type ActiveFinding =
  | { kind: "vuln"; v: Vulnerability }
  | { kind: "internal"; f: InternalFinding };

function FindingModal({
  active,
  name,
  onClose,
}: {
  active: ActiveFinding;
  name: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const closeRef = React.useRef<HTMLButtonElement | null>(null);

  React.useEffect(() => {
    closeRef.current?.focus();
  }, []);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isVuln = active.kind === "vuln";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm sm:p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={t("findings.detail.dialog")}
    >
      <div
        className="panel flex max-h-[85vh] w-full max-w-3xl animate-enter flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {isVuln ? (
          <VulnModalBody vuln={active.v} name={name} onClose={onClose} closeRef={closeRef} />
        ) : (
          <InternalModalBody finding={active.f} name={name} onClose={onClose} closeRef={closeRef} />
        )}
      </div>
    </div>
  );
}

function ModalHeader({
  chip,
  title,
  meta,
  onClose,
  closeRef,
  artifactHref,
}: {
  chip: React.ReactNode;
  title: string;
  meta: React.ReactNode;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement>;
  artifactHref: string;
}) {
  const { t } = useI18n();
  return (
    <div className="flex items-start gap-3 border-b border-line/6 px-5 py-4">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2.5">
          {chip}
          <h3 className="min-w-0 truncate font-mono text-base font-bold" style={{ color: "var(--text-heading)" }}>
            {title}
          </h3>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">{meta}</div>
      </div>
      <a
        className="inline-flex items-center gap-1 font-mono text-[10px] uppercase tracking-[0.14em] text-accent transition-colors hover:text-accent-hover"
        href={artifactHref}
        target="_blank"
        rel="noreferrer"
      >
        <ExternalLink className="h-3 w-3" />
        .md
      </a>
      <button
        ref={closeRef}
        type="button"
        className="rounded-lg p-1.5 text-fg-muted transition-colors hover:bg-raised/6 hover:text-fg"
        onClick={onClose}
        aria-label={t("findings.detail.close")}
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

function VulnModalBody({
  vuln,
  name,
  onClose,
  closeRef,
}: {
  vuln: Vulnerability;
  name: string;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement>;
}) {
  const { t, locale } = useI18n();
  const en = locale === "en";
  const history = Array.isArray(vuln.update_history) ? vuln.update_history : [];
  const locations = Array.isArray(vuln.code_locations) ? vuln.code_locations : [];
  return (
    <>
      <ModalHeader
        chip={<SeverityChip severity={sevKeyOf(vuln.severity)} />}
        title={vuln.title || vuln.id}
        meta={
          <>
            <MetaChip label={t("findings.meta.id")} value={vuln.id ?? ""} />
            <MetaChip
              label={t("findings.meta.cvss")}
              value={typeof vuln.cvss === "number" ? vuln.cvss.toFixed(1) : ""}
            />
            <MetaChip label={t("findings.meta.vector")} value={vuln.cvss_vector ?? ""} />
            <MetaChip label={t("findings.meta.target")} value={vuln.target ?? ""} />
            <MetaChip label={t("findings.meta.endpoint")} value={vuln.endpoint ?? ""} />
            <MetaChip label={t("findings.meta.method")} value={vuln.method ?? ""} />
            <MetaChip label={t("findings.meta.cve")} value={vuln.cve ?? ""} />
            <MetaChip label={t("findings.meta.cwe")} value={vuln.cwe ?? ""} />
            <MetaChip label={t("findings.meta.confidence")} value={vuln.confidence ?? ""} />
            <MetaChip label={en ? "Class" : "发现类别"} value={vuln.finding_class ?? ""} />
            <MetaChip label={en ? "Fix effort" : "修复工作量"} value={vuln.fix_effort ?? ""} />
            <MetaChip label={en ? "Author" : "记录者"} value={vuln.agent_name || vuln.discovered_by_agent_name || vuln.agent_id || vuln.discovered_by_agent || ""} />
            <MetaChip label={en ? "Updated" : "更新时间"} value={vuln.updated_at ?? ""} />
          </>
        }
        onClose={onClose}
        closeRef={closeRef}
        artifactHref={apiURL(
          `/api/runs/${encodeURIComponent(name)}/artifacts/vulnerabilities/${vuln.id}.md`
        )}
      />
      <div className="space-y-4 overflow-y-auto px-5 py-4">
        <Field label={t("findings.field.description")} value={vuln.description ?? ""} />
        <Field label={t("findings.field.impact")} value={vuln.impact ?? ""} />
        <Field label={en ? "Confidence rationale" : "置信度依据"} value={vuln.confidence_rationale || vuln.confidence || ""} />
        <Field label={en ? "Severity change conditions" : "严重性变化条件"} value={vuln.severity_change_conditions ?? ""} />
        <Field
          label={t("findings.field.technicalAnalysis")}
          value={vuln.technical_analysis ?? ""}
        />
        {(vuln.poc_description || vuln.poc_script_code) && (
          <div>
            <div className="micro-label text-[9px]">
              {t("findings.field.proofOfConcept")}
            </div>
            {vuln.poc_description && (
              <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-fg-2">
                {vuln.poc_description}
              </p>
            )}
            {vuln.poc_script_code && (
              <div className="terminal mt-2">
                <div className="terminal-bar">
                  <span className="terminal-bar-dot" aria-hidden />
                  {vuln.poc_language ? `PoC · ${vuln.poc_language}` : "PoC"}
                </div>
                <pre className="terminal-body">{vuln.poc_script_code}</pre>
              </div>
            )}
          </div>
        )}
        <Field
          label={t("findings.field.remediation")}
          value={vuln.remediation_steps ?? ""}
        />
        {locations.length > 0 && (
          <section className="space-y-2" aria-label={en ? "Code locations" : "代码位置"}>
            <div className="micro-label text-[9px]">{en ? "Code locations and suggested fixes" : "代码位置与修复建议"}</div>
            {locations.map((location, index) => (
              <div key={index} className="rounded-xl border border-line/8 bg-surface/42 p-3">
                <DetailRecord value={location} />
              </div>
            ))}
          </section>
        )}
        <Field label={en ? "Fix verification" : "修复验证"} value={vuln.fix_verification ?? ""} />
        <Field label={en ? "Suggested pull request description" : "建议的修复 PR 描述"} value={vuln.fix_pr_body ?? ""} />
        {vuln.dependency_metadata && Object.keys(vuln.dependency_metadata).length > 0 && (
          <section className="space-y-2" aria-label={en ? "Dependency metadata" : "依赖元数据"}>
            <div className="micro-label text-[9px]">{en ? "Dependency metadata" : "依赖元数据"}</div>
            <div className="rounded-xl border border-line/8 bg-surface/42 p-3">
              <DetailRecord value={vuln.dependency_metadata} />
            </div>
          </section>
        )}
        <Field label={t("findings.field.evidence")} value={vuln.evidence ?? ""} mono />
        <Field
          label={t("findings.field.counterevidence")}
          value={String(vuln.counterevidence ?? "")}
        />
        <Field
          label={t("findings.field.assumptions")}
          value={String(vuln.assumptions ?? "")}
        />
        {history.length > 0 && (
          <details className="rounded-xl border border-line/8 p-3">
            <summary className="cursor-pointer text-xs font-medium text-fg-2">
              {en ? "Revision history" : "修订历史"} · {history.length}
            </summary>
            <ol className="mt-3 space-y-3">
              {history.map((revision, index) => (
                <li key={index} className="border-t border-line/6 pt-3"><DetailRecord value={revision} /></li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </>
  );
}

function InternalModalBody({
  finding,
  name,
  onClose,
  closeRef,
}: {
  finding: InternalFinding;
  name: string;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement>;
}) {
  const { t } = useI18n();
  const [markdown, setMarkdown] = React.useState<string | null>(null);

  React.useEffect(() => {
    let disposed = false;
    fetch(
      apiURL(`/api/runs/${encodeURIComponent(name)}/artifacts/internal_findings/${finding.id}.md`)
    )
      .then(async (res) =>
        res.ok
          ? res.text()
          : `# ${finding.title || finding.id}\n\n(${t("findings.artifact.unreadable")})`
      )
      .then((text) => {
        if (!disposed) setMarkdown(text);
      })
      .catch(() => {
        if (!disposed) {
          setMarkdown(
            `# ${finding.title || finding.id}\n\n(${t("findings.artifact.unavailable")})`
          );
        }
      });
    return () => {
      disposed = true;
    };
  }, [name, finding.id, finding.title, t]);

  return (
    <>
      <ModalHeader
        chip={
          finding.severity ? (
            <SeverityChip severity={finding.severity} />
          ) : (
            <span className="mono-chip chip-warning py-px text-[9px]">
              {t("findings.internal")}
            </span>
          )
        }
        title={finding.title || finding.id}
        meta={
          <>
            <MetaChip label={t("findings.meta.type")} value={finding.finding_type ?? ""} />
            <MetaChip label={t("findings.meta.host")} value={finding.host ?? ""} />
            <MetaChip label={t("findings.meta.id")} value={finding.id} />
          </>
        }
        onClose={onClose}
        closeRef={closeRef}
        artifactHref={apiURL(
          `/api/runs/${encodeURIComponent(name)}/artifacts/internal_findings/${finding.id}.md`
        )}
      />
      <div className="overflow-y-auto px-5 py-4">
        {markdown === null ? (
          <div className="flex items-center gap-2 py-6 text-sm text-fg-muted">
            <Spinner /> {t("findings.detail.loading")}
          </div>
        ) : (
          <div className="prose-report">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{markdown}</ReactMarkdown>
          </div>
        )}
      </div>
    </>
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
                  onClick={() => setActive({ kind: "internal", f })}
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
        <FindingModal
          active={active.kind === "vuln" ? { kind: "vuln", v: vulnerabilities.find((v) => v.id === active.v.id) ?? active.v } : active}
          name={name}
          onClose={() => setActive(null)}
        />
      )}
    </div>
  );
}
