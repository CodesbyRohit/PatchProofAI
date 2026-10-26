import type { Attempt, ComparisonCategory, Run, TestResult, TestRun } from "../api/types";
import { CATEGORY_ORDER } from "./verdicts";

export type CellOutcome = TestResult["outcome"] | "absent";

export interface LedgerRow {
  nodeid: string;
  isTarget: boolean;
  category: ComparisonCategory | null;
  baseline: CellOutcome[];
  candidate: CellOutcome[];
  environment: boolean;
  /** First failure message seen: candidate runs first (what the patch did), then baseline. */
  message: string | null;
}

/** The attempt whose candidate runs back the final verdict: the last one that actually executed. */
export function decidingAttempt(run: Run): Attempt | null {
  for (let i = run.attempts.length - 1; i >= 0; i--) {
    const a = run.attempts[i];
    if (a && a.candidate_runs.length > 0) return a;
  }
  return run.attempts[run.attempts.length - 1] ?? null;
}

function lookup(runs: TestRun[]): Map<string, TestResult>[] {
  return runs.map((r) => new Map(r.tests.map((t) => [t.nodeid, t])));
}

export function buildLedger(run: Run): LedgerRow[] {
  const attempt = decidingAttempt(run);
  const candidateRuns = attempt?.candidate_runs ?? [];
  const base = lookup(run.baseline_runs);
  const cand = lookup(candidateRuns);
  const comparison = attempt?.verdict?.comparison ?? run.verdict?.comparison ?? [];
  const byId = new Map(comparison.map((c) => [c.nodeid, c]));
  const targets = new Set(run.baseline?.targets ?? []);

  const ids = new Set<string>();
  for (const m of [...base, ...cand]) for (const id of m.keys()) ids.add(id);
  for (const c of comparison) ids.add(c.nodeid);

  const rows: LedgerRow[] = [...ids].map((nodeid) => {
    const c = byId.get(nodeid);
    const results = [...cand, ...base].map((m) => m.get(nodeid)).filter((t): t is TestResult => !!t);
    return {
      nodeid,
      isTarget: c?.is_target ?? targets.has(nodeid),
      category: c?.category ?? null,
      baseline: base.map((m) => m.get(nodeid)?.outcome ?? "absent"),
      candidate: cand.map((m) => m.get(nodeid)?.outcome ?? "absent"),
      environment: results.some((t) => t.environment_issue),
      message: results.find((t) => t.outcome !== "passed" && t.message)?.message ?? null,
    };
  });

  return rows.sort((a, b) => {
    const ca = a.category ? CATEGORY_ORDER[a.category] : 50;
    const cb = b.category ? CATEGORY_ORDER[b.category] : 50;
    if (ca !== cb) return ca - cb;
    if (a.isTarget !== b.isTarget) return a.isTarget ? -1 : 1;
    return a.nodeid.localeCompare(b.nodeid);
  });
}

export function categoryCounts(rows: LedgerRow[]): Partial<Record<ComparisonCategory, number>> {
  const out: Partial<Record<ComparisonCategory, number>> = {};
  for (const r of rows) if (r.category) out[r.category] = (out[r.category] ?? 0) + 1;
  return out;
}
