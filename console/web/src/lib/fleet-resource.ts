import type { Health, RunsPage } from "./api";

export interface FleetSnapshot {
  runs: RunsPage | null;
  health: Health | null;
  runsError: string | null;
  healthError: string | null;
  syncing: boolean;
  healthLoaded: boolean;
  lastSync: number | null;
  cadenceMs: number;
}

export const EMPTY_FLEET: FleetSnapshot = {
  runs: null, health: null, runsError: null, healthError: null,
  syncing: false, healthLoaded: false, lastSync: null, cadenceMs: 4000,
};

interface FleetTransport {
  runs: (signal: AbortSignal) => Promise<RunsPage>;
  health: (signal: AbortSignal) => Promise<Health>;
  visible: () => boolean;
  watchVisibility: (listener: () => void) => () => void;
  watchAuthClear: (listener: () => void) => () => void;
}

/** One browser/session resource, shared by the shell and task dashboard. */
export function createFleetResource(transport: FleetTransport) {
  let snapshot = EMPTY_FLEET;
  const listeners = new Set<() => void>();
  let running = false, retired = false, revision = 0, signature = "";
  let failures = 0, healthFailures = 0;
  let runsTimer: ReturnType<typeof setTimeout> | undefined;
  let healthTimer: ReturnType<typeof setTimeout> | undefined;
  let unwatchVisibility: (() => void) | undefined;
  let unwatchAuth: (() => void) | undefined;
  type Request = { controller: AbortController; promise: Promise<void> };
  let runsRequest: Request | null = null, healthRequest: Request | null = null;

  const publish = (update: Partial<FleetSnapshot>) => {
    snapshot = { ...snapshot, ...update };
    listeners.forEach(listener => listener());
  };
  const allowed = () => running && !retired && transport.visible();
  const cancelRuns = () => {
    revision += 1;
    clearTimeout(runsTimer);
    runsRequest?.controller.abort();
    runsRequest = null;
  };
  const cancel = () => {
    cancelRuns();
    clearTimeout(healthTimer);
    healthRequest?.controller.abort();
    healthRequest = null;
  };

  function loadRuns(): Promise<void> {
    if (!allowed()) return Promise.resolve();
    if (runsRequest) return runsRequest.promise;
    clearTimeout(runsTimer);
    const controller = new AbortController(), startedRevision = revision;
    const request: Request = { controller, promise: Promise.resolve() };
    runsRequest = request;
    publish({ syncing: true });
    let timedOut = false;
    const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 12_000);
    const current = () => running && !retired && runsRequest === request && revision === startedRevision;
    request.promise = (async () => {
      try {
        const page = await transport.runs(controller.signal);
        if (!current() || controller.signal.aborted) return;
        const nextSignature = JSON.stringify(page), changed = nextSignature !== signature;
        signature = nextSignature;
        failures = 0;
        publish({
          runs: changed ? page : snapshot.runs,
          runsError: null, lastSync: Date.now(),
          cadenceMs: changed || page.runs.some(run => run.live) ? 4000 : 10_000,
        });
      } catch (error) {
        if (current() && (!controller.signal.aborted || timedOut)) {
          failures += 1;
          const base = snapshot.runs?.runs.some(run => run.live) ? 4000 : 10_000;
          publish({ runsError: error instanceof Error ? error.message : String(error), cadenceMs: Math.min(60_000, base * 2 ** Math.min(failures - 1, 3)) });
        }
      } finally {
        clearTimeout(deadline);
        if (current()) {
          runsRequest = null;
          publish({ syncing: false });
          if (allowed()) runsTimer = setTimeout(() => { void loadRuns(); }, snapshot.cadenceMs);
        }
      }
    })();
    return request.promise;
  }

  function loadHealth(): Promise<void> {
    if (!allowed()) return Promise.resolve();
    if (healthRequest) return healthRequest.promise;
    clearTimeout(healthTimer);
    const controller = new AbortController();
    const request: Request = { controller, promise: Promise.resolve() };
    healthRequest = request;
    let timedOut = false;
    const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 10_000);
    const current = () => running && !retired && healthRequest === request;
    request.promise = (async () => {
      try {
        const health = await transport.health(controller.signal);
        if (!current() || controller.signal.aborted) return;
        healthFailures = 0;
        publish({ health, healthError: null, healthLoaded: true });
      } catch (error) {
        if (current() && (!controller.signal.aborted || timedOut)) {
          healthFailures += 1;
          publish({ healthError: error instanceof Error ? error.message : String(error), healthLoaded: true });
        }
      } finally {
        clearTimeout(deadline);
        if (current()) {
          healthRequest = null;
          if (allowed()) healthTimer = setTimeout(() => { void loadHealth(); }, Math.min(60_000, 15_000 * 2 ** Math.min(healthFailures, 2)));
        }
      }
    })();
    return request.promise;
  }

  const refresh = async () => { await Promise.all([loadRuns(), loadHealth()]); };
  const visibility = () => {
    if (transport.visible()) void refresh();
    else { cancel(); publish({ syncing: false }); }
  };
  const clearSession = () => {
    retired = true;
    cancel();
    snapshot = EMPTY_FLEET;
    signature = "";
    listeners.forEach(listener => listener());
  };

  return {
    getSnapshot: () => snapshot,
    isRetired: () => retired,
    subscribe(listener: () => void) {
      listeners.add(listener);
      if (!running && !retired) {
        running = true;
        unwatchVisibility = transport.watchVisibility(visibility);
        unwatchAuth = transport.watchAuthClear(clearSession);
        void refresh();
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          running = false;
          cancel();
          unwatchVisibility?.(); unwatchAuth?.();
          snapshot = EMPTY_FLEET;
          signature = ""; failures = 0; healthFailures = 0;
        }
      };
    },
    refresh,
    forgetRuns(names: string[]) {
      // Invalidate any response started before the successful mutation.
      cancelRuns();
      const removed = new Set(names), page = snapshot.runs;
      if (page) {
        const runs = page.runs.filter(run => !removed.has(run.name));
        const severity: Record<string, number> = {};
        for (const run of runs) for (const [key, count] of Object.entries(run.severity)) severity[key] = (severity[key] ?? 0) + count;
        const updated = { ...page, runs, totals: { runs: runs.length, live: runs.filter(run => run.live).length, severity } };
        signature = JSON.stringify(updated);
        publish({ runs: updated, syncing: false });
      } else publish({ syncing: false });
      if (allowed()) runsTimer = setTimeout(() => { void loadRuns(); }, 0);
    },
  };
}
