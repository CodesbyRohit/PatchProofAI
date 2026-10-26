import pytest

from patchproof.schemas import FileEdit
from patchproof.verification.patching import (
    PatchError,
    apply_edits,
    apply_unified_diff,
    parse_unified_diff,
    prepare_candidate,
    render_diff,
)

FILES = {
    "calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
    "tests/test_calc.py": "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "pyproject.toml": "[project]\nname='x'\n",
}


def test_edit_applies_and_renders_canonical_diff():
    new = apply_edits(FILES, [FileEdit(file="calc.py", search="    return a - b\n", replace="    return a + b\n")])
    assert "return a + b" in new["calc.py"]
    diff = render_diff(FILES, new)
    assert diff.startswith("--- a/calc.py\n+++ b/calc.py\n")
    assert "-    return a - b" in diff and "+    return a + b" in diff
    # The canonical diff must round-trip through the strict applier.
    assert apply_unified_diff(FILES, diff)["calc.py"] == new["calc.py"]


def test_edit_tolerates_trailing_whitespace_only():
    new = apply_edits(FILES, [FileEdit(file="calc.py", search="    return a - b   ", replace="    return a + b")])
    assert "return a + b" in new["calc.py"]
    with pytest.raises(PatchError, match="not found"):
        apply_edits(FILES, [FileEdit(file="calc.py", search="return a - b\n  ", replace="x")])
    with pytest.raises(PatchError, match="not found"):
        apply_edits(FILES, [FileEdit(file="calc.py", search="def add(a, b):\n  return a - b", replace="x")])  # wrong indent


def test_edit_ambiguous():
    files = {"m.py": "x = 1\nx = 1\n"}
    with pytest.raises(PatchError, match="matches 2"):
        apply_edits(files, [FileEdit(file="m.py", search="x = 1\n", replace="x = 2\n")])


@pytest.mark.parametrize("path,rule", [
    ("tests/test_calc.py", "file.test_protected"),
    ("conftest.py", "file.test_protected"),
    ("pyproject.toml", "file.config_protected"),
    ("../calc.py", "path.traversal"),
    ("/etc/passwd", "path.absolute"),
    ("C:/Windows/x.py", "path.absolute"),
    ("missing.py", "file.unknown"),
    (".github/workflows/ci.py", "file.config_protected"),
])
def test_edit_path_policy(path, rule):
    with pytest.raises(PatchError) as info:
        apply_edits(FILES | {".github/workflows/ci.py": ""}, [FileEdit(file=path, search="x", replace="y")])
    assert info.value.findings[0].rule == rule
    assert info.value.blocked


def test_unified_diff_with_offset_and_insertion():
    diff = (
        "--- a/calc.py\n+++ b/calc.py\n"
        "@@ -6,1 +6,2 @@\n"  # stated line is wrong by one; unique context still locates it
        "     return a * b\n+# trailing comment\n"
    )
    new = apply_unified_diff(FILES, diff)
    assert new["calc.py"].endswith("    return a * b\n# trailing comment\n")


def test_unified_diff_pure_insertion_hunk():
    diff = "--- a/calc.py\n+++ b/calc.py\n@@ -0,0 +1,1 @@\n+\"\"\"Calculator.\"\"\"\n"
    new = apply_unified_diff(FILES, diff)
    assert new["calc.py"].startswith('"""Calculator."""\ndef add')


def test_unified_diff_rejections():
    with pytest.raises(PatchError, match="No file changes"):
        parse_unified_diff("just some prose, not a diff")
    with pytest.raises(PatchError) as info:
        parse_unified_diff("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x\n")
    assert info.value.findings[0].rule == "patch.create_delete"
    with pytest.raises(PatchError) as info:
        parse_unified_diff("--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n")
    assert info.value.findings[0].rule == "patch.rename"
    with pytest.raises(PatchError) as info:
        parse_unified_diff("diff --git a/x b/x\nGIT binary patch\nliteral 1\n")
    assert info.value.findings[0].rule == "patch.binary"
    with pytest.raises(PatchError, match="line counts"):
        parse_unified_diff("--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,3 @@\n-x\n+y\n")
    with pytest.raises(PatchError, match="does not apply"):
        apply_unified_diff(FILES, "--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-nonexistent\n+y\n")
    with pytest.raises(PatchError, match="too_large|exceeds"):
        parse_unified_diff("x" * 70_000)


def test_unified_diff_cannot_touch_tests():
    diff = "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -4 +4 @@\n-    assert add(1, 2) == 3\n+    assert True\n"
    with pytest.raises(PatchError) as info:
        apply_unified_diff(FILES, diff)
    assert info.value.blocked


def test_prepare_candidate_success_records_exact_diff():
    cand, new = prepare_candidate(FILES, provenance="user_supplied", label="x",
                                  edits=[FileEdit(file="calc.py", search="a - b", replace="a + b")])
    assert cand.applied and new is not None
    assert cand.changed_files == ["calc.py"]
    assert "+    return a + b" in cand.diff


def test_prepare_candidate_syntax_error_is_invalid_not_blocked():
    cand, new = prepare_candidate(FILES, provenance="live_model", label="x",
                                  edits=[FileEdit(file="calc.py", search="    return a - b\n", replace="    return (a +\n")])
    assert new is None and not cand.applied
    assert [f.rule for f in cand.policy_findings] == ["patch.syntax_error"]
    assert cand.policy_findings[0].severity == "invalid"


@pytest.mark.parametrize("replacement,cap", [
    ("    import os\n    return a + b\n", "import os"),
    ("    __import__('subprocess')\n    return a + b\n", "use __import__()"),
    ("    open('/etc/passwd')\n    return a + b\n", "use open()"),
    ("    return ().__class__.__bases__[0].__subclasses__()\n", "access .__subclasses__"),
    ("    from pytest import skip\n    return a + b\n", "import pytest"),
])
def test_prepare_candidate_blocks_new_dangerous_capabilities(replacement, cap):
    cand, new = prepare_candidate(FILES, provenance="live_model", label="x",
                                  edits=[FileEdit(file="calc.py", search="    return a - b\n", replace=replacement)])
    assert new is None
    assert any(cap in f.message and f.severity == "block" for f in cand.policy_findings)


def test_existing_capability_is_not_flagged():
    files = {"m.py": "import os\n\ndef p():\n    return os.sep + 'a'\n"}
    cand, new = prepare_candidate(files, provenance="live_model", label="x",
                                  edits=[FileEdit(file="m.py", search="'a'", replace="'b'")])
    assert new is not None and cand.applied


def test_prepare_candidate_noop_and_empty():
    cand, new = prepare_candidate(FILES, provenance="live_model", label="x",
                                  edits=[FileEdit(file="calc.py", search="a * b", replace="a * b")])
    assert new is None and cand.policy_findings[0].rule == "patch.noop"
    cand, new = prepare_candidate(FILES, provenance="user_supplied", label="x", diff="")
    assert new is None
