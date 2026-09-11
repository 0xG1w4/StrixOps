"use client";
import * as React from "react";
import { taskRequest } from "@/lib/task-batches";

export function useQueueResource<T>(path: string | null, poll = true) {
  const [snapshot, setSnapshot] = React.useState<{
    path: string;
    data: T | null;
    error: unknown;
  } | null>(null);
  const [revision, setRevision] = React.useState(0);
  const retry = React.useCallback(() => setRevision((value) => value + 1), []);
  React.useEffect(() => {
    if (!path) return;
    let disposed = false;
    let timer: number | undefined;
    let pending: AbortController | null = null;
    let resume = false;
    const load = async () => {
      if (disposed || document.hidden || pending) return;
      const controller = new AbortController();
      pending = controller;
      try {
        const data = await taskRequest<T>(path, { signal: controller.signal });
        if (!disposed && !controller.signal.aborted)
          setSnapshot({ path, data, error: null });
      } catch (error) {
        if (!disposed && !controller.signal.aborted)
          setSnapshot((old) => ({
            path,
            data: old?.path === path ? old.data : null,
            error,
          }));
      } finally {
        pending = null;
        if (!disposed && !document.hidden) {
          if (resume) {
            resume = false;
            void load();
          } else if (poll) timer = window.setTimeout(() => void load(), 5000);
        }
      }
    };
    const visibility = () => {
      window.clearTimeout(timer);
      if (document.hidden) {
        resume = false;
        pending?.abort();
      } else if (pending) resume = true;
      else void load();
    };
    void load();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      pending?.abort();
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [path, poll, revision]);
  const current = snapshot?.path === path ? snapshot : null;
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: Boolean(path) && !current,
    retry,
  };
}
