"use client";

import * as React from "react";
import { ChevronLeft, ChevronRight, Download, KeyRound, RefreshCw } from "lucide-react";
import { Spinner } from "@/components/ui";
import { apiURL } from "@/lib/api";
import { authFetch } from "@/lib/auth";
import { CREDENTIAL_STATUSES, filterCredentials, parseCredentialsPage } from "@/lib/credentials";
import type { Credential, CredentialStatus, CredentialsPage } from "@/lib/credentials";
import { useI18n } from "@/lib/i18n";
import styles from "./CredentialsPanel.module.css";

const PAGE_SIZE = 25;
type Resource = { name: string; data: CredentialsPage | null; loading: boolean; error: boolean };

function useCredentials(name: string, live: boolean) {
  const [state, setState] = React.useState<Resource>({ name, data: null, loading: true, error: false });
  const [revision, setRevision] = React.useState(0);
  React.useEffect(() => {
    let disposed = false;
    let resume = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    const load = async () => {
      if (disposed || document.hidden || controller) return;
      const request = new AbortController();
      controller = request;
      let timedOut = false;
      const deadline = window.setTimeout(() => { timedOut = true; request.abort(); }, 15_000);
      setState(previous => ({ name, data: previous.name === name ? previous.data : null, loading: true, error: false }));
      try {
        const response = await authFetch(apiURL(`/api/runs/${encodeURIComponent(name)}/credentials`), { signal: request.signal });
        if (!response.ok) throw new Error("credentials_unavailable");
        const data = parseCredentialsPage(await response.json());
        if (!disposed && !request.signal.aborted) setState({ name, data, loading: false, error: false });
      } catch {
        if (!disposed && !document.hidden && (!request.signal.aborted || timedOut)) {
          setState(previous => ({ name, data: previous.name === name ? previous.data : null, loading: false, error: true }));
        }
      } finally {
        window.clearTimeout(deadline);
        controller = null;
        if (!disposed && !document.hidden) {
          if (resume) { resume = false; void load(); }
          else if (live) timer = window.setTimeout(() => void load(), 10_000);
        }
      }
    };
    const visibility = () => {
      window.clearTimeout(timer);
      if (document.hidden) { resume = false; controller?.abort(); }
      else if (controller) resume = true;
      else void load();
    };
    void load();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      controller?.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [name, live, revision]);
  const current = state.name === name ? state : { name, data: null, loading: true, error: false };
  return { ...current, refresh: () => setRevision(value => value + 1) };
}

function statusLabel(status: CredentialStatus, en: boolean) {
  const labels = {
    unverified: ["未验证", "Unverified"], validated: ["验证通过", "Validated"],
    failed: ["验证失败", "Validation failed"], unknown: ["状态未知", "Unknown status"],
  };
  return labels[status][en ? 1 : 0];
}

function sourceLabel(kind: string, en: boolean) {
  const labels: Record<string, [string, string]> = {
    vulnerability: ["漏洞", "Vulnerability"], finding: ["内部发现", "Finding"],
    note: ["共享笔记", "Shared note"], coverage: ["测试覆盖", "Test coverage"],
    threat_model: ["威胁模型", "Threat model"], root: ["Root 结论", "Root conclusion"],
    campaign: ["扫描记录", "Campaign record"], credential_csv: ["凭据 CSV", "Credential CSV"],
  };
  return labels[kind]?.[en ? 1 : 0] ?? kind;
}

function secretLabel(kind: string, en: boolean) {
  const labels: Record<string, [string, string]> = {
    password: ["密码", "Password"], hash: ["哈希", "Hash"], api_key: ["API 密钥", "API key"],
    token: ["令牌", "Token"], secret: ["密钥", "Secret"], private_key: ["私钥", "Private key"],
    encryption_key: ["加密密钥", "Encryption key"], username: ["账号", "Account"],
  };
  return labels[kind]?.[en ? 1 : 0] ?? kind;
}

function warningLabel(code: string, en: boolean) {
  const labels: Record<string, [string, string]> = {
    run_unreadable: ["任务记录无法读取", "Run record could not be read"],
    vulnerabilities_unreadable: ["部分漏洞记录无法读取", "Some vulnerability records could not be read"],
    findings_unreadable: ["部分内部发现无法读取", "Some findings could not be read"],
    notes_unreadable: ["共享笔记无法完整读取", "Shared notes could not be fully read"],
    assessment_unreadable: ["测试覆盖或威胁模型无法完整读取", "Coverage or threat models could not be fully read"],
    credential_csv_unreadable: ["部分凭据 CSV 无法读取", "Some credential CSV files could not be read"],
    credential_csv_limit: ["凭据 CSV 数量或大小超出读取上限", "The credential CSV count or size exceeded the reading limit"],
    credential_records_unparsed: ["部分凭据发现的格式无法解析，请查看原始记录", "Some credential findings could not be parsed. Check the original records"],
    source_limit: ["来源数量或大小超出读取上限", "The source count or size exceeded the reading limit"],
  };
  return labels[code]?.[en ? 1 : 0] ?? (en ? "Some scan records could not be read" : "部分扫描记录无法读取");
}

function CredentialRow({ row, en }: { row: Credential; en: boolean }) {
  const severities: Record<string, [string, string]> = {
    critical: ["严重", "Critical"], high: ["高危", "High"], medium: ["中危", "Medium"],
    low: ["低危", "Low"], info: ["信息", "Info"], informational: ["信息", "Info"],
  };
  return (
    <tr>
      <td>
        <div className={styles.host}>{row.host || (en ? "Host not recorded" : "未记录主机")}</div>
        <div className={styles.account}>{row.username || "—"}</div>
      </td>
      <td>
        {(row.password || row.secret_type === "password") && <div className={styles.secret}><span>{secretLabel(row.secret_type || "password", en)}</span>{row.password ? <pre tabIndex={0} aria-label={secretLabel(row.secret_type || "password", en)}>{row.password}</pre> : <p className={styles.muted}>{en ? "Empty password" : "空密码"}</p>}</div>}
        {row.hash && <div className={styles.secret}><span>{en ? "Hash" : "哈希"}</span><pre tabIndex={0} aria-label={en ? "Hash" : "哈希"}>{row.hash}</pre></div>}
        {!row.password && !row.hash && row.secret_type !== "password" && <span className={styles.muted}>{en ? "No secret recorded" : "未记录密码或密钥"}</span>}
      </td>
      <td>
        <span className={styles.status} data-status={row.validation_status}>{statusLabel(row.validation_status, en)}</span>
        {row.severity && <div className={styles.severity}>{severities[row.severity.toLowerCase()]?.[en ? 1 : 0] ?? row.severity}</div>}
      </td>
      <td>
        {row.source && <p className={styles.sourceText}>{row.source}</p>}
        {row.sources.length > 0 && <ul className={styles.provenance} aria-label={en ? "Source records" : "来源记录"}>{row.sources.map((source, index) => (
          <li key={`${source.kind}:${source.id}:${index}`}><span>{sourceLabel(source.kind, en)}</span><strong>{source.title || source.id || "—"}</strong>{source.id && source.title && <code>{source.id}</code>}</li>
        ))}</ul>}
        {row.note && <details className={styles.note}><summary>{en ? "Notes" : "备注"}</summary><p>{row.note}</p></details>}
        {!row.source && row.sources.length === 0 && !row.note && <span className={styles.muted}>—</span>}
      </td>
    </tr>
  );
}

export default function CredentialsPanel({ name, live }: { name: string; live: boolean }) {
  const { locale } = useI18n();
  const en = locale === "en";
  const result = useCredentials(name, live);
  const [search, setSearch] = React.useState("");
  const [status, setStatus] = React.useState<CredentialStatus | "">("");
  const [page, setPage] = React.useState(0);
  const [downloadState, setDownloadState] = React.useState<"idle" | "downloading" | "failed" | "partial">("idle");
  const downloadRequest = React.useRef<AbortController | null>(null);
  React.useEffect(() => {
    setSearch(""); setStatus(""); setPage(0); setDownloadState("idle");
    return () => { downloadRequest.current?.abort(); downloadRequest.current = null; };
  }, [name]);
  const rows = result.data?.credentials ?? [];
  const filtered = React.useMemo(() => filterCredentials(rows, search, status), [rows, search, status]);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1));
  const offset = currentPage * PAGE_SIZE;
  const visibleRows = filtered.slice(offset, offset + PAGE_SIZE);
  const unreadable = result.data?.source_status === "unreadable";
  const partial = result.data?.source_status === "partial";
  const canDownload = rows.length > 0 && !unreadable && !result.error && downloadState !== "downloading";
  const download = async () => {
    if (!canDownload || downloadRequest.current) return;
    const controller = new AbortController();
    downloadRequest.current = controller;
    setDownloadState("downloading");
    const deadline = window.setTimeout(() => controller.abort(), 30_000);
    let objectURL: string | null = null;
    try {
      const response = await authFetch(apiURL(`/api/runs/${encodeURIComponent(name)}/credentials.csv`), { signal: controller.signal });
      if (!response.ok) throw new Error("credentials_download_failed");
      const blob = await response.blob();
      if (downloadRequest.current !== controller) return;
      if (controller.signal.aborted) throw new Error("credentials_download_timed_out");
      objectURL = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectURL;
      link.download = `${name}-credentials.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setDownloadState(response.headers.get("X-Credential-Source-Status") === "partial" ? "partial" : "idle");
    } catch {
      if (downloadRequest.current === controller) setDownloadState("failed");
    } finally {
      window.clearTimeout(deadline);
      if (objectURL) URL.revokeObjectURL(objectURL);
      if (downloadRequest.current === controller) downloadRequest.current = null;
    }
  };

  return (
    <section className={styles.panel} aria-label={en ? "Credentials" : "凭据"}>
      <header className={styles.header}>
        <div><h2><KeyRound size={16} aria-hidden />{en ? "Credentials" : "凭据"}</h2><p>{en ? "Accounts, passwords, keys, and hashes collected from scan records. Validation status follows the original records." : "汇总扫描记录中的账号、密码、密钥与哈希，验证状态以原始记录为准。"}</p></div>
        <button type="button" className={styles.download} disabled={!canDownload} onClick={() => void download()}><Download size={14} aria-hidden />{downloadState === "downloading" ? (en ? "Downloading…" : "下载中…") : (en ? "Download all CSV" : "下载全部 CSV")}</button>
      </header>
      <div className={styles.filters}>
        <label><span>{en ? "Search credentials" : "搜索凭据"}</span><input className="input-shell" value={search} placeholder={en ? "Host, account, secret, or source" : "主机、账号、密钥或来源"} onChange={event => { setSearch(event.target.value); setPage(0); }} /></label>
        <label><span>{en ? "Validation status" : "验证状态"}</span><select className="input-shell" value={status} onChange={event => { setStatus(event.target.value as CredentialStatus | ""); setPage(0); }}><option value="">{en ? "All statuses" : "所有状态"}</option>{CREDENTIAL_STATUSES.map(value => <option key={value} value={value}>{statusLabel(value, en)}</option>)}</select></label>
      </div>
      <div className={styles.resultsBar}><span>{result.data ? `${filtered.length} / ${rows.length} ${en ? "credentials" : "条凭据"}` : "—"}{partial && ` · ${en ? "Partial inventory" : "部分汇总"}`}</span><button type="button" onClick={result.refresh} disabled={result.loading}><RefreshCw size={13} aria-hidden />{result.loading ? (en ? "Refreshing…" : "刷新中…") : (en ? "Refresh" : "刷新")}</button></div>
      {result.error && <p className={styles.warning} role="status">{result.data ? (en ? "Refresh failed. Showing the previously fetched inventory. Please retry." : "刷新失败，当前显示上次读取的凭据，请重试。") : (en ? "Credential records could not be read. Please refresh to retry." : "无法读取凭据记录，请刷新重试。")}</p>}
      {(partial || unreadable) && <div className={styles.warning} role="status"><p>{unreadable ? (en ? "Scan records could not be read. Credential discovery is unknown." : "扫描记录无法读取，无法确认是否发现凭据。") : (en ? "Some scan records could not be fully read. This inventory and its CSV may be incomplete." : "部分扫描记录无法完整读取，当前凭据列表及 CSV 可能不完整。")}</p>{result.data!.warnings.length > 0 && <ul>{Array.from(new Set(result.data!.warnings.map(code => warningLabel(code, en)))).map(message => <li key={message}>{message}</li>)}</ul>}</div>}
      {downloadState === "failed" && <p className={styles.warning} role="status">{en ? "CSV download failed. Please retry." : "CSV 下载失败，请重试。"}</p>}
      {downloadState === "partial" && <p className={styles.warning} role="status">{en ? "CSV downloaded. Some credential sources could not be fully collected; the export may be incomplete." : "CSV 已下载，但部分凭据来源无法完整汇总，导出内容可能有遗漏。"}</p>}
      {visibleRows.length > 0 ? <>
        <div className={styles.tableScroll} tabIndex={0} role="region" aria-label={en ? "Credential inventory" : "凭据列表"} aria-busy={result.loading}>
          <table><thead><tr><th scope="col">{en ? "Host / account" : "主机 / 账号"}</th><th scope="col">{en ? "Password / key / hash" : "密码 / 密钥 / 哈希"}</th><th scope="col">{en ? "Validation" : "验证"}</th><th scope="col">{en ? "Source / notes" : "来源 / 备注"}</th></tr></thead><tbody>{visibleRows.map(row => <CredentialRow key={row.id} row={row} en={en} />)}</tbody></table>
        </div>
        <nav className={styles.pagination} aria-label={en ? "Credential pages" : "凭据分页"}><span>{offset + 1}–{offset + visibleRows.length} / {filtered.length}</span><div><button type="button" disabled={currentPage === 0} aria-label={en ? "Previous credentials page" : "上一页凭据"} onClick={() => setPage(currentPage - 1)}><ChevronLeft size={16} /></button><button type="button" disabled={offset + PAGE_SIZE >= filtered.length} aria-label={en ? "Next credentials page" : "下一页凭据"} onClick={() => setPage(currentPage + 1)}><ChevronRight size={16} /></button></div></nav>
      </> : !result.error && !unreadable && <div className={styles.empty}>
        {result.loading ? <><Spinner /><span>{en ? "Loading credentials…" : "加载凭据…"}</span></> : <><KeyRound size={24} aria-hidden /><strong>{rows.length > 0 ? (en ? "No matching credentials" : "没有符合条件的凭据") : partial ? (en ? "No credentials in the readable records" : "当前可读取的记录中没有凭据") : result.data?.source_status === "missing" ? (en ? "No scan records to collect from yet" : "尚无可汇总的扫描记录") : (en ? "No credentials recorded" : "暂无凭据记录")}</strong><p>{rows.length > 0 ? (en ? "Adjust your search or status filter." : "调整搜索或验证状态筛选。") : (en ? "Credentials saved in scan findings and notes will appear here." : "扫描发现与笔记中记录的凭据会显示在这里。")}</p></>}
      </div>}
    </section>
  );
}
