#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="uas-sandbox"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/output"

# --- Discover container engine ---
ENGINE=""
for cmd in podman docker nerdctl; do
    if command -v "$cmd" &>/dev/null; then
        ENGINE="$cmd"
        break
    fi
done

if [ -z "$ENGINE" ]; then
    echo "ERROR: No container engine found (checked podman, docker, nerdctl)." >&2
    exit 1
fi

echo "Using container engine: ${ENGINE}"

# --- Ensure output directory exists ---
mkdir -p "$OUTPUT_DIR"

# --- Build the container image ---
echo "Building image '${IMAGE_NAME}'..."
"$ENGINE" build -t "$IMAGE_NAME" "$SCRIPT_DIR"

# --- Run the container ---
echo "Running validation..."
"$ENGINE" run --rm \
    -v "${OUTPUT_DIR}:/output:Z" \
    "$IMAGE_NAME"

# --- Verify artifacts ---
if [ -f "${OUTPUT_DIR}/screenshot.png" ] && [ -f "${OUTPUT_DIR}/dom_snapshot.html" ]; then
    echo "Validation PASSED: artifacts found in ${OUTPUT_DIR}"
else
    echo "Validation FAILED: expected artifacts missing from ${OUTPUT_DIR}" >&2
    exit 1
fi
