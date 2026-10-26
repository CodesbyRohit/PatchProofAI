import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setToken } from "../api/token";
import { App } from "../App";
import { fx, json, mockFetch } from "./fixtures";

function openRun(id: string) {
  window.history.replaceState(null, "", `/runs/${id}`);
  return render(<App />);
}

describe("run page", () => {
  it("shows a FAILED verdict with the three regressions the plausible patch caused", async () => {
    const run = fx.plausible();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    expect(await screen.findByRole("img", { name: "Verdict: Failed verification" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /breaks 3 tests that passed before/ })).toBeInTheDocument();

    const table = screen.getByRole("table");
    const regressions = within(table).getAllByText("Regression");
    expect(regressions).toHaveLength(3);
    expect(within(table).getAllByText("Fixed")).toHaveLength(2);
    // Regressions sort to the top of the ledger.
    const firstRow = within(table).getAllByRole("row")[1]!;
    expect(within(firstRow).getByText("Regression")).toBeInTheDocument();
    // Per-run cells are grounded in the baseline/candidate test runs.
    expect(within(firstRow).getByRole("img", { name: "Baseline run 1: passed" })).toBeInTheDocument();
    expect(within(firstRow).getByRole("img", { name: "Patched run 2: failed" })).toBeInTheDocument();
  });

  it("labels scripted patches as demo data, never as model output", async () => {
    const run = fx.minimal();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    expect(await screen.findByRole("img", { name: "Verdict: Verified for tested cases" })).toBeInTheDocument();
    expect(screen.getByText("Demo data: scripted patch")).toBeInTheDocument();
    expect(screen.getByText(/not model output/i, { selector: ".attempt__source" })).toBeInTheDocument();
    expect(screen.queryByText(/BharatCode model output/)).not.toBeInTheDocument();
  });

  it("explains HTTP 402 as an access issue, not a code failure", async () => {
    const run = fx.planRequired();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    const alert = await screen.findByRole("alert", { name: "Model access issue, not a code failure" });
    expect(alert).toHaveTextContent("HTTP 402");
    expect(alert).toHaveTextContent("nothing about your code was judged");
    expect(alert).toHaveTextContent("req-402-fixture");
    expect(screen.getByRole("img", { name: "Verdict: Inconclusive" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Verdict: Failed verification" })).not.toBeInTheDocument();
  });

  it("does not claim the code was unjudged when an earlier attempt was executed", async () => {
    // Attempt 1 executed and failed; the attempt-2 repair request then hit HTTP 402.
    const run = fx.plausible();
    const first = { ...run.attempts[0]!, diagnosis_source: "live_model" as const };
    const second = { ...first, number: 2, patch: null, candidate_runs: [], verdict: null, error: "HTTP 402: plan" };
    const mixed = { ...run, patch_source: "ai" as const, model_error_kind: "plan_required", attempts: [first, second] };
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(mixed), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    const alert = await screen.findByRole("alert", { name: /^A later model request failed/ });
    expect(alert).toHaveTextContent("after attempt 1 had already been executed");
    expect(alert).toHaveAccessibleName("A later model request failed (plan_required)");
    expect(alert).not.toHaveTextContent("nothing about your code was judged");
    expect(alert).not.toHaveTextContent("not a code failure");
    expect(screen.getByRole("img", { name: "Verdict: Failed verification" })).toBeInTheDocument();
  });

  it("explains a missing API key and keeps the real baseline evidence", async () => {
    const run = fx.aiNoConfig();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    expect(await screen.findByRole("alert", { name: "No model configured on the server" })).toBeInTheDocument();
    expect(screen.getByText(/No patched runs: the patch was not executed/)).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: /Baseline run 1: failed/ })).toHaveLength(2);
  });

  it("shows the policy finding for a blocked patch that edited a test", async () => {
    const run = fx.blocked();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    expect(await screen.findByRole("img", { name: "Verdict: Blocked by safety policy" })).toBeInTheDocument();
    const findings = screen.getByRole("list", { name: "Policy findings" });
    expect(findings).toHaveTextContent("file.test_protected");
    expect(screen.getByText("not applied")).toBeInTheDocument();
    // No diff exists for a blocked patch, so no diff download is offered.
    expect(screen.getByRole("button", { name: /report\.md/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /patch\.diff/ })).not.toBeInTheDocument();
  });

  it("surfaces environment collection errors once, with every run they appeared in", async () => {
    const run = fx.inconclusive();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    expect(await screen.findByRole("img", { name: "Verdict: Inconclusive" })).toBeInTheDocument();
    expect(screen.getByText("Collection errors")).toBeInTheDocument();
    expect(screen.getAllByText("tests/test_config_loader.py", { selector: "li > .mono" })).toHaveLength(1);
    expect(screen.getByText(/in Baseline run 1\/2, Baseline run 2\/2, Candidate 1 run 1\/2, Candidate 1 run 2\/2/)).toBeInTheDocument();
  });

  it("downloads the real reports with the access token and the backend's filename", async () => {
    const run = fx.plausible();
    setToken("ppa_test-token");
    const { calls } = mockFetch({
      [`GET /api/runs/${run.id}`]: () => json(run),
      "GET /api/examples": () => json(fx.examples()),
      [`GET /api/runs/${run.id}/patch.diff`]: () =>
        new Response("--- a/m.py\n", { headers: { "Content-Disposition": `attachment; filename="${run.id}.diff"` } }),
      [`GET /api/runs/${run.id}/report.md`]: () => json({ detail: "Run not found." }, 404),
    });
    // jsdom has no object URLs; provide them for this test.
    const createObjectURL = vi.fn(() => "blob:report");
    Object.defineProperty(URL, "createObjectURL", { value: createObjectURL, configurable: true, writable: true });
    Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true, writable: true });
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push(this.download);
    });
    openRun(run.id);

    await userEvent.click(await screen.findByRole("button", { name: /patch\.diff/ }));
    const diff = calls.find((c) => c.path === `/api/runs/${run.id}/patch.diff`);
    expect(new Headers(diff?.init?.headers).get("Authorization")).toBe("Bearer ppa_test-token");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(clicked).toEqual([`${run.id}.diff`]);

    await userEvent.click(screen.getByRole("button", { name: /report\.md/ }));
    expect(await screen.findByText("Download failed")).toBeInTheDocument();
    expect(screen.getByText("Run not found.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /report\.json/ })).toBeInTheDocument();
  });

  it("expands a test row to show the real failure message", async () => {
    const run = fx.plausible();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    const toggle = await screen.findByRole("button", { name: /test_partial_last_page_is_counted/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(screen.getByRole("table")).getByText(/assert 2 == 3/)).toBeInTheDocument();
  });

  it("polls a running run until the backend reports it finished", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const done = fx.minimal();
      const running = { ...fx.minimal(), state: "running", verdict: null, finished_at: null, attempts: [], events: done.events.slice(0, 4) };
      const responses = [running, running, done];
      const { calls } = mockFetch({
        [`GET /api/runs/${done.id}`]: () => json(responses.shift() ?? done),
        "GET /api/examples": () => json(fx.examples()),
      });
      openRun(done.id);

      expect(await screen.findByText("Running")).toBeInTheDocument();
      expect(screen.getByText("polling backend", { exact: false })).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      expect(await screen.findByRole("img", { name: "Verdict: Verified for tested cases" })).toBeInTheDocument();
      const polls = calls.filter((c) => c.path === `/api/runs/${done.id}`).length;
      expect(polls).toBe(3);
      await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      expect(calls.filter((c) => c.path === `/api/runs/${done.id}`).length).toBe(polls); // stopped polling
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports an interrupted run as incomplete, without a verdict", async () => {
    const run = { ...fx.minimal(), state: "error", verdict: null, error: "The server restarted before this run finished." };
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    openRun(run.id);

    const title = await screen.findByText("This run did not finish");
    expect(title.closest('[role="alert"]')).toHaveTextContent("The server restarted before this run finished.");
    expect(screen.queryByRole("img", { name: /^Verdict:/ })).not.toBeInTheDocument();
  });

  it("shows a not-found state for unknown runs", async () => {
    mockFetch({ "GET /api/runs/run_missing": () => json({ detail: "Run not found." }, 404) });
    openRun("run_missing");
    expect(await screen.findByText("Run not found")).toBeInTheDocument();
    expect(screen.getByText("Run not found.")).toBeInTheDocument();
  });

  it("rejects a malformed run response instead of rendering it", async () => {
    mockFetch({ "GET /api/runs/run_bad": () => json({ id: "run_bad", state: "weird" }) });
    openRun("run_bad");
    expect(await screen.findByText("Unreadable response")).toBeInTheDocument();
  });
});
