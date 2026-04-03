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

Credentials are stored in `$PWD/.uas_auth/` (gitignored), which is
bind-mounted into the container as `/root/.claude`.  Run `bash
setup_auth.sh` to authenticate interactively inside the container.
On subsequent runs, the entrypoint detects valid credentials and
skips interactive setup entirely.

### Auth Setup

Integration tests and production runs use credentials stored in
`.uas_auth/` (gitignored), completely separate from your host
`~/.claude/` config.  Run the setup script once after cloning:

```bash
bash setup_auth.sh
```

This launches Claude Code inside the same container used for testing.
Authenticate, adjust settings if needed, then type `/exit`.
Credentials are saved to `.uas_auth/` and reused automatically.

Re-run the script at any time to change configuration.

### Running Tests

Integration tests run inside the `uas-engine` container using
credentials from `.uas_auth/`.  The container image is automatically
rebuilt when source files change.

```bash
# Run the full test suite (unit + integration):
python3 -m pytest tests/

# Run only unit tests (no auth required):
python3 -m pytest tests/ -m "not integration"

# Run only integration tests (use -s to see Claude output):
python3 -m pytest -s -m integration
```

### Local & Container Runners

```bash
# Run UAS locally (no container) — useful for TUI testing:
bash run_local.sh "your goal here"
bash run_local.sh --dry-run "your goal here"

# Run UAS in the uas-engine container:
bash run_container.sh "your goal here"

# Quick integration test (container, auto-rebuilds):
bash integration/quick_test.sh

# Prompt evaluation suite (container by default, --local available):
python3 integration/eval.py
python3 integration/eval.py --local
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
# Via CLI flag (writes to .uas_state/runs/<run_id>/output.json by default):
uas -o "your goal"

# Specify a custom path:
uas -o results.json "your goal"

# Or via environment variable:
UAS_OUTPUT=results.json uas "your goal"
```

The JSON file contains the goal, overall status (`completed`, `failed`, or
`blocked`), per-step results with elapsed times and timing breakdowns
(LLM vs sandbox time), and the total elapsed time.

### Event Log & Provenance

Record a structured event log and provenance graph for the run:

```bash
# Via CLI flag (writes to .uas_state/runs/<run_id>/events.jsonl by default):
uas --events "your goal"

# Specify a custom path:
uas --events my_events.jsonl "your goal"

# Or via environment variable:
UAS_EVENTS=events.jsonl uas "your goal"
```

The event log records every phase boundary as a JSONL file. The
provenance graph (`.uas_state/runs/<run_id>/provenance.json`) tracks data lineage
from goal through decomposition, code generation, and execution.

### HTML Run Report

Generate a self-contained HTML report with interactive DAG
visualization, execution timeline, per-step details, and provenance:

```bash
# Via CLI flag (writes to .uas_state/runs/<run_id>/report.html by default):
uas --report "your goal"

# Specify a custom path:
uas --report my_report.html "your goal"

# Or via environment variable:
UAS_REPORT=report.html uas "your goal"
```

The report includes five tabs: Overview (metrics and Mermaid DAG),
Timeline (LLM vs sandbox time bars), Steps (expandable details with
output, errors, files, and code evolution diffs), Provenance
(interactive graph), and Explanation (when `--explain` is also active).

### Execution Trace (Perfetto)

Export a Chrome Trace Event JSON file viewable in
[Perfetto](https://ui.perfetto.dev) for detailed timeline analysis:

```bash
# Via CLI flag (writes to .uas_state/runs/<run_id>/trace.json by default):
uas --trace "your goal"

# Specify a custom path:
uas --trace my_trace.json "your goal"

# Or via environment variable:
UAS_TRACE=trace.json uas "your goal"
```

Open the resulting file at ui.perfetto.dev (drag and drop) or
`chrome://tracing`. The trace shows Architect, Orchestrator, and
Sandbox activity on separate processes with per-step threads,
counter tracks for cumulative LLM and sandbox metrics, and
metadata for each span (attempt number, exit code, error type).

### Run Explanation

Print a human-readable explanation of what happened during a run,
including critical path analysis, failure taxonomy, rewrite
effectiveness, and cost breakdown:

```bash
# Via CLI flag (prints to stderr after completion):
uas --explain "your goal"

# Combine with --report to include in the HTML report:
uas --explain --report "your goal"

# Or via environment variable:
UAS_EXPLAIN=1 uas "your goal"
```

For post-hoc analysis of a previous run without re-executing:

```bash
python3 -m architect.explain /path/to/workspace
python3 -m architect.explain --step 2 /path/to/workspace
python3 -m architect.explain --failure 3 /path/to/workspace
python3 -m architect.explain --critical-path /path/to/workspace
python3 -m architect.explain --cost /path/to/workspace
```

### Non-Interactive / Local Mode

```bash
# Run without containers (uses local Python + Claude Code CLI):
UAS_SANDBOX_MODE=local UAS_GOAL="your goal" python3 -m architect.main

# Read goal from a file (useful for long or multi-line goals):
python3 -m architect.main --goal-file goal.txt
UAS_GOAL_FILE=goal.txt python3 -m architect.main

# Or run the prompt evaluation suite:
python3 integration/eval.py                # Run all prompt cases
python3 integration/eval.py -k hello       # Run cases matching 'hello'
python3 integration/eval.py --list         # List available cases
python3 integration/eval.py --local        # Use local subprocess mode
python3 integration/eval.py -v             # Verbose (show architect logs)
```

When `UAS_GOAL`, `UAS_TASK`, or `UAS_GOAL_FILE` is set, the entrypoint
skips the interactive Claude Code setup and proceeds directly to
execution.

## Requirements

- [Podman](https://podman.io/) or [Docker](https://www.docker.com/)
- Python packages: `rich>=13.0` (terminal dashboard), `jinja2>=3.1`
  (HTML report generation)

## Project Structure

```
.
├── install.sh                # Builds image and installs `uas` CLI
├── start_orchestrator.sh     # Alternative: build and launch manually
├── entrypoint.sh             # Two-stage entrypoint (setup then run)
├── Containerfile             # Image (Podman + Python + Claude Code CLI)
├── requirements.txt          # Python dependencies
├── config.py                 # Layered config (TOML + env vars)
├── uas.example.toml          # Sample config file with all keys
├── architect/                # Architect Agent (installed to /uas)
│   ├── main.py               # Controller loop
│   ├── planner.py            # LLM task decomposition + rewrite
│   ├── spec_generator.py     # UAS markdown spec writer
│   ├── executor.py           # Builds uas-sandbox image, runs Orchestrator
│   ├── state.py              # JSON state persistence
│   ├── events.py             # Structured event log system
│   ├── provenance.py         # W3C PROV-inspired provenance graph
│   ├── dashboard.py          # Rich terminal dashboard
│   ├── code_tracker.py       # Code evolution tracking across retries
│   ├── trace_export.py       # Perfetto trace export
│   ├── explain.py            # Decision explanation layer
│   ├── __main__.py           # Standalone explanation CLI
│   ├── report.py             # HTML report generator
│   └── report_template.html  # Jinja2 HTML template
├── orchestrator/             # Execution Orchestrator (containerized)
│   ├── main.py               # Build-Run-Evaluate loop
│   ├── llm_client.py         # Claude Code CLI subprocess wrapper
│   ├── claude_config.py      # CLAUDE.md template for workspace guidance
│   ├── sandbox.py            # Sandboxed code execution (local or container)
│   └── parser.py             # Code extraction from LLM responses
├── tests/                    # Unit and integration tests (pytest)
│   ├── conftest.py           # Shared fixtures, container helpers, auth
│   ├── test_integration.py   # Integration tests (run in uas-engine)
│   └── test_*.py             # Unit test modules
├── integration/              # Integration tests
│   ├── quick_test.sh         # Quick test (creates hello.txt)
│   ├── eval.py               # Prompt evaluation runner
│   └── prompts.json          # Prompt cases with goals and checks
├── setup_auth.sh             # One-time auth setup (interactive)
├── run_local.sh              # Run UAS locally (for TUI testing)
└── run_container.sh          # Run UAS in uas-engine container
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
              ├─ State Manager   -> tracks .uas_state/runs/<run_id>/
              └─ Executor        -> invokes Orchestrator loop
                   └─ uas-sandbox (python:3.12-slim)
                       └─ Orchestrator
                           ├─ LLM Client -> Claude Code CLI wrapper
                           └─ Sandbox    -> local subprocess (containerized)
```

All LLM calls go through the Claude Code CLI (`claude -p`)
installed inside the container, streaming output line-by-line for
real-time visibility into LLM generation. Authentication
is persisted to `$PWD/.uas_auth/` via bind mount, so interactive
login is only required once per project.

**Model tiering:** Set `UAS_MODEL_PLANNER` and/or `UAS_MODEL_CODER`
to use different models for planning vs code generation. Both fall
back to `UAS_MODEL` when unset.

### Architect Agent

The Architect takes a natural-language goal, uses the LLM to decompose it
into atomic steps, generates a UAS markdown spec for each, and drives
the Orchestrator to execute them sequentially.

**Planning:** The Planner sends the goal to the LLM with a structured prompt
that enforces self-contained steps with `title`, `description`, and
`depends_on` fields (JSON array). The prompt places the goal and examples
(data) at the top and instructions at the bottom for optimal response
quality. The LLM must produce a `<complexity_assessment>` justifying the
number of steps and an `<anti_patterns>` checklist guards against common
decomposition mistakes. For medium and complex goals, **multi-plan voting**
generates three decomposition plans in parallel (each with a different
strategy bias: default, simplicity, robustness) and selects the
highest-scoring plan based on parallelism ratio, description specificity,
and step compactness. A quick complexity estimation gate skips voting
overhead for trivial and simple goals. After critique, trivially combinable
steps in the same execution level (both with short descriptions and no
dependency relationship) are merged to reduce LLM calls and sandbox
invocations.

**Context propagation:** When step N depends on step M, the Architect
builds structured XML context using **dependency output distillation**:
each completed dependency is summarized into a `<dependency>` element
with `<files_produced>`, `<key_outputs>`, and `<relevant_data>` tags,
using the step's `UAS_RESULT` summary as the primary source and raw
stdout only as a fallback. A **structured progress file**
(`.uas_state/runs/<run_id>/progress.md`) replaces the flat scratchpad for context
building, with sections for current state, key decisions, completed
steps, and lessons learned — updated after every step completion or
failure. **Recursive workspace scanning** (up to 3 levels deep, skipping
`.uas_state/`, `.git/`, `__pycache__/`, `node_modules/`, `venv/`) groups
files by directory with previews and JSON key extraction, capped at
4000 chars. When `UAS_MAX_CONTEXT_LENGTH` is set and context exceeds
the limit, **tiered context compression** applies: Tier 1 (< 60%)
passes through unchanged, Tier 2 (60–80%) deterministically strips
previews and truncates stdout, Tier 3 (80–100%) uses LLM
summarization, and Tier 4 (> 100%) performs emergency truncation
retaining only the progress file and the tail of context.

**Self-correction:** If the Orchestrator fails a step (after its own 3
internal retries), the Architect uses Reflexion-based error recovery
with up to 4 progressive escalation rewrites:
1. Structured reflection with root cause diagnosis
2. Forced alternative strategy
3. Decomposition into granular sub-phases
4. Maximally defensive final attempt

After each failure, the Architect generates a **structured reflection**
via LLM (error type, root cause, lesson learned, next strategy) and
stores it in the step's persistent `reflections` list. All accumulated
reflections are passed as `<reflection_history>` into subsequent rewrite
prompts, enabling the LLM to learn from the full failure history and
avoid repeating failed strategies. Reflections are also written to the
global scratchpad so other steps can learn from them.

**Error-type-adaptive retry budgets** classify each failure (dependency,
logic, environment, network, timeout, format) and exit the retry loop
early when additional retries are unlikely to help — for example,
dependency errors get 1 retry, timeouts get 0 (immediate decomposition),
while logic errors get the full budget.

**Counterfactual root cause tracing:** When a failing step has
dependencies, the Architect asks the LLM whether the root cause is in
the current step or propagated from a dependency. Root cause tracing
runs before retry budget checks, so backtracking is always attempted
even when stagnation would otherwise stop retries. If a dependency is
identified as the root cause, **informed backtracking** augments the
dependency's description with downstream failure context (the error
message, verification criteria, and guidance to change approach), then
re-executes that dependency step and retries the current step with
fresh output — limited to depth 1 to avoid infinite loops. As a safety
net, **verification stagnation detection** forces backtracking when
2+ consecutive attempts pass code execution but fail
validation/verification with similar errors, even if root cause
tracing attributed the failure to the current step.

Each rewrite prompt also includes a `<previous_attempts>` section and a
`<counterfactual>` reasoning step. Outputs are red-flagged and resampled
if they show signs of confusion (excessive length or verbatim error
repetition). If all rewrites are exhausted, it halts with
`.uas_state/blocker.md`.

**Verification:** After a step exits successfully (code 0), post-execution
validation checks the `UAS_RESULT` JSON (status field, file existence)
and, if the step has a `verify` field, generates and runs a verification
script through the Orchestrator. If either check fails, the step
re-enters the rewrite loop rather than being marked complete. After all
steps finish, a final validation pass writes `.uas_state/validation.md` to the
workspace summarizing produced files and flagging any missing outputs.

**Best-practice guardrails:** Generated code is checked at two levels.
At the prompt level, the planner, code-generation prompt, and workspace
`CLAUDE.md` instruct the LLM to follow modern best practices: use
`git init -b main` for repositories, add `.gitignore` and `README.md`,
pin dependency versions, use HTTPS, never hardcode secrets, avoid
`eval()`/`exec()`/`shell=True`, catch specific exceptions, and use
context managers with `encoding="utf-8"`. After execution, a regex
scanner checks generated `.py` files for violations — hardcoded API
keys (error severity, triggers rewrite), bare `except:`, `eval()`,
`shell=True`, plain HTTP URLs, and `git init` without `-b` (warning
severity, logged). For multi-file projects, workspace-level checks
verify the Git branch is `main`, and that `.gitignore`, `README`,
and a dependency file exist. Warnings appear in `.uas_state/validation.md`.

**Workspace guidance:** Before each orchestrator invocation, the Executor
writes a `.claude/CLAUDE.md` file to the workspace. This gives the Claude
Code CLI persistent instructions on coding standards, environment details,
output format (`UAS_RESULT` JSON), security, and error handling best
practices. The file is dynamic per-step: it includes the current step
number, total steps, dependencies, and a summary of what prior steps
produced, so the LLM has full context of its position in the plan.

**Dynamic mid-execution re-planning:** After each step completes, the
Architect checks whether downstream steps still align with the actual
output. If a step produces different files than what dependent steps
reference (e.g. `output.json` instead of expected `data.csv`), the
Architect triggers incremental re-planning: it sends the completed
steps, the unexpected result, and the remaining plan to the LLM, which
adjusts pending steps to match reality. Re-planning is limited to once
per execution level to avoid infinite adjustment loops. Additionally,
**step description enrichment** appends concrete details (files
produced, data summaries) from each completed step to its dependents'
descriptions — a lightweight, LLM-free optimization that improves
downstream code generation accuracy.

**Parallel execution:** Independent steps (no dependency relationship)
run concurrently, optionally capped by `UAS_MAX_PARALLEL`. Per-step
timing tracks LLM call time vs sandbox execution time for performance
analysis.

**State:** Each run's state is persisted to `.uas_state/runs/<run_id>/state.json`
after every significant event (step start, completion, failure, rewrite).
Per-run directories (`.uas_state/runs/<run_id>/`) isolate all artifacts
(specs, code versions, events, reports) so multiple runs never overwrite
each other. A shared scratchpad (`.uas_state/scratchpad.md`) enables
cross-run learning with per-run filtering via `[run:<run_id>]` tags.
An environment probe runs on the first step, recording Python version,
installed packages, and disk space to the scratchpad so subsequent steps
can avoid wrong assumptions about the execution environment.

**Streaming decomposition:** During Phase 1, the LLM's analysis and
plan output is streamed to stderr line-by-line as it is generated,
giving real-time visibility into the decomposition process.

**Progress heartbeats:** Other long-running operations (code
generation, sandbox execution, orchestrator runs) emit periodic
heartbeat messages to stderr every 15–30 seconds showing elapsed
time. This ensures continuous feedback even during multi-minute
waits, so the user always knows the system is still working.

**Terminal dashboard:** During execution, a Rich Live dashboard shows
the DAG structure with step statuses (pending/executing/completed/failed),
active step details, and a timing breakdown. All panels support
scrolling: use `↑`/`↓` or `j`/`k` to scroll, `Tab` to cycle the
focused panel (DAG, Activity Log, Claude Code Output), `g`/`G` or
`Home`/`End` to jump to the top or bottom, and `PgUp`/`PgDn` for
larger jumps. The focused panel is highlighted with a bold border and
shows a scroll position indicator when content overflows. Press `P`
at any time to pause execution — the current step(s) will finish,
then the Architect waits before starting the next level. Press `P`
again to resume. When stdout is not a TTY or `rich` is not installed,
it falls back to the original print-based progress reporting.

**Event log & provenance:** When `--events` is passed (or `UAS_EVENTS`
is set), every significant action is recorded as a typed event in
`.uas_state/runs/<run_id>/events.jsonl` (one JSON object per line). A W3C
PROV-inspired provenance graph (`.uas_state/runs/<run_id>/provenance.json`)
tracks the full transformation chain from goal to result using
content-addressed entities, activities, and agents. Cross-attempt
linking connects rewrite errors to subsequent code versions.

**Code evolution tracking:** Every version of generated code is
recorded across orchestrator retries and architect-level rewrites
(up to 12 versions per step). Versions are persisted to
`.uas_state/runs/<run_id>/code_versions/{step_id}.json` with metadata
(attempt indices, prompt hash, exit code, error summary). Unified
diffs between consecutive versions are computed and displayed in the
HTML report with colorized add/remove highlighting. A retry
effectiveness metric indicates whether code changes converged
toward a solution. Each code version is linked in the provenance
graph via `wasDerivedFrom` edges.

**Execution trace export:** When `--trace` is passed (or `UAS_TRACE`
is set), a Chrome Trace Event JSON file is written to
`.uas_state/runs/<run_id>/trace.json`. The trace maps Architect, Orchestrator, and
Sandbox activity to separate processes with per-step threads,
enabling microsecond-precision timeline analysis in Perfetto or
`chrome://tracing`. Counter tracks show cumulative LLM calls and
sandbox runs.

**Decision explanation:** When `--explain` is passed (or
`UAS_EXPLAIN` is set), a post-run explanation is printed to stderr
covering critical path analysis, failure taxonomy, rewrite
effectiveness scoring, and cost breakdown. The standalone CLI
(`python3 -m architect.explain`) supports post-hoc analysis of
previous runs. When combined with `--report`, explanations are
included as a fifth tab in the HTML report.

### Orchestrator (Build-Run-Evaluate Loop)

```
1. Receive task (CLI arg / env var / stdin)
2. Scan workspace for existing files (used in prompt context)
3. Verify sandbox works (trivial print statement)
4. For attempt = 1..3:
   a. Build XML-structured prompt (data first: <environment>,
      <task>, <workspace_state>; then instructions: <role>,
      <constraints>, <output_contract>)
   b. Determine sample count N for this attempt (see below)
   c. If N=1: send prompt to LLM, extract code, execute in sandbox
   d. If N>1: generate N code samples in parallel with different
      prompt hints (default, robustness, simplicity), execute all
      in sandbox, select the best by execution score
   e. Parse JSON response, extract result field (text fallback)
   f. Emit delimited stdout/stderr blocks for reliable parsing
   g. Parse UAS_RESULT JSON line from stdout if present
   h. If exit_code == 0 -> SUCCESS, stop
   i. Else -> escalating error feedback:
      - 1st retry: root cause analysis + corrected script
      - 2nd retry: fundamentally different strategy required
      - 3rd retry: maximally defensive (try/except everywhere)
5. If all 3 attempts fail -> exit with error
```

Scripts are instructed to print a structured summary line:
`UAS_RESULT: {"status": "ok", "files_written": [...], "summary": "..."}`
which is parsed by both the Orchestrator and Architect for richer
context propagation and result validation.

**Best-of-N code generation:** When `UAS_BEST_OF_N` is set to 2 or 3,
the Orchestrator generates multiple code samples in parallel on retry
attempts and selects the best one by execution score. The first attempt
is always single-sample; on the second attempt N scales to 2, on the
third to 3 (capped by `UAS_BEST_OF_N`). Each sample uses a different
prompt variation (default, robustness-focused, simplicity-focused).
Samples are scored by exit code (success strongly preferred), UAS_RESULT
richness (files written, summary presence), and stdout informativeness.
This allocates extra compute budget where it's most needed — on harder
problems that have already failed once.

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

## Implicit Intelligence

By default, UAS automatically applies a set of behavioral enhancements that
improve reliability and output quality:

- **Goal expansion:** Vague goals are automatically clarified with concrete
  success criteria before decomposition.
- **Cross-run knowledge base:** Package versions and lessons learned from
  previous runs are persisted and used to avoid repeating past mistakes.
- **PyPI version resolution:** Current stable versions of suggested packages
  are resolved from PyPI and injected into prompts so the LLM pins versions.
- **Git management:** The workspace is automatically initialized as a git
  repository, with checkpoints committed after each successful step.
- **Research-first prompts:** The LLM is instructed to reason through its
  approach, verify library versions, and check for pitfalls before coding.
- **Output validation:** Post-execution checks verify that the `UAS_RESULT`
  JSON is present and that reported files actually exist.

All of these are active by default. To disable them all at once (useful for
debugging or minimal overhead), set:

```bash
UAS_MINIMAL=1 uas "your goal"
```

Individual features can be opted out of (e.g., `UAS_NO_LLM_GUARDRAILS=1`),
but `UAS_MINIMAL` is the simplest single switch to disable everything.

## Configuration File

Settings can be persisted in TOML files instead of environment
variables.  UAS checks these locations (later overrides earlier):

1. Built-in defaults
2. `~/.config/uas/config.toml` (user-global)
3. `{workspace}/.uas/config.toml` (project-level)
4. `UAS_*` environment variables (highest priority)

Config keys match the env var names without the `UAS_` prefix
(e.g. `UAS_MODEL` becomes `model`).  See `uas.example.toml` for
all available keys.

```toml
# .uas/config.toml
model = "claude-sonnet-4-6"
sandbox_mode = "local"
max_parallel = 4
persistent_retry = true
```

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `UAS_GOAL` | Goal for the Architect Agent | *(prompted)* |
| `UAS_GOAL_FILE` | Read goal from this file path | *(off)* |
| `UAS_TASK` | Task for the Orchestrator | *(prompted)* |
| `UAS_SANDBOX_MODE` | `container` or `local` | `container` |
| `UAS_WORKSPACE` | Workspace directory path | `/workspace` |
| `UAS_SANDBOX_IMAGE` | Sandbox container image | `python:3.12-slim` |
| `UAS_SANDBOX_TIMEOUT` | Sandbox execution timeout (seconds) | *(none)* |
| `UAS_DRY_RUN` | Preview plan without executing (`1`, `true`, or `yes`) | *(off)* |
| `UAS_RESUME` | Resume from saved state (`1`, `true`, or `yes`) | *(off)* |
| `UAS_OUTPUT` | Write JSON results summary to this file path | *(off)* |
| `UAS_EVENTS` | Write structured event log to this file path | *(off)* |
| `UAS_REPORT` | Generate HTML report at this file path | *(off)* |
| `UAS_TRACE` | Export Perfetto trace to this file path | *(off)* |
| `UAS_EXPLAIN` | Print run explanation to stderr (`1`, `true`, or `yes`) | *(off)* |
| `UAS_LLM_TIMEOUT` | LLM call timeout in seconds | *(none)* |
| `UAS_MODEL` | Override the Claude model (passed as `--model` to CLI) | *(default)* |
| `UAS_MODEL_PLANNER` | Model for planning, decomposition, and reflection | `UAS_MODEL` |
| `UAS_MODEL_CODER` | Model for code generation | `UAS_MODEL` |
| `UAS_BEST_OF_N` | Max parallel code samples per retry attempt (1 = disabled) | `1` |
| `UAS_MAX_PARALLEL` | Max concurrent orchestrator invocations per level | *(unlimited)* |
| `UAS_MAX_CONTEXT_LENGTH` | Max chars of inter-step context to propagate | *(unlimited)* |
| `UAS_MAX_ERROR_LENGTH` | Max chars of error output to include in rewrites | `3000` |
| `UAS_RATE_LIMIT_WAIT` | Base wait (seconds) for rate-limit backoff at the Architect level | `120` |
| `UAS_RATE_LIMIT_MAX_WAIT` | Maximum wait (seconds) per rate-limit retry | `600` |
| `UAS_RATE_LIMIT_RETRIES` | Max rate-limit retries per step before failing | `3` |
| `UAS_USAGE_LIMIT_WAIT` | Wait (seconds) between usage-limit retries | `3600` |
| `UAS_USAGE_LIMIT_RETRIES` | Max usage-limit retries per step before failing | `5` |
| `UAS_MINIMAL` | Disable all optional enhancements (`1`, `true`, or `yes`) | *(off)* |
| `UAS_NO_LLM_GUARDRAILS` | Skip LLM-based guardrail review (`1`) | *(off)* |
| `UAS_VERBOSE` | Enable debug logging (`1`, `true`, or `yes`) | *(off)* |
| `UAS_HOST_UID` | Host user UID for file ownership in containers | *(auto-set by wrapper)* |
| `UAS_HOST_GID` | Host user GID for file ownership in containers | *(auto-set by wrapper)* |
| `ANTHROPIC_API_KEY` | Anthropic API key | *(uses Claude CLI auth)* |

## License

Licensed under the Apache License v2.0 with LLVM Exceptions. See [LICENSE](LICENSE) for details.
