import { useId, useRef, useState, type FormEvent } from "react";
import { ApiError, auth, gh } from "../api/client";
import type { CommitInfo, Me, RepoSummary } from "../api/types";
import { ErrorNotice, Loading, Notice } from "../components/Notice";
import { useAsync } from "../lib/hooks";
import { useSession } from "../lib/session";
import { navigate } from "../router";

const SIGN_IN_ERRORS: Record<string, string> = {
  state: "The sign-in could not be verified (it may have been started in another tab or expired). Please try again.",
  not_allowed: "This GitHub account is not allowed to use this PatchProof instance.",
  denied: "GitHub did not complete the authorization. Nothing was connected.",
  unavailable: "GitHub could not be reached to finish signing in. Please try again.",
};

export function GitHubPage() {
  const { me, error, loading, reload } = useSession();
  const signInError = new URLSearchParams(window.location.search).get("error");

  return (
    <div className="page github">
      <header className="page__head">
        <p className="eyebrow">Repositories</p>
        <h1 className="page__title">Verify a fix on your GitHub repository</h1>
        <p className="section__lede">
          PatchProof fetches one exact commit, runs its tests before and after the patch in the Docker sandbox, and shows
          the evidence. It never pushes to your repository unless you approve a draft pull request for a verified patch.
        </p>
      </header>
      {signInError && (
        <Notice tone="bad" role="alert" title="Not signed in">
          <p>{SIGN_IN_ERRORS[signInError] ?? SIGN_IN_ERRORS.unavailable}</p>
        </Notice>
      )}
      {loading && !me && <Loading label="Checking your session…" />}
      {error && <ErrorNotice error={error} onRetry={reload} title="Could not check your session" />}
      {me && <GitHubBody me={me} />}
    </div>
  );
}

function GitHubBody({ me }: { me: Me }) {
  if (!me.github.configured) {
    return (
      <Notice tone="warn" title="GitHub sign-in is not configured on this instance">
        <p>
          The operator has not registered a GitHub App for this PatchProof server. Uploads and bundled examples still work
          from the workspace.
        </p>
      </Notice>
    );
  }
  if (me.kind !== "user") return <ConnectCard />;
  return <RepoWorkspace me={me} />;
}

function ConnectCard() {
  return (
    <section className="connect">
      <h2 className="section__heading">Connect GitHub</h2>
      <ul className="small">
        <li>PatchProof asks GitHub who you are and lists the repositories where you installed the PatchProof app.</li>
        <li>Public repositories can be used without installing the app; private ones only where it is installed.</li>
        <li>Your GitHub token stays on the PatchProof server, encrypted. It never reaches the sandbox or the AI model.</li>
      </ul>
      <a className="btn btn--primary" href={auth.signInUrl()}>Sign in with GitHub</a>
    </section>
  );
}

function RepoWorkspace({ me }: { me: Me }) {
  const { reload } = useSession();
  const [selected, setSelected] = useState<RepoSummary | null>(null);
  const [leaving, setLeaving] = useState<ApiError | null>(null);

  async function signOut(disconnect: boolean) {
    try {
      await (disconnect ? auth.disconnect() : auth.signOut());
      reload();
    } catch (e) {
      setLeaving(e instanceof ApiError ? e : new ApiError("malformed", "Could not sign out."));
    }
  }

  return (
    <>
      <section className="account" aria-label="GitHub account">
        <p>Signed in as <strong>@{me.login}</strong></p>
        <div className="account__actions">
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => void signOut(false)}>Sign out</button>
          <button type="button" className="btn btn--ghost btn--sm" onClick={() => void signOut(true)}>
            Disconnect GitHub
          </button>
        </div>
        {leaving && <ErrorNotice error={leaving} />}
      </section>
      <div className="github__grid">
        <RepoPicker me={me} selected={selected} onSelect={setSelected} />
        {selected ? <RunForm key={selected.full_name} repo={selected} /> : (
          <p className="muted small github__hint">Choose a repository to continue.</p>
        )}
      </div>
    </>
  );
}

function RepoPicker({ me, selected, onSelect }: { me: Me; selected: RepoSummary | null; onSelect: (r: RepoSummary) => void }) {
  const ids = useId();
  const installs = useAsync("gh:installations", () => gh.installations());
  const [installation, setInstallation] = useState<number | null>(null);
  const active = installation ?? installs.data?.[0]?.id ?? null;
  const [pages, setPages] = useState<{ key: number | null; items: RepoSummary[]; page: number; more: boolean }>(
    { key: null, items: [], page: 0, more: true });
  const [loadingMore, setLoadingMore] = useState(false);
  const [listError, setListError] = useState<ApiError | null>(null);
  const [filter, setFilter] = useState("");
  const [publicName, setPublicName] = useState("");
  const [lookupError, setLookupError] = useState<ApiError | null>(null);

  const current = pages.key === active ? pages : { key: active, items: [], page: 0, more: true };

  async function loadMore() {
    if (active === null) return;
    setLoadingMore(true);
    setListError(null);
    try {
      const next = await gh.repos(active, current.page + 1);
      setPages({ key: active, items: [...current.items, ...next.repositories], page: current.page + 1, more: next.has_more });
    } catch (e) {
      setListError(e instanceof ApiError ? e : new ApiError("malformed", "Could not list repositories."));
    } finally {
      setLoadingMore(false);
    }
  }

  async function lookUp(e: FormEvent) {
    e.preventDefault();
    setLookupError(null);
    try {
      onSelect(await gh.repo(publicName.trim()));
    } catch (err) {
      setLookupError(err instanceof ApiError ? err : new ApiError("malformed", "Could not find that repository."));
    }
  }

  const shown = current.items.filter((r) => r.full_name.toLowerCase().includes(filter.trim().toLowerCase()));

  return (
    <section className="picker" aria-labelledby={`${ids}-title`}>
      <h2 id={`${ids}-title`} className="section__heading">1 · Repository</h2>
      {installs.loading && !installs.data && <Loading label="Loading your app installations…" />}
      {installs.error && <ErrorNotice error={installs.error} onRetry={installs.reload} title="Could not list installations" />}
      {installs.data && installs.data.length === 0 && (
        <Notice tone="info" title="The PatchProof app is not installed yet">
          <p>Install it on the repositories you want to verify, then come back. You can also use any public repository below.</p>
          {me.github.install_url && <p><a href={me.github.install_url}>Install the PatchProof app on GitHub</a></p>}
        </Notice>
      )}
      {installs.data && installs.data.length > 1 && (
        <>
          <label className="field__label" htmlFor={`${ids}-inst`}>Account</label>
          <select id={`${ids}-inst`} className="field" value={active ?? ""} onChange={(e) => setInstallation(Number(e.target.value))}>
            {installs.data.map((i) => <option key={i.id} value={i.id}>{i.account}</option>)}
          </select>
        </>
      )}
      {active !== null && (
        <>
          {current.page === 0 && !loadingMore && !listError && (
            <button type="button" className="btn btn--ghost" onClick={() => void loadMore()}>Show repositories</button>
          )}
          {current.page > 0 && (
            <>
              <label className="field__label" htmlFor={`${ids}-filter`}>Filter loaded repositories</label>
              <input id={`${ids}-filter`} className="field" value={filter} onChange={(e) => setFilter(e.target.value)} />
              <p className="small muted" aria-live="polite">
                Showing {shown.length} of {current.items.length} loaded{current.more ? " (more available)" : ""}.
              </p>
              <ul className="plain repos">
                {shown.map((r) => (
                  <li key={r.id}>
                    <button type="button" className={`repo ${selected?.full_name === r.full_name ? "repo--selected" : ""}`}
                      aria-pressed={selected?.full_name === r.full_name} onClick={() => onSelect(r)}>
                      <span className="mono">{r.full_name}</span>
                      <span className={`chip ${r.private ? "chip--private" : "chip--quiet"}`}>{r.private ? "private" : "public"}</span>
                      {r.archived && <span className="chip chip--quiet">archived</span>}
                    </button>
                  </li>
                ))}
              </ul>
              {current.more && (
                <button type="button" className="btn btn--ghost btn--sm" disabled={loadingMore} onClick={() => void loadMore()}>
                  {loadingMore ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          )}
          {loadingMore && current.page === 0 && <Loading label="Loading repositories…" />}
          {listError && <ErrorNotice error={listError} onRetry={() => void loadMore()} title="Could not list repositories" />}
        </>
      )}
      <form className="lookup" onSubmit={(e) => void lookUp(e)}>
        <label className="field__label" htmlFor={`${ids}-public`}>Or a public repository (owner/name)</label>
        <div className="access-form__row">
          <input id={`${ids}-public`} className="field mono" placeholder="octocat/hello-world" value={publicName}
            onChange={(e) => setPublicName(e.target.value)} spellCheck={false} autoComplete="off" />
          <button type="submit" className="btn btn--ghost" disabled={!/^[^/\s]+\/[^/\s]+$/.test(publicName.trim())}>Use</button>
        </div>
      </form>
      {lookupError && <ErrorNotice error={lookupError} title="Repository not available" />}
    </section>
  );
}

function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function RunForm({ repo }: { repo: RepoSummary }) {
  const ids = useId();
  const branches = useAsync(`gh:branches:${repo.full_name}`, () => gh.branches(repo.full_name));
  const [branch, setBranch] = useState<string | null>(null);
  const ref = branch ?? repo.default_branch ?? branches.data?.branches[0]?.name ?? null;
  const commit = useAsync<CommitInfo | null>(`gh:commit:${repo.full_name}:${ref}`, () =>
    ref ? gh.commit(repo.full_name, ref) : Promise.resolve(null));
  const [mode, setMode] = useState<"ai" | "user">("ai");
  const [issue, setIssue] = useState("");
  const [diff, setDiff] = useState("");
  const [consent, setConsent] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const retryKey = useRef<{ body: string; key: string } | null>(null);

  const sha = commit.data?.sha ?? null;
  const needsConsent = repo.private && mode === "ai";
  const ready = sha && !pending && (mode === "ai" ? !needsConsent || consent : diff.trim().length > 0);

  async function start(e: FormEvent) {
    e.preventDefault();
    if (!sha) return;
    const body = {
      repository: repo.full_name, ref, sha, patch_source: mode, allow_model_upload: needsConsent && consent,
      ...(issue.trim() ? { issue: issue.trim() } : {}), ...(mode === "user" ? { user_patch: diff } : {}),
    };
    const serialized = JSON.stringify(body);
    // A start whose answer was lost is retried with the same key, so the backend does not start it twice.
    const key = retryKey.current?.body === serialized ? retryKey.current.key : newKey();
    setPending(true);
    setError(null);
    try {
      const run = await gh.createRun(body, key);
      retryKey.current = null;
      navigate(`/runs/${run.id}`);
    } catch (err) {
      const apiErr = err instanceof ApiError ? err : new ApiError("malformed", "Could not start the run.");
      retryKey.current = apiErr.kind === "network" || apiErr.kind === "timeout" ? { body: serialized, key } : null;
      setError(apiErr);
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="runform" onSubmit={(e) => void start(e)} aria-labelledby={`${ids}-title`}>
      <h2 id={`${ids}-title`} className="section__heading">2 · {repo.full_name}</h2>
      <p className="small muted">
        {repo.private ? "Private" : "Public"} repository{repo.access === "public" ? ", read without credentials" : ""}.
        {repo.description ? ` ${repo.description}` : ""}
      </p>

      <label className="field__label" htmlFor={`${ids}-branch`}>Branch</label>
      {branches.error && <ErrorNotice error={branches.error} onRetry={branches.reload} title="Could not list branches" />}
      <select id={`${ids}-branch`} className="field" value={ref ?? ""} onChange={(e) => setBranch(e.target.value)}
        disabled={!branches.data}>
        {(branches.data?.branches ?? (ref ? [{ name: ref }] : [])).map((b) => <option key={b.name} value={b.name}>{b.name}</option>)}
      </select>

      <div className="commit" aria-live="polite">
        {commit.loading && <Loading label="Resolving the commit…" />}
        {commit.error && <ErrorNotice error={commit.error} onRetry={commit.reload} title="Could not resolve the branch" />}
        {commit.data && (
          <p className="small">
            The run is pinned to commit{" "}
            <a className="mono" href={commit.data.html_url ?? undefined} title={commit.data.sha}>{commit.data.sha.slice(0, 12)}</a>
            {commit.data.message ? <> · {commit.data.message}</> : null}
            {commit.data.author ? <span className="muted"> · {commit.data.author}</span> : null}
            {commit.data.date ? <span className="muted"> · {new Date(commit.data.date).toLocaleString()}</span> : null}
          </p>
        )}
      </div>

      <label className="field__label" htmlFor={`${ids}-issue`}>Bug description, traceback or failing test (optional)</label>
      <textarea id={`${ids}-issue`} className="field" rows={4} maxLength={4000} value={issue} onChange={(e) => setIssue(e.target.value)} />

      <fieldset className="mode">
        <legend className="field__label">Patch</legend>
        <label><input type="radio" name={`${ids}-mode`} checked={mode === "ai"} onChange={() => setMode("ai")} /> Ask BharatCode for a patch</label>
        <label><input type="radio" name={`${ids}-mode`} checked={mode === "user"} onChange={() => setMode("user")} /> Verify my own diff</label>
      </fieldset>
      {mode === "user" && (
        <>
          <label className="field__label" htmlFor={`${ids}-diff`}>Unified diff</label>
          <textarea id={`${ids}-diff`} className="field mono" rows={6} maxLength={64000} value={diff} onChange={(e) => setDiff(e.target.value)} />
        </>
      )}
      {needsConsent && (
        <label className="consent">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />{" "}
          I agree that selected files of this private repository (failing tests and the source they use) are sent to the
          BharatCode model API to generate a patch.
        </label>
      )}
      <button type="submit" className="btn btn--primary" disabled={!ready}>{pending ? "Starting…" : "Start verification"}</button>
      {error && <ErrorNotice error={error} title="Could not start the run" />}
    </form>
  );
}
