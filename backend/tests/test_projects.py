import io
import stat
import zipfile

import pytest

from patchproof.projects import (
    ProjectRejected,
    build_project,
    is_source_path,
    is_test_path,
    normalize_path,
    read_zip,
    validate_files,
)


def make_zip(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


@pytest.mark.parametrize("bad", ["../evil.py", "a/../../evil.py", "/etc/passwd", "C:/x.py", "a\\b.py",
                                 "a/./b.py", "x\x00.py", "", "a/" * 10 + "b.py", "weird$name.py"])
def test_normalize_rejects_dangerous_paths(bad):
    with pytest.raises(ProjectRejected):
        normalize_path(bad)


def test_normalize_accepts_normal_paths():
    assert normalize_path("pkg/mod.py") == "pkg/mod.py"
    assert normalize_path("tests/test_x.py") == "tests/test_x.py"


def test_zip_traversal_rejected():
    with pytest.raises(ProjectRejected, match="traversal"):
        read_zip(make_zip({"../../outside.py": b"x = 1\n"}))


def test_zip_absolute_path_rejected():
    with pytest.raises(ProjectRejected, match="Absolute"):
        read_zip(make_zip({"/abs.py": b"x = 1\n"}))


def test_zip_symlink_rejected():
    with pytest.raises(ProjectRejected, match="Symbolic"):
        read_zip(make_zip({"a.py": b"x = 1\n"}, symlink="link.py"))


def test_zip_not_a_zip():
    with pytest.raises(ProjectRejected, match="valid ZIP"):
        read_zip(b"definitely not a zip")


def test_zip_strips_single_top_level_dir_and_ignores_caches():
    files = read_zip(make_zip({
        "repo-main/app.py": b"x = 1\r\n",
        "repo-main/tests/test_app.py": b"def test_x():\n    pass\n",
        "repo-main/__pycache__/app.cpython-312.pyc": b"\x00\x01",
    }))
    assert [f.path for f in files] == ["app.py", "tests/test_app.py"]
    assert files[0].content == "x = 1\n"  # CRLF normalised


def test_zip_bomb_like_declared_size_rejected():
    big = b"a" * (5 * 1024 * 1024)
    with pytest.raises(ProjectRejected):
        read_zip(make_zip({"big.py": big}))


def test_unsupported_extension_rejected():
    with pytest.raises(ProjectRejected, match="Unsupported file type"):
        validate_files([("app.py", b"x=1"), ("payload.exe", b"MZ")])


def test_binary_content_rejected():
    with pytest.raises(ProjectRejected, match="Binary"):
        validate_files([("app.py", b"x=\x00")])


def test_oversized_file_rejected():
    with pytest.raises(ProjectRejected, match="exceeds"):
        validate_files([("app.py", b"#" * (300 * 1024))])


def test_requires_python():
    with pytest.raises(ProjectRejected, match="Python"):
        validate_files([("README.md", b"hi")])


def test_case_insensitive_duplicates_rejected():
    with pytest.raises(ProjectRejected, match="Duplicate"):
        validate_files([("App.py", b"x=1"), ("app.py", b"x=2")])


def test_classification_and_summary():
    files = validate_files([
        ("src/shop/cart.py", b"import requests\nimport json\nfrom shop import util\n"),
        ("src/shop/util.py", b"X = 1\n"),
        ("tests/test_cart.py", b"import pytest\nfrom shop.cart import *\n"),
        ("conftest.py", b""),
        ("README.md", b"# readme"),
    ])
    project = build_project("demo<script>", "inline", files)
    assert project.name == "demoscript"
    s = project.summary
    assert s.source_files == ["src/shop/cart.py", "src/shop/util.py"]
    assert "tests/test_cart.py" in s.test_files and "conftest.py" in s.test_files
    assert s.third_party_imports == ["requests"]
    assert any("requests" in n for n in s.notes)
    assert is_test_path("pkg/test_x.py") and is_test_path("pkg/x_test.py")
    assert not is_source_path("setup.py")
