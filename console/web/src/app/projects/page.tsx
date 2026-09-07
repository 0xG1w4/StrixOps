"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, Check, FolderKanban, Pencil, Plus, RotateCw, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { createProject, deleteProject, getProjects, updateProject, type ProjectSummary } from "@/lib/api";
import { EmptyState, Panel, Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import styles from "./projects.module.css";

type ScopeRule = {
  kind: "any" | "domain" | "ip" | "cidr";
  value: string;
  include_subdomains?: boolean;
};

type WorkspaceProject = ProjectSummary & {
  scope_rules?: ScopeRule[];
  scope_revision?: number;
};

const PROJECT_COLORS = [
  { key: "gold", hex: "#b97a29", labelKey: "projects.color.gold" },
  { key: "cyan", hex: "#536ddb", labelKey: "projects.color.cyan" },
  { key: "violet", hex: "#755bb9", labelKey: "projects.color.violet" },
  { key: "success", hex: "#368c78", labelKey: "projects.color.success" },
  { key: "danger", hex: "#cf565b", labelKey: "projects.color.danger" },
  { key: "neutral", hex: "#68768a", labelKey: "projects.color.neutral" },
] as const;

const COLOR_BY_KEY = Object.fromEntries(PROJECT_COLORS.map((entry) => [entry.key, entry.hex]));

const COPY = {
  "zh-CN": {
    subtitle: "选择项目，进入独立的任务、发现与范围工作区。",
    index: "项目索引",
    search: "搜索项目",
    tasks: "任务",
    live: "运行中",
    risk: "高风险",
    scope: "范围",
    unrestricted: "不限制",
    updated: "最后活动",
    never: "暂无活动",
    total: "个项目",
    unassigned: "个任务尚未归入项目",
    open: "打开项目",
    edit: "编辑项目",
    remove: "删除项目",
    removeConfirm: "再次点击确认删除",
    noMatch: "没有匹配的项目",
    noMatchHint: "尝试缩短关键词或清除搜索。",
  },
  en: {
    subtitle: "Choose a project to enter its tasks, findings, and scope workspace.",
    index: "Project index",
    search: "Search projects",
    tasks: "Tasks",
    live: "Live",
    risk: "High risk",
    scope: "Scope",
    unrestricted: "Unrestricted",
    updated: "Last activity",
    never: "No activity",
    total: "projects",
    unassigned: "tasks are not assigned to a project",
    open: "Open project",
    edit: "Edit project",
    remove: "Delete project",
    removeConfirm: "Click again to delete",
    noMatch: "No matching projects",
    noMatchHint: "Try a shorter keyword or clear the search.",
  },
} as const;

function highRisk(project: ProjectSummary): number {
  return Object.entries(project.severity ?? {}).reduce((sum, [severity, count]) => {
    return ["critical", "high"].includes(severity.toLowerCase()) ? sum + count : sum;
  }, 0);
}

function scopeSummary(project: WorkspaceProject, unrestricted: string): string {
  const rules = project.scope_rules ?? [];
  if (rules.length === 0 || rules.some((rule) => rule.kind === "any" || rule.value === "*")) {
    return `* · ${unrestricted}`;
  }
  const first = rules[0]?.value || "*";
  return rules.length > 1 ? `${first} +${rules.length - 1}` : first;
}

function relativeTime(value: string | null | undefined, locale: string, empty: string): string {
  if (!value) return empty;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return empty;
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const absolute = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  if (absolute < 60) return formatter.format(seconds, "second");
  if (absolute < 3_600) return formatter.format(Math.round(seconds / 60), "minute");
  if (absolute < 86_400) return formatter.format(Math.round(seconds / 3_600), "hour");
  if (absolute < 1_209_600) return formatter.format(Math.round(seconds / 86_400), "day");
  return new Intl.DateTimeFormat(locale, { month: "short", day: "numeric", year: "numeric" }).format(date);
}

export default function ProjectsPage() {
  const { locale, t } = useI18n();
  const copy = COPY[locale];
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [projects, setProjects] = React.useState<WorkspaceProject[]>([]);
  const [unassignedCount, setUnassignedCount] = React.useState(0);
  const [modal, setModal] = React.useState<{ mode: "create" } | { mode: "edit"; project: WorkspaceProject } | null>(null);
  const [query, setQuery] = React.useState("");

  const reload = React.useCallback(async () => {
    try {
      const page = await getProjects();
      setProjects(page.projects as WorkspaceProject[]);
      setUnassignedCount(page.unassigned_count);
      setPhase("ready");
    } catch {
      setPhase("error");
    }
  }, []);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const filteredProjects = React.useMemo(() => {
    const term = query.trim().toLowerCase();
    if (!term) return projects;
    return projects.filter((project) =>
      [project.name, project.description, project.id, ...(project.scope_rules ?? []).map((rule) => rule.value)]
        .some((value) => (value || "").toLowerCase().includes(term))
    );
  }, [projects, query]);

  return (
    <div className={styles.projectsRoot}>
      <header className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">&gt; {t("projects.path")}</div>
          <h1 className="page-title">{t("projects.title")}</h1>
          <p className="page-copy">{copy.subtitle}</p>
        </div>
        <div className="hero-actions">
          <button className="button-primary" onClick={() => setModal({ mode: "create" })}>
            <Plus className="h-4 w-4" aria-hidden="true" /> {t("projects.new")}
          </button>
        </div>
      </header>

      {phase === "error" && (
        <div className="alert-error mb-4 flex items-center justify-between">
          <span>{t("common.offline")}</span>
          <button className="button-secondary button-compact" onClick={() => void reload()}>
            <RotateCw className="h-3.5 w-3.5" aria-hidden="true" /> {t("common.retry")}
          </button>
        </div>
      )}

      {phase === "loading" ? (
        <div className="panel panel-hairline flex items-center gap-2 p-6 text-sm text-fg-muted">
          <Spinner /> {t("common.loading")}
        </div>
      ) : (
        <Panel
          code="INDEX"
          title={copy.index}
          actions={
            <div className="relative w-full sm:w-72">
              <Search
                className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted"
                strokeWidth={1.8}
                aria-hidden="true"
              />
              <input
                type="search"
                className="input-shell min-h-10 pl-9 font-mono text-xs tracking-wide"
                placeholder={copy.search}
                aria-label={copy.search}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
          }
        >
          {filteredProjects.length === 0 ? (
            <EmptyState
              title={query ? copy.noMatch : t("projects.noProjects")}
              hint={query ? copy.noMatchHint : t("projects.empty.hint")}
              action={query ? undefined : (
                <button className="button-primary" onClick={() => setModal({ mode: "create" })}>
                  <Plus className="h-4 w-4" aria-hidden="true" /> {t("projects.new")}
                </button>
              )}
            />
          ) : (
            <div className={styles.projectList}>
              {filteredProjects.map((project, index) => {
                const color = COLOR_BY_KEY[project.color] ?? COLOR_BY_KEY.cyan;
                return (
                  <article
                    key={project.id}
                    className={styles.projectRow}
                    style={{ "--project-color": color } as React.CSSProperties}
                  >
                    <Link
                      href={`/projects/detail?id=${encodeURIComponent(project.id)}`}
                      className={styles.projectLink}
                      aria-label={`${copy.open}: ${project.name}`}
                    >
                      <span className={styles.projectGlyph} aria-hidden="true">
                        <FolderKanban className="h-4 w-4" strokeWidth={1.7} />
                      </span>
                      <span className={styles.projectIdentity}>
                        <span className={styles.projectCode}>[PRJ-{String(index + 1).padStart(2, "0")}]</span>
                        <span className={styles.projectName}>{project.name}</span>
                        {project.description && <span className={styles.projectDescription}>{project.description}</span>}
                      </span>
                      <span className={styles.projectStats}>
                        <span className={styles.projectStat}>
                          <strong>{String(project.run_count).padStart(2, "0")}</strong>
                          <span className={styles.metaLabel}>{copy.tasks}</span>
                        </span>
                        <span className={`${styles.projectStat} ${styles.projectStatLive}`}>
                          <strong>{String(project.live_count).padStart(2, "0")}</strong>
                          <span className={styles.metaLabel}>{copy.live}</span>
                        </span>
                        <span className={`${styles.projectStat} ${styles.projectStatRisk}`}>
                          <strong>{String(highRisk(project)).padStart(2, "0")}</strong>
                          <span className={styles.metaLabel}>{copy.risk}</span>
                        </span>
                        <span className={styles.projectStat}>
                          <strong title={scopeSummary(project, copy.unrestricted)}>{scopeSummary(project, copy.unrestricted)}</strong>
                          <span className={styles.metaLabel}>{copy.scope}</span>
                        </span>
                      </span>
                      <ArrowRight className={`${styles.rowChevron} h-4 w-4`} aria-hidden="true" />
                    </Link>
                    <div className={styles.projectActions}>
                      <button
                        className={styles.rowAction}
                        onClick={() => setModal({ mode: "edit", project })}
                        aria-label={`${copy.edit}: ${project.name}`}
                        title={copy.edit}
                      >
                        <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                      <DeleteAction project={project} label={copy.remove} confirmLabel={copy.removeConfirm} onDeleted={reload} />
                    </div>
                  </article>
                );
              })}
              <footer className={styles.directoryFooter}>
                <span>{projects.length.toString().padStart(2, "0")} {copy.total}</span>
                <span>{copy.updated}: {relativeTime(projects[0]?.last_run_at, locale, copy.never)}</span>
                {unassignedCount > 0 && <span>{unassignedCount.toString().padStart(2, "0")} {copy.unassigned}</span>}
              </footer>
            </div>
          )}
        </Panel>
      )}

      {modal && (
        <ProjectModal
          key={modal.mode === "edit" ? `edit-${modal.project.id}` : "create"}
          mode={modal.mode}
          project={modal.mode === "edit" ? modal.project : undefined}
          onClose={() => setModal(null)}
          onSaved={() => {
            setModal(null);
            void reload();
          }}
        />
      )}
    </div>
  );
}

function DeleteAction({ project, label, confirmLabel, onDeleted }: {
  project: WorkspaceProject;
  label: string;
  confirmLabel: string;
  onDeleted: () => Promise<void>;
}) {
  const { t } = useI18n();
  const [armed, setArmed] = React.useState(false);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), 3200);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const remove = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    try {
      await deleteProject(project.id);
      toast.success(t("common.saved"));
      await onDeleted();
    } catch (error) {
      toast.error(t("common.error"), { description: String(error) });
    } finally {
      setBusy(false);
      setArmed(false);
    }
  };

  return (
    <button
      className={`${styles.rowAction} ${armed ? "text-danger" : ""}`}
      onClick={() => void remove()}
      disabled={busy}
      aria-label={`${armed ? confirmLabel : label}: ${project.name}`}
      title={armed ? confirmLabel : label}
    >
      {busy ? <Spinner /> : <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />}
    </button>
  );
}

function ProjectModal({ mode, project, onClose, onSaved }: {
  mode: "create" | "edit";
  project?: WorkspaceProject;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const isEdit = mode === "edit";
  const [name, setName] = React.useState(project?.name ?? "");
  const [description, setDescription] = React.useState(project?.description ?? "");
  const [color, setColor] = React.useState(project?.color ?? "gold");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const save = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      if (isEdit && project) {
        await updateProject(project.id, { name: name.trim(), description: description.trim(), color });
      } else {
        await createProject({ name: name.trim(), description: description.trim(), color });
      }
      toast.success(t("common.saved"));
      onSaved();
    } catch (caught) {
      setError(String(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm sm:p-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="project-dialog-title"
    >
      <div className="panel w-full max-w-lg animate-enter">
        <div className="border-b border-line/6 px-5 py-4">
          <div className="eyebrow">{isEdit ? t("common.edit") : t("projects.new")}</div>
          <h2 id="project-dialog-title" className="mt-2 font-mono text-base font-bold text-fg">
            {isEdit ? project?.name : t("projects.new")}
          </h2>
        </div>
        <div className="space-y-4 px-5 py-5">
          <div>
            <label className="field-label" htmlFor="project-name">{t("projects.name")}</label>
            <input
              id="project-name"
              className="input mt-1.5"
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void save();
              }}
            />
          </div>
          <div>
            <label className="field-label" htmlFor="project-description">{t("projects.description")}</label>
            <textarea
              id="project-description"
              className="textarea-shell mt-1.5 min-h-20"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          <fieldset>
            <legend className="field-label">{t("projects.color")}</legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {PROJECT_COLORS.map((entry) => (
                <button
                  key={entry.key}
                  type="button"
                  onClick={() => setColor(entry.key)}
                  className={cn(
                    "flex items-center gap-2 rounded-lg border px-3 py-2 text-xs transition-all",
                    color === entry.key ? "border-accent/30 bg-accent/10" : "border-line/8 hover:border-line/14"
                  )}
                  aria-pressed={color === entry.key}
                >
                  <span className="h-3 w-3 rounded-full" style={{ background: entry.hex }} aria-hidden="true" />
                  {t(entry.labelKey)}
                  {color === entry.key && <Check className="h-3 w-3 text-accent" aria-hidden="true" />}
                </button>
              ))}
            </div>
          </fieldset>
          {error && <div className="alert-error">{error}</div>}
          <div className="flex justify-end gap-2 pt-1">
            <button className="button-secondary" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
            <button className="button-primary" onClick={() => void save()} disabled={busy || !name.trim()}>
              {busy ? <Spinner /> : <Check className="h-4 w-4" aria-hidden="true" />}
              {t("common.save")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
