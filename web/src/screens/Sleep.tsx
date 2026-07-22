import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { Chart } from "../components/Chart";
import { LoadingState, OfflineState, EmptyState } from "../components/states";
import { ProvenanceChip } from "../components/ProvenanceChip";
import { humanizeDevice, minutesToHm, shortDate } from "../lib/format";
import type { EChartsOption } from "echarts";
import type { SleepStage } from "../types";

const STAGE_COLORS: Record<string, string> = {
  deep: "#7EE0B1",
  rem: "#5FC8E8",
  light: "#4B8FA8",
  core: "#4B8FA8",
  awake: "#F87171",
};

function stageColor(stage: string): string {
  return STAGE_COLORS[stage.toLowerCase()] ?? "#9AA49E";
}

export function Sleep() {
  const { data, loading, offline, reload } = useAsync(() => api.sleep(30));

  if (loading) return <LoadingState label="Reviewing your nights" />;
  if (offline) return <OfflineState onRetry={reload} />;
  if (!data) return <OfflineState onRetry={reload} />;

  const durations = [...(data.durations ?? [])].sort((a, b) =>
    a.date.localeCompare(b.date)
  );
  const dates = durations.map((d) => shortDate(d.date));
  const hours = durations.map((d) => Number((d.value / 60).toFixed(2)));
  const median =
    hours.length > 0
      ? [...hours].sort((a, b) => a - b)[Math.floor(hours.length / 2)]
      : 0;

  const durationOption: EChartsOption = {
    grid: { left: 8, right: 12, top: 20, bottom: 4, containLabel: true },
    xAxis: {
      type: "category",
      data: dates,
      axisLine: { lineStyle: { color: "#232826" } },
      axisLabel: { color: "#9AA49E", fontSize: 10, interval: "auto" },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      name: "hours",
      nameTextStyle: { color: "#9AA49E", fontSize: 10 },
      splitLine: { lineStyle: { color: "#1c211f" } },
      axisLabel: { color: "#9AA49E", fontSize: 10 },
    },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(1)} h`,
    },
    series: [
      {
        name: "baseline",
        type: "line",
        data: dates.map(() => median),
        lineStyle: { color: "#9AA49E", width: 1, type: "dashed" },
        symbol: "none",
        z: 1,
      },
      {
        name: "duration",
        type: "line",
        data: hours,
        smooth: true,
        symbol: "circle",
        symbolSize: 5,
        lineStyle: { color: "#7EE0B1", width: 2 },
        itemStyle: { color: "#7EE0B1" },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(126,224,177,0.22)" },
              { offset: 1, color: "rgba(126,224,177,0)" },
            ],
          },
        },
        z: 2,
      },
    ],
  };

  // Latest night stage breakdown.
  const stages = data.stages ?? [];
  const latestDate =
    stages.length > 0
      ? stages.map((s) => s.date).sort((a, b) => b.localeCompare(a))[0]
      : null;
  const lastNight: SleepStage[] = latestDate
    ? stages.filter((s) => s.date === latestDate)
    : [];
  const stageDevice = lastNight[0]?.device_key;

  const stageOption: EChartsOption | null =
    lastNight.length > 0
      ? {
          grid: { left: 8, right: 12, top: 10, bottom: 4, containLabel: true },
          xAxis: {
            type: "value",
            splitLine: { lineStyle: { color: "#1c211f" } },
            axisLabel: {
              color: "#9AA49E",
              fontSize: 10,
              formatter: (v: number) => `${Math.round(v / 60)}h`,
            },
          },
          yAxis: {
            type: "category",
            data: ["Last night"],
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: "#9AA49E", fontSize: 10 },
          },
          tooltip: {
            trigger: "item",
            valueFormatter: (v) => minutesToHm(Number(v)),
          },
          legend: {
            bottom: 0,
            textStyle: { color: "#9AA49E", fontSize: 10 },
            icon: "roundRect",
          },
          series: lastNight.map((s) => ({
            name: s.stage,
            type: "bar",
            stack: "night",
            data: [s.minutes],
            itemStyle: { color: stageColor(s.stage), borderRadius: 2 },
            barWidth: 26,
          })),
        }
      : null;

  const totalLast = lastNight.reduce((sum, s) => sum + s.minutes, 0);

  return (
    <div className="space-y-6 animate-fade">
      <header>
        <h1 className="font-serif text-3xl">Sleep</h1>
        <p className="mt-1 text-sm text-muted">
          Sleep is owned by {humanizeDevice("whoop")}. Duration and stages come
          from that device for consistency.
        </p>
      </header>

      <section>
        <SectionTitle>Duration trend</SectionTitle>
        <Card>
          {durations.length > 0 ? (
            <>
              <Chart option={durationOption} height={240} />
              <p className="mt-2 text-xs text-muted">
                Dashed line is your {durations.length}-night median of{" "}
                {median.toFixed(1)} h.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted">No sleep durations recorded yet.</p>
          )}
        </Card>
      </section>

      <section>
        <SectionTitle>Last night stages</SectionTitle>
        {stageOption ? (
          <Card>
            <div className="mb-3 flex items-center justify-between">
              <p className="font-serif text-2xl tnum">{minutesToHm(totalLast)}</p>
              <ProvenanceChip deviceKey={stageDevice} />
            </div>
            <Chart option={stageOption} height={140} />
          </Card>
        ) : (
          <EmptyState
            title="No stage data for last night"
            body="Stage breakdown appears once your sleep device has synced."
          />
        )}
      </section>
    </div>
  );
}
