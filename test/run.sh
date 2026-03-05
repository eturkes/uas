#!/usr/bin/env bash
set -euo pipefail

# Simple smoke test for UAS.
# Run from the repo root: bash test/run.sh
#
# What it does:
#   Asks UAS to create a single file (hello.txt) with known content.
#   After the run, checks that the file exists and contains the expected text.
#
# First run: you will need to authenticate interactively.
# Subsequent runs: .uas_auth/ persists credentials automatically.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GOAL='Create a file called hello.txt containing exactly the text: Hello from UAS'

echo "============================================================"
echo "  UAS Smoke Test"
echo "============================================================"
echo ""
echo "  Goal: ${GOAL}"
echo "  Working directory: ${SCRIPT_DIR}"
echo ""

# Run UAS from inside the test/ directory so .uas_auth/ lands here
cd "$SCRIPT_DIR"

# Re-install wrapper to pick up any script changes
bash "$REPO_ROOT/install.sh"

# Launch UAS
uas "$GOAL"
EXIT_CODE=$?

echo ""
echo "UAS exited with code: ${EXIT_CODE}"

# --- Verify auth persistence ---
if [ -d "$SCRIPT_DIR/.uas_auth" ]; then
    echo "PASS: .uas_auth/ directory was created."
else
    echo "FAIL: .uas_auth/ directory was NOT created."
fi

# --- Verify output ---
TARGET="$SCRIPT_DIR/hello.txt"
if [ ! -f "$TARGET" ]; then
    echo "FAIL: hello.txt was not created."
    exit 1
fi

CONTENT="$(cat "$TARGET")"
if echo "$CONTENT" | grep -q "Hello from UAS"; then
    echo "PASS: hello.txt contains expected content."
    echo "  Content: ${CONTENT}"
else
    echo "FAIL: hello.txt does not contain expected content."
    echo "  Expected: 'Hello from UAS'"
    echo "  Got: '${CONTENT}'"
    exit 1
fi

echo ""
echo "ALL CHECKS PASSED"
