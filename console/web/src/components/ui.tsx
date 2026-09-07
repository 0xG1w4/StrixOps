"use client";

import * as React from "react";
import { MatrixText } from "@/components/MatrixText";

function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/* ============================================================================
   TONES
   ========================================================================= */

export type Tone =
  | "accent"
  | "success"
  | "warning"
  | "danger"
  | "violet"
  | "neutral";

/** Status string -> pill tone. Identical map on every page. */
const STATUS_TONES: Record<string, Tone> = {
  // cyan
  running: "accent",
  preparing: "accent",
  live: "accent",
  // gold
  waiting: "warning",
  timeout: "warning",
  stopped: "warning",
  interrupted: "warning",
  stale: "warning",
  // green
  reporting: "success",
  completed: "success",
  // red
  failed: "danger",
  crashed: "danger",
  // violet
  dispatched: "violet",
  // gray
  pending: "neutral",
  unknown: "neutral",
  neutral: "neutral",
};

function statusTone(status: string): Tone {
  return STATUS_TONES[(status || "unknown").toLowerCase()] ?? "neutral";
}

function statusCode(status: string): string {
  const key = (status || "unknown").toLowerCase();
  if (["running", "preparing", "live"].includes(key)) return "RUN";
  if (["reporting", "completed"].includes(key)) return "OK";
  if (["failed", "crashed"].includes(key)) return "ERR";
  if (["waiting", "timeout", "stopped", "interrupted", "stale"].includes(key)) return "WAIT";
  if (key === "dispatched") return "DSP";
  return "IDLE";
}

/* ============================================================================
   PANEL
   ========================================================================= */

export function Panel({
  title,
  code,
  actions,
  tone = "default",
  className,
  children,
}: {
  title?: string;
  code?: string;
  actions?: React.ReactNode;
  tone?: "default" | "cyan" | "success" | "danger" | "violet";
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className={cn(
        "panel panel-hairline",
        tone !== "default" && `panel-${tone}`,
        className
      )}
    >
      {(title || actions) && (
        <div className="panel-header">
          {title ? (
            <h2 className="panel-title">
              {code && <span className="panel-code">[{code}]</span>}
              {title}
            </h2>
          ) : <span />}
          {actions && <div className="panel-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

/* ============================================================================
   METRIC CARD
   ========================================================================= */

export function MetricCard({
  label,
  value,
  code,
  matrix = false,
  tone = "neutral",
  hint,
}: {
  label: string;
  value: string | number;
  code?: string;
  matrix?: boolean;
  tone?: Tone;
  hint?: string;
}) {
  return (
    <div className={cn("metric-card", tone !== "neutral" && `metric-${tone}`)}>
      {code && <div className="metric-code">[{code}]</div>}
      <div className="metric-label">{label}</div>
      <div className="metric-value">
        {matrix ? <MatrixText value={value} /> : value}
      </div>
      {hint && <div className="metric-detail">{hint}</div>}
    </div>
  );
}

/* ============================================================================
   STATUS PILL
   ========================================================================= */

export function StatusPill({
  status,
  label,
  live = false,
}: {
  status: string;
  label?: React.ReactNode;
  live?: boolean;
}) {
  const tone = statusTone(status);
  return (
    <span className={cn("status-pill", `status-${tone}`)}>
      {live && <span className="status-pill-dot animate-pulse" aria-hidden="true" />}
      <span className="status-code" aria-hidden="true">[{statusCode(status)}]</span>
      {label ?? status}
    </span>
  );
}

/* ============================================================================
   SEVERITY CHIP
   ========================================================================= */

const SEV_CLASSES: Record<string, string> = {
  CRITICAL: "sev-critical",
  HIGH: "sev-high",
  MEDIUM: "sev-medium",
  LOW: "sev-low",
  INFO: "sev-info",
};

export function SeverityChip({ severity }: { severity: string }) {
  const key = (severity || "INFO").toUpperCase();
  return (
    <span className={cn("sev-chip", SEV_CLASSES[key] ?? "sev-info")}>{key}</span>
  );
}

/* ============================================================================
   CHIP — mono metadata chip with tone tints
   ========================================================================= */

export function Chip({
  tone = "default",
  children,
}: {
  tone?: "default" | "accent" | "success" | "warning" | "danger" | "violet" | "neutral";
  children: React.ReactNode;
}) {
  return (
    <span className={cn("mono-chip", tone !== "default" && `chip-${tone}`)}>
      {children}
    </span>
  );
}

/* ============================================================================
   MICRO LABEL
   ========================================================================= */

export function MicroLabel({ children }: { children: React.ReactNode }) {
  return <span className="micro-label">{children}</span>;
}

/* ============================================================================
   EMPTY STATE
   ========================================================================= */

export function EmptyState({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-title">{title}</div>
      {hint && <p className="empty-copy">{hint}</p>}
      {action}
    </div>
  );
}

/* ============================================================================
   TABS
   ========================================================================= */

export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: string[];
  active: string;
  onChange: (tab: string) => void;
}) {
  return (
    <div className="tabs-bar" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab}
          type="button"
          role="tab"
          aria-selected={tab === active}
          className={cn("tab-item", tab === active && "tab-item-active")}
          onClick={() => onChange(tab)}
        >
          {tab}
        </button>
      ))}
    </div>
  );
}

/* ============================================================================
   SPINNER
   ========================================================================= */

export function Spinner({ className }: { className?: string }) {
  return <span className={cn("spinner", className)} aria-hidden="true" />;
}

/* ============================================================================
   CONFIRM BUTTON — two-stage click guard
   ========================================================================= */

export function ConfirmButton({
  onConfirm,
  label,
  confirmLabel = "Confirm?",
  danger = false,
}: {
  onConfirm: () => void;
  label: string;
  confirmLabel?: string;
  danger?: boolean;
}) {
  const [armed, setArmed] = React.useState(false);

  React.useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), 3200);
    return () => window.clearTimeout(id);
  }, [armed]);

  return (
    <button
      type="button"
      className={cn(
        danger ? "button-danger" : "button-primary",
        armed && "animate-pulse"
      )}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else {
          setArmed(true);
        }
      }}
    >
      {armed ? confirmLabel : label}
    </button>
  );
}
