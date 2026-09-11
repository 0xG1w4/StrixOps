"use client";
import * as React from "react";
import { FileText, X } from "lucide-react";
import { StatusPill } from "@/components/ui";
import { apiURL, type RunDetail } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import styles from "./ConversationControls.module.css";

interface PromptManifest { agents?: Array<{ agent_id: string; prompt_file: string }> }
function promptFile(value: unknown): value is string {
  return typeof value === "string" && /^prompt_[a-zA-Z0-9_-]+\.md$/.test(value);
}

export default function ConversationAgents({ name, run, selected, onSelect, onClose }: {
  name: string; run: RunDetail | null; selected: string; onSelect: (id: string) => void; onClose: () => void;
}) {
  const { locale } = useI18n();
  const c = (zh: string, en: string) => locale === "en" ? en : zh;
  const [prompt, setPrompt] = React.useState<{ key: string; file: string | null } | null>(null);
  const key = `${name}:${selected}`;
  React.useEffect(() => {
    if (!selected || document.hidden) return;
    let active = true;
    const controller = new AbortController();
    const load = async () => {
      let file: string | null = null;
      try {
        const response = await fetch(apiURL(`/api/runs/${encodeURIComponent(name)}/prompts/prompt_manifest.json`), { cache: "no-store", signal: controller.signal });
        if (response.ok) {
          const manifest: PromptManifest = await response.json();
          const candidate = manifest.agents?.find(agent => agent.agent_id === selected)?.prompt_file;
          if (promptFile(candidate)) file = candidate;
        } else if (response.status === 404) {
          const inventory = await fetch(apiURL(`/api/runs/${encodeURIComponent(name)}/prompts`), { cache: "no-store", signal: controller.signal });
          const data = await inventory.json() as { prompts?: Array<{ agent: string; file: string }> };
          const candidate = data.prompts?.find(item => item.agent === selected)?.file;
          if (promptFile(candidate)) file = candidate;
        }
      } catch { /* A missing snapshot does not prevent conversation use. */ }
      if (active) setPrompt({ key, file });
    };
    void load();
    return () => { active = false; controller.abort(); };
  }, [name, selected, key]);
  const entries = Object.entries(run?.agents || {});
  const ordered: Array<{ id: string; depth: number }> = [];
  const visited = new Set<string>();
  const walk = (id: string, depth: number) => {
    if (visited.has(id)) return;
    visited.add(id);
    ordered.push({ id, depth });
    if (depth < 20) entries.filter(([, agent]) => agent.parent_id === id).forEach(([child]) => walk(child, depth + 1));
  };
  entries.filter(([, agent]) => !agent.parent_id || !run?.agents?.[agent.parent_id]).forEach(([id]) => walk(id, 0));
  entries.forEach(([id]) => walk(id, 0));
  const entry = selected ? run?.agents?.[selected] : null;
  const file = prompt?.key === key ? prompt.file : null;
  return (
    <aside className={styles.agents} aria-label={c("代理樹與詳情", "Agent tree and details")} onKeyDown={event => { if (event.key === "Escape") onClose(); }}>
      <div className={styles.agentHeader}>
        <strong>{c("代理", "Agents")} · {entries.length}</strong>
        <button type="button" onClick={onClose} aria-label={c("收合代理面板", "Close agent panel")}><X size={15} /></button>
      </div>
      <nav className={styles.agentTree} aria-label={c("切換代理", "Select an agent")}>
        <button type="button" className={styles.agentRow} aria-pressed={!selected} onClick={() => onSelect("")}>
          <strong>{c("全部對話", "All conversations")}</strong><small>{c("指令僅送主代理", "Hints go only to root")}</small>
        </button>
        {ordered.map(({ id, depth }) => {
          const agent = run!.agents[id];
          return (
            <button type="button" key={id} className={styles.agentRow} aria-pressed={selected === id} onClick={() => onSelect(id)} style={{ paddingLeft: 12 + Math.min(depth, 4) * 12 }}>
              <span><strong>{agent.name || id}</strong><StatusPill status={agent.status || "unknown"} /></span>
              <code>{id}</code>
              {agent.task && <small className={styles.agentTask}>{agent.task}</small>}
            </button>
          );
        })}
        {!entries.length && <p className={styles.agentEmpty}>{c("尚未取得代理紀錄。", "No agent records yet.")}</p>}
      </nav>
      {selected && (
        <section className={styles.agentDetail} aria-label={c("選取代理詳情", "Selected agent details")}>
          <h3>{entry?.name || selected}</h3><code>{selected}</code>
          <p>{entry?.task || c("沒有任務說明。", "No task description recorded.")}</p>
          {file ? (
            <a href={apiURL(`/api/runs/${encodeURIComponent(name)}/prompts/${encodeURIComponent(file)}`)} target="_blank" rel="noopener noreferrer"><FileText size={13} />{c("查看 Prompt", "View prompt")}</a>
          ) : <span>{c("此代理沒有可用的 Prompt 快照。", "No prompt snapshot is available for this agent.")}</span>}
        </section>
      )}
    </aside>
  );
}
