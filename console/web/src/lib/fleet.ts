"use client";

import { useMemo, useSyncExternalStore } from "react";
import { getJSON, type Health, type RunsPage } from "./api";
import { useAuth } from "./auth";
import { createFleetResource, EMPTY_FLEET } from "./fleet-resource";

let current: { identity: string; resource: ReturnType<typeof createFleetResource> } | null = null;

function sessionFleet(identity: string) {
  if (!current || current.identity !== identity || current.resource.isRetired()) {
    current = { identity, resource: createFleetResource({
      runs: signal => getJSON<RunsPage>("/api/runs", { signal }),
      health: signal => getJSON<Health>("/api/health", { signal }),
      visible: () => document.visibilityState === "visible",
      watchVisibility: listener => {
        document.addEventListener("visibilitychange", listener);
        return () => document.removeEventListener("visibilitychange", listener);
      },
      watchAuthClear: listener => {
        window.addEventListener("strixops:auth-cleared", listener);
        return () => window.removeEventListener("strixops:auth-cleared", listener);
      },
    }) };
  }
  return current.resource;
}

export function useFleet() {
  const auth = useAuth();
  const identity = auth.status === "authenticated" ? auth.session.csrf_token : "";
  const resource = useMemo(() => sessionFleet(identity), [identity]);
  const state = useSyncExternalStore(resource.subscribe, resource.getSnapshot, () => EMPTY_FLEET);
  return { ...state, refresh: resource.refresh, forgetRuns: resource.forgetRuns };
}
