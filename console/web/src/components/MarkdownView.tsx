"use client";

import { Code2, Eye } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import styles from "./MarkdownView.module.css";

export type MarkdownViewMode = "preview" | "raw";

export function MarkdownViewToggle({ value, onChange, disabled = false }: {
  value: MarkdownViewMode;
  onChange: (value: MarkdownViewMode) => void;
  disabled?: boolean;
}) {
  const { locale } = useI18n();
  const en = locale === "en";
  return <div className={styles.toggle} role="group" aria-label={en ? "Content display mode" : "内容显示方式"}>
    <button type="button" aria-pressed={value === "preview"} disabled={disabled} onClick={() => onChange("preview")}>
      <Eye size={14} aria-hidden="true" />{en ? "Preview" : "预览"}
    </button>
    <button type="button" aria-pressed={value === "raw"} disabled={disabled} onClick={() => onChange("raw")}
      title={en ? "View Markdown source" : "查看 Markdown 原文"}>
      <Code2 size={14} aria-hidden="true" />Raw
    </button>
  </div>;
}

/** Render the saved Markdown as text without parsing or rewriting it. */
export function RawMarkdown({ content }: { content: string }) {
  const { locale } = useI18n();
  return <pre className={styles.raw} tabIndex={0} role="region" aria-label={locale === "en" ? "Markdown source" : "Markdown 原文"} data-markdown-raw>
    <code>{content}</code>
  </pre>;
}
