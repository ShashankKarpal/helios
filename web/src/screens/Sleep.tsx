import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { Chart } from "../components/Chart";
import { LoadingState, OfflineState, EmptyState } from "../components/states";
import { ProvenanceChip } from "../components/ProvenanceChip";
import { humanizeDevice, minutesToHm, shortDate } from "../lib/format";
import type { EChartsOption } from "echarts";
import type { SleepNight } from "../types";

const STAGE_META: { key: keyof NonNullable<SleepNight["stages"]>; label: string; color: string }[] = [
  { key: "deep_min", label: "Deep", color: "#7EE0B1" },
  { key: "rem_min", label: "REM", color: "#5FC8E8" },
  { key: "light_min", label: "Light", color: "#4B8FA8" },
  { key: "awake_min", label: "Awake", color: "#F87171" },
];

function hoursToHm(h: number | null | undefined): string {
  if (h == null) return "--";
  return minutesToHm(Math.round(h * 60));
}

function deltaText(value: number | null | undefined, base: number | null | undefined): string {
  if (value == null || base == null || base === 0) return "";
  const pct = ((value - base) / base) * 100;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(0)}%`;
}

export function Sleep() {
  const { data, loading, offline, reload } = useAsync(() => api.sleep(31), [], "sleep");

  if (loading) return <LoadingState label="Reviewing your nights" />;
  if (offline) return <OfflineState onRetry={reload} />;
  if (!data) return <OfflineState onRetry={reload} />;

  const nights = data.nights ?? [];
  const summary = data.summary ?? {};
  const last = summary.last_night ?? (nights.length ? nights[nights.length - 1] : null);

  // Stage architecture for the most recent night that has stages.
  const stagedNight = [...nights].reverse().find((n) => n.stages);
  const stages = stagedNight?.stages;
  const asleepMin = stages
    ? stages.deep_min + stages.rem_min + stages.light_min
    : 0;

  // Duration trend.
  const dates = nights.map((n) => shortDate(n.date));
  const hours = nights.map((n) => (n.asleep_h == null ? null : Number(n.asleep_h.toFixed(2))));
  const median = summary.median ?? null;

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
      splitLine: { lineStyle: { color: "#1c211f" } },
      axisLabel: { color: "#9AA49E", fontSize: 10, formatter: (v: number) => `${v}h` },
    },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => (v == null ? "--" : `${Number(v).toFixed(1)} h`),
    },
    series: [
      ...(median != null
        ? [{
            name: "median",
            type: "line" as const,
            data: dates.map(() => median),
            lineStyle: { color: "#9AA49E", width: 1, type: "dashed" as const },
            symbol: "none",
            z: 1,
          }]
        : []),
      {
        name: "asleep",
        type: "line",
        data: hours,
        smooth: true,
        symbol: "circle",
        symbolSize: 5,
        connectNulls: true,
        lineStyle: { color: "#7EE0B1", width: 2 },
        itemStyle: { color: "#7EE0B1" },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
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

  const stageOption: EChartsOption | null = stages
    ? {
        grid: { left: 8, right: 12, top: 6, bottom: 40, containLabel: true },
        xAxis: {
          type: "value",
          max: asleepMin + stages.awake_min,
          splitLine: { show: false },
          axisLabel: { show: false },
        },
        yAxis: {
          type: "category",
          data: [""],
          axisLine: { show: false },
          axisTick: { show: false },
        },
        tooltip: { trigger: "item", valueFormatter: (v) => minutesToHm(Number(v)) },
        legend: {
          bottom: 0,
          textStyle: { color: "#9AA49E", fontSize: 10 },
          icon: "roundRect",
          itemGap: 14,
        },
        series: STAGE_META.map((s) => ({
          name: s.label,
          type: "bar",
          stack: "night",
          data: [stages[s.key]],
          itemStyle: { color: s.color, borderRadius: 2 },
          barWidth: 26,
        })),
      }
    : null;

  return (
    <div className="space-y-6 animate-fade">
      <header>
        <h1 className="font-serif text-3xl">Sleep</h1>
        <p className="mt-1 text-sm text-muted">
          Asleep time, not time in bed. Nightly hours follow your device
          priority, {humanizeDevice("whoop")} first.
        </p>
      </header>

      {last && (
        <section>
          <SectionTitle>Last night</SectionTitle>
          <Card>
            <div className="flex items-baseline justify-between">
              <p className="font-serif text-4xl tnum">{hoursToHm(last.asleep_h)}</p>
              <ProvenanceChip deviceKey={last.device} grade={last.grade} />
            </div>
            <div className="mt-3 grid grid-cols-3 gap-3 text-sm">
              <div>
                <p className="text-muted text-xs">Efficiency</p>
                <p className="tnum">
                  {last.efficiency_pct != null ? `${last.efficiency_pct.toFixed(0)}%` : "--"}
                </p>
              </div>
              <div>
                <p className="text-muted text-xs">In bed</p>
                <p className="tnum">{hoursToHm(last.in_bed_h)}</p>
              </div>
              <div>
                <p className="text-muted text-xs">Window</p>
                <p className="tnum">
                  {last.fell_asleep && last.woke ? `${last.fell_asleep} to ${last.woke}` : "--"}
                </p>
              </div>
            </div>
          </Card>
        </section>
      )}

      {stages && stagedNight && (
        <section>
          <SectionTitle>Stages ({shortDate(stagedNight.date)})</SectionTitle>
          <Card>
            <div className="mb-2 flex items-center justify-between">
              <div className="flex gap-4 text-sm">
                {STAGE_META.filter((s) => s.key !== "awake_min").map((s) => (
                  <div key={s.key}>
                    <p className="text-muted text-xs">{s.label}</p>
                    <p className="tnum">
                      {minutesToHm(stages[s.key])}
                      {asleepMin > 0 && (
                        <span className="text-muted text-xs">
                          {" "}({Math.round((stages[s.key] / asleepMin) * 100)}%)
                        </span>
                      )}
                    </p>
                  </div>
                ))}
                <div>
                  <p className="text-muted text-xs">Awake</p>
                  <p className="tnum">{minutesToHm(stages.awake_min)}</p>
                </div>
              </div>
              <ProvenanceChip deviceKey={stagedNight.stage_source} />
            </div>
            {stageOption && <Chart option={stageOption} height={110} />}
            <p className="mt-1 text-xs text-muted">
              Percentages are shares of time asleep. Awake time never counts as sleep.
            </p>
          </Card>
        </section>
      )}

      <section>
        <SectionTitle>How this week compares</SectionTitle>
        <Card>
          <div className="grid grid-cols-3 gap-3 text-sm">
            <div>
              <p className="text-muted text-xs">7-night avg</p>
              <p className="tnum">{hoursToHm(summary.avg_7d)}</p>
              <p className="text-xs text-muted tnum">
                {deltaText(summary.avg_7d, summary.avg_prev_7d)} vs prior week
              </p>
            </div>
            <div>
              <p className="text-muted text-xs">Same day last week</p>
              <p className="tnum">{hoursToHm(summary.same_weekday_last_week)}</p>
              <p className="text-xs text-muted tnum">
                {deltaText(last?.asleep_h, summary.same_weekday_last_week)} last night vs then
              </p>
            </div>
            <div>
              <p className="text-muted text-xs">Efficiency, 7-night avg</p>
              <p className="tnum">
                {summary.efficiency_avg_7d != null ? `${summary.efficiency_avg_7d.toFixed(0)}%` : "--"}
              </p>
            </div>
          </div>
        </Card>
      </section>

      <section>
        <SectionTitle>Duration trend</SectionTitle>
        <Card>
          {nights.length > 0 ? (
            <>
              <Chart option={durationOption} height={240} />
              <p className="mt-2 text-xs text-muted">
                Dashed line is your {nights.length}-night median
                {median != null ? ` of ${median.toFixed(1)} h` : ""}.
              </p>
            </>
          ) : (
            <EmptyState
              title="No sleep recorded yet"
              body="Nights appear once your devices sync."
            />
          )}
        </Card>
      </section>
    </div>
  );
}
