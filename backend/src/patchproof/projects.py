"""Project intake: strict validation of uploaded ZIPs and pasted files, plus structure summaries."""

from __future__ import annotations

import ast
import io
import re
import stat
import sys
import uuid
import zipfile
from collections.abc import Iterable
from pathlib import PurePosixPath

from .schemas import Project, ProjectFile, ProjectSummary

MAX_FILES = 200
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_PATH_LENGTH = 200
MAX_DEPTH = 8

ALLOWED_SUFFIXES = {".py", ".txt", ".md", ".rst", ".toml", ".cfg", ".ini", ".json", ".yaml", ".yml"}
IGNORED_PARTS = {"__pycache__", ".git", ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".idea", ".vscode"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.\- ]+$")


class ProjectRejected(ValueError):
    """Raised when an input cannot be accepted. The message is safe to show to users."""


def normalize_path(raw: str) -> str:
    """Validate a relative project path and return it in POSIX form, or raise ProjectRejected."""
    if not raw or len(raw) > MAX_PATH_LENGTH:
        raise ProjectRejected(f"Invalid path length: {raw[:60]!r}")
    if "\x00" in raw:
        raise ProjectRejected("Paths may not contain NUL bytes.")
    if "\\" in raw:
        raise ProjectRejected(f"Backslashes are not allowed in paths: {raw!r}")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ProjectRejected(f"Absolute paths are not allowed: {raw!r}")
    parts = raw.split("/")
    if len(parts) > MAX_DEPTH:
        raise ProjectRejected(f"Path is nested too deeply: {raw!r}")
    for part in parts:
        if part in {"..", ".", ""}:
            raise ProjectRejected(f"Path traversal is not allowed: {raw!r}")
        if not _SEGMENT_RE.match(part):
            raise ProjectRejected(f"Unsupported characters in path: {raw!r}")
    return "/".join(parts)


def _is_ignored(path: str) -> bool:
    parts = path.split("/")
    return any(p in IGNORED_PARTS for p in parts) or PurePosixPath(path).suffix in IGNORED_SUFFIXES


def _decode(path: str, data: bytes) -> str:
    if b"\x00" in data:
        raise ProjectRejected(f"Binary content is not supported: {path}")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProjectRejected(f"File is not valid UTF-8 text: {path}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_files(entries: Iterable[tuple[str, bytes]]) -> list[ProjectFile]:
    files: dict[str, ProjectFile] = {}
    total = 0
    for raw_path, data in entries:
        path = normalize_path(raw_path)
        if _is_ignored(path):
            continue
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise ProjectRejected(
                f"Unsupported file type: {path}. Allowed: {', '.join(sorted(ALLOWED_SUFFIXES))}"
            )
        if len(data) > MAX_FILE_BYTES:
            raise ProjectRejected(f"File exceeds {MAX_FILE_BYTES // 1024} KB: {path}")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ProjectRejected(f"Project exceeds {MAX_TOTAL_BYTES // (1024 * 1024)} MB in total.")
        if path.lower() in {p.lower() for p in files}:
            raise ProjectRejected(f"Duplicate path (case-insensitive): {path}")
        files[path] = ProjectFile(path=path, content=_decode(path, data))
        if len(files) > MAX_FILES:
            raise ProjectRejected(f"Project has more than {MAX_FILES} files.")
    if not files:
        raise ProjectRejected("No supported files were found.")
    if not any(p.endswith(".py") for p in files):
        raise ProjectRejected("PatchProof currently supports Python projects only (no .py files found).")
    return sorted(files.values(), key=lambda f: f.path)


def read_zip(data: bytes) -> list[ProjectFile]:
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ProjectRejected(f"Archive exceeds {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ProjectRejected("The upload is not a valid ZIP archive.") from exc
    infos = [i for i in archive.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES * 2:
        raise ProjectRejected("Archive contains too many entries.")
    declared = 0
    for info in infos:
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ProjectRejected(f"Symbolic links are not allowed: {info.filename}")
        if info.flag_bits & 0x1:
            raise ProjectRejected("Encrypted archives are not supported.")
        declared += info.file_size
        if declared > MAX_TOTAL_BYTES * 2:
            raise ProjectRejected("Archive expands beyond the allowed size.")
    names = [normalize_path(i.filename) for i in infos]
    # Strip one shared top-level directory, as produced by "Download ZIP" on GitHub.
    prefix = ""
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        prefix = next(iter(tops)) + "/"

    def entries():
        for info, name in zip(infos, names, strict=True):
            rel = name[len(prefix):]
            if _is_ignored(rel):
                continue
            with archive.open(info) as fh:
                chunk = fh.read(MAX_FILE_BYTES + 1)  # bounded read: never trust declared sizes
            yield rel, chunk

    return validate_files(entries())


# ---------------------------------------------------------------------------
# Classification and summaries
# ---------------------------------------------------------------------------

PROTECTED_NAMES = {
    "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "tox.ini",
    "noxfile.py", "manage.py", "requirements.txt", "requirements-dev.txt", "Pipfile",
}


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part in {"tests", "test", "testing"} for part in p.parts[:-1]):
        return True
    return p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"


def is_source_path(path: str) -> bool:
    return path.endswith(".py") and not is_test_path(path) and PurePosixPath(path).name not in PROTECTED_NAMES


def project_modules(paths: Iterable[str]) -> set[str]:
    """Top-level importable names provided by the project itself."""
    mods: set[str] = set()
    for path in paths:
        if not path.endswith(".py"):
            continue
        parts = PurePosixPath(path).parts
        for i in range(len(parts)):
            seg = parts[i]
            mods.add(seg[:-3] if seg.endswith(".py") else seg)
        if parts and parts[0] == "src" and len(parts) > 1:
            mods.add(parts[1][:-3] if parts[1].endswith(".py") else parts[1])
    return mods


def imported_top_levels(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def summarize(files: list[ProjectFile]) -> ProjectSummary:
    paths = [f.path for f in files]
    local = project_modules(paths)
    stdlib = set(sys.stdlib_module_names)
    third_party: set[str] = set()
    for f in files:
        if f.path.endswith(".py"):
            third_party |= imported_top_levels(f.content) - local - stdlib - {"pytest", "_pytest", "__future__"}
    source = [p for p in paths if is_source_path(p)]
    tests = [p for p in paths if p.endswith(".py") and is_test_path(p)]
    other = [p for p in paths if p not in source and p not in tests]
    notes = []
    if not tests:
        notes.append("No test files detected. PatchProof needs a failing test to reproduce the defect.")
    if third_party:
        notes.append(
            "Imports third-party packages that the sandbox does not provide: "
            + ", ".join(sorted(third_party))
            + ". Affected tests will be reported as environment failures."
        )
    return ProjectSummary(
        file_count=len(files),
        total_bytes=sum(len(f.content.encode()) for f in files),
        source_files=source,
        test_files=tests,
        other_files=other,
        third_party_imports=sorted(third_party),
        notes=notes,
    )


def build_project(name: str, origin: str, files: list[ProjectFile], trusted: bool = False,
                  project_id: str | None = None) -> Project:
    clean_name = re.sub(r"[^A-Za-z0-9_.\- ]", "", name)[:80] or "project"
    return Project(
        id=project_id or f"prj_{uuid.uuid4().hex[:12]}",
        name=clean_name,
        origin=origin,  # type: ignore[arg-type]
        trusted=trusted,
        files=files,
        summary=summarize(files),
    )
