import type { ApiError } from "../api/client";
import type { Status } from "../api/types";
import { ErrorNotice } from "./Notice";

export function StatusStrip({ status, error, loading, onRetry }: {
  status: Status | null;
  error: ApiError | null;
  loading: boolean;
  onRetry: () => void;
}) {
  if (error) return <ErrorNotice error={error} onRetry={onRetry} title="Backend status unavailable" />;
  if (loading && !status) {
    // Same rows as the loaded panel, so the page below does not move when the answer arrives.
    return (
      <section className="status status--loading" role="status" aria-label="Checking backend status">
        {["Sandbox", "Model", "Rules"].map((key) => (
          <div key={key} className="status__item">
            <span className="status__key">{key}</span>
            <span className="status__value">Checking…</span>
          </div>
        ))}
      </section>
    );
  }
  if (!status) return null;

  const { sandbox, model } = status;
  const sandboxTone = sandbox.docker_available ? "good" : "warn";
  let modelTone = "warn";
  let modelText = "Not configured. AI mode is off; demo mode works without a key.";
  if (model.configured && model.error) {
    modelTone = "bad";
    modelText = model.error;
  } else if (model.configured && model.model_available === false) {
    modelTone = "bad";
    modelText = `${model.model} is not in the model catalog.`;
  } else if (model.configured) {
    modelTone = "good";
    modelText = `${model.model} key set, listed in the ${model.catalog_source ?? "model"} catalog. Generation access is only confirmed when a run calls the model.`;
  }

  return (
    <section className="status" aria-label="Backend status">
      <div className={`status__item status__item--${sandboxTone}`}>
        <span className="status__key">Sandbox</span>
        <span className="status__value">
          {sandbox.docker_available
            ? `Docker isolation ready · ${sandbox.detail}`
            : `Docker unavailable. Uploads and AI patches will be blocked; bundled demos run as a local subprocess. ${sandbox.detail}`}
        </span>
      </div>
      <div className={`status__item status__item--${modelTone}`}>
        <span className="status__key">Model</span>
        <span className="status__value">{modelText}</span>
      </div>
      <div className="status__item">
        <span className="status__key">Rules</span>
        <span className="status__value">
          {status.limits.verification_runs}× baseline, {status.limits.verification_runs}× patched, up to {status.limits.max_ai_attempts} AI attempts, {status.limits.run_timeout_s}s per run
        </span>
      </div>
      {sandbox.unsandboxed_override && (
        <div className="status__item status__item--bad">
          <span className="status__key">Warning</span>
          <span className="status__value">PATCHPROOF_ALLOW_UNSANDBOXED is on: untrusted code runs on the host without isolation.</span>
        </div>
      )}
    </section>
  );
}
