"use client";

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Download, RefreshCw } from "lucide-react";
import { EmptyState, Spinner } from "@/components/ui";
import { apiURL, getJSON } from "@/lib/api";
import type { AssessmentPage, CoverageEntry, RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

const RESOLVED = new Set(["reported", "no_issue_found", "ruled_out", "not_applicable"]);

function outcomeLabel(value: string, en: boolean): string {
  const labels: Record<string, [string, string]> = {
    reported: ["已报告发现", "Finding reported"],
    no_issue_found: ["本项未发现问题", "No issue found in this item"],
    ruled_out: ["已排除", "Ruled out"],
    not_applicable: ["不适用", "Not applicable"],
    needs_follow_up: ["待跟进", "Needs follow-up"],
  };
  return labels[value]?.[en ? 1 : 0] ?? value;
}

function History({ entries, en }: { entries?: Array<Record<string, unknown>>; en: boolean }) {
  if (!Array.isArray(entries) || entries.length === 0) return null;
  return (
    <details className="mt-3 border-t border-line/6 pt-3">
      <summary className="cursor-pointer text-xs text-fg-muted">
        {en ? "Revision history" : "修订历史"} · {entries.length}
      </summary>
      <ol className="mt-2 space-y-2">
        {entries.map((entry, index) => (
          <li key={index}>
            <pre className="whitespace-pre-wrap break-words rounded-lg bg-raised/4 p-3 text-[11px] text-fg-2">
              {JSON.stringify(entry, null, 2)}
            </pre>
          </li>
        ))}
      </ol>
    </details>
  );
}

function CoverageRow({ entry, en }: { entry: CoverageEntry; en: boolean }) {
  const unresolved = !RESOLVED.has(entry.outcome);
  return (
    <details className="rounded-xl border border-line/8 bg-surface/42 p-3">
      <summary className="flex cursor-pointer flex-wrap items-center gap-2 text-sm">
        <span className={`mono-chip text-[9px] ${unresolved ? "chip-warning" : ""}`}>
          {outcomeLabel(entry.outcome, en)}
        </span>
        <span className="min-w-0 flex-1 break-words font-medium text-fg">{entry.surface}</span>
        <span className="text-xs text-fg-muted">{entry.risk_area}</span>
      </summary>
      <div className="mt-3 space-y-2 text-xs text-fg-2">
        <div className="flex flex-wrap gap-2 font-mono text-[10px] text-fg-muted">
          {(entry.id || entry.entry_id) && <span>{entry.id || entry.entry_id}</span>}
          {(entry.agent_name || entry.agent_id) && <span>{en ? "Last author" : "最近记录者"} · {entry.agent_name || entry.agent_id}</span>}
          {(entry.updated_at || entry.timestamp) && <span>{entry.updated_at || entry.timestamp}</span>}
        </div>
        {(entry.created_by_name || entry.created_by || entry.created_at) && <p className="text-[10px] text-fg-muted">{en ? "Created by" : "首次记录"} · {[entry.created_by_name || entry.created_by, entry.created_at].filter(Boolean).join(" · ")}</p>}
        <div className="micro-label text-[9px]">{en ? "Recorded evidence" : "记录的证据"}</div>
        <p className="whitespace-pre-wrap break-words leading-relaxed">
          {entry.evidence || (en ? "No evidence recorded." : "未记录证据。")}
        </p>
        <History entries={entry.history} en={en} />
      </div>
    </details>
  );
}

function isAssessment(value: AssessmentPage): boolean {
  return value?.schema_version === 1
    && ["unknown", "recorded"].includes(value.coverage?.status)
    && Array.isArray(value.coverage?.entries)
    && value.coverage.entries.every((entry) => entry && typeof entry.surface === "string" && typeof entry.risk_area === "string" && typeof entry.outcome === "string" && (entry.evidence == null || typeof entry.evidence === "string"))
    && ["unknown", "recorded"].includes(value.threat_models?.status)
    && Array.isArray(value.threat_models?.models)
    && value.threat_models.models.every((model) => model && typeof model.target === "string" && typeof model.content === "string"
      && (model.amendments == null || (Array.isArray(model.amendments) && model.amendments.every((item) => item && typeof item.content === "string"))));
}

export default function AssessmentPanel({ name, run }: { name: string; run: RunDetail | null }) {
  const { locale, t } = useI18n();
  const en = locale === "en";
  const [data, setData] = React.useState<AssessmentPage | null>(null);
  const [phase, setPhase] = React.useState<"loading" | "ready" | "missing" | "error">("loading");
  const [error, setError] = React.useState("");
  const [unresolvedOnly, setUnresolvedOnly] = React.useState(false);
  const requestId = React.useRef(0);
  const live = Boolean(run?.live);

  const load = React.useCallback(async () => {
    const current = ++requestId.current;
    try {
      const page = await getJSON<AssessmentPage & { source_status?: string }>(`/api/runs/${encodeURIComponent(name)}/assessment`);
      if (current !== requestId.current) return;
      if (!isAssessment(page)) throw new Error(en ? "Assessment record format is not supported." : "无法读取此评估记录格式。");
      setData(page);
      setPhase("ready");
      setError(page.source_status === "unreadable"
        ? (en ? "The stored assessment could not be read. Coverage and unresolved work are unknown." : "无法读取已保存的评估记录，测试覆盖与未完成项目仍未知。")
        : "");
    } catch (cause) {
      if (current !== requestId.current) return;
      const message = cause instanceof Error ? cause.message : String(cause);
      if (message.startsWith("404")) {
        setData(null);
        setPhase("missing");
        setError("");
      } else {
        setPhase("error");
        setError(message);
      }
    }
  }, [name, en]);

  React.useEffect(() => {
    setData(null);
    setPhase("loading");
    setError("");
    setUnresolvedOnly(false);
    void load();
    const timer = live ? window.setInterval(() => {
      if (!document.hidden) void load();
    }, 5000) : undefined;
    return () => {
      requestId.current += 1;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [load, live]);

  if (phase === "loading") {
    return <div className="flex items-center gap-2 p-5 text-sm text-fg-muted"><Spinner />{t("common.loading")}</div>;
  }

  const recordedCoverage = data?.coverage.status === "recorded";
  const recordedModels = data?.threat_models.status === "recorded";
  const entries = recordedCoverage ? data.coverage.entries : [];
  const models = recordedModels ? data.threat_models.models : [];
  const visible = unresolvedOnly ? entries.filter((entry) => !RESOLVED.has(entry.outcome)) : entries;
  const unrecorded = en ? "Not recorded" : "未记录";
  const unresolvedCount = recordedCoverage && Number.isInteger(data.coverage.unresolved_count) && (data.coverage.unresolved_count ?? -1) >= 0
    ? data.coverage.unresolved_count : null;

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-sm font-medium text-fg">{en ? "Assessment records" : "评估记录"}</h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-fg-muted">
            {en
              ? "These are agent-reported checks. A completed run or resolved recorded items do not establish full coverage or the absence of vulnerabilities."
              : "此处展示智能体记录的检查。运行完成或已记录项目均已处理，不代表测试范围完整，也不代表没有漏洞。"}
          </p>
        </div>
        <div className="flex gap-2">
          <button className="button-secondary button-compact" type="button" onClick={() => void load()} aria-label={en ? "Refresh assessment" : "刷新评估"}>
            <RefreshCw className="h-3.5 w-3.5" />{en ? "Refresh" : "刷新"}
          </button>
          {data && (
            <a className="button-secondary button-compact" href={apiURL(`/api/runs/${encodeURIComponent(name)}/assessment`)} download={`${name}-assessment.json`}>
              <Download className="h-3.5 w-3.5" />JSON
            </a>
          )}
        </div>
      </div>
      {error && <div className="alert-error" role="alert">{error}{data && <p className="mt-1">{en ? "Showing the last loaded record." : "当前展示上次读取的记录。"}</p>}</div>}
      {!data && phase !== "error" && (
        <EmptyState title={unrecorded} hint={en ? "This run has no assessment record. Coverage and unresolved work are unknown." : "此运行尚无评估记录。测试覆盖与未完成项目仍未知。"} />
      )}
      {data && (
        <>
          <div className="grid gap-2 sm:grid-cols-3">
            {[
              [en ? "Recorded checks" : "已记录检查", recordedCoverage ? entries.length : unrecorded],
              [en ? "Unresolved items" : "未完成项目", unresolvedCount ?? unrecorded],
              [en ? "Threat models" : "威胁模型", recordedModels ? models.length : unrecorded],
            ].map(([label, value]) => <div className="info-tile" key={label}><div className="info-label">{label}</div><div className="info-value text-sm">{value}</div></div>)}
          </div>
          <section className="space-y-3" aria-label={en ? "Coverage" : "测试覆盖"}>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="micro-label">{en ? "Coverage" : "测试覆盖"} · {en ? "Agent reported" : "智能体记录"}</h3>
              {recordedCoverage && <label className="flex cursor-pointer items-center gap-2 text-xs text-fg-muted"><input type="checkbox" className="accent-accent" checked={unresolvedOnly} onChange={(event) => setUnresolvedOnly(event.target.checked)} />{en ? "Unresolved only" : "只看未完成项目"}</label>}
            </div>
            {!recordedCoverage ? <p className="text-xs text-fg-muted">{unrecorded}</p> : visible.length === 0 ? (
              <p className="text-xs text-fg-muted">{en ? "No matching recorded items. Full coverage remains unverified." : "没有符合条件的已记录项目，完整覆盖范围仍未经验证。"}</p>
            ) : visible.map((entry, index) => <CoverageRow key={entry.id || entry.entry_id || index} entry={entry} en={en} />)}
          </section>
          <section className="space-y-3 border-t border-line/6 pt-4" aria-label={en ? "Threat models" : "威胁模型"}>
            <h3 className="micro-label">{en ? "Threat models" : "威胁模型"}</h3>
            {!recordedModels && <p className="text-xs text-fg-muted">{unrecorded}</p>}
            {models.map((model, index) => (
              <details key={`${model.target}-${index}`} className="rounded-xl border border-line/8 bg-surface/42 p-3">
                <summary className="cursor-pointer break-words text-sm font-medium text-fg">{model.target}</summary>
                <p className="mt-2 text-[10px] text-fg-muted">{[model.written_by_name || model.written_by, model.updated_at, model.revision != null ? `${en ? "Revision" : "版本"} ${model.revision}` : ""].filter(Boolean).join(" · ")}</p>
                <div className="prose-report mt-3"><ReactMarkdown remarkPlugins={[remarkGfm]}>{model.content}</ReactMarkdown></div>
                {Array.isArray(model.amendments) && model.amendments.length > 0 && (
                  <div className="mt-3 space-y-3 border-t border-line/6 pt-3">
                    <div className="micro-label text-[9px]">{en ? "Amendments" : "补充修订"}</div>
                    {model.amendments.map((amendment, amendmentIndex) => (
                      <div key={amendmentIndex} className="border-l-2 border-accent/30 pl-3">
                        <p className="text-[10px] text-fg-muted">{[amendment.agent_name || amendment.agent_id, amendment.timestamp].filter(Boolean).join(" · ")}</p>
                        <div className="prose-report mt-1"><ReactMarkdown remarkPlugins={[remarkGfm]}>{amendment.content}</ReactMarkdown></div>
                      </div>
                    ))}
                  </div>
                )}
                <History entries={model.history} en={en} />
              </details>
            ))}
          </section>
          {data.generated_at && <p className="font-mono text-[10px] text-fg-faint">{en ? "Record updated" : "记录更新"} · {data.generated_at}</p>}
        </>
      )}
    </div>
  );
}
