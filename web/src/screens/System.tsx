import { api, ApiError } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { ErrorState, LoadingState, OfflineState, StaleBanner } from "../components/states";
import { briefStatusLine, formatAsOf, formatNumber, humanizeDevice, humanizeMetric } from "../lib/format";
import type { HealthResponse, ToolFreshness, WatchdogRow } from "../types";

// Is the pipeline healthy (rank 4)? Read-only. It reads once each time the
// screen opens and again only on Refresh: no timer, no polling (owner,
// 2026-10-29). /api/health is open and cheap; /api/tool/freshness carries the
// brief, the watchdog rows and the Whoop window warnings, off the event loop.
// Each reply shows on its own: when the watchdog read fails (the moment this
// screen matters most), the Mac facts still show, and the reverse (review 1).

const STATUS_WORDS: Record<string, string> = {
  silent: "silent",
  stale: "late",
  error: "error",
  waiting: "waiting",
  corroboration_decayed: "second source quiet",
};

function rowTitle(r: WatchdogRow): string {
  const source = humanizeDevice(r.device_key);
  return r.metric === "*" ? source : `${humanizeMetric(r.metric)} from ${source}`;
}

function WatchRow({ row }: { row: WatchdogRow }) {
  // age_hours means "since the last data" only on rows with last_seen; on the
  // Whoop cloud and relay rows it counts from a pull or a refusal, and their
  // message or fix says what (code review 3).
  const age =
    row.last_seen && typeof row.age_hours === "number" ? `${Math.round(row.age_hours)} h since the last data` : null;
  return (
    <div className="border-t border-hairline py-3 first:border-t-0 first:pt-0">
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm">{rowTitle(row)}</p>
        <span className="shrink-0 text-xs text-caution">{STATUS_WORDS[row.status] ?? row.status}</span>
      </div>
      {row.message ? <p className="mt-1 text-xs leading-relaxed text-muted">{row.message}</p> : null}
      {age ? <p className="mt-1 text-xs text-muted">{age}</p> : null}
      {row.fix ? <p className="mt-1 text-xs leading-relaxed text-muted">{row.fix}</p> : null}
    </div>
  );
}

// One reply: the value on screen, when it was read, and this load's error.
// A failed refresh keeps the last good value with a note instead of replacing
// it with nothing (code review 2).
interface Part<T> {
  value: T | null;
  at: Date | null;
  error: string | null;
}

interface SystemData {
  health: Part<HealthResponse>;
  status: Part<ToolFreshness>;
}

// The last good read of each reply, for this page load.
let lastHealth: { value: HealthResponse; at: Date } | null = null;
let lastStatus: { value: ToolFreshness; at: Date } | null = null;

// The first sentence of an error, without its full stop: it is quoted inside
// another sentence (code review round 2, finding 7).
function why(err: unknown): string {
  const text = err instanceof Error ? err.message : "Something went wrong.";
  return text.split(". ")[0].replace(/\.$/, "");
}

type Settled<T> = { ok: true; value: T } | { ok: false; reason: unknown };

function settle<T>(p: Promise<T>): Promise<Settled<T>> {
  return p.then((value) => ({ ok: true as const, value }), (reason: unknown) => ({ ok: false as const, reason }));
}

// One after the other: /api/health's COUNT runs on heliosd's event loop and
// would wait there for the store lock while the watchdog's worker holds it
// (code review 4). Both failing fails the screen, offline first, else naming
// both errors (code review 5); one failing leaves the other with a note.
async function loadSystem(): Promise<SystemData> {
  const h = await settle(api.health());
  const f = await settle(api.systemStatus());
  if (!h.ok && !f.ok) {
    for (const r of [h.reason, f.reason]) {
      if (r instanceof ApiError && r.status === 0) throw r;
    }
    throw new Error(`Neither read worked: health (${why(h.reason)}); status (${why(f.reason)}).`);
  }
  const now = new Date();
  if (h.ok) lastHealth = { value: h.value, at: now };
  if (f.ok) lastStatus = { value: f.value, at: now };
  return {
    health: h.ok
      ? { value: h.value, at: now, error: null }
      : { value: lastHealth?.value ?? null, at: lastHealth?.at ?? null, error: why(h.reason) },
    status: f.ok
      ? { value: f.value, at: now, error: null }
      : { value: lastStatus?.value ?? null, at: lastStatus?.at ?? null, error: why(f.reason) },
  };
}

function ReadFailed({ what, error }: { what: string; error: string | null }) {
  return (
    <Card>
      <p className="text-sm text-caution">
        {what} could not be read{error ? ` (${error})` : ""}. Refresh to try again.
      </p>
    </Card>
  );
}

// A part on screen from an earlier read because this refresh failed.
function OlderRead({ what, part }: { what: string; part: Part<unknown> }) {
  if (!part.error || !part.value || !part.at) return null;
  return (
    <p className="text-xs text-caution" role="status">
      {what}: showing the read from {formatAsOf(part.at.toISOString())}; this refresh failed ({part.error}).
    </p>
  );
}

function Fact({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-4 border-t border-hairline py-2.5 first:border-t-0 first:pt-0">
      <span className="text-sm text-muted">{label}</span>
      <span className={`text-sm ${warn ? "text-caution" : ""}`}>{value}</span>
    </div>
  );
}

export function System() {
  const { data, loading, offline, error, stale, fetchedAt, pending, reload } = useAsync(loadSystem, [], "system");

  if (loading) return <LoadingState label="Checking the Mac" />;
  if (!data) {
    if (offline) return <OfflineState onRetry={reload} />;
    if (error) return <ErrorState message={error} onRetry={reload} />;
    return <OfflineState onRetry={reload} />;
  }

  const health = data.health.value;
  const status = data.status.value;
  const healthFresh = health !== null && !data.health.error;
  const statusFresh = status !== null && !data.status.error;
  // The brief and the warnings come from whichever reply is fresh: the full
  // block from this read, else /api/health's small block from this read (its
  // check line), else the older full block (code review round 2, 1 and 2).
  const fullBrief = statusFresh || (!healthFresh && status) ? status : null;
  const brief = fullBrief ? briefStatusLine(fullBrief.brief) : null;
  const smallBrief = !fullBrief ? health?.brief ?? null : null;
  const rows = status?.watchdog ?? [];
  const alerts = rows.filter((r) => r.tier !== "informational");
  const info = rows.filter((r) => r.tier === "informational");
  // The same Whoop window warnings come with both replies.
  const warnings = (statusFresh ? status?.warnings : healthFresh ? health?.warnings : status?.warnings ?? health?.warnings) ?? [];
  // "Read at" only when everything on screen is from this read (finding 3).
  const allFresh = !data.health.error && !data.status.error;

  return (
    <div className="space-y-6 animate-fade">
      {stale ? <StaleBanner fetchedAt={fetchedAt} reason={error} onRetry={reload} /> : null}
      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-serif text-3xl">System</h1>
          <p className="mt-1 text-sm text-muted">
            Whether the Mac is collecting and writing as it should.
            {fetchedAt && allFresh ? ` Read at ${formatAsOf(fetchedAt.toISOString())}.` : ""}
          </p>
        </div>
        <button
          onClick={reload}
          disabled={pending}
          aria-label="Refresh system status"
          className="shrink-0 rounded-full border border-hairline px-3 py-1.5 text-xs text-text transition-colors hover:bg-hairline/40 disabled:opacity-50"
        >
          {pending ? "Refreshing..." : "Refresh"}
        </button>
      </header>

      <OlderRead what="The brief and the watchdog" part={data.status} />
      <OlderRead what="The Mac's health" part={data.health} />

      <section>
        <SectionTitle>Morning brief</SectionTitle>
        {fullBrief && brief ? (
          <Card>
            <p className={`text-sm ${brief.overdue ? "text-caution" : ""}`}>{brief.text}</p>
            <div className="mt-3">
              {fullBrief.brief.kind ? (
                <Fact label="Written by" value={fullBrief.brief.kind === "model" ? "Local AI" : "Template"} />
              ) : null}
              <Fact label="Due by" value={fullBrief.brief.due} />
              {fullBrief.brief.check ? <Fact label="Check" value={fullBrief.brief.check} warn={brief.overdue} /> : null}
              {fullBrief.brief.morning_step ? <Fact label="Morning step" value={fullBrief.brief.morning_step} /> : null}
            </div>
          </Card>
        ) : smallBrief ? (
          <Card>
            <p className={`text-sm ${smallBrief.overdue ? "text-caution" : ""}`}>{smallBrief.check}</p>
            <div className="mt-3">
              <Fact label="Due by" value={smallBrief.due} />
            </div>
          </Card>
        ) : (
          <ReadFailed what="The brief" error={data.status.error} />
        )}
      </section>

      <section>
        <SectionTitle>Needs attention</SectionTitle>
        {status ? (
          <Card>
            {alerts.length > 0 ? (
              alerts.map((r, i) => <WatchRow key={`${r.metric}-${r.device_key}-${i}`} row={r} />)
            ) : (
              <p className="text-sm text-muted">Nothing needs attention. Every source is delivering.</p>
            )}
          </Card>
        ) : (
          <ReadFailed what="The watchdog" error={data.status.error} />
        )}
      </section>

      {warnings.length > 0 ? (
        <section>
          <SectionTitle>Settings warnings</SectionTitle>
          <Card>
            {warnings.map((w, i) => (
              <p key={i} className="border-t border-hairline py-2.5 text-xs leading-relaxed text-caution first:border-t-0 first:pt-0">
                {w}
              </p>
            ))}
          </Card>
        </section>
      ) : null}

      <section>
        <SectionTitle>The Mac</SectionTitle>
        {health ? (
          <Card>
            <Fact label="Helios running since" value={health.started_at ? formatAsOf(health.started_at) : "unknown"} />
            <Fact label="Local AI (LM Studio)" value={health.llm ? "reachable" : "not reachable"} warn={!health.llm} />
            <Fact label="Whoop" value={health.whoop ? "connected" : "not connected"} warn={!health.whoop} />
            <Fact label="Sign-in required" value={health.auth ? "yes" : "no (rollback mode)"} warn={!health.auth} />
            <Fact label="Samples stored" value={formatNumber(health.samples)} />
          </Card>
        ) : (
          <ReadFailed what="The Mac's health" error={data.health.error} />
        )}
      </section>

      {info.length > 0 ? (
        <section>
          <details>
            <summary className="cursor-pointer text-xs uppercase tracking-[0.18em] text-muted">
              For information ({info.length})
            </summary>
            <div className="mt-3">
              <Card>
                {info.map((r, i) => (
                  <WatchRow key={`${r.metric}-${r.device_key}-${i}`} row={r} />
                ))}
              </Card>
            </div>
          </details>
        </section>
      ) : null}
    </div>
  );
}
