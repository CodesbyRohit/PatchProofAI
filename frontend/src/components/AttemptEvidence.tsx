import type { Attempt } from "../api/types";
import { DiffView } from "./DiffView";

const PROVENANCE: Record<Attempt["diagnosis_source"], string> = {
  live_model: "BharatCode model output",
  user_supplied: "User-supplied patch",
  scripted_example: "Scripted demo patch (maintainer-written, not model output)",
  benchmark_fixture: "Benchmark fixture",
};

export function AttemptEvidence({ attempt, multiple }: { attempt: Attempt; multiple: boolean }) {
  const { diagnosis, patch } = attempt;
  return (
    <article className="attempt" aria-label={`Attempt ${attempt.number}`}>
      <header className="attempt__head">
        {multiple && <span className="attempt__num">Attempt {attempt.number}</span>}
        <span className="attempt__source">{PROVENANCE[attempt.diagnosis_source]}</span>
      </header>

      {attempt.error && (
        <p className="attempt__error">
          <strong>No usable patch:</strong> {attempt.error}
        </p>
      )}

      {diagnosis ? (
        <div className="diagnosis">
          <h4>Root cause</h4>
          <p className="diagnosis__summary">{diagnosis.summary}</p>
          <p>{diagnosis.root_cause}</p>
          {diagnosis.evidence.length > 0 && (
            <>
              <h4>Cited evidence</h4>
              <ul className="evidence">
                {diagnosis.evidence.map((e, i) => (
                  <li key={i}>
                    <span className="mono evidence__ref">
                      {e.file}
                      {e.lines ? `:${e.lines}` : ""}
                    </span>
                    <span>{e.observation}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
          {diagnosis.rationale && (
            <>
              <h4>Why this patch</h4>
              <p>{diagnosis.rationale}</p>
            </>
          )}
          {diagnosis.potential_regressions.length > 0 && (
            <>
              <h4>Regressions the model anticipated</h4>
              <ul>{diagnosis.potential_regressions.map((r) => <li key={r}>{r}</li>)}</ul>
            </>
          )}
          <p className="diagnosis__confidence small">
            Model's self-reported confidence: <strong>{diagnosis.confidence}</strong>
            {diagnosis.confidence_explanation ? ` — ${diagnosis.confidence_explanation}` : ""}. It has no effect on the verdict.
          </p>
        </div>
      ) : (
        attempt.diagnosis_source === "scripted_example" && (
          <p className="muted small">
            Scripted demo patches come without a diagnosis. The patch below is a fixed example bundled with PatchProof.
          </p>
        )
      )}

      {patch && (
        <div className="patch">
          <h4>
            Patch: {patch.label}{" "}
            <span className={`chip chip--${patch.applied ? "quiet" : "blocked"}`}>
              {patch.applied ? `applied to a disposable copy · ${patch.changed_files.join(", ")}` : "not applied"}
            </span>
          </h4>
          {patch.policy_findings.length > 0 && (
            <ul className="findings" aria-label="Policy findings">
              {patch.policy_findings.map((f, i) => (
                <li key={i} className={`finding finding--${f.severity}`}>
                  <span className="finding__rule mono">{f.rule}</span>
                  <span>{f.message}</span>
                  {f.file && <span className="mono muted small">{f.file}</span>}
                </li>
              ))}
            </ul>
          )}
          {patch.diff ? (
            <DiffView diff={patch.diff} label={`Unified diff for ${patch.label}`} />
          ) : patch.edits.length > 0 ? (
            <div className="edits">
              <p className="small muted">No diff was produced. The proposed search/replace edits:</p>
              {patch.edits.map((e, i) => (
                <div key={i} className="edit">
                  <p className="mono small">{e.file}</p>
                  <DiffView
                    diff={[...e.search.replace(/\n$/, "").split("\n").map((l) => `-${l}`), ...e.replace.replace(/\n$/, "").split("\n").map((l) => `+${l}`)].join("\n")}
                    label={`Proposed edit ${i + 1} to ${e.file}`}
                  />
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}

      {attempt.model_calls.length > 0 && (
        <details className="calls">
          <summary>Model calls ({attempt.model_calls.length})</summary>
          <table className="calls__table">
            <thead>
              <tr><th scope="col">Purpose</th><th scope="col">Model</th><th scope="col">Latency</th><th scope="col">Tokens</th><th scope="col">Finish</th><th scope="col">x-request-id</th></tr>
            </thead>
            <tbody>
              {attempt.model_calls.map((c, i) => (
                <tr key={i}>
                  <td>{c.purpose}</td>
                  <td className="mono">{c.model}</td>
                  <td>{(c.latency_ms / 1000).toFixed(1)} s</td>
                  <td>{c.total_tokens ?? "—"}</td>
                  <td>{c.finish_reason ?? "—"}</td>
                  <td className="mono small">{c.request_id ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </article>
  );
}
