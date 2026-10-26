import { useEffect, useEffectEvent, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Run } from "../api/types";

export interface AsyncState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
}

function toApiError(err: unknown): ApiError {
  return err instanceof ApiError ? err : new ApiError("malformed", "Unexpected error while reading the response.");
}

interface Settled<T> {
  /** Which load this result belongs to; a newer request makes the state "loading" again. */
  request: number;
  key: string;
  data: T | null;
  error: ApiError | null;
}

/** Load data once per `key` (and again on reload()). */
export function useAsync<T>(key: string, load: () => Promise<T>): AsyncState<T> {
  const [request, setRequest] = useState(0);
  const [settled, setSettled] = useState<Settled<T>>({ request: -1, key, data: null, error: null });
  const loader = useEffectEvent(load);

  useEffect(() => {
    let cancelled = false;
    loader().then(
      (data) => !cancelled && setSettled({ request, key, data, error: null }),
      (e: unknown) => !cancelled && setSettled((prev) => ({ request, key, data: prev.data, error: toApiError(e) })),
    );
    return () => {
      cancelled = true;
    };
  }, [key, request]);

  return {
    data: settled.data,
    error: settled.error,
    loading: settled.request !== request || settled.key !== key,
    reload: () => setRequest((r) => r + 1),
  };
}

export const POLL_INTERVAL_MS = 900;
/** After this many consecutive failed polls, stop and let the user retry. */
const MAX_POLL_FAILURES = 4;

/** Fetch a run and keep polling the real backend while it is queued or running. */
export function useRun(id: string): AsyncState<Run> & { polling: boolean } {
  const [data, setData] = useState<Run | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [polling, setPolling] = useState(true);
  const [request, setRequest] = useState(0);
  const failures = useRef(0);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    failures.current = 0;

    const poll = async () => {
      try {
        const run = await api.run(id);
        if (cancelled) return;
        failures.current = 0;
        const live = run.state === "queued" || run.state === "running";
        setData(run);
        setError(null);
        setPolling(live);
        if (live) timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch (e) {
        if (cancelled) return;
        const err = toApiError(e);
        failures.current += 1;
        const retry = err.status !== 404 && failures.current < MAX_POLL_FAILURES;
        setError(err);
        setPolling(retry);
        if (retry) timer = setTimeout(poll, POLL_INTERVAL_MS * 2);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [id, request]);

  return {
    data,
    error,
    loading: data === null && error === null,
    polling,
    reload: () => setRequest((r) => r + 1),
  };
}
