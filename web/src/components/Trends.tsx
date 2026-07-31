import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import type { MetricPoint, MetricResponse } from "../types";
import { Card, SectionTitle } from "./Card";
import { formatValue, formatDelta, humanizeDevice } from "../lib/format";

// The nine home-page metrics, in display order. "night" metrics describe last
// night, so today's daily value is already final; "day" metrics are running
// totals, so today is partial and yesterday is the last complete day.
const METRICS = [
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

type MetricDef = (typeof METRICS)[number];

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
function Sparkline({ values }: { values: (number | null)[] }) {
  const w = 100;
  const h = 36;
  const pad = 3;
  const nums = values.filter((v): v is number => v != null);
  if (nums.length < 2) {
    return <p className="text-right text-xs text-muted">not enough data</p>;
  }
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
      className="h-10 w-full"
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
      />
      {lastVal != null ? (
        <circle cx={x(lastIdx)} cy={y(lastVal)} r="2.2" fill="var(--mint)" />
      ) : null}
    </svg>
  );
}

function TrendRow({ def, resp }: { def: MetricDef; resp: MetricResponse | null }) {
  const byDate = new Map<string, MetricPoint>();
  (resp?.series ?? []).forEach((p) => byDate.set(String(p.date).slice(0, 10), p));

  const today = localIso(0);
  // Window of 7 days ending on the headline day.
  const endOffset = def.kind === "night" && byDate.has(today) ? 0 : 1;
  const dates: string[] = [];
  for (let i = 6; i >= 0; i--) dates.push(localIso(endOffset + i));
  const values = dates.map((d) => byDate.get(d)?.value ?? null);

  const head = byDate.get(dates[6]) ?? null;
  const label =
    def.kind === "night" ? (endOffset === 0 ? "Last night" : "Prev night") : "Yesterday";

  const prior = values.slice(0, 6).filter((v): v is number => v != null);
  const avg = prior.length ? prior.reduce((a, b) => a + b, 0) / prior.length : null;
  const deltaPct = head != null && avg ? ((head.value - avg) / avg) * 100 : null;

  return (
    <div className="flex items-center gap-4 border-t border-hairline py-3.5 first:border-t-0 first:pt-0">
      <div className="w-[32%] min-w-0 shrink-0">
        <p className="truncate text-sm text-muted">{def.name}</p>
        <p className="mt-0.5 truncate text-[11px] text-muted/70">
          {head ? humanizeDevice(head.device_key) : ""}
        </p>
      </div>
      <div className="w-[26%] shrink-0">
        <p className="font-serif text-2xl leading-none tnum">
          {formatValue(head?.value ?? null, def.digits)}
          <span className="ml-1 text-xs text-muted">{head?.unit ?? ""}</span>
        </p>
        <p className="mt-1 text-[11px] text-muted tnum">
          {label}
          {deltaPct != null ? ` · ${formatDelta(deltaPct)} vs 7d` : ""}
        </p>
      </div>
      <div className="min-w-0 flex-1">
        <Sparkline values={values} />
      </div>
    </div>
  );
}

/// "Last 7 days" home section: one row per metric with the latest complete
/// value and a 7-day sparkline. Fetches all nine series in parallel; a metric
/// whose fetch fails renders as "not enough data" instead of sinking the section.
export function TrendsSection() {
  const { data } = useAsync(
    () => Promise.all(METRICS.map((m) => api.metric(m.key, 9).catch(() => null))),
    [],
    "trends7d"
  );
  return (
    <section>
      <SectionTitle>Last 7 days</SectionTitle>
      <Card>
        {data ? (
          METRICS.map((m, i) => <TrendRow key={m.key} def={m} resp={data[i]} />)
        ) : (
          <p className="text-sm text-muted">Loading trends...</p>
        )}
      </Card>
    </section>
  );
}
