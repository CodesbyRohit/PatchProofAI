"""Common sandbox interface and the disposable workspace helper."""

from __future__ import annotations

import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

MAX_CAPTURE_CHARS = 20_000

REPORT_FILENAME = "report.jsonl"


def pytest_args(per_test_timeout_s: int, workdir: str) -> list[str]:
    return [
        "-m", "pytest",
        "-q",
        "--color=no",
        f"--rootdir={workdir}",
        f"--confcutdir={workdir}",  # never load conftest.py files from above the workspace
        "-p", "no:cacheprovider",
        "-p", "patchproof_reporter",
        "--continue-on-collection-errors",
        f"--timeout={per_test_timeout_s}",
        "-o", "addopts=",
    ]


def truncate(text: str, limit: int = MAX_CAPTURE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n\n...[output truncated by PatchProof]...\n\n" + text[-limit // 2:]


@dataclass
class RawExecution:
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    sandbox_error: str | None = None
    report: str = ""
    extra: dict = field(default_factory=dict)


class SandboxRunner(ABC):
    name: str = "abstract"
    isolated: bool = False

    @abstractmethod
    def run_pytest(self, workspace: Path, output_dir: Path, timeout_s: float,
                   per_test_timeout_s: int) -> RawExecution: ...


class Workspace:
    """A disposable directory holding one copy of the project plus a separate output dir."""

    def __init__(self, files: dict[str, str]):
        # resolve() expands Windows 8.3 short names (e.g. SARIKA~1); a mismatch between the
        # short cwd and long file paths makes pytest pick a rootdir outside the workspace.
        self.root = Path(tempfile.mkdtemp(prefix="patchproof_")).resolve()
        self.project = self.root / "work"
        self.output = self.root / "out"
        self.project.mkdir()
        self.output.mkdir()
        self.output.chmod(0o777)  # the sandbox runs as an unprivileged UID
        for rel, content in files.items():
            target = (self.project / rel).resolve()
            if not target.is_relative_to(self.project.resolve()):
                raise ValueError(f"Refusing to write outside the workspace: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")

    def read_report(self) -> str:
        path = self.output / REPORT_FILENAME
        if not path.is_file():
            return ""
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(5 * 1024 * 1024)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
