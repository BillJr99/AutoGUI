#!/usr/bin/env bash
# Test container entrypoint.
# Brings up Xvfb + fluxbox + (optional) ollama, then hands off to
# tests/run_all.sh. Logs every service to /tmp/test-results/services.log
# so post-mortem is easy.
set -euo pipefail

LOG_DIR="/tmp/test-results"
mkdir -p "$LOG_DIR"
SERVICES_LOG="$LOG_DIR/services.log"
: > "$SERVICES_LOG"

echo "[entrypoint] $(date) starting services" | tee -a "$SERVICES_LOG"

# 1. Xvfb on :99
mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
Xvfb :99 -screen 0 1280x800x24 -ac >>"$SERVICES_LOG" 2>&1 &
XVFB_PID=$!
sleep 1
touch "$HOME/.Xauthority" && DISPLAY=:99 xauth generate :99 . trusted 2>>"$SERVICES_LOG" || true
echo "[entrypoint] Xvfb PID=$XVFB_PID display=:99" | tee -a "$SERVICES_LOG"

# 2. Window manager so wmctrl can list windows
DISPLAY=:99 fluxbox >>"$SERVICES_LOG" 2>&1 &
FLUXBOX_PID=$!
sleep 1
echo "[entrypoint] fluxbox PID=$FLUXBOX_PID" | tee -a "$SERVICES_LOG"

# 3. Optional dbus
dbus-launch >>"$SERVICES_LOG" 2>&1 || true

# 4. Optional Ollama serve
if [ "${AUTOGUI_LLM_SYSTEM:-}" = "ollama_bundled" ] && command -v ollama >/dev/null 2>&1; then
    ollama serve >>"$SERVICES_LOG" 2>&1 &
    OLLAMA_PID=$!
    # Wait up to 15 s for Ollama to start responding.
    for _ in $(seq 1 30); do
        if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    echo "[entrypoint] ollama PID=$OLLAMA_PID" | tee -a "$SERVICES_LOG"
fi

# 5. Probe display so failures surface here rather than mid-test.
if ! DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
    echo "[entrypoint] FATAL: Xvfb did not come up; check $SERVICES_LOG" >&2
    exit 1
fi

export DISPLAY=:99

# Hand off.
exec bash /app/tests/run_all.sh "$@"
