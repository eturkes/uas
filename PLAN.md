# UAS Enhancement Plan: Maximizing Computational Power and Reasoning

This plan identifies concrete improvements to maximize UAS's problem-solving ability for long-running, complex, multi-step tasks. Changes are ordered by impact and organized into sequential implementation sections, each completable in a single Claude Code session.

**Guiding principles from research:**
- Execution feedback beats self-critique (test results > introspection)
- Simplicity wins (Mini-SWE-Agent with ~100 LoC matches complex frameworks)
- Better problem specification has the highest impact on task completion
- Persistent reflection memory yields consistent gains across benchmarks
- Context engineering (not raw context size) determines long-run success

---

## Section 1: Prompt Architecture Overhaul [DONE]

**Rationale:** Claude best practices show that prompt structure alone can improve response quality by up to 30%. The current prompts put instructions before data — Claude performs better with data first, instructions last. Additionally, adding structured thinking sections and workspace-aware context dramatically improves code correctness.

**Files to modify:**
- `orchestrator/main.py` — `build_prompt()`
- `orchestrator/claude_config.py` — `CLAUDE_MD_TEMPLATE`
- `architect/planner.py` — `DECOMPOSITION_PROMPT`, `CRITIQUE_PROMPT`, `REFLECT_PROMPT`, `DECOMPOSE_STEP_PROMPT`

**Changes:**

### 1a. Restructure orchestrator code-gen prompt (`build_prompt`)
- Move `<environment>` and `<task>` (data) to the top of the prompt
- Move `<role>`, `<constraints>`, and `<verification>` (instructions) to the bottom
- Add `<workspace_state>` section listing existing files in the workspace so the LLM knows what's already been created by prior steps
- Add `<analysis>` instruction: require the LLM to analyze the task in `<analysis>` tags before writing code (structured thinking)
- Pass workspace file listing via env var or parameter from the architect

### 1b. Restructure decomposition prompt (`DECOMPOSITION_PROMPT`)
- Move the goal to the top as data, rules and output format to the bottom
- Add a `<complexity_assessment>` tag: instruct the LLM to explicitly estimate complexity (trivial/simple/medium/complex) and justify the number of steps chosen
- Strengthen the `<analysis>` section to require identifying: key sub-problems, risk areas, parallelization opportunities, and likely failure modes
- Add an `<anti_patterns>` section listing common decomposition mistakes (over-splitting trivial tasks, under-splitting complex ones, missing dependencies)

### 1c. Restructure reflect/rewrite prompts
- Move failure output data to the top, diagnosis instructions to the bottom
- Add `<previous_attempts>` section that includes a concise summary of ALL prior attempts for this step (not just the latest), so the LLM can see the full history and avoid repeating failed strategies
- Add `<counterfactual>` instruction: "Before proposing a fix, reason about whether the root cause is in this step or propagated from a dependency step"

### 1d. Enhance workspace CLAUDE.md
- Add section on the current task's context (step number, total steps, what prior steps produced)
- Make it dynamic per-step rather than static boilerplate (the executor already rewrites it per invocation — leverage this to include step-specific context)

---

## Section 2: Self-Consistency Planning (Multi-Plan Voting) [DONE]

**Rationale:** Self-consistency (generating N plans and voting) is one of the most reliably effective techniques across benchmarks. Confidence-weighted voting achieves the same accuracy with 46% fewer samples. For UAS, this means the decomposition phase produces better plans, especially for complex goals.

**Files to modify:**
- `architect/planner.py` — new function `decompose_goal_with_voting()`
- `architect/main.py` — call site in `main()`

**Changes:**

### 2a. Multi-plan generation
- Add `decompose_goal_with_voting(goal, n_samples=3)` that calls `decompose_goal` N times (in parallel using ThreadPoolExecutor)
- Each call uses a slightly different temperature hint (append "Approach this from a different angle" to 2nd and 3rd calls, or use `--model` with different seeds if available)
- For trivial goals (estimated <3 steps by the first plan), skip voting and use the single plan

### 2b. Plan scoring and selection
- Score each plan on: number of steps (prefer fewer), parallelizability (prefer more parallel levels), step description specificity (prefer longer, more detailed descriptions), and structural validity
- Implement a simple scoring function: `score = parallelism_ratio * 0.4 + specificity * 0.3 + compactness * 0.3` where parallelism_ratio = parallel_steps/total_steps, specificity = avg description length (capped), compactness = 1/num_steps (normalized)
- Select the highest-scoring plan
- Log the scoring to the event log for post-hoc analysis

### 2c. Complexity estimation gate
- Before running full voting, make a single quick LLM call: "Rate the complexity of this goal: trivial (1 step), simple (2-3), medium (4-7), complex (8+). Respond with just the category."
- If trivial/simple: use single decomposition (no voting overhead)
- If medium/complex: use 3-plan voting
- Store the estimated complexity in state for downstream use

---

## Section 3: Reflexion-Based Error Recovery [DONE]

**Rationale:** Reflexion with persistent memory raises HumanEval from 76.4% to 82.6% in multi-agent settings. The current system's scratchpad partially implements this, but lacks structured reflection and adaptive retry budgets. This section upgrades error recovery from "try harder" to "learn from failure."

**Files to modify:**
- `architect/planner.py` — `reflect_and_rewrite()`, new `generate_reflection()`
- `architect/main.py` — `execute_step()` retry logic
- `architect/state.py` — new reflection storage

**Changes:**

### 3a. Structured reflection memory
- Add a `reflections` list to each step in the state: `[{"attempt": N, "error_type": str, "root_cause": str, "strategy_tried": str, "lesson": str}]`
- After each failure, generate a structured reflection via LLM: "Given this failure, fill in: error_type, root_cause, what_was_tried, lesson_learned, what_to_try_next"
- Pass ALL accumulated reflections for the step into subsequent rewrite prompts as a `<reflection_history>` section
- Also write reflections to the global scratchpad so other steps can learn from them

### 3b. Error-type-adaptive retry budgets
- Classify each error using the existing `classify_failure()` from explain.py (move it to a shared location)
- Adjust retry strategy based on error type:
  - `dependency_error`: 1 retry (just add the missing package), then escalate
  - `logic_error`: full 4 retries (the code needs iterative fixing)
  - `environment_error`: 1 retry with diagnostic probe, then escalate
  - `network_error`: 2 retries with wait (may be transient)
  - `timeout`: 0 retries, immediately decompose into sub-steps
  - `format_error`: 2 retries (clarify output format)
- Don't reduce MAX_SPEC_REWRITES, but change the escalation strategy based on error type

### 3c. Counterfactual root cause tracing
- When a step fails and it has dependencies, before rewriting, ask: "Could this error be caused by incorrect output from a dependency step? If step N produced file X, and this step reads file X, is file X correct?"
- If the LLM identifies a dependency as the root cause, mark the dependency for re-execution instead of rewriting the current step
- Implement `trace_root_cause(step, error, completed_outputs)` that returns either "self" (rewrite this step) or a dependency step ID (re-execute that step)

### 3d. Backtracking support
- When root cause tracing identifies a dependency step as the problem, re-execute that step with a corrected description, then re-execute the current step
- Limit backtracking depth to 1 (only backtrack to immediate dependencies, not transitively) to avoid infinite loops
- Track which steps have been backtracked to avoid re-backtracking to the same step

---

## Section 4: Context Engineering [DONE]

**Rationale:** Anthropic's research shows context engineering (not raw size) determines agent success in long-running tasks. The current scratchpad is flat text with simple truncation. Upgrading to structured, hierarchical context with smart compression enables 100+ step workflows.

**Files to modify:**
- `architect/main.py` — `build_context()`, `summarize_context()`
- `architect/state.py` — scratchpad upgrade
- `architect/executor.py` — `scan_workspace_files()`, workspace context passing

**Changes:**

### 4a. Structured progress file
- Replace the flat scratchpad with a structured progress file (`.state/progress.md`) that has sections:
  ```
  ## Current State
  - Steps completed: N/M
  - Current step: title
  - Known blockers: ...

  ## Key Decisions
  - [timestamp] Decision: rationale

  ## Completed Steps
  - Step 1 (title): summary, files: [list], time: Xs
  - Step 2 (title): summary, files: [list], time: Xs

  ## Lessons Learned
  - [timestamp] lesson from step X failure
  - [timestamp] environment discovery
  ```
- Update after every step completion/failure
- Include in context for all subsequent steps (replaces raw scratchpad)

### 4b. Recursive workspace scanning
- Upgrade `scan_workspace_files()` to scan recursively (up to 3 levels deep) instead of just top-level
- Skip `.state/`, `.git/`, `__pycache__/`, `node_modules/`, `venv/` directories
- For each file, include: path, size, type, and preview (first 200 chars for text files)
- Group files by directory in the output for readability
- Cap total scan output at 4000 chars to avoid context bloat

### 4c. Tiered context compression
- Replace the single `summarize_context()` with a tiered approach:
  1. **Tier 1** (context < 60% of limit): No compression, include everything
  2. **Tier 2** (60-80%): Remove file previews, keep only file names and sizes; truncate stdout to last 500 chars per step
  3. **Tier 3** (80-100%): Summarize all dependency outputs into a single paragraph per step using LLM; keep only the structured progress file verbatim
  4. **Tier 4** (>100%): Emergency truncation — progress file + last step output only
- Implement as `compress_context(context, max_length)` with deterministic tier selection (no LLM call for tiers 1-2, LLM call only for tier 3)

### 4d. Dependency output distillation
- When building context from completed dependency steps, instead of passing raw stdout/stderr, distill each dependency's output into a structured summary:
  ```xml
  <dependency step="N" title="...">
    <files_produced>file1.txt (1234 bytes), file2.json (5678 bytes)</files_produced>
    <key_outputs>summary of what was produced</key_outputs>
    <relevant_data>any data the current step needs to reference</relevant_data>
  </dependency>
  ```
- Use the step's `summary` field (from UAS_RESULT) as the primary output description
- Only include raw stdout as fallback when no structured summary is available

---

## Section 5: Claude CLI Optimization [DONE]

**Rationale:** The LLM client currently uses `claude -p` with basic text output. Using JSON output mode enables cleaner parsing, and passing workspace context improves code quality. Model selection allows using cheaper/faster models for simple sub-tasks.

**Files to modify:**
- `orchestrator/llm_client.py` — `ClaudeCodeClient`
- `orchestrator/main.py` — `build_prompt()`, `main()`
- `orchestrator/parser.py` — `extract_code()`

**Changes:**

### 5a. JSON output mode
- Add `--output-format json` flag to the Claude CLI invocation
- Parse the JSON response to extract the `result` field cleanly
- Fall back to text mode parsing if JSON parsing fails (backwards compatibility)
- This eliminates the fragile regex-based code extraction in parser.py for the primary path

### 5b. Workspace-aware code generation
- Before calling the LLM for code generation, scan the workspace and include a `<workspace_files>` section in the prompt listing existing files (name, size, type)
- This prevents the LLM from regenerating files that already exist and helps it reference them correctly
- Pass workspace path as an env var to the orchestrator so it can scan

### 5c. Model tiering (optional, env-var controlled)
- Add `UAS_MODEL_PLANNER` and `UAS_MODEL_CODER` env vars
- Use the planner model (default: same as UAS_MODEL) for decomposition, critique, and reflection
- Use the coder model (default: same as UAS_MODEL) for code generation
- This allows using a more powerful model for planning and a faster model for code generation (or vice versa)

### 5d. Improved error output parsing
- Currently errors are extracted via regex patterns (`_STDOUT_PATTERN`, `_STDERR_PATTERN`). These are fragile.
- Restructure the orchestrator's output format to use clear delimiters:
  ```
  ===STDOUT_START===
  ...
  ===STDOUT_END===
  ===STDERR_START===
  ...
  ===STDERR_END===
  ```
- Parse these delimiters in the architect's executor, falling back to regex for backwards compatibility

---

## Section 6: Dynamic Mid-Execution Re-planning [DONE]

**Rationale:** The current system executes the initial plan rigidly. If a step produces unexpected output that invalidates later steps, the system doesn't adapt — it either fails later or produces wrong results. Dynamic re-planning catches this.

**Files to modify:**
- `architect/main.py` — new `should_replan()`, modify execution loop
- `architect/planner.py` — new `replan_remaining_steps()`

**Changes:**

### 6a. Post-step plan validation
- After each step completes, check if the result matches what downstream steps expect:
  - Parse UAS_RESULT and compare files_written against what dependent steps reference
  - If the step produced fewer/different files than expected, flag for re-planning
- Implement `should_replan(step, result, remaining_steps)` that returns True if remaining steps need adjustment

### 6b. Incremental re-planning
- When re-planning is triggered, don't re-decompose from scratch — instead:
  1. Provide the LLM with: original goal, completed steps + their outputs, the failing/unexpected result, remaining steps
  2. Ask: "Given what's been accomplished so far and this result, adjust the remaining steps. Keep completed steps, modify pending ones."
  3. Replace pending steps in the state with the new plan
  4. Re-validate dependencies and topological sort
- Limit re-planning to once per execution level to avoid infinite adjustment loops

### 6c. Step description enrichment
- After each step completes, use its output to enrich the descriptions of dependent steps
- Example: if step 1 writes `data.csv` with columns `[name, price, date]`, append to step 2's description: "The input file data.csv has columns: name, price, date"
- This is a lightweight form of re-planning that improves downstream success without a full re-plan LLM call

---

## Section 7: Best-of-N Code Generation with Execution Voting

**Rationale:** The S* framework shows that parallel sampling + sequential refinement with execution feedback dramatically improves code correctness. GPT-4o-mini with S* outperforms o1-preview by 3.7% on LiveCodeBench. This is the highest-impact test-time compute scaling technique.

**Files to modify:**
- `orchestrator/main.py` — `main()` loop, new `generate_and_vote()`
- `orchestrator/sandbox.py` — (no changes needed, already supports multiple runs)

**Changes:**

### 7a. Parallel code generation
- Add `UAS_BEST_OF_N` env var (default: 1, recommended: 2-3 for complex tasks)
- When N>1, generate N code samples from the same prompt (in parallel using ThreadPoolExecutor)
- Each sample uses slightly different prompt variations (append "Approach A: prioritize simplicity" / "Approach B: prioritize robustness" / "Approach C: prioritize efficiency")

### 7b. Execution-based selection
- Execute all N samples in the sandbox
- If exactly one succeeds (exit code 0): use it
- If multiple succeed: prefer the one with the most informative UAS_RESULT (has `files_written`, has `summary`)
- If none succeed: fall back to the current single-sample retry loop with the best-performing sample (least severe error)

### 7c. Budget-aware gating
- Only use best-of-N on retry attempts (first attempt is always single-sample)
- If the step has already failed once, increase N to 2 for the next attempt
- If the step has failed twice, increase N to 3
- This allocates compute budget where it's most needed (harder problems)

---

## Implementation Notes

- Each section is designed to be implementable in a single Claude Code session
- Sections 1-4 are the highest priority — they address the biggest gaps
- Sections 5-7 are valuable but more invasive — implement after 1-4 are stable
- All changes should maintain backwards compatibility (new features gated behind env vars)
- Run `python3 -m pytest tests/` after each section to ensure nothing breaks
- The existing test suite covers the core paths; new tests should be added for new functionality

## Risk Assessment

| Section | Risk | Mitigation |
|---------|------|------------|
| 1. Prompt restructuring | Low — prompt changes only | A/B test with existing eval suite |
| 2. Multi-plan voting | Medium — adds LLM calls | Gate behind complexity check |
| 3. Reflexion recovery | Medium — changes retry logic | Preserve existing escalation as fallback |
| 4. Context engineering | Low — additive changes | Tiered compression is deterministic |
| 5. CLI optimization | Medium — changes CLI interface | Fall back to text mode on error |
| 6. Dynamic re-planning | High — changes execution loop | Limit to 1 re-plan per level |
| 7. Best-of-N | Medium — multiplies LLM calls | Gate behind retry count |
