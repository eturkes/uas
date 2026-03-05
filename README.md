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
#
# Credentials are saved to .uas_auth/ in your project directory.
# On subsequent runs, authentication is skipped automatically.
```

The installer places a `uas` wrapper in `~/.local/bin`.
Ensure that directory is in your `PATH`.

### Project-Level Authentication

On first run, `uas` launches an interactive Claude Code session for
authentication. Credentials are persisted to `$PWD/.uas_auth/`, which
is bind-mounted into the container as `/root/.claude`. On subsequent
runs, the entrypoint detects valid credentials and skips interactive
setup entirely.

To pre-seed auth for CI or automated use, copy your host Claude config:

```bash
cp -r ~/.claude .uas_auth
```

### Non-Interactive / Local Mode

```bash
# Run without containers (uses local Python + Claude Code CLI):
UAS_SANDBOX_MODE=local UAS_GOAL="your goal" python3 -m architect.main

# Or run the E2E tests:
python3 e2e_test.py          # Simple two-step test
./run_e2e_test.sh            # Full pipeline test (fetches live data)
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
│   ├── executor.py           # Builds uas-sandbox image, runs Orchestrator
│   └── state.py              # JSON state persistence
├── orchestrator/             # Execution Orchestrator (containerized)
│   ├── main.py               # Build-Run-Evaluate loop
│   ├── llm_client.py         # Claude Code CLI subprocess wrapper
│   ├── sandbox.py            # Nested Podman sandbox execution
│   └── parser.py             # Code extraction from LLM responses
├── Containerfile             # Image (Podman + Python + Claude Code CLI)
├── e2e_test.py               # End-to-end test (local mode, simple)
├── run_e2e_test.sh           # End-to-end test (full pipeline, live data)
├── final_architecture_report.md  # Auth and test harness report
├── bug_fix_report.md         # Detailed bug analysis and fixes
├── architect_design.md       # Architect architecture documentation
├── orchestrator_design.md    # Orchestrator architecture documentation
└── stack_decisions.md        # Stack rationale
```

## Architecture

```
User (any directory)
 └─ uas "goal"                     # ~/.local/bin/uas wrapper
     └─ uas-engine:latest           # $PWD -> /workspace, .uas_auth -> /root/.claude
         ├─ Stage 1: Auth check (skip if .uas_auth has valid creds)
         └─ Stage 2: Architect Agent (code in /uas, output in /workspace)
              ├─ Planner        -> Claude Code decomposes goal
              ├─ Spec Generator  -> writes UAS markdown specs
              ├─ State Manager   -> tracks plan_state.json
              └─ Executor        -> invokes Orchestrator loop
                   └─ uas-sandbox (python:3.12-slim)
                       └─ Orchestrator
                           ├─ LLM Client -> Claude Code CLI wrapper
                           └─ Sandbox    -> local subprocess (containerized)
```

All LLM calls go through the Claude Code CLI (`claude -p`)
installed inside the container. Authentication is persisted to
`$PWD/.uas_auth/` via bind mount, so interactive login is only
required once per project.

Steps share a persistent `/workspace` directory. The Architect
captures stdout from each step and injects it as context into
dependent steps. If a step fails after all Orchestrator retries,
the Architect rewrites the spec up to 2 times before halting with
`ARCHITECT_BLOCKER.md`.

See [architect_design.md](architect_design.md) and [orchestrator_design.md](orchestrator_design.md) for full details.

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.
