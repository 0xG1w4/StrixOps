"use client";

import * as React from "react";
import { Check, Eye, EyeOff, Search, Radio } from "lucide-react";
import { toast } from "sonner";
import { Panel, Spinner } from "@/components/ui";
import { apiURL, getJSON, putJSON } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import {
  searchCost,
  searchDuration,
  webSearchStatus,
  type PerplexityModel,
  type WebSearchConnectionTest,
  type WebSearchIntegrationSettings,
} from "@/lib/web-search";
import styles from "./WebSearchSettings.module.css";

const ENDPOINT = "/api/settings/integrations";
const defaultTimeout = (model: PerplexityModel) => model === "sonar-reasoning-pro" ? 300 : 30;

export function WebSearchSettings() {
  const { t, locale } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => en ? english : zh;
  const fieldId = React.useId();
  const [settings, setSettings] = React.useState<WebSearchIntegrationSettings | null>(null);
  const [loadState, setLoadState] = React.useState<"loading" | "ready" | "error">("loading");
  const [reloadVersion, setReloadVersion] = React.useState(0);
  const [input, setInput] = React.useState("");
  const [enabled, setEnabled] = React.useState(false);
  const [model, setModel] = React.useState<PerplexityModel>("sonar");
  const [timeout, setTimeoutValue] = React.useState("30");
  const [timeoutTouched, setTimeoutTouched] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [saveError, setSaveError] = React.useState(false);
  const [visible, setVisible] = React.useState(false);
  const [testing, setTesting] = React.useState(false);
  const [testResult, setTestResult] = React.useState<WebSearchConnectionTest | null>(null);
  const [testError, setTestError] = React.useState<string | null>(null);
  const testRequest = React.useRef<AbortController | null>(null);
  const mounted = React.useRef(true);
  React.useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      testRequest.current?.abort();
    };
  }, []);

  const accept = (res: WebSearchIntegrationSettings, preserveTimeoutChoice = false) => {
    setSettings(res);
    setEnabled(res.perplexity_enabled);
    setModel(res.perplexity_model);
    setTimeoutValue(String(res.perplexity_timeout_seconds));
    if (!preserveTimeoutChoice) setTimeoutTouched(res.perplexity_timeout_seconds !== defaultTimeout(res.perplexity_model));
  };
  React.useEffect(() => {
    let active = true;
    getJSON<WebSearchIntegrationSettings>(ENDPOINT).then(res => {
      if (!active) return;
      accept(res);
      setLoadState("ready");
    }, () => {
      if (active) setLoadState("error");
    });
    return () => { active = false; };
    // Loading a new response resets the form to its saved settings.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadVersion]);

  const ready = loadState === "ready";
  const configured = ready && Boolean(settings?.perplexity_api_key_set);
  const timeoutValue = Number(timeout);
  const validTimeout = timeout.trim() !== "" && Number.isInteger(timeoutValue)
    && timeoutValue >= 10 && timeoutValue <= 300;
  const dirty = Boolean(input.trim()) || Boolean(settings && (
    enabled !== settings.perplexity_enabled
    || model !== settings.perplexity_model
    || timeoutValue !== settings.perplexity_timeout_seconds
    || !validTimeout
  ));
  const busy = saving || testing;
  const canSave = ready && !busy && dirty && validTimeout;
  const clearTest = () => {
    setTestResult(null);
    setTestError(null);
    setSaveError(false);
  };
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSave) return;
    setSaving(true);
    clearTest();
    try {
      const res = await putJSON<WebSearchIntegrationSettings>(ENDPOINT, {
        ...(input.trim() ? { perplexity_api_key: input.trim() } : {}),
        perplexity_enabled: enabled,
        perplexity_model: model,
        perplexity_timeout_seconds: timeoutValue,
      });
      if (!mounted.current) return;
      accept(res, timeoutTouched);
      setInput("");
      setVisible(false);
      toast.success(t("common.saved"));
    } catch {
      if (mounted.current) setSaveError(true);
    } finally {
      if (mounted.current) setSaving(false);
    }
  };
  const testConnection = async () => {
    if (!ready || busy || dirty || !configured || !settings?.perplexity_enabled) return;
    const controller = new AbortController();
    testRequest.current = controller;
    const timer = window.setTimeout(() => controller.abort(), (settings.perplexity_timeout_seconds + 5) * 1000);
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      const response = await fetch(apiURL(`${ENDPOINT}/test`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("service_error");
      const result: WebSearchConnectionTest = await response.json();
      if (typeof result?.success !== "boolean" || typeof result.code !== "string") {
        throw new Error("invalid_response");
      }
      if (mounted.current && !controller.signal.aborted) setTestResult(result);
    } catch {
      if (mounted.current) setTestError(controller.signal.aborted ? "timeout" : "service_error");
    } finally {
      window.clearTimeout(timer);
      if (mounted.current) setTesting(false);
    }
  };
  const tested = testResult?.success === true && testResult.code === "ok";
  const resultCode = testError || testResult?.code;
  let statusText = c("已設 Key · 尚未測試", "Key set · Not tested");
  if (loadState === "loading") statusText = t("common.loading");
  else if (loadState === "error") statusText = t("settings.integrations.unavailable");
  else if (testing) statusText = c("測試連線中…", "Testing connection…");
  else if (resultCode) statusText = webSearchStatus(resultCode, en);
  else if (settings?.perplexity_error_code) statusText = webSearchStatus(settings.perplexity_error_code, en);
  else if (!settings?.perplexity_enabled) statusText = c("未啟用", "Disabled");
  else if (!configured) statusText = c("未設定 API Key", "API key not set");
  const statusState = tested ? "success"
    : resultCode || settings?.perplexity_error_code || loadState === "error" ? "error" : "ready";

  return (
    <Panel
      code="SEARCH"
      title={t("settings.integrations.searchTitle")}
      className={styles.panel}
      actions={(
        <span className={styles.status} data-state={statusState} role="status">
          {loadState === "loading" || testing
            ? <Spinner />
            : <span className={styles.statusDot} aria-hidden="true" />}
          {statusText}
        </span>
      )}
    >
      <div className={styles.body}>
        <div className={styles.provider}>
          <span className={styles.providerIcon} aria-hidden="true">
            <Search size={18} strokeWidth={1.7} />
          </span>
          <div>
            <h3 className={styles.providerName}>Perplexity</h3>
            <p className={styles.providerHint}>
              {c(
                "讓 Agent 查找公開技術文件與漏洞資訊。設定將套用到之後啟動的任務。",
                "Let agents look up public technical documentation and vulnerability information. Settings apply to newly started tasks.",
              )}
            </p>
            <p className={styles.providerHint}>
              {c(
                "未啟用或搜尋失敗時，任務會繼續使用其他工具。",
                "When search is disabled or fails, tasks continue using other tools.",
              )}
            </p>
          </div>
        </div>
        <form className={styles.form} onSubmit={save} aria-busy={busy}>
          <label className={styles.enableRow} htmlFor={`${fieldId}-enabled`}>
            <span>{c("啟用網頁搜尋", "Enable web search")}</span>
            <input
              id={`${fieldId}-enabled`}
              type="checkbox"
              role="switch"
              checked={enabled}
              disabled={!ready || busy}
              onChange={event => { setEnabled(event.target.checked); clearTest(); }}
            />
          </label>
          <label className="field-label" htmlFor={fieldId}>
            {t("settings.integrations.keyLabel")}
          </label>
          <div className={styles.inputWrap}>
            <input
              id={fieldId}
              className={`input-shell ${styles.keyInput}`}
              type={visible ? "text" : "password"}
              placeholder={ready ? configured ? settings?.perplexity_api_key_masked || "••••••••" : "pplx-…" : "—"}
              value={input}
              onChange={event => { setInput(event.target.value); clearTest(); }}
              disabled={!ready || busy}
              spellCheck={false}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              aria-describedby={`${fieldId}-hint`}
            />
            <button
              type="button"
              className={styles.visibility}
              disabled={!ready || busy || !input}
              onClick={() => setVisible(value => !value)}
              aria-label={t(visible ? "settings.integrations.hideKey" : "settings.integrations.showKey")}
              aria-controls={fieldId}
            >
              {visible ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
          <p className={styles.hint} id={`${fieldId}-hint`}>
            {configured
              ? c(
                "留白會保留目前的 Key。已儲存 Key 不代表連線或額度已驗證。",
                "Leave blank to keep the current key. A saved key does not verify connectivity or available quota.",
              )
              : t("settings.integrations.storageHint")}
            {settings?.perplexity_key_source === "environment" && (
              <> {c("目前使用主機環境中的 Key。", "Currently using the host environment's key.")}</>
            )}
          </p>
          <div className={styles.options}>
            <label>
              <span className="field-label">{c("搜尋模式", "Search mode")}</span>
              <select
                className="input-shell"
                value={model}
                disabled={!ready || busy}
                onChange={event => {
                  const next = event.target.value as PerplexityModel;
                  setModel(next);
                  if (!timeoutTouched) setTimeoutValue(String(defaultTimeout(next)));
                  clearTest();
                }}
              >
                <option value="sonar">{c("一般搜尋 · Sonar", "Standard search · Sonar")}</option>
                <option value="sonar-reasoning-pro">
                  {c("深度搜尋 · Sonar Reasoning Pro", "Deep search · Sonar Reasoning Pro")}
                </option>
              </select>
            </label>
            <label>
              <span className="field-label">{c("逾時秒數", "Timeout (seconds)")}</span>
              <input
                className="input-shell"
                type="number"
                min={10}
                max={300}
                step={1}
                inputMode="numeric"
                value={timeout}
                disabled={!ready || busy}
                onChange={event => {
                  setTimeoutValue(event.target.value);
                  setTimeoutTouched(true);
                  clearTest();
                }}
                aria-invalid={!validTimeout}
              />
            </label>
          </div>
          {!validTimeout && ready && (
            <p className={styles.error} role="alert">
              {c("逾時秒數需為 10–300 的整數。", "Timeout must be a whole number from 10 to 300 seconds.")}
            </p>
          )}
          <div className={styles.actions}>
            <button className={`button-primary ${styles.save}`} type="submit" disabled={!canSave}>
              {saving ? <Spinner /> : <Check size={16} aria-hidden="true" />}
              {t(saving ? "settings.integrations.saving" : "common.save")}
            </button>
            {dirty && (
              <span className={styles.hint}>
                {c("有未儲存的變更，請先儲存再測試連線。", "Save your changes before testing the connection.")}
              </span>
            )}
          </div>
          {loadState === "error" && (
            <div className={styles.errorRow}>
              <p role="alert">{t("settings.integrations.loadError")}</p>
              <button
                type="button"
                className={styles.retry}
                onClick={() => { setLoadState("loading"); setReloadVersion(value => value + 1); }}
              >
                {t("common.retry")}
              </button>
            </div>
          )}
          {saveError && <p className={styles.error} role="alert">{t("settings.integrations.saveError")}</p>}
          <div className={styles.testArea}>
            <div className={styles.testAction}>
              <button
                type="button"
                className={`button-secondary ${styles.testButton}`}
                disabled={!ready || busy || dirty || !configured || !settings?.perplexity_enabled}
                onClick={() => void testConnection()}
              >
                {testing ? <Spinner /> : <Radio size={15} />}
                {testing ? c("測試連線中…", "Testing connection…") : c("測試已儲存設定", "Test saved settings")}
              </button>
              <small>
                {c("會送出一次公開查詢，可能產生 API 費用。", "Sends one public query and may incur an API charge.")}
              </small>
            </div>
            {resultCode && (
              <div className={styles.testResult} data-success={tested}>
                <strong>{webSearchStatus(resultCode, en)}</strong>
                {testResult && (
                  <span>
                    {c("耗時", "Duration")}: {searchDuration(testResult.duration_seconds)} · {" "}
                    {c("服務端回報費用", "Provider-reported cost")}: {searchCost(testResult.usage?.cost_usd)}
                  </span>
                )}
              </div>
            )}
          </div>
        </form>
      </div>
    </Panel>
  );
}
