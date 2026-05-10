#!/usr/bin/env bash
# AutoGUI dependency installer — Linux, macOS, WSL.
#
# Idempotent: every dependency is checked first and skipped when already
# installed.  Loud by design: every install command is echoed before
# running so you can audit exactly what's about to happen.
#
# Run manually:
#     bash scripts/install-dependencies.sh
#
# Run automatically: set "install_dependencies": true in config.json
# (mainline) or pi-extension/config.json — AutoGUI invokes this script
# once at startup before initialising the agent.
#
# Sections:
#   1. Detect OS / display server / package manager
#   2. System binaries (per-platform)
#   3. Python deps (mainline)
#   4. Playwright + Chromium (mainline + pi-extension)
#   5. Pi-extension Node deps
#
# Exit codes:
#   0  all targeted deps are present (or were installed successfully)
#   1  hard failure (no package manager, sudo refused, network error)
#
# This script never installs anything not on the explicit dep list, never
# changes shell rc files, and never modifies anything outside the system
# package cache + the project's pip / npm / node_modules state.

set -u
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# --- 1. Detect ---------------------------------------------------------------

case "$(uname -s)" in
  Linux*)  OS=linux ;;
  Darwin*) OS=macos ;;
  *) echo "[install] Unsupported OS: $(uname -s). Use install-dependencies.ps1 on Windows."; exit 1 ;;
esac

IS_WSL=0
if [ "$OS" = "linux" ] && grep -qi microsoft /proc/version 2>/dev/null; then
  IS_WSL=1
fi

DISPLAY_SERVER=
if [ "$OS" = "linux" ] && [ "$IS_WSL" -eq 0 ]; then
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then DISPLAY_SERVER=wayland
  elif [ -n "${DISPLAY:-}" ]; then DISPLAY_SERVER=x11
  else DISPLAY_SERVER=headless
  fi
fi

if [ "$OS" = "linux" ]; then
  if   command -v apt-get >/dev/null 2>&1; then PKG=apt
  elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
  elif command -v pacman  >/dev/null 2>&1; then PKG=pacman
  elif command -v zypper  >/dev/null 2>&1; then PKG=zypper
  else
    echo "[install] No supported Linux package manager (apt-get/dnf/pacman/zypper)."
    exit 1
  fi
elif [ "$OS" = "macos" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "[install] Homebrew is not installed. Install it first from https://brew.sh, then re-run."
    exit 1
  fi
  PKG=brew
fi

echo "[install] os=$OS pkg=$PKG wsl=$IS_WSL display=${DISPLAY_SERVER:-n/a}"

# --- Helpers -----------------------------------------------------------------

run() {
  echo "[install] \$ $*"
  "$@"
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

have_py_module() {
  # $2 is the python interpreter to test against (default python3).
  local mod="$1"
  local py="${2:-python3}"
  "$py" -c "import $mod" >/dev/null 2>&1
}

pkg_install() {
  case "$PKG" in
    apt)    run sudo apt-get install -y "$@" ;;
    dnf)    run sudo dnf install -y "$@" ;;
    pacman) run sudo pacman -S --noconfirm "$@" ;;
    zypper) run sudo zypper install -y "$@" ;;
    brew)   run brew install "$@" ;;
  esac
}

# Install one binary if missing.  $1 = command to look for; remaining args are
# the package names to pass to the package manager.
ensure_cmd() {
  local cmd="$1"; shift
  if have_cmd "$cmd"; then
    echo "[skip ] $cmd already on PATH"
    return 0
  fi
  echo "[need ] $cmd missing"
  pkg_install "$@"
}

ensure_py_module() {
  local mod="$1"; shift
  local py="${PY:-python3}"
  if have_py_module "$mod" "$py"; then
    echo "[skip ] python module $mod already importable"
    return 0
  fi
  echo "[need ] python module $mod missing"
  run "$py" -m pip install --quiet "$@"
}

# --- 2. System binaries ------------------------------------------------------

if [ "$OS" = "linux" ] && [ "$IS_WSL" -eq 0 ]; then
  # apt has different package names than the executable for some.
  if [ "$DISPLAY_SERVER" = "x11" ]; then
    case "$PKG" in
      apt)
        ensure_cmd wmctrl wmctrl
        ensure_cmd xdotool xdotool
        ensure_cmd xclip xclip
        # Headers needed when pip installs pyautogui's Linux deps.
        run sudo apt-get install -y python3-tk python3-dev || true
        ;;
      dnf)
        ensure_cmd wmctrl wmctrl
        ensure_cmd xdotool xdotool
        ensure_cmd xclip xclip
        run sudo dnf install -y python3-tkinter python3-devel || true
        ;;
      pacman)
        ensure_cmd wmctrl wmctrl
        ensure_cmd xdotool xdotool
        ensure_cmd xclip xclip
        run sudo pacman -S --noconfirm tk || true
        ;;
      zypper)
        ensure_cmd wmctrl wmctrl
        ensure_cmd xdotool xdotool
        ensure_cmd xclip xclip
        ;;
    esac
  elif [ "$DISPLAY_SERVER" = "wayland" ]; then
    case "$PKG" in
      apt)
        ensure_cmd grim grim
        ensure_cmd ydotool ydotool
        ensure_cmd wl-paste wl-clipboard
        echo "[note ] If you use Sway, also: sudo apt install sway && sudo systemctl --user enable --now ydotool || sudo ydotoold &"
        ;;
      dnf)
        ensure_cmd grim grim
        ensure_cmd ydotool ydotool
        ensure_cmd wl-paste wl-clipboard
        ;;
      pacman)
        ensure_cmd grim grim
        ensure_cmd ydotool ydotool
        ensure_cmd wl-paste wl-clipboard
        ;;
      zypper)
        ensure_cmd grim grim
        ensure_cmd ydotool ydotool
        ensure_cmd wl-paste wl-clipboard
        ;;
    esac
  fi

  # Linux a11y for desktop_click_element (graceful — skip if Python not present).
  if have_cmd python3; then
    case "$PKG" in
      apt)
        run sudo apt-get install -y python3-pyatspi gir1.2-atspi-2.0 || true
        ;;
      dnf)
        run sudo dnf install -y python3-pyatspi || true
        ;;
      pacman)
        echo "[note ] AT-SPI bindings on Arch: sudo pacman -S python-pyatspi (varies by repo)"
        ;;
    esac
  fi

  # OCR for desktop_click_text.
  ensure_cmd tesseract tesseract-ocr 2>/dev/null \
    || pkg_install tesseract  # dnf/pacman/zypper use plain "tesseract"
  # ImageMagick — Set-of-Mark overlay + failure GIF assembly.
  ensure_cmd convert imagemagick
fi

if [ "$OS" = "macos" ]; then
  ensure_cmd tesseract tesseract
  ensure_cmd convert imagemagick
  echo "[note ] macOS automation needs the terminal running Pi/AutoGUI to have"
  echo "        Accessibility permission: System Settings → Privacy & Security"
  echo "        → Accessibility — add your terminal app."
fi

if [ "$IS_WSL" -eq 1 ]; then
  echo "[note ] On WSL the Windows desktop is driven via powershell.exe interop;"
  echo "        Linux GUI tools (xdotool, wmctrl, grim, ydotool) are NOT needed."
  echo "        Install Tesseract on the WSL Linux side for OCR:"
  ensure_cmd tesseract tesseract-ocr
  echo "[note ] For ImageMagick (Set-of-Mark overlay):"
  ensure_cmd convert imagemagick
fi

# --- 3. Python deps (mainline) ----------------------------------------------

if [ -f requirements.txt ]; then
  PY="${PYTHON:-python3}"
  if ! have_cmd "$PY"; then
    echo "[install] $PY not on PATH; install Python 3 first."
  else
    if ! "$PY" -m pip --version >/dev/null 2>&1; then
      echo "[install] pip not bound to $PY; trying ensurepip..."
      run "$PY" -m ensurepip --upgrade || true
    fi
    echo "[install] pip install -r requirements.txt"
    run "$PY" -m pip install --quiet -r requirements.txt

    # Optional Python deps used by the click ladder, OCR, browser, a11y.
    ensure_py_module pyperclip pyperclip
    if have_cmd tesseract; then
      ensure_py_module pytesseract pytesseract
    fi
    ensure_py_module playwright playwright

    # Platform-specific optional packages.
    if [ "$OS" = "macos" ]; then
      ensure_py_module Quartz pyobjc-framework-Quartz pyobjc-framework-AppKit
    fi
    if [ "$IS_WSL" -eq 1 ]; then
      # uiautomation/pywin32 only make sense for Python running on the Windows
      # side, not for WSL Linux.  Skip them here.
      :
    fi

    # Playwright Chromium.
    if "$PY" -c "import playwright" >/dev/null 2>&1; then
      run "$PY" -m playwright install chromium
    fi
  fi
else
  echo "[skip ] no requirements.txt found at $PROJECT_ROOT — mainline Python deps not installed"
fi

# --- 4. Pi-extension Node deps ----------------------------------------------

if [ -d pi-extension ] && [ -f pi-extension/package.json ]; then
  if have_cmd npm; then
    # Always run npm install — it is idempotent and picks up any newly added
    # optional deps (e.g. playwright) even when node_modules already exists.
    echo "[install] cd pi-extension && npm install"
    ( cd pi-extension && run npm install --silent )
    # Browser bindings for the pi-extension's Playwright.
    # playwright is now in optionalDependencies so it lands in node_modules
    # after npm install; npx uses the local copy without fetching it again.
    ( cd pi-extension && run npx playwright install chromium ) || true
  else
    echo "[note ] npm not on PATH; skipping pi-extension node deps"
  fi
fi

echo "[install] done."
