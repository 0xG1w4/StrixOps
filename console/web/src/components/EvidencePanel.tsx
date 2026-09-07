"use client";

/* ============================================================================
   EvidencePanel — evidence files collected from the agent's workspace.

   Grouped by category (credential_dump, ssh_key, cloud_credential, …),
   each entry shows filename, size, SHA256 prefix, timestamp, and a floating
   content preview modal. Downloads link directly to the console API.
   ========================================================================= */

import * as React from "react";
import {
  Cloud,
  Database,
  FileText,
  KeyRound,
  Lock,
  Map,
  Settings2,
  Wallet,
  X,
} from "lucide-react";
import { EmptyState, MicroLabel, Panel, Spinner } from "@/components/ui";
import { apiURL, getEvidence, type EvidenceEntry, type EvidencePage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { relTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const CATEGORY_ICONS: Record<string, React.ComponentType<{ className?: string }>> = {
  credential_dump: Lock,
  credential: KeyRound,
  ssh_key: KeyRound,
  cloud_credential: Cloud,
  crypto: Wallet,
  config: Settings2,
  recon_data: Map,
  database: Database,
  data: FileText,
  other: FileText,
};

const CATEGORY_TONES: Record<string, string> = {
  credential_dump: "text-danger",
  credential: "text-warning",
  ssh_key: "text-warning",
  cloud_credential: "text-violet",
  crypto: "text-warning",
  config: "text-accent",
  recon_data: "text-fg-muted",
  database: "text-accent",
  data: "text-fg-muted",
  other: "text-fg-muted",
};

export default function EvidencePanel({ name, run }: { name: string; run: any }) {
  const { t } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [data, setData] = React.useState<EvidencePage | null>(null);
  const [selected, setSelected] = React.useState<EvidenceEntry | null>(null);
  const live = Boolean(run?.live);

  const load = React.useCallback(async () => {
    try {
      const page = await getEvidence(name);
      setData(page);
      setPhase("ready");
    } catch {
      setPhase("error");
    }
  }, [name]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const grouped = React.useMemo<Array<[string, EvidenceEntry[]]>>(() => {
    if (!data?.evidence?.length) return [] as Array<[string, EvidenceEntry[]]>;
    const map: Record<string, EvidenceEntry[]> = {};
    for (const entry of data.evidence) {
      (map[entry.category] ??= []).push(entry);
    }
    return Object.entries(map);
  }, [data]);

  return (
    <div className="space-y-4 p-4">
      {phase === "loading" && (
        <div className="flex items-center gap-2 py-4 text-sm text-fg-muted">
          <Spinner /> {t("common.loading")}
        </div>
      )}
      {phase === "error" && (
        <div className="alert-error">{t("common.offline")}</div>
      )}
      {phase === "ready" && (!data || data.evidence.length === 0) && (
        <EmptyState title={t("evidence.empty")} hint={t("evidence.empty.hint")} />
      )}
      {phase === "ready" && data && data.evidence.length > 0 && (
        <Panel
          title={t("evidence.title")}
          actions={<MicroLabel>{t("evidence.count", { n: data.totals.count })} · {data.totals.total_human}</MicroLabel>}
        >
          <div className="space-y-4">
            {grouped.map(([category, entries]) => {
              const Icon = CATEGORY_ICONS[category] ?? FileText;
              const tone = CATEGORY_TONES[category] ?? "text-fg-muted";
              return (
                <div key={category}>
                  <div className="mb-2 flex items-center gap-2">
                    <Icon className={cn("h-4 w-4", tone)} />
                    <span className="micro-label">
                      {t(`evidence.category.${category}`)} ({entries.length})
                    </span>
                  </div>
                  <div className="space-y-2">
                    {entries.map((entry) => (
                      <EvidenceRow
                        key={entry.filename}
                        entry={entry}
                        Icon={Icon}
                        tone={tone}
                        onSelect={() => setSelected(entry)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>
      )}

      {/* floating content preview */}
      {selected && <EvidenceModal entry={selected} name={name} onClose={() => setSelected(null)} />}
    </div>
  );
}

function EvidenceRow({
  entry,
  Icon,
  tone,
  onSelect,
}: {
  entry: EvidenceEntry;
  Icon: React.ComponentType<{ className?: string }>;
  tone: string;
  onSelect: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2.5 rounded-[1rem] border border-line/6 bg-surface/60 px-3.5 py-3 transition-colors hover:border-line/12">
      <Icon className={cn("h-4 w-4 shrink-0", tone)} />
      <span className="min-w-0 flex-1 truncate font-mono text-sm text-fg">{entry.filename}</span>
      <span className="shrink-0 font-mono text-[10px] text-fg-muted">{entry.size_human}</span>
      <span className="hidden shrink-0 font-mono text-[10px] text-fg-faint sm:inline" title={entry.sha256}>
        {entry.sha256_short}
      </span>
      <span className="shrink-0 font-mono text-[10px] text-fg-faint">{relTime(entry.collected_at)}</span>
      {entry.oversize && (
        <span className="mono-chip chip-warning" title={t_oversize()}>
          ⚠ oversize
        </span>
      )}
      <div className="flex shrink-0 gap-1.5">
        <button
          type="button"
          className="button-ghost button-compact"
          onClick={onSelect}
        >
          <FileText className="h-3 w-3" />
        </button>
        <a
          href={apiURL(entry.download_url)}
          className="button-ghost button-compact"
          download={entry.filename}
        >
          ↓
        </a>
      </div>
    </div>
  );
}

function t_oversize() {
  return "File too large to copy — metadata only";
}

function EvidenceModal({
  entry,
  name,
  onClose,
}: {
  entry: EvidenceEntry;
  name: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [content, setContent] = React.useState<string | null>(null);
  const closeRef = React.useRef<HTMLButtonElement | null>(null);
  const Icon = CATEGORY_ICONS[entry.category] ?? FileText;
  const tone = CATEGORY_TONES[entry.category] ?? "text-fg-muted";

  React.useEffect(() => {
    closeRef.current?.focus();
  }, []);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  React.useEffect(() => {
    let disposed = false;
    fetch(apiURL(`/api/runs/${encodeURIComponent(name)}/evidence/${encodeURIComponent(entry.filename)}`))
      .then(async (res) => (res.ok ? res.text() : "(unable to load)"))
      .then((text) => {
        if (!disposed) setContent(text.slice(0, 5000));
      })
      .catch(() => {
        if (!disposed) setContent("(error loading file)");
      });
    return () => {
      disposed = true;
    };
  }, [name, entry.filename]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm sm:p-6"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="panel flex max-h-[85vh] w-full max-w-3xl animate-enter flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-start gap-3 border-b border-line/6 px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2.5">
              <Icon className={cn("h-5 w-5", tone)} />
              <h3 className="min-w-0 truncate font-mono text-base font-bold" style={{ color: "var(--text-heading)" }}>
                {entry.filename}
              </h3>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <span className="mono-chip text-[9px]">{t(`evidence.category.${entry.category}`)}</span>
              <span className="mono-chip text-[9px]">{entry.size_human}</span>
              {entry.sha256_short && (
                <span className="mono-chip text-[9px]" title={entry.sha256}>
                  SHA256: {entry.sha256_short}
                </span>
              )}
              {entry.oversize && <span className="mono-chip chip-warning text-[9px]">⚠ oversize</span>}
            </div>
          </div>
          <a
            href={apiURL(entry.download_url)}
            className="button-primary button-compact"
            download={entry.filename}
          >
            ↓ {t("evidence.download")}
          </a>
          <button
            ref={closeRef}
            type="button"
            className="rounded-lg p-1.5 text-fg-muted transition-colors hover:bg-raised/6 hover:text-fg"
            onClick={onClose}
            aria-label={t("common.close")}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {/* content preview */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {content === null ? (
            <div className="flex items-center gap-2 py-6 text-sm text-fg-muted">
              <Spinner /> {t("common.loading")}
            </div>
          ) : (
            <pre className="whitespace-pre-wrap break-all font-mono text-xs leading-relaxed text-fg-2">
              {content}
              {content.length >= 5000 && "\n\n… (truncated preview — download for full file)"}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
