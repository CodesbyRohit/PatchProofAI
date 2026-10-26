import { joinApi, parseApiBase } from "./base";
import { getToken } from "./token";
import type {
  Branch,
  CommitInfo,
  Example,
  GitHubRunRequest,
  Installation,
  Me,
  Project,
  PullRequestPreview,
  PullRequestRecord,
  RepoPage,
  RepoSummary,
  Run,
  RunRequest,
  RunSummary,
  Status,
} from "./types";

export type ApiErrorKind = "network" | "timeout" | "http" | "auth" | "malformed";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  /** True when /api is not routed to a PatchProof backend at all: the page host answered 404 itself. */
  readonly notRouted: boolean;

  constructor(kind: ApiErrorKind, message: string, status?: number, notRouted = false) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.notRouted = notRouted;
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
/** The signed-in session's CSRF token (from /api/auth/me); sent on every request that changes state. */
let csrfToken: string | null = null;
export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

async function send(path: string, init: RequestInit, timeoutMs: number): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const token = getToken();
  const writes = (init.method ?? "GET").toUpperCase() !== "GET";
  let res: Response;
  try {
    res = await fetch(joinApi(API_BASE, path), {
      ...init,
      signal: controller.signal,
      // The session cookie must travel with requests to a separately hosted (same-site) API too.
      credentials: API_BASE ? "include" : "same-origin",
      headers: {
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(writes && csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        ...init.headers,
      },
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
    // The backend answers its own 404s with {"detail": ...}. A 404 without a detail field came from whatever
    // serves the page because /api is not routed to the PatchProof backend. Hosts differ: some answer with no
    // body or HTML, and Vercel answers `Accept: application/json` with {"error": {"code": "404", ...}}, so the
    // body's shape cannot be the test.
    const detail = describeDetail(body);
    const unrouted = res.status === 404 && detail === null && !API_BASE ? API_NOT_ROUTED : null;
    const message = detail ?? unrouted ?? HTTP_FALLBACK[res.status] ?? `Request failed with HTTP ${res.status}.`;
    throw new ApiError(res.status === 401 ? "auth" : "http", message, res.status, unrouted !== null);
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

function isMe(x: unknown): x is Me {
  return isObj(x) && typeof x.authenticated === "boolean" && isObj(x.github);
}

const isOk = (x: unknown): x is { ok: boolean } => isObj(x) && x.ok === true;
const isInstallations = (x: unknown): x is Installation[] =>
  Array.isArray(x) && x.every((i) => isObj(i) && typeof i.id === "number" && typeof i.account === "string");
const isRepo = (x: unknown): x is RepoSummary =>
  isObj(x) && typeof x.full_name === "string" && typeof x.private === "boolean" && typeof x.access === "string";
const isRepoPage = (x: unknown): x is RepoPage =>
  isObj(x) && Array.isArray(x.repositories) && x.repositories.every(isRepo) && typeof x.has_more === "boolean";
const isBranches = (x: unknown): x is { branches: Branch[]; has_more: boolean } =>
  isObj(x) && Array.isArray(x.branches) && x.branches.every((b) => isObj(b) && typeof b.name === "string");
const isCommit = (x: unknown): x is CommitInfo => isObj(x) && typeof x.sha === "string" && /^[0-9a-f]{40}$/.test(x.sha);
const isPullRequest = (x: unknown): x is PullRequestRecord => isObj(x) && typeof x.state === "string" && typeof x.branch === "string";
const isPreview = (x: unknown): x is PullRequestPreview =>
  isObj(x) && typeof x.diff === "string" && typeof x.patch_sha256 === "string" && Array.isArray(x.changed_files);

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

const json = (method: string, body: unknown, headers: Record<string, string> = {}): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json", ...headers },
  body: JSON.stringify(body),
});
const q = encodeURIComponent;

export const auth = {
  me: () => request("/api/auth/me", { method: "GET" }, isMe),
  /** Full-page navigation: the browser goes to GitHub and comes back to /github. */
  signInUrl: () => joinApi(API_BASE, "/api/auth/github/start"),
  signOut: () => request("/api/auth/logout", { method: "POST" }, isOk),
  disconnect: () => request("/api/auth/github/disconnect", { method: "POST" }, isOk),
  /** Deletes every run, project and usage record stored for this user, and revokes the GitHub authorization. */
  deleteAccount: () => request("/api/account", { method: "DELETE" }, (x): x is { deleted: Record<string, number> } => isObj(x) && isObj(x.deleted)),
};

export const gh = {
  installations: () => request("/api/github/installations", { method: "GET" }, isInstallations),
  repos: (installationId: number, page: number) =>
    request(`/api/github/installations/${installationId}/repos?page=${page}`, { method: "GET" }, isRepoPage),
  repo: (fullName: string) => request(`/api/github/repo?full_name=${q(fullName)}`, { method: "GET" }, isRepo),
  branches: (fullName: string) =>
    request(`/api/github/branches?full_name=${q(fullName)}`, { method: "GET" }, isBranches),
  commit: (fullName: string, ref: string) =>
    request(`/api/github/commit?full_name=${q(fullName)}&ref=${q(ref)}`, { method: "GET" }, isCommit),
  createRun: (body: GitHubRunRequest, idempotencyKey: string) =>
    request("/api/github/runs", json("POST", body, { "Idempotency-Key": idempotencyKey }), isRun, 60_000),
  pullRequestPreview: (runId: string) =>
    request(`/api/runs/${q(runId)}/pull-request`, { method: "GET" }, isPreview),
  createPullRequest: (runId: string, body: { title: string; body: string; patch_sha256: string }) =>
    request(`/api/runs/${q(runId)}/pull-request`, json("POST", body), isPullRequest, 120_000),
};

export const downloads = {
  patch: (id: string) => `/api/runs/${encodeURIComponent(id)}/patch.diff`,
  markdown: (id: string) => `/api/runs/${encodeURIComponent(id)}/report.md`,
  json: (id: string) => `/api/runs/${encodeURIComponent(id)}/report.json`,
};

/** Upload limits enforced by the backend (projects.py). Checked client-side only to fail fast. */
export const UPLOAD_LIMIT_BYTES = 4 * 1024 * 1024;
