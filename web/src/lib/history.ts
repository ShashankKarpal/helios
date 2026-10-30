// Pure helpers for History's Workouts and ECG segments (rank 5, 2026-10-29).
// Display only: these format what the store recorded and draw it; nothing
// here scores, compares or reads meaning into a value. Wall times come from
// the offset the server sends (the session's own zone, or the zone the ECG
// was printed in), never from the browser's zone. Checked by
// scripts/check-history.mjs.

// The .ts extension lets check-history.mjs run this file under Node directly.
import { formatNumber, humanizeDevice } from "./format.ts";

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export interface Wall {
  date: string;
  time: string;
  offset: string;
}

// "2026-01-07T08:00:00+04:00" -> { date, time "08:00", offset "+04:00" }.
export function wallParts(iso: string | null | undefined): Wall | null {
  const m = String(iso ?? "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/);
  if (!m) return null;
  return { date: m[1], time: m[2], offset: m[3] === "Z" ? "+00:00" : m[3] };
}

function ymd(date: string): [number, number, number] | null {
  const m = date.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

// "2026-01-07" -> "Wed 7 Jan 2026" (fixed English, no zone involved).
export function dayLabel(date: string): string {
  const p = ymd(date);
  if (!p) return date;
  const [y, mo, d] = p;
  const wd = new Date(Date.UTC(y, mo - 1, d)).getUTCDay();
  return `${WEEKDAYS[wd]} ${d} ${MONTHS[mo - 1].slice(0, 3)} ${y}`;
}

function shortDay(date: string): string {
  return dayLabel(date).replace(/^\w{3} /, "");
}

// "2026-06-24" -> "June 2026"
export function monthLabel(date: string): string {
  const p = ymd(date);
  return p ? `${MONTHS[p[1] - 1]} ${p[0]}` : date;
}

export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const mins = Math.round(seconds / 60);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} h` : `${h} h ${m} min`;
}

// The distance in the unit it was recorded in; metres from 1000 up read as km.
export function formatDistance(value: number | null | undefined, unit: string | null | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  const u = unit ?? "";
  if (u === "m" && value >= 1000) return `${(value / 1000).toFixed(2)} km`;
  if (u === "m" || u === "yd") return `${formatNumber(Math.round(value))} ${u}`;
  return `${value.toFixed(2)} ${u}`.trim();
}

export function formatEnergy(value: number | null | undefined, unit: string | null | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  const u = !unit || /^k?cal$/i.test(unit) ? "kcal" : unit;
  return `${formatNumber(Math.round(value))} ${u}`;
}

export function formatHr(value: number | null | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  return `${Math.round(value)} bpm`;
}

function dateInZone(instant: string, zone: string): string | null {
  const d = new Date(instant);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit" })
      .format(d);
  } catch {
    return instant.slice(0, 10);
  }
}

// Consecutive rows under the month of each row's own wall date, so a header
// never contradicts the date on its card. The list is sorted by instant, so a
// session recorded in another zone near a month's edge can bring a month
// back; that group gets its own key.
export function groupByMonth<T extends { start_local: string | null }>(rows: T[]): { key: string; label: string; rows: T[] }[] {
  const out: { key: string; label: string; rows: T[] }[] = [];
  for (const r of rows) {
    const w = wallParts(r.start_local);
    const label = w ? monthLabel(w.date) : "Time not recorded";
    const last = out[out.length - 1];
    if (last && last.label === label) last.rows.push(r);
    else out.push({ key: `${label}:${out.length}`, label, rows: [r] });
  }
  return out;
}

// Older pages fetched earlier follow the first page only while it ends where
// it ended then (same next_cursor); a newer first page drops them.
export function olderStillFollow(firstNext: string | null, savedFor: string | null | undefined): boolean {
  return firstNext != null && savedFor !== undefined && firstNext === savedFor;
}

// "1,105 sessions, from the Apple Health export (to 24 Jun 2026)". The date
// is the archive's newest, read from the data, on the reporting zone's
// calendar. Null when the archive is empty (the screen says so instead).
export function archiveLine(total: number, newestUtc: string | null, zone: string, one: string,
  many: string): string | null {
  if (!total || !newestUtc) return null;
  const day = dateInZone(newestUtc, zone);
  const n = formatNumber(total);
  return `${n} ${total === 1 ? one : many}, from the Apple Health export${day ? ` (to ${shortDay(day)})` : ""}`;
}

// Owner wording (2026-10-29): the Bridge's newer raw rows are "records",
// never "workouts"; copies of one workout from several devices overlap.
export function newerLine(n: number, kind: "workout" | "ECG"): string | null {
  if (!n) return null;
  const base = n === 1
    ? `1 newer ${kind} record reached Helios through the Bridge and is not shown here yet.`
    : `${formatNumber(n)} newer ${kind} records reached Helios through the Bridge and are not shown here yet.`;
  return kind === "workout" ? `${base} One workout can have a record from each device.` : base;
}

const HK_PREFIXES = ["HKQuantityTypeIdentifier", "HKCategoryTypeIdentifier", "HKWorkoutEventType", "HKMetadataKey", "HK"];

// "HKQuantityTypeIdentifierActiveEnergyBurned" -> "Active energy burned":
// the prefix off and the words split, nothing renamed.
export function hkLabel(id: string | null | undefined): string {
  let t = String(id ?? "");
  const prefix = HK_PREFIXES.find((p) => t.startsWith(p) && t.length > p.length);
  if (prefix) t = t.slice(prefix.length);
  const words = t.match(/[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+/g) ?? [];
  return words
    .map((w, i) => (w.length > 1 && w === w.toUpperCase() ? w : i === 0 ? w[0].toUpperCase() + w.slice(1).toLowerCase() : w.toLowerCase()))
    .join(" ");
}

// Apple's sync bookkeeping, never shown (the server drops these too).
const META_HIDDEN = new Set(["HKMetadataKeySyncVersion", "HKMetadataKeySyncIdentifier"]);

// The store holds each source's metadata pairs twice (live 2026-10-30, a
// data finding for a later round); an exact repeat of a (key, value) pair
// shows once, in first-seen order. The same key with another value stays.
export function metadataPairs(items: [string, string][]): [string, string][] {
  const seen = new Set<string>();
  return items.filter(([k, v]) => {
    const id = JSON.stringify([k, v]);
    if (META_HIDDEN.has(k) || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

// "Metadata (7)": the count of distinct pairs shown.
export function metadataHeading(shown: number, truncated: boolean): string {
  return `Metadata (${truncated ? "first " : ""}${formatNumber(shown)})`;
}

const NUM = String.raw`(-?\d+(?:\.\d+)?)`;

// A workout metadata value as the owner reads it. Each shape below is one
// the store holds (live 2026-10-30); anything else shows as recorded.
export function metaValue(key: string, value: string): string {
  const v = String(value ?? "").trim();
  if (key === "HKWeatherHumidity") {
    // The Apple export stores humidity as percent x 100 ("4300 %" is 43 %).
    const m = v.match(new RegExp(`^${NUM} %$`));
    if (m) return `${formatNumber(Number(m[1]) / 100, 1)} %`;
  }
  return value;
}

// The server sends the device registry's label; a device the registry does
// not list comes back as its store key (apple_watch_ultra), which reads as
// words here, as on the other screens.
export function deviceLabel(label: string | null | undefined): string {
  if (!label) return "Unknown source";
  return /^[a-z0-9_]+$/.test(label) ? humanizeDevice(label) : label;
}

const r1 = (v: number) => Math.round(v * 10) / 10;

// The server's outline (integers on a 0 to 1000 grid, north up) scaled into
// a box `box` wide with `pad` around it. No coordinate goes in or out.
// A narrow shape (a north-south line) is centred across the box.
export function outlinePoints(outline: number[][], width: number, height: number, box: number, pad: number): string {
  if (!outline.length) return "";
  const scale = (box - 2 * pad) / Math.max(width, height, 1);
  const dx = (box - 2 * pad - width * scale) / 2;
  return outline.map(([x, y]) => `${r1(pad + dx + x * scale)},${r1(pad + y * scale)}`).join(" ");
}

export function outlineHeight(width: number, height: number, box: number, pad: number): number {
  const scale = (box - 2 * pad) / Math.max(width, height, 1);
  return r1(height * scale + 2 * pad);
}

// One ECG row: 10 s across 1000 units (100 per second), 40 units per
// millivolt up (the paper scale, 25 mm/s and 10 mm/mV), a 0.2 s by 0.5 mV
// grid, 3 mV either side of the row's baseline. Every recording is drawn at
// this one scale; the screen lets a taller wave overflow rather than cut it.
export const ECG_ROW = { width: 1000, height: 240, unitsPerS: 100, unitsPerMv: 40, gridX: 20, gridY: 20 };

export interface WaveRow {
  startS: number;
  points: string;
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

const r2 = (v: number) => Math.round(v * 100) / 100;

// Splits the samples into rows of `perRow` seconds by each sample's time
// (index / rate), keeping every sample. Each row sits on its own median so
// the trace stays in view; the vertical scale never changes.
export function waveformRows(microvolts: number[], rate: number, perRow: number): WaveRow[] {
  if (!microvolts.length || !(rate > 0)) return [];
  const rows: number[][] = [];
  microvolts.forEach((v, i) => {
    const k = Math.floor(i / rate / perRow);
    (rows[k] ??= []).push(i);
  });
  return rows.filter(Boolean).map((idx, k) => {
    const startS = k * perRow;
    const base = median(idx.map((i) => microvolts[i]));
    const points = idx
      .map((i) => {
        const x = r2((i / rate - startS) * ECG_ROW.unitsPerS);
        const y = r2(ECG_ROW.height / 2 - ((microvolts[i] - base) / 1000) * ECG_ROW.unitsPerMv);
        return `${x},${y}`;
      })
      .join(" ");
    return { startS, points };
  });
}
