"""
playwright_install.py — Optional one-shot installer for Playwright + Chromium.

Triggered by tools.auto_install_playwright when the browser tools are enabled
but the Python package or its bundled browser binary is missing.

Always loud: every command + return code prints to stdout so the user sees
what is being installed.  Attempts at most once per process.
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

_INSTALL_ATTEMPTED = False


def _have_playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _have_chromium() -> bool:
    """Best-effort check that the Chromium binary Playwright fetched exists."""
    if not _have_playwright():
        return False
    try:
        from playwright._impl._driver import compute_driver_executable  # type: ignore
        # We don't actually run the driver — its presence is enough to say
        # `playwright install` has been done at least once.
        path = compute_driver_executable()
        from pathlib import Path
        return bool(path and Path(path).exists())
    except Exception:
        return False


def status() -> dict:
    return {
        "playwright_installed": _have_playwright(),
        "chromium_present": _have_chromium(),
        "ready": _have_playwright() and _have_chromium(),
    }


def ensure(auto_install: bool = False) -> dict:
    global _INSTALL_ATTEMPTED
    snap = status()
    if snap["ready"]:
        return snap
    if not auto_install:
        snap["message"] = (
            "Playwright stack incomplete; set tools.auto_install_playwright=true "
            "in config.json to install automatically, or run:\n"
            "  pip install playwright && playwright install chromium"
        )
        return snap
    if _INSTALL_ATTEMPTED:
        snap["message"] = "Auto-install already attempted earlier this session."
        return snap
    _INSTALL_ATTEMPTED = True

    print("[playwright_install] auto_install_playwright=true and the stack is "
          "incomplete — installing now.", flush=True)

    if not snap["playwright_installed"]:
        if not _run([sys.executable, "-m", "pip", "install", "--quiet", "playwright"]):
            return {**status(), "message": "pip install playwright failed."}

    if not _run([sys.executable, "-m", "playwright", "install", "chromium"]):
        return {**status(), "message": "`playwright install chromium` failed."}

    final = status()
    final["message"] = (
        "Playwright + Chromium ready." if final["ready"]
        else "Auto-install completed but `ready` is still False — see logs."
    )
    return final


def _run(cmd: list[str]) -> bool:
    print(f"[playwright_install] $ {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        print(f"[playwright_install] command not found: {e}", flush=True)
        return False
    except subprocess.TimeoutExpired:
        print("[playwright_install] timed out after 10 minutes.", flush=True)
        return False
    if result.returncode != 0:
        print(f"[playwright_install] failed (rc={result.returncode}):\n"
              f"  stdout: {result.stdout[-400:]}\n"
              f"  stderr: {result.stderr[-400:]}", flush=True)
        return False
    print("[playwright_install] ok.", flush=True)
    return True
