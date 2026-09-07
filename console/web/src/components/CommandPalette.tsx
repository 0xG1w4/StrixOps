"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { usePathname, useRouter } from "next/navigation";
import {
  ArrowUpRight, BarChart3, Crosshair, FileSearch, FolderOpen, LibraryBig,
  LoaderCircle, Radar, Search, SlidersHorizontal, X, type LucideIcon,
} from "lucide-react";
import { getJSON, getProjects, type ProjectSummary, type RunSummary, type RunsPage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./CommandPalette.module.css";

const COPY = {
  "zh-CN": {
    title: "快速跳转", trigger: "搜索", placeholder: "搜索项目、任务或页面…",
    description: "搜索项目和任务，或跳转页面。使用上下方向键选择，Enter 打开。",
    projects: "项目", runs: "任务 · 报告", pages: "页面与操作", newTask: "新建任务",
    projectError: "项目加载失败", runError: "任务加载失败", cached: "保留上次结果",
    retry: "重试", loading: "正在同步…", empty: "没有匹配结果", tryAgain: "试试项目名、任务名或目标地址。",
    report: "查看报告", tasks: "个任务", move: "选择", select: "打开", close: "关闭",
    refine: "缩小搜索范围可查看更多结果", resultCount: "个匹配结果",
  },
  en: {
    title: "Quick navigation", trigger: "Search", placeholder: "Search projects, tasks or pages…",
    description: "Search projects and tasks, or navigate to a page. Use arrow keys to select and Enter to open.",
    projects: "Projects", runs: "Tasks · reports", pages: "Pages & actions", newTask: "New task",
    projectError: "Could not load projects", runError: "Could not load tasks", cached: "Showing previous results",
    retry: "Retry", loading: "Syncing…", empty: "No matching results", tryAgain: "Try a project name, task name or target.",
    report: "View report", tasks: "tasks", move: "Select", select: "Open", close: "Close",
    refine: "Refine your search to see more results", resultCount: "matching results",
  },
};

interface Entry {
  id: string;
  label: string;
  detail?: string;
  meta?: string;
  href: string;
  search: string;
  icon: LucideIcon;
  mono?: boolean;
}

interface Group {
  id: string;
  label: string;
  entries: Entry[];
  total: number;
}

const CACHE_MS = 30_000;

export default function CommandPalette() {
  const { locale, t } = useI18n();
  const copy = COPY[locale];
  const router = useRouter();
  const pathname = usePathname();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [activeId, setActiveId] = React.useState("");
  const [projects, setProjects] = React.useState<ProjectSummary[]>([]);
  const [runs, setRuns] = React.useState<RunSummary[]>([]);
  const [errors, setErrors] = React.useState({ projects: false, runs: false });
  const [loading, setLoading] = React.useState(false);
  const [shortcut, setShortcut] = React.useState("Ctrl K");
  const loadedAt = React.useRef(0);
  const pending = React.useRef(false);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const contentRef = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const previousFocus = React.useRef<HTMLElement | null>(null);
  const listId = React.useId();

  const refresh = React.useCallback(async () => {
    if (pending.current) return;
    pending.current = true;
    setLoading(true);
    // Each source settles independently so a slow endpoint never hides results
    // from the healthy one. Failed refreshes deliberately retain prior data.
    const [projectsOk, runsOk] = await Promise.all([
      getProjects().then((page) => {
        setProjects(page.projects);
        setErrors((previous) => ({ ...previous, projects: false }));
        return true;
      }, () => {
        setErrors((previous) => ({ ...previous, projects: true }));
        return false;
      }),
      getJSON<RunsPage>("/api/runs").then((page) => {
        setRuns(page.runs);
        setErrors((previous) => ({ ...previous, runs: false }));
        return true;
      }, () => {
        setErrors((previous) => ({ ...previous, runs: true }));
        return false;
      }),
    ]);
    loadedAt.current = projectsOk && runsOk ? Date.now() : 0;
    pending.current = false;
    setLoading(false);
  }, []);

  const changeOpen = React.useCallback((nextOpen: boolean) => {
    if (nextOpen) {
      // Do not stack global navigation on an active editor, confirmation or drawer.
      const otherDialog = Array.from(document.querySelectorAll<HTMLElement>(
        '[role="dialog"], [role="alertdialog"], dialog[open]',
      )).some((element) => element !== contentRef.current && element.getClientRects().length > 0);
      if (otherDialog) return;
      previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      setQuery("");
      setActiveId("");
    }
    setOpen(nextOpen);
  }, []);

  React.useEffect(() => {
    setShortcut(/Mac|iPhone|iPad|iPod/.test(navigator.platform) ? "⌘ K" : "Ctrl K");
    const handleShortcut = (event: KeyboardEvent) => {
      if (event.isComposing || event.defaultPrevented || event.altKey || event.shiftKey) return;
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "k") return;
      const otherDialog = Array.from(document.querySelectorAll<HTMLElement>(
        '[role="dialog"], [role="alertdialog"], dialog[open]',
      )).some((element) => element !== contentRef.current && element.getClientRects().length > 0);
      if (otherDialog) return;
      event.preventDefault();
      changeOpen(!open);
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [changeOpen, open]);

  React.useEffect(() => { setOpen(false); }, [pathname]);
  React.useEffect(() => {
    if (open && Date.now() - loadedAt.current > CACHE_MS) void refresh();
  }, [open, refresh]);

  const groups = React.useMemo((): Group[] => {
    const needles = query.trim().toLocaleLowerCase(locale).split(/\s+/).filter(Boolean);
    const matches = (entry: Entry) => needles.every((needle) => entry.search.toLocaleLowerCase(locale).includes(needle));
    const projectNames = new Map(projects.map((project) => [project.id, project.name]));
    const projectEntries: Entry[] = [...projects]
      .sort((a, b) => (b.last_run_at || b.updated_at).localeCompare(a.last_run_at || a.updated_at))
      .map((project) => ({
        id: `project:${project.id}`, label: project.name,
        detail: `${project.run_count} ${copy.tasks}`,
        href: `/projects/detail?id=${encodeURIComponent(project.id)}`,
        search: `${project.name} ${project.description} ${project.id}`, icon: FolderOpen,
      })).filter(matches);
    const runEntries: Entry[] = [...runs]
      .sort((a, b) => (b.start_time || "").localeCompare(a.start_time || ""))
      .map((run) => ({
        id: `run:${run.name}`, label: run.target || run.name, detail: run.name,
        // Run summaries do not attest report availability. This opens the report
        // view, whose own loading/empty state remains authoritative.
        meta: `${t(`status.${run.status}`) === `status.${run.status}` ? t("status.unknown") : t(`status.${run.status}`)} · ${copy.report}`,
        href: `/run?name=${encodeURIComponent(run.name)}&tab=report`,
        search: `${run.name} ${run.target} ${projectNames.get(run.project_id) ?? ""} ${run.status} ${copy.report}`,
        icon: FileSearch, mono: true,
      })).filter(matches);
    const pages: Array<{ href: string; key: string; icon: LucideIcon; words: string }> = [
      { href: "/scan", key: "newTask", icon: Crosshair, words: "new create scan launch 新建 创建 扫描 启动" },
      { href: "/projects", key: "nav.projects", icon: FolderOpen, words: "projects 项目" },
      { href: "/results", key: "nav.results", icon: FileSearch, words: "reports results 报告" },
      { href: "/", key: "nav.runs", icon: Radar, words: "overview dashboard 总览 仪表板" },
      { href: "/insights", key: "nav.insights", icon: BarChart3, words: "insights analytics 洞察 分析" },
      { href: "/skills", key: "nav.skills", icon: LibraryBig, words: "skills library 技能" },
      { href: "/settings", key: "nav.settings", icon: SlidersHorizontal, words: "settings 设置" },
    ];
    const pageEntries = pages.map((page) => {
      const label = page.key === "newTask" ? copy.newTask : t(page.key);
      return { id: `page:${page.href}`, label, detail: page.href, href: page.href,
        search: `${label} ${page.words}`, icon: page.icon };
    }).filter(matches);
    return [
      { id: "projects", label: copy.projects, entries: projectEntries.slice(0, 5), total: projectEntries.length },
      { id: "runs", label: copy.runs, entries: runEntries.slice(0, 6), total: runEntries.length },
      { id: "pages", label: copy.pages, entries: pageEntries, total: pageEntries.length },
    ].filter((group) => group.entries.length > 0);
  }, [query, projects, runs, locale, copy, t]);

  const entries = React.useMemo(() => groups.flatMap((group) => group.entries), [groups]);
  const selected = entries.find((entry) => entry.id === activeId) ?? entries[0];
  const selectedIndex = selected ? entries.findIndex((entry) => entry.id === selected.id) : -1;
  const resultCount = groups.reduce((sum, group) => sum + group.total, 0);
  const capped = resultCount > entries.length;

  React.useEffect(() => {
    if (!open || selectedIndex < 0) return;
    document.getElementById(`${listId}-option-${selectedIndex}`)?.scrollIntoView({ block: "nearest" });
  }, [open, listId, selectedIndex]);

  const navigate = (entry: Entry) => {
    setOpen(false);
    router.push(entry.href);
  };

  const onInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      if (entries.length) setActiveId(entries[(selectedIndex + direction + entries.length) % entries.length].id);
    } else if (event.key === "Enter" && selected) {
      event.preventDefault();
      navigate(selected);
    }
  };

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Trigger asChild>
        <button ref={triggerRef} type="button" className={styles.trigger}
          aria-label={copy.title} aria-keyshortcuts="Meta+K Control+K" title={`${copy.title} (${shortcut})`}>
          <Search size={15} aria-hidden="true" />
          <span>{copy.trigger}</span><kbd>{shortcut}</kbd>
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content ref={contentRef} className={styles.content}
          onOpenAutoFocus={(event) => { event.preventDefault(); inputRef.current?.focus(); }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            const previous = previousFocus.current;
            const target = previous?.isConnected && previous !== document.body && previous !== document.documentElement ? previous : triggerRef.current;
            target?.focus();
          }}>
          <Dialog.Title className="sr-only">{copy.title}</Dialog.Title>
          <Dialog.Description className="sr-only">{copy.description}</Dialog.Description>
          <div className={styles.searchRow}>
            <span className={styles.prompt} aria-hidden="true">&gt;</span>
            <input ref={inputRef} value={query} onChange={(event) => { setQuery(event.target.value); setActiveId(""); }}
              onKeyDown={onInputKeyDown} placeholder={copy.placeholder} aria-label={copy.title}
              role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={listId}
              aria-activedescendant={selectedIndex >= 0 ? `${listId}-option-${selectedIndex}` : undefined}
              autoComplete="off" autoCorrect="off" spellCheck={false} />
            <Dialog.Close asChild><button type="button" className={styles.close} aria-label={copy.close}><X size={17} /></button></Dialog.Close>
          </div>
          {(errors.projects || errors.runs) && (
            <div className={styles.error} role="status">
              <span>
                {[errors.projects && copy.projectError, errors.runs && copy.runError].filter(Boolean).join(" · ")}
                {((errors.projects && projects.length > 0) || (errors.runs && runs.length > 0)) && ` · ${copy.cached}`}
              </span>
              <button type="button" disabled={loading} onClick={() => void refresh()}>{copy.retry}</button>
            </div>
          )}
          <div className={styles.results} id={listId} role="listbox" aria-label={copy.title} aria-busy={loading}>
            {groups.map((group) => (
              <div key={group.id} role="group" aria-labelledby={`${listId}-${group.id}`} className={styles.group}>
                <div className={styles.groupHeading} id={`${listId}-${group.id}`}>
                  <span>{group.label}</span><span>{group.entries.length < group.total ? `${group.entries.length} / ` : ""}{group.total}</span>
                </div>
                {group.entries.map((entry) => {
                  const index = entries.findIndex((item) => item.id === entry.id);
                  const Icon = entry.icon;
                  return (
                    <div key={entry.id} id={`${listId}-option-${index}`} role="option" aria-selected={selected?.id === entry.id}
                      className={styles.option} onPointerMove={() => setActiveId(entry.id)}
                      onMouseDown={(event) => event.preventDefault()} onClick={() => navigate(entry)}>
                      <Icon size={17} className={styles.optionIcon} aria-hidden="true" />
                      <span className={styles.optionCopy}>
                        <span className={styles.optionLabel}>{entry.label}</span>
                        {entry.detail && <span className={entry.mono ? styles.monoDetail : styles.detail}>{entry.detail}</span>}
                        {entry.meta && <span className={styles.meta}>{entry.meta}</span>}
                      </span>
                      <ArrowUpRight size={15} className={styles.arrow} aria-hidden="true" />
                    </div>
                  );
                })}
              </div>
            ))}
            {entries.length === 0 && !loading && <div className={styles.empty}><strong>{copy.empty}</strong><span>{copy.tryAgain}</span></div>}
          </div>
          <div className={styles.footer}>
            <span className={styles.sync} role="status" aria-live="polite">
              {loading ? <><LoaderCircle size={13} className={styles.spinner} aria-hidden="true" />{copy.loading}</> : <>{resultCount} {copy.resultCount}</>}
            </span>
            <span className={styles.keys}><kbd>↑↓</kbd>{copy.move}<kbd>↵</kbd>{copy.select}</span>
          </div>
          {capped && <p className={styles.capped}>{copy.refine}</p>}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
