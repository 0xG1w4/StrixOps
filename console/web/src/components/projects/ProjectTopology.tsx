"use client";

import * as React from "react";
import Link from "next/link";
import { ArrowRight, Check, CircleAlert, Eye, EyeOff, KeyRound, Maximize2, Minimize2, Network, RefreshCw, Search, Server, X } from "lucide-react";
import { Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { generateProjectTopology, getProjectTopology, getProjectTopologyStatus, revealTopologyCredential, type ProjectTopologyResponse, type TopologyCredential, type TopologyEdge, type TopologyNode, type TopologyRecord, type TopologySecret, type TopologySnapshot } from "@/lib/topology";
import TopologyCanvas from "./TopologyCanvas";
import { getNodeGrouping, groupTopologyNodes, type GroupingBasis } from "@/lib/topology-graph";
import styles from "./ProjectTopology.module.css";

const COPY = {
  "zh-CN": {
    title: "内网拓扑", hint: "在同一张画布中查看 CIDR 分组与主机关联。",
    generate: "生成拓扑", regenerate: "重新生成", generating: "生成中…", loading: "正在读取拓扑…",
    refresh: "检查更新", retry: "重试", loadFailed: "无法读取拓扑，请重试。", generateFailed: "生成失败，已有拓扑仍保留。请重试。",
    manual: "拓扑由你手动生成", manualHint: "汇总项目内网任务已记录的主机、关系、漏洞与凭据。生成后保存为快照。",
    noTasks: "此项目还没有内网任务", noTasksHint: "创建内网任务并发现主机后，即可生成拓扑。",
    noHosts: "尚未收录主机", noHostsHint: "已检查内网任务，但没有可识别的主机记录。任务收录主机后，请重新生成。",
    missing: "来源数据暂时不可用。恢复任务数据后，请检查更新。",
    partial: "部分来源资料不完整。此拓扑只包含已读取的记录。", warnings: "来源提示", sourceWarning: "此来源有未完整收录的资料", freshnessUnknown: "暂时无法检查最新资料。当前显示已保存的拓扑，请稍后检查更新。",
    stale: "任务资料已有变化，点击“重新生成”以更新拓扑。", generated: "生成于", version: "版本",
    hosts: "主机", subnets: "网络分组", relations: "关系", tasks: "内网任务", withCredentials: "含凭据的主机",
    search: "搜索主机名称、IP、网段或网络环境", allGroups: "全部网络分组", unknownSubnet: "未归属", unknownContext: "未指定网络环境",
    allRisk: "全部风险", highRisk: "高风险", credentialsOnly: "有凭据", filters: "高亮主机", noMatch: "没有匹配的主机，仍保留完整拓扑。", reset: "清除高亮",
    observed: "已识别", reachable: "已确认可达", unnamed: "未记录主机名称", membership: "地址分组与扫描范围不等于已确认网段；连线来自主机关系记录",
    verified: "已记录验证", unverified: "未验证关系", edgeHint: "验证状态来自任务记录；查看来源证据确认细节。",
    matched: "匹配主机", fullscreen: "全屏画布", exitFullscreen: "退出全屏", groupBasis: "分组依据", groupCidr: "CIDR 分组",
    recorded_subnet: "已记录网段", scan_range: "扫描范围", address_group: "地址分组", unassigned: "未归属", mixed: "多种分组依据",
    groupUpgrade: "旧快照按已记录网段或地址分组显示。重新生成可纳入旧任务保存的扫描范围，无需重新扫描。",
    subnetConflict: "来源的网段记录存在冲突，当前按扫描范围或地址分组展示。", overviewTab: "概览", vulnTab: "漏洞", findingTab: "发现", credentialTab: "凭据", sourceTab: "来源",
    relationships: "主机关联", relationshipDetails: "关系详情", relationBundleHint: "这里汇总的是具体主机之间的关系，不代表整个网段互通。", from: "来源主机", to: "目标主机",
    details: "主机详情", close: "关闭详情", vulnerabilities: "漏洞", findings: "发现", credentials: "凭据", sources: "来源与关系",
    noVulns: "此主机没有关联的漏洞记录。", noFindings: "此主机没有关联的发现记录。", noCredentials: "此主机没有明确关联的凭据。",
    noSources: "未记录来源证据。", noRelations: "此主机尚无主机间关系记录。", context: "网络环境", subnet: "网段", role: "角色", os: "操作系统",
    firstSeen: "首次发现", lastSeen: "最近发现", backfilled: "从历史资料回补", related: "关联主机", evidence: "来源证据", openTask: "打开来源任务",
    username: "账号", password: "密码", emptyPassword: "空密码", hash: "哈希", validation: "验证状态", show: "显示秘密", hide: "隐藏秘密", revealing: "正在读取…",
    revealFailed: "无法读取凭据。来源可能已更改，请检查更新并重新生成。", noSecret: "没有可显示的密码或哈希。", secretHint: "仅在点击显示时读取当前来源的秘密；关闭详情后会清除。",
    unknown: "未记录", confirmed: "已验证", untested: "未验证", invalid: "无效", failed: "验证失败", valid: "有效", partialStatus: "部分验证", source: "来源", agent: "Agent",
    critical: "严重", high: "高危", medium: "中危", low: "低危", info: "信息", unknownRisk: "未评级",
  },
  en: {
    title: "Internal topology", hint: "Explore CIDR groups and host relationships on one canvas.",
    generate: "Generate topology", regenerate: "Regenerate", generating: "Generating…", loading: "Loading topology…",
    refresh: "Check for updates", retry: "Retry", loadFailed: "Could not load topology. Try again.", generateFailed: "Generation failed. The existing topology is preserved. Try again.",
    manual: "Generate your topology", manualHint: "Combine recorded hosts, relationships, findings, and credentials from this project's internal tasks into a saved snapshot.",
    noTasks: "No internal tasks in this project", noTasksHint: "Create an internal task and discover hosts to generate a topology.",
    noHosts: "No hosts recorded yet", noHostsHint: "Internal tasks were checked, but no host records were found. Regenerate after tasks record hosts.",
    missing: "Source data is unavailable. Check for updates after task data is restored.",
    partial: "Some source data is incomplete. This topology includes only the records that could be read.", warnings: "Source notices", sourceWarning: "Some records from this source could not be included", freshnessUnknown: "Unable to check the latest data. The saved topology is shown; check for updates again later.",
    stale: "Task data has changed. Select Regenerate to update this topology.", generated: "Generated", version: "Version",
    hosts: "Hosts", subnets: "Network groups", relations: "Relationships", tasks: "Internal tasks", withCredentials: "Hosts with credentials",
    search: "Search hostname, IP, subnet, or network context", allGroups: "All network groups", unknownSubnet: "Unassigned", unknownContext: "Network context unspecified",
    allRisk: "All risk levels", highRisk: "High risk", credentialsOnly: "With credentials", filters: "Highlight hosts", noMatch: "No matching hosts. The full topology remains visible.", reset: "Clear highlights",
    observed: "Identified", reachable: "Reachability confirmed", unnamed: "Hostname not recorded", membership: "Address groups and scan ranges are not confirmed subnets; edges reflect recorded host relationships",
    verified: "Recorded as verified", unverified: "Unverified relationship", edgeHint: "Verification comes from task records. Inspect source evidence for details.",
    matched: "Matching hosts", fullscreen: "Fullscreen canvas", exitFullscreen: "Exit fullscreen", groupBasis: "Group basis", groupCidr: "CIDR group",
    recorded_subnet: "Recorded subnet", scan_range: "Scan range", address_group: "Address group", unassigned: "Unassigned", mixed: "Multiple grouping sources",
    groupUpgrade: "This older snapshot uses recorded subnets or address groups. Regenerate to include saved scan ranges; no rescan is needed.",
    subnetConflict: "Source subnet records conflict. This host is displayed using a scan range or address group.", overviewTab: "Overview", vulnTab: "Vulns", findingTab: "Findings", credentialTab: "Credentials", sourceTab: "Sources",
    relationships: "Host relationships", relationshipDetails: "Relationship details", relationBundleHint: "These are relationships between specific hosts, not connectivity between entire subnets.", from: "Source host", to: "Target host",
    details: "Host details", close: "Close details", vulnerabilities: "Vulnerabilities", findings: "Findings", credentials: "Credentials", sources: "Sources & relationships",
    noVulns: "No vulnerability records are linked to this host.", noFindings: "No finding records are linked to this host.", noCredentials: "No credentials are explicitly linked to this host.",
    noSources: "No source evidence recorded.", noRelations: "No host-to-host relationships recorded for this host.", context: "Network context", subnet: "Subnet", role: "Role", os: "Operating system",
    firstSeen: "First seen", lastSeen: "Last seen", backfilled: "Recovered from historical records", related: "Related hosts", evidence: "Source evidence", openTask: "Open source task",
    username: "Username", password: "Password", emptyPassword: "Empty password", hash: "Hash", validation: "Validation", show: "Show secrets", hide: "Hide secrets", revealing: "Retrieving…",
    revealFailed: "Could not read this credential. Its source may have changed. Check for updates and regenerate.", noSecret: "No password or hash is available.", secretHint: "Secrets are read from the current source only when revealed, and cleared when details close.",
    unknown: "Not recorded", confirmed: "Verified", untested: "Unverified", invalid: "Invalid", failed: "Validation failed", valid: "Valid", partialStatus: "Partially verified", source: "Source", agent: "Agent",
    critical: "Critical", high: "High", medium: "Medium", low: "Low", info: "Info", unknownRisk: "Unrated",
  },
} as const;

type Copy = (typeof COPY)[keyof typeof COPY];
type DetailTab = "overview" | "vulnerabilities" | "findings" | "credentials" | "sources";
const DETAIL_TABS: DetailTab[] = ["overview", "vulnerabilities", "findings", "credentials", "sources"];
const EMPTY_NODES: TopologyNode[] = [];
const EMPTY_EDGES: TopologyEdge[] = [];
const nodeName = (node: TopologyNode) => node.hostname || node.ip || node.address;
const field = (record: TopologyRecord, name: string) => typeof record[name] === "string" ? record[name] as string : "";
const taskHref = (name: string) => `/run?name=${encodeURIComponent(name)}`;
function timestamp(value: string, locale: string): string {
  const date = new Date(value);
  return value && Number.isFinite(date.getTime()) ? date.toLocaleString(locale) : "—";
}
function riskLabel(value: string, copy: Copy) {
  return ({ critical: copy.critical, high: copy.high, medium: copy.medium, low: copy.low, info: copy.info, informational: copy.info } as Record<string, string>)[value.toLowerCase()] ?? copy.unknownRisk;
}
function RiskBadge({ severity, copy }: { severity: string; copy: Copy }) {
  const risk = severity.toLowerCase();
  const tone = ["critical", "high", "medium", "low"].includes(risk) ? risk : "info";
  return <span className={`sev-chip sev-${tone}`}>{riskLabel(severity, copy)}</span>;
}
function warningLabel(code: string, locale: string, fallback: string) {
  const labels: Record<string, [string, string]> = {
    recorded_subnet_conflict: ["同一主机的网段记录存在冲突，当前使用扫描范围或地址分组。", "A host has conflicting subnet records; a scan range or address group is shown."],
    scan_ranges_unreadable: ["部分任务的扫描范围无法读取，已按可用主机地址分组。", "Some scan ranges could not be read; available host addresses are used for grouping."],
    source_unreadable: ["无法读取部分任务来源。", "Some task sources could not be read."],
    legacy_inventory: ["旧任务没有完整主机清单，已从保留资料回补可识别的主机。", "This older task has no complete host inventory; identifiable hosts were recovered from retained records."],
    host_inventory_unreadable: ["无法读取任务的主机清单。", "The task's host inventory could not be read."],
    observations_unreadable: ["内网观察记录无法读取。", "Internal observations could not be read."],
    findings_unreadable: ["无法读取部分漏洞或发现记录。", "Some vulnerability or finding records could not be read."],
    credentials_partial: ["部分凭据记录不完整。", "Some credential records are incomplete."],
    credentials_unreadable: ["无法读取凭据来源。", "Credential sources could not be read."],
    unmatched_records: ["部分记录没有明确关联的主机，尚未编入拓扑。", "Some records could not be linked to a specific host and were not included."],
    ambiguous_host: ["部分主机身份不明确，未自动合并。", "Some host identities are ambiguous and were not merged automatically."],
    relation_unmatched: ["部分关系无法匹配到主机节点，尚未绘制。", "Some relationships could not be matched to host nodes and were not drawn."],
    sources_changed: ["生成期间来源资料发生变化，重新生成可纳入最新记录。", "Sources changed during generation. Regenerate to include the latest records."],
  };
  return labels[code]?.[locale === "zh-CN" ? 0 : 1] ?? fallback;
}
function relationLabel(value: string, locale: string) {
  const labels: Record<string, [string, string]> = {
    connectivity: ["网络连通", "Connectivity"], pivot: ["跳板路径", "Pivot path"],
    trust: ["信任关系", "Trust relationship"], other: ["其他关系", "Other relationship"],
    reachable_from: ["访问路径", "Reachability"], observed_from: ["发现路径", "Discovery path"],
  };
  return labels[value]?.[locale === "zh-CN" ? 0 : 1] ?? value;
}

export default function ProjectTopology({ projectId }: { projectId: string }) {
  const { locale } = useI18n();
  const copy = COPY[locale];
  const [data, setData] = React.useState<ProjectTopologyResponse | null>(null);
  const [busy, setBusy] = React.useState<"read" | "generate" | null>("read");
  const [error, setError] = React.useState<"read" | "generate" | null>(null);
  const [freshnessUnknown, setFreshnessUnknown] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [group, setGroup] = React.useState("");
  const [risk, setRisk] = React.useState("all");
  const [credentialOnly, setCredentialOnly] = React.useState(false);
  const [selectedEdgeIds, setSelectedEdgeIds] = React.useState<string[]>([]);
  const [fullscreen, setFullscreen] = React.useState(false);
  const container = React.useRef<HTMLElement | null>(null);
  const detailPanel = React.useRef<HTMLElement | null>(null);
  const searchInput = React.useRef<HTMLInputElement | null>(null);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const origin = React.useRef<HTMLButtonElement | null>(null);
  const request = React.useRef<AbortController | null>(null);
  const generation = React.useRef(0);
  const fingerprint = React.useRef("");
  const loaded = React.useRef(false);

  const load = React.useCallback(async (action: "read" | "generate", quiet = false) => {
    if (quiet && (request.current || !loaded.current)) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const sequence = ++generation.current;
    if (!quiet) { setBusy(action); setError(null); }
    const timeout = window.setTimeout(() => controller.abort(), 60_000);
    try {
      if (quiet) {
        const status = await getProjectTopologyStatus(projectId, fingerprint.current, controller.signal);
        if (sequence !== generation.current) return;
        setData((current) => {
          if (!current) return current;
          const warnings = [...new Map([...(current.snapshot?.warnings ?? []), ...status.warnings].map((warning) => [JSON.stringify([warning.code, warning.run]), warning])).values()];
          return { ...current, ...status, warnings };
        });
      } else {
        const result = action === "generate"
          ? await generateProjectTopology(projectId, controller.signal)
          : await getProjectTopology(projectId, controller.signal);
        if (sequence !== generation.current) return;
        fingerprint.current = result.snapshot?.source_fingerprint ?? "";
        loaded.current = true;
        setData(result);
      }
      setFreshnessUnknown(false);
      setError(null);
    } catch {
      if (sequence === generation.current && action === "read") setFreshnessUnknown(true);
      if (sequence === generation.current && !quiet) setError(action);
    } finally {
      window.clearTimeout(timeout);
      if (sequence === generation.current) { request.current = null; setBusy(null); }
    }
  }, [projectId]);

  React.useEffect(() => {
    loaded.current = false; fingerprint.current = "";
    setData(null); setSelectedId(null); setSelectedEdgeIds([]);
    void load("read");
    // Reads only check freshness; generation is exclusively a user action.
    const poll = window.setInterval(() => { if (document.visibilityState === "visible") void load("read", true); }, 30_000);
    const clear = () => { generation.current += 1; loaded.current = false; request.current?.abort(); setData(null); setSelectedId(null); setSelectedEdgeIds([]); };
    window.addEventListener("strixops:auth-cleared", clear);
    return () => {
      generation.current += 1; request.current?.abort(); request.current = null;
      window.clearInterval(poll); window.removeEventListener("strixops:auth-cleared", clear);
    };
  }, [load]);

  const snapshot = data?.snapshot;
  const nodes = snapshot?.nodes ?? EMPTY_NODES;
  const edges = snapshot?.edges ?? EMPTY_EDGES;
  const nodeIndex = React.useMemo(() => new Map(nodes.map(node => [node.id, node])), [nodes]);
  const selected = selectedId ? nodeIndex.get(selectedId) ?? null : null;
  const selectedEdges = React.useMemo(() => {
    const ids = new Set(selectedEdgeIds);
    return edges.filter(edge => ids.has(edge.id));
  }, [edges, selectedEdgeIds]);
  React.useEffect(() => {
    if (selectedId && !nodeIndex.has(selectedId)) setSelectedId(null);
    const currentEdges = new Set(edges.map(edge => edge.id));
    setSelectedEdgeIds(ids => ids.some(id => !currentEdges.has(id)) ? ids.filter(id => currentEdges.has(id)) : ids);
  }, [nodeIndex, edges, selectedId]);
  const groups = React.useMemo(() => groupTopologyNodes(nodes), [nodes]);
  React.useEffect(() => {
    if (group && !groups.some(entry => entry.id === group)) setGroup("");
  }, [groups, group]);
  const hasFilters = !!(query.trim() || group || risk !== "all" || credentialOnly);
  const matching = React.useMemo(() => nodes.filter(node => {
    const grouping = getNodeGrouping(node);
    return (!group || grouping.key === group)
      && (risk !== "high" || ["critical", "high"].includes(node.severity.toLowerCase()))
      && (!credentialOnly || node.credentials.length > 0)
      && (!query.trim() || [node.hostname, node.ip, node.address, grouping.cidr, grouping.context].some(value => value.toLowerCase().includes(query.trim().toLowerCase())));
  }), [nodes, query, group, risk, credentialOnly]);
  const highlightIds = React.useMemo(() => hasFilters ? matching.map(node => node.id) : undefined, [hasFilters, matching]);
  const reset = () => { setQuery(""); setGroup(""); setRisk("all"); setCredentialOnly(false); };
  const closeDetails = React.useCallback(() => {
    setSelectedId(null); setSelectedEdgeIds([]);
    requestAnimationFrame(() => { if (origin.current?.isConnected) origin.current.focus({ preventScroll: true }); else searchInput.current?.focus({ preventScroll: true }); });
  }, []);
  const selectHost = React.useCallback((id: string) => { setSelectedId(id); setSelectedEdgeIds([]); }, []);
  const onSelect = React.useCallback((node: TopologyNode, element: HTMLButtonElement) => { origin.current = element; selectHost(node.id); }, [selectHost]);
  const onSelectEdges = React.useCallback((selection: TopologyEdge[]) => { setSelectedId(null); setSelectedEdgeIds(selection.map(edge => edge.id)); }, []);
  React.useEffect(() => {
    if (!selectedId && !selectedEdgeIds.length) return;
    const frame = requestAnimationFrame(() => {
      const panel = detailPanel.current;
      if (!panel) return;
      if (window.innerWidth <= 800 || (container.current?.clientWidth ?? 0) <= 780) panel.scrollIntoView({ block: "start" });
      panel.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedId, selectedEdgeIds]);
  React.useEffect(() => {
    const update = () => setFullscreen(document.fullscreenElement === container.current);
    document.addEventListener("fullscreenchange", update);
    return () => document.removeEventListener("fullscreenchange", update);
  }, []);
  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement === container.current) await document.exitFullscreen();
      else await container.current?.requestFullscreen();
    } catch { /* Browsers may disable fullscreen; the canvas remains usable inline. */ }
  };
  return <section ref={container} className={styles.root} aria-label={copy.title} onKeyDown={event => {
    if (event.key === "Escape" && (selected || selectedEdges.length)) { event.stopPropagation(); closeDetails(); }
  }}>
    <header className={styles.header}>
      <div><h2><Network size={17} aria-hidden="true" />{copy.title}</h2><p>{copy.hint}</p></div>
      <div className={styles.actions}>
        <button type="button" className={styles.iconButton} aria-label={fullscreen ? copy.exitFullscreen : copy.fullscreen} title={fullscreen ? copy.exitFullscreen : copy.fullscreen} onClick={() => void toggleFullscreen()}>{fullscreen ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button>
        <button type="button" className={styles.secondary} disabled={busy !== null} onClick={() => void load("read")}><RefreshCw size={14} aria-hidden="true" />{copy.refresh}</button>
        <button type="button" className={styles.primary} disabled={busy !== null || !data || data.eligible_runs === 0 || data.source_status === "missing"} onClick={() => void load("generate")}>
          {busy === "generate" ? <Spinner /> : <Network size={15} aria-hidden="true" />}{busy === "generate" ? copy.generating : snapshot ? copy.regenerate : copy.generate}
        </button>
      </div>
    </header>
    {error && <div role="alert" className={styles.error}><CircleAlert size={15} aria-hidden="true" />{error === "generate" ? copy.generateFailed : copy.loadFailed}<button type="button" disabled={busy !== null} onClick={() => void load(error)}>{copy.retry}</button></div>}
    {data?.stale && <div role="status" className={styles.notice}><RefreshCw size={14} aria-hidden="true" />{copy.stale}</div>}
    {data && freshnessUnknown && <div role="status" className={styles.notice}><CircleAlert size={14} aria-hidden="true" />{copy.freshnessUnknown}</div>}
    {data?.source_status === "missing" && (data.eligible_runs > 0 || snapshot) && <div className={styles.notice}>{copy.missing}</div>}
    {(data?.source_status === "partial" || snapshot?.partial) && <div className={styles.notice}><CircleAlert size={14} aria-hidden="true" />{copy.partial}</div>}
    {!!data?.warnings.length && <details className={styles.warnings}><summary>{copy.warnings} · {data.warnings.length}</summary><ul>{data.warnings.map((warning, index) => <li key={`${warning.code}:${warning.run}:${index}`}>
      <span>{warningLabel(warning.code, locale, copy.sourceWarning)}</span>{warning.run && <Link href={taskHref(warning.run)}>{warning.run}</Link>}
    </li>)}</ul></details>}
    {snapshot && !snapshot.grouping_version && nodes.length > 0 && <p className={styles.upgradeNote}>{copy.groupUpgrade}</p>}
    {!data && busy && <div className={styles.empty} role="status"><Spinner /><p>{copy.loading}</p></div>}
    {data && <>
      <div className={styles.summaryBar}>
        <div className={styles.stats} aria-label={copy.title}>{[[nodes.length, copy.hosts], [groups.length, copy.subnets], [edges.length, copy.relations], [nodes.filter(node => node.credentials.length).length, copy.withCredentials]].map(([count, label]) => <span key={label}><strong>{count}</strong> {label}</span>)}</div>
        {snapshot && <div className={styles.snapshotMeta}><span>{copy.generated} · {timestamp(snapshot.generated_at, locale)}</span><span>v{snapshot.version}</span></div>}
      </div>
      {!snapshot ? <div className={styles.empty}><Network size={36} aria-hidden="true" /><h3>{data.eligible_runs ? copy.manual : copy.noTasks}</h3><p>{data.eligible_runs ? copy.manualHint : copy.noTasksHint}</p></div>
        : !nodes.length ? <div className={styles.empty}><Server size={32} aria-hidden="true" /><h3>{copy.noHosts}</h3><p>{copy.noHostsHint}</p></div>
          : <>
            <div className={styles.toolbar} role="group" aria-label={copy.filters}>
              <label className={styles.search}><Search size={15} aria-hidden="true" /><input ref={searchInput} type="search" aria-label={copy.search} placeholder={copy.search} value={query} onChange={event => setQuery(event.target.value)} /></label>
              <select aria-label={copy.subnets} value={group} onChange={event => setGroup(event.target.value)}><option value="">{copy.allGroups}</option>{groups.map(entry => <option key={entry.id} value={entry.id}>{entry.cidr || copy.unknownSubnet}{entry.context ? ` · ${entry.context}` : ""} ({entry.nodes.length})</option>)}</select>
              <select aria-label={copy.allRisk} value={risk} onChange={event => setRisk(event.target.value)}><option value="all">{copy.allRisk}</option><option value="high">{copy.highRisk}</option></select>
              <button type="button" className={styles.filter} aria-pressed={credentialOnly} onClick={() => setCredentialOnly(!credentialOnly)}><KeyRound size={14} aria-hidden="true" />{copy.credentialsOnly}</button>
            </div>
            {hasFilters && <div className={styles.matchStatus} role="status"><span>{matching.length ? `${copy.matched} · ${matching.length} / ${nodes.length}` : copy.noMatch}</span><button type="button" className={styles.textButton} onClick={reset}>{copy.reset}</button></div>}
            <div className={styles.workspace}>
              <div className={styles.canvasPane}><TopologyCanvas projectId={projectId} nodes={nodes} edges={edges} locale={locale} selectedId={selectedId} onSelect={onSelect} onSelectEdges={onSelectEdges} query={query} highlightIds={highlightIds} selectedEdgeIds={selectedEdgeIds} /></div>
              {(selected || selectedEdges.length > 0) && <aside ref={detailPanel} className={styles.drawer} tabIndex={-1} aria-label={selected ? copy.details : copy.relationshipDetails}>
                {selected ? <HostDetails key={`${selected.id}:${snapshot.version}`} node={selected} snapshot={snapshot} projectId={projectId} locale={locale} copy={copy} onSelect={selectHost} onClose={closeDetails} />
                  : <RelationshipDetails edges={selectedEdges} nodes={nodeIndex} locale={locale} copy={copy} onSelect={selectHost} onClose={closeDetails} />}
              </aside>}
            </div>
            <footer className={styles.legend}>{copy.membership}</footer>
          </>}
    </>}
  </section>;
}

function basisLabel(basis: GroupingBasis, copy: Copy) { return copy[basis]; }

function HostDetails({ node, snapshot, projectId, locale, copy, onSelect, onClose }: { node: TopologyNode; snapshot: TopologySnapshot; projectId: string; locale: string; copy: Copy; onSelect: (id: string) => void; onClose: () => void }) {
  const [tab, setTab] = React.useState<DetailTab>("overview");
  const id = React.useId();
  const grouping = getNodeGrouping(node);
  const nodeIndex = React.useMemo(() => new Map(snapshot.nodes.map(entry => [entry.id, entry])), [snapshot.nodes]);
  const edges = React.useMemo(() => snapshot.edges.filter(edge => edge.source === node.id || edge.target === node.id), [snapshot.edges, node.id]);
  const tabKey = (event: React.KeyboardEvent<HTMLButtonElement>, current: DetailTab) => {
    const index = DETAIL_TABS.indexOf(current);
    const next = event.key === "ArrowRight" ? (index + 1) % DETAIL_TABS.length : event.key === "ArrowLeft" ? (index + DETAIL_TABS.length - 1) % DETAIL_TABS.length : event.key === "Home" ? 0 : event.key === "End" ? DETAIL_TABS.length - 1 : null;
    if (next === null) return;
    event.preventDefault(); setTab(DETAIL_TABS[next]);
    document.getElementById(`${id}-${DETAIL_TABS[next]}`)?.focus();
  };
  const tabLabels = { overview: copy.overviewTab, vulnerabilities: copy.vulnTab, findings: copy.findingTab, credentials: copy.credentialTab, sources: copy.sourceTab };
  return <>
    <header className={styles.drawerHeader}><div><span className={styles.kicker}>{copy.details}</span><h3 className={styles.drawerTitle}>{nodeName(node)}</h3><p className={styles.drawerDescription}>{node.ip || node.address}</p></div><button type="button" className={styles.iconButton} aria-label={copy.close} onClick={onClose}><X size={18} /></button></header>
    <div className={styles.drawerBadges}><RiskBadge severity={node.severity} copy={copy} /><span>{node.status === "reachable" ? copy.reachable : copy.observed}</span>{node.backfilled && <span>{copy.backfilled}</span>}</div>
    <nav className={styles.detailTabs} role="tablist" aria-label={copy.details}>{DETAIL_TABS.map(entry => <button key={entry} type="button" id={`${id}-${entry}`} role="tab" aria-selected={tab === entry} tabIndex={tab === entry ? 0 : -1} aria-controls={`${id}-panel`} onClick={() => setTab(entry)} onKeyDown={event => tabKey(event, entry)}>{tabLabels[entry]}{entry !== "overview" && <span>{entry === "sources" ? node.sources.length + edges.length : node[entry].length}</span>}</button>)}</nav>
    <div className={styles.drawerBody} role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${tab}`} tabIndex={0}>
      {tab === "overview" && <>
        {node.group_conflict && <p className={styles.note}>{copy.subnetConflict}</p>}
        <dl className={styles.properties}>{[[copy.groupCidr, grouping.cidr || copy.unknownSubnet], [copy.groupBasis, basisLabel(grouping.basis, copy)], [copy.context, grouping.context || copy.unknownContext], [copy.os, node.os], [copy.role, node.role], [copy.firstSeen, timestamp(node.first_seen, locale)], [copy.lastSeen, timestamp(node.last_seen, locale)]].filter(([, value]) => value).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
        <h3 className={styles.sectionTitle}>{copy.related} · {edges.length}</h3>
        {edges.length ? edges.map(edge => { const other = nodeIndex.get(edge.source === node.id ? edge.target : edge.source); return <div className={styles.relatedRow} key={edge.id}><span>{edge.source === node.id ? <ArrowRight size={14} /> : <ArrowRight size={14} className={styles.incoming} />}</span><button type="button" className={styles.textButton} disabled={!other} onClick={() => other && onSelect(other.id)}>{other ? nodeName(other) : copy.unknown}<small>{other?.ip || other?.address}</small></button><span>{relationLabel(edge.relation_type, locale)}</span></div>; }) : <p className={styles.note}>{copy.noRelations}</p>}
      </>}
      {(tab === "vulnerabilities" || tab === "findings") && <RecordList records={node[tab]} empty={tab === "vulnerabilities" ? copy.noVulns : copy.noFindings} copy={copy} />}
      {tab === "credentials" && <><p className={styles.note}>{copy.secretHint}</p>{node.credentials.length ? node.credentials.map(credential => <Credential key={`${credential.source_run}:${credential.id}`} credential={credential} projectId={projectId} nodeId={node.id} version={snapshot.version} copy={copy} />) : <p className={styles.note}>{copy.noCredentials}</p>}</>}
      {tab === "sources" && <>
        <h3 className={styles.sectionTitle}>{copy.related} · {edges.length}</h3><p className={styles.note}>{copy.edgeHint}</p>
        {edges.length ? edges.map(edge => <RelationshipRecord key={edge.id} edge={edge} nodes={nodeIndex} locale={locale} copy={copy} onSelect={onSelect} />) : <p className={styles.note}>{copy.noRelations}</p>}
        <h3 className={styles.sectionTitle}>{copy.evidence} · {node.sources.length}</h3>{node.sources.length ? node.sources.map((source, index) => <article className={styles.record} key={`${source.run}:${source.source}:${index}`}><div className={styles.recordHeader}><code>{source.source || copy.unknown}</code>{source.observed_at && <time>{timestamp(source.observed_at, locale)}</time>}</div><p>{source.evidence || copy.noSources}</p>{source.agent_id && <p className={styles.note}>{copy.agent} · {source.agent_id}</p>}{source.run && <SourceLink name={source.run} copy={copy} />}</article>) : <p className={styles.note}>{copy.noSources}</p>}
      </>}
    </div>
  </>;
}

function RelationshipRecord({ edge, nodes, locale, copy, onSelect }: { edge: TopologyEdge; nodes: Map<string, TopologyNode>; locale: string; copy: Copy; onSelect: (id: string) => void }) {
  return <article className={styles.record}>
    <div className={styles.recordHeader}><span className={styles.relationship} data-verified={edge.verified}>{edge.verified ? <Check size={13} /> : <CircleAlert size={13} />}{edge.verified ? copy.verified : copy.unverified}</span><code>{relationLabel(edge.relation_type, locale)}</code></div>
    <div className={styles.edgeEndpoints}>{[[copy.from, edge.source], [copy.to, edge.target]].map(([label, id]) => { const node = nodes.get(id); return <div key={label}><small>{label}</small><button type="button" className={styles.textButton} disabled={!node} onClick={() => node && onSelect(node.id)}>{node ? nodeName(node) : id}<small>{node?.ip || node?.address}</small></button></div>; })}</div>
    <p>{edge.evidence || copy.noSources}</p>{edge.source_run && <SourceLink name={edge.source_run} copy={copy} />}
  </article>;
}

function RelationshipDetails({ edges, nodes, locale, copy, onSelect, onClose }: { edges: TopologyEdge[]; nodes: Map<string, TopologyNode>; locale: string; copy: Copy; onSelect: (id: string) => void; onClose: () => void }) {
  return <><header className={styles.drawerHeader}><div><span className={styles.kicker}>{copy.relationshipDetails}</span><h3 className={styles.drawerTitle}>{copy.relationships} · {edges.length}</h3></div><button type="button" className={styles.iconButton} aria-label={copy.close} onClick={onClose}><X size={18} /></button></header><div className={styles.drawerBody}><p className={styles.note}>{copy.relationBundleHint}</p>{edges.map(edge => <RelationshipRecord key={edge.id} edge={edge} nodes={nodes} locale={locale} copy={copy} onSelect={onSelect} />)}</div></>;
}

function SourceLink({ name, copy }: { name: string; copy: Copy }) {
  return <Link className={styles.sourceLink} href={taskHref(name)} title={copy.openTask}><ArrowRight size={13} aria-hidden="true" /><span>{name}</span></Link>;
}

function RecordList({ records, empty, copy }: { records: TopologyRecord[]; empty: string; copy: Copy }) {
  if (!records.length) return <p className={styles.note}>{empty}</p>;
  return <>{records.map((record, index) => <article className={styles.record} key={`${field(record, "source_run")}:${field(record, "id")}:${index}`}>
    <div className={styles.recordHeader}><RiskBadge severity={field(record, "severity") || "info"} copy={copy} /><code>{field(record, "id")}</code></div>
    <h3>{field(record, "title") || field(record, "id") || copy.unknown}</h3>
    {(field(record, "description") || field(record, "content")) && <p>{field(record, "description") || field(record, "content")}</p>}
    {field(record, "source_run") && <SourceLink name={field(record, "source_run")} copy={copy} />}
  </article>)}</>;
}

function Credential({ credential, projectId, nodeId, version, copy }: { credential: TopologyCredential; projectId: string; nodeId: string; version: number; copy: Copy }) {
  const [secret, setSecret] = React.useState<TopologySecret | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState(false);
  const controller = React.useRef<AbortController | null>(null);
  React.useEffect(() => () => controller.current?.abort(), []);
  const reveal = async () => {
    if (secret) { setSecret(null); return; }
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setLoading(true); setError(false);
    const timeout = window.setTimeout(() => request.abort(), 20_000);
    try {
      const result = await revealTopologyCredential(projectId, nodeId, credential, version, request.signal);
      if (!request.signal.aborted) setSecret(result);
    } catch { if (controller.current === request) setError(true); }
    finally { window.clearTimeout(timeout); if (controller.current === request) setLoading(false); }
  };
  const validation = ({ verified: copy.confirmed, validated: copy.confirmed, untested: copy.untested, unverified: copy.untested, unknown: copy.untested, invalid: copy.invalid, failed: copy.failed, valid: copy.valid, partial: copy.partialStatus } as Record<string, string>)[credential.validation_status] ?? credential.validation_status;
  return <article className={styles.record}>
    <div className={styles.recordHeader}><KeyRound size={16} aria-hidden="true" /><code>{credential.secret_type || copy.credentials}</code></div>
    <dl className={styles.properties}><div><dt>{copy.username}</dt><dd>{credential.username || copy.unknown}</dd></div><div><dt>{copy.validation}</dt><dd>{validation || copy.untested}</dd></div>
      {credential.has_password && <div><dt>{copy.password}</dt><dd className={styles.secret}>{secret ? secret.password === "" ? copy.emptyPassword : secret.password : "••••••••"}</dd></div>}
      {credential.has_hash && <div><dt>{copy.hash}</dt><dd className={styles.secret}>{secret ? secret.hash || "—" : "••••••••"}</dd></div>}
    </dl>
    {credential.validation_evidence && <p>{credential.validation_evidence}</p>}
    {credential.has_password || credential.has_hash ? <button type="button" className={styles.secondary} disabled={loading} aria-pressed={!!secret} onClick={() => void reveal()}>{loading ? <Spinner /> : secret ? <EyeOff size={14} /> : <Eye size={14} />}{loading ? copy.revealing : secret ? copy.hide : copy.show}</button> : <p className={styles.note}>{copy.noSecret}</p>}
    {error && <p role="alert" className={styles.secretError}>{copy.revealFailed}</p>}
    {credential.source_run && <SourceLink name={credential.source_run} copy={copy} />}
  </article>;
}
