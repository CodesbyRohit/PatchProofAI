"""Deterministic benchmark: fixed bug-fix cases run through the real verification engine.

Every case is a small Python project with a pytest suite plus one scripted candidate patch, stored in
benchmark/cases/<id>/. No model is ever called: the engine runs without a BharatCode client and every
patch is recorded with provenance "benchmark_fixture". Expected outcomes are written in each case.json
before the case is run; a mismatch is reported, never silently accepted.

    uv run python -m patchproof.benchmark                 # all cases, Docker sandbox required
    uv run python -m patchproof.benchmark --only cf-mean-of-empty --out /tmp/bench
"""

from __future__ import annotations

import argparse
import dataclasses
import platform
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import __version__
from .config import REPO_ROOT, Settings, load_settings
from .examples import ScriptedPatch, load_project_dir
from .projects import build_project, validate_files
from .sandbox import docker_status
from .schemas import ComparisonCategory, FileEdit, PatchSource, Run, RunHealth, VerdictStatus
from .verification.engine import Engine

BENCHMARK_DIR = REPO_ROOT / "benchmark"
CASES_DIR = BENCHMARK_DIR / "cases"
RESULTS_DIR = BENCHMARK_DIR / "results"
FLAKE_PLACEHOLDER = "__PATCHPROOF_FLAKE_DEADLINE__"

Category = Literal[
    "correct_fix", "regression_trap", "incomplete_fix", "prohibited_edit", "invalid_patch",
    "collection_break", "inconclusive", "flaky", "resource_limit",
]


class BenchmarkSetupError(RuntimeError):
    """The benchmark cannot run as requested (e.g. no Docker sandbox, malformed case)."""


# ---------------------------------------------------------------------------
# Case definitions
# ---------------------------------------------------------------------------


class CasePatch(BaseModel):
    label: str
    description: str = ""
    edits: list[FileEdit] = Field(default_factory=list)
    diff: str | None = None

    @model_validator(mode="after")
    def _has_content(self) -> CasePatch:
        if not self.edits and not self.diff:
            raise ValueError("a case patch needs edits or a diff")
        return self


class Expected(BaseModel):
    verdict: VerdictStatus
    # Tests that passed at baseline and failed or vanished after the patch. None: not checked
    # (the patch is never executed, or the count is not the point of the case).
    regressions: int | None = None
    # Explicit runner outcome: True when the case must end with a patched run killed by a resource
    # limit (exit 137), False when it must not. None: not checked.
    resource_limit_kill: bool | None = None
    rationale: str


class BenchmarkCase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    title: str
    category: Category
    patch: CasePatch
    expected: Expected
    # Seconds after the case starts at which the flaky test's deadline falls (see FLAKE_PLACEHOLDER).
    flaky_deadline_s: float | None = Field(default=None, gt=0)
    # Per-test timeout for this case, overriding the server default. Flaky cases need one longer than
    # their deadline, because the flaky test sleeps until the deadline in the first baseline run.
    per_test_timeout_s: int | None = Field(default=None, gt=0, le=60)

    @model_validator(mode="after")
    def _deadline_fits(self) -> BenchmarkCase:
        if self.flaky_deadline_s is not None:
            if self.per_test_timeout_s is None or self.per_test_timeout_s < self.flaky_deadline_s + 5:
                raise ValueError("flaky cases need per_test_timeout_s at least 5 s longer than flaky_deadline_s")
        return self


@dataclasses.dataclass(frozen=True)
class LoadedCase:
    case: BenchmarkCase
    files: dict[str, str]


def load_case(folder: Path) -> LoadedCase:
    meta = folder / "case.json"
    if not meta.is_file():
        raise BenchmarkSetupError(f"{folder.name}: missing case.json")
    try:
        case = BenchmarkCase.model_validate_json(meta.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BenchmarkSetupError(f"{folder.name}: invalid case.json: {exc}") from exc
    if case.id != folder.name:
        raise BenchmarkSetupError(f"{folder.name}: case id {case.id!r} does not match its folder")
    project_dir = folder / "project"
    if not project_dir.is_dir():
        raise BenchmarkSetupError(f"{case.id}: missing project/ directory")
    files = {path: data.decode("utf-8") for path, data in load_project_dir(project_dir)}
    if not any(p.startswith("tests/") for p in files):
        raise BenchmarkSetupError(f"{case.id}: project has no tests/ directory")
    has_placeholder = any(FLAKE_PLACEHOLDER in text for text in files.values())
    if has_placeholder != (case.flaky_deadline_s is not None):
        raise BenchmarkSetupError(
            f"{case.id}: {FLAKE_PLACEHOLDER} must appear in the project exactly when flaky_deadline_s is set")
    return LoadedCase(case, files)


def load_cases(cases_dir: Path = CASES_DIR, only: list[str] | None = None) -> list[LoadedCase]:
    if not cases_dir.is_dir():
        raise BenchmarkSetupError(f"No benchmark cases directory at {cases_dir}")
    loaded = [load_case(d) for d in sorted(cases_dir.iterdir()) if d.is_dir()]
    if only:
        unknown = sorted(set(only) - {lc.case.id for lc in loaded})
        if unknown:
            raise BenchmarkSetupError(f"Unknown case id(s): {', '.join(unknown)}")
        loaded = [lc for lc in loaded if lc.case.id in only]
    return loaded


def materialize(files: dict[str, str], deadline_s: float | None, now: float) -> dict[str, str]:
    """Substitute the flaky-test deadline (an absolute epoch time) into the project files."""
    if deadline_s is None:
        return dict(files)
    stamp = repr(now + deadline_s)
    return {path: text.replace(FLAKE_PLACEHOLDER, stamp) for path, text in files.items()}


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class TestSummary(BaseModel):
    nodeid: str
    baseline: str
    candidate: str
    category: ComparisonCategory
    is_target: bool


class RunCounts(BaseModel):
    label: str
    health: str
    passed: int
    failed: int
    error: int
    skipped: int
    collection_errors: int
    exit_code: int | None
    detail: str | None


class CaseResult(BaseModel):
    id: str
    title: str
    category: Category
    expected: Expected
    actual_verdict: VerdictStatus | None
    headline: str | None
    reasons: list[str]
    run_state: str
    error: str | None = None
    patch_provenance: str | None
    patch_applied: bool
    policy_findings: list[str]
    targets: list[str]
    flaky: list[str]
    fixed_targets: int
    # None when no patched run completed (blocked/invalid patch, unusable baseline, crashed sandbox).
    regressions: int | None
    still_failing: int
    # Patched runs killed by a resource limit (container exit 137). Reported as a serious warning:
    # the patch could not be verified safely, which is different from being shown to be wrong.
    resource_limit_events: list[str]
    baseline_runs: list[RunCounts]
    candidate_runs: list[RunCounts]
    tests: list[TestSummary]
    sandbox: str | None
    sandbox_isolated: bool
    duration_s: float
    matched: bool
    mismatches: list[str]


class BenchmarkReport(BaseModel):
    patchproof_version: str
    started_at: float
    duration_s: float
    sandbox_backend: str
    sandbox_detail: str
    verification_runs: int
    per_test_timeout_s: int
    python: str
    platform: str
    cases: list[CaseResult]
    summary: dict


def _counts(run) -> RunCounts:
    c = run.counts()
    return RunCounts(label=run.label, health=run.health.value, passed=c["passed"], failed=c["failed"],
                     error=c["error"], skipped=c["skipped"], collection_errors=len(run.collection_errors),
                     exit_code=run.exit_code, detail=run.detail)


def evaluate(case: BenchmarkCase, run: Run, duration_s: float) -> CaseResult:
    verdict = run.verdict
    attempt = run.attempts[-1] if run.attempts else None
    patch = attempt.patch if attempt else None
    candidate_runs = attempt.candidate_runs if attempt else []
    comparison = verdict.comparison if verdict else []
    cats = Counter(c.category for c in comparison)
    # Regressions are only measurable when at least one patched run completed with per-test results.
    executed = any(r.health == RunHealth.ok for r in candidate_runs)
    regressions = cats[ComparisonCategory.regression] + cats[ComparisonCategory.missing] if executed else None

    killed = [f"{r.label}: {r.detail}" for r in candidate_runs
              if r.health == RunHealth.sandbox_error and r.exit_code == 137]
    mismatches: list[str] = []
    if run.state != "finished" or verdict is None:
        mismatches.append(f"run did not finish (state {run.state}): {run.error or 'no verdict'}")
    elif verdict.status != case.expected.verdict:
        mismatches.append(f"verdict {verdict.status.value}, expected {case.expected.verdict.value}")
    if case.expected.regressions is not None and regressions != case.expected.regressions:
        mismatches.append(f"regressions {regressions}, expected {case.expected.regressions}")
    if case.expected.resource_limit_kill is not None and bool(killed) != case.expected.resource_limit_kill:
        mismatches.append("expected a patched run to be killed by a resource limit, but none was"
                          if case.expected.resource_limit_kill else f"unexpected resource-limit kill: {killed[0]}")
    if case.flaky_deadline_s is not None and not (run.baseline and run.baseline.flaky):
        mismatches.append("precondition not met: the time-dependent test did not change outcome between the "
                          "baseline runs (the first baseline run probably started after its deadline)")
    if patch and patch.provenance != "benchmark_fixture":
        mismatches.append(f"patch provenance {patch.provenance}, expected benchmark_fixture")

    return CaseResult(
        id=case.id, title=case.title, category=case.category, expected=case.expected,
        actual_verdict=verdict.status if verdict else None,
        headline=verdict.headline if verdict else None,
        reasons=verdict.reasons if verdict else [],
        run_state=run.state, error=run.error,
        patch_provenance=patch.provenance if patch else None,
        patch_applied=bool(patch and patch.applied),
        policy_findings=[f"{f.rule}: {f.message}" for f in patch.policy_findings] if patch else [],
        targets=run.baseline.targets if run.baseline else [],
        flaky=run.baseline.flaky if run.baseline else [],
        fixed_targets=sum(1 for c in comparison if c.is_target and c.category == ComparisonCategory.fixed),
        regressions=regressions,
        still_failing=cats[ComparisonCategory.still_failing],
        baseline_runs=[_counts(r) for r in run.baseline_runs],
        candidate_runs=[_counts(r) for r in candidate_runs],
        tests=[TestSummary(nodeid=c.nodeid, baseline=c.baseline, candidate=c.candidate, category=c.category,
                           is_target=c.is_target) for c in comparison],
        resource_limit_events=killed,
        sandbox=run.sandbox, sandbox_isolated=run.sandbox_isolated,
        duration_s=round(duration_s, 2),
        matched=not mismatches, mismatches=mismatches,
    )


def run_case(engine: Engine, loaded: LoadedCase) -> CaseResult:
    case = loaded.case
    if case.per_test_timeout_s is not None:
        engine = Engine(dataclasses.replace(engine.settings, per_test_timeout_s=case.per_test_timeout_s), None)
    start = time.monotonic()
    files = materialize(loaded.files, case.flaky_deadline_s, time.time())
    project = build_project(case.title, "example",
                            validate_files((p, t.encode("utf-8")) for p, t in files.items()),
                            trusted=True, project_id=f"benchmark:{case.id}")
    scripted = ScriptedPatch(id=case.id, label=case.patch.label, description=case.patch.description,
                             edits=case.patch.edits, diff=case.patch.diff)
    run = Run(id=f"bench_{case.id}", created_at=time.time(), state="queued", project_id=project.id,
              project_name=case.title, patch_source=PatchSource.scripted)
    engine.execute(run, project, scripted=scripted, scripted_provenance="benchmark_fixture")
    return evaluate(case, run, time.monotonic() - start)


def summarize(results: list[CaseResult]) -> dict:
    """Aggregate metrics, computed only from the results of this run."""
    by_category: dict[str, dict[str, int]] = {}
    for r in results:
        entry = by_category.setdefault(r.category, {"cases": 0, "matched": 0})
        entry["cases"] += 1
        entry["matched"] += r.matched
    confusion: dict[str, dict[str, int]] = {}
    for r in results:
        row = confusion.setdefault(r.expected.verdict.value, {})
        actual = r.actual_verdict.value if r.actual_verdict else "NO_VERDICT"
        row[actual] = row.get(actual, 0) + 1
    regressing = [r for r in results if r.expected.verdict == VerdictStatus.failed and (r.expected.regressions or 0) > 0]
    return {
        "cases": len(results),
        "matched": sum(r.matched for r in results),
        "mismatched": [r.id for r in results if not r.matched],
        "by_category": by_category,
        "expected_vs_actual": confusion,
        "regressing_patches_rejected": sum(1 for r in regressing if r.actual_verdict == VerdictStatus.failed),
        "regressing_patches": len(regressing),
        "regressions_observed": sum(r.regressions or 0 for r in results),
        "prohibited_edits_blocked": sum(1 for r in results if r.category == "prohibited_edit"
                                        and r.actual_verdict == VerdictStatus.blocked),
        "prohibited_edits": sum(1 for r in results if r.category == "prohibited_edit"),
        "false_verified": [r.id for r in results if r.actual_verdict == VerdictStatus.verified
                           and r.expected.verdict != VerdictStatus.verified],
    }


def benchmark_settings(allow_local: bool, base: Settings | None = None) -> Settings:
    base = base or load_settings()
    return dataclasses.replace(base, api_key=None, sandbox_backend="local" if allow_local else "docker",
                               allow_unsandboxed=False)


def run_benchmark(cases: list[LoadedCase], settings: Settings, *, require_docker: bool = True,
                  progress=None) -> BenchmarkReport:
    if require_docker:
        ok, detail = docker_status(settings.sandbox_image, ttl_s=0)
        if not ok:
            raise BenchmarkSetupError(f"The Docker sandbox is required but unavailable: {detail}")
    else:
        detail = "local subprocess (NOT isolated; results may depend on the host's installed packages)"
    engine = Engine(settings, client=None)  # no model client: the benchmark can never call BharatCode
    started, t0 = time.time(), time.monotonic()
    results = []
    for i, loaded in enumerate(cases, 1):
        result = run_case(engine, loaded)
        results.append(result)
        if progress:
            progress(i, len(cases), result)
    return BenchmarkReport(
        patchproof_version=__version__, started_at=started, duration_s=round(time.monotonic() - t0, 2),
        sandbox_backend=settings.sandbox_backend, sandbox_detail=detail,
        verification_runs=settings.verification_runs, per_test_timeout_s=settings.per_test_timeout_s,
        python=platform.python_version(), platform=platform.platform(terse=True),
        cases=results, summary=summarize(results),
    )


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

_SHORT = {
    "VERIFIED_FOR_TESTED_CASES": "VERIFIED",
    "FAILED_VERIFICATION": "FAILED",
    "INCONCLUSIVE": "INCONCLUSIVE",
    "BLOCKED_BY_SAFETY_POLICY": "BLOCKED",
}


def _short(v: VerdictStatus | None) -> str:
    return _SHORT.get(v.value, v.value) if v else "-"


def _reg(n: int | None) -> str:
    return "-" if n is None else str(n)


def render_markdown(report: BenchmarkReport) -> str:
    s = report.summary
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(report.started_at))
    lines = [
        "# PatchProof benchmark results",
        "",
        f"Run {when} · PatchProof {report.patchproof_version} · sandbox: {report.sandbox_backend} "
        f"({report.sandbox_detail}) · {report.verification_runs} baseline + {report.verification_runs} patched "
        f"runs per case · Python {report.python} · {report.duration_s:.0f} s total",
        "",
        "All patches are scripted benchmark fixtures. No model was called.",
        "",
        f"**{s['matched']} of {s['cases']} cases matched their expected outcome.**",
        "",
        f"- Regressing patches rejected: {s['regressing_patches_rejected']} of {s['regressing_patches']}",
        f"- Prohibited edits blocked: {s['prohibited_edits_blocked']} of {s['prohibited_edits']}",
        f"- Regressions observed across all cases: {s['regressions_observed']}",
        f"- Patches verified when they should not have been: {len(s['false_verified'])}"
        + (f" ({', '.join(s['false_verified'])})" if s["false_verified"] else ""),
        "",
        "| Case | Category | Expected | Actual | Regressions (expected / actual) | Time | Match |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report.cases:
        lines.append(
            f"| `{r.id}` | {r.category} | {_short(r.expected.verdict)} | {_short(r.actual_verdict)} | "
            f"{_reg(r.expected.regressions)} / {_reg(r.regressions)} | {r.duration_s:.1f} s | "
            f"{'yes' if r.matched else '**no**'} |"
        )
    killed = [r for r in report.cases if r.resource_limit_events]
    if killed:
        lines += ["", "## Resource-limit warnings", "",
                  "Patched runs killed by a sandbox resource limit. The patch could not be verified safely; "
                  "this is reported as INCONCLUSIVE, not as proof that the patch is wrong.", ""]
        lines += [f"- `{r.id}`: {event}" for r in killed for event in r.resource_limit_events]
    mismatched = [r for r in report.cases if not r.matched]
    if mismatched:
        lines += ["", "## Mismatches", ""]
        for r in mismatched:
            lines.append(f"### `{r.id}`: {r.title}")
            lines.append("")
            lines += [f"- {m}" for m in r.mismatches]
            lines.append(f"- Expected because: {r.expected.rationale}")
            if r.headline:
                lines.append(f"- Engine headline: {r.headline}")
            lines += [f"- Engine reason: {reason}" for reason in r.reasons[:5]]
            if r.flaky:
                lines.append(f"- Flaky at baseline: {', '.join(r.flaky)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: BenchmarkReport, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "results.json", out_dir / "results.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m patchproof.benchmark", description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", type=Path, default=CASES_DIR, help="cases directory (default: benchmark/cases)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR, help="output directory (default: benchmark/results)")
    parser.add_argument("--only", nargs="+", metavar="ID", help="run only these case ids")
    parser.add_argument("--allow-local", action="store_true",
                        help="run without Docker as a local subprocess (not isolated; for development only)")
    args = parser.parse_args(argv)

    def progress(i: int, n: int, r: CaseResult) -> None:
        mark = "ok  " if r.matched else "MISS"
        print(f"[{i:>2}/{n}] {mark} {r.id:<32} {_short(r.actual_verdict):<12} "
              f"regressions={_reg(r.regressions):<2} {r.duration_s:5.1f}s"
              + ("" if r.matched else "  <- " + "; ".join(r.mismatches)), flush=True)

    try:
        cases = load_cases(args.cases, args.only)
        report = run_benchmark(cases, benchmark_settings(args.allow_local), require_docker=not args.allow_local,
                               progress=progress)
    except BenchmarkSetupError as exc:
        print(f"benchmark setup error: {exc}", file=sys.stderr)
        return 2
    json_path, md_path = write_report(report, args.out)
    s = report.summary
    print(f"\n{s['matched']}/{s['cases']} cases matched in {report.duration_s:.0f}s. Wrote {json_path} and {md_path}")
    return 0 if not s["mismatched"] else 1


if __name__ == "__main__":
    sys.exit(main())
