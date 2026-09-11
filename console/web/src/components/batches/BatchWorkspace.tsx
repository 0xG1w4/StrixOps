"use client";
import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Layers, Plus, RefreshCw } from "lucide-react";
import { ConfirmButton, Spinner } from "@/components/ui";
import { useI18n } from "@/lib/i18n";
import {
  itemActive,
  queueStatus,
  taskDate,
  taskError,
  taskRequest,
  type QueueSettings,
  type ScanBatch,
} from "@/lib/task-batches";
import { useQueueResource } from "./useQueueResource";
import styles from "./BatchWorkspace.module.css";

export function QueueCapacity({ data }: { data: QueueSettings | null }) {
  const { locale } = useI18n();
  const en = locale === "en";
  return (
    <div className={styles.capacity}>
      <span>
        {en ? "Host capacity" : "主机并行上限"}{" "}
        <strong>{data?.max_active_targets ?? "—"}</strong>
      </span>
      <span>
        {en ? "Active targets" : "正在执行"}{" "}
        <strong>{data?.active_targets ?? "—"}</strong>
      </span>
      <span>
        {en ? "Waiting targets" : "等待目标"}{" "}
        <strong>{data?.waiting_targets ?? "—"}</strong>
      </span>
      <span>
        {en ? "Blocked" : "受阻目标"}{" "}
        <strong>{data?.blocked_targets ?? "—"}</strong>
      </span>
      <Link href="/settings#task-queue">
        {en ? "Queue settings" : "队列设置"} →
      </Link>
    </div>
  );
}

const settled = (batch: ScanBatch) =>
  ["completed", "failed", "cancelled"].includes(batch.status) &&
  (batch.counts?.queued ?? 0) +
    (batch.counts?.active ?? 0) +
    (batch.counts?.blocked ?? 0) ===
    0;
const label = (batch: ScanBatch, en: boolean) =>
  batch.status === "completed" &&
  ((batch.counts?.failed ?? 0) > 0 || (batch.counts?.blocked ?? 0) > 0)
    ? en
      ? "Completed with issues"
      : "已结束 · 有失败目标"
    : queueStatus(batch.status, en);

export default function BatchWorkspace({ id }: { id?: string }) {
  const { locale, t } = useI18n();
  const en = locale === "en";
  const c = (zh: string, english: string) => (en ? english : zh);
  const router = useRouter();
  const list = useQueueResource<{ batches: ScanBatch[]; total: number }>(
    id ? null : "/api/scan-batches",
  );
  const detail = useQueueResource<ScanBatch>(
    id ? `/api/scan-batches/${encodeURIComponent(id)}` : null,
  );
  const capacity = useQueueResource<QueueSettings>("/api/scan-queue/settings");
  const [query, setQuery] = React.useState("");
  const [filter, setFilter] = React.useState("");
  const [busy, setBusy] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState("");
  const mounted = React.useRef(true);
  React.useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const batch = detail.data;
  const items = Array.isArray(batch?.items) ? batch.items : [];
  const batches = Array.isArray(list.data?.batches) ? list.data.batches : [];
  const visibleItems = items.filter(
    (item) =>
      (!filter || item.status === filter) &&
      item.target.toLowerCase().includes(query.toLowerCase()),
  );
  const visibleBatches = batches.filter(
    (item) =>
      (!filter || item.status === filter) &&
      `${item.name} ${item.id}`.toLowerCase().includes(query.toLowerCase()),
  );
  const resource = id ? detail : list;
  const refresh = () => {
    resource.retry();
    capacity.retry();
  };
  const mutate = async (path: string, key: string, method = "POST") => {
    if (busy) return;
    setBusy(key);
    setNotice("");
    try {
      await taskRequest(path, {
        method,
        ...(method === "POST" ? { body: {} } : {}),
      });
      if (!mounted.current) return;
      if (method === "DELETE") router.push("/batches");
      else {
        refresh();
        setNotice(
          c(
            "取消请求已提交，正在更新目标状态。",
            "Cancellation requested. Target statuses are being updated.",
          ),
        );
      }
    } catch (error) {
      if (mounted.current) setNotice(taskError(error, en));
    } finally {
      if (mounted.current) setBusy(null);
    }
  };
  const metrics = batch
    ? ([
        ["queued", c("排队", "Queued")],
        ["active", c("执行中", "Active")],
        ["completed", c("完成", "Completed")],
        ["failed", c("失败", "Failed")],
        ["cancelled", c("取消", "Cancelled")],
        ["blocked", c("受阻", "Blocked")],
      ] as const)
    : [];

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          {id && (
            <Link href="/batches" className={styles.breadcrumb}>
              <ArrowLeft size={12} />
              {c("所有批次", "All batches")}
            </Link>
          )}
          <h1>
            {id
              ? batch?.name || c("批次详情", "Batch details")
              : c("批次队列", "Batch queue")}
          </h1>
          <p>
            {c(
              "每个目标独立执行，使用独立的任务、容器、上下文与报告。",
              "Each target runs independently with its own task, container, context and report.",
            )}
          </p>
          {batch && (
            <p>
              {taskDate(batch.created_at)} · {batch.target_count}{" "}
              {c("个目标", "targets")} · {c("批次并行", "Batch concurrency")}{" "}
              {batch.max_concurrent} ·{" "}
              <span className={styles.status} data-status={batch.status}>
                {label(batch, en)}
              </span>
            </p>
          )}
        </div>
        <div className={styles.actions}>
          <button
            type="button"
            className="button-secondary button-compact"
            onClick={refresh}
          >
            <RefreshCw size={14} />
            {t("common.refresh") === "common.refresh"
              ? c("更新", "Refresh")
              : t("common.refresh")}
          </button>
          {!id && (
            <Link
              href="/scan?batch=1"
              className="button-primary button-compact"
            >
              <Plus size={14} />
              {c("创建批次", "Create batch")}
            </Link>
          )}
          {batch && !settled(batch) && (
            <ConfirmButton
              label={c("取消批次", "Cancel batch")}
              confirmLabel={c("确认停止未完成目标", "Stop unfinished targets")}
              danger
              disabled={Boolean(busy)}
              onConfirm={() =>
                void mutate(
                  `/api/scan-batches/${encodeURIComponent(batch.id)}/cancel`,
                  "batch",
                )
              }
            />
          )}
          {batch && settled(batch) && (
            <ConfirmButton
              label={c("删除批次记录", "Delete batch record")}
              confirmLabel={c(
                "确认删除记录，保留任务",
                "Delete record, keep tasks",
              )}
              danger
              disabled={Boolean(busy)}
              onConfirm={() =>
                void mutate(
                  `/api/scan-batches/${encodeURIComponent(batch.id)}`,
                  "delete",
                  "DELETE",
                )
              }
            />
          )}
        </div>
      </header>
      <QueueCapacity data={capacity.data} />
      {(notice || resource.error || capacity.error) && (
        <div className={styles.notice} role="status">
          <span>
            {notice || taskError(resource.error || capacity.error, en)}
            {resource.error && resource.data
              ? c(" 显示上次记录。", " Showing previous records.")
              : ""}
          </span>
          <button className="button-secondary button-compact" onClick={refresh}>
            {t("common.retry")}
          </button>
        </div>
      )}
      {batch && batch.counts.blocked > 0 && (
        <p className={styles.notice} role="status">
          {c(
            "受阻目标仍保留主机容量。请先确认原任务进程与容器已完成清理，批次记录暂时无法删除。",
            "Blocked targets retain host capacity. Verify cleanup of their original processes and containers before the batch record can be deleted.",
          )}
        </p>
      )}
      {batch && (
        <>
          <div className={styles.metrics}>
            {metrics.map(([key, text]) => (
              <div className={styles.metric} key={key}>
                <span>{text}</span>
                <strong>{batch.counts?.[key] ?? "—"}</strong>
              </div>
            ))}
          </div>
          <div className={styles.progress} aria-hidden="true">
            {metrics.map(([key]) => (
              <span
                key={key}
                data-status={key}
                style={{
                  width: `${(100 * (batch.counts?.[key] ?? 0)) / Math.max(1, batch.target_count)}%`,
                }}
              />
            ))}
          </div>
        </>
      )}
      <section
        className={styles.panel}
        aria-label={
          id ? c("批次目标", "Batch targets") : c("批次列表", "Batch list")
        }
      >
        <div className={styles.toolbar}>
          <input
            className="input-shell"
            aria-label={c("筛选列表", "Filter list")}
            placeholder={
              id
                ? c("筛选目标地址…", "Filter target addresses…")
                : c("搜索批次名称…", "Search batch names…")
            }
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <select
            className="select-shell"
            aria-label={c("状态筛选", "Filter status")}
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          >
            <option value="">{c("所有状态", "All statuses")}</option>
            {(id
              ? [
                  "queued",
                  "starting",
                  "waiting_capacity",
                  "running",
                  "cancelling",
                  "completed",
                  "failed",
                  "cancelled",
                  "blocked",
                ]
              : [
                  "queued",
                  "running",
                  "cancelling",
                  "completed",
                  "partial",
                  "failed",
                  "cancelled",
                  "blocked",
                ]
            ).map((value) => (
              <option key={value} value={value}>
                {queueStatus(value, en)}
              </option>
            ))}
          </select>
          <small>
            {id ? visibleItems.length : visibleBatches.length} /{" "}
            {id ? items.length : (list.data?.total ?? "—")}
          </small>
        </div>
        {resource.loading ? (
          <div className={styles.empty}>
            <Spinner /> {t("common.loading")}
          </div>
        ) : (id ? visibleItems.length : visibleBatches.length) === 0 ? (
          <div className={styles.empty}>
            <Layers size={22} style={{ margin: "0 auto 12px" }} />
            <p>
              {resource.error && !resource.data
                ? c(
                    "记录暂时无法读取，请重试。",
                    "Records are unavailable. Please retry.",
                  )
                : query || filter
                  ? c("没有符合条件的记录。", "No records match these filters.")
                  : id
                    ? c(
                        "此批次尚无目标记录。",
                        "No target records are available.",
                      )
                    : c(
                        "尚未建立批次。加入多个目标后，每个目标会排入独立任务。",
                        "No batches yet. Add multiple targets to queue independent tasks.",
                      )}
            </p>
            {!id && (
              <Link
                href="/scan?batch=1"
                className="button-secondary button-compact"
              >
                {c("添加目标", "Add targets")}
              </Link>
            )}
          </div>
        ) : (
          <div
            className={styles.tableScroll}
            tabIndex={0}
            aria-label={c("可横向滚动的目标表", "Scrollable target table")}
          >
            <table className={styles.table}>
              <thead>
                <tr>
                  <th>{id ? c("目标", "Target") : c("批次", "Batch")}</th>
                  <th>{c("状态", "Status")}</th>
                  <th>
                    {id
                      ? c("任务与报告", "Task and report")
                      : c("进度", "Progress")}
                  </th>
                  <th>
                    {id ? c("操作", "Actions") : c("建立时间", "Created")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {id
                  ? visibleItems.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <details className={styles.targetDetails}>
                            <summary>
                              <span className={styles.targetPreview}>
                                {item.target}
                              </span>
                              <span className={styles.targetExpand}>
                                {c("展开完整目标", "Expand full target")}
                              </span>
                              <span className={styles.targetCollapse}>
                                {c("收起完整目标", "Collapse full target")}
                              </span>
                            </summary>
                            <span className={styles.target}>{item.target}</span>
                          </details>
                          <span className={styles.meta}>{item.scan_type}</span>
                        </td>
                        <td>
                          <span
                            className={styles.status}
                            data-status={item.status}
                          >
                            {queueStatus(item.status, en)}
                          </span>
                          {(item.error_code || item.error) && (
                            <p className={styles.error}>
                              {taskError(item.error_code, en)}
                            </p>
                          )}
                        </td>
                        <td>
                          <div className={styles.actions}>
                            {item.run_name ? (
                              <Link
                                className={styles.title}
                                href={`/run?name=${encodeURIComponent(item.run_name)}`}
                              >
                                {c("打开任务", "Open task")} ↗
                              </Link>
                            ) : (
                              <span className={styles.meta}>
                                {c(
                                  "等待创建独立任务",
                                  "Waiting to create task",
                                )}
                              </span>
                            )}
                            {item.run_name && item.report_ready && (
                              <Link
                                className={styles.title}
                                href={`/run?name=${encodeURIComponent(item.run_name)}&tab=report`}
                              >
                                {c("报告", "Report")} ↗
                              </Link>
                            )}
                          </div>
                        </td>
                        <td>
                          {itemActive(item.status) && (
                            <ConfirmButton
                              label={
                                item.status === "cancelling"
                                  ? c("正在取消", "Cancelling")
                                  : c("取消目标", "Cancel target")
                              }
                              confirmLabel={c("确认取消", "Confirm cancel")}
                              danger
                              disabled={
                                Boolean(busy) || item.status === "cancelling"
                              }
                              onConfirm={() =>
                                void mutate(
                                  `/api/scan-batches/${encodeURIComponent(id)}/items/${encodeURIComponent(item.id)}/cancel`,
                                  item.id,
                                )
                              }
                            />
                          )}
                        </td>
                      </tr>
                    ))
                  : visibleBatches.map((item) => (
                      <tr key={item.id}>
                        <td>
                          <Link
                            className={styles.title}
                            href={`/batches/detail?id=${encodeURIComponent(item.id)}`}
                          >
                            {item.name || item.id}
                          </Link>
                          <span className={styles.meta}>
                            {item.target_count}{" "}
                            {c("个独立目标", "independent targets")} ·{" "}
                            {c("并行", "Concurrency")} {item.max_concurrent}
                          </span>
                        </td>
                        <td>
                          <span
                            className={styles.status}
                            data-status={item.status}
                          >
                            {label(item, en)}
                          </span>
                        </td>
                        <td>
                          <span>
                            {item.counts?.completed ?? "—"} /{" "}
                            {item.target_count}
                          </span>
                          <span className={styles.meta}>
                            {item.counts?.active ?? "—"} {c("执行", "active")} ·{" "}
                            {item.counts?.queued ?? "—"} {c("排队", "queued")} ·{" "}
                            {item.counts?.failed ?? "—"} {c("失败", "failed")}
                          </span>
                        </td>
                        <td>
                          <span className={styles.meta}>
                            {taskDate(item.created_at)}
                          </span>
                        </td>
                      </tr>
                    ))}
              </tbody>
            </table>
          </div>
        )}
        <footer className={styles.footer}>
          {c(
            "并行数量同时受批次设置与主机上限约束，超出的目标保持排队。删除批次记录不会删除任务或报告。",
            "Concurrency is bounded by both batch and host limits; excess targets remain queued. Deleting a batch record keeps its tasks and reports.",
          )}
        </footer>
      </section>
    </div>
  );
}
