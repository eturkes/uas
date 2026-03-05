# Universal Agentic Specification (UAS)

A two-layer autonomous system that takes abstract human goals and drives them to completion. The **Architect Agent** decomposes goals into atomic steps, generates UAS-compliant specs, and feeds them to the **Execution Orchestrator**, which generates and runs code in a secure Podman-in-Podman sandbox.

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

The Architect handles multi-step goals by capturing stdout from
each step and injecting it as context into dependent steps. If a
step fails after all Orchestrator retries, the Architect rewrites
the spec up to 2 times before halting with `ARCHITECT_BLOCKER.md`.

See [architect_design.md](architect_design.md) and [orchestrator_design.md](orchestrator_design.md) for full details.

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.
