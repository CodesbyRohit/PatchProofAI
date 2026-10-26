import json

from patchproof.schemas import RunHealth, TestOutcome
from patchproof.verification.results import classify_health, environment_cause, parse_report


def lines(*records):
    return "\n".join(json.dumps(r) for r in records)


def test_parse_report_aggregates_phases():
    report = lines(
        {"type": "start"},
        {"type": "test", "nodeid": "t::ok", "when": "setup", "outcome": "passed", "duration": 0.1},
        {"type": "test", "nodeid": "t::ok", "when": "call", "outcome": "passed", "duration": 0.2},
        {"type": "test", "nodeid": "t::ok", "when": "teardown", "outcome": "passed", "duration": 0.0},
        {"type": "test", "nodeid": "t::bad", "when": "setup", "outcome": "passed", "duration": 0},
        {"type": "test", "nodeid": "t::bad", "when": "call", "outcome": "failed", "message": "AssertionError"},
        {"type": "test", "nodeid": "t::fx", "when": "setup", "outcome": "failed", "message": "fixture boom"},
        {"type": "test", "nodeid": "t::skip", "when": "setup", "outcome": "skipped", "message": "skip"},
        {"type": "test", "nodeid": "t::td", "when": "setup", "outcome": "passed"},
        {"type": "test", "nodeid": "t::td", "when": "call", "outcome": "passed"},
        {"type": "test", "nodeid": "t::td", "when": "teardown", "outcome": "failed", "message": "teardown"},
        {"type": "collect_error", "nodeid": "tests/test_y.py", "message": "ModuleNotFoundError: No module named 'yaml'"},
        {"type": "collect_error", "nodeid": "tests/test_z.py", "message": "SyntaxError: bad"},
        {"type": "finish", "exitstatus": 1, "collected": 5},
    ) + "\n{\"type\": \"test\", \"nodeid\": \"partial"  # torn final line is ignored
    parsed = parse_report(report, {"app"})
    tests, cerrs, status, finished = parsed.tests, parsed.collection_errors, parsed.exitstatus, parsed.finished
    outcomes = {t.nodeid: t.outcome for t in tests}
    assert outcomes == {"t::ok": TestOutcome.passed, "t::bad": TestOutcome.failed, "t::fx": TestOutcome.error,
                        "t::skip": TestOutcome.skipped, "t::td": TestOutcome.error}
    assert next(t for t in tests if t.nodeid == "t::ok").duration_s == 0.3
    assert [(c.nodeid, c.kind) for c in cerrs] == [("tests/test_y.py", "environment"), ("tests/test_z.py", "code")]
    assert status == 1 and finished


def test_abrupt_exit_names_the_running_test():
    report = lines(
        {"type": "start"},
        {"type": "begin", "nodeid": "t::ok"},
        {"type": "test", "nodeid": "t::ok", "when": "call", "outcome": "passed"},
        {"type": "test", "nodeid": "t::ok", "when": "teardown", "outcome": "passed"},
        {"type": "begin", "nodeid": "t::hang"},
        {"type": "test", "nodeid": "t::hang", "when": "setup", "outcome": "passed"},
    )
    parsed = parse_report(report, set())
    assert not parsed.finished and parsed.aborted_in == "t::hang"


def test_missing_local_module_is_code_not_environment():
    assert environment_cause("ModuleNotFoundError: No module named 'app.utils'", {"app"}) is None
    assert environment_cause("ModuleNotFoundError: No module named 'numpy'", {"app"}) == "numpy"


def test_classify_health():
    assert classify_health(False, False, True, 0, [], []) == RunHealth.ok
    assert classify_health(False, False, True, 5, [], []) == RunHealth.no_tests
    assert classify_health(True, False, False, None, [], []) == RunHealth.timeout
    assert classify_health(False, True, False, None, [], []) == RunHealth.sandbox_error
    assert classify_health(False, False, False, None, [], []) == RunHealth.report_missing
    assert classify_health(False, False, True, 3, [], []) == RunHealth.internal_error
