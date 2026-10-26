import { api, ApiError, describeDetail } from "../api/client";
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
