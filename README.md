# Universal Agentic Specification (UAS)

A two-layer autonomous system that takes abstract human goals and drives them to completion. The **Architect Agent** decomposes goals into atomic steps, generates UAS-compliant specs, and feeds them to the **Execution Orchestrator**, which generates and runs code in a sandboxed environment.

Supports two execution modes:
- **Container mode** (default): Podman-in-Podman sandbox with isolated networking and resource limits.
- **Local mode** (`UAS_SANDBOX_MODE=local`): Direct subprocess execution for development, testing, and environments without nested container support.

## Quick Start

```bash
# Install (builds the container image and creates the `uas` CLI):
./install.sh

# Run from any project directory:
cd ~/my-project
uas "your goal here"

# On first run you will enter an interactive Claude Code session.
# Authenticate, configure settings, then type /exit to hand off
# to the Architect Agent.
```

The installer places a `uas` wrapper in `~/.local/bin`.
Ensure that directory is in your `PATH`.

### Non-Interactive / Local Mode

```bash
# Run without containers (uses local Python + Claude Code CLI):
UAS_SANDBOX_MODE=local UAS_GOAL="your goal" python3 -m architect.main

# Or run the E2E test:
python3 e2e_test.py
```

When `UAS_GOAL` or `UAS_TASK` is set, the entrypoint skips the
interactive Claude Code setup and proceeds directly to execution.

## Requirements

- [Podman](https://podman.io/) or [Docker](https://www.docker.com/)

## Project Structure

```
.
├── install.sh                # Builds image and installs `uas` CLI
├── start_orchestrator.sh     # Alternative: build and launch manually
├── entrypoint.sh             # Two-stage entrypoint (setup then run)
├── architect/                # Architect Agent (installed to /uas)
│   ├── main.py               # Controller loop
│   ├── planner.py            # LLM task decomposition + rewrite
│   ├── spec_generator.py     # UAS markdown spec writer
│   ├── executor.py           # Orchestrator subprocess interface
│   └── state.py              # JSON state persistence
├── orchestrator/             # Execution Orchestrator (containerized)
│   ├── main.py               # Build-Run-Evaluate loop
│   ├── llm_client.py         # Claude Code CLI subprocess wrapper
│   ├── sandbox.py            # Nested Podman sandbox execution
│   └── parser.py             # Code extraction from LLM responses
├── Containerfile             # Image (Podman + Python + Claude Code CLI)
├── e2e_test.py               # End-to-end test (local mode)
├── bug_fix_report.md         # Detailed bug analysis and fixes
├── architect_design.md       # Architect architecture documentation
├── orchestrator_design.md    # Orchestrator architecture documentation
└── stack_decisions.md        # Stack rationale
```

## Architecture

```
User (any directory)
 └─ uas "goal"                     # ~/.local/bin/uas wrapper
     └─ uas-engine:latest           # $PWD mounted to /workspace
         ├─ Stage 1: Interactive Claude Code session (auth/setup)
         └─ Stage 2: Architect Agent (code in /uas, output in /workspace)
              ├─ Planner        -> Claude Code decomposes goal
              ├─ Spec Generator  -> writes UAS markdown specs
              ├─ State Manager   -> tracks plan_state.json
              └─ Executor        -> invokes Orchestrator loop
                   └─ Orchestrator
                       ├─ LLM Client -> Claude Code CLI wrapper
                       └─ Sandbox    -> nested Podman container
```

All LLM calls go through the Claude Code CLI (`claude -p`)
installed inside the container. No API keys or host-mounted
auth files are required — authentication happens interactively
in Stage 1.

Steps share a persistent `/workspace` directory. The Architect
captures stdout from each step and injects it as context into
dependent steps. If a step fails after all Orchestrator retries,
the Architect rewrites the spec up to 2 times before halting with
`ARCHITECT_BLOCKER.md`.

See [architect_design.md](architect_design.md) and [orchestrator_design.md](orchestrator_design.md) for full details.

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.
