"""Integration tests for the full verification loop.

These execute real pytest runs of trusted fixture code with the local subprocess runner, and use a
scripted fake model client. The Docker path is covered in test_docker_sandbox.py.
"""

import dataclasses
import json
import time

import pytest
from conftest import FakeClient

from patchproof.agent.client import BharatCodeError
from patchproof.examples import load_examples
from patchproof.projects import build_project, validate_files
from patchproof.schemas import PatchSource, Run, VerdictStatus
from patchproof.verification.engine import Engine

PAGINATION_SEARCH = "    return total_items // per_page\n"


def diagnosis(replace: str, search: str = PAGINATION_SEARCH, file: str = "pagination.py") -> str:
    return json.dumps({
        "summary": "page_count uses floor division",
        "root_cause": "Floor division drops the final partial page.",
        "evidence": [{"file": "pagination.py", "lines": "10", "observation": "//"}],
        "edits": [{"file": file, "search": search, "replace": replace}],
        "rationale": "Round up.",
        "confidence": "medium",
        "confidence_explanation": "Matches the failing assertions.",
    })


@pytest.fixture
def examples(settings):
    return load_examples(settings.examples_dir)


def new_run(project, source=PatchSource.ai) -> Run:
    return Run(id="run_test", created_at=time.time(), state="queued", project_id=project.id,
               project_name=project.name, patch_source=source, issue="last page missing")


def untrusted(project):
    """The same files, but as if a user uploaded them."""
    files = validate_files((f.path, f.content.encode()) for f in project.files)
    return build_project(project.name, "upload", files)


@pytest.mark.parametrize("example_id,patch_id", [
    ("pagination-off-by-one", "plausible-plus-one"),
    ("pagination-off-by-one", "minimal-fix"),
    ("pagination-off-by-one", "edit-the-test"),
    ("cart-shared-state", "ignore-initial-items"),
    ("cart-shared-state", "copy-or-new-list"),
    ("report-missing-dependency", "guard-empty"),
])
def test_bundled_scripted_patches_get_expected_verdicts(settings, examples, example_id, patch_id):
    example, project = examples[example_id]
    patch = next(p for p in example.patches if p.id == patch_id)
    engine = Engine(settings, None)
    run = engine.execute(new_run(project, PatchSource.scripted), project, scripted=patch)
    assert run.state == "finished", run.error
    assert run.verdict.status.value == patch.expected_verdict
    assert run.sandbox == "local-subprocess" and not run.sandbox_isolated
    assert run.baseline_runs and all(r.tests for r in run.baseline_runs)


def test_ai_loop_rejects_regression_then_verifies_repair(settings, examples):
    _, project = examples["pagination-off-by-one"]
    client = FakeClient([
        diagnosis("    return total_items // per_page + 1\n"),
        diagnosis("    return (total_items + per_page - 1) // per_page\n"),
    ])
    engine = Engine(dataclasses.replace(settings, allow_unsandboxed=True), client)
    run = engine.execute(new_run(untrusted(project)), untrusted(project))
    assert run.state == "finished", run.error
    assert [a.verdict.status for a in run.attempts] == [VerdictStatus.failed, VerdictStatus.verified]
    assert run.verdict.status == VerdictStatus.verified
    # The repair request carried the real observed failure evidence back to the model.
    feedback = client.requests[1][-1]["content"]
    assert "Regression: tests/test_pagination.py::test_exact_multiple_has_no_extra_page" in feedback
    assert run.attempts[0].diagnosis_source == "live_model"
    assert any("Sent 2 file(s) to the BharatCode API" in d for d in run.disclosures)


def test_ai_unparseable_output_gets_one_bounded_correction(settings, examples):
    _, project = examples["pagination-off-by-one"]
    client = FakeClient(["Sure! The bug is obvious.", diagnosis("    return -(-total_items // per_page)\n")])
    engine = Engine(dataclasses.replace(settings, allow_unsandboxed=True, max_ai_attempts=1), client)
    run = engine.execute(new_run(project), project)
    assert run.verdict.status == VerdictStatus.verified
    assert [c.purpose for c in run.attempts[0].model_calls] == ["diagnose", "format-correction"]


def test_ai_patch_that_edits_tests_is_blocked_and_never_executed(settings, examples):
    _, project = examples["pagination-off-by-one"]
    client = FakeClient([diagnosis("    assert page_count(11, 5) == 2\n", search="    assert page_count(11, 5) == 3\n",
                                   file="tests/test_pagination.py")])
    engine = Engine(dataclasses.replace(settings, allow_unsandboxed=True, max_ai_attempts=1), client)
    run = engine.execute(new_run(project), project)
    assert run.verdict.status == VerdictStatus.blocked
    assert run.attempts[0].candidate_runs == []


def test_ai_patch_not_executed_without_sandbox(settings, examples):
    _, project = examples["pagination-off-by-one"]
    client = FakeClient([diagnosis("    return -(-total_items // per_page)\n")])
    engine = Engine(settings, client)  # local backend, no unsandboxed override
    run = engine.execute(new_run(project), project)
    assert run.verdict.status == VerdictStatus.blocked
    assert run.attempts[0].patch.applied and run.attempts[0].candidate_runs == []


def test_untrusted_upload_is_blocked_without_sandbox(settings, examples):
    _, project = examples["pagination-off-by-one"]
    run = Engine(settings, FakeClient([])).execute(new_run(untrusted(project)), untrusted(project))
    assert run.verdict.status == VerdictStatus.blocked
    assert run.baseline_runs == []


def test_docker_required_but_unavailable(settings, examples):
    _, project = examples["pagination-off-by-one"]
    engine = Engine(dataclasses.replace(settings, sandbox_backend="docker"), None,
                    docker_check=lambda: (False, "Docker daemon is not reachable."))
    run = engine.execute(new_run(project, PatchSource.scripted), project,
                         scripted=examples["pagination-off-by-one"][0].patches[0])
    assert run.verdict.status == VerdictStatus.blocked
    assert "not reachable" in run.verdict.reasons[0]


def test_model_not_configured_is_inconclusive_with_real_baseline(settings, examples):
    _, project = examples["pagination-off-by-one"]
    run = Engine(settings, None).execute(new_run(project), project)
    assert run.verdict.status == VerdictStatus.inconclusive
    assert run.baseline and len(run.baseline.targets) == 2


def test_model_outage_is_inconclusive(settings, examples):
    _, project = examples["pagination-off-by-one"]
    client = FakeClient([BharatCodeError("unavailable", "HTTP 503", request_id="rid-x")])
    engine = Engine(dataclasses.replace(settings, allow_unsandboxed=True), client)
    run = engine.execute(new_run(project), project)
    assert run.verdict.status == VerdictStatus.inconclusive
    assert "rid-x" in run.attempts[0].error


def test_user_patch_unified_diff(settings, examples):
    _, project = examples["pagination-off-by-one"]
    diff = (
        "--- a/pagination.py\n+++ b/pagination.py\n@@ -9,2 +9,2 @@\n"
        "         raise ValueError(\"total_items cannot be negative\")\n"
        "-    return total_items // per_page\n"
        "+    return -(-total_items // per_page)\n"
    )
    engine = Engine(dataclasses.replace(settings, allow_unsandboxed=True), None)
    run = engine.execute(new_run(project, PatchSource.user), project, user_patch=diff)
    assert run.verdict.status == VerdictStatus.verified, run.verdict.reasons


def test_hanging_patch_is_never_verified(settings, examples):
    # On Windows pytest-timeout hard-exits the process (INCONCLUSIVE, culprit named); on POSIX it
    # fails the single test (FAILED). Either way the patch must not be verified.
    _, project = examples["pagination-off-by-one"]
    client = FakeClient([diagnosis("    while True:\n        pass\n")])
    engine = Engine(dataclasses.replace(settings, allow_unsandboxed=True, max_ai_attempts=1,
                                        per_test_timeout_s=2, verification_runs=1), client)
    run = engine.execute(new_run(project), project)
    assert run.verdict.status in (VerdictStatus.failed, VerdictStatus.inconclusive)
    if run.verdict.status == VerdictStatus.inconclusive:
        assert any("ended abruptly while running tests/test_pagination.py::" in r for r in run.verdict.reasons)
