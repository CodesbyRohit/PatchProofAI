"""Turn the in-sandbox reporter's JSON lines into structured test results."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..schemas import CollectionError, RunHealth, TestOutcome, TestResult

_MISSING_MODULE_RE = re.compile(r"ModuleNotFoundError: No module named '([\w.]+)'")


def environment_cause(message: str | None, local_modules: set[str]) -> str | None:
    """Return the missing third-party module if the failure is environmental, else None."""
    if not message:
        return None
    for match in _MISSING_MODULE_RE.finditer(message):
        top = match.group(1).split(".")[0]
        if top not in local_modules:
            return top
    return None


@dataclass
class ParsedReport:
    tests: list[TestResult]
    collection_errors: list[CollectionError]
    exitstatus: int | None
    finished: bool
    aborted_in: str | None = None  # test that was running when the process died


def parse_report(report_text: str, local_modules: set[str]) -> ParsedReport:
    phases: dict[str, dict[str, dict]] = {}
    order: list[str] = []
    collection_errors: list[CollectionError] = []
    exitstatus: int | None = None
    finished = False
    last_begun: str | None = None
    for line in report_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a partially written final line, e.g. after a kill
        kind = rec.get("type")
        if kind == "collect_error":
            msg = rec.get("message") or ""
            collection_errors.append(
                CollectionError(
                    nodeid=str(rec.get("nodeid") or "<session>"),
                    message=msg,
                    kind="environment" if environment_cause(msg, local_modules) else "code",
                )
            )
        elif kind == "begin":
            last_begun = str(rec.get("nodeid"))
        elif kind == "test":
            nodeid = str(rec.get("nodeid"))
            if nodeid not in phases:
                phases[nodeid] = {}
                order.append(nodeid)
            phases[nodeid][str(rec.get("when"))] = rec
        elif kind == "finish":
            finished = True
            exitstatus = rec.get("exitstatus")

    tests: list[TestResult] = []
    for nodeid in order:
        p = phases[nodeid]
        setup, call, teardown = p.get("setup"), p.get("call"), p.get("teardown")
        outcome = TestOutcome.passed
        message = None
        if setup and setup["outcome"] == "failed":
            outcome, message = TestOutcome.error, setup.get("message")
        elif setup and setup["outcome"] == "skipped":
            outcome, message = TestOutcome.skipped, setup.get("message")
        elif call:
            if call["outcome"] == "failed":
                outcome, message = TestOutcome.failed, call.get("message")
            elif call["outcome"] == "skipped":
                outcome, message = TestOutcome.skipped, call.get("message")
        elif not call:
            outcome, message = TestOutcome.error, "Test did not run its call phase."
        if teardown and teardown["outcome"] == "failed" and outcome == TestOutcome.passed:
            outcome, message = TestOutcome.error, teardown.get("message")
        duration = sum(float(r.get("duration") or 0) for r in p.values())
        tests.append(
            TestResult(
                nodeid=nodeid,
                outcome=outcome,
                duration_s=round(duration, 4),
                message=message,
                environment_issue=outcome in (TestOutcome.failed, TestOutcome.error)
                and environment_cause(message, local_modules) is not None,
            )
        )
    aborted = None
    if not finished and last_begun and "teardown" not in phases.get(last_begun, {}):
        aborted = last_begun
    return ParsedReport(tests, collection_errors, exitstatus, finished, aborted)


def classify_health(timed_out: bool, sandbox_error: bool, finished: bool, exitstatus: int | None,
                    tests: list[TestResult], collection_errors: list[CollectionError]) -> RunHealth:
    if sandbox_error:
        return RunHealth.sandbox_error
    if timed_out:
        return RunHealth.timeout
    if not finished:
        return RunHealth.report_missing
    if exitstatus == 5 and not tests and not collection_errors:
        return RunHealth.no_tests
    if exitstatus in (0, 1):
        return RunHealth.ok
    if exitstatus == 2 and collection_errors:
        return RunHealth.ok  # collection errors interrupt pytest; they are analysed explicitly
    return RunHealth.internal_error
