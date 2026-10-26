import { joinApi, parseApiBase } from "../api/base";
import { api, API_BASE, ApiError, describeDetail } from "../api/client";
import { setToken } from "../api/token";
import { buildLedger } from "../lib/ledger";
import { deriveStages } from "../lib/stages";
import { fx, json, mockFetch } from "./fixtures";

describe("api client", () => {
  it("turns FastAPI validation errors into readable messages", () => {
    expect(describeDetail({ detail: [{ loc: ["body", "user_patch"], msg: "String too long" }] })).toBe("user_patch: String too long");
    expect(describeDetail({ detail: "Paste a unified diff to verify." })).toBe("Paste a unified diff to verify.");
    expect(describeDetail("nope")).toBeNull();
  });

  it("reports non-JSON success bodies as malformed", async () => {
    mockFetch({ "GET /api/status": () => new Response("<html>proxy error</html>", { status: 200 }) });
    await expect(api.status()).rejects.toMatchObject({ kind: "malformed" });
  });

  it("uses a fallback message for HTTP errors without a JSON detail", async () => {
    mockFetch({ "GET /api/runs": () => new Response("Bad Gateway", { status: 502 }) });
    const err = await api.runs().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ kind: "http", status: 502, message: "The PatchProof backend is not responding." });
  });

  it("explains a non-JSON 404 as /api not being routed to the backend", async () => {
    mockFetch({ "GET /api/status": () => new Response("<html>404: NOT_FOUND</html>", { status: 404 }) });
    const err = await api.status().catch((e: unknown) => e);
    expect(err).toMatchObject({ kind: "http", status: 404, notRouted: true });
    expect((err as ApiError).message).toMatch(/\/api is not routed to a PatchProof backend/);
  });

  it("explains a JSON host 404 (Vercel answers Accept: application/json) as /api not being routed", async () => {
    // The exact shape production returned: documented in the audit, not a PatchProof body (no `detail`).
    mockFetch({
      "GET /api/status": () =>
        json({ error: { code: "404", message: "The page could not be found" } }, 404),
    });
    const err = await api.status().catch((e: unknown) => e);
    expect(err).toMatchObject({ kind: "http", status: 404, notRouted: true });
    expect((err as ApiError).message).toMatch(/\/api is not routed to a PatchProof backend/);
  });

  it("keeps the backend's own message for a JSON 404", async () => {
    mockFetch({ "GET /api/runs/run_gone": () => json({ detail: "Run not found." }, 404) });
    await expect(api.run("run_gone")).rejects.toMatchObject({
      status: 404,
      message: "Run not found.",
      notRouted: false,
    });
  });

  it("sends the access token and classifies 401 as an auth error", async () => {
    const { calls } = mockFetch({ "GET /api/status": () => json({ detail: "The access token is not valid." }, 401) });
    setToken("ppa_abc");
    const err = await api.status().catch((e: unknown) => e);
    expect(new Headers(calls[0]?.init?.headers).get("Authorization")).toBe("Bearer ppa_abc");
    expect(err).toMatchObject({ kind: "auth", status: 401, message: "The access token is not valid." });
  });

  it("sends no Authorization header without a token", async () => {
    const { calls } = mockFetch({ "GET /api/status": () => json(fx.status()) });
    await api.status();
    expect(new Headers(calls[0]?.init?.headers).has("Authorization")).toBe(false);
  });

  it("sends the Idempotency-Key when starting a run", async () => {
    const { calls } = mockFetch({ "POST /api/runs": () => json(fx.minimal(), 202) });
    await api.createRun({ example_id: "x", patch_source: "scripted", scripted_patch_id: "y" }, "key-0123456789");
    expect(new Headers(calls[0]?.init?.headers).get("Idempotency-Key")).toBe("key-0123456789");
    expect(new Headers(calls[0]?.init?.headers).get("Content-Type")).toBe("application/json");
  });

  it("aborts requests that exceed the timeout", async () => {
    vi.useFakeTimers();
    try {
      vi.stubGlobal("fetch", vi.fn((_: unknown, init?: RequestInit) => new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      })));
      const pending = api.run("run_slow").catch((e: unknown) => e);
      await vi.advanceTimersByTimeAsync(21_000);
      expect(await pending).toMatchObject({ kind: "timeout" });
    } finally {
      vi.useRealTimers();
    }
  });

  it("accepts a real run record", async () => {
    const run = fx.minimal();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run) });
    await expect(api.run(run.id)).resolves.toMatchObject({ id: run.id, state: "finished" });
  });
});

describe("API base URL", () => {
  it("defaults to the same origin", () => {
    expect(API_BASE).toBe(""); // the test build sets no VITE_API_BASE_URL
    expect(parseApiBase(undefined)).toBe("");
    expect(parseApiBase("  ")).toBe("");
  });

  it("normalizes absolute URLs so joining never doubles or drops a slash", () => {
    for (const raw of ["https://api.example.com", "https://api.example.com/", "https://API.example.com//"]) {
      expect(joinApi(parseApiBase(raw), "/api/status")).toBe("https://api.example.com/api/status");
    }
    expect(joinApi(parseApiBase("https://example.com/patchproof/"), "/api/runs")).toBe("https://example.com/patchproof/api/runs");
    expect(parseApiBase("http://127.0.0.1:8000", { production: true })).toBe("http://127.0.0.1:8000");
  });

  it("rejects addresses that would send requests to the wrong place", () => {
    expect(() => parseApiBase("api.example.com")).toThrow(/absolute URL/);
    expect(() => parseApiBase("/api")).toThrow(/absolute URL/);
    expect(() => parseApiBase("ftp://api.example.com")).toThrow(/https:\/\//);
    expect(() => parseApiBase("https://user:pw@api.example.com")).toThrow(/credentials/);
    expect(() => parseApiBase("https://api.example.com/?x=1")).toThrow(/query/);
    expect(() => parseApiBase("http://api.example.com", { production: true })).toThrow(/must use https/);
  });
});

describe("derived views", () => {
  it("derives every pipeline stage from recorded events", () => {
    const stages = deriveStages(fx.plausible());
    expect(stages.map((s) => s.state)).toEqual(["done", "done", "done", "done", "done"]);
    const blocked = deriveStages(fx.blocked());
    expect(blocked.find((s) => s.key === "patch")?.state).toBe("failed");
    expect(blocked.find((s) => s.key === "candidate")?.state).toBe("skipped");
  });

  it("marks only the stage with an open 'started' event as active", () => {
    const run = fx.plausible();
    const live = { ...run, state: "running" as const, events: run.events.slice(0, 3) };
    expect(deriveStages(live).map((s) => s.state)).toEqual(["done", "active", "pending", "pending", "pending"]);
  });

  it("builds ledger rows that match the backend comparison", () => {
    const run = fx.plausible();
    const rows = buildLedger(run);
    expect(rows).toHaveLength(run.verdict!.comparison.length);
    for (const c of run.verdict!.comparison) {
      expect(rows.find((r) => r.nodeid === c.nodeid)?.category).toBe(c.category);
    }
  });
});
