"use client";

import * as React from "react";
import { BookOpen, ListChecks, Network } from "lucide-react";
import AssessmentPanel from "@/components/AssessmentPanel";
import NotesPanel from "@/components/NotesPanel";
import type { RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type { NotebookView } from "@/lib/run-navigation";

export default function NotebookPanel({ name, run, view, onViewChange }: {
  name: string;
  run: RunDetail | null;
  view: NotebookView;
  onViewChange: (view: NotebookView) => void;
}) {
  const { locale } = useI18n();
  const en = locale === "en";
  const choices: Array<{ value: NotebookView; label: string; icon: typeof BookOpen }> = [
    { value: "shared", label: en ? "Shared notes" : "共享筆記", icon: BookOpen },
    { value: "coverage", label: en ? "Test coverage" : "測試覆蓋", icon: ListChecks },
    { value: "threat_models", label: en ? "Threat models" : "威脅模型", icon: Network },
  ];
  return (
    <section className="min-w-0" aria-label={en ? "Notes" : "筆記"}>
      <div className="flex flex-wrap gap-2 border-b border-line/6 p-3" role="group" aria-label={en ? "Note type" : "筆記類型"}>
        {choices.map(({ value, label, icon: Icon }) => (
          <button
            key={value}
            type="button"
            aria-pressed={view === value}
            className={`inline-flex min-h-9 items-center gap-2 rounded border px-3 py-2 text-xs transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${view === value ? "border-accent/40 bg-accent/10 text-accent" : "border-line/10 text-fg-muted hover:border-line/30 hover:text-fg"}`}
            onClick={() => onViewChange(value)}
          >
            <Icon size={14} aria-hidden />{label}
          </button>
        ))}
      </div>
      {view === "shared"
        ? <NotesPanel key={name} runName={name} live={Boolean(run?.live)} />
        : <AssessmentPanel key={name} name={name} run={run} view={view} />}
    </section>
  );
}
