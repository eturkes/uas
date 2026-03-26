# UAS Improvement Plan

Analysis based on the `rehab/` run (goal_001.txt: SCI Rehabilitation Analytics Suite).
The run completed all 14 steps with zero retries, yet the output is broken in practice
and underperforms what a single Claude session would produce.

---

## Root Cause Analysis

### 1. Data Leakage in Feature Selection
The model uses `discharge_scim_total` (a **discharge-time** measurement) to predict
`discharge_ais` (also a discharge-time outcome). This is textbook data leakage -- you
can't use future data to predict the future. The feature dominated importance at 27%,
meaning the model learned a trivial correlation. A human data scientist (or a single
Claude session with full context) would immediately flag this.

**Why UAS missed it:** The planner decomposed feature engineering (step 9) and
modeling (step 10) into separate steps. Step 10's spec said "identify features from
admission_features.csv" but the generated code blindly used discharge columns because
they were available in the dataframe. The verification criteria ("model trains and
produces metrics") didn't check for leakage.

### 2. Model Worse Than Baseline (44% vs 56%)
The RF classifier performs worse than always predicting the majority class. With only
27 patients across 5 AIS classes (distribution: D=15, A=6, C=3, E=2, B=1), the model
has insufficient data for meaningful multiclass classification.

**Why UAS missed it:** No quality gate checks whether a model outperforms its baseline.
The step succeeded (exit code 0, files written) so UAS moved on. The verification
criteria didn't include "model accuracy > baseline accuracy".

### 3. 100% NaN in Critical Columns
`admission_total_motor`, `admission_light_touch_total`, `admission_pin_prick_total`,
and `discharge_total_motor` are ALL NaN (28/28). The simulator produced scores at the
raw level but the feature engineering step failed to aggregate them properly for
admission timepoints. Despite this being documented in the step 10 context
("Critical Missing Data: admission_uems...100% NaN"), the modeling step just
filled NaN with 0 and continued.

**Why UAS missed it:** The context-passing mechanism correctly flagged the NaN issue
in step 10's spec, but the code generator treated it as a constraint to work around
(impute with 0) rather than a bug to fix upstream. There is no mechanism for a
downstream step to trigger re-execution of an upstream step when it discovers the
upstream output is fundamentally broken.

### 4. Subgroup Analysis is a Stub
`subgroup_results.json` contains only patient counts and mean ages -- no statistical
tests, no effect sizes, no recovery trajectories, despite the spec requesting
Mann-Whitney U tests, bootstrap CIs, and longitudinal trajectory analysis.

**Why UAS missed it:** The spec for step 10 was overloaded (RF model + subgroup
analysis + recovery trajectories). The generated script prioritized the model and
left subgroups as placeholder dicts. The verification criteria were too vague to
catch this. The 250-line script limit meant the code generator couldn't fit
everything.

### 5. Dashboard Hardcoded Workspace Paths
`tab_simulator.py` and `tab_insights.py` hardcode
`_WORKSPACE = os.environ.get("WORKSPACE", "/workspace/workspace")` -- a
container-specific path. Running the dashboard outside the container
(i.e., in real-world use) fails because this path doesn't exist.

**Why UAS missed it:** Each step runs inside the sandbox where `/workspace/workspace`
is valid. The verification script also runs there, so it passes. No step tests
portability.

### 6. Excessive Step Count and Token Waste
14 steps for what is essentially: simulate -> clean -> features -> model -> dashboard.
Steps like "project scaffold" (step 2) and "data loader module" (step 8) add overhead
without value. Each step invokes a full LLM generation + sandbox cycle. The run took
~11,500 seconds total (192 minutes / 3.2 hours).

---

## Improvement Sections

Each section below is a self-contained unit of work. Complete them in order,
one per coding session. Mark sections done by changing `[ ]` to `[x]`.

---

### Section 1: Semantic Verification Criteria
**Files:** `architect/planner.py`, `architect/main.py`

**Problem:** Verification criteria are mechanical ("file exists, has rows, stdout has
numbers") rather than semantic ("model outperforms baseline", "no discharge-time
features used as predictors for discharge outcomes", "all claimed analyses are
present in output").

**Changes:**

1. In `planner.py`'s `DECOMPOSITION_PROMPT`, add a new `<verification_guidelines>`
   section after `<anti_patterns>`:
   ```
   <verification_guidelines>
   Write verification criteria that test CORRECTNESS, not just EXISTENCE:
   - For data steps: verify row counts, column types, and value ranges match
     expectations. Check for unexpected 100% NaN columns.
   - For modeling steps: verify model outperforms a trivial baseline (majority
     class, mean prediction). If it doesn't, the step has FAILED.
   - For analysis steps: verify each claimed analysis actually appears in the
     output (not just that the output file exists).
   - For integration steps: verify the output works in a clean environment,
     not just the current sandbox.
   Anti-pattern: "file exists and is non-empty" -- this catches nothing.
   Good pattern: "model_metrics.json accuracy > baseline_accuracy AND all
   per_class_f1 values are defined AND confusion matrix is not degenerate
   (predicts more than one class)"
   </verification_guidelines>
   ```

2. In `main.py`'s `check_output_quality()`, add CSV column-level checks:
   - For CSV files, check for columns that are 100% NaN and warn.
   - For JSON files containing `accuracy` and `baseline_accuracy`, verify
     accuracy >= baseline_accuracy.

- [x] Done

---

### Section 2: Data Leakage Detection
**Files:** `architect/planner.py`, `architect/main.py`

**Problem:** No mechanism detects when a predictive model uses target-correlated
features from the same time period as the target variable.

**Changes:**

1. In `planner.py`'s `DECOMPOSITION_PROMPT` `<anti_patterns>` section, add:
   ```
   - Data leakage in predictive modeling: when a step trains a model to predict
     an outcome (e.g., discharge status), it must ONLY use features available at
     prediction time (e.g., admission features). Using discharge-time measurements
     to predict discharge outcomes is data leakage. The step description must
     explicitly state which features are allowed and why. The verify criteria must
     check that no future-time features are included.
   ```

2. In the `<expert_approach>` section, add:
   ```
   - For predictive modeling tasks, always specify the temporal boundary: which
     data is available at prediction time vs. which data is the target. Instruct
     the code to explicitly filter features by this boundary.
   ```

3. In `main.py`'s `check_output_quality()`, when a step's `files_written` includes
   a model file (`.joblib`, `.pkl` ending with "model") AND a metrics JSON:
   - Parse the metrics JSON for `feature_names`.
   - If any feature name contains temporal indicators matching the target
     (e.g., both contain "discharge"), emit a warning.

- [x] Done

---

### Section 3: Upstream Backtracking on Data Quality
**Files:** `architect/main.py`

**Problem:** When step 10 discovered that critical columns in step 9's output were
100% NaN, it worked around the problem (filled with 0) instead of triggering a
re-run of step 9. The existing `_is_verification_stagnation()` only detects
repeated failures of the *same* step, not "this step's input is broken."

**Changes:**

1. Add a `check_input_quality()` function called at the start of step execution
   (before code generation), that scans the dependency outputs:
   - For CSV files from dependencies: check for columns that are >90% NaN and
     flag them.
   - If flagged, include a prominent warning in the step's context:
     `"WARNING: Dependency output has quality issues: [details]. Consider whether
     these indicate a bug in the upstream step. If the data is fundamentally
     broken, this step should report the issue rather than work around it."`

2. Extend the reflection logic in `execute_step_with_retries()`: when a step's
   error mentions "all NaN" or "no valid data" or "constant column" AND the root
   cause trace points to a dependency, automatically trigger backtracking to the
   dependency (currently this only happens after repeated stagnation).

- [ ] Done

---

### Section 4: Step Overload Prevention
**Files:** `architect/planner.py`

**Problem:** Step 10 was asked to do RF modeling + LOSO CV + subgroup discovery +
recovery trajectories -- too much for a single 250-line script. The generator
prioritized the model and stubbed out the rest.

**Changes:**

1. In `planner.py`'s `DECOMPOSITION_PROMPT` `<anti_patterns>`, strengthen the
   existing under-splitting guidance:
   ```
   - Overloading steps: A step that requires model training AND statistical
     testing AND visualization will fail to do all three well. Split into
     separate steps: one trains and saves the model, one loads the model and
     runs statistical analyses, one generates visualizations. Each step should
     have ONE primary responsibility.
   - The 250-line limit is real: if a step description contains more than 3
     distinct deliverables, it MUST be split. Count deliverables explicitly in
     your analysis.
   ```

2. In `critique_and_refine_plan()`, add a heuristic check: if any step's
   description is longer than 1500 characters AND mentions more than 2 distinct
   output files, flag it for splitting in the critique prompt.

- [ ] Done

---

### Section 5: Portable Workspace Paths
**Files:** `orchestrator/claude_config.py`, `architect/main.py`

**Problem:** Generated code hardcodes `/workspace/workspace` as the fallback path.
This works inside the container but breaks when running outside it.

**Changes:**

1. In `orchestrator/claude_config.py`'s CLAUDE.md template, make the workspace
   path guidance more explicit:
   ```
   - The WORKSPACE environment variable points to the project root.
   - For the fallback, use the script's own directory:
     `os.environ.get("WORKSPACE", os.path.dirname(os.path.abspath(__file__)))`
   - NEVER hardcode `/workspace` or `/workspace/workspace` as a fallback.
   - When a module is inside a subdirectory (e.g., dashboard/), the fallback
     should be the PARENT directory:
     `os.environ.get("WORKSPACE", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))`
   ```

2. In `main.py`'s `check_output_quality()`, for `.py` files: scan for the string
   literal `"/workspace"` in default arguments or assignments. If found, emit a
   warning: "Hardcoded /workspace path detected -- will break outside container."

- [ ] Done

---

### Section 6: Duplicate Code Across Steps
**Files:** `orchestrator/claude_config.py`

**Problem:** Each dashboard tab module re-implements `_t()` (translation helper),
re-loads model artifacts at module level, and re-derives feature columns. This is
because each step generates code independently without awareness of what utilities
already exist.

**Changes:**

1. In `claude_config.py`'s module API propagation, when listing functions from
   prior steps, also include a directive:
   ```
   IMPORTANT: Before writing a new helper function, check if an equivalent
   already exists in the modules listed above. Import and reuse existing
   functions rather than reimplementing them. In particular:
   - Translation helpers: use the one from dashboard/translations.py
   - Data loading: use the one from data_loader.py
   - Feature engineering: use functions from feature_engineering.py
   ```

2. This is a documentation-level fix -- the module API list is already propagated,
   but the CLAUDE.md doesn't explicitly tell the generator to reuse rather than
   reinvent.

- [ ] Done

---

### Section 7: Richer Context Distillation for Modeling Steps
**Files:** `architect/main.py`

**Problem:** The context passed to step 10 correctly noted "Critical Missing Data:
admission_uems...100% NaN" but the code generator ignored this because it was buried
in a large context block. The information was accurate but not actionable.

**Changes:**

1. In `build_context()` / `distill_dependency_for_step()`, when the dependency
   output contains data quality warnings (NaN columns, degenerate distributions),
   promote these to a top-level `<data_quality_warnings>` section rather than
   embedding them in the middle of a context block.

2. In the spec generator, if the step is a modeling step (description mentions
   "model", "train", "predict", "classify", "regress"), prepend a directive:
   ```
   BEFORE writing any modeling code, review the <data_quality_warnings> section.
   If critical features are all NaN, you must either:
   (a) Fix the upstream computation by re-running feature extraction, OR
   (b) Report the issue and use only features with valid data.
   Do NOT silently impute all-NaN columns with 0 -- this produces meaningless
   models.
   ```

- [ ] Done

---

### Section 8: End-to-End Integration Test
**Files:** New file `integration/test_rehab_quality.py`

**Problem:** No automated way to verify that a completed run actually produces
usable output. The eval.py exists but doesn't test real-world quality.

**Changes:**

1. Create `integration/test_rehab_quality.py` that:
   - Checks model accuracy > baseline accuracy in `model_metrics.json`.
   - Checks no feature name shares a temporal prefix with the target
     (e.g., "discharge" features predicting "discharge" target).
   - Checks all columns in `admission_features.csv` used as model features
     have <50% NaN.
   - Checks `subgroup_results.json` contains actual statistical test results
     (not just counts).
   - Checks no `.py` file in `workspace/` contains hardcoded `/workspace`
     paths.
   - Runs `python -c "from dashboard.app import app"` to verify import works.

2. This test can be run against the `rehab/workspace/` output to validate
   improvements and can serve as a template for project-specific quality gates.

- [ ] Done

---

## Summary of Impact

| Issue | Root Cause in UAS | Section |
|-------|------------------|---------|
| Data leakage (discharge features predict discharge) | No leakage detection in planner or verifier | 2 |
| Model worse than baseline | Verification only checks existence, not quality | 1 |
| 100% NaN columns silently imputed | No upstream backtracking on data quality | 3 |
| Subgroup analysis is a stub | Step overloaded with too many deliverables | 4 |
| Hardcoded container paths | CLAUDE.md fallback path guidance insufficient | 5 |
| Duplicate helper functions | No reuse directive in code generation context | 6 |
| Data warnings ignored by generator | Warnings buried in context, not actionable | 7 |
| No post-run quality check | No integration test for output quality | 8 |
