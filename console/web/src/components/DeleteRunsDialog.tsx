"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Trash2 } from "lucide-react";
import { Spinner } from "@/components/ui";
import { del, runTargetLabel, type OkResult, type RunSummary } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { deleteRuns, type RunDeletionResult } from "@/lib/run-deletion";
import styles from "./DeleteRunsDialog.module.css";

const COPY = {
  en: {
    title: "Delete selected tasks",
    count: (count: number) => `${count} task${count === 1 ? "" : "s"} selected for deletion`,
    warning: "These tasks and their reports, evidence and logs will be permanently deleted. This cannot be undone.",
    confirm: "Delete permanently",
    progress: (done: number, total: number) => `Processing ${done} / ${total} tasks…`,
    unconfirmed: "The server did not confirm deletion.",
  },
  "zh-CN": {
    title: "删除选中的任务",
    count: (count: number) => `即将删除 ${count} 个任务`,
    warning: "这些任务及其报告、证据和日志将被永久删除，删除后无法恢复。",
    confirm: "永久删除",
    progress: (done: number, total: number) => `正在处理 ${done} / ${total} 个任务…`,
    unconfirmed: "服务器未确认删除成功。",
  },
};

export default function DeleteRunsDialog({
  runs,
  onClose,
  onDeleted,
}: {
  runs: RunSummary[];
  onClose: () => void;
  onDeleted: (result: RunDeletionResult) => void;
}) {
  const { locale, t } = useI18n();
  const copy = COPY[locale === "en" ? "en" : "zh-CN"];
  // Keep both the displayed scope and the deletion request fixed while open.
  const [selection] = React.useState(() => {
    const seen = new Set<string>();
    return runs.filter((run) => {
      if (seen.has(run.name)) return false;
      seen.add(run.name);
      return true;
    }).map((run) => ({ name: run.name, target: runTargetLabel(run) }));
  });
  const [busy, setBusy] = React.useState(false);
  const [done, setDone] = React.useState(0);
  const inFlight = React.useRef(false);
  const cancelRef = React.useRef<HTMLButtonElement>(null);

  const close = () => {
    if (!inFlight.current) onClose();
  };
  const confirm = async () => {
    if (inFlight.current || selection.length === 0) return;
    inFlight.current = true;
    setBusy(true);
    const result = await deleteRuns(
      selection.map((run) => run.name),
      async (name) => {
        const response = await del<OkResult>(`/api/runs/${encodeURIComponent(name)}`);
        if (response?.ok !== true) throw new Error(copy.unconfirmed);
      },
      (completed) => setDone(completed),
    );
    inFlight.current = false;
    setBusy(false);
    onDeleted(result);
  };

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) close(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={styles.dialog}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelRef.current?.focus({ preventScroll: true });
          }}
          onEscapeKeyDown={(event) => { if (inFlight.current) event.preventDefault(); }}
          onInteractOutside={(event) => { if (inFlight.current) event.preventDefault(); }}
        >
          <header className={styles.header}>
            <Trash2 size={20} aria-hidden="true" />
            <Dialog.Title className={styles.title}>{copy.title}</Dialog.Title>
          </header>
          <div className={styles.body}>
            <Dialog.Description className={styles.warning}>{copy.warning}</Dialog.Description>
            <p className={styles.count}>{copy.count(selection.length)}</p>
            <ul className={styles.list} aria-label={copy.count(selection.length)} tabIndex={0}>
              {selection.map((run) => (
                <li key={run.name}>
                  <strong>{run.name}</strong>
                  <span>{run.target}</span>
                </li>
              ))}
            </ul>
          </div>
          <footer className={styles.footer}>
            <p className={styles.progress} role="status" aria-live="polite">
              {busy ? copy.progress(done, selection.length) : ""}
            </p>
            <div className={styles.actions}>
              <button ref={cancelRef} type="button" className="button-secondary button-compact" disabled={busy} onClick={close}>
                {t("common.cancel")}
              </button>
              <button type="button" className={`button-danger button-compact ${styles.confirm}`} disabled={busy || selection.length === 0} onClick={() => void confirm()}>
                {busy ? <Spinner /> : <Trash2 size={14} aria-hidden="true" />}
                {copy.confirm}
              </button>
            </div>
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
