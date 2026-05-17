#!/usr/bin/env bash
# start.sh — Launch AutoGUI with OSScreenObserver on Linux / WSL.
#
# What it does:
#   1. Pulls all git submodules (OSScreenObserver, etc.)
#   2. Checks for required Python packages; prompts to install if missing.
#   3. Starts every submodule service in the background (port-checks first).
#   4. Launches AutoGUI (python main.py), forwarding any extra arguments.
#   5. On exit, kills any submodule service this script started.
#
# Usage:
#   bash start.sh                    # interactive TUI
#   bash start.sh "open a browser"  # single-command mode
#   PYTHON=python3.11 bash start.sh  # custom interpreter

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSO_DIR="$SCRIPT_DIR/OSScreenObserver"
PY="${PYTHON:-python3}"
OSO_PID=""
OSO_STARTED=false

# --- 1. Pull submodules ------------------------------------------------------
echo "[start.sh] Updating submodules..."
git -C "$SCRIPT_DIR" submodule update --init --recursive

# --- 2. Dependency check / install prompt ------------------------------------
_deps_ok() {
    "$PY" -c "import textual, flask" 2>/dev/null
}

if ! _deps_ok; then
    printf "[start.sh] Required packages appear missing. Install dependencies now? [Y/n] "
    read -r _ans || _ans=Y
    case "${_ans:-Y}" in
        [Nn]*)
            echo "[start.sh] Skipping install. Some features may not work."
            ;;
        *)
            bash "$SCRIPT_DIR/scripts/install-dependencies.sh"
            if [ -f "$OSO_DIR/requirements.txt" ]; then
                echo "[start.sh] Installing OSScreenObserver dependencies..."
                "$PY" -m pip install --quiet -r "$OSO_DIR/requirements.txt"
            fi
            ;;
    esac
fi

# --- 3. Cleanup trap (only kills services this script started) ---------------
cleanup() {
    if [ "$OSO_STARTED" = true ] && [ -n "$OSO_PID" ]; then
        echo "[start.sh] Stopping OSScreenObserver (PID $OSO_PID)..."
        kill "$OSO_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# --- 4. Start OSScreenObserver if not already running ------------------------
if curl -sf http://127.0.0.1:5001/api/healthz >/dev/null 2>&1; then
    echo "[start.sh] OSScreenObserver is already running."
else
    echo "[start.sh] Starting OSScreenObserver..."
    (cd "$OSO_DIR" && "$PY" main.py --mode inspect) &
    OSO_PID=$!
    OSO_STARTED=true
    for _i in $(seq 1 10); do
        curl -sf http://127.0.0.1:5001/api/healthz >/dev/null 2>&1 && break || true
        sleep 1
    done
    echo "[start.sh] OSScreenObserver started (PID $OSO_PID)."
fi

# --- 5. Launch AutoGUI -------------------------------------------------------
cd "$SCRIPT_DIR"
"$PY" main.py "$@"
