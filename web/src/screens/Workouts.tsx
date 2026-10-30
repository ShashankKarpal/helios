import { useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { EmptyState, ErrorState, LoadingState, OfflineState, StaleBanner } from "../components/states";
import {
  archiveLine,
  dayLabel,
  deviceLabel,
  formatDistance,
  formatDuration,
  formatEnergy,
  formatHr,
  hkLabel,
  metadataHeading,
  metadataPairs,
  metaLabel,
  metaValue,
  groupByMonth,
  newerLine,
  olderStillFollow,
  outlineHeight,
  outlinePoints,
  wallParts,
} from "../lib/history";
import { formatNumber } from "../lib/format";
import type { FieldSource, WorkoutDetail, WorkoutRow, WorkoutsResponse, WorkoutSourceRow } from "../types";

// History > Workouts (rank 5, 2026-10-29). The sessions the Apple Health
// export import built, newest first, 50 at a time. Display only: each value
// is shown as recorded with the device it came from; nothing is scored,
// compared or advised. A missing value is left out, never shown as 0.
// Sessions the matcher could not decide are listed as "matching undecided".
// The route is an outline with no map and no coordinates.

// Kept for this page load, like the History segment: older pages already
// fetched (and the first page's next_cursor they follow), the open session
// and the list's scroll position.
let olderPages: WorkoutRow[] = [];
let olderCursor: string | null | undefined;
let olderFor: string | null | undefined;
let openWorkout: string | null = null;
let listScroll = 0;

function when(row: WorkoutRow): string {
  const w = wallParts(row.start_local);
  return w ? `${dayLabel(w.date)}, ${w.time}` : "Time not recorded";
}

function facts(row: WorkoutRow): string {
  return [
    formatDuration(row.duration_s),
    formatDistance(row.distance, row.distance_unit),
    formatEnergy(row.energy_kcal, row.energy_unit),
    row.hr_avg != null ? `avg ${formatHr(row.hr_avg)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

function Undecided({ status }: { status: string }) {
  return status === "queued" ? <span className="ml-2 text-xs text-muted">(matching undecided)</span> : null;
}

function WorkoutCard({ row, onOpen }: { row: WorkoutRow; onOpen: (id: string) => void }) {
  return (
    <button
      onClick={() => onOpen(row.workout_id)}
      className="w-full rounded-2xl border border-hairline bg-surface p-4 text-left transition-colors hover:bg-hairline/30"
    >
      <span className="block text-xs text-muted tnum">{when(row)}</span>
      <span className="mt-0.5 block font-serif text-lg">
        {row.activity_label}
        <Undecided status={row.status} />
      </span>
      {facts(row) ? <span className="mt-1 block text-sm tnum">{facts(row)}</span> : null}
      <span className="mt-1 block text-xs text-muted">
        {(row.sources ?? []).map(deviceLabel).join(" + ")}
        {row.has_route ? " · route" : ""}
      </span>
    </button>
  );
}

function ArchiveHeader({ data }: { data: WorkoutsResponse }) {
  const archive = archiveLine(data.total ?? 0, data.archive?.newest_start_utc ?? null, data.reporting_zone, "session",
    "sessions");
  const newer = newerLine(data.newer_records ?? 0, "workout");
  return (
    <div className="space-y-1">
      {archive ? <p className="text-sm">{archive}</p> : null}
      {newer ? <p className="text-xs text-muted">{newer}</p> : null}
    </div>
  );
}

function WorkoutList({ onOpen }: { onOpen: (id: string) => void }) {
  const first = useAsync(() => api.workouts(), [], "workouts:first");
  const [older, setOlder] = useState<WorkoutRow[]>(olderPages);
  const [cursor, setCursor] = useState<string | null | undefined>(olderCursor);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  if (first.loading) return <LoadingState label="Reading your workouts" />;
  if (!first.data) {
    if (first.offline) return <OfflineState onRetry={first.reload} />;
    return <ErrorState message={first.error ?? "Something went wrong."} onRetry={first.reload} />;
  }
  const data = first.data;
  // A first page that changed since the older pages were fetched (an import
  // added sessions) drops them: they would leave a gap after the first page.
  const keep = olderStillFollow(data.next_cursor, olderFor);
  const olderRows = keep ? older : [];
  const next = keep ? cursor : data.next_cursor;
  const rows = [...data.workouts, ...olderRows.filter((o) => !data.workouts.some((w) => w.workout_id === o.workout_id))];

  async function loadOlder() {
    if (!next || loadingMore) return;   // a second click before the first page lands
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await api.workouts(next);
      olderPages = [...olderRows, ...page.workouts];
      olderCursor = page.next_cursor;
      olderFor = data.next_cursor;
      setOlder(olderPages);
      setCursor(olderCursor);
    } catch (err) {
      setMoreError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      setLoadingMore(false);
    }
  }

  if (data.total === 0) {
    return (
      <div className="space-y-4">
        <EmptyState
          title="No workout history imported yet."
          body="It comes from an Apple Health export import."
        />
        <ArchiveHeader data={data} />
      </div>
    );
  }

  const months = groupByMonth(rows);

  return (
    <div className="space-y-6">
      {first.stale ? <StaleBanner fetchedAt={first.fetchedAt} reason={first.error} onRetry={first.reload} /> : null}
      <ArchiveHeader data={data} />
      {months.map((m) => (
        <section key={m.key}>
          <SectionTitle>{m.label}</SectionTitle>
          <div className="space-y-2">
            {m.rows.map((r) => (
              <WorkoutCard key={r.workout_id} row={r} onOpen={onOpen} />
            ))}
          </div>
        </section>
      ))}
      {moreError ? <p className="text-center text-xs text-muted" role="alert">{moreError}</p> : null}
      {next ? (
        <div className="text-center">
          <button
            onClick={loadOlder}
            disabled={loadingMore}
            className="rounded-full border border-hairline px-4 py-2 text-sm transition-colors hover:bg-hairline/40 disabled:opacity-50"
          >
            {loadingMore ? "Loading..." : "Load older"}
          </button>
        </div>
      ) : (
        <p className="text-center text-xs text-muted">The oldest session in the archive.</p>
      )}
    </div>
  );
}

function From({ src }: { src: FieldSource | null }) {
  return <span className="text-xs text-muted">{src ? deviceLabel(src.label) : ""}</span>;
}

function FieldRow({ label, value, src }: { label: string; value: string | null; src: FieldSource | null }) {
  if (!value) return null;
  return (
    <div className="grid grid-cols-[7.5rem_1fr] gap-x-3 gap-y-0.5 py-1.5">
      <p className="text-xs text-muted">{label}</p>
      <div>
        <p className="text-sm tnum">{value}</p>
        <From src={src} />
      </div>
    </div>
  );
}

function Outline({ route }: { route: NonNullable<WorkoutDetail["route"]> }) {
  const box = 320;
  const pad = 12;
  const height = outlineHeight(route.width, route.height, box, pad);
  const points = `${formatNumber(route.n_points)} recorded point${route.n_points === 1 ? "" : "s"}`;
  if (!route.outline.length) {
    return (
      <Card>
        <p className="text-sm text-muted">A route is recorded ({points}), but it has too few points to draw.</p>
      </Card>
    );
  }
  return (
    <Card>
      <svg
        viewBox={`0 0 ${box} ${height}`}
        className="mx-auto block w-full max-w-xs"
        role="img"
        aria-label="Route outline"
      >
        <polyline
          points={outlinePoints(route.outline, route.width, route.height, box, pad)}
          fill="none"
          stroke="var(--text)"
          strokeWidth="1.5"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      </svg>
      <p className="mt-2 text-xs text-muted">
        The shape of the route from {points}. No map: the shape only.
      </p>
    </Card>
  );
}

function SourceCard({ s }: { s: WorkoutSourceRow }) {
  const pairs = metadataPairs(s.metadata.items);
  const counts = Object.entries(s.events.counts)
    .map(([k, n]) => `${n} ${hkLabel(k).toLowerCase()}`)
    .join(", ");
  return (
    <Card className="space-y-2">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-sm">{deviceLabel(s.label)}</p>
        <p className="text-xs text-muted">{s.role}</p>
      </div>
      <p className="text-xs text-muted">
        {[s.source_name, s.source_version ? `version ${s.source_version}` : null, s.activity_label,
          formatDuration(s.duration_s)].filter(Boolean).join(" · ")}
      </p>
      {s.statistics.length ? (
        <ul className="space-y-0.5 text-xs tnum">
          {s.statistics.map((st, i) => (
            <li key={i}>
              {hkLabel(st.hk_type)}:{" "}
              {[
                st.sum != null ? `total ${formatNumber(st.sum, 2)}` : null,
                st.avg != null ? `avg ${formatNumber(st.avg, 1)}` : null,
                st.min != null ? `min ${formatNumber(st.min, 1)}` : null,
                st.max != null ? `max ${formatNumber(st.max, 1)}` : null,
              ].filter(Boolean).join(", ")}{" "}
              {st.unit ?? ""}
            </li>
          ))}
        </ul>
      ) : null}
      {counts ? <p className="text-xs">Events: {counts}</p> : null}
      {pairs.length ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted">
            {metadataHeading(pairs.length, s.metadata.total > s.metadata.items.length)}
          </summary>
          <ul className="mt-1 space-y-0.5 break-all">
            {pairs.map(([k, v], i) => (
              <li key={i}>
                {metaLabel(k)}: {metaValue(k, v)}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </Card>
  );
}

function matchingLine(w: WorkoutDetail["workout"]): string {
  if (w.status === "queued") return "Matching undecided: the matcher could not decide which records belong together, so this one is listed as recorded.";
  if (w.status === "matched") return `${w.n_sources} records matched into this session.`;
  return "One record.";
}

function WorkoutDetailView({ id, onBack }: { id: string; onBack: () => void }) {
  const d = useAsync(() => api.workout(id), [id], `workout:${id}`);
  const back = (
    <button onClick={onBack} aria-label="Back to workouts" className="text-sm text-muted underline-offset-2 hover:underline">
      ‹ Workouts
    </button>
  );
  if (d.loading) return <div className="space-y-4">{back}<LoadingState label="Reading the session" /></div>;
  if (!d.data) {
    const gone = d.error?.includes("(404)");
    return (
      <div className="space-y-4">
        {back}
        {d.offline ? (
          <OfflineState onRetry={d.reload} />
        ) : gone ? (
          <EmptyState title="This workout is no longer in the store." />
        ) : (
          <ErrorState message={d.error ?? "Something went wrong."} onRetry={d.reload} />
        )}
      </div>
    );
  }
  const { workout: w, field_sources: fs } = d.data;
  const start = wallParts(w.start_local);
  const hr = w.hr_avg != null
    ? `avg ${formatHr(w.hr_avg)}${w.hr_min != null && w.hr_max != null ? `, ${Math.round(w.hr_min)} to ${Math.round(w.hr_max)}` : ""}`
    : null;
  return (
    <div className="space-y-6">
      {back}
      {d.stale ? <StaleBanner fetchedAt={d.fetchedAt} reason={d.error} onRetry={d.reload} /> : null}
      <div>
        <h1 className="font-serif text-2xl">
          {w.activity_label}
          <Undecided status={w.status} />
        </h1>
        <p className="mt-1 text-sm text-muted tnum">
          {start ? `${dayLabel(start.date)}, ${start.time} (${w.tz})` : "Time not recorded"}
        </p>
      </div>
      <Card className="divide-y divide-hairline">
        <FieldRow label="Duration" value={formatDuration(w.duration_s)} src={fs.timing} />
        <FieldRow label="Distance" value={formatDistance(w.distance, w.distance_unit)} src={fs.distance} />
        <FieldRow label="Active energy" value={formatEnergy(w.energy_kcal, w.energy_unit)} src={fs.energy} />
        <FieldRow label="Heart rate" value={hr} src={fs.hr} />
        <FieldRow
          label="Strain"
          value={w.strain != null ? `${Math.round(w.strain * 10) / 10} (device-reported)` : null}
          src={fs.strain}
        />
      </Card>
      {d.data.route ? (
        <section>
          <SectionTitle>Route</SectionTitle>
          <Outline route={d.data.route} />
        </section>
      ) : null}
      <section>
        <SectionTitle>Recorded by</SectionTitle>
        <p className="mb-2 text-xs text-muted">{matchingLine(w)}</p>
        <div className="space-y-2">
          {d.data.sources.map((s) => (
            <SourceCard key={s.source_id} s={s} />
          ))}
        </div>
      </section>
    </div>
  );
}

export function Workouts() {
  const [open, setOpen] = useState<string | null>(openWorkout);
  const restore = useRef(false);

  useLayoutEffect(() => {
    if (open === null && restore.current) {
      window.scrollTo(0, listScroll);
      restore.current = false;
    } else if (open !== null) {
      window.scrollTo(0, 0);
    }
  }, [open]);

  function show(id: string) {
    listScroll = window.scrollY;
    openWorkout = id;
    setOpen(id);
  }
  function back() {
    openWorkout = null;
    restore.current = true;
    setOpen(null);
  }

  return open ? <WorkoutDetailView id={open} onBack={back} /> : <WorkoutList onOpen={show} />;
}
