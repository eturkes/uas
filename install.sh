#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="uas-engine"
IMAGE_TAG="${IMAGE_NAME}:latest"
INSTALL_DIR="${HOME}/.local/bin"

echo "============================================================"
echo "  UAS Framework Installer"
echo "============================================================"
echo ""

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
echo "Building '${IMAGE_TAG}'..."
"$ENGINE" build --no-cache -t "$IMAGE_TAG" -f "${SCRIPT_DIR}/Containerfile" "$SCRIPT_DIR"
echo "Image '${IMAGE_TAG}' built successfully."
echo ""

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
echo ""

# --- Ensure install directory exists ---
mkdir -p "$INSTALL_DIR"

# --- Generate the wrapper script ---
WRAPPER="${INSTALL_DIR}/uas"
cat > "$WRAPPER" << 'WRAPPER_EOF'
#!/usr/bin/env bash
set -euo pipefail

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

AUTH_DIR="$PWD/.uas_auth"
mkdir -p "$AUTH_DIR"

# Claude Code stores user-level state in ~/.claude.json (separate from
# ~/.claude/).  Seed an empty file so the bind mount works on first run.
CLAUDE_JSON="$AUTH_DIR/claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

# Persistent Podman storage so sandbox images and committed project
# containers survive across uas sessions.
UAS_STORAGE="$HOME/.uas/containers"
mkdir -p "$UAS_STORAGE"

exec "$ENGINE" run --rm -it \
    --privileged \
    -e IS_SANDBOX=1 \
    -e UAS_HOST_UID="$(id -u)" \
    -e UAS_HOST_GID="$(id -g)" \
    -e UAS_HOST_WORKSPACE="$PWD" \
    -v "$AUTH_DIR:/root/.claude:Z" \
    -v "$CLAUDE_JSON:/root/.claude.json:Z" \
    -v "$UAS_STORAGE:/var/lib/containers:Z" \
    -v "$PWD:/workspace:Z" \
    -w /workspace \
    uas-engine:latest \
    "$@"
WRAPPER_EOF

chmod +x "$WRAPPER"

echo "Wrapper installed to: ${WRAPPER}"
echo ""
echo "============================================================"
echo "  Installation complete!"
echo ""
echo "  Make sure ${INSTALL_DIR} is in your PATH, then run:"
echo "    cd <your-project-dir>"
echo "    uas [goal]"
echo "============================================================"
