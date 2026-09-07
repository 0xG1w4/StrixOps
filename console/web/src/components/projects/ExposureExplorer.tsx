"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import Link from "next/link";
import { ArrowRight, Braces, ChevronDown, Globe2, Network, Radar, X } from "lucide-react";
import { Panel, SeverityChip, Spinner, StatusPill } from "@/components/ui";
import { Select } from "@/components/Select";
import type { InternalFinding, ProjectFindings, RunSummary, Vulnerability } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./ExposureExplorer.module.css";

const COPY = {
  "zh-CN": {
    title: "暴露关系", viewAll: "全部发现", target: "目标", task: "任务", finding: "发现",
    targets: "个目标", tasks: "个任务", web: "Web 漏洞", internal: "内部发现",
    all: "全部", highRisk: "高风险", severity: "风险等级", critical: "严重", high: "高危",
    medium: "中危", low: "低危", info: "信息", unknown: "未记录", loading: "正在加载关系…",
    error: "关系数据暂时无法加载。", errorAction: "重新加载页面后重试。", retry: "重试",
    empty: "尚未记录发现。", emptyHint: "任务记录发现后，将在这里关联目标与来源。",
    noMatch: "没有符合此风险等级的发现。", reset: "显示全部", showing: "显示", matching: "符合筛选",
    records: "条记录", more: "显示更多", close: "关闭详情", details: "关系详情", sourceReport: "来源报告",
    sourceFindings: "原始发现", relatedTasks: "关联任务", relatedFindings: "关联发现",
    detailHint: "只读 · 保留原始任务来源", summary: "摘要", impact: "影响", analysis: "技术分析",
    evidence: "证据", remediation: "修复建议", confidence: "置信度", endpoint: "端点", host: "主机",
    method: "请求方法", findingType: "发现类型", sourceFile: "来源文件", started: "开始时间",
    finished: "结束时间", recorded: "记录时间", status: "状态", mode: "模式", noDetails: "此记录没有更多详情。",
    missingRun: "来源任务详情不可用。", scopeOutside: "不符合当前项目范围", unknownTarget: "未记录目标",
    recordsHint: "按来源记录计数，未跨任务去重。", allRelated: "包含此节点所有风险等级。",
  },
  en: {
    title: "Exposure lineage", viewAll: "All findings", target: "Target", task: "Task", finding: "Finding",
    targets: "targets", tasks: "tasks", web: "Web findings", internal: "Internal findings",
    all: "All", highRisk: "High risk", severity: "Severity", critical: "Critical", high: "High",
    medium: "Medium", low: "Low", info: "Info", unknown: "Not recorded", loading: "Loading relationships…",
    error: "Relationship data is unavailable.", errorAction: "Reload the page to try again.", retry: "Retry",
    empty: "No findings recorded yet.", emptyHint: "Findings will connect targets to their source tasks here.",
    noMatch: "No findings match this severity.", reset: "Show all", showing: "Showing", matching: "matching",
    records: "records", more: "Show more", close: "Close details", details: "Relationship details", sourceReport: "Source report",
    sourceFindings: "Original findings", relatedTasks: "Related tasks", relatedFindings: "Related findings",
    detailHint: "Read only · Original task sources", summary: "Summary", impact: "Impact", analysis: "Technical analysis",
    evidence: "Evidence", remediation: "Remediation", confidence: "Confidence", endpoint: "Endpoint", host: "Host",
    method: "Method", findingType: "Finding type", sourceFile: "Source file", started: "Started",
    finished: "Finished", recorded: "Recorded", status: "Status", mode: "Mode", noDetails: "No additional detail in this record.",
    missingRun: "Source task details are unavailable.", scopeOutside: "Outside current project scope", unknownTarget: "Target not recorded",
    recordsHint: "Source records, not deduplicated across tasks.", allRelated: "Includes all severities for this node.",
  },
} as const;

type Copy = (typeof COPY)["en"] | (typeof COPY)["zh-CN"];
type RiskFilter = "all" | "highRisk" | "critical" | "high" | "medium" | "low" | "info" | "unknown";
type WebRecord = Vulnerability & { source_run: string };
type InternalRecord = InternalFinding & { source_run: string };
type FindingRecord = {
  key: string;
  title: string;
  /** Recorded value retained verbatim in finding details. */
  target: string;
  /** Host-only visual grouping; does not change finding identity or counts. */
  groupTarget: string;
  runName: string;
  severity: string;
} & ({ kind: "web"; original: WebRecord } | { kind: "internal"; original: InternalRecord });
type Selection =
  | { kind: "target"; target: string }
  | { kind: "task"; target: string; runName: string }
  | { kind: "finding"; key: string };
type TargetGroup = { target: string; tasks: Array<{ name: string; records: FindingRecord[] }> };
const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info", "unknown"] as const;
const PAGE_SIZE = 6;

/** Conservative display grouping only. No DNS, CIDR expansion, or finding deduplication. */
export function exposureGroupTarget(target: string): string {
  if (/^https?:\/\//i.test(target)) {
    try {
      const url = new URL(target);
      const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
      return `${hostname}${url.port ? `:${url.port}` : ""}`;
    } catch { return target; }
  }
  // Non-host expressions, paths, CIDRs, credentials and surrounding whitespace stay exact.
  if (!target || /[\s/@?#\\*%]/.test(target)) return target;
  const bracketed = target.match(/^\[([0-9a-f:.]+)\](?::(\d{1,5}))?$/i);
  if (bracketed || (target.match(/:/g)?.length ?? 0) > 1) {
    const address = bracketed?.[1] || target;
    const port = bracketed?.[2];
    if (port && Number(port) > 65535) return target;
    try {
      const hostname = new URL(`http://[${address}]`).hostname.toLowerCase();
      return `${hostname}${port ? `:${Number(port)}` : ""}`;
    } catch { return target; }
  }
  const host = target.match(/^([^:]+)(?::(\d{1,5}))?$/);
  if (!host || (host[2] && Number(host[2]) > 65535)) return target;
  const hostname = host[1].toLowerCase().replace(/\.$/, "");
  if (hostname.length > 253 || !hostname.split(".").every((label) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return target;
  // Avoid guessing legacy/abbreviated IPv4 forms or numeric target identifiers.
  if (/^[0-9.]+$/.test(hostname) && !(/^\d+\.\d+\.\d+\.\d+$/.test(hostname) && hostname.split(".").every((part) => Number(part) <= 255 && (part === "0" || !part.startsWith("0"))))) return target;
  return `${hostname}${host[2] ? `:${Number(host[2])}` : ""}`;
}

function severity(value?: string): string {
  const normalized = value?.trim().toLowerCase() || "unknown";
  return normalized === "informational" ? "info" : normalized;
}

function severityRank(value: string): number {
  const rank = SEVERITY_ORDER.indexOf(value as (typeof SEVERITY_ORDER)[number]);
  return rank < 0 ? SEVERITY_ORDER.length : rank;
}

function matches(record: FindingRecord, filter: RiskFilter): boolean {
  if (filter === "all") return true;
  if (filter === "highRisk") return ["critical", "high"].includes(record.severity);
  if (filter === "unknown") return !SEVERITY_ORDER.slice(0, -1).some((item) => item === record.severity);
  return record.severity === filter;
}

function groups(records: FindingRecord[]): TargetGroup[] {
  const result = new Map<string, Map<string, FindingRecord[]>>();
  for (const record of records) {
    if (!result.has(record.groupTarget)) result.set(record.groupTarget, new Map());
    const tasks = result.get(record.groupTarget)!;
    if (!tasks.has(record.runName)) tasks.set(record.runName, []);
    tasks.get(record.runName)!.push(record);
  }
  return Array.from(result, ([target, tasks]) => ({
    target,
    tasks: Array.from(tasks, ([name, records]) => ({ name, records })),
  }));
}

function sourceURL(name: string, tab = "report"): string {
  return `/run?name=${encodeURIComponent(name)}&tab=${tab}`;
}

function formatDate(value: string | null | undefined, locale: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString(locale === "en" ? "en-US" : "zh-CN", { hour12: false });
}

/** Read-only project lineage. Counts are source records, never inferred unique issues. */
export function ExposureExplorer({ projectId, runs, findings, status, onViewAll, onRetry }: {
  projectId: string;
  runs: RunSummary[];
  findings: ProjectFindings | null;
  status: "loading" | "ready" | "error";
  onViewAll: () => void;
  onRetry?: () => void;
}) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  const runMap = React.useMemo(() => new Map(runs.map((run) => [run.name, run])), [runs]);
  const records = React.useMemo<FindingRecord[]>(() => [
    ...(findings?.vulnerabilities ?? []).map((finding, index): FindingRecord => ({
      key: `web:${finding.source_run}:${finding.id}:${index}`,
      kind: "web", original: finding, title: finding.title || finding.id,
      target: finding.target || runMap.get(finding.source_run)?.target || "",
      groupTarget: exposureGroupTarget(finding.target || runMap.get(finding.source_run)?.target || ""),
      runName: finding.source_run, severity: severity(finding.severity),
    })),
    ...(findings?.internal ?? []).map((finding, index): FindingRecord => ({
      key: `internal:${finding.source_run}:${finding.id}:${index}`,
      kind: "internal", original: finding, title: finding.title || finding.finding_type || finding.id,
      target: finding.host || runMap.get(finding.source_run)?.target || "",
      groupTarget: exposureGroupTarget(finding.host || runMap.get(finding.source_run)?.target || ""),
      runName: finding.source_run, severity: severity(finding.severity),
    })),
  ].sort((a, b) => severityRank(a.severity) - severityRank(b.severity)), [findings, runMap]);
  const highRiskCount = records.filter((record) => matches(record, "highRisk")).length;
  const [preference, setPreference] = React.useState<{ projectId: string; filter: RiskFilter } | null>(null);
  const filter = preference?.projectId === projectId ? preference.filter : highRiskCount ? "highRisk" : "all";
  const [limit, setLimit] = React.useState(PAGE_SIZE);
  const [selection, setSelection] = React.useState<Selection | null>(null);
  const [relatedLimit, setRelatedLimit] = React.useState(12);
  const originRef = React.useRef<HTMLButtonElement | null>(null);
  React.useEffect(() => { setSelection(null); setLimit(PAGE_SIZE); }, [projectId]);
  React.useEffect(() => { setLimit(PAGE_SIZE); }, [filter]);

  const matching = records.filter((record) => matches(record, filter));
  // Group before pagination so subsequent rows grow a branch instead of shuffling it.
  const groupedMatches = groups(matching);
  const orderedMatches = groupedMatches.flatMap((group) => group.tasks.flatMap((task) => task.records));
  const visible = orderedMatches.slice(0, limit);
  const visibleGroups = groups(visible);
  const selectedFinding = selection?.kind === "finding" ? records.find((record) => record.key === selection.key) : undefined;
  const selectedTarget = selection?.kind === "target" || selection?.kind === "task" ? selection.target : selectedFinding?.groupTarget;
  const selectedRunName = selection?.kind === "task" ? selection.runName : selectedFinding?.runName;
  const selectedRun = selectedRunName ? runMap.get(selectedRunName) : undefined;
  const selectedRecords = selection?.kind === "finding"
    ? selectedFinding ? [selectedFinding] : []
    : records.filter((record) => record.groupTarget === selectedTarget && (!selectedRunName || record.runName === selectedRunName));
  const selectedTasks = Array.from(new Set(selectedRecords.map((record) => record.runName)));
  const selectedTitle = selection?.kind === "finding" ? selectedFinding?.title : selection?.kind === "task" ? selectedRunName : selectedTarget || copy.unknownTarget;
  const dialogOpen = selection !== null && (selection.kind !== "finding" || !!selectedFinding);
  React.useEffect(() => { setRelatedLimit(12); }, [selection?.kind, selectedTarget, selectedRunName]);

  const chooseFilter = (value: RiskFilter) => { setPreference({ projectId, filter: value }); setLimit(PAGE_SIZE); };
  const open = (next: Selection, event: React.MouseEvent<HTMLButtonElement>) => {
    originRef.current = event.currentTarget;
    setSelection(next);
  };

  return (
    <>
      <Panel code="REL" title={copy.title} className={styles.panel} actions={<button type="button" className="button-ghost button-compact" onClick={onViewAll}>{copy.viewAll}<ArrowRight size={13} aria-hidden="true" /></button>}>
        {status === "loading" ? <div className={styles.empty} role="status"><Spinner /><span>{copy.loading}</span></div> : status === "error" ? <div className={styles.empty} role="alert"><strong>{copy.error}</strong>{onRetry ? <button type="button" className="button-secondary button-compact" onClick={onRetry}>{copy.retry}</button> : <span>{copy.errorAction}</span>}</div> : records.length === 0 ? <div className={styles.empty}><strong>{copy.empty}</strong><span>{copy.emptyHint}</span></div> : (
          <div className={styles.body}>
            <div className={styles.toolbar}>
              <div className={styles.filters} aria-label={copy.severity}>
                {(["all", "highRisk"] as const).map((value) => <button type="button" className={styles.filter} aria-pressed={filter === value} key={value} onClick={() => chooseFilter(value)}>{copy[value]}<span>{value === "all" ? records.length : highRiskCount}</span></button>)}
              </div>
              <Select
                className={styles.select}
                value={filter}
                onValueChange={(value) => chooseFilter(value as RiskFilter)}
                aria-label={copy.severity}
                options={[
                  { value: "all", label: `${copy.severity} · ${copy.all}` },
                  { value: "highRisk", label: `${copy.severity} · ${copy.highRisk}` },
                  ...SEVERITY_ORDER.map((value) => ({
                    value,
                    label: `${copy[value]} · ${records.filter((record) => matches(record, value)).length}`,
                  })),
                ]}
              />
            </div>
            <div className={styles.mapMeta} aria-live="polite"><span>{copy.showing} {visible.length} / {matching.length} {copy.matching}</span><span>{records.filter((record) => record.kind === "web").length} WEB · {records.filter((record) => record.kind === "internal").length} INT</span></div>
            {visible.length === 0 ? <div className={styles.empty}><span>{copy.noMatch}</span><button type="button" className="button-secondary button-compact" onClick={() => chooseFilter("all")}>{copy.reset}</button></div> : (
              <ul className={styles.targets}>
                {visibleGroups.map((group) => <li className={styles.targetGroup} key={group.target}>
                  <button type="button" className={styles.targetNode} onClick={(event) => open({ kind: "target", target: group.target }, event)} aria-label={`${copy.target}: ${group.target || copy.unknownTarget}`} title={group.target || copy.unknownTarget}>
                    <Globe2 size={15} aria-hidden="true" /><span className={styles.nodeLabel}>{group.target || copy.unknownTarget}</span><span className={styles.nodeCount}>{groupedMatches.find((item) => item.target === group.target)?.tasks.length} {copy.tasks}</span><ArrowRight size={12} aria-hidden="true" />
                  </button>
                  <ul className={styles.taskBranches}>
                    {group.tasks.map((task) => <li className={styles.taskBranch} key={task.name}>
                      <button type="button" className={styles.taskNode} onClick={(event) => open({ kind: "task", target: group.target, runName: task.name }, event)} aria-label={`${copy.task}: ${task.name}`} title={task.name}>
                        <Braces size={13} aria-hidden="true" /><span className={styles.nodeLabel}>{task.name}</span><span className={styles.nodeTag}>TASK</span>
                      </button>
                      <ul className={styles.findingBranches}>
                        {task.records.map((record) => <li className={styles.findingBranch} key={record.key}><button type="button" className={styles.findingNode} onClick={(event) => open({ kind: "finding", key: record.key }, event)} title={record.title} aria-label={`${record.kind === "web" ? copy.web : copy.internal}: ${record.title}`}>
                          <span className={styles.findingGlyph} data-severity={record.severity} aria-hidden="true">{record.kind === "web" ? <Radar size={14} /> : <Network size={14} />}</span>
                          <span className={styles.findingText}>{record.title}</span><span className={styles.findingMeta}><span className={styles.nodeTag}>{record.kind === "web" ? "WEB" : "INT"}</span><SeverityChip severity={record.severity} /></span>
                        </button></li>)}
                      </ul>
                    </li>)}
                  </ul>
                </li>)}
              </ul>
            )}
            <div className={styles.mapFooter}><span>{copy.recordsHint}</span>{visible.length < matching.length && <button type="button" className="button-ghost button-compact" onClick={() => setLimit((count) => count + PAGE_SIZE)}>{copy.more}<span>+{Math.min(PAGE_SIZE, matching.length - visible.length)}</span><ChevronDown size={13} aria-hidden="true" /></button>}</div>
          </div>
        )}
      </Panel>

      <Dialog.Root open={dialogOpen} onOpenChange={(isOpen) => { if (!isOpen) setSelection(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className={styles.overlay} />
          <Dialog.Content className={styles.drawer} onCloseAutoFocus={(event) => { event.preventDefault(); if (originRef.current?.isConnected) originRef.current.focus(); }}>
            <header className={styles.drawerHeader}><div><span className={styles.kicker}>[REL] {selection?.kind === "target" ? copy.target : selection?.kind === "task" ? copy.task : selectedFinding?.kind === "internal" ? copy.internal : copy.web}</span><Dialog.Title className={styles.drawerTitle}>{selectedTitle || copy.details}</Dialog.Title><Dialog.Description className={styles.drawerDescription}>{copy.detailHint}</Dialog.Description></div><Dialog.Close className={styles.close} aria-label={copy.close}><X size={19} aria-hidden="true" /></Dialog.Close></header>
            <div className={styles.drawerBody} key={selection ? JSON.stringify(selection) : "closed"}>
              {selection?.kind !== "target" && <nav className={styles.crumbs} aria-label={copy.title}><button type="button" onClick={() => setSelection({ kind: "target", target: selectedTarget || "" })}><Globe2 size={13} aria-hidden="true" /><span>{selectedTarget || copy.unknownTarget}</span></button>{selection?.kind === "finding" && selectedRunName && <><ArrowRight size={11} aria-hidden="true" /><button type="button" onClick={() => setSelection({ kind: "task", target: selectedTarget || "", runName: selectedRunName })}><Braces size={13} aria-hidden="true" /><span>{selectedRunName}</span></button></>}</nav>}

              {selection?.kind === "finding" && selectedFinding ? <FindingDetail record={selectedFinding} locale={locale} copy={copy} /> : <>
                <div className={styles.countGrid}><Count label={copy.web} value={selectedRecords.filter((record) => record.kind === "web").length} /><Count label={copy.internal} value={selectedRecords.filter((record) => record.kind === "internal").length} />{selection?.kind === "target" && <Count label={copy.task} value={selectedTasks.length} />}</div>
                {selection?.kind === "task" && (selectedRun ? <dl className={styles.properties}><Property label={copy.status}><StatusPill status={selectedRun.status} live={selectedRun.live} /></Property><Property label={copy.target}>{selectedRun.target}</Property><Property label={copy.mode}>{selectedRun.scan_type}</Property><Property label={copy.started}>{formatDate(selectedRun.start_time, locale)}</Property><Property label={copy.finished}>{formatDate(selectedRun.end_time, locale)}</Property>{selectedRun.scope_match === false && <Property label="Scope"><span className={styles.warning}>{copy.scopeOutside}</span></Property>}</dl> : <p className={styles.detailNote}>{copy.missingRun}</p>)}
                <div className={styles.detailSection}><div className={styles.sectionHeading}><h3>{selection?.kind === "target" ? copy.relatedTasks : copy.relatedFindings}</h3><span>{selection?.kind === "target" ? selectedTasks.length : selectedRecords.length}</span></div><p className={styles.detailNote}>{copy.allRelated}</p>
                  <ul className={styles.relatedList}>{selection?.kind === "target" ? selectedTasks.slice(0, relatedLimit).map((name) => <li key={name}><button type="button" className={styles.relatedItem} onClick={() => setSelection({ kind: "task", target: selectedTarget || "", runName: name })}><Braces size={14} aria-hidden="true" /><span>{name}</span><span className={styles.relatedCount}>{selectedRecords.filter((record) => record.runName === name).length}</span><ArrowRight size={13} aria-hidden="true" /></button></li>) : selectedRecords.slice(0, relatedLimit).map((record) => <li key={record.key}><button type="button" className={styles.relatedItem} onClick={() => setSelection({ kind: "finding", key: record.key })}>{record.kind === "web" ? <Radar size={14} aria-hidden="true" /> : <Network size={14} aria-hidden="true" />}<span>{record.title}<small>{record.kind === "web" ? copy.web : copy.internal}</small></span><SeverityChip severity={record.severity} /></button></li>)}</ul>
                  {(selection?.kind === "target" ? selectedTasks.length : selectedRecords.length) > relatedLimit && <button type="button" className="button-ghost button-compact" onClick={() => setRelatedLimit((count) => count + 12)}>{copy.more}<ChevronDown size={13} aria-hidden="true" /></button>}
                </div>
                <p className={styles.detailNote}>{copy.recordsHint}</p>
              </>}
            </div>
            {selectedRunName && <footer className={styles.drawerFooter}><Link className="button-primary" href={sourceURL(selectedRunName)} onClick={() => setSelection(null)}>{copy.sourceReport}<ArrowRight size={14} aria-hidden="true" /></Link><Link className="button-secondary" href={sourceURL(selectedRunName, "findings")} onClick={() => setSelection(null)}>{copy.sourceFindings}</Link></footer>}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return <div className={styles.count}><strong>{value}</strong><span>{label}</span></div>;
}

function Property({ label, children }: { label: string; children: React.ReactNode }) {
  if (children === null || children === undefined || children === "") return null;
  return <div className={styles.property}><dt>{label}</dt><dd>{children}</dd></div>;
}

function DetailText({ label, value, evidence = false }: { label: string; value?: string; evidence?: boolean }) {
  if (!value?.trim()) return null;
  return <details className={styles.textSection} open={!evidence && value.length < 700}><summary>{label}<ChevronDown size={14} aria-hidden="true" /></summary>{evidence ? <pre>{value}</pre> : <p>{value}</p>}</details>;
}

function FindingDetail({ record, locale, copy }: { record: FindingRecord; locale: string; copy: Copy }) {
  const value = record.original;
  return <>
    <div className={styles.findingHeading}><SeverityChip severity={record.severity} /><span className="mono-chip">{record.kind === "web" ? "WEB" : "INT"}</span><span className={styles.findingId}>{value.id}</span></div>
    <dl className={styles.properties}>
      <Property label={copy.target}>{record.target || copy.unknownTarget}</Property>
      {record.kind === "web" ? <><Property label={copy.endpoint}>{record.original.endpoint}</Property><Property label={copy.method}>{record.original.method}</Property><Property label="CVSS">{record.original.cvss}</Property><Property label="CVSS vector">{record.original.cvss_vector}</Property><Property label="CVE">{record.original.cve}</Property><Property label="CWE">{record.original.cwe}</Property><Property label={copy.confidence}>{record.original.confidence}</Property><Property label={copy.recorded}>{formatDate(record.original.timestamp, locale)}</Property></> : <><Property label={copy.findingType}>{record.original.finding_type}</Property><Property label={copy.sourceFile}>{record.original.source_file}</Property></>}
    </dl>
    {record.kind === "web" ? <div className={styles.textSections}><DetailText label={copy.summary} value={record.original.description} /><DetailText label={copy.impact} value={record.original.impact} /><DetailText label={copy.analysis} value={record.original.technical_analysis} /><DetailText label={copy.evidence} value={record.original.evidence} evidence /><DetailText label={copy.remediation} value={record.original.remediation_steps} />{![record.original.description, record.original.impact, record.original.technical_analysis, record.original.evidence, record.original.remediation_steps].some((text) => text?.trim()) && <p className={styles.detailNote}>{copy.noDetails}</p>}</div> : <p className={styles.detailNote}>{copy.noDetails}</p>}
  </>;
}
