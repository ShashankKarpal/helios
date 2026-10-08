import type { ReactNode } from "react";
import { Card } from "./Card";
import { reportingZone } from "../lib/format";

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-muted">
      <div
        className="h-6 w-6 animate-spin rounded-full border-2 border-hairline"
        style={{ borderTopColor: "var(--mint)" }}
      />
      <p className="mt-4 text-sm">{label}...</p>
    </div>
  );
}

export function OfflineState({ onRetry }: { onRetry?: () => void }) {
  return (
    <Card className="text-center">
      <p className="font-serif text-xl">Helios is resting.</p>
      <p className="mt-2 text-sm text-muted">
        The local service is not reachable right now. Your data stays on your
        machine, so nothing is lost. Try again once heliosd is running.
      </p>
      {onRetry ? (
        <button
          onClick={onRetry}
          className="mt-4 rounded-full border border-hairline px-4 py-2 text-sm text-text transition-colors hover:bg-hairline/40"
        >
          Try again
        </button>
      ) : null}
    </Card>
  );
}

export function EmptyState({
  title,
  body,
}: {
  title: string;
  body?: ReactNode;
}) {
  return (
    <Card className="text-center">
      <p className="font-serif text-lg">{title}</p>
      {body ? <p className="mt-2 text-sm text-muted">{body}</p> : null}
    </Card>
  );
}

// A request failed and nothing is cached: say what failed, never a look-alike
// of an empty screen. The message comes from the API client (a 401 says the
// token was rejected and to reload; a 500 says the server failed).
export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card className="text-center">
      <p className="font-serif text-xl" role="alert">Helios could not answer.</p>
      <p className="mt-2 text-sm text-muted">{message}</p>
      {onRetry ? (
        <button
          onClick={onRetry}
          className="mt-4 rounded-full border border-hairline px-4 py-2 text-sm text-text transition-colors hover:bg-hairline/40"
        >
          Try again
        </button>
      ) : null}
    </Card>
  );
}

function clock(d: Date): string {
  try {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: reportingZone() });
  } catch {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  }
}

// Cached data is on screen and the refresh failed: say so, with the time the
// data was fetched, instead of letting old numbers read as current.
export function StaleBanner({
  fetchedAt,
  reason,
  onRetry,
}: {
  fetchedAt: Date | null;
  reason: string | null;
  onRetry?: () => void;
}) {
  const when = fetchedAt ? `from ${clock(fetchedAt)}` : "from an earlier load";
  return (
    <div
      role="status"
      className="flex items-center justify-between gap-3 rounded-2xl border px-4 py-2.5 text-xs"
      style={{ borderColor: "var(--caution)", color: "var(--caution)" }}
    >
      <span>
        Showing data {when}; the refresh failed{reason ? ` (${reason.replace(/\.$/, "")})` : ""}.
      </span>
      {onRetry ? (
        <button onClick={onRetry} className="shrink-0 underline underline-offset-2">
          Retry
        </button>
      ) : null}
    </div>
  );
}
