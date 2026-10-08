import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAsync } from "../lib/useAsync";
import type { Signal, ActionItem, FocusItem, ActionStatus, WhoopPullResult } from "../types";
import { Card, SectionTitle } from "../components/Card";
import { ProvenanceChip } from "../components/ProvenanceChip";
import { LoadingState, OfflineState, ErrorState, StaleBanner } from "../components/states";
import {
  ExtraTrendRows,
  Sparkline,
  useTrends,
  windowEnding,
  type TrendData,
} from "../components/Trends";
import {
  humanizeMetric,
  stateColorVar,
  stateLabel,
  trendArrow,
  formatValue,
  formatDelta,
  formatAsOf,
  setReportingZone,
  isUncompared,
  awaitingLabel,
  formatMetricValue,
  flagLabel,
} from "../lib/format";

function FocusCard({ item }: { item: FocusItem }) {
  // No value yet is "--", never a fake 0 against the target.
  const missing = item.current == null || Number.isNaN(item.current);
  const pct =
    !missing && item.target > 0
      ? Math.max(0, Math.min(100, ((item.current as number) / item.target) * 100))
      : 0;
  return (
    <div className="min-w-[10.5rem] flex-1 rounded-2xl border border-hairline bg-surface p-4">
      <p className="text-sm text-muted">{item.name}</p>
      <p className="mt-1 font-serif text-2xl tnum">
        {missing ? "--" : formatValue(item.current)}
        <span className="ml-1 text-sm text-muted">/ {formatValue(item.target)} {item.unit}</span>
      </p>
      {missing ? <p className="mt-1 text-xs text-muted">no {item.unit} yet today</p> : null}
      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-hairline">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${pct}%`, backgroundColor: "var(--mint)" }}
        />
      </div>
    </div>
  );
}

function SignalRow({ signal, trend }: { signal: Signal; trend?: TrendData }) {
  const color = stateColorVar(signal.state);
  // A running total (today, so far) or a non-owner value carries no
  // comparison: no arrow, no delta, and no grade for the partial day.
  const uncompared = isUncompared(signal);
  const partial = signal.state === "in_progress" || signal.partial === true;
  const fallback = signal.state === "fallback" || signal.fallback === true;
  const arrow = uncompared ? { glyph: "", label: "no comparison" } : trendArrow(signal.delta_pct);
  const shown = formatMetricValue(signal.metric, signal.value, signal.unit, 1);
  // The sparkline covers the 7 days ending on this row's own day, so a
  // running total for today sits beside today's trend, not yesterday's.
  const spark = trend ? (signal.date ? windowEnding(trend, signal.date.slice(0, 10)) : trend.values) : null;
  return (
    <div className="border-t border-hairline py-4 first:border-t-0 first:pt-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className="inline-block h-2 w-2 rounded-full"
              style={{ backgroundColor: color }}
            />
            <span className="text-sm text-muted">
              {humanizeMetric(signal.metric)}
            </span>
          </div>
          <div className="mt-1.5 flex items-baseline gap-2">
            <span className="font-serif text-3xl tnum" style={{ color }}>
              {shown.text}
            </span>
            {shown.unit ? <span className="text-sm text-muted">{shown.unit}</span> : null}
            {partial ? <span className="text-xs text-muted">so far</span> : null}
            {arrow.glyph ? (
              <span
                className="ml-1 text-lg"
                style={{ color }}
                title={arrow.label}
                aria-label={arrow.label}
              >
                {arrow.glyph}
              </span>
            ) : null}
            {!uncompared && signal.delta_pct != null ? (
              <span className="text-xs text-muted tnum">
                {formatDelta(signal.delta_pct)}
              </span>
            ) : null}
            {spark ? (
              <div className="min-w-0 flex-1 self-center pl-3">
                <Sparkline values={spark} />
              </div>
            ) : null}
          </div>
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-xs"
          style={{ color, border: `1px solid ${color}33` }}
        >
          {stateLabel(signal.state)}
        </span>
      </div>
      {signal.why ? (
        <p className="mt-2 text-sm leading-relaxed text-text/80">{signal.why}</p>
      ) : null}
      <div className="mt-3">
        <ProvenanceChip
          deviceKey={signal.device_key}
          grade={partial ? null : signal.grade}
          fallback={fallback}
          partial={partial}
        />
      </div>
    </div>
  );
}

// Ask the Mac for the latest Whoop records and say what happened in words.
async function pullWhoop(): Promise<string> {
  let res: WhoopPullResult;
  try {
    res = await api.whoopPull(3);
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 0) return "Whoop not asked: Helios is offline.";
      if (err.status === 503 || err.status === 400 || err.status === 404) return "Whoop is not connected on the Mac.";
      return `Whoop pull failed: ${err.message}`;
    }
    return "Whoop pull failed.";
  }
  if (res.skipped === "rate_limited") {
    const s = typeof res.retry_after_s === "number" ? Math.max(1, Math.round(res.retry_after_s)) : null;
    // The busy reply carries the last pull's outcome: a failure stays a failure.
    if (res.last && res.last.ok === false) {
      const why = res.last.error ?? "unknown error";
      return s ? `Whoop pull failed a moment ago (${why}); next pull allowed in ${s} s.` : `Whoop pull failed a moment ago (${why}).`;
    }
    return s ? `Whoop was asked a moment ago; next pull allowed in ${s} s.` : "Whoop was asked a moment ago.";
  }
  if (res.skipped) return `Whoop pull skipped (${String(res.skipped).replace(/_/g, " ")}).`;
  if (res.ok === false || res.error) return `Whoop pull failed: ${res.error ?? "unknown error"}`;
  const counts = Object.entries(res)
    .filter(([k, v]) => typeof v === "number" && k !== "retry_after_s")
    .map(([k, v]) => `${v} ${k.replace(/_/g, " ")}`);
  return counts.length ? `Whoop asked: ${counts.join(", ")}.` : "Whoop asked; recomputing.";
}

// A resolved action is one the owner has already answered. The stored status
// comes with /api/today; the optimistic in-memory update covers the moment
// between the tap and the next reload.
type Resolution = "adopted" | "dismissed" | "done";

function storedResolution(status?: ActionStatus): Resolution | null {
  if (status === "adopted" || status === "dismissed" || status === "done") return status;
  return null;
}

function ActionRow({
  action,
  onAdopt,
  onDismiss,
  busy,
  resolved,
  error,
}: {
  action: ActionItem;
  onAdopt: () => void;
  onDismiss: () => void;
  busy: boolean;
  resolved: Resolution | null;
  error?: string | null;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-t border-hairline py-3 first:border-t-0 first:pt-0">
      <div className="min-w-0">
        {action.category ? (
          <p className="text-xs uppercase tracking-wide text-muted">
            {action.category}
          </p>
        ) : null}
        <p className="text-sm leading-relaxed">{action.text}</p>
        {error ? (
          <p className="mt-1 text-xs" style={{ color: "var(--alert)" }} role="alert">
            {error}
          </p>
        ) : null}
      </div>
      {resolved ? (
        <span
          className="shrink-0 text-xs"
          style={{
            color: resolved === "dismissed" ? "var(--muted)" : "var(--mint)",
          }}
        >
          {resolved === "adopted" ? "Adopted" : resolved === "done" ? "Done" : "Dismissed"}
        </span>
      ) : (
        <div className="flex shrink-0 items-center gap-2">
          <button
            disabled={busy || !action.action_id}
            onClick={onAdopt}
            className="rounded-full px-3 py-1 text-xs font-medium transition-colors disabled:opacity-40"
            style={{ color: "var(--mint)", border: "1px solid var(--mint)" }}
          >
            Adopt
          </button>
          <button
            disabled={busy || !action.action_id}
            onClick={onDismiss}
            className="rounded-full border border-hairline px-3 py-1 text-xs text-muted transition-colors hover:bg-hairline/40 disabled:opacity-40"
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}

// One-tap capture. Deterministic proposals straight to confirm: no model
// call, so a tap lands in well under a second even mid-backfill. Kinds feed
// the events table and, from there, the cutoff finder and correlations.
const CAPTURE_CHIPS = [
  { label: "Coffee", kind: "caffeine", item: "coffee" },
  { label: "Drink", kind: "alcohol", item: "drink" },
  { label: "Med", kind: "med", item: "medication" },
] as const;

function CaptureChips() {
  const [busy, setBusy] = useState<string | null>(null);
  const [logged, setLogged] = useState<string | null>(null);

  async function tap(chip: (typeof CAPTURE_CHIPS)[number]) {
    setBusy(chip.label);
    try {
      await api.quicklogConfirm(
        { kind: chip.kind, item: chip.item, amount: "", minutes_ago: 0 },
        "chip"
      );
      setLogged(chip.label);
      window.setTimeout(
        () => setLogged((l) => (l === chip.label ? null : l)),
        2000
      );
    } catch {
      // ignore: the chip stays tappable for a retry.
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs uppercase tracking-wide text-muted">
        Quick log
      </span>
      {CAPTURE_CHIPS.map((chip) => (
        <button
          key={chip.label}
          disabled={busy === chip.label}
          onClick={() => tap(chip)}
          className="rounded-full border border-hairline bg-surface px-3 py-1 text-xs text-text transition-colors hover:bg-hairline/40 disabled:opacity-50"
          style={
            logged === chip.label
              ? { color: "var(--mint)", borderColor: "var(--mint)" }
              : undefined
          }
        >
          {logged === chip.label ? "Logged ✓" : chip.label}
        </button>
      ))}
    </div>
  );
}

export function Today() {
  const { data, loading, offline, error, stale, fetchedAt, reload } = useAsync(() => api.today(), [], "today");
  // Every date on this screen is a reporting-zone calendar day (the server's
  // "date" and "zone"), never the browser's.
  setReportingZone(data?.zone);
  const { trends } = useTrends(data?.date);
  const [pulling, setPulling] = useState(false);
  // Plain-words result of the last Pull latest (the Whoop part), shown briefly.
  const [pullNote, setPullNote] = useState<string | null>(null);
  useEffect(() => {
    if (!pullNote) return;
    const t = setTimeout(() => setPullNote(null), 12000);
    return () => clearTimeout(t);
  }, [pullNote]);

  // While the local model writes a richer narrative in the background, poll so
  // it swaps in without a manual refresh. Stops as soon as it is ready.
  useEffect(() => {
    if (data?.narrative_status !== "generating") return;
    const t = setTimeout(reload, 5000);
    return () => clearTimeout(t);
  }, [data, reload]);
  // Optimistic overlay only; the stored status on each action is the truth
  // on load (K1: this used to be the only source, so a reload or another
  // device showed every action as unresolved).
  const [actionState, setActionState] = useState<
    Record<string, "adopted" | "dismissed">
  >({});
  const [busyAction, setBusyAction] = useState<string | null>(null);
  // The last Adopt or Dismiss that could not be saved, shown under its row
  // (or under the list when a reload removed the row).
  const [actionError, setActionError] = useState<{ id: string; text: string } | null>(null);

  async function pullLatest() {
    setPulling(true);
    // Nudge the native bridge to sync, but only on iOS, where the custom
    // scheme has a handler. On the desktop it navigates to Safari's "address
    // is invalid" page and aborts this function, so guard it and use a hidden
    // iframe (non-blocking) instead of navigating the whole page.
    if (/iPhone|iPad|iPod/.test(navigator.userAgent)) {
      try {
        const frame = document.createElement("iframe");
        frame.style.display = "none";
        frame.src = "helios-bridge://sync";
        document.body.appendChild(frame);
        window.setTimeout(() => frame.remove(), 1000);
      } catch {
        // ignore: bridge may not be installed.
      }
    }
    // Whoop first (T13): the button used to run only the recompute, so during
    // the morning gap nothing on this screen could fetch last night's record.
    setPullNote(await pullWhoop());
    try {
      await api.recompute(7);
    } catch {
      // ignore: still refetch below.
    }
    reload();
    setPulling(false);
  }

  async function resolveAction(
    action: ActionItem,
    status: "adopted" | "dismissed"
  ) {
    const id = action.action_id;
    if (!id) return;
    setBusyAction(id);
    setActionError(null);
    try {
      try {
        await api.setActionStatus(id, status);
      } catch (err) {
        // 404: the stored row changed since this screen rendered. Reading
        // Today re-files the day's actions under the same stable ids, so
        // reload it and retry once with the same id.
        if (!(err instanceof ApiError && err.status === 404)) throw err;
        await api.today();
        reload();
        await api.setActionStatus(id, status);
      }
      setActionState((prev) => ({ ...prev, [id]: status }));
    } catch (err) {
      // Never swallowed: the buttons stay so the owner can try again.
      const text =
        err instanceof ApiError && err.status === 404
          ? "Not saved: this suggestion is no longer current."
          : err instanceof ApiError && err.status === 0
            ? "Not saved: Helios is offline."
            : "Not saved. Try again.";
      setActionError({ id, text });
    } finally {
      setBusyAction(null);
    }
  }

  if (loading) {
    const h = new Date().getHours();
    const part = h < 12 ? "morning" : h < 17 ? "afternoon" : "evening";
    return <LoadingState label={`Reading your ${part}`} />;
  }
  if (!data) {
    if (offline) return <OfflineState onRetry={reload} />;
    if (error) return <ErrorState message={error} onRetry={reload} />;
    return <OfflineState onRetry={reload} />;
  }

  return (
    <div className="space-y-6 animate-fade">
      {stale ? <StaleBanner fetchedAt={fetchedAt} reason={error} onRetry={reload} /> : null}
      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-serif text-3xl leading-tight">{data.greeting}</h1>
          {data.verdict ? (
            <p className="mt-2 font-serif text-lg text-text/85">{data.verdict}</p>
          ) : null}
          {data.as_of ? (
            <p className="mt-1.5 text-xs text-muted">
              Phone data as of {formatAsOf(data.as_of, data.zone)}
            </p>
          ) : null}
          {pullNote ? (
            <p className="mt-1 text-xs text-muted" role="status">
              {pullNote}
            </p>
          ) : null}
        </div>
        <button
          onClick={pullLatest}
          disabled={pulling}
          className="shrink-0 rounded-full border border-hairline px-3 py-1.5 text-xs text-text transition-colors hover:bg-hairline/40 disabled:opacity-50"
        >
          {pulling ? "Pulling..." : "Pull latest"}
        </button>
      </header>

      <CaptureChips />

      {data.awaiting && data.awaiting.length > 0 ? (
        <section>
          <Card>
            <p className="font-serif text-lg">Waiting for Whoop</p>
            <p className="mt-1.5 text-sm leading-relaxed text-muted">
              Last night's {awaitingLabel(data.awaiting)} {data.awaiting.length === 1 ? "is" : "are"} not in yet.
              The verdict and narrative hold until the night is scored; Pull latest asks Whoop again.
            </p>
          </Card>
        </section>
      ) : null}

      {data.context_flags && data.context_flags.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {data.context_flags.map((flag, i) => (
            <span
              key={i}
              className="rounded-full border border-hairline bg-surface px-2.5 py-1 text-xs text-muted"
              title={flag}
            >
              {flagLabel(flag)}
            </span>
          ))}
        </div>
      ) : null}

      {data.focus && data.focus.length > 0 ? (
        <section>
          <SectionTitle>Today's focus</SectionTitle>
          <div className="flex flex-wrap gap-3">
            {data.focus.map((f, i) => (
              <FocusCard key={i} item={f} />
            ))}
          </div>
        </section>
      ) : null}

      <section>
        <SectionTitle>Recovery signals</SectionTitle>
        <Card>
          {data.signals && data.signals.length > 0 ? (
            <>
              <div>
                {data.signals.map((s, i) => (
                  <SignalRow key={`${s.metric}-${i}`} signal={s} trend={trends[s.metric]} />
                ))}
              </div>
              <ExtraTrendRows
                trends={trends}
                exclude={data.signals.map((s) => s.metric)}
              />
              <p className="mt-4 border-t border-hairline pt-3 text-xs leading-relaxed text-muted">
                Each marker is compared to your own baseline. No composite score.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted">
              No signals yet. Pull latest once your devices have synced.
            </p>
          )}
        </Card>
      </section>

      {data.narrative ? (
        <section>
          <SectionTitle>Narrative</SectionTitle>
          <Card>
            <div className="mb-3 flex items-center gap-2">
              {data.validated ? (
                <span className="inline-flex items-center gap-1.5 rounded-full border border-hairline px-2 py-0.5 text-xs text-mint">
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: "var(--mint)" }}
                  />
                  local AI
                </span>
              ) : null}
              {data.narrative_status === "generating" ? (
                <span className="inline-flex items-center gap-1.5 text-xs text-muted">
                  <span
                    className="h-3 w-3 animate-spin rounded-full border border-hairline"
                    style={{ borderTopColor: "var(--mint)" }}
                  />
                  Writing a richer brief...
                </span>
              ) : data.model ? (
                <span className="text-xs text-muted">{data.model}</span>
              ) : null}
            </div>
            <p className="text-[15px] leading-relaxed text-text/90">
              {data.narrative}
            </p>
          </Card>
        </section>
      ) : null}

      {data.actions && data.actions.length > 0 ? (
        <section>
          <SectionTitle>Suggested actions</SectionTitle>
          <Card>
            {data.actions.map((a, i) => {
              const id = a.action_id ?? `idx-${i}`;
              return (
                <ActionRow
                  key={id}
                  action={a}
                  busy={busyAction === a.action_id}
                  resolved={
                    (a.action_id ? actionState[a.action_id] : undefined) ??
                    storedResolution(a.status)
                  }
                  onAdopt={() => resolveAction(a, "adopted")}
                  onDismiss={() => resolveAction(a, "dismissed")}
                  error={actionError && actionError.id === a.action_id ? actionError.text : null}
                />
              );
            })}
            {actionError && !data.actions.some((a) => a.action_id === actionError.id) ? (
              <p className="mt-2 text-xs" style={{ color: "var(--alert)" }} role="alert">
                {actionError.text}
              </p>
            ) : null}
          </Card>
        </section>
      ) : null}
    </div>
  );
}
