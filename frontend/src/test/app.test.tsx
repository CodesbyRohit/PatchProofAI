import { render, screen, within } from "@testing-library/react";
import { App } from "../App";
import { fx, json, mockFetch } from "./fixtures";

const base = {
  "GET /api/status": () => json(fx.status()),
  "GET /api/examples": () => json(fx.examples()),
};

describe("app shell", () => {
  it("names the wordmark link by its visible text", async () => {
    mockFetch(base);
    render(<App />);
    expect(screen.getByRole("link", { name: "PatchProof AI" })).toHaveAttribute("href", "/");
    await screen.findAllByRole("article");
  });

  it("sets a title per route", async () => {
    mockFetch({ ...base, "GET /api/runs": () => json(fx.runs()) });
    render(<App />);
    expect(document.title).toBe("PatchProof AI: evidence before you trust a patch");
    await screen.findAllByRole("article");
  });

  it("titles a run page with its id", async () => {
    const run = fx.minimal();
    mockFetch({ [`GET /api/runs/${run.id}`]: () => json(run), "GET /api/examples": () => json(fx.examples()) });
    window.history.replaceState(null, "", `/runs/${run.id}`);
    render(<App />);
    expect(document.title).toBe(`Run ${run.id} · PatchProof AI`);
    await screen.findByRole("img", { name: /Verdict:/ });
  });

  it("reserves the status panel's rows while checking the backend", async () => {
    let answer: (r: Response) => void = () => {};
    mockFetch({ ...base, "GET /api/status": () => new Promise<Response>((ok) => (answer = ok)) });
    render(<App />);
    const checking = screen.getByRole("status", { name: "Checking backend status" });
    const keys = within(checking).getAllByText(/^(Sandbox|Model|Rules)$/).map((el) => el.textContent);
    expect(keys).toEqual(["Sandbox", "Model", "Rules"]);
    answer(json(fx.status()));
    const panel = await screen.findByRole("region", { name: "Backend status" });
    expect(within(panel).getByText("Sandbox")).toBeInTheDocument();
  });
});
