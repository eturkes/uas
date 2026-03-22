# UAS Improvement Plan

Improvements identified from analyzing the `rehab/` project, a 12-step SCI
Rehabilitation Analytics & Prediction Suite that UAS reported as successfully
completed but which fails to launch due to cross-module import errors, has no
git commits, and contains orphaned code.

Each section is independent and should be completed by a coding agent in a
fresh session. Mark a section complete by changing `[ ]` to `[x]`.

---

## [x] Section 1: Propagate module public API in dependency context

### Problem
The rehab project's `tab_cohort.py` imports `create_card` from
`layout_components.py`, but the actual function is named `make_card`.
Similarly, it uses `CHART_COLORS` but only imports `COLORS`. This happens
because downstream steps never see the exact exported symbols of
upstream-generated modules -- they only receive file paths and prose summaries
in the dependency context.

### Root cause
`_distill_dependency_output()` and `_distill_dependency_output_llm()` in
`architect/main.py` (lines ~798-960) pass `files_written` and `summary` to
dependent steps, but never extract the actual public API of generated Python
modules. The LLM generating downstream code must guess function/class/constant
names, and guesses wrong.

### Changes required

**File: `architect/main.py`**

1. Add a helper function `extract_module_api(filepath)` that:
   - Parses a `.py` file with `ast.parse()`
   - Extracts top-level function names (`ast.FunctionDef`), class names
     (`ast.ClassDef`), and module-level constant assignments
     (`ast.Assign`/`ast.AnnAssign` where the target is an uppercase name)
   - Returns a dict: `{"functions": [...], "classes": [...], "constants": [...]}`
   - Handles parse errors gracefully (returns empty dict)

2. Modify `_distill_dependency_output()` (~line 798) to:
   - After building `files_str`, iterate `files_written` for `.py` files
   - Call `extract_module_api()` on each
   - Add a `<module_api>` XML element inside the `<dependency>` block, e.g.:
     ```xml
     <module_api file="src/layout_components.py">
       functions: make_card, create_kpi_card, make_dropdown, ...
       constants: CARD_STYLE, DEFAULT_PADDING, ...
     </module_api>
     ```

3. Modify `_distill_dependency_output_llm()` (~line 897) similarly:
   - Include the extracted API in the prompt so the LLM distillation
     preserves it
   - Add to the `TARGETED_DISTILL_PROMPT` template a section like:
     ```
     Module APIs (exact exported names — downstream steps MUST use these):
     {module_apis}
     ```

4. Add tests in a new file `tests/test_module_api.py`:
   - Test `extract_module_api()` on sample Python source
   - Test that distilled output includes `<module_api>` for `.py` files

### Files to modify
- `architect/main.py`

### Files to create
- `tests/test_module_api.py`

### Verification
- Run `python -m pytest tests/test_module_api.py -v`
- Run `python -m pytest tests/test_build_context.py -v` (existing context tests still pass)

---

## [x] Section 2: Cross-module import resolution guardrail

### Problem
The rehab project ships with broken imports (`from src.layout_components import
create_card` where `create_card` does not exist). UAS has per-file guardrails
(regex + LLM) and project-level guardrails, but none check that imports between
generated modules actually resolve.

### Root cause
`check_guardrails()` in `architect/main.py` (~line 1649) scans individual
files for patterns like hardcoded secrets, `eval()`, etc. but never validates
cross-file import relationships. `check_project_guardrails()` (~line 1754)
only checks for presence of `.gitignore`, `README`, and dependency files.

### Changes required

**File: `architect/main.py`**

1. Add a function `check_cross_module_imports(workspace)` that:
   - Finds all `.py` files in the workspace (recursive, skip `.state/`,
     `.git/`, `__pycache__/`, `venv/`, `node_modules/`)
   - For each file, uses `ast.parse()` to extract `ImportFrom` nodes where
     the module path is local (starts with `.` or matches a directory/file
     in the workspace)
   - For each such import, resolves the target module file and checks that
     every imported name exists in that module's top-level namespace (using
     the same `extract_module_api()` from Section 1, or an `ast.parse()`
     of the target)
   - Returns a list of dicts:
     `{"file": "src/tab_cohort.py", "line": 12, "imports": "create_card",
       "from_module": "src.layout_components", "severity": "error",
       "description": "name 'create_card' not found in src/layout_components.py; available: make_card, create_kpi_card, ..."}`

2. Call `check_cross_module_imports()` inside `validate_workspace()`
   (~line 2069), after the existing file-existence checks. Append results
   to the validation report under a `## Cross-Module Import Errors` heading.

3. Also call it inside the per-step post-execution guardrail block
   (~line 2535) so that import errors are caught immediately after the
   step that introduces them, enabling the rewrite loop to fix them.

4. Add tests in `tests/test_cross_imports.py`:
   - Create a temp workspace with two `.py` files where one imports a
     nonexistent name from the other
   - Verify `check_cross_module_imports()` returns the expected error
   - Test with valid imports (no errors returned)
   - Test with relative imports

### Files to modify
- `architect/main.py`

### Files to create
- `tests/test_cross_imports.py`

### Verification
- Run `python -m pytest tests/test_cross_imports.py -v`
- Run `python -m pytest tests/test_guardrails.py -v` (existing guardrail tests still pass)

---

## [x] Section 3: Application entry-point smoke test in final validation

### Problem
The rehab project's final "smoke test" step (step 13) checked syntax and ran
individual imports, reporting "5/7 imports OK" and marking success. But the
application still fails to launch because the _transitive_ import chain is
broken: `app.py` imports `tab_cohort`, which fails on `create_card`.

### Root cause
`validate_workspace()` in `architect/main.py` (~line 2069) checks that
claimed files exist and runs LLM-based goal assessment, but never attempts
to actually import or run the generated application. The per-step verification
(`verify_step_output()`) only checks what each step's `verify` field
specifies, and no step required a full transitive import test.

### Changes required

**File: `architect/main.py`**

1. Add a function `smoke_test_entry_point(workspace, state)` that:
   - Identifies the likely entry point by scanning for:
     - Files with `if __name__ == "__main__"` guards
     - Files named `app.py`, `main.py`, `run.py`, `server.py`, `dashboard.py`
     - The file referenced in `run.sh` or similar launcher scripts
   - Attempts a dry import: runs
     `python -c "import sys; sys.path.insert(0, '<project_dir>'); import <module>"`
     in a subprocess with a short timeout (15s)
   - If the import fails, captures the traceback and returns it as an error
   - Returns `None` on success, or error string on failure

2. Call `smoke_test_entry_point()` inside `validate_workspace()` after
   the project guardrails check. If it fails, add the error to the
   validation report under `## Launch Test`.

3. Additionally, when the smoke test fails with an `ImportError`, trigger
   a remediation: re-run the failing step (the one that produced the
   broken module) through the existing rewrite loop with the import error
   as context.

4. Add tests in `tests/test_smoke_test.py`:
   - Create a temp workspace with a valid `app.py` that imports from a
     local module -- verify smoke test passes
   - Create a temp workspace with a broken import chain -- verify smoke
     test returns the ImportError
   - Test entry-point detection from `run.sh`

### Files to modify
- `architect/main.py`

### Files to create
- `tests/test_smoke_test.py`

### Verification
- Run `python -m pytest tests/test_smoke_test.py -v`
- Run `python -m pytest tests/test_validation.py -v` (existing validation tests still pass)

---

## [x] Section 4: CLAUDE.md import consistency guidance

### Problem
`tab_trajectory.py` contains fragile multi-level fallback imports:
```python
try:
    from styles import CHART_COLORS
except ImportError:
    try:
        from chart_helpers import CHART_COLORS
    except ImportError:
        from rehab.src.styles import CHART_COLORS
```
This pattern masks errors and uses inconsistent module paths. The generated
code should use a single, correct import path.

### Root cause
The CLAUDE.md template in `orchestrator/claude_config.py` has no guidance on
import consistency for multi-module projects. Each step's generated code
independently decides how to import from sibling modules, leading to
inconsistent patterns.

### Changes required

**File: `orchestrator/claude_config.py`**

1. Add an `## Import Conventions` section to `CLAUDE_MD_TEMPLATE` after the
   existing `## Coding Standards` section:

   ```
   ## Import Conventions
   When generating modules that are part of a multi-file project:
   - Use consistent package-relative imports throughout the project
     (e.g., always `from src.module import name`, never bare `from module import name`)
   - NEVER use try/except ImportError fallback chains to handle different import paths
     -- this masks real errors. Pick one correct import path and use it.
   - When importing from a sibling module produced by a prior step, use the EXACT
     names listed in the dependency context. Do not rename, alias, or guess.
   - If the dependency context lists `functions: make_card`, import `make_card`,
     not `create_card` or any other variation.
   ```

2. Modify `_format_step_context()` (~line 76) to include module API
   information when prior steps produced `.py` files. Add a subsection
   like:
   ```
   ### Available Module APIs
   - `src/layout_components.py`: functions=[make_card, create_kpi_card, ...], constants=[...]
   - `src/styles.py`: constants=[COLOR_PALETTE, CHART_COLORS, ...], functions=[apply_chart_theme]
   ```
   This requires the step context dict to carry the extracted API info
   (from Section 1).

3. Add tests in `tests/test_claude_config.py`:
   - Verify the template contains the import conventions section
   - Verify `_format_step_context()` includes module APIs when provided
   - Verify the output is valid markdown

### Files to modify
- `orchestrator/claude_config.py`

### Files to create
- `tests/test_claude_config.py` (if it doesn't exist; otherwise extend it)

### Verification
- Run `python -m pytest tests/test_claude_config.py -v`

---

## [x] Section 5: Harden git finalization

### Problem
The rehab project's git repo has no commits despite `ensure_git_repo()` and
`finalize_git()` existing in the codebase. The `.git/` directory exists but
the `uas-wip` branch was never created or the squash merge silently failed.

### Root cause
`ensure_git_repo()` (~line 165) only initializes if the workspace root has
>1 non-dot entry. In the rehab case, the project lives in a subdirectory
(`rehab/rehab/`) and the workspace root may have had only `ALL_SCIDATA.csv`
and `goal_001.txt` at init time. Also, all exceptions are silently caught
and logged at DEBUG level, making failures invisible.

### Changes required

**File: `architect/main.py`**

1. Modify `ensure_git_repo()` to:
   - Lower the threshold from >1 file to >0 files (any non-dot entry
     triggers git init)
   - Also check subdirectories: if any subdirectory contains `.py` files,
     consider it a project worth tracking
   - Log at WARNING level (not DEBUG) when git init fails, including the
     exception details

2. Modify `finalize_git()` to:
   - Log at WARNING level when squash merge fails
   - When `uas-wip` doesn't exist, check if there are uncommitted changes
     on `main` and commit them (handles the case where `ensure_git_repo()`
     ran but `git_checkpoint()` never switched to `uas-wip`)
   - Add a fallback: if squash merge fails, attempt a regular commit of
     all tracked+untracked files on `main`

3. Add a pre-finalize step: before calling `finalize_git()`, check that
   `.gitignore` covers data files (`.csv`, `.joblib`, `.npz`) and other
   artifacts that shouldn't be committed.

4. Add tests in `tests/test_git_finalize.py`:
   - Test `ensure_git_repo()` with a workspace containing only one file
   - Test `ensure_git_repo()` with a workspace containing a subdirectory
     with `.py` files
   - Test `finalize_git()` when `uas-wip` doesn't exist but there are
     uncommitted changes
   - Test `finalize_git()` when squash merge fails

### Files to modify
- `architect/main.py`

### Files to create
- `tests/test_git_finalize.py`

### Verification
- Run `python -m pytest tests/test_git_finalize.py -v`
- Run `python -m pytest tests/test_commit_hygiene.py -v` (existing git tests still pass)

---

## [x] Section 6: Orphaned module detection in project guardrails

### Problem
The rehab project contains `tab_simulator.py` which is generated but never
imported by `app.py` or any other module. The validation report noted it as
orphaned but took no action. This wastes a full step of LLM compute and
confuses the project structure.

### Root cause
`check_project_guardrails()` (~line 1754) and its LLM variant don't check
for orphaned Python modules. The LLM-based project review happened to notice
it, but only as a soft warning in the validation report with no remediation.

### Changes required

**File: `architect/main.py`**

1. Add a function `detect_orphaned_modules(workspace)` that:
   - Finds all `.py` files in the workspace (skip `__init__.py`, test
     files, `conftest.py`, and the entry point)
   - For each file, checks if it's imported by any other `.py` file in
     the workspace (scan for `import <module>` or `from <module> import`)
   - Returns a list of orphaned file paths

2. Call `detect_orphaned_modules()` from `check_project_guardrails()`
   and include results as warnings.

3. In the post-execution phase (~line 2570 where guardrail warnings are
   processed), when an orphaned module is detected AND it was produced by
   the current step, log a specific warning that includes what the module
   exports and which step was expected to import it.

4. Add tests in `tests/test_orphaned_modules.py`:
   - Workspace with all modules imported -- no orphans
   - Workspace with one module not imported by any other -- detected
   - `__init__.py` and entry points are excluded from orphan detection

### Files to modify
- `architect/main.py`

### Files to create
- `tests/test_orphaned_modules.py`

### Verification
- Run `python -m pytest tests/test_orphaned_modules.py -v`
- Run `python -m pytest tests/test_guardrails.py -v` (existing guardrail tests still pass)
