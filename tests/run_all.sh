#!/usr/bin/env bash
# tests/run_all.sh — orchestrate every test phase in order.
#
# Phases (each emits a JUnit XML to /tmp/test-results/<phase>.xml):
#   1. autogui-regression  : pytest tests/ -m "not user" --ignore=tests/user
#   2. oso-regression      : pytest OSScreenObserver/tests/ -m "not user" --ignore=OSScreenObserver/tests/user
#   3. oso-user            : pytest OSScreenObserver/tests/user/ -m "user"
#   4. autogui-user        : pytest tests/user/ -m "user and not integration"
#   5. autogui-integration : pytest tests/user/integration_oso/ -m "integration"
#   6. pi-extension-user   : node-based tests in pi-extension/tests/user/
#
# Each phase is fail-fast by default. Override with PHASE_FAILFAST=0 to
# keep going (collect all results) or PHASE=<name> to run a single phase.

set -uo pipefail

APP_ROOT="${APP_ROOT:-/app}"
RESULTS_DIR="${RESULTS_DIR:-/tmp/test-results}"
mkdir -p "$RESULTS_DIR"
PHASE_FAILFAST="${PHASE_FAILFAST:-1}"
PHASE_FILTER="${PHASE:-all}"

cd "$APP_ROOT"

GLOBAL_RC=0

run_phase() {
    local name="$1"; shift
    if [ "$PHASE_FILTER" != "all" ] && [ "$PHASE_FILTER" != "$name" ]; then
        return 0
    fi
    echo
    echo "═══════════════════════════════════════════════════════════════════════"
    echo "▶ phase: $name"
    echo "  cmd:   $*"
    echo "═══════════════════════════════════════════════════════════════════════"
    local rc
    set +e
    "$@"
    rc=$?
    set -e
    if [ "$rc" -ne 0 ]; then
        echo "✗ phase $name FAILED with rc=$rc" >&2
        GLOBAL_RC=$rc
        if [ "$PHASE_FAILFAST" = "1" ]; then
            return "$rc"
        fi
    else
        echo "✓ phase $name passed"
    fi
    return 0
}

# Phase 1 — existing AutoGUI regression (must stay green).
run_phase autogui-regression \
    python -m pytest "$APP_ROOT/tests/" \
        --ignore="$APP_ROOT/tests/user" \
        -m "not user" -q \
        --junitxml="$RESULTS_DIR/autogui-regression.xml" \
    || true

# Phase 2 — existing OSO regression.
if [ -d "$APP_ROOT/OSScreenObserver/tests" ]; then
    run_phase oso-regression \
        bash -c "cd $APP_ROOT/OSScreenObserver && python -m pytest tests/ \
            --ignore=tests/user -m 'not user' -q \
            --junitxml=$RESULTS_DIR/oso-regression.xml" \
        || true
fi

# Phase 3 — OSO user tier.
if [ -d "$APP_ROOT/OSScreenObserver/tests/user" ]; then
    run_phase oso-user \
        bash -c "cd $APP_ROOT/OSScreenObserver && python -m pytest tests/user/ -m user -q \
            --junitxml=$RESULTS_DIR/oso-user.xml" \
        || true
fi

# Phase 4 — AutoGUI user tier (non-integration).
run_phase autogui-user \
    python -m pytest "$APP_ROOT/tests/user/" \
        --ignore="$APP_ROOT/tests/user/integration_oso" \
        -m "user" -q \
        --junitxml="$RESULTS_DIR/autogui-user.xml" \
    || true

# Phase 5 — Integration tier.
if [ -d "$APP_ROOT/tests/user/integration_oso" ]; then
    run_phase autogui-integration \
        python -m pytest "$APP_ROOT/tests/user/integration_oso/" \
            -m "integration" -q \
            --junitxml="$RESULTS_DIR/autogui-integration.xml" \
        || true
fi

# Phase 6 — pi-extension user tests.
if [ -d "$APP_ROOT/pi-extension/tests/user" ]; then
    run_phase pi-extension-user \
        bash -c "cd $APP_ROOT/pi-extension && bash tests/user/run.sh" \
        || true
fi

echo
echo "═══════════════════════════════════════════════════════════════════════"
if [ "$GLOBAL_RC" -eq 0 ]; then
    echo "ALL PHASES PASSED"
else
    echo "SOME PHASES FAILED (rc=$GLOBAL_RC)"
fi
echo "Results: $RESULTS_DIR/"
ls -la "$RESULTS_DIR" 2>/dev/null || true
exit "$GLOBAL_RC"
