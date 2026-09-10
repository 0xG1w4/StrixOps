"use client";

import * as React from "react";
import { isMcpTestLive, type McpModelRound, type McpTest, type McpTestEvent } from "@/lib/mcp-api";
import { dateText, McpStatus, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

type Copy = ReturnType<typeof useMcpCopy>;
const numeric = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
const seconds = (value?: number | null) => numeric(value) ? `${value < 10 ? Math.round(value * 10) / 10 : Math.round(value)} s` : "—";
const tokens = (value?: number | null) => numeric(value) && Number.isInteger(value) ? value.toLocaleString() : "—";
const TOOL_NAMES = new Set(["list_selected_requests", "inspect_request", "replay_request", "list_skills", "load_skill", "record_coverage", "create_vulnerability_report", "finish_request_test"]);
const toolNames = (names?: string[]) => [...new Set((Array.isArray(names) ? names : []).filter(name => TOOL_NAMES.has(name)))];
const API_MODES = new Set(["auto", "chat_completions", "responses"]);
const REASONING_EFFORTS = new Set(["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]);

function modelSettings(detail: { api_mode?: string; reasoning_effort?: string; output_limit?: number | null }, c: Copy) {
  const fields: string[] = [];
  if (detail.api_mode && API_MODES.has(detail.api_mode)) fields.push(`API: ${detail.api_mode}`);
  if (detail.reasoning_effort && REASONING_EFFORTS.has(detail.reasoning_effort)) fields.push(`${c("推理強度", "Reasoning effort")}: ${detail.reasoning_effort === "default" ? c("模型服務預設", "Model service default") : detail.reasoning_effort}`);
  if (detail.output_limit === null || numeric(detail.output_limit)) fields.push(`${c("輸出上限", "Output limit")}: ${detail.output_limit === null ? c("模型服務預設", "Model service default") : tokens(detail.output_limit)}`);
  return fields;
}

function phaseName(phase: string | undefined, c: Copy) {
  const names: Record<string, string> = {
    initialization: c("準備測試", "Preparing test"), model: c("等待模型回覆", "Waiting for model"),
    tool: c("檢視與記錄證據", "Inspecting and recording evidence"), replay: c("重送請求", "Replaying request"),
    wrapup: c("整理部分成果", "Wrapping up results"), completion: c("整理結論", "Finalizing conclusions"),
  };
  return names[phase || ""] || c("執行測試", "Running test");
}

function finishName(row: McpModelRound, c: Copy) {
  if (row.truncated || row.finish_reason === "length") return c("輸出達上限", "Output limit reached");
  const reasons: Record<string, string> = {
    completed: c("回覆完成", "Response complete"), stop: c("回覆完成", "Response complete"),
    tool_calls: c("呼叫工具", "Tool call"), content_filter: c("內容受限", "Content restricted"),
    incomplete: c("回覆未完成", "Response incomplete"), failed: c("回覆失敗", "Response failed"),
    cancelled: c("已中止", "Interrupted"),
  };
  if (row.finish_reason && reasons[row.finish_reason]) return reasons[row.finish_reason];
  const outcomes: Record<string, string> = {
    wrapup_required: c("轉入收尾", "Moved to wrap-up"), time_budget_exceeded: c("時間用盡", "Time limit reached"),
    cancelled: c("已中止", "Interrupted"), failed: c("回合未完成", "Round incomplete"),
  };
  return outcomes[row.outcome || ""] || "—";
}

function modelRounds(test: McpTest): McpModelRound[] {
  const rows = new Map<number, McpModelRound>();
  for (const event of test.events || []) {
    if (!numeric(event.round) || !Number.isInteger(event.round)) continue;
    const previous = rows.get(event.round) || { round: event.round };
    if (event.type?.startsWith("agent.model_")) rows.set(event.round, { ...previous, ...event, round: event.round });
    else if (event.tool && TOOL_NAMES.has(event.tool)) rows.set(event.round, { ...previous, tools: [...new Set([...toolNames(previous.tools), event.tool])] });
  }
  for (const row of test.result?.diagnostics?.rounds || []) {
    if (numeric(row.round) && Number.isInteger(row.round)) rows.set(row.round, { ...rows.get(row.round), ...row });
  }
  return [...rows.values()].sort((a, b) => a.round - b.round);
}

function liveActivity(event: McpTestEvent | undefined, c: Copy) {
  if (event?.type === "agent.model_streaming") {
    if (event.activity === "tool_call") return c("模型正在準備工具呼叫", "Model is preparing a tool call");
    if (event.activity === "text") return c("模型正在輸出回覆", "Model output is arriving");
    if (event.activity === "reasoning") return c("模型正在處理，串流連線正常", "Model is processing; stream is active");
    return c("已接收模型串流", "Receiving the model stream");
  }
  if (event?.tool && TOOL_NAMES.has(event.tool)) return `${c("工具", "Tool")}: ${event.tool}`;
  return phaseName(event?.phase, c);
}

export function budgetExceeded(test: McpTest) {
  return test.result?.completion_reason === "time_budget_exceeded" || test.result?.diagnostics?.reason === "time_budget_exceeded" || test.error?.trim().toLowerCase().startsWith("time budget exceeded");
}

export function TestStatus({ test }: { test: McpTest }) {
  const c = useMcpCopy();
  if (budgetExceeded(test)) return <span className={styles.status} data-tone="danger"><span aria-hidden="true" />{c("時間用盡", "Time limit reached")}</span>;
  if (test.result?.partial && test.status === "completed") return <span className={styles.status} data-tone="muted"><span aria-hidden="true" />{c("部分完成", "Partially complete")}</span>;
  return <McpStatus status={test.status} />;
}

export function TestDiagnostics({ test }: { test: McpTest }) {
  const c = useMcpCopy();
  const live = isMcpTestLive(test);
  const [clock, setClock] = React.useState(Date.now());
  React.useEffect(() => {
    if (!live) return;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [test.id, live]);
  const detail = test.result?.diagnostics;
  const recent = [...(test.events || [])].reverse();
  const event = recent.find(item => item.phase);
  const rounds = modelRounds(test);
  const current = rounds.at(-1);
  const settings = modelSettings({ api_mode: detail?.api_mode ?? current?.api_mode, reasoning_effort: detail?.reasoning_effort ?? current?.reasoning_effort, output_limit: detail?.output_limit !== undefined ? detail.output_limit : current?.output_limit }, c);
  const started = Date.parse(test.started_at || "");
  const elapsed = live && Number.isFinite(started) ? Math.max(0, (clock - started) / 1000) : detail?.elapsed_seconds;
  const eventCount = recent.find(item => numeric(item.request_count))?.request_count;
  const count = live ? eventCount ?? test.requests_used : test.requests_used ?? detail?.request_count ?? eventCount;
  const limited = budgetExceeded(test) || test.result?.completion_reason === "time_budget_limited";
  if (!live && !detail && !limited && !rounds.length) return null;
  return <section className={styles.testDiagnostics} aria-label={c("測試進度與耗時", "Test progress and timing")}>
    <div className={styles.testMeta}>
      <span>{c("已用時間", "Elapsed")}: {seconds(elapsed)}</span>
      <span>{c("已使用重送", "Replays used")}: {count ?? "—"} / {test.config?.max_requests ?? "—"}</span>
      <span>{c("模型回合", "Model rounds")}: {detail?.model_rounds ?? current?.round ?? "—"}</span>
    </div>
    {live && <p className={styles.modelActivity} role="status"><span className={styles.activityDot} aria-hidden="true" />{liveActivity(event, c)}{test.config?.max_seconds != null && elapsed != null ? ` · ${c("剩餘約", "About")} ${seconds(Math.max(0, test.config.max_seconds - elapsed))}${c("", " remaining")}` : ""}</p>}
    {current && <div className={styles.roundSnapshot}>
      <div className={styles.roundHeading}><strong>{c("最新模型回合", "Latest model round")} {current.round}</strong><span>{finishName(current, c)}</span></div>
      <dl className={styles.modelMetrics}>
        <div><dt>{c("首個串流事件", "First stream event")}</dt><dd>{seconds(current.first_event_seconds)}</dd></div>
        <div><dt>{c("首個模型輸出", "First model output")}</dt><dd>{seconds(current.first_output_seconds)}</dd></div>
        <div><dt>{c("輸入 Token", "Input tokens")}</dt><dd>{tokens(current.input_tokens)}</dd></div>
        <div><dt>{c("輸出 Token", "Output tokens")}</dt><dd>{tokens(current.output_tokens)}</dd></div>
        <div><dt>{c("快取輸入 Token", "Cached input tokens")}</dt><dd>{tokens(current.cached_input_tokens)}</dd></div>
        <div><dt>{c("推理 Token", "Reasoning tokens")}</dt><dd>{tokens(current.reasoning_tokens)}</dd></div>
      </dl>
      {(current.truncated || current.finish_reason === "length") && <p className={styles.roundNotice}>{c("此回合輸出被截斷，不能視為完整回覆。", "This round's output was truncated and is not a complete response.")}</p>}
    </div>}
    {limited && <p>{budgetExceeded(test)
      ? c(`測試已達時間上限，當時階段：${phaseName(detail?.phase, c)}。已取得的證據與結果保留，未完成項目需要後續測試。`, `The time limit was reached during: ${phaseName(detail?.phase, c)}. Saved evidence and results remain available; incomplete checks need follow-up.`)
      : c("Agent 已在時間上限前整理部分成果；請查看未完成項目，這不代表所有檢查都已通過。", "The Agent wrapped up partial results before the deadline. Review the remaining checks; this does not mean every check passed.")}</p>}
    {rounds.length > 0 && <details className={styles.roundDetails}><summary>{c("模型回合明細", "Model round details")} <span>{rounds.length}</span></summary>{settings.length > 0 && <p className={styles.metricNote}>{settings.join(" · ")}</p>}<div className={styles.roundRows}>{rounds.map(row => <div className={styles.roundRow} key={row.round}>
      <div className={styles.roundHeading}><strong>{c("回合", "Round")} {row.round}{row.stage === "wrapup" ? ` · ${c("收尾", "Wrap-up")}` : ""}</strong><span>{finishName(row, c)}</span></div>
      <div className={styles.roundMeasures}><span>{c("耗時", "Duration")}: {seconds(row.duration_seconds)}</span><span>{c("首事件／首輸出", "First event / output")}: {seconds(row.first_event_seconds)} / {seconds(row.first_output_seconds)}</span><span>{c("輸入／輸出 Token", "Input / output tokens")}: {tokens(row.input_tokens)} / {tokens(row.output_tokens)}</span><span>{c("推理 Token", "Reasoning tokens")}: {tokens(row.reasoning_tokens)}</span></div>
      {(row.system_chars !== undefined || row.input_chars !== undefined) && <p className={styles.metricNote}>{c("系統／對話字元（不含工具定義）", "System / conversation characters (excluding tool definitions)")}: {tokens(row.system_chars)} / {tokens(row.input_chars)}</p>}
      {toolNames(row.tools).length > 0 && <div className={styles.roundTools}>{toolNames(row.tools).map(name => <code key={name}>{name}</code>)}</div>}
    </div>)}</div><p className={styles.metricNote}>{c("時間從各回合開始計算。首個模型輸出包含推理、文字或工具參數的串流活動。— 表示供應端未回報、尚未收到或舊紀錄未保存，並非 0。", "Timings start at each round. First model output includes reasoning, text, or tool-argument stream activity. — means not reported, not yet received, or unavailable in an older record; it does not mean zero.")}</p></details>}
  </section>;
}

export function TestEvent({ event }: { event: McpTestEvent }) {
  const c = useMcpCopy();
  const names: Record<string, string> = {
    "agent.started": c("開始測試", "Test started"), "agent.finished": c("測試結束", "Test ended"),
    "agent.model_started": c("等待模型回覆", "Waiting for model"), "agent.model_completed": c("模型已回覆", "Model responded"),
    "agent.model_failed": c("模型回合未完成", "Model round did not complete"),
    "agent.tool_started": c("開始工具操作", "Tool operation started"), "agent.tool_completed": c("工具操作完成", "Tool operation completed"),
    "agent.request_started": c("開始重送請求", "Replay started"), "agent.request_completed": c("重送完成，證據已保存", "Replay completed; evidence saved"),
    "agent.request_failed": c("重送未完成", "Replay did not complete"), "agent.wrapup_started": c("剩餘時間有限，開始整理成果", "Wrapping up within the remaining time"),
    "agent.skills_loaded": c("已載入測試技能", "Test skills loaded"), "agent.finding_recorded": c("已記錄發現", "Finding recorded"),
  };
  const label = event.type === "agent.model_streaming" ? liveActivity(event, c) : names[event.type || ""] || c("測試進度更新", "Test progress updated");
  const tool = event.tool && TOOL_NAMES.has(event.tool) ? event.tool : null;
  const metrics = event.type === "agent.model_completed" || event.type === "agent.model_failed";
  return <div><time>{dateText(event.created_at || event.timestamp, true)}</time><span>{label}{tool && <> · <code>{tool}</code></>}{numeric(event.round) ? ` · ${c("回合", "Round")} ${event.round}` : ""}{numeric(event.duration_seconds) ? ` · ${seconds(event.duration_seconds)}` : ""}
    {event.type === "agent.model_streaming" && <small>{c("首事件／首輸出", "First event / output")}: {seconds(event.first_event_seconds)} / {seconds(event.first_output_seconds)}</small>}
    {metrics && <small>{c("輸入／輸出 Token", "Input / output tokens")}: {tokens(event.input_tokens)} / {tokens(event.output_tokens)} · {finishName({ ...event, round: event.round || 0 }, c)}</small>}
  </span></div>;
}
