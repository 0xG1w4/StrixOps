"use client";

import * as React from "react";
import { CircleAlert, CircleCheck, FileUp, ListChecks, X } from "lucide-react";
import { apiURL } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { Spinner } from "@/components/ui";
import styles from "./MultiTargetEditor.module.css";

export const MAX_TARGETS = 100;
const MAX_LIST_BYTES = 512 * 1024;

type TargetItem = {
  target: string;
  valid: boolean;
  allowed: boolean;
  error?: string;
  normalized_target?: string;
};
type TargetValidation = {
  valid: boolean;
  targets: string[];
  target_count: number;
  items: TargetItem[];
  scope_revision?: number;
};
type TargetCheck = {
  key: string;
  state: "checking" | "ready" | "error";
  result?: TargetValidation;
  message?: string;
};

const COPY = {
  "zh-CN": {
    label: "目标清单",
    hint: "每行一个目标，2–100 个。所有目标使用上方所选任务类型。",
    description: "同一次任务共享分析上下文，并汇总为一份报告。",
    import: "导入 .txt",
    importHint: "追加到清单；空行与 # 注释行会略过，重复目标只保留一次。",
    fileError: "请选择不超过 512 KB 的 UTF-8 .txt 文件。",
    readError: "无法读取文件，请重新导入或直接粘贴内容。",
    tooFew: "至少输入 2 个不同目标，或返回单目标任务。",
    tooMany: "最多支持 100 个不同目标，请减少清单后继续。",
    checking: "正在逐一检查目标格式与项目范围…",
    error: "无法完成目标检查，请重试。",
    retry: "重新检查",
    malformed: "目标检查返回了无效结果，请重新检查。",
    ready: "所有目标已通过格式与范围检查",
    blocked: "清单中有无效或超出项目范围的目标，请修改后继续。",
    pending: "待检查",
    invalid: "格式无效",
    outside: "超出项目范围",
    allowed: "符合项目范围",
    valid: "格式有效",
    count: "个目标",
    duplicates: "个重复项已合并",
    remove: "移除目标",
    empty: "粘贴目标清单或导入文本文件，检查结果会逐项显示在这里。",
    list: "逐项目标检查结果",
    revision: "项目范围版本",
    remaining: "个目标未显示；请将清单缩减至 100 个以内。",
  },
  en: {
    label: "Target list",
    hint: "One target per line, 2–100 targets. All use the task type selected above.",
    description: "One task shares analysis context across these targets and produces one report.",
    import: "Import .txt",
    importHint: "Append to the list. Blank lines and # comments are ignored; duplicates are merged.",
    fileError: "Choose a UTF-8 .txt file no larger than 512 KB.",
    readError: "Could not read this file. Try again or paste its contents.",
    tooFew: "Enter at least 2 different targets, or return to a single-target task.",
    tooMany: "A task supports up to 100 different targets. Shorten the list to continue.",
    checking: "Checking each target’s format and project scope…",
    error: "Could not complete target checks. Try again.",
    retry: "Check again",
    malformed: "The target check returned an invalid result. Check again.",
    ready: "All targets passed format and scope checks",
    blocked: "Some targets are invalid or outside project scope. Edit the list to continue.",
    pending: "Pending check",
    invalid: "Invalid format",
    outside: "Outside project scope",
    allowed: "Within project scope",
    valid: "Valid format",
    count: "targets",
    duplicates: "duplicates merged",
    remove: "Remove target",
    empty: "Paste a target list or import a text file. Each target’s check will appear here.",
    list: "Individual target check results",
    revision: "Project scope version",
    remaining: "more targets hidden. Shorten the list to 100 targets or fewer.",
  },
};

export function parseTargetList(text: string) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#"));
  const targets = [...new Set(lines)];
  return { targets, duplicates: lines.length - targets.length };
}

function isValidation(value: unknown, targets: string[]): value is TargetValidation {
  if (!value || typeof value !== "object") return false;
  const result = value as TargetValidation;
  return typeof result.valid === "boolean"
    && result.target_count === targets.length
    && Array.isArray(result.targets) && result.targets.length === targets.length
    && result.targets.every((target, index) => target === targets[index])
    && Array.isArray(result.items) && result.items.length === targets.length
    && result.items.every((item, index) => item && item.target === targets[index]
      && typeof item.valid === "boolean" && typeof item.allowed === "boolean"
      && (item.error === undefined || typeof item.error === "string")
      && (item.normalized_target === undefined || typeof item.normalized_target === "string"))
    && (!result.valid || result.items.every((item) => item.valid && item.allowed))
    && (result.scope_revision === undefined || Number.isInteger(result.scope_revision));
}

export function useMultiTargetCheck({ enabled, text, scanType, projectId, retry }: {
  enabled: boolean;
  text: string;
  scanType: "web" | "internal";
  projectId: string;
  retry: number;
}) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  const parsed = React.useMemo(() => parseTargetList(text), [text]);
  const targetsJSON = JSON.stringify(parsed.targets);
  const key = JSON.stringify([enabled, projectId, scanType, targetsJSON, retry]);
  const [stored, setStored] = React.useState<TargetCheck | null>(null);
  const countValid = parsed.targets.length >= 2 && parsed.targets.length <= MAX_TARGETS;
  const check = stored?.key === key ? stored : null;

  React.useEffect(() => {
    if (!enabled || !countValid) return;
    const targets = JSON.parse(targetsJSON) as string[];
    const controller = new AbortController();
    let alive = true;
    let timeout: number | undefined;
    setStored({ key, state: "checking" });
    const timer = window.setTimeout(async () => {
      timeout = window.setTimeout(() => controller.abort(), 20000);
      try {
        const response = await fetch(apiURL("/api/scans/validate-targets"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ targets, scan_type: scanType, project_id: projectId }),
          cache: "no-store",
          signal: controller.signal,
        });
        const result: unknown = await response.json();
        if (!response.ok) {
          const detail = result && typeof result === "object" && "detail" in result ? result.detail : null;
          throw new Error(typeof detail === "string" ? detail : copy.error);
        }
        if (!isValidation(result, targets)) throw new Error(copy.malformed);
        if (alive) setStored({ key, state: "ready", result });
      } catch (error) {
        if (alive) setStored({ key, state: "error", message: error instanceof Error && !["AbortError", "TypeError", "SyntaxError"].includes(error.name) ? error.message : copy.error });
      } finally {
        if (timeout !== undefined) window.clearTimeout(timeout);
      }
    }, 350);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      if (timeout !== undefined) window.clearTimeout(timeout);
      controller.abort();
    };
  }, [enabled, countValid, targetsJSON, projectId, scanType, key, copy.error, copy.malformed]);

  const accepted = enabled && countValid && check?.state === "ready" && check.result?.valid === true;
  const message = parsed.targets.length < 2 ? copy.tooFew
    : parsed.targets.length > MAX_TARGETS ? copy.tooMany
      : check?.state === "error" ? check.message || copy.error
        : check?.state === "ready" ? accepted ? copy.ready : copy.blocked : copy.checking;
  return { ...parsed, check, accepted, message, countValid };
}

export function MultiTargetEditor({ text, onChange, internal, projectId, disabled, validation, onRetry, onImportingChange }: {
  text: string;
  onChange: (text: string) => void;
  internal: boolean;
  projectId: string;
  disabled: boolean;
  validation: ReturnType<typeof useMultiTargetCheck>;
  onRetry: () => void;
  onImportingChange: (importing: boolean) => void;
}) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  const fileInput = React.useRef<HTMLInputElement>(null);
  const editor = React.useRef<HTMLTextAreaElement>(null);
  const currentText = React.useRef(text);
  const mounted = React.useRef(true);
  const [importing, setImporting] = React.useState(false);
  const [fileError, setFileError] = React.useState("");
  currentText.current = text;
  React.useEffect(() => {
    mounted.current = true;
    editor.current?.focus();
    return () => { mounted.current = false; onImportingChange(false); };
  }, [onImportingChange]);
  const { targets, duplicates, check, accepted, message, countValid } = validation;
  const pending = countValid && (!check || check.state === "checking");
  const problem = targets.length > MAX_TARGETS || check?.state === "error" || (check?.state === "ready" && !accepted);

  const importFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || disabled) return;
    setFileError("");
    if (!file.name.toLowerCase().endsWith(".txt") || file.size > MAX_LIST_BYTES) {
      setFileError(copy.fileError);
      return;
    }
    setImporting(true);
    onImportingChange(true);
    try {
      const contents = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (contents.includes("\0")) throw new Error("binary file");
      if (mounted.current) {
        const next = [currentText.current.trimEnd(), contents].filter(Boolean).join("\n");
        if (new TextEncoder().encode(next).length > MAX_LIST_BYTES) setFileError(copy.fileError);
        else onChange(next);
        editor.current?.focus();
      }
    } catch {
      if (mounted.current) setFileError(copy.readError);
    } finally {
      if (mounted.current) { setImporting(false); onImportingChange(false); }
    }
  };

  return (
    <div className={styles.editor}>
      <div className={styles.context}>
        <ListChecks size={18} aria-hidden="true" />
        <p>{copy.description}</p>
      </div>
      <div className={styles.toolbar}>
        <label htmlFor="scan-targets">{copy.label}</label>
        <button type="button" onClick={() => fileInput.current?.click()} disabled={disabled || importing} aria-describedby="scan-targets-import-hint">
          {importing ? <Spinner /> : <FileUp size={14} aria-hidden="true" />}{copy.import}
        </button>
        <input ref={fileInput} type="file" accept=".txt,text/plain" className={styles.fileInput} onChange={(event) => void importFile(event)} tabIndex={-1} aria-hidden="true" />
      </div>
      <textarea
        ref={editor}
        id="scan-targets"
        value={text}
        onChange={(event) => { setFileError(""); onChange(event.target.value); }}
        placeholder={internal ? "10.0.0.0/24\n10.0.1.10" : "https://app.example.com\nhttps://api.example.com"}
        rows={5}
        maxLength={MAX_LIST_BYTES}
        spellCheck={false}
        autoComplete="off"
        disabled={disabled || importing}
        aria-invalid={problem}
        aria-describedby="scan-targets-hint scan-targets-status"
      />
      <div className={styles.meta}>
        <span id="scan-targets-hint">{copy.hint}</span>
        <span className={targets.length > MAX_TARGETS ? styles.danger : styles.count}>{targets.length} / {MAX_TARGETS} {copy.count}</span>
      </div>
      <p id="scan-targets-import-hint" className={styles.hint}>{copy.importHint}</p>
      {duplicates > 0 && <p className={styles.dedup} role="status">{duplicates} {copy.duplicates}</p>}
      {fileError && <p className={styles.danger} role="alert">{fileError}</p>}
      <div id="scan-targets-status" className={`${styles.status} ${problem ? styles.danger : accepted ? styles.success : ""}`} role="status" aria-live="polite">
        {pending ? <Spinner /> : accepted ? <CircleCheck size={15} aria-hidden="true" /> : <CircleAlert size={15} aria-hidden="true" />}
        <span>{message}</span>
        {check?.state === "error" && <button type="button" onClick={onRetry} disabled={disabled}>{copy.retry}</button>}
      </div>
      {targets.length === 0 ? <p className={styles.empty}>{copy.empty}</p> : (
        <div className={styles.results} role="region" aria-label={copy.list} tabIndex={0}>
          <ul>
            {targets.slice(0, MAX_TARGETS).map((target, index) => {
              const result = check?.result?.items[index];
              const invalid = result && (!result.valid || !result.allowed);
              const verdict = result ? !result.valid ? copy.invalid : !result.allowed ? copy.outside : projectId ? copy.allowed : copy.valid : copy.pending;
              return (
                <li key={target}>
                  <span className={styles.index} aria-hidden="true">{index + 1}</span>
                  <div className={styles.resultBody}>
                    <code>{target}</code>
                    <span className={invalid ? styles.danger : result ? styles.success : styles.muted}>
                      {result ? invalid ? <CircleAlert size={12} aria-hidden="true" /> : <CircleCheck size={12} aria-hidden="true" /> : null}
                      {verdict}{result?.error ? ` · ${result.error}` : ""}
                    </span>
                  </div>
                  <button
                    type="button"
                    title={`${copy.remove}: ${target}`}
                    aria-label={`${copy.remove}: ${target}`}
                    disabled={disabled || importing}
                    onClick={() => {
                      onChange(text.split(/\r?\n/).filter((line) => line.trim() !== target).join("\n"));
                      editor.current?.focus();
                    }}
                  ><X size={15} aria-hidden="true" /></button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      {targets.length > MAX_TARGETS && <p className={styles.danger}>{targets.length - MAX_TARGETS} {copy.remaining}</p>}
      {projectId && check?.result?.scope_revision !== undefined && <p className={styles.hint}>{copy.revision} {check.result.scope_revision}</p>}
    </div>
  );
}
