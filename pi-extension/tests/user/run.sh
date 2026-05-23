#!/usr/bin/env bash
# Compile + run pi-extension user tests with Node's built-in test runner.
#
# Usage:
#   bash tests/user/run.sh
#
# Requires Node 22+ (for native fetch / node:test) and the dev deps
# installed via `npm install`.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$EXT_DIR"

if [ ! -d node_modules ]; then
    echo "[run.sh] Installing pi-extension dependencies..."
    npm install --silent
fi

DIST_DIR="$EXT_DIR/dist-tests"
rm -rf "$DIST_DIR"
echo "[run.sh] Compiling TypeScript..."
npx tsc -p tests/user/tsconfig.json

echo "[run.sh] Running tests..."
node --test "$DIST_DIR/tests/user/"*.test.js
