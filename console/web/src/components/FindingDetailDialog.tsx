"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ExternalLink, RefreshCw, X } from "lucide-react";
import { SeverityChip, Spinner } from "@/components/ui";
import { MarkdownViewToggle, RawMarkdown, type MarkdownViewMode } from "@/components/MarkdownView";
import { apiURL, responseText } from "@/lib/api";
import type { InternalFinding, Vulnerability } from "@/lib/api";
import { authFetch } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";

function severityOf(severity: string | undefined): string {
  const key = (severity || "INFO").toUpperCase();
  return ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"].includes(key) ? key : "INFO";
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

/* -------------------------------------------------------------------- modal */

export type ActiveFinding =
  | { kind: "vuln"; v: Vulnerability }
  | { kind: "internal"; f: InternalFinding };

type MarkdownArtifact = { key: string; phase: "loading" | "ready" | "error"; content: string };

function useFindingMarkdown(path: string, enabled: boolean, revision = "") {
  const key = JSON.stringify([path, revision]);
  const [artifact, setArtifact] = React.useState<MarkdownArtifact | null>(null);
  const [attempt, retry] = React.useReducer(value => value + 1, 0);
  React.useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setArtifact({ key, phase: "loading", content: "" });
    const timer = window.setTimeout(() => {
      controller.abort();
      setArtifact({ key, phase: "error", content: "" });
    }, 20_000);
    void authFetch(apiURL(path), { signal: controller.signal })
      .then(async response => {
        if (!response.ok) throw new Error("markdown_unavailable");
        return responseText(response, controller.signal);
      })
      .then(content => {
        if (!controller.signal.aborted) setArtifact({ key, phase: "ready", content });
      })
      .catch(() => {
        if (!controller.signal.aborted) setArtifact({ key, phase: "error", content: "" });
      })
      .finally(() => window.clearTimeout(timer));
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [path, key, enabled, attempt]);
  return { phase: artifact?.key === key ? artifact.phase : "loading", content: artifact?.key === key ? artifact.content : "", retry };
}

function FindingMarkdown({ artifact, raw }: { artifact: ReturnType<typeof useFindingMarkdown>; raw: boolean }) {
  const { t, locale } = useI18n();
  if (artifact.phase === "loading") return <div className="flex items-center gap-2 py-6 text-sm text-fg-muted" role="status"><Spinner />{t("findings.detail.loading")}</div>;
  if (artifact.phase === "error") return <div className="space-y-3" role="alert">
    <p className="text-sm text-fg-muted">{locale === "en" ? "Could not load the Markdown source. Retry or open the .md file." : "无法加载 Markdown 原文。请重试或打开 .md 文件。"}</p>
    <button type="button" className="button-secondary button-compact" onClick={artifact.retry}><RefreshCw size={14} aria-hidden="true" />{t("common.retry")}</button>
  </div>;
  return raw ? <RawMarkdown content={artifact.content} /> : <div className="prose-report"><ReactMarkdown remarkPlugins={[remarkGfm]}>{artifact.content}</ReactMarkdown></div>;
}

export function FindingDetailDialog({
  active,
  name,
  onClose,
  showSource = false,
}: {
  active: ActiveFinding;
  name: string;
  onClose: () => void;
  showSource?: boolean;
}) {
  const closeRef = React.useRef<HTMLButtonElement | null>(null);
  const previousFocus = React.useRef<HTMLElement | null>(null);
  const identity = JSON.stringify([name, active.kind, active.kind === "vuln" ? active.v.id : active.f.id]);

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm" />
        <Dialog.Content
          className="panel fixed left-1/2 top-1/2 z-50 flex max-h-[85dvh] w-[calc(100vw-2rem)] max-w-3xl -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden outline-none sm:w-[calc(100vw-3rem)]"
          style={{ position: "fixed" }}
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
            closeRef.current?.focus({ preventScroll: true });
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (previousFocus.current?.isConnected) previousFocus.current.focus({ preventScroll: true });
          }}
        >
          {active.kind === "vuln" ? (
            <VulnModalBody key={identity} vuln={active.v} name={name} onClose={onClose} closeRef={closeRef} showSource={showSource} />
          ) : (
            <InternalModalBody key={identity} finding={active.f} name={name} onClose={onClose} closeRef={closeRef} showSource={showSource} />
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function ModalHeader({
  chip,
  title,
  meta,
  onClose,
  closeRef,
  artifactHref,
  sourceRun,
}: {
  chip: React.ReactNode;
  title: string;
  meta: React.ReactNode;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement>;
  artifactHref: string;
  sourceRun?: string;
}) {
  const { t, locale } = useI18n();
  return (
    <div className="flex shrink-0 items-start gap-3 border-b border-line/6 px-5 py-4">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2.5">
          {chip}
          <Dialog.Title asChild>
            <h3 className="min-w-0 truncate font-mono text-base font-bold" style={{ color: "var(--text-heading)" }}>
              {title}
            </h3>
          </Dialog.Title>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {sourceRun && <MetaChip label={locale === "en" ? "Source task" : "来源任务"} value={sourceRun} />}
          {meta}
        </div>
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
  showSource,
}: {
  vuln: Vulnerability;
  name: string;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement>;
  showSource: boolean;
}) {
  const { t, locale } = useI18n();
  const en = locale === "en";
  const [view, setView] = React.useState<MarkdownViewMode>("preview");
  const artifactPath = `/api/runs/${encodeURIComponent(name)}/artifacts/vulnerabilities/${encodeURIComponent(vuln.id)}.md`;
  const artifact = useFindingMarkdown(artifactPath, view === "raw", vuln.updated_at || vuln.timestamp || "");
  const history = Array.isArray(vuln.update_history) ? vuln.update_history : [];
  const locations = Array.isArray(vuln.code_locations) ? vuln.code_locations : [];
  return (
    <>
      <ModalHeader
        chip={<SeverityChip severity={severityOf(vuln.severity)} />}
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
        artifactHref={apiURL(artifactPath)}
        sourceRun={showSource ? name : undefined}
      />
      <div className="flex shrink-0 items-center justify-end border-b border-line/6 px-5 py-2">
        <MarkdownViewToggle value={view} onChange={setView} />
      </div>
      <div className="min-h-0 min-w-0 space-y-4 overflow-y-auto px-5 py-4">
        {view === "raw" ? <FindingMarkdown artifact={artifact} raw /> : <>
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
        </>}
      </div>
    </>
  );
}

function InternalModalBody({
  finding,
  name,
  onClose,
  closeRef,
  showSource,
}: {
  finding: InternalFinding;
  name: string;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement>;
  showSource: boolean;
}) {
  const { t } = useI18n();
  const [view, setView] = React.useState<MarkdownViewMode>("preview");
  const artifactPath = `/api/runs/${encodeURIComponent(name)}/artifacts/internal_findings/${encodeURIComponent(finding.id)}.md`;
  const artifact = useFindingMarkdown(artifactPath, true);

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
        artifactHref={apiURL(artifactPath)}
        sourceRun={showSource ? name : undefined}
      />
      <div className="flex shrink-0 items-center justify-end border-b border-line/6 px-5 py-2">
        <MarkdownViewToggle value={view} onChange={setView} />
      </div>
      <div className="min-h-0 min-w-0 overflow-y-auto px-5 py-4">
        <FindingMarkdown artifact={artifact} raw={view === "raw"} />
      </div>
    </>
  );
}
