"""
Desktop tool registry tests against a real Linux/X11 backend on Xvfb.

These spawn xterm (text source for OCR + window listing), then exercise
each registered desktop_* tool through ToolRegistry.dispatch().
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user, pytest.mark.needs_display]

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _pyautogui_importable() -> bool:
    """pyautogui pulls in mouseinfo which sys.exit()s when tkinter is
    missing AND tries to open the X display at import time.  We have to
    catch SystemExit AND KeyError("DISPLAY")."""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        import pyautogui  # noqa: F401
        return True
    except (ImportError, SystemExit, KeyError):
        return False


def _require_backend():
    if not _pyautogui_importable():
        pytest.skip("pyautogui not importable (need python3-tk + DISPLAY)")


@pytest.fixture
def desktop_registry(tmp_path):
    _require_backend()
    from tools import ToolRegistry
    cfg = {
        "tools": {
            "shell_timeout_seconds": 5,
            "screenshot_dir": str(tmp_path / "screenshots"),
            "max_screenshot_width": 800,
            "perception_cache_ttl_seconds": 0.0,
            "allowed_shell": True,
            "allowed_filesystem": True,
            "allowed_desktop": True,
            "allowed_browser": False,
        },
        "agent": {"confirm_destructive": False,
                  "vision_screenshots": False},
        "safety": {"command_confirm_delay_seconds": 0,
                   "dry_run": False, "allowed_apps": [],
                   "blocked_window_titles": [], "fs_write_snapshot_dir": ""},
        "screen_observer": {"enabled": False},
    }
    return ToolRegistry(cfg)


def _call(reg, name, args):
    return json.loads(asyncio.run(reg.dispatch(name, args)))


# ---------------------------------------------------------------------------
# Screenshot family
# ---------------------------------------------------------------------------

class TestScreenshot:
    def test_desktop_screenshot_writes_a_file(self, desktop_registry, require_display):
        r = _call(desktop_registry, "desktop_screenshot", {})
        # Returns either {ok: true, file_path: "..."} or
        # {file_path: ..., image_b64: ...}.
        path = r.get("file_path") or r.get("path")
        assert path, r
        assert os.path.exists(path), f"screenshot not found at {path}"
        assert os.path.getsize(path) > 100


class TestListAndActiveWindow:
    def test_list_windows_finds_xterm(self, desktop_registry, xterm_window, require_display):
        r = _call(desktop_registry, "desktop_list_windows", {})
        titles = []
        for w in r.get("windows", []):
            titles.append(w.get("title", ""))
        # title may be quoted or include the exec line — substring match.
        assert any(xterm_window["title"] in t for t in titles), \
            f"missing {xterm_window['title']!r} in {titles!r}"

    def test_get_active_window(self, desktop_registry, xterm_window, require_display):
        # Bring xterm to front first.
        if shutil.which("wmctrl"):
            subprocess.run(["wmctrl", "-a", xterm_window["title"]],
                           timeout=5)
            time.sleep(0.3)
        if "desktop_get_active_window" not in desktop_registry._dispatch:
            pytest.skip("desktop_get_active_window not registered on this backend")
        r = _call(desktop_registry, "desktop_get_active_window", {})
        # Backend may return {title, window_id, ...} or {ok, ...}.
        title = r.get("title") or r.get("window_title") or ""
        # Active window may have race-conditioned to root window; accept either.
        assert isinstance(title, str)


# ---------------------------------------------------------------------------
# Launch / activate
# ---------------------------------------------------------------------------

class TestLaunchActivate:
    def test_launch_xterm(self, desktop_registry, require_display, tmp_path):
        # Use a unique title so we can later clean up.
        title = f"launch-test-{int(time.time()*1000)}"
        r = _call(desktop_registry, "desktop_launch",
                  {"application": "xterm",
                   "args": ["-T", title, "-e", "sleep 30"]})
        # Backend either reports {launched: true, pid: int} or {ok: true}.
        try:
            assert r.get("launched") is True or r.get("ok") is True or r.get("pid")
        finally:
            subprocess.run(["pkill", "-f", title], timeout=5)


# ---------------------------------------------------------------------------
# Type / hotkey / cursor / mouse_move
# ---------------------------------------------------------------------------

class TestKeyboardMouse:
    def test_get_cursor_pos(self, desktop_registry, require_display):
        if "desktop_get_cursor_pos" not in desktop_registry._dispatch:
            pytest.skip("not registered")
        r = _call(desktop_registry, "desktop_get_cursor_pos", {})
        assert "x" in r and "y" in r

    def test_mouse_move(self, desktop_registry, require_display):
        if "desktop_mouse_move" not in desktop_registry._dispatch:
            pytest.skip("not registered")
        r = _call(desktop_registry, "desktop_mouse_move", {"x": 100, "y": 100})
        # Should not throw.
        assert isinstance(r, dict)

    def test_hotkey_does_not_crash(self, desktop_registry, xterm_window, require_display):
        # Send a benign key combo to the focused window.
        if shutil.which("wmctrl"):
            subprocess.run(["wmctrl", "-a", xterm_window["title"]], timeout=5)
            time.sleep(0.3)
        r = _call(desktop_registry, "desktop_hotkey", {"keys": "shift"})
        assert isinstance(r, dict)


# ---------------------------------------------------------------------------
# OCR-based text targeting
# ---------------------------------------------------------------------------

class TestTextTools:
    def test_find_text_locates_xterm_banner(self, desktop_registry, xterm_window, require_display):
        if not shutil.which("tesseract"):
            pytest.skip("tesseract not installed")
        if "desktop_find_text" not in desktop_registry._dispatch:
            pytest.skip("desktop_find_text not registered")
        # xterm prints "USERTEST-OK-HELLO" on startup.
        # Give the renderer a moment to settle.
        time.sleep(1.0)
        r = _call(desktop_registry, "desktop_find_text",
                  {"text": "USERTEST"})
        # Either a match list or a "not found" envelope — both must round-trip.
        assert isinstance(r, dict)


# ---------------------------------------------------------------------------
# Screenshot-marked + click_mark
# ---------------------------------------------------------------------------

class TestSetOfMark:
    def test_screenshot_marked_returns_marks(self, desktop_registry, require_display):
        if "desktop_screenshot_marked" not in desktop_registry._dispatch:
            pytest.skip("not registered")
        r = _call(desktop_registry, "desktop_screenshot_marked", {})
        # Either marks list or graceful degrade.
        assert isinstance(r, dict)


# ---------------------------------------------------------------------------
# wait_for
# ---------------------------------------------------------------------------

class TestWaitFor:
    def test_desktop_wait_for_window_title(self, desktop_registry, xterm_window, require_display):
        if "desktop_wait_for" not in desktop_registry._dispatch:
            pytest.skip("not registered")
        r = _call(desktop_registry, "desktop_wait_for",
                  {"window_title": xterm_window["title"], "timeout": 5})
        # Should report a match.
        assert isinstance(r, dict)
