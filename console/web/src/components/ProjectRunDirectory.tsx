"use client";

import * as React from "react";
import Link from "next/link";
import { ChevronDown, ChevronRight, FileText, FolderOpen } from "lucide-react";
import { Chip, EmptyState, SeverityChip, StatusPill } from "@/components/ui";
import { runTargetLabel, runTargets, type ProjectSummary, type RunSummary } from "@/lib/api";
import { relTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

const PROJECT_COLORS: Record<string, string> = {
  gold: "#b97a29",
  cyan: "#536ddb",
  violet: "#755bb9",
  success: "#368c78",
  danger: "#cf565b",
  neutral: "#68768a",
};

const SEVERITIES = ["critical", "high", "medium", "low", "info"] as const;
const UNASSIGNED_ID = "__unassigned__";

type ProjectGroup = {
  id: string;
  project: ProjectSummary | null;
  name: string;
  description: string;
  color: string;
  runs: RunSummary[];
};

interface ProjectRunDirectoryProps {
  projects: ProjectSummary[];
  runs: RunSummary[];
  query?: string;
  renderProjectActions?: (project: ProjectSummary) => React.ReactNode;
  emptyAction?: React.ReactNode;
  emptyHint?: string;
}

function displayStatus(run: RunSummary): string {
  const status = (run.status || "unknown").toLowerCase();
  if (run.live) return "live";
  if (status === "failed" && (run.failure_reason || "").toLowerCase() === "interrupted") {
    return "stopped";
  }
  return status;
}

function countSeverity(run: RunSummary, severity: string): number {
  const key = Object.keys(run.severity ?? {}).find((item) => item.toLowerCase() === severity);
  return key ? run.severity[key] ?? 0 : 0;
}

function matchesRun(run: RunSummary, query: string): boolean {
  return [run.name, ...runTargets(run), run.scan_type, run.status]
    .some((value) => (value || "").toLowerCase().includes(query));
}

export default function ProjectRunDirectory({
  projects,
  runs,
  query = "",
  renderProjectActions,
  emptyAction,
  emptyHint,
}: ProjectRunDirectoryProps) {
  const { t, locale } = useI18n();
  const [collapsed, setCollapsed] = React.useState<Set<string>>(new Set());
  const normalizedQuery = query.trim().toLowerCase();

  const groups = React.useMemo<ProjectGroup[]>(() => {
    const knownIds = new Set(projects.map((project) => project.id));
    const grouped = new Map<string, RunSummary[]>();
    for (const project of projects) grouped.set(project.id, []);

    const unassigned: RunSummary[] = [];
    for (const run of runs) {
      const projectId = run.project_id || "";
      if (projectId && knownIds.has(projectId)) grouped.get(projectId)!.push(run);
      else unassigned.push(run);
    }

    const newestFirst = (a: RunSummary, b: RunSummary) =>
      (b.start_time || "").localeCompare(a.start_time || "");

    const projectGroups: ProjectGroup[] = projects.map((project) => ({
      id: project.id,
      project,
      name: project.name,
      description: project.description,
      color: PROJECT_COLORS[project.color] ?? PROJECT_COLORS.cyan,
      runs: (grouped.get(project.id) ?? []).sort(newestFirst),
    }));

    if (unassigned.length > 0) {
      projectGroups.push({
        id: UNASSIGNED_ID,
        project: null,
        name: t("projects.unassigned"),
        description: t("projects.unassigned.hint"),
        color: PROJECT_COLORS.neutral,
        runs: unassigned.sort(newestFirst),
      });
    }

    if (!normalizedQuery) return projectGroups;
    return projectGroups.flatMap((group) => {
      const projectMatch = [group.name, group.description]
        .some((value) => value.toLowerCase().includes(normalizedQuery));
      const matchingRuns = projectMatch
        ? group.runs
        : group.runs.filter((run) => matchesRun(run, normalizedQuery));
      return projectMatch || matchingRuns.length > 0 ? [{ ...group, runs: matchingRuns }] : [];
    });
  }, [normalizedQuery, projects, runs, t]);

  const toggleGroup = (id: string) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (groups.length === 0) {
    return (
      <EmptyState
        title={normalizedQuery ? t("dashboard.noMatch") : t("projects.noProjects")}
        hint={normalizedQuery ? t("projects.noMatch", { query: query.trim() }) : emptyHint ?? t("projects.empty.hint")}
        action={normalizedQuery ? undefined : emptyAction}
      />
    );
  }

  return (
    <div className="project-directory">
      {groups.map((group) => {
        const isCollapsed = collapsed.has(group.id);
        const liveCount = group.runs.filter((run) => run.live).length;
        const findingCount = group.runs.reduce(
          (sum, run) => sum + run.vulnerability_count + run.internal_finding_count,
          0
        );

        return (
          <section
            key={group.id}
            className="project-branch"
            style={{ "--project-color": group.color } as React.CSSProperties}
          >
            <header className="project-branch-header">
              <button
                type="button"
                className="project-branch-toggle"
                onClick={() => toggleGroup(group.id)}
                aria-expanded={!isCollapsed}
                aria-label={t(isCollapsed ? "projects.expand" : "projects.collapse", { name: group.name })}
              >
                <span className="project-branch-marker" aria-hidden="true" />
                <ChevronDown className={`project-branch-chevron ${isCollapsed ? "-rotate-90" : ""}`} />
                <span className="project-branch-copy">
                  <span className="project-branch-title">
                    <span className="project-node-code">[{group.project ? "PRJ" : "SYS"}]</span>
                    <strong>{group.name}</strong>
                  </span>
                  {group.description && <span className="project-branch-description">{group.description}</span>}
                </span>
              </button>

              <div className="project-branch-stats" aria-label={t("projects.summary", { name: group.name })}>
                {liveCount > 0 && <span className="project-stat-live">{liveCount.toString().padStart(2, "0")} LIVE</span>}
                <span>{group.runs.length.toString().padStart(2, "0")} {t("projects.tasks.short")}</span>
                <span>{findingCount.toString().padStart(2, "0")} {t("projects.findings.short")}</span>
              </div>

              <div className="project-branch-actions">
                {group.project && (
                  <Link
                    href={`/projects/detail?id=${encodeURIComponent(group.project.id)}`}
                    className="button-secondary button-compact"
                    title={t("projects.open")}
                  >
                    <FolderOpen className="h-3.5 w-3.5" />
                    <span>{t("projects.open")}</span>
                  </Link>
                )}
                {group.project && renderProjectActions?.(group.project)}
              </div>
            </header>

            {!isCollapsed && (
              <div className="project-task-list">
                {group.runs.length === 0 ? (
                  <div className="project-task-empty">
                    <span>[IDLE]</span>
                    <p>{t("projects.noTasks")}</p>
                  </div>
                ) : (
                  group.runs.map((run) => {
                    const status = displayStatus(run);
                    const statusKey = `status.${status}`;
                    const translatedStatus = t(statusKey);
                    const severityEntries = SEVERITIES
                      .map((severity) => [severity, countSeverity(run, severity)] as const)
                      .filter(([, count]) => count > 0);

                    return (
                      <div key={run.name} className="project-task-row">
                        <Link
                          href={`/run?name=${encodeURIComponent(run.name)}&tab=report`}
                          className="project-task-main"
                          aria-label={t("projects.openTaskReport", { name: runTargetLabel(run) })}
                        >
                          <span className={`project-task-signal ${run.live ? "is-live" : ""}`} aria-hidden="true" />
                          <span className="project-task-status">
                            <StatusPill
                              status={status}
                              live={run.live}
                              label={translatedStatus === statusKey ? run.status : translatedStatus}
                            />
                          </span>
                          <span className="project-task-identity">
                            <strong title={runTargets(run).join("\n")}>{runTargetLabel(run)}</strong>
                            <span>
                              <b>[RUN]</b>
                              {run.scan_type || "web"} · {run.name} · {run.start_time ? relTime(run.start_time, locale) : "—"}
                            </span>
                            {run.failure_reason && (
                              <em>{run.failure_reason === "interrupted" ? t("results.stopped") : run.failure_reason}</em>
                            )}
                          </span>
                          <span className="project-task-findings">
                            {severityEntries.length > 0 ? (
                              severityEntries.map(([severity, count]) => (
                                <span key={severity}>
                                  <SeverityChip severity={severity.toUpperCase()} />
                                  <b>{count}</b>
                                </span>
                              ))
                            ) : (
                              <span className="project-task-clear">{t("projects.noFindings")}</span>
                            )}
                            {run.internal_finding_count > 0 && (
                              <Chip tone="neutral">INT {run.internal_finding_count}</Chip>
                            )}
                          </span>
                          <span className="project-task-report">
                            <FileText className="h-3.5 w-3.5" />
                            {t("run.tab.report")}
                            <ChevronRight className="h-3.5 w-3.5" />
                          </span>
                        </Link>
                        <Link
                          href={`/run?name=${encodeURIComponent(run.name)}&tab=files`}
                          className="project-task-artifacts"
                          aria-label={t("projects.openTaskArtifacts", { name: runTargetLabel(run) })}
                          title={t("run.tab.artifacts")}
                        >
                          <FolderOpen className="h-3.5 w-3.5" />
                          <span>{t("run.tab.artifacts")}</span>
                        </Link>
                      </div>
                    );
                  })
                )}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
