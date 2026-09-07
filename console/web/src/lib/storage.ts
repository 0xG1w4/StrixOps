/* ============================================================================
   localStorage keys shared across pages — single definition so the launcher,
   rerun dialog and run list can never drift apart.
   ========================================================================= */

/** Launcher model route: { base, key, model } — never leaves the browser. */
export const LLM_CFG_KEY = "strixops_llm_cfg";

/** Last operator instruction text, replayed into the launcher textarea. */
export const INSTRUCTION_KEY = "strixops_last_instruction";

/** Dashboard run-list page size (30/50/100). */
export const PAGE_SIZE_KEY = "strixops_runs_page_size";

export function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // storage unavailable — callers keep their defaults
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* best-effort */
  }
}

export function removeStorage(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* best-effort */
  }
}
