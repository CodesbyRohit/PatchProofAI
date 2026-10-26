import type { Run } from "../api/types";
import { deriveStages, type StageState } from "../lib/stages";

const STATE_WORD: Record<StageState, string> = {
  pending: "waiting",
  active: "running",
  done: "done",
  failed: "stopped",
  skipped: "not reached",
};

export function Pipeline({ run }: { run: Run }) {
  const stages = deriveStages(run);
  return (
    <ol className="pipeline" aria-label="Verification stages">
      {stages.map((s, i) => (
        <li key={s.key} className={`pipeline__stage pipeline__stage--${s.state}`} aria-current={s.state === "active" ? "step" : undefined}>
          <span className="pipeline__index" aria-hidden="true">{String(i + 1).padStart(2, "0")}</span>
          <div className="pipeline__body">
            <p className="pipeline__title">
              {s.title} <span className="pipeline__state">{STATE_WORD[s.state]}</span>
            </p>
            <p className="pipeline__caption">{s.caption}</p>
            {s.events.length > 0 && (
              <ul className="pipeline__events">
                {s.events.map((e, j) => (
                  <li key={j} className={`event event--${e.status}`}>
                    {e.message}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
