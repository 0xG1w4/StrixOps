"use client";

import * as React from "react";
import { Panel } from "@/components/ui";
import type { RunDetail } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useI18n } from "@/lib/i18n";

export default function AgentContextPanel({ run }: { run: RunDetail }) {
  const { t, locale } = useI18n();
  const counts = new Intl.NumberFormat(locale);
  const percentages = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  const ids = [...new Set([...Object.keys(run.agents || {}), ...Object.keys(run.agent_context || {})])]
    .sort((a, b) => Number(run.agents?.[b]?.parent_id === null) - Number(run.agents?.[a]?.parent_id === null));
  const snapshot = run.model_context;
  const referenceCapacity = snapshot?.capacity_source === "model_catalog" || snapshot?.capacity_source === "configured_fallback";
  const probe = snapshot?.probe;
  const showProbe = probe && probe.status !== "disabled" && probe.status !== "skipped_metadata";
  const accepted = probe?.largest_accepted_input_tokens;

  return (
    <Panel code="CTX" title={t("run.agentContext.title")}>
      <div className="space-y-2 border-b border-line/6 px-4 py-3 text-xs leading-relaxed text-fg-muted">
        <p>{t("run.agentContext.hint")}</p>
        {snapshot && (
          <p className={snapshot.capacity_source === "configured_fallback" ? "text-warning" : undefined}>
            {t("run.agentContext.capacitySource", { source: t(`run.context.source.${snapshot.capacity_source}`) })}
            {referenceCapacity && <span> · {t("run.agentContext.reference")}</span>}
          </p>
        )}
        {showProbe && (
          <p className={probe.status === "failed" || probe.status === "timeout" || probe.status === "unverified" ? "text-warning" : undefined}>
            {t(`run.agentContext.probe.${probe.status}`)}
            {typeof accepted === "number" && Number.isFinite(accepted) && accepted >= 0 && (
              <span> · {t("run.agentContext.probe.accepted", { n: counts.format(accepted) })}</span>
            )}
          </p>
        )}
      </div>
      {ids.length === 0 ? (
        <p className="px-4 py-4 text-sm text-fg-muted">{t("run.context.unrecorded")}</p>
      ) : (
        <ul className="max-h-80 overflow-y-auto" aria-label={t("run.agentContext.title")}>
          {ids.map((id) => {
            const agent = run.agents?.[id];
            const context = run.agent_context?.[id];
            const name = agent?.name || context?.agent_name || id;
            const role = agent?.parent_id === null ? "root" : agent?.parent_id ? "child" : "agent";
            const input = context && Number.isFinite(context.input_tokens) && context.input_tokens >= 0 ? context.input_tokens : null;
            const modelMatches = !context?.model || context.model === snapshot?.model;
            const capacity = snapshot && modelMatches && Number.isFinite(snapshot.capacity_tokens) && snapshot.capacity_tokens > 0
              ? snapshot.capacity_tokens : null;
            const percent = input !== null && capacity !== null ? input / capacity * 100 : null;
            const estimated = context?.source === "estimate";
            const approximatePercent = estimated || referenceCapacity;
            const used = input === null ? "—" : `${estimated ? "≈" : ""}${counts.format(input)}`;
            const fraction = t("run.agentContext.fraction", { used, capacity: capacity === null ? "—" : counts.format(capacity) });
            const percentage = percent === null ? "" : `${approximatePercent ? "≈" : ""}${percentages.format(percent)}%`;
            const overflow = percent !== null && percent > 100;
            const requesting = context?.phase === "request" && run.live && (!agent || agent.status === "running");
            const timestamp = context?.updated_at && Number.isFinite(Date.parse(context.updated_at)) ? fmtTime(context.updated_at, locale) : null;

            return (
              <li key={id} className="space-y-2 border-b border-line/6 px-4 py-3 last:border-b-0" aria-label={name}>
                <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="shrink-0 text-[10px] uppercase tracking-wide text-fg-muted">{t(`run.agentContext.role.${role}`)}</span>
                    <span className="break-all font-mono text-xs font-semibold text-fg" title={id}>{name}</span>
                  </div>
                  <div className={`font-mono text-xs tabular-nums ${overflow ? "text-danger" : "text-fg"}`}>
                    {fraction}{percent !== null && <span className="ml-2">{percentage}</span>}
                  </div>
                </div>
                {percent !== null && (
                  <div role="progressbar" aria-label={t("run.agentContext.progress", { name })}
                    aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, percent)}
                    aria-valuetext={`${fraction} · ${percentage}${referenceCapacity ? ` · ${t("run.agentContext.reference")}` : ""}`}
                    className="h-1.5 overflow-hidden rounded-full bg-line/10">
                    <div className={`h-full rounded-full ${overflow ? "bg-danger" : "bg-accent"}`} style={{ width: `${Math.min(100, percent)}%` }} />
                  </div>
                )}
                <div className="flex flex-wrap gap-x-2 gap-y-1 text-[10px] leading-relaxed text-fg-muted">
                  {input === null ? <span>{t("run.context.unrecorded")}</span> : (
                    <>
                      <span>{t(requesting ? "run.agentContext.request" : "run.agentContext.response")}</span>
                      <span>· {t(estimated ? "run.agentContext.estimate" : "run.agentContext.reported")}</span>
                      {timestamp && <time dateTime={context?.updated_at}>· {t("run.agentContext.updated", { time: timestamp })}</time>}
                    </>
                  )}
                  {capacity === null && <span>{t("run.agentContext.capacityUnknown")}</span>}
                  {context?.model && !modelMatches && <span className="break-all">{context.model}</span>}
                  {overflow && <span className="text-danger">{t(referenceCapacity ? "run.agentContext.overReference" : "run.agentContext.overCapacity")}</span>}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
