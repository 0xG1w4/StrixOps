"use client";
import * as React from "react";
import Link from "next/link";
import { Eye, EyeOff, Globe2, Radio } from "lucide-react";
import { Panel, Spinner, ConfirmButton } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { taskRequest } from "@/lib/task-batches";
import {
  fofaError,
  type FofaSettings as Settings,
  type FofaTest,
} from "@/lib/fofa";
import styles from "./WebSearchSettings.module.css";

export function FofaSettings() {
  const { locale, t } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => (en ? english : zh);
  const [settings, setSettings] = React.useState<Settings | null>(null);
  const [email, setEmail] = React.useState("");
  const [key, setKey] = React.useState("");
  const [enabled, setEnabled] = React.useState(false);
  const [visible, setVisible] = React.useState(false);
  const [error, setError] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [revision, setRevision] = React.useState(0);
  const [busy, setBusy] = React.useState<"save" | "test" | null>(null);
  const [result, setResult] = React.useState<FofaTest | null>(null);
  const mounted = React.useRef(true);
  const localeIsEnglish = React.useRef(en);
  localeIsEnglish.current = en;
  const pending = React.useRef<AbortController | null>(null);
  const accept = (data: Settings) => {
    setSettings(data);
    setEmail(data.email);
    setEnabled(data.enabled);
    setKey("");
    setVisible(false);
  };
  React.useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    setBusy(null);
    setError("");
    taskRequest<Settings>("/api/fofa/settings", {
      signal: controller.signal,
    }).then(
      (data) => {
        if (!controller.signal.aborted) accept(data);
      },
      (error) => {
        if (!controller.signal.aborted)
          setError(fofaError(error, localeIsEnglish.current));
      },
    );
    return () => {
      mounted.current = false;
      controller.abort();
      pending.current?.abort();
    };
  }, [revision]);
  const dirty = Boolean(
    settings &&
      (email.trim() !== settings.email ||
        enabled !== settings.enabled ||
        key.trim()),
  );
  const clearFeedback = () => {
    setResult(null);
    setError("");
    setNotice("");
  };
  const save = async (clearKey = false) => {
    if (!settings || busy) return;
    clearFeedback();
    setBusy("save");
    const controller = new AbortController();
    pending.current = controller;
    try {
      const data = await taskRequest<Settings>("/api/fofa/settings", {
        method: "PUT",
        signal: controller.signal,
        body: clearKey
          ? { clear_key: true }
          : {
              email: email.trim(),
              enabled,
              ...(key.trim() ? { key: key.trim() } : {}),
            },
      });
      if (mounted.current && !controller.signal.aborted) {
        accept(data);
        setNotice(c("FOFA 设置已保存。", "FOFA settings saved."));
      }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted)
        setError(fofaError(error, en));
    } finally {
      if (mounted.current && !controller.signal.aborted) setBusy(null);
    }
  };
  const test = async () => {
    if (!settings?.enabled || !settings.key_set || dirty || busy) return;
    clearFeedback();
    setBusy("test");
    const controller = new AbortController();
    pending.current = controller;
    try {
      const data = await taskRequest<FofaTest>("/api/fofa/settings/test", {
        method: "POST",
        body: {},
        signal: controller.signal,
      });
      if (mounted.current && !controller.signal.aborted) setResult(data);
    } catch (error) {
      if (mounted.current && !controller.signal.aborted)
        setError(fofaError(error, en));
    } finally {
      if (mounted.current && !controller.signal.aborted) setBusy(null);
    }
  };
  const status = !settings
    ? c("尚未读取设置", "Settings not loaded")
    : !settings.enabled
      ? c("未启用", "Disabled")
      : !settings.key_set
        ? c("尚未设置凭证", "Credentials not set")
        : result?.success
          ? c("连接成功", "Connection verified")
          : c("已保存，尚未验证连接", "Saved, connection not verified");
  return (
    <section id="fofa-settings" className={styles.panel}>
      <Panel
        code="FOFA"
        title={c("FOFA 资产搜索", "FOFA asset search")}
        actions={
          <span
            className={styles.status}
            data-state={
              result?.success
                ? "success"
                : result && !result.success
                  ? "error"
                  : undefined
            }
          >
            {status}
          </span>
        }
      >
        <div className={styles.body}>
          <div className={styles.provider}>
            <span className={styles.providerIcon}>
              <Globe2 size={18} />
            </span>
            <div>
              <strong className={styles.providerName}>FOFA</strong>
              <p className={styles.providerHint}>
                {c(
                  "查询公网资产，筛选后交给独立目标任务。",
                  "Search public assets, then choose targets for independent tasks.",
                )}
              </p>
              <Link href="/fofa" className="text-accent text-xs">
                {c("打开 FOFA", "Open FOFA")} →
              </Link>
            </div>
          </div>
          <form
            className={styles.form}
            onSubmit={(event) => {
              event.preventDefault();
              void save();
            }}
          >
            <label className={styles.enableRow}>
              {c("启用 FOFA", "Enable FOFA")}
              <input
                type="checkbox"
                checked={enabled}
                disabled={!settings || Boolean(busy)}
                onChange={(event) => {
                  setEnabled(event.target.checked);
                  clearFeedback();
                }}
              />
            </label>
            <label htmlFor="fofa-email" className="micro-label">
              Email · {c("选填", "Optional")}
            </label>
            <input
              id="fofa-email"
              className="input-shell w-full mb-4"
              type="email"
              value={email}
              disabled={!settings || Boolean(busy)}
              onChange={(event) => {
                setEmail(event.target.value);
                clearFeedback();
              }}
              autoComplete="off"
              maxLength={320}
            />
            <label htmlFor="fofa-key" className="micro-label">
              API Key
            </label>
            <div className={styles.controls}>
              <div className={styles.inputWrap}>
                <input
                  id="fofa-key"
                  className={`input-shell ${styles.keyInput}`}
                  type={visible ? "text" : "password"}
                  value={key}
                  placeholder={
                    settings?.key_set
                      ? settings.key_masked
                      : c("输入 FOFA API Key", "Enter FOFA API key")
                  }
                  disabled={!settings || Boolean(busy)}
                  onChange={(event) => {
                    setKey(event.target.value);
                    clearFeedback();
                  }}
                  autoComplete="new-password"
                  spellCheck={false}
                  maxLength={8192}
                />
                <button
                  type="button"
                  className={styles.visibility}
                  aria-label={
                    visible
                      ? c("隐藏 Key", "Hide key")
                      : c("显示 Key", "Show key")
                  }
                  onClick={() => setVisible((value) => !value)}
                >
                  {visible ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
              <button
                type="submit"
                className={`button-primary ${styles.save}`}
                disabled={!dirty || Boolean(busy)}
              >
                {busy === "save" ? <Spinner /> : t("common.save")}
              </button>
            </div>
            <p className={styles.hint}>
              {c(
                "Key 留空会保留已保存值。修改设置后请先保存，再测试连接。",
                "Leave the key blank to retain it. Save changes before testing the connection.",
              )}
            </p>
            {settings?.key_set && (
              <div className={styles.actions}>
                <ConfirmButton
                  label={c("清除已保存 Key", "Clear saved key")}
                  confirmLabel={c("确认清除", "Confirm clear")}
                  danger
                  disabled={Boolean(busy)}
                  onConfirm={() => void save(true)}
                />
              </div>
            )}
            <div className={styles.testArea}>
              <div className={styles.testAction}>
                <button
                  type="button"
                  className="button-secondary"
                  disabled={
                    !settings?.enabled ||
                    !settings.key_set ||
                    dirty ||
                    Boolean(busy)
                  }
                  onClick={() => void test()}
                >
                  {busy === "test" ? <Spinner /> : <Radio size={14} />}
                  {c("测试已保存连接", "Test saved connection")}
                </button>
                <small>
                  {c(
                    "测试读取账号信息，不执行资产查询。",
                    "The test reads account information without searching for assets.",
                  )}
                </small>
              </div>
              {result && (
                <div
                  className={styles.testResult}
                  data-success={result.success}
                >
                  <strong>
                    {result.success
                      ? c("连接成功", "Connection verified")
                      : fofaError(result.code, en)}
                  </strong>
                  {result.account && (
                    <>
                      <span>
                        {c("查询余量", "Remaining queries")}:{" "}
                        {result.account.remaining_queries ?? "—"}
                      </span>
                      <span>
                        {c("数据余量", "Remaining data")}:{" "}
                        {result.account.remaining_data ?? "—"}
                      </span>
                    </>
                  )}
                </div>
              )}
            </div>
            {notice && (
              <p className={styles.hint} role="status">
                {notice}
              </p>
            )}
            {error && (
              <div className={styles.errorRow} role="status">
                <span>{error}</span>
                {!settings && (
                  <button
                    type="button"
                    className={styles.retry}
                    onClick={() => setRevision((value) => value + 1)}
                  >
                    {t("common.retry")}
                  </button>
                )}
              </div>
            )}
          </form>
        </div>
      </Panel>
    </section>
  );
}
