"""The verification loop: baseline -> diagnosis -> validated patch -> candidate runs -> verdict."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..agent.client import BharatCodeClient, BharatCodeError
from ..agent.diagnose import DiagnosisParseError, parse_diagnosis
from ..agent.prompts import build_diagnosis_messages, build_repair_message
from ..config import Settings
from ..examples import ScriptedPatch
from ..projects import project_modules
from ..redact import redact
from ..sandbox import DockerSandbox, LocalSubprocessRunner, SandboxRunner, Workspace, docker_status
from ..schemas import (
    Attempt,
    CandidatePatch,
    ComparisonCategory,
    Diagnosis,
    FileEdit,
    ModelCallInfo,
    PatchProvenance,
    PatchSource,
    Project,
    Run,
    RunEvent,
    RunHealth,
    TestRun,
    Verdict,
    VerdictStatus,
)
from .patching import prepare_candidate
from .results import classify_health, parse_report
from .verdict import (
    analyze_baseline,
    decide,
    verdict_for_rejected_patch,
    verdict_for_unusable_baseline,
)

log = logging.getLogger("patchproof.engine")


class Engine:
    def __init__(
        self,
        settings: Settings,
        client: BharatCodeClient | None,
        *,
        docker_runner: SandboxRunner | None = None,
        local_runner: SandboxRunner | None = None,
        docker_check: Callable[[], tuple[bool, str]] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.docker_runner = docker_runner or DockerSandbox(settings.sandbox_image, instance=settings.instance_id)
        self.local_runner = local_runner or LocalSubprocessRunner()
        self.docker_check = docker_check or (lambda: docker_status(settings.sandbox_image))

    # -- sandbox selection -------------------------------------------------------

    def runner_for(self, trusted_code: bool) -> tuple[SandboxRunner | None, str]:
        backend = self.settings.sandbox_backend
        if backend in ("auto", "docker"):
            ok, detail = self.docker_check()
            if ok:
                return self.docker_runner, detail
            if backend == "docker":
                return None, f"Docker sandbox required but unavailable: {detail}"
        if trusted_code:
            return self.local_runner, "Bundled PatchProof code only, run as a local subprocess (not isolated)."
        if self.settings.allow_unsandboxed:
            return self.local_runner, "PATCHPROOF_ALLOW_UNSANDBOXED=1: untrusted code runs WITHOUT isolation."
        _, detail = self.docker_check()
        return None, (
            "No isolation boundary is available for untrusted code (uploaded projects or AI-generated "
            f"patches). {detail} PatchProof refuses to execute it on the host."
        )

    # -- helpers -----------------------------------------------------------------

    def _event(self, run: Run, step: str, status: str, message: str, notify: Callable[[Run], None]) -> None:
        run.events.append(RunEvent(at=time.time(), step=step, status=status, message=message))  # type: ignore[arg-type]
        notify(run)

    def execute_tests(self, files: dict[str, str], runner: SandboxRunner, label: str) -> TestRun:
        local = project_modules(files)
        with Workspace(files) as ws:
            raw = runner.run_pytest(ws.project, ws.output, self.settings.run_timeout_s,
                                    self.settings.per_test_timeout_s)
            report = ws.read_report()
        parsed = parse_report(report, local)
        tests, cerrs, exitstatus = parsed.tests, parsed.collection_errors, parsed.exitstatus
        health = classify_health(raw.timed_out, raw.sandbox_error is not None, parsed.finished, exitstatus, tests, cerrs)
        detail = raw.sandbox_error
        if health == RunHealth.ok and raw.exit_code is not None and exitstatus is not None and raw.exit_code != exitstatus:
            health = RunHealth.report_missing
            detail = f"process exit code {raw.exit_code} does not match the reported pytest status {exitstatus}"
        if parsed.aborted_in and not detail:
            detail = f"the test process ended abruptly while running {parsed.aborted_in}"
        return TestRun(
            label=label, sandbox=runner.name, isolated=runner.isolated, command=raw.command,
            health=health, exit_code=raw.exit_code, duration_s=raw.duration_s, tests=tests,
            collection_errors=cerrs, stdout=redact(raw.stdout), stderr=redact(raw.stderr), detail=detail,
        )

    def _run_candidate(self, run: Run, attempt: Attempt, new_files: dict[str, str], runner: SandboxRunner,
                       notify: Callable[[Run], None]) -> None:
        n = self.settings.verification_runs
        for i in range(n):
            label = f"Candidate {attempt.number} run {i + 1}/{n}"
            self._event(run, "candidate", "started", f"{label}: running the full test suite on the patched copy", notify)
            result = self.execute_tests(new_files, runner, label)
            attempt.candidate_runs.append(result)
            c = result.counts()
            self._event(run, "candidate", "completed",
                        f"{label}: {c['passed']} passed, {c['failed']} failed, {c['error']} errors, "
                        f"{c['skipped']} skipped ({result.health.value}, {result.duration_s:.1f}s)", notify)
        attempt.verdict = decide(run.baseline, run.baseline_runs, attempt.candidate_runs)  # type: ignore[arg-type]

    def _finish(self, run: Run, verdict: Verdict, notify: Callable[[Run], None]) -> None:
        run.verdict = verdict
        run.state = "finished"
        run.finished_at = time.time()
        self._event(run, "verdict", "completed", f"{verdict.status.value}: {verdict.headline}", notify)

    # -- main entry --------------------------------------------------------------

    def execute(self, run: Run, project: Project, *, scripted: ScriptedPatch | None = None,
                user_patch: str | None = None, notify: Callable[[Run], None] = lambda r: None,
                scripted_provenance: PatchProvenance = "scripted_example") -> Run:
        """``scripted_provenance`` labels where a scripted patch came from (bundled example or benchmark fixture)."""
        try:
            self._execute(run, project, scripted, user_patch, notify, scripted_provenance)
        except Exception as exc:  # never leave a run hanging
            log.exception("run %s failed", run.id)
            run.state = "error"
            run.error = redact(f"Internal error: {type(exc).__name__}: {exc}")
            run.finished_at = time.time()
            notify(run)
        return run

    def _execute(self, run: Run, project: Project, scripted: ScriptedPatch | None, user_patch: str | None,
                 notify: Callable[[Run], None], scripted_provenance: PatchProvenance = "scripted_example") -> None:
        files = {f.path: f.content for f in project.files}
        run.state = "running"
        self._event(run, "project", "completed",
                    f"Loaded {project.summary.file_count} files ({len(project.summary.source_files)} source, "
                    f"{len(project.summary.test_files)} test)", notify)

        base_runner, detail = self.runner_for(project.trusted)
        if base_runner is None:
            self._event(run, "sandbox", "failed", detail, notify)
            return self._finish(run, Verdict(
                status=VerdictStatus.blocked,
                headline="Blocked by safety policy: no sandbox is available to run this project's code.",
                reasons=[detail]), notify)
        run.sandbox, run.sandbox_isolated = base_runner.name, base_runner.isolated
        run.disclosures.append(_sandbox_disclosure(base_runner, detail))
        self._event(run, "sandbox", "info", detail, notify)

        # 1. Baseline ------------------------------------------------------------
        for i in range(self.settings.verification_runs):
            label = f"Baseline run {i + 1}/{self.settings.verification_runs}"
            self._event(run, "baseline", "started", f"{label}: running the original test suite", notify)
            result = self.execute_tests(files, base_runner, label)
            run.baseline_runs.append(result)
            c = result.counts()
            self._event(run, "baseline", "completed",
                        f"{label}: {c['passed']} passed, {c['failed']} failed, {c['error']} errors, "
                        f"{len(result.collection_errors)} collection errors ({result.health.value}, "
                        f"{result.duration_s:.1f}s)", notify)
        run.baseline = analyze_baseline(run.baseline_runs)
        if not run.baseline.usable:
            self._event(run, "baseline", "failed", "; ".join(run.baseline.reasons), notify)
            return self._finish(run, verdict_for_unusable_baseline(run.baseline), notify)
        self._event(run, "baseline", "info",
                    f"Reproduced {len(run.baseline.targets)} failing target(s): {', '.join(run.baseline.targets[:4])}",
                    notify)

        # 2. Candidate source ----------------------------------------------------
        if run.patch_source == PatchSource.ai:
            return self._ai_loop(run, project, files, notify)

        trusted_candidate = project.trusted and run.patch_source == PatchSource.scripted
        cand_runner, cand_detail = self.runner_for(trusted_candidate)
        if run.patch_source == PatchSource.scripted:
            assert scripted is not None
            attempt = Attempt(number=1, diagnosis_source=scripted_provenance)
            candidate, new_files = prepare_candidate(
                files, provenance=scripted_provenance, label=scripted.label,
                edits=scripted.edits or None, diff=scripted.diff)
        else:
            attempt = Attempt(number=1, diagnosis_source="user_supplied")
            candidate, new_files = prepare_candidate(files, provenance="user_supplied",
                                                     label="User-supplied patch", diff=user_patch or "")
        attempt.patch = candidate
        run.attempts.append(attempt)
        self._event(run, "patch", "completed" if new_files else "failed", _patch_message(candidate), notify)
        if new_files is None:
            attempt.verdict = verdict_for_rejected_patch(candidate)
            return self._finish(run, attempt.verdict, notify)
        if cand_runner is None:
            attempt.verdict = Verdict(status=VerdictStatus.blocked,
                                      headline="Blocked by safety policy: no sandbox for untrusted patch execution.",
                                      reasons=[cand_detail])
            return self._finish(run, attempt.verdict, notify)
        self._run_candidate(run, attempt, new_files, cand_runner, notify)
        self._finish(run, attempt.verdict, notify)  # type: ignore[arg-type]

    # -- AI loop -------------------------------------------------------------------

    def _ask_model(self, messages: list[dict], attempt: Attempt, purpose: str
                   ) -> tuple[Diagnosis, list[FileEdit], str]:
        assert self.client is not None
        model = self.settings.model
        last_text = ""
        for fmt_try in range(2):  # one bounded correction for unparseable output
            try:
                result = self.client.chat(messages, model=model, max_tokens=self.settings.max_output_tokens,
                                          reasoning_effort=self.settings.reasoning_effort)
                text = result.text
                attempt.model_calls.append(ModelCallInfo(
                    model=result.model, latency_ms=result.latency_ms, request_id=result.request_id,
                    prompt_tokens=result.usage.get("prompt_tokens"),
                    completion_tokens=result.usage.get("completion_tokens"),
                    total_tokens=result.usage.get("total_tokens"), finish_reason=result.finish_reason,
                    http_attempts=result.attempts, purpose=purpose if fmt_try == 0 else "format-correction"))
            except BharatCodeError as err:
                if err.kind != "truncated" or not err.partial_text:
                    raise
                text = err.partial_text  # keep partial output; it may still contain the JSON
                attempt.model_calls.append(ModelCallInfo(model=model, latency_ms=0, request_id=err.request_id,
                                                         finish_reason="length", purpose=purpose))
            last_text = text
            try:
                diagnosis, edits = parse_diagnosis(text)
                return diagnosis, edits, text
            except DiagnosisParseError as exc:
                if fmt_try == 1:
                    raise
                messages = messages + build_repair_message(text, f"Your response could not be parsed: {exc}")
        raise DiagnosisParseError(f"Unparseable response: {last_text[:200]}")

    def _ai_loop(self, run: Run, project: Project, files: dict[str, str], notify: Callable[[Run], None]) -> None:
        if self.client is None or not self.client.configured:
            run.model_error_kind = "not_configured"
            self._event(run, "diagnosis", "failed", "BharatCode API key is not configured on the server.", notify)
            return self._finish(run, Verdict(
                status=VerdictStatus.inconclusive,
                headline="Inconclusive: no candidate patch was produced because the model is not configured.",
                reasons=["Set BHARATCODE_API_KEY on the server, or run a bundled scripted patch instead.",
                         "The baseline evidence above is real; only the AI step was skipped."],
                targets=run.baseline.targets if run.baseline else []), notify)
        cand_runner, cand_detail = self.runner_for(trusted_code=False)
        assert run.baseline is not None
        messages, chosen = build_diagnosis_messages(files, run.baseline_runs, run.baseline.targets, run.issue)
        run.disclosures.append(
            f"Sent {len(chosen)} file(s) to the BharatCode API ({self.settings.model}) for diagnosis: "
            + ", ".join(chosen) + ". No other project files were transmitted."
        )
        try:
            source = self.client.ensure_model(self.settings.model)
            self._event(run, "diagnosis", "info", f"Model {self.settings.model} validated against the {source} list", notify)
        except BharatCodeError as err:
            run.model_error_kind = err.kind
            self._event(run, "diagnosis", "failed", err.user_message(), notify)
            return self._finish(run, Verdict(status=VerdictStatus.inconclusive,
                                             headline="Inconclusive: the model could not be used.",
                                             reasons=[err.user_message()]), notify)

        verdict: Verdict | None = None
        for number in range(1, self.settings.max_ai_attempts + 1):
            attempt = Attempt(number=number, diagnosis_source="live_model")
            run.attempts.append(attempt)
            self._event(run, "diagnosis", "started",
                        f"Attempt {number}: asking {self.settings.model} for a root cause and minimal patch", notify)
            try:
                diagnosis, edits, raw = self._ask_model(messages, attempt, "diagnose" if number == 1 else "repair")
            except (BharatCodeError, DiagnosisParseError) as err:
                msg = err.user_message() if isinstance(err, BharatCodeError) else f"Model output was unusable: {err}"
                attempt.error = msg
                run.model_error_kind = err.kind if isinstance(err, BharatCodeError) else "malformed"
                self._event(run, "diagnosis", "failed", msg, notify)
                if verdict is None:  # otherwise the previous attempt's observed verdict stands
                    verdict = Verdict(status=VerdictStatus.inconclusive,
                                      headline="Inconclusive: the model did not produce a usable patch.",
                                      reasons=[msg])
                break
            attempt.diagnosis = diagnosis
            calls = attempt.model_calls
            self._event(run, "diagnosis", "completed",
                        f"Attempt {number}: {diagnosis.summary} (model confidence: {diagnosis.confidence}; "
                        f"{sum(c.latency_ms for c in calls) / 1000:.1f}s)", notify)

            candidate, new_files = prepare_candidate(files, provenance="live_model",
                                                     label=f"AI attempt {number}", edits=edits)
            attempt.patch = candidate
            self._event(run, "patch", "completed" if new_files else "failed", _patch_message(candidate), notify)
            if new_files is None:
                attempt.verdict = verdict = verdict_for_rejected_patch(candidate)
                feedback = "The patch was rejected before execution:\n" + "\n".join(
                    f"- {f.message}" for f in candidate.policy_findings)
            elif cand_runner is None:
                attempt.verdict = verdict = Verdict(
                    status=VerdictStatus.blocked,
                    headline="Blocked by safety policy: no sandbox is available to execute the AI-generated patch.",
                    reasons=[cand_detail, "The diagnosis and patch are shown for review but were NOT executed."])
                break
            else:
                self._run_candidate(run, attempt, new_files, cand_runner, notify)
                verdict = attempt.verdict
                assert verdict is not None
                if verdict.status != VerdictStatus.failed:
                    break
                feedback = _failure_feedback(verdict, attempt.candidate_runs)
            if number < self.settings.max_ai_attempts:
                self._event(run, "repair", "info",
                            f"Attempt {number} was not verified; sending the observed evidence back for one more attempt",
                            notify)
                messages = messages + build_repair_message(raw, feedback)
        assert verdict is not None
        self._finish(run, verdict, notify)


def _sandbox_disclosure(runner: SandboxRunner, detail: str) -> str:
    if runner.isolated:
        return ("Tests ran in a disposable Docker container: no network, read-only filesystem and "
                "workspace, all Linux capabilities dropped, unprivileged user, memory/CPU/PID limits.")
    return f"Tests ran as a local subprocess WITHOUT isolation. {detail}"


def _patch_message(candidate: CandidatePatch) -> str:
    if candidate.applied:
        return f"{candidate.label}: validated and applied to a disposable copy ({', '.join(candidate.changed_files)})"
    return f"{candidate.label}: rejected before execution: " + "; ".join(f.message for f in candidate.policy_findings[:3])


def _failure_feedback(verdict: Verdict, runs: list[TestRun]) -> str:
    lines = ["Your patch was applied and the test suite was run. It was NOT verified:", *(f"- {r}" for r in verdict.reasons[:10])]
    bad = {c.nodeid for c in verdict.comparison
           if c.category in (ComparisonCategory.regression, ComparisonCategory.still_failing)}
    for t in runs[0].tests if runs else []:
        if t.nodeid in bad and t.message:
            lines.append(f"\n### {t.nodeid}\n{t.message[-1500:]}")
    for e in runs[0].collection_errors if runs else []:
        lines.append(f"\n### Collection error {e.nodeid}\n{e.message[-1500:]}")
    return "\n".join(lines)
