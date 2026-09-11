"use client";
import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ChevronLeft,
  ChevronRight,
  Globe2,
  History,
  RefreshCw,
  Search,
  Settings2,
  X,
} from "lucide-react";
import { ConfirmButton, Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { taskDate, taskRequest } from "@/lib/task-batches";
import {
  fofaError,
  fofaStatus,
  type FofaHistory,
  type FofaSearch,
  type FofaSettings,
} from "@/lib/fofa";
import { useQueueResource } from "@/components/batches/useQueueResource";
import FofaResults from "./FofaResults";
import styles from "./FofaWorkspace.module.css";

export default function FofaWorkspace() {
  const { locale, t } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => (en ? english : zh);
  const router = useRouter();
  const params = useSearchParams();
  const selectedId = params.get("search") || "";
  const [query, setQuery] = React.useState("");
  const [maxResults, setMaxResults] = React.useState("100");
  const [historyOpen, setHistoryOpen] = React.useState(true);
  const [historyOffset, setHistoryOffset] = React.useState(0);
  const [pollSelected, setPollSelected] = React.useState(true);
  const [busy, setBusy] = React.useState<"create" | "delete" | null>(null);
  const [notice, setNotice] = React.useState("");
  const settings = useQueueResource<FofaSettings>("/api/fofa/settings", false);
  const history = useQueueResource<FofaHistory>(
    `/api/fofa/searches?limit=20&offset=${historyOffset}`,
  );
  const detail = useQueueResource<{ search: FofaSearch }>(
    selectedId ? `/api/fofa/searches/${encodeURIComponent(selectedId)}` : null,
    pollSelected,
  );
  const search =
    detail.data?.search?.search_id === selectedId ? detail.data.search : null;
  const searches = Array.isArray(history.data?.searches)
    ? history.data.searches
    : [];
  const mounted = React.useRef(true);
  const pending = React.useRef<AbortController | null>(null);
  const queryLoadedFor = React.useRef("");
  const queryInput = React.useRef<HTMLInputElement>(null);
  const currentSearchId = React.useRef(selectedId);
  currentSearchId.current = selectedId;
  React.useEffect(() => {
    mounted.current = true;
    if (window.innerWidth <= 800) setHistoryOpen(false);
    return () => {
      mounted.current = false;
      pending.current?.abort();
    };
  }, []);
  React.useEffect(() => {
    setPollSelected(true);
    setNotice("");
  }, [selectedId]);
  React.useEffect(() => {
    if (!search) return;
    setPollSelected(["queued", "running"].includes(search.status));
    if (queryLoadedFor.current !== search.search_id) {
      queryLoadedFor.current = search.search_id;
      setQuery(search.query);
      setMaxResults(String(search.max_results));
    }
  }, [search]);
  const selectSearch = (item: FofaSearch) => {
    setQuery(item.query);
    setMaxResults(String(item.max_results));
    setPollSelected(true);
    setNotice("");
    router.replace(`/fofa?search=${encodeURIComponent(item.search_id)}`);
    if (window.innerWidth <= 800) setHistoryOpen(false);
  };
  const validLimit =
    maxResults.trim() !== "" &&
    Number.isInteger(Number(maxResults)) &&
    Number(maxResults) >= 1 &&
    Number(maxResults) <= 10000;
  const ready = Boolean(settings.data?.enabled && settings.data.key_set);
  const running =
    searches.some((item) => ["queued", "running"].includes(item.status)) ||
    Boolean(search && ["queued", "running"].includes(search.status));
  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!ready || busy || !query.trim() || !validLimit || running) return;
    const submitted = query.trim();
    const searchAtSubmission = selectedId;
    setBusy("create");
    setNotice("");
    const controller = new AbortController();
    pending.current = controller;
    try {
      const data = await taskRequest<{ search: FofaSearch }>(
        "/api/fofa/searches",
        {
          method: "POST",
          body: { query: submitted, max_results: Number(maxResults) },
          signal: controller.signal,
        },
      );
      if (!mounted.current) return;
      if (!data.search?.search_id) {
        setNotice(fofaError("invalid_response", en));
        return;
      }
      if (currentSearchId.current === searchAtSubmission)
        selectSearch(data.search);
      history.retry();
    } catch (error) {
      if (mounted.current) setNotice(fofaError(error, en));
    } finally {
      if (mounted.current) setBusy(null);
    }
  };
  const remove = async () => {
    if (!search || busy) return;
    const id = search.search_id;
    setBusy("delete");
    setNotice("");
    const controller = new AbortController();
    pending.current = controller;
    try {
      await taskRequest(`/api/fofa/searches/${encodeURIComponent(id)}`, {
        method: "DELETE",
        signal: controller.signal,
      });
      if (mounted.current) {
        if (currentSearchId.current === id) {
          queryLoadedFor.current = "";
          router.replace("/fofa");
        }
        history.retry();
      }
    } catch (error) {
      if (mounted.current) setNotice(fofaError(error, en));
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.heading}>
          <h1>FOFA</h1>
          <span>{c("资产查询", "Asset search")}</span>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className="button-secondary button-compact"
            aria-expanded={historyOpen}
            aria-controls="fofa-history"
            onClick={() => setHistoryOpen((value) => !value)}
          >
            <History size={14} />
            {c("历史", "History")} · {history.data?.total ?? "—"}
          </button>
          <Link
            href="/settings#fofa-settings"
            className="button-secondary button-compact"
          >
            <Settings2 size={14} />
            {c("FOFA 设置", "FOFA settings")}
          </Link>
        </div>
      </header>
      <form className={styles.searchPanel} onSubmit={create}>
        <label className={styles.searchLabel} htmlFor="fofa-query">
          {c("查询语句", "Search query")}
        </label>
        <div className={styles.searchRow}>
          <div className={styles.query}>
            <Search size={16} aria-hidden="true" />
            <input
              ref={queryInput}
              id="fofa-query"
              placeholder='domain="example.com"'
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={4096}
              autoComplete="off"
              spellCheck={false}
              disabled={Boolean(busy)}
            />
          </div>
          <label className={styles.limit}>
            <span>{c("本次载入上限", "Results to load")}</span>
            <input
              type="number"
              min={1}
              max={10000}
              step={1}
              value={maxResults}
              onChange={(event) => setMaxResults(event.target.value)}
              disabled={Boolean(busy)}
            />
          </label>
          <button
            type="submit"
            className="button-primary"
            disabled={
              !ready || busy !== null || !query.trim() || !validLimit || running
            }
          >
            {busy === "create" ? <Spinner /> : <Search size={15} />}
            {c("查询 FOFA", "Search FOFA")}
          </button>
        </div>
        <p className={styles.hint}>
          {c(
            "查询会使用 FOFA 账号额度；只访问 FOFA API，不访问结果中的目标。默认载入 100 条，上限 10,000 条。",
            "Queries use your FOFA account allowance and access only the FOFA API, not the returned targets. Default 100 results; maximum 10,000.",
          )}
        </p>
        {!validLimit && (
          <p className={styles.hint} role="status">
            {c(
              "请输入 1–10,000 的整数。",
              "Enter an integer from 1 to 10,000.",
            )}
          </p>
        )}
        {!ready && settings.data && (
          <p className={styles.hint} role="status">
            {fofaError(
              settings.data.enabled ? "not_configured" : "disabled",
              en,
            )}{" "}
            <Link href="/settings#fofa-settings">
              {c("前往设置", "Open settings")} →
            </Link>
          </p>
        )}
        {running && (
          <p className={styles.hint} role="status">
            {c(
              "已有查询进行中；结果会逐页更新。可继续筛选已有结果。",
              "A search is running; results update as pages arrive. You can filter the results already loaded.",
            )}
          </p>
        )}
      </form>
      {(notice || settings.error) && (
        <div className={styles.notice} role="status">
          <span>{notice || fofaError(settings.error, en)}</span>
          {settings.error && (
            <button
              className="button-secondary button-compact"
              onClick={settings.retry}
            >
              {t("common.retry")}
            </button>
          )}
        </div>
      )}
      <div className={styles.workspace}>
        {historyOpen && (
          <aside
            id="fofa-history"
            className={styles.history}
            aria-label={c("FOFA 查询历史", "FOFA search history")}
          >
            <header className={styles.historyHead}>
              <span>{c("已保存查询", "Saved searches")}</span>
              <button
                className={styles.iconButton}
                aria-label={c("关闭历史", "Close history")}
                onClick={() => setHistoryOpen(false)}
              >
                <X size={13} />
              </button>
            </header>
            {history.error && (
              <div className={styles.tableError} role="status">
                {fofaError(history.error, en)}
                <button
                  className="button-secondary button-compact"
                  onClick={history.retry}
                >
                  {t("common.retry")}
                </button>
              </div>
            )}
            <div className={styles.historyList}>
              {history.loading ? (
                <p className={styles.hint}>{t("common.loading")}</p>
              ) : searches.length ? (
                searches.map((item) => (
                  <button
                    type="button"
                    className={styles.historyItem}
                    key={item.search_id}
                    disabled={Boolean(busy)}
                    aria-current={
                      item.search_id === selectedId ? "true" : undefined
                    }
                    onClick={() => selectSearch(item)}
                  >
                    <span className={styles.historyQuery}>{item.query}</span>
                    <span className={styles.historyMeta}>
                      <span>{fofaStatus(item.status, en)}</span>
                      <span>
                        {item.loaded_count.toLocaleString()} {c("条", "loaded")}
                      </span>
                    </span>
                    <span className={styles.historyMeta}>
                      {taskDate(item.created_at)}
                    </span>
                  </button>
                ))
              ) : (
                <p className={styles.hint}>
                  {history.error
                    ? c(
                        "历史记录暂时无法读取。",
                        "Search history is unavailable.",
                      )
                    : c("尚无查询记录。", "No search history yet.")}
                </p>
              )}
            </div>
            <footer className={styles.historyFoot}>
              <button
                className={styles.iconButton}
                aria-label={c("上一页历史", "Previous history page")}
                disabled={historyOffset === 0}
                onClick={() =>
                  setHistoryOffset((value) => Math.max(0, value - 20))
                }
              >
                <ChevronLeft size={13} />
              </button>
              <span>
                {Math.floor(historyOffset / 20) + 1} /{" "}
                {Math.max(1, Math.ceil((history.data?.total ?? 0) / 20))}
              </span>
              <button
                className={styles.iconButton}
                aria-label={c("下一页历史", "Next history page")}
                disabled={historyOffset + 20 >= (history.data?.total ?? 0)}
                onClick={() => setHistoryOffset((value) => value + 20)}
              >
                <ChevronRight size={13} />
              </button>
            </footer>
          </aside>
        )}
        <main className={styles.main}>
          {detail.error && (
            <div className={styles.notice} role="status">
              <span>{fofaError(detail.error, en)}</span>
              <button
                className="button-secondary button-compact"
                onClick={detail.retry}
              >
                {t("common.retry")}
              </button>
            </div>
          )}
          {search ? (
            <>
              <div className={styles.header} style={{ marginBottom: 12 }}>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <span className={styles.searchLabel}>
                    {c("当前结果来自", "Current results for")}
                  </span>
                  <span className={styles.historyQuery} title={search.query}>
                    {search.query}
                  </span>
                </div>
                <div className={styles.actions}>
                  <button
                    type="button"
                    className={styles.iconButton}
                    aria-label={c("更新查询状态", "Refresh search status")}
                    onClick={() => {
                      detail.retry();
                      history.retry();
                    }}
                  >
                    <RefreshCw size={14} />
                  </button>
                  <ConfirmButton
                    label={c("删除查询", "Delete search")}
                    confirmLabel={c("删除并停止查询", "Delete and stop search")}
                    danger
                    disabled={Boolean(busy)}
                    onConfirm={() => void remove()}
                  />
                </div>
              </div>
              <FofaResults key={search.search_id} search={search} />
            </>
          ) : (
            <div className={styles.empty}>
              {detail.loading ? <Spinner /> : <Globe2 size={29} />}
              <h2>
                {detail.loading
                  ? c("读取查询记录…", "Loading search records…")
                  : selectedId && detail.error
                    ? c("无法读取此查询", "Could not load this search")
                    : c(
                        "查询资产，再选择目标",
                        "Find assets, then choose targets",
                      )}
              </h2>
              <p>
                {c(
                  "输入 FOFA 查询语句，或打开历史查询。结果先供检查，选取后再配置任务。",
                  "Enter a FOFA query or open a saved search. Review the results, then configure tasks for selected targets.",
                )}
              </p>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
