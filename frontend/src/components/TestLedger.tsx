import { useState } from "react";
import type { CollectionError, Run } from "../api/types";
import { splitNodeId } from "../lib/format";
import { buildLedger, categoryCounts, decidingAttempt, type CellOutcome, type LedgerRow } from "../lib/ledger";
import { CATEGORY_LABEL, CATEGORY_TONE } from "../lib/verdicts";

const GLYPH: Record<CellOutcome, string> = { passed: "✓", failed: "✕", error: "!", skipped: "–", absent: "·" };
const OUTCOME_WORD: Record<CellOutcome, string> = {
  passed: "passed",
  failed: "failed",
  error: "error",
  skipped: "skipped",
  absent: "not reported",
};

function Cell({ outcome, label }: { outcome: CellOutcome; label: string }) {
  return (
    <span className={`cell cell--${outcome}`} role="img" aria-label={`${label}: ${OUTCOME_WORD[outcome]}`} title={`${label}: ${OUTCOME_WORD[outcome]}`}>
      {GLYPH[outcome]}
    </span>
  );
}

function Row({ row }: { row: LedgerRow }) {
  const [open, setOpen] = useState(false);
  const { file, name } = splitNodeId(row.nodeid);
  const tone = row.category ? CATEGORY_TONE[row.category] : "quiet";
  return (
    <>
      <tr className={`ledger__row ledger__row--${tone}`}>
        <th scope="row" className="ledger__test">
          {row.message ? (
            <button type="button" className="ledger__toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
              <span className="ledger__caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
              <span className="ledger__name">{name}</span>
            </button>
          ) : (
            <span className="ledger__name ledger__name--static">{name}</span>
          )}
          <span className="ledger__file">{file}</span>
          {row.isTarget && <span className="tag tag--target">target</span>}
          {row.environment && <span className="tag tag--env">environment</span>}
        </th>
        <td className="ledger__cells">
          {row.baseline.map((o, i) => <Cell key={i} outcome={o} label={`Baseline run ${i + 1}`} />)}
        </td>
        <td className="ledger__arrow" aria-hidden="true">→</td>
        <td className="ledger__cells">
          {row.candidate.length
            ? row.candidate.map((o, i) => <Cell key={i} outcome={o} label={`Patched run ${i + 1}`} />)
            : <span className="muted small">not run</span>}
        </td>
        <td className="ledger__category">
          {row.category ? <span className={`cat cat--${tone}`}>{CATEGORY_LABEL[row.category]}</span> : <span className="muted">—</span>}
        </td>
      </tr>
      {open && row.message && (
        <tr className="ledger__detail">
          <td colSpan={5}>
            <pre className="log">{row.message}</pre>
          </td>
        </tr>
      )}
    </>
  );
}

export function TestLedger({ run }: { run: Run }) {
  const rows = buildLedger(run);
  const counts = categoryCounts(rows);
  const attempt = decidingAttempt(run);
  const candidateRuns = attempt?.candidate_runs ?? [];
  // The same collection error usually repeats in every run; group it and list where it appeared.
  const grouped = new Map<string, CollectionError & { runs: string[] }>();
  for (const r of [...run.baseline_runs, ...candidateRuns]) {
    for (const e of r.collection_errors) {
      const key = `${e.nodeid}\n${e.message}`;
      const entry = grouped.get(key) ?? { ...e, runs: [] };
      entry.runs.push(r.label);
      grouped.set(key, entry);
    }
  }
  const collection = [...grouped.values()];
  const unhealthy = [...run.baseline_runs, ...candidateRuns].filter((r) => r.health !== "ok");

  if (rows.length === 0 && run.baseline_runs.length === 0) {
    return <p className="muted">No tests have run yet.</p>;
  }

  const tally: [string, number | undefined, string][] = [
    ["Fixed", counts.fixed, "good"],
    ["Regressions", (counts.regression ?? 0) + (counts.missing ?? 0) || undefined, "bad"],
    ["Still failing", counts.still_failing, "bad"],
    ["Still passing", counts.still_passing, "quiet"],
    ["Skipped / flaky / env", (counts.newly_skipped ?? 0) + (counts.flaky ?? 0) + (counts.environment ?? 0) || undefined, "warn"],
  ];

  return (
    <div className="ledger">
      {candidateRuns.length > 0 && (
        <dl className="tally">
          {tally.map(([label, n, tone]) => (
            <div key={label} className={`tally__item tally__item--${tone} ${n ? "" : "tally__item--zero"}`}>
              <dt>{label}</dt>
              <dd>{n ?? 0}</dd>
            </div>
          ))}
        </dl>
      )}
      {candidateRuns.length === 0 && run.state === "finished" && (
        <p className="muted small">
          No patched runs: the patch was not executed. The baseline column is the only test evidence for this run.
        </p>
      )}
      <div className="ledger__scroll" tabIndex={0} aria-label="Test comparison table">
        <table className="ledger__table">
          <caption className="sr-only">
            Each test's outcome in every baseline run and every patched run, with the comparison category assigned by the backend.
          </caption>
          <thead>
            <tr>
              <th scope="col">Test</th>
              <th scope="col">Baseline</th>
              <th scope="col" aria-hidden="true" />
              <th scope="col">Patched</th>
              <th scope="col">Result</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => <Row key={r.nodeid} row={r} />)}
          </tbody>
        </table>
      </div>
      <p className="legend small muted" aria-hidden="true">
        <span className="cell cell--passed">✓</span> passed <span className="cell cell--failed">✕</span> failed{" "}
        <span className="cell cell--error">!</span> error <span className="cell cell--skipped">–</span> skipped{" "}
        <span className="cell cell--absent">·</span> not reported
      </p>
      {collection.length > 0 && (
        <div className="subsection">
          <h4>Collection errors</h4>
          <ul className="plain">
            {collection.map((e) => (
              <li key={`${e.nodeid}:${e.runs[0]}`}>
                <span className="mono">{e.nodeid}</span> <span className="tag">{e.kind}</span>{" "}
                <span className="muted small">in {e.runs.join(", ")}</span>
                <pre className="log">{e.message}</pre>
              </li>
            ))}
          </ul>
        </div>
      )}
      {unhealthy.length > 0 && (
        <div className="subsection">
          <h4>Runs that did not complete cleanly</h4>
          <ul className="plain">
            {unhealthy.map((r) => (
              <li key={r.label}>
                <strong>{r.label}</strong>: <span className="mono">{r.health}</span>
                {r.detail ? <span> — {r.detail}</span> : null}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
