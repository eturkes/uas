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

# --- Ensure install directory exists ---
mkdir -p "$INSTALL_DIR"

# --- Install the framework's canonical Claude settings alongside the
#     wrapper so every run can re-seed .uas_auth/settings.json. ---
FRAMEWORK_SETTINGS_DEST="${INSTALL_DIR}/uas-framework-settings.json"
cp -f "${SCRIPT_DIR}/framework_settings.json" "$FRAMEWORK_SETTINGS_DEST"

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

# Refresh settings.json from the framework canonical (installed by install.sh
# alongside this wrapper) so every run sees the same Claude defaults.
FRAMEWORK_SETTINGS="$(dirname "$(readlink -f "$0")")/uas-framework-settings.json"
if [ -f "$FRAMEWORK_SETTINGS" ]; then
    cp -f "$FRAMEWORK_SETTINGS" "$AUTH_DIR/settings.json"
fi

# --- Per-project container reuse ---
# Deterministic name from the project directory so packages installed
# during a previous run are already present on the next run.
PROJECT_HASH=$(echo -n "$PWD" | sha256sum | cut -c1-12)
PROJECT_IMAGE="uas-project-${PROJECT_HASH}"
CONTAINER_NAME="uas-run-${PROJECT_HASH}"

# Use committed project image if available, otherwise base engine image.
BASE_IMAGE="uas-engine:latest"
if "$ENGINE" image inspect "$PROJECT_IMAGE" &>/dev/null; then
    BASE_IMAGE="$PROJECT_IMAGE"
fi

# Remove leftover container from a previous interrupted run.
"$ENGINE" rm -f "$CONTAINER_NAME" 2>/dev/null || true

# Run without --rm so we can commit the container afterward.
"$ENGINE" run -it \
    --privileged \
    --name "$CONTAINER_NAME" \
    -e IS_SANDBOX=1 \
    -e UAS_SANDBOX_MODE=local \
    -e UAS_HOST_UID="$(id -u)" \
    -e UAS_HOST_GID="$(id -g)" \
    -v "$AUTH_DIR:/root/.claude:Z" \
    -v "$CLAUDE_JSON:/root/.claude.json:Z" \
    -v "$PWD:/workspace:Z" \
    -w /workspace \
    "$BASE_IMAGE" \
    "$@"
EXIT_CODE=$?

# Commit the container as a project-specific image so installed
# packages (pip, apt-get) are preserved for the next run.
"$ENGINE" commit "$CONTAINER_NAME" "$PROJECT_IMAGE" 2>/dev/null && \
    echo "Project image saved: $PROJECT_IMAGE" || true

# Clean up the container.
"$ENGINE" rm -f "$CONTAINER_NAME" 2>/dev/null || true

exit $EXIT_CODE
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
