"use client";

import * as React from "react";
import { ChevronDown, LoaderCircle, Play, Repeat2, Save, Trash2 } from "lucide-react";
import { McpApiError, mcpApi, mcpError, type McpTask, type McpFlow, type McpCatalog, type McpReplayPatch, type McpTest } from "@/lib/mcp-api";
import { McpError, McpModal, shortId, useMcpCopy } from "./shared";
import styles from "./mcp.module.css";

function rules(value: string): string[] { return [...new Set(value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean))]; }

export function TaskForm({ task, onClose, onSaved }: { task?: McpTask; onClose: () => void; onSaved: (task: McpTask) => void }) {
  const c = useMcpCopy();
  const [name, setName] = React.useState(task?.name || "");
  const [allowed, setAllowed] = React.useState(task?.allow_hosts.join("\n") || "");
  const [excluded, setExcluded] = React.useState(task?.exclude_hosts.join("\n") || "");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); if (saving) return;
    if (!name.trim() || !rules(allowed).length) { setError(c("请填写任务名称，并指定至少一个允许的主机。", "Enter a task name and at least one allowed host.")); return; }
    setSaving(true); setError("");
    try {
      const body = { name: name.trim(), allow_hosts: rules(allowed), exclude_hosts: rules(excluded), ...(task?.agent_config ? { agent_config: task.agent_config } : {}) };
      const result = task ? await mcpApi.updateTask(task.id, body) : await mcpApi.createTask(body);
      onSaved(result.task);
    } catch (e) { setError(mcpError(e)); } finally { setSaving(false); }
  };
  return <McpModal title={task ? c("任务设置", "Task settings") : c("创建 MCP 任务", "Create MCP task")} description={c("每个任务独立保存流量、测试与报告。", "Each task keeps its own traffic, tests, and reports.")} onClose={onClose}>
    <form className={styles.form} onSubmit={save}>
      <label className={styles.field}>{c("任务名称", "Task name")}<input className={styles.input} value={name} onChange={e => setName(e.target.value)} maxLength={160} placeholder={c("例如：商城登录与订单流程", "e.g. Shop sign-in and order flow")} required /></label>
      <div className={styles.formColumns}>
        <label className={styles.field}>{c("允许的主机", "Allowed hosts")}<textarea className={`${styles.input} ${styles.codeInput}`} rows={5} value={allowed} onChange={e => setAllowed(e.target.value)} placeholder={"shop.example.com\n*.example.com"} required /><span className={styles.fieldHint}>{c("每行一个 hostname 或 IP；使用 *.example.com 包含子域名。", "One hostname or IP per line; use *.example.com for subdomains.")}</span></label>
        <label className={styles.field}>{c("排除的主机", "Excluded hosts")}<textarea className={`${styles.input} ${styles.codeInput}`} rows={5} value={excluded} onChange={e => setExcluded(e.target.value)} placeholder="payments.example.com" /><span className={styles.fieldHint}>{c("排除规则优先；请求重放与 Agent 都遵守此范围。", "Exclusions take precedence. Replay and Agent tests use this scope.")}</span></label>
      </div>
      {task && <div className={styles.formNote}>{c("正在捕获时变更范围，可能需要重新启动代理；后端会检查当前状态。", "Scope changes during capture may require a proxy restart; the server checks the current state.")}</div>}
      {error && <McpError>{error}</McpError>}
      <div className={styles.formActions}><button className={styles.secondaryButton} type="button" onClick={onClose}>{c("取消", "Cancel")}</button><button className={styles.primaryButton} disabled={saving} type="submit"><Save size={15} />{saving ? c("保存中…", "Saving…") : task ? c("保存设置", "Save settings") : c("创建任务", "Create task")}</button></div>
    </form>
  </McpModal>;
}

export function AgentTestForm({ task, flowIds, catalog, catalogError, onClose, onCreated }: { task: McpTask; flowIds: string[]; catalog: McpCatalog | null; catalogError?: string; onClose: () => void; onCreated: (test: McpTest) => void }) {
  const c = useMcpCopy();
  const defaults = task.agent_config || catalog?.defaults || {};
  const [profile, setProfile] = React.useState(defaults.profile_id || "");
  const [skills, setSkills] = React.useState<string[]>(defaults.skills || []);
  const [instruction, setInstruction] = React.useState(defaults.instruction || "");
  const [maxRequests, setMaxRequests] = React.useState(defaults.max_requests ?? 12);
  const [maxSeconds, setMaxSeconds] = React.useState(defaults.max_seconds ?? 300);
  const [skillQuery, setSkillQuery] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (saving) return;
    if (!Number.isInteger(maxRequests) || maxRequests < 1 || maxRequests > 100 || !Number.isInteger(maxSeconds) || maxSeconds < 5 || maxSeconds > 900) { setError(c("请求上限为 1–100，时间上限为 5–900 秒。", "Use 1–100 requests and a time limit of 5–900 seconds.")); return; }
    if (skills.length > 8 || flowIds.length > 50) { setError(c("每次最多选择 8 个技能与 50 条请求。", "Select up to 8 skills and 50 requests per test.")); return; }
    setSaving(true); setError("");
    try { const result = await mcpApi.createTest(task.id, { flow_ids: flowIds, profile_id: profile || null, skills, instruction: instruction.trim(), max_requests: maxRequests, max_seconds: maxSeconds }); onCreated(result.test); }
    catch (e) { setError(mcpError(e)); } finally { setSaving(false); }
  };
  const matchingSkills = (catalog?.skills || []).filter(skill => `${skill.id} ${skill.description || ""}`.toLowerCase().includes(skillQuery.toLowerCase()));
  return <McpModal wide title={c("创建 Agent 测试", "Create Agent test")} description={c(`针对 ${flowIds.length} 条已选请求创建测试，保留证据与来源关联。`, `Test ${flowIds.length} selected request(s) with evidence linked to their source.`)} onClose={onClose}>
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.selectedFlowIds}>{flowIds.map(id => <code key={id}>#{shortId(id)}</code>)}</div>
      <div className={styles.formColumns}>
        <label className={styles.field}>{c("模型配置", "Model profile")}<select className={styles.input} value={profile} onChange={e => setProfile(e.target.value)}><option value="">{c("使用当前启用的配置", "Use the active profile")}</option>{(catalog?.profiles || []).map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <div className={styles.formColumns}><label className={styles.field}>{c("最多重放次数", "Replay limit")}<input className={styles.input} type="number" min={1} max={100} value={maxRequests} onChange={e => setMaxRequests(Number(e.target.value))} required /></label><label className={styles.field}>{c("整次测试秒数", "Total time limit (s)")}<input className={styles.input} type="number" min={5} max={900} value={maxSeconds} onChange={e => setMaxSeconds(Number(e.target.value))} required /></label></div>
      </div>
      <p className={styles.fieldHint}>{c("所有已选请求共享这份时间与重放预算，包含模型回复、重放和整理结果。新任务默认 300 秒；较慢模型可调整时间，最多 900 秒。", "All selected requests share this time and replay budget, including model responses, replays, and final results. New tasks default to 300 seconds; allow more time for slower models, up to 900 seconds.")}</p>
      <label className={styles.field}>{c("本次测试指示", "Instructions for this test")}<textarea className={styles.input} rows={4} value={instruction} onChange={e => setInstruction(e.target.value)} maxLength={8000} placeholder={c("描述要验证的行为、角色与前置条件。", "Describe the behavior, roles, and prerequisites to verify.")} /></label>
      <div className={styles.field}><div className={styles.fieldHeading}><span>{c("共享技能", "Shared skills")}</span><span className={styles.muted}>{skills.length} {c("个已选择", "selected")}</span></div><input className={styles.input} aria-label={c("搜索技能", "Search skills")} placeholder={c("搜索技能名称或描述", "Search skill names or descriptions")} value={skillQuery} onChange={e => setSkillQuery(e.target.value)} />
        <div className={styles.skillPicker}>{matchingSkills.map(skill => <label key={skill.id} className={styles.skillOption}><input type="checkbox" checked={skills.includes(skill.id)} disabled={!skills.includes(skill.id) && skills.length >= 8} onChange={e => setSkills(old => e.target.checked ? [...old, skill.id] : old.filter(id => id !== skill.id))} /><span><code>{skill.id}</code>{skill.description && <small>{skill.description}</small>}</span></label>)}{!matchingSkills.length && <p className={styles.muted}>{catalog ? c("没有符合的技能。", "No matching skills.") : c("正在读取技能目录…", "Loading skills…")}</p>}</div>
      </div>
      {catalog?.prompt_template && <details className={styles.disclosure}><summary>{c("查看 MCP 测试模板", "View MCP test template")}<ChevronDown size={14} /></summary><pre className={styles.promptPreview}>{catalog.prompt_template}</pre></details>}
      {catalogError && <McpError>{catalogError}</McpError>}
      <p className={styles.fieldHint}>{c("技能与模型设置使用既有资源；这里的指示只应用到本次测试。", "Skills and model profiles are shared resources; these instructions apply only to this test.")}</p>
      {error && <McpError>{error}</McpError>}
      <div className={styles.formActions}><button type="button" className={styles.secondaryButton} onClick={onClose}>{c("取消", "Cancel")}</button><button type="submit" className={styles.primaryButton} disabled={saving || !catalog}><Play size={15} />{saving ? c("创建中…", "Creating…") : c("开始测试", "Start test")}</button></div>
    </form>
  </McpModal>;
}

export function ReplayForm({ taskId, flow, onClose, onReplayed }: { taskId: string; flow: McpFlow; onClose: () => void; onReplayed: (flow: McpFlow) => void }) {
  const c = useMcpCopy();
  const [method, setMethod] = React.useState(flow.method);
  const [url, setUrl] = React.useState(flow.url);
  const [editHeaders, setEditHeaders] = React.useState(false);
  const [headers, setHeaders] = React.useState("{}");
  const [editBody, setEditBody] = React.useState(false);
  const [body, setBody] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState("");
  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (saving) return;
    setError("");
    let modifications: McpReplayPatch;
    try {
      const parsed = new URL(url);
      if (!["https:", "http:"].includes(parsed.protocol)) throw new Error(c("URL 必须使用 http 或 https。", "Use an http or https URL."));
      let decodedUrl = url;
      for (let i = 0; i < 3; i += 1) {
        try { const next = decodeURIComponent(decodedUrl); if (next === decodedUrl) break; decodedUrl = next; } catch { break; }
      }
      if (url !== flow.url && /\[(?:redacted|masked)\]/i.test(decodedUrl)) throw new Error(c("修改后的 URL 仍含掩码标记。请取消重放，先在请求内容按「显示敏感内容」后再编辑；或填入实际值。未变更 URL 的重放会沿用原始值。", "The edited URL still contains a mask. Cancel replay and reveal sensitive values before editing, or enter the actual values. Replaying an unchanged URL preserves its original values."));
      modifications = { ...(method !== flow.method ? { method } : {}), ...(url !== flow.url ? { url } : {}), ...(editBody ? { body } : {}) };
      if (editHeaders) { const parsedHeaders: unknown = JSON.parse(headers); if (!parsedHeaders || Array.isArray(parsedHeaders) || typeof parsedHeaders !== "object" || Object.values(parsedHeaders).some(v => typeof v !== "string")) throw new Error(c("Headers 请使用名称与字符串值的 JSON 对象。", "Headers must be a JSON object with string values.")); modifications.headers = parsedHeaders as Record<string, string>; }
    } catch (e) { setError(mcpError(e)); return; }
    setSaving(true);
    try { const result = await mcpApi.replay(taskId, flow.id, modifications); onReplayed(result.flow); }
    catch (e) { setError(mcpError(e)); } finally { setSaving(false); }
  };
  return <McpModal wide title={c("重放请求", "Replay request")} description={c(`从原始请求 #${shortId(flow.id)} 创建新请求，结果另存为 replay 流量。`, `Create a new request from #${shortId(flow.id)} and save its response as replay traffic.`)} onClose={onClose}>
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.replayAddress}><label className={styles.field}>Method<select className={styles.input} value={method} onChange={e => setMethod(e.target.value)}>{[...new Set([flow.method, "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])].map(m => <option key={m}>{m}</option>)}</select></label><label className={styles.field}>URL<input className={`${styles.input} ${styles.codeInput}`} value={url} onChange={e => setUrl(e.target.value)} type="url" required /></label></div>
      <div className={styles.formNote}>{c("未修改的字段会沿用服务器保存的原始值，包括登录信息。画面中的掩码值不会覆盖原始凭据。", "Unchanged fields retain the server's original values, including authentication. Display masks never overwrite original credentials.")}</div>
      <label className={styles.checkLabel}><input type="checkbox" checked={editHeaders} onChange={e => setEditHeaders(e.target.checked)} />{c("修改 Headers", "Modify headers")}</label>
      {editHeaders && <label className={styles.field}><span className={styles.fieldHint}>{c("JSON 格式；只填写要修改的字段。", "JSON format; include only headers to change.")}</span><textarea aria-label="Header modifications" className={`${styles.input} ${styles.codeInput}`} rows={5} value={headers} onChange={e => setHeaders(e.target.value)} spellCheck={false} /></label>}
      <label className={styles.checkLabel}><input type="checkbox" checked={editBody} onChange={e => setEditBody(e.target.checked)} />{c("替换 Body", "Replace body")}</label>
      {editBody && <label className={styles.field}><span className={styles.fieldHint}>{c("输入新的完整内容；留空会清除 Body。", "Enter the complete new body; leave blank to clear it.")}</span><textarea aria-label="Replacement request body" className={`${styles.input} ${styles.codeInput}`} rows={6} value={body} onChange={e => setBody(e.target.value)} spellCheck={false} /></label>}
      {error && <McpError>{error}</McpError>}
      <div className={styles.formActions}><button type="button" className={styles.secondaryButton} onClick={onClose}>{c("取消", "Cancel")}</button><button type="submit" className={styles.primaryButton} disabled={saving}><Repeat2 size={15} />{saving ? c("重放中…", "Replaying…") : c("发送请求", "Send request")}</button></div>
    </form>
  </McpModal>;
}

export function DeleteTaskDialog({ task, onClose, onDeleted }: { task: McpTask; onClose: () => void; onDeleted: () => void }) {
  const c = useMcpCopy();
  const [snapshot, setSnapshot] = React.useState(task);
  const [reportCount, setReportCount] = React.useState<number | undefined>(task.counts?.reports);
  const [loading, setLoading] = React.useState(true);
  const [deleting, setDeleting] = React.useState(false);
  const [error, setError] = React.useState("");
  React.useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([mcpApi.task(task.id, controller.signal), mcpApi.reports(task.id, controller.signal)]).then(([taskResult, reportResult]) => {
      if (controller.signal.aborted) return;
      if (taskResult.status === "fulfilled") setSnapshot(taskResult.value.task);
      if (reportResult.status === "fulfilled") setReportCount(reportResult.value.reports.length);
      if (taskResult.status === "rejected" || reportResult.status === "rejected") setError(c("部分数据数量无法更新；删除范围仍包含此任务的全部数据。", "Some counts could not be refreshed. Deletion still includes all data in this task."));
      setLoading(false);
    });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task.id]);
  const remove = async () => {
    if (deleting || loading) return;
    setDeleting(true); setError("");
    try {
      const result = await mcpApi.deleteTask(task.id);
      if (!result.deleted || result.task_id !== task.id) throw new Error(c("服务器尚未确认删除完成，请刷新后检查。", "The server did not confirm deletion. Refresh to check the task."));
      onDeleted();
    } catch (e) { if (e instanceof McpApiError && e.status === 404) { onDeleted(); return; } setError(mcpError(e)); setDeleting(false); }
  };
  return <McpModal title={c("删除 MCP 任务", "Delete MCP task")} description={c("此操作无法恢复。", "This action cannot be undone.")} onClose={() => { if (!deleting) onClose(); }}>
    <div className={styles.form}>
      <div className={styles.deleteIdentity}><strong>{snapshot.name}</strong><code>{snapshot.id}</code></div>
      <p className={styles.deleteDescription}>{c("将停止此任务的代理与进行中的测试，并永久移除以下数据：", "Stops this task's proxy and active tests, then permanently removes:")}</p>
      <dl className={styles.deleteCounts}>
        <div><dt>{c("请求", "Requests")}</dt><dd>{loading ? "…" : snapshot.counts?.flows ?? "—"}</dd></div>
        <div><dt>{c("测试结果", "Test results")}</dt><dd>{loading ? "…" : snapshot.counts?.tests ?? "—"}</dd></div>
        <div><dt>{c("报告", "Reports")}</dt><dd>{loading ? "…" : reportCount ?? "—"}</dd></div>
      </dl>
      <p className={styles.fieldHint}>{c("共享 CA 证书与其他任务会保留。", "The shared CA certificate and other tasks are retained.")}</p>
      {error && <McpError>{error}</McpError>}
      <div className={styles.formActions}><button type="button" className={styles.secondaryButton} disabled={deleting} onClick={onClose}>{c("取消", "Cancel")}</button><button type="button" className={styles.dangerButton} disabled={loading || deleting} onClick={() => void remove()}>{deleting ? <LoaderCircle size={15} className={styles.spin} /> : <Trash2 size={15} />}{deleting ? c("删除中…", "Deleting…") : c("永久删除任务", "Delete permanently")}</button></div>
    </div>
  </McpModal>;
}
