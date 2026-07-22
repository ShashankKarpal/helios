import type { ReactNode } from "react";
import { Card } from "./Card";

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
