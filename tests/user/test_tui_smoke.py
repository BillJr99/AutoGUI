"""
TUI smoke test — run `python main.py --check` as a subprocess to confirm
the entry point loads, parses args, and exits cleanly without needing
a live LLM or display.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.user]

ROOT = Path(__file__).resolve().parents[2]


def test_main_help_runs():
    r = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    out = (r.stdout + r.stderr).lower()
    assert "usage" in out or "openwebui" in out or "agent" in out


def test_main_check_exits():
    """--check should attempt a health probe and exit (non-zero is fine
    when there's no OpenWebUI server reachable). We just want to confirm
    the CLI doesn't crash on the flag itself."""
    r = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--check"],
        capture_output=True, text=True, timeout=15,
        env={**__import__("os").environ,
              "AUTOGUI_CONFIG": "__no_config__.json"},
    )
    # The flag exists; exit code reflects the probe result. Crash would
    # produce a non-zero exit AND a Python traceback in stderr.
    assert "Traceback" not in r.stderr, r.stderr[:500]
