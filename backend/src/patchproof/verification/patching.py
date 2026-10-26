"""Candidate patches: search/replace edits and strict unified diffs.

Nothing here touches the filesystem. Patches are applied to an in-memory copy of the
project; the engine writes the result into a disposable workspace afterwards.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from ..schemas import CandidatePatch, FileEdit, PatchProvenance, PolicyFinding
from .policy import (
    MAX_CHANGED_FILES,
    MAX_CHANGED_LINES,
    MAX_PATCH_BYTES,
    capability_findings,
    check_target_path,
)


class PatchError(Exception):
    def __init__(self, findings: list[PolicyFinding]):
        self.findings = findings
        super().__init__("; ".join(f.message for f in findings))

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings)


def _invalid(message: str, file: str | None = None, rule: str = "patch.invalid") -> PatchError:
    return PatchError([PolicyFinding(rule=rule, severity="invalid", message=message, file=file)])


# ---------------------------------------------------------------------------
# Search/replace edits (the format the model is asked to produce)
# ---------------------------------------------------------------------------


def _find_unique(haystack: str, needle: str) -> int | None:
    first = haystack.find(needle)
    if first == -1 or haystack.find(needle, first + 1) != -1:
        return None
    return first


def _apply_edit(content: str, edit: FileEdit) -> str:
    if not edit.search:
        raise _invalid(f"Edit for {edit.file} has an empty search block.", edit.file)
    count = content.count(edit.search)
    if count == 1:
        return content.replace(edit.search, edit.replace, 1)
    if count > 1:
        raise _invalid(
            f"Search block matches {count} locations in {edit.file}; it must match exactly one.",
            edit.file, "patch.ambiguous",
        )
    # Tolerate trailing-whitespace differences only; indentation must match exactly.
    lines = content.split("\n")
    search_lines = [s.rstrip() for s in edit.search.strip("\n").split("\n")]
    stripped = [s.rstrip() for s in lines]
    n = len(search_lines)
    hits = [i for i in range(len(lines) - n + 1) if stripped[i:i + n] == search_lines]
    if len(hits) == 1:
        i = hits[0]
        replacement = edit.replace.strip("\n").split("\n") if edit.replace.strip("\n") else []
        return "\n".join(lines[:i] + replacement + lines[i + n:])
    if len(hits) > 1:
        raise _invalid(f"Search block is ambiguous in {edit.file}.", edit.file, "patch.ambiguous")
    raise _invalid(f"Search block was not found in {edit.file}.", edit.file, "patch.not_found")


def apply_edits(files: dict[str, str], edits: list[FileEdit]) -> dict[str, str]:
    if not edits:
        raise _invalid("The candidate contains no edits.")
    project_paths = set(files)
    blocked = [f for e in edits if (f := check_target_path(e.file, project_paths))]
    if blocked:
        raise PatchError(blocked)
    new_files = dict(files)
    for edit in edits:
        new_files[edit.file] = _apply_edit(new_files[edit.file], edit)
    return new_files


# ---------------------------------------------------------------------------
# Unified diffs (user-supplied patches and recorded fixtures)
# ---------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # each prefixed by ' ', '-', or '+'

    def old_block(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln[0] in " -"]

    def new_block(self) -> list[str]:
        return [ln[1:] for ln in self.lines if ln[0] in " +"]


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)


def _strip_prefix(raw: str) -> str:
    path = raw.split("\t", 1)[0].strip()
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_unified_diff(text: str) -> list[FilePatch]:
    if len(text.encode()) > MAX_PATCH_BYTES:
        raise _invalid(f"Patch exceeds {MAX_PATCH_BYTES // 1024} KB.", rule="patch.too_large")
    if "\x00" in text or "GIT binary patch" in text or re.search(r"^Binary files ", text, re.M):
        raise PatchError([PolicyFinding(rule="patch.binary", severity="block",
                                        message="Binary patches are not allowed.")])
    lines = text.replace("\r\n", "\n").split("\n")
    patches: list[FilePatch] = []
    i = 0
    current: FilePatch | None = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _strip_prefix(line[4:]), _strip_prefix(lines[i + 1][4:])
            if old == "/dev/null" or new == "/dev/null":
                raise PatchError([PolicyFinding(rule="patch.create_delete", severity="block",
                                                message="Creating or deleting files is not allowed.",
                                                file=new if old == "/dev/null" else old)])
            if old != new:
                raise PatchError([PolicyFinding(rule="patch.rename", severity="block",
                                                message=f"Renaming files is not allowed: {old} -> {new}",
                                                file=old)])
            current = FilePatch(old, new)
            patches.append(current)
            i += 2
            continue
        m = _HUNK_RE.match(line)
        if m:
            if current is None:
                raise _invalid("Hunk found before any file header.")
            hunk = Hunk(int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1))
            i += 1
            old_seen = new_seen = 0
            while i < len(lines) and (old_seen < hunk.old_len or new_seen < hunk.new_len):
                body = lines[i]
                if body.startswith("\\"):
                    i += 1
                    continue
                if body == "":
                    body = " "  # some tools drop the leading space on blank context lines
                tag = body[0]
                if tag not in " +-":
                    raise _invalid(f"Malformed hunk line in {current.new_path}: {body[:60]!r}", current.new_path)
                if tag in " -":
                    old_seen += 1
                if tag in " +":
                    new_seen += 1
                hunk.lines.append(body)
                i += 1
            if old_seen != hunk.old_len or new_seen != hunk.new_len:
                raise _invalid(f"Hunk line counts do not match its header in {current.new_path}.", current.new_path)
            current.hunks.append(hunk)
            continue
        i += 1
    if not patches or not any(p.hunks for p in patches):
        raise _invalid("No file changes were found in the patch. Expected a unified diff.")
    return patches


def _apply_hunks(content: str, patch: FilePatch) -> str:
    lines = content.split("\n")
    offset = 0
    for hunk in patch.hunks:
        old = hunk.old_block()
        # "-l,0" means "insert after line l", i.e. at 0-based index l.
        base = hunk.old_start - 1 + (1 if hunk.old_len == 0 else 0)
        expected = base + offset
        if 0 <= expected <= len(lines) and lines[expected:expected + len(old)] == old:
            pos = expected
        else:
            hits = [j for j in range(len(lines) - len(old) + 1) if lines[j:j + len(old)] == old]
            if len(hits) != 1:
                raise _invalid(
                    f"Hunk at line {hunk.old_start} does not apply cleanly to {patch.new_path}.",
                    patch.new_path, "patch.does_not_apply",
                )
            pos = hits[0]
        new = hunk.new_block()
        lines[pos:pos + len(old)] = new
        offset = pos - base + len(new) - len(old)
    return "\n".join(lines)


def apply_unified_diff(files: dict[str, str], text: str) -> dict[str, str]:
    patches = parse_unified_diff(text)
    project_paths = set(files)
    blocked = [f for p in patches if (f := check_target_path(p.new_path, project_paths))]
    if blocked:
        raise PatchError(blocked)
    new_files = dict(files)
    for patch in patches:
        new_files[patch.new_path] = _apply_hunks(new_files[patch.new_path], patch)
    return new_files


# ---------------------------------------------------------------------------
# Canonical diff rendering and full candidate preparation
# ---------------------------------------------------------------------------


def render_diff(old: dict[str, str], new: dict[str, str]) -> str:
    chunks: list[str] = []
    for path in sorted(new):
        if old.get(path) == new[path]:
            continue
        diff = difflib.unified_diff(
            old.get(path, "").splitlines(keepends=True),
            new[path].splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
        text = "".join(d if d.endswith("\n") else d + "\n\\ No newline at end of file\n" for d in diff)
        chunks.append(text)
    return "".join(chunks)


def prepare_candidate(
    files: dict[str, str],
    *,
    provenance: PatchProvenance,
    label: str,
    edits: list[FileEdit] | None = None,
    diff: str | None = None,
) -> tuple[CandidatePatch, dict[str, str] | None]:
    """Validate and apply a candidate. Returns the record and the patched files (None if rejected)."""
    candidate = CandidatePatch(provenance=provenance, label=label, edits=edits or [])
    try:
        if edits:
            new_files = apply_edits(files, edits)
        elif diff:
            new_files = apply_unified_diff(files, diff)
        else:
            raise _invalid("No patch content was provided.")
        changed = sorted(p for p in new_files if new_files[p] != files.get(p))
        if not changed:
            raise _invalid("The patch does not change any file.", rule="patch.noop")
        canonical = render_diff(files, new_files)
        candidate.diff = canonical
        candidate.changed_files = changed
        changed_lines = sum(
            1 for ln in canonical.split("\n")
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
        )
        findings: list[PolicyFinding] = []
        if len(changed) > MAX_CHANGED_FILES:
            findings.append(PolicyFinding(rule="patch.too_many_files", severity="block",
                                          message=f"Patch changes {len(changed)} files (limit {MAX_CHANGED_FILES})."))
        if changed_lines > MAX_CHANGED_LINES:
            findings.append(PolicyFinding(rule="patch.too_large", severity="block",
                                          message=f"Patch changes {changed_lines} lines (limit {MAX_CHANGED_LINES})."))
        for path in changed:
            try:
                compile(new_files[path], path, "exec")  # parses only; never executes
            except SyntaxError as exc:
                findings.append(PolicyFinding(rule="patch.syntax_error", severity="invalid", file=path,
                                              message=f"Patched file does not parse: {path}:{exc.lineno}: {exc.msg}"))
            findings.extend(capability_findings(path, files.get(path, ""), new_files[path]))
        if findings:
            raise PatchError(findings)
    except PatchError as err:
        candidate.policy_findings = err.findings
        return candidate, None
    candidate.applied = True
    return candidate, new_files
