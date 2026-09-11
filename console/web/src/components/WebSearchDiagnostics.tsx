"use client";

import * as React from "react";
import { Search, RefreshCw, ChevronDown } from "lucide-react";
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
        const response = await fetch(apiURL(`/api/runs/${encodeURIComponent(runName)}/web-search`), {
          cache: "no-store",
          signal: current.signal,
        });
        if (!response.ok) throw new Error("unavailable");
        const data: WebSearchStats = await response.json();
        if (typeof data?.available !== "boolean"
          || !["ok", "not_recorded", "invalid_stats"].includes(data.code)
          || !Array.isArray(data.recent)) {
          throw new Error("invalid_stats");
        }
        if (!disposed && !current.signal.aborted) {
          setSnapshot({ name: runName, data, failed: data.code === "invalid_stats" });
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
  if (!current || (data?.code === "not_recorded" && !current.failed)) return null;
  const recent = Array.isArray(data?.recent) ? data.recent.slice(-20).reverse() : [];
  const counters: Array<[string, number | null | undefined]> = [
    [c("工具呼叫", "Tool calls"), data?.calls],
    [c("成功", "Succeeded"), data?.successes],
    [c("失敗", "Failed"), data?.failures],
    [c("略過", "Skipped"), data?.skipped],
    [c("實際 API 請求", "Actual API requests"), data?.requests],
  ];

  return (
    <section className={styles.panel} aria-label={c("網頁搜尋診斷", "Web search diagnostics")}>
      <div className={styles.header}>
        <div className={styles.identity}>
          <Search size={15} />
          <h3>{c("網頁搜尋", "Web search")}</h3>
          <span>Perplexity</span>
          {data?.partial_usage && <small>{c("部分用量資料", "Partial usage data")}</small>}
        </div>
        <button
          type="button"
          className={styles.refresh}
          aria-label={c("更新網頁搜尋紀錄", "Refresh web search records")}
          onClick={() => setRevision(value => value + 1)}
        >
          <RefreshCw size={13} />
        </button>
      </div>
      {current.failed && (
        <p className={styles.notice} role="status">
          {data?.available
            ? c("更新失敗，顯示上次取得的搜尋紀錄。", "Refresh failed. Showing the previously fetched search records.")
            : c("暫時無法讀取搜尋紀錄。", "Search records are temporarily unavailable.")}
        </p>
      )}
      {data?.circuit_code && (
        <p className={styles.notice}>
          {webSearchStatus(data.circuit_code, en)} · {" "}
          {c("此任務已停止後續服務請求。", "Further service requests are stopped for this task.")}
        </p>
      )}
      <div className={styles.counters}>
        {counters.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{searchCount(value)}</strong>
          </div>
        ))}
      </div>
      <details className={styles.details}>
        <summary>
          <span>{c("耗時、用量與最近紀錄", "Timing, usage, and recent records")}</span>
          <ChevronDown size={13} />
        </summary>
        <div className={styles.content}>
          <dl className={styles.usage}>
            <div>
              <dt>{c("搜尋耗時合計", "Search duration")}</dt>
              <dd>{searchDuration(data?.duration_seconds)}</dd>
            </div>
            <div>
              <dt>{c("輸入／輸出 Token", "Input / output tokens")}</dt>
              <dd>{searchCount(data?.input_tokens)} / {searchCount(data?.output_tokens)}</dd>
            </div>
            <div>
              <dt>{c("服務端回報費用", "Provider-reported cost")}</dt>
              <dd>{searchCost(data?.reported_cost_usd)}</dd>
            </div>
          </dl>
          <p className={styles.note}>
            {c(
              "— 表示沒有回報資料，並非 0。費用為服務端回報值；部分用量僅顯示已回報的小計。",
              "— means not reported, not zero. Cost is reported by the provider; partial usage shows only the observed subtotal.",
            )}
          </p>
          {recent.length ? (
            <ol className={styles.records}>
              {recent.map((record, index) => (
                <li key={`${record.timestamp}-${index}`}>
                  <div className={styles.recordHeader}>
                    <time>
                      {record.timestamp && Number.isFinite(Date.parse(record.timestamp))
                        ? new Date(record.timestamp).toLocaleString() : "—"}
                    </time>
                    <span data-status={record.status}>{webSearchStatus(record.code, en)}</span>
                  </div>
                  <div className={styles.recordMeta}>
                    <span>
                      {record.model && ["sonar", "sonar-reasoning-pro"].includes(record.model)
                        ? record.model : c("模型未記錄", "Model not recorded")}
                    </span>
                    <span>{searchDuration(record.duration_seconds)}</span>
                    <span>{c("API 請求", "API requests")}: {searchCount(record.attempts)}</span>
                    <span>
                      {c("輸入／輸出 Token", "Input / output tokens")}: {" "}
                      {searchCount(record.input_tokens)} / {searchCount(record.output_tokens)}
                    </span>
                    <span>{c("回報費用", "Reported cost")}: {searchCost(record.cost_usd)}</span>
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <p className={styles.note}>{c("目前沒有搜尋呼叫紀錄。", "No search calls have been recorded.")}</p>
          )}
        </div>
      </details>
    </section>
  );
}
