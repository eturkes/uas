# UAS Improvement Plan

Iterative improvement plan for the Universal Agentic Specification project.
Each step is self-contained and completable in a single session.

## Steps

### Step 1: Add unit test framework and tests for pure-logic modules `[x]`

- Add `pytest` to `requirements.txt`.
- Create `tests/` directory with `conftest.py`.
- Write unit tests for `orchestrator/parser.py` (`extract_code`).
- Write unit tests for `architect/planner.py` (`parse_steps_json`).
- Write unit tests for `architect/state.py` (`init_state`, `save_state`, `load_state`, `add_steps`).
- Write unit tests for `architect/spec_generator.py` (`generate_spec`, `build_task_from_spec`).
- Write unit tests for `architect/main.py` (`build_context`).
- Remove any committed `__pycache__/` files from git tracking.
- Verify `.gitignore` covers `tests/` artifacts (e.g. `.pytest_cache/`).
- **Test**: `python -m pytest tests/ -v`

### Step 2: Add structured logging `[x]`

- Replace all `print()` calls with Python `logging` module.
- Configure log levels: DEBUG for code dumps, INFO for progress, ERROR for failures.
- Add a `--verbose` / `-v` flag for debug output.
- Keep stdout clean for piping (logs go to stderr).
- Update README with logging info.
- **Test**: `python -m pytest tests/ -v`

### Step 3: Add plan resumability `[x]`

- In `architect/main.py`, call `load_state()` at startup and resume from the last incomplete step.
- Add a `--resume` CLI flag and `UAS_RESUME` env var.
- Add a `--fresh` flag to force a clean start (current behavior).
- Handle edge cases: corrupted state file, missing spec files.
- Update README.
- **Test**: `python -m pytest tests/ -v`

### Step 4: Improve Orchestrator prompts and code extraction `[ ]`

- Rewrite `build_prompt()` with a clearer, more structured prompt template.
- Include Python version, workspace path example code, and explicit constraints.
- Improve `extract_code()` to handle multiple code blocks (pick the longest Python one).
- Add better fallback detection logic.
- Fix `spec_generator.py` duplicated description in "Objective" and "Task" sections.
- **Test**: `python -m pytest tests/ -v`

### Step 5: Add input validation and error output standardization `[ ]`

- Add goal/task length validation (warn on very long inputs).
- Standardize error truncation to a configurable constant.
- Add graceful container cleanup on timeout (kill the container by name).
- Validate `depends_on` references in planner output (no circular deps, no out-of-range refs).
- **Test**: `python -m pytest tests/ -v`

### Step 6: Add parallel step execution `[ ]`

- In `architect/main.py`, build a DAG from `depends_on` fields.
- Execute independent steps concurrently using `concurrent.futures.ThreadPoolExecutor`.
- Maintain correct context propagation for dependent steps.
- Add topological sort validation.
- **Test**: `python -m pytest tests/ -v`
