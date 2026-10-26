import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Me, RepoSummary } from "../api/types";
import { App } from "../App";
import { fx, json, mockFetch } from "./fixtures";

const SHA = "a".repeat(40);
const signedIn: Partial<Me> = {
  authenticated: true, kind: "user", login: "alice", csrf: "csrf-123",
  github: { configured: true, install_url: "https://github.com/apps/patchproof/installations/new", pull_requests: false },
};

function repo(name: string, isPrivate: boolean): RepoSummary {
  return { id: name.length, full_name: name, private: isPrivate, description: null, default_branch: "main", archived: false,
    html_url: `https://github.com/${name}`, pushed_at: null, access: "installed" };
}

function openGitHub(search = "") {
  window.history.replaceState(null, "", `/github${search}`);
  return render(<App />);
}

const header = (init: RequestInit | undefined, name: string) => new Headers(init?.headers).get(name);

describe("GitHub page", () => {
  it("explains when GitHub sign-in is not configured", async () => {
    mockFetch({});
    openGitHub();
    expect(await screen.findByText("GitHub sign-in is not configured on this instance")).toBeInTheDocument();
  });

  it("offers sign-in, and explains a failed one", async () => {
    mockFetch({ "GET /api/auth/me": () => json(fx.me({ github: { configured: true, install_url: null, pull_requests: false } })) });
    openGitHub("?error=not_allowed");
    expect(await screen.findByRole("link", { name: "Sign in with GitHub" })).toHaveAttribute("href", "/api/auth/github/start");
    expect(screen.getByText("This GitHub account is not allowed to use this PatchProof instance.")).toBeInTheDocument();
  });

  it("goes from repository to a pinned, consented run on a private repository", async () => {
    const run = fx.minimal();
    const page1 = Array.from({ length: 100 }, (_, i) => repo(`alice/repo${i}`, i % 2 === 0));
    const { calls } = mockFetch({
      "GET /api/auth/me": () => json(fx.me(signedIn)),
      "GET /api/github/installations": () => json([{ id: 101, account: "alice", repository_selection: "selected" }]),
      "GET /api/github/installations/101/repos": (url) =>
        url.includes("page=1")
          ? json({ repositories: page1, total_count: 101, page: 1, has_more: true })
          : json({ repositories: [repo("alice/private-app", true)], total_count: 101, page: 2, has_more: false }),
      "GET /api/github/branches": () => json({ branches: [{ name: "main", sha: SHA, protected: false }], has_more: false }),
      "GET /api/github/commit": () => json({ sha: SHA, message: "Add calc", author: "Alice", date: "2026-10-01T00:00:00Z", html_url: null }),
      "POST /api/github/runs": () => json({ ...run, state: "queued" }, 202),
      [`GET /api/runs/${run.id}`]: () => json(run),
      "GET /api/examples": () => json(fx.examples()),
    });
    const user = userEvent.setup();
    openGitHub();

    expect(await screen.findByText("@alice", { selector: "strong" })).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Show repositories" }));
    expect(await screen.findByText(/Showing 100 of 100 loaded \(more available\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await user.type(screen.getByLabelText("Filter loaded repositories"), "private-app");
    const pick = await screen.findByRole("button", { name: /alice\/private-app/ });
    expect(within(pick).getByText("private")).toBeInTheDocument();
    await user.click(pick);

    expect(await screen.findByText("Add calc", { exact: false })).toBeInTheDocument();
    expect(screen.getByTitle(SHA)).toHaveTextContent(SHA.slice(0, 12));
    const startButton = screen.getByRole("button", { name: "Start verification" });
    expect(startButton).toBeDisabled(); // AI on a private repository needs consent first
    await user.click(screen.getByRole("checkbox", { name: /sent to the BharatCode model API/ }));
    await user.type(screen.getByLabelText(/Bug description/), "add() subtracts");
    await user.click(startButton);

    expect(await screen.findByRole("img", { name: /Verdict:/ })).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST" && c.path === "/api/github/runs");
    expect(JSON.parse(String(post?.init?.body))).toEqual({
      repository: "alice/private-app", ref: "main", sha: SHA, patch_source: "ai", allow_model_upload: true,
      issue: "add() subtracts",
    });
    expect(header(post?.init, "X-CSRF-Token")).toBe("csrf-123");
    expect(header(post?.init, "Idempotency-Key")).toBeTruthy();
  });

  it("looks up a public repository and verifies a user diff without consent", async () => {
    const { calls } = mockFetch({
      "GET /api/auth/me": () => json(fx.me(signedIn)),
      "GET /api/github/installations": () => json([]),
      "GET /api/github/repo": () => json({ ...repo("octo/public-lib", false), access: "public" }),
      "GET /api/github/branches": () => json({ branches: [{ name: "main", sha: SHA, protected: false }], has_more: false }),
      "GET /api/github/commit": () => json({ sha: SHA, message: "m", author: null, date: null, html_url: null }),
      "POST /api/github/runs": () => json({ detail: "Too many runs. Please wait a minute." }, 429),
    });
    const user = userEvent.setup();
    openGitHub();
    expect(await screen.findByText("The PatchProof app is not installed yet")).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Or a public repository/), "octo/public-lib");
    await user.click(screen.getByRole("button", { name: "Use" }));
    expect(await screen.findByText(/Public repository, read without credentials/)).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Verify my own diff" }));
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Unified diff"), "--- a/x");
    await screen.findByTitle(SHA);
    await user.click(screen.getByRole("button", { name: "Start verification" }));
    expect(await screen.findByText("Too many runs. Please wait a minute.")).toBeInTheDocument();
    const body = JSON.parse(String(calls.find((c) => c.method === "POST")?.init?.body));
    expect(body).toMatchObject({ repository: "octo/public-lib", patch_source: "user", user_patch: "--- a/x", allow_model_upload: false });
  });

  it("signs out with the CSRF token", async () => {
    const { calls } = mockFetch({
      "GET /api/auth/me": () => json(fx.me(signedIn)),
      "GET /api/github/installations": () => json([]),
      "POST /api/auth/logout": () => json({ ok: true }),
    });
    openGitHub();
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    const post = calls.find((c) => c.path === "/api/auth/logout");
    expect(header(post?.init, "X-CSRF-Token")).toBe("csrf-123");
  });
});

describe("run page for a repository", () => {
  function verifiedRepoRun() {
    const run = fx.minimal();
    return { ...run, source: { provider: "github" as const, repo_id: 11, full_name: "alice/private-app", private: true,
      ref: "main", sha: SHA, html_url: `https://github.com/alice/private-app/tree/${SHA}`, skipped_count: 2,
      skipped: ["logo.png: file type not used by PatchProof", "link.py: symbolic link"] } };
  }

  it("shows the pinned source and that pull requests are disabled", async () => {
    const run = verifiedRepoRun();
    mockFetch({ "GET /api/auth/me": () => json(fx.me(signedIn)), [`GET /api/runs/${run.id}`]: () => json(run),
      "GET /api/examples": () => json(fx.examples()) });
    window.history.replaceState(null, "", `/runs/${run.id}`);
    render(<App />);
    const source = await screen.findByRole("region", { name: "Source revision" });
    expect(within(source).getByTitle(SHA)).toHaveAttribute("href", run.source.html_url);
    expect(within(source).getByText("2 files not used")).toBeInTheDocument();
    expect(await screen.findByText(/Pull requests are disabled on this instance/)).toBeInTheDocument();
  });

  it("creates a draft pull request only after the patch is reviewed", async () => {
    const run = verifiedRepoRun();
    const diff = run.attempts[0]!.patch!.diff;
    const { calls } = mockFetch({
      "GET /api/auth/me": () => json(fx.me({ ...signedIn, github: { ...signedIn.github!, pull_requests: true } })),
      [`GET /api/runs/${run.id}`]: () => json(run),
      "GET /api/examples": () => json(fx.examples()),
      [`GET /api/runs/${run.id}/pull-request`]: () => json({ enabled: true, repository: "alice/private-app", source_sha: SHA,
        base: "main", branch: `patchproof/${run.id}`, changed_files: ["calc.py"], diff, patch_sha256: "f".repeat(64), existing: null }),
      [`POST /api/runs/${run.id}/pull-request`]: () => json({ state: "created", branch: `patchproof/${run.id}`, base: "main",
        patch_sha256: "f".repeat(64), url: "https://github.com/alice/private-app/pull/7", number: 7, error: null, created_at: 1 }),
    });
    window.history.replaceState(null, "", `/runs/${run.id}`);
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Prepare a draft pull request" }));
    expect(await screen.findByLabelText("Patch to be committed")).toBeInTheDocument();
    const create = screen.getByRole("button", { name: "Create draft pull request" });
    expect(create).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "I reviewed this patch" }));
    await user.click(create);
    expect(await screen.findByRole("link", { name: "Open pull request #7" })).toHaveAttribute("href", "https://github.com/alice/private-app/pull/7");
    const post = calls.find((c) => c.method === "POST");
    expect(JSON.parse(String(post?.init?.body)).patch_sha256).toBe("f".repeat(64));
    expect(header(post?.init, "X-CSRF-Token")).toBe("csrf-123");
  });
});
