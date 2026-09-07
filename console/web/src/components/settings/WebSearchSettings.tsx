"use client";

import * as React from "react";
import { Check, Eye, EyeOff, Search } from "lucide-react";
import { toast } from "sonner";
import { Panel, Spinner } from "@/components/ui";
import { getJSON, putJSON } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./WebSearchSettings.module.css";

interface IntegrationSettings {
  perplexity_api_key_set: boolean;
  perplexity_api_key_masked: string;
}

const ENDPOINT = "/api/settings/integrations";

export function WebSearchSettings() {
  const { t } = useI18n();
  const fieldId = React.useId();
  const [settings, setSettings] = React.useState<IntegrationSettings | null>(null);
  const [loadState, setLoadState] = React.useState<"loading" | "ready" | "error">("loading");
  const [reloadVersion, setReloadVersion] = React.useState(0);
  const [input, setInput] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState(false);
  const [visible, setVisible] = React.useState(false);

  React.useEffect(() => {
    let active = true;
    getJSON<IntegrationSettings>(ENDPOINT).then(
      (res) => {
        if (!active) return;
        setSettings(res);
        setLoadState("ready");
      },
      () => {
        if (active) setLoadState("error");
      },
    );
    return () => { active = false; };
  }, [reloadVersion]);

  const ready = loadState === "ready";
  const configured = ready && Boolean(settings?.perplexity_api_key_set);
  const canSave = ready && !saving && Boolean(input.trim());
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setSaveError(false);
    try {
      const res = await putJSON<IntegrationSettings>(ENDPOINT, { perplexity_api_key: input.trim() });
      setSettings(res);
      setInput("");
      setVisible(false);
      toast.success(t("common.saved"));
    } catch {
      // Keep the draft for retry; never echo a response that could contain a key.
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  };

  const statusText = loadState === "loading"
    ? t("common.loading")
    : loadState === "error"
      ? t("settings.integrations.unavailable")
      : configured ? t("settings.integrations.configured") : t("settings.notSet");

  return (
    <Panel
      code="SEARCH"
      title={t("settings.integrations.searchTitle")}
      className={styles.panel}
      actions={
        <span className={styles.status} data-state={configured ? "configured" : loadState} role="status">
          {loadState === "loading" ? <Spinner /> : <span className={styles.statusDot} aria-hidden="true" />}
          {statusText}
        </span>
      }
    >
      <div className={styles.body}>
        <div className={styles.provider}>
          <span className={styles.providerIcon} aria-hidden="true"><Search size={18} strokeWidth={1.7} /></span>
          <div>
            <h3 className={styles.providerName}>Perplexity</h3>
            <p className={styles.providerHint}>{t("settings.integrations.hint")}</p>
          </div>
        </div>

        <form className={styles.form} onSubmit={save} aria-busy={saving}>
          <label className="field-label" htmlFor={fieldId}>{t("settings.integrations.keyLabel")}</label>
          <div className={styles.controls}>
            <div className={styles.inputWrap}>
              <input
                id={fieldId}
                className={`input-shell ${styles.keyInput}`}
                type={visible ? "text" : "password"}
                placeholder={ready ? configured ? settings?.perplexity_api_key_masked || "••••••••" : "pplx-…" : "—"}
                value={input}
                onChange={(event) => { setInput(event.target.value); setSaveError(false); }}
                disabled={!ready || saving}
                spellCheck={false}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                aria-describedby={`${fieldId}-hint${saveError ? ` ${fieldId}-error` : ""}`}
              />
              <button
                type="button"
                className={styles.visibility}
                disabled={!ready || saving || !input}
                onClick={() => setVisible((value) => !value)}
                aria-label={t(visible ? "settings.integrations.hideKey" : "settings.integrations.showKey")}
                aria-controls={fieldId}
              >
                {visible ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <button className={`button-primary ${styles.save}`} type="submit" disabled={!canSave}>
              {saving ? <Spinner /> : <Check size={16} aria-hidden="true" />}
              {t(saving ? "settings.integrations.saving" : "common.save")}
            </button>
          </div>
          <p className={styles.hint} id={`${fieldId}-hint`}>
            {t(configured ? "settings.integrations.keepKey" : "settings.integrations.storageHint")}
          </p>
          {loadState === "error" && (
            <div className={styles.errorRow}>
              <p role="alert">{t("settings.integrations.loadError")}</p>
              <button type="button" className={styles.retry} onClick={() => {
                setLoadState("loading");
                setReloadVersion((value) => value + 1);
              }}>{t("common.retry")}</button>
            </div>
          )}
          {saveError && <p className={styles.error} id={`${fieldId}-error`} role="alert">{t("settings.integrations.saveError")}</p>}
        </form>
      </div>
    </Panel>
  );
}
