# UAS Final Architecture Report

## Overview

This report documents the implementation of Project-Level Authentication and the E2E Test Harness for the UAS (Universal Autonomous System) framework, along with findings from the autonomous debug loop.

---

## Phase 1: Project-Level Authentication

### Problem

Authentication required manual interactive `claude` CLI login on every container run. The credentials were ephemeral and lost when the container stopped.

### Solution

A bind-mount strategy persists authentication credentials at the project level using a `.uas_auth` directory.

### Architecture

```
User's Project Directory ($PWD)
  |-- .uas_auth/                  # Claude CLI config (bind-mounted as /root/.claude)
  |   |-- .credentials.json      # OAuth/API token
  |   |-- settings.json           # User preferences
  |   `-- ...                     # Other CLI state
  |-- src/
  `-- ...
```

### Files Modified

**`install.sh` (wrapper script generation)**
- The generated `uas` wrapper script now detects `$PWD/.uas_auth` and, if present, adds `-v "$PWD/.uas_auth:/root/.claude:Z"` to the container run command.
- If `.uas_auth` does not exist, the container launches without the mount, falling back to interactive setup.

**`entrypoint.sh` (container entrypoint)**
- Before launching the interactive `claude` CLI (Stage 1), the script checks for `/root/.claude/.credentials.json`.
- If the file exists, is non-empty, and contains a `Token` field, authentication is considered valid and Stage 1 is skipped entirely.
- The container proceeds directly to Stage 2 (Architect Agent launch).
- If no valid auth is found, the original interactive flow runs, and the user authenticates via `claude`. Because `.uas_auth` is bind-mounted, the credentials persist to the host.

**`start_orchestrator.sh`**
- Updated to also pass through the `.uas_auth` bind mount when launching the orchestrator container.

**`.gitignore`**
- Added `.uas_auth/` to prevent accidental commit of credentials.

### Auth Flow

```
1. First run:  No .uas_auth/ exists
   -> Container starts interactive claude CLI
   -> User authenticates
   -> Credentials saved to /root/.claude (= $PWD/.uas_auth on host)

2. Subsequent runs:  .uas_auth/ exists with valid credentials
   -> Container detects valid auth at /root/.claude/.credentials.json
   -> Skips interactive setup
   -> Launches Architect Agent immediately
```

---

## Phase 2: E2E Test Harness

### Implementation

Created `run_e2e_test.sh` in the repository root. The script:

1. **Cleans** `./e2e_workspace` (removes all prior artifacts).
2. **Copies** the host's `~/.claude` directory into `./e2e_workspace/.uas_auth` to provide authentication without interactive login.
3. **Runs** the Architect Agent with:
   - `UAS_GOAL` set to the astronaut data pipeline prompt.
   - `UAS_WORKSPACE` pointing to `./e2e_workspace`.
   - `UAS_SANDBOX_MODE=local` (subprocess execution, no nested containers needed for testing).
4. **Asserts** that `summary.txt` exists and matches the pattern `There are currently \d+ astronauts in space.`
5. Exits 0 on success, 1 on failure.

### Test Prompt

> Build a two-step data pipeline: 1. Fetch JSON from http://api.open-notify.org/astros.json and save to raw_astros.json. 2. Read raw_astros.json, extract the astronaut count, and write "There are currently X astronauts in space." to summary.txt.

### Artifacts Produced

- `e2e_workspace/raw_astros.json` - Raw API response
- `e2e_workspace/summary.txt` - Final output with astronaut count
- `e2e_workspace/architect_state/` - Plan state and specs

---

## Phase 3: Autonomous Debug Loop Results

### Outcome

The E2E test passed on the first run (exit code 0). No bugs were encountered during the test execution.

### Execution Log Summary

```
Phase 1: Decomposed goal into 2 steps
  1. Fetch astronaut data
  2. Generate summary (depends on step 1)

Phase 2: Execution
  Step 1/2: Fetch astronaut data     -> SUCCEEDED
  Step 2/2: Generate summary         -> SUCCEEDED

Result: "There are currently 12 astronauts in space."
Assertion: PASSED (matches regex pattern)
```

### Prior Bug Fixes Already In Place

The codebase already contained fixes from previous debug sessions (documented in `bug_fix_report.md`):

1. **CLI subprocess PATH resolution** (`llm_client.py`): Uses `shutil.which("claude")` to resolve the absolute path, preventing PATH-related failures.
2. **PYTHONPATH for `/workspace` CWD** (`executor.py`): Sets `PYTHONPATH` to the framework root so Python module imports work correctly.
3. **Nested container inception prevention** (`executor.py`): The sandbox image is a lightweight `python:3.12-slim` image, not the full `uas-engine` image, avoiding recursive container builds.
4. **`IS_SANDBOX=1` environment propagation**: Set at all execution levels to signal the Claude CLI that it's running in a sandbox context.
5. **Session variable stripping** (`llm_client.py`): Removes `CLAUDECODE` and `CLAUDE_CODE_SESSION` from the subprocess environment to prevent nested-session detection errors.

### Architecture Diagram

```
Host Machine
  |
  |-- $PWD/.uas_auth/  <--bind-mount-->  Container: /root/.claude
  |-- $PWD/            <--bind-mount-->  Container: /workspace
  |
  `-- uas (wrapper script)
        |
        v
  Container (uas-engine:latest)
    |
    |-- entrypoint.sh
    |     |-- Check /root/.claude/.credentials.json
    |     |-- [SKIP interactive auth if valid]
    |     `-- Launch Architect Agent
    |
    |-- Architect Agent (architect/main.py)
    |     |-- Decompose goal into steps (LLM)
    |     |-- For each step:
    |     |     |-- Generate spec
    |     |     |-- Build task
    |     |     `-- Call Orchestrator (executor.py)
    |     |           |-- local mode: subprocess
    |     |           `-- container mode: lightweight sandbox
    |     |                 |-- Orchestrator (orchestrator/main.py)
    |     |                 |     |-- Build prompt
    |     |                 |     |-- LLM generates code
    |     |                 |     |-- Execute in sandbox
    |     |                 |     `-- Retry loop (3 attempts)
    |     |                 `-- sandbox.py: run code
    |     `-- Save state
    |
    `-- /workspace (user's project files + output)
```

---

## Summary

| Component | Status |
|---|---|
| Project-Level Auth (install.sh wrapper) | Implemented |
| Project-Level Auth (entrypoint.sh skip logic) | Implemented |
| Project-Level Auth (start_orchestrator.sh) | Implemented |
| E2E Test Harness (run_e2e_test.sh) | Implemented and passing |
| Autonomous Debug Loop | Completed (0 iterations needed) |
