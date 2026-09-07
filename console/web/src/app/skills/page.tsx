"use client";

/* ============================================================================
   /skills — corpus & system-prompt management with CodeMirror editing.
   Saves take effect on the NEXT scan (editable-install source tree).
   ========================================================================= */

import * as React from "react";
import CodeMirror from "@uiw/react-codemirror";
import { markdown as markdownLanguage } from "@codemirror/lang-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ChevronRight,
  Eye,
  FileText,
  LibraryBig,
  Pencil,
  RotateCw,
  Save,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";
import { Chip, EmptyState, MicroLabel, Panel, Spinner } from "@/components/ui";
import { getJSON, postJSON, putText } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";

interface SkillFileEntry {
  path: string;
  category: string;
  name: string;
  size: number;
  modified: number;
}

interface PromptPartEntry {
  name: string;
  file: string;
  size: number;
  modified: number;
}

type Selection =
  | { kind: "skill"; path: string }
  | { kind: "prompt"; name: string }
  | null;

export default function SkillsPage() {
  const { t, locale } = useI18n();
  const [phase, setPhase] = React.useState<"loading" | "ready" | "error">("loading");
  const [files, setFiles] = React.useState<SkillFileEntry[]>([]);
  const [prompts, setPrompts] = React.useState<PromptPartEntry[]>([]);
  const [query, setQuery] = React.useState("");
  const [selected, setSelected] = React.useState<Selection>(null);
  const [content, setContent] = React.useState("");
  const [dirty, setDirty] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [preview, setPreview] = React.useState(false);
  const [meta, setMeta] = React.useState<string>("");

  const reload = React.useCallback(async () => {
    try {
      const [skillPage, promptPage] = await Promise.all([
        getJSON<{ files: SkillFileEntry[] }>("/api/skills"),
        getJSON<{ parts: PromptPartEntry[] }>("/api/prompts"),
      ]);
      setFiles(skillPage.files);
      setPrompts(promptPage.parts);
      setPhase("ready");
    } catch {
      setPhase("error");
    }
  }, []);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  const open = React.useCallback(async (sel: Selection) => {
    setSelected(sel);
    setContent("");
    setDirty(false);
    setPreview(false);
    if (!sel) return;
    try {
      if (sel.kind === "skill") {
        const page = await getJSON<{ content: string; path: string }>(
          `/api/skills/file?path=${encodeURIComponent(sel.path)}`
        );
        setContent(page.content);
        setMeta(sel.path);
      } else {
        const page = await getJSON<{ content: string }>(
          `/api/prompts/${encodeURIComponent(sel.name)}`
        );
        setContent(page.content);
        setMeta(`prompt_parts/${sel.name}.md`);
      }
    } catch (e) {
      toast.error(t("common.error"), { description: String(e) });
    }
  }, [t]);

  const save = async () => {
    if (!selected || !dirty || saving) return;
    setSaving(true);
    try {
      if (selected.kind === "skill") {
        await putText(
          `/api/skills/file?path=${encodeURIComponent(selected.path)}`,
          content
        );
      } else {
        await postJSON(`/api/prompts/${encodeURIComponent(selected.name)}`, {
          content,
        });
      }
      setDirty(false);
      toast.success(t("common.saved"), {
        description: t("common.saved.hint"),
      });
      void reload();
    } catch (e) {
      toast.error(t("common.error"), { description: String(e) });
    } finally {
      setSaving(false);
    }
  };

  /* group skill files by category */
  const q = query.trim().toLowerCase();
  const categories = React.useMemo(() => {
    const matching = q
      ? files.filter((f) => f.path.toLowerCase().includes(q))
      : files;
    const map = new Map<string, SkillFileEntry[]>();
    for (const f of matching) {
      const list = map.get(f.category) || [];
      list.push(f);
      map.set(f.category, list);
    }
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [files, q]);

  return (
    <div>
      <header className="hero-panel">
        <div className="hero-copy">
          <div className="eyebrow">&gt; {t("skills.path")}</div>
          <h1 className="page-title">{t("skills.title")}</h1>
          <p className="page-copy">{t("skills.copy")}</p>
        </div>
        <div className="hero-actions">
          <MicroLabel>
            <LibraryBig className="mr-1 inline h-3 w-3" />
            {files.length} skills · {prompts.length} prompts
          </MicroLabel>
        </div>
      </header>

      {phase === "error" && (
        <div className="alert-error mb-4 flex items-center justify-between">
          <span>{t("common.offline")}</span>
          <button className="button-secondary button-compact" onClick={() => void reload()}>
            <RotateCw className="h-3.5 w-3.5" /> {t("common.retry")}
          </button>
        </div>
      )}

      <div className="grid gap-4 lg:grid-cols-[19rem_minmax(0,1fr)]">
        {/* ---- library tree --------------------------------------------- */}
        <aside className="panel panel-hairline flex max-h-[78vh] flex-col overflow-hidden">
          <div className="border-b border-line/6 p-3">
            <input
              className="input-shell min-h-9 py-1.5 text-xs"
              placeholder={`${t("common.search")}…`}
              aria-label={t("skills.search")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <div className="mb-1.5 flex items-center gap-1.5 px-1.5">
              <Sparkles className="h-3.5 w-3.5 text-violet" />
              <span className="micro-label">{t("skills.prompts")}</span>
            </div>
            {prompts.map((p) => (
              <TreeRow
                key={p.name}
                active={selected?.kind === "prompt" && selected.name === p.name}
                label={p.name}
                sub={`${p.size}B`}
                onClick={() => void open({ kind: "prompt", name: p.name })}
              />
            ))}
            <div className="section-divider my-2" />
            <div className="mb-1.5 flex items-center gap-1.5 px-1.5">
              <LibraryBig className="h-3.5 w-3.5 text-accent" />
              <span className="micro-label">{t("skills.corpus")}</span>
            </div>
            {phase === "loading" ? (
              <div className="flex items-center gap-2 p-3 text-xs text-fg-muted">
                <Spinner /> {t("common.loading")}
              </div>
            ) : (
              categories.map(([category, entries]) => (
                <div key={category} className="mb-1">
                  <div className="px-1.5 py-1 font-mono text-[10px] uppercase tracking-[0.16em] text-fg-faint">
                    {category} · {entries.length}
                  </div>
                  {entries.map((f) => (
                    <TreeRow
                      key={f.path}
                      active={selected?.kind === "skill" && selected.path === f.path}
                      label={f.name}
                      sub={fmtTime(new Date(f.modified * 1000).toISOString(), locale)}
                      onClick={() => void open({ kind: "skill", path: f.path })}
                    />
                  ))}
                </div>
              ))
            )}
          </div>
        </aside>

        {/* ---- editor ---------------------------------------------------- */}
        <section className="panel panel-hairline flex max-h-[78vh] min-h-[420px] flex-col overflow-hidden">
          {!selected ? (
            <div className="flex flex-1 items-center justify-center p-6">
              <EmptyState
                title={t("skills.corpus")}
                hint={t("skills.selectHint")}
                action={
                  <span className="mono-chip">
                    <FileText className="h-3 w-3" /> markdown
                  </span>
                }
              />
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2 border-b border-line/6 px-4 py-3">
                <Chip tone={selected.kind === "prompt" ? "violet" : "accent"}>
                  {selected.kind === "prompt" ? "prompt" : "skill"}
                </Chip>
                <span className="truncate font-mono text-xs text-fg-muted" title={meta}>
                  {meta}
                </span>
                {dirty && <span className="mono-chip chip-warning">{t("skills.unsaved")}</span>}
                <div className="ml-auto flex items-center gap-1.5">
                  <button
                    type="button"
                    className={`button-ghost button-compact ${preview ? "" : "text-accent"}`}
                    onClick={() => setPreview(false)}
                    aria-pressed={!preview}
                  >
                    <Pencil className="h-3.5 w-3.5" /> {t("skills.edit")}
                  </button>
                  <button
                    type="button"
                    className={`button-ghost button-compact ${preview ? "text-accent" : ""}`}
                    onClick={() => setPreview(true)}
                    aria-pressed={preview}
                  >
                    <Eye className="h-3.5 w-3.5" /> {t("skills.preview")}
                  </button>
                  <button
                    type="button"
                    className="button-primary button-compact"
                    disabled={!dirty || saving}
                    onClick={() => void save()}
                  >
                    {saving ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
                    {t("common.save")}
                  </button>
                </div>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto">
                {content === "" ? (
                  <div className="flex items-center gap-2 p-6 text-sm text-fg-muted">
                    <Spinner /> {t("common.loading")}
                  </div>
                ) : preview ? (
                  <div className="prose-report p-5">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
                  </div>
                ) : (
                  <div className="editor-shell">
                    <CodeMirror
                      value={content}
                      height="100%"
                      theme={cmTheme}
                      extensions={[markdownLanguage()]}
                      onChange={(value) => {
                        setContent(value);
                        setDirty(true);
                      }}
                      basicSetup={{ foldGutter: false, highlightActiveLine: true }}
                    />
                  </div>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function TreeRow({
  active,
  label,
  sub,
  onClick,
}: {
  active: boolean;
  label: string;
  sub: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-[13px] transition-colors",
        active ? "bg-accent/10 text-fg" : "text-fg-2 hover:bg-raised/4"
      )}
    >
      <ChevronRight
        className={cn("h-3 w-3 shrink-0", active ? "text-accent" : "text-fg-faint")}
      />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="shrink-0 font-mono text-[9px] text-fg-faint">{sub}</span>
    </button>
  );
}

/* CodeMirror theme driven by the console's CSS channels — flips with the
   dark/light theme automatically. */
import { EditorView } from "@codemirror/view";

const cmTheme = EditorView.theme({
  "&": { height: "100%", fontSize: "0.82rem", backgroundColor: "transparent" },
  ".cm-scroller": {
    fontFamily: "var(--font-mono)",
    color: "var(--text-primary)",
  },
  ".cm-gutters": {
    backgroundColor: "transparent",
    borderRight: "1px solid rgb(var(--raise-rgb) / 0.08)",
    color: "var(--text-muted)",
  },
  ".cm-activeLine": { backgroundColor: "rgb(var(--raise-rgb) / 0.05)" },
  ".cm-activeLineGutter": { backgroundColor: "rgb(var(--raise-rgb) / 0.07)" },
  "&.cm-focused": { outline: "none" },
  ".cm-selectionBackground, ::selection": {
    backgroundColor: "rgb(var(--cyan-rgb) / 0.18) !important",
  },
});
