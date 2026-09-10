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
    mcpApi.access(controller.signal).then(result => {
      if (controller.signal.aborted) return;
      // Failed saved credentials never survive a reload or server-side rotation.
      if (!result.allowed && mcpAuth.hasToken()) mcpAuth.clear(false);
      setAccess(result); setHasToken(result.allowed && mcpAuth.hasToken());
    }).catch(e => { if (!controller.signal.aborted) setError(mcpError(e)); })
      .finally(() => { if (request.current === controller) setChecking(false); });
    return () => controller.abort();
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
        setError(result.reason === "token_required" ? c("Token 不正確或已變更，請重新輸入。", "The token is incorrect or has changed. Enter it again.") : result.message);
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
    setAccess({ allowed: false, token_configured: true, reason: "token_required", message: "" });
  };

  if (access?.allowed) return <div className={styles.root}>
    {hasToken && <div className={styles.accessBar}><span><LockKeyhole size={13} />{c("已驗證 MCP 存取", "MCP access verified")}</span><button type="button" className={styles.textButton} onClick={disconnect}><LogOut size={13} />{c("清除 Token", "Clear token")}</button></div>}
    <React.Fragment key={workspaceKey}>{children}</React.Fragment>
  </div>;
  return <div className={styles.root}><section className={styles.accessGate} aria-labelledby="mcp-access-title">
    <div className={styles.eyebrow}><Network size={14} />MCP / ACCESS</div>
    <h1 id="mcp-access-title">{c("連線到 MCP 工作台", "Connect to the MCP workbench")}</h1>
    <p>{c("使用這台 StrixOps 主機的 MCP Token，即可在目前網址管理任務、流量與測試。", "Use this StrixOps host's MCP token to manage tasks, traffic, and tests at your current address.")}</p>
    {checking && !access ? <div className={styles.accessChecking}><LoaderCircle size={20} className={styles.spin} />{c("檢查存取設定…", "Checking access…")}</div> : <>
      {expired && <p className={styles.formNote}>{c("MCP 驗證已失效。畫面中的任務資料已清除，請重新連線。", "MCP authorization expired. Task data has been cleared from this view; connect again.")}</p>}
      {error && <McpError onRetry={!access ? () => setRevision(value => value + 1) : undefined}>{error}</McpError>}
      {access?.reason === "token_required" && <form onSubmit={event => void connect(event)} className={styles.accessForm}>
        <label className={styles.field} htmlFor="mcp-access-token">MCP Token<input id="mcp-access-token" type="password" autoComplete="current-password" spellCheck={false} autoCapitalize="none" maxLength={4096} value={token} disabled={checking} onChange={event => setToken(event.target.value)} aria-describedby="mcp-token-storage" autoFocus /></label>
        <p className={styles.fieldHint} id="mcp-token-storage">{c("Token 僅保留在此分頁的工作階段，重新整理後仍可使用；關閉分頁或清除 Token 即移除。", "The token stays in this tab's session across reloads. Close the tab or clear the token to remove it.")}</p>
        <button type="submit" className={styles.primaryButton} disabled={checking || !token.trim()}>{checking ? <LoaderCircle size={14} className={styles.spin} /> : <KeyRound size={14} />}{checking ? c("驗證中…", "Verifying…") : c("連線到工作台", "Connect to workbench")}</button>
      </form>}
      {access?.reason === "token_not_configured" && <div className={styles.accessSetup}><h2>{c("主機尚未設定 MCP Token", "The host needs an MCP token")}</h2><p>{c("請在主機設定環境變數 STRIXOPS_MCP_TOKEN，使用自行產生的隨機 Token，然後重新啟動 Console。完成後回到此頁輸入相同 Token。", "Set STRIXOPS_MCP_TOKEN on the host to a generated random token, then restart the Console. Return here and enter the same token.")}</p><code>STRIXOPS_MCP_TOKEN</code><button type="button" className={styles.secondaryButton} onClick={() => setRevision(value => value + 1)} disabled={checking}>{c("重新檢查設定", "Check setup again")}</button></div>}
      {access?.reason === "origin_rejected" && <div className={styles.accessSetup}><h2>{c("目前來源無法通過驗證", "This origin could not be verified")}</h2><p>{c("請使用主機的正式 Console 網址。若透過反向代理，請確認轉送的 Host 與 Origin 和瀏覽器網址一致。", "Use the host's Console address. If a reverse proxy is involved, check that its forwarded Host and Origin match your browser address.")}</p><button type="button" className={styles.secondaryButton} onClick={() => setRevision(value => value + 1)}>{c("重新檢查", "Check again")}</button></div>}
    </>}
  </section></div>;
}
