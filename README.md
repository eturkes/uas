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

### Quick Test

```bash
# Quick test from the repo (first run requires interactive auth):
bash integration/quick_test.sh
```

### Resuming a Run

If the Architect is interrupted or a step fails, you can resume from where
it left off instead of starting over:

```bash
# Resume from saved state:
uas --resume "your goal"

# Or via environment variable:
UAS_RESUME=1 uas "your goal"

# Force a clean start (ignore any saved state):
uas --fresh "your goal"
```

When resuming, completed steps are skipped and their outputs are used as
context for dependent steps. If the saved state is corrupted or missing,
the Architect falls back to a fresh start automatically.

### Dry-Run Mode

Preview the decomposition plan without executing any steps:

```bash
# Via CLI flag:
uas --dry-run "your goal"

# Or via environment variable:
UAS_DRY_RUN=1 uas "your goal"
```

Dry-run mode runs Phase 1 (decomposition) and prints the step DAG with titles,
descriptions, and dependency structure, then exits without executing anything.

### JSON Output

Write a machine-readable JSON summary of the run:

```bash
# Via CLI flag:
uas -o results.json "your goal"

# Or via environment variable:
UAS_OUTPUT=results.json uas "your goal"
```

The JSON file contains the goal, overall status (`completed`, `failed`, or
`blocked`), per-step results with elapsed times and timing breakdowns
(LLM vs sandbox time), and the total elapsed time.

### Non-Interactive / Local Mode

```bash
# Run without containers (uses local Python + Claude Code CLI):
UAS_SANDBOX_MODE=local UAS_GOAL="your goal" python3 -m architect.main

# Or run the prompt evaluation suite:
python3 integration/eval.py                # Run all prompt cases
python3 integration/eval.py -k hello       # Run cases matching 'hello'
python3 integration/eval.py --list         # List available cases
python3 integration/eval.py --local        # Use local subprocess mode
python3 integration/eval.py -v             # Verbose (show architect logs)
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
│   ├── claude_config.py      # CLAUDE.md template for workspace guidance
│   ├── sandbox.py            # Sandboxed code execution (local or container)
│   └── parser.py             # Code extraction from LLM responses
├── tests/                    # Unit tests (pytest)
│   ├── conftest.py           # Shared fixtures
│   └── test_*.py             # Test modules
└── integration/              # Integration tests
    ├── quick_test.sh            # Quick test (creates hello.txt)
    ├── eval.py                # Prompt evaluation runner
    └── prompts.json           # Prompt cases with goals and checks
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
              ├─ State Manager   -> tracks .state/state.json
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
`depends_on` fields (JSON array). After critique, trivially combinable
steps in the same execution level (both with short descriptions and no
dependency relationship) are merged to reduce LLM calls and sandbox
invocations.

**Context propagation:** When step N depends on step M, the Architect
builds structured XML context from step M's output (`<previous_step_output>`,
`<workspace_files>`, `<verification>`, `<scratchpad>` tags). Observation
masking replaces older dependency outputs with compact summaries, keeping
recent outputs in full. Workspace files are scanned for previews (with
JSON key extraction). A persistent scratchpad (`.state/scratchpad.md`)
accumulates timestamped learnings across steps — successes, failures,
and environment details — giving all steps visibility into the run's
history. If context exceeds the limit, it is compressed via the LLM
with fallback to truncation.

**Self-correction:** If the Orchestrator fails a step (after its own 3
internal retries), the Architect uses reflection-based error recovery
with up to 4 progressive escalation rewrites:
1. Structured reflection with root cause diagnosis
2. Forced alternative strategy
3. Decomposition into granular sub-phases
4. Maximally defensive final attempt

Outputs are red-flagged and resampled if they show signs of confusion
(excessive length or verbatim error repetition). If all rewrites are
exhausted, it halts with `BLOCKER.md`.

**Verification:** After a step exits successfully (code 0), post-execution
validation checks the `UAS_RESULT` JSON (status field, file existence)
and, if the step has a `verify` field, generates and runs a verification
script through the Orchestrator. If either check fails, the step
re-enters the rewrite loop rather than being marked complete. After all
steps finish, a final validation pass writes `VALIDATION.md` to the
workspace summarizing produced files and flagging any missing outputs.

**Workspace guidance:** Before each orchestrator invocation, the Executor
writes a `.claude/CLAUDE.md` file to the workspace. This gives the Claude
Code CLI persistent instructions on coding standards, environment details,
output format (`UAS_RESULT` JSON), and error handling best practices.

**Parallel execution:** Independent steps (no dependency relationship)
run concurrently, capped by `UAS_MAX_PARALLEL` (default 4) to prevent
resource exhaustion. Per-step timing tracks LLM call time vs sandbox
execution time for performance analysis.

**State:** All state is persisted to `.state/state.json`
after every significant event (step start, completion, failure, rewrite).
An environment probe runs on the first step, recording Python version,
installed packages, and disk space to the scratchpad so subsequent steps
can avoid wrong assumptions about the execution environment.

### Orchestrator (Build-Run-Evaluate Loop)

```
1. Receive task (CLI arg / env var / stdin)
2. Verify sandbox works (trivial print statement)
3. For attempt = 1..3:
   a. Build XML-structured prompt (<role>, <environment>,
      <task>, <constraints>, <verification>)
   b. Send prompt to LLM -> receive response
   c. Extract code block from response
   d. Execute code in sandbox container
   e. Parse UAS_RESULT JSON line from stdout if present
   f. If exit_code == 0 -> SUCCESS, stop
   g. Else -> escalating error feedback:
      - 1st retry: root cause analysis + corrected script
      - 2nd retry: fundamentally different strategy required
      - 3rd retry: maximally defensive (try/except everywhere)
4. If all 3 attempts fail -> exit with error
```

Scripts are instructed to print a structured summary line:
`UAS_RESULT: {"status": "ok", "files_written": [...], "summary": "..."}`
which is parsed by both the Orchestrator and Architect for richer
context propagation and result validation.

### Security Model

| Layer | Control |
|---|---|
| Host <-> Container | Only the workspace directory is mounted writable. Auth credentials are mounted read-only. No other host paths are exposed. |
| Container environment | Full network access, no memory or CPU limits, writable filesystem. Each task runs in its own isolated container. |
| LLM-generated code | Never executed on the host. Always runs inside a container. |

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
| `UAS_SANDBOX_TIMEOUT` | Sandbox execution timeout (seconds) | *(none)* |
| `UAS_DRY_RUN` | Preview plan without executing (`1`, `true`, or `yes`) | *(off)* |
| `UAS_RESUME` | Resume from saved state (`1`, `true`, or `yes`) | *(off)* |
| `UAS_OUTPUT` | Write JSON results summary to this file path | *(off)* |
| `UAS_LLM_TIMEOUT` | LLM call timeout in seconds | *(none)* |
| `UAS_MODEL` | Override the Claude model (passed as `--model` to CLI) | *(default)* |
| `UAS_MAX_PARALLEL` | Max concurrent orchestrator invocations per level | `4` |
| `UAS_MAX_CONTEXT_LENGTH` | Max chars of inter-step context to propagate | `8000` |
| `UAS_MAX_ERROR_LENGTH` | Max chars of error output to include in rewrites | `2000` |
| `UAS_VERBOSE` | Enable debug logging (`1`, `true`, or `yes`) | *(off)* |
| `ANTHROPIC_API_KEY` | Anthropic API key | *(uses Claude CLI auth)* |

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.
