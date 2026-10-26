"""Bundled examples: trusted demo projects shipped with PatchProof, each with scripted patches."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from .projects import build_project, validate_files
from .schemas import FileEdit, Project


class ScriptedPatch(BaseModel):
    id: str
    label: str
    description: str
    edits: list[FileEdit] = Field(default_factory=list)
    diff: str | None = None
    expected_verdict: str | None = None


class Example(BaseModel):
    id: str
    title: str
    scenario: str
    description: str
    issue: str
    recommended_patch: str | None = None
    patches: list[ScriptedPatch] = Field(default_factory=list)


def load_project_dir(root: Path) -> list[tuple[str, bytes]]:
    return [(p.relative_to(root).as_posix(), p.read_bytes()) for p in sorted(root.rglob("*")) if p.is_file()]


def load_examples(examples_dir: Path) -> dict[str, tuple[Example, Project]]:
    out: dict[str, tuple[Example, Project]] = {}
    if not examples_dir.is_dir():
        return out
    for meta_path in sorted(examples_dir.glob("*/example.json")):
        folder = meta_path.parent
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for patch in meta.get("patches", []):
            if patch.get("diff_file"):
                patch["diff"] = (folder / patch.pop("diff_file")).read_text(encoding="utf-8")
        example = Example.model_validate(meta)
        files = validate_files(load_project_dir(folder / "project"))
        project = build_project(example.title, "example", files, trusted=True, project_id=f"example:{example.id}")
        out[example.id] = (example, project)
    return out
