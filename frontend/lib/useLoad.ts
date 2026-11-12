"use client";

import { useCallback, useEffect, useState } from "react";

// The same discriminated-union idea as HealthStatus, made reusable: a page is loading, has
// data, or has an error, and TypeScript knows which fields exist in each case.
export type LoadState<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "error"; error: unknown };

/** Run `load` on mount (and whenever it changes). `reload()` fetches again and keeps showing
 * the current data meanwhile; `setData()` replaces it with a fresher copy the caller already
 * has (e.g. the PO returned by an approve call), so no extra request is needed. */
export function useLoad<T>(load: () => Promise<T>) {
  const [result, setResult] = useState<LoadState<T>>({ state: "loading" });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let cancelled = false;
    load()
      .then((data) => !cancelled && setResult({ state: "ok", data }))
      .catch((error: unknown) => !cancelled && setResult({ state: "error", error }));
    return () => {
      cancelled = true;
    };
  }, [load, generation]);

  const reload = useCallback(() => setGeneration((g) => g + 1), []);
  const setData = useCallback((data: T) => setResult({ state: "ok", data }), []);
  return { result, reload, setData };
}
