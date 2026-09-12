"use client";

import * as React from "react";
import Link from "next/link";
import * as Dialog from "@radix-ui/react-dialog";
import * as Tabs from "@radix-ui/react-tabs";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUpRight, Download, FileText, History, RefreshCw, ShieldAlert, X } from "lucide-react";
import { Select } from "@/components/Select";
import { getJSON, type ProjectReportVersion } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./ProjectReportReader.module.css";

const TEXT = {
  "zh-CN": {
    title: "项目总报告", close: "关闭报告", version: "报告版本", summary: "摘要", full: "完整报告",
    sources: "来源任务", download: "下载 Markdown", loading: "正在加载报告…", retry: "重试",
    error: "无法加载此版本，请重试。", corrupt: "此版本未通过完整性检查，无法读取或下载。",
    unavailable: "此版本尚不可读取。", snapshot: "已保存快照", outdated: "来源已更新", current: "来源一致",
    staleHint: "此处保留生成时的快照，不包含之后的变化。", risk: "风险分布", unique: "去重漏洞",
    occurrences: "记录次数", internal: "内网发现", targets: "覆盖目标", keyFindings: "关键发现",
    coverage: "任务覆盖", included: "已纳入", excluded: "未纳入", total: "生成时任务数",
    openSources: "查看来源任务", noFindings: "此快照没有已验证漏洞。", allFindings: "查看全部发现",
    legacyFindings: "此版本未保存结构化发现摘要。", legacyStats: "此版本未保存风险统计。",
    viewFull: "阅读完整报告", source: "来源", sourceReport: "打开任务报告", noExcluded: "所有任务均已纳入。",
    noSources: "此版本没有来源任务记录。", provenance: "快照校验信息", reportHash: "报告 SHA-256",
    snapshotHash: "来源快照 SHA-256", missingReport: "缺少最终报告", running: "运行中", failed: "任务失败",
    crashed: "任务异常退出", stopped: "已停止", queued: "等待中", reporting: "报告生成中", unknown: "未完成",
    newSources: "新增来源任务", changedSources: "来源内容变更", removedSources: "来源任务不可用", projectChanged: "项目设置变更",
    task: "任务", findings: "漏洞", noContent: "此版本的报告正文为空。", showSources: "查看来源",
  },
  en: {
    title: "Project report", close: "Close report", version: "Report version", summary: "Summary", full: "Full report",
    sources: "Source tasks", download: "Download Markdown", loading: "Loading report…", retry: "Retry",
    error: "Could not load this version. Try again.", corrupt: "This version failed its integrity check and cannot be read or downloaded.",
    unavailable: "This version is not ready to read.", snapshot: "Saved snapshot", outdated: "Sources updated", current: "Sources match",
    staleHint: "This view preserves the generation snapshot, without subsequent changes.", risk: "Risk distribution", unique: "Unique findings",
    occurrences: "Occurrences", internal: "Internal findings", targets: "Targets covered", keyFindings: "Key findings",
    coverage: "Task coverage", included: "Included", excluded: "Excluded", total: "Tasks at generation",
    openSources: "View source tasks", noFindings: "No validated vulnerabilities in this snapshot.", allFindings: "View all findings",
    legacyFindings: "This version has no structured findings summary.", legacyStats: "This version has no saved risk statistics.",
    viewFull: "Read full report", source: "Source", sourceReport: "Open task report", noExcluded: "All tasks were included.",
    noSources: "No source tasks recorded in this version.", provenance: "Snapshot verification", reportHash: "Report SHA-256",
    snapshotHash: "Source snapshot SHA-256", missingReport: "Missing final report", running: "Running", failed: "Failed",
    crashed: "Crashed", stopped: "Stopped", queued: "Queued", reporting: "Report in progress", unknown: "Not completed",
    newSources: "New source tasks", changedSources: "Source content changed", removedSources: "Source tasks unavailable", projectChanged: "Project settings changed",
    task: "Task", findings: "Findings", noContent: "This report version has no body content.", showSources: "View sources",
  },
} as const;

type Copy = (typeof TEXT)[keyof typeof TEXT];
type Mode = "summary" | "full" | "sources";
const SEVERITIES = ["critical", "high", "medium", "low", "info"] as const;
const SEVERITY_NAMES = {
  "zh-CN": { critical: "严重", high: "高危", medium: "中危", low: "低危", info: "信息" },
  en: { critical: "Critical", high: "High", medium: "Medium", low: "Low", info: "Info" },
};

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function dateLabel(value: string, locale: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function sourceHref(run: string): string {
  return `/run?name=${encodeURIComponent(run)}&tab=report`;
}

function reasonLabel(reason: string, copy: Copy): string {
  const labels: Record<string, string> = {
    missing_final_report: copy.missingReport, status_running: copy.running, status_failed: copy.failed,
    status_crashed: copy.crashed, status_stopped: copy.stopped, status_queued: copy.queued,
    status_reporting: copy.reporting, status_pending: copy.queued,
  };
  return labels[reason] || copy.unknown;
}

export default function ProjectReportReader({
  projectId, initialReport, versions, onClose,
}: {
  projectId: string;
  initialReport: ProjectReportVersion;
  versions: ProjectReportVersion[];
  onClose: () => void;
}) {
  const { locale } = useI18n();
  const copy = TEXT[locale];
  const [versionId, setVersionId] = React.useState(initialReport.version_id);
  const [mode, setMode] = React.useState<Mode>("summary");
  const [report, setReport] = React.useState<ProjectReportVersion | null>(null);
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [attempt, retry] = React.useReducer((value: number) => value + 1, 0);
  const previousFocus = React.useRef<HTMLElement | null>(null);
  const bodyRef = React.useRef<HTMLDivElement | null>(null);
  const versionOptions = React.useMemo(() => {
    const options = new Map(versions.map((version) => [version.version_id, version]));
    if (!options.has(initialReport.version_id)) options.set(initialReport.version_id, initialReport);
    return [...options.values()].sort((a, b) => b.version - a.version);
  }, [versions, initialReport]);
  const selectedMetadata = versionOptions.find((version) => version.version_id === versionId);

  React.useEffect(() => {
    // Dispose each selection independently so a slower older response can never
    // replace the newly selected version or enable its download.
    let disposed = false;
    setPhase("loading");
    setReport(null);
    void getJSON<{ report: ProjectReportVersion }>(
      `/api/projects/${encodeURIComponent(projectId)}/reports/${encodeURIComponent(versionId)}`
    ).then((page) => {
      if (disposed) return;
      if (page.report.project_id !== projectId || page.report.version_id !== versionId) {
        setPhase("error");
        return;
      }
      setReport(page.report);
      setPhase("ready");
    }).catch(() => {
      if (!disposed) setPhase("error");
    });
    return () => { disposed = true; };
  }, [projectId, versionId, attempt]);

  React.useEffect(() => {
    bodyRef.current?.scrollTo({ top: 0 });
  }, [versionId, mode]);

  const metadata = report?.version_id === versionId ? report : selectedMetadata;
  const currentReport = phase === "ready" && report?.version_id === versionId ? report : null;
  const corrupt = currentReport?.integrity_ok === false || currentReport?.status === "corrupt";
  const readable = currentReport?.ready && !corrupt;

  const download = () => {
    if (!readable || !currentReport?.content) return;
    const url = URL.createObjectURL(new Blob([currentReport.content], { type: "text/markdown;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${projectId}-${versionId}.md`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.dialog}
          onOpenAutoFocus={() => { previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
          onCloseAutoFocus={(event) => { event.preventDefault(); previousFocus.current?.focus(); }}>
          <header className={styles.header}>
            <FileText className={styles.headerIcon} aria-hidden="true" />
            <div className={styles.heading}>
              <Dialog.Title className={styles.title}>{copy.title}</Dialog.Title>
              <Dialog.Description className={styles.subtitle}>{initialReport.project_name || projectId}</Dialog.Description>
            </div>
            <button type="button" onClick={download} disabled={!readable || !currentReport?.content} className={`button-secondary button-compact ${styles.download}`} aria-label={copy.download}>
              <Download size={14} aria-hidden="true" /><span>{copy.download}</span>
            </button>
            <Dialog.Close asChild><button type="button" className={styles.iconButton} aria-label={copy.close}><X size={19} /></button></Dialog.Close>
          </header>

          <div className={styles.versionBar}>
            <label className={styles.versionLabel}>
              <History size={14} aria-hidden="true" /><span className={styles.srOnly}>{copy.version}</span>
              <Select
                className={styles.versionSelect}
                value={versionId}
                onValueChange={setVersionId}
                aria-label={copy.version}
                options={versionOptions.map((version) => ({
                  value: version.version_id,
                  label: `${version.version_id} · ${dateLabel(version.generated_at, locale)} · ${version.language === "en" ? "EN" : "简中"}`,
                }))}
              />
            </label>
            <span className={`${styles.snapshotStatus} ${metadata?.stale ? styles.staleStatus : ""}`}>
              {metadata?.stale === true ? copy.outdated : metadata?.stale === false ? copy.current : copy.snapshot}
              {metadata?.language && ` · ${metadata.language === "en" ? "EN" : "简中"}`}
            </span>
          </div>

          <Tabs.Root value={mode} onValueChange={(value) => setMode(value as Mode)} className={styles.workspace}>
            <Tabs.List className={styles.tabs} aria-label={copy.title}>
              <Tabs.Trigger className={styles.tab} value="summary">{copy.summary}</Tabs.Trigger>
              <Tabs.Trigger className={styles.tab} value="full">{copy.full}</Tabs.Trigger>
              <Tabs.Trigger className={styles.tab} value="sources">{copy.sources}</Tabs.Trigger>
            </Tabs.List>
            <div ref={bodyRef} className={styles.body} aria-busy={phase === "loading"}>
              {phase === "loading" && <div className={styles.state} role="status"><RefreshCw size={18} className={styles.spinner} aria-hidden="true" />{copy.loading}</div>}
              {phase === "error" && <div className={styles.state} role="alert">
                <p>{copy.error}</p><button type="button" className="button-secondary button-compact" onClick={retry}><RefreshCw size={14} />{copy.retry}</button>
              </div>}
              {currentReport && !readable && <div className={styles.state} role="alert"><ShieldAlert size={24} /><p>{corrupt ? copy.corrupt : copy.unavailable}</p></div>}
              {readable && currentReport && <>
                {currentReport.stale && <div className={styles.staleNotice} role="note">
                  <span>{copy.staleHint}</span>
                  {!!currentReport.stale_reasons?.length && <details><summary>{copy.outdated}</summary><ul>
                    {currentReport.stale_reasons.map((reason, index) => {
                      const labels: Record<string, string> = { new_source_run: copy.newSources, source_run_changed: copy.changedSources, source_run_removed: copy.removedSources, project_changed: copy.projectChanged };
                      return <li key={`${reason.code}-${index}`}>{labels[reason.code] || copy.outdated}{reason.runs?.length ? ` · ${reason.runs.length}` : ""}</li>;
                    })}
                  </ul></details>}
                </div>}
                <Tabs.Content className={styles.tabContent} value="summary">
                  <SnapshotSummary report={currentReport} copy={copy} locale={locale} onMode={setMode} />
                </Tabs.Content>
                <Tabs.Content className={styles.tabContent} value="full">
                  {currentReport.content ? <div className={`prose-report report-document ${styles.markdown}`}>
                    <ReportMarkdown report={currentReport} locale={locale} />
                  </div> : <p className={styles.empty}>{copy.noContent}</p>}
                </Tabs.Content>
                <Tabs.Content className={styles.tabContent} value="sources"><SourceTasks report={currentReport} copy={copy} /></Tabs.Content>
              </>}
            </div>
          </Tabs.Root>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function ReportMarkdown({ report, locale }: { report: ProjectReportVersion; locale: keyof typeof TEXT }) {
  const sourceNames = React.useMemo(
    () => new Set(report.source_runs.map((source) => text(source.run)).filter(Boolean)),
    [report.source_runs],
  );
  const components = React.useMemo<Components>(() => ({
    table: ({ children }) => (
      <div className="report-table-scroll" role="region" tabIndex={0}
        aria-label={locale === "en" ? "Report table, scroll horizontally when needed" : "報告表格，可左右捲動"}>
        <table>{children}</table>
      </div>
    ),
    pre: ({ children }) => (
      <pre tabIndex={0} aria-label={locale === "en" ? "Code block" : "程式碼區塊"}>{children}</pre>
    ),
    // Existing stored Markdown remains byte-for-byte unchanged. Only exact
    // snapshot source identifiers become navigation links in the reader.
    code({ children, className }) {
      const name = typeof children === "string" ? children : "";
      return name && sourceNames.has(name)
        ? <Link href={sourceHref(name)}><code>{name}</code></Link>
        : <code className={className}>{children}</code>;
    },
    td({ children, style }) {
      const names = typeof children === "string" ? children.split(", ") : [];
      return <td style={style}>{names.length > 0 && names.every((name) => sourceNames.has(name))
        ? names.map((name, index) => <React.Fragment key={name}>{index > 0 ? ", " : ""}<Link href={sourceHref(name)}>{name}</Link></React.Fragment>)
        : children}</td>;
    },
  }), [locale, sourceNames]);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{report.content ?? ""}</ReactMarkdown>;
}

function SnapshotSummary({ report, copy, locale, onMode }: {
  report: ProjectReportVersion; copy: Copy; locale: keyof typeof TEXT; onMode: (mode: Mode) => void;
}) {
  const stats = report.stats;
  const findings = report.snapshot_summary?.key_findings;
  const included = count(report.included_run_count);
  const excluded = report.excluded_runs?.length ?? 0;
  const total = count(report.total_run_count);
  const unique = stats ? count(stats.unique_vulnerability_count) : null;
  const sourceNames = new Set(report.source_runs.map((source) => text(source.run)).filter(Boolean));
  const riskTotal = SEVERITIES.reduce((sum, severity) => sum + count(stats?.severity?.[severity]), 0);

  return <div className={styles.summaryGrid}>
    <div className={styles.findingColumn}>
      <section className={styles.section} aria-labelledby="report-risk-title">
        <div className={styles.sectionHeading}><h3 id="report-risk-title">{copy.risk}</h3>{unique !== null && <span>{unique} {copy.unique}</span>}</div>
        {stats ? <>
          <div className={styles.riskBar} aria-hidden="true">
            {riskTotal ? SEVERITIES.filter((severity) => count(stats.severity[severity]) > 0).map((severity) => <span key={severity} data-severity={severity} style={{ flexGrow: count(stats.severity[severity]) }} />) : <span className={styles.noRisk} />}
          </div>
          <dl className={styles.riskLegend}>{SEVERITIES.map((severity) => <div key={severity}>
            <dt><i data-severity={severity} aria-hidden="true" />{SEVERITY_NAMES[locale][severity]}</dt><dd>{count(stats.severity[severity])}</dd>
          </div>)}</dl>
        </> : <p className={styles.empty}>{copy.legacyStats}</p>}
      </section>

      <section className={styles.section} aria-labelledby="report-findings-title">
        <div className={styles.sectionHeading}><h3 id="report-findings-title">{copy.keyFindings}</h3>{findings && unique !== null && <span>{findings.length} / {unique}</span>}</div>
        {findings === undefined ? <div className={styles.empty}><p>{copy.legacyFindings}</p><button type="button" className={styles.textButton} onClick={() => onMode("full")}>{copy.viewFull}<ArrowUpRight size={13} /></button></div> : findings.length === 0 ? <p className={styles.empty}>{copy.noFindings}</p> : <ol className={styles.findingList}>
          {findings.map((finding) => {
            const severity = SEVERITIES.find((value) => value === finding.severity) || "info";
            const sources = finding.source_runs.filter((run) => sourceNames.has(run));
            return <li key={finding.fingerprint} className={styles.finding}>
              <div className={styles.findingHeading}><span className={styles.severity} data-severity={severity}>{SEVERITY_NAMES[locale][severity]}</span><h4>{finding.title}</h4></div>
              <p className={styles.location}>{finding.target}{finding.endpoint && finding.endpoint !== "—" ? ` · ${finding.endpoint}` : ""}</p>
              <div className={styles.findingSources}><span>{copy.source}</span>{sources.slice(0, 2).map((run) => <Link key={run} href={sourceHref(run)} className={styles.sourceChip} title={`${copy.sourceReport}: ${run}`}><span>{run}</span><ArrowUpRight size={11} aria-hidden="true" /></Link>)}
                {sources.length > 2 && <button type="button" className={styles.textButton} onClick={() => onMode("sources")} aria-label={`${copy.showSources} (${sources.length})`}>+{sources.length - 2}</button>}
              </div>
            </li>;
          })}
        </ol>}
        {!!findings?.length && <button type="button" className={styles.textButton} onClick={() => onMode("full")}>{copy.allFindings}<ArrowUpRight size={13} /></button>}
      </section>
    </div>

    <aside className={styles.coverageColumn}>
      <section className={styles.section} aria-labelledby="report-coverage-title">
        <div className={styles.sectionHeading}><h3 id="report-coverage-title">{copy.coverage}</h3></div>
        <div className={styles.coverageValue}><strong>{included}</strong><span>/ {total}</span></div>
        <p className={styles.caption}>{copy.included} / {copy.total}</p>
        <div className={styles.coverageBar} aria-hidden="true"><span style={{ width: `${total > 0 ? Math.min(100, included / total * 100) : 0}%` }} /></div>
        <dl className={styles.coverageStats}><div><dt>{copy.excluded}</dt><dd>{excluded}</dd></div>
          {stats && <><div><dt>{copy.targets}</dt><dd>{count(stats.target_count)}</dd></div><div><dt>{copy.occurrences}</dt><dd>{count(stats.vulnerability_occurrence_count)}</dd></div><div><dt>{copy.internal}</dt><dd>{count(stats.unique_internal_finding_count)}</dd></div></>}
        </dl>
        <button type="button" className={styles.textButton} onClick={() => onMode("sources")}>{copy.openSources}<ArrowUpRight size={13} /></button>
      </section>
    </aside>
  </div>;
}

function SourceTasks({ report, copy }: { report: ProjectReportVersion; copy: Copy }) {
  const sources = report.source_runs.filter((source) => text(source.run));
  const excluded = report.excluded_runs ?? [];
  return <div className={styles.sourceView}>
    <section className={styles.section}>
      <div className={styles.sectionHeading}><h3>{copy.included}</h3><span>{sources.length} {copy.task}</span></div>
      {sources.length ? <ul className={styles.sourceList}>{sources.map((source) => <li key={text(source.run)}>
        <Link className={styles.sourceRow} href={sourceHref(text(source.run))} aria-label={`${copy.sourceReport}: ${text(source.run)}`}>
          <FileText size={17} aria-hidden="true" /><div className={styles.sourceIdentity}><strong>{text(source.target) || text(source.run)}</strong><code>{text(source.run)}</code></div>
          <span className={styles.sourceCount}>{count(source.vulnerability_count)} {copy.findings}</span><ArrowUpRight size={15} aria-hidden="true" />
        </Link>
      </li>)}</ul> : <p className={styles.empty}>{copy.noSources}</p>}
    </section>

    <details className={styles.disclosure} open={excluded.length > 0}>
      <summary>{copy.excluded}<span>{excluded.length}</span></summary>
      {excluded.length ? <ul className={styles.excludedList}>{excluded.map((source) => <li key={source.run}>
        <code>{source.run}</code><span title={source.reason}>{reasonLabel(source.reason, copy)}</span>
      </li>)}</ul> : <p className={styles.empty}>{copy.noExcluded}</p>}
    </details>

    <details className={styles.disclosure}>
      <summary>{copy.provenance}</summary>
      <dl className={styles.hashes}><div><dt>{copy.snapshotHash}</dt><dd><code>{report.source_snapshot_hash || "—"}</code></dd></div>
        {sources.map((source) => <div key={text(source.run)}><dt>{text(source.run)} · {copy.reportHash}</dt><dd><code>{text(source.report_sha256) || "—"}</code></dd></div>)}
      </dl>
    </details>
  </div>;
}
