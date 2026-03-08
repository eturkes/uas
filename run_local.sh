#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Run UAS locally (no container) with the repo-local auth.
# Useful for visually testing the TUI dashboard.
#
# Usage:
#   bash run_local.sh "Create a file called hello.txt with Hello from UAS"
#   bash run_local.sh --dry-run "Fetch the current weather for Tokyo"
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTH_DIR="$SCRIPT_DIR/.uas_auth"

if [ ! -f "$AUTH_DIR/.credentials.json" ]; then
    echo "No credentials found. Run: bash setup_auth.sh" >&2
    exit 1
fi

export CLAUDE_CONFIG_DIR="$AUTH_DIR"
export UAS_SANDBOX_MODE=local
export PYTHONPATH="$SCRIPT_DIR"

WORKSPACE="$(mktemp -d)"
export UAS_WORKSPACE="$WORKSPACE"
echo "Workspace: $WORKSPACE"

python3 -m architect.main "$@"
