import type { Grade, SignalState } from "../types";

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
    default:
      return "Neutral";
  }
}

// Trend arrow derived from delta_pct. Direction only; interpretation of good vs
// bad is carried by the signal state, not the arrow.
export function trendArrow(deltaPct?: number | null): {
  glyph: string;
  label: string;
} {
  if (deltaPct == null || Number.isNaN(deltaPct)) {
    return { glyph: "→", label: "no change" };
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
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

export function shortDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function minutesToHm(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  if (h <= 0) return `${m}m`;
  return `${h}h ${m}m`;
}
