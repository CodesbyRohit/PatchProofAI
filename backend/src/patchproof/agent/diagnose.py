"""Parse and validate the model's structured diagnosis."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from ..schemas import Diagnosis, FileEdit

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
MAX_EDITS = 8


class DiagnosisParseError(ValueError):
    pass


def _candidates(text: str) -> list[str]:
    text = _THINK_RE.sub("", text).strip()
    out = [m.group(1).strip() for m in _FENCE_RE.finditer(text)]
    out.append(text)
    return out


def extract_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    for chunk in _candidates(text):
        for start in [i for i, ch in enumerate(chunk) if ch == "{"][:50]:
            try:
                obj, _ = decoder.raw_decode(chunk, start)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and ("edits" in obj or "summary" in obj):
                return obj
    raise DiagnosisParseError("No JSON object with 'summary' and 'edits' was found in the response.")


def parse_diagnosis(text: str) -> tuple[Diagnosis, list[FileEdit]]:
    data = extract_json(text)
    raw_edits = data.pop("edits", None)
    if not isinstance(raw_edits, list) or not raw_edits:
        raise DiagnosisParseError("'edits' must be a non-empty list of {file, search, replace} objects.")
    if len(raw_edits) > MAX_EDITS:
        raise DiagnosisParseError(f"Too many edits ({len(raw_edits)}); at most {MAX_EDITS} are allowed.")
    edits: list[FileEdit] = []
    for i, e in enumerate(raw_edits):
        if not isinstance(e, dict):
            raise DiagnosisParseError(f"edits[{i}] is not an object.")
        try:
            edits.append(FileEdit(file=str(e.get("file", "")).strip().removeprefix("./"),
                                  search=str(e["search"]), replace=str(e.get("replace", ""))))
        except KeyError as exc:
            raise DiagnosisParseError(f"edits[{i}] is missing 'search'.") from exc
    conf = str(data.get("confidence", "low")).lower()
    data["confidence"] = conf if conf in {"low", "medium", "high"} else "low"
    for key in ("potential_regressions", "additional_tests"):
        val = data.get(key)
        if isinstance(val, str):
            data[key] = [val]
        elif not isinstance(val, list):
            data[key] = []
        else:
            data[key] = [str(v) for v in val]
    ev = data.get("evidence")
    data["evidence"] = [
        {"file": str(x.get("file", "")), "lines": None if x.get("lines") is None else str(x.get("lines")),
         "observation": str(x.get("observation", ""))}
        for x in ev if isinstance(x, dict)
    ] if isinstance(ev, list) else []
    for key in ("summary", "root_cause", "rationale", "confidence_explanation"):
        if key in data and not isinstance(data[key], str):
            data[key] = json.dumps(data[key])
    try:
        diagnosis = Diagnosis.model_validate({k: v for k, v in data.items() if k in Diagnosis.model_fields})
    except ValidationError as exc:
        raise DiagnosisParseError(f"Diagnosis fields are invalid: {exc.errors()[0]['msg']}") from exc
    return diagnosis, edits
