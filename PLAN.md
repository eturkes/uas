# PLAN.md - Replacing Hardcoded Decision Logic with LLM-Steered Prompt Engineering

## Assessment

UAS has a **well-designed two-layer architecture** where the Architect and Orchestrator
already delegate the core creative work (goal decomposition, code generation, error
reflection, re-planning) to an LLM. However, several intermediate decision points use
**hardcoded heuristics** where an LLM call—or better prompt engineering on existing
calls—would produce more adaptive, context-sensitive behavior. The key areas:

### What's Already Good (LLM-Driven)
- Goal decomposition via `DECOMPOSITION_PROMPT`
- Complexity estimation via `COMPLEXITY_PROMPT`
- Error reflection via `REFLECT_PROMPT` / `REFLECTION_GEN_PROMPT`
- Root cause tracing via `ROOT_CAUSE_PROMPT`
- Re-planning via `REPLAN_PROMPT`
- Plan critique via `CRITIQUE_PROMPT`
- Code generation via `build_prompt()`

### What's Hardcoded (Should Be LLM-Steered)

| Area | File:Lines | Current Approach | Problem |
|------|-----------|-----------------|---------|
| **Plan scoring/voting** | `planner.py:337-367` | Weighted formula: `parallelism*0.4 + specificity*0.3 + compactness*0.3` | Arbitrary weights; doesn't consider task semantics, correctness, or completeness |
| **Step merging** | `planner.py:910-1014` | Pairs steps with `desc < 200 chars` in same level | Doesn't understand semantic relatedness; char-count is a poor proxy for complexity |
| **Failure classification** | `explain.py:17-55` | Keyword matching against `_FAILURE_PATTERNS` dict | Misses nuanced errors; can't handle multi-category or novel errors |
| **Error retry budgets** | `main.py:59-71` | Static dict mapping error type to max retries | Ignores whether progress was made, or whether the error is actually recoverable |
| **Context compression tiers** | `main.py:185-247` | Fixed ratio thresholds (0.6, 0.8, 1.0) with regex stripping | Doesn't know which context is most relevant to the current step |
| **Re-plan trigger** | `main.py:300-386` | Regex file-reference matching in step descriptions | Fragile; misses semantic mismatches (e.g. wrong data format, incomplete output) |
| **Best-of-N scoring** | `orchestrator/main.py:290-313` | Hardcoded point system (exit=1000, UAS_RESULT=100, etc.) | Doesn't evaluate code quality, correctness approach, or match to task |
| **Dependency distillation** | `main.py:250-297` | Fixed XML template with truncation at char limits | Doesn't know what downstream steps actually need from this dependency |
| **Guardrail checks** | `main.py:676-713` | Regex pattern matching for security violations | Can't catch semantic security issues; high false-positive rate |
| **Rewrite escalation strategy** | `main.py:1262-1268, 1397-1421` | Hardcoded sequence: reflect → alternative → decompose → defensive | Doesn't adapt to what actually went wrong; the same escalation for all error types |
| **Step enrichment** | `planner.py:1183-1230` | String concatenation of file lists and summaries | Doesn't prioritize what's most useful for the downstream step |
| **Confused output detection** | `planner.py:804-812` | Length heuristic (>3x original) and error-in-output check | Can't detect semantic confusion like hallucinated approaches |

### Design Principles for Refactoring

1. **Fold decisions into existing LLM calls** where possible, rather than adding new ones
2. **Use structured output formats** (JSON) to extract decisions from LLM reasoning
3. **Keep hardcoded logic only for** pure algorithms (topological sort, DAG validation),
   output parsing, and safety-critical bounds (max retries, timeouts)
4. **Preserve fallbacks** so that LLM parse failures degrade to the current heuristics

---

## Section 1: Plan Voting — Replace `score_plan()` with LLM-Based Selection
**Status: Complete**

### Problem
`planner.py:score_plan()` uses a rigid formula (`parallelism*0.4 + specificity*0.3 +
compactness*0.3`) that doesn't evaluate whether a plan is actually correct, complete,
or well-suited to the specific goal. A plan with 2 highly parallel but wrong steps
scores better than 5 correct sequential ones.

### Changes

**File: `architect/planner.py`**

1. Create a new `PLAN_SELECTION_PROMPT` that presents all candidate plans to the LLM
   and asks it to select the best one. The prompt should:
   - Include the original goal
   - Present each plan as a numbered option with its full step list
   - Ask the LLM to reason about correctness, completeness, parallelism potential,
     and risk before selecting
   - Return a structured JSON response: `{"selected_plan": <index>, "reasoning": "..."}`

2. Create a new function `select_best_plan(goal, plans)` that:
   - Formats all plans into the selection prompt
   - Calls the LLM once
   - Parses the response to get the selected plan index
   - Falls back to `score_plan()` if parsing fails

3. In `decompose_goal_with_voting()`, replace the `scored = [(score_plan(p), ...)]`
   block with a call to `select_best_plan(goal, plans)`.

4. Keep `score_plan()` as the fallback only — do not delete it.

### Testing
- Run `pytest tests/test_planner.py` to ensure decomposition still works
- Run `python -m pytest tests/ -k voting` if any voting tests exist

---

## Section 2: Step Merging — Replace Heuristic with LLM Judgment
**Status: Complete**

### Problem
`planner.py:merge_trivial_steps()` merges steps purely based on description length
(< 200 chars) and topological level, without understanding whether the steps are
semantically related or whether merging them makes the combined task harder.

### Changes

**File: `architect/planner.py`**

1. Create a `MERGE_EVALUATION_PROMPT` that:
   - Takes the goal and the full step list
   - Asks the LLM which steps in the same execution level could be safely combined
   - Requires reasoning about semantic relatedness and combined complexity
   - Returns JSON: `{"merges": [[step_a, step_b], ...], "reasoning": "..."}`

2. Create function `merge_steps_with_llm(goal, steps)` that:
   - Computes execution levels via `topological_sort()`
   - Only submits levels with 2+ steps to the LLM for merge evaluation
   - Parses the response and performs the merges
   - Falls back to `merge_trivial_steps()` on parse failure

3. In `main()` (architect/main.py), replace the call to `merge_trivial_steps(steps)`
   with `merge_steps_with_llm(goal, steps)`.

### Testing
- Run `pytest tests/` to verify no regressions

---

## Section 3: Failure Classification — Fold into Reflection Prompt
**Status: Complete**

### Problem
`explain.py:classify_failure()` uses keyword matching against a static dictionary.
Meanwhile, `generate_reflection()` already asks the LLM to classify the error type
in its structured JSON response. The hardcoded classifier is redundant and less
accurate.

### Changes

**File: `architect/explain.py`**

1. Rename `classify_failure()` to `classify_failure_heuristic()` (keep as fallback).

2. Create `classify_failure(error_text, step_context=None)` that:
   - If `step_context` is provided and has `reflections`, use the most recent
     reflection's `error_type` field (already LLM-generated)
   - Otherwise, fall back to `classify_failure_heuristic()`

**File: `architect/main.py`**

3. At `main.py:1222`, where `classify_failure(error_info)` is called, pass the
   step as context: `classify_failure(error_info, step_context=step)` so it can
   use the reflection that was just generated a few lines later.

4. Reorder the logic slightly: move the `generate_reflection()` call (lines 1226-1242)
   to happen **before** the `classify_failure()` call (line 1222), so the reflection's
   `error_type` is available for classification.

**File: `architect/planner.py`**

5. In `REFLECTION_GEN_PROMPT`, add a note reinforcing that the LLM's `error_type`
   classification should be one of the canonical types: `dependency_error`,
   `logic_error`, `environment_error`, `network_error`, `timeout`, `format_error`,
   `unknown`.

### Testing
- Run `pytest tests/test_explain.py` if it exists
- Run `pytest tests/` for full regression check

---

## Section 4: Error Retry Budgets — Make Adaptive via Reflection
**Status: Complete**

### Problem
`main.py:_ERROR_RETRY_BUDGETS` is a static dict that maps error types to max retries.
This ignores whether progress was made between attempts. A "logic_error" that produces
progressively better output deserves more retries than one that repeats the same mistake.

### Changes

**File: `architect/main.py`**

1. Create function `should_continue_retrying(step, spec_attempt, error_type, reflections)`
   that:
   - Checks if the last 2 reflections show the same `root_cause` and `error_type`
     (stagnation → stop retrying)
   - Checks if `what_to_try_next` from the last reflection is substantively different
     from previous strategies (progress → continue)
   - Uses `_ERROR_RETRY_BUDGETS` as the upper bound but allows early termination
     or extension based on reflection quality
   - Returns `(should_continue: bool, reason: str)`

2. At `main.py:1276-1293`, replace the static budget check with a call to
   `should_continue_retrying()`. The static budgets remain as hard ceilings.

3. This is NOT an LLM call — it's using the LLM's prior reflection output
   (already generated) to make a smarter decision. No new API calls needed.

### Testing
- Run `pytest tests/` for regression check

---

## Section 5: Context Compression — LLM-Guided Relevance Filtering
**Status: Complete**

### Problem
`main.py:compress_context()` uses fixed ratio thresholds and regex-based stripping.
It doesn't know which parts of the context are most relevant to the current step.

### Changes

**File: `architect/main.py`**

1. Modify the Tier 2 compression to be smarter: instead of regex-stripping previews,
   ask the existing `summarize_context()` LLM call (Tier 3) to do the work earlier.
   Merge Tiers 2 and 3 into a single approach:
   - If ratio > 0.6, call `summarize_context()` with an enhanced prompt that includes
     the current step's description, so the LLM knows what to preserve
   - Fall back to regex stripping if LLM call fails

2. Update `summarize_context()` to accept a `current_step_description` parameter and
   include it in the prompt: "The next step that will consume this context is: {desc}.
   Prioritize preserving information relevant to that step."

3. Keep Tier 4 (emergency truncation) as-is — it's a safety net.

### Testing
- Run `pytest tests/` for regression check

---

## Section 6: Re-plan Trigger — Semantic Mismatch Detection via LLM
**Status: Pending**

### Problem
`main.py:should_replan()` uses regex to find file references in step descriptions and
compare them against actual files. This misses semantic mismatches (wrong data format,
incomplete data, unexpected structure) and has high false-positive/negative rates.

### Changes

**File: `architect/main.py`**

1. Create `REPLAN_CHECK_PROMPT` that:
   - Shows the completed step's actual output (files, summary, UAS_RESULT)
   - Shows the descriptions of dependent steps
   - Asks: "Do the remaining steps need adjustment based on this step's actual output?"
   - Returns JSON: `{"needs_replan": true/false, "reason": "..."}`

2. Create function `should_replan_llm(step, remaining_steps, state)` that:
   - Formats the prompt
   - Calls the LLM
   - Parses the response
   - Falls back to the current regex-based `should_replan()` on failure

3. In the main execution loop, replace `should_replan()` calls with
   `should_replan_llm()`.

4. Rename the old function to `should_replan_heuristic()` and keep as fallback.

### Testing
- Run `pytest tests/` for regression check

---

## Section 7: Best-of-N Scoring — LLM-Based Code Evaluation
**Status: Pending**

### Problem
`orchestrator/main.py:score_result()` uses a hardcoded point system that doesn't
evaluate code quality, approach correctness, or how well the output matches the task.

### Changes

**File: `orchestrator/main.py`**

1. Create `CODE_EVALUATION_PROMPT` that:
   - Shows the original task
   - Shows each candidate's code and execution result (exit code, stdout, stderr)
   - Asks the LLM to rank them by correctness, robustness, and output quality
   - Returns JSON: `{"ranking": [candidate_indices], "reasoning": "..."}`

2. Create function `evaluate_candidates(task, candidates)` that:
   - Only invoked when N > 1 (best-of-N is active)
   - Formats the evaluation prompt
   - Calls the LLM
   - Falls back to `score_result()` ranking on parse failure

3. In `generate_and_vote()`, replace the `scored.sort(key=...)` block with
   a call to `evaluate_candidates()` when viable.

4. Keep `score_result()` as fallback.

### Notes
- This adds one LLM call per best-of-N round but only when N > 1 (retries only).
  The cost is bounded and the quality improvement is significant.

### Testing
- Run `pytest tests/` for regression check

---

## Section 8: Dependency Distillation — Step-Aware Summarization
**Status: Pending**

### Problem
`main.py:_distill_dependency_output()` builds a fixed XML template with arbitrary
truncation limits. It doesn't know what the downstream step actually needs.

### Changes

**File: `architect/main.py`**

1. Create `DISTILL_PROMPT` template:
   ```
   The following step just completed:
   Step {dep_id} ({title}): {summary}
   Files: {files}
   Output: {output_preview}

   The next step that will use this output:
   Step {next_id} ({next_title}): {next_description}

   Summarize ONLY the information from the completed step that is relevant
   to the next step. Be concise. Include file paths and key data.
   ```

2. Create function `distill_dependency_for_step(dep_step, next_step)` that:
   - Calls the LLM with the distill prompt
   - Returns the distilled summary
   - Falls back to `_distill_dependency_output()` on failure

3. In `build_context()`, when building dependency context for a step, use
   `distill_dependency_for_step()` instead of `_distill_dependency_output()`.

### Notes
- This adds one LLM call per dependency per step. For large DAGs this could be
  expensive. Gate it behind `UAS_SMART_DISTILL=1` env var, with the old function
  as default.

### Testing
- Run `pytest tests/` for regression check

---

## Section 9: Guardrail Checks — Supplement Regex with LLM Review
**Status: Pending**

### Problem
`main.py:check_guardrails()` uses regex patterns that can't catch semantic security
issues (e.g., building SQL from user input, logging secrets indirectly, using
`pickle.loads` on data from a network request).

### Changes

**File: `architect/main.py`**

1. Create `GUARDRAIL_REVIEW_PROMPT`:
   ```
   Review this Python script for security and best-practice violations:
   ```python
   {code}
   ```

   Check for:
   - Hardcoded secrets, API keys, tokens
   - SQL injection, command injection
   - Unsafe deserialization (pickle, yaml.load without SafeLoader)
   - Use of eval/exec on untrusted data
   - Plain HTTP URLs (should be HTTPS)
   - Missing input validation on external data
   - Bare except clauses

   Return JSON: {"violations": [{"line": N, "description": "...", "severity": "error|warning"}], "clean": true/false}
   ```

2. Create function `check_guardrails_llm(code)` that:
   - Calls the LLM with the review prompt
   - Parses the violations
   - Falls back to `check_guardrails()` regex on failure

3. In `execute_step()`, replace `check_guardrails(code_content)` with
   `check_guardrails_llm(code_content)`. Keep the regex version as fallback.

### Notes
- Only invoked once per successful step (on workspace Python files), so cost is bounded.
- Gate behind `UAS_LLM_GUARDRAILS=1` env var to keep regex as default for speed.

### Testing
- Run `pytest tests/` for regression check

---

## Section 10: Rewrite Escalation — Dynamic Strategy Selection
**Status: Pending**

### Problem
`main.py:1262-1268` uses a hardcoded escalation sequence
(reflect → alternative → decompose → defensive) regardless of error type. And
`main.py:1397-1421` hardcodes which spec_attempt triggers which rewrite style.

### Changes

**File: `architect/main.py`**

1. Fold the escalation strategy selection into the reflection prompt. After
   `generate_reflection()` returns, use its `what_to_try_next` field to decide
   the rewrite approach rather than the hardcoded `spec_attempt` number.

2. Modify the rewrite dispatch block (lines 1408-1421):
   - Instead of `if spec_attempt == 2: decompose` / `else: reflect_and_rewrite`,
     check the reflection's `what_to_try_next` for keywords like "decompose",
     "simplify", "different approach", etc.
   - If the reflection suggests decomposition, call `decompose_failing_step()`
   - If it suggests a fundamentally different approach, use escalation_level=1
   - Otherwise, use standard reflection-based rewrite
   - Fall back to the current hardcoded sequence if keyword matching is ambiguous

3. Update `REFLECTION_GEN_PROMPT` to include a `"recommended_strategy"` field
   in the JSON output, with explicit options:
   `"one of: reflect_and_fix, alternative_approach, decompose_into_phases, defensive_rewrite"`

**File: `architect/planner.py`**

4. Add `recommended_strategy` to the `REFLECTION_GEN_PROMPT` JSON schema.

### Testing
- Run `pytest tests/` for regression check

---

## Section 11: Step Enrichment — LLM-Guided Context Injection
**Status: Pending**

### Problem
`planner.py:enrich_step_descriptions()` blindly appends file lists and summaries to
downstream step descriptions. It doesn't know which information is actually useful
to each downstream step.

### Changes

**File: `architect/planner.py`**

1. Fold enrichment into the existing context-building flow rather than modifying
   step descriptions directly. Instead of mutating `step["description"]`:
   - Build an `enrichment_context` dict mapping step_id to relevant context strings
   - Pass this through `build_context()` rather than appending to descriptions

2. This is a structural change, not an LLM call. The benefit is that context can
   be filtered/compressed by the existing compression logic rather than being
   permanently baked into descriptions that get passed to every downstream call.

3. In `build_context()`, after building dependency distillations, append any
   enrichment context for the current step.

4. Remove the direct string mutation in `enrich_step_descriptions()`.

### Testing
- Run `pytest tests/` for regression check

---

## Section 12: Confused Output Detection — Enhance Reflection Prompt
**Status: Pending**

### Problem
`planner.py:_is_confused_output()` uses length and substring heuristics that can't
detect semantic confusion (hallucinated approaches, contradictory instructions, etc.).

### Changes

**File: `architect/planner.py`**

1. Rather than a separate detection function, add a self-check instruction to
   `REFLECT_PROMPT`:
   ```
   After writing the improved task description, verify your output:
   - Is it a valid, actionable task description (not an error analysis)?
   - Is it similar in scope to the original task (not vastly longer or shorter)?
   - Does it avoid repeating the error output verbatim?
   If your output fails these checks, revise it before responding.
   ```

2. Keep `_is_confused_output()` as a lightweight post-check, but only for
   triggering a resample — not as the primary quality gate.

3. Add a `"confidence"` field to the reflection JSON output
   (`REFLECTION_GEN_PROMPT`): `"confidence": "high|medium|low"`. Low confidence
   triggers a resample.

### Testing
- Run `pytest tests/` for regression check

---

## Implementation Notes

### Priority Order
Sections 1, 3, 4, 10 are highest impact with lowest cost (fold into existing calls
or use existing LLM output). Sections 6, 7, 8 add new LLM calls and should be gated
behind env vars. Sections 2, 5, 9, 11, 12 are refinements.

### Cost Awareness
Each new LLM call adds latency and API cost. The design above minimizes new calls by:
- Reusing output from existing calls (Sections 3, 4, 10, 12)
- Replacing heuristic + LLM pairs with single LLM calls (Sections 1, 6)
- Gating expensive additions behind env vars (Sections 7, 8, 9)

### Backward Compatibility
Every change preserves the current behavior as a fallback. If an LLM call fails or
returns unparseable output, the system degrades to the existing heuristic.
