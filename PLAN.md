# UAS Improvement Plan

Each step is designed to be completed in a single coding session.
After each step, run `python3 -m pytest tests/ -v` to verify all tests pass.

## Step 1: Add unit tests for orchestrator main loop and executor

**Goal:** Cover the two most critical untested code paths.

**Changes:**
- Create `tests/test_orchestrator_main.py` with mocked LLM + sandbox:
  - Test successful single-attempt execution
  - Test retry on sandbox failure (up to 3 attempts)
  - Test failure after all retries exhausted
  - Test handling of empty code extraction
  - Test task input from env var, CLI args, and stdin
  - Test `build_prompt` with and without previous errors
- Create `tests/test_executor.py` with mocked subprocess:
  - Test `run_orchestrator` in local mode
  - Test `run_orchestrator` timeout handling
  - Test `extract_sandbox_stdout` with various log formats
  - Test `find_engine` with/without podman/docker
- Update `tests/conftest.py` if shared fixtures are needed.

**Validation:** `python3 -m pytest tests/ -v` — all new + existing tests pass.

## Step 2: Improve decomposition and orchestrator prompts

**Goal:** Reduce wasted retries by making sandbox capabilities and
constraints explicit.

**Changes:**
- In `architect/planner.py` `DECOMPOSITION_PROMPT`:
  - Add explicit capability: "The sandbox has full network access and can
    install packages freely (e.g. pip install). Use any libraries needed."
  - Add: "Each step must produce observable output to stdout so downstream
    steps can use the results."
  - Add: "Do NOT create steps that require user interaction."
- In `orchestrator/main.py` `build_prompt`:
  - Add to Environment section: "The script runs inside a sandboxed
    container with full network access."
  - Add: "You may install packages freely (e.g. pip install) and use any
    libraries needed."
- Add tests for prompt content in `tests/test_orchestrator_main.py` and
  `tests/test_planner.py` verifying the key capabilities appear.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass.

## Step 3: Add dry-run mode

**Goal:** Let users preview the decomposition plan without executing it.

**Changes:**
- In `architect/main.py`:
  - Add `--dry-run` CLI flag and `UAS_DRY_RUN` env var.
  - When active, run Phase 1 (decompose) but skip Phase 2 (execute).
  - Print the step DAG to stderr with titles, descriptions, and
    dependency structure.
  - Exit 0 after displaying the plan.
- Add tests in `tests/test_dry_run.py`:
  - Test that `--dry-run` flag is parsed.
  - Test that dry-run mode produces plan output without calling executor.
- Update README.md: document `--dry-run` and `UAS_DRY_RUN`.
- Update the Environment Variables table in README.md.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass.

## Step 4: Add progress reporting

**Goal:** Give users real-time feedback on execution progress.

**Changes:**
- In `architect/main.py`:
  - Add a `report_progress` function that prints a compact status line:
    `[3/7] Step 3: "Parse data" (attempt 1, 2 completed, 0 failed)`
  - Call it at the start of each step and after completion/failure.
  - Add elapsed time tracking per step and total.
  - At completion, print a summary table:
    step id | title | status | elapsed time
- Add tests in `tests/test_progress.py`:
  - Test `report_progress` output format.
  - Test elapsed time tracking.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass.

## Step 5: Make LLM client configurable and resilient

**Goal:** Support timeout configuration, model selection, and transient
failure retry.

**Changes:**
- In `orchestrator/llm_client.py`:
  - Accept `timeout` from `UAS_LLM_TIMEOUT` env var (default 120s).
  - Accept model override from `UAS_MODEL` env var (passed as
    `--model` flag to claude CLI).
  - Add retry with exponential backoff for transient errors (timeout,
    connection errors) — max 2 retries.
- Add tests in `tests/test_llm_client.py`:
  - Test timeout configuration from env var.
  - Test model flag is passed when `UAS_MODEL` is set.
  - Test retry behavior on transient failures.
  - Test that non-transient errors are not retried.
- Update README.md Environment Variables table with `UAS_LLM_TIMEOUT`
  and `UAS_MODEL`.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass.

## Step 6: Improve stdout extraction and context propagation

**Goal:** Make inter-step context propagation more robust and complete.

**Changes:**
- In `architect/executor.py` `extract_sandbox_stdout`:
  - Refactor to use regex-based extraction with clear delimiters.
  - Add support for extracting stderr context as well.
  - Add truncation to prevent context explosion (configurable via
    `UAS_MAX_CONTEXT_LENGTH`, default 4000).
- In `architect/main.py` `build_context`:
  - Include both stdout and stderr summaries from dependencies.
  - Include a list of files written to workspace by each step (if
    available from the orchestrator output).
- Add/update tests in `tests/test_executor.py`:
  - Test `extract_sandbox_stdout` with realistic orchestrator output.
  - Test truncation behavior.
  - Test context building with rich output.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass.

## Step 7: Add structured JSON output

**Goal:** Provide machine-readable run results alongside human-readable logs.

**Changes:**
- In `architect/main.py`:
  - Add `--output` / `-o` flag to specify a JSON results file.
  - Also support `UAS_OUTPUT` env var.
  - At completion (or failure), write a JSON summary:
    ```json
    {
      "goal": "...",
      "status": "completed|failed|blocked",
      "steps": [
        {"id": 1, "title": "...", "status": "...", "elapsed": 12.3}
      ],
      "total_elapsed": 45.6
    }
    ```
  - Write this alongside (not replacing) the existing state file.
- Add tests in `tests/test_output.py`:
  - Test JSON output file is created with correct structure.
  - Test output on success, failure, and blocked states.
- Update README.md with `--output` docs and `UAS_OUTPUT` in env table.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass.

## Step 8: Cleanup and documentation polish

**Goal:** Final polish pass.

**Changes:**
- Verify `.gitignore` covers all generated artifacts (check for any
  stray `__pycache__` references, temp files, etc.).
- Remove any stray `__pycache__` directories from the repo if tracked.
- Verify README.md is fully up-to-date with all new features.
- Ensure the Project Structure section reflects any new files.
- Verify all env vars are documented.
- Run full test suite one final time.

**Validation:** `python3 -m pytest tests/ -v` — all tests pass, clean `git status`.
