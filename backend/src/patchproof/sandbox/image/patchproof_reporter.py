"""pytest plugin loaded inside the sandbox (``-p patchproof_reporter``).

It writes one JSON object per line to the file named by ``PATCHPROOF_REPORT`` so the
backend can read structured, per-test results instead of scraping console output.
It only uses the standard library and pytest hook APIs.
"""

import json
import os

_MAX_MESSAGE = 4000
_handle = None


def _write(record):
    global _handle
    path = os.environ.get("PATCHPROOF_REPORT")
    if not path:
        return
    if _handle is None:
        _handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
    _handle.write(json.dumps(record) + "\n")
    _handle.flush()


def _longrepr(report):
    if not report.longrepr:
        return None
    try:
        text = report.longreprtext
    except Exception:  # pragma: no cover - defensive
        text = str(report.longrepr)
    if len(text) > _MAX_MESSAGE:
        text = "...[truncated]...\n" + text[-_MAX_MESSAGE:]
    return text


def pytest_sessionstart(session):
    _write({"type": "start"})


def pytest_collectreport(report):
    if report.failed:
        _write({"type": "collect_error", "nodeid": report.nodeid, "message": _longrepr(report)})


def pytest_runtest_logstart(nodeid, location):
    _write({"type": "begin", "nodeid": nodeid})


def pytest_runtest_logreport(report):
    _write(
        {
            "type": "test",
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": round(float(report.duration), 4),
            "wasxfail": hasattr(report, "wasxfail"),
            "message": _longrepr(report),
        }
    )


def pytest_sessionfinish(session, exitstatus):
    _write({"type": "finish", "exitstatus": int(exitstatus), "collected": session.testscollected})
    if _handle is not None:
        _handle.close()
