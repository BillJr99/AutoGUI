#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OSO_DIR="$SCRIPT_DIR/OSScreenObserver"
OSO_PID=""
OSO_STARTED=false

# Pull submodule if not yet populated
git -C "$SCRIPT_DIR" submodule update --init --recursive

cleanup() {
    if [ "$OSO_STARTED" = true ] && [ -n "$OSO_PID" ]; then
        echo "[start.sh] Stopping OSScreenObserver (PID $OSO_PID)..."
        kill "$OSO_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Check whether OSScreenObserver is already running on its default port
if curl -sf http://127.0.0.1:5001/api/healthz >/dev/null 2>&1; then
    echo "[start.sh] OSScreenObserver is already running."
else
    echo "[start.sh] Starting OSScreenObserver..."
    cd "$OSO_DIR"
    python main.py &
    OSO_PID=$!
    OSO_STARTED=true
    cd "$SCRIPT_DIR"

    # Wait up to 10 s for the server to become reachable
    for i in $(seq 1 10); do
        if curl -sf http://127.0.0.1:5001/api/healthz >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    echo "[start.sh] OSScreenObserver started (PID $OSO_PID)."
fi

# Start AutoGUI (pass any extra arguments through)
cd "$SCRIPT_DIR"
python main.py "$@"
