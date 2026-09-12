"use client";

import { useSyncExternalStore } from "react";

export interface AuthSession {
  authenticated: true;
  username: "strix";
  must_change_password: boolean;
  csrf_token: string;
  expires_at: string;
  last_login: { at: string; ip: string } | null;
}

export interface AuthAccount {
  username: "strix";
  must_change_password: boolean;
  login_history: Array<{ at: string; ip: string }>;
}

type AuthSnapshot =
  | { status: "loading" | "anonymous" | "unavailable"; session: null }
  | { status: "authenticated"; session: AuthSession };

const INITIAL: AuthSnapshot = { status: "loading", session: null };
let snapshot: AuthSnapshot = INITIAL;
let generation = 0;
let refreshPending: Promise<void> | null = null;
const listeners = new Set<() => void>();
const pendingRequests = new Set<AbortController>();

function update(next: AuthSnapshot) {
  const changedIdentity = snapshot.session?.csrf_token !== next.session?.csrf_token;
  const locked = next.status !== "authenticated" || next.session.must_change_password;
  if (changedIdentity || (locked && snapshot.status === "authenticated" && !snapshot.session.must_change_password)) {
    generation += 1;
    for (const request of pendingRequests) request.abort();
    pendingRequests.clear();
    if (locked && typeof window !== "undefined") window.dispatchEvent(new Event("strixops:auth-cleared"));
  }
  snapshot = next;
  for (const listener of listeners) listener();
}

export function useAuth() {
  return useSyncExternalStore(
    subscribeAuth,
    () => snapshot,
    () => INITIAL,
  );
}

function subscribeAuth(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function parseSession(value: unknown): AuthSession | null {
  if (!value || typeof value !== "object") throw new AuthError("invalid_response");
  const data = value as Record<string, unknown>;
  if (data.authenticated === false) return null;
  if (data.authenticated !== true || data.username !== "strix" || typeof data.must_change_password !== "boolean" || typeof data.csrf_token !== "string" || !data.csrf_token || typeof data.expires_at !== "string") {
    throw new AuthError("invalid_response");
  }
  return data as unknown as AuthSession;
}

export class AuthError extends Error {
  constructor(readonly code: string) { super(code); this.name = "AuthError"; }
}

async function errorCode(response: Response): Promise<string> {
  const payload = await response.clone().json().catch(() => null);
  return typeof payload?.detail?.code === "string" ? payload.detail.code : "request_failed";
}

/** The sole credentialed browser transport. Console credentials never leave this origin. */
export async function authFetch(input: string | URL, init: RequestInit = {}): Promise<Response> {
  const url = new URL(String(input), window.location.origin);
  if (url.origin !== window.location.origin) throw new AuthError("invalid_origin");
  const requestGeneration = generation;
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (init.signal?.aborted) abort();
  else init.signal?.addEventListener("abort", abort, { once: true });
  pendingRequests.add(controller);
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes((init.method ?? "GET").toUpperCase())) {
    if (snapshot.session) headers.set("X-CSRF-Token", snapshot.session.csrf_token);
    headers.set("X-StrixOps-Request", "1");
  }
  try {
    const response = await fetch(url, {
      cache: "no-store", ...init, headers, signal: controller.signal,
      credentials: "same-origin", redirect: "error",
    });
    if (generation !== requestGeneration) throw new DOMException("Session changed", "AbortError");
    if (response.status === 401) {
      update({ status: "anonymous", session: null });
    } else if (response.status === 403 && await errorCode(response) === "password_change_required") {
      if (snapshot.session) update({ status: "authenticated", session: { ...snapshot.session, must_change_password: true } });
      else update({ status: "loading", session: null });
      void refreshAuthSession();
    }
    return response;
  } finally {
    init.signal?.removeEventListener("abort", abort);
    pendingRequests.delete(controller);
  }
}

/** Also used after SSE errors; simultaneous checks share one request. */
export function refreshAuthSession(): Promise<void> {
  if (refreshPending) return refreshPending;
  const requestGeneration = generation;
  refreshPending = (async () => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10_000);
    try {
      const response = await fetch("/api/auth/session", { cache: "no-store", credentials: "same-origin", redirect: "error", signal: controller.signal });
      if (!response.ok) throw new AuthError("session_unavailable");
      const session = parseSession(await response.json());
      if (generation === requestGeneration) update(session ? { status: "authenticated", session } : { status: "anonymous", session: null });
    } catch {
      if (generation === requestGeneration) update({ status: "unavailable", session: null });
    } finally {
      window.clearTimeout(timer);
      refreshPending = null;
    }
  })();
  return refreshPending;
}

async function submitSession(path: string, body: Record<string, string>, login = false): Promise<AuthSession> {
  const requestGeneration = generation;
  const headers: Record<string, string> = { "Content-Type": "application/json", "X-StrixOps-Request": "1" };
  if (!login && snapshot.session) headers["X-CSRF-Token"] = snapshot.session.csrf_token;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 20_000);
  try {
    const transport = login ? fetch : authFetch;
    const response = await transport(`/api/auth/${path}`, { method: "POST", headers, body: JSON.stringify(body), credentials: "same-origin", redirect: "error", cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new AuthError(await errorCode(response));
    const session = parseSession(await response.json());
    if (!session) throw new AuthError("invalid_response");
    if (requestGeneration !== generation) throw new DOMException("Session changed", "AbortError");
    update({ status: "authenticated", session });
    return session;
  } finally {
    window.clearTimeout(timer);
  }
}

export const login = (username: string, password: string) => submitSession("login", { username, password }, true);
export const changePassword = (current_password: string, new_password: string) => submitSession("password", { current_password, new_password });

export async function logout(): Promise<void> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await authFetch("/api/auth/logout", { method: "POST", signal: controller.signal });
    if (!response.ok && response.status !== 401) throw new AuthError(await errorCode(response));
    update({ status: "anonymous", session: null });
  } finally { window.clearTimeout(timer); }
}

export async function getAccount(signal?: AbortSignal): Promise<AuthAccount> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(abort, 10_000);
  try {
    const response = await authFetch("/api/auth/account", { signal: controller.signal });
    if (!response.ok) throw new AuthError(await errorCode(response));
    const data = await response.json();
    if (data?.username !== "strix" || !Array.isArray(data.login_history)) throw new AuthError("invalid_response");
    return {
      username: "strix", must_change_password: data.must_change_password === true,
      login_history: data.login_history.slice(0, 5).filter((entry: unknown): entry is { at: string; ip: string } => (
        !!entry && typeof entry === "object"
        && typeof (entry as { at?: unknown }).at === "string"
        && typeof (entry as { ip?: unknown }).ip === "string"
      )),
    };
  } finally {
    window.clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
