"""The repair-evaluation harness: task validity, independent scoring, tamper resistance, hidden-test secrecy, statistics.

Tasks here are repo-authored and trusted, so scoring runs on the local runner; the end-to-end arms that call the
(scripted) model run in the real Docker sandbox in test_docker_sandbox.py.
"""

import json

import pytest

from patchproof.agent.prompts import build_diagnosis_messages
from patchproof.evaluation import (
    EvaluationError,
    LeakGuard,
    Task,
    leaked_from,
    load_tasks,
    prepare_task,
    render_report,
    score_patch,
    summarize,
    wilson,
)
from patchproof.sandbox import LocalSubprocessRunner
from patchproof.schemas import TestOutcome
from patchproof.verification.engine import Engine

TASKS = {t.id: t for t in load_tasks()}
MEDIAN_WRONG = (
    "--- a/src/stats.py\n+++ b/src/stats.py\n@@ -3,4 +3,6 @@\n     if not values:\n"
    '         raise ValueError("median of an empty sequence")\n     ordered = sorted(values)\n'
    "-    return ordered[len(ordered) // 2]\n+    if len(ordered) % 2:\n+        return ordered[len(ordered) // 2]\n"
    "+    return sum(ordered[1:-1]) / (len(ordered) - 2)\n"
)


@pytest.fixture(scope="module")
def harness():
    import dataclasses

    from patchproof.config import load_settings

    settings = dataclasses.replace(load_settings(), sandbox_backend="local", verification_runs=2)
    engine, runner = Engine(settings, None), LocalSubprocessRunner()
    contexts = {tid: prepare_task(engine, task, runner) for tid, task in TASKS.items()}
    return engine, runner, contexts


def test_the_task_set_is_labelled_and_complete():
    assert len(TASKS) >= 4
    for task in TASKS.values():
        assert task.kind in ("synthetic", "real") and task.license and task.issue
        assert task.hidden and task.reference_diff.startswith("--- a/")
    assert {t.kind for t in TASKS.values()} == {"synthetic"}  # no real-world tasks are claimed


def test_every_task_has_a_failing_original_and_a_passing_reference(harness):
    _, _, contexts = harness  # prepare_task raised for any task that violates this
    for tid, ctx in contexts.items():
        assert ctx.reference_ids and any(n.startswith("tests/test_hidden") for n in ctx.reference_ids), tid
        assert len(ctx.baseline_passing) < len(ctx.reference_ids), tid


def test_prepare_task_rejects_a_task_whose_original_already_passes(harness):
    engine, runner, _ = harness
    base = TASKS["median-even"]
    fixed = Task(**{**base.__dict__, "visible": {**base.visible, "src/stats.py": base.visible["src/stats.py"].replace(
        "return ordered[len(ordered) // 2]", "return 2.5 if len(ordered) == 4 else ordered[len(ordered) // 2]")}})
    with pytest.raises(EvaluationError, match="no bug to fix"):
        prepare_task(engine, fixed, runner)


def test_prepare_task_rejects_a_reference_that_does_not_fix_it(harness):
    engine, runner, _ = harness
    base = TASKS["median-even"]
    with pytest.raises(EvaluationError, match="does not pass every visible and hidden test"):
        prepare_task(engine, Task(**{**base.__dict__, "reference_diff": MEDIAN_WRONG}), runner)


def test_the_reference_fix_scores_correct_for_every_task(harness):
    engine, runner, contexts = harness
    for tid, task in TASKS.items():
        score = score_patch(engine, task, contexts[tid], runner, task.reference_diff)
        assert score.outcome == "correct", (tid, score)


def test_a_patch_that_passes_the_visible_tests_but_not_the_hidden_ones_is_incorrect(harness):
    engine, runner, contexts = harness
    task = TASKS["median-even"]
    score = score_patch(engine, task, contexts["median-even"], runner, MEDIAN_WRONG)
    assert (score.outcome, score.visible_failed) == ("incorrect", 0) and score.hidden_failed >= 1


def test_breaking_previously_passing_behaviour_is_a_regression(harness):
    engine, runner, contexts = harness
    task = TASKS["median-even"]
    diff = ("--- a/src/stats.py\n+++ b/src/stats.py\n@@ -3,4 +3,4 @@\n     if not values:\n"
            '-        raise ValueError("median of an empty sequence")\n+        return 0\n     ordered = sorted(values)\n'
            "     return ordered[len(ordered) // 2]\n")
    score = score_patch(engine, task, contexts["median-even"], runner, diff)
    assert score.outcome == "regression"


def test_no_patch_and_unusable_patches_never_score_as_correct(harness):
    engine, runner, contexts = harness
    task, ctx = TASKS["chunk-remainder"], contexts["chunk-remainder"]
    assert score_patch(engine, task, ctx, runner, None).outcome == "no_patch"
    assert score_patch(engine, task, ctx, runner, "   \n").outcome == "no_patch"
    garbage = score_patch(engine, task, ctx, runner, "this is not a diff")
    assert garbage.outcome == "rejected"


def test_editing_the_tests_cannot_make_a_patch_pass(harness):
    engine, runner, contexts = harness
    task, ctx = TASKS["chunk-remainder"], contexts["chunk-remainder"]
    weaken = ("--- a/tests/test_chunks.py\n+++ b/tests/test_chunks.py\n@@ -8,4 +8,4 @@\n def test_partial_last_chunk():\n"
              "-    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]\n"
              "+    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4]]\n \n \n")
    assert score_patch(engine, task, ctx, runner, weaken).outcome == "rejected"
    # Adding or editing a hidden test path is refused too, and a conftest cannot bend the suite.
    add_hidden = ("--- /dev/null\n+++ b/tests/test_hidden_chunks.py\n@@ -0,0 +1 @@\n+def test_x(): pass\n")
    assert score_patch(engine, task, ctx, runner, add_hidden).outcome == "rejected"
    conftest = "--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+import pytest\n"
    assert score_patch(engine, task, ctx, runner, conftest).outcome == "rejected"


def test_scoring_does_not_trust_the_arms_own_workspace(harness):
    """The scorer starts from the pristine task, so a patch cannot rely on files an arm may have changed."""
    engine, runner, contexts = harness
    task = TASKS["chunk-remainder"]
    # A diff written against a modified source (extra line) does not apply to the pristine project.
    stale = task.reference_diff.replace('raise ValueError("size must be positive")', 'raise ValueError("bad size")')
    assert score_patch(engine, task, contexts["chunk-remainder"], runner, stale).outcome == "rejected"


# -- hidden tests stay hidden ------------------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_the_real_prompt_contains_no_hidden_test_content(harness, task_id):
    engine, runner, _ = harness
    task = TASKS[task_id]
    runs = [engine.execute_tests(task.visible, runner, "b1"), engine.execute_tests(task.visible, runner, "b2")]
    from patchproof.verification.verdict import analyze_baseline

    analysis = analyze_baseline(runs)
    messages, chosen = build_diagnosis_messages(task.visible, runs, analysis.targets, task.issue)
    assert leaked_from(messages, task) == []
    assert not set(chosen) & set(task.hidden)


def test_the_leak_detector_catches_hidden_paths_and_lines():
    task = TASKS["median-even"]
    path = next(iter(task.hidden))
    assert leaked_from([{"role": "user", "content": f"see {path} for details"}], task) == [path]
    line = "assert median([9, 1, 1, 9, 5, 5]) == 5"
    assert len(line) >= 30
    assert leaked_from([{"role": "user", "content": f"hint:\n    {line}\n"}], task)
    assert leaked_from([{"role": "user", "content": "median of an even list is wrong"}], task) == []


def test_the_leak_guard_stops_the_request_before_it_reaches_the_model():
    task = TASKS["median-even"]

    class Client:
        configured = True
        sent = 0

        def chat(self, messages, **kwargs):
            Client.sent += 1

    guarded = LeakGuard(Client(), task)
    with pytest.raises(EvaluationError, match="hidden tests would be sent"):
        guarded.chat([{"role": "user", "content": next(iter(task.hidden))}])
    assert Client.sent == 0


def test_a_task_whose_hidden_tests_overlap_visible_files_is_refused(tmp_path):
    folder = tmp_path / "t"
    (folder / "project/tests").mkdir(parents=True)
    (folder / "hidden/tests").mkdir(parents=True)
    (folder / "project/tests/test_a.py").write_text("def test_a(): pass\n")
    (folder / "hidden/tests/test_a.py").write_text("def test_a(): pass\n")
    (folder / "reference.diff").write_text("")
    (folder / "task.json").write_text(json.dumps({"id": "t", "title": "t", "kind": "synthetic", "issue": "x"}))
    with pytest.raises(EvaluationError, match="share paths"):
        load_tasks(tmp_path)


# -- statistics and report ---------------------------------------------------------------------------------------


def test_wilson_interval_matches_known_values():
    low, high = wilson(5, 10)
    assert (round(low, 3), round(high, 3)) == (0.237, 0.763)
    assert wilson(0, 0) is None
    assert wilson(0, 5)[0] == 0.0 and wilson(5, 5)[1] == 1.0


def result(arm, outcome, verdict=None, false_verification=False, duration=10.0, tokens=100, task="t"):
    return {"arm": arm, "task_id": task, "outcome": outcome, "verdict": verdict, "false_verification": false_verification,
            "duration_s": duration, "prompt_tokens": tokens, "completion_tokens": 0}


def test_summary_separates_correctness_from_the_arms_own_claims():
    rows = [
        result("baseline", "correct"), result("baseline", "incorrect"), result("baseline", "unscored"),
        result("patchproof", "correct", "VERIFIED_FOR_TESTED_CASES"),
        result("patchproof", "incorrect", "VERIFIED_FOR_TESTED_CASES", false_verification=True),
        result("patchproof", "incorrect", "FAILED_VERIFICATION"),
        result("patchproof", "regression", "INCONCLUSIVE"),
    ]
    s = summarize(rows)
    assert (s["baseline"]["scored"], s["baseline"]["unscored"], s["baseline"]["correct"]) == (2, 1, 1)
    p = s["patchproof"]
    assert (p["scored"], p["correct"], p["verified"], p["false_verifications"]) == (4, 1, 2, 1)
    assert p["false_verification_rate"] == 0.5 and p["inconclusive"] == 1
    assert s["baseline"]["verified"] == 0 and s["baseline"]["false_verification_rate"] is None


def test_the_report_states_its_limits_and_makes_no_superiority_claim():
    rows = [result("baseline", "correct"), result("patchproof", "correct", "VERIFIED_FOR_TESTED_CASES")]
    doc = {"meta": {"model": "m", "version": "0", "tasks": 1, "synthetic": 1, "real": 0, "repeats": 1,
                    "platform": "x", "when": "now"}, "summary": summarize(rows), "results": rows,
           "exclusions": ["t / baseline: model unavailable (rate_limit)"]}
    text = render_report(doc)
    assert "1 tasks (1 synthetic, 0 real)" in text and "model unavailable" in text
    assert "do not show how PatchProof performs on real-world projects" in text
    assert "outperform" not in text.lower() and "better than" not in text.lower()


def test_dataset_tasks_never_leak_into_the_benchmark_or_examples():
    # The deterministic benchmark and bundled examples are separate artefacts with their own expectations.
    from patchproof.benchmark import CASES_DIR

    assert not any(t in {p.name for p in CASES_DIR.iterdir()} for t in TASKS)
    assert TestOutcome.passed.value == "passed"
