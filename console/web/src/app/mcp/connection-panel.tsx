"use client";

import * as React from "react";
import { Check, Copy, Eye, EyeOff, KeyRound, LoaderCircle } from "lucide-react";
import { mcpApi, mcpError, type McpConnection, type McpTask } from "@/lib/mcp-api";
import { SharedCaPanel } from "./ca-panel";
import { McpError, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(value); return; } catch { /* HTTP and browser permissions can require the fallback below. */ }
  }
  const previousFocus = document.activeElement;
  const input = document.createElement("textarea");
  input.value = value; input.readOnly = true;
  input.style.position = "fixed"; input.style.left = "-9999px";
  document.body.append(input);
  try {
    input.select();
    if (!document.execCommand("copy")) throw new Error("clipboard-unavailable");
  } finally {
    input.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus();
  }
}

export function ConnectionPanel({ task }: { task: McpTask }) {
  const c = useMcpCopy();
  const session = task.session;
  const host = session?.proxy_host || "127.0.0.1";
  const port = session?.proxy_port;
  const bindHost = (session?.proxy_bind_host || host).replace(/^\[|\]$/g, "").toLowerCase();
  const loopback = bindHost === "localhost" || bindHost === "::1" || /^127\./.test(bindHost);
  const displayHost = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  const tunnelHost = bindHost.includes(":") ? `[${bindHost}]` : bindHost;
  return <section className={styles.connectionPanel}><div><h3>{c("瀏覽器代理", "Browser proxy")}</h3>
    <p>{c("啟動時會自動依目前的 Console 網址準備代理連線。將下列位址填入瀏覽器的 HTTP 與 HTTPS 代理設定，瀏覽目標網站後，流量就會出現在這個任務。", "Starting the proxy prepares its connection using your current Console address. Enter the address below in your browser's HTTP and HTTPS proxy settings, then browse your target to capture traffic in this task.")}</p>
    <code className={styles.proxyAddress}>{port ? `${displayHost}:${port}` : c("啟動後顯示位址", "Address available after start")}</code>
    {port && loopback && <><small>{c("此代理只監聽主機本機位址。若瀏覽器在另一台電腦，請先建立 SSH 通道，再使用 127.0.0.1 與上述連接埠。", "This proxy listens on the server's loopback address. From another computer, create an SSH tunnel and use 127.0.0.1 with the port above.")}</small><code className={styles.tunnelCommand}>{`ssh -N -L ${port}:${tunnelHost}:${port} user@server`}</code></>}
    {port && !loopback && <small>{c("此代理可從其他電腦連入。主機防火牆需允許測試電腦存取上述連接埠。", "This proxy accepts connections from other computers. The host firewall must allow your testing computer to reach this port.")}</small>}
    {session?.proxy_auth_required && <ProxyCredentials key={session.id} task={task} />}
  </div><div><SharedCaPanel task={task} /></div></section>;
}

function ProxyCredentials({ task }: { task: McpTask }) {
  const c = useMcpCopy();
  const session = task.session!;
  const [credentials, setCredentials] = React.useState<McpConnection["credentials"] | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [copied, setCopied] = React.useState<"username" | "password" | null>(null);
  const request = React.useRef<AbortController | null>(null);
  React.useEffect(() => () => request.current?.abort(), []);
  const generated = session.proxy_auth_source === "generated" || session.credentials_available;
  const reveal = async () => {
    if (loading) return;
    const controller = new AbortController(); request.current?.abort(); request.current = controller;
    setLoading(true); setError(""); setCopied(null);
    try {
      const result = await mcpApi.connection(task.id, controller.signal);
      if (controller.signal.aborted) return;
      if (result.session_id !== session.id) throw new Error(c("代理工作階段已變更，請重新開啟連線資訊。", "The proxy session changed. Reopen its connection details."));
      if (!result.credentials.available || !result.credentials.username || !result.credentials.password) throw new Error(c("目前無法取得代理帳密，請重新開啟連線資訊。", "Proxy credentials are unavailable. Reopen the connection details."));
      setCredentials(result.credentials);
    } catch (e) { if (!controller.signal.aborted) setError(mcpError(e)); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  };
  const hide = () => { request.current?.abort(); setCredentials(null); setCopied(null); setError(""); setLoading(false); };
  const copy = async (field: "username" | "password") => {
    const value = credentials?.[field];
    if (!value) return;
    try { await copyText(value); setCopied(field); setError(""); }
    catch { setError(c("瀏覽器無法自動複製，請選取欄位內容手動複製。", "Automatic copying is unavailable. Select the field and copy it manually.")); }
  };
  return <div className={styles.proxyCredentials}>
    <h3><KeyRound size={14} />{c("代理帳號與密碼", "Proxy credentials")}</h3>
    {generated ? <>
      <p>{c("這次監聽的帳密已自動產生並保存。瀏覽器要求代理驗證時，填入這組帳密；重新啟動代理會產生新的一組。", "Credentials are generated and saved for this capture. Enter them when your browser requests proxy authentication. Restarting the proxy creates a new pair.")}</p>
      <dl className={styles.credentialFields}>{(["username", "password"] as const).map(field => <div key={field}><dt>{field === "username" ? c("帳號", "Username") : c("密碼", "Password")}</dt><dd><code aria-label={!credentials ? c("已隱藏", "Hidden") : undefined}>{credentials?.[field] || "••••••••••••"}</code>{credentials && <button type="button" className={styles.iconButton} aria-label={field === "username" ? c("複製代理帳號", "Copy proxy username") : c("複製代理密碼", "Copy proxy password")} onClick={() => void copy(field)}>{copied === field ? <Check size={14} /> : <Copy size={14} />}</button>}</dd></div>)}</dl>
      <button type="button" className={styles.secondaryButton} disabled={loading} onClick={credentials ? hide : () => void reveal()}>{loading ? <LoaderCircle size={14} className={styles.spin} /> : credentials ? <EyeOff size={14} /> : <Eye size={14} />}{loading ? c("讀取中…", "Loading…") : credentials ? c("隱藏帳密", "Hide credentials") : c("顯示帳密", "Show credentials")}</button>
      <span className={styles.credentialCopyStatus} role="status">{copied ? (copied === "username" ? c("帳號已複製", "Username copied") : c("密碼已複製", "Password copied")) : ""}</span>
      {error && <McpError onRetry={!credentials ? () => void reveal() : undefined}>{error}</McpError>}
    </> : <p>{c("這台主機已使用自訂代理帳密。瀏覽器要求驗證時，請填入管理者提供的帳號與密碼。", "This host uses custom proxy credentials. Enter the username and password provided by your administrator when the browser requests authentication.")}</p>}
  </div>;
}
