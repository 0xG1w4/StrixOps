"use client";

import { authFetch } from "@/lib/auth";


import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertTriangle, Search, RefreshCw, X } from "lucide-react";
import { apiURL } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { searchCost, searchCount, searchDuration, webSearchStatus, type WebSearchStats } from "@/lib/web-search";
import styles from "./WebSearchDiagnostics.module.css";

const POLL_MS = 10_000;

export default function WebSearchDiagnostics({ runName, live }: { runName: string; live: boolean }) {
  const { locale } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => en ? english : zh;
  const [snapshot, setSnapshot] = React.useState<{ name: string; data: WebSearchStats | null; failed: boolean } | null>(null);
  const [revision, setRevision] = React.useState(0);
  const [openRun, setOpenRun] = React.useState<string | null>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const closeRef = React.useRef<HTMLButtonElement>(null);
  React.useEffect(() => setOpenRun(null), [runName]);
  React.useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let resume = false;
    const load = async () => {
      if (disposed || document.hidden || controller) return;
      const current = new AbortController();
      controller = current;
      let timedOut = false;
      const deadline = window.setTimeout(() => {
        timedOut = true;
        current.abort();
      }, 8000);
      try {
        const response = await authFetch(apiURL(`/api/runs/${encodeURIComponent(runName)}/web-search`), {
          cache: "no-store",
          signal: current.signal,
        });
        if (!response.ok) throw new Error("unavailable");
        const data: WebSearchStats = await response.json();
        if (typeof data?.available !== "boolean"
          || !["ok", "not_recorded", "invalid_stats"].includes(data.code)
          || !Array.isArray(data.recent)
          || data.code === "invalid_stats") {
          throw new Error("invalid_stats");
        }
        if (!disposed && !current.signal.aborted) {
          setSnapshot({ name: runName, data, failed: false });
        }
      } catch {
        if (!disposed && !document.hidden && (!current.signal.aborted || timedOut)) {
          setSnapshot(previous => ({
            name: runName,
            data: previous?.name === runName ? previous.data : null,
            failed: true,
          }));
        }
      } finally {
        window.clearTimeout(deadline);
        controller = null;
        if (!disposed && !document.hidden) {
          if (resume) { resume = false; void load(); }
          else if (live) timer = window.setTimeout(() => void load(), POLL_MS);
        }
      }
    };
    const visibility = () => {
      window.clearTimeout(timer);
      if (document.hidden) { resume = false; controller?.abort(); }
      else if (controller) resume = true;
      else void load();
    };
    void load();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [runName, live, revision]);

  const current = snapshot?.name === runName ? snapshot : null;
  const data = current?.data;
  const recorded = data?.available ? data : null;
  const recent = Array.isArray(recorded?.recent) ? recorded.recent.slice(-20).reverse() : [];
  const noRecords = data?.code === "not_recorded" && !current?.failed;
  const warning = current?.failed
    ? c("记录更新失败", "Records could not be refreshed")
    : recorded?.circuit_code
      ? webSearchStatus(recorded.circuit_code, en)
      : typeof recorded?.failures === "number" && recorded.failures > 0
        ? c(`有 ${searchCount(recorded.failures)} 次搜索失败`, `${searchCount(recorded.failures)} failed search calls`)
        : null;
  const triggerValue = !current
    ? c("加载中…", "Loading…")
    : noRecords
      ? c("无记录", "No records")
      : c(`${searchCount(recorded?.calls)} 次调用`, `${searchCount(recorded?.calls)} calls`);
  const triggerLabel = `${c("网页搜索", "Web search")} · ${triggerValue}${warning ? ` · ${warning}` : ""}`;
  const counters: Array<[string, number | null | undefined]> = [
    [c("工具调用", "Tool calls"), recorded?.calls],
    [c("成功", "Succeeded"), recorded?.successes],
    [c("失败", "Failed"), recorded?.failures],
    [c("跳过", "Skipped"), recorded?.skipped],
    [c("实际 API 请求", "Actual API requests"), recorded?.requests],
  ];

  return (
    <Dialog.Root open={openRun === runName} onOpenChange={open => setOpenRun(open ? runName : null)}>
      <Dialog.Trigger asChild>
        <button
          ref={triggerRef}
          type="button"
          className={styles.trigger}
          aria-label={triggerLabel}
          title={triggerLabel}
          data-warning={warning ? "true" : undefined}
        >
          <Search size={14} aria-hidden="true" />
          <span>{c("网页搜索", "Web search")}</span>
          <span className={styles.triggerValue}>{triggerValue}</span>
          {warning && <AlertTriangle size={13} className={styles.warningIcon} aria-hidden="true" />}
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={styles.drawer}
          onOpenAutoFocus={event => {
            event.preventDefault();
            closeRef.current?.focus({ preventScroll: true });
          }}
          onCloseAutoFocus={event => {
            event.preventDefault();
            if (triggerRef.current?.isConnected) triggerRef.current.focus({ preventScroll: true });
          }}
        >
          <header className={styles.header}>
            <div className={styles.heading}>
              <div className={styles.identity}>
                <Search size={15} aria-hidden="true" />
                <span>Perplexity</span>
              </div>
              <Dialog.Title className={styles.title}>
                {c("网页搜索诊断", "Web search diagnostics")}
              </Dialog.Title>
              <Dialog.Description className={styles.description}>
                {c("此任务所有代理的搜索活动。", "Search activity across all agents in this task.")}
              </Dialog.Description>
              <span className={styles.runName}>{runName}</span>
            </div>
            <div className={styles.actions}>
              <button
                type="button"
                className={styles.iconButton}
                aria-label={c("更新网页搜索记录", "Refresh web search records")}
                onClick={() => setRevision(value => value + 1)}
              >
                <RefreshCw size={15} aria-hidden="true" />
              </button>
              <Dialog.Close asChild>
                <button
                  ref={closeRef}
                  type="button"
                  className={styles.iconButton}
                  aria-label={c("关闭网页搜索诊断", "Close web search diagnostics")}
                >
                  <X size={17} aria-hidden="true" />
                </button>
              </Dialog.Close>
            </div>
          </header>
          <div className={styles.content} tabIndex={0} aria-label={c("网页搜索记录", "Web search records")}>
            {!current && <p className={styles.note} role="status">{c("正在加载搜索记录…", "Loading search records…")}</p>}
            {current?.failed && (
              <p className={styles.notice} role="status">
                {recorded
                  ? c("更新失败，显示上次取得的搜索记录。", "Refresh failed. Showing the previously fetched search records.")
                  : c("暂时无法读取搜索记录。", "Search records are temporarily unavailable.")}
              </p>
            )}
            {recorded?.circuit_code && (
              <p className={styles.notice}>
                {webSearchStatus(recorded.circuit_code, en)} · {" "}
                {c("此任务已停止后续服务请求。", "Further service requests are stopped for this task.")}
              </p>
            )}
            <section aria-label={c("搜索次数", "Search counts")}>
              <div className={styles.sectionHeading}>
                <h3>{c("搜索概况", "Search activity")}</h3>
                {recorded?.partial_usage && <small>{c("部分用量数据", "Partial usage data")}</small>}
              </div>
              <dl className={styles.counters}>
                {counters.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{searchCount(value)}</dd>
                  </div>
                ))}
              </dl>
            </section>
            <section className={styles.section} aria-label={c("搜索用量", "Search usage")}>
              <h3>{c("耗时与用量", "Timing and usage")}</h3>
              <dl className={styles.usage}>
                <div>
                  <dt>{c("搜索耗时合计", "Search duration")}</dt>
                  <dd>{searchDuration(recorded?.duration_seconds)}</dd>
                </div>
                <div>
                  <dt>{c("输入／输出 Token", "Input / output tokens")}</dt>
                  <dd>{searchCount(recorded?.input_tokens)} / {searchCount(recorded?.output_tokens)}</dd>
                </div>
                <div>
                    <dt>{c("服务端返回的费用", "Provider-reported cost")}</dt>
                  <dd>{searchCost(recorded?.reported_cost_usd)}</dd>
                </div>
              </dl>
              <p className={styles.note}>
                {c(
                  "— 表示未返回数据，并非 0。费用为服务端返回值；部分用量仅显示已返回的小计。",
                  "— means not reported, not zero. Cost is reported by the provider; partial usage shows only the observed subtotal.",
                )}
              </p>
            </section>
            <section className={styles.section} aria-label={c("最近搜索记录", "Recent search records")}>
              <h3>{c("最近记录", "Recent records")} <span>{c("最多 20 条", "Up to 20")}</span></h3>
              {recent.length ? (
                <ol className={styles.records}>
                  {recent.map((record, index) => (
                    <li key={`${record.timestamp}-${index}`}>
                      <div className={styles.recordHeader}>
                        <time>
                          {record.timestamp && Number.isFinite(Date.parse(record.timestamp))
                            ? new Date(record.timestamp).toLocaleString() : "—"}
                        </time>
                        <span data-status={record.status}>
                          {record.code
                            ? webSearchStatus(record.code, en)
                            : c("状态未记录", "Status not recorded")}
                        </span>
                      </div>
                      <div className={styles.recordMeta}>
                        <span>
                          {record.model && ["sonar", "sonar-reasoning-pro"].includes(record.model)
                            ? record.model : c("模型未记录", "Model not recorded")}
                        </span>
                        <span>{searchDuration(record.duration_seconds)}</span>
                        <span>{c("API 请求", "API requests")}: {searchCount(record.attempts)}</span>
                        <span>
                          {c("输入／输出 Token", "Input / output tokens")}: {" "}
                          {searchCount(record.input_tokens)} / {searchCount(record.output_tokens)}
                        </span>
                          <span>{c("返回的费用", "Reported cost")}: {searchCost(record.cost_usd)}</span>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : current && !current.failed ? (
                <p className={styles.note}>
                  {noRecords || recorded?.calls === 0
                    ? c("此任务当前没有搜索调用记录。", "No search calls have been recorded for this task.")
                    : c("当前没有可显示的最近搜索记录。", "No recent search records are available.")}
                </p>
              ) : (
                <p className={styles.note}>{c("搜索记录尚未取得。", "Search records have not been retrieved.")}</p>
              )}
            </section>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
