import { useEffect, useState } from "react";
import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import type { Signal, ActionItem, FocusItem } from "../types";
import { Card, SectionTitle } from "../components/Card";
import { ProvenanceChip } from "../components/ProvenanceChip";
import { LoadingState, OfflineState } from "../components/states";
import {
  humanizeMetric,
  stateColorVar,
  stateLabel,
  trendArrow,
  formatValue,
  formatDelta,
} from "../lib/format";

function FocusCard({ item }: { item: FocusItem }) {
  const pct =
    item.target > 0
      ? Math.max(0, Math.min(100, (item.current / item.target) * 100))
      : 0;
  return (
    <div className="min-w-[10.5rem] flex-1 rounded-2xl border border-hairline bg-surface p-4">
      <p className="text-sm text-muted">{item.name}</p>
      <p className="mt-1 font-serif text-2xl tnum">
        {formatValue(item.current)}
        <span className="ml-1 text-sm text-muted">/ {formatValue(item.target)} {item.unit}</span>
      </p>
      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-hairline">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${pct}%`, backgroundColor: "var(--mint)" }}
        />
      </div>
    </div>
  );
}

function SignalRow({ signal }: { signal: Signal }) {
  const color = stateColorVar(signal.state);
  const arrow = trendArrow(signal.delta_pct);
  return (
    <div className="border-t border-hairline py-4 first:border-t-0 first:pt-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
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
              {formatValue(signal.value, 1)}
            </span>
            <span className="text-sm text-muted">{signal.unit}</span>
            <span
              className="ml-1 text-lg"
              style={{ color }}
              title={arrow.label}
              aria-label={arrow.label}
            >
              {arrow.glyph}
            </span>
            {signal.delta_pct != null ? (
              <span className="text-xs text-muted tnum">
                {formatDelta(signal.delta_pct)}
              </span>
            ) : null}
          </div>
        </div>
        <span
          className="shrink-0 rounded-full px-2 py-0.5 text-[11px]"
          style={{ color, border: `1px solid ${color}33` }}
        >
          {stateLabel(signal.state)}
        </span>
      </div>
      {signal.why ? (
        <p className="mt-2 text-sm leading-relaxed text-text/80">{signal.why}</p>
      ) : null}
      <div className="mt-3">
        <ProvenanceChip deviceKey={signal.device_key} grade={signal.grade} />
      </div>
    </div>
  );
}

function ActionRow({
  action,
  onAdopt,
  onDismiss,
  busy,
  resolved,
}: {
  action: ActionItem;
  onAdopt: () => void;
  onDismiss: () => void;
  busy: boolean;
  resolved: "adopted" | "dismissed" | null;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-t border-hairline py-3 first:border-t-0 first:pt-0">
      <div className="min-w-0">
        {action.category ? (
          <p className="text-[11px] uppercase tracking-wide text-muted">
            {action.category}
          </p>
        ) : null}
        <p className="text-sm leading-relaxed">{action.text}</p>
      </div>
      {resolved ? (
        <span
          className="shrink-0 text-xs"
          style={{
            color: resolved === "adopted" ? "var(--mint)" : "var(--muted)",
          }}
        >
          {resolved === "adopted" ? "Adopted" : "Dismissed"}
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

/// "Phone data as of" label: "today 20:20" for same-day, "Jul 23, 20:20"
/// otherwise. The server timestamp is naive local time with microseconds
/// ("2026-07-24 20:20:35.891754"), which Date() cannot parse as-is.
function asOfLabel(ts: string): string {
  const d = new Date(ts.replace(" ", "T").replace(/\.\d+$/, ""));
  if (isNaN(d.getTime())) return ts;
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return `today ${hm}`;
  const md = d.toLocaleDateString([], { month: "short", day: "numeric" });
  return `${md}, ${hm}`;
}

export function Today() {
  const { data, loading, offline, reload } = useAsync(() => api.today(), [], "today");
  const [pulling, setPulling] = useState(false);

  // While the local model writes a richer narrative in the background, poll so
  // it swaps in without a manual refresh. Stops as soon as it is ready.
  useEffect(() => {
    if (data?.narrative_status !== "generating") return;
    const t = setTimeout(reload, 5000);
    return () => clearTimeout(t);
  }, [data, reload]);
  const [actionState, setActionState] = useState<
    Record<string, "adopted" | "dismissed">
  >({});
  const [busyAction, setBusyAction] = useState<string | null>(null);

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
    if (!action.action_id) return;
    setBusyAction(action.action_id);
    try {
      await api.setActionStatus(action.action_id, status);
      setActionState((prev) => ({ ...prev, [action.action_id as string]: status }));
    } catch {
      // ignore: leave as unresolved so the user can retry.
    } finally {
      setBusyAction(null);
    }
  }

  if (loading) {
    const h = new Date().getHours();
    const part = h < 12 ? "morning" : h < 17 ? "afternoon" : "evening";
    return <LoadingState label={`Reading your ${part}`} />;
  }
  if (offline) return <OfflineState onRetry={reload} />;
  if (!data) return <OfflineState onRetry={reload} />;

  return (
    <div className="space-y-6 animate-fade">
      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="font-serif text-3xl leading-tight">{data.greeting}</h1>
          {data.verdict ? (
            <p className="mt-2 font-serif text-lg text-text/85">{data.verdict}</p>
          ) : null}
          {data.as_of ? (
            <p className="mt-1.5 text-xs text-muted">
              Phone data as of {asOfLabel(data.as_of)}
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

      {data.context_flags && data.context_flags.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {data.context_flags.map((flag, i) => (
            <span
              key={i}
              className="rounded-full border border-hairline bg-surface px-2.5 py-1 text-[11px] text-muted"
            >
              {flag}
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
                  <SignalRow key={`${s.metric}-${i}`} signal={s} />
                ))}
              </div>
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
                <span className="inline-flex items-center gap-1.5 rounded-full border border-hairline px-2 py-0.5 text-[11px] text-mint">
                  <span
                    className="inline-block h-1.5 w-1.5 rounded-full"
                    style={{ backgroundColor: "var(--mint)" }}
                  />
                  local AI
                </span>
              ) : null}
              {data.narrative_status === "generating" ? (
                <span className="inline-flex items-center gap-1.5 text-[11px] text-muted">
                  <span
                    className="h-3 w-3 animate-spin rounded-full border border-hairline"
                    style={{ borderTopColor: "var(--mint)" }}
                  />
                  Writing a richer brief...
                </span>
              ) : data.model ? (
                <span className="text-[11px] text-muted">{data.model}</span>
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
                  resolved={a.action_id ? actionState[a.action_id] ?? null : null}
                  onAdopt={() => resolveAction(a, "adopted")}
                  onDismiss={() => resolveAction(a, "dismissed")}
                />
              );
            })}
          </Card>
        </section>
      ) : null}
    </div>
  );
}
