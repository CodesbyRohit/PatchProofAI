import { joinApi, parseApiBase } from "./base";
import { getToken } from "./token";
import type { Example, Project, Run, RunRequest, RunSummary, Status } from "./types";

export type ApiErrorKind = "network" | "timeout" | "http" | "auth" | "malformed";

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

/** "" means same origin. Validated at build time by vite.config.ts, so it cannot be malformed here. */
export const API_BASE = parseApiBase(import.meta.env.VITE_API_BASE_URL);

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
  401: "This PatchProof instance requires an access token.",
  404: "Not found.",
  413: "The upload is too large.",
  422: "The request was rejected.",
  429: "Too many requests. Please wait a minute.",
  500: "The PatchProof backend hit an internal error.",
  502: "The PatchProof backend is not responding.",
  503: "The PatchProof backend is busy. Try again shortly.",
  504: "The PatchProof backend did not respond in time.",
};

const API_NOT_ROUTED =
  "The PatchProof API is not reachable from this site: /api is not routed to a PatchProof backend. " +
  "If this frontend is hosted on its own (for example on Vercel), configure the /api rewrite to the backend.";

function unreachableMessage(): string {
  return API_BASE
    ? `Cannot reach the PatchProof backend at ${API_BASE}. It may be down, or not allow requests from this site (CORS).`
    : "Cannot reach the PatchProof backend. Is the API server running on port 8000?";
}

function parseJson(text: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** Fetch an API path with the access token and a timeout. Resolves only for a 2xx response. */
async function send(path: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const token = getToken();
  let res: Response;
  try {
    res = await fetch(joinApi(API_BASE, path), {
      ...init,
      signal: controller.signal,
      headers: { Accept: "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...init.headers },
    });
  } catch {
    if (controller.signal.aborted) {
      throw new ApiError("timeout", `The backend did not answer within ${Math.round(timeoutMs / 1000)} s.`);
    }
    throw new ApiError("network", unreachableMessage(), undefined);
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    const body = parseJson(await res.text());
    // The backend answers every 404 with JSON. A 404 without a JSON body came from whatever serves the page
    // (for example a static host such as Vercel) because /api is not routed to the PatchProof backend.
    const unrouted = res.status === 404 && body === undefined && !API_BASE ? API_NOT_ROUTED : null;
    const message =
      describeDetail(body) ?? unrouted ?? HTTP_FALLBACK[res.status] ?? `Request failed with HTTP ${res.status}.`;
    throw new ApiError(res.status === 401 ? "auth" : "http", message, res.status);
  }
  return res;
}

async function request<T>(
  path: string,
  init: RequestInit,
  validate: (body: unknown) => body is T,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const res = await send(path, init, timeoutMs);
  const body = parseJson(await res.text());
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
  /** `idempotencyKey`: resend the same key when retrying the same start, and the backend will not start it twice. */
  createRun: (body: RunRequest, idempotencyKey?: string) =>
    request(
      "/api/runs",
      {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}) },
        body: JSON.stringify(body),
      },
      isRun,
    ),
  uploadProject: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request("/api/projects/upload", { method: "POST", body: form }, isProject, 60_000);
  },
  /** Fetch a report or diff (with the access token) and hand it to the browser as a file. */
  download: async (path: string, fallbackName: string) => {
    const res = await send(path, { method: "GET", headers: { Accept: "*/*" } }, 60_000);
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const name = /filename="([^"]+)"/.exec(disposition)?.[1] ?? fallbackName;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  },
};

export const downloads = {
  patch: (id: string) => `/api/runs/${encodeURIComponent(id)}/patch.diff`,
  markdown: (id: string) => `/api/runs/${encodeURIComponent(id)}/report.md`,
  json: (id: string) => `/api/runs/${encodeURIComponent(id)}/report.json`,
};

/** Upload limits enforced by the backend (projects.py). Checked client-side only to fail fast. */
export const UPLOAD_LIMIT_BYTES = 4 * 1024 * 1024;
