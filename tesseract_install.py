"""
tesseract_install.py — Optional one-shot installer for the OCR stack.

Used when desktop_click_text / desktop_find_text need OCR but the runtime
environment does not yet have tesseract + pytesseract.  The installer is
gated by the config flag `tools.auto_install_tesseract: true`; otherwise
the OCR path simply returns a "please install" message.

Platform coverage
-----------------
  Linux (apt/dnf/pacman/zypper)  — installs tesseract-ocr via the
                                   detected package manager (sudo).
  macOS (Homebrew)               — `brew install tesseract`.
  Windows (winget)               — UB-Mannheim build via winget.
  WSL                            — uses the Linux side's apt by default
                                   (avoids needing the Windows binary
                                   to be on PATH inside the WSL shell).

Always runs `pip install pytesseract` afterwards so the Python wrapper
matches the system binary.

Designed to be loud: every command + result is logged at INFO so the
user can see exactly what was installed and where.  The install is
attempted at most once per process — repeated invocations short-circuit
once a binary is on PATH.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_INSTALL_ATTEMPTED = False


def _is_wsl() -> bool:
    if platform.system() != "Linux":
        return False
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def _have_pytesseract() -> bool:
    try:
        import pytesseract  # noqa: F401
        return True
    except ImportError:
        return False


def status() -> dict:
    """Return a snapshot of OCR availability without changing anything."""
    binary = shutil.which("tesseract")
    return {
        "tesseract_binary": binary,
        "pytesseract_installed": _have_pytesseract(),
        "ready": bool(binary and _have_pytesseract()),
    }


def ensure(auto_install: bool = False) -> dict:
    """
    Make OCR available.  Returns a status dict; callers should treat
    `ready=False` as a hard miss.

    When auto_install is False and the stack is missing, no install is
    attempted — the result just describes what's missing.
    """
    global _INSTALL_ATTEMPTED

    snap = status()
    if snap["ready"]:
        return snap

    if not auto_install:
        snap["message"] = (
            "OCR stack incomplete; set tools.auto_install_tesseract=true "
            "in config.json to install automatically, or install manually:\n"
            "  Linux:   sudo apt install tesseract-ocr  (or dnf/pacman/zypper)\n"
            "  macOS:   brew install tesseract\n"
            "  Windows: winget install UB-Mannheim.TesseractOCR\n"
            "  Then:    pip install pytesseract"
        )
        return snap

    if _INSTALL_ATTEMPTED:
        snap["message"] = "Auto-install already attempted earlier this session."
        return snap
    _INSTALL_ATTEMPTED = True

    print("[tesseract_install] auto_install_tesseract=true and OCR stack is "
          "incomplete — attempting install now.", flush=True)

    binary_ok = bool(snap["tesseract_binary"]) or _install_binary()
    pip_ok = snap["pytesseract_installed"] or _install_pytesseract()

    final = status()
    final["message"] = (
        "OCR stack ready." if final["ready"]
        else (
            f"Auto-install incomplete (binary_ok={binary_ok}, "
            f"pytesseract_ok={pip_ok}). See log output above for details."
        )
    )
    return final


def _run(cmd: list[str]) -> bool:
    print(f"[tesseract_install] $ {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        print(f"[tesseract_install] command not found: {e}", flush=True)
        return False
    except subprocess.TimeoutExpired:
        print("[tesseract_install] install timed out after 10 minutes.", flush=True)
        return False
    if result.returncode != 0:
        print(f"[tesseract_install] failed (rc={result.returncode}):\n"
              f"  stdout: {result.stdout[-400:]}\n"
              f"  stderr: {result.stderr[-400:]}", flush=True)
        return False
    print("[tesseract_install] ok.", flush=True)
    return True


def _install_binary() -> bool:
    """Pick the best installer for this OS and run it."""
    system = platform.system()

    if system == "Linux" or _is_wsl():
        if shutil.which("apt-get"):
            return _run(["sudo", "apt-get", "update"]) and _run(
                ["sudo", "apt-get", "install", "-y", "tesseract-ocr"]
            )
        if shutil.which("dnf"):
            return _run(["sudo", "dnf", "install", "-y", "tesseract"])
        if shutil.which("pacman"):
            return _run(["sudo", "pacman", "-S", "--noconfirm", "tesseract"])
        if shutil.which("zypper"):
            return _run(["sudo", "zypper", "install", "-y", "tesseract-ocr"])
        print("[tesseract_install] No supported Linux package manager found "
              "(tried apt-get/dnf/pacman/zypper).", flush=True)
        return False

    if system == "Darwin":
        if shutil.which("brew"):
            return _run(["brew", "install", "tesseract"])
        print("[tesseract_install] Homebrew not found. Install Homebrew first "
              "(https://brew.sh) or install tesseract manually.", flush=True)
        return False

    if system == "Windows":
        if shutil.which("winget"):
            return _run([
                "winget", "install", "--id=UB-Mannheim.TesseractOCR",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ])
        print("[tesseract_install] winget not found. Install tesseract from "
              "https://github.com/UB-Mannheim/tesseract/wiki and add it to PATH.",
              flush=True)
        return False

    print(f"[tesseract_install] Auto-install is not supported on {system}.",
          flush=True)
    return False


def _install_pytesseract() -> bool:
    return _run([sys.executable, "-m", "pip", "install", "--quiet", "pytesseract"])
