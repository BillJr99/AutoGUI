"""
install_runner.py — Pick the right install script for this OS and run it.

Tiny module so both the mainline `main.py` and the pi-extension can share
the same OS-detection logic when they decide to invoke the dependency
installer.  The install scripts themselves live under `scripts/` and are
the canonical source of truth — this module just spawns them.
"""

from __future__ import annotations

import logging
import os
import platform as _platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def script_for_platform(project_root: Path) -> tuple[list[str], Path] | None:
    """
    Return (cmd_argv, script_path) for the install script appropriate for
    this OS, or None if no script applies.
    """
    scripts_dir = project_root / "scripts"
    system = _platform.system()
    if system == "Windows":
        ps1 = scripts_dir / "install-dependencies.ps1"
        if ps1.exists():
            powershell = shutil.which("powershell") or "powershell"
            return ([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)], ps1)
        return None
    # Linux, macOS, WSL all share the bash script.
    sh = scripts_dir / "install-dependencies.sh"
    if sh.exists():
        bash = shutil.which("bash") or "bash"
        return ([bash, str(sh)], sh)
    return None


def run_installer(project_root: Path) -> int:
    """
    Run the appropriate install script.  Streams output live (loud by
    design — same convention as the scripts themselves).  Returns the
    exit code; 0 = success.
    """
    picked = script_for_platform(project_root)
    if picked is None:
        logger.warning("[install_runner] No install script found under scripts/.")
        return 1
    argv, path = picked
    print(f"[install_runner] Running {path}", flush=True)
    try:
        proc = subprocess.run(argv, cwd=str(project_root))
        return proc.returncode
    except Exception as e:
        logger.warning("[install_runner] Failed to run %s: %s", path, e)
        return 1
