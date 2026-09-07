"use client";

import * as React from "react";
import { ArrowRight, ChevronDown } from "lucide-react";
import { apiURL } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./ProxyStatusPanel.module.css";

type ProxyState = "healthy" | "degraded" | "ready" | "unavailable" | "unknown" | "stopped" | "disabled";
type ErrorKind = "timeout" | "tls" | "connection" | "unknown";
type Sample = { timestamp: string; requests: number; errors: number };
type ProxySnapshot = {
  state: ProxyState;
  checked_at: string | null;
  last_activity_at: string | null;
  reason: string;
  checks: { listener: boolean | null; api: boolean | null; capture: boolean | null };
  metrics: {
    captured_total: number | null;
    requests_window: number | null;
    proxy_errors_window: number | null;
    timeouts_window: number | null;
  };
  series: Sample[];
  recent_errors: { timestamp: string; kind: ErrorKind }[];
  window_seconds: number;
  partial: boolean;
  source: "live" | "saved" | "none";
};

type SnapshotResult = { name: string; data: ProxySnapshot | null; failed: boolean };
const POLL_MS = 10_000;
const STALE_MS = 30_000;
const STATES: ProxyState[] = ["healthy", "degraded", "ready", "unavailable", "unknown", "stopped", "disabled"];

const COPY = {
  "zh-CN": {
    label: "Caido 代理状态", tools: "工具", target: "目标", checking: "检查中", details: "查看详情", collapse: "收起详情",
    states: { healthy: "代理在线", degraded: "在线 · 近期有错误", ready: "已就绪 · 等待流量", unavailable: "代理不可用", unknown: "状态未知", stopped: "历史快照", disabled: "代理未启用", stale: "状态已过期" },
    captured: "已捕获", requests: "窗口请求", errors: "代理错误", timeouts: "其中超时", checked: "检查于", capture: "最近捕获",
    history: "历史快照", recentTimeouts: "在线 · 近期有超时", noData: "暂无流量数据", incomplete: "窗口数据不足", chart: "请求与代理错误趋势", requestsLegend: "请求", errorsLegend: "代理错误",
    window: "最近 5 分钟 · 每格 10 秒", ending: "截至", partial: "日志数据不足以覆盖完整 5 分钟，窗口计数与图表暂不可用。",
    boundary: "图表仅统计经过 Caido 的 HTTP 流量。代理错误来自 Caido 日志，不代表全部 HTTP 502；超时不代表目标离线。",
    listener: "代理端口", api: "Caido API", capturedCheck: "流量捕获", yes: "正常", no: "不可用", observed: "已观察到", waiting: "未观察到", unknown: "未知",
    refreshFailed: "无法更新代理状态，当前显示上次获取的数据。", firstFailed: "暂时无法获取代理状态。",
    recent: "最近代理错误", noErrors: "当前日志窗口没有记录到代理错误。", noErrorData: "暂无代理错误记录。",
    kinds: { timeout: "超时", tls: "TLS 错误", connection: "连接错误", unknown: "代理错误" },
    reasons: {
      proxy_disabled: "此任务未启用 Caido 代理。", scan_stopped: "任务已结束，显示最后保存的数据。",
      runtime_binding_missing: "尚未找到此任务的代理运行信息。", runtime_binding_invalid: "此任务的代理运行信息不可用。",
      container_unavailable: "暂时无法访问此任务的代理容器。", collection_failed: "此次未能完成代理状态检查。",
      listener_unreachable: "无法连接容器内的代理端口。", api_unreachable: "无法访问 Caido API。",
      capture_unknown: "代理状态检查未能确认捕获数据。", recent_proxy_errors: "代理仍在线，近期日志记录到了转发错误。",
      traffic_captured: "代理在线，已捕获工具请求。", awaiting_traffic: "代理已就绪，尚未捕获到请求。",
    },
  },
  en: {
    label: "Caido proxy status", tools: "Tools", target: "Target", checking: "Checking", details: "Show details", collapse: "Hide details",
    states: { healthy: "Proxy online", degraded: "Online · recent errors", ready: "Ready · awaiting traffic", unavailable: "Proxy unavailable", unknown: "Status unknown", stopped: "Historical snapshot", disabled: "Proxy disabled", stale: "Status stale" },
    captured: "Captured", requests: "Window requests", errors: "Proxy errors", timeouts: "Timeouts", checked: "Checked", capture: "Last capture",
    history: "Saved snapshot", recentTimeouts: "Online · recent timeouts", noData: "No traffic data yet", incomplete: "Incomplete window", chart: "Requests and proxy errors", requestsLegend: "Requests", errorsLegend: "Proxy errors",
    window: "Last 5 minutes · 10s bins", ending: "Ending", partial: "Logs do not cover the full 5-minute window; window counts and the chart are unavailable.",
    boundary: "The chart includes only HTTP traffic through Caido. Proxy errors come from Caido logs, not all HTTP 502 responses; a timeout does not mean the target is offline.",
    listener: "Proxy listener", api: "Caido API", capturedCheck: "Traffic capture", yes: "Available", no: "Unavailable", observed: "Observed", waiting: "Not observed", unknown: "Unknown",
    refreshFailed: "Unable to refresh proxy status. Showing the previously fetched data.", firstFailed: "Proxy status is currently unavailable.",
    recent: "Recent proxy errors", noErrors: "No proxy errors recorded in the current log window.", noErrorData: "No proxy error data available.",
    kinds: { timeout: "Timeout", tls: "TLS error", connection: "Connection error", unknown: "Proxy error" },
    reasons: {
      proxy_disabled: "Caido is not enabled for this run.", scan_stopped: "This run has ended. Showing the last saved data.",
      runtime_binding_missing: "No proxy runtime information is available for this run yet.", runtime_binding_invalid: "This run's proxy runtime information is unavailable.",
      container_unavailable: "This run's proxy container is currently unavailable.", collection_failed: "The proxy status check could not be completed.",
      listener_unreachable: "The proxy listener inside the container could not be reached.", api_unreachable: "The Caido API could not be reached.",
      capture_unknown: "The status check could not confirm capture data.", recent_proxy_errors: "The proxy is online, with forwarding errors recorded recently.",
      traffic_captured: "The proxy is online and has captured tool requests.", awaiting_traffic: "The proxy is ready and has not captured a request yet.",
    },
  },
};

/** A poll finishes before the next one starts; hidden pages cancel in-flight work. */
function useProxySnapshot(runName: string, live: boolean, enabled: boolean) {
  const [result, setResult] = React.useState<SnapshotResult | null>(null);
  const [now, setNow] = React.useState(() => Date.now());

  React.useEffect(() => {
    if (!enabled || !runName) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | null = null;
    let resumePending = false;

    const load = async () => {
      if (disposed || document.hidden || controller) return;
      clearTimeout(timer);
      setNow(Date.now());
      const request = new AbortController();
      controller = request;
      let timedOut = false;
      const deadline = setTimeout(() => { timedOut = true; request.abort(); }, 8_000);
      try {
        const response = await fetch(apiURL(`/api/runs/${encodeURIComponent(runName)}/proxy`), {
          cache: "no-store", signal: request.signal,
        });
        if (!response.ok) throw new Error("Proxy status unavailable");
        const data: ProxySnapshot = await response.json();
        if (!data || !STATES.includes(data.state) || !data.checks || !data.metrics || !Array.isArray(data.series) || !Array.isArray(data.recent_errors)) {
          throw new Error("Invalid proxy status");
        }
        if (!disposed && !request.signal.aborted) {
          setResult({ name: runName, data, failed: false });
          setNow(Date.now());
        }
      } catch {
        if (!disposed && !document.hidden && (!request.signal.aborted || timedOut)) {
          setResult((previous) => ({ name: runName, data: previous?.name === runName ? previous.data : null, failed: true }));
          setNow(Date.now());
        }
      } finally {
        clearTimeout(deadline);
        controller = null;
        if (!disposed && !document.hidden) {
          if (resumePending) { resumePending = false; void load(); }
          else if (live) timer = setTimeout(() => void load(), POLL_MS);
        }
      }
    };

    const onVisibility = () => {
      clearTimeout(timer);
      if (document.hidden) { resumePending = false; controller?.abort(); }
      else if (controller) resumePending = true;
      else void load();
    };
    void load();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [runName, live, enabled]);

  return { result: enabled && result?.name === runName ? result : null, now };
}

function validCount(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function TrafficChart({ data, locale, compact = false }: { data: ProxySnapshot | null; locale: "zh-CN" | "en"; compact?: boolean }) {
  const copy = COPY[locale];
  // The backend supplies actual 10-second bins. Missing bins stay gaps, not zeroes.
  const samples = (data?.series ?? []).filter((sample) => Number.isFinite(Date.parse(sample.timestamp)) && validCount(sample.requests) && validCount(sample.errors)).slice(-30);
  const end = samples.length ? Math.max(...samples.map((sample) => Date.parse(sample.timestamp))) + 10_000 : 0;
  const start = end - 300_000;
  const peak = Math.max(1, ...samples.flatMap((sample) => [sample.requests, sample.errors]));
  const countText = (n: number) => n.toLocaleString(locale);
  return (
    <div className={compact ? styles.miniChart : styles.chart}>
      {samples.length ? (
        <svg viewBox="0 0 360 72" preserveAspectRatio="none" role="img" aria-label={`${copy.chart} · ${copy.window}`}>
          <title>{copy.chart} · {copy.window}</title>
          {[12, 36, 60].map((y) => <line key={y} x1="0" y1={y} x2="360" y2={y} className={styles.gridLine} />)}
          {samples.map((sample) => {
            const x = Math.floor((Date.parse(sample.timestamp) - start) / 10_000) * 12;
            const requestsHeight = sample.requests / peak * 54;
            const errorsHeight = sample.errors / peak * 54;
            return (
              <g key={sample.timestamp}>
                <title>{new Date(sample.timestamp).toLocaleTimeString(locale)} · {copy.requestsLegend}: {countText(sample.requests)} · {copy.errorsLegend}: {countText(sample.errors)}</title>
                <rect x={x + 1} y="64" width="9" height="1" className={styles.binMarker} />
                <rect x={x + 1} y={60 - requestsHeight} width="5" height={requestsHeight} className={styles.requestBar} />
                <rect x={x + 7} y={60 - errorsHeight} width="3" height={errorsHeight} className={styles.errorBar} />
              </g>
            );
          })}
        </svg>
      ) : <span className={styles.chartEmpty}>{data?.partial ? copy.incomplete : copy.noData}</span>}
      {!compact && (
        <div className={styles.chartAxis}>
          <span>−5 min</span>
          <span>{samples.length ? `${copy.ending} ${new Date(data?.checked_at || end).toLocaleTimeString(locale)}` : "—"}</span>
        </div>
      )}
    </div>
  );
}

export default function ProxyStatusPanel({ runName, live, enabled }: { runName: string; live: boolean; enabled: boolean }) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  const [expanded, setExpanded] = React.useState(false);
  const detailId = React.useId();
  const { result, now } = useProxySnapshot(runName, live, enabled);
  const data = result?.data ?? null;
  const checkedAt = data?.checked_at ? Date.parse(data.checked_at) : NaN;
  const stale = Boolean(result?.failed && data) || Boolean(live && data && (
    data.source === "saved" || (Number.isFinite(checkedAt) && now - checkedAt > STALE_MS) ||
    (["healthy", "degraded", "ready"].includes(data.state) && (!Number.isFinite(checkedAt) || data.source !== "live"))
  ));
  const state = !enabled ? "disabled" : stale ? "stale" : !live ? "stopped" : data?.state ?? "unknown";
  const online = live && !stale && ["healthy", "degraded", "ready"].includes(state);
  const recentTimeout = Boolean((data?.metrics.timeouts_window ?? 0) > 0 || data?.recent_errors.some((error) => error.kind === "timeout"));
  const statusText = enabled && live && !result ? copy.checking : state === "degraded" && recentTimeout ? copy.recentTimeouts : copy.states[state];
  const number = (value: number | null | undefined) => validCount(value) ? value.toLocaleString(locale) : "—";
  const time = (value: string | null | undefined) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleTimeString(locale) : "—";
  const reasonKey = (!enabled ? "proxy_disabled" : !live ? "scan_stopped" : data?.reason) as keyof typeof copy.reasons | undefined;
  const reason = result?.failed ? (data ? copy.refreshFailed : copy.firstFailed) : reasonKey ? copy.reasons[reasonKey] : undefined;
  const tone = state === "degraded" || state === "stale" ? "warning" : online ? "online" : state === "unavailable" ? "error" : "neutral";

  React.useEffect(() => { setExpanded(false); }, [runName]);

  return (
    <section className={styles.panel} data-tone={tone} data-stale={stale || undefined} aria-label={copy.label}>
      <div className={styles.summary}>
        <div className={styles.identity}>
          <div className={styles.route} aria-label={`${copy.tools} → Caido → ${copy.target}`}>
            <span>{copy.tools}</span><ArrowRight aria-hidden="true" />
            <span className={styles.caido}>Caido<span key={data?.checked_at ?? state} className={`${styles.dot} ${online ? styles.fresh : ""}`} aria-hidden="true" /></span>
            <ArrowRight aria-hidden="true" /><span>{copy.target}</span>
          </div>
          <div className={styles.stateLine}>
            <span className={styles.state} role="status">{statusText}</span>
            {data?.source === "saved" && state !== "stopped" && <span className={styles.source}>{copy.history}</span>}
          </div>
          <span className={styles.checked} title={data?.checked_at ?? undefined}>{copy.checked} <time dateTime={data?.checked_at ?? undefined}>{time(data?.checked_at)}</time></span>
        </div>
        <dl className={styles.metrics}>
          <div><dt>{copy.captured}</dt><dd>{number(data?.metrics.captured_total)}</dd></div>
          <div><dt>{copy.requests}<span className={styles.windowLabel}> / 5m</span></dt><dd>{number(data?.metrics.requests_window)}</dd></div>
          <div><dt>{copy.errors}<span className={styles.windowLabel}> / 5m</span></dt><dd className={validCount(data?.metrics.proxy_errors_window) && data.metrics.proxy_errors_window > 0 ? styles.errorCount : undefined}>{number(data?.metrics.proxy_errors_window)}</dd></div>
        </dl>
        <div className={styles.preview} aria-hidden="true">
          <TrafficChart data={data} locale={locale} compact />
          <span className={styles.previewCaption}>5 min <span>· 10s</span>{data?.partial ? " · *" : ""}</span>
        </div>
        <button type="button" className={styles.disclosure} aria-expanded={expanded} aria-controls={detailId} aria-label={expanded ? copy.collapse : copy.details} title={expanded ? copy.collapse : copy.details} onClick={() => setExpanded((value) => !value)}>
          <ChevronDown aria-hidden="true" />
        </button>
      </div>

      {expanded && (
        <div className={styles.details} id={detailId}>
          <div className={styles.detailChart}>
            <div className={styles.chartHeading}><span>{copy.window}</span><span className={styles.legend}><i className={styles.requestsKey} />{copy.requestsLegend}<i className={styles.errorsKey} />{copy.errorsLegend}</span></div>
            <TrafficChart data={data} locale={locale} />
            <p className={styles.chartNote}>{data?.partial && <strong>{copy.partial} </strong>}{copy.boundary}</p>
          </div>
          <div className={styles.diagnostics}>
            {reason && <p className={styles.reason}>{reason}</p>}
            <dl className={styles.checks}>
              {(["listener", "api", "capture"] as const).map((check) => {
                const value = data?.checks[check];
                return <div key={check}><dt>{check === "capture" ? copy.capturedCheck : copy[check]}</dt><dd data-available={value === true && !stale && live ? "true" : undefined}>{typeof value !== "boolean" ? copy.unknown : check === "capture" ? (value ? copy.observed : copy.waiting) : value ? copy.yes : copy.no}</dd></div>;
              })}
              <div><dt>{copy.capture}</dt><dd><time dateTime={data?.last_activity_at ?? undefined} title={data?.last_activity_at ?? undefined}>{time(data?.last_activity_at)}</time></dd></div>
              <div><dt>{copy.timeouts}<span className={styles.windowLabel}> / 5m</span></dt><dd>{number(data?.metrics.timeouts_window)}</dd></div>
            </dl>
            <div className={styles.recentErrors}>
              <span className={styles.errorsHeading}>{copy.recent}</span>
              {data?.recent_errors.length ? <ul>{[...data.recent_errors].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp)).slice(0, 3).map((error, index) => <li key={`${error.timestamp}-${index}`}><span>{copy.kinds[error.kind] ?? copy.kinds.unknown}</span><time dateTime={error.timestamp}>{time(error.timestamp)}</time></li>)}</ul> : <p>{data?.metrics.proxy_errors_window === 0 ? copy.noErrors : copy.noErrorData}</p>}
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
