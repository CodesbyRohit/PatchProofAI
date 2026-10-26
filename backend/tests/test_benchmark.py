"""Tests for the benchmark harness. Cases execute with the local runner (trusted fixture code); the full
benchmark against the Docker sandbox runs in CI via `python -m patchproof.benchmark`."""

import dataclasses
import json
import shutil
import time

import pytest
from conftest import make_run

from patchproof.benchmark import (
    CASES_DIR,
    FLAKE_PLACEHOLDER,
    BenchmarkReport,
    BenchmarkSetupError,
    benchmark_settings,
    evaluate,
    load_case,
    load_cases,
    materialize,
    render_markdown,
    run_benchmark,
    run_case,
    write_report,
)
from patchproof.schemas import (
    Attempt,
    BaselineAnalysis,
    CandidatePatch,
    Run,
    RunHealth,
    Verdict,
    VerdictStatus,
)
from patchproof.verification.engine import Engine


@pytest.fixture(scope="module")
def cases():
    return {lc.case.id: lc for lc in load_cases()}


def test_cases_load_and_cover_every_outcome(cases):
    assert len(cases) >= 20
    categories = {lc.case.category for lc in cases.values()}
    assert {"correct_fix", "regression_trap", "incomplete_fix", "prohibited_edit", "invalid_patch",
            "inconclusive", "flaky"} <= categories
    assert {lc.case.expected.verdict for lc in cases.values()} == set(VerdictStatus)
    for lc in cases.values():
        assert any(p.startswith("tests/") for p in lc.files)
        assert lc.case.expected.rationale


def test_flaky_cases_carry_the_deadline_placeholder(cases):
    flaky = [lc for lc in cases.values() if lc.case.category == "flaky"]
    assert flaky
    for lc in flaky:
        assert lc.case.flaky_deadline_s is not None
        assert lc.case.per_test_timeout_s >= lc.case.flaky_deadline_s + 5
        assert any(FLAKE_PLACEHOLDER in text for text in lc.files.values())


def test_materialize_substitutes_an_absolute_deadline(cases):
    lc = next(lc for lc in cases.values() if lc.case.flaky_deadline_s)
    files = materialize(lc.files, lc.case.flaky_deadline_s, now=1000.0)
    text = next(t for t in files.values() if "DEADLINE =" in t)
    assert FLAKE_PLACEHOLDER not in text
    assert f"DEADLINE = {1000.0 + lc.case.flaky_deadline_s!r}" in text
    assert materialize({"a.py": "x = 1\n"}, None, now=0.0) == {"a.py": "x = 1\n"}


def _copy_case(tmp_path, case_id):
    target = tmp_path / case_id
    shutil.copytree(CASES_DIR / case_id, target)
    return target


@pytest.mark.parametrize("mutate,message", [
    (lambda m: m.update(id="other-id"), "does not match its folder"),
    (lambda m: m["expected"].update(verdict="PROBABLY_FINE"), "invalid case.json"),
    (lambda m: m.update(flaky_deadline_s=3, per_test_timeout_s=10), "must appear in the project exactly when"),
    (lambda m: m.update(flaky_deadline_s=8, per_test_timeout_s=10), "at least 5 s longer"),
    (lambda m: m["patch"].update(edits=[]), "invalid case.json"),
])
def test_malformed_cases_are_rejected(tmp_path, mutate, message):
    folder = _copy_case(tmp_path, "cf-adult-age-boundary")
    meta = json.loads((folder / "case.json").read_text(encoding="utf-8"))
    mutate(meta)
    (folder / "case.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(BenchmarkSetupError, match=message):
        load_case(folder)


def test_unknown_case_ids_are_rejected():
    with pytest.raises(BenchmarkSetupError, match="Unknown case id"):
        load_cases(only=["no-such-case"])


def test_benchmark_settings_never_carry_an_api_key(settings):
    s = benchmark_settings(allow_local=False, base=dataclasses.replace(settings, api_key="sk-not-used"))
    assert s.api_key is None and s.sandbox_backend == "docker" and not s.allow_unsandboxed
    assert benchmark_settings(allow_local=True, base=settings).sandbox_backend == "local"


@pytest.mark.parametrize("case_id,verdict,regressions", [
    ("cf-adult-age-boundary", VerdictStatus.verified, 0),
    ("rt-clamp-max-only", VerdictStatus.failed, 1),
    ("if-unique-sorted", VerdictStatus.failed, 0),
    ("bl-edit-test-expectation", VerdictStatus.blocked, None),
    ("iv-search-not-found", VerdictStatus.failed, None),
    ("in-no-reproducible-failure", VerdictStatus.inconclusive, None),
])
def test_cases_run_through_the_real_engine(settings, cases, case_id, verdict, regressions):
    result = run_case(Engine(settings, None), cases[case_id])
    assert result.actual_verdict == verdict
    assert result.regressions == regressions
    assert result.matched, result.mismatches
    if result.patch_provenance is not None:
        assert result.patch_provenance == "benchmark_fixture"


def test_a_wrong_expectation_is_reported_not_hidden(settings, cases):
    lc = cases["rt-clamp-max-only"]
    wrong = dataclasses.replace(lc, case=lc.case.model_copy(update={
        "expected": lc.case.expected.model_copy(update={"verdict": VerdictStatus.verified, "regressions": 0})}))
    result = run_case(Engine(settings, None), wrong)
    assert not result.matched
    assert any("expected VERIFIED_FOR_TESTED_CASES" in m for m in result.mismatches)
    assert any("regressions 1, expected 0" in m for m in result.mismatches)


def test_report_round_trips_and_summarizes_actual_results(settings, cases, tmp_path):
    chosen = [cases["cf-adult-age-boundary"], cases["rt-clamp-max-only"], cases["bl-edit-test-expectation"]]
    report = run_benchmark(chosen, settings, require_docker=False)
    s = report.summary
    assert s["cases"] == 3 and s["matched"] == 3 and s["mismatched"] == []
    assert s["regressing_patches_rejected"] == 1 and s["prohibited_edits_blocked"] == 1
    assert s["expected_vs_actual"]["FAILED_VERIFICATION"] == {"FAILED_VERIFICATION": 1}
    assert report.sandbox_backend == "local" and "NOT isolated" in report.sandbox_detail

    json_path, md_path = write_report(report, tmp_path)
    assert BenchmarkReport.model_validate_json(json_path.read_text(encoding="utf-8")) == report
    md = md_path.read_text(encoding="utf-8")
    assert "**3 of 3 cases matched their expected outcome.**" in md
    assert "| `rt-clamp-max-only` | regression_trap | FAILED | FAILED | 1 / 1 |" in md
    assert "No model was called." in md


def test_markdown_lists_mismatches_with_the_engine_reasons(settings, cases):
    lc = cases["rt-clamp-max-only"]
    wrong = dataclasses.replace(lc, case=lc.case.model_copy(update={
        "expected": lc.case.expected.model_copy(update={"verdict": VerdictStatus.verified})}))
    md = render_markdown(run_benchmark([wrong], settings, require_docker=False))
    assert "## Mismatches" in md and "Engine reason: Regression:" in md


def test_docker_is_required_unless_explicitly_waived(settings, cases):
    missing = dataclasses.replace(settings, sandbox_image=f"patchproof-missing:{int(time.time())}")
    with pytest.raises(BenchmarkSetupError, match="Docker sandbox is required"):
        run_benchmark([cases["cf-adult-age-boundary"]], missing, require_docker=True)


def _finished_run(case, *, baseline_runs, candidate_runs, verdict_status, flaky=()):
    run = Run(id="r", created_at=0, state="finished", project_id="p", project_name="p", patch_source="scripted")
    run.baseline_runs = baseline_runs
    run.baseline = BaselineAnalysis(usable=True, targets=["t::bug"], passing=[], flaky=list(flaky))
    run.attempts = [Attempt(number=1, diagnosis_source="benchmark_fixture", candidate_runs=candidate_runs,
                            patch=CandidatePatch(provenance="benchmark_fixture", label="x", applied=True))]
    run.verdict = Verdict(status=verdict_status, headline="h")
    return run


def test_resource_limit_kill_is_an_explicit_checked_outcome(cases):
    case = cases["rt-digits-infinite-loop"].case
    assert case.expected.verdict == VerdictStatus.inconclusive and case.expected.resource_limit_kill is True
    base = [make_run("b", {"t::bug": "failed"})]
    killed = make_run("c", {}, health=RunHealth.sandbox_error).model_copy(
        update={"exit_code": 137, "detail": "Container was killed (exit 137), most likely by the memory limit."})
    ok = evaluate(case, _finished_run(case, baseline_runs=base, candidate_runs=[killed, killed],
                                      verdict_status=VerdictStatus.inconclusive), 1.0)
    assert ok.matched, ok.mismatches
    assert ok.regressions is None and len(ok.resource_limit_events) == 2
    # Same verdict, but no kill: the runner outcome the case exists to check did not happen.
    timed_out = make_run("c", {}, health=RunHealth.timeout)
    miss = evaluate(case, _finished_run(case, baseline_runs=base, candidate_runs=[timed_out],
                                        verdict_status=VerdictStatus.inconclusive), 1.0)
    assert not miss.matched and "resource limit" in miss.mismatches[0]


def test_flaky_case_reports_an_unmet_precondition(cases):
    case = cases["fl-flaky-bystander"].case
    base = [make_run("b", {"t::bug": "failed"})]
    result = evaluate(case, _finished_run(case, baseline_runs=base, candidate_runs=[],
                                          verdict_status=VerdictStatus.inconclusive, flaky=()), 1.0)
    assert any(m.startswith("precondition not met") for m in result.mismatches)
