"use client";

/* ============================================================================
   /insights — skill usage analytics: top skills bar chart, category mix,
   cross-run trend, per-run table. Data: GET /api/analytics/skills.
   ========================================================================= */

import * as React from "react";
import Link from "next/link";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip as ChartTooltip,
  XAxis,
  YAxis,
} from "recharts";
import { RotateCw } from "lucide-react";
import { Chip, EmptyState, MicroLabel, MetricCard, Panel, Spinner } from "@/components/ui";
import { getJSON } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { fmtTime } from "@/lib/format";

interface SkillUsage {
  skill: string;
  loads: number;
  runs: number;
  category: string;
}

interface CategoryUsage {
  category: string;
  loads: number;
}

interface RunSkillUsage {
  run: string;
  target: string;
  status: string;
  start_time: string;
  skills: Record<string, number>;
  total: number;
}

interface AnalyticsPage {
  skills: SkillUsage[];
  categories: CategoryUsage[];
  runs: RunSkillUsage[];
  totals: {
    distinct_skills: number;
    total_loads: number;
    runs_with_skills: number;
  };
}

const CHART_COLORS = [
  "#536ddb",
  "#368c78",
  "#755bb9",
  "#b97a29",
  "#cf565b",
  "#5e8dbe",
  "#68768a",
  "#8ca2ff",
];

export default function InsightsPage() {
  const { t, locale } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [data, setData] = React.useState<AnalyticsPage | null>(null);
  const [refreshing, setRefreshing] = React.useState(false);
  const [updatedAt, setUpdatedAt] = React.useState<string | null>(null);
  const busy = React.useRef(false);

  const load = React.useCallback(async () => {
    if (busy.current) return;
    busy.current = true;
    setRefreshing(true);
    setPhase("loading");
    try {
      const page = await getJSON<AnalyticsPage>("/api/analytics/skills");
      setData(page);
      setPhase("ready");
      setUpdatedAt(new Date().toISOString());
    } catch {
      setPhase("error");
    } finally {
      busy.current = false;
      setRefreshing(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const topSkills = React.useMemo(
    () => (data ? data.skills.slice(0, 12).map((s) => ({ name: shortName(s.skill), ...s })) : []),
    [data]
  );

  const trend = React.useMemo(
    () =>
      data
        ? [...data.runs]
            .sort((a, b) => a.start_time.localeCompare(b.start_time))
            .map((r) => ({
              run: r.run,
              loads: r.total,
              distinct: Object.keys(r.skills).length,
            }))
        : [],
    [data]
  );

  return (
    <div className="insights-page">
      <header className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">&gt; {t("insights.path")}</div>
          <h1 className="page-title">{t("insights.title")}</h1>
          <p className="page-copy">{t("insights.copy")}</p>
        </div>
        <div className="hero-actions">
          {updatedAt && <MicroLabel>{t("insights.updatedAt", { time: fmtTime(updatedAt, locale) })}</MicroLabel>}
          <button type="button" className="button-secondary button-compact" onClick={() => void load()} disabled={refreshing} aria-busy={refreshing}>
            <RotateCw className={`h-3.5 w-3.5 ${refreshing ? "animate-spin motion-reduce:animate-none" : ""}`} aria-hidden="true" />
            {t(refreshing ? "insights.refreshing" : "insights.refresh")}
          </button>
        </div>
      </header>

      {phase === "error" && (
        <div className="alert-error flex items-center justify-between gap-3" role="alert">
          <span>{t(data ? "insights.staleError" : "insights.loadError")}</span>
          <button className="button-secondary button-compact" onClick={() => void load()} disabled={refreshing}>
            <RotateCw className="h-3.5 w-3.5" /> {t("common.retry")}
          </button>
        </div>
      )}

      {!data && phase === "loading" ? (
        <div className="panel panel-hairline flex items-center gap-2 p-6 text-sm text-fg-muted" role="status">
          <Spinner /> {t("common.loading")}
        </div>
      ) : !data ? null : data.totals.total_loads === 0 ? (
        <div className="panel panel-hairline p-4">
          <EmptyState
            title={t("common.empty")}
            hint={t("insights.emptyHint")}
          />
        </div>
      ) : (
        <div className="space-y-4">
          {/* metric tiles */}
          <div className="dashboard-metrics insights-metrics">
            <MetricCard code="LOAD" label={t("insights.skillLoads")} value={String(data.totals.total_loads).padStart(2, "0")} matrix tone="accent" />
            <MetricCard code="SKL" label={t("insights.distinctSkills")} value={String(data.totals.distinct_skills).padStart(2, "0")} matrix tone="violet" />
            <MetricCard code="RUN" label={t("insights.runsWithSkills")} value={String(data.totals.runs_with_skills).padStart(2, "0")} matrix tone="success" />
          </div>

          <div className="grid gap-4 xl:grid-cols-[1.35fr_0.85fr]">
            {/* top skills bar chart */}
            <Panel code="TOP" title={t("insights.skillLoads")} actions={<MicroLabel>{t("insights.top", { n: topSkills.length })}</MicroLabel>}>
              <div className="h-80">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={topSkills} layout="vertical" margin={{ left: 8, right: 16, top: 4, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--raise-rgb) / 0.08)" />
                    <XAxis type="number" stroke="var(--text-muted)" fontSize={11} fontFamily="var(--font-mono)" allowDecimals={false} />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={150}
                      stroke="var(--text-muted)"
                      fontSize={11}
                      fontFamily="var(--font-mono)"
                      interval={0}
                    />
                    <ChartTooltip
                      contentStyle={{
                        background: "var(--bg-panel-solid)",
                        border: "1px solid var(--border-primary)",
                        borderRadius: 3,
                        fontFamily: "var(--font-mono)",
                        fontSize: 11,
                        color: "var(--text-primary)",
                      }}
                    />
                    <Bar dataKey="loads" name={t("insights.skillLoads")} radius={0}>
                      {topSkills.map((_, i) => (
                        <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Panel>

            {/* category mix pie */}
            <Panel code="MIX" title={t("insights.category")}>
              <div className="h-80">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={data.categories}
                      dataKey="loads"
                      nameKey="category"
                      innerRadius="45%"
                      outerRadius="75%"
                      paddingAngle={2}
                    >
                      {data.categories.map((_, i) => (
                        <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} stroke="none" />
                      ))}
                    </Pie>
                    <ChartTooltip
                      contentStyle={{
                        background: "var(--bg-panel-solid)",
                        border: "1px solid var(--border-primary)",
                        borderRadius: 3,
                        fontFamily: "var(--font-mono)",
                        fontSize: 11,
                        color: "var(--text-primary)",
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div className="flex flex-wrap gap-1.5 pt-2">
                {data.categories.map((c, i) => (
                  <span key={c.category} className="mono-chip">
                    <span
                      className="mr-1 inline-block h-2 w-2 rounded-full"
                      style={{ background: CHART_COLORS[i % CHART_COLORS.length] }}
                      aria-hidden
                    />
                    {c.category} · {c.loads}
                  </span>
                ))}
              </div>
            </Panel>
          </div>

          {/* cross-run trend */}
          <Panel code="TREND" title={t("insights.trend")}>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trend} margin={{ left: 8, right: 16, top: 8, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--raise-rgb) / 0.08)" />
                  <XAxis dataKey="run" tickFormatter={(name: string) => name.split("_").at(-1) || name} stroke="var(--text-muted)" fontSize={11} fontFamily="var(--font-mono)" />
                  <YAxis stroke="var(--text-muted)" fontSize={11} fontFamily="var(--font-mono)" allowDecimals={false} />
                  <ChartTooltip
                    contentStyle={{
                      background: "var(--bg-panel-solid)",
                      border: "1px solid var(--border-primary)",
                      borderRadius: 3,
                      fontFamily: "var(--font-mono)",
                      fontSize: 11,
                      color: "var(--text-primary)",
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line type="monotone" dataKey="loads" name={t("insights.skillLoads")} stroke="var(--signal-accent)" strokeWidth={2} dot={{ r: 3, fill: "var(--signal-accent)" }} />
                  <Line type="monotone" dataKey="distinct" name={t("insights.distinctSkills")} stroke="var(--signal-success)" strokeWidth={1.5} dot={{ r: 2, fill: "var(--signal-success)" }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </Panel>

          {/* per-run table */}
          <Panel code="RUNS" title={t("insights.runDetail")}>
            <div className="table-wrap">
              <table className="table-shell">
                <thead>
                  <tr className="table-head">
                    <th>{t("insights.table.run")}</th>
                    <th>{t("insights.table.target")}</th>
                    <th>{t("insights.table.status")}</th>
                    <th>{t("insights.table.skills")}</th>
                    <th>{t("insights.table.loads")}</th>
                    <th>{t("insights.table.started")}</th>
                  </tr>
                </thead>
                <tbody>
                  {data.runs.map((r) => (
                    <tr key={r.run} className="table-row">
                      <td className="font-mono text-xs"><Link href={`/run?name=${encodeURIComponent(r.run)}&tab=report`} className="text-accent underline-offset-4 hover:underline">{r.run}</Link></td>
                      <td className="max-w-[14rem] truncate font-mono text-xs">{r.target || "—"}</td>
                      <td>
                        <Chip
                          tone={
                            r.status === "completed"
                              ? "success"
                              : r.status === "failed" || r.status === "crashed"
                                ? "danger"
                                : "neutral"
                          }
                        >
                          {t(`status.${r.status.toLowerCase()}`)}
                        </Chip>
                      </td>
                      <td className="max-w-[22rem] truncate text-xs text-fg-muted" title={Object.keys(r.skills).join(", ")}>
                        {Object.keys(r.skills).slice(0, 5).join(", ")}
                        {Object.keys(r.skills).length > 5 ? ` +${Object.keys(r.skills).length - 5}` : ""}
                      </td>
                      <td className="font-mono text-xs">{r.total}</td>
                      <td className="font-mono text-[10px] text-fg-faint">{fmtTime(r.start_time, locale)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}

function shortName(skill: string): string {
  // "vulnerabilities/sql_injection" -> "sql_injection" when it fits better
  const parts = skill.split("/");
  return parts.length > 1 ? parts[parts.length - 1] : skill;
}
