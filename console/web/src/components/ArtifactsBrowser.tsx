"use client";

/* ============================================================================
   ArtifactsBrowser — browse everything the engine wrote into a run dir.

   Left: a lazy file tree (root artifacts, then one group per directory).
   Right: an in-console preview — .md renders through the same react-markdown
   pipeline as the report panel, .csv as parsed rows, .json/.jsonl pretty,
   everything else raw — with a formatted/raw toggle and per-file + zip-bundle
   download. Backed by GET /api/runs/{name}/artifacts (index) and the
   containment-safe artifacts file route.
   ========================================================================= */

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Braces,
  Download,
  File as FileIcon,
  FileText,
  Package,
  RefreshCw,
  ScrollText,
  Search,
  Table,
} from "lucide-react";
import { EmptyState, Spinner } from "@/components/ui";
import { apiURL, archiveURL, getJSON } from "@/lib/api";
import type { ArtifactFile, ArtifactsIndexPage } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const INDEX_POLL_MS = 10000;
const PREVIEW_CHAR_CAP = 400_000;
const CSV_ROW_CAP = 400;

/* ------------------------------------------------------------------ helpers */

function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

function fmtBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function extOf(path: string): string {
  const base = path.split("/").pop() || "";
  const dot = base.lastIndexOf(".");
  return dot === -1 ? "" : base.slice(dot + 1).toLowerCase();
}

function iconFor(path: string) {
  const ext = extOf(path);
  if (ext === "md") return FileText;
  if (ext === "csv") return Table;
  if (ext === "json" || ext === "jsonl") return Braces;
  if (ext === "log" || ext === "txt") return ScrollText;
  return FileIcon;
}

function kindKey(path: string): string {
  const ext = extOf(path);
  if (ext === "md") return "artifacts.kind.markdown";
  if (ext === "csv") return "artifacts.kind.csv";
  if (ext === "json" || ext === "jsonl") return "artifacts.kind.json";
  if (ext === "log" || ext === "txt") return "artifacts.kind.text";
  return "artifacts.kind.file";
}

/** Minimal RFC-4180 CSV reader — quoted cells, doubled quotes, CRLF. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else {
          quoted = false;
        }
      } else {
        cell += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n") {
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else if (ch !== "\r") {
      cell += ch;
    }
  }
  if (cell !== "" || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  return rows.filter((r) => r.length > 1 || (r[0] ?? "") !== "");
}

/** Pretty-print .json / .jsonl payloads (jsonl: one document per line). */
function prettyStructured(text: string, path: string): string {
  if (extOf(path) === "jsonl") {
    const out: string[] = [];
    for (const line of text.split("\n")) {
      if (!line.trim()) continue;
      try {
        out.push(JSON.stringify(JSON.parse(line), null, 2));
      } catch {
        out.push(line);
      }
    }
    return out.join("\n");
  }
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/* --------------------------------------------------------------- components */

function FileRow({
  file,
  active,
  onSelect,
}: {
  file: ArtifactFile;
  active: boolean;
  onSelect: () => void;
}) {
  const Icon = iconFor(file.path);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      title={file.path}
      className={cn(
        "flex w-full items-center gap-2 rounded-[0.7rem] border px-2.5 py-1.5 text-left transition-colors",
        active
          ? "border-accent/30 bg-accent/10 text-[#7ff4ff]"
          : "border-transparent text-fg-2 hover:border-line/8 hover:bg-raised/4"
      )}
    >
      <Icon className="h-3.5 w-3.5 shrink-0" strokeWidth={1.8} />
      <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
        {file.path.split("/").pop()}
      </span>
      <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.12em] text-fg-faint">
        {fmtBytes(file.size)}
      </span>
    </button>
  );
}

function PreviewFrame({
  name,
  path,
  text,
  truncated,
}: {
  name: string;
  path: string;
  text: string;
  truncated: boolean;
}) {
  const { t } = useI18n();
  const ext = extOf(path);
  const formattable = ext === "md" || ext === "csv" || ext === "json" || ext === "jsonl";
  const [raw, setRaw] = React.useState(false);
  React.useEffect(() => setRaw(false), [path]);

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line/6 px-4 py-2.5">
      <span className="min-w-0 truncate font-mono text-[11px] text-fg-2" title={path}>
        {path}
        <span className="ml-2 text-fg-faint">
          {t(kindKey(path))} · {fmtBytes(new Blob([text]).size)}
          {truncated ? ` · ${t("artifacts.preview.truncated")}` : ""}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        {formattable && (
          <button
            type="button"
            className="button-ghost min-h-8 px-2 text-[10px] uppercase tracking-[0.14em]"
            onClick={() => setRaw((v) => !v)}
          >
            {raw ? t("artifacts.preview.formatted") : t("artifacts.preview.raw")}
          </button>
        )}
        <a
          className="button-secondary button-compact"
          href={apiURL(
            `/api/runs/${encodeURIComponent(name)}/artifacts/${path
              .split("/")
              .map(encodeURIComponent)
              .join("/")}`
          )}
          download={path.split("/").pop()}
        >
          <Download className="h-3.5 w-3.5" />
          {t("artifacts.download.file")}
        </a>
      </span>
    </div>
  );

  if (ext === "md" && !raw) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="prose-report">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
          </div>
        </div>
      </div>
    );
  }

  if (ext === "csv" && !raw) {
    const rows = parseCsv(text);
    const head = rows[0] ?? [];
    const body = rows.slice(1, CSV_ROW_CAP + 1);
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        {header}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {rows.length === 0 ? (
            <p className="font-mono text-[11px] text-fg-muted">
              {t("artifacts.preview.emptyFile")}
            </p>
          ) : (
            <table className="w-full border-collapse text-left font-mono text-[11px]">
              <thead>
                <tr>
                  {head.map((cell, i) => (
                    <th
                      key={i}
                      className="whitespace-nowrap border-b border-line/10 px-2.5 py-1.5 text-[10px] uppercase tracking-[0.12em] text-fg-muted"
                    >
                      {cell}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {body.map((row, r) => (
                  <tr key={r} className="odd:bg-raised[0.02]">
                    {row.map((cell, c) => (
                      <td
                        key={c}
                        className="max-w-[24rem] truncate border-b border-line/5 px-2.5 py-1.5 text-fg-2"
                        title={cell}
                      >
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {rows.length - 1 > CSV_ROW_CAP && (
            <p className="pt-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-faint">
              {t("artifacts.preview.csvCapped", {
                shown: CSV_ROW_CAP,
                total: rows.length - 1,
              })}
            </p>
          )}
        </div>
      </div>
    );
  }

  const shown =
    ext === "json" || ext === "jsonl" ? prettyStructured(text, path) : text;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {header}
      <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-[11px] leading-snug text-fg-2">
        {shown}
      </pre>
    </div>
  );
}

/* -------------------------------------------------------------------- panel */

export default function ArtifactsBrowser({
  name,
  live = false,
  className,
}: {
  name: string;
  live?: boolean;
  className?: string;
}) {
  const { t } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = React.useState("");
  const [files, setFiles] = React.useState<ArtifactFile[]>([]);
  const [query, setQuery] = React.useState("");
  const [selected, setSelected] = React.useState<ArtifactFile | null>(null);
  const [preview, setPreview] = React.useState<{ file: ArtifactFile; text: string; truncated: boolean } | null>(null);
  const [loadingFile, setLoadingFile] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const page = await getJSON<ArtifactsIndexPage>(
        `/api/runs/${encodeURIComponent(name)}/artifacts`
      );
      setFiles(page.files);
      setPhase("ready");
    } catch (e) {
      setPhase((prev) => (prev === "ready" ? prev : "error"));
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [name]);

  React.useEffect(() => {
    void load();
    if (!live) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, INDEX_POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, live]);

  /* preview fetch — selection driven, safe to re-run */
  React.useEffect(() => {
    if (!selected) {
      setPreview(null);
      return;
    }
    let disposed = false;
    setLoadingFile(true);
    const url = apiURL(
      `/api/runs/${encodeURIComponent(name)}/artifacts/${selected.path.split("/").map(encodeURIComponent).join("/")}`
    );
    fetch(url)
      .then(async (res) => {
        if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
        return res.text();
      })
      .then((text) => {
        if (disposed) return;
        setPreview({
          file: selected,
          text: text.length > PREVIEW_CHAR_CAP ? text.slice(0, PREVIEW_CHAR_CAP) : text,
          truncated: text.length > PREVIEW_CHAR_CAP,
        });
      })
      .catch(() => {
        if (!disposed) setPreview(null);
      })
      .finally(() => {
        if (!disposed) setLoadingFile(false);
      });
    return () => {
      disposed = true;
    };
  }, [selected, name]);

  /* groups: root files first, then one bucket per directory (by depth) */
  const q = query.trim().toLowerCase();
  const filtered = React.useMemo(
    () => (q ? files.filter((f) => f.path.toLowerCase().includes(q)) : files),
    [files, q]
  );
  const groups = React.useMemo(() => {
    const root: ArtifactFile[] = [];
    const dirs = new Map<string, ArtifactFile[]>();
    for (const file of filtered) {
      const slash = file.path.lastIndexOf("/");
      if (slash === -1) {
        root.push(file);
        continue;
      }
      const dir = file.path.slice(0, slash);
      const list = dirs.get(dir) || [];
      list.push(file);
      dirs.set(dir, list);
    }
    return { root, dirs: [...dirs.entries()].sort((a, b) => a[0].localeCompare(b[0])) };
  }, [filtered]);

  if (phase === "loading") {
    return (
      <div className="space-y-2 p-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="skeleton-line w-2/3" />
        ))}
        <p className="pt-1 font-mono text-[10px] uppercase tracking-[0.18em] text-fg-muted">
          {t("artifacts.loading")}
        </p>
      </div>
    );
  }

  if (phase === "error") {
    return (
      <div className="space-y-3 p-4">
        <div className="alert-error" role="alert">
          {t("artifacts.error", { error })}
        </div>
        <button
          type="button"
          className="button-secondary button-compact"
          onClick={() => {
            setPhase("loading");
            void load();
          }}
        >
          <RefreshCw className="h-3.5 w-3.5" />
          {t("common.retry")}
        </button>
      </div>
    );
  }

  if (files.length === 0) {
    return (
      <div className="p-4">
        <EmptyState
          title={live ? t("artifacts.empty.live.title") : t("artifacts.empty.done.title")}
          hint={
            live
              ? t("artifacts.empty.live.hint")
              : t("artifacts.empty.done.hint")
          }
        />
      </div>
    );
  }

  return (
    <div className={cn("flex min-h-0 flex-col gap-3 p-4 md:flex-row", className)}>
      {/* tree */}
      <div className="flex min-h-0 flex-col gap-2.5 md:w-72 md:flex-shrink-0">
        <div className="flex items-center gap-2">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-fg-muted" />
            <input
              className="input-shell min-h-9 py-1.5 pl-8 text-[11px]"
              placeholder={t("artifacts.search")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label={t("artifacts.search")}
              spellCheck={false}
            />
          </div>
          <a
            className="button-secondary button-compact"
            href={archiveURL(name)}
            download={`${name}.zip`}
            title={t("artifacts.download.archive.hint")}
          >
            <Package className="h-3.5 w-3.5" />
            {t("artifacts.download.archive")}
          </a>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-0.5">
          <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-fg-muted">
            {t("artifacts.files", { n: filtered.length })}
          </p>
          {groups.root.length > 0 && (
            <div className="space-y-1">
              {groups.root.map((file) => (
                <FileRow
                  key={file.path}
                  file={file}
                  active={selected?.path === file.path}
                  onSelect={() => setSelected(file)}
                />
              ))}
            </div>
          )}
          {groups.dirs.map(([dir, list]) => (
            <div key={dir} className="space-y-1">
              <div className="flex items-center gap-2 px-1 pt-1">
                <span className="mono-chip py-0.5 text-[9px]">{dir}/</span>
                <span className="font-mono text-[9px] text-fg-faint">{list.length}</span>
              </div>
              {list.map((file) => (
                <FileRow
                  key={file.path}
                  file={file}
                  active={selected?.path === file.path}
                  onSelect={() => setSelected(file)}
                />
              ))}
            </div>
          ))}
          {filtered.length === 0 && (
            <p className="px-1 text-[11px] text-fg-muted">
              {t("artifacts.search.empty", { query: query.trim() })}
            </p>
          )}
        </div>
      </div>

      {/* preview */}
      <div className="panel panel-hairline flex min-h-[18rem] flex-1 flex-col overflow-hidden bg-surface-deep/40">
        {!selected ? (
          <EmptyState
            title={t("artifacts.select.title")}
            hint={t("artifacts.select.hint")}
          />
        ) : loadingFile && !preview ? (
          <div className="flex flex-1 items-center justify-center gap-2 p-6 font-mono text-[10px] uppercase tracking-[0.16em] text-fg-muted">
            <Spinner className="h-3.5 w-3.5" />
            {t("artifacts.preview.loading", { path: selected.path })}
          </div>
        ) : preview && preview.file.path === selected.path ? (
          <PreviewFrame
            name={name}
            path={preview.file.path}
            text={preview.text}
            truncated={preview.truncated}
          />
        ) : (
          <EmptyState
            title={t("artifacts.preview.unavailable.title")}
            hint={t("artifacts.preview.unavailable.hint", { path: selected.path })}
          />
        )}
      </div>
    </div>
  );
}
