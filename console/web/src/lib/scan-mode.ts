export type ScanMode = "default" | "deep";

/** Runs created before modes were introduced retain the existing workflow. */
export function scanMode(value: unknown): ScanMode {
  return value === "deep" ? "deep" : "default";
}

export function scanModeLabel(value: unknown, en: boolean): string {
  return scanMode(value) === "deep" ? (en ? "Deep" : "Deep 深度测试") : (en ? "Default" : "默认");
}

export function scanModeHint(value: ScanMode, en: boolean): string {
  return value === "deep"
    ? (en ? "Investigate promising leads more thoroughly and validate related paths before concluding."
      : "进一步追踪有价值的线索，验证关联路径后再总结。")
    : (en ? "Use the current assessment workflow." : "沿用现有测试流程。");
}
