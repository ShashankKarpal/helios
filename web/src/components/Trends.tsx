import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import type { MetricPoint, MetricResponse } from "../types";
import { formatValue, formatDelta, humanizeDevice } from "../lib/format";

// The nine home-page metrics. "night" metrics describe last night, so today's
// daily value is already final; "day" metrics are running totals, so today is
// partial and yesterday is the last complete day.
export const TREND_METRICS = [
  { key: "recovery_score", name: "Recovery score", kind: "night", digits: 0 },
  { key: "hrv_rmssd", name: "HRV (rMSSD)", kind: "night", digits: 0 },
  { key: "resting_hr", name: "Resting heart rate", kind: "night", digits: 0 },
  { key: "sleep_duration", name: "Sleep duration", kind: "night", digits: 1 },
  { key: "respiratory_rate", name: "Respiratory rate", kind: "night", digits: 1 },
  { key: "spo2", name: "SpO2", kind: "night", digits: 1 },
  { key: "steps", name: "Steps", kind: "day", digits: 0 },
  { key: "active_energy", name: "Active energy", kind: "day", digits: 0 },
  { key: "basal_energy", name: "Basal energy", kind: "day", digits: 0 },
] as const;

export type MetricDef = (typeof TREND_METRICS)[number];

export interface TrendData {
  def: MetricDef;
  values: (number | null)[];
  head: MetricPoint | null;
  label: string;
  deltaPct: number | null;
}

/// Local-timezone ISO date, offset days back. The server keys daily_values by
/// local date, so UTC-based toISOString() would be wrong for evening hours.
function localIso(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() - offsetDays);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
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

function buildTrend(def: MetricDef, resp: MetricResponse | null): TrendData {
  const byDate = new Map<string, MetricPoint>();
  (resp?.series ?? []).forEach((p) => byDate.set(String(p.date).slice(0, 10), p));

  const today = localIso(0);
  // Window of 7 days ending on the latest complete day.
  const endOffset = def.kind === "night" && byDate.has(today) ? 0 : 1;
  const dates: string[] = [];
  for (let i = 6; i >= 0; i--) dates.push(localIso(endOffset + i));
  const values = dates.map((d) => byDate.get(d)?.value ?? null);

  const head = byDate.get(dates[6]) ?? null;
  const label =
    def.kind === "night" ? (endOffset === 0 ? "last night" : "prev night") : "yesterday";

  const prior = values.slice(0, 6).filter((v): v is number => v != null);
  const avg = prior.length ? prior.reduce((a, b) => a + b, 0) / prior.length : null;
  const deltaPct = head != null && avg ? ((head.value - avg) / avg) * 100 : null;

  return { def, values, head, label, deltaPct };
}

/// Fetches all nine 7-day series in parallel (a failed metric renders as no
/// sparkline instead of sinking the screen). Cached across tab switches.
export function useTrends(): { trends: Record<string, TrendData>; ready: boolean } {
  const { data } = useAsync(
    () =>
      Promise.all(
        TREND_METRICS.map((m) => api.metric(m.key, 9).catch(() => null))
      ),
    [],
    "trends7d"
  );
  const trends: Record<string, TrendData> = {};
  if (data) TREND_METRICS.forEach((m, i) => (trends[m.key] = buildTrend(m, data[i])));
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
                {formatValue(t.head?.value ?? null, m.digits)}
              </span>
              <span className="text-sm text-muted">{t.head?.unit ?? ""}</span>
              <span className="text-xs text-muted tnum">
                {t.label}
                {t.deltaPct != null ? ` · ${formatDelta(t.deltaPct)} vs 7d` : ""}
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
