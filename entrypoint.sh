#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# File ownership fix: ensure workspace files are owned by the launching user
# =============================================================================
if [ -n "${UAS_HOST_UID:-}" ] && [ "${UAS_HOST_UID}" != "0" ]; then
    _fix_ownership() {
        chown -R "${UAS_HOST_UID}:${UAS_HOST_GID:-$UAS_HOST_UID}" /workspace 2>/dev/null || true
    }
    trap _fix_ownership EXIT
fi

# =============================================================================
# Subcommands (e.g. `prune`)
# =============================================================================
if [ "${1:-}" = "prune" ]; then
    shift
    cd /uas
    python3 -P -m architect.state prune "$@"
    exit $?
fi

# =============================================================================
# Non-interactive mode: skip Stage 1 when called programmatically
# =============================================================================
if [ -n "${UAS_TASK:-}" ] || [ -n "${UAS_GOAL:-}" ] || [ -n "${UAS_GOAL_FILE:-}" ]; then
    echo "Non-interactive mode detected (UAS_TASK, UAS_GOAL, or UAS_GOAL_FILE set)."
    echo "Skipping interactive setup."
    cd /uas
    python3 -P -m architect.main "$@"
    exit $?
fi

# =============================================================================
# Stage 1: Interactive Claude Code Setup (skipped if auth is pre-mounted)
# =============================================================================

AUTH_VALID=false

# Check if credentials exist in the mounted config directory
if [ -f /root/.claude/.credentials.json ]; then
    # Verify the credentials file is non-empty and contains a token
    if [ -s /root/.claude/.credentials.json ] && grep -q "Token" /root/.claude/.credentials.json 2>/dev/null; then
        echo "============================================================"
        echo "  Valid authentication found in mounted config."
        echo "  Skipping interactive setup."
        echo "============================================================"
        AUTH_VALID=true
    fi
fi

if [ "$AUTH_VALID" = false ]; then
    echo "============================================================"
    echo "  UAS Orchestrator - Interactive Setup"
    echo "============================================================"
    echo ""
    echo "  You are in the interactive setup phase."
    echo "  Please authenticate with Claude Code and configure any"
    echo "  initial settings (e.g., select your model, log in)."
    echo ""
    echo "  When you are finished, type /exit to hand control to the"
    echo "  Architect Agent, which will run autonomously."
    echo ""
    echo "============================================================"
    echo ""

    claude
fi

# =============================================================================
# Stage 2: Run the Architect Agent Framework
# =============================================================================
echo ""
echo "============================================================"
echo "  Interactive setup complete. Starting Architect Agent..."
echo "============================================================"
echo ""

cd /uas
python3 -P -m architect.main "$@"
