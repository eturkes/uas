#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Non-interactive mode: skip Stage 1 when called programmatically
# =============================================================================
if [ -n "${UAS_TASK:-}" ] || [ -n "${UAS_GOAL:-}" ]; then
    echo "Non-interactive mode detected (UAS_TASK or UAS_GOAL set)."
    echo "Skipping interactive setup."
    cd /uas
    exec python3 -m architect.main "$@"
fi

# =============================================================================
# Stage 1: Interactive Claude Code Setup
# =============================================================================
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

# =============================================================================
# Stage 2: Run the Architect Agent Framework
# =============================================================================
echo ""
echo "============================================================"
echo "  Interactive setup complete. Starting Architect Agent..."
echo "============================================================"
echo ""

cd /uas
exec python3 -m architect.main "$@"
