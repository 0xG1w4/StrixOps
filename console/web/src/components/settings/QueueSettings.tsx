"use client";
import * as React from "react";
import { Layers } from "lucide-react";
import { Panel, Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import {
  taskError,
  taskRequest,
  type QueueSettings as Settings,
} from "@/lib/task-batches";
import styles from "./WebSearchSettings.module.css";
export function QueueSettings() {
  const { locale, t } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => (en ? english : zh);
  const [settings, setSettings] = React.useState<Settings | null>(null);
  const [value, setValue] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [loadError, setLoadError] = React.useState<unknown>(null);
  const [saveError, setSaveError] = React.useState<unknown>(null);
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const mounted = React.useRef(true);
  const dirty = React.useRef(false);
  const saving = React.useRef(false);
  const pending = React.useRef<AbortController | null>(null);
  const reload = React.useCallback(async () => {
    if (saving.current) return;
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setLoadError(null);
    try {
      const data = await taskRequest<Settings>("/api/scan-queue/settings", {
        signal: controller.signal,
      });
      if (mounted.current && !controller.signal.aborted) {
        setSettings(data);
        // Returning to the tab must not replace a user's unsaved choice.
        if (!dirty.current) setValue(String(data.max_active_targets));
      }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted) setLoadError(error);
    } finally {
      if (pending.current === controller) {
        pending.current = null;
        if (mounted.current) setLoading(false);
      }
    }
  }, []);
  React.useEffect(() => {
    mounted.current = true;
    const visibility = () => {
      if (document.hidden) pending.current?.abort();
      else void reload();
    };
    if (!document.hidden) void reload();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      mounted.current = false;
      pending.current?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [reload]);
  const number = Number(value);
  const valid =
    value.trim() !== "" &&
    Number.isInteger(number) &&
    number >= 1 &&
    number <= 16;
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!valid || saving.current || !settings) return;
    saving.current = true;
    // An older GET must never overwrite the settings returned by this PUT.
    pending.current?.abort();
    pending.current = null;
    setLoading(false);
    setBusy(true);
    setNotice("");
    setSaveError(null);
    try {
      const data = await taskRequest<Settings>("/api/scan-queue/settings", {
        method: "PUT",
        body: { max_active_targets: number },
      });
      if (mounted.current) {
        setSettings(data);
        setValue(String(data.max_active_targets));
        dirty.current = false;
        setLoadError(null);
        setNotice(c("主机并行上限已保存。", "Host concurrency limit saved."));
      }
    } catch (error) {
      if (mounted.current) setSaveError(error);
    } finally {
      saving.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <section id="task-queue" className={styles.panel}>
      <Panel code="QUEUE" title={c("任务队列", "Task queue")}>
        <div className={styles.body}>
          <div className={styles.provider}>
            <span className={styles.providerIcon}>
              <Layers size={18} />
            </span>
            <div>
              <strong className={styles.providerName}>
                {c("主机容量", "Host capacity")}
              </strong>
              <p className={styles.providerHint}>
                {c(
                  "单目标任务与所有批次共用此上限，超出的目标等待空位。",
                  "Single tasks and all batches share this limit. Excess targets wait for capacity.",
                )}
              </p>
            </div>
          </div>
          <form className={styles.form} onSubmit={save} aria-busy={loading || busy}>
            <label className="micro-label" htmlFor="queue-cap">
              {c("同时执行目标数", "Concurrent active targets")}
            </label>
            <div className={styles.controls}>
              <input
                id="queue-cap"
                type="number"
                min={1}
                max={16}
                step={1}
                value={value}
                placeholder="—"
                aria-describedby="queue-cap-help queue-cap-status"
                onChange={(event) => {
                  dirty.current = event.target.value !== String(settings?.max_active_targets ?? "");
                  setValue(event.target.value);
                  setNotice("");
                  setSaveError(null);
                }}
                className="input-shell"
                disabled={!settings || busy}
              />
              <button
                className="button-primary"
                type="submit"
                disabled={
                  !valid ||
                  !settings ||
                  busy ||
                  number === settings.max_active_targets
                }
              >
                {busy ? <Spinner /> : t("common.save")}
              </button>
            </div>
            <p id="queue-cap-help" className={styles.hint}>
              {c(
                "范围 1–16，默认 2。降低上限不会停止已执行的任务；新的目标会等待空位。",
                "Range 1–16, default 2. Lowering the limit keeps active tasks running; new targets wait for capacity.",
              )}
            </p>
            <p className={styles.hint}>
              {c("执行中", "Active")}: {settings?.active_targets ?? "—"} ·{" "}
              {c("等待", "Waiting")}: {settings?.waiting_targets ?? "—"} ·{" "}
              {c("受阻", "Blocked")}: {settings?.blocked_targets ?? "—"}
            </p>
            {settings && !valid && (
              <p className={styles.error}>
                {c("请输入 1–16 的整数。", "Enter an integer from 1 to 16.")}
              </p>
            )}
            <div id="queue-cap-status" role="status" aria-live="polite">
              {loading && (
                <p className={styles.hint}>
                  {settings
                    ? c("正在刷新主机容量…", "Refreshing host capacity…")
                    : c("正在读取并行上限，读取成功后即可修改。", "Loading the concurrency limit. It can be edited once loaded.")}
                </p>
              )}
              {loadError != null && (
                <div className={styles.errorRow}>
                  <span>
                    {settings
                      ? c("无法刷新设置；已保留当前输入与上次读取的容量信息。", "Could not refresh settings. Your input and the last loaded capacity information are retained.")
                      : c("尚未读取到并行上限，因此暂时无法修改。", "The concurrency limit has not loaded and cannot be edited yet.")}{" "}
                    {taskError(loadError, en)}
                  </span>
                  <button
                    className={styles.retry}
                    type="button"
                    disabled={loading || busy}
                    onClick={() => void reload()}
                  >
                    {t("common.retry")}
                  </button>
                </div>
              )}
              {saveError != null && <p className={styles.error}>{taskError(saveError, en)}</p>}
              {notice && <p className={styles.hint}>{notice}</p>}
            </div>
          </form>
        </div>
      </Panel>
    </section>
  );
}
