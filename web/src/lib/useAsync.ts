import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  offline: boolean;
  error: string | null;
  // True when data is on screen but the last load failed (offline or error):
  // the screen must say the data is from fetchedAt and the refresh failed.
  stale: boolean;
  // When the data on screen was fetched (null until the first success).
  fetchedAt: Date | null;
  reload: () => void;
}

interface CacheEntry {
  data: unknown;
  at: Date;
}

// Module-level cache that survives component unmounts. Screens are conditionally
// rendered (App swaps the active tab), so without this every tab switch would
// refetch from zero and flash a full-screen spinner. With a cacheKey, a remount
// shows the last data instantly and revalidates quietly in the background.
const cache = new Map<string, CacheEntry>();

// Runs an async loader on mount and exposes a manual reload. Distinguishes a
// network-offline failure (ApiError status 0) from other errors so screens can
// show a friendly offline state. Pass a cacheKey to make tab switches instant.
// A failed revalidation never silently keeps old data looking current: error
// and offline are set beside the cached data, and stale says so.
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  cacheKey?: string
): AsyncState<T> {
  const cached = cacheKey ? cache.get(cacheKey) : undefined;
  const [data, setData] = useState<T | null>((cached?.data as T | undefined) ?? null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(cached?.at ?? null);
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
        const at = new Date();
        setData(result);
        setFetchedAt(at);
        hasData.current = true;
        if (cacheKey) cache.set(cacheKey, { data: result, at });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 0) {
          setOffline(true);
          setError("Helios is offline.");
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

  const stale = data != null && (offline || error != null);
  return { data, loading, offline, error, stale, fetchedAt, reload };
}
