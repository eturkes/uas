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

### Section 2 — Protect requirements during replanning [x] Done

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

### Section 3 — Enforce creation/integration separation in decomposition [x] Done

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

### Section 4 — Richer dependency context with file signatures [x] Done

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

### Section 5 — Integration checkpoint steps [x] Done

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

### Section 6 — Validation-driven correction loop [x] Done

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

### Section 7 — Decomposition depth scaling for complex goals [x] Done

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

### Section 8 — Tests and verification [x] Done

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

---

## Second round: Execution-layer structural defects

Sections 1-8 fixed planning quality (decomposition depth, coverage tracking,
replanning protection). A second test run (`rehab/`) with these fixes produced
a plan with 27 well-decomposed steps — all reported success. However, the
workspace is structurally broken and unusable:

**Observed defects in `rehab/`:**

1. **Nested duplication** — The workspace IS the project root, but generated code
   created a `rehab/` subdirectory inside it, so the canonical project lives at
   `rehab/rehab/` while the workspace root has stale partial copies.

2. **16 step-script artifacts at root** — Files like `build_cleaner.py`,
   `step3_create_tabs.py`, `validate_phase6.py` are UAS execution artifacts,
   not project files. They were created by Claude Code CLI's tools during LLM
   generation (side effects of the agent, not the sandbox script).

3. **`scripts/` directory polluted** — Contains `step01_config_schema.py` and
   `validate_phase2.py` instead of app entry points. The real
   `run_dashboard.py` is at `rehab/scripts/run_dashboard.py` (nested copy).

4. **Stale modules from architecture evolution** — Early steps created
   `src/rehab/tabs/` with 5 files and `src/rehab/cleaner.py`; later steps
   moved to `src/rehab/dashboard/` and `src/rehab/data/cleaner.py`. Old
   files persist. Also `src/rehab/i18n.py` vs `src/rehab/locale/`,
   `src/rehab/translations/` vs `src/rehab/locale/`.

5. **Data debris** — `cleaned_patient_data.csv`, `raw_patient_data.csv`,
   `simulation_spec.json` at workspace root (intermediate step outputs).

6. **89 MB `.uas_auth/`** — Claude Code's plugin/auth infrastructure leaked
   into the workspace.

7. **README references broken paths** — `python scripts/run_dashboard.py` but
   that file doesn't exist at the workspace root; it's inside `rehab/rehab/`.

**Root cause:** The orchestrator uses `claude -p --dangerously-skip-permissions`
which gives Claude Code full tool access. While generating a "self-contained
Python script" response, Claude Code also writes files, creates directories, and
runs commands as side effects. The cleanup function (`cleanup_workspace_artifacts`)
only removes root-level `.py` files containing the `UAS_RESULT` marker — it
misses files in subdirectories, files without the marker, data files,
directories, and all files created by Claude Code's tools (vs the sandbox
script).

Each section below addresses one defect category. Sections are independent and
ordered by impact.

---

### Section 9 — Isolate LLM generation side effects [x] Done

**Problem:** The orchestrator invokes `claude -p --dangerously-skip-permissions`
with the workspace as CWD. Claude Code, being a full agent, uses its Write,
Bash, and other tools during generation — creating files, directories, and even
`.uas_auth/` infrastructure (89 MB) in the workspace. These side effects are
never cleaned up because the cleanup function only targets sandbox script
artifacts.

**Fix:** Run the Claude Code CLI in an isolated temporary directory so its tool
side effects don't land in the workspace. Only the text response (containing the
generated Python script) is returned to the orchestrator.

**Files to change:**

**9a. `orchestrator/llm_client.py` — isolate CWD in `_run_streaming()`:**
- Before spawning the Claude Code CLI subprocess, create a temporary directory
  (`tempfile.mkdtemp()`)
- Set `cwd` of the subprocess to this temp directory
- Set `CLAUDE_HOME` or equivalent env var to a temp location to prevent
  `.uas_auth/` from landing in the workspace
- After the subprocess completes, delete the temp directory
- The `WORKSPACE` env var is NOT set in the CLI's environment — the CLI should
  only produce text, not modify files

**9b. `orchestrator/claude_config.py` — update CLAUDE.md guidance:**
- Remove instructions about working "in the workspace" since the CLI now runs
  in isolation
- Strengthen the instruction: "You are generating a Python script as TEXT
  output. Do NOT use Write, Edit, or Bash tools to create files. Output the
  complete script in a single fenced code block."
- Add: "Do NOT create any files or directories. Your only job is to produce
  the script text in your response."

**9c. `orchestrator/llm_client.py` — suppress tool use if possible:**
- Investigate whether Claude Code CLI has a flag to disable tool use or restrict
  it to read-only operations. If so, use that flag.
- If not, the CWD isolation from 9a is sufficient to prevent workspace damage.

**9d. Tests:**
- `tests/test_llm_isolation.py` — verify that the temp directory is created and
  cleaned up, and that the workspace is not modified during LLM generation
- Mock the subprocess call and verify env/cwd are set correctly

**Acceptance criteria:**
- After LLM generation, the workspace contains zero new files (only the sandbox
  script execution should create files)
- `.uas_auth/` never appears in the workspace
- No loose step scripts (build_*.py, validate_*.py) appear in the workspace
- The temp directory is always cleaned up, even on error

---

### Section 10 — Workspace snapshot and recursive diff cleanup [x] Done

**Problem:** `cleanup_workspace_artifacts()` only checks root-level `.py` files
containing `UAS_RESULT`. It misses: files in subdirectories, files without the
marker, data files (`.csv`, `.json`), empty directories, and files created by
prior steps that are now stale. In the rehab run, this left 16 loose scripts,
3 data files, and stale directory trees.

**Fix:** Take a full recursive workspace snapshot before each step. After step
execution (including verification), diff the snapshot against the current state.
Any new file that is NOT in the step's declared `files_written` output is an
artifact and gets removed.

**Files to change:**

**10a. `architect/main.py` — new `snapshot_workspace()` function:**
```python
def snapshot_workspace(workspace: str) -> set[str]:
    """Return the set of all relative file paths under workspace."""
    paths = set()
    skip = {".git", ".uas_state", ".uas_auth", "__pycache__", "node_modules"}
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), workspace)
            paths.add(rel)
    return paths
```

**10b. `architect/main.py` — new `cleanup_step_artifacts()` function:**
```python
def cleanup_step_artifacts(
    workspace: str,
    pre_snapshot: set[str],
    step_output_files: set[str],
) -> list[str]:
    """Remove files created during a step that aren't claimed output."""
    post_snapshot = snapshot_workspace(workspace)
    new_files = post_snapshot - pre_snapshot
    claimed = {os.path.normpath(f) for f in step_output_files}
    artifacts = new_files - claimed
    removed = []
    for rel in artifacts:
        fpath = os.path.join(workspace, rel)
        try:
            os.remove(fpath)
            removed.append(rel)
        except OSError:
            pass
    # Remove empty directories left behind
    _remove_empty_dirs(workspace)
    return removed
```

**10c. `architect/main.py` — integrate into step execution loop:**
- Before calling `run_orchestrator()`, take `pre_snapshot = snapshot_workspace()`
- After orchestrator + verification, call `cleanup_step_artifacts()`
- Replace existing `cleanup_workspace_artifacts()` calls with the new function
- Log removed artifacts

**10d. Tests:**
- `tests/test_snapshot_cleanup.py`
- Create a temp workspace, add some files, take snapshot, add artifacts, verify
  cleanup removes only the artifacts

**Acceptance criteria:**
- After each step, only files that existed before the step OR files the step
  claimed to create remain in the workspace
- Data file debris (`.csv`, `.json`) from intermediate processing is removed
- Empty directories left by cleanup are also removed
- Step output files (the actual project code) are never removed

---

### Section 11 — Detect and prevent nested project duplication

**Problem:** Generated scripts sometimes create a subdirectory named after the
project inside the workspace (e.g., `os.makedirs("rehab/src/rehab/")` when the
workspace IS already the rehab project root). This produces `rehab/rehab/` with
the canonical project, while the workspace root has stale partial copies.

**Fix:** Multi-layered prevention: (1) strengthen CLAUDE.md guidance,
(2) add a post-step check that detects mirrored directory structures, and
(3) if detected, promote the nested copy and delete the nesting.

**Files to change:**

**11a. `orchestrator/claude_config.py` — add explicit anti-nesting rule:**
- Add to CLAUDE.md:
  ```
  ## Critical: Workspace IS the project root
  The workspace directory IS the project root. Do NOT create a subdirectory
  named after the project. If the project is called "myapp", do NOT create
  "myapp/" inside the workspace. Write files directly to the workspace:
  - CORRECT: os.path.join(workspace, "src", "myapp", "main.py")
  - WRONG:   os.path.join(workspace, "myapp", "src", "myapp", "main.py")
  ```

**11b. `architect/main.py` — new `detect_nested_duplication()` function:**
- After each step, scan the workspace for directories that mirror the root
  structure:
  ```python
  def detect_nested_duplication(workspace: str) -> str | None:
      """Detect a nested directory that mirrors the workspace structure.
      Returns the nested path if found, None otherwise."""
      root_dirs = {d for d in os.listdir(workspace)
                   if os.path.isdir(os.path.join(workspace, d))
                   and d not in SKIP_DIRS}
      for d in root_dirs:
          nested = os.path.join(workspace, d)
          nested_children = {c for c in os.listdir(nested)
                             if os.path.isdir(os.path.join(nested, c))
                             and c not in SKIP_DIRS}
          # If the nested directory has src/ or similar project markers
          # AND the root also has them, it's a duplication
          project_markers = {"src", "scripts", "tests", "data"}
          if len(nested_children & project_markers) >= 2:
              if len(root_dirs & project_markers) >= 2:
                  return d
      return None
  ```

**11c. `architect/main.py` — new `resolve_nested_duplication()` function:**
- If duplication detected, compare file counts and modification times
- The nested copy (usually more complete) is promoted: its contents are moved
  to the workspace root, replacing stale root files
- The empty nested directory is removed
- Log the resolution clearly

**11d. `architect/planner.py` — inject project name into step context:**
- Pass the project/workspace name to step descriptions so generated scripts
  know not to create that directory
- Add to dependency context: `<workspace_name>rehab</workspace_name>` so
  scripts can avoid the pattern

**11e. Tests:**
- `tests/test_nested_duplication.py`
- Create workspace with both root and nested structures, verify detection
  and resolution

**Acceptance criteria:**
- A generated script that creates `rehab/src/` inside the workspace gets
  detected and resolved after step execution
- After resolution, the workspace has a single flat project structure
- README paths (`scripts/run_dashboard.py`) resolve correctly from root

---

### Section 12 — Project structure manifest with stale file detection

**Problem:** As steps execute and the architecture evolves, old files persist.
In the rehab run: `src/rehab/tabs/` (5 files, created step 3) coexists with
`src/rehab/dashboard/` (created step 9), `src/rehab/cleaner.py` coexists with
`src/rehab/data/cleaner.py`, `src/rehab/translations/` coexists with
`src/rehab/locale/`. No mechanism detects that earlier files are superseded.

**Fix:** Maintain a running project manifest — the canonical set of files that
constitute the project. When a step creates files that functionally replace
earlier ones (same module name in a different location, or same purpose with
a different structure), mark the old files as stale and remove them.

**Files to change:**

**12a. `architect/main.py` — new `ProjectManifest` class:**
```python
class ProjectManifest:
    """Track the canonical set of project files across steps."""
    def __init__(self):
        self.files: dict[str, int] = {}  # rel_path → step_id that created it

    def add_step_output(self, step_id: int, files: list[str]):
        for f in files:
            self.files[f] = step_id

    def detect_superseded(self, new_files: list[str]) -> list[str]:
        """Find existing files that are functionally replaced by new_files."""
        superseded = []
        for new_f in new_files:
            new_base = os.path.basename(new_f)
            new_module = os.path.splitext(new_base)[0]
            for old_f in list(self.files.keys()):
                if old_f == new_f:
                    continue
                old_base = os.path.basename(old_f)
                old_module = os.path.splitext(old_base)[0]
                # Same module name in different location = likely superseded
                if old_module == new_module and old_base == new_base:
                    superseded.append(old_f)
        return superseded
```

**12b. `architect/main.py` — integrate manifest into step loop:**
- After each step completes, add its `files_written` to the manifest
- Call `detect_superseded()` with the new files
- For detected supersessions, use LLM to confirm (is `src/rehab/cleaner.py`
  superseded by `src/rehab/data/cleaner.py`?)
- If confirmed, remove the old file and update the manifest
- Log: "Removed stale file src/rehab/cleaner.py (superseded by
  src/rehab/data/cleaner.py in step 12)"

**12c. `architect/main.py` — directory-level supersession:**
- Detect when an entire directory is superseded:
  - `tabs/` → `dashboard/` (same purpose, different structure)
  - `translations/` → `locale/` (same purpose)
- Heuristic: if a new directory has similar file count and overlapping module
  names with an old directory, flag for LLM review

**12d. Tests:**
- `tests/test_project_manifest.py`
- Test that `cleaner.py` at root is flagged when `data/cleaner.py` is created
- Test that `tabs/` directory is flagged when `dashboard/` is created

**Acceptance criteria:**
- After the rehab run, only one copy of each module exists
- Stale directory trees (`tabs/`, `translations/`) are removed when their
  replacements (`dashboard/`, `locale/`) are created
- The manifest tracks which step created each file for traceability

---

### Section 13 — Holistic end-of-run workspace validation

**Problem:** Step-level validation checks that each step's outputs exist and
pass their verification criteria, but never validates the project as a whole.
In the rehab run: README says `python scripts/run_dashboard.py` but this file
doesn't exist at the workspace root; imports between modules may not resolve;
orphaned files bloat the workspace.

**Fix:** After all steps complete (and after any correction loop), run a
holistic validation pass that checks cross-cutting concerns.

**Files to change:**

**13a. `architect/main.py` — new `holistic_validation()` function:**
```python
def holistic_validation(workspace: str, state: dict) -> list[str]:
    """Check project-wide coherence. Returns list of issue strings."""
    issues = []
    issues.extend(_check_readme_accuracy(workspace))
    issues.extend(_check_import_resolution(workspace))
    issues.extend(_check_orphaned_files(workspace, state))
    issues.extend(_check_entry_points(workspace))
    return issues
```

**13b. `architect/main.py` — `_check_readme_accuracy()`:**
- Parse README.md for code blocks containing file paths or shell commands
- For each referenced path (e.g., `scripts/run_dashboard.py`), verify it exists
- For each shell command (e.g., `python scripts/run_dashboard.py`), verify the
  target script exists
- For the project structure tree in README, verify each listed file exists
- Return issues for any mismatches

**13c. `architect/main.py` — `_check_import_resolution()`:**
- For each `.py` file in the workspace, parse imports using `ast`
- For relative imports, verify the target module exists
- For package imports (e.g., `from rehab.data.loader import load_data`),
  verify the module path exists
- Return issues for unresolvable imports

**13d. `architect/main.py` — `_check_orphaned_files()`:**
- Compare files on disk against the union of all steps' `files_written`
- Files that no step claims AND were not in the initial workspace are orphans
- Exclude expected files (.gitignore, pyproject.toml, etc.)
- Return issues listing orphaned files

**13e. `architect/main.py` — `_check_entry_points()`:**
- Parse `pyproject.toml` for declared entry points / scripts
- Verify each entry point target exists and is importable
- Check that `scripts/` directory (if present) contains runnable files

**13f. `architect/main.py` — integrate into finalization:**
- After the correction loop (Section 6), run `holistic_validation()`
- Feed issues into `generate_corrective_steps()` for one more correction round
  if critical issues found (README references missing files, imports broken)
- Log all issues to the run report

**13g. Tests:**
- `tests/test_holistic_validation.py`
- Create a workspace with a README referencing a non-existent script, verify
  detection
- Create a workspace with broken imports, verify detection

**Acceptance criteria:**
- `python scripts/run_dashboard.py` in README triggers a validation failure
  when that file doesn't exist
- Broken cross-module imports are detected before the run finalizes
- Orphaned step artifacts are flagged
- At least one correction round runs for critical issues (missing entry points,
  broken imports)
