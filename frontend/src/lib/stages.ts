import type { Run, RunEvent } from "../api/types";

export type StageState = "pending" | "active" | "done" | "failed" | "skipped";

export interface Stage {
  key: string;
  title: string;
  caption: string;
  state: StageState;
  events: RunEvent[];
}

const STAGES: { key: string; title: string; caption: string; steps: string[] }[] = [
  { key: "setup", title: "Project & sandbox", caption: "Load files, pick an isolation boundary", steps: ["project", "sandbox"] },
  { key: "baseline", title: "Baseline", caption: "Original tests, run twice", steps: ["baseline"] },
  { key: "patch", title: "Diagnosis & patch", caption: "Root cause, edits, policy checks", steps: ["diagnosis", "patch", "repair"] },
  { key: "candidate", title: "Patched runs", caption: "Full suite on the patched copy", steps: ["candidate"] },
  { key: "verdict", title: "Verdict", caption: "Deterministic backend rules", steps: ["verdict"] },
];

/**
 * Derive pipeline stages purely from the events the backend recorded. Nothing here is simulated:
 * a stage is "active" only when its latest event is a "started" one and the run is still going.
 */
export function deriveStages(run: Run): Stage[] {
  const live = run.state === "queued" || run.state === "running";
  return STAGES.map((def) => {
    const events = run.events.filter((e) => def.steps.includes(e.step));
    let state: StageState;
    if (events.some((e) => e.status === "failed")) state = "failed";
    else if (events.length === 0) state = live ? "pending" : "skipped";
    else if (live && events[events.length - 1]?.status === "started") state = "active";
    else state = "done";
    return { key: def.key, title: def.title, caption: def.caption, state, events };
  });
}
