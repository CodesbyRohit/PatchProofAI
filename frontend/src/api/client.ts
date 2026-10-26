import type { Example, Project, Run, RunRequest, RunSummary, Status } from "./types";

export type ApiErrorKind = "network" | "timeout" | "http" | "malformed";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;

  constructor(kind: ApiErrorKind, message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
  }
}

const DEFAULT_TIMEOUT_MS = 20_000;
const RUN_STATES = new Set(["queued", "running", "finished", "error"]);

/** FastAPI returns {"detail": "..."} for app errors and {"detail": [{loc, msg}, ...]} for validation errors. */
export function describeDetail(body: unknown): string | null {
  if (!body || typeof body !== "object" || !("detail" in body)) return null;
  const detail = (body as { detail: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (!d || typeof d !== "object") return null;
        const { loc, msg } = d as { loc?: unknown; msg?: unknown };
        const where = Array.isArray(loc) ? loc.filter((x) => x !== "body").join(".") : "";
        return typeof msg === "string" ? (where ? `${where}: ${msg}` : msg) : null;
      })
      .filter(Boolean);
    return parts.length ? parts.join("; ") : null;
  }
  return null;
}

const HTTP_FALLBACK: Record<number, string> = {
  404: "Not found.",
  413: "The upload is too large.",
  422: "The request was rejected.",
  429: "Too many requests. Please wait a minute.",
  500: "The PatchProof backend hit an internal error.",
  502: "The PatchProof backend is not responding.",
  503: "The PatchProof backend is busy. Try again shortly.",
  504: "The PatchProof backend did not respond in time.",
};

async function request<T>(
  path: string,
  init: RequestInit,
  validate: (body: unknown) => body is T,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(path, { ...init, signal: controller.signal, headers: { Accept: "application/json", ...init.headers } });
  } catch {
    if (controller.signal.aborted) {
      throw new ApiError("timeout", `The backend did not answer within ${Math.round(timeoutMs / 1000)} s.`);
    }
    throw new ApiError("network", "Cannot reach the PatchProof backend. Is the API server running on port 8000?", undefined);
  } finally {
    clearTimeout(timer);
  }

  const text = await res.text();
  let body: unknown = undefined;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = undefined;
    }
  }
  if (!res.ok) {
    const message = describeDetail(body) ?? HTTP_FALLBACK[res.status] ?? `Request failed with HTTP ${res.status}.`;
    throw new ApiError("http", message, res.status);
  }
  if (body === undefined || !validate(body)) {
    throw new ApiError("malformed", "The backend sent a response PatchProof could not read.", res.status);
  }
  return body;
}

// -- minimal shape guards: enough to fail loudly instead of rendering garbage ------------------------

const isObj = (x: unknown): x is Record<string, unknown> => typeof x === "object" && x !== null && !Array.isArray(x);

export function isRun(x: unknown): x is Run {
  return (
    isObj(x) &&
    typeof x.id === "string" &&
    typeof x.state === "string" &&
    RUN_STATES.has(x.state) &&
    typeof x.project_name === "string" &&
    Array.isArray(x.events) &&
    Array.isArray(x.baseline_runs) &&
    Array.isArray(x.attempts)
  );
}

function isRunSummaryList(x: unknown): x is RunSummary[] {
  return Array.isArray(x) && x.every((r) => isObj(r) && typeof r.id === "string" && typeof r.state === "string");
}

function isExampleList(x: unknown): x is Example[] {
  return Array.isArray(x) && x.every((e) => isObj(e) && typeof e.id === "string" && Array.isArray(e.patches));
}

function isStatus(x: unknown): x is Status {
  return isObj(x) && isObj(x.model) && isObj(x.sandbox) && isObj(x.limits);
}

function isProject(x: unknown): x is Project {
  return isObj(x) && typeof x.id === "string" && Array.isArray(x.files) && isObj(x.summary);
}

// -- endpoints ---------------------------------------------------------------------------------------

export const api = {
  // /api/status may contact the BharatCode catalog when a key is configured, so allow it more time.
  status: () => request("/api/status", { method: "GET" }, isStatus, 30_000),
  examples: () => request("/api/examples", { method: "GET" }, isExampleList),
  runs: (limit = 30) => request(`/api/runs?limit=${limit}`, { method: "GET" }, isRunSummaryList),
  run: (id: string) => request(`/api/runs/${encodeURIComponent(id)}`, { method: "GET" }, isRun),
  createRun: (body: RunRequest) =>
    request(
      "/api/runs",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      isRun,
    ),
  uploadProject: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request("/api/projects/upload", { method: "POST", body: form }, isProject, 60_000);
  },
};

export const downloads = {
  patch: (id: string) => `/api/runs/${encodeURIComponent(id)}/patch.diff`,
  markdown: (id: string) => `/api/runs/${encodeURIComponent(id)}/report.md`,
  json: (id: string) => `/api/runs/${encodeURIComponent(id)}/report.json`,
};

/** Upload limits enforced by the backend (projects.py). Checked client-side only to fail fast. */
export const UPLOAD_LIMIT_BYTES = 4 * 1024 * 1024;
