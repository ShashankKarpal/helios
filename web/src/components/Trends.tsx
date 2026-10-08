import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import type { MetricPoint, MetricResponse } from "../types";
import { formatDelta, formatMetricValue, humanizeDevice, addDays, zoneToday } from "../lib/format";

// The nine home-page metrics. "night" metrics describe last night, so today's
// daily value is already final; "day" metrics are calendar-day values
// (running totals, or an interval that closes at day end such as Apple's
// resting heart rate), so today is partial and yesterday is the last complete
// day. Resting HR and SpO2 are day metrics (M16): a today value for them is a
// morning fragment or a fallback device, never "last night".
export const TREND_METRICS = [
  { key: "recovery_score", name: "Recovery score", kind: "night", digits: 0 },
  { key: "hrv_rmssd", name: "HRV (rMSSD)", kind: "night", digits: 0 },
  { key: "resting_hr", name: "Resting heart rate", kind: "day", digits: 0 },
  { key: "sleep_duration", name: "Sleep duration", kind: "night", digits: 1 },
  { key: "respiratory_rate", name: "Respiratory rate", kind: "night", digits: 1 },
  { key: "spo2", name: "SpO2", kind: "day", digits: 1 },
  { key: "steps", name: "Steps", kind: "day", digits: 0 },
  { key: "active_energy", name: "Active energy", kind: "day", digits: 0 },
  { key: "basal_energy", name: "Basal energy", kind: "day", digits: 0 },
] as const;

// Days fetched per metric: a 7-day window plus the 7 days before it for the
// comparison mean, plus the partial today.
const FETCH_DAYS = 16;
const PRIOR_MIN_DAYS = 3;

export type MetricDef = (typeof TREND_METRICS)[number];

export interface TrendData {
  def: MetricDef;
  // Every fetched point by date, so a row can draw the window that ends on
  // its own day (one day per row).
  byDate: Map<string, MetricPoint>;
  values: (number | null)[];
  head: MetricPoint | null;
  label: string;
  // head against the mean of the 7 days before the window (null when fewer
  // than PRIOR_MIN_DAYS of them have a value).
  deltaPct: number | null;
  priorDays: number;
  deltaLabel: string;
}


// The n-day window of values ending on endIso (inclusive), one entry per day.
export function windowEnding(t: TrendData, endIso: string, n = 7): (number | null)[] {
  const out: (number | null)[] = [];
  for (let i = n - 1; i >= 0; i--) out.push(t.byDate.get(addDays(endIso, -i))?.value ?? null);
  return out;
}

// Dependency-free SVG sparkline: nine ECharts instances on a phone would cost
// far more than these few polyline points. Gaps (null days) are skipped.
export function Sparkline({ values }: { values: (number | null)[] }) {
  const w = 100;
  const h = 32;
  const pad = 3;
  const nums = values.filter((v): v is number => v != null);
  if (nums.length < 2) return null;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const x = (i: number) => pad + (i / (values.length - 1)) * (w - 2 * pad);
  const y = (v: number) => h - pad - ((v - min) / span) * (h - 2 * pad);
  const pts = values
    .map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`))
    .filter(Boolean)
    .join(" ");
  let lastIdx = -1;
  values.forEach((v, i) => {
    if (v != null) lastIdx = i;
  });
  const lastVal = lastIdx >= 0 ? values[lastIdx] : null;
  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="none"
      className="h-8 w-full"
      aria-hidden
    >
      <polyline
        points={pts}
        fill="none"
        stroke="var(--mint)"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
        opacity="0.9"
      />
      {lastVal != null ? (
        <circle cx={x(lastIdx)} cy={y(lastVal)} r="2.2" fill="var(--mint)" />
      ) : null}
    </svg>
  );
}

function buildTrend(def: MetricDef, resp: MetricResponse | null, todayIso?: string): TrendData {
  const byDate = new Map<string, MetricPoint>();
  (resp?.series ?? []).forEach((p) => byDate.set(String(p.date).slice(0, 10), p));

  // The reporting-zone today: the server's own reporting_date when it sends
  // one, else the date /api/today reported, else the zone's calendar. The
  // browser clock is never consulted (the phone may be in another zone).
  const today = resp?.reporting_date ?? todayIso ?? zoneToday();
  // Window of 7 days ending on the latest complete day.
  const endOffset = def.kind === "night" && byDate.has(today) ? 0 : 1;
  const dates: string[] = [];
  for (let i = 6; i >= 0; i--) dates.push(addDays(today, -(endOffset + i)));
  const values = dates.map((d) => byDate.get(d)?.value ?? null);

  const head = byDate.get(dates[6]) ?? null;
  const label =
    def.kind === "night" ? (endOffset === 0 ? "last night" : "prev night") : "yesterday";

  // The comparison really covers 7 days: the 7 days before the head day,
  // not the 6 others in the window (the old "vs 7d" averaged 6).
  const priorDates: string[] = [];
  for (let i = 1; i <= 7; i++) priorDates.push(addDays(dates[6], -i));
  const prior = priorDates.map((d) => byDate.get(d)?.value ?? null).filter((v): v is number => v != null);
  const avg = prior.length >= PRIOR_MIN_DAYS ? prior.reduce((a, b) => a + b, 0) / prior.length : null;
  const deltaPct = head != null && avg ? ((head.value - avg) / avg) * 100 : null;
  const deltaLabel = prior.length === 7 ? "vs prior 7d" : `vs prior 7d (${prior.length} of 7)`;

  return { def, byDate, values, head, label, deltaPct, priorDays: prior.length, deltaLabel };
}

/// Fetches all nine 7-day series in parallel (a failed metric renders as no
/// sparkline instead of sinking the screen). Cached across tab switches.
export function useTrends(todayIso?: string): { trends: Record<string, TrendData>; ready: boolean } {
  const { data } = useAsync(
    () =>
      Promise.all(
        TREND_METRICS.map((m) => api.metric(m.key, FETCH_DAYS).catch(() => null))
      ),
    [],
    "trends7d-v2"
  );
  const trends: Record<string, TrendData> = {};
  if (data) TREND_METRICS.forEach((m, i) => (trends[m.key] = buildTrend(m, data[i], todayIso)));
  return { trends, ready: !!data };
}

/// Rows for home-page metrics that the signals list does not already show
/// (typically steps and the energy totals). Same row grammar as SignalRow:
/// name, then the digit with the sparkline married to it on the same line.
export function ExtraTrendRows({
  trends,
  exclude,
}: {
  trends: Record<string, TrendData>;
  exclude: string[];
}) {
  const missing = TREND_METRICS.filter(
    (m) => !exclude.includes(m.key) && trends[m.key]?.head != null
  );
  if (!missing.length) return null;
  return (
    <>
      {missing.map((m) => {
        const t = trends[m.key];
        return (
          <div key={m.key} className="border-t border-hairline py-4">
            <div className="flex items-center gap-2">
              <span
                className="inline-block h-2 w-2 rounded-full"
                style={{ backgroundColor: "var(--muted)" }}
              />
              <span className="text-sm text-muted">{m.name}</span>
            </div>
            <div className="mt-1.5 flex items-baseline gap-2">
              <span className="font-serif text-3xl tnum">
                {formatMetricValue(m.key, t.head?.value ?? null, t.head?.unit, m.digits).text}
              </span>
              <span className="text-sm text-muted">
                {formatMetricValue(m.key, t.head?.value ?? null, t.head?.unit, m.digits).unit}
              </span>
              <span className="text-xs text-muted tnum">
                {t.label}
                {t.deltaPct != null ? ` · ${formatDelta(t.deltaPct)} ${t.deltaLabel}` : ""}
              </span>
              <div className="min-w-0 flex-1 self-center pl-3">
                <Sparkline values={t.values} />
              </div>
            </div>
            <p className="mt-2 text-[11px] text-muted/70">
              {t.head ? humanizeDevice(t.head.device_key) : ""}
            </p>
          </div>
        );
      })}
    </>
  );
}
