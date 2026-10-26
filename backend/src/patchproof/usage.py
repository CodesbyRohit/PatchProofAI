"""Usage accounting, plan quotas, privacy-conscious product events, and the (disabled) billing interface.

Everything here is server-side: quotas and plans are never taken from the browser. Usage rows record what a run
cost (sandbox seconds, model tokens, duration, verdict), never what it contained. Events are an allowlisted set
of names with a keyed hash of the owner, a few enumerated properties, and a retention limit.

    uv run python -m patchproof.usage report [--days 30]       # funnel, completion, cost per run (operator)
    uv run python -m patchproof.usage set-plan user:123 member  # assign a plan (operator)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import secrets
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import Settings, load_settings
from .schemas import PatchSource, Run
from .store import Store

EVENT_RETENTION_S = 90 * 24 * 3600

# Events and the only properties (with allowed values) each may carry. Nothing else is stored.
EVENTS: dict[str, dict[str, set[str] | None]] = {
    "landing_viewed": {},
    "github_sign_in_started": {},
    "github_connected": {},
    "repository_selected": {"visibility": {"public", "private"}},
    "run_submitted": {"origin": {"example", "upload", "inline", "github"}, "patch_source": {"ai", "user", "scripted"}},
    "run_completed": {"state": {"finished", "error"}, "verdict": None},  # verdict: one of the four statuses
    "patch_downloaded": {},
    "report_downloaded": {},
    "pull_request_created": {},
}
CLIENT_EVENTS = {"landing_viewed", "repository_selected"}  # the only ones the browser may send
VERDICTS = {"VERIFIED_FOR_TESTED_CASES", "FAILED_VERIFICATION", "INCONCLUSIVE", "BLOCKED_BY_SAFETY_POLICY"}


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_runs: int | None  # None: no monthly limit


class QuotaExceeded(Exception):
    def __init__(self, used: int, limit: int, resets_at: float):
        super().__init__(f"Monthly limit reached: {used} of {limit} verifications used.")
        self.used, self.limit, self.resets_at = used, limit, resets_at


class BillingProvider:
    """Payment integration point. No provider is configured, so nothing can be bought and nothing is charged."""

    enabled = False

    def checkout_url(self, owner: str, plan: str) -> str:
        raise NotImplementedError("Billing is not configured on this PatchProof instance.")


def period_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def period_end(ts: float) -> float:
    d = datetime.fromtimestamp(ts, UTC)
    nxt = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=UTC)
    return nxt.timestamp()


class Usage:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.billing = BillingProvider()
        self._salt = store.meta_value("analytics_salt", lambda: secrets.token_hex(32)).encode()
        self.plans = {
            "free": Plan("free", settings.free_monthly_runs or None),
            "member": Plan("member", None),
        }

    # -- plans and quotas ------------------------------------------------------------------------------

    def plan_for(self, owner: str) -> Plan:
        return self.plans.get(self.store.get_plan(owner) or "free", self.plans["free"])

    @staticmethod
    def counts_toward_quota(origin: str, patch_source: PatchSource) -> bool:
        # The bundled demo (maintainer-written patches on bundled examples) stays free: it calls no model.
        return not (origin == "example" and patch_source == PatchSource.scripted)


    def account(self, owner: str, now: float | None = None) -> dict:
        now = now or time.time()
        plan = self.plan_for(owner)
        return {
            "plan": plan.name if owner != "local" else "local",
            "period": period_of(now),
            "runs_used": self.store.counted_runs(owner, period_of(now)),
            "runs_limit": None if owner == "local" else plan.monthly_runs,
            "resets_at": period_end(now),
            "billing": {"enabled": self.billing.enabled},
        }

    # -- accounting -----------------------------------------------------------------------------------

    def run_started(self, run: Run, origin: str, private: bool) -> None:
        """Record the run and enforce the monthly quota in one atomic step; raise QuotaExceeded if it is used up.

        The open local principal (single user) has no quota.
        """
        counts = self.counts_toward_quota(origin, run.patch_source)
        limit = None if run.owner == "local" else self.plan_for(run.owner).monthly_runs
        blocked = self.store.reserve_usage(run.id, run.owner, period_of(run.created_at), origin,
                                           run.patch_source.value, private, counts, limit)
        if blocked is not None:
            raise QuotaExceeded(blocked, limit or 0, period_end(run.created_at))
        self.event("run_submitted", run.owner, origin=origin, patch_source=run.patch_source.value)

    def run_finished(self, run: Run) -> None:
        test_runs = [*run.baseline_runs, *(r for a in run.attempts for r in a.candidate_runs)]
        calls = [c for a in run.attempts for c in a.model_calls]
        self.store.record_usage_finish(
            run.id, run.state, run.verdict.status.value if run.verdict else None,
            (run.finished_at - run.created_at) if run.finished_at else None,
            sum(r.duration_s for r in test_runs), len(calls),
            sum(c.prompt_tokens or 0 for c in calls), sum(c.completion_tokens or 0 for c in calls))
        props = {"state": run.state} | ({"verdict": run.verdict.status.value} if run.verdict else {})
        self.event("run_completed", run.owner, **props)

    # -- analytics -----------------------------------------------------------------------------------

    def subject(self, owner: str | None) -> str | None:
        if not owner or owner == "local":
            return None
        return hmac.new(self._salt, owner.encode(), hashlib.sha256).hexdigest()[:20]

    def event(self, name: str, owner: str | None, **props: str) -> bool:
        """Record an allowlisted event. Unknown names or property values are dropped, never stored."""
        if not self.settings.analytics or name not in EVENTS:
            return False
        allowed = EVENTS[name]
        clean = {}
        for key, value in props.items():
            if key not in allowed:
                continue
            values = allowed[key] if allowed[key] is not None else VERDICTS
            if value in values:
                clean[key] = value
        self.store.add_event(name, self.subject(owner), json.dumps(clean, sort_keys=True), EVENT_RETENTION_S)
        return True


# -- operator report ------------------------------------------------------------------------------------


def report(settings: Settings, store: Store, days: int) -> str:
    since = time.time() - days * 86400
    rows = store.usage_rows(since)
    events = store.events_since(since)
    lines = [f"PatchProof usage, last {days} days ({datetime.fromtimestamp(since, UTC):%Y-%m-%d} onwards)", ""]

    counts: dict[str, int] = {}
    for name, *_ in events:
        counts[name] = counts.get(name, 0) + 1
    lines.append("Funnel (events):")
    for name in EVENTS:
        lines.append(f"  {name:24} {counts.get(name, 0)}")

    finished = [r for r in rows if r["state"] in ("finished", "error")]
    verified = [r for r in rows if r["verdict"] == "VERIFIED_FOR_TESTED_CASES"]
    lines += ["", f"Runs: {len(rows)} started, {len(finished)} ended, "
                  f"{sum(r['state'] == 'finished' for r in rows)} with a verdict, {len(verified)} verified"]
    by_verdict: dict[str, int] = {}
    for r in rows:
        key = r["verdict"] or ("error" if r["state"] == "error" else "unfinished")
        by_verdict[key] = by_verdict.get(key, 0) + 1
    for key, n in sorted(by_verdict.items()):
        lines.append(f"  {key:28} {n}")
    owners = {r["owner"] for r in rows if r["owner"] != "local"}
    days_by_owner: dict[str, set[str]] = {}
    for r in rows:
        days_by_owner.setdefault(r["owner"], set()).add(datetime.fromtimestamp(r["created_at"], UTC).strftime("%Y-%m-%d"))
    returning = sum(1 for o in owners if len(days_by_owner.get(o, ())) >= 2)
    lines.append(f"Users with runs: {len(owners)}; returned on another day: {returning}")
    durations = [r["duration_s"] for r in finished if r["duration_s"] is not None]
    if durations:
        lines.append(f"Median time to result: {statistics.median(durations):.1f} s")

    sandbox_h = sum(r["sandbox_s"] for r in rows) / 3600
    tokens = sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows)
    lines += ["", f"Sandbox time: {sandbox_h:.3f} h; model calls: {sum(r['model_calls'] for r in rows)}; tokens: {tokens}"]
    if settings.cost_per_sandbox_hour is None or settings.cost_per_1k_tokens is None:
        lines.append("Cost per run: not measured. Set PATCHPROOF_COST_PER_SANDBOX_HOUR and "
                     "PATCHPROOF_COST_PER_1K_MODEL_TOKENS to the prices you actually pay.")
    elif rows:
        cost = sandbox_h * settings.cost_per_sandbox_hour + tokens / 1000 * settings.cost_per_1k_tokens
        line = f"Variable cost (sandbox + model, excluding hosting): {cost:.4f} total, {cost / len(rows):.4f} per run"
        if verified:
            line += f", {cost / len(verified):.4f} per verified run"
        lines.append(line)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="patchproof.usage", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--days", type=int, default=30)
    sp = sub.add_parser("set-plan")
    sp.add_argument("owner", help="e.g. user:123 (GitHub user id) or token:<prefix>")
    sp.add_argument("plan", choices=["free", "member"])
    args = parser.parse_args(argv[1:])
    settings = load_settings()
    store = Store(settings.db_path)
    if args.command == "report":
        print(report(settings, store, args.days))
    else:
        store.set_plan(args.owner, args.plan)
        print(f"{args.owner} -> {args.plan}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
