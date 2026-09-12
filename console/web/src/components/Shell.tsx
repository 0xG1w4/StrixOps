"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTheme } from "next-themes";
import {
  BarChart3,
  Crosshair,
  FileSearch,
  FolderOpen,
  LibraryBig,
  Globe2,
  Layers,
  Menu,
  Moon,
  Radar,
  SlidersHorizontal,
  Sun,
  Waypoints,
  X,
  type LucideIcon,
} from "lucide-react";
import * as Dialog from "@radix-ui/react-dialog";
import { MatrixText } from "@/components/MatrixText";
import CommandPalette from "@/components/CommandPalette";
import { getJSON, runTargetLabel, runTargets, type Health, type RunSummary, type RunsPage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

interface NavItem {
  href: string;
  key: string;
  icon: LucideIcon;
}

interface NavGroup {
  key: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    key: "nav.group.operations",
    items: [
      { href: "/", key: "nav.runs", icon: Radar },
      { href: "/scan", key: "nav.scan", icon: Crosshair },
      { href: "/mcp", key: "nav.mcp", icon: Waypoints },
    ],
  },
  {
    key: "nav.group.manage",
    items: [
      { href: "/projects", key: "nav.projects", icon: FolderOpen },
      { href: "/batches", key: "nav.batches", icon: Layers },
      { href: "/results", key: "nav.results", icon: FileSearch },
    ],
  },
  {
    key: "nav.group.intelligence",
    items: [
      { href: "/fofa", key: "nav.fofa", icon: Globe2 },
      { href: "/skills", key: "nav.skills", icon: LibraryBig },
      { href: "/insights", key: "nav.insights", icon: BarChart3 },
    ],
  },
  {
    key: "nav.group.system",
    items: [{ href: "/settings", key: "nav.settings", icon: SlidersHorizontal }],
  },
];

const NAV_ITEMS = NAV_GROUPS.flatMap((group) => group.items);
const MOBILE_ITEMS = NAV_ITEMS.slice(0, 3);

function navIsActive(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/" || pathname.startsWith("/run");
  return pathname === href || pathname.startsWith(`${href}/`);
}

function currentPageKey(pathname: string): string {
  return NAV_ITEMS.find((item) => navIsActive(item.href, pathname))?.key ?? "nav.runs";
}

interface EngineStats {
  online: boolean;
  loaded: boolean;
  liveRuns: number;
  totalRuns: number;
  findings: number;
  primaryRun: RunSummary | null;
  lastSync: number | null;
}

function useEngineStats(): EngineStats {
  const [stats, setStats] = React.useState<EngineStats>({
    online: false,
    loaded: false,
    liveRuns: 0,
    totalRuns: 0,
    findings: 0,
    primaryRun: null,
    lastSync: null,
  });

  React.useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const health = await getJSON<Health>("/api/health");
        let page: RunsPage | null = null;
        try {
          page = await getJSON<RunsPage>("/api/runs");
        } catch {
          /* Health is authoritative; fleet details are optional enrichment. */
        }
        if (cancelled) return;
        setStats((previous) => ({
          online: Boolean(health.ok),
          loaded: true,
          liveRuns: health.live_runs ?? page?.totals?.live ?? previous.liveRuns,
          totalRuns: page?.totals?.runs ?? page?.runs.length ?? previous.totalRuns,
          findings: page
            ? Object.values(page.totals?.severity ?? {}).reduce((sum, count) => sum + count, 0)
            : previous.findings,
          primaryRun: page ? page.runs.find((run) => run.live) ?? null : previous.primaryRun,
          lastSync: page ? Date.now() : previous.lastSync,
        }));
      } catch {
        if (!cancelled) {
          setStats((previous) => ({ ...previous, online: false, loaded: true }));
        }
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  return stats;
}

function BrandMark() {
  return (
    <span className="signal-brand-mark" aria-hidden="true">
      <span />
      <span />
    </span>
  );
}

function BrandLockup() {
  const { t } = useI18n();
  return (
    <span className="signal-brand-lockup">
      <BrandMark />
      <span className="signal-wordmark">
        <MatrixText value="STRIXOPS" />
        <span className="signal-tagline">{t("shell.tagline")}</span>
      </span>
    </span>
  );
}

function NavLink({
  item,
  active,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  onNavigate?: () => void;
}) {
  const { t } = useI18n();
  const Icon = item.icon;
  const label = t(item.key);

  return (
    <Link
      href={item.href}
      className={cn("nav-link", active && "nav-link-active")}
      aria-current={active ? "page" : undefined}
      aria-label={label}
      title={label}
      onClick={onNavigate}
    >
      <span className="nav-active-caret" aria-hidden="true">&gt;</span>
      <span className="nav-icon">
        <Icon className="h-4 w-4" strokeWidth={1.8} />
      </span>
      <span className="nav-copy">{label}</span>
    </Link>
  );
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const { t } = useI18n();

  return (
    <nav className="sidebar-nav" aria-label={t("nav.primary")}>
      {NAV_GROUPS.map((group) => (
        <section className="nav-group" key={group.key} aria-label={t(group.key)}>
          <h2 className="nav-group-title">// {t(group.key)}</h2>
          <div className="nav-group-items">
            {group.items.map((item) => (
              <NavLink
                key={item.href}
                item={item}
                active={navIsActive(item.href, pathname)}
                onNavigate={onNavigate}
              />
            ))}
          </div>
        </section>
      ))}
    </nav>
  );
}

function EnginePanel({ stats }: { stats: EngineStats }) {
  const { t } = useI18n();
  const state = stats.online ? "online" : stats.loaded ? "offline" : "checking";

  return (
    <div className="engine-panel">
      <div className="engine-panel-line">
        <span className={cn("engine-status-dot", `engine-status-${state}`)} aria-hidden="true" />
        <span className="engine-label">ENGINE / {t("nav.engine")}</span>
        <strong className={cn("engine-state", `engine-state-${state}`)}>
          {t(`shell.${state}`)}
        </strong>
      </div>
      <div className="engine-panel-meta">
        <span>{stats.liveRuns.toString().padStart(2, "0")} LIVE</span>
        <span>{stats.totalRuns.toString().padStart(2, "0")} RUNS</span>
        <span>v1.2.3</span>
      </div>
    </div>
  );
}

function SidebarContent({
  stats,
  onNavigate,
}: {
  stats: EngineStats;
  onNavigate?: () => void;
}) {
  const { t } = useI18n();
  return (
    <div className="sidebar-content">
      <Link href="/" className="sidebar-brand" onClick={onNavigate} aria-label={t("shell.home")}>
        <BrandLockup />
      </Link>
      <NavList onNavigate={onNavigate} />
      <EnginePanel stats={stats} />
    </div>
  );
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const { t } = useI18n();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  const dark = !mounted || theme !== "light";

  return (
    <button
      type="button"
      className="shell-icon-button"
      onClick={() => setTheme(dark ? "light" : "dark")}
      aria-label={t("shell.toggleTheme")}
      title={dark ? t("settings.theme.light") : t("settings.theme.dark")}
    >
      {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </button>
  );
}

function LocaleSwitch() {
  const { locale, setLocale, t } = useI18n();
  return (
    <div className="locale-switch" role="group" aria-label={t("shell.language")}>
      <button
        type="button"
        className={locale === "zh-CN" ? "is-active" : undefined}
        aria-pressed={locale === "zh-CN"}
        onClick={() => setLocale("zh-CN")}
      >
        简
      </button>
      <button
        type="button"
        className={locale === "en" ? "is-active" : undefined}
        aria-pressed={locale === "en"}
        onClick={() => setLocale("en")}
      >
        EN
      </button>
    </div>
  );
}

function zuluTime(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`;
}

function CommandRail({ stats }: { stats: EngineStats }) {
  const { t } = useI18n();
  const [clock, setClock] = React.useState("");
  React.useEffect(() => {
    const update = () => setClock(zuluTime(new Date()));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, []);

  const run = stats.primaryRun;
  const active = stats.online && stats.liveRuns > 0;
  const state = !stats.loaded ? "checking" : !stats.online ? "offline" : active ? "live" : "ready";

  return (
    <section className={cn("command-rail", `command-rail-${state}`)} aria-label={t("shell.commandRail")}>
      <span className="command-live">
        <span className="command-live-dot" aria-hidden="true" />
        {active ? <MatrixText value="LIVE" /> : t(`shell.${state}`)}
      </span>
      {active && <span className="command-prompt" aria-hidden="true">ops@strix:~$</span>}
      {active && <span className="command-verb">watch</span>}
      {active && run ? (
        <Link href={`/run?name=${encodeURIComponent(run.name)}`} className="command-target">
          <strong title={runTargets(run).join("\n")}>{runTargetLabel(run)}</strong>
          <span>{run.name}</span>
        </Link>
      ) : (
        <span className="command-empty">{t(state === "offline" ? "shell.connectionLost" : state === "checking" || active ? "shell.syncing" : "shell.noActiveRun")}</span>
      )}
      {active && <span className="signal-dots" aria-hidden="true">
        {Array.from({ length: 7 }, (_, index) => <i key={index} />)}
      </span>}
      {active && <span className="command-count">{stats.liveRuns.toString().padStart(2, "0")} LIVE</span>}
      {active ? <time className="command-clock">{clock}</time> : stats.lastSync && (
        <time className="command-sync" dateTime={new Date(stats.lastSync).toISOString()}>{t("shell.syncedAt")} {zuluTime(new Date(stats.lastSync))}</time>
      )}
    </section>
  );
}

function MobileBottomNav({ onMore }: { onMore: () => void }) {
  const pathname = usePathname();
  const { t } = useI18n();

  return (
    <nav className="mobile-bottom-nav" aria-label={t("nav.mobile")}>
      {MOBILE_ITEMS.map((item) => {
        const Icon = item.icon;
        const active = navIsActive(item.href, pathname);
        return (
          <Link key={item.href} href={item.href} className={active ? "is-active" : undefined} aria-current={active ? "page" : undefined}>
            <Icon className="h-[18px] w-[18px]" strokeWidth={1.8} />
            <span>{t(item.key)}</span>
          </Link>
        );
      })}
      <button type="button" onClick={onMore}>
        <Menu className="h-[18px] w-[18px]" strokeWidth={1.8} />
        <span>{t("nav.more")}</span>
      </button>
    </nav>
  );
}

export default function Shell({ children }: { children: React.ReactNode }) {
  const stats = useEngineStats();
  const pathname = usePathname();
  const { t } = useI18n();
  const [navOpen, setNavOpen] = React.useState(false);

  React.useEffect(() => setNavOpen(false), [pathname]);

  return (
    <Dialog.Root open={navOpen} onOpenChange={setNavOpen}>
      <div className="app-shell">
        <a className="skip-link" href="#main-content">
          {t("shell.skipContent")}
        </a>
        <aside className="app-sidebar">
          <SidebarContent stats={stats} />
        </aside>

        <div className="app-main">
          <header className="shell-topbar">
            <div className="shell-topbar-leading">
              <Dialog.Trigger asChild>
                <button type="button" className="drawer-trigger" aria-label={t("shell.openMenu")}>
                  <Menu className="h-4 w-4" strokeWidth={1.8} />
                </button>
              </Dialog.Trigger>
              <Link href="/" className="mobile-brand" aria-label={t("shell.home")}>
                <MatrixText value="STRIXOPS" />
              </Link>
              <div className="shell-breadcrumb">
                <span>STRIXOPS</span>
                <span aria-hidden="true">/</span>
                <strong>{t(currentPageKey(pathname))}</strong>
              </div>
            </div>
            <div className="shell-topbar-actions">
              <CommandPalette />
              <LocaleSwitch />
              <ThemeToggle />
            </div>
          </header>

          <CommandRail stats={stats} />
          <main id="main-content" tabIndex={-1} className="page-container animate-enter">
            {children}
          </main>
          <MobileBottomNav onMore={() => setNavOpen(true)} />
        </div>
      </div>

      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay" />
        <Dialog.Content className="drawer-content" aria-label={t("nav.primary")}>
          <Dialog.Title className="sr-only">{t("nav.primary")}</Dialog.Title>
          <Dialog.Close asChild>
            <button type="button" className="drawer-close" aria-label={t("shell.closeMenu")}>
              <X className="h-4 w-4" />
            </button>
          </Dialog.Close>
          <SidebarContent stats={stats} onNavigate={() => setNavOpen(false)} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
