const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function parseDate(iso: string | number | null | undefined): Date | null {
  if (iso === null || iso === undefined || iso === "") return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "Sep 3, 14:07" — locale-stable, or "—" for empty/invalid input. */
export function fmtTime(
  iso: string | number | null | undefined,
  locale?: "zh-CN" | "en"
): string {
  const d = parseDate(iso);
  if (!d) return "—";
  if (locale) {
    return new Intl.DateTimeFormat(locale, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(d);
  }
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** "2d 04h" / "1h 07m" / "12m 30s" / "45s" — or "—" for null/undefined/negative. */
export function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined || Number.isNaN(sec) || sec < 0) return "—";
  const total = Math.floor(sec);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (days > 0) return `${days}d ${pad2(hours)}h`;
  if (hours > 0) return `${hours}h ${pad2(minutes)}m`;
  if (minutes > 0) return `${minutes}m ${pad2(seconds)}s`;
  return `${seconds}s`;
}

/** "just now" / "3m ago" / "2h ago" / "4d ago" — falls back to fmtTime beyond 14d. */
export function relTime(
  iso: string | number | null | undefined,
  locale?: "zh-CN" | "en"
): string {
  const d = parseDate(iso);
  if (!d) return "—";
  const deltaMs = d.getTime() - Date.now();
  const diffMs = Math.abs(deltaMs);
  const absSec = Math.abs(diffMs) / 1000;
  if (locale) {
    const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    if (absSec < 45) return formatter.format(0, "second");
    if (absSec < 3600) return formatter.format(Math.round(deltaMs / 60000), "minute");
    if (absSec < 86400) return formatter.format(Math.round(deltaMs / 3600000), "hour");
    if (absSec < 1209600) return formatter.format(Math.round(deltaMs / 86400000), "day");
    return fmtTime(iso, locale);
  }
  if (absSec < 45) return "just now";
  const minutes = Math.round(absSec / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days}d ago`;
  return fmtTime(iso);
}
