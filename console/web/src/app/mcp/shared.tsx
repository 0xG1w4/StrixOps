"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertCircle, X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import styles from "./mcp.module.css";

export function useMcpCopy() {
  const { locale } = useI18n();
  return (zh: string, en: string) => locale === "en" ? en : zh;
}

export function McpStatus({ status }: { status: string }) {
  const c = useMcpCopy();
  const names: Record<string, string> = {
    idle: c("尚未啟動", "Idle"), capturing: c("捕獲中", "Capturing"), stopped: c("已停止", "Stopped"),
    ended: c("已結束", "Ended"), error: c("錯誤", "Error"), starting: c("啟動中", "Starting"), stopping: c("停止中", "Stopping"),
    queued: c("排隊中", "Queued"), running: c("測試中", "Running"), completed: c("已完成", "Completed"),
    failed: c("失敗", "Failed"), cancelled: c("已取消", "Cancelled"), blocked: c("無法繼續", "Blocked"),
    ending: c("結束中", "Ending"),
    deleting: c("刪除中", "Deleting"), delete_failed: c("刪除失敗", "Delete failed"),
  };
  const tone = ["capturing", "running", "starting", "deleting"].includes(status) ? "active" : ["completed"].includes(status) ? "success" : ["error", "failed", "blocked", "delete_failed"].includes(status) ? "danger" : "muted";
  return <span className={styles.status} data-tone={tone}><span aria-hidden="true" />{names[status] || status}</span>;
}

export function McpError({ children, onRetry }: { children: React.ReactNode; onRetry?: () => void }) {
  const c = useMcpCopy();
  return <div className={styles.error} role="alert"><AlertCircle size={16} aria-hidden="true" /><span>{children}</span>{onRetry && <button type="button" className={styles.textButton} onClick={onRetry}>{c("重試", "Retry")}</button>}</div>;
}

export function McpModal({ title, description, children, onClose, wide = false }: { title: string; description?: string; children: React.ReactNode; onClose: () => void; wide?: boolean }) {
  const c = useMcpCopy();
  return <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}><Dialog.Portal><Dialog.Overlay className={styles.overlay} /><Dialog.Content className={`${styles.dialog} ${wide ? styles.dialogWide : ""}`} {...(!description ? { "aria-describedby": undefined } : {})}>
    <div className={styles.dialogHeader}><div><Dialog.Title className={styles.dialogTitle}>{title}</Dialog.Title>{description && <Dialog.Description className={styles.dialogDescription}>{description}</Dialog.Description>}</div><Dialog.Close asChild><button className={styles.iconButton} aria-label={c("關閉", "Close")} type="button"><X size={18} /></button></Dialog.Close></div>
    {children}
  </Dialog.Content></Dialog.Portal></Dialog.Root>;
}

export function dateText(value?: string | null, short = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  return short ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : date.toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function shortId(value: string): string {
  const identity = value.replace(/^(?:mcp|capture|flow|test|job|report|replay)_/, "");
  return identity.length > 14 ? identity.slice(0, 8) : identity;
}

export function sizeText(value?: number): string {
  if (value === undefined) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function downloadMarkdown(markdown: string, filename: string) {
  const url = URL.createObjectURL(new Blob([markdown], { type: "text/markdown;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
