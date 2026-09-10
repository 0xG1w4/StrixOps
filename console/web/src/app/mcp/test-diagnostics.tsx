"use client";

import * as React from "react";
import { isMcpTestLive, type McpTest, type McpTestEvent } from "@/lib/mcp-api";
import { dateText, McpStatus, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

type Copy = ReturnType<typeof useMcpCopy>;
const seconds = (value?: number) => typeof value === "number" && Number.isFinite(value) ? `${Math.max(0, Math.round(value))} s` : "—";

function phaseName(phase: string | undefined, c: Copy) {
  const names: Record<string, string> = {
    initialization: c("準備測試", "Preparing test"), model: c("等待模型回覆", "Waiting for model"),
    tool: c("檢視與記錄證據", "Inspecting and recording evidence"), replay: c("重送請求", "Replaying request"),
    wrapup: c("整理部分成果", "Wrapping up results"), completion: c("整理結論", "Finalizing conclusions"),
  };
  return names[phase || ""] || c("執行測試", "Running test");
}

export function budgetExceeded(test: McpTest) {
  return test.result?.completion_reason === "time_budget_exceeded" || test.result?.diagnostics?.reason === "time_budget_exceeded" || test.error === "Time budget exceeded";
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
  const event = [...(test.events || [])].reverse().find(item => item.phase);
  const started = Date.parse(test.started_at || "");
  const elapsed = live && Number.isFinite(started) ? Math.max(0, (clock - started) / 1000) : detail?.elapsed_seconds;
  const count = test.requests_used ?? detail?.request_count ?? [...(test.events || [])].reverse().find(item => typeof item.request_count === "number")?.request_count;
  const limited = budgetExceeded(test) || test.result?.completion_reason === "time_budget_limited";
  if (!live && !detail && !limited) return null;
  return <section className={styles.testDiagnostics} aria-label={c("測試進度與耗時", "Test progress and timing")}>
    <div className={styles.testMeta}>
      <span>{c("已用時間", "Elapsed")}: {seconds(elapsed)}</span>
      <span>{c("已使用重送", "Replays used")}: {count ?? "—"} / {test.config?.max_requests ?? "—"}</span>
      {detail?.model_rounds != null && <span>{c("模型回合", "Model rounds")}: {detail.model_rounds}</span>}
    </div>
    {live && <p role="status">{phaseName(event?.phase, c)}{test.config?.max_seconds != null && elapsed != null ? ` · ${c("剩餘約", "About")} ${seconds(test.config.max_seconds - elapsed)}${c("", " remaining")}` : ""}</p>}
    {limited && <p>{budgetExceeded(test)
      ? c(`測試已達時間上限，當時階段：${phaseName(detail?.phase, c)}。已取得的證據與結果保留，未完成項目需要後續測試。`, `The time limit was reached during: ${phaseName(detail?.phase, c)}. Saved evidence and results remain available; incomplete checks need follow-up.`)
      : c("Agent 已在時間上限前整理部分成果；請查看未完成項目，這不代表所有檢查都已通過。", "The Agent wrapped up partial results before the deadline. Review the remaining checks; this does not mean every check passed.")}</p>}
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
  const label = names[event.type || ""] || event.message || c("測試進度更新", "Test progress updated");
  return <div><time>{dateText(event.created_at || event.timestamp, true)}</time><span>{label}{event.round != null ? ` · ${c("回合", "Round")} ${event.round}` : ""}{event.duration_seconds != null ? ` · ${seconds(event.duration_seconds)}` : ""}</span></div>;
}
