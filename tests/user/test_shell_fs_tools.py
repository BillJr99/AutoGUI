"""
End-to-end tests for the shell + filesystem tool registrations.

These tests build a real ToolRegistry (no LLM, no stubs) and invoke each
tool via `registry.dispatch(...)`, asserting the JSON envelope they
return. The shell/fs tools are platform-agnostic and always registered
when the corresponding config flag is set.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def make_registry(tmp_path):
    """Build a real ToolRegistry with shell + fs enabled, desktop/browser off."""
    from tools import ToolRegistry

    def _build(**overrides):
        cfg = {
            "tools": {
                "shell_timeout_seconds": 5,
                "screenshot_dir": str(tmp_path / "screenshots"),
                "max_screenshot_width": 800,
                "perception_cache_ttl_seconds": 0.0,
                "allowed_shell": True,
                "allowed_filesystem": True,
                "allowed_desktop": False,
                "allowed_browser": False,
            },
            "agent": {"confirm_destructive": False,
                      "vision_screenshots": False},
            "safety": {"command_confirm_delay_seconds": 0,
                       "dry_run": False, "allowed_apps": [],
                       "blocked_window_titles": [],
                       "fs_write_snapshot_dir": str(tmp_path / "fs_snapshots")},
        }
        cfg["tools"].update(overrides.get("tools", {}))
        return ToolRegistry(cfg)

    return _build


def _call(reg, name, args):
    return asyncio.run(reg.dispatch(name, args))


# ---------------------------------------------------------------------------
# shell_run
# ---------------------------------------------------------------------------

class TestShellRun:
    def test_echo_happy_path(self, make_registry):
        reg = make_registry()
        result = json.loads(_call(reg, "shell_run", {"command": "echo hello-shell"}))
        # shell_run returns {stdout, stderr, exit_code, timed_out} — no `ok`.
        assert result["exit_code"] == 0
        assert "hello-shell" in result["stdout"]
        assert result["timed_out"] is False

    def test_nonzero_exit_records_exit_code(self, make_registry):
        reg = make_registry()
        result = json.loads(_call(reg, "shell_run", {"command": "false"}))
        assert result["exit_code"] == 1
        assert result["timed_out"] is False

    def test_timeout_returns_clean_error(self, make_registry):
        # `shell_timeout_seconds` is set via config, not via call args.
        reg = make_registry(tools={"shell_timeout_seconds": 1})
        result = json.loads(_call(reg, "shell_run", {"command": "sleep 3"}))
        assert result["timed_out"] is True or result["exit_code"] != 0

    def test_disallowed_when_flag_off(self, make_registry):
        reg = make_registry(tools={"allowed_shell": False})
        # shell_run not registered at all.
        assert "shell_run" not in reg._dispatch


# ---------------------------------------------------------------------------
# fs_read / fs_write / fs_list
# ---------------------------------------------------------------------------

class TestFilesystemTools:
    def test_round_trip_read_write_list(self, make_registry, tmp_path):
        reg = make_registry()
        target = tmp_path / "hello.txt"
        body = "user-test contents"

        w = json.loads(_call(reg, "fs_write",
                             {"path": str(target), "content": body}))
        assert w["success"] is True
        assert w["bytes_written"] == len(body)

        r = json.loads(_call(reg, "fs_read", {"path": str(target)}))
        assert r["content"] == body
        assert r["truncated"] is False

        ls = json.loads(_call(reg, "fs_list", {"path": str(tmp_path),
                                                "pattern": "*.txt"}))
        names = [e["name"] for e in ls["entries"]]
        assert "hello.txt" in names

    def test_fs_read_truncates_at_max_bytes(self, make_registry, tmp_path):
        reg = make_registry()
        big = tmp_path / "big.txt"
        big.write_text("x" * 1024)
        r = json.loads(_call(reg, "fs_read",
                             {"path": str(big), "max_bytes": 50}))
        assert r["truncated"] is True
        assert len(r["content"]) <= 50

    def test_fs_list_pattern_filters(self, make_registry, tmp_path):
        reg = make_registry()
        (tmp_path / "a.log").write_text("a")
        (tmp_path / "b.log").write_text("b")
        (tmp_path / "c.txt").write_text("c")
        r = json.loads(_call(reg, "fs_list",
                             {"path": str(tmp_path), "pattern": "*.log"}))
        names = [e["name"] for e in r["entries"]]
        assert set(names) == {"a.log", "b.log"}, names

    def test_fs_read_missing_file_returns_error(self, make_registry, tmp_path):
        reg = make_registry()
        r = json.loads(_call(reg, "fs_read",
                             {"path": str(tmp_path / "nope.txt")}))
        # fs_read returns {error: "..."} on failure.
        assert "error" in r
        assert "exist" in r["error"].lower() or "not" in r["error"].lower()

    def test_disallowed_when_flag_off(self, make_registry):
        reg = make_registry(tools={"allowed_filesystem": False})
        for name in ("fs_read", "fs_write", "fs_list"):
            assert name not in reg._dispatch
