import { useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card } from "../components/Card";
import { EmptyState, ErrorState, LoadingState, OfflineState, StaleBanner } from "../components/states";
import {
  archiveLine,
  dayLabel,
  ECG_ROW,
  formatDuration,
  newerLine,
  olderStillFollow,
  waveformRows,
  wallParts,
} from "../lib/history";
import type { EcgRow, EcgsResponse } from "../types";

// History > ECG (rank 5, 2026-10-29). The recordings the Apple Health export
// import kept, newest first. The classification is Apple's ECG app's own
// text exactly as recorded, labelled device-reported, in the same plain
// colour for every recording; Helios reads nothing into it. The waveform is
// fetched only when one recording opens and drawn at one fixed scale (paper
// speed 25 mm/s, 10 mm/mV) in rows of 10 seconds.

// Kept for this page load: older pages (and the first page's next_cursor
// they follow), the open recording's row and the list's scroll position.
let olderPages: EcgRow[] = [];
let olderCursor: string | null | undefined;
let olderFor: string | null | undefined;
let openEcg: EcgRow | null = null;
let listScroll = 0;

function recordedAt(e: EcgRow): string {
  const w = wallParts(e.recorded_local);
  return w ? `${dayLabel(w.date)}, ${w.time}` : "Time not recorded";
}

function technical(e: EcgRow): string {
  return [
    formatDuration(e.duration_s),
    `${Math.round(e.sample_rate_hz * 1000) / 1000} Hz`,
    e.lead,
    e.device,
  ]
    .filter(Boolean)
    .join(" · ");
}

function Classification({ text }: { text: string | null }) {
  return (
    <span className="block text-sm">
      <span className="text-muted">Device-reported: </span>
      {text ? text : <span className="text-muted">no classification recorded</span>}
    </span>
  );
}

function EcgCard({ e, onOpen }: { e: EcgRow; onOpen: (row: EcgRow) => void }) {
  return (
    <button
      onClick={() => onOpen(e)}
      className="w-full space-y-1 rounded-2xl border border-hairline bg-surface p-4 text-left transition-colors hover:bg-hairline/30"
    >
      <span className="block text-sm tnum">{recordedAt(e)}</span>
      <Classification text={e.classification} />
      <span className="block text-xs text-muted tnum">{technical(e)}</span>
    </button>
  );
}

function Header({ data }: { data: EcgsResponse }) {
  const archive = archiveLine(data.total ?? 0, data.archive?.newest_recorded_utc ?? null, data.reporting_zone,
    "recording", "recordings");
  const newer = newerLine(data.newer_records ?? 0, "ECG");
  return (
    <div className="space-y-1">
      {archive ? <p className="text-sm">{archive}</p> : null}
      {newer ? <p className="text-xs text-muted">{newer}</p> : null}
      <p className="text-xs text-muted">{data.classification_note}</p>
    </div>
  );
}

function EcgList({ onOpen }: { onOpen: (row: EcgRow) => void }) {
  const first = useAsync(() => api.ecgs(), [], "ecgs:first");
  const [older, setOlder] = useState<EcgRow[]>(olderPages);
  const [cursor, setCursor] = useState<string | null | undefined>(olderCursor);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);

  if (first.loading) return <LoadingState label="Reading your ECG recordings" />;
  if (!first.data) {
    if (first.offline) return <OfflineState onRetry={first.reload} />;
    return <ErrorState message={first.error ?? "Something went wrong."} onRetry={first.reload} />;
  }
  const data = first.data;
  // A first page that changed since the older pages were fetched drops them.
  const keep = olderStillFollow(data.next_cursor, olderFor);
  const olderRows = keep ? older : [];
  const next = keep ? cursor : data.next_cursor;
  const rows = [...data.ecgs, ...olderRows.filter((o) => !data.ecgs.some((e) => e.ecg_id === o.ecg_id))];

  async function loadOlder() {
    if (!next || loadingMore) return;   // a second click before the first page lands
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await api.ecgs(next);
      olderPages = [...olderRows, ...page.ecgs];
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
        <EmptyState title="No ECG history imported yet." body="It comes from an Apple Health export import." />
        <Header data={data} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {first.stale ? <StaleBanner fetchedAt={first.fetchedAt} reason={first.error} onRetry={first.reload} /> : null}
      <Header data={data} />
      <div className="space-y-2">
        {rows.map((e) => (
          <EcgCard key={e.ecg_id} e={e} onOpen={onOpen} />
        ))}
      </div>
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
        <p className="text-center text-xs text-muted">The oldest recording in the archive.</p>
      )}
    </div>
  );
}

function Grid() {
  const xs = [];
  for (let x = 0; x <= ECG_ROW.width; x += ECG_ROW.gridX) xs.push(x);
  const ys = [];
  for (let y = 0; y <= ECG_ROW.height; y += ECG_ROW.gridY) ys.push(y);
  return (
    <g stroke="var(--hairline)" strokeWidth="1">
      {xs.map((x) => (
        <line key={`x${x}`} x1={x} y1={0} x2={x} y2={ECG_ROW.height} />
      ))}
      {ys.map((y) => (
        <line key={`y${y}`} x1={0} y1={y} x2={ECG_ROW.width} y2={y} />
      ))}
    </g>
  );
}

function Waveform({ id, rate }: { id: string; rate: number }) {
  const w = useAsync(() => api.ecgWaveform(id), [id], `ecg-wave:${id}`);
  if (w.loading) return <LoadingState label="Drawing the recording" />;
  if (!w.data) {
    if (w.offline) return <OfflineState onRetry={w.reload} />;
    if (w.error?.includes("(404)")) return <EmptyState title="The recording's voltages are not in the store." />;
    if (w.error?.includes("(422)")) {
      return <EmptyState title="This recording cannot be drawn here." body="It is too long, or recorded in a unit this view does not draw." />;
    }
    return <ErrorState message={w.error ?? "Something went wrong."} onRetry={w.reload} />;
  }
  const rows = waveformRows(w.data.microvolts, w.data.sample_rate_hz || rate, 10);
  return (
    <Card className="space-y-3">
      {rows.map((r) => (
        <div key={r.startS}>
          <p className="mb-1 text-xs text-muted tnum">{r.startS} s</p>
          <svg
            viewBox={`0 0 ${ECG_ROW.width} ${ECG_ROW.height}`}
            className="block w-full"
            overflow="visible"
            role="img"
            aria-label={`Recording from ${r.startS} to ${r.startS + 10} seconds`}
          >
            <Grid />
            <polyline points={r.points} fill="none" stroke="var(--text)" strokeWidth="1.5" strokeLinejoin="round" />
          </svg>
        </div>
      ))}
      <p className="text-xs text-muted tnum">
        {w.data.n_samples.toLocaleString("en-US")} samples at {Math.round(w.data.sample_rate_hz * 1000) / 1000} Hz,
        {" "}{w.data.lead}, drawn in whole microvolts. Grid: 0.2 s across, 0.5 mV up.
      </p>
    </Card>
  );
}

function EcgDetail({ row, onBack }: { row: EcgRow; onBack: () => void }) {
  // The row comes from the list (no second read of it); the waveform loads here.
  const id = row.ecg_id;
  const back = (
    <button onClick={onBack} aria-label="Back to ECG recordings" className="text-sm text-muted underline-offset-2 hover:underline">
      ‹ ECG
    </button>
  );
  const w = wallParts(row.recorded_local);
  return (
    <div className="space-y-5">
      {back}
      <div>
        <h1 className="font-serif text-2xl tnum">{w ? `${dayLabel(w.date)}, ${w.time}` : "Time not recorded"}</h1>
        {w ? <p className="mt-1 text-xs text-muted">UTC{w.offset}</p> : null}
      </div>
      <Card className="space-y-2">
        <p className="text-xs text-muted">Device-reported classification, as recorded by Apple's ECG app</p>
        <p className="text-sm">{row.classification ?? "No classification recorded"}</p>
        <p className="text-xs text-muted">Symptoms: {row.symptoms ? row.symptoms : "none recorded"}</p>
      </Card>
      {row.has_waveform ? (
        <Waveform id={id} rate={row.sample_rate_hz} />
      ) : (
        <EmptyState title="The recording's voltages are not in the store." />
      )}
      <p className="text-xs text-muted tnum">
        {[row.lead, row.unit ? `recorded in ${row.unit}` : null, row.device,
          row.software_version ? `software ${row.software_version}` : null]
          .filter(Boolean)
          .join(" · ")}
      </p>
      <p className="text-xs text-muted">Helios draws the recording only. It does not check or interpret it.</p>
    </div>
  );
}

export function Ecg() {
  const [open, setOpen] = useState<EcgRow | null>(openEcg);
  const restore = useRef(false);

  useLayoutEffect(() => {
    if (open === null && restore.current) {
      window.scrollTo(0, listScroll);
      restore.current = false;
    } else if (open !== null) {
      window.scrollTo(0, 0);
    }
  }, [open]);

  function show(row: EcgRow) {
    listScroll = window.scrollY;
    openEcg = row;
    setOpen(row);
  }
  function back() {
    openEcg = null;
    restore.current = true;
    setOpen(null);
  }

  return open ? <EcgDetail row={open} onBack={back} /> : <EcgList onOpen={show} />;
}
