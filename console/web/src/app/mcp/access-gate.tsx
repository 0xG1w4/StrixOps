"use client";

import * as React from "react";
import { KeyRound, LoaderCircle, LockKeyhole, LogOut, Network } from "lucide-react";
import { mcpApi, mcpAuth, mcpError, type McpAccess } from "@/lib/mcp-api";
import { McpError, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

export function McpAccessGate({ children }: { children: React.ReactNode }) {
  const c = useMcpCopy();
  const [access, setAccess] = React.useState<McpAccess | null>(null);
  const [token, setToken] = React.useState("");
  const [error, setError] = React.useState("");
  const [checking, setChecking] = React.useState(true);
  const [hasToken, setHasToken] = React.useState(false);
  const [revision, setRevision] = React.useState(0);
  const [workspaceKey, setWorkspaceKey] = React.useState(0);
  const [expired, setExpired] = React.useState(false);
  const request = React.useRef<AbortController | null>(null);

  React.useEffect(() => mcpAuth.onRequired(() => {
    request.current?.abort();
    setAccess(null); setHasToken(false); setToken(""); setError(""); setChecking(true);
    setExpired(true); setWorkspaceKey(value => value + 1); setRevision(value => value + 1);
  }), []);

  React.useEffect(() => {
    const controller = new AbortController(); request.current = controller;
    setChecking(true); setError("");
    const initialize = async () => {
      let result = await mcpApi.access(controller.signal);
      if (controller.signal.aborted) return;
      // Failed saved credentials never survive a reload or server-side rotation.
      if (!result.allowed && mcpAuth.hasToken()) mcpAuth.clear(false);
      if (result.mode === "automatic" && result.bootstrap_available && (!result.allowed || !mcpAuth.hasToken())) {
        // Keep the workbench unmounted until automatic setup has completed.
        setAccess({ ...result, allowed: false });
        const bootstrap = await mcpApi.bootstrap(controller.signal);
        if (controller.signal.aborted) return;
        if (!bootstrap.allowed || !bootstrap.token) throw new Error(c("MCP 自动初始化未完成，请重试。", "MCP initialization did not complete. Retry to reconnect."));
        mcpAuth.save(bootstrap.token);
        result = await mcpApi.access(controller.signal);
        if (controller.signal.aborted) return;
      }
      if (result.allowed) setExpired(false);
      setAccess(result); setHasToken(result.allowed && mcpAuth.hasToken());
    };
    void initialize().catch(e => { if (!controller.signal.aborted) setError(mcpError(e)); })
      .finally(() => { if (request.current === controller) setChecking(false); });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision]);

  const connect = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!token.trim() || checking) return;
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const candidate = token.trim(); setChecking(true); setError("");
    try {
      const result = await mcpApi.access(controller.signal, candidate);
      if (controller.signal.aborted) return;
      setToken("");
      if (!result.allowed) {
        setAccess(result);
        setError(result.reason === "token_required" ? c("Token 不正确或已变更，请重新输入。", "The token is incorrect or has changed. Enter it again.") : result.message);
        return;
      }
      mcpAuth.save(candidate); setHasToken(true); setExpired(false); setAccess(result);
      setWorkspaceKey(value => value + 1);
    } catch (e) { if (!controller.signal.aborted) { setToken(""); setError(mcpError(e)); } }
    finally { if (request.current === controller) setChecking(false); }
  };
  const disconnect = () => {
    request.current?.abort(); mcpAuth.clear(false);
    setHasToken(false); setToken(""); setError(""); setExpired(false); setChecking(false);
    setWorkspaceKey(value => value + 1);
    setAccess({ allowed: false, token_configured: true, mode: "manual", bootstrap_available: false, reason: "token_required", message: "" });
  };

  if (access?.allowed) return <div className={styles.root}>
    {hasToken && access.mode === "manual" && <div className={styles.accessBar}><span><LockKeyhole size={13} />{c("已验证 MCP 访问", "MCP access verified")}</span><button type="button" className={styles.textButton} onClick={disconnect}><LogOut size={13} />{c("清除 Token", "Clear token")}</button></div>}
    <React.Fragment key={workspaceKey}>{children}</React.Fragment>
  </div>;
  return <div className={styles.root}><section className={styles.accessGate} aria-labelledby="mcp-access-title">
    <div className={styles.eyebrow}><Network size={14} />MCP / ACCESS</div>
    <h1 id="mcp-access-title">{c("连接到 MCP 工作台", "Connect to the MCP workbench")}</h1>
    <p>{access?.mode === "manual" ? c("这台主机已设置自定义 MCP Token，请输入后连接到工作台。", "This host uses a custom MCP token. Enter it to connect to the workbench.") : c("MCP 会自动创建并保存连接设置，完成后即可管理任务、流量与测试。", "MCP creates and saves its connection settings automatically so you can manage tasks, traffic, and tests.")}</p>
    {checking && access?.mode !== "manual" ? <div className={styles.accessChecking}><LoaderCircle size={20} className={styles.spin} />{c("正在准备 MCP 工作台…", "Preparing the MCP workbench…")}</div> : <>
      {expired && <p className={styles.formNote}>{c("MCP 验证已失效。画面中的任务数据已清除，请重新连接。", "MCP authorization expired. Task data has been cleared from this view; connect again.")}</p>}
      {error && <McpError onRetry={access?.mode !== "manual" ? () => setRevision(value => value + 1) : undefined}>{error}</McpError>}
      {access?.mode === "manual" && access.reason === "token_required" && <form onSubmit={event => void connect(event)} className={styles.accessForm}>
        <label className={styles.field} htmlFor="mcp-access-token">MCP Token<input id="mcp-access-token" type="password" autoComplete="current-password" spellCheck={false} autoCapitalize="none" maxLength={4096} value={token} disabled={checking} onChange={event => setToken(event.target.value)} aria-describedby="mcp-token-storage" autoFocus /></label>
        <p className={styles.fieldHint} id="mcp-token-storage">{c("Token 仅保留在此标签页的会话中，刷新后仍可使用；关闭标签页或清除 Token 即移除。", "The token stays in this tab's session across reloads. Close the tab or clear the token to remove it.")}</p>
        <button type="submit" className={styles.primaryButton} disabled={checking || !token.trim()}>{checking ? <LoaderCircle size={14} className={styles.spin} /> : <KeyRound size={14} />}{checking ? c("验证中…", "Verifying…") : c("连接到工作台", "Connect to workbench")}</button>
      </form>}
      {access?.mode === "automatic" && !error && access.reason !== "origin_rejected" && <div className={styles.accessSetup}><h2>{c("无法自动连接", "Automatic connection is unavailable")}</h2><p>{access.message || c("请通过主机 IP、localhost 或主机已设置的 Console 网址打开 MCP，再重新连接。", "Open MCP through the server IP, localhost, or the host's configured Console address, then reconnect.")}</p><button type="button" className={styles.secondaryButton} onClick={() => setRevision(value => value + 1)} disabled={checking}>{c("重新连接", "Reconnect")}</button></div>}
      {access?.reason === "origin_rejected" && <div className={styles.accessSetup}><h2>{c("当前来源无法通过验证", "This origin could not be verified")}</h2><p>{c("请使用主机的正式 Console 网址。若通过反向代理，请确认转发的 Host 与 Origin 和浏览器网址一致。", "Use the host's Console address. If a reverse proxy is involved, check that its forwarded Host and Origin match your browser address.")}</p><button type="button" className={styles.secondaryButton} onClick={() => setRevision(value => value + 1)}>{c("重新检查", "Check again")}</button></div>}
    </>}
  </section></div>;
}
