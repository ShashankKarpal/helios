import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { Chart } from "../components/Chart";
import { LoadingState, OfflineState, EmptyState } from "../components/states";
import { ProvenanceChip } from "../components/ProvenanceChip";
import { formatValue, shortDate } from "../lib/format";
import type { EChartsOption } from "echarts";
import type { ActivityPoint } from "../types";

const STEP_TARGET = 8000;

function latest(points: ActivityPoint[]): ActivityPoint | null {
  if (!points || points.length === 0) return null;
  return [...points].sort((a, b) => b.date.localeCompare(a.date))[0];
}

function Tile({
  label,
  value,
  unit,
  point,
  accent,
}: {
  label: string;
  value: string;
  unit: string;
  point: ActivityPoint | null;
  accent?: boolean;
}) {
  // Honesty over decoration: an empty tile says so instead of showing an
  // "Unknown source" chip, and a value that is not from today carries its date
  // so old backfill data is never mistaken for current.
  const todayIso = new Date().toLocaleDateString("en-CA");
  const stale = point?.date && point.date < todayIso;
  return (
    <div className="flex-1 rounded-2xl border border-hairline bg-surface p-4">
      <p className="text-xs uppercase tracking-wide text-muted">{label}</p>
      <p
        className="mt-1.5 font-serif text-2xl tnum"
        style={{ color: accent ? "var(--mint)" : "var(--text)" }}
      >
        {value}
        <span className="ml-1 text-sm text-muted">{unit}</span>
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {point ? (
          <>
            <ProvenanceChip deviceKey={point.device_key} grade={point.grade} />
            {stale && (
              <span className="text-[11px] text-muted">
                as of {shortDate(point.date)}
              </span>
            )}
          </>
        ) : (
          <span className="text-[11px] text-muted">No data synced yet</span>
        )}
      </div>
    </div>
  );
}

export function Activity() {
  const { data, loading, offline, reload } = useAsync(() => api.activity(30), [], "activity");

  if (loading) return <LoadingState label="Adding up your movement" />;
  if (offline) return <OfflineState onRetry={reload} />;
  if (!data) return <OfflineState onRetry={reload} />;

  const steps = data.steps ?? [];
  const energy = data.active_energy ?? [];
  const strain = data.strain ?? [];
  const vo2 = data.vo2max ?? [];

  const latestSteps = latest(steps);
  const latestEnergy = latest(energy);
  const latestStrain = latest(strain);
  const latestVo2 = latest(vo2);

  const sortedSteps = [...steps].sort((a, b) => a.date.localeCompare(b.date));
  const stepDates = sortedSteps.map((s) => shortDate(s.date));
  const stepValues = sortedSteps.map((s) => Math.round(s.value));

  const stepOption: EChartsOption = {
    grid: { left: 8, right: 12, top: 24, bottom: 4, containLabel: true },
    xAxis: {
      type: "category",
      data: stepDates,
      axisLine: { lineStyle: { color: "#232826" } },
      axisLabel: { color: "#9AA49E", fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#1c211f" } },
      axisLabel: {
        color: "#9AA49E",
        fontSize: 10,
        formatter: (v: number) => (v >= 1000 ? `${v / 1000}k` : `${v}`),
      },
    },
    tooltip: { trigger: "axis" },
    series: [
      {
        name: "target",
        type: "line",
        data: stepDates.map(() => STEP_TARGET),
        lineStyle: { color: "#9AA49E", width: 1, type: "dashed" },
        symbol: "none",
      },
      {
        name: "steps",
        type: "bar",
        data: stepValues.map((v) => ({
          value: v,
          itemStyle: {
            color: v >= STEP_TARGET ? "#7EE0B1" : "#4B8FA8",
            borderRadius: [3, 3, 0, 0],
          },
        })),
        barWidth: "55%",
      },
    ],
  };

  const hasAny =
    steps.length + energy.length + strain.length + vo2.length > 0;

  return (
    <div className="space-y-6 animate-fade">
      <header>
        <h1 className="font-serif text-3xl">Activity</h1>
        <p className="mt-1 text-sm text-muted">Movement, effort and capacity.</p>
      </header>

      {!hasAny ? (
        <EmptyState
          title="No activity yet"
          body="Steps, energy and strain will appear once your devices sync."
        />
      ) : (
        <>
          <div className="flex flex-wrap gap-3">
            <Tile
              label="Steps"
              value={formatValue(latestSteps?.value ?? null)}
              unit={`/ ${formatValue(STEP_TARGET)}`}
              point={latestSteps}
              accent={(latestSteps?.value ?? 0) >= STEP_TARGET}
            />
            <Tile
              label="Active energy"
              value={formatValue(latestEnergy?.value ?? null)}
              unit="kcal"
              point={latestEnergy}
            />
          </div>
          <div className="flex flex-wrap gap-3">
            <Tile
              label="Strain"
              value={formatValue(latestStrain?.value ?? null, 1)}
              unit=""
              point={latestStrain}
            />
            <Tile
              label="VO2 Max"
              value={formatValue(latestVo2?.value ?? null, 1)}
              unit="ml/kg/min"
              point={latestVo2}
            />
          </div>

          <section>
            <SectionTitle>Steps vs target</SectionTitle>
            <Card>
              {sortedSteps.length > 0 ? (
                <>
                  <Chart option={stepOption} height={240} />
                  <p className="mt-2 text-xs text-muted">
                    Dashed line marks the {formatValue(STEP_TARGET)} step target.
                  </p>
                </>
              ) : (
                <p className="text-sm text-muted">No step history yet.</p>
              )}
            </Card>
          </section>
        </>
      )}
    </div>
  );
}
