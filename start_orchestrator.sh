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

# --- Build the sandbox image (pre-built on host for reliability) ---
echo "Building sandbox image..."
SANDBOX_DF=$(mktemp)
cat > "$SANDBOX_DF" << 'SBOX_EOF'
FROM docker.io/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
WORKDIR /uas
COPY orchestrator/ ./orchestrator/
VOLUME /workspace
WORKDIR /workspace
SBOX_EOF
"$ENGINE" build -t uas-sandbox -f "$SANDBOX_DF" "$SCRIPT_DIR"
rm -f "$SANDBOX_DF"

# Export sandbox image for the engine container's Podman
UAS_STORAGE="$HOME/.uas/containers"
mkdir -p "$UAS_STORAGE"
"$ENGINE" save uas-sandbox -o "$UAS_STORAGE/sandbox.tar"
echo "Sandbox image exported."

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

# Persistent Podman storage so sandbox images and committed project
# containers survive across uas sessions.
UAS_STORAGE="$HOME/.uas/containers"
mkdir -p "$UAS_STORAGE"

# --- Launch the Orchestrator with nested-container privileges ---
echo "Launching Orchestrator..."
TTY_ARGS=()
if [ -t 0 ] && [ -t 1 ]; then
    TTY_ARGS=("-it")
fi

exec "$ENGINE" run --rm \
    "${TTY_ARGS[@]+"${TTY_ARGS[@]}"}" \
    --privileged \
    -e IS_SANDBOX=1 \
    -e "UAS_HOST_UID=$(id -u)" \
    -e "UAS_HOST_GID=$(id -g)" \
    -e "UAS_HOST_WORKSPACE=$PWD" \
    -v "${AUTH_DIR}:/root/.claude:Z" \
    -v "${CLAUDE_JSON}:/root/.claude.json:Z" \
    -v "${UAS_STORAGE}:/var/lib/containers:Z" \
    -v "$PWD:/workspace:Z" \
    -w /workspace \
    "${ENV_ARGS[@]+"${ENV_ARGS[@]}"}" \
    "$IMAGE_NAME" \
    "$@"
