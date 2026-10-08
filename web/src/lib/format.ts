import type { Grade, SignalState } from "../types";

// The reporting zone is the server's calendar (Asia/Dubai for this install).
// Every "today", "yesterday" and date label on the web comes from it, never
// from the browser clock: the phone may be anywhere while the Mac's day is
// the one the data is filed under. /api/today sends "zone" and the Today
// screen records it here; until then the default applies.
export const DEFAULT_ZONE = "Asia/Dubai";
let currentZone: string | null = null;

export function setReportingZone(zone?: string | null): void {
  if (zone && typeof zone === "string") currentZone = zone;
}

export function reportingZone(): string {
  return currentZone ?? DEFAULT_ZONE;
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

export function isDateOnly(s: string): boolean {
  return DATE_ONLY.test(s);
}

// A YYYY-MM-DD string is a calendar date, not an instant: parse it at UTC
// midnight and format it with timeZone UTC so no browser zone can shift it.
function parseDateOnly(iso: string): Date | null {
  if (!isDateOnly(iso)) return null;
  const [y, m, d] = iso.split("-").map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d));
  return Number.isNaN(dt.getTime()) ? null : dt;
}

function ymdInZone(now: Date, zone: string): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(now);
}

// Today's calendar date in the reporting zone (YYYY-MM-DD).
export function zoneToday(zone: string = reportingZone(), now: Date = new Date()): string {
  try {
    return ymdInZone(now, zone);
  } catch {
    return ymdInZone(now, DEFAULT_ZONE);
  }
}

// Calendar arithmetic on YYYY-MM-DD strings (no zone involved).
export function addDays(iso: string, n: number): string {
  const d = parseDateOnly(iso);
  if (!d) return iso;
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

// Humanize device keys into friendly names.
const DEVICE_NAMES: Record<string, string> = {
  apple_watch_ultra: "Apple Watch Ultra",
  apple_watch: "Apple Watch",
  whoop: "Whoop",
  zepp_helio: "Amazfit Helio",
  iphone: "iPhone",
  zepp_life_scale: "Scale",
};

export function humanizeDevice(key?: string): string {
  if (!key) return "Unknown source";
  if (DEVICE_NAMES[key]) return DEVICE_NAMES[key];
  return key
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

// Humanize a metric key like "resting_heart_rate" into "Resting Heart Rate".
export function humanizeMetric(key: string): string {
  const overrides: Record<string, string> = {
    hrv: "HRV",
    rhr: "Resting Heart Rate",
    vo2max: "VO2 Max",
    vo2_max: "VO2 Max",
    spo2: "SpO2",
    rem: "REM",
  };
  if (overrides[key]) return overrides[key];
  return key
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export function stateColorVar(state: SignalState): string {
  switch (state) {
    case "favorable":
      return "var(--mint)";
    case "flag":
      return "var(--alert)";
    case "insufficient":
    case "in_progress":
    case "fallback":
      return "var(--muted)";
    default:
      return "var(--text)";
  }
}

export function stateLabel(state: SignalState): string {
  switch (state) {
    case "favorable":
      return "Favorable";
    case "flag":
      return "Needs attention";
    case "insufficient":
      return "Not enough data";
    case "in_progress":
      return "So far today";
    case "fallback":
      return "Fallback device";
    default:
      return "Neutral";
  }
}

// A running total or a non-owner value has no comparison to show.
export function isUncompared(signal: { state: SignalState; fallback?: boolean; partial?: boolean }): boolean {
  return (
    signal.state === "in_progress" ||
    signal.state === "fallback" ||
    signal.fallback === true ||
    signal.partial === true
  );
}

// The core markers the verdict waits on, in words.
export function awaitingLabel(markers: string[]): string {
  const names: Record<string, string> = {
    recovery_score: "recovery score",
    hrv_rmssd: "HRV (rMSSD)",
    sleep_duration: "sleep",
    resting_hr_sleep: "sleeping heart rate",
    respiratory_rate: "respiratory rate",
    strain: "strain",
  };
  const words = markers.map((m) => names[m] ?? humanizeMetric(m).toLowerCase());
  if (words.length === 0) return "";
  if (words.length === 1) return words[0];
  return words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
}

// Trend arrow derived from delta_pct. Direction only; interpretation of good vs
// bad is carried by the signal state, not the arrow.
export function trendArrow(deltaPct?: number | null): {
  glyph: string;
  label: string;
} {
  // No delta means no comparison was made (a partial day, a fallback device,
  // no baseline), which is not the same as "no change": show nothing.
  if (deltaPct == null || Number.isNaN(deltaPct)) {
    return { glyph: "", label: "no comparison" };
  }
  if (deltaPct > 1.5) return { glyph: "↗", label: "trending up" };
  if (deltaPct < -1.5) return { glyph: "↘", label: "trending down" };
  return { glyph: "→", label: "steady" };
}

export function formatValue(value: number | null | undefined, digits = 0): string {
  if (value == null || Number.isNaN(value)) return "--";
  const rounded =
    Math.abs(value) >= 100 ? Math.round(value) : Number(value.toFixed(digits));
  return rounded.toLocaleString(undefined, {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function formatDelta(deltaPct?: number | null): string {
  if (deltaPct == null || Number.isNaN(deltaPct)) return "";
  const sign = deltaPct > 0 ? "+" : "";
  return `${sign}${deltaPct.toFixed(1)}%`;
}

export function gradeColorVar(grade?: Grade): string {
  switch (grade) {
    case "A":
      return "var(--mint)";
    case "B":
      return "var(--text)";
    case "C":
      return "var(--caution)";
    case "D":
      return "var(--alert)";
    default:
      return "var(--muted)";
  }
}

export function formatDate(iso: string): string {
  const dateOnly = parseDateOnly(iso);
  if (dateOnly) {
    return dateOnly.toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    });
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: reportingZone(),
  });
}

export function shortDate(iso: string): string {
  const dateOnly = parseDateOnly(iso);
  if (dateOnly) {
    return dateOnly.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  }
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: reportingZone() });
}

// "Phone data as of" label in the reporting zone: "today 06:17" on the
// reporting today, else "Oct 7, 20:20". The server sends either an
// offset-aware ISO 8601 instant (parsed as such and shown in the zone) or,
// from older builds, a naive "YYYY-MM-DD HH:MM:SS.ffffff" wall time that is
// already in the zone and is shown as written.
export function formatAsOf(ts: string, zone: string = reportingZone(), now: Date = new Date()): string {
  const s = String(ts).trim();
  const today = zoneToday(zone, now);
  const naive = s.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?$/);
  if (naive) {
    const [, day, hm] = naive;
    return day === today ? `today ${hm}` : `${shortDate(day)}, ${hm}`;
  }
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  let day: string;
  let hm: string;
  try {
    day = ymdInZone(d, zone);
    hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: zone });
  } catch {
    day = ymdInZone(d, DEFAULT_ZONE);
    hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: DEFAULT_ZONE });
  }
  return day === today ? `today ${hm}` : `${shortDate(day)}, ${hm}`;
}

export function minutesToHm(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  if (h <= 0) return `${m}m`;
  return `${h}h ${m}m`;
}
