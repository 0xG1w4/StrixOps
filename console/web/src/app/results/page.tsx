"use client";

/* ============================================================================
   /results — project-first report directory.

   Projects are the primary unit, with their scan tasks directly beneath.
   A task opens its report; artifacts remain an explicit secondary action.
   ========================================================================= */

import * as React from "react";
import { RotateCw, Search, WifiOff } from "lucide-react";
import ProjectRunDirectory from "@/components/ProjectRunDirectory";
import { EmptyState, Panel, Spinner } from "@/components/ui";
import { getJSON, getProjects } from "@/lib/api";
import type { ProjectsPage, RunsPage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const RESULTS_POLL_MS = 15000;

export default function ResultsPage() {
  const { t } = useI18n();
  const [runsPage, setRunsPage] = React.useState<RunsPage | null>(null);
  const [projectsPage, setProjectsPage] = React.useState<ProjectsPage | null>(null);
  const [offline, setOffline] = React.useState(false);
  const [error, setError] = React.useState("");
  const [query, setQuery] = React.useState("");

  const load = React.useCallback(async () => {
    try {
      const [nextRuns, nextProjects] = await Promise.all([
        getJSON<RunsPage>("/api/runs"),
        getProjects(),
      ]);
      setRunsPage(nextRuns);
      setProjectsPage(nextProjects);
      setOffline(false);
      setError("");
    } catch (loadError) {
      setOffline(true);
      setError(loadError instanceof Error ? loadError.message : String(loadError));
    }
  }, []);

  React.useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, RESULTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const booting = (runsPage === null || projectsPage === null) && !offline;
  const hasSnapshot = runsPage !== null && projectsPage !== null;

  return (
    <div className="flex min-w-0 flex-col gap-3.5">
      <section className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">&gt; {t("results.path")}</div>
          <h1 className="page-title">{t("results.title")}</h1>
          <p className="page-copy">{t("results.hint")}</p>
        </div>
        <div className="hero-actions">
          <button
            type="button"
            className="button-secondary button-compact"
            onClick={() => void load()}
            title={t("dashboard.sync.hint")}
          >
            <RotateCw className="h-3.5 w-3.5" strokeWidth={1.8} />
            {t("dashboard.sync")}
          </button>
        </div>
      </section>

      <Panel
        code="RPT"
        title={t("results.tree")}
        actions={
          <div className="relative w-full sm:w-72">
            <Search
              className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-muted"
              strokeWidth={1.8}
            />
            <input
              type="search"
              className="input-shell min-h-10 pl-9 font-mono text-xs tracking-wide"
              placeholder={t("results.search")}
              aria-label={t("results.search")}
              autoComplete="off"
              spellCheck={false}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
        }
      >
        {offline && hasSnapshot && (
          <div className="alert-warning mx-4 mt-3 flex flex-wrap items-center gap-3">
            <WifiOff className="h-4 w-4 shrink-0" strokeWidth={1.8} />
            <span className="min-w-0">{t("results.staleSnapshot")}</span>
          </div>
        )}

        {booting ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2, 3].map((index) => (
              <div key={index} className="skeleton-line w-2/3" />
            ))}
            <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
              {t("common.loading")}
            </p>
          </div>
        ) : !hasSnapshot ? (
          <div className="space-y-3 p-4">
            <div className="alert-error flex flex-wrap items-center gap-3">
              <WifiOff className="h-4 w-4 shrink-0" strokeWidth={1.8} />
              <span className="min-w-0 break-words">
                <strong className="font-semibold">{t("common.offline")}</strong>
                {error ? ` — ${error}` : ""}
              </span>
            </div>
            <EmptyState title={t("common.offline")} hint={t("results.reconnectHint")} />
          </div>
        ) : (
          <ProjectRunDirectory
            projects={projectsPage.projects}
            runs={runsPage.runs}
            query={query}
            emptyHint={t("results.emptyHint")}
          />
        )}

        {hasSnapshot && (
          <div className="project-directory-footer">
            <span>{t("results.taskCount", { n: runsPage.runs.length })}</span>
            <span className="inline-flex items-center gap-1.5">
              {offline ? (
                <>
                  <Spinner className="h-3 w-3" />
                  {t("common.loading")}
                </>
              ) : (
                <>
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" aria-hidden="true" />
                  {t("results.synced")}
                </>
              )}
            </span>
          </div>
        )}
      </Panel>
    </div>
  );
}
