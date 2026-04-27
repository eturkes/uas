#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# UAS Auth Setup
#
# Launches an interactive Claude Code session inside the same container
# environment used for testing.  Authenticate, adjust settings, then
# type /exit.  Credentials are saved to .uas_auth/ and reused by all
# subsequent test and production runs.
#
# Re-run this script at any time to change configuration.
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="uas-engine:latest"

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

# --- Ensure image exists ---
if ! "$ENGINE" image inspect "$IMAGE_TAG" &>/dev/null; then
    echo "Container image '${IMAGE_TAG}' not found. Run install.sh first:" >&2
    echo "  bash ${SCRIPT_DIR}/install.sh" >&2
    exit 1
fi

AUTH_DIR="${SCRIPT_DIR}/.uas_auth"
mkdir -p "$AUTH_DIR"

# Claude Code stores user-level state in ~/.claude.json (separate from the
# ~/.claude/ config directory).  Seed an empty file so the bind mount works,
# then persist it across runs.
CLAUDE_JSON="$AUTH_DIR/claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

# Always overwrite settings.json with the framework's canonical defaults so
# every Claude invocation under UAS sees the same env, theme, and disabled
# UI knobs regardless of prior local edits.
cp -f "${SCRIPT_DIR}/framework_settings.json" "$AUTH_DIR/settings.json"

echo "============================================================"
echo "  UAS Auth Setup"
echo "============================================================"
echo ""
echo "  Launching Claude Code inside the uas-engine container."
echo "  Authenticate and configure settings, then type /exit."
echo ""
echo "  Credentials will be saved to: .uas_auth/"
echo "============================================================"
echo ""

"$ENGINE" run --rm -it \
    --privileged \
    -e IS_SANDBOX=1 \
    -v "$AUTH_DIR:/root/.claude:Z" \
    -v "$CLAUDE_JSON:/root/.claude.json:Z" \
    --entrypoint claude \
    "$IMAGE_TAG" \
    --dangerously-skip-permissions --effort max

# --- Fix ownership (container runs as root, so files are owned by root) ---
if command -v sudo &>/dev/null; then
    sudo chown -R "$(id -u):$(id -g)" "$AUTH_DIR"
else
    chown -R "$(id -u):$(id -g)" "$AUTH_DIR" 2>/dev/null || true
fi

# --- Verify credentials were written ---
if [ -f "$AUTH_DIR/.credentials.json" ] && [ -s "$AUTH_DIR/.credentials.json" ]; then
    echo ""
    echo "============================================================"
    echo "  Authentication saved to .uas_auth/"
    echo "  You can now run integration tests:"
    echo "    pytest -m integration"
    echo "============================================================"
else
    echo ""
    echo "WARNING: No credentials found in .uas_auth/"
    echo "  Run this script again to retry authentication."
    exit 1
fi
