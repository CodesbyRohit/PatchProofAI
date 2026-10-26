import { useId, useState, type FormEvent } from "react";
import { ApiError, gh } from "../api/client";
import type { PullRequestPreview, PullRequestRecord, Run } from "../api/types";
import { useSession } from "../lib/session";
import { DiffView } from "./DiffView";
import { ErrorNotice, Notice } from "./Notice";

/** Which repository revision the run used: the evidence is only about this commit. */
export function RepoSourceBlock({ run }: { run: Run }) {
  const source = run.source;
  if (!source) return null;
  return (
    <section className="aside-block" aria-label="Source revision">
      <h3 className="aside-title">Source</h3>
      <p className="mono small">{source.full_name}</p>
      <p className="small">
        {source.ref ? <>Branch <span className="mono">{source.ref}</span>, commit </> : "Commit "}
        <a className="mono" href={source.html_url} title={source.sha}>{source.sha.slice(0, 12)}</a>
        {source.private ? " · private" : " · public"}
      </p>
      {source.skipped_count > 0 && (
        <details className="small">
          <summary>{source.skipped_count} files not used</summary>
          <ul className="plain mono">
            {source.skipped.map((s) => <li key={s}>{s}</li>)}
            {source.skipped_count > source.skipped.length && <li>…and {source.skipped_count - source.skipped.length} more</li>}
          </ul>
        </details>
      )}
    </section>
  );
}

/** Optional, approval-gated: a draft pull request for a verified patch, on a new branch. */
export function PullRequestPanel({ run }: { run: Run }) {
  const { me } = useSession();
  const ids = useId();
  const [preview, setPreview] = useState<PullRequestPreview | null>(null);
  const [record, setRecord] = useState<PullRequestRecord | null>(run.pull_request ?? null);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  if (!run.source || run.verdict?.status !== "VERIFIED_FOR_TESTED_CASES" || me?.kind !== "user") return null;

  if (record?.state === "created" && record.url) {
    return (
      <section className="aside-block" aria-label="Pull request">
        <h3 className="aside-title">Pull request</h3>
        <p className="small">Draft pull request opened from <span className="mono">{record.branch}</span>.</p>
        <p><a href={record.url}>Open pull request #{record.number}</a></p>
      </section>
    );
  }
  if (!me.github.pull_requests) {
    return (
      <section className="aside-block" aria-label="Pull request">
        <h3 className="aside-title">Pull request</h3>
        <p className="small muted">Pull requests are disabled on this instance. Download the patch below and apply it yourself.</p>
      </section>
    );
  }

  async function prepare() {
    setBusy(true);
    setError(null);
    try {
      const p = await gh.pullRequestPreview(run.id);
      setPreview(p);
      setTitle(`Fix: ${run.issue?.split("\n")[0]?.slice(0, 80) || "verified patch from PatchProof"}`);
      setBody(`Verified by PatchProof AI against ${p.source_sha.slice(0, 12)}: ${run.verdict?.headline ?? ""}\n\n` +
        "Verified for the tested cases only; please review before merging.");
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError("malformed", "Could not prepare the pull request."));
    } finally {
      setBusy(false);
    }
  }

  async function create(e: FormEvent) {
    e.preventDefault();
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      setRecord(await gh.createPullRequest(run.id, { title, body, patch_sha256: preview.patch_sha256 }));
    } catch (err) {
      setError(err instanceof ApiError ? err : new ApiError("malformed", "GitHub did not create the pull request."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="aside-block" aria-label="Pull request">
      <h3 className="aside-title">Pull request</h3>
      {!preview && (
        <button type="button" className="btn btn--ghost btn--block" disabled={busy} onClick={() => void prepare()}>
          {busy ? "Preparing…" : "Prepare a draft pull request"}
        </button>
      )}
      {preview && (
        <form className="prform" onSubmit={(e) => void create(e)}>
          <Notice tone="info" title="Review before anything is written">
            <p className="small">
              A new branch <span className="mono">{preview.branch}</span> is created in{" "}
              <span className="mono">{preview.repository}</span> from commit{" "}
              <span className="mono">{preview.source_sha.slice(0, 12)}</span>, with a draft pull request into{" "}
              <span className="mono">{preview.base ?? "the default branch"}</span>. Nothing is merged or pushed elsewhere.
            </p>
          </Notice>
          <p className="small">Changes {preview.changed_files.length} file(s): <span className="mono">{preview.changed_files.join(", ")}</span></p>
          <DiffView diff={preview.diff} label="Patch to be committed" />
          <label className="field__label" htmlFor={`${ids}-title`}>Title</label>
          <input id={`${ids}-title`} className="field" maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} required />
          <label className="field__label" htmlFor={`${ids}-body`}>Description</label>
          <textarea id={`${ids}-body`} className="field" rows={4} maxLength={20000} value={body} onChange={(e) => setBody(e.target.value)} />
          <label className="consent">
            <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} /> I reviewed this patch
          </label>
          <button type="submit" className="btn btn--primary btn--block" disabled={busy || !reviewed || !title.trim()}>
            {busy ? "Creating…" : "Create draft pull request"}
          </button>
        </form>
      )}
      {record?.state === "failed" && <p className="small" role="alert">GitHub did not create the pull request: {record.error}</p>}
      {error && <ErrorNotice error={error} title="Pull request not created" />}
    </section>
  );
}
