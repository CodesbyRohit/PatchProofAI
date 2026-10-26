import { useId, useState, type ChangeEvent, type FormEvent } from "react";
import { api, ApiError, UPLOAD_LIMIT_BYTES } from "../api/client";
import type { Example, Project, Status } from "../api/types";
import { ErrorNotice, Loading, Notice } from "../components/Notice";
import { StatusStrip } from "../components/StatusStrip";
import { formatBytes } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { useStartRun } from "../lib/startRun";
import { verdictMeta } from "../lib/verdicts";

const FEATURED = "pagination-off-by-one";

function aiBlocker(status: Status | null): string | null {
  if (!status) return "Backend status is unknown.";
  if (!status.model.configured) return "AI mode needs BHARATCODE_API_KEY on the server.";
  if (status.model.error) return status.model.error;
  if (!status.sandbox.untrusted_execution) return "AI patches need the Docker sandbox, which is unavailable.";
  return null;
}

function ExampleCard({ example, featured, status, runner }: {
  example: Example;
  featured: boolean;
  status: Status | null;
  runner: ReturnType<typeof useStartRun>;
}) {
  const { start, pending, error } = runner;
  const blocker = aiBlocker(status);
  const hintId = useId();
  const busy = pending !== null;
  return (
    <article className={`example ${featured ? "example--featured" : ""}`} aria-labelledby={`${hintId}-title`}>
      <header className="example__head">
        <p className="eyebrow">{example.scenario}</p>
        <h3 id={`${hintId}-title`} className="example__title">{example.title}</h3>
        <p className="example__desc">{example.description}</p>
        <p className="example__files mono small muted">
          {example.summary.source_files.join(", ")} · {example.summary.test_files.length} test file
          {example.summary.test_files.length === 1 ? "" : "s"}
          {example.summary.third_party_imports.length > 0 && ` · imports ${example.summary.third_party_imports.join(", ")}`}
        </p>
      </header>

      <ol className="steps">
        {example.patches.map((p, i) => {
          const key = `${example.id}:${p.id}`;
          const expected = verdictMeta(p.expected_verdict);
          return (
            <li key={p.id} className="step">
              <span className="step__num" aria-hidden="true">{i + 1}</span>
              <div className="step__body">
                <p className="step__label">{p.label}</p>
                <p className="step__desc small">{p.description}</p>
                {expected && (
                  <p className="step__expect small">
                    Maintainers expect: <span className={`chip chip--${expected.tone}`}>{expected.label}</span>
                  </p>
                )}
              </div>
              <button
                type="button"
                className="btn btn--primary"
                disabled={busy}
                onClick={() => start(key, { example_id: example.id, patch_source: "scripted", scripted_patch_id: p.id })}
                aria-label={`Run scripted patch: ${p.label}`}
              >
                {pending === key ? "Starting…" : "Run"}
              </button>
            </li>
          );
        })}
      </ol>

      <div className="example__ai">
        <button
          type="button"
          className="btn btn--ghost"
          disabled={busy || blocker !== null}
          aria-describedby={blocker ? `${hintId}-ai` : undefined}
          onClick={() => start(`${example.id}:ai`, { example_id: example.id, patch_source: "ai" })}
        >
          {pending === `${example.id}:ai` ? "Starting…" : "Diagnose with BharatCode"}
        </button>
        {blocker && <span id={`${hintId}-ai`} className="small muted">{blocker}</span>}
      </div>
      {error && error.key.startsWith(`${example.id}:`) && <ErrorNotice error={error.error} title="Could not start the run" />}
    </article>
  );
}

function UploadPanel({ status }: { status: Status | null }) {
  const [project, setProject] = useState<Project | null>(null);
  const [uploadError, setUploadError] = useState<ApiError | null>(null);
  const [uploading, setUploading] = useState(false);
  const [issue, setIssue] = useState("");
  const [diff, setDiff] = useState("");
  const runner = useStartRun();
  const ids = useId();
  const blocker = aiBlocker(status);
  const noSandbox = status !== null && !status.sandbox.untrusted_execution;

  async function onFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    setProject(null);
    setUploadError(null);
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setUploadError(new ApiError("http", "Only .zip archives are supported for upload.", 422));
      return;
    }
    if (file.size > UPLOAD_LIMIT_BYTES) {
      setUploadError(new ApiError("http", `The archive is ${formatBytes(file.size)}; the limit is ${formatBytes(UPLOAD_LIMIT_BYTES)}.`, 413));
      return;
    }
    setUploading(true);
    try {
      setProject(await api.uploadProject(file));
    } catch (err) {
      setUploadError(err instanceof ApiError ? err : new ApiError("malformed", "Upload failed."));
    } finally {
      setUploading(false);
    }
  }

  function submitDiff(e: FormEvent) {
    e.preventDefault();
    if (!project || !diff.trim()) return;
    void runner.start("user", { project_id: project.id, patch_source: "user", user_patch: diff, issue: issue.trim() || undefined });
  }

  return (
    <div className="upload">
      <div className="upload__drop">
        <label htmlFor={`${ids}-file`} className="upload__label">
          <span className="upload__cta">{uploading ? "Uploading…" : "Choose a .zip of a Python project"}</span>
          <span className="small muted">pytest suite required · up to 4 MB archive, 2 MB extracted, 200 files</span>
        </label>
        <input id={`${ids}-file`} type="file" accept=".zip,application/zip" onChange={onFile} disabled={uploading} className="upload__input" />
      </div>
      {uploadError && <ErrorNotice error={uploadError} title="Upload rejected" />}
      {noSandbox && (
        <Notice tone="warn" title="No sandbox for uploaded code">
          <p>Docker is unavailable, so the backend will block runs of uploaded projects instead of executing them on the host.</p>
        </Notice>
      )}

      {project && (
        <div className="uploaded">
          <p className="eyebrow">Uploaded project</p>
          <h3 className="uploaded__name">{project.name}</h3>
          <dl className="facts">
            <div><dt>Files</dt><dd>{project.summary.file_count} · {formatBytes(project.summary.total_bytes)}</dd></div>
            <div><dt>Source</dt><dd className="mono small">{project.summary.source_files.join(", ") || "none"}</dd></div>
            <div><dt>Tests</dt><dd className="mono small">{project.summary.test_files.join(", ") || "none found"}</dd></div>
            {project.summary.third_party_imports.length > 0 && (
              <div><dt>Third-party</dt><dd className="mono small">{project.summary.third_party_imports.join(", ")}</dd></div>
            )}
          </dl>
          {project.summary.notes.length > 0 && (
            <Notice tone="warn" title="Notes from the backend">
              <ul>{project.summary.notes.map((n) => <li key={n}>{n}</li>)}</ul>
            </Notice>
          )}

          <label htmlFor={`${ids}-issue`} className="field__label">What goes wrong? <span className="muted">(optional, sent to the model in AI mode)</span></label>
          <textarea id={`${ids}-issue`} className="field" rows={3} maxLength={4000} value={issue} onChange={(e) => setIssue(e.target.value)} />

          <div className="uploaded__actions">
            <div>
              <button
                type="button"
                className="btn btn--primary"
                disabled={runner.pending !== null || blocker !== null}
                aria-describedby={blocker ? `${ids}-aihint` : undefined}
                onClick={() => runner.start("ai", { project_id: project.id, patch_source: "ai", issue: issue.trim() || undefined })}
              >
                {runner.pending === "ai" ? "Starting…" : "Diagnose with BharatCode"}
              </button>
              {blocker && <p id={`${ids}-aihint`} className="small muted">{blocker}</p>}
            </div>
            <form onSubmit={submitDiff} className="diffform">
              <label htmlFor={`${ids}-diff`} className="field__label">Or verify your own patch (unified diff)</label>
              <textarea
                id={`${ids}-diff`}
                className="field mono"
                rows={6}
                maxLength={64000}
                placeholder={"--- a/module.py\n+++ b/module.py\n@@ ..."}
                value={diff}
                onChange={(e) => setDiff(e.target.value)}
              />
              <button type="submit" className="btn btn--ghost" disabled={runner.pending !== null || !diff.trim()}>
                {runner.pending === "user" ? "Starting…" : "Verify this diff"}
              </button>
            </form>
          </div>
          {runner.error && <ErrorNotice error={runner.error.error} title="Could not start the run" />}
        </div>
      )}
    </div>
  );
}

export function Workspace() {
  const status = useAsync("status", () => api.status());
  const examples = useAsync("examples", () => api.examples());
  const runner = useStartRun();
  const list = examples.data ?? [];
  const ordered = [...list.filter((e) => e.id === FEATURED), ...list.filter((e) => e.id !== FEATURED)];

  return (
    <div className="page workspace">
      <section className="hero">
        <div className="hero__copy">
          <p className="eyebrow">Evidence-based debugging for Python</p>
          <h1 className="hero__title">
            A patch that looks right <em>is not evidence.</em>
          </h1>
          <p className="hero__lede">
            PatchProof runs your tests before and after a fix in an isolated sandbox, compares every test, and lets
            deterministic rules—never the model—decide whether the patch holds.
          </p>
        </div>
        <StatusStrip status={status.data} error={status.error} loading={status.loading} onRetry={status.reload} />
      </section>

      <section className="section" aria-labelledby="demo-title">
        <div className="section__intro">
          <h2 id="demo-title" className="section__heading"><span className="section__num">A</span>Demo mode</h2>
          <p className="section__lede">
            Bundled projects with <strong>scripted patches</strong> written by the maintainers. They are labelled demo data, never
            model output, and need no API key. Everything after the patch—the sandboxed runs, the comparison, the
            verdict—is real.
          </p>
        </div>
        {examples.loading && !examples.data && <Loading label="Loading bundled examples…" />}
        {/* An access-token prompt is already shown by the status strip above; one form is enough. */}
        {examples.error && examples.error.kind !== "auth" && (
          <ErrorNotice error={examples.error} onRetry={examples.reload} title="Could not load examples" />
        )}
        {examples.data && examples.data.length === 0 && (
          <Notice tone="warn" title="No bundled examples found">
            <p>The backend found no <span className="mono">examples/*/example.json</span>. Check PATCHPROOF_EXAMPLES_DIR.</p>
          </Notice>
        )}
        <div className="examples">
          {ordered.map((ex) => (
            <ExampleCard key={ex.id} example={ex} featured={ex.id === FEATURED} status={status.data} runner={runner} />
          ))}
        </div>
      </section>

      <section className="section" aria-labelledby="upload-title">
        <div className="section__intro">
          <h2 id="upload-title" className="section__heading"><span className="section__num">B</span>Your project</h2>
          <p className="section__lede">
            Upload a zipped Python project with a pytest suite. Uploaded code is treated as hostile: it only runs inside the
            Docker sandbox, with no network access.
          </p>
        </div>
        <UploadPanel status={status.data} />
      </section>
    </div>
  );
}
