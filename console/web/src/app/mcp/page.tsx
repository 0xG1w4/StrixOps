"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight, CheckCheck, ChevronDown, FileText, FlaskConical, Globe2, Layers3, LoaderCircle, Network, Play, Plus, RefreshCw, Search, Settings2, ShieldCheck, Square, Trash2, Waypoints } from "lucide-react";
import { McpApiError, mcpApi, mcpError, isMcpTaskLive, isMcpTestLive, type McpTask, type McpFlow, type McpEndpoint, type McpCatalog, type McpTest, type McpReport } from "@/lib/mcp-api";
import { AgentTestForm, DeleteTaskDialog, ReplayForm, TaskForm } from "./forms";
import { SharedCaPanel } from "./ca-panel";
import { McpAccessGate } from "./access-gate";
import { FlowDetail, ReportsPanel, TestsPanel } from "./panels";
import { dateText, McpError, McpModal, McpStatus, shortId, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

const POLL_MS = 3000;
const activeTests = (tests: McpTest[]) => tests.some(isMcpTestLive);

export default function McpPage() {
  return <React.Suspense fallback={<div className={styles.contentEmpty}><LoaderCircle size={26} className={styles.spin} /></div>}><McpAccessGate><McpRouter /></McpAccessGate></React.Suspense>;
}

function McpRouter() {
  const params = useSearchParams();
  const id = params.get("task");
  const [catalog, setCatalog] = React.useState<McpCatalog | null>(null);
  const [catalogError, setCatalogError] = React.useState("");
  React.useEffect(() => { const controller = new AbortController(); mcpApi.catalog(controller.signal).then(value => { if (!controller.signal.aborted) setCatalog(value); }).catch(e => { if (!controller.signal.aborted) setCatalogError(mcpError(e)); }); return () => controller.abort(); }, []);
  return <div className={styles.root}>{id ? <TaskWorkspace key={id} taskId={id} catalog={catalog} catalogError={catalogError} /> : <TaskDirectory />}</div>;
}

function TaskDirectory() {
  const c = useMcpCopy();
  const unavailableNotice = useSearchParams().get("notice") === "task-unavailable";
  const router = useRouter();
  const [tasks, setTasks] = React.useState<McpTask[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [createOpen, setCreateOpen] = React.useState(false);
  const [deleteTask, setDeleteTask] = React.useState<McpTask | null>(null);
  const [caOpen, setCaOpen] = React.useState(false);
  const [revision, setRevision] = React.useState(0);
  React.useEffect(() => {
    let disposed = false; let timer: ReturnType<typeof setTimeout>; let controller: AbortController | null = null;
    const load = async () => {
      if (disposed || document.hidden) return;
      controller?.abort(); const current = new AbortController(); controller = current;
      const timeout = setTimeout(() => current.abort(), 10000);
      try { const result = await mcpApi.tasks(current.signal); if (disposed || controller !== current || current.signal.aborted) return; setTasks(result.tasks); setError(""); if (result.tasks.some(isMcpTaskLive)) timer = setTimeout(() => void load(), POLL_MS); }
      catch (e) { if (!disposed && controller === current && !document.hidden) setError(current.signal.aborted ? c("讀取任務逾時，請重試。", "Loading tasks timed out. Retry to reconnect.") : mcpError(e)); }
      finally { clearTimeout(timeout); if (!disposed && controller === current) setLoading(false); }
    };
    const visibility = () => { clearTimeout(timer); if (document.hidden) controller?.abort(); else void load(); };
    void load(); document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision]);
  return <>
    <header className={styles.pageHeader}><div><div className={styles.eyebrow}><Waypoints size={14} /> MCP / TRAFFIC</div><h1>MCP</h1><p>{c("將瀏覽器流量轉成可探索的頁面、API 與測試證據。", "Turn browser traffic into discoverable pages, APIs, and test evidence.")}</p></div><div className={styles.actionGroup}><button type="button" className={styles.iconButton} aria-label={c("重新整理", "Refresh")} onClick={() => setRevision(v => v + 1)}><RefreshCw size={16} /></button><button type="button" className={styles.secondaryButton} aria-expanded={caOpen} onClick={() => setCaOpen(value => !value)}><ShieldCheck size={15} />{c("共用 CA", "Shared CA")}</button><button type="button" className={styles.primaryButton} onClick={() => setCreateOpen(true)}><Plus size={16} />{c("建立任務", "Create task")}</button></div></header>
    {unavailableNotice && <div className={styles.formNote} role="status">{c("任務已刪除或無法再取得；請從下方選取其他任務。", "The task was deleted or is no longer available. Choose another task below.")}</div>}
    {caOpen && <section className={styles.caStandalone}><SharedCaPanel /></section>}
    {error && <McpError onRetry={() => setRevision(v => v + 1)}>{error}</McpError>}
    <section className={styles.directory} aria-label={c("MCP 任務", "MCP tasks")}>
      <div className={styles.sectionHeader}><h2>{c("獨立流量任務", "Traffic tasks")}</h2><span>{tasks.length} {c("個任務", "tasks")}</span></div>
      {loading ? <div className={styles.contentEmpty}><LoaderCircle size={28} className={styles.spin} /><p>{c("讀取任務…", "Loading tasks…")}</p></div> : !tasks.length ? <div className={styles.directoryEmpty}><div className={styles.emptyGlyph}><Network size={34} /></div><h2>{c("從一段瀏覽流程開始", "Start with a browsing session")}</h2><p>{c("建立任務並啟動代理，瀏覽目標網站後，即可從捕獲的請求展開測試。", "Create a task, start its proxy, and browse your target. Use the captured requests to begin testing.")}</p><div className={styles.emptySteps}><span>01 {c("連線代理", "Connect proxy")}</span><ArrowRight size={14} /><span>02 {c("探索流量", "Explore traffic")}</span><ArrowRight size={14} /><span>03 {c("測試與報告", "Test and report")}</span></div><button className={styles.primaryButton} onClick={() => setCreateOpen(true)}><Plus size={15} />{c("建立第一個任務", "Create your first task")}</button></div> : <div className={styles.taskRows}>{tasks.map(task => <div className={styles.taskRowShell} key={task.id}><Link href={`/mcp?task=${encodeURIComponent(task.id)}`} className={styles.taskRow}><div className={styles.taskGlyph}><Waypoints size={21} /></div><div className={styles.taskIdentity}><div><h3 title={task.name}>{task.name}</h3><McpStatus status={task.status} /></div><p title={task.allow_hosts.join(" · ")}>{task.allow_hosts.join(" · ") || c("尚未設定主機", "No hosts configured")}</p></div><div className={styles.taskCounts}><span><strong>{task.counts?.flows ?? 0}</strong>{c("請求", "requests")}</span><span><strong>{task.counts?.tests ?? 0}</strong>{c("測試", "tests")}</span></div><time>{dateText(task.updated_at)}</time><ArrowRight size={16} className={styles.rowArrow} /></Link><button type="button" className={styles.deleteRowButton} disabled={task.status === "deleting"} aria-label={c(`刪除任務 ${task.name}`, `Delete task ${task.name}`)} onClick={() => setDeleteTask(task)}><Trash2 size={15} /></button></div>)}</div>}
    </section>
    {deleteTask && <DeleteTaskDialog task={deleteTask} onClose={() => setDeleteTask(null)} onDeleted={() => { setTasks(old => old.filter(item => item.id !== deleteTask.id)); setDeleteTask(null); setRevision(value => value + 1); }} />}
    {createOpen && <TaskForm onClose={() => setCreateOpen(false)} onSaved={task => { setCreateOpen(false); router.push(`/mcp?task=${encodeURIComponent(task.id)}`); }} />}
  </>;
}

type WorkspaceTab = "traffic" | "tests" | "reports";

function TaskWorkspace({ taskId, catalog, catalogError }: { taskId: string; catalog: McpCatalog | null; catalogError: string }) {
  const c = useMcpCopy();
  const router = useRouter();
  const [task, setTask] = React.useState<McpTask | null>(null);
  const [tests, setTests] = React.useState<McpTest[]>([]);
  const [reports, setReports] = React.useState<McpReport[]>([]);
  const [endpoints, setEndpoints] = React.useState<McpEndpoint[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [dataError, setDataError] = React.useState("");
  const [actionError, setActionError] = React.useState("");
  const [refresh, setRefresh] = React.useState(0);
  const [tab, setTab] = React.useState<WorkspaceTab>("traffic");
  const [settingsOpen, setSettingsOpen] = React.useState(false);
  const [connectionOpen, setConnectionOpen] = React.useState(false);
  const [endOpen, setEndOpen] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const [action, setAction] = React.useState<string | null>(null);
  const [selectedFlowId, setSelectedFlowId] = React.useState<string | null>(null);
  const [testFlowIds, setTestFlowIds] = React.useState<string[] | null>(null);
  const refreshAll = React.useCallback(() => setRefresh(value => value + 1), []);
  const liveRef = React.useRef(false);
  React.useEffect(() => {
    if (task && ["deleting", "delete_failed"].includes(task.status)) { setSettingsOpen(false); setEndOpen(false); setTestFlowIds(null); }
  }, [task?.status]);

  React.useEffect(() => {
    let disposed = false; let timer: ReturnType<typeof setTimeout>; let controller: AbortController | null = null;
    const load = async () => {
      if (disposed || document.hidden) return;
      controller?.abort(); const current = new AbortController(); controller = current;
      const timeout = setTimeout(() => current.abort(), 12000);
      const results = await Promise.allSettled([mcpApi.task(taskId, current.signal), mcpApi.tests(taskId, current.signal), mcpApi.endpoints(taskId, current.signal), mcpApi.reports(taskId, current.signal)]);
      clearTimeout(timeout); if (disposed || controller !== current || document.hidden) return;
      if (current.signal.aborted) { setError(c("更新逾時，正在顯示上次取得的資料。", "Update timed out. Showing the last available data.")); }
      else {
        const [taskResult, testResult, endpointResult, reportResult] = results;
        if (taskResult.status === "fulfilled") { setTask(taskResult.value.task); setError(""); }
        else if (taskResult.reason instanceof McpApiError && taskResult.reason.status === 404) { router.replace("/mcp?notice=task-unavailable"); return; }
        else setError(mcpError(taskResult.reason));
        if (testResult.status === "fulfilled") setTests(testResult.value.tests);
        if (endpointResult.status === "fulfilled") setEndpoints(endpointResult.value.endpoints);
        if (reportResult.status === "fulfilled") setReports(reportResult.value.reports);
        const failed = [testResult, endpointResult, reportResult].filter(result => result.status === "rejected");
        setDataError(failed.map(result => result.status === "rejected" ? mcpError(result.reason) : "").join(" · "));
        liveRef.current = (taskResult.status === "fulfilled" ? isMcpTaskLive(taskResult.value.task) : liveRef.current) || (testResult.status === "fulfilled" && activeTests(testResult.value.tests));
      }
      setLoading(false); if (liveRef.current) timer = setTimeout(() => void load(), POLL_MS);
    };
    const visibility = () => { clearTimeout(timer); if (document.hidden) controller?.abort(); else void load(); };
    void load(); document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, refresh]);

  const transition = async (next: "start" | "stop" | "end") => {
    if (action) return; setAction(next); setActionError("");
    try { const result = await mcpApi.transition(taskId, next); setTask({ ...result.task, ...(result.session ? { session: result.session } : {}) }); setEndOpen(false); if (next === "start") setConnectionOpen(true); refreshAll(); }
    catch (e) { if (e instanceof McpApiError && e.status === 404) router.replace("/mcp?notice=task-unavailable"); else setActionError(mcpError(e)); } finally { setAction(null); }
  };
  const openFlow = (id: string) => { setSelectedFlowId(id); setTab("traffic"); };
  if (!task) return <><Link href="/mcp" className={styles.backLink}><ArrowLeft size={14} />{c("返回 MCP", "Back to MCP")}</Link>{error ? <McpError onRetry={refreshAll}>{error}</McpError> : <div className={styles.contentEmpty}><LoaderCircle size={28} className={styles.spin} /><p>{c("讀取工作台…", "Loading workspace…")}</p></div>}</>;
  const ended = task.status === "ended";
  const capturing = task.status === "capturing";
  const session = task.session;
  const proxyError = task.error || session?.error || session?.ingest_error || (["capacity_reached", "storage_error"].includes(session?.capture_status?.state || "") ? session?.capture_status?.error || session?.capture_status?.message : "");
  const busy = Boolean(action) || ["starting", "stopping", "ending", "deleting"].includes(task.status);
  const deletionBlocked = ["deleting", "delete_failed"].includes(task.status);
  const mutationBlocked = busy || deletionBlocked;
  return <>
    <Link href="/mcp" className={styles.backLink}><ArrowLeft size={14} />{c("所有 MCP 任務", "All MCP tasks")}</Link>
    <header className={styles.pageHeader}><div><div className={styles.eyebrow}>MCP / <span className={styles.mono}>{shortId(task.id)}</span></div><h1>{task.name}</h1><div className={styles.taskSubtitle}><McpStatus status={task.status} /><span>{task.allow_hosts.join(" · ")}</span></div></div><div className={styles.actionGroup}><button className={styles.iconButton} type="button" aria-label={c("重新整理", "Refresh")} onClick={refreshAll}><RefreshCw size={15} /></button><button className={styles.secondaryButton} disabled={ended || mutationBlocked} onClick={() => setSettingsOpen(true)}><Settings2 size={15} />{c("設定", "Settings")}</button>{!ended && <button className={styles.secondaryButton} disabled={mutationBlocked} onClick={() => setEndOpen(true)}><CheckCheck size={15} />{c("結束任務", "End task")}</button>}<button type="button" className={styles.deleteButton} disabled={busy} onClick={() => setDeleteOpen(true)}><Trash2 size={14} />{task.status === "delete_failed" ? c("重試刪除", "Retry delete") : c("刪除", "Delete")}</button></div></header>
    {(error || dataError || actionError) && <McpError onRetry={refreshAll}>{actionError || error || `${c("部分資料無法更新", "Some data could not be updated")}: ${dataError}`}</McpError>}
    <section className={styles.sessionBar} aria-label={c("代理工作階段", "Proxy session")}><div className={styles.sessionIdentity}><span className={styles.sessionSignal} data-live={capturing}><Network size={18} /></span><div><div className={styles.sessionTitle}>{capturing ? c("代理正在捕獲流量", "Proxy is capturing traffic") : task.status === "error" ? c("代理需要處理", "Proxy needs attention") : ended ? c("任務已結束，資料已保留", "Task ended; records retained") : c("啟動代理，開始觀察網站", "Start the proxy to observe your site")}</div><div className={styles.sessionMeta}>{session?.proxy_port ? <code>{session.proxy_host || "127.0.0.1"}:{session.proxy_port}</code> : <span>{c("每個工作階段使用獨立容器", "Each session uses an isolated container")}</span>}{session && <span>SESSION #{shortId(session.id)}</span>}</div></div></div><div className={styles.actionGroup}><button className={styles.secondaryButton} onClick={() => setConnectionOpen(value => !value)} aria-expanded={connectionOpen}><ShieldCheck size={14} />{c("連線方式", "Connection")}<ChevronDown size={13} /></button>{task.status === "error" && session && <button className={styles.secondaryButton} disabled={mutationBlocked} onClick={() => void transition("stop")}><Square size={13} />{c("停止代理", "Stop proxy")}</button>}{!ended && <button className={capturing ? styles.secondaryButton : styles.primaryButton} disabled={mutationBlocked} onClick={() => void transition(capturing ? "stop" : "start")}>{busy ? <LoaderCircle size={14} className={styles.spin} /> : capturing ? <Square size={13} /> : <Play size={14} />}{busy ? c("處理中…", "Working…") : capturing ? c("停止捕獲", "Stop capture") : c("啟動代理", "Start proxy")}</button>}</div></section>
    {proxyError && <McpError>{proxyError}</McpError>}
    {connectionOpen && <ConnectionPanel task={task} />}
    <div className={styles.workspaceTabs} role="tablist" aria-label={c("工作台內容", "Workspace content")}>{([{ id: "traffic", label: c("流量", "Traffic"), icon: Network, count: task.counts?.flows }, { id: "tests", label: c("Agent 測試", "Agent tests"), icon: FlaskConical, count: tests.length }, { id: "reports", label: c("報告", "Reports"), icon: FileText, count: reports.length }] as const).map(item => <button type="button" key={item.id} role="tab" id={`mcp-workspace-tab-${item.id}`} aria-selected={tab === item.id} aria-controls={`mcp-workspace-${item.id}`} onClick={() => setTab(item.id)}><item.icon size={15} />{item.label}{item.count != null && <span>{item.count}</span>}</button>)}</div>
    <div role="tabpanel" id="mcp-workspace-traffic" aria-labelledby="mcp-workspace-tab-traffic" hidden={tab !== "traffic"}><TrafficWorkspace task={task} live={isMcpTaskLive(task) || activeTests(tests)} active={tab === "traffic"} endpoints={endpoints} refresh={refresh} selectedId={selectedFlowId} onSelect={setSelectedFlowId} onTest={setTestFlowIds} onRefresh={refreshAll} onTests={() => setTab("tests")} /></div>
    {tab === "tests" && <div role="tabpanel" id="mcp-workspace-tests" aria-labelledby="mcp-workspace-tab-tests"><TestsPanel readOnly={deletionBlocked} taskId={taskId} tests={tests} loading={loading} error={dataError} onRefresh={refreshAll} onSelectFlow={openFlow} /></div>}
    {tab === "reports" && <div role="tabpanel" id="mcp-workspace-reports" aria-labelledby="mcp-workspace-tab-reports"><ReportsPanel readOnly={deletionBlocked} taskId={taskId} reports={reports} onRefresh={refreshAll} /></div>}
    {deleteOpen && <DeleteTaskDialog task={task} onClose={() => setDeleteOpen(false)} onDeleted={() => router.replace("/mcp")} />}
    {settingsOpen && <TaskForm task={task} onClose={() => setSettingsOpen(false)} onSaved={updated => { setTask(updated); setSettingsOpen(false); refreshAll(); }} />}
    {testFlowIds && <AgentTestForm task={task} flowIds={testFlowIds} catalog={catalog} catalogError={catalogError} onClose={() => setTestFlowIds(null)} onCreated={test => { setTests(old => [test, ...old.filter(item => item.id !== test.id)]); setTestFlowIds(null); setTab("tests"); refreshAll(); }} />}
    {endOpen && <McpModal title={c("結束這個 MCP 任務？", "End this MCP task?")} description={c("代理將停止，任務不再接收新流量與測試。現有資料仍可檢視並產生報告。", "The proxy stops accepting traffic and new tests. Existing records remain available for review and reporting.")} onClose={() => !action && setEndOpen(false)}><div className={styles.form}>{activeTests(tests) && <div className={styles.warningNote}>{c("仍有 Agent 測試進行中；後端會處理任務結束時的執行狀態。", "Agent tests are still active; the server will handle their state when ending the task.")}</div>}{actionError && <McpError>{actionError}</McpError>}<div className={styles.formActions}><button className={styles.secondaryButton} disabled={Boolean(action)} onClick={() => setEndOpen(false)}>{c("繼續工作", "Keep working")}</button><button className={styles.primaryButton} disabled={Boolean(action)} onClick={() => void transition("end")}>{action ? c("結束中…", "Ending…") : c("結束任務", "End task")}</button></div></div></McpModal>}
  </>;
}

function ConnectionPanel({ task }: { task: McpTask }) {
  const c = useMcpCopy();
  const session = task.session;
  const host = session?.proxy_host || "127.0.0.1";
  const port = session?.proxy_port;
  const bindHost = (session?.proxy_bind_host || host).replace(/^\[|\]$/g, "").toLowerCase();
  const loopback = bindHost === "localhost" || bindHost === "::1" || /^127\./.test(bindHost);
  const displayHost = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  const tunnelHost = bindHost.includes(":") ? `[${bindHost}]` : bindHost;
  return <section className={styles.connectionPanel}><div><h3>{c("瀏覽器代理", "Browser proxy")}</h3><p>{c("在瀏覽器的 HTTP 與 HTTPS 代理設定填入下列位址。瀏覽目標網站後，流量會出現在這個任務。", "Use this address for your browser's HTTP and HTTPS proxy. Browsing your target will add traffic to this task.")}</p><code className={styles.proxyAddress}>{port ? `${displayHost}:${port}` : c("啟動後顯示位址", "Address available after start")}</code>
    {port && loopback && <><small>{c("此代理只監聽主機本機位址。若瀏覽器在另一台電腦，請先建立 SSH 通道，再使用 127.0.0.1 與上述連接埠。", "This proxy listens on the server's loopback address. From another computer, create an SSH tunnel and use 127.0.0.1 with the port above.")}</small><code className={styles.tunnelCommand}>{`ssh -N -L ${port}:${tunnelHost}:${port} user@server`}</code></>}
    {port && !loopback && <small>{c("此代理允許從其他電腦連入。請確認主機防火牆允許你的測試電腦存取上述連接埠。", "This proxy accepts connections from other computers. Allow your testing computer to reach this port through the host firewall.")}</small>}
    {session?.proxy_auth_required && <div className={styles.formNote}>{c("瀏覽器連線時會要求代理帳號與密碼。請使用主機設定的 STRIXOPS_MCP_PROXY_AUTH 帳密；這與工作台的 MCP Token 分開。", "Your browser will request a proxy username and password. Use the host's STRIXOPS_MCP_PROXY_AUTH credentials; these are separate from the workbench's MCP token.")}</div>}
  </div><div><SharedCaPanel task={task} /></div></section>;
}

function TrafficWorkspace({ task, live, active, endpoints, refresh, selectedId, onSelect, onTest, onRefresh, onTests }: { task: McpTask; live: boolean; active: boolean; endpoints: McpEndpoint[]; refresh: number; selectedId: string | null; onSelect: (id: string | null) => void; onTest: (ids: string[]) => void; onRefresh: () => void; onTests: () => void }) {
  const c = useMcpCopy();
  const [query, setQuery] = React.useState("");
  const [debounced, setDebounced] = React.useState("");
  const [kind, setKind] = React.useState("");
  const [source, setSource] = React.useState("");
  const [flows, setFlows] = React.useState<McpFlow[]>([]);
  const [total, setTotal] = React.useState(0);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [moreLoading, setMoreLoading] = React.useState(false);
  const [flowError, setFlowError] = React.useState("");
  const [checked, setChecked] = React.useState<string[]>([]);
  const [detail, setDetail] = React.useState<McpFlow | null>(null);
  const [detailRevealed, setDetailRevealed] = React.useState(false);
  const [revealChoice, setRevealChoice] = React.useState<{ id: string | null; revealed: boolean }>({ id: selectedId, revealed: false });
  const revealed = revealChoice.id === selectedId && revealChoice.revealed;
  React.useEffect(() => { setRevealChoice({ id: selectedId, revealed: false }); }, [selectedId, active]);
  const [detailLoading, setDetailLoading] = React.useState(false);
  const [detailError, setDetailError] = React.useState("");
  const [replayOpen, setReplayOpen] = React.useState(false);
  const [detailRevision, setDetailRevision] = React.useState(0);
  const selectedRef = React.useRef(selectedId); selectedRef.current = selectedId;
  const filterKey = `${task.id}:${debounced}:${kind}:${source}`;
  const filterRef = React.useRef("");
  const paginationController = React.useRef<AbortController | null>(null);
  const loadedExtraRef = React.useRef(false);
  React.useEffect(() => { const timer = setTimeout(() => setDebounced(query.trim()), 250); return () => clearTimeout(timer); }, [query]);

  React.useEffect(() => {
    if (!active) return;
    const changed = filterRef.current !== filterKey;
    if (changed) { filterRef.current = filterKey; setFlows([]); setChecked([]); setLoading(true); setNextCursor(null); loadedExtraRef.current = false; paginationController.current?.abort(); setMoreLoading(false); }
    let disposed = false; let timer: ReturnType<typeof setTimeout>; let controller: AbortController | null = null;
    const load = async () => {
      if (disposed || document.hidden) return;
      controller?.abort(); const current = new AbortController(); controller = current;
      const timeout = setTimeout(() => current.abort(), 10000);
      try {
        const result = await mcpApi.flows(task.id, { q: debounced, kind, source, limit: 50 }, current.signal);
        if (disposed || controller !== current || current.signal.aborted) return;
        setFlows(previous => { if (!loadedExtraRef.current) return result.flows; const merged = new Map(previous.map(flow => [flow.id, flow])); result.flows.forEach(flow => merged.set(flow.id, flow)); return [...merged.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)); });
        setTotal(result.total); if (!loadedExtraRef.current) setNextCursor(result.next_cursor || null); setFlowError("");
        if (!selectedRef.current && result.flows.length) onSelect(result.flows[0].id);
      } catch (e) { if (!disposed && controller === current && !document.hidden) setFlowError(current.signal.aborted ? c("流量更新逾時；已保留畫面中的資料。", "Traffic update timed out; existing rows are retained.") : mcpError(e)); }
      finally { clearTimeout(timeout); if (!disposed && controller === current) { setLoading(false); if (live) timer = setTimeout(() => void load(), POLL_MS); } }
    };
    const visibility = () => { clearTimeout(timer); if (document.hidden) controller?.abort(); else void load(); };
    void load(); document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id, filterKey, debounced, kind, source, refresh, live, active]);

  React.useEffect(() => {
    if (!selectedId || !active) return;
    let disposed = false; let timer: ReturnType<typeof setTimeout>; let controller: AbortController | null = null;
    setDetailLoading(true); setDetailError(""); setDetail(previous => previous?.id === selectedId ? previous : null);
    const load = async () => {
      if (disposed || document.hidden) return;
      controller?.abort(); const current = new AbortController(); controller = current;
      const timeout = setTimeout(() => current.abort(), 10000);
      try { const result = await mcpApi.flow(task.id, selectedId, current.signal, revealed); if (disposed || controller !== current || current.signal.aborted) return; setDetail(result.flow); setDetailRevealed(revealed); setDetailError(""); if (live && !result.flow.error && (!result.flow.response || ["request_headers", "request", "response_headers"].includes(result.flow.stage || ""))) timer = setTimeout(() => void load(), POLL_MS); }
      catch (e) { if (!disposed && controller === current && !document.hidden) setDetailError(current.signal.aborted ? c("讀取內容逾時，請重新選取請求。", "Loading content timed out. Select the request again.") : mcpError(e)); }
      finally { clearTimeout(timeout); if (!disposed && controller === current) setDetailLoading(false); }
    };
    const visibility = () => { clearTimeout(timer); if (document.hidden) controller?.abort(); else void load(); };
    void load(); document.addEventListener("visibilitychange", visibility);
    return () => { disposed = true; clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, task.id, detailRevision, live, active, revealed]);

  React.useEffect(() => () => paginationController.current?.abort(), []);
  const loadMore = async () => {
    if (!nextCursor || moreLoading) return;
    const key = filterKey; const controller = new AbortController(); paginationController.current = controller; setMoreLoading(true);
    try { const result = await mcpApi.flows(task.id, { q: debounced, kind, source, limit: 50, after: nextCursor }, controller.signal); if (controller.signal.aborted || filterRef.current !== key) return; loadedExtraRef.current = true; setFlows(old => [...new Map([...old, ...result.flows].map(flow => [flow.id, flow])).values()]); setNextCursor(result.next_cursor || null); setTotal(result.total); }
    catch (e) { if (!controller.signal.aborted) setFlowError(mcpError(e)); } finally { if (!controller.signal.aborted) setMoreLoading(false); }
  };
  const grouped = React.useMemo(() => {
    const result = new Map<string, McpEndpoint[]>();
    endpoints.filter(endpoint => !kind || endpoint.kind === kind).forEach(endpoint => result.set(endpoint.host, [...(result.get(endpoint.host) || []), endpoint]));
    return [...result.entries()];
  }, [endpoints, kind]);
  const mutable = !["ended", "ending", "starting", "stopping", "deleting", "delete_failed"].includes(task.status);
  return <>
    <div className={styles.trafficToolbar}><div className={styles.searchField}><Search size={15} /><input aria-label={c("搜尋流量", "Search traffic")} value={query} onChange={e => setQuery(e.target.value)} placeholder={c("搜尋 URL、主機或請求…", "Search URL, host, or request…")} /></div><label className={styles.sourceFilter}><span>{c("來源", "Source")}</span><select value={source} onChange={e => setSource(e.target.value)} aria-label={c("請求來源", "Request source")}><option value="">{c("全部來源", "All sources")}</option><option value="user">{c("瀏覽器", "Browser")}</option><option value="replay">{c("重送", "Replay")}</option><option value="agent">Agent</option></select></label><button className={styles.secondaryButton} disabled={!checked.length || !mutable} onClick={() => onTest(checked)}><FlaskConical size={14} />{c("測試選取", "Test selected")}{checked.length > 0 && <span>{checked.length}</span>}</button></div>
    {flowError && <McpError onRetry={onRefresh}>{flowError}</McpError>}
    <div className={styles.trafficGrid}>
      <aside className={styles.siteTree}><div className={styles.treeHeading}><Globe2 size={14} /><h3>{c("已觀察的網站", "Observed sites")}</h3></div><div className={styles.kindFilters}>{[{ id: "", label: c("全部請求", "All traffic"), count: task.counts?.flows }, { id: "page", label: c("頁面", "Pages"), count: task.counts?.pages }, { id: "api", label: "API", count: task.counts?.apis }, { id: "asset", label: c("靜態資源", "Assets") }, { id: "other", label: c("其他", "Other") }].map(item => <button key={item.id} type="button" aria-pressed={kind === item.id} onClick={() => setKind(item.id)}><span>{item.label}</span>{item.count != null && <small>{item.count}</small>}</button>)}</div><div className={styles.hostInventory}>{grouped.map(([host, entries]) => <details key={host} open><summary title={host}><Globe2 size={12} /><span>{host}</span><small>{entries.length}</small></summary><div>{entries.map((endpoint, index) => <button type="button" key={endpoint.id || `${endpoint.method}:${endpoint.path}:${index}`} className={styles.endpoint} title={`${endpoint.method} ${endpoint.host}${endpoint.path}`} aria-label={`${endpoint.method} ${endpoint.host}${endpoint.path}`} onClick={() => { setQuery(`${host}${endpoint.path.split("{id}")[0]}`); if (endpoint.latest_flow_id) onSelect(endpoint.latest_flow_id); }}><span className={styles.endpointMethod}>{endpoint.method}</span><span>{endpoint.path}</span><small>{endpoint.count ?? endpoint.flow_count ?? ""}</small></button>)}</div></details>)}{!grouped.length && <p className={styles.treeEmpty}>{c("有流量後，這裡會建立頁面與 API 目錄。", "Pages and APIs appear here as traffic arrives.")}</p>}</div><div className={styles.treeFootnote}><Layers3 size={12} />{c("端點由已觀察路徑分組", "Grouped from observed paths")}</div></aside>
      <section className={styles.flowList} aria-label={c("捕獲的請求", "Captured requests")}><div className={styles.flowListHeader}><label><input type="checkbox" aria-label={c("選取目前列表的所有請求", "Select all visible requests")} checked={flows.length > 0 && flows.every(flow => checked.includes(flow.id))} onChange={e => setChecked(e.target.checked ? flows.map(flow => flow.id) : [])} /><span>{c("請求", "Requests")}</span></label><span>{flows.length} / {total}</span></div>
        {!flows.length ? <div className={styles.flowEmpty}>{loading ? <LoaderCircle size={24} className={styles.spin} /> : <Network size={26} />}<h3>{loading ? c("讀取流量…", "Loading traffic…") : query || kind || source ? c("沒有符合的請求", "No matching requests") : c("等待第一筆流量", "Waiting for traffic")}</h3><p>{query || kind || source ? c("調整搜尋或篩選條件。", "Adjust your search or filters.") : c("將瀏覽器連到代理，再開啟範圍內的網站。", "Connect your browser to the proxy, then open a site in scope.")}</p></div> : <div className={styles.flowRows}>{flows.map(flow => <div className={styles.flowRow} key={flow.id} data-selected={selectedId === flow.id}><label className={styles.flowCheckbox}><input aria-label={c(`選取請求 ${flow.method} ${flow.path}`, `Select request ${flow.method} ${flow.path}`)} type="checkbox" checked={checked.includes(flow.id)} onChange={e => setChecked(old => e.target.checked ? [...old, flow.id] : old.filter(id => id !== flow.id))} /></label><button type="button" className={styles.flowSelect} title={`${flow.method} ${flow.url}`} aria-label={`${flow.method} ${flow.url}`} aria-pressed={selectedId === flow.id} onClick={() => { onSelect(flow.id); if (selectedId === flow.id) setDetailRevision(value => value + 1); }}><span className={styles.method} data-method={flow.method}>{flow.method}</span><span className={styles.flowIdentity}><span>{flow.path || "/"}</span><small>{flow.host} · {dateText(flow.created_at, true)}</small></span><span className={styles.flowState}><span data-error={Boolean(flow.error) || Number(flow.status_code) >= 400}>{flow.status_code ?? (flow.error ? "ERR" : "…")}</span><small>{flow.source === "user" ? "Browser" : flow.source === "replay" ? "Replay" : "Agent"}</small></span></button></div>)}</div>}
        {nextCursor && <button className={styles.loadMore} disabled={moreLoading} onClick={() => void loadMore()}>{moreLoading ? c("讀取中…", "Loading…") : c("載入較早的請求", "Load earlier requests")}<ChevronDown size={13} /></button>}
      </section>
      <FlowDetail flow={active && detail?.id === selectedId && detailRevealed === revealed ? detail : null} revealed={revealed} onToggleReveal={() => { setDetail(null); setDetailLoading(true); setRevealChoice({ id: selectedId, revealed: !revealed }); }} loading={detailLoading} error={detailError} mutable={mutable} onReplay={() => setReplayOpen(true)} onTest={() => detail && onTest([detail.id])} onSelectFlow={id => onSelect(id)} onTests={onTests} />
    </div>
    {replayOpen && detail && mutable && <ReplayForm taskId={task.id} flow={detail} onClose={() => setReplayOpen(false)} onReplayed={flow => { setReplayOpen(false); setDetail(flow); setDetailRevealed(false); setRevealChoice({ id: flow.id, revealed: false }); onSelect(flow.id); setSource(""); setQuery(""); setKind(""); onRefresh(); }} />}
  </>;
}
