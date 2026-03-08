#!/usr/bin/env bash
set -euo pipefail

# Simple quick test for UAS.
# Run from anywhere: bash integration/quick_test.sh
#
# What it does:
#   Asks UAS to create a single file (hello.txt) with known content
#   inside the uas-engine container using the repo-level .uas_auth/
#   credentials.  Rebuilds the image automatically if source files
#   have changed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AUTH_DIR="$REPO_ROOT/.uas_auth"
IMAGE_TAG="uas-engine:latest"
GOAL='Create a file called hello.txt containing exactly the text: Hello from UAS'

# --- Check auth ---
if [ ! -f "$AUTH_DIR/.credentials.json" ]; then
    echo "No credentials found. Run: bash setup_auth.sh" >&2
    exit 1
fi

# --- Discover container engine ---
ENGINE=""
for cmd in podman docker; do
    if command -v "$cmd" &>/dev/null; then
        ENGINE="$cmd"
        break
    fi
done

if [ -z "$ENGINE" ]; then
    echo "ERROR: No container engine found (checked podman, docker)." >&2
    exit 1
fi

# --- Rebuild image if stale or missing ---
_needs_rebuild() {
    if ! "$ENGINE" image exists "$IMAGE_TAG" 2>/dev/null; then
        return 0
    fi
    local image_epoch
    image_epoch=$("$ENGINE" image inspect "$IMAGE_TAG" \
        --format '{{.Created}}' | xargs -I{} date -d {} +%s 2>/dev/null) || return 0
    local f
    for f in "$REPO_ROOT/Containerfile" "$REPO_ROOT/requirements.txt" \
             "$REPO_ROOT/entrypoint.sh" "$REPO_ROOT"/architect/*.py \
             "$REPO_ROOT"/orchestrator/*.py; do
        [ "$(stat -c %Y "$f" 2>/dev/null || echo 0)" -gt "$image_epoch" ] && return 0
    done
    return 1
}

if _needs_rebuild; then
    echo "Rebuilding ${IMAGE_TAG}..."
    "$ENGINE" build -t "$IMAGE_TAG" \
        -f "$REPO_ROOT/Containerfile" "$REPO_ROOT"
fi

# --- Seed claude.json if missing ---
CLAUDE_JSON="$AUTH_DIR/claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

WORKSPACE="$(mktemp -d)"

echo "============================================================"
echo "  UAS Quick Test"
echo "============================================================"
echo ""
echo "  Goal: ${GOAL}"
echo "  Workspace: ${WORKSPACE}"
echo ""

# Launch UAS in the container
"$ENGINE" run --rm -it \
    --privileged \
    -e IS_SANDBOX=1 \
    -e "UAS_HOST_UID=$(id -u)" \
    -e "UAS_HOST_GID=$(id -g)" \
    -v "$AUTH_DIR:/root/.claude:Z" \
    -v "$CLAUDE_JSON:/root/.claude.json:Z" \
    -v "$WORKSPACE:/workspace:Z" \
    -w /workspace \
    "$IMAGE_TAG" \
    "$GOAL"
EXIT_CODE=$?

echo ""
echo "UAS exited with code: ${EXIT_CODE}"

# --- Verify output ---
TARGET="$WORKSPACE/hello.txt"
if [ ! -f "$TARGET" ]; then
    echo "FAIL: hello.txt was not created."
    exit 1
fi

CONTENT="$(cat "$TARGET")"
if echo "$CONTENT" | grep -q "Hello from UAS"; then
    echo "PASS: hello.txt contains expected content."
    echo "  Content: ${CONTENT}"
else
    echo "FAIL: hello.txt does not contain expected content."
    echo "  Expected: 'Hello from UAS'"
    echo "  Got: '${CONTENT}'"
    exit 1
fi

echo ""
echo "ALL CHECKS PASSED"
