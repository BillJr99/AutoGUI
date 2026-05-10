"""Predicate normalisation + filesystem checker."""

from __future__ import annotations

from predicates import (
    check_filesystem_predicate_sync,
    normalize,
    render,
)


def test_normalize_accepts_known_kind():
    p = normalize({"kind": "file_exists", "path": "/tmp/x"})
    assert p == {"kind": "file_exists", "path": "/tmp/x"}


def test_normalize_rejects_unknown_kind():
    assert normalize({"kind": "smelly", "value": "x"}) is None


def test_normalize_aliases_type_to_kind():
    p = normalize({"type": "file_exists", "path": "/tmp/x"})
    assert p["kind"] == "file_exists"
    assert "type" not in p


def test_render_known_kinds():
    assert "Notepad" in render({"kind": "window_title_contains", "value": "Notepad"})
    assert "Save" in render({"kind": "text_visible", "value": "Save"})


def test_file_exists_pass(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    res = check_filesystem_predicate_sync({"kind": "file_exists", "path": str(f)})
    assert res.ok is True


def test_file_exists_fail(tmp_path):
    res = check_filesystem_predicate_sync({"kind": "file_exists", "path": str(tmp_path / "missing")})
    assert res.ok is False
    assert "absent" in res.detail.lower() or "missing" in res.detail.lower()


def test_file_contains_pass(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    res = check_filesystem_predicate_sync(
        {"kind": "file_contains", "path": str(f), "value": "world"},
    )
    assert res.ok is True


def test_file_contains_fail(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello world")
    res = check_filesystem_predicate_sync(
        {"kind": "file_contains", "path": str(f), "value": "missing"},
    )
    assert res.ok is False


def test_file_absent_pass(tmp_path):
    res = check_filesystem_predicate_sync(
        {"kind": "file_absent", "path": str(tmp_path / "nope")},
    )
    assert res.ok is True


def test_file_exists_rejects_directory(tmp_path):
    """A directory at the path should NOT satisfy file_exists — the kind
    is named for *files*, and the README documents that semantic."""
    d = tmp_path / "a_dir"
    d.mkdir()
    res = check_filesystem_predicate_sync({"kind": "file_exists", "path": str(d)})
    assert res.ok is False
    assert "directory" in res.detail.lower() or "absent" in res.detail.lower()


def test_file_absent_passes_for_directory(tmp_path):
    """Symmetric: a directory at the path should NOT make file_absent
    fail — only a regular file should count as 'present'."""
    d = tmp_path / "a_dir"
    d.mkdir()
    res = check_filesystem_predicate_sync({"kind": "file_absent", "path": str(d)})
    assert res.ok is True
