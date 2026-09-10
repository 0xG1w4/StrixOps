"use client";

import * as React from "react";
import { Download, ShieldCheck } from "lucide-react";
import { mcpApi, mcpError, type McpCaInfo, type McpTask } from "@/lib/mcp-api";
import { McpError, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

export function SharedCaPanel({ task }: { task?: McpTask }) {
  const c = useMcpCopy();
  const [info, setInfo] = React.useState<McpCaInfo | null>(null);
  const [error, setError] = React.useState("");
  const [revision, setRevision] = React.useState(0);
  React.useEffect(() => {
    const controller = new AbortController();
    mcpApi.sharedCaInfo(controller.signal).then(value => { if (!controller.signal.aborted) { setInfo(value); setError(""); } }).catch(e => { if (!controller.signal.aborted) setError(mcpError(e)); });
    return () => controller.abort();
  }, [revision]);
  const session = task?.session;
  const separateSession = Boolean(session && (session.ca_shared === false || (session.ca_ready && session.ca_shared !== true)));
  return <div className={styles.caPanel}>
    <h3><ShieldCheck size={15} />{c("共用 HTTPS CA", "Shared HTTPS CA")}</h3>
    <p>{c("同一個 StrixOps 的所有新任務共用這張 CA。每個測試瀏覽器只需信任一次；重新啟動或刪除任務不會更換憑證。", "New tasks in this StrixOps instance share one CA. Trust it once per testing browser; restarts and task deletion preserve the certificate.")}</p>
    <a className={styles.secondaryButton} href={mcpApi.sharedCaURL()} download="strixops-mcp-ca.pem"><Download size={14} />{c("下載共用 CA", "Download shared CA")}</a>
    {separateSession && <div className={styles.legacyCaNote}>
      <p>{session?.ca_shared === false ? c("目前代理仍使用舊的工作階段憑證。下次重新啟動此代理後才會改用共用 CA；現在請使用目前代理的 CA。", "This proxy still uses a legacy session certificate. It switches to the shared CA the next time you restart this proxy; use its current CA for now.") : c("目前代理是否使用共用 CA 尚未確認。請先使用此代理的實際 CA，避免憑證不符。", "The current proxy's CA has not been confirmed as shared. Use its actual CA to avoid a certificate mismatch.")}</p>
      {session?.ca_ready ? <a className={styles.secondaryButton} href={mcpApi.caURL(task!.id)} download><Download size={14} />{c("下載目前代理 CA", "Download current proxy CA")}</a> : <span className={styles.muted}>{c("目前代理的 CA 暫時無法取得。", "The current proxy's CA is temporarily unavailable.")}</span>}
    </div>}
    {info && <dl className={styles.caMetadata}><div><dt>SHA-256</dt><dd><code>{info.sha256}</code></dd></div>{info.not_after && <div><dt>{c("有效期限", "Expires")}</dt><dd>{new Date(info.not_after).toLocaleString()}</dd></div>}</dl>}
    {error && <McpError onRetry={() => setRevision(value => value + 1)}>{error}</McpError>}
    <p className={styles.scopeFootnote}>{c("下載內容只有公開憑證，不包含私鑰。", "The download contains only the public certificate, never the private key.")}</p>
  </div>;
}
