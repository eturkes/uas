# UAS Architectural Refactoring Plan

> Principal directive: eliminate brittleness, enforce strict provenance, and
> transition UAS to a deterministic "Flow Engineering" paradigm.

Each phase is independent at the phase boundary but internally sequential.
Phases are ordered by dependency (later phases consume earlier ones).

---

## Phase 1: Subprocess Worker Simplification

Replace the complex `subprocess.Popen` streaming wrapper in `llm_client.py`
with a clean `subprocess.run(["claude", ...])` call. The current implementation
(~200 lines of streaming, heartbeat threading, and pipe deadlock avoidance)
is over-engineered for a system that only needs the final JSON result.

### Tasks

- [x] **1.1** In `orchestrator/llm_client.py`, remove `_run_streaming()` method
      (lines 271-347) and its background stderr thread / pipe coordination logic.
- [x] **1.2** Replace `generate()` body (lines 453-580+) with a single
      `subprocess.run(["claude", "-p", "--dangerously-skip-permissions",
      "--model", model, "--output-format", "json"], input=prompt,
      capture_output=True, text=True, timeout=self.timeout)` call.
      Feed the prompt via `input=` kwarg (stdin), collect result from
      `result.stdout`.
- [x] **1.3** Remove the `progress_callback` / `stream-json` code paths from
      `generate()` and `_dispatch_progress()`. UAS does not need real-time
      token streaming; the Orchestrator only consumes the final
      `LLMResult(text, usage)`.
- [x] **1.4** Simplify `_parse_json_output()` — it remains the sole output
      parser. Delete `_parse_stream_json_output()` entirely (lines 404-451).
- [x] **1.5** Update `run_orchestrator()` in `architect/executor.py`
      (lines 145-190) to remove `progress_callback` plumbing and the
      `UAS_STREAM_PROGRESS` env var injection.
- [x] **1.6** In `architect/executor.py`, simplify `_run_streaming()` (lines
      193-244) to a `subprocess.run()` call. The architect's executor spawns
      `python -m orchestrator.main` — this does not need line-by-line stderr
      streaming either; capture and return.
- [x] **1.7** Remove `heartbeat_log()` context manager from `llm_client.py`
      (lines 216-244) and its import in `sandbox.py` line 9. Replace the
      sandbox heartbeat usage with a simple timeout on `subprocess.run`.
- [x] **1.8** Update all tests that mock `_run_streaming`, `Popen`, or
      `progress_callback` behavior. Grep `tests/` for these symbols and
      adapt assertions to the new `subprocess.run` interface.
- [x] **1.9** Smoke-test the full pipeline: `architect -> executor ->
      orchestrator -> llm_client -> claude CLI` with a trivial goal to
      confirm the simplified subprocess chain works end to end.

---

## Phase 2: Fuzzy Functions for State Parsing

Replace all regex and substring-matching state parsers with LLM-backed
`@fuzzy_function` calls that return typed Pydantic models. This eliminates
brittle pattern maintenance and gives deterministic structured outputs.

### Tasks

- [x] **2.1** Create `uas/fuzzy.py` (new module at project root). Implement:
      - `@fuzzy_function` decorator that accepts a Pydantic `BaseModel` as
        the return type annotation.
      - Internally calls `anthropic.Anthropic().messages.create()` (or a
        local model endpoint via `config.get("fuzzy_model")`) with a system
        prompt that enforces JSON output matching the Pydantic schema.
      - Validates the response with `Model.model_validate_json()`.
      - Caches identical inputs (LRU) to avoid redundant API calls for
        repeated evaluations within the same run.
- [x] **2.2** Define Pydantic result models in `uas/fuzzy_models.py`:
      - `ExecutionResult(success: bool, revert_needed: bool, error_category:
        str | None, summary: str)`
      - `UASResult(status: Literal["ok", "error"], files_written: list[str],
        summary: str, error: str | None)`
      - `ErrorClassification(category: Literal["rate_limit", "capacity",
        "auth", "connection", "timeout", "prompt_too_long",
        "output_truncated", "unknown"], retryable: bool,
        recommended_backoff: float, message: str)`
      - `CodeQuality(has_uas_result: bool, has_input_call: bool,
        is_file_modification: bool, missing_imports: list[str])`
- [x] **2.3** Replace `_UAS_RESULT_PATTERN` regex in `orchestrator/main.py`
      (line 96) with a fuzzy function:
      `parse_uas_output(stdout: str) -> UASResult`. The function's docstring
      becomes the LLM prompt: "Extract the UAS_RESULT JSON from sandbox
      stdout. Return structured fields."
- [x] **2.4** Replace `classify_error()` in `orchestrator/llm_client.py`
      (lines 96-180) and all its pattern lists (`_AUTH_PATTERNS`,
      `_RATE_LIMIT_PATTERNS`, etc.) with a fuzzy function:
      `classify_llm_error(returncode: int, stdout: str, stderr: str)
      -> ErrorClassification`.
- [x] **2.5** Replace `_INPUT_CALL_PATTERN` and `_FILE_MODIFICATION_PATTERN`
      in `orchestrator/main.py` (lines 100-108) with a fuzzy function:
      `assess_code_quality(code: str, task: str) -> CodeQuality`.
      This subsumes the existing `pre_execution_check()` regex checks.
- [x] **2.6** Replace `extract_sandbox_stdout()` / `extract_sandbox_stderr()`
      regex fallbacks in `architect/executor.py` with a fuzzy function:
      `parse_sandbox_output(raw: str) -> SandboxOutput(stdout: str,
      stderr: str, uas_result: dict | None)`. Keep the delimiter-based
      extraction as the fast path; only invoke the fuzzy function on
      delimiter absence.
- [x] **2.7** Create `evaluate_sandbox(stdout: str, stderr: str, exit_code:
      int) -> ExecutionResult` fuzzy function. Wire it into the Orchestrator
      main loop (after `run_in_sandbox()` returns) so the DAG's next-step
      decision is driven by the structured `ExecutionResult`, not raw
      exit codes.
- [x] **2.8** Add a `UAS_FUZZY_ENABLED=true` env var / config toggle so
      fuzzy functions can be disabled (falling back to current regex) for
      cost-sensitive or offline runs.
- [x] **2.9** Write unit tests for every fuzzy function using mocked
      Anthropic responses. Test both the happy path (valid JSON returned)
      and the fallback (malformed response, timeout, API error).

---

## Phase 3: Git-Driven State Management ("Time Travel")

Upgrade git from a passive checkpointing tool to the authoritative state
manager for the Reflexion loop. Every worker attempt gets a branch; failed
attempts are hard-reset to restore a clean filesystem.

### Tasks

- [x] **3.1** In `architect/main.py`, refactor `ensure_git_repo()` (line 293)
      to create a `uas-main` baseline tag after the initial commit. This
      immutable tag marks the pre-execution filesystem state for the entire
      run.
- [x] **3.2** Create `architect/git_state.py` (new module) with:
      - `create_attempt_branch(workspace: str, step_id: int, attempt: int)
        -> str`: creates branch `uas/step-{id}/attempt-{n}` from the
        latest checkpoint on `uas-wip`.
      - `commit_attempt(workspace: str, branch: str, message: str)`: stages
        all changes and commits on the attempt branch.
      - `rollback_to_checkpoint(workspace: str, step_id: int)`: executes
        `git checkout uas-wip && git reset --hard` to restore the filesystem
        to the last successful checkpoint. Deletes failed attempt branches.
      - `promote_attempt(workspace: str, branch: str)`: fast-forward merges
        the successful attempt branch into `uas-wip`.
- [x] **3.3** Wire `create_attempt_branch()` into the Orchestrator retry
      loop in `orchestrator/main.py` (line 1455, top of `for attempt`). Each
      attempt starts on a clean branch forked from the last checkpoint.
- [x] **3.4** Wire `rollback_to_checkpoint()` into the Orchestrator failure
      path (line 1587, `previous_code = code` block). When an attempt fails,
      hard-reset the workspace before the next attempt begins.
- [x] **3.5** Add the 3-strike rollback rule: in `architect/main.py`'s
      step execution loop, if a step fails 3 consecutive attempts, call
      `rollback_to_checkpoint()` to reset to the pre-step filesystem state,
      mark the step as `failed`, and continue to the next independent step.
- [x] **3.6** Refactor `git_checkpoint()` (line 406) to commit on the
      current attempt branch (not directly on `uas-wip`). Only
      `promote_attempt()` advances `uas-wip`.
- [x] **3.7** Update `finalize_git()` (line 559) to clean up all leftover
      `uas/step-*/attempt-*` branches before the final squash merge.
- [x] **3.8** Add a `git log --oneline uas-wip` provenance dump to the
      run's `output.json` so the full attempt history is auditable.
- [x] **3.9** Write integration tests: simulate a 3-attempt failure sequence,
      verify the workspace filesystem is byte-identical to the pre-step
      state after rollback.

---

## Phase 4: Architect-Enforced TDD

Constrain the Architect to emit unit tests before implementation code.
The Orchestrator's success criteria becomes binary: `pytest` exit code 0.

### Tasks

- [x] **4.1** In `architect/planner.py`, modify `decompose_goal_with_voting()`
      (the decomposition prompt, lines 190-302) to enforce a mandatory
      pattern: for every implementation step, a preceding `test:` step must
      exist in the DAG with the implementation step in its `depends_on`.
      Add this as a hard constraint in the decomposition prompt and the
      JSON schema examples.
- [x] **4.2** Add a post-decomposition validation pass in `architect/main.py`
      (after line 5639, end of Phase 1) that scans the step DAG and rejects
      any plan where an implementation step lacks a preceding test step.
      If invalid, re-prompt the planner with the specific violation.
- [x] **4.3** Define the test step contract: test steps must output files
      matching `test_*.py` or `*_test.py` in the workspace. The step
      description must include "Write pytest tests for..." and the
      `outputs` field must list the test file paths.
- [x] **4.4** Modify the implementation step's prompt injection (in
      `build_prompt()` at `orchestrator/main.py` line 651) to include:
      - The test file content (read from workspace after the test step runs).
      - An explicit constraint: "All tests in `{test_file}` must pass.
        Run `pytest {test_file}` as your final validation."
- [x] **4.5** Replace the current UAS_RESULT success check in the
      Orchestrator main loop (line 1578) with a binary `pytest` gate:
      - After sandbox execution, if the step has an associated test file,
        run `pytest {test_file} --tb=short -q` in the sandbox.
      - Success = pytest exit code 0. Failure = retry with the pytest
        output as the error.
- [x] **4.6** Update the Architect's post-step validation (the correction
      loop) to re-run `pytest` on the full test suite after corrections,
      not just the step's own tests.
- [x] **4.7** Add a `UAS_TDD_ENFORCE=true` config toggle (default true).
      When false, revert to the current behavior for legacy compatibility.
- [x] **4.8** Write tests for the TDD enforcement: mock a decomposition that
      lacks test steps, verify it is rejected and re-prompted.

---

## Phase 5: The Context Janitor

Add a mandatory post-edit formatting step. After any worker execution that
modifies files, UAS runs `ruff format` (or `black`) to normalize the code
before it enters the next context window.

### Tasks

- [x] **5.1** Create `uas/janitor.py` (new module) with:
      - `format_workspace(workspace: str, files: list[str] | None = None)`:
        Runs `ruff format` on specified files (or all `.py` files if None).
        Falls back to `black` if `ruff` is not installed. Falls back to
        no-op if neither is available.
      - `lint_workspace(workspace: str, files: list[str] | None = None)
        -> list[str]`: Runs `ruff check --select=F` (Pyflakes only) and
        returns any fatal errors (undefined names, unused imports that
        shadow builtins).
- [x] **5.2** Wire `format_workspace()` into the Orchestrator success path
      in `orchestrator/main.py` (after line 1578, `exit_code == 0` block).
      Before exiting, format all files listed in `UAS_RESULT.files_written`.
- [x] **5.3** Wire `format_workspace()` into `git_checkpoint()` in
      `architect/main.py` (line 406). Format before committing so every
      checkpoint is clean.
- [x] **5.4** Wire `lint_workspace()` into `evaluate_sandbox()` (Phase 2's
      fuzzy function) as a pre-check. If the linter finds fatal errors
      (e.g., `NameError`-class issues), mark `revert_needed=True` in the
      `ExecutionResult` without burning an LLM call.
- [x] **5.5** Ensure `ruff` is available in both sandbox modes:
      - Local mode: add `ruff` to UAS's own `pyproject.toml` dependencies.
      - Container mode: add `uv pip install ruff` to the container image
        build step in `architect/executor.py`.
- [x] **5.6** Add config option `context_janitor.formatter` with values
      `"ruff"` (default), `"black"`, or `"none"` to allow user override.
- [x] **5.7** Write tests: generate intentionally messy code, run the
      janitor, verify the output is `ruff format`-compliant.

---

## Phase 6: Strict Context Pruning

On a failed worker execution, completely wipe the worker's conversational
history. The retry prompt must contain ONLY: (1) the Architect's immutable
spec, (2) the current code state, and (3) the exact error stack trace.

### Tasks

- [x] **6.1** In `orchestrator/main.py`, refactor `build_prompt()` (line 651)
      to accept a `mode: Literal["full", "retry_clean"]` parameter.
      - `"full"` (attempt 1): current behavior — includes environment,
        knowledge, approach, workspace files, full context.
      - `"retry_clean"` (attempt 2+): stripped-down prompt with only three
        sections: `<spec>`, `<current_code>`, `<error>`.
- [x] **6.2** Define the `<spec>` section: extract the Architect's immutable
      step description from `UAS_TASK` env var or the `step_context` dict
      passed by the executor. This is the single source of truth for what
      the worker must produce.
- [ ] **6.3** Define the `<current_code>` section: read the actual files
      from the workspace (not the previously generated code variable).
      Use `scan_workspace()` to get current file contents. This grounds
      the retry in filesystem reality, not the LLM's memory.
- [ ] **6.4** Define the `<error>` section: include ONLY the last
      `result["stderr"]` and the last 50 lines of `result["stdout"]`.
      Strip ANSI escape codes. No attempt history, no prior code snippets,
      no retry guidance prose.
- [ ] **6.5** Remove the `attempt_history` accumulation logic (lines
      1433-1434, 1478-1483, 1494-1499, 1528-1533, 1591-1596). The retry
      loop variable `attempt_history` is deleted entirely.
- [ ] **6.6** Remove `_llm_retry_guidance()` and the hardcoded fallback
      guidance (lines 609-648). These injected opinions that conflicted
      with the Architect's spec.
- [ ] **6.7** In the retry loop (line 1455), after a failed attempt:
      1. Call `rollback_to_checkpoint()` (Phase 3) to reset the filesystem.
      2. Call `format_workspace()` (Phase 5) on the checkpoint state.
      3. Build the retry prompt in `"retry_clean"` mode.
      4. Generate fresh — the LLM has zero memory of prior attempts.
- [ ] **6.8** Update the Architect's `_build_step_context()` in
      `architect/main.py` to always include the full step spec (title +
      description + verify criteria + outputs) as a `step_spec` key in
      the env dict, so it is available to `build_prompt()` for the
      `<spec>` section.
- [ ] **6.9** Write tests: mock a 3-attempt sequence, verify that attempt 2
      and 3 prompts contain zero references to prior attempts' code or
      error messages, and that they do contain the spec and current
      filesystem state.

---

## Execution Order

Phases should be implemented in this order due to dependencies:

1. **Phase 1** (Subprocess) — no dependencies, simplifies all later work.
2. **Phase 3** (Git State) — no dependencies on Phase 1 output, but should
   land before Phases 4-6 which consume rollback primitives.
3. **Phase 2** (Fuzzy Functions) — can proceed in parallel with Phase 3.
4. **Phase 5** (Context Janitor) — depends on Phase 3 (formats before
   checkpoint commits).
5. **Phase 4** (TDD) — depends on Phase 3 (test files committed to
   attempt branches).
6. **Phase 6** (Context Pruning) — depends on Phases 3 and 5 (rollback +
   format before retry prompt).
