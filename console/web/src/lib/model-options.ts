import type { ModelApiMode, ModelReasoningEffort } from "./api";

export const API_MODES: ModelApiMode[] = ["auto", "chat_completions", "responses"];
export const REASONING_EFFORTS: ModelReasoningEffort[] = ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"];

const SNAPSHOT = "(?:-\\d{4}-\\d{2}-\\d{2})?";
const ASTRA = new RegExp(`^gpt-6-astra${SNAPSHOT}$`);
const GPT54_PRO = new RegExp(`^gpt-5\\.4-pro${SNAPSHOT}$`);
const GPT54 = new RegExp(`^gpt-5\\.4(?:-mini|-nano)?${SNAPSHOT}$`);
const GPT55 = new RegExp(`^gpt-5\\.5${SNAPSHOT}$`);
const GPT56 = new RegExp(`^gpt-5\\.6(?:-sol|-terra|-luna)?${SNAPSHOT}$`);

// Match documented names only. A gateway's custom deployment alias stays configurable.
function knownModelName(model: string): string {
  let name = model.trim().toLowerCase();
  while (name.startsWith("openrouter/") || name.startsWith("litellm/")) name = name.slice(name.indexOf("/") + 1);
  return name.replace(/^openai\//, "");
}

export function isAstraModel(model: string): boolean {
  return ASTRA.test(knownModelName(model));
}

export function resolveModelApiMode(model: string, mode: ModelApiMode): Exclude<ModelApiMode, "auto"> {
  const name = knownModelName(model);
  return mode === "auto" ? [ASTRA, GPT54_PRO, GPT55, GPT56].some((pattern) => pattern.test(name)) ? "responses" : "chat_completions" : mode;
}

export function allowedModelEfforts(model: string): ModelReasoningEffort[] | null {
  const name = knownModelName(model);
  if (ASTRA.test(name)) return ["low", "medium", "high", "xhigh", "max"];
  if (GPT54_PRO.test(name)) return ["medium", "high", "xhigh"];
  if (GPT54.test(name) || GPT55.test(name)) return ["none", "low", "medium", "high", "xhigh"];
  if (GPT56.test(name)) return ["none", "low", "medium", "high", "xhigh", "max"];
  return null;
}

export function modelOptionError(model: string, mode: ModelApiMode, effort: ModelReasoningEffort): "responsesRequired" | "reasoningResponses" | "unsupportedEffort" | null {
  const name = knownModelName(model);
  const resolved = resolveModelApiMode(model, mode);
  if ((ASTRA.test(name) || GPT54_PRO.test(name)) && resolved !== "responses") return "responsesRequired";
  const allowed = allowedModelEfforts(model);
  if (allowed && effort !== "default" && !allowed.includes(effort)) return "unsupportedEffort";
  if (allowed && resolved === "chat_completions" && effort !== "none"
    && (effort !== "default" || GPT55.test(name) || GPT56.test(name))) return "reasoningResponses";
  return null;
}

export function normalizeModelEndpoint(value: string): string {
  return value.trim().replace(/\/+$/, "");
}
