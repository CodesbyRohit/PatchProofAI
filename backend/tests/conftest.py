from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from patchproof.agent.client import ChatResult
from patchproof.config import REPO_ROOT, load_settings
from patchproof.schemas import RunHealth, TestOutcome, TestResult, TestRun


@pytest.fixture
def settings(tmp_path: Path):
    return dataclasses.replace(
        load_settings(),
        api_key=None,
        sandbox_backend="local",
        allow_unsandboxed=False,
        verification_runs=2,
        db_path=tmp_path / "test.sqlite3",
        examples_dir=REPO_ROOT / "examples",
        run_timeout_s=60,
    )


def make_run(label: str, outcomes: dict[str, str], health: RunHealth = RunHealth.ok, env: set[str] = frozenset(),
             collection_errors=None) -> TestRun:
    return TestRun(
        label=label, sandbox="fake", isolated=True, command=[], health=health, exit_code=0, duration_s=0.1,
        tests=[TestResult(nodeid=n, outcome=TestOutcome(o), environment_issue=n in env,
                          message=None if o == "passed" else f"{n} {o}")
               for n, o in outcomes.items()],
        collection_errors=collection_errors or [],
    )


class FakeClient:
    """Stands in for BharatCodeClient; returns queued responses and records requests."""

    configured = True

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    def ensure_model(self, model: str) -> str:
        return "documented"

    def list_models(self):
        return ["qwen-3.8-27b"], "documented"

    def chat(self, messages, *, model, **kwargs):
        self.requests.append(messages)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(text=item, model=model, latency_ms=5, finish_reason="stop", request_id="req-test",
                          usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
