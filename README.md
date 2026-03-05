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

### Smoke Test

```bash
# Quick test from the repo (first run requires interactive auth):
bash test/run.sh
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
├── Containerfile             # Image (Podman + Python + Claude Code CLI)
├── requirements.txt          # Python dependencies
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
├── test/
│   └── run.sh                # Smoke test (creates hello.txt via container)
├── e2e_test.py               # E2E test (local mode, two-step task)
└── run_e2e_test.sh           # E2E test (full pipeline, live data)
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

### Architect Agent

The Architect takes a natural-language goal, uses the LLM to decompose it
into atomic steps, generates a UAS markdown spec for each, and drives
the Orchestrator to execute them sequentially.

**Planning:** The Planner sends the goal to the LLM with a structured prompt
that enforces self-contained steps with `title`, `description`, and
`depends_on` fields (JSON array).

**Context propagation:** When step N depends on step M, the Architect
captures sandbox stdout from step M and injects it as literal context
into step N's task description.

**Self-correction:** If the Orchestrator fails a step (after its own 3
internal retries), the Architect rewrites the spec up to 2 times by
sending the LLM the original task plus truncated error output. If all
rewrites are exhausted, it halts with `ARCHITECT_BLOCKER.md`.

**State:** All state is persisted to `architect_state/plan_state.json`
after every significant event (step start, completion, failure, rewrite).

### Orchestrator (Build-Run-Evaluate Loop)

```
1. Receive task (CLI arg / env var / stdin)
2. Verify nested Podman works (trivial print statement)
3. For attempt = 1..3:
   a. Build prompt (include previous error if retrying)
   b. Send prompt to LLM -> receive response
   c. Extract code block from response
   d. Execute code in sandbox container
   e. If exit_code == 0 -> SUCCESS, stop
   f. Else -> feed error back into prompt, retry
4. If all 3 attempts fail -> exit with error
```

### Security Model

| Layer | Control |
|---|---|
| Host <-> Orchestrator | `--privileged` grants kernel capabilities for nested namespaces. No host socket mounted. |
| Orchestrator <-> Sandbox | `--network=none`, `--read-only` filesystem, `--memory=256m`, `--cpus=1`, 60s timeout. Script bind-mounted read-only. |
| LLM-generated code | Never executed in the Orchestrator process. Always runs inside the nested sandbox. |

`--privileged` is required because Podman-in-Podman needs `CAP_SYS_ADMIN`
and `/dev/fuse` access to create inner container namespaces. The vfs
storage driver is used for the inner Podman for maximum compatibility
in nested scenarios.

## Logging

All log output goes to **stderr**, keeping stdout clean for piping.
By default only INFO-level messages (progress and results) are shown.
Pass `-v` / `--verbose` to enable DEBUG output (includes generated code
dumps and full sandbox output):

```bash
# Verbose architect run:
python3 -m architect.main -v "your goal"

# Verbose orchestrator run:
python3 -m orchestrator.main -v "your task"

# Or via environment variable:
UAS_VERBOSE=1 python3 -m architect.main "your goal"
```

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `UAS_GOAL` | Goal for the Architect Agent | *(prompted)* |
| `UAS_TASK` | Task for the Orchestrator | *(prompted)* |
| `UAS_SANDBOX_MODE` | `container` or `local` | `container` |
| `UAS_WORKSPACE` | Workspace directory path | `/workspace` |
| `UAS_SANDBOX_IMAGE` | Sandbox container image | `python:3.12-slim` |
| `UAS_SANDBOX_TIMEOUT` | Sandbox execution timeout (seconds) | `60` |
| `UAS_VERBOSE` | Enable debug logging (`1`, `true`, or `yes`) | *(off)* |
| `ANTHROPIC_API_KEY` | Anthropic API key | *(uses Claude CLI auth)* |

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.
