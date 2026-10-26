import { useState } from "react";
import { api, ApiError, downloads } from "../api/client";
import type { Run } from "../api/types";
import { AttemptEvidence } from "../components/AttemptEvidence";
import { ModelErrorNotice } from "../components/ModelErrorNotice";
import { ErrorNotice, Loading, Notice } from "../components/Notice";
import { Pipeline } from "../components/Pipeline";
import { SourceBadge } from "../components/SourceBadge";
import { TestLedger } from "../components/TestLedger";
import { VerdictStamp } from "../components/VerdictStamp";
import { formatDuration, formatTime } from "../lib/format";
import { useAsync, useRun } from "../lib/hooks";
import { useStartRun } from "../lib/startRun";
import { verdictMeta } from "../lib/verdicts";
import { Link } from "../router";

function VerdictPanel({ run }: { run: Run }) {
  const v = run.verdict;
  if (!v) return null;
  const meta = verdictMeta(v.status);
  return (
    <section className={`verdict verdict--${meta?.tone ?? "inconclusive"}`} aria-labelledby="verdict-headline">
      <VerdictStamp verdict={v} />
      <div className="verdict__text">
        <h2 id="verdict-headline" className="verdict__headline">{v.headline}</h2>
        {v.reasons.length > 0 && (
          <ul className="verdict__reasons">{v.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        )}
        {meta && <p className="verdict__meaning small">{meta.meaning}</p>}
        <p className="verdict__by small muted">Decided by {v.decided_by}. The model never decides the verdict.</p>
      </div>
    </section>
  );
}

function LiveBanner({ run, polling }: { run: Run; polling: boolean }) {
  if (run.state !== "queued" && run.state !== "running") return null;
  const last = run.events[run.events.length - 1];
  const elapsed = last ? last.at - run.created_at : 0;
  return (
    <div className="live" role="status" aria-live="polite">
      <span className="live__dot" aria-hidden="true" />
      <span>
        <strong>{run.state === "queued" ? "Queued" : "Running"}</strong>
        {last ? ` · ${last.message}` : " · waiting for the first event"}
      </span>
      <span className="muted small mono">
        {elapsed > 0 ? `${formatDuration(elapsed)} in` : ""} {polling ? "· polling backend" : ""}
      </span>
    </div>
  );
}

function ContinueDemo({ run }: { run: Run }) {
  const exampleId = run.project_id.startsWith("example:") ? run.project_id.slice("example:".length) : null;
  const examples = useAsync(exampleId ?? "", () => (exampleId ? api.examples() : Promise.resolve([])));
  const { start, pending, error } = useStartRun();
  const example = examples.data?.find((e) => e.id === exampleId);
  if (!example || run.state === "queued" || run.state === "running") return null;
  const current = run.attempts[0]?.patch?.label;
  return (
    <section className="aside-block">
      <h3 className="aside-title">Same project, other patches</h3>
      <p className="small muted">Scripted demo patches for “{example.title}”.</p>
      <ul className="plain continue">
        {example.patches.map((p) => (
          <li key={p.id}>
            <button
              type="button"
              className="btn btn--ghost btn--block"
              disabled={pending !== null || (run.patch_source === "scripted" && p.label === current)}
              onClick={() => start(p.id, { example_id: example.id, patch_source: "scripted", scripted_patch_id: p.id })}
            >
              {pending === p.id ? "Starting…" : p.label}
              {run.patch_source === "scripted" && p.label === current && <span className="small muted"> (this run)</span>}
            </button>
          </li>
        ))}
      </ul>
      {error && <ErrorNotice error={error.error} title="Could not start the run" />}
    </section>
  );
}

function Downloads({ run }: { run: Run }) {
  const hasDiff = run.attempts.some((a) => a.patch?.diff);
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);

  // Fetched rather than linked, so the request carries the access token of a hosted instance.
  async function save(path: string, name: string) {
    setBusy(name);
    setFailure(null);
    try {
      await api.download(path, name);
    } catch (e) {
      setFailure(e instanceof ApiError ? e : new ApiError("malformed", "The download failed."));
    } finally {
      setBusy(null);
    }
  }

  const items = [
    ...(hasDiff ? [{ path: downloads.patch(run.id), name: "patch.diff", what: "exact diff applied" }] : []),
    { path: downloads.markdown(run.id), name: "report.md", what: "verification report" },
    { path: downloads.json(run.id), name: "report.json", what: "full record with raw logs" },
  ];
  return (
    <section className="aside-block">
      <h3 className="aside-title">Reports</h3>
      <ul className="plain downloads">
        {items.map((item) => (
          <li key={item.name}>
            <button type="button" className="download" disabled={busy !== null} onClick={() => void save(item.path, item.name)}>
              {item.name} <span>{busy === item.name ? "downloading…" : item.what}</span>
            </button>
          </li>
        ))}
      </ul>
      {failure && <ErrorNotice error={failure} title="Download failed" />}
    </section>
  );
}

function RawLogs({ run }: { run: Run }) {
  const runs = [...run.baseline_runs, ...run.attempts.flatMap((a) => a.candidate_runs)];
  if (!runs.length) return null;
  return (
    <section className="section">
      <h3 className="section__title"><span className="section__num">04</span>Raw test output</h3>
      {runs.map((r) => (
        <details key={r.label} className="rawlog">
          <summary>
            <span>{r.label}</span>
            <span className="mono small muted">
              {r.sandbox}{r.isolated ? " · isolated" : " · NOT isolated"} · {r.health} · exit {r.exit_code ?? "—"} · {formatDuration(r.duration_s)}
            </span>
          </summary>
          {r.detail && <p className="small">{r.detail}</p>}
          <pre className="log">{r.stdout || "(no stdout)"}</pre>
          {r.stderr && <pre className="log log--err">{r.stderr}</pre>}
          <p className="small muted mono command">{r.command.join(" ")}</p>
        </details>
      ))}
    </section>
  );
}

export function RunPage({ id }: { id: string }) {
  const { data: run, error, loading, polling, reload } = useRun(id);

  if (!run) {
    if (error) {
      return (
        <div className="page">
          <ErrorNotice error={error} onRetry={reload} title={error.status === 404 ? "Run not found" : undefined} />
          <p><Link to="/history">See run history</Link></p>
        </div>
      );
    }
    return <div className="page">{loading && <Loading label="Loading run from the backend…" />}</div>;
  }

  const attempts = run.attempts;
  return (
    <div className="page run">
      <header className="run__head">
        <p className="eyebrow mono">
          {run.id} · started {formatTime(run.created_at)}
          {run.finished_at ? ` · took ${formatDuration(run.finished_at - run.created_at)}` : ""}
        </p>
        <h1 className="run__title">{run.project_name}</h1>
        <div className="run__badges">
          <SourceBadge source={run.patch_source} />
          {run.sandbox && (
            <span className={`source ${run.sandbox_isolated ? "" : "source--danger"}`}>
              {run.sandbox_isolated ? "Docker sandbox · isolated" : `${run.sandbox} · not isolated`}
            </span>
          )}
        </div>
      </header>

      {run.patch_source === "scripted" && (
        <Notice tone="info" title="Demo data: scripted patch">
          <p>
            This patch was written by the PatchProof maintainers and ships with the example. It is <strong>not</strong> model output.
            The test runs, comparison and verdict below are real results from executing it.
          </p>
        </Notice>
      )}

      {error && <ErrorNotice error={error} onRetry={reload} title="Lost contact with the backend while polling" />}
      <LiveBanner run={run} polling={polling} />
      {run.state === "error" && (
        <Notice tone="bad" role="alert" title="This run did not finish">
          <p>{run.error ?? "The backend reported an error."} No verdict was reached, so nothing here should be read as a result.</p>
        </Notice>
      )}

      <div className="run__grid">
        <div className="run__main">
          <VerdictPanel run={run} />
          <ModelErrorNotice run={run} />

          {run.issue && (
            <section className="issue">
              <p className="eyebrow">Reported issue</p>
              <p>{run.issue}</p>
            </section>
          )}

          <section className="section">
            <h3 className="section__title"><span className="section__num">01</span>Test evidence: baseline vs. patched</h3>
            {run.baseline && !run.baseline.usable && (
              <Notice tone="warn" title="The baseline could not be used">
                <ul>{run.baseline.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              </Notice>
            )}
            {run.baseline && run.baseline.flaky.length > 0 && (
              <Notice tone="warn" title="Flaky tests excluded">
                <p className="mono small">{run.baseline.flaky.join(", ")}</p>
              </Notice>
            )}
            <TestLedger run={run} />
          </section>

          <section className="section">
            <h3 className="section__title"><span className="section__num">02</span>Diagnosis and patch</h3>
            {attempts.length === 0 ? (
              <p className="muted">
                {run.state === "finished" ? "No patch was produced for this run." : "Waiting for the baseline to finish."}
              </p>
            ) : (
              attempts.map((a) => <AttemptEvidence key={a.number} attempt={a} multiple={attempts.length > 1} />)
            )}
          </section>

          {run.disclosures.length > 0 && (
            <section className="section">
              <h3 className="section__title"><span className="section__num">03</span>Disclosures</h3>
              <ul>{run.disclosures.map((d) => <li key={d}>{d}</li>)}</ul>
            </section>
          )}

          <RawLogs run={run} />
        </div>

        <aside className="run__aside">
          <section className="aside-block">
            <h3 className="aside-title">Pipeline</h3>
            <Pipeline run={run} />
          </section>
          <Downloads run={run} />
          <ContinueDemo run={run} />
        </aside>
      </div>
    </div>
  );
}
