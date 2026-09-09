"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import {
  ArrowLeft,
  Check,
  ChevronDown,
  CircleAlert,
  CircleCheck,
  Cpu,
  Globe,
  History,
  ListPlus,
  Network,
  Plus,
  Rocket,
  Settings2,
  Terminal,
} from "lucide-react";
import {
  getJSON,
  getProjects,
  getSettings,
  postJSON,
  type Health,
  type ModelProfile,
  type ProjectSummary,
  type RunSummary,
  type ScanLaunched,
} from "@/lib/api";
import { EmptyState, Spinner } from "@/components/ui";
import { MAX_TARGETS, MultiTargetEditor, useMultiTargetCheck } from "@/components/scan/MultiTargetEditor";
import { Select } from "@/components/Select";
import { useI18n } from "@/lib/i18n";
import { INSTRUCTION_KEY, readStorage, writeStorage } from "@/lib/storage";
import styles from "./scan.module.css";

const INSTRUCTION_SOFT_LIMIT = 4000;
type ScanMode = "web" | "internal";
type Translate = (key: string, vars?: Record<string, string | number>) => string;
type RecentTarget = { target: string; scan_type: ScanMode };
type LoadState = "loading" | "ready" | "error";
type ScopeResult = {
  allowed: boolean;
  normalized_target: string;
  reason: string;
  scope_revision: number;
};
type ScopeCheck = {
  key: string;
  state: "checking" | "allowed" | "blocked" | "error";
  failure?: "missing" | "invalid" | "network";
  result?: ScopeResult;
};

const COPY = {
  "zh-CN": {
    heading: "新建任务",
    setup: "任务配置",
    targetHint: "输入目标后检查格式与项目范围",
    scopeChecking: "正在检查项目范围…",
    scopeAllowed: "目标符合项目范围",
    scopeBlocked: "目标超出项目范围，请修改目标或项目。",
    scopeError: "暂时无法确认项目范围，请重试。",
    scopeMissing: "项目已不存在，请重新选择项目。",
    scopeInvalid: "目标或项目范围无效，请检查输入与范围设置。",
    scopeLabel: "项目范围",
    scopeSettings: "查看范围",
    scopeRetry: "重新检查",
    inputReady: "目标格式有效",
    inputRequired: "输入目标以继续",
    checkingEngine: "正在连接引擎…",
    configuration: "模型与连接",
    modelUnavailable: "请配置模型",
    modelLoading: "正在加载模型…",
    network: "内网连接",
    optional: "可选",
    direct: "直连",
    draft: "已保存草稿",
    noInstruction: "使用默认指令",
    savedInstruction: "已保存的指令会用于本次任务",
    moreTargets: "最近目标",
    noProject: "未分类",
    selectedProjectMissing: "所选项目不可用",
    followActive: "跟随当前模型配置",
    launchHint: "使用当前配置启动",
    profileRequired: "选择模型配置后即可启动",
    manageProfiles: "管理模型",
    status: "启动状态",
    multiTarget: "多目标任务",
    singleTarget: "返回单目标",
    singleMode: "单目标任务",
    multiLaunch: "启动多目标任务",
    multiReady: "个目标 · 一次任务 · 一份报告",
    importingTargets: "正在导入目标清单…",
  },
  en: {
    heading: "New task",
    setup: "Task setup",
    targetHint: "Enter a target to check its format and project scope",
    scopeChecking: "Checking project scope…",
    scopeAllowed: "Target is within project scope",
    scopeBlocked: "Target is outside project scope. Change the target or project.",
    scopeError: "Could not verify project scope. Try again.",
    scopeMissing: "This project no longer exists. Select another project.",
    scopeInvalid: "The target or project scope is invalid. Check both settings.",
    scopeLabel: "Project scope",
    scopeSettings: "View scope",
    scopeRetry: "Check again",
    inputReady: "Target format is valid",
    inputRequired: "Enter a target to continue",
    checkingEngine: "Connecting to the engine…",
    configuration: "Model and connection",
    modelUnavailable: "Configure a model",
    modelLoading: "Loading models…",
    network: "Internal connection",
    optional: "Optional",
    direct: "Direct",
    draft: "Saved draft",
    noInstruction: "Use default instructions",
    savedInstruction: "Your saved instructions will be used for this task",
    moreTargets: "Recent targets",
    noProject: "Unassigned",
    selectedProjectMissing: "Selected project is unavailable",
    followActive: "Follow the active model profile",
    launchHint: "Launch with the current configuration",
    profileRequired: "Select a model profile to launch",
    manageProfiles: "Manage models",
    status: "Launch status",
    multiTarget: "Multi-target task",
    singleTarget: "Back to single target",
    singleMode: "Single-target task",
    multiLaunch: "Launch multi-target task",
    multiReady: "targets · One task · One report",
    importingTargets: "Importing target list…",
  },
};

function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

const IPV4_RE = /^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/;
const HOST_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/i;

function isHost(host: string): boolean {
  return IPV4_RE.test(host) || HOST_RE.test(host) || host.toLowerCase() === "localhost";
}

function validateWebTarget(raw: string, t: Translate): string | null {
  const value = raw.trim();
  if (/\s/.test(value)) return t("scan.validation.targetSpaces");
  if (/^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value);
      if (!url.hostname) return t("scan.validation.urlMissingHost");
      return null;
    } catch {
      return t("scan.validation.urlInvalid");
    }
  }
  const index = value.lastIndexOf(":");
  if (index !== -1) {
    const host = value.slice(0, index);
    const port = value.slice(index + 1);
    if (!/^\d{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
      return t("scan.validation.portInvalid", { port });
    }
    return isHost(host) ? null : t("scan.validation.webTarget");
  }
  return isHost(value) ? null : t("scan.validation.webTarget");
}

function validateInternalTarget(raw: string, t: Translate): string | null {
  const value = raw.trim();
  if (/\s/.test(value)) return t("scan.validation.scopeSpaces");
  if (value.includes("/")) {
    const parts = value.split("/");
    if (parts.length > 2) return t("scan.validation.cidrSingle");
    const [ip, prefix] = parts;
    if (!/^\d{1,2}$/.test(prefix) || Number(prefix) > 32) {
      return t("scan.validation.prefix");
    }
    return IPV4_RE.test(ip) ? null : t("scan.validation.cidrIpv4");
  }
  return isHost(value) ? null : t("scan.validation.internalTarget");
}

function validateSocks5(raw: string, t: Translate): string | null {
  const value = raw.trim();
  if (!value) return null;
  const match = value.match(/^(?:socks5h?:\/\/)?(.+?):(\d{1,5})$/i);
  if (!match) return t("scan.validation.socksFormat");
  const port = Number(match[2]);
  if (port < 1 || port > 65535) return t("scan.validation.portRange");
  if (!isHost(match[1])) return t("scan.validation.proxyHost");
  return null;
}

function validateGsocket(raw: string, t: Translate): string | null {
  if (raw.trim() && raw.trim().length < 8) return t("scan.validation.gsocketShort");
  return null;
}

function parseLaunchError(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const statusMatch = raw.match(/^(\d{3}):\s*([\s\S]*)$/);
  const status = statusMatch?.[1] ?? "";
  const body = statusMatch?.[2] ?? raw;
  try {
    const parsed = JSON.parse(body) as { detail?: unknown };
    if (typeof parsed.detail === "string") {
      return status ? `${status} ${parsed.detail}` : parsed.detail;
    }
    if (Array.isArray(parsed.detail)) {
      const messages = parsed.detail.map((detail) =>
        detail && typeof detail === "object" && "msg" in detail
          ? String((detail as { msg: unknown }).msg)
          : String(detail)
      );
      return status ? `${status} ${messages.join("; ")}` : messages.join("; ");
    }
  } catch {
    // A non-JSON response is still useful to show when launch fails.
  }
  return raw;
}

function FieldError({ children }: { children: React.ReactNode }) {
  return (
    <p className={styles.error}>
      <CircleAlert size={14} aria-hidden="true" />
      <span>{children}</span>
    </p>
  );
}

export default function ScanLauncherPage() {
  const router = useRouter();
  const { t, locale } = useI18n();
  const copy = COPY[locale];
  const [mode, setMode] = React.useState<ScanMode>("web");
  const [target, setTarget] = React.useState("");
  const [multiple, setMultiple] = React.useState(false);
  const [targetsText, setTargetsText] = React.useState("");
  const [multiRetry, setMultiRetry] = React.useState(0);
  const [importingTargets, setImportingTargets] = React.useState(false);
  const [socks5, setSocks5] = React.useState("");
  const [gsocket, setGsocket] = React.useState("");
  const [crypto, setCrypto] = React.useState(false);
  const [instruction, setInstruction] = React.useState("");
  const [language, setLanguage] = React.useState<"zh-CN" | "en">("zh-CN");
  const [projectId, setProjectId] = React.useState("");
  const [projects, setProjects] = React.useState<ProjectSummary[]>([]);
  const [projectsState, setProjectsState] = React.useState<LoadState>("loading");
  const [profiles, setProfiles] = React.useState<ModelProfile[]>([]);
  const [activeProfileId, setActiveProfileId] = React.useState<string | null>(null);
  const [profileId, setProfileId] = React.useState("");
  const [profilesState, setProfilesState] = React.useState<LoadState>("loading");
  const [busy, setBusy] = React.useState(false);
  const [launchError, setLaunchError] = React.useState("");
  const [engineOnline, setEngineOnline] = React.useState<boolean | null>(null);
  const [recent, setRecent] = React.useState<RecentTarget[]>([]);
  const [recentState, setRecentState] = React.useState<LoadState>("loading");
  const [scopeCheck, setScopeCheck] = React.useState<ScopeCheck | null>(null);
  const [scopeRetry, setScopeRetry] = React.useState(0);
  const [hydrated, setHydrated] = React.useState(false);
  const internal = mode === "internal";
  const trimmedTarget = target.trim();
  const multiValidation = useMultiTargetCheck({ enabled: multiple, text: targetsText, scanType: mode, projectId, retry: multiRetry });

  React.useEffect(() => {
    const saved = readStorage(INSTRUCTION_KEY);
    if (typeof saved === "string" && saved) setInstruction(saved);
    const params = new URLSearchParams(window.location.search);
    const requestedProject = params.get("project_id");
    if (requestedProject) setProjectId(requestedProject);
    const requestedTarget = params.get("target");
    if (requestedTarget) setTarget(requestedTarget);
    const requestedMode = params.get("scan_type");
    if (requestedMode === "web" || requestedMode === "internal") setMode(requestedMode);
    try {
      const requestedTargets: unknown = JSON.parse(params.get("targets") || "null");
      if (Array.isArray(requestedTargets) && requestedTargets.length >= 2 && requestedTargets.length <= MAX_TARGETS
        && requestedTargets.every((value) => typeof value === "string" && value.length <= 4096 && !/[\r\n]/.test(value))) {
        setTargetsText(requestedTargets.join("\n"));
        setMultiple(true);
      }
    } catch { /* An invalid optional URL draft does not change the single-target form. */ }
    setHydrated(true);
  }, []);

  React.useEffect(() => {
    if (hydrated) writeStorage(INSTRUCTION_KEY, instruction);
  }, [instruction, hydrated]);

  const loadProfiles = React.useCallback(async () => {
    setProfilesState("loading");
    try {
      const page = await getSettings();
      setProfiles(page.profiles);
      setActiveProfileId(page.active_profile_id);
      setProfilesState("ready");
    } catch {
      setProfilesState("error");
    }
  }, []);

  const loadProjects = React.useCallback(async () => {
    setProjectsState("loading");
    try {
      const page = await getProjects();
      setProjects(page.projects);
      setProjectsState("ready");
    } catch {
      setProjectsState("error");
    }
  }, []);

  React.useEffect(() => { void loadProfiles(); }, [loadProfiles]);
  React.useEffect(() => { void loadProjects(); }, [loadProjects]);

  const effectiveProfile =
    profiles.find((profile) => profile.id === profileId) ??
    profiles.find((profile) => profile.id === activeProfileId) ??
    null;
  const effectiveProfileModel = effectiveProfile
    ? internal
      ? effectiveProfile.model_internal || effectiveProfile.model_web
      : effectiveProfile.model_web || effectiveProfile.model_internal
    : "";
  const selectedProject = projects.find((project) => project.id === projectId);

  React.useEffect(() => {
    let alive = true;
    const check = async () => {
      try {
        const health = await getJSON<Health>("/api/health");
        if (alive) setEngineOnline(Boolean(health.ok));
      } catch {
        if (alive) setEngineOnline(false);
      }
    };
    void check();
    const id = window.setInterval(() => void check(), 10000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);

  const loadRecent = React.useCallback(async () => {
    setRecentState("loading");
    try {
      const data = await getJSON<{ runs: RunSummary[] }>("/api/runs");
      const runs = [...data.runs].sort((a, b) =>
        (b.start_time || "").localeCompare(a.start_time || "")
      );
      const seen = new Set<string>();
      const values: RecentTarget[] = [];
      for (const run of runs) {
        const value = (run.target || "").trim();
        if (!value || seen.has(value)) continue;
        seen.add(value);
        values.push({ target: value, scan_type: run.scan_type === "internal" ? "internal" : "web" });
        if (values.length >= 5) break;
      }
      setRecent(values);
      setRecentState("ready");
    } catch {
      setRecentState("error");
    }
  }, []);

  React.useEffect(() => { void loadRecent(); }, [loadRecent]);

  const targetError = trimmedTarget
    ? internal ? validateInternalTarget(target, t) : validateWebTarget(target, t)
    : null;
  const targetValid = Boolean(trimmedTarget) && targetError === null;
  // Including every input and the retry token makes an old successful result
  // ineligible immediately, before the effect for the next input runs.
  const scopeKey = JSON.stringify([projectId, mode, trimmedTarget, scopeRetry]);
  const currentScope = scopeCheck?.key === scopeKey ? scopeCheck : null;
  const scopeState = projectId && targetValid
    ? currentScope?.state ?? "checking"
    : null;

  React.useEffect(() => {
    if (multiple || !projectId || !targetValid) {
      setScopeCheck(null);
      return;
    }
    let alive = true;
    setScopeCheck({ key: scopeKey, state: "checking" });
    const timer = window.setTimeout(async () => {
      try {
        const result = await postJSON<ScopeResult>(
          `/api/projects/${encodeURIComponent(projectId)}/validate-target`,
          { target: trimmedTarget, scan_type: mode }
        );
        if (!alive) return;
        // Unexpected payloads must not turn an unverified target green.
        if (typeof result.allowed !== "boolean" || typeof result.scope_revision !== "number") {
          setScopeCheck({ key: scopeKey, state: "error" });
          return;
        }
        setScopeCheck({ key: scopeKey, state: result.allowed ? "allowed" : "blocked", result });
      } catch (error) {
        const message = error instanceof Error ? error.message : "";
        const failure = message.startsWith("404:") ? "missing"
          : message.startsWith("422:") ? "invalid" : "network";
        if (alive) setScopeCheck({ key: scopeKey, state: "error", failure });
      }
    }, 350);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [multiple, projectId, mode, trimmedTarget, targetValid, scopeKey]);

  const socksError = validateSocks5(socks5, t);
  const gsocketError = validateGsocket(gsocket, t);
  const bothTransports = Boolean(socks5.trim()) && Boolean(gsocket.trim());
  const scopeMessage = scopeState === "checking" ? copy.scopeChecking
    : scopeState === "allowed" ? copy.scopeAllowed
      : scopeState === "blocked" ? copy.scopeBlocked
        : scopeState === "error" ? currentScope?.failure === "missing" ? copy.scopeMissing
          : currentScope?.failure === "invalid" ? copy.scopeInvalid : copy.scopeError : "";
  const blockers = [
    importingTargets ? copy.importingTargets : "",
    multiple ? !multiValidation.accepted ? multiValidation.message : ""
      : !targetValid ? targetError || copy.inputRequired : "",
    !multiple && projectId && targetValid && scopeState !== "allowed" ? scopeMessage : "",
    internal ? bothTransports ? t("scan.route.exclusive") : socksError || gsocketError || "" : "",
    profilesState === "loading" ? copy.modelLoading
      : !effectiveProfile ? copy.profileRequired : "",
    engineOnline === null ? copy.checkingEngine
      : engineOnline === false ? t("scan.engine.unreachable") : "",
  ].filter(Boolean);
  const canLaunch = hydrated && blockers.length === 0;
  const targetAccepted = targetValid && (!projectId || scopeState === "allowed");

  const launch = async () => {
    if (!canLaunch || busy) return;
    setBusy(true);
    setLaunchError("");
    const socks = socks5.trim();
    try {
      const response = await postJSON<ScanLaunched>("/api/scans", {
        ...(multiple ? { targets: multiValidation.targets } : { target: trimmedTarget }),
        scan_type: mode,
        crypto: internal && crypto,
        socks5: internal && socks
          ? /^socks5h?:\/\//i.test(socks) ? socks : `socks5://${socks}`
          : "",
        gsocket: internal ? gsocket.trim() : "",
        instruction,
        dry_run: false,
        language,
        profile_id: effectiveProfile?.id ?? "",
        project_id: projectId,
      });
      if (!response.run_name) throw new Error(t("scan.launch.missingRunName"));
      router.push(`/run?name=${encodeURIComponent(response.run_name)}`);
    } catch (error) {
      setLaunchError(parseLaunchError(error));
      setBusy(false);
      // A project may have changed while the form was open. Refresh the
      // displayed verdict after rejection; launch remains server-validated.
      if (projectId) setScopeRetry((value) => value + 1);
      if (multiple) setMultiRetry((value) => value + 1);
    }
  };

  const transportLabel = socks5.trim()
    ? `SOCKS5 · ${socks5.trim().replace(/^socks5h?:\/\//i, "")}`
    : gsocket.trim() ? t("scan.transport.gsocket") : copy.direct;
  const instructionCount = instruction.length;
  const instructionPreview = instruction.trim().replace(/\s+/g, " ").slice(0, 90);

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleGroup}>
          <span className={styles.prompt} aria-hidden="true">&gt;_</span>
          <h1>{copy.heading}</h1>
        </div>
        <span className={cn(styles.engine, engineOnline === true && styles.success, engineOnline === false && styles.danger)}>
          {engineOnline === null ? <Spinner /> : <Cpu size={14} aria-hidden="true" />}
          {t(engineOnline === null ? "scan.engine.checking" : engineOnline ? "scan.engine.online" : "scan.engine.offline")}
        </span>
      </header>

      <section className={cn(styles.launchPanel, multiple && styles.multiPanel)} aria-label={copy.setup}>
        <div className={styles.setupRow}>
          <div className={styles.projectField}>
            <label htmlFor="scan-project">{t("scan.project")}</label>
            <Select
              id="scan-project"
              className={`select-shell ${styles.projectSelect}`}
              value={projectId}
              onValueChange={setProjectId}
              disabled={projectsState === "loading" || busy}
              options={[
                { value: "", label: copy.noProject },
                ...(projectId && !selectedProject ? [{ value: projectId, label: copy.selectedProjectMissing }] : []),
                ...projects.map((project) => ({ value: project.id, label: project.name })),
              ]}
            />
            {projectsState === "error" && (
              <div className={styles.inlineError}>
                <span>{t("scan.project.error")}</span>
                <button type="button" onClick={() => void loadProjects()}>{t("common.retry")}</button>
              </div>
            )}
            {projectsState === "ready" && projects.length === 0 && (
              <Link href="/projects" className={styles.textLink}>{t("projects.new")} →</Link>
            )}
          </div>
          <fieldset className={styles.modeField}>
            <legend>{t("scan.mode")}</legend>
            <div className={styles.segments}>
              {(["web", "internal"] as const).map((value) => {
                const Icon = value === "web" ? Globe : Network;
                return (
                  <button key={value} type="button" aria-pressed={mode === value} onClick={() => setMode(value)} disabled={busy}>
                    <Icon size={15} aria-hidden="true" />
                    {t(`scan.mode.${value}`)}
                  </button>
                );
              })}
            </div>
          </fieldset>
        </div>

        <div className={styles.targetSection}>
          <div className={styles.targetModeBar}>
            <span className={cn(styles.targetModeName, multiple && styles.multiModeName)}>
              {multiple ? <ListPlus size={16} aria-hidden="true" /> : <Terminal size={15} aria-hidden="true" />}
              {multiple ? copy.multiTarget : copy.singleMode}
            </span>
            <button
              type="button"
              className={styles.targetModeButton}
              disabled={busy}
              aria-pressed={multiple}
              onClick={() => {
                if (!multiple && !targetsText && trimmedTarget) setTargetsText(trimmedTarget);
                if (!multiple) setMultiRetry((value) => value + 1);
                setMultiple((value) => !value);
                setLaunchError("");
                if (multiple) window.requestAnimationFrame(() => document.getElementById("scan-target")?.focus());
              }}
            >
              {multiple ? <ArrowLeft size={14} aria-hidden="true" /> : <ListPlus size={14} aria-hidden="true" />}
              {multiple ? copy.singleTarget : copy.multiTarget}
            </button>
          </div>
          {multiple ? (
            <>
              {projectId && <div className={styles.multiScopeLink}><Link className={styles.textLink} href={`/projects/detail?id=${encodeURIComponent(projectId)}&tab=scope`}>{copy.scopeSettings} ↗</Link></div>}
              <MultiTargetEditor
                text={targetsText}
                onChange={(value) => { setTargetsText(value); setLaunchError(""); }}
                internal={internal}
                projectId={projectId}
                disabled={busy}
                validation={multiValidation}
                onRetry={() => setMultiRetry((value) => value + 1)}
                onImportingChange={setImportingTargets}
              />
            </>
          ) : <>
          <div className={styles.targetLabel}>
            <label htmlFor="scan-target">{t(internal ? "scan.target.scope" : "scan.target.url")}</label>
            {projectId && (
              <Link className={styles.textLink} href={`/projects/detail?id=${encodeURIComponent(projectId)}&tab=scope`}>
                {copy.scopeSettings} ↗
              </Link>
            )}
          </div>
          <div className={cn(styles.targetInput, (targetError || scopeState === "blocked") && styles.targetInvalid)}>
            <Terminal size={20} className={styles.targetIcon} aria-hidden="true" />
            <input
              id="scan-target"
              placeholder={internal ? "10.0.0.0/24" : "https://target.example.com"}
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              spellCheck={false}
              autoComplete="off"
              disabled={busy}
              aria-invalid={Boolean(targetError || scopeState === "blocked")}
              aria-describedby="scan-target-status"
            />
            {targetAccepted && <CircleCheck size={18} className={styles.success} aria-hidden="true" />}
            {scopeState === "checking" && <Spinner />}
          </div>
          <div id="scan-target-status" className={styles.targetStatus} role="status" aria-live="polite">
            {targetError ? <FieldError>{targetError}</FieldError>
              : scopeState === "blocked" ? <FieldError>{copy.scopeBlocked}</FieldError>
                : scopeState === "error" ? (
                  <div className={styles.inlineError}>
                    <span>{scopeMessage}</span>
                    <button type="button" onClick={() => setScopeRetry((value) => value + 1)}>{copy.scopeRetry}</button>
                  </div>
                ) : (
                  <span className={cn(targetAccepted && styles.success)}>
                    {scopeMessage || (targetValid ? copy.inputReady : copy.targetHint)}
                  </span>
                )}
          </div>
          <details className={styles.recent}>
            <summary><History size={13} aria-hidden="true" />{copy.moreTargets}<ChevronDown size={13} aria-hidden="true" /></summary>
            <div className={styles.recentTargets}>
              {recentState === "loading" && <Spinner />}
              {recentState === "error" && (
                <div className={styles.inlineError}>
                  <span>{t("scan.recent.error")}</span>
                  <button type="button" onClick={() => void loadRecent()}>{t("common.retry")}</button>
                </div>
              )}
              {recentState === "ready" && recent.length === 0 && <span>{t("scan.recent.empty")}</span>}
              {recentState === "ready" && recent.map((value) => (
                <button
                  key={value.target}
                  type="button"
                  title={t("scan.recent.fill", { mode: t(`scan.mode.${value.scan_type}`) })}
                  onClick={() => { setTarget(value.target); setMode(value.scan_type); }}
                  disabled={busy}
                >
                  {value.scan_type === "internal" ? <Network size={13} /> : <Globe size={13} />}
                  <span>{value.target}</span>
                </button>
              ))}
            </div>
          </details>
          </>}
        </div>

        <div className={styles.launchFooter}>
          <fieldset className={styles.languageField}>
            <legend>{t("scan.language")}</legend>
            <div className={styles.languageOptions}>
              {(["zh-CN", "en"] as const).map((value) => (
                <button key={value} type="button" aria-pressed={language === value} onClick={() => setLanguage(value)} disabled={busy}>
                  {t(value === "zh-CN" ? "scan.language.zh" : "scan.language.en")}
                </button>
              ))}
            </div>
          </fieldset>
          <div className={styles.launchAction}>
            <span className={styles.launchCaption}>{effectiveProfile?.name || copy.modelUnavailable}</span>
            <button
              type="button"
              className={cn("button-primary", styles.launchButton)}
              disabled={!canLaunch || busy}
              aria-busy={busy}
              title={canLaunch ? copy.launchHint : blockers.join(" · ")}
              onClick={() => void launch()}
            >
              {busy ? <Spinner /> : <Rocket size={16} aria-hidden="true" />}
              {busy ? t("scan.launching") : multiple ? copy.multiLaunch : t("scan.launch")}
            </button>
          </div>
        </div>
        <div className={styles.preflight} role="status" aria-label={copy.status}>
          {canLaunch
            ? <><CircleCheck size={14} className={styles.success} aria-hidden="true" /><span>{multiple ? `${multiValidation.targets.length} ${copy.multiReady}` : t("scan.launch.ready")}</span></>
            : <><span className={styles.statusDot} aria-hidden="true" /><span>{blockers.slice(0, 2).join(" · ")}</span></>}
        </div>
        {launchError && (
          <div className={styles.launchError} role="alert">
            <strong>{t("scan.launch.failed")}</strong>
            <p>{launchError}</p>
          </div>
        )}
      </section>

      <div className={styles.options}>
        <details className={styles.optionPanel} open={!effectiveProfile && profilesState === "ready" ? true : undefined}>
          <summary>
            <Cpu size={17} aria-hidden="true" />
            <span className={styles.optionTitle}>{t("scan.profile")}</span>
            <span className={cn(styles.optionValue, !effectiveProfile && styles.danger)}>
              {profilesState === "loading" ? copy.modelLoading : effectiveProfileModel || effectiveProfile?.name || copy.modelUnavailable}
            </span>
            <ChevronDown size={15} className={styles.chevron} aria-hidden="true" />
          </summary>
          <div className={styles.optionBody}>
            <div className={styles.optionToolbar}>
              <span>{profileId === "" ? copy.followActive : effectiveProfile?.name}</span>
              <Link href="/settings" className={styles.textLink}><Settings2 size={13} />{copy.manageProfiles}</Link>
            </div>
            {profilesState === "loading" ? <Spinner />
              : profilesState === "error" ? (
                <div className={styles.inlineError}>
                  <span>{t("scan.profile.error")}</span>
                  <button type="button" onClick={() => void loadProfiles()}>{t("common.retry")}</button>
                </div>
              ) : profiles.length === 0 ? (
                <EmptyState title={t("scan.profile.empty")} hint={t("scan.profile.emptyHint")} action={
                  <Link href="/settings" className="button-primary"><Plus size={14} />{t("scan.profile.openSettings")}</Link>
                } />
              ) : (
                <div className={styles.profileList}>
                  {profiles.map((profile) => {
                    const model = internal ? profile.model_internal || profile.model_web : profile.model_web || profile.model_internal;
                    return (
                      <button
                        key={profile.id}
                        type="button"
                        aria-pressed={effectiveProfile?.id === profile.id}
                        onClick={() => setProfileId(profile.id === activeProfileId ? "" : profile.id)}
                        disabled={busy}
                      >
                        <span className={styles.profileMark}>{effectiveProfile?.id === profile.id && <Check size={13} />}</span>
                        <span className={styles.profileMain}>
                          <strong>{profile.name}{profile.id === activeProfileId && <small>{t("scan.profile.active")}</small>}</strong>
                          <span>{model || t("scan.profile.modelMissing")}</span>
                          <span className={styles.profileEndpoint}>{profile.llm_api_base} · {profile.llm_api_key || t("scan.profile.keyMissing")}</span>
                        </span>
                        <span className={styles.profileRoute} title={profile.llm_api_base}>{profile.route_type === "openrouter" ? "OpenRouter" : t("scan.profile.custom")}</span>
                      </button>
                    );
                  })}
                </div>
              )}
          </div>
        </details>

        {internal && (
          <details className={styles.optionPanel}>
            <summary>
              <Network size={17} aria-hidden="true" />
              <span className={styles.optionTitle}>{copy.network}</span>
              <span className={cn(styles.optionValue, (bothTransports || socksError || gsocketError) && styles.danger)}>
                {transportLabel}{crypto ? ` · ${t("scan.crypto")}` : ""}
              </span>
              <ChevronDown size={15} className={styles.chevron} aria-hidden="true" />
            </summary>
            <div className={styles.optionBody}>
              <div className={styles.routeGrid}>
                <div>
                  <label htmlFor="scan-socks5">{t("scan.route.socks5")}</label>
                  <input id="scan-socks5" className="input-shell" placeholder="socks5://10.0.0.1:1080" value={socks5} onChange={(event) => setSocks5(event.target.value)} spellCheck={false} autoComplete="off" disabled={busy} />
                  {socksError ? <FieldError>{socksError}</FieldError> : <p className={styles.help}>{t("scan.route.socks5.hint")}</p>}
                </div>
                <div>
                  <label htmlFor="scan-gsocket">{t("scan.route.gsocket")}</label>
                  <input id="scan-gsocket" type="password" className="input-shell" placeholder={t("scan.route.gsocket.placeholder")} value={gsocket} onChange={(event) => setGsocket(event.target.value)} autoComplete="off" disabled={busy} />
                  {gsocketError ? <FieldError>{gsocketError}</FieldError> : <p className={styles.help}>{t("scan.route.gsocket.hint")}</p>}
                </div>
              </div>
              {bothTransports && <FieldError>{t("scan.route.exclusive")}</FieldError>}
              <div className={styles.cryptoRow}>
                <div><strong>{t("scan.crypto")}</strong><p className={styles.help}>{t("scan.crypto.hint")}</p></div>
                <button type="button" role="switch" aria-checked={crypto} aria-label={t("scan.crypto")} className={styles.toggle} onClick={() => setCrypto(!crypto)} disabled={busy}><span /></button>
              </div>
            </div>
          </details>
        )}

        <details className={styles.optionPanel}>
          <summary>
            <Terminal size={17} aria-hidden="true" />
            <span className={styles.optionTitle}>{t("scan.instruction")}</span>
            <span className={styles.optionValue}>{instructionCount > 0 ? `${copy.draft} · ${t("scan.instruction.chars", { n: instructionCount.toLocaleString() })}` : copy.noInstruction}</span>
            <ChevronDown size={15} className={styles.chevron} aria-hidden="true" />
          </summary>
          {instructionPreview && <p className={styles.draftPreview} title={copy.savedInstruction}>{instructionPreview}{instruction.trim().length > 90 ? "…" : ""}</p>}
          <div className={styles.optionBody}>
            <label htmlFor="scan-instruction">{t("scan.instruction")}</label>
            <textarea id="scan-instruction" className="textarea-shell" placeholder={t("scan.instruction.placeholder")} value={instruction} onChange={(event) => setInstruction(event.target.value)} spellCheck={false} disabled={busy} />
            <div className={styles.instructionMeta}>
              <span>{t("scan.instruction.remembered")}</span>
              <span className={cn(instructionCount > INSTRUCTION_SOFT_LIMIT && styles.warning)}>{t("scan.instruction.chars", { n: instructionCount.toLocaleString() })}</span>
            </div>
          </div>
        </details>
      </div>
    </div>
  );
}
