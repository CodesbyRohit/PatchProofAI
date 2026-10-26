import { api } from "../api/client";
import { ErrorNotice, Loading } from "../components/Notice";
import { SourceBadge } from "../components/SourceBadge";
import { VerdictChip } from "../components/VerdictStamp";
import { formatTime } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { Link } from "../router";

const STATE_LABEL = { queued: "Queued", running: "Running", finished: "Finished", error: "Did not finish" } as const;

export function History() {
  const runs = useAsync("runs", () => api.runs(50));
  return (
    <div className="page history">
      <header className="page__head">
        <p className="eyebrow">Run history</p>
        <h1 className="page__title">Every verdict, with its evidence</h1>
        <button type="button" className="btn btn--ghost btn--sm" onClick={runs.reload} disabled={runs.loading}>
          {runs.loading ? "Refreshing…" : "Refresh"}
        </button>
      </header>

      {runs.loading && !runs.data && <Loading label="Loading run history…" />}
      {runs.error && <ErrorNotice error={runs.error} onRetry={runs.reload} title="Could not load run history" />}
      {runs.data && runs.data.length === 0 && (
        <div className="empty">
          <p>No runs yet.</p>
          <Link to="/" className="btn btn--primary">Try a demo example</Link>
        </div>
      )}
      {runs.data && runs.data.length > 0 && (
        <ul className="plain runs">
          {runs.data.map((r) => (
            <li key={r.id}>
              <Link to={`/runs/${r.id}`} className="runrow">
                <span className="runrow__when mono small">{formatTime(r.created_at)}</span>
                <span className="runrow__name">{r.project_name}</span>
                <span className="runrow__meta">
                  <SourceBadge source={r.patch_source} />
                  {r.state === "finished" ? <VerdictChip status={r.verdict} /> : <span className="chip chip--quiet">{STATE_LABEL[r.state]}</span>}
                </span>
                {r.headline && <span className="runrow__headline small muted">{r.headline}</span>}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
