"""Turn a GitHub repository archive into a PatchProof project.

Uploads are strict (any unsupported file rejects the upload). A real repository always contains files PatchProof
does not use (licences, images, symlinks, CI config), so this reader *skips* them and reports what it skipped,
while applying the same path validation and bounded reads. Kept-file limits are not silently enforced by
truncation: a repository whose Python/text content exceeds them is rejected, because dropping files could break
its tests.
"""

from __future__ import annotations

import io
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .projects import (
    ALLOWED_SUFFIXES,
    MAX_FILE_BYTES,
    ProjectRejected,
    _decode,
    _is_ignored,
    normalize_path,
)
from .schemas import ProjectFile

MAX_REPO_FILES = 300
MAX_REPO_TOTAL_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000
MAX_SKIPPED_LISTED = 50
LFS_POINTER = b"version https://git-lfs.github.com/spec/"


@dataclass
class RepositoryFiles:
    files: list[ProjectFile]
    skipped_count: int = 0
    skipped: list[str] = field(default_factory=list)  # "path: reason", first MAX_SKIPPED_LISTED

    def skip(self, path: str, reason: str) -> None:
        self.skipped_count += 1
        if len(self.skipped) < MAX_SKIPPED_LISTED:
            self.skipped.append(f"{path[:200]}: {reason}")


def _open(data: bytes) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo], str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ProjectRejected("GitHub returned an archive PatchProof could not read.") from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ProjectRejected(f"The repository has more than {MAX_ARCHIVE_ENTRIES} entries.")
    # GitHub archives put everything under one top-level directory ("owner-repo-sha/").
    tops = {i.filename.split("/", 1)[0] for i in infos}
    prefix = next(iter(tops)) + "/" if len(tops) == 1 else ""
    return archive, infos, prefix


def read_repository_zip(data: bytes) -> RepositoryFiles:
    archive, infos, prefix = _open(data)
    result = RepositoryFiles(files=[])
    seen: set[str] = set()
    total = 0
    for info in infos:
        rel_raw = info.filename[len(prefix):] if info.filename.startswith(prefix) else info.filename
        if info.is_dir() or not rel_raw:
            continue
        if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
            result.skip(rel_raw, "symbolic link")
            continue
        if info.flag_bits & 0x1:
            raise ProjectRejected("Encrypted archive entries are not supported.")
        try:
            rel = normalize_path(rel_raw)
        except ProjectRejected:
            result.skip(rel_raw, "unsupported path")
            continue
        if _is_ignored(rel):
            continue
        if PurePosixPath(rel).suffix.lower() not in ALLOWED_SUFFIXES:
            result.skip(rel, "file type not used by PatchProof")
            continue
        if info.file_size > MAX_FILE_BYTES:
            result.skip(rel, f"larger than {MAX_FILE_BYTES // 1024} KB")
            continue
        with archive.open(info) as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)  # bounded: never trust declared sizes
        if len(raw) > MAX_FILE_BYTES:
            result.skip(rel, f"larger than {MAX_FILE_BYTES // 1024} KB")
            continue
        if raw.startswith(LFS_POINTER):
            result.skip(rel, "Git LFS pointer (content not fetched)")
            continue
        try:
            text = _decode(rel, raw)
        except ProjectRejected:
            result.skip(rel, "binary or not UTF-8")
            continue
        if rel.lower() in seen:
            result.skip(rel, "duplicate path (case-insensitive)")
            continue
        seen.add(rel.lower())
        total += len(raw)
        if len(seen) > MAX_REPO_FILES or total > MAX_REPO_TOTAL_BYTES:
            raise ProjectRejected(
                f"The repository has more than {MAX_REPO_FILES} supported files or "
                f"{MAX_REPO_TOTAL_BYTES // (1024 * 1024)} MB of them, beyond PatchProof's current limits.")
        result.files.append(ProjectFile(path=rel, content=text))
    if not result.files:
        raise ProjectRejected("No supported files were found in the repository.")
    if not any(f.path.endswith(".py") for f in result.files):
        raise ProjectRejected("PatchProof currently supports Python projects only (no .py files found).")
    result.files.sort(key=lambda f: f.path)
    return result


def raw_entries(data: bytes, paths: set[str]) -> dict[str, bytes]:
    """The original bytes of the given project paths (used to keep line endings when writing a patch back)."""
    archive, infos, prefix = _open(data)
    out = {}
    for info in infos:
        rel = info.filename[len(prefix):] if info.filename.startswith(prefix) else info.filename
        if rel in paths and not info.is_dir():
            with archive.open(info) as fh:
                out[rel] = fh.read(MAX_FILE_BYTES + 1)
    return out


def encode_like(original: bytes | None, text: str) -> bytes:
    """Encode new file text with the original's BOM and line endings, so a fix stays a minimal diff."""
    body = text
    if original is not None and b"\r\n" in original:
        body = body.replace("\n", "\r\n")
    encoded = body.encode("utf-8")
    if original is not None and original.startswith(b"\xef\xbb\xbf"):
        encoded = b"\xef\xbb\xbf" + encoded
    return encoded
