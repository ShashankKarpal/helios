import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  offline: boolean;
  error: string | null;
  reload: () => void;
}

// Module-level cache that survives component unmounts. Screens are conditionally
// rendered (App swaps the active tab), so without this every tab switch would
// refetch from zero and flash a full-screen spinner. With a cacheKey, a remount
// shows the last data instantly and revalidates quietly in the background.
const cache = new Map<string, unknown>();

// Runs an async loader on mount and exposes a manual reload. Distinguishes a
// network-offline failure (ApiError status 0) from other errors so screens can
// show a friendly offline state. Pass a cacheKey to make tab switches instant.
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  cacheKey?: string
): AsyncState<T> {
  const cached = cacheKey ? (cache.get(cacheKey) as T | undefined) : undefined;
  const [data, setData] = useState<T | null>(cached ?? null);
  const [loading, setLoading] = useState(cached === undefined);
  const [offline, setOffline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const hasData = useRef(cached !== undefined);

  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    // Only show the blocking spinner when there is nothing to display yet.
    // A background revalidation of already-cached data must not blank the screen.
    if (!hasData.current) setLoading(true);
    setOffline(false);
    setError(null);
    loader()
      .then((result) => {
        if (cancelled) return;
        setData(result);
        hasData.current = true;
        if (cacheKey) cache.set(cacheKey, result);
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
