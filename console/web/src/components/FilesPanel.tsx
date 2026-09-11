"use client";

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AlertTriangle, ChevronRight, Download, FileText, Folder, Package, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { apiURL, archiveURL } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import {
  buildFileTree, extension, fileLink, filterRunFiles, formatBytes, mergeRunFiles,
  parseFileCsv, PREVIEW_BYTES, readPreview,
  type FileFilter, type FilePreview, type FileTree, type RunFile,
} from "@/lib/files";
import { Spinner } from "@/components/ui";
import styles from "./FilesPanel.module.css";

const REQUEST_TIMEOUT_MS = 12_000;
type IndexKind = "artifacts" | "evidence";

function usePageVisible() {
  const [visible, setVisible] = React.useState(true);
  React.useEffect(() => {
    const update = () => setVisible(!document.hidden);
    update(); document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

export default function FilesPanel({ name, live, initialFilter = "all" }: {
  name: string; live: boolean; initialFilter?: FileFilter;
}) {
  return <FilesWorkspace key={`${name}:${initialFilter}`} name={name} live={live} initialFilter={initialFilter} />;
}

function FilesWorkspace({ name, live, initialFilter }: { name: string; live: boolean; initialFilter: FileFilter }) {
  const { locale } = useI18n();
  const en = locale === "en";
  const visible = usePageVisible();
  const [sources, setSources] = React.useState<{ artifacts: unknown[]; evidence: unknown[] }>({ artifacts: [], evidence: [] });
  const [hasIndex, setHasIndex] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [failures, setFailures] = React.useState<Array<{ kind: IndexKind; timeout: boolean }>>([]);
  const [refresh, setRefresh] = React.useState(0);
  const [filter, setFilter] = React.useState<FileFilter>(initialFilter);
  const [query, setQuery] = React.useState("");
  const [selectedPath, setSelectedPath] = React.useState<string | null>(null);
  const previewRef = React.useRef<HTMLElement>(null);

  React.useEffect(() => {
    if (!visible || document.hidden) return;
    const controller = new AbortController(); let disposed = false; let timer: ReturnType<typeof setTimeout>;
    async function loadIndex(kind: IndexKind): Promise<unknown[]> {
      const request = new AbortController(); let timedOut = false;
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      const deadline = setTimeout(() => { timedOut = true; request.abort(); }, REQUEST_TIMEOUT_MS);
      try {
        const response = await fetch(apiURL(`/api/runs/${encodeURIComponent(name)}/${kind}`), { signal: request.signal, cache: "no-store" });
        if (!response.ok) throw new Error("index_unavailable");
        const page = await response.json();
        const rows = kind === "artifacts" ? page?.files : page?.evidence;
        if (!Array.isArray(rows)) throw new Error("invalid_index");
        return rows;
      } catch (error) {
        if (timedOut) throw new Error("index_timeout");
        throw error;
      } finally {
        clearTimeout(deadline); controller.signal.removeEventListener("abort", abort);
      }
    }
    async function load() {
      const results = await Promise.allSettled([loadIndex("artifacts"), loadIndex("evidence")]);
      if (disposed || controller.signal.aborted) return;
      setSources(previous => ({
        artifacts: results[0].status === "fulfilled" ? results[0].value : previous.artifacts,
        evidence: results[1].status === "fulfilled" ? results[1].value : previous.evidence,
      }));
      if (results.some(result => result.status === "fulfilled")) setHasIndex(true);
      setFailures(results.flatMap((result, index) => result.status === "rejected"
        ? [{ kind: index === 0 ? "artifacts" as const : "evidence" as const, timeout: result.reason instanceof Error && result.reason.message === "index_timeout" }]
        : []));
      setLoading(false);
      if (live) timer = setTimeout(() => { if (!document.hidden) void load(); }, 10_000);
    }
    void load();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [name, live, visible, refresh]);

  const files = React.useMemo(() => mergeRunFiles(name, sources.artifacts, sources.evidence), [name, sources]);
  const filtered = React.useMemo(() => filterRunFiles(files, filter, query), [files, filter, query]);
  const tree = React.useMemo(() => buildFileTree(filtered), [filtered]);
  const selected = files.find(file => file.path === selectedPath) ?? null;
  const evidenceCount = files.filter(file => file.evidence).length;
  const undelivered = files.filter(file => file.metadata && !file.metadata.deliverable).length;
  const tabs: Array<{ key: FileFilter; label: string; count: number }> = [
    { key: "all", label: en ? "All files" : "全部", count: files.length },
    { key: "evidence", label: en ? "Evidence" : "證據", count: evidenceCount },
    { key: "artifacts", label: en ? "Other files" : "其他檔案", count: files.length - evidenceCount },
  ];
  const choose = (file: RunFile) => {
    setSelectedPath(file.path);
    if (window.innerWidth < 760) requestAnimationFrame(() => previewRef.current?.scrollIntoView({ block: "start" }));
  };

  return (
    <div className={styles.panel} data-testid="files-panel">
      <div className={styles.toolbar}>
        <div className={styles.filterGroup} role="group" aria-label={en ? "File sources" : "檔案來源"}>
          {tabs.map(tab => <button key={tab.key} type="button" aria-pressed={filter === tab.key}
            onClick={() => setFilter(tab.key)}>{tab.label}<span>{hasIndex ? tab.count : "—"}</span></button>)}
        </div>
        <div className={styles.actions}>
          <button className="button-ghost button-compact" type="button" onClick={() => setRefresh(value => value + 1)}
            aria-label={en ? "Refresh files" : "重新整理檔案"}><RefreshCw size={14} />{en ? "Refresh" : "重新整理"}</button>
          <a className="button-secondary button-compact" href={archiveURL(name)} download={`${name}.zip`}
            title={en ? "Download the available run files as ZIP" : "下載任務中可交付的檔案 ZIP"}>
            <Package size={14} />{en ? "Download ZIP" : "下載 ZIP"}</a>
        </div>
      </div>
      {failures.length > 0 && <div className={styles.error} role="alert">
        <AlertTriangle size={16} />
        <span>{en ? "Could not refresh " : "無法更新"}{failures.map(({ kind }) => kind === "evidence" ? en ? "evidence metadata" : "證據中繼資料" : en ? "file index" : "檔案索引").join(en ? " and " : "與")}{en ? ". " : "。"}
          {failures.some(failure => failure.timeout) && (en ? "A request timed out. " : "請求逾時。")}
          {en ? "The list may be incomplete or show the last available records." : "列表可能不完整，或顯示上次取得的資料。"}</span>
        <button className="button-secondary button-compact" type="button" onClick={() => setRefresh(value => value + 1)}>{en ? "Retry" : "重試"}</button>
      </div>}
      {undelivered > 0 && <p className={styles.deliverySummary}><AlertTriangle size={13} />
        {en ? `${undelivered} evidence item(s) have metadata only; select one to see why.` : `${undelivered} 筆證據僅有中繼資料，選取後可查看未交付原因。`}</p>}
      <div className={styles.workspace}>
        <aside className={styles.sidebar} aria-label={en ? "Run files" : "任務檔案"}>
          <label className={styles.search}><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)}
            placeholder={en ? "Search path, category or SHA256" : "搜尋路徑、類別或 SHA256"}
            aria-label={en ? "Search files" : "搜尋檔案"} spellCheck={false} /></label>
          <div className={styles.count} aria-live="polite">{loading ? en ? "Loading files…" : "載入檔案中…"
            : !hasIndex ? en ? "File count unavailable" : "無法取得檔案數量" : en ? `${filtered.length} files` : `${filtered.length} 個檔案`}</div>
          <div className={styles.tree}>
            {loading && <div className={styles.empty}><Spinner /><span>{en ? "Loading file indexes" : "載入檔案索引"}</span></div>}
            {!loading && !hasIndex && <div className={styles.empty}><AlertTriangle size={24} />
              <strong>{en ? "File indexes unavailable" : "無法取得檔案索引"}</strong>
              <small>{en ? "Retry to load the file and evidence indexes." : "請重試以載入檔案與證據索引。"}</small></div>}
            {!loading && hasIndex && filtered.length === 0 && <div className={styles.empty}>
              <Folder size={24} /><span>{query || filter !== "all" ? en ? "No matching files" : "沒有符合條件的檔案" : en ? "No files available yet" : "尚無可用檔案"}</span>
              <small>{en ? "Files appear here as the task saves them." : "任務保存檔案後會顯示於此。"}</small>
            </div>}
            <TreeRows key={`${filter}:${query}`} tree={tree} selected={selectedPath} choose={choose} en={en} />
          </div>
        </aside>
        <section className={styles.preview} ref={previewRef} aria-label={en ? "File preview" : "檔案預覽"}>
          {selected ? <SelectedFile key={selected.path} file={selected} files={files} visible={visible} en={en} />
            : <div className={styles.empty}><FileText size={32} /><strong>{en ? "Select a file" : "選取檔案"}</strong>
              <span>{selectedPath ? en ? "This file is no longer in the available index." : "此檔案已不在目前索引中。" : en ? "Evidence and other outputs share one preview." : "證據與其他產物共用同一個預覽區。"}</span></div>}
        </section>
      </div>
    </div>
  );
}

function TreeRows({ tree, selected, choose, en, depth = 0 }: {
  tree: FileTree; selected: string | null; choose: (file: RunFile) => void; en: boolean; depth?: number;
}) {
  return <>
    {tree.files.map(file => <button key={file.path} type="button" className={styles.fileRow} aria-pressed={selected === file.path}
      title={file.path} onClick={() => choose(file)} data-file-path={file.path}>
      {file.evidence ? <ShieldCheck size={14} /> : <FileText size={14} />}
      <span className={styles.fileName}>{file.path.split("/").pop()}</span>
      {!file.downloadPath && <AlertTriangle size={13} className={styles.warningIcon} aria-label={en ? "Not delivered" : "未交付"} />}
      <small>{formatBytes(file.size)}</small>
    </button>)}
    {[...tree.directories].sort(([a], [b]) => a.localeCompare(b)).map(([directory, child]) => <details key={directory} open className={styles.directory}>
      <summary title={directory}><ChevronRight size={12} /><Folder size={13} /><span>{directory}</span></summary>
      <div className={styles.branch} style={{ marginLeft: depth < 5 ? 9 : 0, paddingLeft: depth < 5 ? 5 : 0, borderLeftWidth: depth < 5 ? 1 : 0 }}>
        <TreeRows tree={child} selected={selected} choose={choose} en={en} depth={depth + 1} />
      </div>
    </details>)}
  </>;
}

function SelectedFile({ file, files, visible, en }: { file: RunFile; files: RunFile[]; visible: boolean; en: boolean }) {
  const [raw, setRaw] = React.useState(false);
  const [preview, setPreview] = React.useState<FilePreview | null>(null);
  const [status, setStatus] = React.useState<"loading" | "ready" | "error" | "timeout">("loading");
  const [retry, setRetry] = React.useState(0);
  const ext = extension(file.path);
  const formattable = ["md", "markdown", "csv", "json", "jsonl"].includes(ext);
  const metadata = file.metadata;
  React.useEffect(() => {
    if (!file.downloadPath || !visible || document.hidden) return;
    const controller = new AbortController(); let disposed = false;
    setStatus("loading"); setPreview(null);
    const deadline = setTimeout(() => {
      if (!disposed) { controller.abort(); setStatus("timeout"); }
    }, REQUEST_TIMEOUT_MS);
    fetch(apiURL(file.downloadPath), { signal: controller.signal, cache: "no-store", headers: { Range: `bytes=0-${PREVIEW_BYTES}` } })
      .then(async response => {
        // A byte range cannot be satisfied for an empty file; the server reports its exact size.
        if (response.status === 416 && response.headers.get("content-range")?.trim() === "bytes */0") {
          return { text: "", binary: false, truncated: false };
        }
        if (!response.ok) throw new Error("preview_unavailable");
        return readPreview(response, controller.signal);
      })
      .then(result => { if (!disposed && !controller.signal.aborted) { setPreview(result); setStatus("ready"); } })
      .catch(() => { if (!disposed && !controller.signal.aborted) setStatus("error"); })
      .finally(() => clearTimeout(deadline));
    return () => { disposed = true; clearTimeout(deadline); controller.abort(); };
  }, [file.downloadPath, file.size, file.mtime, metadata?.sha256, visible, retry]);
  const formatted = React.useMemo(() => {
    if (!preview || raw || !["json", "jsonl"].includes(ext)) return preview?.text ?? "";
    try {
      const text = ext === "jsonl" ? preview.text.split("\n").filter(line => line.trim()).map(line => JSON.stringify(JSON.parse(line), null, 2)).join("\n")
        : JSON.stringify(JSON.parse(preview.text), null, 2);
      return text.length <= PREVIEW_BYTES * 3 ? text : preview.text;
    } catch { return preview.text; }
  }, [preview, raw, ext]);
  const csv = React.useMemo(() => preview && ext === "csv" && !raw ? parseFileCsv(preview.text) : [], [preview, ext, raw]);
  const date = (value: string) => Number.isNaN(Date.parse(value)) ? value || "—" : new Date(value).toLocaleString(en ? "en-US" : "zh-TW");

  return <>
    <header className={styles.previewHeader}>
      <div className={styles.identity}><span className={styles.kind}>{file.evidence ? en ? "Evidence" : "證據" : en ? "File" : "檔案"}</span>
        <h3>{file.path}</h3><small>{formatBytes(file.size)}{ext ? ` · ${ext.toUpperCase()}` : ""}</small></div>
      <div className={styles.actions}>
        {formattable && preview && !preview.binary && <button className="button-ghost button-compact" type="button" onClick={() => setRaw(value => !value)}>
          {raw ? en ? "Formatted" : "格式化" : en ? "Raw" : "原始內容"}</button>}
        {file.downloadPath ? <a className="button-secondary button-compact" href={apiURL(file.downloadPath)} download={file.path.split("/").pop()}>
          <Download size={14} />{en ? "Download file" : "下載檔案"}</a>
          : <button className="button-secondary button-compact" type="button" disabled><Download size={14} />{en ? "Not delivered" : "未交付"}</button>}
      </div>
    </header>
    {metadata && <dl className={styles.metadata}>
      <div><dt>{en ? "Category" : "類別"}</dt><dd>{metadata.categoryLabel || metadata.category}<small>{metadata.categoryLabel ? metadata.category : ""}</small></dd></div>
      <div><dt>{en ? "Collected" : "收集時間"}</dt><dd><time dateTime={metadata.collectedAt}>{date(metadata.collectedAt)}</time></dd></div>
      <div><dt>{en ? "Delivery" : "交付狀態"}</dt><dd className={!metadata.deliverable ? styles.warningText : undefined}>{metadata.deliverable ? en ? "Available" : "可下載" : en ? "Metadata only" : "僅中繼資料"}{metadata.oversize && <small>{en ? "Oversize" : "超出大小限制"}</small>}</dd></div>
      <div className={styles.digest}><dt>SHA256</dt><dd>{metadata.sha256 || (en ? "Not recorded" : "未記錄")}</dd></div>
    </dl>}
    {file.evidence && !metadata && <p className={styles.notice}>{en ? "Evidence metadata is unavailable; this file was returned by the file index." : "證據中繼資料目前不可用；此檔案來自檔案索引。"}</p>}
    {!file.downloadPath ? <div className={styles.unavailable}><AlertTriangle size={22} />
      <strong>{en ? "This evidence was not delivered" : "這筆證據尚未交付"}</strong>
      <p>{metadata?.oversize ? en ? "The captured file exceeded the collection limit. Only its metadata is available." : "擷取檔案超出收集大小限制，目前僅保留中繼資料。" : en ? "The attachment could not be collected or is no longer available. Preview and download are disabled." : "附件未能收集或已無法存取，因此無法預覽或下載。"}</p>
      {metadata?.error && <pre>{metadata.error}</pre>}
    </div> : status === "loading" ? <div className={styles.empty}><Spinner /><span>{en ? "Loading preview…" : "載入預覽中…"}</span></div>
      : status === "error" || status === "timeout" ? <div className={styles.empty} role="alert"><AlertTriangle size={24} /><strong>{status === "timeout" ? en ? "Preview request timed out" : "預覽請求逾時" : en ? "Preview unavailable" : "無法載入預覽"}</strong>
        <span>{status === "timeout" ? en ? "Loading exceeded 12 seconds. Retry or download the file." : "載入超過 12 秒，請重試或下載檔案。" : en ? "The file may have changed or become unavailable. Retry or download the file." : "檔案可能已變更或無法存取，請重試或下載檔案。"}</span>
        <button className="button-secondary button-compact" type="button" onClick={() => setRetry(value => value + 1)}>{en ? "Retry preview" : "重試預覽"}</button></div>
        : preview?.binary ? <div className={styles.empty}><FileText size={30} /><strong>{en ? "Binary file" : "二進位檔案"}</strong><span>{en ? "Download this file to view it in a compatible application." : "請下載後使用相容的程式開啟。"}</span></div>
          : preview && <>
            {preview.truncated && <p className={styles.notice}>{en ? "Preview is limited to the first 400 KB. Download for the complete file." : "預覽僅顯示前 400 KB，下載可取得完整檔案。"}</p>}
            <div className={styles.content}>
              {!preview.text ? <p className={styles.notice}>{en ? "Empty file" : "空白檔案"}</p>
                : ["md", "markdown"].includes(ext) && !raw ? <div className={`prose-report ${styles.markdown}`}><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
                  urlTransform={value => { const link = fileLink(value, file.path, files); return link.startsWith("/api/") ? apiURL(link) : link; }}
                  components={{
                    a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
                    img: ({ alt }) => <span className={styles.notice}>{en ? "Image not loaded" : "圖片未載入"}{alt ? `: ${alt}` : ""}</span>,
                  }}>{preview.text}</ReactMarkdown></div>
                  : ext === "csv" && !raw ? <><div className={styles.tableScroll}><table><thead><tr>{(csv[0] ?? []).slice(0, 200).map((cell, index) => <th key={index}>{cell}</th>)}</tr></thead>
                    <tbody>{csv.slice(1, 401).map((row, index) => <tr key={index}>{row.slice(0, 200).map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div>
                    {(csv.length > 401 || csv.some(row => row.length > 200)) && <p className={styles.notice}>{en ? "Table preview is limited to 400 rows and 200 columns." : "表格預覽上限為 400 列、200 欄。"}</p>}</>
                    : <pre className={styles.code}>{formatted}</pre>}
            </div>
          </>}
  </>;
}
