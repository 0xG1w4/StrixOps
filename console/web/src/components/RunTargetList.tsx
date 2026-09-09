"use client";

import { useI18n } from "@/lib/i18n";

/** Native disclosure keeps every launch target reachable by keyboard on narrow screens. */
export default function RunTargetList({ targets }: { targets: string[] }) {
  const { locale } = useI18n();
  if (targets.length < 2) return null;
  return (
    <details className="mt-3 max-w-full rounded-lg border border-line/10 bg-surface-deep/40 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-mono text-fg-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent">
        {locale === "zh-CN" ? `${targets.length} 个目标 · 查看完整范围` : `${targets.length} targets · View full scope`}
      </summary>
      <ol className="mt-3 max-h-40 list-decimal space-y-2 overflow-y-auto pl-5 font-mono text-fg-muted">
        {targets.map((target, index) => <li key={`${index}:${target}`} className="break-all pr-2">{target}</li>)}
      </ol>
    </details>
  );
}
