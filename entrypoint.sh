#!/usr/bin/env bash
set -euo pipefail

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

exec python3 -m architect.main "$@"
