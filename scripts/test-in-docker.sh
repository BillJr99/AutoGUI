#!/usr/bin/env bash
# scripts/test-in-docker.sh — interactive host-side entry for the full
# AutoGUI + OSScreenObserver + pi-extension test suite.
#
# What it does:
#   1. Walks the user through an interactive LLM picker (system, base URL,
#      API key, chat model, VLM model).  Choices are persisted to
#      test-results/llm-config.json and re-used on subsequent runs.
#   2. Builds tests/user/Dockerfile.tests as image autogui-tests.
#   3. Runs the image with the picker values exported as env vars.
#   4. Always tears down the container (and optionally the image) on exit
#      via a trap, including on Ctrl-C / build failure / pytest non-zero.
#
# Flags:
#   --phase {regression,user,integration,all}
#       Run only one tier.  Default: all.
#   --no-llm           Force AUTOGUI_LLM_SYSTEM=stub (skip real-LLM tests).
#   --reconfigure      Re-prompt the LLM picker even if a config exists.
#   --non-interactive  Require env vars; do not prompt.
#   --list-models      After picking system, list available models via the
#                      endpoint's /api/tags or /v1/models.
#   --keep-container   Don't auto-remove the container on exit; drop into
#                      a shell with `docker exec` on failure.
#   --remove-image     Also remove the image on exit.
#   --build-only       Build the image and exit before running tests.
#   -h, --help         Print this help.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

IMAGE_NAME="autogui-tests"
CONTAINER_NAME="autogui-tests-run-$$"
RESULTS_HOST_DIR="$REPO_ROOT/test-results"
LLM_CONFIG_FILE="$RESULTS_HOST_DIR/llm-config.json"

PHASE="all"
NO_LLM=0
RECONFIGURE=0
NON_INTERACTIVE=0
LIST_MODELS=0
KEEP_CONTAINER=0
REMOVE_IMAGE=0
BUILD_ONLY=0

usage() { sed -n '1,30p' "$0"; exit 0; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --phase)           PHASE="$2"; shift 2 ;;
        --no-llm)          NO_LLM=1; shift ;;
        --reconfigure)     RECONFIGURE=1; shift ;;
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --list-models)     LIST_MODELS=1; shift ;;
        --keep-container)  KEEP_CONTAINER=1; shift ;;
        --remove-image)    REMOVE_IMAGE=1; shift ;;
        --build-only)      BUILD_ONLY=1; shift ;;
        -h|--help)         usage ;;
        *) echo "Unknown arg: $1" >&2; usage ;;
    esac
done

mkdir -p "$RESULTS_HOST_DIR"

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup trap — runs on every exit (success, failure, Ctrl-C).
# ─────────────────────────────────────────────────────────────────────────────
cleanup() {
    local rc=$?
    set +e
    if [ "$KEEP_CONTAINER" != "1" ]; then
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 && \
            echo "[test-in-docker] container removed: $CONTAINER_NAME"
    else
        echo "[test-in-docker] --keep-container set; not removing $CONTAINER_NAME"
    fi
    if [ "$REMOVE_IMAGE" = "1" ]; then
        docker rmi "$IMAGE_NAME" >/dev/null 2>&1 && \
            echo "[test-in-docker] image removed: $IMAGE_NAME"
    fi
    echo "[test-in-docker] results: $RESULTS_HOST_DIR"
    exit $rc
}
trap cleanup EXIT INT TERM

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
ask() {
    # ask "prompt" "default"
    local p="$1" d="${2:-}"
    local r
    if [ "$NON_INTERACTIVE" = "1" ]; then
        echo "$d"; return
    fi
    if [ -n "$d" ]; then
        read -r -p "$p [$d] " r || true
    else
        read -r -p "$p " r || true
    fi
    echo "${r:-$d}"
}

ask_secret() {
    local p="$1"
    local r
    if [ "$NON_INTERACTIVE" = "1" ]; then echo ""; return; fi
    read -r -s -p "$p " r || true
    echo
    echo "$r"
}

ask_choice() {
    # ask_choice "header" "default_num" "label1" "label2" ...
    local hdr="$1" def="$2"; shift 2
    local i=1
    if [ "$NON_INTERACTIVE" != "1" ]; then
        echo "$hdr"
        for label in "$@"; do
            echo "  [$i] $label"
            i=$((i+1))
        done
    fi
    local r
    r="$(ask 'Choose number:' "$def")"
    case "$r" in
        ''|*[!0-9]*) echo "$def" ;;
        *) echo "$r" ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────
# LLM picker
# ─────────────────────────────────────────────────────────────────────────────
pick_llm() {
    # If a config exists and --reconfigure wasn't passed, just source it.
    if [ "$RECONFIGURE" != "1" ] && [ -f "$LLM_CONFIG_FILE" ]; then
        # shellcheck disable=SC1090
        echo "[test-in-docker] Reusing existing LLM config: $LLM_CONFIG_FILE"
        # Parse the JSON minimally with python (it's already required for tests).
        AUTOGUI_LLM_SYSTEM="$(python3 -c "import json,sys; print(json.load(open('$LLM_CONFIG_FILE')).get('system',''))")"
        AUTOGUI_LLM_BASE_URL="$(python3 -c "import json,sys; print(json.load(open('$LLM_CONFIG_FILE')).get('base_url',''))")"
        AUTOGUI_LLM_API_KEY="$(python3 -c "import json,sys; print(json.load(open('$LLM_CONFIG_FILE')).get('api_key',''))")"
        AUTOGUI_LLM_MODEL="$(python3 -c "import json,sys; print(json.load(open('$LLM_CONFIG_FILE')).get('model',''))")"
        AUTOGUI_VLM_MODEL="$(python3 -c "import json,sys; print(json.load(open('$LLM_CONFIG_FILE')).get('vlm_model',''))")"
        return 0
    fi

    if [ "$NO_LLM" = "1" ]; then
        AUTOGUI_LLM_SYSTEM="stub"
        AUTOGUI_LLM_BASE_URL=""
        AUTOGUI_LLM_API_KEY=""
        AUTOGUI_LLM_MODEL="stub"
        AUTOGUI_VLM_MODEL=""
    else
        echo
        echo "═══════════════════════════════════════════════════════════════════"
        echo "  AutoGUI test harness — LLM picker"
        echo "═══════════════════════════════════════════════════════════════════"
        local sys_choice
        sys_choice="$(ask_choice "Which LLM system should drive the real-LLM tests?" "1" \
            "Bundled Ollama (default, runs inside the container)" \
            "OpenWebUI" \
            "OpenAI-compatible endpoint" \
            "Anthropic API" \
            "Stub only (no real LLM; skip slow_llm tests)")"
        case "$sys_choice" in
            1) AUTOGUI_LLM_SYSTEM="ollama_bundled"
               AUTOGUI_LLM_BASE_URL="http://127.0.0.1:11434"
               AUTOGUI_LLM_API_KEY=""
               local def_chat="qwen2.5:0.5b" def_vlm="qwen2.5vl:3b"
               ;;
            2) AUTOGUI_LLM_SYSTEM="openwebui"
               AUTOGUI_LLM_BASE_URL="$(ask 'OpenWebUI base URL:' 'http://localhost:3000')"
               AUTOGUI_LLM_API_KEY="$(ask_secret 'OpenWebUI API key:')"
               local def_chat="llama3.1:70b" def_vlm=""
               ;;
            3) AUTOGUI_LLM_SYSTEM="openai_compat"
               AUTOGUI_LLM_BASE_URL="$(ask 'Endpoint base URL:' 'https://api.openai.com')"
               AUTOGUI_LLM_API_KEY="$(ask_secret 'API key:')"
               local def_chat="gpt-4o-mini" def_vlm=""
               ;;
            4) AUTOGUI_LLM_SYSTEM="anthropic"
               AUTOGUI_LLM_BASE_URL="$(ask 'Anthropic base URL:' 'https://api.anthropic.com')"
               AUTOGUI_LLM_API_KEY="$(ask_secret 'Anthropic API key:')"
               local def_chat="claude-opus-4-7" def_vlm=""
               ;;
            *) AUTOGUI_LLM_SYSTEM="stub"
               AUTOGUI_LLM_BASE_URL=""
               AUTOGUI_LLM_API_KEY=""
               local def_chat="stub" def_vlm=""
               ;;
        esac
        if [ "$LIST_MODELS" = "1" ] && [ "$AUTOGUI_LLM_SYSTEM" != "stub" ]; then
            local tags
            if [ "$AUTOGUI_LLM_SYSTEM" = "ollama_bundled" ]; then
                tags="$(curl -s "$AUTOGUI_LLM_BASE_URL/api/tags" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin) if sys.stdin.isatty() is False else {}; print('\n'.join(m['name'] for m in d.get('models', [])))")"
            else
                tags="$(curl -s -H "Authorization: Bearer $AUTOGUI_LLM_API_KEY" "$AUTOGUI_LLM_BASE_URL/v1/models" 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('\n'.join(m.get('id','?') for m in d.get('data', [])))" 2>/dev/null)"
            fi
            if [ -n "$tags" ]; then
                echo "Available models on the endpoint:"
                echo "$tags" | sed 's/^/    /'
            fi
        fi
        AUTOGUI_LLM_MODEL="$(ask 'Chat model:' "$def_chat")"
        AUTOGUI_VLM_MODEL="$(ask 'VLM model (Enter to skip vision tier):' "$def_vlm")"
    fi

    cat > "$LLM_CONFIG_FILE" <<EOF
{
  "system": "$AUTOGUI_LLM_SYSTEM",
  "base_url": "$AUTOGUI_LLM_BASE_URL",
  "api_key": "$AUTOGUI_LLM_API_KEY",
  "model": "$AUTOGUI_LLM_MODEL",
  "vlm_model": "$AUTOGUI_VLM_MODEL"
}
EOF
    chmod 600 "$LLM_CONFIG_FILE"
    echo "[test-in-docker] LLM config saved to $LLM_CONFIG_FILE"
}

# ─────────────────────────────────────────────────────────────────────────────
# Build + run
# ─────────────────────────────────────────────────────────────────────────────
pick_llm

INCLUDE_OLLAMA="false"
if [ "$AUTOGUI_LLM_SYSTEM" = "ollama_bundled" ]; then
    INCLUDE_OLLAMA="true"
fi

echo "[test-in-docker] Building image $IMAGE_NAME (INCLUDE_OLLAMA=$INCLUDE_OLLAMA)..."
docker build \
    -f "$REPO_ROOT/tests/user/Dockerfile.tests" \
    --build-arg INCLUDE_OLLAMA="$INCLUDE_OLLAMA" \
    --build-arg OLLAMA_CHAT_MODEL="${AUTOGUI_LLM_MODEL:-qwen2.5:0.5b}" \
    --build-arg OLLAMA_VLM_MODEL="${AUTOGUI_VLM_MODEL:-qwen2.5vl:3b}" \
    -t "$IMAGE_NAME" \
    "$REPO_ROOT"

if [ "$BUILD_ONLY" = "1" ]; then
    echo "[test-in-docker] --build-only set; exiting before run."
    exit 0
fi

echo "[test-in-docker] Running tests (phase=$PHASE, container=$CONTAINER_NAME)..."
docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    -v "$RESULTS_HOST_DIR:/tmp/test-results" \
    -e AUTOGUI_LLM_SYSTEM="$AUTOGUI_LLM_SYSTEM" \
    -e AUTOGUI_LLM_BASE_URL="$AUTOGUI_LLM_BASE_URL" \
    -e AUTOGUI_LLM_API_KEY="$AUTOGUI_LLM_API_KEY" \
    -e AUTOGUI_LLM_MODEL="$AUTOGUI_LLM_MODEL" \
    -e AUTOGUI_VLM_MODEL="$AUTOGUI_VLM_MODEL" \
    -e PHASE="$PHASE" \
    "$IMAGE_NAME"
RC=$?

echo "[test-in-docker] Tests exited with rc=$RC"
exit "$RC"
