"use client";
import * as React from "react";
import { Layers } from "lucide-react";
import { Panel, Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { useQueueResource } from "@/components/batches/useQueueResource";
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
  const resource = useQueueResource<Settings>(
    "/api/scan-queue/settings",
    false,
  );
  const [value, setValue] = React.useState("2");
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const mounted = React.useRef(true);
  React.useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  React.useEffect(() => {
    if (resource.data) setValue(String(resource.data.max_active_targets));
  }, [resource.data]);
  const number = Number(value);
  const valid =
    value.trim() !== "" &&
    Number.isInteger(number) &&
    number >= 1 &&
    number <= 16;
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!valid || busy || !resource.data) return;
    setBusy(true);
    setNotice("");
    try {
      await taskRequest("/api/scan-queue/settings", {
        method: "PUT",
        body: { max_active_targets: number },
      });
      if (mounted.current) {
        resource.retry();
        setNotice(c("主机并行上限已保存。", "Host concurrency limit saved."));
      }
    } catch (error) {
      if (mounted.current) setNotice(taskError(error, en));
    } finally {
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
          <form className={styles.form} onSubmit={save}>
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
                onChange={(event) => setValue(event.target.value)}
                className="input-shell"
                disabled={!resource.data || busy}
              />
              <button
                className="button-primary"
                type="submit"
                disabled={
                  !valid ||
                  !resource.data ||
                  busy ||
                  number === resource.data.max_active_targets
                }
              >
                {busy ? <Spinner /> : t("common.save")}
              </button>
            </div>
            <p className={styles.hint}>
              {c(
                "范围 1–16，默认 2。降低上限不会停止已执行的任务；新的目标会等待空位。",
                "Range 1–16, default 2. Lowering the limit keeps active tasks running; new targets wait for capacity.",
              )}
            </p>
            <p className={styles.hint}>
              {c("执行中", "Active")}: {resource.data?.active_targets ?? "—"} ·{" "}
              {c("等待", "Waiting")}: {resource.data?.waiting_targets ?? "—"} ·{" "}
              {c("受阻", "Blocked")}: {resource.data?.blocked_targets ?? "—"}
            </p>
            {!valid && (
              <p className={styles.error}>
                {c("请输入 1–16 的整数。", "Enter an integer from 1 to 16.")}
              </p>
            )}
            {(notice || resource.error) && (
              <div className={styles.errorRow} role="status">
                <span>{notice || taskError(resource.error, en)}</span>
                {resource.error && (
                  <button
                    className={styles.retry}
                    type="button"
                    onClick={resource.retry}
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
