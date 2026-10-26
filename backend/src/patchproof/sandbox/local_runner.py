"""Run pytest as a host subprocess. NOT a sandbox.

Used only for code that ships with PatchProof (bundled examples with bundled patches,
benchmark fixtures, and the backend's own tests), or when the operator explicitly sets
PATCHPROOF_ALLOW_UNSANDBOXED=1 for local development. The engine enforces this.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .base import REPORT_FILENAME, RawExecution, SandboxRunner, pytest_args, truncate

PLUGIN_DIR = Path(__file__).resolve().parent / "image"


class LocalSubprocessRunner(SandboxRunner):
    name = "local-subprocess"
    isolated = False

    def run_pytest(self, workspace: Path, output_dir: Path, timeout_s: float,
                   per_test_timeout_s: int) -> RawExecution:
        cmd = [sys.executable, *pytest_args(per_test_timeout_s, str(workspace))]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.pathsep.join([str(PLUGIN_DIR), str(workspace / "src"), str(workspace)]),
            "PATCHPROOF_REPORT": str(output_dir / REPORT_FILENAME),
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        for key in ("SYSTEMROOT", "TEMP", "TMP", "WINDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        started = time.monotonic()
        proc = subprocess.Popen(cmd, cwd=workspace, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", **kwargs)
        try:
            out, err = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            out, err = proc.communicate()
            return RawExecution(command=["python", *cmd[1:]], exit_code=None,
                                stdout=truncate(out or ""), stderr=truncate(err or ""),
                                duration_s=round(time.monotonic() - started, 3), timed_out=True)
        return RawExecution(command=["python", *cmd[1:]], exit_code=proc.returncode,
                            stdout=truncate(out), stderr=truncate(err),
                            duration_s=round(time.monotonic() - started, 3))


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
