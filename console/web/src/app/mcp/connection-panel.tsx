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
  return <section className={styles.connectionPanel}><div><h3>{c("浏览器代理", "Browser proxy")}</h3>
    <p>{c("启动时会自动根据当前的 Console 网址准备代理连接。将下列地址填入浏览器的 HTTP 与 HTTPS 代理设置，浏览目标网站后，流量就会出现在这个任务。", "Starting the proxy prepares its connection using your current Console address. Enter the address below in your browser's HTTP and HTTPS proxy settings, then browse your target to capture traffic in this task.")}</p>
    <code className={styles.proxyAddress}>{port ? `${displayHost}:${port}` : c("启动后显示地址", "Address available after start")}</code>
    {port && loopback && <><small>{c("此代理只监听主机本机地址。若浏览器在另一台电脑，请先创建 SSH 通道，再使用 127.0.0.1 与上述端口。", "This proxy listens on the server's loopback address. From another computer, create an SSH tunnel and use 127.0.0.1 with the port above.")}</small><code className={styles.tunnelCommand}>{`ssh -N -L ${port}:${tunnelHost}:${port} user@server`}</code></>}
    {port && !loopback && <small>{c("此代理可从其他电脑连入。主机防火墙需允许测试电脑访问上述端口。", "This proxy accepts connections from other computers. The host firewall must allow your testing computer to reach this port.")}</small>}
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
      if (result.session_id !== session.id) throw new Error(c("代理会话已变更，请重新打开连接信息。", "The proxy session changed. Reopen its connection details."));
      if (!result.credentials.available || !result.credentials.username || !result.credentials.password) throw new Error(c("当前无法取得代理账号密码，请重新打开连接信息。", "Proxy credentials are unavailable. Reopen the connection details."));
      setCredentials(result.credentials);
    } catch (e) { if (!controller.signal.aborted) setError(mcpError(e)); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  };
  const hide = () => { request.current?.abort(); setCredentials(null); setCopied(null); setError(""); setLoading(false); };
  const copy = async (field: "username" | "password") => {
    const value = credentials?.[field];
    if (!value) return;
    try { await copyText(value); setCopied(field); setError(""); }
    catch { setError(c("浏览器无法自动复制，请选择字段内容手动复制。", "Automatic copying is unavailable. Select the field and copy it manually.")); }
  };
  return <div className={styles.proxyCredentials}>
    <h3><KeyRound size={14} />{c("代理账号与密码", "Proxy credentials")}</h3>
    {generated ? <>
      <p>{c("这次监听的账号密码已自动生成并保存。浏览器要求代理验证时，填入这组账号密码；重新启动代理会生成新的一组。", "Credentials are generated and saved for this capture. Enter them when your browser requests proxy authentication. Restarting the proxy creates a new pair.")}</p>
      <dl className={styles.credentialFields}>{(["username", "password"] as const).map(field => <div key={field}><dt>{field === "username" ? c("账号", "Username") : c("密码", "Password")}</dt><dd><code aria-label={!credentials ? c("已隐藏", "Hidden") : undefined}>{credentials?.[field] || "••••••••••••"}</code>{credentials && <button type="button" className={styles.iconButton} aria-label={field === "username" ? c("复制代理账号", "Copy proxy username") : c("复制代理密码", "Copy proxy password")} onClick={() => void copy(field)}>{copied === field ? <Check size={14} /> : <Copy size={14} />}</button>}</dd></div>)}</dl>
      <button type="button" className={styles.secondaryButton} disabled={loading} onClick={credentials ? hide : () => void reveal()}>{loading ? <LoaderCircle size={14} className={styles.spin} /> : credentials ? <EyeOff size={14} /> : <Eye size={14} />}{loading ? c("加载中…", "Loading…") : credentials ? c("隐藏账号密码", "Hide credentials") : c("显示账号密码", "Show credentials")}</button>
      <span className={styles.credentialCopyStatus} role="status">{copied ? (copied === "username" ? c("账号已复制", "Username copied") : c("密码已复制", "Password copied")) : ""}</span>
      {error && <McpError onRetry={!credentials ? () => void reveal() : undefined}>{error}</McpError>}
    </> : <p>{c("这台主机已使用自定义代理账号密码。浏览器要求验证时，请填入管理者提供的账号与密码。", "This host uses custom proxy credentials. Enter the username and password provided by your administrator when the browser requests authentication.")}</p>}
  </div>;
}
