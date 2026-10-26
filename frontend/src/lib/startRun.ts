import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { RunRequest } from "../api/types";
import { navigate } from "../router";

/** Start a run on the backend and open its page. `pending` holds the key of the action in flight. */
export function useStartRun() {
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<{ key: string; error: ApiError } | null>(null);

  async function start(key: string, body: RunRequest) {
    setPending(key);
    setError(null);
    try {
      const run = await api.createRun(body);
      navigate(`/runs/${run.id}`);
    } catch (e) {
      setError({ key, error: e instanceof ApiError ? e : new ApiError("malformed", "Could not start the run.") });
    } finally {
      setPending(null);
    }
  }

  return { start, pending, error };
}
