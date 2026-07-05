"""
test_backends.py — backend parity tests (CI tier, headless).

Covers the generic launch default, per-platform launch overrides, the
list_windows native fallbacks (win32gui, Quartz, xdotool), and the macOS
AXPress click path.  Platform APIs that don't exist on the CI host
(win32gui, Quartz, AppKit, ApplicationServices) are injected as fake
modules via sys.modules, so these paths are mock-verified only — manual
verification on real Windows/macOS hardware is still required.
"""

from __future__ import annotations

import os
import stat
import sys
import types

import pytest

from backends.base import DesktopBackend
from backends.linux_x11 import X11Backend
from backends.macos import MacOSBackend
from backends.windows import WindowsBackend
from backends.wsl import WSLBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _stub_exe(directory, name: str, script: str) -> None:
    """Drop an executable shell stub into *directory*."""
    path = directory / name
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Generic launch default (backends/base.py)
# ---------------------------------------------------------------------------

class TestBaseLaunch:
    async def test_launch_spawns_subprocess(self):
        backend = DesktopBackend()
        result = await backend.launch("/bin/sh", ["-c", "exit 0"])
        assert result.get("success") is True
        assert result["application"] == "/bin/sh"
        assert result["args"] == ["-c", "exit 0"]
        assert isinstance(result["pid"], int)
        assert result["method"] == "subprocess"

    async def test_launch_missing_binary_returns_error(self):
        backend = DesktopBackend()
        result = await backend.launch("definitely-missing-app-xyz")
        assert "error" in result

    async def test_launch_nonzero_exit_returns_error(self):
        backend = DesktopBackend()
        result = await backend.launch("/bin/sh", ["-c", "echo boom >&2; exit 3"])
        assert "error" in result
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# Windows: win32gui.EnumWindows fallback + startfile launch fallback
# ---------------------------------------------------------------------------

def _install_fake_win32(monkeypatch, hwnds: dict) -> list:
    """Register fake win32gui/win32process modules.  ``hwnds`` maps
    hwnd -> {visible, title, rect, pid}.  Returns the enum-call log."""
    calls = []

    def enum_windows(cb, extra):
        calls.append("EnumWindows")
        for hwnd in hwnds:
            cb(hwnd, extra)

    win32gui = _module(
        "win32gui",
        EnumWindows=enum_windows,
        IsWindowVisible=lambda h: hwnds[h]["visible"],
        GetWindowText=lambda h: hwnds[h]["title"],
        GetWindowRect=lambda h: hwnds[h]["rect"],
        GetForegroundWindow=lambda: next(iter(hwnds)),
    )
    win32process = _module(
        "win32process",
        GetWindowThreadProcessId=lambda h: (1, hwnds[h]["pid"]),
    )
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process", win32process)
    return calls


class TestWindowsListWindows:
    async def test_win32gui_enumeration_schema(self, monkeypatch):
        hwnds = {
            101: {"visible": True, "title": "Notepad", "rect": (10, 20, 410, 320), "pid": 55},
            102: {"visible": False, "title": "hidden", "rect": (0, 0, 1, 1), "pid": 56},
            103: {"visible": True, "title": "", "rect": (0, 0, 1, 1), "pid": 57},
        }
        calls = _install_fake_win32(monkeypatch, hwnds)
        backend = WindowsBackend()
        result = await backend.list_windows()
        assert calls == ["EnumWindows"]
        assert result["method"] == "win32gui"
        assert result["count"] == 1  # invisible + untitled windows skipped
        win = result["windows"][0]
        assert win["id"] == "101"
        assert win["title"] == "Notepad"
        assert win["pid"] == 55
        assert win["active"] is True
        assert (win["x"], win["y"], win["width"], win["height"]) == (10, 20, 400, 300)
        # Same schema keys as the PowerShell path.
        assert set(win) == {"id", "title", "app", "pid", "active", "x", "y", "width", "height"}

    async def test_without_pywin32_degrades_without_crash(self):
        # No fake win32gui registered and no powershell on the CI host:
        # list_windows must still return the standard dict shape.
        backend = WindowsBackend()
        result = await backend.list_windows()
        assert isinstance(result, dict)
        assert "windows" in result or "error" in result


class TestWindowsLaunch:
    async def test_launch_with_args_errors_on_posix_host(self):
        # creationflags=0x8 (DETACHED_PROCESS) is Windows-only, so the direct
        # spawn raises on this host; with args present there is no startfile
        # fallback, so an error dict must come back (never an exception).
        backend = WindowsBackend()
        result = await backend.launch("/bin/sh", ["-c", "exit 0"])
        assert "error" in result

    async def test_startfile_fallback(self, monkeypatch):
        opened = []
        monkeypatch.setattr(os, "startfile", lambda target: opened.append(target),
                            raising=False)
        backend = WindowsBackend()
        result = await backend.launch("winword")  # spawn fails on this host
        assert result.get("success") is True
        assert result["method"] == "startfile"
        assert opened == ["winword"]

    async def test_startfile_not_used_when_args_present(self, monkeypatch):
        opened = []
        monkeypatch.setattr(os, "startfile", lambda target: opened.append(target),
                            raising=False)
        backend = WindowsBackend()
        result = await backend.launch("winword", ["/safe"])
        assert "error" in result
        assert opened == []


# ---------------------------------------------------------------------------
# WSL: PowerShell Start-Process fallback chain
# ---------------------------------------------------------------------------

class TestWSLLaunch:
    async def test_direct_then_powershell_failure_reports_both(self):
        backend = WSLBackend()
        result = await backend.launch("definitely-missing-app-xyz")
        assert "error" in result
        assert "direct:" in result["error"]
        assert "powershell:" in result["error"]


# ---------------------------------------------------------------------------
# macOS: Quartz list_windows fallback
# ---------------------------------------------------------------------------

def _install_fake_quartz(monkeypatch, infos: list[dict]) -> None:
    quartz = _module(
        "Quartz",
        kCGWindowListOptionOnScreenOnly=1,
        kCGWindowListExcludeDesktopElements=16,
        kCGNullWindowID=0,
        CGWindowListCopyWindowInfo=lambda options, wid: infos,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)


class TestMacOSListWindows:
    async def test_quartz_fallback_schema(self, monkeypatch):
        _install_fake_quartz(monkeypatch, [
            {
                "kCGWindowLayer": 0,
                "kCGWindowName": "Untitled",
                "kCGWindowOwnerName": "TextEdit",
                "kCGWindowOwnerPID": 77,
                "kCGWindowBounds": {"X": 5, "Y": 10, "Width": 640, "Height": 480},
            },
            {   # menu bar / overlay layers are skipped
                "kCGWindowLayer": 25,
                "kCGWindowName": "Menubar",
                "kCGWindowOwnerName": "Window Server",
                "kCGWindowOwnerPID": 1,
                "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1920, "Height": 24},
            },
        ])
        backend = MacOSBackend()
        # osascript does not exist on the CI host, so list_windows takes the
        # Quartz fallback automatically.
        result = await backend.list_windows()
        assert result["method"] == "quartz"
        assert result["count"] == 1
        win = result["windows"][0]
        assert win == {
            "title": "Untitled", "app": "TextEdit", "pid": 77,
            "x": 5, "y": 10, "width": 640, "height": 480,
        }

    async def test_no_quartz_returns_error(self):
        backend = MacOSBackend()
        result = await backend.list_windows()
        assert "error" in result


# ---------------------------------------------------------------------------
# macOS: AX element click (AXUIElementPerformAction) — mock-verified
# ---------------------------------------------------------------------------

class FakeAXElement:
    def __init__(self, attrs: dict):
        self.attrs = attrs


def _install_fake_ax(monkeypatch, root: FakeAXElement, trusted: bool = True,
                     press_result: int = 0) -> dict:
    """Register fake ApplicationServices + AppKit modules around an AX tree.

    Returns a log dict recording PerformAction invocations."""
    log = {"pressed": [], "created_for_pid": []}

    def create_application(pid):
        log["created_for_pid"].append(pid)
        return root

    def copy_attribute_value(elem, attr, _out):
        # pyobjc returns (err, value); err 0 == kAXErrorSuccess.
        return (0, elem.attrs.get(attr))

    def perform_action(elem, action):
        log["pressed"].append((elem, action))
        return press_result

    application_services = _module(
        "ApplicationServices",
        AXIsProcessTrusted=lambda: trusted,
        AXUIElementCreateApplication=create_application,
        AXUIElementCopyAttributeValue=copy_attribute_value,
        AXUIElementPerformAction=perform_action,
    )

    class _FrontApp:
        @staticmethod
        def processIdentifier():
            return 4242

    class _Workspace:
        @staticmethod
        def frontmostApplication():
            return _FrontApp()

    appkit = _module(
        "AppKit",
        NSWorkspace=types.SimpleNamespace(sharedWorkspace=lambda: _Workspace()),
    )
    monkeypatch.setitem(sys.modules, "ApplicationServices", application_services)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    return log


def _sample_tree() -> tuple[FakeAXElement, FakeAXElement]:
    button = FakeAXElement({
        "AXRole": "AXButton",
        "AXTitle": "OK",
        "AXDescription": "confirm dialog",
    })
    window = FakeAXElement({
        "AXRole": "AXWindow",
        "AXTitle": "Dialog",
        "AXChildren": [button],
    })
    root = FakeAXElement({"AXRole": "AXApplication", "AXChildren": [window]})
    return root, button


class TestMacOSAXClick:
    async def test_ax_press_clicks_matching_element(self, monkeypatch):
        root, button = _sample_tree()
        log = _install_fake_ax(monkeypatch, root)
        backend = MacOSBackend()
        assert backend.capabilities()["ax_actions"] is True

        result = await backend.click_element("OK", control_type="button")
        assert result.get("success") is True
        assert result["method"] == "ax_press"
        assert result["name"] == "OK"
        assert result["control_type"] == "AXButton"
        assert result["pid"] == 4242
        assert log["created_for_pid"] == [4242]
        assert log["pressed"] == [(button, "AXPress")]

    async def test_no_match_falls_back_to_coordinate_path(self, monkeypatch):
        root, _button = _sample_tree()
        log = _install_fake_ax(monkeypatch, root)
        backend = MacOSBackend()
        result = await backend.click_element("Cancel")
        # No AXPress fired; the coordinate fallback (find_element via
        # osascript) fails headless, surfacing an error dict — not a crash.
        assert log["pressed"] == []
        assert "error" in result

    async def test_untrusted_process_gates_capability(self, monkeypatch):
        root, _button = _sample_tree()
        log = _install_fake_ax(monkeypatch, root, trusted=False)
        backend = MacOSBackend()
        assert backend.capabilities()["ax_actions"] is False
        result = await backend.click_element("OK")
        assert log["pressed"] == []  # AX path never attempted
        assert "error" in result

    async def test_perform_action_error_code_falls_back(self, monkeypatch):
        root, _button = _sample_tree()
        log = _install_fake_ax(monkeypatch, root, press_result=-25204)
        backend = MacOSBackend()
        result = await backend.click_element("OK")
        assert log["pressed"], "AXPress should have been attempted"
        assert "error" in result  # coordinate fallback also fails headless

    async def test_right_click_bypasses_ax_press(self, monkeypatch):
        root, _button = _sample_tree()
        log = _install_fake_ax(monkeypatch, root)
        backend = MacOSBackend()
        await backend.click_element("OK", button="right")
        assert log["pressed"] == []  # AXPress only maps to single left-click

    async def test_capability_without_pyobjc_is_false(self):
        backend = MacOSBackend()
        assert backend.capabilities()["ax_actions"] is False


# ---------------------------------------------------------------------------
# macOS: find_element AppleScript port (stubbed osascript)
# ---------------------------------------------------------------------------

class TestMacOSFindElement:
    async def test_parses_osascript_json(self, tmp_path, monkeypatch):
        _stub_exe(
            tmp_path, "osascript",
            'echo \'{"name":"OK","control_type":"AXButton",'
            '"rect":{"x":10,"y":20,"width":30,"height":40}}\'',
        )
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
        backend = MacOSBackend()
        result = await backend.find_element(name="OK")
        assert result == {
            "name": "OK",
            "control_type": "AXButton",
            "rect": {"x": 10, "y": 20, "width": 30, "height": 40},
        }

    async def test_nonzero_exit_mentions_accessibility_permission(self, tmp_path, monkeypatch):
        _stub_exe(tmp_path, "osascript", "exit 1")
        monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
        backend = MacOSBackend()
        result = await backend.find_element(name="OK")
        assert "Accessibility permission" in result.get("error", "")

    async def test_missing_osascript_returns_error(self):
        backend = MacOSBackend()
        result = await backend.find_element(name="OK")
        assert "error" in result


# ---------------------------------------------------------------------------
# Linux/X11: xdotool fallback when wmctrl is absent
# ---------------------------------------------------------------------------

_XDOTOOL_STUB = r"""
case "$1" in
  search) echo 123 ;;
  getwindowname) echo "Terminal" ;;
  getwindowpid) echo 42 ;;
  getwindowgeometry)
    echo "WINDOW=123"
    echo "X=15"
    echo "Y=25"
    echo "WIDTH=800"
    echo "HEIGHT=600"
    echo "SCREEN=0"
    ;;
esac
"""


class TestX11ListWindows:
    async def test_xdotool_fallback_schema(self, tmp_path, monkeypatch):
        # No wmctrl stub — only xdotool exists on the trimmed PATH.
        _stub_exe(tmp_path, "xdotool", _XDOTOOL_STUB)
        monkeypatch.setenv("PATH", str(tmp_path))
        backend = X11Backend()
        result = await backend.list_windows()
        assert result["method"] == "xdotool"
        assert result["count"] == 1
        win = result["windows"][0]
        assert win["id"] == hex(123)
        assert win["pid"] == 42
        assert win["title"] == "Terminal"
        assert (win["x"], win["y"], win["width"], win["height"]) == (15, 25, 800, 600)
        # Same schema keys as the wmctrl path.
        assert set(win) == {"id", "pid", "app", "x", "y", "width", "height", "title"}

    async def test_neither_tool_returns_install_hint(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))  # empty dir — no tools at all
        backend = X11Backend()
        result = await backend.list_windows()
        assert "wmctrl not found" in result.get("error", "")
