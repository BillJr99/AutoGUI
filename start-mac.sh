#!/usr/bin/env bash
# start-mac.sh — Launch AutoGUI with OSScreenObserver on macOS.
#
# What it does:
#   1. Pulls all git submodules (OSScreenObserver, etc.)
#   2. Checks for required Python packages; prompts to install if missing.
#      Requires Homebrew (https://brew.sh) for system-level packages.
#   3. Starts every submodule service in the background (port-checks first).
#   4. Launches AutoGUI (python3 main.py), forwarding any extra arguments.
#   5. On exit, kills any submodule service this script started.
#
# macOS note: AutoGUI needs Accessibility permission to control the desktop.
#   System Settings → Privacy & Security → Accessibility → add your terminal.
#
# Usage:
#   bash start-mac.sh                    # interactive TUI
#   bash start-mac.sh "open a browser"  # single-command mode
#   PYTHON=/usr/local/bin/python3.11 bash start-mac.sh  # custom interpreter

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSO_DIR="$SCRIPT_DIR/OSScreenObserver"
PY="${PYTHON:-python3}"
OSO_PID=""
OSO_STARTED=false

# --- 1. Pull submodules ------------------------------------------------------
echo "[start-mac.sh] Updating submodules..."
git -C "$SCRIPT_DIR" submodule update --init --recursive

# --- 2. Homebrew check -------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
    echo "[start-mac.sh] Homebrew is not installed."
    echo "  Install it from https://brew.sh, then re-run this script."
    exit 1
fi

# --- 3. Dependency check / install prompt ------------------------------------
_deps_ok() {
    "$PY" -c "import textual, flask" 2>/dev/null
}

if ! _deps_ok; then
    printf "[start-mac.sh] Required packages appear missing. Install dependencies now? [Y/n] "
    read -r _ans || _ans=Y
    case "${_ans:-Y}" in
        [Nn]*)
            echo "[start-mac.sh] Skipping install. Some features may not work."
            ;;
        *)
            bash "$SCRIPT_DIR/scripts/install-dependencies.sh"
            if [ -f "$OSO_DIR/requirements.txt" ]; then
                echo "[start-mac.sh] Installing OSScreenObserver dependencies..."
                "$PY" -m pip install --quiet -r "$OSO_DIR/requirements.txt"
            fi
            ;;
    esac
fi

# --- 4. Accessibility permission reminder ------------------------------------
echo "[start-mac.sh] Reminder: your terminal needs Accessibility permission."
echo "  System Settings → Privacy & Security → Accessibility → add your terminal app."

# --- 5. Cleanup trap (only kills services this script started) ---------------
cleanup() {
    if [ "$OSO_STARTED" = true ] && [ -n "$OSO_PID" ]; then
        echo "[start-mac.sh] Stopping OSScreenObserver (PID $OSO_PID)..."
        kill "$OSO_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# --- 6. Start OSScreenObserver if not already running ------------------------
if curl -sf http://127.0.0.1:5001/api/healthz >/dev/null 2>&1; then
    echo "[start-mac.sh] OSScreenObserver is already running."
else
    echo "[start-mac.sh] Starting OSScreenObserver..."
    (cd "$OSO_DIR" && "$PY" main.py --mode inspect) &
    OSO_PID=$!
    OSO_STARTED=true
    for _i in $(seq 1 10); do
        curl -sf http://127.0.0.1:5001/api/healthz >/dev/null 2>&1 && break || true
        sleep 1
    done
    echo "[start-mac.sh] OSScreenObserver started (PID $OSO_PID)."
fi

# --- 7. Launch AutoGUI -------------------------------------------------------
cd "$SCRIPT_DIR"
"$PY" main.py "$@"
