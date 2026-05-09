"""
platform_detect.py — Runtime OS and display environment detection.

Returns a PlatformInfo dict consumed by backends.get_backend() to select the
correct desktop automation backend.  Checks (in order):

  1. WSL  — Linux with a Microsoft kernel string
  2. Windows native
  3. macOS
  4. Linux/Wayland — WAYLAND_DISPLAY is set
  5. Linux/X11    — DISPLAY is set
  6. Headless Linux — no display at all
"""

import os
import platform
from pathlib import Path
from typing import TypedDict


class PlatformInfo(TypedDict):
    system: str        # platform.system() → "Linux", "Windows", "Darwin"
    is_wsl: bool
    is_wayland: bool
    is_x11: bool
    has_display: bool  # True when any display mechanism is available
    release: str       # platform.release(), lowercased


def _proc_version_contains_microsoft() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def detect() -> PlatformInfo:
    system = platform.system()
    release = platform.release().lower()

    # Primary check: kernel release string (fast, works in most WSL setups).
    # Fallback: read /proc/version directly — some WSL2 distros expose
    # "microsoft" there even when uname -r is configured to a custom string.
    is_wsl = system == "Linux" and (
        "microsoft" in release
        or "wsl" in release
        or _proc_version_contains_microsoft()
    )

    wayland = os.environ.get("WAYLAND_DISPLAY", "")
    xdisplay = os.environ.get("DISPLAY", "")

    # Wayland and X11 only apply to native Linux, not WSL (WSL uses Windows display)
    is_wayland = bool(wayland) and not is_wsl
    is_x11 = bool(xdisplay) and not is_wayland and not is_wsl

    has_display = (
        is_wayland
        or is_x11
        or is_wsl
        or system in ("Windows", "Darwin")
    )

    return {
        "system": system,
        "is_wsl": is_wsl,
        "is_wayland": is_wayland,
        "is_x11": is_x11,
        "has_display": has_display,
        "release": release,
    }


def summarize(info: PlatformInfo) -> str:
    system = info["system"]
    if info["is_wsl"]:
        return f"WSL (kernel: {platform.release()})"
    if info["is_wayland"]:
        return f"Linux/Wayland (kernel: {platform.release()})"
    if info["is_x11"]:
        return f"Linux/X11 (kernel: {platform.release()})"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0] or platform.release()}"
    if system == "Windows":
        return f"Windows {platform.version()}"
    return f"{system} {platform.release()} [headless — no display]"
