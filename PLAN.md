# PLAN: Improving UAS output quality

## Context

The `rehab/` directory is the output of `uas --goal-file goal_001.txt` — a
complex SCI rehabilitation dashboard. Although UAS reported 9/9 steps completed
and the validation rated goal satisfaction at "medium confidence", the output is
broken in real-world use:

- 3 of 4 dashboard tabs are empty placeholders
- Modeling (XGBoost + SHAP) and subgroup analysis are 16-19 line stubs
- Temporal analysis is computed but never displayed in the dashboard
- Motor column JA→EN mapping is wrong for the real-data pathway
- Language mixing in Japanese mode
- Step 12 required 4 rewrites and took 67 minutes (half the total run time)

**Root causes identified:**

1. **Steps too coarse** — 9 steps for a project that needs 15-20. Modeling,
   explainability, subgroups, and 3 dashboard tabs were never decomposed into
   real steps.
2. **Replanning removed critical work** — Steps 6-10 vanished during
   mid-execution replanning, orphaning goal requirements (modeling, SHAP,
   subgroups, individual dashboard tabs).
3. **Creation + integration coupling** — Building a module AND integrating it
   into an existing pipeline in one step caused 100% of the rewrite failures
   (meta-learning lesson from the run itself).
4. **No goal-coverage verification** — Nothing checks that every requirement in
   the goal maps to at least one step before execution starts.
5. **Validation doesn't trigger correction** — "Medium confidence" + 6 concrete
   issues are reported but never acted on.
6. **Context propagation loses critical details** — Dependency summaries drop
   exact column names and function signatures, so downstream steps guess wrong.
7. **No integration checkpoints** — Each step passes its own verification, but
   nothing checks that modules work together until the final smoke test.

Each section below addresses one root cause. Sections are independent and
ordered by impact. Each is scoped to be completable in a single coding session.

---

### Section 1 — Goal-coverage matrix before execution [x] Done

**Problem:** UAS decomposes the goal into steps but never verifies that ALL goal
requirements are covered. In the rehab run, modeling, SHAP explainability,
subgroup discovery, and 3 dashboard tabs had no real steps assigned.

**Fix:** After decomposition (and after any replanning), build a coverage matrix
that maps each extractable requirement from the goal to the step(s) that
address it. If any requirement is uncovered, add steps before execution begins.

**Files to change:**

**1a. `architect/planner.py` — new `extract_requirements()` function:**
- Takes goal text, returns a list of atomic requirement strings
- Uses LLM to parse the goal into discrete deliverables
- Example output for rehab goal: `["data simulator from spec", "cleaning
  pipeline", "bilingual translations", "XGBoost predictive model",
  "SHAP explainability", "subgroup discovery", "dashboard tab: cohort overview",
  "dashboard tab: patient simulator", "dashboard tab: insight engine",
  "bilingual toggle", ...]`

**1b. `architect/planner.py` — new `verify_coverage()` function:**
- Takes requirements list + step list, returns uncovered requirements
- Uses LLM to judge whether each requirement is addressed by at least one step
- Returns `{requirement: str, covered: bool, covering_steps: list[int]}` for
  each requirement

**1c. `architect/planner.py` — new `fill_coverage_gaps()` function:**
- Takes uncovered requirements + existing steps, generates new steps to fill gaps
- Inserts them into the DAG with correct dependencies
- Called after `decompose_goal` and after every `replan_remaining_steps`

**1d. `architect/main.py` — integrate into execution flow:**
- After `decompose_goal_with_voting()` returns, call `extract_requirements()`
  then `verify_coverage()` then `fill_coverage_gaps()` if needed
- After every `replan_remaining_steps()`, re-verify coverage
- Log coverage matrix to progress.md

**1e. Tests:**
- `tests/test_coverage_matrix.py` — unit tests for extraction and verification
- Mock LLM responses to test gap detection and step insertion

**Acceptance criteria:**
- For the rehab goal, the coverage matrix identifies at least:
  predictive modeling, SHAP, subgroup discovery, and each dashboard tab
- If decomposition produces only 5 steps for a complex goal, gap-filling adds
  the missing ones before execution starts
- Replanning that removes a step triggers re-verification

---

### Section 2 — Protect requirements during replanning

**Problem:** `replan_remaining_steps()` in `architect/planner.py` can drop steps
that were the sole coverage for a goal requirement. In the rehab run, steps
6-10 (modeling, SHAP, subgroups, dashboard tabs) vanished during replanning.

**Fix:** Constrain replanning to preserve goal coverage. Replanning can
rewrite, merge, or reorder steps, but cannot remove a step that is the only
one covering a requirement without adding a replacement.

**Files to change:**

**2a. `architect/planner.py` — modify `replan_remaining_steps()`:**
- Accept a `requirements` list parameter
- After LLM produces new steps, re-verify coverage via `verify_coverage()`
- If coverage regressed, append the dropped requirements to the prompt and
  retry (up to 2 attempts)
- If still uncovered, call `fill_coverage_gaps()` on the new plan

**2b. `architect/planner.py` — modify `REPLAN_PROMPT`:**
- Add a `<protected_requirements>` section listing requirements that MUST have
  at least one covering step in the new plan
- Instruct the LLM: "You may rewrite, merge, split, or reorder steps, but you
  MUST NOT remove coverage for any protected requirement."

**2c. `architect/main.py` — pass requirements through replan calls:**
- Store extracted requirements in state.json
- Pass them to `replan_remaining_steps()` at every call site

**2d. Tests:**
- `tests/test_replan_protection.py` — verify that replanning preserves coverage
- Test case: replanning tries to drop modeling step → coverage check catches it
  → step is restored or replaced

**Acceptance criteria:**
- Replanning can never reduce goal coverage below what decomposition produced
- State.json includes a `requirements` field with the extracted list
- A replan that drops the only step covering "SHAP explainability" either
  retries or fills the gap

---

### Section 3 — Enforce creation/integration separation in decomposition

**Problem:** Steps that both create a new module AND integrate it into an
existing pipeline are the primary source of rewrite failures. The rehab run's
meta-learning confirms: "Creation + integration coupling caused 100% of errors."

**Fix:** Add an explicit anti-pattern to the decomposition prompt and a
post-decomposition validation that splits coupled steps.

**Files to change:**

**3a. `architect/planner.py` — update `DECOMPOSITION_PROMPT`:**
- Add to `<anti_patterns>`:
  ```
  - Coupling creation and integration: NEVER have a single step that both
    creates a new module AND modifies an existing one to import/use it.
    Split into two steps: (1) create the module with its own tests/verification,
    (2) integrate it into the existing codebase. This is the #1 cause of
    rewrite failures.
  ```

**3b. `architect/planner.py` — new `split_coupled_steps()` function:**
- Post-decomposition pass that examines each step description
- Heuristic detection: step mentions both "create/write/build X" AND
  "update/modify/integrate into Y"
- LLM-assisted split: ask the LLM to separate into creation + integration steps
- Adjust dependencies: integration step depends on creation step

**3c. `architect/planner.py` — integrate into `decompose_goal_with_voting()`:**
- After voting selects the best plan, run `split_coupled_steps()`
- Before returning final steps

**3d. Tests:**
- `tests/test_split_coupled.py` — detect and split coupled steps
- Test: step "Create temporal.py and update pipeline.py to use it" → two steps

**Acceptance criteria:**
- No step in the final plan both creates a new file and modifies an existing one
- Split steps have correct dependency ordering (integration depends on creation)

---

### Section 4 — Richer dependency context with file signatures

**Problem:** When step N depends on step M, the context passed includes only
a summary and file list. Exact column names, function signatures, and class
interfaces are lost, causing downstream steps to guess wrong (e.g., the motor
column mapping bug in the rehab run).

**Fix:** Include actual file signatures (imports + function/class definitions +
first lines of docstrings) from files produced by dependency steps.

**Files to change:**

**4a. `architect/executor.py` — new `extract_file_signatures()` function:**
- For each `.py` file in `files_written`, use `ast` module to extract:
  - All function names with their parameter lists
  - All class names with their method names
  - Module-level constant/variable assignments
  - The first 2 lines of each docstring
- For `.json` files, extract top-level keys and first 3 entries of lists
- For `.csv` files, extract column names and row count
- Return a structured string, capped at ~2000 chars per file

**4b. `architect/executor.py` — modify dependency context builder:**
- Currently: `<key_outputs>` contains UAS_RESULT summary
- Add: `<file_signatures>` section with the extracted signatures
- These are included in the XML context block passed to downstream steps

**4c. `architect/spec_generator.py` — include signatures in specs:**
- When generating a step spec, include dependency file signatures in the
  `## Context` section
- Label clearly: "The following functions/columns are available from Step N"

**4d. Tests:**
- `tests/test_file_signatures.py` — test signature extraction on sample files
- Test that a `.py` file with `def clean_dataset(df: pd.DataFrame) -> pd.DataFrame`
  produces a signature including the function name and parameter types

**Acceptance criteria:**
- Downstream steps receive exact function names, parameter lists, and column
  names from dependency files
- A step that depends on the simulator receives a list of all columns in the
  output DataFrame
- File signatures are capped to prevent context bloat

---

### Section 5 — Integration checkpoint steps

**Problem:** Each step verifies itself in isolation. The rehab run had steps
that passed individually but produced incompatible outputs (e.g., temporal
analysis computed but never wired into the dashboard, model training that
references columns the cleaner didn't produce).

**Fix:** Automatically insert integration checkpoint steps at phase boundaries.
These steps import all modules from prior phases and run a minimal end-to-end
pipeline, catching interface mismatches before downstream steps build on them.

**Files to change:**

**5a. `architect/planner.py` — new `insert_integration_checkpoints()` function:**
- Analyze the step DAG to find phase boundaries (steps where the dependency
  graph widens — i.e., multiple subsequent steps depend on a set of prior steps)
- Insert a checkpoint step that:
  - Imports all modules written by the preceding phase
  - Calls each public function with minimal valid inputs
  - Asserts no ImportErrors, no NameErrors, no missing columns
  - Prints interface summary (function signatures, DataFrame shapes, column names)
- The checkpoint step depends on all steps in the preceding phase
- All steps in the next phase depend on the checkpoint

**5b. `architect/planner.py` — checkpoint step template:**
```python
CHECKPOINT_TEMPLATE = """Write a Python script that validates the interface
between completed modules. For each module produced by steps {step_ids}:
1. Import the module
2. Call its main function(s) with minimal valid inputs
3. Print the return type, shape (if DataFrame), and column names (if applicable)
4. Assert no errors occur
This is a validation step — it must not modify any files."""
```

**5c. `architect/main.py` — call after decomposition:**
- After gap-filling (Section 1) and splitting (Section 3), insert checkpoints
- Checkpoints are lightweight (no file output) and fast

**5d. Tests:**
- `tests/test_integration_checkpoints.py`
- Test: a 9-step plan with 3 phases gets 2 checkpoint steps inserted

**Acceptance criteria:**
- Complex plans (7+ steps) get at least one integration checkpoint
- A checkpoint step catches the "temporal analysis not wired to dashboard" bug
  before it becomes a silent failure
- Checkpoints don't produce files — they only validate

---

### Section 6 — Validation-driven correction loop

**Problem:** `validate_workspace()` in `architect/main.py` produces a report
with concrete issues (missing tabs, mapping bugs, unused analysis) but never
acts on them. "Medium confidence" with 6 issues should trigger corrective steps.

**Fix:** When validation finds issues at below-high confidence, generate and
execute corrective steps before finalizing.

**Files to change:**

**6a. `architect/main.py` — modify post-validation flow:**
- After `validate_workspace()` and `validate_workspace_llm()`:
  - If confidence is "high" → finalize as today
  - If confidence is "medium" or "low" → enter correction loop
  - Extract concrete issues from validation report
  - Generate corrective steps (one per issue, max 5)
  - Execute them through the normal orchestrator loop
  - Re-validate
  - Max 2 correction rounds to prevent infinite loops

**6b. `architect/planner.py` — new `generate_corrective_steps()` function:**
- Takes validation issues list + current state
- Produces step dicts targeting each specific issue
- Examples: "Fix motor column mapping in translations.py",
  "Wire temporal analysis results into recovery tab",
  "Implement overview tab with cohort visualizations"

**6c. `architect/main.py` — correction budget:**
- Track correction rounds in state.json
- Set max corrective steps per round (5) and max rounds (2)
- After max rounds, finalize with whatever quality was achieved and log a warning

**6d. Tests:**
- `tests/test_correction_loop.py`
- Mock a validation that returns medium confidence with 3 issues
- Verify corrective steps are generated and would address the issues

**Acceptance criteria:**
- A "medium confidence" validation triggers at least one correction round
- Corrective steps are specific (not "fix all issues" — one step per issue)
- The correction loop terminates (max 2 rounds, max 5 steps per round)
- If corrections improve confidence to "high", the loop exits early

---

### Section 7 — Decomposition depth scaling for complex goals

**Problem:** The rehab goal is ~2500 words across 3 major phases with 10+
distinct deliverables, but UAS produced only 9 steps. The decomposition prompt
examples show medium goals (3 steps) but nothing at the scale of the rehab goal.

**Fix:** Add complexity-aware minimum step counts and a complex-goal
decomposition example to the prompt.

**Files to change:**

**7a. `architect/planner.py` — modify `estimate_complexity()` post-processing:**
- After complexity classification, set minimum step counts:
  - Trivial: 1 step
  - Simple: 2-3 steps
  - Medium: 4-7 steps
  - Complex: 8-15 steps (currently uncapped — add minimum of 8)
- If the initial decomposition returns fewer steps than the minimum, re-prompt
  with explicit instruction: "This goal requires at least N steps. You returned
  M. Decompose further."

**7b. `architect/planner.py` — add complex-goal example to `DECOMPOSITION_PROMPT`:**
- Add Example 4 showing a complex goal decomposed into 12+ steps:
  - Phase boundaries clearly marked
  - Creation and integration separated
  - Integration checkpoints between phases
  - No step with more than 2 deliverables

**7c. `architect/planner.py` — deliverable counting heuristic:**
- After decomposition, count deliverables per step (files mentioned,
  modules created, features implemented)
- If any step has more than 3 deliverables, flag for splitting

**7d. Tests:**
- `tests/test_complexity_scaling.py`
- Test that a 2500-word goal with 3 phases gets classified as complex
  and produces at least 8 steps

**Acceptance criteria:**
- Complex goals produce at least 8 steps
- No step has more than 3 distinct deliverables
- The complex-goal example in the prompt demonstrates phase-aware decomposition

---

### Section 8 — Tests and verification

Run the full test suite after all sections are implemented and fix any
regressions. Verify the improvements work together:

- Coverage matrix catches missing requirements
- Replanning preserves coverage
- Coupled steps are split
- File signatures propagate to downstream steps
- Integration checkpoints catch interface mismatches
- Validation triggers corrections
- Complex goals produce enough steps

**Manual verification:**
- Re-run `uas --goal-file goal_001.txt --dry-run` on the rehab goal
- Verify the decomposition produces 12+ steps with creation/integration
  separated, integration checkpoints, and full goal coverage
- Compare the step plan against the rehab run's actual issues
