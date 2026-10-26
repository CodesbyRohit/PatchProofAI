import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { RunRequest } from "../api/types";
import { navigate } from "../router";

function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** Start a run on the backend and open its page. `pending` holds the key of the action in flight. */
export function useStartRun() {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<{ key: string; error: ApiError } | null>(null);
  // When a start times out or loses the connection, the backend may still have started the run. Retrying the
  // same action with the same body reuses the Idempotency-Key, so the backend returns that run, not a second one.
  const unconfirmed = useRef<{ action: string; body: string; idempotencyKey: string } | null>(null);

  async function start(key: string, body: RunRequest) {
    setPending(key);
    setError(null);
    const serialized = JSON.stringify(body);
    const previous = unconfirmed.current;
    const idempotencyKey =
      previous && previous.action === key && previous.body === serialized ? previous.idempotencyKey : newKey();
    try {
      const run = await api.createRun(body, idempotencyKey);
      unconfirmed.current = null;
      navigate(`/runs/${run.id}`);
    } catch (e) {
      const err = e instanceof ApiError ? e : new ApiError("malformed", "Could not start the run.");
      unconfirmed.current =
        err.kind === "timeout" || err.kind === "network" ? { action: key, body: serialized, idempotencyKey } : null;
      setError({ key, error: err });
    } finally {
      setPending(null);
    }
  }

  return { start, pending, error };
}
