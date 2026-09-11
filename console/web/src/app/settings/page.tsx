"use client";

import * as React from "react";
import { useTheme } from "next-themes";
import { Check, Copy, KeyRound, Languages, Moon, Pencil, Plus, Sun, Zap } from "lucide-react";
import {
  activateProfile,
  createProfile,
  deleteProfile,
  getJSON,
  getSettings,
  updateProfile,
  type Health,
  type ModelProfile,
  type ProfileWrite,
  type ModelApiMode,
  type ModelReasoningEffort,
} from "@/lib/api";
import { readStorage, removeStorage, LLM_CFG_KEY } from "@/lib/storage";
import { Chip, ConfirmButton, EmptyState, MicroLabel, Panel, Spinner } from "@/components/ui";
import { ModelRouteDialog, OPENROUTER_BASE, type ModelRouteDraft as Draft } from "@/components/settings/ModelRouteDialog";
import { WebSearchSettings } from "@/components/settings/WebSearchSettings";
import { FofaSettings } from "@/components/settings/FofaSettings";
import { QueueSettings } from "@/components/settings/QueueSettings";
import { relTime } from "@/lib/format";
import { useI18n, type Locale } from "@/lib/i18n";
import { allowedModelEfforts, modelOptionError, normalizeModelEndpoint, resolveModelApiMode } from "@/lib/model-options";

/* ============================================================================
   Appearance — theme (dark default) + interface language (zh-CN default).
   These are console-chrome settings; the scan REPORT language is a separate
   per-launch choice on the scan page.
   ========================================================================= */

function AppearancePanel() {
  const { t, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  const isDark = mounted ? theme !== "light" : true;

  const themeCard = (value: "dark" | "light", label: string, Icon: typeof Moon) => (
    <button
      key={value}
      type="button"
      onClick={() => setTheme(value)}
      className={`flex items-center gap-3 rounded-xl border p-3.5 text-left transition-all ${
        isDark === (value === "dark")
          ? "border-accent/30 bg-accent/10"
          : "border-line/8 hover:border-line/14"
      }`}
    >
      <Icon className={`h-4 w-4 ${isDark === (value === "dark") ? "text-accent" : "text-fg-muted"}`} />
      <span className="text-sm font-medium text-fg">{label}</span>
      {isDark === (value === "dark") && <Check className="ml-auto h-3.5 w-3.5 text-accent" />}
    </button>
  );

  const localeCard = (value: Locale, label: string) => (
    <button
      key={value}
      type="button"
      onClick={() => setLocale(value)}
      className={`flex items-center gap-3 rounded-xl border p-3.5 text-left transition-all ${
        locale === value
          ? "border-accent/30 bg-accent/10"
          : "border-line/8 hover:border-line/14"
      }`}
    >
      <Languages className={`h-4 w-4 ${locale === value ? "text-accent" : "text-fg-muted"}`} />
      <span className="text-sm font-medium text-fg">{label}</span>
      {locale === value && <Check className="ml-auto h-3.5 w-3.5 text-accent" />}
    </button>
  );

  return (
    <Panel title={t("settings.appearance")}>
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="micro-label mb-2">{t("settings.theme")}</div>
          <div className="grid grid-cols-2 gap-2.5">
            {themeCard("dark", t("settings.theme.dark"), Moon)}
            {themeCard("light", t("settings.theme.light"), Sun)}
          </div>
        </div>
        <div>
          <div className="micro-label mb-2">{t("settings.language")}</div>
          <div className="grid grid-cols-2 gap-2.5">
            {localeCard("zh-CN", "简体中文")}
            {localeCard("en", "English")}
          </div>
        </div>
      </div>
    </Panel>
  );
}

const EMPTY_DRAFT: Draft = {
  id: null,
  name: "",
  route_type: "openrouter",
  llm_api_base: OPENROUTER_BASE,
  llm_api_key: "",
  keyVisible: false,
  saved_key_available: false,
  model_web: "",
  model_internal: "",
  api_mode_web: "auto",
  api_mode_internal: "auto",
  reasoning_effort_web: "default",
  reasoning_effort_internal: "default",
};

function profileDraft(profile: ModelProfile): Partial<Draft> {
  return {
    id: profile.id, name: profile.name, route_type: profile.route_type,
    llm_api_base: profile.llm_api_base, llm_api_key: profile.llm_api_key,
    keyVisible: false, saved_key_available: profile.llm_api_key_set,
    model_web: profile.model_web, model_internal: profile.model_internal,
    api_mode_web: profile.api_mode_web ?? "chat_completions",
    api_mode_internal: profile.api_mode_internal ?? "chat_completions",
    reasoning_effort_web: profile.reasoning_effort_web ?? "default",
    reasoning_effort_internal: profile.reasoning_effort_internal ?? "default",
  };
}

export default function SettingsPage() {
  const { t, locale } = useI18n();
  const [profiles, setProfiles] = React.useState<ModelProfile[]>([]);
  const [activeId, setActiveId] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [offline, setOffline] = React.useState("");
  const [draft, setDraft] = React.useState<Draft | null>(null);
  const [draftErrors, setDraftErrors] = React.useState<string[]>([]);
  const [saving, setSaving] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const [engine, setEngine] = React.useState<Health | null>(null);
  const [importable, setImportable] = React.useState(false);
  const [importingLegacy, setImportingLegacy] = React.useState(false);

  const reload = React.useCallback(async () => {
    try {
      const [page, health] = await Promise.all([
        getSettings(),
        getJSON<Health>("/api/health").catch(() => null),
      ]);
      setProfiles(page.profiles);
      setActiveId(page.active_profile_id);
      setEngine(health);
      setOffline("");
    } catch (e) {
      setOffline(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void reload();
    try {
      setImportable(Boolean(readStorage(LLM_CFG_KEY)));
    } catch {
      setImportable(false);
  }
  }, [reload]);

  const flash = (text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice(""), 4000);
  };

  const openCreate = (prefill?: Partial<Draft>) => {
    setDraftErrors([]);
    setImportingLegacy(false);
    setDraft({ ...EMPTY_DRAFT, ...prefill });
  };

  const duplicateProfile = (profile: ModelProfile) => {
    const baseName = t("settings.duplicateName", { name: profile.name });
    let name = baseName;
    let suffix = 2;
    while (profiles.some((existing) => existing.name === name)) name = `${baseName} ${suffix++}`;
    openCreate({ ...profileDraft(profile), id: null, copy_from_profile_id: profile.id, name });
  };

  /* Import the launcher's persisted LLM config as a first profile. */
  const importLauncherConfig = () => {
    try {
      const raw = readStorage(LLM_CFG_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) as Partial<{ base: string; key: string; model: string }>;
      const base = typeof v.base === "string" ? v.base : "";
      openCreate({
        name: "Default",
        route_type: base.includes("openrouter") ? "openrouter" : "custom",
        llm_api_base: base || OPENROUTER_BASE,
        llm_api_key: typeof v.key === "string" ? v.key : "",
        model_web: typeof v.model === "string" ? v.model : "",
        model_internal: typeof v.model === "string" ? v.model : "",
      });
      setImportingLegacy(true);
    } catch {
      openCreate();
    }
  };

  const validateDraft = (d: Draft): string[] => {
    const errors: string[] = [];
    if (!d.name.trim()) errors.push(t("settings.validation.name"));
    const source = profiles.find((profile) => profile.id === (d.id || d.copy_from_profile_id));
    const usingSavedKey = !d.llm_api_key.trim() || d.llm_api_key.trim().startsWith("•••");
    if (usingSavedKey && !source?.llm_api_key_set) errors.push(t("settings.validation.key"));
    if (usingSavedKey && source && (source.route_type !== d.route_type
      || normalizeModelEndpoint(source.llm_api_base) !== normalizeModelEndpoint(d.llm_api_base))) {
      errors.push(t("settings.validation.endpointChanged"));
    }
    if (d.route_type === "custom") {
      if (!d.llm_api_base.trim()) errors.push(t("settings.validation.base"));
      else if (!/^https?:\/\//.test(d.llm_api_base.trim())) errors.push(t("settings.validation.scheme"));
    }
    if (!d.model_web.trim() && !d.model_internal.trim()) errors.push(t("settings.validation.model"));
    for (const slot of ["web", "internal"] as const) {
      const error = modelOptionError(d[`model_${slot}`], d[`api_mode_${slot}`], d[`reasoning_effort_${slot}`]);
      if (error) errors.push(`${t(slot === "web" ? "settings.webModel" : "settings.internalModel")}: ${t(`settings.validation.${error}`, { efforts: allowedModelEfforts(d[`model_${slot}`])?.join(" / ") || "" })}`);
    }
    return errors;
  };

  const saveDraft = async () => {
    if (!draft) return;
    const errors = validateDraft(draft);
    setDraftErrors(errors);
    if (errors.length) return;
    setSaving(true);
    const body: ProfileWrite = {
      name: draft.name.trim(),
      route_type: draft.route_type,
      llm_api_base: draft.route_type === "openrouter" ? OPENROUTER_BASE : draft.llm_api_base.trim(),
      llm_api_key: draft.llm_api_key.trim(),
      model_web: draft.model_web.trim().replace(/^openrouter\//, ""),
      model_internal: draft.model_internal.trim().replace(/^openrouter\//, ""),
      api_mode_web: draft.api_mode_web,
      api_mode_internal: draft.api_mode_internal,
      reasoning_effort_web: draft.reasoning_effort_web,
      reasoning_effort_internal: draft.reasoning_effort_internal,
    };
    try {
      if (draft.id) {
        await updateProfile(draft.id, body);
        flash(t("settings.notice.updated", { name: body.name }));
      } else {
        const created = await createProfile({ ...body, copy_from_profile_id: draft.copy_from_profile_id });
        flash(t("settings.notice.created", { name: created.name }));
      }
      if (importingLegacy) {
        removeStorage(LLM_CFG_KEY);
        setImportable(false);
        setImportingLegacy(false);
      }
      setDraft(null);
      await reload();
    } catch (e) {
      setDraftErrors([e instanceof Error ? e.message : String(e)]);
    } finally {
      setSaving(false);
    }
  };

  const activate = async (id: string) => {
    try {
      await activateProfile(id);
      await reload();
      flash(t("settings.notice.activated"));
    } catch (e) {
      flash(e instanceof Error ? e.message : String(e));
    }
  };

  const remove = async (id: string) => {
    try {
      await deleteProfile(id);
      await reload();
      flash(t("settings.notice.deleted"));
    } catch (e) {
      flash(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="flex min-w-0 flex-col gap-3.5">
      <header className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">&gt; {t("settings.path")}</div>
          <h1 className="page-title">{t("settings.title")}</h1>
          <p className="page-copy">{t("settings.copy")}</p>
        </div>
        <div className="hero-actions">
          <button className="button-primary" onClick={() => openCreate()}>
            <Plus size={15} /> {t("settings.newProfile")}
          </button>
        </div>
      </header>

      {notice && (
        <div className="alert-info flex items-center gap-2" role="status">
          <Zap size={14} /> {notice}
        </div>
      )}
      {offline && (
        <div className="alert-error">
          {t("settings.offline", { error: offline })}
        </div>
      )}

      {/* ---- appearance ----------------------------------------------------- */}
      <AppearancePanel />

      {/* ---- integrations (web_search API key) ------------------------------ */}
      <WebSearchSettings />
      <FofaSettings />
      <QueueSettings />

      {/* ---- engine card -------------------------------------------------- */}
      <Panel code="ENGINE" title={t("settings.engine")} tone="cyan">
        <div className="grid gap-4 md:grid-cols-3">
          <InfoTile label={t("settings.console")} value={engine?.ok ? t("settings.connected") : offline ? t("shell.offline") : "…"} />
          <InfoTile label={t("settings.runsRoot")} value={engine?.runs_root ?? "—"} mono />
          <InfoTile label={t("settings.liveRuns")} value={String(engine?.live_runs ?? 0)} />
        </div>
      </Panel>

      {/* ---- profiles ------------------------------------------------------ */}
      <Panel
        code="ROUTES"
        title={t("settings.profiles")}
        actions={<MicroLabel>{t("settings.configured", { n: profiles.length })}</MicroLabel>}
      >
        {loading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-fg-muted">
            <Spinner /> {t("settings.loadingProfiles")}
          </div>
        ) : profiles.length === 0 ? (
          <EmptyState
            title={t("settings.noProfiles")}
            hint={t("settings.noProfiles.hint")}
            action={
              <div className="flex gap-2">
                <button className="button-primary" onClick={() => openCreate()}>
                  <Plus size={15} /> {t("settings.createProfile")}
                </button>
                {importable && (
                  <button className="button-secondary" onClick={importLauncherConfig}>
                    {t("settings.importLauncher")}
                  </button>
                )}
              </div>
            }
          />
        ) : (
          <div className="space-y-3">
            {profiles.map((p) => {
              const active = p.id === activeId;
              return (
                <div
                  key={p.id}
                  className={`panel-inner relative rounded-xl border p-4 transition-colors ${
                    active ? "border-[rgba(0,229,255,0.34)] bg-[rgba(0,229,255,0.05)]" : "border-line/6 hover:border-line/12"
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2.5">
                    <span className="text-[15px] font-semibold text-fg">{p.name}</span>
                    <Chip tone={p.route_type === "openrouter" ? "accent" : "violet"}>
                      {p.route_type === "openrouter" ? "OpenRouter" : t("settings.custom")}
                    </Chip>
                    {active && <Chip tone="success">● {t("settings.active")}</Chip>}
                    <span className="mono-chip ml-auto text-fg-muted" title={p.llm_api_key}>
                      <KeyRound size={11} className="mr-1 inline" />
                      {p.llm_api_key || t("settings.notSet")}
                    </span>
                  </div>
                  <div className="mt-2.5 grid gap-2 text-xs md:grid-cols-3">
                    <ModelCell label={t("settings.webModel")} value={p.model_web}
                      apiMode={p.api_mode_web ?? "chat_completions"} effort={p.reasoning_effort_web ?? "default"} />
                    <ModelCell label={t("settings.internalModel")} value={p.model_internal}
                      apiMode={p.api_mode_internal ?? "chat_completions"} effort={p.reasoning_effort_internal ?? "default"} />
                    <ModelCell label={t("settings.apiBaseShort")} value={p.llm_api_base} />
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <span className="micro-label">{t("settings.updated", { time: relTime(p.updated_at, locale) })}</span>
                    <div className="ml-auto flex flex-wrap gap-2">
                      {!active && (
                        <button className="button-compact button-cyan" onClick={() => activate(p.id)}>
                          <Check size={13} /> {t("settings.activate")}
                        </button>
                      )}
                      <button
                        className="button-compact button-secondary"
                        onClick={() => openCreate(profileDraft(p))}
                      >
                        <Pencil size={13} /> {t("common.edit")}
                      </button>
                      <button className="button-compact button-secondary" onClick={() => duplicateProfile(p)}
                        aria-label={t("settings.duplicateProfileLabel", { name: p.name })}>
                        <Copy size={13} /> {t("settings.duplicateProfile")}
                      </button>
                      <ConfirmButton
                        label={t("common.delete")}
                        confirmLabel={t("settings.deleteProfile")}
                        danger
                        onConfirm={() => remove(p.id)}
                      />
                    </div>
                  </div>
                </div>
              );
            })}
            {importable && (
              <button
                className="text-xs text-fg-muted underline decoration-dotted hover:text-fg-2"
                onClick={importLauncherConfig}
              >
                {t("settings.importLauncher.inline")}
              </button>
            )}
          </div>
        )}
      </Panel>

      {draft && (
        <ModelRouteDialog
          draft={draft}
          onChange={setDraft}
          onClose={() => { setDraft(null); setImportingLegacy(false); }}
          onSave={() => void saveDraft()}
          errors={draftErrors}
          saving={saving}
        />
      )}
    </div>
  );
}

function InfoTile({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="info-tile">
      <div className="micro-label">{label}</div>
      <div className={`mt-1 truncate text-sm text-fg ${mono ? "mono" : ""}`} title={value}>
        {value}
      </div>
    </div>
  );
}

function ModelCell({ label, value, apiMode, effort }: {
  label: string; value: string; apiMode?: ModelApiMode; effort?: ModelReasoningEffort;
}) {
  const { t } = useI18n();
  const api = apiMode ? resolveModelApiMode(value, apiMode) === "responses" ? "Responses" : "Chat Completions" : "";
  return (
    <div className="min-w-0">
      <div className="micro-label">{label}</div>
      <div className="mono mt-0.5 truncate text-fg" title={value || "—"}>
        {value || "—"}
      </div>
      {value && apiMode && <div className="mt-1 text-[11px] leading-relaxed text-fg-muted">
        {apiMode === "auto" ? t("settings.autoApi", { api }) : api}
        {" · "}{effort === "default" || !effort ? t("settings.providerDefault") : effort}
      </div>}
    </div>
  );
}
