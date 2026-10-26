"""Repair evaluation: does PatchProof's verify-and-revise workflow fix bugs better than a single model attempt?

This is separate from the deterministic benchmark (benchmark.py), which tests the *verifier* with scripted patches
and no model. Here a model really generates patches, and an independent scorer decides whether they are correct.

Design (each rule is enforced by code and covered by tests/test_evaluation.py):

- A task has a visible project (source plus some tests, the way a user would upload it), hidden tests, and a
  reference fix. Hidden tests are never in the model prompt or in the arm's workspace.
- Two arms get the same model, the same task and the same prompt inputs (baseline test evidence, issue, files):
    baseline   one model attempt; the patch is applied with PatchProof's patch rules but never executed or revised
    patchproof the full engine: execute baseline, generate, run the candidate in the sandbox, revise once if it fails
  The patchproof arm may use up to PATCHPROOF_MAX_AI_ATTEMPTS attempts; the baseline gets exactly one.
- Scoring never trusts an arm's workspace. The final diff is re-applied to the *pristine* visible project through the
  same patch policy (tests and configuration are protected), the hidden tests are added only then, and the whole
  suite runs in the Docker sandbox. "Correct" means every visible and hidden test passes, with the same tests the
  reference fix passes (nothing skipped or missing).
- A false verification is a PatchProof verdict of VERIFIED_FOR_TESTED_CASES on a patch the scorer calls incorrect.
- Results record outcomes, timings and token counts, never source code or issue text.

    uv run python -m patchproof.evaluation validate-tasks           # reference fixes pass, originals fail (Docker)
    uv run python -m patchproof.evaluation run --repeats 3 --out evaluation/results/<name>   # needs a model key
    uv run python -m patchproof.evaluation report evaluation/results/<name>/results.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__
from .agent.client import BharatCodeError
from .agent.diagnose import DiagnosisParseError
from .agent.prompts import build_diagnosis_messages
from .config import REPO_ROOT, Settings, load_settings
from .examples import load_project_dir
from .projects import build_project, validate_files
from .sandbox import SandboxRunner, docker_status
from .schemas import Attempt, PatchSource, Run, RunHealth, TestOutcome, TestRun, VerdictStatus
from .verification.engine import Engine
from .verification.patching import prepare_candidate
from .verification.verdict import analyze_baseline

TASKS_DIR = REPO_ROOT / "evaluation" / "tasks"
ARMS = ("baseline", "patchproof")
MIN_LEAK_LINE = 30  # a hidden-test line this long appearing in a prompt counts as a leak


class EvaluationError(RuntimeError):
    """The evaluation cannot be run or trusted as configured."""


@dataclass
class Task:
    id: str
    title: str
    kind: str  # "synthetic" (written for this repository) or "real" (taken from a public project, with a licence)
    issue: str
    visible: dict[str, str]
    hidden: dict[str, str]
    reference_diff: str
    license: str = ""


def load_tasks(root: Path = TASKS_DIR, only: set[str] | None = None) -> list[Task]:
    tasks = []
    for meta_path in sorted(root.glob("*/task.json")):
        folder = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if only and meta["id"] not in only:
            continue
        visible = {f.path: f.content for f in validate_files(load_project_dir(folder / "project"))}
        hidden = {p: b.decode("utf-8") for p, b in load_project_dir(folder / "hidden")}
        clash = set(visible) & set(hidden)
        if clash:
            raise EvaluationError(f"{meta['id']}: hidden tests share paths with visible files: {sorted(clash)}")
        if meta.get("kind") not in ("synthetic", "real"):
            raise EvaluationError(f"{meta['id']}: kind must be 'synthetic' or 'real'")
        tasks.append(Task(id=meta["id"], title=meta["title"], kind=meta["kind"], issue=meta["issue"], visible=visible,
                          hidden=hidden, reference_diff=(folder / "reference.diff").read_text(encoding="utf-8"),
                          license=meta.get("license", "")))
    return tasks


# -- keeping hidden tests hidden ------------------------------------------------------------------------------


def leaked_from(messages: list[dict], task: Task) -> list[str]:
    """Hidden paths or hidden-test lines found in text about to be sent to the model."""
    text = "\n".join(str(m.get("content", "")) for m in messages)
    found = [p for p in task.hidden if p in text]
    for path, content in task.hidden.items():
        for line in content.splitlines():
            stripped = line.strip()
            if len(stripped) >= MIN_LEAK_LINE and stripped in text:
                found.append(f"{path}: {stripped[:50]}")
                break
    return found


class LeakGuard:
    """Wraps a model client and refuses any request that would reveal a hidden test."""

    def __init__(self, client, task: Task):
        self._client, self._task = client, task
        self.configured = getattr(client, "configured", True)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def chat(self, messages, **kwargs):
        leaks = leaked_from(messages, self._task)
        if leaks:
            raise EvaluationError(f"{self._task.id}: hidden tests would be sent to the model: {leaks}")
        return self._client.chat(messages, **kwargs)


# -- independent scoring ----------------------------------------------------------------------------------------


@dataclass
class TaskContext:
    """What the scorer needs from the pristine task, computed once."""

    baseline_passing: set[str]
    reference_ids: set[str]  # every test the reference fix passes (visible + hidden)


@dataclass
class Score:
    outcome: str  # correct | incorrect | regression | no_patch | rejected | unscored
    detail: str = ""
    hidden_failed: int = 0
    visible_failed: int = 0
    patch_sha256: str | None = None
    changed_files: int = 0


def _ok(run: TestRun) -> bool:
    return run.health == RunHealth.ok


def prepare_task(engine: Engine, task: Task, runner: SandboxRunner) -> TaskContext:
    """Validate the task: the original must fail a visible test, and the reference fix must pass everything."""
    original = engine.execute_tests(task.visible, runner, f"{task.id}: original")
    if not _ok(original):
        raise EvaluationError(f"{task.id}: the original project did not run cleanly ({original.health.value})")
    if not any(t.outcome != TestOutcome.passed for t in original.tests):
        raise EvaluationError(f"{task.id}: the original project passes every visible test, so there is no bug to fix")
    candidate, fixed = prepare_candidate(task.visible, provenance="benchmark_fixture", label="reference",
                                         diff=task.reference_diff)
    if fixed is None:
        raise EvaluationError(f"{task.id}: the reference fix was rejected: "
                              + "; ".join(f.message for f in candidate.policy_findings))
    full = engine.execute_tests({**fixed, **task.hidden}, runner, f"{task.id}: reference")
    bad = [t.nodeid for t in full.tests if t.outcome != TestOutcome.passed]
    if not _ok(full) or bad or full.collection_errors:
        raise EvaluationError(f"{task.id}: the reference fix does not pass every visible and hidden test: {bad}")
    hidden_ids = {t.nodeid for t in full.tests if any(t.nodeid.startswith(p) for p in task.hidden)}
    if not hidden_ids:
        raise EvaluationError(f"{task.id}: no hidden tests ran")
    return TaskContext(
        baseline_passing={t.nodeid for t in original.tests if t.outcome == TestOutcome.passed},
        reference_ids={t.nodeid for t in full.tests})


def score_patch(engine: Engine, task: Task, ctx: TaskContext, runner: SandboxRunner, diff: str | None,
                runs: int = 2) -> Score:
    """Decide independently whether `diff` fixes the task. The arm's own workspace and verdict are not consulted."""
    if not diff or not diff.strip():
        return Score("no_patch", "the arm produced no patch")
    digest = hashlib.sha256(diff.encode()).hexdigest()
    candidate, patched = prepare_candidate(task.visible, provenance="live_model", label="scored", diff=diff)
    if patched is None:
        return Score("rejected", "; ".join(f.message for f in candidate.policy_findings[:3]) or "patch not applicable",
                     patch_sha256=digest)
    touched = set(candidate.changed_files) & set(task.hidden)
    if touched:
        return Score("rejected", f"the patch touches hidden test paths: {sorted(touched)}", patch_sha256=digest)
    suite = {**patched, **task.hidden}  # hidden tests enter only here, after the patch is applied
    results = [engine.execute_tests(suite, runner, f"{task.id}: scoring {i + 1}/{runs}") for i in range(runs)]
    if not all(_ok(r) for r in results):
        return Score("unscored", "; ".join(sorted({r.health.value for r in results if not _ok(r)})),
                     patch_sha256=digest, changed_files=len(candidate.changed_files))
    outcomes = [{t.nodeid: t.outcome for t in r.tests} for r in results]
    if any(o != outcomes[0] for o in outcomes[1:]):
        return Score("unscored", "test outcomes differed between scoring runs (unstable)", patch_sha256=digest,
                     changed_files=len(candidate.changed_files))
    final = outcomes[0]
    missing = set(ctx.reference_ids) - set(final)  # a test the reference run has but this run lacks
    hidden_failed = sum(1 for n, o in final.items() if o != TestOutcome.passed and any(n.startswith(p) for p in task.hidden))
    visible_failed = sum(1 for n, o in final.items() if o != TestOutcome.passed) - hidden_failed
    errors = bool(results[0].collection_errors)
    common = {"hidden_failed": hidden_failed, "visible_failed": visible_failed, "patch_sha256": digest,
              "changed_files": len(candidate.changed_files)}
    if not hidden_failed and not visible_failed and not missing and not errors:
        return Score("correct", **common)
    regressed = [n for n in ctx.baseline_passing if final.get(n) != TestOutcome.passed]
    if regressed:
        return Score("regression", f"{len(regressed)} previously passing test(s) now fail", **common)
    detail = "tests missing from the run" if missing else "hidden or visible tests still fail"
    return Score("incorrect", detail, **common)


# -- arms -------------------------------------------------------------------------------------------------------


@dataclass
class ArmResult:
    task_id: str
    kind: str
    arm: str
    repeat: int
    model: str
    outcome: str
    detail: str
    verdict: str | None
    false_verification: bool
    hidden_failed: int
    visible_failed: int
    attempts: int
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    duration_s: float
    patch_sha256: str | None
    changed_files: int
    started_at: float = field(default_factory=time.time)


def _usage(attempts: list[Attempt]) -> tuple[int, int, int]:
    calls = [c for a in attempts for c in a.model_calls]
    return len(calls), sum(c.prompt_tokens or 0 for c in calls), sum(c.completion_tokens or 0 for c in calls)


def run_patchproof_arm(engine: Engine, task: Task, runner: SandboxRunner) -> tuple[str | None, str | None, Run]:
    """(final diff, verdict, run). The diff is the last applied patch the engine produced, if any."""
    run = Run(id=f"eval_{uuid.uuid4().hex[:10]}", created_at=time.time(), state="queued", project_id=task.id,
              project_name=task.id, patch_source=PatchSource.ai, issue=task.issue)
    project = build_project(task.id, "upload", validate_files([(p, c.encode()) for p, c in task.visible.items()]))
    engine.execute(run, project)
    patch = next((a.patch for a in reversed(run.attempts) if a.patch and a.patch.applied and a.patch.diff), None)
    return (patch.diff if patch else None), (run.verdict.status.value if run.verdict else None), run


def run_baseline_arm(engine: Engine, task: Task, runner: SandboxRunner) -> tuple[str | None, list[Attempt], str]:
    """One model attempt with the same prompt inputs as the engine, never executed, never revised."""
    baseline = [engine.execute_tests(task.visible, runner, f"Baseline run {i + 1}")
                for i in range(engine.settings.verification_runs)]
    analysis = analyze_baseline(baseline)
    if not analysis.usable:
        return None, [], "the baseline did not reproduce a failure"
    messages, _ = build_diagnosis_messages(task.visible, baseline, analysis.targets, task.issue)
    attempt = Attempt(number=1, diagnosis_source="live_model")
    try:
        _, edits, _ = engine._ask_model(messages, attempt, "diagnose")  # noqa: SLF001 - same call the engine makes
    except (BharatCodeError, DiagnosisParseError) as exc:
        return None, [attempt], f"no usable model output: {type(exc).__name__}"
    candidate, patched = prepare_candidate(task.visible, provenance="live_model", label="baseline attempt", edits=edits)
    return (candidate.diff if patched is not None else None), [attempt], "" if patched is not None else "patch rejected"


def evaluate_task(engine: Engine, task: Task, ctx: TaskContext, runner: SandboxRunner, arm: str, repeat: int,
                  client) -> ArmResult:
    guarded = Engine(engine.settings, LeakGuard(client, task), docker_runner=engine.docker_runner,
                     local_runner=engine.local_runner, docker_check=engine.docker_check)
    started = time.monotonic()
    verdict: str | None = None
    attempts: list[Attempt] = []
    try:
        if arm == "patchproof":
            diff, verdict, run = run_patchproof_arm(guarded, task, runner)
            attempts = run.attempts
            note = run.error or ""
        else:
            diff, attempts, note = run_baseline_arm(guarded, task, runner)
    except EvaluationError:
        raise  # a leak or a broken task invalidates the whole evaluation; never record it as a result
    duration = time.monotonic() - started
    score = score_patch(engine, task, ctx, runner, diff)
    calls, prompt_tokens, completion_tokens = _usage(attempts)
    return ArmResult(
        task_id=task.id, kind=task.kind, arm=arm, repeat=repeat, model=engine.settings.model, outcome=score.outcome,
        detail=score.detail or note, verdict=verdict,
        false_verification=verdict == VerdictStatus.verified.value and score.outcome != "correct",
        hidden_failed=score.hidden_failed, visible_failed=score.visible_failed, attempts=len(attempts),
        model_calls=calls, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        duration_s=round(duration, 2), patch_sha256=score.patch_sha256, changed_files=score.changed_files)


# -- statistics and report ----------------------------------------------------------------------------------------


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for a proportion, or None when there is no data."""
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize(results: list[dict]) -> dict:
    out: dict = {}
    for arm in ARMS:
        rows = [r for r in results if r["arm"] == arm]
        scored = [r for r in rows if r["outcome"] != "unscored"]
        n = len(scored)
        count = lambda o, rows_=scored: sum(r["outcome"] == o for r in rows_)  # noqa: E731
        verified = [r for r in scored if r["verdict"] == VerdictStatus.verified.value]
        out[arm] = {
            "arm": arm, "results": len(rows), "scored": n, "unscored": len(rows) - n,
            "correct": count("correct"), "incorrect": count("incorrect"), "regression": count("regression"),
            "no_patch": count("no_patch"), "rejected": count("rejected"),
            "fix_rate": (count("correct") / n if n else None), "fix_rate_ci95": wilson(count("correct"), n),
            "regression_rate": (count("regression") / n if n else None),
            "verified": len(verified), "false_verifications": sum(r["false_verification"] for r in verified),
            "false_verification_rate": (sum(r["false_verification"] for r in verified) / len(verified)) if verified else None,
            "false_verification_ci95": wilson(sum(r["false_verification"] for r in verified), len(verified)),
            "inconclusive": sum(r["verdict"] == VerdictStatus.inconclusive.value for r in scored),
            "median_time_s": statistics.median(r["duration_s"] for r in scored) if scored else None,
            "model_tokens": sum(r["prompt_tokens"] + r["completion_tokens"] for r in rows),
        }
    return out


def render_report(doc: dict) -> str:
    s, meta = doc["summary"], doc["meta"]
    pct = lambda v: "n/a" if v is None else f"{v * 100:.0f}%"  # noqa: E731
    ci = lambda v: "" if v is None else f" ({v[0] * 100:.0f}-{v[1] * 100:.0f}%)"  # noqa: E731
    lines = [
        "# PatchProof repair evaluation", "",
        f"- Model: `{meta['model']}`; PatchProof {meta['version']}; {meta['tasks']} tasks "
        f"({meta['synthetic']} synthetic, {meta['real']} real), {meta['repeats']} repeat(s) per arm",
        f"- Run on {meta['platform']} at {meta['when']}", "",
        "| | baseline (one attempt) | PatchProof (verify and revise) |", "|---|---|---|",
    ]
    rows = [
        ("Scored results", lambda a: str(a["scored"])), ("Unscored (infrastructure)", lambda a: str(a["unscored"])),
        ("Independently correct", lambda a: f"{a['correct']} ({pct(a['fix_rate'])}){ci(a['fix_rate_ci95'])}"),
        ("Incorrect", lambda a: str(a["incorrect"])), ("Regressions", lambda a: f"{a['regression']} ({pct(a['regression_rate'])})"),
        ("No patch / rejected", lambda a: f"{a['no_patch']} / {a['rejected']}"),
        ("Claimed verified", lambda a: str(a["verified"]) if a["arm"] == "patchproof" else "n/a (does not verify)"),
        ("False verifications", lambda a: f"{a['false_verifications']} ({pct(a['false_verification_rate'])})"
         f"{ci(a['false_verification_ci95'])}" if a["verified"] else "n/a"),
        ("Inconclusive", lambda a: str(a["inconclusive"])),
        ("Median time", lambda a: "n/a" if a["median_time_s"] is None else f"{a['median_time_s']:.1f} s"),
        ("Model tokens", lambda a: str(a["model_tokens"])),
    ]
    for label, f in rows:
        lines.append(f"| {label} | {f(s['baseline'])} | {f(s['patchproof'])} |")
    lines += ["", "## What this does and does not show", "",
              "- A result is *independently correct* only if the patch, re-applied to the pristine project, passes every "
              "visible and hidden test. The arms' own verdicts are not used for correctness.",
              "- Intervals are 95% Wilson intervals over results, which treat repeats of the same task as independent. "
              "They understate uncertainty when there are few tasks.",
              "- Synthetic tasks are small and written for this repository. They show the harness works; they do not "
              "show how PatchProof performs on real-world projects.",
              "- The baseline gets one attempt; PatchProof gets up to the configured number of attempts. Part of any "
              "difference is therefore extra model calls, which the token row shows.",
              "- Cost is reported as model tokens only; sandbox and hosting costs are not included.",
              "- No claim should be made beyond the task count above. With few tasks, differences are anecdotes."]
    if doc.get("exclusions"):
        lines += ["", "## Excluded or unscored", *[f"- {e}" for e in doc["exclusions"]]]
    return "\n".join(lines) + "\n"


# -- command line -----------------------------------------------------------------------------------------------


def _engine(settings: Settings, client) -> Engine:
    return Engine(settings, client)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="patchproof.evaluation", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-tasks")
    run_p = sub.add_parser("run")
    run_p.add_argument("--repeats", type=int, default=3)
    run_p.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    run_p.add_argument("--arms", default=",".join(ARMS))
    run_p.add_argument("--out", required=True)
    rep_p = sub.add_parser("report")
    rep_p.add_argument("results")
    args = parser.parse_args(argv[1:])

    if args.command == "report":
        print(render_report(json.loads(Path(args.results).read_text(encoding="utf-8"))))
        return 0

    settings = load_settings()
    ok, detail = docker_status(settings.sandbox_image)
    if not ok:
        print(f"The Docker sandbox is required: {detail}", file=sys.stderr)
        return 2
    from .agent.client import BharatCodeClient

    client = BharatCodeClient(settings.api_key, settings.base_url, timeout_s=settings.request_timeout_s,
                              max_retries=settings.max_retries) if settings.api_key else None
    engine = _engine(settings, client)
    only = {t for t in args.tasks.split(",") if t} if args.command == "run" else None
    tasks = load_tasks(only=only or None)
    runner = engine.docker_runner
    contexts = {}
    for task in tasks:
        contexts[task.id] = prepare_task(engine, task, runner)
        print(f"task ok   {task.id} ({task.kind})")
    if args.command == "validate-tasks":
        print(f"{len(tasks)} task(s): originals fail, reference fixes pass every visible and hidden test.")
        return 0

    if client is None or not client.configured:
        print("No BHARATCODE_API_KEY is configured, so no model results can be produced. Nothing was run.", file=sys.stderr)
        return 2
    results, exclusions = [], []
    arms = [a for a in args.arms.split(",") if a in ARMS]
    for repeat in range(1, args.repeats + 1):
        for task in tasks:
            for arm in arms:
                try:
                    r = evaluate_task(engine, task, contexts[task.id], runner, arm, repeat, client)
                except BharatCodeError as exc:
                    exclusions.append(f"{task.id} / {arm} / repeat {repeat}: model unavailable ({exc.kind})")
                    continue
                results.append(asdict(r))
                print(f"{task.id:18} {arm:10} #{repeat} {r.outcome:10} verdict={r.verdict} {r.duration_s:.1f}s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = {
        "meta": {"version": __version__, "model": settings.model, "tasks": len(tasks), "repeats": args.repeats,
                 "synthetic": sum(t.kind == "synthetic" for t in tasks), "real": sum(t.kind == "real" for t in tasks),
                 "platform": platform.platform(), "when": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())},
        "summary": summarize(results), "results": results,
        "exclusions": exclusions + [f"{r['task_id']} / {r['arm']}: {r['outcome']} ({r['detail']})"
                                    for r in results if r["outcome"] == "unscored"],
    }
    (out / "results.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (out / "report.md").write_text(render_report(doc), encoding="utf-8")
    print(f"Wrote {out / 'results.json'} and {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
