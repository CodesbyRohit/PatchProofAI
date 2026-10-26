from conftest import make_run

from patchproof.schemas import (
    CandidatePatch,
    CollectionError,
    ComparisonCategory,
    PolicyFinding,
    RunHealth,
    VerdictStatus,
)
from patchproof.verification.verdict import (
    analyze_baseline,
    decide,
    verdict_for_rejected_patch,
    verdict_for_unusable_baseline,
)

BASE = {"t::a": "passed", "t::b": "passed", "t::bug": "failed"}


def baseline(outcomes=BASE, n=2, **kw):
    runs = [make_run(f"b{i}", outcomes, **kw) for i in range(n)]
    return runs, analyze_baseline(runs)


def test_verified_when_target_fixed_and_no_regressions():
    runs, analysis = baseline()
    assert analysis.usable and analysis.targets == ["t::bug"]
    cand = [make_run(f"c{i}", {"t::a": "passed", "t::b": "passed", "t::bug": "passed"}) for i in range(2)]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.verified
    cats = {c.nodeid: c.category for c in v.comparison}
    assert cats == {"t::a": ComparisonCategory.still_passing, "t::b": ComparisonCategory.still_passing,
                    "t::bug": ComparisonCategory.fixed}


def test_regression_is_rejected_even_when_target_is_fixed():
    runs, analysis = baseline()
    cand = [make_run("c", {"t::a": "failed", "t::b": "passed", "t::bug": "passed"}) for _ in range(2)]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.failed
    assert "breaks 1 test" in v.headline
    assert any("Regression: t::a" in r for r in v.reasons)


def test_target_still_failing():
    runs, analysis = baseline()
    v = decide(analysis, runs, [make_run("c", BASE)])
    assert v.status == VerdictStatus.failed
    assert any("still fails" in r for r in v.reasons)


def test_pre_existing_failures_are_not_regressions():
    outcomes = {"t::a": "passed", "t::bug": "failed", "t::other": "error"}
    runs, analysis = baseline(outcomes)
    assert analysis.targets == ["t::bug", "t::other"]
    cand = [make_run("c", {"t::a": "passed", "t::bug": "passed", "t::other": "error"})]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.failed
    assert not any("Regression" in r for r in v.reasons)


def test_intermittent_candidate_failure_counts_as_failure():
    runs, analysis = baseline()
    cand = [make_run("c1", {"t::a": "passed", "t::b": "passed", "t::bug": "passed"}),
            make_run("c2", {"t::a": "passed", "t::b": "passed", "t::bug": "failed"})]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.failed


def test_flaky_baseline_target_is_not_reproducible():
    runs = [make_run("b1", BASE), make_run("b2", BASE | {"t::bug": "passed"})]
    analysis = analyze_baseline(runs)
    assert not analysis.usable and analysis.flaky == ["t::bug"]
    assert verdict_for_unusable_baseline(analysis).status == VerdictStatus.inconclusive


def test_all_passing_baseline_has_nothing_to_verify():
    runs, analysis = baseline({"t::a": "passed"})
    assert not analysis.usable
    assert any("no failing test" in r for r in analysis.reasons)


def test_baseline_timeout_is_inconclusive():
    runs = [make_run("b", BASE, health=RunHealth.timeout)]
    analysis = analyze_baseline(runs)
    assert not analysis.usable
    assert any("wall-clock" in r for r in analysis.reasons)


def test_environment_failure_caps_verdict_at_inconclusive():
    outcomes = BASE | {"t::needs_yaml": "error"}
    runs, analysis = baseline(outcomes, env={"t::needs_yaml"})
    assert analysis.usable and analysis.environment_failures == ["t::needs_yaml"]
    cand = [make_run("c", {"t::a": "passed", "t::b": "passed", "t::bug": "passed", "t::needs_yaml": "error"},
                     env={"t::needs_yaml"})]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.inconclusive


def test_only_environment_failures_cannot_be_verified():
    runs, analysis = baseline({"t::a": "passed", "t::x": "error"}, env={"t::x"})
    assert not analysis.usable


def test_candidate_timeout_is_inconclusive():
    runs, analysis = baseline()
    cand = [make_run("c", {"t::a": "passed", "t::b": "passed", "t::bug": "passed"}),
            make_run("c2", {}, health=RunHealth.timeout)]
    v = decide(analysis, runs, cand)
    # A timed-out run is a gap in the evidence, not proof of failure.
    assert v.status == VerdictStatus.inconclusive
    assert any("wall-clock" in r for r in v.reasons)
    only_timeout = decide(analysis, runs, [make_run("c", {}, health=RunHealth.timeout)])
    assert only_timeout.status == VerdictStatus.inconclusive and not only_timeout.comparison


def test_skipped_target_is_inconclusive():
    runs, analysis = baseline()
    cand = [make_run("c", {"t::a": "passed", "t::b": "passed", "t::bug": "skipped"})]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.inconclusive


def test_newly_skipped_passing_test_is_inconclusive():
    runs, analysis = baseline()
    cand = [make_run("c", {"t::a": "skipped", "t::b": "passed", "t::bug": "passed"})]
    assert decide(analysis, runs, cand).status == VerdictStatus.inconclusive


def test_collection_error_target_fixed():
    err = [CollectionError(nodeid="tests/test_m.py", message="SyntaxError: invalid syntax", kind="code")]
    runs = [make_run("b", {"tests/test_ok.py::t": "passed"}, collection_errors=err)]
    analysis = analyze_baseline(runs)
    assert analysis.targets == ["collect::tests/test_m.py"]
    cand = [make_run("c", {"tests/test_ok.py::t": "passed", "tests/test_m.py::t1": "passed"})]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.verified


def test_new_collection_error_after_patch_is_failure():
    runs, analysis = baseline()
    err = [CollectionError(nodeid="tests/test_x.py", message="NameError", kind="code")]
    cand = [make_run("c", {"t::bug": "passed"}, collection_errors=err)]
    v = decide(analysis, runs, cand)
    assert v.status == VerdictStatus.failed
    assert any("breaks test collection" in r for r in v.reasons)


def test_rejected_patch_verdicts():
    blocked = CandidatePatch(provenance="live_model", label="x", policy_findings=[
        PolicyFinding(rule="file.test_protected", severity="block", message="no")])
    invalid = CandidatePatch(provenance="live_model", label="x", policy_findings=[
        PolicyFinding(rule="patch.not_found", severity="invalid", message="no")])
    assert verdict_for_rejected_patch(blocked).status == VerdictStatus.blocked
    assert verdict_for_rejected_patch(invalid).status == VerdictStatus.failed
