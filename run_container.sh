#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Run UAS in the uas-engine container with a real TTY.
# Useful for visually testing the TUI dashboard in the container environment.
#
# Usage:
#   bash run_container.sh "Create a file called hello.txt with Hello from UAS"
#   bash run_container.sh --dry-run "Fetch the current weather for Tokyo"
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

if ! "$ENGINE" image exists "$IMAGE_TAG" 2>/dev/null; then
    echo "Container image '${IMAGE_TAG}' not found. Run install.sh first:" >&2
    echo "  bash ${SCRIPT_DIR}/install.sh" >&2
    exit 1
fi

AUTH_DIR="$SCRIPT_DIR/.uas_auth"
if [ ! -f "$AUTH_DIR/.credentials.json" ]; then
    echo "No credentials found. Run: bash setup_auth.sh" >&2
    exit 1
fi

CLAUDE_JSON="$AUTH_DIR/claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

# Refresh settings.json from the framework canonical so every run sees
# the same Claude defaults (env vars, disabled UI, etc.).
cp -f "${SCRIPT_DIR}/framework_settings.json" "$AUTH_DIR/settings.json"

WORKSPACE="$(mktemp -d)"
echo "Workspace: $WORKSPACE"

"$ENGINE" run --rm -it \
    --privileged \
    -e IS_SANDBOX=1 \
    -e UAS_HOST_UID="$(id -u)" \
    -e UAS_HOST_GID="$(id -g)" \
    -v "$AUTH_DIR:/root/.claude:Z" \
    -v "$CLAUDE_JSON:/root/.claude.json:Z" \
    -v "$WORKSPACE:/workspace:Z" \
    -w /workspace \
    "$IMAGE_TAG" \
    "$@"
