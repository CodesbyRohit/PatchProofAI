// Real backend responses captured from the running API with the Docker sandbox (see fixtures/README.md).
import type { Example, Run, RunSummary, Status } from "../api/types";
import aiNoConfig from "./fixtures/ai_noconfig.json";
import blocked from "./fixtures/blocked.json";
import examples from "./fixtures/examples.json";
import inconclusive from "./fixtures/inconclusive.json";
import minimal from "./fixtures/minimal.json";
import planRequired from "./fixtures/plan_required.json";
import plausible from "./fixtures/plausible.json";
import runs from "./fixtures/runs.json";
import status from "./fixtures/status.json";

const clone = <T,>(x: unknown): T => structuredClone(x) as T;

export const fx = {
  status: () => clone<Status>(status),
  examples: () => clone<Example[]>(examples),
  runs: () => clone<RunSummary[]>(runs),
  plausible: () => clone<Run>(plausible),
  minimal: () => clone<Run>(minimal),
  blocked: () => clone<Run>(blocked),
  inconclusive: () => clone<Run>(inconclusive),
  aiNoConfig: () => clone<Run>(aiNoConfig),
  planRequired: () => clone<Run>(planRequired),
};

type Handler = (url: string, init: RequestInit | undefined) => Response | Promise<Response>;

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Route fetch() calls by "METHOD /path" (query strings ignored). Unrouted calls fail the test loudly. */
export function mockFetch(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; init?: RequestInit }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init?.method ?? "GET").toUpperCase();
    const path = url.split("?")[0] ?? url;
    calls.push({ method, path, init });
    const handler = routes[`${method} ${path}`];
    if (!handler) throw new Error(`Unexpected request: ${method} ${path}`);
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}
