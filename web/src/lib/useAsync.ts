import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api";

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  offline: boolean;
  error: string | null;
  reload: () => void;
}

// Runs an async loader on mount and exposes a manual reload. Distinguishes a
// network-offline failure (ApiError status 0) from other errors so screens can
// show a friendly offline state.
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setOffline(false);
    setError(null);
    loader()
      .then((result) => {
        if (cancelled) return;
        setData(result);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 0) {
          setOffline(true);
        } else {
          setError(err instanceof Error ? err.message : "Something went wrong.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  return { data, loading, offline, error, reload };
}
