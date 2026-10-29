import { api, ApiError } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { ErrorState, LoadingState, OfflineState, StaleBanner } from "../components/states";
import { briefStatusLine, formatAsOf, humanizeDevice, humanizeMetric } from "../lib/format";
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
  const age = typeof row.age_hours === "number" ? `${Math.round(row.age_hours)} h since the last data` : null;
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

interface SystemData {
  health: HealthResponse | null;
  healthError: string | null;
  status: ToolFreshness | null;
  statusError: string | null;
}

function reason(r: PromiseSettledResult<unknown>): string | null {
  if (r.status === "fulfilled") return null;
  return r.reason instanceof Error ? r.reason.message : "Something went wrong.";
}

// Both reads at once. Both failing is a failure of the whole screen (offline,
// or the first error); one failing leaves the other on screen with a note.
async function loadSystem(): Promise<SystemData> {
  const [h, f] = await Promise.allSettled([api.health(), api.systemStatus()]);
  if (h.status === "rejected" && f.status === "rejected") {
    throw h.reason instanceof ApiError && h.reason.status === 0 ? h.reason : f.reason;
  }
  return {
    health: h.status === "fulfilled" ? h.value : null,
    healthError: reason(h),
    status: f.status === "fulfilled" ? f.value : null,
    statusError: reason(f),
  };
}

function ReadFailed({ what, why }: { what: string; why: string | null }) {
  return (
    <Card>
      <p className="text-sm text-caution">
        {what} could not be read{why ? ` (${why.replace(/\.$/, "")})` : ""}. Refresh to try again.
      </p>
    </Card>
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

  const { health, status } = data;
  const brief = status ? briefStatusLine(status.brief) : null;
  const rows = status?.watchdog ?? [];
  const alerts = rows.filter((r) => r.tier !== "informational");
  const info = rows.filter((r) => r.tier === "informational");
  const warnings = status?.warnings ?? [];

  return (
    <div className="space-y-6 animate-fade">
      {stale ? <StaleBanner fetchedAt={fetchedAt} reason={error} onRetry={reload} /> : null}
      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-serif text-3xl">System</h1>
          <p className="mt-1 text-sm text-muted">
            Whether the Mac is collecting and writing as it should.
            {fetchedAt ? ` Read at ${formatAsOf(fetchedAt.toISOString())}.` : ""}
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

      <section>
        <SectionTitle>Morning brief</SectionTitle>
        {status && brief ? (
          <Card>
            <p className={`text-sm ${brief.overdue ? "text-caution" : ""}`}>{brief.text}</p>
            <div className="mt-3">
              {status.brief.kind ? (
                <Fact label="Written by" value={status.brief.kind === "model" ? "Local AI" : "Template"} />
              ) : null}
              <Fact label="Due by" value={status.brief.due} />
              {status.brief.check ? <Fact label="Check" value={status.brief.check} warn={brief.overdue} /> : null}
              {status.brief.morning_step ? <Fact label="Morning step" value={status.brief.morning_step} /> : null}
            </div>
          </Card>
        ) : (
          <ReadFailed what="The brief and the watchdog" why={data.statusError} />
        )}
      </section>

      {status ? (
        <section>
          <SectionTitle>Needs attention</SectionTitle>
          <Card>
            {alerts.length > 0 ? (
              alerts.map((r, i) => <WatchRow key={`${r.metric}-${r.device_key}-${i}`} row={r} />)
            ) : (
              <p className="text-sm text-muted">Nothing needs attention. Every source is delivering.</p>
            )}
          </Card>
        </section>
      ) : null}

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
            <Fact label="Samples stored" value={health.samples.toLocaleString()} />
          </Card>
        ) : (
          <ReadFailed what="The Mac's health" why={data.healthError} />
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
