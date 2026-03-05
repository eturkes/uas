#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE="${SCRIPT_DIR}/e2e_workspace"
TIMEOUT=600  # 10 minutes

GOAL='Build a two-step data pipeline: 1. Fetch JSON from http://api.open-notify.org/astros.json and save to raw_astros.json. 2. Read raw_astros.json, extract the astronaut count, and write "There are currently X astronauts in space." to summary.txt.'

echo "============================================================"
echo "  UAS End-to-End Test"
echo "============================================================"

# --- Clean previous test artifacts ---
echo "Cleaning e2e_workspace..."
rm -rf "$WORKSPACE"
mkdir -p "$WORKSPACE"

# --- Copy host Claude CLI auth config ---
HOST_CLAUDE_DIR="${HOME}/.claude"
AUTH_DIR="${WORKSPACE}/.uas_auth"

if [ -d "$HOST_CLAUDE_DIR" ]; then
    echo "Copying Claude CLI config from ${HOST_CLAUDE_DIR} to ${AUTH_DIR}..."
    cp -r "$HOST_CLAUDE_DIR" "$AUTH_DIR"
else
    echo "WARNING: No Claude CLI config found at ${HOST_CLAUDE_DIR}." >&2
    echo "  The test will likely fail without authentication." >&2
fi

# --- Run the framework ---
echo ""
echo "Running UAS Architect Agent..."
echo "  Goal: ${GOAL}"
echo "  Workspace: ${WORKSPACE}"
echo "  Timeout: ${TIMEOUT}s"
echo "============================================================"
echo ""

export UAS_GOAL="$GOAL"
export UAS_WORKSPACE="$WORKSPACE"
export UAS_SANDBOX_MODE="local"

EXIT_CODE=0
cd "$SCRIPT_DIR"
timeout "${TIMEOUT}" python3 -m architect.main 2>&1 || EXIT_CODE=$?

echo ""
echo "Architect exited with code: ${EXIT_CODE}"

# --- Assert results ---
SUMMARY_FILE="${WORKSPACE}/summary.txt"

if [ ! -f "$SUMMARY_FILE" ]; then
    echo ""
    echo "E2E TEST FAILED: summary.txt was not created in ${WORKSPACE}"
    exit 1
fi

CONTENT="$(cat "$SUMMARY_FILE")"
echo "summary.txt content: ${CONTENT}"

if echo "$CONTENT" | grep -qP 'There are currently \d+ astronauts in space\.'; then
    echo ""
    echo "E2E TEST PASSED"
    exit 0
else
    echo ""
    echo "E2E TEST FAILED: summary.txt does not match expected format."
    echo "  Expected: 'There are currently X astronauts in space.'"
    echo "  Got: '${CONTENT}'"
    exit 1
fi
