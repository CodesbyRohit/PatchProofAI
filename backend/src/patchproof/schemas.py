"""Pydantic schemas shared by the API, the verification engine, and the reports."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectFile(BaseModel):
    path: str
    content: str


class ProjectSummary(BaseModel):
    file_count: int
    total_bytes: int
    source_files: list[str]
    test_files: list[str]
    other_files: list[str]
    third_party_imports: list[str]
    notes: list[str] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    origin: Literal["example", "upload", "inline"]
    trusted: bool = False  # True only for code bundled with PatchProof
    files: list[ProjectFile]
    summary: ProjectSummary


class InlineProjectRequest(BaseModel):
    name: str = Field(default="pasted-project", max_length=80)
    files: list[ProjectFile] = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


class TestOutcome(StrEnum):
    passed = "passed"
    failed = "failed"
    error = "error"
    skipped = "skipped"


class TestResult(BaseModel):
    nodeid: str
    outcome: TestOutcome
    duration_s: float = 0.0
    message: str | None = None
    environment_issue: bool = False  # failure caused by the environment, not the code


class CollectionError(BaseModel):
    nodeid: str
    message: str
    kind: Literal["code", "environment"]


class RunHealth(StrEnum):
    ok = "ok"  # pytest completed and produced a structured report
    no_tests = "no_tests"
    timeout = "timeout"
    sandbox_error = "sandbox_error"
    report_missing = "report_missing"
    internal_error = "internal_error"


class TestRun(BaseModel):
    label: str
    sandbox: str
    isolated: bool
    command: list[str]
    health: RunHealth
    exit_code: int | None
    duration_s: float
    tests: list[TestResult] = Field(default_factory=list)
    collection_errors: list[CollectionError] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    detail: str | None = None

    def counts(self) -> dict[str, int]:
        out = {o.value: 0 for o in TestOutcome}
        for t in self.tests:
            out[t.outcome.value] += 1
        return out


# ---------------------------------------------------------------------------
# Diagnosis and patches
# ---------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    file: str
    lines: str | None = None
    observation: str


class FileEdit(BaseModel):
    file: str
    search: str
    replace: str


class Diagnosis(BaseModel):
    summary: str
    root_cause: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    rationale: str = ""
    potential_regressions: list[str] = Field(default_factory=list)
    additional_tests: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "low"
    confidence_explanation: str = ""


PatchProvenance = Literal["live_model", "user_supplied", "scripted_example", "benchmark_fixture"]


class ModelCallInfo(BaseModel):
    model: str
    latency_ms: int
    request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    http_attempts: int = 1
    purpose: str = "diagnose"


class PolicyFinding(BaseModel):
    rule: str
    severity: Literal["block", "invalid"]
    message: str
    file: str | None = None


class CandidatePatch(BaseModel):
    provenance: PatchProvenance
    label: str
    diff: str = ""
    changed_files: list[str] = Field(default_factory=list)
    edits: list[FileEdit] = Field(default_factory=list)
    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    applied: bool = False


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class VerdictStatus(StrEnum):
    verified = "VERIFIED_FOR_TESTED_CASES"
    failed = "FAILED_VERIFICATION"
    inconclusive = "INCONCLUSIVE"
    blocked = "BLOCKED_BY_SAFETY_POLICY"


class ComparisonCategory(StrEnum):
    fixed = "fixed"
    still_failing = "still_failing"
    regression = "regression"
    still_passing = "still_passing"
    newly_skipped = "newly_skipped"
    missing = "missing"
    new_test = "new_test"
    unchanged_skip = "unchanged_skip"
    flaky = "flaky"
    environment = "environment"


class TestComparison(BaseModel):
    nodeid: str
    baseline: str
    candidate: str
    category: ComparisonCategory
    is_target: bool = False


class Verdict(BaseModel):
    status: VerdictStatus
    headline: str
    reasons: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    comparison: list[TestComparison] = Field(default_factory=list)
    decided_by: str = "PatchProof verification engine (deterministic rules over observed test results)"


class BaselineAnalysis(BaseModel):
    usable: bool
    targets: list[str]
    passing: list[str]
    flaky: list[str] = Field(default_factory=list)
    environment_failures: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class PatchSource(StrEnum):
    ai = "ai"
    user = "user"
    scripted = "scripted"


class RunRequest(BaseModel):
    project_id: str | None = None
    example_id: str | None = None
    patch_source: PatchSource = PatchSource.ai
    scripted_patch_id: str | None = None
    user_patch: str | None = Field(default=None, max_length=64_000)
    issue: str | None = Field(default=None, max_length=4_000)


class RunEvent(BaseModel):
    at: float
    step: str
    status: Literal["started", "completed", "failed", "skipped", "info"]
    message: str


class Attempt(BaseModel):
    number: int
    diagnosis: Diagnosis | None = None
    diagnosis_source: PatchProvenance
    patch: CandidatePatch | None = None
    candidate_runs: list[TestRun] = Field(default_factory=list)
    verdict: Verdict | None = None
    model_calls: list[ModelCallInfo] = Field(default_factory=list)
    error: str | None = None


class Run(BaseModel):
    id: str
    created_at: float
    finished_at: float | None = None
    state: Literal["queued", "running", "finished", "error"]
    project_id: str
    project_name: str
    patch_source: PatchSource
    issue: str | None = None
    sandbox: str | None = None
    sandbox_isolated: bool = False
    events: list[RunEvent] = Field(default_factory=list)
    baseline_runs: list[TestRun] = Field(default_factory=list)
    baseline: BaselineAnalysis | None = None
    attempts: list[Attempt] = Field(default_factory=list)
    verdict: Verdict | None = None
    error: str | None = None
    disclosures: list[str] = Field(default_factory=list)


class RunSummary(BaseModel):
    id: str
    created_at: float
    state: str
    project_name: str
    patch_source: PatchSource
    verdict: VerdictStatus | None = None
    headline: str | None = None
