"use client";

import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import Link from "next/link";
import { ChevronRight, FlaskConical, Loader2, RotateCw, Search, Square, X } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import {
  fetchPromptProbeCatalog, PromptProbeError, runPromptProbe,
  type PromptProbeCatalog, type PromptProbeItem, type PromptProbeKind,
  type PromptProbeResult, type PromptProbeRoute, type PromptProbeStatus,
} from "@/lib/prompt-probes";
import styles from "./PromptProbeDialog.module.css";

type RowStatus = PromptProbeStatus | "queued" | "testing" | "cancelled";
type FilterStatus = RowStatus | "untested" | "all";
interface ProbeRow { status: RowStatus; result?: PromptProbeResult; code?: string; message?: string }
export interface PromptProbeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialSelection?: { kind: PromptProbeKind; name: string } | null;
}

const COPY = {
  "zh-CN": {
    title: "Prompt 拒绝测试", subtitle: "用你指定的任务，检查各个 Prompt／技能的模型回应。",
    boundary: "每次请求将一个已保存片段作为系统指令，搭配下方任务原文。结果只判断本次回应是否出现拒绝信号；任务是否完成请核对回覆。这里不提供工具，也不组装完整扫描上下文或展开模板变量。",
    task: "测试任务（必填）", taskPlaceholder: "输入实际想测试的请求。请提供所需上下文；每个选中项目都会收到相同的任务。",
    taskHint: "任务原文会作为用户消息送出，不再附加“说明片段”的固定问题。修改任务会清空旧结果。",
    taskRequired: "请先输入测试任务。", taskLimit: "测试任务超出字数上限。", taskHash: "任务 SHA-256", testedTask: "本次任务原文",
    truncated: "回覆超过显示上限，以下内容已截断。", redacted: "回覆保留模型原文，凭证样式的内容会遮蔽。",
    route: "测试模型路由", web: "Web 扫描", internal: "内网扫描", defaultEffort: "服务商默认", noRoutes: "尚无可用的已保存模型路由。", settings: "前往设置", refresh: "刷新清单", loading: "正在读取已保存的 Prompt、技能与路由…",
    scope: "内容范围", all: "全部", prompts: "Prompts", skills: "技能", search: "搜索名称或路径…", searchLabel: "搜索测试项目", filter: "筛选测试状态", selectAll: "全选筛选结果", clear: "清空选择", selected: (count: number) => `已选择 ${count} 项`, usage: (count: number) => `开始后最多发送 ${count} 个模型请求，逐项执行，每项一次，不自动重试；会产生实际模型用量。`, empty: "没有符合筛选条件的项目。",
    start: "开始测试", stop: "停止测试", close: "关闭", progress: (done: number, total: number) => `${done} / ${total} 项已结束`, running: "测试中", ready: "等待开始", done: "本次测试已结束", stopped: "已停止后续测试，已完成的结果保留。", cancelHint: "停止或关闭窗口会取消后续测试；已发送的请求仍可能产生用量。", session: "结果仅保留在本次窗口；修改任务、切换路由、刷新或重新打开会清空。",
    changed: "Prompt 或模型路由已变化，批次已停止。请刷新清单后重新选择测试。", error: "无法完成请求，请检查控制台服务。", response: "模型原始回覆", diagnostics: "诊断信息", code: "结果代码", checked: "测试时间", duration: "耗时", hash: "原文 SHA-256", routeHash: "路由指纹", version: "测试版本", details: "展开查看结果与诊断", noResult: "尚无模型响应结果。", possible: "无法判定 · 疑似拒绝", elapsed: (ms: number) => `${(ms / 1000).toFixed(1)} 秒`,
    policy: "服务商明确回报 cyber_policy，剩余测试已取消。这可能是路由或账号的安全工作访问限制，请先确认服务商返回的原因。",
    batchError: "请求失败，剩余测试已取消。已完成结果与错误详情保留在下方。",
    statuses: { all: "全部状态", untested: "未测试", queued: "待测试", testing: "测试中", responded: "未侦测到拒绝", refused: "模型拒绝", blocked: "政策阻挡", error: "请求错误", inconclusive: "无法判定", cancelled: "已取消" },
    shortStatuses: { responded: "未侦测到拒绝", refused: "拒绝", blocked: "阻挡", error: "错误", inconclusive: "未定", cancelled: "取消" },
    errors: { endpoint_missing: "当前后端尚未提供 Prompt 测试接口，请更新并重启控制台后端。", console_connection: "无法连接控制台后端，请确认服务正在运行。", console_invalid_response: "控制台后端返回了无效的测试结果，请检查服务。", console_http_error: "控制台请求失败，请检查服务。", prompt_changed: "原文已变化，请刷新清单。", content_changed: "原文已变化，请刷新清单。", route_changed: "模型路由已变化，请刷新清单。", probe_busy: "控制台已有其他诊断请求正在运行，请稍后手动开始测试。" } as Record<string, string>,
  },
  en: {
    title: "Prompt refusal test", subtitle: "Check model responses to your task with each saved prompt or skill.",
    boundary: "Each request sends one saved fragment as system instructions with your task below, unchanged. Results report refusal signals in this response; review the reply for task completion. No tools, full scan context, or expanded template variables are provided.",
    task: "Test task (required)", taskPlaceholder: "Enter the request you want to test, including its context. Every selected item receives this same task.",
    taskHint: "Your exact task is sent as the user message. No fixed fragment-description question is added. Editing the task clears previous results.",
    taskRequired: "Enter a test task to start.", taskLimit: "The test task exceeds the character limit.", taskHash: "Task SHA-256", testedTask: "Task sent",
    truncated: "The reply exceeded the display limit and is truncated below.", redacted: "The model's original text is shown with credential-like content redacted.",
    route: "Model route to test", web: "Web scanning", internal: "Internal scanning", defaultEffort: "Provider default", noRoutes: "No saved model routes are available.", settings: "Open settings", refresh: "Refresh catalog", loading: "Loading saved prompts, skills, and routes…",
    scope: "Content scope", all: "All", prompts: "Prompts", skills: "Skills", search: "Search names or paths…", searchLabel: "Search test items", filter: "Filter test status", selectAll: "Select filtered items", clear: "Clear selection", selected: (count: number) => `${count} selected`, usage: (count: number) => `Start sends up to ${count} model requests, sequentially, once per item with no automatic retries. Actual model usage is incurred.`, empty: "No items match these filters.",
    start: "Start test", stop: "Stop test", close: "Close", progress: (done: number, total: number) => `${done} / ${total} items finished`, running: "Testing", ready: "Ready to start", done: "This test has finished", stopped: "Remaining tests stopped. Completed results have been kept.", cancelHint: "Stopping or closing cancels remaining tests. Requests already sent may still incur usage.", session: "Results stay in this dialog only. Editing tasks, changing routes, refreshing, or reopening clears them.",
    changed: "The prompt or model route changed, so this batch stopped. Refresh the catalog and select your tests again.", error: "The request could not be completed. Check the console backend.", response: "Original model reply", diagnostics: "Diagnostics", code: "Result code", checked: "Checked at", duration: "Duration", hash: "Source SHA-256", routeHash: "Route fingerprint", version: "Probe version", details: "Expand result and diagnostics", noResult: "No model response is available yet.", possible: "Inconclusive · possible refusal", elapsed: (ms: number) => `${(ms / 1000).toFixed(1)} s`,
    policy: "The provider explicitly returned cyber_policy, so remaining tests were cancelled. This may be a route or account access restriction for security work. Check the provider's reported reason first.",
    batchError: "The request failed and remaining tests were cancelled. Completed results and error details are kept below.",
    statuses: { all: "All statuses", untested: "Not tested", queued: "Queued", testing: "Testing", responded: "No refusal observed", refused: "Model refusal", blocked: "Policy block", error: "Request error", inconclusive: "Inconclusive", cancelled: "Cancelled" },
    shortStatuses: { responded: "No refusal observed", refused: "Refused", blocked: "Blocked", error: "Error", inconclusive: "Inconclusive", cancelled: "Cancelled" },
    errors: { endpoint_missing: "This backend does not provide prompt testing yet. Update and restart the console backend.", console_connection: "Cannot connect to the console backend. Check that it is running.", console_invalid_response: "The console returned an invalid test result. Check the backend service.", console_http_error: "The console request failed. Check the backend service.", prompt_changed: "The saved text changed. Refresh the catalog.", content_changed: "The saved text changed. Refresh the catalog.", route_changed: "The model route changed. Refresh the catalog.", probe_busy: "Other diagnostics are already running. Start this test manually later." } as Record<string, string>,
  },
};

const itemKey = (item: { kind: PromptProbeKind; name: string }) => `${item.kind}:${item.name}`;
const routeKey = (route: PromptProbeRoute) => `${route.profile_id}:${route.scan_type}`;
const finalStatuses = ["responded", "refused", "blocked", "error", "inconclusive", "cancelled"] as const;
const filterStatuses: FilterStatus[] = ["all", "untested", "queued", "testing", ...finalStatuses];

export function PromptProbeDialog({ open, onOpenChange, initialSelection }: PromptProbeDialogProps) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  const [catalog, setCatalog] = React.useState<PromptProbeCatalog | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [routeId, setRouteId] = React.useState("");
  const [testTask, setTestTask] = React.useState("");
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [scope, setScope] = React.useState<PromptProbeKind | "all">("prompt");
  const [query, setQuery] = React.useState("");
  const [filter, setFilter] = React.useState<FilterStatus>("all");
  const [rows, setRows] = React.useState<Record<string, ProbeRow>>({});
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState<"stopped" | "changed" | "policy" | "batchError" | null>(null);
  const [loadError, setLoadError] = React.useState<PromptProbeError | null>(null);
  const catalogRequest = React.useRef<AbortController | null>(null);
  const activeRun = React.useRef<AbortController | null>(null);
  const returnFocus = React.useRef<HTMLElement | null>(null);
  const initialRef = React.useRef(initialSelection);
  initialRef.current = initialSelection;
  const routeInputId = React.useId();
  const taskInputId = React.useId();
  const taskHintId = React.useId();
  const statusInputId = React.useId();

  const stop = React.useCallback(() => {
    const controller = activeRun.current;
    if (!controller) return;
    activeRun.current = null;
    controller.abort();
    setRows((previous) => Object.fromEntries(Object.entries(previous).map(([key, row]) => [key,
      row.status === "queued" || row.status === "testing" ? { ...row, status: "cancelled" as const } : row,
    ])));
    setBusy(false);
    setNotice("stopped");
  }, []);

  const refresh = React.useCallback(async () => {
    if (activeRun.current) return;
    catalogRequest.current?.abort();
    const controller = new AbortController();
    catalogRequest.current = controller;
    setLoading(true);
    setBusy(false);
    setLoadError(null);
    setCatalog(null);
    setRows({});
    setNotice(null);
    setFilter("all");
    try {
      const next = await fetchPromptProbeCatalog(controller.signal);
      if (controller.signal.aborted || catalogRequest.current !== controller) return;
      const requested = initialRef.current;
      const matching = requested && next.items.find((item) => itemKey(item) === itemKey(requested));
      setCatalog(next);
      setSelected(new Set((matching ? [matching] : next.items.filter((item) => item.kind === "prompt")).map(itemKey)));
      setScope(matching?.kind ?? "prompt");
      const defaultRoute = next.routes.find((candidate) => candidate.profile_id === next.active_profile_id && candidate.scan_type === "web")
        ?? next.routes.find((candidate) => candidate.profile_id === next.active_profile_id) ?? next.routes[0];
      setRouteId(defaultRoute ? routeKey(defaultRoute) : "");
    } catch (error) {
      if (controller.signal.aborted || catalogRequest.current !== controller) return;
      setLoadError(error instanceof PromptProbeError ? error : new PromptProbeError("console_connection", "Cannot load the catalog."));
    } finally {
      if (catalogRequest.current === controller) {
        catalogRequest.current = null;
        setLoading(false);
      }
    }
  }, []);

  React.useEffect(() => {
    if (open) {
      setQuery("");
      void refresh();
    } else {
      stop();
      catalogRequest.current?.abort();
      catalogRequest.current = null;
    }
    return () => {
      catalogRequest.current?.abort();
      catalogRequest.current = null;
    };
  }, [open, refresh, stop]);

  React.useEffect(() => () => {
    catalogRequest.current?.abort();
    catalogRequest.current = null;
    activeRun.current?.abort();
    activeRun.current = null;
  }, []);

  const changeOpen = (next: boolean) => {
    if (!next) stop();
    onOpenChange(next);
  };
  const route = catalog?.routes.find((candidate) => routeKey(candidate) === routeId);
  const items = catalog?.items ?? [];
  const selectedItems = items.filter((item) => selected.has(itemKey(item)));
  const taskLength = Array.from(testTask).length;
  const taskValid = Boolean(testTask.trim()) && Boolean(catalog && taskLength <= catalog.max_task_chars);
  const normalizedQuery = query.trim().toLowerCase();
  const visibleItems = items.filter((item) => (scope === "all" || item.kind === scope)
    && item.name.toLowerCase().includes(normalizedQuery)
    && (filter === "all" || (rows[itemKey(item)]?.status ?? "untested") === filter));
  const counts = Object.fromEntries(finalStatuses.map((status) => [status, Object.values(rows).filter((row) => row.status === status).length])) as Record<typeof finalStatuses[number], number>;
  const total = Object.keys(rows).length;
  const finished = Object.values(counts).reduce((sum, value) => sum + value, 0);
  const apiLabel = (value: string) => value === "responses" ? "Responses" : value === "chat_completions" ? "Chat Completions" : value;
  const effortLabel = (value: string) => value === "default" ? copy.defaultEffort : value;
  const routeLabel = (value: PromptProbeRoute) => `${value.profile_name} · ${value.scan_type === "web" ? copy.web : copy.internal} · ${value.model}`;

  const start = async () => {
    if (activeRun.current || !route || !selectedItems.length || !catalog || loading || !taskValid || notice === "changed") return;
    const controller = new AbortController();
    activeRun.current = controller;
    const batch = [...selectedItems];
    const batchTask = testTask;
    setBusy(true);
    setNotice(null);
    setFilter("all");
    setRows(Object.fromEntries(batch.map((item) => [itemKey(item), { status: "queued" as const }])));
    try {
      for (const item of batch) {
        if (controller.signal.aborted || activeRun.current !== controller) break;
        const key = itemKey(item);
        setRows((previous) => ({ ...previous, [key]: { status: "testing" } }));
        try {
          const result = await runPromptProbe({ kind: item.kind, name: item.name, sha256: item.sha256,
            profile_id: route.profile_id, scan_type: route.scan_type, route_fingerprint: route.route_fingerprint,
            test_task: batchTask }, controller.signal);
          if (controller.signal.aborted || activeRun.current !== controller) break;
          setRows((previous) => ({ ...previous, [key]: { status: result.status, result } }));
          if (result.status === "blocked" && result.code === "cyber_policy") {
            setNotice("policy");
            setRows((previous) => Object.fromEntries(Object.entries(previous).map(([rowKey, row]) => [rowKey,
              row.status === "queued" ? { ...row, status: "cancelled" as const } : row,
            ])));
            break;
          }
        } catch (error) {
          if (controller.signal.aborted || activeRun.current !== controller) break;
          const failure = error instanceof PromptProbeError ? error : new PromptProbeError("console_connection", "Cannot complete the request.");
          setRows((previous) => ({ ...previous, [key]: { status: "error", code: failure.code, message: failure.message } }));
          setNotice(failure.status === 409 || ["prompt_changed", "content_changed", "route_changed"].includes(failure.code) ? "changed" : "batchError");
          setRows((previous) => Object.fromEntries(Object.entries(previous).map(([rowKey, row]) => [rowKey,
            row.status === "queued" ? { ...row, status: "cancelled" as const } : row,
          ])));
          break;
        }
      }
    } finally {
      if (activeRun.current === controller) {
        activeRun.current = null;
        setBusy(false);
      }
    }
  };

  const toggle = (item: PromptProbeItem) => setSelected((previous) => {
    const next = new Set(previous);
    const key = itemKey(item);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  return (
    <Dialog.Root open={open} onOpenChange={changeOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content className={styles.dialog}
          onOpenAutoFocus={() => { returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; }}
          onCloseAutoFocus={(event) => { if (returnFocus.current?.isConnected) { event.preventDefault(); returnFocus.current.focus(); } }}>
          <header className={styles.header}>
            <span className={styles.headerIcon}><FlaskConical size={21} /></span>
            <div className={styles.heading}>
              <Dialog.Title className={styles.title}>{copy.title}</Dialog.Title>
              <Dialog.Description className={styles.subtitle}>{copy.subtitle}</Dialog.Description>
            </div>
            <Dialog.Close className={styles.close} aria-label={copy.close}><X size={19} /></Dialog.Close>
          </header>

          <div className={styles.body}>
            <div className={styles.boundary}>
              <p>{copy.boundary}</p>
            </div>

            {loading && <p className={styles.empty} role="status"><Loader2 size={17} className={styles.spin} />{copy.loading}</p>}
            {loadError && <div className={styles.error} role="alert">
              <p>{copy.errors[loadError.code] ?? loadError.message ?? copy.error}</p>
              <button type="button" className={styles.textButton} onClick={() => void refresh()}><RotateCw size={14} />{copy.refresh}</button>
            </div>}

            {catalog && <>
              <section className={styles.routeSection}>
                <div className={styles.sectionHeading}>
                  <label htmlFor={routeInputId}>{copy.route}</label>
                  <button type="button" className={styles.textButton} disabled={busy} onClick={() => void refresh()}><RotateCw size={13} />{copy.refresh}</button>
                </div>
                {catalog.routes.length ? <>
                  <select id={routeInputId} className={styles.input} value={routeId} disabled={busy} onChange={(event) => {
                    setRouteId(event.target.value); setRows({}); setNotice(null); setFilter("all");
                  }}>
                    {catalog.routes.map((candidate) => <option key={routeKey(candidate)} value={routeKey(candidate)}>
                      {routeLabel(candidate)} · {apiLabel(candidate.api_mode)} · {effortLabel(candidate.reasoning_effort)}
                    </option>)}
                  </select>
                  {route && <p className={styles.routeMeta}>{route.model} · {apiLabel(route.api_mode)} · {effortLabel(route.reasoning_effort)}</p>}
                </> : <p className={styles.hint}>{copy.noRoutes} <Link href="/settings" onClick={() => changeOpen(false)}>{copy.settings}</Link></p>}
              </section>

              <section className={styles.taskSection}>
                <div className={styles.sectionHeading}>
                  <label htmlFor={taskInputId}>{copy.task}</label>
                  <span className={styles.hint}>{taskLength.toLocaleString()} / {catalog.max_task_chars.toLocaleString()}</span>
                </div>
                <textarea id={taskInputId} className={`${styles.input} ${styles.taskInput}`} value={testTask}
                  rows={4} disabled={busy} required placeholder={copy.taskPlaceholder} aria-describedby={taskHintId}
                  aria-invalid={taskLength > catalog.max_task_chars}
                  onChange={(event) => {
                    setTestTask(event.target.value); setRows({}); setFilter("all");
                    setNotice((previous) => previous === "changed" ? previous : null);
                  }} />
                <p id={taskHintId} className={styles.hint}>{copy.taskHint}</p>
                {!testTask.trim() && <p className={styles.hint}>{copy.taskRequired}</p>}
                {taskLength > catalog.max_task_chars && <p className={styles.error} role="alert">{copy.taskLimit}</p>}
              </section>

              <section className={styles.itemsSection}>
                <div className={styles.toolbar}>
                  <div className={styles.scope} role="group" aria-label={copy.scope}>
                    {(["prompt", "skill", "all"] as const).map((value) => <button key={value} type="button" aria-pressed={scope === value} disabled={busy} onClick={() => setScope(value)}>
                      {value === "prompt" ? copy.prompts : value === "skill" ? copy.skills : copy.all}
                    </button>)}
                  </div>
                  <div className={styles.search}><Search size={15} /><input value={query} placeholder={copy.search} aria-label={copy.searchLabel} onChange={(event) => setQuery(event.target.value)} /></div>
                </div>
                <div className={styles.selectionBar}>
                  <div className={styles.selectionActions}>
                    <span>{copy.selected(selectedItems.length)}</span>
                    <button type="button" className={styles.textButton} disabled={busy || !visibleItems.length} onClick={() => setSelected((previous) => new Set([...previous, ...visibleItems.map(itemKey)]))}>{copy.selectAll}</button>
                    <button type="button" className={styles.textButton} disabled={busy || !selectedItems.length} onClick={() => setSelected(new Set())}>{copy.clear}</button>
                  </div>
                  <select id={statusInputId} aria-label={copy.filter} className={styles.statusFilter} value={filter} onChange={(event) => setFilter(event.target.value as FilterStatus)}>
                    {filterStatuses.map((status) => <option key={status} value={status}>{copy.statuses[status]}</option>)}
                  </select>
                </div>

                {total > 0 && <div className={styles.progressBlock}>
                  <div className={styles.progressHeading} role="status" aria-live="polite"><span>{busy ? copy.running : copy.done}</span><span>{copy.progress(finished, total)}</span></div>
                  <progress className={styles.progress} value={finished} max={total} aria-label={copy.progress(finished, total)} />
                  <div className={styles.counts}>{finalStatuses.map((status) => <span key={status} data-status={status}><b>{counts[status]}</b>{copy.shortStatuses[status]}</span>)}</div>
                </div>}
                {notice && <p className={notice === "stopped" ? styles.notice : styles.error} role="status">{copy[notice]}</p>}

                <div className={styles.items} aria-label={copy.scope}>
                  {!visibleItems.length && <p className={styles.empty}>{copy.empty}</p>}
                  {visibleItems.map((item) => {
                    const key = itemKey(item);
                    const row = rows[key];
                    const status = row?.status ?? "untested";
                    const result = row?.result;
                    const code = result?.code ?? row?.code;
                    const message = result?.message ?? (row?.code && copy.errors[row.code]) ?? row?.message;
                    return <div className={styles.item} key={key} data-status={status}>
                      <input className={styles.checkbox} type="checkbox" aria-label={`${copy.statuses[status]}: ${item.name}`} checked={selected.has(key)} disabled={busy} onChange={() => toggle(item)} />
                      <details className={styles.itemDetails}>
                        <summary title={copy.details}>
                          <ChevronRight size={14} className={styles.chevron} />
                          <span className={styles.itemName}><strong>{item.name}</strong><small>{item.kind === "prompt" ? "PROMPT" : "SKILL"} · {item.size.toLocaleString()} B</small></span>
                          <span className={styles.badge} data-status={status}>{status === "testing" && <Loader2 size={12} className={styles.spin} />}{code === "possible_refusal" ? copy.possible : copy.statuses[status]}</span>
                        </summary>
                        <div className={styles.resultDetails}>
                          {message && <p>{message}</p>}
                          {!result && !message && <p className={styles.hint}>{copy.noResult}</p>}
                          {result && <div className={styles.excerpt}><span>{copy.testedTask}</span><pre>{result.test_task}</pre></div>}
                          {result?.response_text && <div className={styles.excerpt}>
                            <span>{copy.response}</span><pre>{result.response_text}</pre>
                            <p className={styles.hint}>{copy.redacted}</p>
                          </div>}
                          {result?.response_truncated && <p className={styles.hint}>{copy.truncated}</p>}
                          {result?.diagnostics && <div className={styles.excerpt}><span>{copy.diagnostics}</span><pre>{result.diagnostics}</pre></div>}
                          <dl className={styles.metadata}>
                            {code && <><dt>{copy.code}</dt><dd>{code}</dd></>}
                            <dt>{copy.hash}</dt><dd>{item.sha256}</dd>
                            {result && <>
                              <dt>{copy.taskHash}</dt><dd>{result.task_sha256}</dd>
                              <dt>{copy.route}</dt><dd>{result.model} · {result.scan_type === "web" ? copy.web : copy.internal} · {apiLabel(result.api_mode)} · {effortLabel(result.reasoning_effort)}</dd>
                              <dt>{copy.routeHash}</dt><dd>{result.route_fingerprint}</dd>
                              <dt>{copy.checked}</dt><dd>{new Date(result.checked_at).toLocaleString(locale)}</dd>
                              <dt>{copy.duration}</dt><dd>{copy.elapsed(result.duration_ms)}</dd>
                              <dt>{copy.version}</dt><dd>{result.probe_version}</dd>
                            </>}
                          </dl>
                        </div>
                      </details>
                    </div>;
                  })}
                </div>
                <p className={styles.hint}>{copy.usage(selectedItems.length)}</p>
              </section>
            </>}
          </div>

          <footer className={styles.footer}>
            <div><p>{copy.cancelHint}</p><p>{copy.session}</p></div>
            <div className={styles.actions}>
              <Dialog.Close className="button-secondary button-compact">{copy.close}</Dialog.Close>
              {busy ? <button type="button" className="button-secondary button-compact" onClick={stop}><Square size={14} />{copy.stop}</button>
                : <button type="button" className="button-primary button-compact" disabled={loading || !route || !selectedItems.length || !taskValid || notice === "changed"} onClick={() => void start()}><FlaskConical size={15} />{copy.start}</button>}
            </div>
          </footer>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
