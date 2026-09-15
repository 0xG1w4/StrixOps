"use client";

import { authFetch } from "@/lib/auth";


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
        const response = await authFetch(apiURL(`/api/runs/${encodeURIComponent(name)}/${kind}`), { signal: request.signal, cache: "no-store" });
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
    { key: "evidence", label: en ? "Evidence" : "证据", count: evidenceCount },
    { key: "artifacts", label: en ? "Other files" : "其他文件", count: files.length - evidenceCount },
  ];
  const choose = (file: RunFile) => {
    setSelectedPath(file.path);
    if (window.innerWidth < 760) requestAnimationFrame(() => previewRef.current?.scrollIntoView({ block: "start" }));
  };

  return (
    <div className={styles.panel} data-testid="files-panel">
      <div className={styles.toolbar}>
        <div className={styles.filterGroup} role="group" aria-label={en ? "File sources" : "文件来源"}>
          {tabs.map(tab => <button key={tab.key} type="button" aria-pressed={filter === tab.key}
            onClick={() => setFilter(tab.key)}>{tab.label}<span>{hasIndex ? tab.count : "—"}</span></button>)}
        </div>
        <div className={styles.actions}>
          <button className="button-ghost button-compact" type="button" onClick={() => setRefresh(value => value + 1)}
            aria-label={en ? "Refresh files" : "刷新文件"}><RefreshCw size={14} />{en ? "Refresh" : "刷新"}</button>
          <a className="button-secondary button-compact" href={archiveURL(name)} download={`${name}.zip`}
            title={en ? "Download the available run files as ZIP" : "将任务中可交付的文件下载为 ZIP"}>
            <Package size={14} />{en ? "Download ZIP" : "下载 ZIP"}</a>
        </div>
      </div>
      {failures.length > 0 && <div className={styles.error} role="alert">
        <AlertTriangle size={16} />
        <span>{en ? "Could not refresh " : "无法刷新"}{failures.map(({ kind }) => kind === "evidence" ? en ? "evidence metadata" : "证据元数据" : en ? "file index" : "文件索引").join(en ? " and " : "与")}{en ? ". " : "。"}
          {failures.some(failure => failure.timeout) && (en ? "A request timed out. " : "请求超时。")}
          {en ? "The list may be incomplete or show the last available records." : "列表可能不完整，或显示上次获取的记录。"}</span>
        <button className="button-secondary button-compact" type="button" onClick={() => setRefresh(value => value + 1)}>{en ? "Retry" : "重试"}</button>
      </div>}
      {undelivered > 0 && <p className={styles.deliverySummary}><AlertTriangle size={13} />
        {en ? `${undelivered} evidence item(s) have metadata only; select one to see why.` : `${undelivered} 项证据仅有元数据，选择后可查看未交付原因。`}</p>}
      <div className={styles.workspace}>
        <aside className={styles.sidebar} aria-label={en ? "Run files" : "任务文件"}>
          <label className={styles.search}><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)}
            placeholder={en ? "Search path, category or SHA256" : "搜索路径、类别或 SHA256"}
            aria-label={en ? "Search files" : "搜索文件"} spellCheck={false} /></label>
          <div className={styles.count} aria-live="polite">{loading ? en ? "Loading files…" : "正在加载文件…"
            : !hasIndex ? en ? "File count unavailable" : "无法获取文件数量" : en ? `${filtered.length} files` : `${filtered.length} 个文件`}</div>
          <div className={styles.tree}>
            {loading && <div className={styles.empty}><Spinner /><span>{en ? "Loading file indexes" : "加载文件索引"}</span></div>}
            {!loading && !hasIndex && <div className={styles.empty}><AlertTriangle size={24} />
              <strong>{en ? "File indexes unavailable" : "无法获取文件索引"}</strong>
              <small>{en ? "Retry to load the file and evidence indexes." : "请重试以加载文件与证据索引。"}</small></div>}
            {!loading && hasIndex && filtered.length === 0 && <div className={styles.empty}>
              <Folder size={24} /><span>{query || filter !== "all" ? en ? "No matching files" : "没有符合条件的文件" : en ? "No files available yet" : "尚无可用文件"}</span>
              <small>{en ? "Files appear here as the task saves them." : "任务保存文件后会显示于此。"}</small>
            </div>}
            <TreeRows key={`${filter}:${query}`} tree={tree} selected={selectedPath} choose={choose} en={en} />
          </div>
        </aside>
        <section className={styles.preview} ref={previewRef} aria-label={en ? "File preview" : "文件预览"}>
          {selected ? <SelectedFile key={selected.path} file={selected} files={files} visible={visible} en={en} />
            : <div className={styles.empty}><FileText size={32} /><strong>{en ? "Select a file" : "选择文件"}</strong>
              <span>{selectedPath ? en ? "This file is no longer in the available index." : "此文件已不在目前索引中。" : en ? "Evidence and other outputs share one preview." : "证据与其他产物共用同一个预览区。"}</span></div>}
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
    authFetch(apiURL(file.downloadPath), { signal: controller.signal, cache: "no-store", headers: { Range: `bytes=0-${PREVIEW_BYTES}` } })
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
  const date = (value: string) => Number.isNaN(Date.parse(value)) ? value || "—" : new Date(value).toLocaleString(en ? "en-US" : "zh-CN");

  return <>
    <header className={styles.previewHeader}>
      <div className={styles.identity}><span className={styles.kind}>{file.evidence ? en ? "Evidence" : "证据" : en ? "File" : "文件"}</span>
        <h3>{file.path}</h3><small>{formatBytes(file.size)}{ext ? ` · ${ext.toUpperCase()}` : ""}</small></div>
      <div className={styles.actions}>
        {formattable && preview && !preview.binary && <button className="button-ghost button-compact" type="button" onClick={() => setRaw(value => !value)}>
          {raw ? en ? "Formatted" : "格式化" : en ? "Raw" : "原始内容"}</button>}
        {file.downloadPath ? <a className="button-secondary button-compact" href={apiURL(file.downloadPath)} download={file.path.split("/").pop()}>
          <Download size={14} />{en ? "Download file" : "下载文件"}</a>
          : <button className="button-secondary button-compact" type="button" disabled><Download size={14} />{en ? "Not delivered" : "未交付"}</button>}
      </div>
    </header>
    {metadata && <dl className={styles.metadata}>
      <div><dt>{en ? "Category" : "类别"}</dt><dd>{metadata.categoryLabel || metadata.category}<small>{metadata.categoryLabel ? metadata.category : ""}</small></dd></div>
      <div><dt>{en ? "Collected" : "收集时间"}</dt><dd><time dateTime={metadata.collectedAt}>{date(metadata.collectedAt)}</time></dd></div>
      <div><dt>{en ? "Delivery" : "交付状态"}</dt><dd className={!metadata.deliverable ? styles.warningText : undefined}>{metadata.deliverable ? en ? "Available" : "可下载" : en ? "Metadata only" : "仅元数据"}{metadata.oversize && <small>{en ? "Oversize" : "超出大小限制"}</small>}</dd></div>
      <div className={styles.digest}><dt>SHA256</dt><dd>{metadata.sha256 || (en ? "Not recorded" : "未记录")}</dd></div>
    </dl>}
    {file.evidence && !metadata && <p className={styles.notice}>{en ? "Evidence metadata is unavailable; this file was returned by the file index." : "证据元数据目前不可用；此文件来自文件索引。"}</p>}
    {!file.downloadPath ? <div className={styles.unavailable}><AlertTriangle size={22} />
      <strong>{en ? "This evidence was not delivered" : "这项证据尚未交付"}</strong>
      <p>{metadata?.oversize ? en ? "The captured file exceeded the collection limit. Only its metadata is available." : "采集的文件超出收集大小限制，当前仅保留元数据。" : en ? "The attachment could not be collected or is no longer available. Preview and download are disabled." : "附件未能收集或已无法访问，因此无法预览或下载。"}</p>
      {metadata?.error && <pre>{metadata.error}</pre>}
    </div> : status === "loading" ? <div className={styles.empty}><Spinner /><span>{en ? "Loading preview…" : "正在加载预览…"}</span></div>
      : status === "error" || status === "timeout" ? <div className={styles.empty} role="alert"><AlertTriangle size={24} /><strong>{status === "timeout" ? en ? "Preview request timed out" : "预览请求超时" : en ? "Preview unavailable" : "无法加载预览"}</strong>
        <span>{status === "timeout" ? en ? "Loading exceeded 12 seconds. Retry or download the file." : "加载超过 12 秒，请重试或下载文件。" : en ? "The file may have changed or become unavailable. Retry or download the file." : "文件可能已变更或无法访问，请重试或下载文件。"}</span>
        <button className="button-secondary button-compact" type="button" onClick={() => setRetry(value => value + 1)}>{en ? "Retry preview" : "重试预览"}</button></div>
        : preview?.binary ? <div className={styles.empty}><FileText size={30} /><strong>{en ? "Binary file" : "二进制文件"}</strong><span>{en ? "Download this file to view it in a compatible application." : "请下载后使用兼容的程序打开。"}</span></div>
          : preview && <>
            {preview.truncated && <p className={styles.notice}>{en ? "Preview is limited to the first 400 KB. Download for the complete file." : "预览仅显示前 400 KB，下载可获取完整文件。"}</p>}
            <div className={styles.content}>
              {!preview.text ? <p className={styles.notice}>{en ? "Empty file" : "空文件"}</p>
                : ["md", "markdown"].includes(ext) && !raw ? <div className={`prose-report ${styles.markdown}`}><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
                  urlTransform={value => { const link = fileLink(value, file.path, files); return link.startsWith("/api/") ? apiURL(link) : link; }}
                  components={{
                    a: ({ href, children }) => href ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
                    img: ({ alt }) => <span className={styles.notice}>{en ? "Image not loaded" : "图片未加载"}{alt ? `: ${alt}` : ""}</span>,
                  }}>{preview.text}</ReactMarkdown></div>
                  : ext === "csv" && !raw ? <><div className={styles.tableScroll}><table><thead><tr>{(csv[0] ?? []).slice(0, 200).map((cell, index) => <th key={index}>{cell}</th>)}</tr></thead>
                    <tbody>{csv.slice(1, 401).map((row, index) => <tr key={index}>{row.slice(0, 200).map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div>
                    {(csv.length > 401 || csv.some(row => row.length > 200)) && <p className={styles.notice}>{en ? "Table preview is limited to 400 rows and 200 columns." : "表格预览上限为 400 行、200 列。"}</p>}</>
                    : <pre className={styles.code}>{formatted}</pre>}
            </div>
          </>}
  </>;
}
