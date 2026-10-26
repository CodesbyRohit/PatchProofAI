import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../App";
import { fx, json, mockFetch } from "./fixtures";

const base = {
  "GET /api/status": () => json(fx.status()),
  "GET /api/examples": () => json(fx.examples()),
};

describe("workspace", () => {
  it("puts the regression-trap example first and starts a scripted run with the exact request body", async () => {
    const run = fx.plausible();
    const { calls } = mockFetch({
      ...base,
      "POST /api/runs": () => json({ ...run, state: "queued" }, 202),
      [`GET /api/runs/${run.id}`]: () => json(run),
    });
    render(<App />);

    const cards = await screen.findAllByRole("article");
    expect(within(cards[0]!).getByRole("heading", { name: "Pagination drops the last page" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Run scripted patch: Plausible fix: add one page" }));
    const post = calls.find((c) => c.method === "POST");
    expect(JSON.parse(String(post?.init?.body))).toEqual({
      example_id: "pagination-off-by-one",
      patch_source: "scripted",
      scripted_patch_id: "plausible-plus-one",
    });
    expect(await screen.findByRole("img", { name: "Verdict: Failed verification" })).toBeInTheDocument();
    expect(window.location.pathname).toBe(`/runs/${run.id}`);
  });

  it("disables AI mode with a reason when no API key is configured", async () => {
    mockFetch(base);
    render(<App />);
    const buttons = await screen.findAllByRole("button", { name: "Diagnose with BharatCode" });
    for (const b of buttons) {
      expect(b).toBeDisabled();
      expect(b).toHaveAccessibleDescription("AI mode needs BHARATCODE_API_KEY on the server.");
    }
  });

  it("explains an unavailable Docker sandbox", async () => {
    const status = fx.status();
    status.sandbox = { ...status.sandbox, docker_available: false, untrusted_execution: false, detail: "Docker is not running." };
    mockFetch({ ...base, "GET /api/status": () => json(status) });
    render(<App />);
    expect(await screen.findByText(/Docker unavailable\. Uploads and AI patches will be blocked/)).toBeInTheDocument();
    expect(screen.getByText("No sandbox for uploaded code")).toBeInTheDocument();
  });

  it("tells the user when the backend is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    render(<App />);
    expect(await screen.findByText("Backend status unavailable")).toBeInTheDocument();
    expect(screen.getAllByText(/Cannot reach the PatchProof backend/).length).toBeGreaterThan(0);
  });

  it("explains a hosted frontend whose /api is not routed to a backend", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<!doctype html><h1>404: NOT_FOUND</h1>", { status: 404 })));
    render(<App />);
    expect(await screen.findByText("Backend status unavailable")).toBeInTheDocument();
    expect(screen.getAllByText(/\/api is not routed to a PatchProof backend/).length).toBeGreaterThan(0);
  });

  it("asks once for an access token and reloads with it", async () => {
    const authorized = (init?: RequestInit) => new Headers(init?.headers).get("Authorization") === "Bearer ppa_secret";
    const denied = () => json({ detail: "This PatchProof instance requires an access token." }, 401);
    const { calls } = mockFetch({
      "GET /api/status": (_, init) => (authorized(init) ? json(fx.status()) : denied()),
      "GET /api/examples": (_, init) => (authorized(init) ? json(fx.examples()) : denied()),
    });
    render(<App />);

    expect(await screen.findByText("Access token required")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Access token")).toHaveLength(1); // one form, not one per failed request
    await userEvent.type(screen.getByLabelText("Access token"), "ppa_secret");
    await userEvent.click(screen.getByRole("button", { name: "Use token" }));

    expect(await screen.findAllByRole("article")).not.toHaveLength(0);
    expect(screen.queryByText("Access token required")).not.toBeInTheDocument();
    expect(calls.filter((c) => authorized(c.init)).map((c) => c.path).sort()).toEqual(["/api/examples", "/api/status"]);
  });

  it("reuses the idempotency key when retrying a start whose answer was lost", async () => {
    const run = fx.plausible();
    let attempt = 0;
    const { calls } = mockFetch({
      ...base,
      "POST /api/runs": () => {
        attempt += 1;
        if (attempt === 1) throw new TypeError("Failed to fetch");
        return json({ ...run, state: "queued" }, 202);
      },
      [`GET /api/runs/${run.id}`]: () => json(run),
    });
    render(<App />);
    const button = await screen.findByRole("button", { name: "Run scripted patch: Plausible fix: add one page" });
    await userEvent.click(button);
    expect(await screen.findByText(/Cannot reach the PatchProof backend/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run scripted patch: Plausible fix: add one page" }));
    expect(await screen.findByRole("img", { name: "Verdict: Failed verification" })).toBeInTheDocument();

    const keys = calls.filter((c) => c.method === "POST").map((c) => new Headers(c.init?.headers).get("Idempotency-Key"));
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBeTruthy();
    expect(keys[1]).toBe(keys[0]);
  });

  it("shows the backend's reason when it refuses to start a run", async () => {
    mockFetch({ ...base, "POST /api/runs": () => json({ detail: "Too many runs. Please wait a minute." }, 429) });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Run scripted patch: Minimal fix: ceiling division" }));
    expect(await screen.findByText("Too many runs. Please wait a minute.")).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });

  it("shows an upload rejection from the backend", async () => {
    mockFetch({ ...base, "POST /api/projects/upload": () => json({ detail: "Archive entry escapes the project: ../evil.py" }, 422) });
    render(<App />);
    const input = await screen.findByLabelText(/Choose a \.zip/);
    await userEvent.upload(input, new File(["PK"], "project.zip", { type: "application/zip" }));
    expect(await screen.findByText("Upload rejected")).toBeInTheDocument();
    expect(screen.getByText("Archive entry escapes the project: ../evil.py")).toBeInTheDocument();
  });

  it("rejects non-zip files before uploading", async () => {
    const { calls } = mockFetch(base);
    render(<App />);
    const input = await screen.findByLabelText(/Choose a \.zip/);
    await userEvent.upload(input, new File(["x"], "notes.txt", { type: "text/plain" }), { applyAccept: false });
    expect(await screen.findByText("Only .zip archives are supported for upload.")).toBeInTheDocument();
    expect(calls.some((c) => c.path === "/api/projects/upload")).toBe(false);
  });

  it("uploads a project and submits a user diff for verification", async () => {
    const project = { id: "upl_1", name: "shop", origin: "upload", trusted: false, files: [], summary: { ...fx.examples()[0]!.summary } };
    const run = fx.minimal();
    const { calls } = mockFetch({
      ...base,
      "POST /api/projects/upload": () => json(project),
      "POST /api/runs": () => json({ ...run, state: "queued" }, 202),
      [`GET /api/runs/${run.id}`]: () => json(run),
    });
    render(<App />);
    await userEvent.upload(await screen.findByLabelText(/Choose a \.zip/), new File(["PK"], "shop.zip", { type: "application/zip" }));
    expect(await screen.findByRole("heading", { name: "shop" })).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/verify your own patch/i), "--- a/cart.py");
    await userEvent.click(screen.getByRole("button", { name: "Verify this diff" }));
    const post = calls.find((c) => c.method === "POST" && c.path === "/api/runs");
    expect(JSON.parse(String(post?.init?.body))).toMatchObject({ project_id: "upl_1", patch_source: "user", user_patch: "--- a/cart.py" });
  });
});

describe("history", () => {
  it("lists real runs and reopens one", async () => {
    const run = fx.plausible();
    mockFetch({ "GET /api/runs": () => json(fx.runs()), [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    window.history.replaceState(null, "", "/history");
    render(<App />);

    const links = await screen.findAllByRole("link", { name: /Pagination drops the last page|Report helpers/ });
    expect(links).toHaveLength(5);
    await userEvent.click(screen.getByRole("link", { name: /Rejected: the patch fixes 2 of 2/ }));
    expect(await screen.findByRole("img", { name: "Verdict: Failed verification" })).toBeInTheDocument();
  });

  it("has an empty state", async () => {
    mockFetch({ "GET /api/runs": () => json([]) });
    window.history.replaceState(null, "", "/history");
    render(<App />);
    expect(await screen.findByText("No runs yet.")).toBeInTheDocument();
  });
});
