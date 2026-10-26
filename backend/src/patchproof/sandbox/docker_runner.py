"""Run pytest inside a locked-down, disposable Docker container."""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from pathlib import Path

from .base import REPORT_FILENAME, RawExecution, SandboxRunner, pytest_args, truncate

INSTANCE_LABEL = "patchproof.instance"

_status_lock = threading.Lock()
_status_cache: dict[str, tuple[float, bool, str]] = {}


def docker_status(image: str, ttl_s: float = 30.0) -> tuple[bool, str]:
    """(available, detail). Checks the daemon and that the sandbox image exists. Cached briefly."""
    now = time.monotonic()
    with _status_lock:
        cached = _status_cache.get(image)
        if cached and now - cached[0] < ttl_s:
            return cached[1], cached[2]
    try:
        server = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                                capture_output=True, text=True, timeout=10)
        if server.returncode != 0:
            result = (False, "Docker daemon is not reachable.")
        else:
            img = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                 capture_output=True, text=True, timeout=10)
            if img.returncode != 0:
                result = (False, f"Sandbox image '{image}' is not built. Run scripts/build-sandbox.ps1 or scripts/build-sandbox.sh.")
            else:
                result = (True, f"Docker {server.stdout.strip()} with image {image}")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = (False, "Docker CLI is not installed or did not respond.")
    with _status_lock:
        _status_cache[image] = (now, *result)
    return result


class DockerSandbox(SandboxRunner):
    name = "docker"
    isolated = True

    def __init__(self, image: str, memory: str = "512m", cpus: str = "1", pids: int = 128,
                 instance: str | None = None):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids = pids
        # Labels this server's containers so a restarted server can remove the ones it left behind.
        self.instance = instance

    def build_command(self, container: str, workspace: Path, output_dir: Path,
                      per_test_timeout_s: int) -> list[str]:
        label = ["--label", f"{INSTANCE_LABEL}={self.instance}"] if self.instance else []
        return [
            "docker", "run", "--rm",
            "--name", container,
            *label,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",  # noqa: S108 - private tmpfs inside the container
            "--memory", self.memory, "--memory-swap", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "--ulimit", "nofile=256:256",
            "--ulimit", "fsize=16777216:16777216",
            "--mount", f"type=bind,source={workspace},target=/work,readonly",
            "--mount", f"type=bind,source={output_dir},target=/out",
            "--workdir", "/work",
            "--env", f"PATCHPROOF_REPORT=/out/{REPORT_FILENAME}",
            "--env", "PYTHONPATH=/opt/patchproof:/work/src:/work",
            "--env", "HOME=/tmp",
            self.image,
            "python", *pytest_args(per_test_timeout_s, "/work"),
        ]

    def run_pytest(self, workspace: Path, output_dir: Path, timeout_s: float,
                   per_test_timeout_s: int) -> RawExecution:
        container = f"patchproof-{uuid.uuid4().hex[:12]}"
        cmd = self.build_command(container, workspace, output_dir, per_test_timeout_s)
        # Host paths are replaced so reports never leak local directory names.
        display = [c.replace(str(workspace), "<workspace>").replace(str(output_dir), "<output>") for c in cmd]
        started = time.monotonic()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=30)
            return RawExecution(
                command=display, exit_code=None,
                stdout=truncate(_as_text(exc.stdout)), stderr=truncate(_as_text(exc.stderr)),
                duration_s=round(time.monotonic() - started, 3), timed_out=True,
            )
        except FileNotFoundError:
            return RawExecution(command=display, exit_code=None, stdout="", stderr="",
                                duration_s=0.0, sandbox_error="Docker CLI is not installed.")
        duration = round(time.monotonic() - started, 3)
        sandbox_error = None
        if proc.returncode in (125, 126, 127):
            sandbox_error = f"Container failed to start (exit {proc.returncode}): {proc.stderr.strip()[:500]}"
        elif proc.returncode == 137:
            sandbox_error = "Container was killed (exit 137), most likely by the memory limit."
        return RawExecution(
            command=display, exit_code=proc.returncode,
            stdout=truncate(proc.stdout), stderr=truncate(proc.stderr),
            duration_s=duration, sandbox_error=sandbox_error,
        )

    def remove_orphans(self) -> int:
        """Force-remove containers this instance started that are still present.

        `--rm` and the timeout handler only work while the server that started a container is alive;
        after a crash or restart its containers would otherwise keep running. Returns how many were removed.
        """
        if not self.instance:
            return 0
        try:
            found = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={INSTANCE_LABEL}={self.instance}"],
                                   capture_output=True, text=True, timeout=15)
            ids = found.stdout.split() if found.returncode == 0 else []
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=60)
            return len(ids)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return 0


def _as_text(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
