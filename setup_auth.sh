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
if ! "$ENGINE" image exists "$IMAGE_TAG" 2>/dev/null; then
    echo "Container image '${IMAGE_TAG}' not found. Run install.sh first:" >&2
    echo "  bash ${SCRIPT_DIR}/install.sh" >&2
    exit 1
fi

AUTH_DIR="${SCRIPT_DIR}/.uas_auth"
mkdir -p "$AUTH_DIR"

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
    --entrypoint claude \
    "$IMAGE_TAG"

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
