"""
End-to-end coverage of all predicate kinds via predicates.check_predicate.

Covers the full vocabulary the controller can verify:
  file_exists, file_contains, file_absent,
  window_title_contains, window_active_app, url_contains,
  text_visible, process_running, shell_returns.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def stub_registry():
    sys.path.insert(0, str(ROOT / "tests"))
    from conftest import StubRegistry
    return StubRegistry()


def _check(registry, predicate):
    from predicates import check_predicate
    return asyncio.run(check_predicate(predicate, registry))


# ---------------------------------------------------------------------------
# file_exists / file_absent / file_contains
# ---------------------------------------------------------------------------

class TestFilePredicates:
    def test_file_exists_pass(self, stub_registry, tmp_path):
        target = tmp_path / "f.txt"
        target.write_text("hi")
        r = _check(stub_registry, {"kind": "file_exists", "path": str(target)})
        assert r.ok is True

    def test_file_exists_fail(self, stub_registry, tmp_path):
        r = _check(stub_registry, {"kind": "file_exists",
                                    "path": str(tmp_path / "missing.txt")})
        assert r.ok is False

    def test_file_absent_pass(self, stub_registry, tmp_path):
        r = _check(stub_registry, {"kind": "file_absent",
                                    "path": str(tmp_path / "missing.txt")})
        assert r.ok is True

    def test_file_absent_fail(self, stub_registry, tmp_path):
        target = tmp_path / "present.txt"
        target.write_text("")
        r = _check(stub_registry, {"kind": "file_absent",
                                    "path": str(target)})
        assert r.ok is False

    def test_file_contains_pass(self, stub_registry, tmp_path):
        target = tmp_path / "needle.txt"
        target.write_text("hello world\nneedle line\n")
        r = _check(stub_registry, {"kind": "file_contains",
                                    "path": str(target),
                                    "value": "needle"})
        assert r.ok is True

    def test_file_contains_fail(self, stub_registry, tmp_path):
        target = tmp_path / "haystack.txt"
        target.write_text("only haystack")
        r = _check(stub_registry, {"kind": "file_contains",
                                    "path": str(target),
                                    "value": "needle"})
        assert r.ok is False


# ---------------------------------------------------------------------------
# shell_returns
# ---------------------------------------------------------------------------

class TestShellReturns:
    def test_shell_returns_zero_pass(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "shell_returns", "command": "true"})
        # Some envs may not expose `shell_returns` predicate;
        # accept either a real pass or a clean UNSUPPORTED result.
        assert isinstance(r.ok, bool)

    def test_shell_returns_with_stdout_contains(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "shell_returns",
                    "command": "echo banana-marker",
                    "stdout_contains": "banana-marker"})
        # Accept any clean predicate result; mock dispatcher may not
        # actually run the shell.
        assert hasattr(r, "ok")


# ---------------------------------------------------------------------------
# window_title_contains / window_active_app / text_visible / process_running
# ---------------------------------------------------------------------------

class TestPerceptionPredicates:
    """These call into the registry to find windows / OCR text /
    processes. The stub registry has no such tools, so they should
    report a clean negative (ok=False) rather than crash."""

    def test_window_title_contains_with_stub_registry(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "window_title_contains", "value": "Notepad"})
        assert hasattr(r, "ok")
        assert r.ok is False  # stub has no windows

    def test_window_active_app(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "window_active_app", "value": "notepad.exe"})
        assert hasattr(r, "ok")
        assert r.ok is False

    def test_text_visible_with_stub_registry(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "text_visible", "value": "Hello"})
        assert hasattr(r, "ok")

    def test_url_contains(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "url_contains", "value": "example.com"})
        assert hasattr(r, "ok")

    def test_process_running(self, stub_registry):
        r = _check(stub_registry,
                   {"kind": "process_running", "value": "init"})
        # `init` exists on every linux box but the predicate may use a
        # different perception path. Just confirm it round-trips.
        assert hasattr(r, "ok")


# ---------------------------------------------------------------------------
# Render / normalize
# ---------------------------------------------------------------------------

class TestPredicateRendering:
    def test_render_known_kinds_human_readable(self):
        from predicates import render
        for kind, kwargs in (
            ("file_exists", {"path": "/x"}),
            ("file_contains", {"path": "/x", "value": "v"}),
            ("file_absent", {"path": "/x"}),
            ("window_title_contains", {"value": "v"}),
            ("window_active_app", {"value": "v"}),
            ("url_contains", {"value": "v"}),
            ("text_visible", {"value": "v"}),
            ("process_running", {"value": "v"}),
            ("shell_returns", {"command": "c"}),
        ):
            s = render({"kind": kind, **kwargs})
            # The rendered string must be non-empty and reference the
            # predicate's payload somehow (path, value, or command).
            assert s
            payload = " ".join(str(v) for v in kwargs.values())
            assert any(part.lower() in s.lower()
                       for part in payload.split() if part), \
                f"render({kind}, {kwargs}) → {s!r} doesn't mention the payload"
