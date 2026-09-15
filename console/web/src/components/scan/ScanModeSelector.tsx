"use client";

import { useI18n } from "@/lib/i18n";
import { scanMode, scanModeHint, scanModeLabel, type ScanMode } from "@/lib/scan-mode";

export default function ScanModeSelector({ id, value, onChange, disabled }: {
  id: string;
  value: ScanMode;
  onChange: (value: ScanMode) => void;
  disabled?: boolean;
}) {
  const { locale } = useI18n();
  const en = locale === "en";
  return (
    <div className="min-w-0 space-y-2">
      <label htmlFor={id} className="field-label">{en ? "Testing depth" : "测试深度"}</label>
      <select id={id} className="input-shell w-full" value={value} disabled={disabled}
        aria-describedby={`${id}-hint`} onChange={event => onChange(scanMode(event.target.value))}>
        <option value="default">{scanModeLabel("default", en)}</option>
        <option value="deep">{scanModeLabel("deep", en)}</option>
      </select>
      <p id={`${id}-hint`} className="text-xs leading-relaxed text-fg-muted">{scanModeHint(value, en)}</p>
    </div>
  );
}
