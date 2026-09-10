"use client";

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowRight, CornerDownRight, Download, Eye, EyeOff, FileText, FlaskConical, LoaderCircle, Play, Repeat2, ShieldCheck, Square } from "lucide-react";
import { mcpApi, mcpError, isMcpTestLive, type McpFlow, type McpMessage, type McpTest, type McpReport } from "@/lib/mcp-api";
import { dateText, downloadMarkdown, McpError, McpStatus, shortId, sizeText, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

export function FlowDetail({ flow, loading, error, mutable, onReplay, onTest, onSelectFlow, onTests, revealed, onToggleReveal }: { flow: McpFlow | null; loading: boolean; error: string; mutable: boolean; onReplay: () => void; onTest: () => void; onSelectFlow: (id: string) => void; onTests: () => void; revealed: boolean; onToggleReveal: () => void }) {
  const c = useMcpCopy();
  const [tab, setTab] = React.useState<"request" | "response">("request");
  const [pretty, setPretty] = React.useState(false);
  const previousFlowId = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (flow && previousFlowId.current !== flow.id) {
      previousFlowId.current = flow.id;
      setTab(flow.source === "replay" ? "response" : "request");
    }
  }, [flow?.id, flow?.source]);
  if (!flow) return <section className={styles.flowDetail}><div className={styles.detailEmpty}>{loading ? <LoaderCircle className={styles.spin} size={25} /> : <CornerDownRight size={28} />}<h3>{loading ? c("讀取請求…", "Loading request…") : c("選取一筆請求", "Select a request")}</h3><p>{c("查看原始內容、重送請求，或交由 Agent 驗證。", "Inspect its content, replay it, or ask an Agent to test it.")}</p>{error && <McpError>{error}</McpError>}</div></section>;
  const message = tab === "request" ? flow.request : flow.response;
  return <section className={styles.flowDetail} aria-label={c("請求詳細資料", "Request details")}>
    <div className={styles.detailHeader}><div className={styles.detailMeta}><span className={styles.mono}>#{shortId(flow.id)}</span><span>{flow.status_code ?? "—"} · {flow.duration_ms == null ? "—" : `${Math.round(flow.duration_ms)} ms`}</span></div><h3><span className={styles.method} data-method={flow.method}>{flow.method}</span><span title={flow.path || flow.url}>{flow.path || flow.url}</span></h3><div className={styles.detailHost} role="region" tabIndex={0} aria-label={c("完整請求 URL", "Full request URL")}>{flow.request?.url || flow.url}</div></div>
    <div className={styles.detailToolbar}><div className={styles.tabGroup} role="tablist" aria-label={c("請求與回應", "Request and response")}>{(["request", "response"] as const).map(item => <button key={item} type="button" role="tab" id={`mcp-flow-tab-${item}`} aria-selected={tab === item} aria-controls="mcp-message-panel" onClick={() => setTab(item)}>{item === "request" ? "Request" : "Response"}</button>)}</div><button type="button" className={styles.formatButton} aria-pressed={pretty} onClick={() => setPretty(value => !value)}>{pretty ? "Pretty" : "Raw"}</button></div>
    <div id="mcp-message-panel" role="tabpanel" aria-labelledby={`mcp-flow-tab-${tab}`} className={styles.messagePanel}>
      <div className={styles.maskNote}>{revealed ? <Eye size={12} /> : <ShieldCheck size={12} />}<span>{revealed ? c("正在顯示敏感內容", "Sensitive values visible") : c("敏感欄位已遮罩", "Sensitive values masked")}</span><button type="button" aria-pressed={revealed} disabled={loading} onClick={onToggleReveal} className={styles.revealButton}>{revealed ? <EyeOff size={12} /> : <Eye size={12} />}{revealed ? c("隱藏敏感內容", "Hide sensitive values") : c("顯示敏感內容", "Reveal sensitive values")}</button>{message?.body_size != null && <span className={styles.messageSize}>{sizeText(message.body_size)}</span>}</div>
      {error && <McpError>{error}</McpError>}
      {flow.error && <McpError>{flow.error}</McpError>}
      {loading && <div className={styles.muted}>{c("正在更新…", "Updating…")}</div>}
      {message ? <MessageView message={message} flow={flow} direction={tab} pretty={pretty} /> : <div className={styles.inlineEmpty}>{tab === "response" ? c("尚未收到回應，或此次請求沒有回應。", "No response has been recorded for this request.") : c("請求內容目前無法取得。", "Request content is unavailable.")}</div>}
      {(flow.truncated || message?.truncated) && <div className={styles.warningNote}>{c("內容超過保存上限，這份紀錄已截斷。", "This record was truncated at the storage limit.")}</div>}
    </div>
    <div className={styles.detailActions}><button className={styles.secondaryButton} disabled={!mutable || loading || !flow.request} onClick={onReplay}><Repeat2 size={14} />{c("重送請求", "Replay")}</button><button className={styles.primaryButton} disabled={!mutable || loading || !flow.request} onClick={onTest}><Play size={14} />{c("Agent 測試", "Agent test")}</button></div>
    <div className={styles.evidenceChain}>{flow.parent_flow_id ? <button type="button" onClick={() => onSelectFlow(flow.parent_flow_id!)}>{c("原始請求", "Original")} #{shortId(flow.parent_flow_id)}</button> : <span>{c("原始請求", "Original")} #{shortId(flow.id)}</span>}<ArrowRight size={12} /><span>{flow.source === "replay" ? c("重送", "Replay") : flow.source === "agent" ? "Agent" : c("已捕獲", "Captured")}</span>{flow.test_id && <><ArrowRight size={12} /><button type="button" onClick={onTests}>TEST #{shortId(flow.test_id)}</button></>}</div>
  </section>;
}

function MessageView({ message, flow, direction, pretty }: { message: McpMessage; flow: McpFlow; direction: "request" | "response"; pretty: boolean }) {
  const c = useMcpCopy();
  const headers = Array.isArray(message.headers) ? message.headers : [];
  let requestTarget = message.path || flow.path || "/";
  try { const parsed = new URL(message.url || flow.url); requestTarget = parsed.pathname + parsed.search; } catch { /* Retain the stored path for incomplete URL records. */ }
  const statusLine = direction === "request" ? `${flow.method} ${requestTarget} ${message.http_version || "HTTP"}` : `${message.http_version || "HTTP"} ${flow.status_code ?? ""}`;
  let body = message.body_text ?? "";
  if (pretty && body) { try { body = JSON.stringify(JSON.parse(body), null, 2); } catch { /* Keep text bodies readable without altering their content. */ } }
  const binary = Boolean(message.binary) || (message.body_text == null && Boolean(message.body_base64 || message.body_size));
  if (!pretty) return <pre className={styles.rawMessage}>{statusLine}{"\n"}{headers.map(([key, value]) => `${key}: ${value}`).join("\n")}{"\n\n"}{binary ? c(`[二進位內容 · ${sizeText(message.body_size)}]`, `[Binary content · ${sizeText(message.body_size)}]`) : body}</pre>;
  return <><div className={styles.headersList}>{headers.map(([key, value], index) => <div key={`${key}-${index}`}><span>{key}</span><code>{value}</code></div>)}</div><div className={styles.bodyHeading}>Body</div><pre className={styles.rawMessage}>{binary ? c(`[二進位內容 · ${sizeText(message.body_size)}]`, `[Binary content · ${sizeText(message.body_size)}]`) : body || c("（空）", "(empty)")}</pre></>;
}

export function TestsPanel({ taskId, tests, loading, error, onRefresh, onSelectFlow, readOnly = false }: { readOnly?: boolean; taskId: string; tests: McpTest[]; loading: boolean; error?: string; onRefresh: () => void; onSelectFlow: (id: string) => void }) {
  const c = useMcpCopy();
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [cancelling, setCancelling] = React.useState(false);
  const [actionError, setActionError] = React.useState("");
  const [prompt, setPrompt] = React.useState<{ id: string; content: string } | null>(null);
  const [promptLoading, setPromptLoading] = React.useState(false);
  const [showPrompt, setShowPrompt] = React.useState(false);
  const active = tests.find(test => test.id === selectedId) || tests[0] || null;
  React.useEffect(() => { setShowPrompt(false); setActionError(""); }, [active?.id]);
  React.useEffect(() => {
    if (!showPrompt || !active || prompt?.id === active.id) return;
    const controller = new AbortController(); setPromptLoading(true);
    mcpApi.test(taskId, active.id, controller.signal).then(({ test }) => {
      if (!controller.signal.aborted) setPrompt({ id: test.id, content: typeof test.prompt_snapshot === "string" ? test.prompt_snapshot : test.prompt_snapshot ? JSON.stringify(test.prompt_snapshot, null, 2) : c("此測試沒有保存 Prompt 快照。", "No prompt snapshot is stored for this test.") });
    }).catch(e => { if (!controller.signal.aborted) setActionError(mcpError(e)); }).finally(() => { if (!controller.signal.aborted) setPromptLoading(false); });
    return () => controller.abort();
    // The requested test identity owns the response; translated fallback text is not a fetch dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showPrompt, active?.id, taskId, prompt?.id]);
  const cancel = async () => { if (!active || cancelling) return; setCancelling(true); setActionError(""); try { await mcpApi.cancelTest(taskId, active.id); onRefresh(); } catch (e) { setActionError(mcpError(e)); } finally { setCancelling(false); } };
  if (!tests.length) return <div className={styles.contentEmpty}><FlaskConical size={30} /><h3>{loading ? c("讀取測試…", "Loading tests…") : c("從捕獲的請求開始測試", "Start from a captured request")}</h3><p>{c("回到流量頁，選取一筆或多筆請求，然後建立 Agent 測試。", "Select one or more requests in Traffic, then create an Agent test.")}</p>{error && <McpError onRetry={onRefresh}>{error}</McpError>}</div>;
  return <div className={styles.recordsLayout}><aside className={styles.recordList} aria-label={c("測試列表", "Tests")}>{tests.map(test => <button type="button" key={test.id} className={styles.recordItem} aria-pressed={active?.id === test.id} onClick={() => setSelectedId(test.id)}><span className={styles.recordIdentity}>TEST #{shortId(test.id)}<McpStatus status={test.status} /></span><span className={styles.muted}>{test.flow_ids.length} {c("筆請求", "requests")} · {dateText(test.created_at)}</span></button>)}</aside>
    {active && <section className={styles.recordDetail}><div className={styles.recordHeader}><div><div className={styles.eyebrow}>TEST #{shortId(active.id)}</div><h3>{c("Agent 測試結果", "Agent test results")}</h3></div><div className={styles.actionGroup}><McpStatus status={active.status} />{isMcpTestLive(active) && <button className={styles.secondaryButton} disabled={cancelling || readOnly} onClick={() => void cancel()}><Square size={12} />{cancelling ? c("取消中…", "Cancelling…") : c("取消測試", "Cancel test")}</button>}</div></div>
      {(error || actionError || active.error) && <McpError>{actionError || active.error || error}</McpError>}
      <div className={styles.testMeta}><span>{c("開始", "Started")}: {dateText(active.started_at)}</span><span>{c("完成", "Finished")}: {dateText(active.finished_at)}</span><span>{c("預算", "Budget")}: {active.config?.max_requests ?? "—"} {c("筆請求", "requests")} / {active.config?.max_seconds ?? "—"} s</span></div>
      <div className={styles.selectedFlowIds}>{active.flow_ids.map(id => <button type="button" key={id} onClick={() => onSelectFlow(id)}>{c("原始請求", "Source")} #{shortId(id)}<ArrowRight size={12} /></button>)}</div>
      {active.result?.summary ? <div className={styles.markdown}><ReactMarkdown remarkPlugins={[remarkGfm]}>{active.result.summary}</ReactMarkdown></div> : <div className={styles.inlineEmpty}>{isMcpTestLive(active) ? c("Agent 正在執行。完成後，摘要與證據會顯示在這裡。", "The Agent is working. Its summary and evidence will appear here.") : c("這次測試沒有產出摘要；請查看狀態與執行紀錄。", "This test has no summary. Review its status and execution log.")}</div>}
      {Boolean(active.result?.findings?.length) && <section className={styles.findingsSection}><h4>{c("發現", "Findings")}</h4>{active.result!.findings!.map((finding, index) => <article className={styles.finding} key={finding.id || index}><div><span className={styles.severity} data-severity={(finding.severity || "info").toLowerCase()}>{finding.severity || "INFO"}</span><h4>{finding.title || c("未命名發現", "Untitled finding")}</h4></div>{(finding.description || finding.observation) && <p>{finding.description || finding.observation}</p>}{!!finding.evidence_flow_ids?.length && <div className={styles.selectedFlowIds}>{finding.evidence_flow_ids.map(id => <button type="button" key={id} onClick={() => onSelectFlow(id)}>#{shortId(id)}<ArrowRight size={12} /></button>)}</div>}{finding.evidence != null && <details><summary>{c("檢視證據", "View evidence")}</summary><pre className={styles.rawMessage}>{typeof finding.evidence === "string" ? finding.evidence : JSON.stringify(finding.evidence, null, 2)}</pre></details>}</article>)}</section>}
      {Boolean(active.result?.coverage?.length) && <details className={styles.disclosure}><summary>{c("測試範圍與未完成項目", "Coverage and remaining checks")}</summary><pre className={styles.rawMessage}>{JSON.stringify(active.result!.coverage, null, 2)}</pre></details>}
      <details className={styles.disclosure}><summary>{c("執行紀錄", "Execution log")} <span className={styles.muted}>{active.events?.length || 0}</span></summary><div className={styles.eventLog}>{active.events?.length ? active.events.map((event, index) => <div key={index}><time>{dateText(event.timestamp, true)}</time><span>{event.message || JSON.stringify(event)}</span></div>) : <p className={styles.muted}>{c("目前沒有紀錄。", "No events recorded.")}</p>}</div></details>
      <button type="button" className={styles.textButton} onClick={() => setShowPrompt(value => !value)}>{showPrompt ? c("收起 Prompt 快照", "Hide prompt snapshot") : c("檢視本次 Prompt 快照", "View this test's prompt snapshot")}</button>
      {showPrompt && <pre className={styles.promptPreview}>{promptLoading ? c("讀取中…", "Loading…") : prompt?.id === active.id ? prompt.content : "—"}</pre>}
    </section>}
  </div>;
}

export function ReportsPanel({ taskId, reports, onRefresh, readOnly = false }: { readOnly?: boolean; taskId: string; reports: McpReport[]; onRefresh: () => void }) {
  const c = useMcpCopy();
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [report, setReport] = React.useState<McpReport | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [generating, setGenerating] = React.useState(false);
  const [error, setError] = React.useState("");
  const currentId = selectedId || reports[0]?.id;
  React.useEffect(() => {
    if (!currentId) return;
    const controller = new AbortController(); setLoading(true); setError("");
    mcpApi.report(taskId, currentId, controller.signal).then(result => { if (!controller.signal.aborted) setReport(result.report); }).catch(e => { if (!controller.signal.aborted) setError(mcpError(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [taskId, currentId]);
  const generate = async () => { if (generating) return; setGenerating(true); setError(""); try { const result = await mcpApi.createReport(taskId); setReport(result.report); setSelectedId(result.report.id); onRefresh(); } catch (e) { setError(mcpError(e)); } finally { setGenerating(false); } };
  return <div className={styles.reportWorkspace}><header className={styles.reportHeader}><div><h3>{c("MCP 任務報告", "MCP task reports")}</h3><p>{c("彙整已觀察流量、測試結果、範圍與對應證據。", "Observed traffic, test results, scope, and supporting evidence.")}</p></div><button className={styles.primaryButton} disabled={generating || readOnly} onClick={() => void generate()}>{generating ? <LoaderCircle size={15} className={styles.spin} /> : <FileText size={15} />}{generating ? c("產生中…", "Generating…") : c("產生新報告", "Generate report")}</button></header>
    {error && <McpError>{error}</McpError>}
    {!!reports.length && <div className={styles.reportControls}><label className={styles.inlineField}>{c("報告版本", "Version")}<select className={styles.input} value={currentId || ""} onChange={e => setSelectedId(e.target.value)}>{reports.map(item => <option value={item.id} key={item.id}>{dateText(item.created_at)} · #{shortId(item.id)}</option>)}</select></label><button className={styles.secondaryButton} disabled={!report?.markdown || loading || report.id !== currentId} onClick={() => report?.markdown && downloadMarkdown(report.markdown, `mcp-${taskId}-${report.id}.md`)}><Download size={14} />{c("下載 Markdown", "Download Markdown")}</button></div>}
    {loading ? <div className={styles.contentEmpty}><LoaderCircle size={26} className={styles.spin} /><p>{c("讀取報告…", "Loading report…")}</p></div> : report && report.id === currentId ? <article className={`${styles.markdown} ${styles.reportDocument}`}><ReactMarkdown remarkPlugins={[remarkGfm]}>{report.markdown || c("此報告沒有 Markdown 內容。", "This report has no Markdown content.")}</ReactMarkdown></article> : <div className={styles.contentEmpty}><FileText size={32} /><h3>{c("尚未產生報告", "No report yet")}</h3><p>{c("產生報告以保存目前的觀察結果與測試進度。", "Generate a report to save the current observations and testing progress.")}</p></div>}
  </div>;
}
