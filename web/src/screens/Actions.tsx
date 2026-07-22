import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { SectionTitle } from "../components/Card";
import { LoadingState, OfflineState, EmptyState } from "../components/states";
import { formatDate } from "../lib/format";
import type { ActionHistoryItem, ActionStatus } from "../types";

function statusStyle(status: ActionStatus): { label: string; color: string } {
  switch (status) {
    case "adopted":
      return { label: "Adopted", color: "var(--mint)" };
    case "done":
      return { label: "Done", color: "var(--mint)" };
    case "dismissed":
      return { label: "Dismissed", color: "var(--muted)" };
    default:
      return { label: "Suggested", color: "var(--caution)" };
  }
}

function StatusPill({ status }: { status: ActionStatus }) {
  const s = statusStyle(status);
  return (
    <span
      className="shrink-0 rounded-full px-2.5 py-0.5 text-[11px]"
      style={{ color: s.color, border: `1px solid ${s.color}33` }}
    >
      {s.label}
    </span>
  );
}

export function Actions() {
  const { data, loading, offline, reload } = useAsync(() => api.actions(7));

  if (loading) return <LoadingState label="Gathering your actions" />;
  if (offline) return <OfflineState onRetry={reload} />;

  const actions = data?.actions ?? [];

  // Group by date, most recent first.
  const groups = new Map<string, ActionHistoryItem[]>();
  for (const a of actions) {
    const list = groups.get(a.date) ?? [];
    list.push(a);
    groups.set(a.date, list);
  }
  const orderedDates = [...groups.keys()].sort((a, b) => b.localeCompare(a));

  return (
    <div className="space-y-6 animate-fade">
      <header>
        <h1 className="font-serif text-3xl">Actions</h1>
        <p className="mt-1 text-sm text-muted">
          What Helios suggested and what you did with it.
        </p>
      </header>

      {actions.length === 0 ? (
        <EmptyState
          title="No actions yet"
          body="Suggested actions from your daily read will collect here."
        />
      ) : (
        orderedDates.map((date) => (
          <section key={date}>
            <SectionTitle>{formatDate(date)}</SectionTitle>
            <div className="rounded-2xl border border-hairline bg-surface">
              {groups.get(date)!.map((a) => (
                <div
                  key={a.action_id}
                  className="flex items-start justify-between gap-4 border-t border-hairline p-4 first:border-t-0"
                >
                  <div className="min-w-0">
                    {a.category ? (
                      <p className="text-[11px] uppercase tracking-wide text-muted">
                        {a.category}
                      </p>
                    ) : null}
                    <p className="text-sm leading-relaxed">{a.text}</p>
                    {a.created_by ? (
                      <p className="mt-1 text-[11px] text-muted">
                        via {a.created_by}
                      </p>
                    ) : null}
                  </div>
                  <StatusPill status={a.status} />
                </div>
              ))}
            </div>
          </section>
        ))
      )}
    </div>
  );
}
