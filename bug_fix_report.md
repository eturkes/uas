# Bug Fix Report: UAS Framework Autonomous Loop Failures

## Summary

The UAS framework contained **6 interconnected bugs** that caused the Architect/Orchestrator
autonomous loop to hang or fail when run non-interactively. The root cause was a combination
of interactive-only entrypoints, missing volume mounts, incorrect subprocess isolation, and
a hardcoded read-only sandbox that prevented file I/O between steps.

## Bugs Found and Fixed

### Bug 1: `entrypoint.sh` — Interactive `claude` Blocks Non-Interactive Runs

**File:** `entrypoint.sh:21`
**Symptom:** The entrypoint unconditionally runs bare `claude` (interactive REPL), which
waits for TTY input. When the container is launched programmatically (by the executor or
any automation), the process hangs indefinitely.

**Fix:** Added an early check: if `UAS_TASK` or `UAS_GOAL` environment variables are set,
skip the interactive Stage 1 entirely and proceed directly to the Architect Agent.

### Bug 2: `architect/executor.py` — Orchestrator Container Re-enters Architect

**File:** `architect/executor.py:66`
**Symptom:** `run_orchestrator()` launched a `uas-engine` container with the default
entrypoint (`entrypoint.sh`), which runs the **Architect** (not the Orchestrator). This
created an infinite recursion: Architect -> container -> entrypoint -> Architect -> ...
The process would hang at the interactive `claude` prompt (Bug 1) before reaching recursion.

**Fix:** Override the entrypoint with `--entrypoint python3 -m orchestrator.main` so the
container runs the Orchestrator directly. Added a `local` execution mode that runs the
Orchestrator as a subprocess without containers, for environments without nested container
support.

### Bug 3: `architect/executor.py` — No `/workspace` Volume Mount

**File:** `architect/executor.py:66`
**Symptom:** The executor launched the Orchestrator container without mounting `/workspace`.
Generated code writing to `/workspace` would fail with "No such file or directory" because
the container's workspace volume was empty/nonexistent.

**Fix:** Added `-v {workspace}:/workspace:Z` to the container run command, passing through
the workspace directory from the parent environment.

### Bug 4: `orchestrator/sandbox.py` — Read-Only Sandbox With No Workspace Mount

**File:** `orchestrator/sandbox.py:29-36`
**Symptom:** The sandbox container was run with `--read-only` and no `/workspace` volume
mount. LLM-generated code that tried to write files to `/workspace` (the core use case)
would fail with "Read-only file system" or "No such file or directory."

**Fix:** Added `-v {WORKSPACE_PATH}:/workspace:Z` to mount the workspace directory into
the sandbox. The `--read-only` flag is kept (it only affects the root filesystem), while
the explicitly mounted workspace volume remains writable. Also added `WORKSPACE`
environment variable so generated code can discover the workspace path dynamically.

### Bug 5: `orchestrator/llm_client.py` — Nested Claude Session Detection

**File:** `orchestrator/llm_client.py:17-27`
**Symptom:** When the framework is invoked from within a Claude Code session (e.g., the
interactive setup, or during development), the `CLAUDECODE` environment variable leaks
into child processes. The Claude CLI detects this and refuses to start:
`"Error: Claude Code cannot be launched inside another Claude Code session."`

This was the **silent killer**: the subprocess exited with code 1, the error was reported
as a generic CLI failure, and no code was ever generated.

**Fix:** Strip `CLAUDECODE` and `CLAUDE_CODE_SESSION` from the subprocess environment.
Also added `stdin=subprocess.DEVNULL` to prevent the CLI from ever blocking on stdin.

### Bug 6: `architect/planner.py` — Prompt Contradicts Architecture

**File:** `architect/planner.py:14-32`
**Symptom:** The decomposition prompt told the LLM there is "NO persistent storage between
steps." This caused the LLM to design steps that tried to pass all data via stdout/context
strings instead of using the shared workspace filesystem. For file-based tasks (the primary
use case), the generated code would either fail or produce incorrect results.

**Fix:** Updated the prompt to accurately describe the shared workspace:
"Steps share a persistent workspace directory for file I/O. The path is available via
`os.environ.get('WORKSPACE', '/workspace')`."

## Additional Hardening

- **`orchestrator/main.py:build_prompt()`**: Added WORKSPACE env var instructions to the
  code generation prompt, ensuring LLM-generated scripts use the workspace path dynamically
  rather than hardcoding `/workspace`.
- **`orchestrator/main.py:get_task()`**: Added `sys.stdin.isatty()` check to prevent
  blocking on stdin when running as a subprocess.
- **`start_orchestrator.sh`**: Made `-it` flags conditional on TTY availability, preventing
  hang when launched from non-interactive contexts.
- **All subprocess calls**: Added `stdin=subprocess.DEVNULL` where missing.

## Root Cause Analysis

The **silent bug** was Bug 5 (CLAUDECODE env var leak). It was silent because:

1. The `claude` CLI exited with code 1, which the framework reported as a generic
   "CLI exited with code 1" error.
2. The actual error message ("cannot be launched inside another Claude Code session")
   was buried in stderr and not surfaced to the user.
3. This failure occurred at the very first LLM call (goal decomposition), so the entire
   pipeline failed before any real work began.

The other bugs (1-4, 6) would have been encountered sequentially if Bug 5 were fixed in
isolation. Together, they formed a chain of failures that made the framework non-functional
for any real-world task.

## Verification

The E2E test (`e2e_test.py`) validates the complete pipeline:
- Architect decomposes a two-step goal via LLM
- Each step generates Python code via the Orchestrator
- Code executes in the sandbox with shared workspace
- Step 1 writes `e2e_step1.txt` (contains "HELLO")
- Step 2 reads it and writes `e2e_step2.txt` (contains "HELLO WORLD")

**Result:** PASSED. Both files created with correct content. Full plan state shows
"completed" status for all steps.
