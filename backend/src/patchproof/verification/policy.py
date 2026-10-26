"""Patch policy: which files a candidate patch may touch and what it may introduce.

Two severities:
  * ``block``   - a safety-policy violation (path escape, protected file, new dangerous capability).
                  Produces BLOCKED_BY_SAFETY_POLICY.
  * ``invalid`` - the patch is malformed or cannot be applied. Produces FAILED_VERIFICATION
                  (or a bounded correction request when the patch came from the model).

The capability scan is a guardrail that keeps "fixes" from adding process, file, network, or
interpreter-control code. It is NOT the isolation boundary: the Docker sandbox is.
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from ..projects import PROTECTED_NAMES, is_test_path
from ..schemas import PolicyFinding

MAX_PATCH_BYTES = 64 * 1024
MAX_CHANGED_FILES = 8
MAX_CHANGED_LINES = 400

DANGEROUS_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil", "ctypes", "importlib", "builtins", "pytest",
    "_pytest", "atexit", "signal", "multiprocessing", "threading", "pathlib", "tempfile", "io",
    "pickle", "marshal", "inspect", "gc", "urllib", "http", "requests", "httpx", "asyncio",
    "unittest", "runpy", "code", "pty", "fcntl", "resource", "posix", "nt", "glob", "zipfile",
    "tarfile", "webbrowser", "ftplib", "smtplib", "telnetlib", "ssl", "select", "mmap",
}
DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "exit", "quit", "globals", "locals", "vars",
    "breakpoint", "input", "memoryview",
}
DANGEROUS_ATTRS = {
    "__builtins__", "__subclasses__", "__globals__", "__code__", "__closure__", "__dict__",
    "__class__", "__bases__", "__mro__", "__getattribute__", "f_globals", "f_locals", "f_back",
    "_exit", "system", "popen", "modules",
}

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def check_target_path(path: str, project_paths: set[str]) -> PolicyFinding | None:
    if not path or "\x00" in path:
        return PolicyFinding(rule="path.invalid", severity="block", message="Empty or NUL-containing path.")
    if path.startswith("/") or _DRIVE_RE.match(path) or "\\" in path:
        return PolicyFinding(rule="path.absolute", severity="block", file=path,
                             message=f"Absolute or non-POSIX path is not allowed: {path}")
    parts = PurePosixPath(path).parts
    if any(p in {"..", "."} for p in parts):
        return PolicyFinding(rule="path.traversal", severity="block", file=path,
                             message=f"Path traversal is not allowed: {path}")
    name = parts[-1] if parts else path
    if is_test_path(path):
        return PolicyFinding(rule="file.test_protected", severity="block", file=path,
                             message=f"Tests are the evidence and may not be modified by a candidate patch: {path}")
    if name in PROTECTED_NAMES or path.startswith(".github/") or name.startswith("."):
        return PolicyFinding(rule="file.config_protected", severity="block", file=path,
                             message=f"Build, test, and CI configuration may not be modified: {path}")
    if not path.endswith(".py"):
        return PolicyFinding(rule="file.non_python", severity="block", file=path,
                             message=f"Only Python source files may be patched: {path}")
    if path not in project_paths:
        return PolicyFinding(rule="file.unknown", severity="block", file=path,
                             message=f"Patch targets a file that is not part of the project: {path}")
    return None


def _capabilities(source: str) -> set[str]:
    tree = ast.parse(source)
    caps: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in DANGEROUS_MODULES:
                    caps.add(f"import {top}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level == 0 and top in DANGEROUS_MODULES:
                caps.add(f"import {top}")
        elif isinstance(node, ast.Name) and node.id in DANGEROUS_CALLS:
            caps.add(f"use {node.id}()")
        elif isinstance(node, ast.Attribute) and node.attr in DANGEROUS_ATTRS:
            caps.add(f"access .{node.attr}")
    return caps


def capability_findings(path: str, old_source: str, new_source: str) -> list[PolicyFinding]:
    """Flag dangerous capabilities that the new version adds relative to the old one."""
    try:
        old_caps = _capabilities(old_source)
    except SyntaxError:
        old_caps = set()
    try:
        new_caps = _capabilities(new_source)
    except SyntaxError:
        return []  # syntax errors are reported separately as an invalid patch
    added = sorted(new_caps - old_caps)
    return [
        PolicyFinding(
            rule="code.new_capability",
            severity="block",
            file=path,
            message=f"Patch introduces a capability that a minimal bug fix should not need: {cap}",
        )
        for cap in added
    ]
