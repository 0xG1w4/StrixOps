"use client";

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ChevronDown, ChevronLeft, ChevronRight, FileText, RefreshCw, Search } from "lucide-react";
import { Spinner } from "@/components/ui";
import { apiURL } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import { NOTE_CATEGORIES, type NoteAuthor, type NoteDetail, type NoteRevision, type NotesPage } from "@/lib/notes";
import styles from "./NotesPanel.module.css";

const PAGE_SIZE = 20;
const EMPTY_FILTERS = { search: "", category: "", tags: "", author: "", includeDeleted: false };
type Filters = typeof EMPTY_FILTERS;
type Resource<T> = { data: T | null; loading: boolean; error: "unavailable" | "not_found" | null };

/** Each mounted reader owns its requests; hidden pages and old selections cannot update it. */
function useNoteResource<T extends { success: boolean; error_code?: string }>(
  url: string,
  live: boolean,
  requestVersion?: number | null,
) {
  const [state, setState] = React.useState<Resource<T>>({ data: null, loading: true, error: null });
  const [refresh, setRefresh] = React.useState(0);
  React.useEffect(() => {
    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let resume = false;
    const load = async () => {
      if (disposed || document.hidden || controller) return;
      const request = new AbortController();
      controller = request;
      let timedOut = false;
      const deadline = window.setTimeout(() => { timedOut = true; request.abort(); }, 10_000);
      setState(previous => ({ ...previous, loading: true }));
      try {
        const response = await fetch(apiURL(url), { cache: "no-store", signal: request.signal });
        const data: T = await response.json();
        if (disposed || request.signal.aborted) return;
        if (!response.ok || data?.success !== true) {
          setState(previous => ({
            data: previous.data,
            loading: false,
            error: response.status === 404 && data?.error_code === "note_not_found" ? "not_found" : "unavailable",
          }));
        } else {
          setState({ data, loading: false, error: null });
        }
      } catch {
        if (!disposed && !document.hidden && (!request.signal.aborted || timedOut)) {
          setState(previous => ({ ...previous, loading: false, error: "unavailable" }));
        }
      } finally {
        window.clearTimeout(deadline);
        controller = null;
        if (!disposed && !document.hidden) {
          if (resume) { resume = false; void load(); }
          else if (live) timer = window.setTimeout(() => void load(), 10_000);
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
  }, [url, live, refresh, requestVersion]);
  return { ...state, refresh: () => setRefresh(value => value + 1) };
}

function authorName(author: NoteAuthor | null | undefined) {
  return author?.agent_name || author?.agent_id || "—";
}

function noteTime(value: string | null | undefined, locale: string) {
  return value && Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString(locale === "en" ? "en-US" : "zh-TW") : "—";
}

function revisionLabel(value: number) {
  return Number.isInteger(value) && value > 0 ? `r${value}` : "—";
}

function safeNoteURL(value: string) {
  if (value.startsWith("#")) return value;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? value : "";
  } catch { return ""; }
}

function NoteMarkdown({ content }: { content: string }) {
  const { locale } = useI18n();
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        urlTransform={safeNoteURL}
        components={{
          a: ({ href, children }) => href
            ? <a href={href} target={href.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer">{children}</a>
            : <span>{children}</span>,
          img: ({ alt }) => <span className={styles.imageOmitted}>{locale === "en" ? "Image omitted" : "圖片未載入"}{alt ? `: ${alt}` : ""}</span>,
          table: ({ children }) => <div className={styles.tableScroll}><table>{children}</table></div>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function HistoryEntry({ note }: { note: NoteRevision }) {
  const { locale } = useI18n();
  const [open, setOpen] = React.useState(false);
  return (
    <details className={styles.historyEntry} open={open} onToggle={event => setOpen(event.currentTarget.open)}>
      <summary>
        <strong>{revisionLabel(note.revision)}</strong>
        <span>{authorName(note.updated_by)}</span>
        <time>{noteTime(note.updated_at, locale)}</time>
        {note.deleted && <span className={styles.deleted}>{locale === "en" ? "Deleted" : "已刪除"}</span>}
        <ChevronDown size={13} />
      </summary>
      {open && (
        <div className={styles.historyContent}>
          <h4>{note.title}</h4>
          <div className={styles.tags}>
            <span>{note.category}</span>
            {note.tags?.map(tag => <span key={tag}>{tag}</span>)}
          </div>
          <NoteMarkdown content={note.content} />
        </div>
      )}
    </details>
  );
}

function NoteReader({ runName, noteId, live, listRevision }: {
  runName: string; noteId: string; live: boolean; listRevision: number | null;
}) {
  const { locale } = useI18n();
  const c = (zh: string, en: string) => locale === "en" ? en : zh;
  const [historyOpen, setHistoryOpen] = React.useState(false);
  const [updated, setUpdated] = React.useState<number | null>(null);
  const previousRevision = React.useRef<number | null>(null);
  const result = useNoteResource<NoteDetail>(
    `/api/runs/${encodeURIComponent(runName)}/notes/${encodeURIComponent(noteId)}?include_history=${historyOpen}`,
    live,
    listRevision,
  );
  const note = result.data?.note;
  React.useEffect(() => {
    if (!note) return;
    if (previousRevision.current !== null && previousRevision.current !== note.revision) setUpdated(note.revision);
    previousRevision.current = note.revision;
  }, [note]);
  const history = note?.history?.slice().sort((a, b) => b.revision - a.revision) ?? [];

  return (
    <article className={styles.reader} aria-label={c("筆記內容", "Note content")} aria-busy={result.loading}>
      {result.error && (
        <div className={styles.error} role="status">
          <span>{result.error === "not_found"
            ? c("找不到這則筆記。", "This note could not be found.")
            : note ? c("更新失敗，目前顯示先前讀取的版本。", "Refresh failed. Showing the previously fetched revision.")
              : c("無法讀取筆記，請重試。", "Unable to read this note. Please retry.")}</span>
          <button type="button" onClick={result.refresh}>{c("重試", "Retry")}</button>
        </div>
      )}
      {!note ? (
        <div className={styles.empty}>{result.loading && <><Spinner /> {c("讀取筆記…", "Loading note…")}</>}</div>
      ) : (
        <>
          <header className={styles.noteHeader}>
            <div className={styles.noteFlags}>
              <span>{note.category}</span><strong>{revisionLabel(note.revision)}</strong>
              {note.deleted && <span className={styles.deleted}>{c("已刪除", "Deleted")}</span>}
              <button
                type="button"
                className={styles.iconButton}
                onClick={result.refresh}
                aria-label={c("更新選取的筆記", "Refresh selected note")}
              >
                <RefreshCw size={14} />
              </button>
            </div>
            <h3>{note.title}</h3>
            {note.tags?.length > 0 && <div className={styles.tags}>{note.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
            <dl className={styles.metadata}>
              <div><dt>{c("建立者", "Created by")}</dt><dd>{authorName(note.created_by)}</dd></div>
              <div><dt>{c("建立時間", "Created")}</dt><dd>{noteTime(note.created_at, locale)}</dd></div>
              <div><dt>{c("更新者", "Updated by")}</dt><dd>{authorName(note.updated_by)}</dd></div>
              <div><dt>{c("更新時間", "Updated")}</dt><dd>{noteTime(note.updated_at, locale)}</dd></div>
            </dl>
          </header>
          {updated !== null && (
            <p className={styles.updateNotice} role="status">
              {c("已載入更新版本", "Updated revision loaded")}: {revisionLabel(updated)}
            </p>
          )}
          {note.deleted && (
            <p className={styles.deletionNotice}>
              {c("此筆記已刪除，內容保留供回溯。", "This note was deleted. Its content is retained for reference.")}
              {" "}{authorName(note.deleted_by)} · {noteTime(note.deleted_at, locale)}
            </p>
          )}
          <NoteMarkdown content={note.content} />
          <details className={styles.history} open={historyOpen} onToggle={event => setHistoryOpen(event.currentTarget.open)}>
            <summary><span>{c("修訂歷程", "Revision history")}</span><ChevronDown size={14} /></summary>
            {historyOpen && (
              <div className={styles.historyBody}>
                {note.history_truncated && (
                  <p className={styles.deletionNotice}>
                    {c("較早的部分修訂已不再保留；以下不是完整歷程。", "Some earlier revisions are no longer retained. This history is incomplete.")}
                  </p>
                )}
                {result.loading && !note.history && (
                  <p className={styles.loading}><Spinner /> {c("讀取歷程…", "Loading history…")}</p>
                )}
                {history.map(entry => <HistoryEntry key={entry.revision} note={entry} />)}
                {!result.loading && !result.error && history.length === 0 && (
                  <p className={styles.hint}>{c("沒有較早的修訂。", "No earlier revisions.")}</p>
                )}
              </div>
            )}
          </details>
        </>
      )}
    </article>
  );
}

function NotesResults({ runName, live, filters, offset, onOffset }: {
  runName: string; live: boolean; filters: Filters; offset: number; onOffset: (offset: number) => void;
}) {
  const { locale } = useI18n();
  const c = (zh: string, en: string) => locale === "en" ? en : zh;
  const [selected, setSelected] = React.useState<string | null>(null);
  const query = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
    include_deleted: String(filters.includeDeleted),
  });
  if (filters.search.trim()) query.set("search", filters.search.trim());
  if (filters.category) query.set("category", filters.category);
  if (filters.author.trim()) query.set("author", filters.author.trim());
  filters.tags.split(",").map(tag => tag.trim()).filter(Boolean).forEach(tag => query.append("tags", tag));
  const result = useNoteResource<NotesPage>(`/api/runs/${encodeURIComponent(runName)}/notes?${query}`, live);
  const notes = Array.isArray(result.data?.notes) ? result.data.notes : [];
  const total = result.data?.total;
  const hasFilters = filters.search || filters.category || filters.author || filters.tags;

  return (
    <>
      <div className={styles.resultsBar}>
        <span>{typeof total === "number" ? `${total.toLocaleString()} ${c("則筆記", "notes")}` : "—"}</span>
        <button type="button" className={styles.refreshButton} onClick={result.refresh}>
          <RefreshCw size={13} />{c("更新列表", "Refresh notes")}
        </button>
      </div>
      {result.error && (
        <div className={styles.error} role="status">
          <span>{result.data
            ? c("更新失敗，目前顯示先前讀取的列表。", "Refresh failed. Showing the previously fetched list.")
            : c("筆記資料無法讀取，請重試。", "Note records could not be read. Please retry.")}</span>
          <button type="button" onClick={result.refresh}>{c("重試", "Retry")}</button>
        </div>
      )}
      <div className={styles.workspace}>
        <div className={styles.index}>
          <div className={styles.noteList} aria-label={c("筆記列表", "Notes list")} aria-busy={result.loading}>
            {notes.map(note => (
              <button
                type="button"
                key={note.note_id}
                className={styles.noteRow}
                aria-pressed={selected === note.note_id}
                onClick={() => setSelected(note.note_id)}
              >
                <span className={styles.rowHeading}><strong>{note.title}</strong><span>{revisionLabel(note.revision)}</span></span>
                <span className={styles.rowPreview}>{note.preview || "—"}</span>
                <span className={styles.rowMeta}><span>{note.category}</span>{note.deleted && <span className={styles.deleted}>{c("已刪除", "Deleted")}</span>}</span>
                <span className={styles.rowMeta}><span>{authorName(note.updated_by)}</span><time>{noteTime(note.updated_at, locale)}</time></span>
                {note.tags?.length > 0 && <span className={styles.tags}>{note.tags.map(tag => <span key={tag}>{tag}</span>)}</span>}
              </button>
            ))}
            {notes.length === 0 && !result.error && (
              <div className={styles.empty}>
                {result.loading ? <><Spinner /> {c("讀取筆記…", "Loading notes…")}</> : (
                  <>
                    <FileText size={22} />
                    <strong>{offset > 0 ? c("此頁沒有筆記", "No notes on this page")
                      : hasFilters ? c("沒有符合條件的筆記", "No matching notes")
                        : c("尚無共享筆記", "No shared notes yet")}</strong>
                    <p>{offset > 0 ? c("返回上一頁以查看其他筆記。", "Go back to the previous page to view other notes.")
                      : hasFilters ? c("調整搜尋或篩選條件。", "Adjust your search or filters.")
                        : c("Agent 在任務中保存的參考資料會顯示在這裡。", "Reference notes saved by agents during this task will appear here.")}</p>
                  </>
                )}
              </div>
            )}
          </div>
          <nav className={styles.pagination} aria-label={c("筆記分頁", "Notes pagination")}>
            <button type="button" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - PAGE_SIZE))} aria-label={c("上一頁筆記", "Previous notes page")}><ChevronLeft size={16} /></button>
            <span>{notes.length ? `${offset + 1}–${offset + notes.length}` : "—"}{typeof total === "number" ? ` / ${total}` : ""}</span>
            <button type="button" disabled={!result.data?.has_more} onClick={() => onOffset(offset + PAGE_SIZE)} aria-label={c("下一頁筆記", "Next notes page")}><ChevronRight size={16} /></button>
          </nav>
        </div>
        {selected ? (
          <NoteReader
            key={`${runName}:${selected}`}
            runName={runName}
            noteId={selected}
            live={live}
            listRevision={notes.find(note => note.note_id === selected)?.revision ?? null}
          />
        ) : (
          <div className={styles.readerEmpty}>
            <FileText size={26} />
            <p>{c("選取筆記以閱讀內容與修訂歷程。", "Select a note to read its content and revision history.")}</p>
          </div>
        )}
      </div>
    </>
  );
}

export default function NotesPanel({ runName, live }: { runName: string; live: boolean }) {
  const { locale } = useI18n();
  const c = (zh: string, en: string) => locale === "en" ? en : zh;
  const [draft, setDraft] = React.useState(EMPTY_FILTERS);
  const [filters, setFilters] = React.useState(EMPTY_FILTERS);
  const [offset, setOffset] = React.useState(0);
  const tagsHintId = React.useId();
  const draftTags = draft.tags.split(",").map(tag => tag.trim()).filter(Boolean);
  const invalidTags = draftTags.length > 20 || draftTags.some(tag => Array.from(tag).length > 64);
  const apply = (event: React.FormEvent) => {
    event.preventDefault();
    if (invalidTags) return;
    setOffset(0);
    setFilters({ ...draft });
  };

  return (
    <section className={styles.panel} aria-label={c("共享筆記", "Shared notes")}>
      <div className={styles.intro}>
        <div>
          <FileText size={16} />
          <h2>{c("共享筆記", "Shared notes")}</h2>
          <span>{c("唯讀", "Read only")}</span>
        </div>
        <p>
          {c(
            "供 Agent 共享參考資訊；筆記不代表已驗證的漏洞。",
            "Reference information shared by agents. Notes are not verified vulnerabilities.",
          )}
        </p>
      </div>
      <form className={styles.filters} onSubmit={apply} aria-label={c("篩選筆記", "Filter notes")}>
        <label>
          <span>{c("搜尋", "Search")}</span>
          <input
            className="input-shell"
            value={draft.search}
            maxLength={500}
            placeholder={c("標題、內容或標籤", "Title, content, or tags")}
            onChange={event => setDraft(value => ({ ...value, search: event.target.value }))}
          />
        </label>
        <label>
          <span>{c("分類", "Category")}</span>
          <select
            className="input-shell"
            value={draft.category}
            onChange={event => setDraft(value => ({ ...value, category: event.target.value }))}
          >
            <option value="">{c("所有分類", "All categories")}</option>
            {NOTE_CATEGORIES.map(category => <option key={category} value={category}>{category}</option>)}
          </select>
        </label>
        <label>
          <span>{c("標籤（逗號分隔）", "Tags (comma-separated)")}</span>
          <input
            className="input-shell"
            value={draft.tags}
            maxLength={2600}
            aria-invalid={invalidTags}
            aria-describedby={invalidTags ? tagsHintId : undefined}
            onChange={event => setDraft(value => ({ ...value, tags: event.target.value }))}
          />
        </label>
        <label>
          <span>{c("作者或更新者", "Author or updater")}</span>
          <input
            className="input-shell"
            value={draft.author}
            maxLength={200}
            onChange={event => setDraft(value => ({ ...value, author: event.target.value }))}
          />
        </label>
        <div className={styles.filterActions}>
          <label className={styles.deletedToggle}>
            <input
              type="checkbox"
              checked={draft.includeDeleted}
              onChange={event => setDraft(value => ({ ...value, includeDeleted: event.target.checked }))}
            />
            <span>{c("包含已刪除", "Include deleted")}</span>
          </label>
          <button
            type="button"
            className="button-secondary"
            onClick={() => { setDraft(EMPTY_FILTERS); setFilters(EMPTY_FILTERS); setOffset(0); }}
          >
            {c("清除", "Clear")}
          </button>
          <button type="submit" className="button-primary" disabled={invalidTags}>
            <Search size={14} />{c("套用篩選", "Apply filters")}
          </button>
        </div>
        {invalidTags && (
          <p className={styles.filterError} id={tagsHintId} role="alert">
            {c("最多可使用 20 個標籤，每個標籤不得超過 64 個字元。", "Use at most 20 tags, with no more than 64 characters per tag.")}
          </p>
        )}
      </form>
      <NotesResults
        key={`${runName}:${JSON.stringify(filters)}:${offset}`}
        runName={runName}
        live={live}
        filters={filters}
        offset={offset}
        onOffset={setOffset}
      />
    </section>
  );
}
