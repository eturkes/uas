#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="uas-engine"
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

# --- Build the engine image ---
echo "Building engine image '${IMAGE_NAME}'..."
"$ENGINE" build -t "$IMAGE_NAME" -f "${SCRIPT_DIR}/Containerfile" "$SCRIPT_DIR"

# --- Pass through relevant environment variables ---
ENV_ARGS=()
for var in UAS_GOAL UAS_TASK UAS_SANDBOX_IMAGE UAS_SANDBOX_TIMEOUT; do
    if [ -n "${!var:-}" ]; then
        ENV_ARGS+=("-e" "${var}=${!var}")
    fi
done

# --- Auth mount ---
AUTH_DIR="${PWD}/.uas_auth"
mkdir -p "$AUTH_DIR"

CLAUDE_JSON="$AUTH_DIR/claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

# --- Per-project container reuse ---
PROJECT_HASH=$(echo -n "$PWD" | sha256sum | cut -c1-12)
PROJECT_IMAGE="uas-project-${PROJECT_HASH}"
CONTAINER_NAME="uas-run-${PROJECT_HASH}"

BASE_IMAGE="${IMAGE_NAME}"
if "$ENGINE" image inspect "$PROJECT_IMAGE" &>/dev/null; then
    BASE_IMAGE="$PROJECT_IMAGE"
    echo "Reusing project image: $PROJECT_IMAGE"
fi

"$ENGINE" rm -f "$CONTAINER_NAME" 2>/dev/null || true

# --- Launch the engine ---
echo "Launching engine..."
TTY_ARGS=()
if [ -t 0 ] && [ -t 1 ]; then
    TTY_ARGS=("-it")
fi

"$ENGINE" run \
    "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}" \
    --privileged \
    --name "$CONTAINER_NAME" \
    -e IS_SANDBOX=1 \
    -e UAS_SANDBOX_MODE=local \
    -e "UAS_HOST_UID=$(id -u)" \
    -e "UAS_HOST_GID=$(id -g)" \
    -v "${AUTH_DIR}:/root/.claude:Z" \
    -v "${CLAUDE_JSON}:/root/.claude.json:Z" \
    -v "$PWD:/workspace:Z" \
    -w /workspace \
    "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}" \
    "$BASE_IMAGE" \
    "$@"
EXIT_CODE=$?

"$ENGINE" commit "$CONTAINER_NAME" "$PROJECT_IMAGE" 2>/dev/null && \
    echo "Project image saved: $PROJECT_IMAGE" || true
"$ENGINE" rm -f "$CONTAINER_NAME" 2>/dev/null || true

exit $EXIT_CODE
