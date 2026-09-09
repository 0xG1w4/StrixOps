"use client";

import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  FileText,
  Fingerprint,
  History,
  ListChecks,
  Network,
  Plus,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import {
  generateProjectReport,
  getProjectFindings,
  getProjectReports,
  getProjectRuns,
  runTargetLabel,
  runTargets,
  getProjectSkillAnalytics,
  updateProjectScope,
  type ProjectFindings,
  type ProjectReportVersion,
  type ProjectScopeRule,
  type ProjectSkillAnalytics,
  type ProjectSummary,
  type RunSummary,
} from "@/lib/api";
import { EmptyState, Panel, SeverityChip, Spinner, StatusPill } from "@/components/ui";
import { Select } from "@/components/Select";
import { useI18n } from "@/lib/i18n";
import { readStorage, writeStorage } from "@/lib/storage";
import { ExposureExplorer } from "@/components/projects/ExposureExplorer";
import ProjectReportReader from "@/components/projects/ProjectReportReader";
import styles from "../projects.module.css";

type WorkspaceTab = "overview" | "tasks" | "findings" | "scope";
type SecondaryStatus = {
  findings: "loading" | "ready" | "error";
  skills: "loading" | "ready" | "error";
  reports: "loading" | "ready" | "error";
};

const COPY = {
  "zh-CN": {
    back: "返回项目",
    newTask: "新建任务",
    overview: "总览",
    tasks: "任务",
    findings: "发现",
    scope: "范围",
    live: "运行中",
    skills: "技能",
    risk: "高风险记录",
    exposure: "暴露关系",
    asset: "目标",
    task: "任务",
    finding: "漏洞",
    viewAll: "查看全部",
    showing: "显示",
    highRiskFirst: "高风险优先",
    loading: "加载中…",
    noExposure: "完成任务并记录漏洞后，这里会建立目标、任务与漏洞之间的关系。",
    skillHits: "技能载入",
    hits: "次载入",
    distinct: "个不同技能",
    noSkills: "尚未记录技能调用。",
    expandSkills: "展开全部",
    collapseSkills: "收起列表",
    recentTasks: "最近任务",
    allTasks: "历史任务",
    searchTasks: "搜索任务或目标",
    allStatuses: "全部状态",
    clearFilters: "清除筛选",
    noMatches: "没有符合筛选的任务。",
    noTasks: "此项目还没有任务。",
    taskReport: "打开任务报告",
    projectReport: "项目总报告",
    reportNone: "尚未生成",
    reportReady: "可阅读",
    reportHistory: "历史版本",
    reportOutdated: "需要更新",
    generated: "生成于",
    included: "已纳入任务",
    completedTasks: "已完成任务",
    excluded: "已排除",
    generate: "生成报告",
    regenerate: "重新生成",
    generating: "生成中…",
    openReport: "打开报告",
    reportUnavailable: "至少需要一个已完成且具有最终报告的任务。",
    reportFailed: "无法生成项目报告",
    download: "下载 Markdown",
    closeReport: "关闭报告",
    webFindings: "Web 漏洞",
    internalFindings: "内部发现",
    noFindings: "此项目尚无发现。",
    dataUnavailable: "数据暂时无法加载。",
    source: "来源",
    target: "目标",
    unrestricted: "不限制",
    unrestrictedHint: "使用 *，允许项目从任意目标启动任务。",
    restricted: "限制范围",
    restrictedHint: "只允许下列 Domain、IP 或 CIDR。",
    domain: "Domain",
    ip: "IP",
    cidr: "CIDR",
    includeSubdomains: "包含子域名",
    addRule: "添加目标",
    ruleType: "规则类型",
    removeRule: "移除规则",
    saveScope: "保存范围",
    saving: "保存中…",
    revision: "策略版本",
    launchGuard: "启动限制",
    launchGuardHint: "新建、指派与重新执行任务时，后端都会检查目标。",
    runtimeGuard: "执行期边界",
    runtimeGuardHint: "此版本限制任务入口；网络重新导向与工具流量仍需执行期网络策略。",
    historyGuard: "历史资料",
    historyGuardHint: "缩小范围不会删除旧任务，历史越界任务会保留供审计。",
    historyMarked: "个历史任务已保留并标记为越界。",
    ruleRequired: "限制模式至少需要一个完整规则。",
    status: "状态",
    updated: "最后活动",
    unknown: "未知",
  },
  en: {
    back: "Back to projects",
    newTask: "New task",
    overview: "Overview",
    tasks: "Tasks",
    findings: "Findings",
    scope: "Scope",
    live: "Live",
    skills: "Skills",
    risk: "High-risk records",
    exposure: "Exposure lineage",
    asset: "Target",
    task: "Task",
    finding: "Finding",
    viewAll: "View all",
    showing: "Showing",
    highRiskFirst: "High risk first",
    loading: "Loading…",
    noExposure: "Once completed tasks record findings, target-to-task lineage appears here.",
    skillHits: "Skill loads",
    hits: "loads",
    distinct: "distinct skills",
    noSkills: "No skill activity recorded yet.",
    expandSkills: "Show all",
    collapseSkills: "Collapse list",
    recentTasks: "Recent tasks",
    allTasks: "Task history",
    searchTasks: "Search tasks or targets",
    allStatuses: "All statuses",
    clearFilters: "Clear filters",
    noMatches: "No tasks match these filters.",
    noTasks: "This project has no tasks yet.",
    taskReport: "Open task report",
    projectReport: "Project report",
    reportNone: "Not generated",
    reportReady: "Ready",
    reportHistory: "Version history",
    reportOutdated: "Outdated",
    generated: "Generated",
    included: "Included tasks",
    completedTasks: "Completed tasks",
    excluded: "Excluded",
    generate: "Generate report",
    regenerate: "Regenerate",
    generating: "Generating…",
    openReport: "Open report",
    reportUnavailable: "At least one completed task with a final report is required.",
    reportFailed: "Could not generate project report",
    download: "Download Markdown",
    closeReport: "Close report",
    webFindings: "Web findings",
    internalFindings: "Internal findings",
    noFindings: "No findings in this project yet.",
    dataUnavailable: "Data is temporarily unavailable.",
    source: "Source",
    target: "Target",
    unrestricted: "Unrestricted",
    unrestrictedHint: "Use * to let the project launch against any target.",
    restricted: "Restricted",
    restrictedHint: "Allow only the domains, IPs, and CIDRs below.",
    domain: "Domain",
    ip: "IP",
    cidr: "CIDR",
    includeSubdomains: "Include subdomains",
    addRule: "Add target",
    ruleType: "Rule type",
    removeRule: "Remove rule",
    saveScope: "Save scope",
    saving: "Saving…",
    revision: "Policy revision",
    launchGuard: "Launch guard",
    launchGuardHint: "The backend checks targets when tasks are created, assigned, or rerun.",
    runtimeGuard: "Runtime boundary",
    runtimeGuardHint: "This release guards task entry; redirects and tool traffic still need runtime network policy.",
    historyGuard: "History retained",
    historyGuardHint: "Narrowing scope never deletes old tasks; historical exceptions remain auditable.",
    historyMarked: "historical tasks were retained and marked out of scope.",
    ruleRequired: "Restricted mode needs at least one complete rule.",
    status: "Status",
    updated: "Last activity",
    unknown: "Unknown",
  },
} as const;

const TABS: Array<{ id: WorkspaceTab; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "overview", icon: Activity },
  { id: "tasks", icon: ListChecks },
  { id: "findings", icon: Fingerprint },
  { id: "scope", icon: ShieldCheck },
];

function relativeTime(value: string | null | undefined, locale: string): string {
  if (!value) return "—";
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return "—";
  const seconds = Math.round((time - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (absolute < 60) return formatter.format(seconds, "second");
  if (absolute < 3_600) return formatter.format(Math.round(seconds / 60), "minute");
  if (absolute < 86_400) return formatter.format(Math.round(seconds / 3_600), "hour");
  return formatter.format(Math.round(seconds / 86_400), "day");
}

function statusLabel(status: string, locale: "zh-CN" | "en"): string {
  const zh: Record<string, string> = {
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    interrupted: "已中断",
    pending: "等待中",
  };
  return locale === "zh-CN" ? zh[status.toLowerCase()] ?? status : status;
}

function scopeLabel(project: ProjectSummary, unrestricted: string): string {
  const rules = project.scope_rules ?? [];
  if (!rules.length || rules.some((rule) => rule.kind === "any")) return `* · ${unrestricted}`;
  return rules.length === 1 ? rules[0].value : `${rules[0].value} +${rules.length - 1}`;
}

export default function ProjectDetailPage() {
  const { t } = useI18n();
  return (
    <React.Suspense fallback={<div className="panel flex items-center gap-2 p-6"><Spinner /> {t("common.loading")}</div>}>
      <ProjectWorkspace />
    </React.Suspense>
  );
}

function ProjectWorkspace() {
  const { locale, t } = useI18n();
  const copy = COPY[locale];
  const searchParams = useSearchParams();
  const projectId = searchParams.get("id") ?? "";
  const requestedTab = searchParams.get("tab") as WorkspaceTab | null;
  const [tab, setTab] = React.useState<WorkspaceTab>(
    requestedTab && TABS.some((entry) => entry.id === requestedTab) ? requestedTab : "overview"
  );
  const [project, setProject] = React.useState<ProjectSummary | null>(null);
  const [runs, setRuns] = React.useState<RunSummary[]>([]);
  const [findings, setFindings] = React.useState<ProjectFindings | null>(null);
  const [skills, setSkills] = React.useState<ProjectSkillAnalytics | null>(null);
  const [reports, setReports] = React.useState<ProjectReportVersion[]>([]);
  const [secondaryStatus, setSecondaryStatus] = React.useState<SecondaryStatus>({
    findings: "loading",
    skills: "loading",
    reports: "loading",
  });
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [generating, setGenerating] = React.useState(false);
  const [openReport, setOpenReport] = React.useState<ProjectReportVersion | null>(null);
  const loadSequence = React.useRef(0);

  const load = React.useCallback(async () => {
    if (!projectId) return;
    const sequence = ++loadSequence.current;
    setGenerating(false);
    setPhase("loading");
    setFindings(null);
    setSkills(null);
    setReports([]);
    setSecondaryStatus({ findings: "loading", skills: "loading", reports: "loading" });
    try {
      const detail = await getProjectRuns(projectId);
      if (sequence !== loadSequence.current) return;
      setProject(detail.project);
      setRuns(detail.runs);
      setPhase("ready");
      const [findingResult, skillResult, reportResult] = await Promise.allSettled([
        getProjectFindings(projectId),
        getProjectSkillAnalytics(projectId),
        getProjectReports(projectId),
      ]);
      if (sequence !== loadSequence.current) return;
      if (findingResult.status === "fulfilled") setFindings(findingResult.value);
      if (skillResult.status === "fulfilled") setSkills(skillResult.value);
      if (reportResult.status === "fulfilled") setReports(reportResult.value.reports);
      setSecondaryStatus({
        findings: findingResult.status === "fulfilled" ? "ready" : "error",
        skills: skillResult.status === "fulfilled" ? "ready" : "error",
        reports: reportResult.status === "fulfilled" ? "ready" : "error",
      });
    } catch {
      if (sequence === loadSequence.current) setPhase("error");
    }
  }, [projectId]);

  React.useEffect(() => {
    void load();
    setOpenReport(null);
    return () => { loadSequence.current += 1; };
  }, [load]);

  React.useEffect(() => {
    setTab(requestedTab && TABS.some((entry) => entry.id === requestedTab) ? requestedTab : "overview");
  }, [requestedTab, projectId]);

  const selectTab = (next: WorkspaceTab) => {
    setTab(next);
    const query = new URLSearchParams(window.location.search);
    query.set("tab", next);
    window.history.replaceState(null, "", `${window.location.pathname}?${query.toString()}`);
  };

  const handleTabKey = (event: React.KeyboardEvent<HTMLButtonElement>, current: WorkspaceTab) => {
    const index = TABS.findIndex((entry) => entry.id === current);
    const nextIndex = event.key === "ArrowRight" ? (index + 1) % TABS.length
      : event.key === "ArrowLeft" ? (index - 1 + TABS.length) % TABS.length
        : event.key === "Home" ? 0
          : event.key === "End" ? TABS.length - 1
            : null;
    if (nextIndex === null) return;
    event.preventDefault();
    const next = TABS[nextIndex].id;
    selectTab(next);
    event.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`#workspace-tab-${next}`)?.focus();
  };

  if (!projectId || phase === "error") {
    return (
      <div className="panel">
        <EmptyState
          title={t("common.error")}
          hint={!projectId ? t("projects.detail.missingId") : t("projects.detail.notFound")}
          action={<Link href="/projects" className="button-primary">{copy.back}</Link>}
        />
      </div>
    );
  }
  if (phase === "loading" || !project) {
    return <div className="panel flex items-center gap-2 p-6"><Spinner /> {t("common.loading")}</div>;
  }

  const findingCount = findings
    ? findings.vulnerabilities.length + findings.internal.length
    : project.vulnerability_count + project.internal_finding_count;
  const completedRuns = runs.filter((run) => run.status.toLowerCase() === "completed");
  const latestReport = reports[0] ?? null;

  const generate = async () => {
    const sequence = loadSequence.current;
    setGenerating(true);
    try {
      const report = await generateProjectReport(project.id, locale);
      if (sequence !== loadSequence.current) return;
      setReports((current) => [report, ...current.filter((entry) => entry.version_id !== report.version_id)]);
      setSecondaryStatus((current) => ({ ...current, reports: "ready" }));
      setOpenReport(report);
      toast.success(copy.reportReady);
    } catch (error) {
      if (sequence === loadSequence.current) toast.error(copy.reportFailed, { description: String(error) });
    } finally {
      if (sequence === loadSequence.current) setGenerating(false);
    }
  };

  const showStoredReport = () => { if (latestReport) setOpenReport(latestReport); };

  return (
    <div className={styles.projectsRoot}>
      <header className={styles.workspaceHeader}>
        <div className={styles.workspaceHeading}>
          <Link href="/projects" className={styles.workspaceBack}>
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" /> {copy.back}
          </Link>
          <h1 className={styles.workspaceTitle}>{project.name}</h1>
          {project.description && <p className={styles.workspaceDescription}>{project.description}</p>}
          <div className={styles.workspaceHeaderMeta}>
            <button type="button" className={styles.scopeBadge} onClick={() => selectTab("scope")} title={scopeLabel(project, copy.unrestricted)}>
              <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" /><span>{scopeLabel(project, copy.unrestricted)}</span>
            </button>
            <span className={styles.workspaceActivity}>{copy.updated} · {relativeTime(project.last_run_at, locale)}</span>
          </div>
        </div>
        <div className={styles.workspaceHeaderActions}>
          <Link href={`/scan?project_id=${encodeURIComponent(project.id)}`} className="button-primary">
            <Plus className="h-4 w-4" aria-hidden="true" /> {copy.newTask}
          </Link>
        </div>
      </header>

      <nav className={styles.workspaceTabs} role="tablist" aria-label={project.name}>
        {TABS.map(({ id, icon: Icon }) => {
          const count = id === "tasks" ? runs.length : id === "findings" ? findingCount : null;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              id={`workspace-tab-${id}`}
              aria-controls="workspace-panel"
              aria-selected={tab === id}
              tabIndex={tab === id ? 0 : -1}
              className={`${styles.workspaceTab} ${tab === id ? styles.workspaceTabActive : ""}`}
              onClick={() => selectTab(id)}
              onKeyDown={(event) => handleTabKey(event, id)}
            >
              <Icon className="h-3.5 w-3.5" aria-hidden="true" />
              {copy[id]}
              {count !== null && <span className={styles.tabCount}>{String(count).padStart(2, "0")}</span>}
            </button>
          );
        })}
      </nav>

      <section role="tabpanel" id="workspace-panel" aria-labelledby={`workspace-tab-${tab}`} tabIndex={0} className={styles.workspacePanel}>
      {tab === "overview" && (
        <Overview
          key={project.id}
          project={project}
          runs={runs}
          findings={findings}
          skills={skills}
          latestReport={latestReport}
          reportCount={reports.length}
          completedRuns={completedRuns.length}
          copy={copy}
          locale={locale}
          generating={generating}
          secondaryStatus={secondaryStatus}
          onGenerate={generate}
          onOpenReport={showStoredReport}
          onRetry={() => void load()}
          onTab={selectTab}
        />
      )}
      {tab === "tasks" && <TaskPanel key={project.id} projectId={project.id} runs={runs} copy={copy} locale={locale} title={copy.allTasks} />}
      {tab === "findings" && <FindingsPanel findings={findings} status={secondaryStatus.findings} copy={copy} />}
      {tab === "scope" && <ScopeEditor project={project} copy={copy} onSaved={async () => { await load(); }} />}
      </section>

      {openReport && <ProjectReportReader key={project.id} projectId={project.id} initialReport={openReport} versions={reports} onClose={() => setOpenReport(null)} />}
    </div>
  );
}

type Copy = (typeof COPY)[keyof typeof COPY];

function Overview({ project, runs, findings, skills, latestReport, reportCount, completedRuns, copy, locale, generating, secondaryStatus, onGenerate, onOpenReport, onRetry, onTab }: {
  project: ProjectSummary;
  runs: RunSummary[];
  findings: ProjectFindings | null;
  skills: ProjectSkillAnalytics | null;
  latestReport: ProjectReportVersion | null;
  reportCount: number;
  completedRuns: number;
  copy: Copy;
  locale: "zh-CN" | "en";
  generating: boolean;
  secondaryStatus: SecondaryStatus;
  onGenerate: () => Promise<void>;
  onOpenReport: () => void;
  onRetry: () => void;
  onTab: (tab: WorkspaceTab) => void;
}) {
  const [skillsExpanded, setSkillsExpanded] = React.useState(false);
  const skillListId = React.useId();
  const skillEntries = skills?.skills ?? skills?.top_skills ?? [];
  const visibleSkills = skillsExpanded ? skillEntries : skillEntries.slice(0, 5);
  const maxHits = Math.max(1, ...skillEntries.map((entry) => entry.hits));
  const latestStatus = secondaryStatus.reports === "loading"
    ? copy.loading
    : secondaryStatus.reports === "error"
    ? copy.dataUnavailable
    : latestReport
      ? latestReport.stale ? copy.reportOutdated : copy.reportReady
      : copy.reportNone;

  return (
    <>
      <div className={styles.statusStrip} aria-label={copy.status}>
        <StatusCell value={project.live_count} label={copy.live} />
        <StatusCell value={project.run_count} label={copy.tasks} />
        <StatusCell value={secondaryStatus.skills !== "ready" ? "—" : skills?.totals.distinct_skills ?? 0} label={copy.skills} />
        <StatusCell value={secondaryStatus.findings === "ready" && findings ? [...findings.vulnerabilities, ...findings.internal].filter((finding) => ["critical", "high"].includes((finding.severity || "").trim().toLowerCase())).length : "—"} label={copy.risk} />
      </div>
      <section className={styles.reportStrip} aria-labelledby="project-report-title" aria-busy={generating || secondaryStatus.reports === "loading"}>
        <span className={styles.reportGlyph} aria-hidden="true"><FileText className="h-5 w-5" /></span>
        <div className={styles.reportStripContent}>
          <div className={styles.reportStripHeading}>
            <h2 id="project-report-title">{copy.projectReport}</h2>
            <span className={`${styles.reportStatus} ${latestReport?.stale ? styles.reportStatusStale : ""}`}>{latestStatus}</span>
          </div>
          <div className={styles.reportMeta}>
            {latestReport && <span>{copy.generated} · {relativeTime(latestReport.generated_at, locale)}</span>}
            <span>{latestReport ? copy.included : copy.completedTasks} · {latestReport?.included_run_count ?? completedRuns}</span>
            {!!latestReport?.excluded_runs?.length && <span>{copy.excluded} · {latestReport.excluded_runs.length}</span>}
            {reportCount > 0 && <button type="button" className={styles.reportHistoryLink} onClick={onOpenReport}><History size={12} aria-hidden="true" />{copy.reportHistory} · {reportCount}</button>}
          </div>
          {completedRuns === 0 && <p className={styles.reportHint}>{copy.reportUnavailable}</p>}
        </div>
        <div className={styles.reportActions}>
          {latestReport && <button className="button-primary button-compact" onClick={() => void onOpenReport()}><FileText className="h-3.5 w-3.5" aria-hidden="true" />{copy.openReport}</button>}
          <button className={`${latestReport ? "button-secondary" : "button-primary"} button-compact`} onClick={() => void onGenerate()} disabled={generating || completedRuns === 0 || secondaryStatus.reports === "loading"}>
            {generating ? <Spinner /> : <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />}{generating ? copy.generating : latestReport ? copy.regenerate : copy.generate}
          </button>
        </div>
      </section>
      <div className={styles.overviewGrid}>
        <ExposureExplorer projectId={project.id} runs={runs} findings={findings} status={secondaryStatus.findings} onViewAll={() => onTab("findings")} onRetry={onRetry} />

        <div className={styles.overviewSide}>
          <Panel code="SKL" title={copy.skillHits}>
            <div className={styles.skillBody}>
              {secondaryStatus.skills === "ready" && <div className={styles.skillSummary}>
                <strong>{skills?.totals.total_hits ?? 0}</strong> {copy.hits} · {skills?.totals.distinct_skills ?? 0} {copy.distinct}
              </div>}
              {skillEntries.length === 0 ? <div className={styles.compactEmpty}>{secondaryStatus.skills === "loading" ? copy.loading : secondaryStatus.skills === "error" ? copy.dataUnavailable : copy.noSkills}</div> : (
                <ul id={skillListId} className={`${styles.skillBars} ${skillsExpanded ? styles.skillBarsExpanded : ""}`} aria-label={copy.skillHits} tabIndex={skillsExpanded ? 0 : undefined}>
                  {visibleSkills.map((entry) => (
                    <li key={entry.skill}>
                      <div className={styles.skillBarHead}><span className={styles.skillName} title={entry.skill}>{entry.skill}</span><span className={styles.barValue}>{entry.hits} / {entry.runs} {copy.tasks}</span></div>
                      <div className={styles.barTrack}><div className={styles.barFill} style={{ width: `${Math.max(4, (entry.hits / maxHits) * 100)}%` }} /></div>
                    </li>
                  ))}
                </ul>
              )}
              {skillEntries.length > 5 && (
                <button type="button" className={styles.skillToggle} aria-expanded={skillsExpanded} aria-controls={skillListId} onClick={() => setSkillsExpanded((expanded) => !expanded)}>
                  {skillsExpanded ? <ChevronUp size={14} aria-hidden="true" /> : <ChevronDown size={14} aria-hidden="true" />}
                  {skillsExpanded ? copy.collapseSkills : `${copy.expandSkills} (${skillEntries.length})`}
                </button>
              )}
            </div>
          </Panel>
        </div>

        <div className={styles.recentTasks}>
          <TaskPanel runs={runs.slice(0, 5)} copy={copy} locale={locale} title={copy.recentTasks} onViewAll={() => onTab("tasks")} />
        </div>
      </div>
    </>
  );
}

function StatusCell({ value, label }: { value: number | string; label: string }) {
  const display = typeof value === "number" ? String(value).padStart(2, "0") : value;
  return <div className={styles.statusCell}><strong>{display}</strong><span className={styles.metaLabel}>{label}</span></div>;
}

function TaskPanel({ runs, copy, locale, title, onViewAll, projectId }: { runs: RunSummary[]; copy: Copy; locale: "zh-CN" | "en"; title: string; onViewAll?: () => void; projectId?: string }) {
  const storageKey = projectId ? `strixops_project_tasks:${projectId}` : "";
  const [filters, setFilters] = React.useState(() => {
    const fallback = { query: "", status: "all" };
    if (!storageKey) return fallback;
    try {
      const saved = JSON.parse(readStorage(storageKey) || "null");
      return saved && typeof saved.query === "string" && typeof saved.status === "string"
        ? { query: saved.query, status: saved.status } : fallback;
    } catch { return fallback; }
  });
  React.useEffect(() => {
    if (storageKey) writeStorage(storageKey, JSON.stringify(filters));
  }, [storageKey, filters]);
  const statuses = [...new Set([...runs.map((run) => run.status.toLowerCase()), ...(filters.status === "all" ? [] : [filters.status])])].sort();
  const query = filters.query.trim().toLowerCase();
  const filtered = projectId ? runs.filter((run) =>
    (filters.status === "all" || run.status.toLowerCase() === filters.status) &&
    (!query || `${run.name} ${runTargets(run).join(" ")}`.toLowerCase().includes(query))
  ) : runs;
  const hasFilters = Boolean(query || filters.status !== "all");
  return (
    <Panel code="RUN" title={title} actions={onViewAll ? <button className="button-ghost button-compact" onClick={onViewAll}>{copy.viewAll}</button> : <span className="micro-label" role="status">{filtered.length} / {runs.length}</span>}>
      {projectId && <div className={styles.taskFilters}>
        <input type="search" className="input-shell" aria-label={copy.searchTasks} placeholder={copy.searchTasks} value={filters.query} onChange={(event) => setFilters((current) => ({ ...current, query: event.target.value }))} />
        <Select
          className={`input-shell ${styles.statusSelect}`}
          aria-label={copy.status}
          value={filters.status}
          onValueChange={(status) => setFilters((current) => ({ ...current, status }))}
          options={[
            { value: "all", label: copy.allStatuses },
            ...statuses.map((status) => ({ value: status, label: statusLabel(status, locale) })),
          ]}
        />
        {hasFilters && <button type="button" className="button-ghost button-compact" onClick={() => setFilters({ query: "", status: "all" })}>{copy.clearFilters}</button>}
      </div>}
      {filtered.length === 0 ? <div className={styles.compactEmpty}>{hasFilters ? copy.noMatches : copy.noTasks}</div> : <div className={styles.taskList}>
        {filtered.map((run) => (
          <Link key={run.name} href={`/run?name=${encodeURIComponent(run.name)}&tab=${run.live ? "conversation" : "report"}`} className={styles.taskRow} aria-label={`${copy.taskReport}: ${run.name}`}>
            <StatusPill status={run.status} live={run.live} label={statusLabel(run.status, locale)} />
            <span className={styles.taskIdentity}><strong title={runTargets(run).join("\n")}>{runTargetLabel(run)}</strong><span className={styles.taskMeta}>{run.name} · {(run.scan_type || "web").toUpperCase()}</span></span>
            <span className={styles.taskFindingMeta}>
              <span className="mono-chip">{run.vulnerability_count} FND</span>
              {run.internal_finding_count > 0 && <span className="mono-chip">{run.internal_finding_count} INT</span>}
              {run.scope_match === false && <span className="status-pill status-warning">OUT OF SCOPE</span>}
            </span>
            <span className={styles.taskTime}>{relativeTime(run.start_time, locale)}</span>
            <ArrowRight className={`${styles.taskArrow} h-3.5 w-3.5`} aria-hidden="true" />
          </Link>
        ))}
      </div>}
    </Panel>
  );
}

function FindingsPanel({ findings, status, copy }: { findings: ProjectFindings | null; status: SecondaryStatus["findings"]; copy: Copy }) {
  if (status === "error") return <Panel code="FND" title={copy.findings}><div className={styles.compactEmpty}>{copy.dataUnavailable}</div></Panel>;
  if (!findings) return <div className="panel flex items-center gap-2 p-6"><Spinner /></div>;
  const rows = [
    ...findings.vulnerabilities.map((finding) => ({ ...finding, group: copy.webFindings, target: finding.target || finding.endpoint || "—" })),
    ...findings.internal.map((finding) => ({
      id: finding.id,
      title: finding.title || finding.finding_type || copy.internalFindings,
      severity: finding.severity || "info",
      source_run: finding.source_run,
      target: finding.host || "—",
      group: copy.internalFindings,
    })),
  ];
  return (
    <Panel code="FND" title={copy.findings} actions={<span className="mono-chip">{rows.length}</span>}>
      {rows.length === 0 ? <div className={styles.compactEmpty}>{copy.noFindings}</div> : <div className={styles.findingList}>
        {rows.map((finding) => (
          <Link key={`${finding.source_run}/${finding.id}`} href={`/run?name=${encodeURIComponent(finding.source_run)}&tab=findings`} className={styles.findingRow}>
            <SeverityChip severity={finding.severity} />
            <span className={styles.findingTitle}>{finding.title}</span>
            <span className={styles.findingTarget}>{finding.target}</span>
            <span className={styles.findingSource}>{copy.source} · {finding.source_run}</span>
          </Link>
        ))}
      </div>}
    </Panel>
  );
}

function ScopeEditor({ project, copy, onSaved }: { project: ProjectSummary; copy: Copy; onSaved: (project: ProjectSummary) => void | Promise<void> }) {
  const storedRules = project.scope_rules ?? [{ kind: "any", value: "*" } as ProjectScopeRule];
  const initiallyRestricted = !storedRules.some((rule) => rule.kind === "any");
  const [restricted, setRestricted] = React.useState(initiallyRestricted);
  const [rules, setRules] = React.useState<ProjectScopeRule[]>(
    initiallyRestricted ? storedRules : [{ kind: "domain", value: "", include_subdomains: false }]
  );
  const [saving, setSaving] = React.useState(false);

  React.useEffect(() => {
    const next = project.scope_rules ?? [{ kind: "any", value: "*" } as ProjectScopeRule];
    const isRestricted = !next.some((rule) => rule.kind === "any");
    setRestricted(isRestricted);
    setRules(isRestricted ? next : [{ kind: "domain", value: "", include_subdomains: false }]);
  }, [project.id, project.scope_revision, project.scope_rules]);

  const updateRule = (index: number, patch: Partial<ProjectScopeRule>) => {
    setRules((current) => current.map((rule, position) => position === index ? ({ ...rule, ...patch } as ProjectScopeRule) : rule));
  };
  const changeKind = (index: number, kind: "domain" | "ip" | "cidr") => {
    updateRule(index, kind === "domain" ? { kind, value: "", include_subdomains: false } : { kind, value: "" });
  };
  const addRule = () => setRules((current) => [...current, { kind: "domain", value: "", include_subdomains: false }]);
  const valid = !restricted || (rules.length > 0 && rules.every((rule) => rule.kind !== "any" && rule.value.trim()));

  const save = async () => {
    if (!valid || saving) return;
    setSaving(true);
    try {
      const nextRules: ProjectScopeRule[] = restricted
        ? rules.map((rule) => ({ ...rule, value: rule.value.trim() }) as ProjectScopeRule)
        : [{ kind: "any", value: "*" as const }];
      const result = await updateProjectScope(project.id, nextRules);
      await onSaved(result.project);
      toast.success(copy.saveScope, {
        description: result.historical_out_of_scope.length
          ? `${result.historical_out_of_scope.length} ${copy.historyMarked}`
          : undefined,
      });
    } catch (error) {
      toast.error(copy.saveScope, { description: String(error) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.scopeLayout}>
      <Panel code="SCP" title={copy.scope}>
        <div className={styles.scopeBody}>
          <div className={styles.scopeModes}>
            <button type="button" className={`${styles.scopeMode} ${!restricted ? styles.scopeModeActive : ""}`} onClick={() => setRestricted(false)} aria-pressed={!restricted}>
              <span className={styles.radioDot} /><span><strong>* · {copy.unrestricted}</strong><small>{copy.unrestrictedHint}</small></span>
            </button>
            <button type="button" className={`${styles.scopeMode} ${restricted ? styles.scopeModeActive : ""}`} onClick={() => setRestricted(true)} aria-pressed={restricted}>
              <span className={styles.radioDot} /><span><strong>{copy.restricted}</strong><small>{copy.restrictedHint}</small></span>
            </button>
          </div>
          {restricted && <div className={styles.scopeRuleList}>
            {rules.map((rule, index) => (
              <div className={styles.scopeRule} key={`${index}-${rule.kind}`}>
                <Select
                  className="input-shell h-10 px-2 font-mono text-[13px]"
                  aria-label={`${copy.ruleType} ${index + 1}`}
                  value={rule.kind}
                  onValueChange={(value) => changeKind(index, value as "domain" | "ip" | "cidr")}
                  options={[
                    { value: "domain", label: copy.domain },
                    { value: "ip", label: copy.ip },
                    { value: "cidr", label: copy.cidr },
                  ]}
                />
                <input className="input-shell h-10 font-mono text-[13px]" aria-label={`${copy.target} ${index + 1}`} value={rule.value} placeholder={rule.kind === "domain" ? "example.com" : rule.kind === "ip" ? "203.0.113.10" : "10.20.0.0/16"} onChange={(event) => updateRule(index, { value: event.target.value })} />
                {rule.kind === "domain" && <label className={styles.scopeSubdomains}><input type="checkbox" checked={Boolean(rule.include_subdomains)} onChange={(event) => updateRule(index, { include_subdomains: event.target.checked })} /> {copy.includeSubdomains}</label>}
                <button type="button" className={styles.scopeRemove} onClick={() => setRules((current) => current.filter((_, position) => position !== index))} aria-label={`${copy.removeRule} ${index + 1}`}><Trash2 className="h-3.5 w-3.5" /></button>
              </div>
            ))}
            <button type="button" className="button-secondary button-compact justify-self-start" onClick={addRule}><Plus className="h-3.5 w-3.5" /> {copy.addRule}</button>
          </div>}
          <div className={styles.scopeFooter}>
            <span className={styles.scopeRevision}>{copy.revision} · {String(project.scope_revision ?? 1).padStart(2, "0")}</span>
            <button type="button" className="button-primary" disabled={!valid || saving} onClick={() => void save()}>{saving ? <Spinner /> : <Check className="h-4 w-4" />}{saving ? copy.saving : copy.saveScope}</button>
          </div>
          {!valid && <p className="mt-3 text-xs text-danger">{copy.ruleRequired}</p>}
        </div>
      </Panel>
      <Panel code="POL" title={copy.status}>
        <div className={styles.scopeNotes}>
          <ScopeNote icon={ShieldCheck} title={copy.launchGuard} body={copy.launchGuardHint} />
          <ScopeNote icon={Network} title={copy.runtimeGuard} body={copy.runtimeGuardHint} />
          <ScopeNote icon={CircleAlert} title={copy.historyGuard} body={copy.historyGuardHint} />
        </div>
      </Panel>
    </div>
  );
}

function ScopeNote({ icon: Icon, title, body }: { icon: React.ComponentType<{ className?: string }>; title: string; body: string }) {
  return <div className={styles.scopeNote}><Icon className="h-3.5 w-3.5" /><div><strong>{title}</strong><p>{body}</p></div></div>;
}
