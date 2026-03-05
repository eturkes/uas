#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="uas-orchestrator"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

echo "Using container engine: ${ENGINE}"

# --- Build the Orchestrator image ---
echo "Building Orchestrator image '${IMAGE_NAME}'..."
"$ENGINE" build -t "$IMAGE_NAME" -f "${SCRIPT_DIR}/Containerfile" "$SCRIPT_DIR"

# --- Pass through relevant environment variables ---
ENV_ARGS=()
for var in UAS_GOAL UAS_TASK UAS_SANDBOX_IMAGE UAS_SANDBOX_TIMEOUT; do
    if [ -n "${!var:-}" ]; then
        ENV_ARGS+=("-e" "${var}=${!var}")
    fi
done

# --- Launch the Orchestrator with interactive TTY and nested-container privileges ---
echo "Launching Orchestrator..."
exec "$ENGINE" run --rm -it \
    --privileged \
    "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}" \
    "$IMAGE_NAME" \
    "$@"
