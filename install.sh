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

# --- Build the container image ---
echo "Building '${IMAGE_TAG}'..."
"$ENGINE" build --no-cache -t "$IMAGE_TAG" -f "${SCRIPT_DIR}/Containerfile" "$SCRIPT_DIR"
echo "Image '${IMAGE_TAG}' built successfully."
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

exec "$ENGINE" run --rm -it \
    --privileged \
    -e IS_SANDBOX=1 \
    -e UAS_HOST_UID="$(id -u)" \
    -e UAS_HOST_GID="$(id -g)" \
    -v "$AUTH_DIR:/root/.claude:Z" \
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
