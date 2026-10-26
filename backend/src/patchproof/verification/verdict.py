"""Deterministic verdict rules. The model never decides a verdict; these functions do.

Precedence: any observed failure evidence -> FAILED_VERIFICATION; otherwise any gap in the
evidence -> INCONCLUSIVE; only a clean, complete, repeated pass -> VERIFIED_FOR_TESTED_CASES.
"""

from __future__ import annotations

from collections import defaultdict

from ..schemas import (
    BaselineAnalysis,
    CandidatePatch,
    ComparisonCategory,
    RunHealth,
    TestComparison,
    TestOutcome,
    TestRun,
    Verdict,
    VerdictStatus,
)

COLLECT_PREFIX = "collect::"

_HEALTH_REASONS = {
    RunHealth.timeout: "exceeded the wall-clock time limit",
    RunHealth.no_tests: "collected no tests",
    RunHealth.sandbox_error: "could not be executed by the sandbox",
    RunHealth.report_missing: "ended without a complete structured report (crash, kill, or tampering)",
    RunHealth.internal_error: "ended with a pytest internal or usage error",
}

_FAILING = {TestOutcome.failed, TestOutcome.error}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _health_reason(run: TestRun) -> str:
    text = f"{run.label} {_HEALTH_REASONS.get(run.health, run.health.value)}"
    if run.detail:
        text += f": {run.detail}"
    return text + "."


def _outcomes_by_test(runs: list[TestRun]) -> dict[str, list[TestOutcome | None]]:
    table: dict[str, list[TestOutcome | None]] = defaultdict(lambda: [None] * len(runs))
    for i, run in enumerate(runs):
        for t in run.tests:
            table[t.nodeid][i] = t.outcome
    return dict(table)


def _env_tests(runs: list[TestRun]) -> set[str]:
    return {t.nodeid for run in runs for t in run.tests if t.environment_issue}


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def analyze_baseline(runs: list[TestRun], requested_targets: list[str] | None = None) -> BaselineAnalysis:
    reasons: list[str] = []
    unhealthy = [r for r in runs if r.health != RunHealth.ok]
    for run in unhealthy:
        reasons.append("Baseline " + _health_reason(run))

    env_failures: set[str] = set()
    code_collect: set[str] = set()
    for run in runs:
        for err in run.collection_errors:
            key = COLLECT_PREFIX + err.nodeid
            (env_failures if err.kind == "environment" else code_collect).add(key)
    env_failures |= _env_tests(runs)

    table = _outcomes_by_test(runs)
    flaky = sorted(n for n, outs in table.items() if len(set(outs)) > 1 and n not in env_failures)
    failing = sorted(
        n for n, outs in table.items()
        if n not in env_failures and n not in flaky and all(o in _FAILING for o in outs)
    )
    passing = sorted(n for n, outs in table.items() if all(o == TestOutcome.passed for o in outs))
    targets = failing + sorted(code_collect - env_failures)

    if requested_targets:
        missing = [t for t in requested_targets if t not in targets]
        for t in missing:
            if t in passing:
                reasons.append(f"Requested reproduction test {t} already passes before any patch.")
            else:
                reasons.append(f"Requested reproduction test {t} did not fail reproducibly at baseline.")
        targets = [t for t in targets if t in requested_targets]

    if env_failures:
        reasons.append(
            f"{_plural(len(env_failures), 'test or module')} could not run because of environment "
            "errors (missing dependencies), so they cannot be verified: " + ", ".join(sorted(env_failures)[:5])
        )
    if flaky:
        reasons.append(f"{_plural(len(flaky), 'test')} changed outcome between identical baseline runs (flaky): "
                       + ", ".join(flaky[:5]))
    if not targets and not unhealthy:
        if env_failures:
            reasons.append("No code defect could be reproduced independently of the environment errors.")
        elif flaky:
            reasons.append("The failures were not reproducible across baseline runs.")
        else:
            reasons.append("Every test passes before any patch, so there is no failing test that "
                           "reproduces the reported defect.")
    usable = not unhealthy and bool(targets)
    return BaselineAnalysis(
        usable=usable,
        targets=targets,
        passing=passing,
        flaky=flaky,
        environment_failures=sorted(env_failures),
        reasons=reasons,
    )


def verdict_for_unusable_baseline(analysis: BaselineAnalysis) -> Verdict:
    return Verdict(
        status=VerdictStatus.inconclusive,
        headline="Inconclusive: the defect could not be reproduced reliably, so no fix can be verified.",
        reasons=analysis.reasons,
        targets=analysis.targets,
    )


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------


def verdict_for_rejected_patch(candidate: CandidatePatch) -> Verdict:
    blocked = any(f.severity == "block" for f in candidate.policy_findings)
    reasons = [f.message for f in candidate.policy_findings]
    if blocked:
        return Verdict(
            status=VerdictStatus.blocked,
            headline="Blocked by safety policy: the patch was not applied or executed.",
            reasons=reasons,
        )
    return Verdict(
        status=VerdictStatus.failed,
        headline="Failed verification: the patch is invalid and could not be applied.",
        reasons=reasons,
    )


def _worst(outs: list[TestOutcome | None]) -> str:
    if any(o is None for o in outs):
        return "missing"
    if any(o in _FAILING for o in outs):
        return next(o.value for o in outs if o in _FAILING)  # type: ignore[union-attr]
    if any(o == TestOutcome.skipped for o in outs):
        return "skipped"
    return "passed"


def _baseline_label(outs: list[TestOutcome | None]) -> str:
    vals = {o.value if o else "missing" for o in outs}
    return vals.pop() if len(vals) == 1 else "flaky(" + "/".join(sorted(vals)) + ")"


def decide(analysis: BaselineAnalysis, baseline_runs: list[TestRun], candidate_runs: list[TestRun]) -> Verdict:
    failures: list[str] = []
    gaps: list[str] = []

    for run in candidate_runs:
        if run.health != RunHealth.ok:
            gaps.append("Candidate " + _health_reason(run))
    # Unhealthy runs (timeout, crash, sandbox failure) carry no trustworthy per-test results:
    # they make the verdict inconclusive but are not counted as failure evidence.
    candidate_runs = [r for r in candidate_runs if r.health == RunHealth.ok]
    if not candidate_runs:
        return Verdict(status=VerdictStatus.inconclusive,
                       headline="Inconclusive: no candidate run completed, so the patch could not be evaluated.",
                       reasons=gaps, targets=analysis.targets)

    base_collect = {COLLECT_PREFIX + e.nodeid for r in baseline_runs for e in r.collection_errors}
    cand_collect: dict[str, str] = {}
    for run in candidate_runs:
        for err in run.collection_errors:
            cand_collect[COLLECT_PREFIX + err.nodeid] = err.kind
    for key, kind in sorted(cand_collect.items()):
        if key in base_collect:
            continue
        if kind == "environment":
            gaps.append(f"After the patch, {key[len(COLLECT_PREFIX):]} hit an environment error during collection.")
        else:
            failures.append(f"The patch breaks test collection for {key[len(COLLECT_PREFIX):]}.")

    base_table = _outcomes_by_test(baseline_runs)
    cand_table = _outcomes_by_test(candidate_runs)
    cand_env = _env_tests(candidate_runs)
    targets = set(analysis.targets)
    collect_targets = {t[len(COLLECT_PREFIX):] for t in targets if t.startswith(COLLECT_PREFIX)}
    rows: list[TestComparison] = []

    for nodeid in sorted(set(base_table) | set(cand_table)):
        b_outs = base_table.get(nodeid)
        c_outs = cand_table.get(nodeid, [None] * len(candidate_runs))
        cand = _worst(c_outs)
        cand_label = cand if len(set(c_outs)) <= 1 else "flaky(" + "/".join(sorted({o.value if o else "missing" for o in c_outs})) + ")"
        is_target = nodeid in targets

        if b_outs is None:
            module = nodeid.split("::", 1)[0]
            in_target_module = module in collect_targets
            if cand in ("failed", "error"):
                cat = ComparisonCategory.still_failing if in_target_module else ComparisonCategory.new_test
                failures.append(f"{nodeid} fails after the patch.")
            elif cand == "passed":
                cat = ComparisonCategory.fixed if in_target_module else ComparisonCategory.new_test
            else:
                cat = ComparisonCategory.newly_skipped
                if in_target_module:
                    gaps.append(f"{nodeid} was skipped after the patch.")
            rows.append(TestComparison(nodeid=nodeid, baseline="not collected", candidate=cand_label,
                                       category=cat, is_target=in_target_module))
            continue

        base_label = _baseline_label(b_outs)
        if nodeid in analysis.environment_failures or nodeid in cand_env:
            cat = ComparisonCategory.environment
        elif nodeid in analysis.flaky:
            cat = ComparisonCategory.flaky
        elif is_target:
            if cand == "passed" and len(set(c_outs)) == 1:
                cat = ComparisonCategory.fixed
            elif cand in ("failed", "error"):
                cat = ComparisonCategory.still_failing
                failures.append(f"Target test {nodeid} still fails after the patch.")
            elif cand == "missing":
                cat = ComparisonCategory.missing
                failures.append(f"Target test {nodeid} disappeared after the patch.")
            else:
                cat = ComparisonCategory.newly_skipped
                gaps.append(f"Target test {nodeid} was skipped after the patch, so the fix is unproven.")
        elif all(o == TestOutcome.passed for o in b_outs):
            if cand in ("failed", "error"):
                cat = ComparisonCategory.regression
                failures.append(f"Regression: {nodeid} passed before the patch and fails after it.")
            elif cand == "missing":
                cat = ComparisonCategory.missing
                failures.append(f"{nodeid} passed before the patch but was not run after it.")
            elif cand == "skipped":
                cat = ComparisonCategory.newly_skipped
                gaps.append(f"{nodeid} passed before the patch but was skipped after it.")
            else:
                cat = ComparisonCategory.still_passing
        elif all(o == TestOutcome.skipped for o in b_outs):
            cat = ComparisonCategory.unchanged_skip
        else:
            cat = ComparisonCategory.still_failing if cand in ("failed", "error") else ComparisonCategory.fixed
        rows.append(TestComparison(nodeid=nodeid, baseline=base_label, candidate=cand_label,
                                   category=cat, is_target=is_target))

    for module in sorted(collect_targets):
        key = COLLECT_PREFIX + module
        if key in cand_collect:
            failures.append(f"{module} still fails to import after the patch.")
        elif not any(r.nodeid.startswith(module + "::") and r.category == ComparisonCategory.fixed for r in rows):
            gaps.append(f"{module} now imports, but no test in it was observed passing.")
        rows.insert(0, TestComparison(
            nodeid=key, baseline="collection error",
            candidate="collection error" if key in cand_collect else "collected",
            category=ComparisonCategory.still_failing if key in cand_collect else ComparisonCategory.fixed,
            is_target=True,
        ))

    for nodeid in sorted({r.nodeid for r in rows if r.category == ComparisonCategory.environment}):
        if nodeid not in analysis.environment_failures:
            gaps.append(f"{nodeid} hit an environment error after the patch.")
    if analysis.environment_failures:
        gaps.append("Some tests could not run because of environment errors, so the evidence is incomplete.")
    # A flaky test is excluded from the comparison, but its presence means the suite did not behave
    # reproducibly, so the run as a whole can be at best INCONCLUSIVE (failure evidence still wins).
    if analysis.flaky:
        gaps.append(f"{_plural(len(analysis.flaky), 'test')} changed outcome between identical baseline runs "
                    "(flaky), so the evidence is incomplete: " + ", ".join(analysis.flaky[:5]))

    by_cat: dict[ComparisonCategory, int] = defaultdict(int)
    for r in rows:
        by_cat[r.category] += 1
    n_runs = len(candidate_runs)
    fixed = sum(1 for r in rows if r.is_target and r.category == ComparisonCategory.fixed)
    n_targets = sum(1 for r in rows if r.is_target)
    regressions = by_cat[ComparisonCategory.regression] + by_cat[ComparisonCategory.missing]

    if failures:
        if regressions and fixed:
            headline = (f"Rejected: the patch fixes {fixed} of {_plural(n_targets, 'target test')} but breaks "
                        f"{_plural(regressions, 'test')} that passed before.")
        elif regressions:
            headline = f"Rejected: the patch breaks {_plural(regressions, 'test')} that passed before."
        else:
            headline = f"Rejected: {_plural(n_targets - fixed, 'target test')} still fail after the patch."
        return Verdict(status=VerdictStatus.failed, headline=headline, reasons=failures + gaps,
                       targets=sorted(targets), comparison=rows)
    if gaps:
        return Verdict(status=VerdictStatus.inconclusive,
                       headline="Inconclusive: no failure was observed, but the evidence is incomplete.",
                       reasons=gaps, targets=sorted(targets), comparison=rows)
    kept = by_cat[ComparisonCategory.still_passing]
    return Verdict(
        status=VerdictStatus.verified,
        headline=(f"Verified for tested cases: {_plural(fixed, 'failing test')} now pass and "
                  f"{_plural(kept, 'previously passing test')} still pass, across {_plural(n_runs, 'candidate run')}."),
        reasons=[
            f"All {n_targets} reproduction target(s) failed at baseline and passed in every candidate run.",
            f"No regressions: all {kept} test(s) that passed at baseline still pass.",
            "This is evidence for the tested cases only, not a proof of correctness for all inputs.",
        ],
        targets=sorted(targets),
        comparison=rows,
    )
