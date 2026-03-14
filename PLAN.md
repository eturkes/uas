# PLAN: Replace Hardcoded Decision Paths with LLM-Driven Reasoning

This plan identifies brittle, hardcoded decision paths in UAS and replaces them
with LLM-based reasoning. Each section is designed to be executed sequentially
by a fresh coding agent. Sections are ordered by impact (highest first) and
independence (no section depends on a later section).

**Conventions:**
- `[x]` = completed, `[ ]` = pending
- Each section lists files to modify, what to change, and how to test
- All LLM calls should fall back to the existing heuristic on failure

---

## Section 1: LLM-Based Retry Decision (Replace Stagnation Detection + Error Budgets)
- [x] **Status: Complete**

### Problem
`architect/main.py:83-95` defines `_ERROR_RETRY_BUDGETS` — a hardcoded dict mapping
error types to fixed retry counts (e.g., `timeout: 0`, `dependency_error: 1`,
`logic_error: 4`). This is brittle: a dependency error caused by a typo in a
package name is trivially fixable, but one caused by a missing system library is not.

`architect/main.py:218-270` (`should_continue_retrying`) uses
`difflib.SequenceMatcher` with a hardcoded 0.6 threshold to detect stagnation by
comparing consecutive reflections' `root_cause` text. This misses semantically
equivalent errors with different wording and falsely flags different errors that
happen to use similar language.

### Changes
**File: `architect/main.py`**

1. Add a new LLM prompt constant `RETRY_DECISION_PROMPT` that receives:
   - The error type and message
   - All reflections so far (error_type, root_cause, what_to_try_next)
   - The attempt count and the hard ceiling (MAX_SPEC_REWRITES)
   - The step description
   And asks the LLM to return JSON: `{"continue": true/false, "reason": "..."}`

2. Modify `should_continue_retrying()`:
   - Keep the hard ceiling check (`spec_attempt >= MAX_SPEC_REWRITES`) as-is
   - Replace the `_text_similarity`-based stagnation check AND the
     `_ERROR_RETRY_BUDGETS` lookup with a single LLM call using the new prompt
   - On LLM failure, fall back to the existing heuristic logic (keep
     `_ERROR_RETRY_BUDGETS` and `_text_similarity` as fallback, but move them
     into a `_should_continue_retrying_heuristic()` helper)
   - Keep the "novel approach extension" logic as part of the fallback

3. Remove `_ERROR_RETRY_BUDGETS` from module-level (move into the fallback helper).

### Tests
- Update `tests/test_retry_budget.py` to mock the LLM call and verify:
  - LLM returning `{"continue": false}` stops retries
  - LLM returning `{"continue": true}` allows retries
  - LLM failure falls back to the heuristic
  - Hard ceiling is still respected regardless of LLM response
  - The existing stagnation and budget tests still pass against the fallback

---

## Section 2: LLM-Driven Orchestrator Retry Strategy (Replace Hardcoded Escalation)
- [x] **Status: Complete**

### Problem
`orchestrator/main.py:541-608` contains three hardcoded retry escalation tiers in
`build_prompt()`:
- Attempt 2: "diagnose root cause" with specific instructions
- Attempt 3: "fundamentally different approach" with specific instructions
- Attempt MAX_RETRIES: "simplest possible script" with specific instructions

This rigid ladder is suboptimal: sometimes a simple typo fix is all that's needed
on attempt 3, or simplification is the right move on attempt 2. The architect-level
`reflect_and_rewrite()` already lets the LLM freely choose strategy — the
orchestrator should do the same.

### Changes
**File: `orchestrator/main.py`**

1. Add a new LLM prompt constant `RETRY_STRATEGY_PROMPT` that receives:
   - The original task description
   - The attempt number
   - The previous code (truncated)
   - The error output
   - The full attempt_history (if available)
   And asks the LLM to return a focused retry instruction (2-3 sentences) that
   will be injected into the `<previous_error>` block.

2. Replace the three `if/elif/else` branches (attempt >= MAX_RETRIES,
   attempt > 2, else) with a single LLM call that generates the retry guidance.
   Wrap the generated guidance in the same `<previous_error>` XML tag structure.

3. On LLM failure, fall back to the existing three-tier logic (extract it into
   a helper `_hardcoded_retry_guidance(attempt, code_section, previous_error)`).

### Tests
- Update or add tests in `tests/` that mock the LLM and verify:
  - LLM-generated retry guidance is injected into the prompt
  - LLM failure falls back to the hardcoded tiers
  - The prompt still contains proper XML structure

---

## Section 3: LLM-Based Rewrite Quality Assessment (Replace _is_confused_output)
- [x] **Status: Complete**

### Problem
`architect/planner.py:976-984` (`_is_confused_output`) uses two crude heuristics
to detect confused LLM output:
1. Output > 3x the original description length (min 2000 chars)
2. Error text repeated verbatim (>200 chars) in the output

These miss many forms of low-quality output (contradictory instructions, irrelevant
tangents, copying the prompt back) and falsely flag legitimate long rewrites.

### Changes
**File: `architect/planner.py`**

1. Add a new LLM prompt constant `REWRITE_QUALITY_PROMPT` that receives:
   - The original step description
   - The rewritten description
   - The error that triggered the rewrite
   And asks the LLM to return JSON:
   `{"quality": "good"|"poor", "reason": "..."}` — checking for:
   - Does the rewrite address the root cause?
   - Is it actionable and specific?
   - Does it avoid repeating the error verbatim?
   - Is it a coherent task description (not an essay or analysis)?

2. Replace `_is_confused_output()` with `_check_rewrite_quality()` that:
   - Makes the LLM call
   - Returns `True` (confused) if quality is "poor"
   - Falls back to the existing heuristic on LLM failure

3. Update the call site in `reflect_and_rewrite()` (~line 1079) to use the
   new function.

### Tests
- Update `tests/test_rewrite.py` to verify:
  - LLM returning "poor" quality triggers resampling
  - LLM returning "good" quality does not trigger resampling
  - LLM failure falls back to heuristic
  - Low-confidence reflection still triggers resampling independently

---

## Section 4: LLM Pre-Flight Review (Enhance pre_execution_check)
- [x] **Status: Complete**

### Problem
`orchestrator/main.py:97-126` (`pre_execution_check`) only performs two checks:
1. Python syntax validity (via `compile()`)
2. Presence of `input()` calls

It misses semantically obvious failures like:
- Importing a package that is never pip-installed in the script
- Using a file path without `os.path.join(workspace, ...)`
- Missing the UAS_RESULT output entirely (currently just a warning)
- Obvious infinite loops or blocking operations

### Changes
**File: `orchestrator/main.py`**

1. Add a new LLM prompt constant `PRE_FLIGHT_PROMPT` that receives:
   - The generated code (truncated to ~8000 chars)
   - The task description
   And asks the LLM to return JSON:
   `{"issues": [{"description": "...", "severity": "critical"|"warning"}], "safe_to_run": true/false}`

2. Add a new function `pre_execution_check_llm(code, task)` that:
   - Calls the LLM with the pre-flight prompt
   - Returns (critical_errors, warnings) like the existing function
   - Falls back to the existing `pre_execution_check()` on LLM failure

3. Integrate into the orchestrator's main loop: call `pre_execution_check_llm()`
   when `UAS_MINIMAL` is not set, otherwise use the existing function.
   Gate behind a lightweight check: skip the LLM call if the existing
   `pre_execution_check()` already found critical errors.

### Tests
- Add `tests/test_pre_flight.py`:
  - LLM identifies missing pip install as critical
  - LLM identifies missing workspace path as warning
  - LLM failure falls back to syntax-only check
  - Existing syntax check still catches SyntaxError
  - UAS_MINIMAL mode skips the LLM call

---

## Section 5: Make LLM Guardrails Default (Remove Opt-In Gate)
- [x] **Status: Complete**

### Problem
`architect/main.py:1290-1354` (`check_guardrails_llm`) is gated behind the
`UAS_LLM_GUARDRAILS=1` environment variable. By default, only regex-based
guardrails run. The LLM guardrails catch more nuanced issues (SQL injection,
unsafe deserialization, missing input validation) but are opt-in, meaning most
runs miss these checks.

### Changes
**File: `architect/main.py`**

1. Find the call site where guardrails are invoked (search for
   `UAS_LLM_GUARDRAILS` usage). Change the default behavior:
   - LLM guardrails run by default
   - Add `UAS_NO_LLM_GUARDRAILS=1` as an opt-out for speed-sensitive runs
   - In `UAS_MINIMAL` mode, skip LLM guardrails (use regex only)

2. Update the docstring of `check_guardrails_llm()` to reflect the new default.

3. Always run regex guardrails first as a fast path. Only call LLM guardrails
   if regex found no "error"-severity violations (avoid redundant LLM calls
   when we already know the code has critical issues).

### Tests
- Update `tests/test_guardrails.py`:
  - Verify LLM guardrails run by default (mock the LLM call, assert it's called)
  - Verify `UAS_NO_LLM_GUARDRAILS=1` skips the LLM call
  - Verify `UAS_MINIMAL=1` skips the LLM call
  - Verify regex errors short-circuit the LLM call

---

## Section 6: LLM-Assessed Project Structure (Replace check_project_guardrails)
- [ ] **Status: Pending**

### Problem
`architect/main.py:1357-1423` (`check_project_guardrails`) uses a hardcoded
checklist: git repo, .gitignore, README, requirements.txt. This doesn't consider
the project type — a single-file data analysis script doesn't need a README, while
a web application might also need a Dockerfile or config files.

### Changes
**File: `architect/main.py`**

1. Add a new LLM prompt constant `PROJECT_STRUCTURE_PROMPT` that receives:
   - The original goal
   - The list of files in the workspace
   - The step summaries (what was accomplished)
   And asks the LLM to return JSON:
   `{"warnings": ["..."], "suggestions": ["..."]}` — assessing what project
   artifacts are missing given the specific project type.

2. Add a new function `check_project_guardrails_llm(workspace, goal, steps)` that:
   - Calls the LLM with the project structure prompt
   - Returns a list of warning strings (same interface as current function)
   - Falls back to `check_project_guardrails()` on LLM failure

3. Update `validate_workspace()` to call `check_project_guardrails_llm()` when
   available (not in MINIMAL mode), passing the goal and steps from state.

### Tests
- Update `tests/test_guardrails.py` or add new tests:
  - LLM correctly identifies missing artifacts for a multi-file project
  - LLM does not warn about README for a simple one-off script
  - LLM failure falls back to hardcoded checks
  - UAS_MINIMAL mode uses hardcoded checks

---

## Section 7: LLM Semantic Workspace Validation (Enhance validate_workspace)
- [ ] **Status: Pending**

### Problem
`architect/main.py:1467-1546` (`validate_workspace`) only checks whether claimed
files exist and whether the workspace is empty. It doesn't assess whether the
produced output actually satisfies the original goal. A workspace might have all
claimed files but contain incorrect or incomplete content.

### Changes
**File: `architect/main.py`**

1. Add a new LLM prompt constant `WORKSPACE_VALIDATION_PROMPT` that receives:
   - The original goal
   - The file listing with sizes
   - File content previews (first 200 chars of each text file, max 5 files)
   - Step summaries
   And asks the LLM to return JSON:
   `{"goal_satisfied": true/false, "confidence": "high"|"medium"|"low",
    "issues": ["..."], "summary": "..."}`

2. Add a function `validate_workspace_llm(state, workspace)` that:
   - Calls the LLM with workspace state
   - Appends findings to the validation report
   - Returns the existing validation_data dict augmented with LLM findings
   - Falls back to current behavior on LLM failure

3. Call this at the end of `validate_workspace()` when not in MINIMAL mode.
   Include the LLM's assessment in the validation report markdown.

### Tests
- Add tests:
  - LLM identifies goal not satisfied (files exist but wrong content)
  - LLM confirms goal satisfied
  - LLM failure doesn't break validation
  - Validation report includes LLM assessment when available

---

## Section 8: Smart Emergency Context Compression (Improve Tier 4)
- [ ] **Status: Pending**

### Problem
`architect/main.py:489-500` (Tier 4 emergency truncation) simply concatenates
the progress file with the tail of the context. This loses potentially critical
information from the beginning or middle of the context (e.g., early step outputs
that later steps depend on).

### Changes
**File: `architect/main.py`**

1. Add a new LLM prompt constant `EMERGENCY_COMPRESS_PROMPT` that receives:
   - The context (chunked to fit a reasonable LLM input)
   - The target length
   - The next step description
   And asks the LLM to extract only the information essential for the next step.

2. Modify the Tier 4 path in `compress_context()`:
   - Before doing head/tail truncation, attempt a fast LLM summarization
     with a hard timeout (e.g., 15 seconds)
   - The LLM receives the first 5000 chars + last 5000 chars of context
     (fitting within a reasonable prompt size)
   - If the LLM responds within the timeout, use its summary
   - Otherwise fall back to the existing head/tail truncation

3. Keep the existing truncation as the ultimate fallback.

### Tests
- Update `tests/test_context_compression.py`:
  - LLM emergency compression produces output under the limit
  - Timeout fallback uses head/tail truncation
  - LLM failure falls back to truncation
  - Progress file is still prioritized

---

## Section 9: Task-Aware Score Result (Replace Arbitrary Point Values)
- [ ] **Status: Pending**

### Problem
`orchestrator/main.py:646-669` (`score_result`) assigns arbitrary point values:
exit_code==0 gets +1000, UAS_RESULT gets +100, etc. These weights don't consider
the specific task. For a data pipeline, files_written matters most; for a
computation task, stdout content matters most.

### Changes
**File: `orchestrator/main.py`**

1. Add a `task` parameter to `score_result(result, task=None)`.

2. When task is provided and is not None, add task-aware bonus points:
   - Use a small LLM prompt `SCORE_GUIDANCE_PROMPT` that asks: "Given this task,
     what are the most important success signals? Return JSON:
     `{"priorities": ["files", "stdout_content", "exit_code", ...]}`"
   - Cache the result for the duration of the orchestrator run (one call per task)
   - Apply weighted bonuses based on priorities

3. On LLM failure or when task is None, use the existing static scoring.

4. Update the call site in `generate_and_vote()` to pass the task parameter.

### Tests
- Update `tests/test_voting.py`:
  - Task-aware scoring prioritizes files for file-creation tasks
  - Task-aware scoring prioritizes stdout for computation tasks
  - LLM failure falls back to static scoring
  - Cache prevents multiple LLM calls for same task

---

## Section 10: LLM-Adaptive Best-of-N Budget
- [ ] **Status: Pending**

### Problem
`orchestrator/main.py:631-643` (`_get_best_of_n`) uses a simple linear formula:
attempt 1 → N=1, attempt 2 → N=2, attempt 3 → N=3. This wastes resources on
trivial retries (where N=1 suffices because the fix is obvious) and under-samples
complex failures (where N=3 on attempt 2 would help).

### Changes
**File: `orchestrator/main.py`**

1. Add a new function `_get_best_of_n_llm(attempt, task, previous_error)`:
   - Makes a lightweight LLM call with the task description and error
   - Asks: "Should the system generate 1, 2, or 3 alternative solutions for
     this retry? Consider whether the error suggests a clear fix (N=1) or
     whether multiple diverse approaches would help (N=3)."
   - Returns the LLM's recommended N, capped by UAS_BEST_OF_N
   - Falls back to the linear formula on failure

2. Update the call site in the main orchestrator loop to pass the error context.

3. Gate behind `not MINIMAL_MODE` to avoid extra LLM calls in minimal mode.

### Tests
- Add tests:
  - LLM recommends N=1 for obvious fix → N=1
  - LLM recommends N=3 for complex error → N=3
  - LLM failure falls back to linear formula
  - Caps at UAS_BEST_OF_N regardless of LLM recommendation

---

## Section 11: LLM-Targeted Dependency Output Distillation
- [ ] **Status: Pending**

### Problem
`architect/main.py:503+` (`_distill_dependency_output`) uses a fixed template to
summarize dependency outputs for downstream steps. It includes file lists, stdout
preview, and UAS_RESULT, but doesn't consider what the consuming step actually
needs. A step that needs a specific CSV schema gets the same distillation as a
step that just needs to know a file exists.

### Changes
**File: `architect/main.py`**

1. Add a new LLM prompt constant `TARGETED_DISTILL_PROMPT` that receives:
   - The dependency step's output (files, stdout, UAS_RESULT)
   - The consuming step's description
   And asks the LLM to extract only the information the consuming step needs
   (e.g., file paths, schemas, API responses, configuration values).

2. Add `_distill_dependency_output_llm(dep_id, dep_step, output, consumer_desc)`:
   - Calls the LLM with the targeted distillation prompt
   - Returns a focused distillation string
   - Falls back to `_distill_dependency_output()` on failure

3. Update `build_context()` to use the LLM-targeted version when not in
   MINIMAL mode, passing the current step's description as consumer context.

### Tests
- Add tests:
  - LLM extracts CSV schema when consumer step needs to "analyze data"
  - LLM extracts file path when consumer step needs to "read the output"
  - LLM failure falls back to template distillation
  - Output is more concise than template distillation

---

## Section 12: Post-Run LLM Meta-Learning
- [ ] **Status: Pending**

### Problem
The knowledge base (`state["knowledge"]`) currently only captures:
1. Package versions from pip install output (regex extraction)
2. Lessons from `reflect_and_rewrite` (per-step, per-failure)

It doesn't perform a holistic post-run analysis to identify systemic patterns
(e.g., "this type of goal consistently fails at the API integration step" or
"the decomposition tends to produce too many steps for simple goals").

### Changes
**File: `architect/main.py`**

1. Add a new LLM prompt constant `META_LEARNING_PROMPT` that receives:
   - The original goal
   - All step outcomes (title, status, attempt count, error types)
   - Total run time
   - Any replanning events
   And asks the LLM to return JSON:
   `{"systemic_lessons": [{"pattern": "...", "recommendation": "..."}],
    "decomposition_feedback": "...",
    "knowledge_to_persist": [{"key": "...", "value": "..."}]}`

2. Add a function `post_run_meta_learning(state)` that:
   - Calls the LLM after all steps complete (or fail)
   - Appends systemic lessons to the scratchpad
   - Persists relevant knowledge via `append_knowledge()`
   - Logs findings via the event log

3. Call this at the end of the main loop, after `validate_workspace()` but
   before report generation. Gate behind `not MINIMAL_MODE`.

### Tests
- Add tests:
  - LLM identifies a systemic pattern from run data
  - Lessons are persisted to the knowledge base
  - LLM failure doesn't break the run
  - MINIMAL mode skips meta-learning
