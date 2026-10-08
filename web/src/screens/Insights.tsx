import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import { Card, SectionTitle } from "../components/Card";
import { LoadingState, OfflineState, EmptyState, ErrorState, StaleBanner } from "../components/states";
import type { Insight } from "../types";

function verdictColor(verdict: string): string {
  const v = verdict.toLowerCase();
  if (v.includes("good") || v.includes("positive") || v.includes("improv"))
    return "var(--mint)";
  if (v.includes("watch") || v.includes("caution") || v.includes("mixed"))
    return "var(--caution)";
  if (v.includes("concern") || v.includes("declin") || v.includes("risk"))
    return "var(--alert)";
  return "var(--text)";
}

function InsightCard({ insight }: { insight: Insight }) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-3">
        <h3 className="font-serif text-lg leading-snug">{insight.title}</h3>
        {insight.verdict ? (
          <span
            className="shrink-0 rounded-full px-2 py-0.5 text-xs"
            style={{
              color: verdictColor(insight.verdict),
              border: `1px solid ${verdictColor(insight.verdict)}33`,
            }}
          >
            {insight.verdict}
          </span>
        ) : null}
      </div>
      {insight.detail ? (
        <p className="mt-2 text-sm leading-relaxed text-text/85">
          {insight.detail}
        </p>
      ) : null}
      {insight.method ? (
        <span className="mt-3 inline-block rounded-full border border-hairline bg-bg/60 px-2.5 py-1 text-xs text-muted">
          {insight.method}
        </span>
      ) : null}
    </Card>
  );
}

export function Insights() {
  const { data, loading, offline, error, stale, fetchedAt, reload } = useAsync(() => api.insights(90), [], "insights");

  if (loading) return <LoadingState label="Looking for patterns" />;
  // An error with nothing cached is an error, never "needs a little more history".
  if (!data) {
    if (offline) return <OfflineState onRetry={reload} />;
    if (error) return <ErrorState message={error} onRetry={reload} />;
  }
  // The server reports a failed computation as an error field (A15) instead
  // of an empty list; show it as such.
  const serverError = (data as { error?: string } | null)?.error ?? null;

  const insights = data?.insights ?? [];

  return (
    <div className="space-y-6 animate-fade">
      {stale ? <StaleBanner fetchedAt={fetchedAt} reason={error} onRetry={reload} /> : null}
      <header>
        <h1 className="font-serif text-3xl">Insights</h1>
        <p className="mt-1 text-sm text-muted">
          Patterns found across the last 90 days.
        </p>
      </header>

      {serverError ? (
        <ErrorState message={serverError} onRetry={reload} />
      ) : insights.length > 0 ? (
        <section className="space-y-3">
          <SectionTitle>What stands out</SectionTitle>
          {insights.map((ins, i) => (
            <InsightCard key={i} insight={ins} />
          ))}
        </section>
      ) : (
        <EmptyState
          title="No insights yet"
          body="Helios needs a little more history before it can surface reliable patterns. Check back after a few more days of data."
        />
      )}
    </div>
  );
}
