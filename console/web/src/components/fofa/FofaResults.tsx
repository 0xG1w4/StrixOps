"use client";
import * as React from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { useRouter } from "next/navigation";
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Columns3,
  ExternalLink,
  Filter,
  ListPlus,
  X,
} from "lucide-react";
import { Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import { apiURL } from "@/lib/api";
import { taskRequest } from "@/lib/task-batches";
import {
  EMPTY_FOFA_FILTER,
  fofaError,
  fofaParams,
  fofaStatus,
  type FofaFilter,
  type FofaResults as Results,
  type FofaRow,
  type FofaSearch,
} from "@/lib/fofa";
import { useQueueResource } from "@/components/batches/useQueueResource";
import styles from "./FofaWorkspace.module.css";

type Column = {
  key: keyof FofaRow;
  zh: string;
  en: string;
  width: number;
  sort: boolean;
};
const COLUMNS: Column[] = [
  { key: "host", zh: "主机 / URL", en: "Host / URL", width: 255, sort: true },
  { key: "title", zh: "标题", en: "Title", width: 210, sort: true },
  { key: "ip", zh: "IP", en: "IP", width: 145, sort: true },
  { key: "port", zh: "端口", en: "Port", width: 78, sort: true },
  { key: "protocol", zh: "协议", en: "Protocol", width: 95, sort: true },
  { key: "domain", zh: "域名", en: "Domain", width: 185, sort: true },
  { key: "country", zh: "国家 / 地区", en: "Country", width: 100, sort: true },
  { key: "region", zh: "区域", en: "Region", width: 125, sort: true },
  { key: "city", zh: "城市", en: "City", width: 115, sort: true },
  { key: "server", zh: "服务", en: "Server", width: 160, sort: true },
  {
    key: "lastupdatetime",
    zh: "最近更新",
    en: "Last updated",
    width: 155,
    sort: true,
  },
];
const DEFAULT_COLUMNS = [
  "host",
  "title",
  "ip",
  "port",
  "protocol",
  "country",
  "lastupdatetime",
];
const FILTERS: Array<{
  key: Exclude<keyof FofaFilter, "web_only" | "q">;
  zh: string;
  en: string;
}> = [
  { key: "host", zh: "主机包含", en: "Host contains" },
  { key: "ip", zh: "IP 包含", en: "IP contains" },
  { key: "port", zh: "端口", en: "Exact port" },
  { key: "protocol", zh: "协议", en: "Exact protocol" },
  { key: "title", zh: "标题包含", en: "Title contains" },
  { key: "domain", zh: "域名包含", en: "Domain contains" },
  { key: "country", zh: "国家代码", en: "Exact country" },
  { key: "region", zh: "区域包含", en: "Region contains" },
  { key: "city", zh: "城市包含", en: "City contains" },
  { key: "server", zh: "服务包含", en: "Server contains" },
  { key: "lastupdatetime", zh: "更新时间包含", en: "Updated time contains" },
];
function externalURL(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export default function FofaResults({ search }: { search: FofaSearch }) {
  const { locale, t } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => (en ? english : zh);
  const router = useRouter();
  const [draft, setDraft] = React.useState<FofaFilter>({
    ...EMPTY_FOFA_FILTER,
  });
  const [filters, setFilters] = React.useState<FofaFilter>({
    ...EMPTY_FOFA_FILTER,
  });
  const [sort, setSort] = React.useState("position");
  const [order, setOrder] = React.useState<"asc" | "desc">("asc");
  const [limit, setLimit] = React.useState(50);
  const [offset, setOffset] = React.useState(0);
  const [columns, setColumns] = React.useState<string[]>(DEFAULT_COLUMNS);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [detail, setDetail] = React.useState<FofaRow | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const previousFocus = React.useRef<HTMLElement | null>(null);
  const pageCheckbox = React.useRef<HTMLInputElement>(null);
  const closeRef = React.useRef<HTMLButtonElement>(null);
  const mounted = React.useRef(true);
  const pending = React.useRef<AbortController | null>(null);
  React.useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      pending.current?.abort();
    };
  }, []);
  const params = fofaParams(filters);
  params.set("sort", sort);
  params.set("order", order);
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  const path = `/api/fofa/searches/${encodeURIComponent(search.search_id)}/results?${params}`;
  const resource = useQueueResource<Results>(
    path,
    ["queued", "running"].includes(search.status),
  );
  const results = resource.data;
  const rows = Array.isArray(results?.results) ? results.results : [];
  const usableRows = rows.filter((row) => row.selectable);
  const pageSelected = usableRows.filter((row) =>
    selected.has(row.result_id),
  ).length;
  React.useEffect(() => {
    if (pageCheckbox.current)
      pageCheckbox.current.indeterminate =
        pageSelected > 0 && pageSelected < usableRows.length;
  }, [pageSelected, usableRows.length]);
  const filteredColumns = COLUMNS.filter((column) =>
    columns.includes(column.key),
  );
  const filterDirty = JSON.stringify(draft) !== JSON.stringify(filters);
  const validPort =
    !draft.port.trim() ||
    (/^\d{1,5}$/.test(draft.port.trim()) &&
      Number(draft.port) >= 1 &&
      Number(draft.port) <= 65535);
  const choose = (row: FofaRow, checked: boolean) => {
    if (!row.selectable || busy) return;
    if (checked && selected.size >= 100) {
      setNotice(fofaError("selection_limit", en));
      return;
    }
    setSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(row.result_id);
      else next.delete(row.result_id);
      return next;
    });
    setNotice("");
  };
  const choosePage = (checked: boolean) => {
    const next = new Set(selected);
    for (const row of usableRows)
      if (checked) next.add(row.result_id);
      else next.delete(row.result_id);
    if (next.size > 100) {
      setNotice(fofaError("selection_limit", en));
      return;
    }
    setSelected(next);
    setNotice("");
  };
  const createDraft = async (allMatching = false) => {
    if (busy || (!allMatching && !selected.size)) return;
    setBusy(true);
    setNotice("");
    const controller = new AbortController();
    pending.current = controller;
    try {
      const data = await taskRequest<{
        draft_id: string;
        target_count: number;
      }>(`/api/fofa/searches/${encodeURIComponent(search.search_id)}/draft`, {
        method: "POST",
        signal: controller.signal,
        body: allMatching
          ? { all_matching: true, filters: { ...filters, web_only: true } }
          : { result_ids: [...selected] },
      });
      if (mounted.current && data.draft_id)
        router.push(`/scan?fofa_draft=${encodeURIComponent(data.draft_id)}`);
      else if (mounted.current) setNotice(fofaError("invalid_response", en));
    } catch (error) {
      if (mounted.current) setNotice(fofaError(error, en));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const exportParams = fofaParams(filters);
  exportParams.set("sort", sort);
  exportParams.set("order", order);
  const openDetail = (
    row: FofaRow,
    event: React.MouseEvent<HTMLButtonElement>,
  ) => {
    previousFocus.current = event.currentTarget;
    setDetail(row);
  };
  const applyFilters = (event: React.FormEvent) => {
    event.preventDefault();
    if (!validPort) return;
    setFilters({ ...draft });
    setOffset(0);
    setNotice("");
  };

  return (
    <>
      <section
        className={styles.results}
        aria-label={c("FOFA 搜索结果", "FOFA search results")}
      >
        <div className={styles.resultHead}>
          <div className={styles.counts}>
            <span>
              {c("FOFA 总量", "FOFA total")}
              <strong>{search.total_available?.toLocaleString() ?? "—"}</strong>
            </span>
            <span>
              {c("已载入", "Loaded")}
              <strong>{search.loaded_count.toLocaleString()}</strong>
            </span>
            <span>
              {c("符合筛选", "Matching")}
              <strong>{results?.total.toLocaleString() ?? "—"}</strong>
            </span>
            <span>
              {c("已选", "Selected")}
              <strong>{selected.size}</strong>
            </span>
          </div>
          <span className={styles.status} data-status={search.status}>
            {fofaStatus(search.status, en)}
          </span>
        </div>
        {search.error_code && (
          <div className={styles.tableError} role="status">
            {fofaError(search.error_code, en)}{" "}
            {search.loaded_count > 0 &&
              c(
                "已取得的结果仍可查看与使用。",
                "Previously retrieved results remain available.",
              )}
          </div>
        )}
        {search.limited && (
          <p className={styles.hint} style={{ padding: "0 14px 10px" }}>
            {c(
              "本次只载入所设上限内的结果；筛选、排序与全选均针对已载入的记录。",
              "This search loaded up to the chosen limit. Filtering, sorting and selection apply to loaded records.",
            )}
          </p>
        )}
        <form className={styles.filters} onSubmit={applyFilters}>
          <div className={styles.filterRow}>
            <input
              className="input-shell"
              aria-label={c("筛选已载入结果", "Filter loaded results")}
              placeholder={c(
                "在已载入结果中搜索…",
                "Search within loaded results…",
              )}
              value={draft.q}
              maxLength={500}
              onChange={(event) =>
                setDraft((value) => ({ ...value, q: event.target.value }))
              }
            />
            <label>
              <input
                type="checkbox"
                checked={draft.web_only}
                onChange={(event) =>
                  setDraft((value) => ({
                    ...value,
                    web_only: event.target.checked,
                  }))
                }
              />
              {c("只显示可建立 Web 任务的资产", "Web task targets only")}
            </label>
            <button
              className="button-secondary button-compact"
              type="submit"
              disabled={!validPort || !filterDirty}
            >
              {c("应用筛选", "Apply filters")}
            </button>
            <button
              className="button-secondary button-compact"
              type="button"
              onClick={() => {
                setDraft({ ...EMPTY_FOFA_FILTER });
                setFilters({ ...EMPTY_FOFA_FILTER });
                setOffset(0);
              }}
            >
              {c("清除", "Clear")}
            </button>
          </div>
          <details className={styles.filterDetails}>
            <summary>
              <Filter size={12} />
              {c("字段筛选", "Field filters")}
              <ChevronDown size={12} />
            </summary>
            <div className={styles.filterGrid}>
              {FILTERS.map((field) => (
                <label key={field.key}>
                  <span>{en ? field.en : field.zh}</span>
                  <input
                    className="input-shell"
                    value={draft[field.key]}
                    maxLength={field.key === "port" ? 5 : 200}
                    inputMode={field.key === "port" ? "numeric" : undefined}
                    onChange={(event) =>
                      setDraft((value) => ({
                        ...value,
                        [field.key]: event.target.value,
                      }))
                    }
                  />
                </label>
              ))}
            </div>
          </details>
          {!validPort && (
            <p className={styles.hint} role="status">
              {c(
                "端口须为 1–65535 的整数。",
                "Port must be an integer from 1 to 65535.",
              )}
            </p>
          )}
        </form>
        <div className={styles.tableControls}>
          <small>
            {c(
              "跨页筛选与排序 · 选取在翻页后保留",
              "Filter and sort across pages · Selection persists across pages",
            )}
          </small>
          <div className={styles.actions}>
            <a
              className="button-secondary button-compact"
              href={apiURL(
                `/api/fofa/searches/${encodeURIComponent(search.search_id)}/export?${exportParams}&format=csv`,
              )}
              download
            >
              {c("CSV", "CSV")}
            </a>
            <a
              className="button-secondary button-compact"
              href={apiURL(
                `/api/fofa/searches/${encodeURIComponent(search.search_id)}/export?${exportParams}&format=json`,
              )}
              download
            >
              JSON
            </a>
            <details className={styles.columnPicker}>
              <summary>
                <Columns3 size={13} />
                {c("字段", "Columns")}
              </summary>
              <div className={styles.columnList}>
                {COLUMNS.map((column) => (
                  <label key={column.key}>
                    <input
                      type="checkbox"
                      checked={columns.includes(column.key)}
                      disabled={column.key === "host"}
                      onChange={(event) =>
                        setColumns((previous) =>
                          event.target.checked
                            ? [...previous, column.key]
                            : previous.filter((value) => value !== column.key),
                        )
                      }
                    />
                    {en ? column.en : column.zh}
                  </label>
                ))}
              </div>
            </details>
          </div>
        </div>
        {resource.error && (
          <div className={styles.notice} role="status">
            <span>
              {fofaError(resource.error, en)}
              {resource.data &&
                c(" 显示上次结果。", " Showing previous results.")}
            </span>
            <button
              type="button"
              className="button-secondary button-compact"
              onClick={resource.retry}
            >
              {t("common.retry")}
            </button>
          </div>
        )}
        {resource.loading ? (
          <div className={styles.empty}>
            <Spinner />
            <p>{t("common.loading")}</p>
          </div>
        ) : rows.length ? (
          <div
            className={styles.tableScroll}
            tabIndex={0}
            aria-label={c("可横向滚动的搜索结果", "Scrollable search results")}
          >
            <table
              className={styles.table}
              style={{
                minWidth: filteredColumns.reduce(
                  (sum, column) => sum + column.width,
                  106,
                ),
              }}
            >
              <colgroup>
                <col style={{ width: 42 }} />
                {filteredColumns.map((column) => (
                  <col key={column.key} style={{ width: column.width }} />
                ))}
                <col style={{ width: 64 }} />
              </colgroup>
              <thead>
                <tr>
                  <th>
                    <input
                      ref={pageCheckbox}
                      type="checkbox"
                      aria-label={c(
                        "选择本页可用资产",
                        "Select eligible assets on this page",
                      )}
                      checked={
                        usableRows.length > 0 &&
                        pageSelected === usableRows.length
                      }
                      disabled={busy || !usableRows.length}
                      onChange={(event) => choosePage(event.target.checked)}
                    />
                  </th>
                  {filteredColumns.map((column) => (
                    <th
                      key={column.key}
                      aria-sort={
                        sort === column.key
                          ? order === "asc"
                            ? "ascending"
                            : "descending"
                          : undefined
                      }
                    >
                      {column.sort ? (
                        <button
                          type="button"
                          onClick={() => {
                            setOrder(
                              sort === column.key && order === "asc"
                                ? "desc"
                                : "asc",
                            );
                            setSort(column.key);
                            setOffset(0);
                          }}
                        >
                          {en ? column.en : column.zh}
                          {sort === column.key &&
                            (order === "asc" ? (
                              <ArrowUp size={11} />
                            ) : (
                              <ArrowDown size={11} />
                            ))}
                        </button>
                      ) : en ? (
                        column.en
                      ) : (
                        column.zh
                      )}
                    </th>
                  ))}
                  <th>{c("详情", "Details")}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.result_id}
                    data-selected={selected.has(row.result_id)}
                  >
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`${c("选择", "Select")} ${row.host || row.ip}`}
                        checked={selected.has(row.result_id)}
                        disabled={!row.selectable || busy}
                        onChange={(event) => choose(row, event.target.checked)}
                      />
                    </td>
                    {filteredColumns.map((column) => (
                      <td key={column.key}>
                        {column.key === "host" ? (
                          <>
                            <button
                              type="button"
                              className={styles.targetButton}
                              onClick={(event) => openDetail(row, event)}
                            >
                              <span
                                className={`${styles.cell} ${styles.mono}`}
                                title={row.host}
                              >
                                {row.host || row.ip || "—"}
                              </span>
                            </button>
                            {!row.selectable && (
                              <span className={styles.unavailable}>
                                {row.selection_reason === "unsupported_protocol"
                                  ? c(
                                      "非 Web 资产 · 需确认",
                                      "Non-Web asset · Review required",
                                    )
                                  : c("目标格式需确认", "Target needs review")}
                              </span>
                            )}
                          </>
                        ) : (
                          <span
                            className={`${styles.cell} ${column.key !== "title" ? styles.mono : ""}`}
                            title={String(row[column.key] ?? "")}
                          >
                            {String(row[column.key] ?? "") || "—"}
                          </span>
                        )}
                      </td>
                    ))}
                    <td>
                      <button
                        type="button"
                        className={styles.targetButton}
                        aria-label={`${c("查看资产详情", "View asset details")} ${row.host || row.ip}`}
                        onClick={(event) => openDetail(row, event)}
                      >
                        {c("查看", "View")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className={styles.empty}>
            <p>
              {resource.error
                ? c(
                    "结果暂时无法读取。",
                    "Results are temporarily unavailable.",
                  )
                : ["queued", "running"].includes(search.status) &&
                    search.loaded_count === 0
                  ? c("正在从 FOFA 取得结果…", "Retrieving results from FOFA…")
                  : c(
                      "没有符合筛选条件的结果。",
                      "No results match these filters.",
                    )}
            </p>
          </div>
        )}
        <footer className={styles.pagination}>
          <span>
            {results && results.total > 0
              ? `${offset + 1}–${offset + rows.length} / ${results.total}`
              : `0 / ${results?.total ?? "—"}`}
          </span>
          <div className={styles.actions}>
            <label>
              {c("每页", "Per page")}{" "}
              <select
                className="select-shell"
                aria-label={c("每页结果数", "Results per page")}
                value={limit}
                onChange={(event) => {
                  setLimit(Number(event.target.value));
                  setOffset(0);
                }}
              >
                {[25, 50, 100].map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className={styles.iconButton}
              aria-label={c("上一页", "Previous page")}
              disabled={offset === 0 || resource.loading}
              onClick={() => setOffset((value) => Math.max(0, value - limit))}
            >
              <ChevronLeft size={15} />
            </button>
            <button
              type="button"
              className={styles.iconButton}
              aria-label={c("下一页", "Next page")}
              disabled={!results?.has_more || resource.loading}
              onClick={() => setOffset((value) => value + limit)}
            >
              <ChevronRight size={15} />
            </button>
          </div>
        </footer>
      </section>
      {notice && (
        <div className={styles.notice} role="status" style={{ marginTop: 10 }}>
          {notice}
        </div>
      )}
      <div
        className={styles.selection}
        aria-label={c("选取操作", "Selection actions")}
      >
        <div>
          <strong>
            {selected.size} {c("项已选", "selected")}
          </strong>
          <small>
            {c(
              "最多 100 个不同目标 · 各目标独立任务",
              "Up to 100 distinct targets · Independent tasks",
            )}
          </small>
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className="button-secondary button-compact"
            disabled={!selected.size || busy}
            onClick={() => setSelected(new Set())}
          >
            {c("清除选取", "Clear selection")}
          </button>
          <button
            type="button"
            className="button-secondary button-compact"
            disabled={
              busy ||
              resource.loading ||
              !results?.total ||
              results.total > 100 ||
              !filters.web_only
            }
            title={c(
              "先筛选为 Web 目标且符合数量不超过 100。",
              "Filter to Web targets with no more than 100 matches first.",
            )}
            onClick={() => void createDraft(true)}
          >
            {c("使用全部符合 Web 目标", "Use all matching Web targets")}
          </button>
          <button
            type="button"
            className="button-primary button-compact"
            disabled={!selected.size || busy}
            onClick={() => void createDraft()}
          >
            {busy ? <Spinner /> : <ListPlus size={14} />}
            {c("配置选取任务", "Configure selected tasks")}
          </button>
        </div>
      </div>
      <Dialog.Root
        open={Boolean(detail)}
        onOpenChange={(open) => {
          if (!open) setDetail(null);
        }}
      >
        <Dialog.Portal>
          <Dialog.Overlay className={styles.overlay} />
          <Dialog.Content
            className={styles.drawer}
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              closeRef.current?.focus({ preventScroll: true });
            }}
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              if (previousFocus.current?.isConnected)
                previousFocus.current.focus({ preventScroll: true });
            }}
          >
            <header className={styles.drawerHead}>
              <div>
                <Dialog.Title>{c("资产详情", "Asset details")}</Dialog.Title>
                <Dialog.Description>
                  {c(
                    "来自 FOFA 的资产记录，不代表漏洞或已验证的可达性。",
                    "A FOFA asset record does not establish a vulnerability or verified reachability.",
                  )}
                </Dialog.Description>
              </div>
              <Dialog.Close asChild>
                <button
                  ref={closeRef}
                  type="button"
                  className={styles.iconButton}
                  aria-label={c("关闭资产详情", "Close asset details")}
                >
                  <X size={17} />
                </button>
              </Dialog.Close>
            </header>
            <div className={styles.drawerBody}>
              {detail && (
                <dl className={styles.detailList}>
                  {COLUMNS.map((column) => (
                    <div key={column.key}>
                      <dt>{en ? column.en : column.zh}</dt>
                      <dd>{String(detail[column.key] ?? "") || "—"}</dd>
                    </div>
                  ))}
                  <div>
                    <dt>{c("建立任务目标", "Task target")}</dt>
                    <dd>
                      {detail.web_target ||
                        c(
                          "无法直接建立 Web 任务，请人工确认协议与范围。",
                          "Cannot launch a Web task directly. Review its protocol and scope.",
                        )}
                    </dd>
                  </div>
                </dl>
              )}
            </div>
            <footer className={styles.drawerFoot}>
              {detail?.selectable && (
                <button
                  type="button"
                  className="button-primary button-compact"
                  disabled={busy}
                  onClick={() =>
                    choose(detail, !selected.has(detail.result_id))
                  }
                >
                  {selected.has(detail.result_id)
                    ? c("取消选取", "Deselect asset")
                    : c("选取此资产", "Select this asset")}
                </button>
              )}
              {detail && externalURL(detail.web_target) && (
                <a
                  className="button-secondary button-compact"
                  href={externalURL(detail.web_target)!}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink size={13} />
                  {c("打开网址", "Open URL")}
                </a>
              )}
            </footer>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </>
  );
}
