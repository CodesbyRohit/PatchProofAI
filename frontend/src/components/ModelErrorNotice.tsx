import type { Run } from "../api/types";
import { modelErrorInfo } from "../lib/modelErrors";

/** Explains a failed model step, separating account/access problems from anything about the code. */
export function ModelErrorNotice({ run }: { run: Run }) {
  const info = modelErrorInfo(run.model_error_kind);
  if (!info) return null;
  const providerMessage = [...run.attempts].reverse().find((a) => a.error)?.error;
  // A later repair request can fail after an earlier attempt was executed; that attempt's verdict stands.
  const executed = run.attempts.filter((a) => a.candidate_runs.length > 0).map((a) => a.number);
  return (
    <section className={`notice notice--${info.access ? "access" : "warn"}`} role="alert" aria-labelledby="model-error-title">
      <p className="notice__eyebrow">{info.access ? "Access / configuration" : "Model request"}</p>
      <h3 id="model-error-title" className="notice__title">
        {executed.length > 0 ? `A later model request failed (${run.model_error_kind})` : info.title}
      </h3>
      <div className="notice__body">
        {executed.length > 0 ? (
          <p>
            The model request failed after attempt {executed.join(", ")} had already been executed. The verdict above comes
            from that attempt's real test runs; only the later model request was affected.
          </p>
        ) : (
          <p>{info.explanation}</p>
        )}
        {providerMessage && (
          <p className="mono small notice__provider">
            <span className="muted">Recorded message: </span>
            {providerMessage}
          </p>
        )}
        <ol>
          {info.steps.map((s) => <li key={s}>{s}</li>)}
        </ol>
      </div>
    </section>
  );
}
