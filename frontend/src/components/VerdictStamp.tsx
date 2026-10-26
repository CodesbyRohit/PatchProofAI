import type { Verdict } from "../api/types";
import { verdictMeta } from "../lib/verdicts";

export function VerdictStamp({ verdict, size = "lg" }: { verdict: Verdict; size?: "lg" | "sm" }) {
  const meta = verdictMeta(verdict.status);
  if (!meta) return <span className="mono">{verdict.status}</span>;
  return (
    <div className={`stamp stamp--${meta.tone} stamp--${size}`} role="img" aria-label={`Verdict: ${meta.label}`}>
      <span className="stamp__top">{meta.stampTop}</span>
      <span className="stamp__bottom">{meta.stampBottom}</span>
    </div>
  );
}

export function VerdictChip({ status }: { status: Verdict["status"] | null }) {
  const meta = verdictMeta(status);
  if (!meta) return <span className="chip chip--quiet">No verdict</span>;
  return <span className={`chip chip--${meta.tone}`}>{meta.label}</span>;
}
