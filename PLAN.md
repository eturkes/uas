# UAS Improvement Plan

Issues discovered by analyzing the `rehab/` run output.
Each section is a self-contained task for a coding agent to execute independently.

---

## Section 1: Fix re-plan backward-level bug

**Status: DONE**

**Problem:** When `_post_step_replan_and_enrich` triggers a re-plan, newly added steps
whose dependencies are all already completed get placed in earlier topological levels
than the current `level_idx`. The execution loop uses `continue` (staying at the same
`level_idx`), so those earlier levels are never revisited and the steps are permanently
skipped. This is what caused step 18 to remain `pending` in the rehab run.

**Files:**
- `architect/main.py` — the execution loop (~line 4066-4197) and
  `_post_step_replan_and_enrich` (~line 3900-4064)
- `tests/test_replanning.py` — add a regression test

**Fix:**
After a re-plan regenerates `levels` via `topological_sort`, find the earliest level
that contains a pending step and reset `level_idx` to that level index. Completed
steps are already skipped by the loop body, so restarting from an earlier level is
safe and inexpensive.

In `_post_step_replan_and_enrich`, after `levels = topological_sort(state["steps"])`
succeeds (~line 4035), compute the earliest pending level:

```python
# Find earliest level with a pending step
completed_ids = {s["id"] for s in state["steps"] if s["status"] == "completed"}
for i, lvl in enumerate(levels):
    if any(sid not in completed_ids for sid in lvl):
        earliest_pending_level = i
        break
else:
    earliest_pending_level = len(levels)
```

Then the function needs to communicate this back. Options:
- Return the target `level_idx` instead of `True` (change callers to compare)
- Or set a nonlocal variable that the loop reads

In the while loop, after `_post_step_replan_and_enrich` returns, set `level_idx` to the
returned earliest pending level (instead of keeping the current value via `continue`).

Also fix `replanned_levels` tracking — after a re-plan the level indices change, so
the set is stale. Either clear it after re-planning or remove the once-per-level
restriction entirely (the LLM call to `should_replan_llm` is cheap relative to step
execution).

**Test:** Add a test in `tests/test_replanning.py` that constructs a scenario where a
re-plan adds a step whose deps are all completed, verifying the step is placed before
the current level and does get executed. Use the existing test patterns (mock LLM
client, construct state dict, call `replan_remaining_steps`, then simulate the
execution loop logic).

**Verify:** Run `python -m pytest tests/test_replanning.py -v` — all tests pass.

---

## Section 2: Validate all steps completed before marking run done

**Status: DONE**

**Problem:** At ~line 4199-4202, the run is marked `"completed"` simply because the
level loop finished iterating. It never checks whether every step actually reached
`"completed"` status. This allowed the rehab run to be marked complete with step 18
still `pending`.

**Files:**
- `architect/main.py` (~line 4199-4202)
- `tests/test_resume.py` or a new test

**Fix:**
Before setting `state["status"] = "completed"`, check all steps:

```python
# All done
state["total_elapsed"] = time.monotonic() - run_start

unfinished = [s for s in state["steps"] if s["status"] != "completed"]
if unfinished:
    ids = [s["id"] for s in unfinished]
    logger.error(
        "Execution loop finished but %d step(s) not completed: %s",
        len(unfinished), ids,
    )
    state["status"] = "blocked"
    save_state(state)
    if output_path:
        write_json_output(state, output_path)
    dashboard.finish(state)
    sys.exit(1)

state["status"] = "completed"
save_state(state)
```

**Test:** Add a unit test that constructs a state with one pending step, calls the
completion check logic, and asserts the run status is set to `"blocked"`.

**Verify:** Run `python -m pytest tests/ -k "test_" --co -q` to confirm the new test
is discovered, then run it.

---

## Section 3: Expand gitignore data patterns and verify clean repo

**Status: DONE**

**Problem:** `_ensure_gitignore_data_patterns` only covers `*.joblib` and `*.npz`.
Common data artifacts like `*.csv`, `*.pkl`, `*.parquet`, and `models/` are not
included. The rehab run left data files (including patient CSV and pickled models)
unprotected by the root gitignore. Additionally, `finalize_git` silently swallows all
exceptions and never verifies the repo is clean after finalization.

**Files:**
- `architect/main.py` — `_ensure_gitignore_data_patterns` (~line 335-352) and
  `finalize_git` (~line 379-501)
- `tests/test_git_finalize.py`

**Fix (gitignore patterns):**
Expand `required_patterns` in `_ensure_gitignore_data_patterns`:

```python
required_patterns = [
    "*.csv", "*.pkl", "*.parquet", "*.joblib", "*.npz",
    "*.h5", "*.hdf5", "*.feather", "*.arrow",
    "*.sqlite", "*.db",
    "models/",
]
```

**Fix (finalize verification):**
At the end of `finalize_git`, after the try block's happy path (before the except),
add a post-finalize check:

```python
# Verify repository is clean
porcelain = subprocess.run(
    ["git", "status", "--porcelain"],
    cwd=workspace,
    capture_output=True,
    text=True,
)
if porcelain.stdout.strip():
    logger.warning(
        "Git repo still dirty after finalize:\n%s",
        porcelain.stdout[:500],
    )
```

This is a warning, not an error, so it won't break the run but will surface in logs.

**Test:** Update `TestEnsureGitignoreDataPatterns` in `tests/test_git_finalize.py` to
assert the new patterns are covered. Add a test that verifies `finalize_git` produces
a clean repo (no untracked data files) when data files exist alongside a proper
gitignore.

**Verify:** Run `python -m pytest tests/test_git_finalize.py -v` — all tests pass.

---

## Section 4: Sanitize files from `extract_workspace_files`

**Status: DONE**

**Problem:** `_sanitize_files_written` (strips trailing parenthesized annotations like
`(symlink)`) is only applied to files from the UAS_RESULT JSON. Files extracted via
`extract_workspace_files` (regex-based extraction from orchestrator stderr) are merged
without sanitization, causing `validate_uas_result` to fail on paths with annotations.

**Files:**
- `architect/main.py` (~line 3090-3104)
- `tests/test_executor.py` or `tests/test_output.py`

**Fix:**
Apply `_sanitize_files_written` to the regex-extracted files as well, right after
extraction:

```python
step["files_written"] = _sanitize_files_written(
    extract_workspace_files(result["stderr"])
)
```

This is a one-line change at ~line 3090-3092.

**Test:** Add a test that feeds orchestrator output containing annotated paths
(e.g., `/workspace/data/file.csv (symlink)`) to `extract_workspace_files`, pipes it
through `_sanitize_files_written`, and verifies the annotation is stripped.

**Verify:** Run `python -m pytest tests/test_executor.py tests/test_output.py -v` — all
tests pass.

---

## Section 5: Reset `executing` steps to `pending` on resume

**Status: DONE**

**Problem:** When UAS is interrupted mid-step, that step's status is `"executing"`.
On resume, the execution loop only special-cases `"completed"` steps (skips them);
everything else goes to the pending list. This works by accident for `"executing"`
steps (they are not `"completed"` so they end up in `pending`), but it's fragile and
undocumented. A defensive reset makes the intent explicit.

**Files:**
- `architect/main.py` — `try_resume` (~line 3661-3676)
- `tests/test_resume.py`

**Fix:**
In `try_resume`, after loading the state, reset any `"executing"` steps back to
`"pending"`:

```python
for step in state.get("steps", []):
    if step["status"] == "executing":
        logger.info(
            "Resetting interrupted step %s (%s) to pending.",
            step["id"], step["title"],
        )
        step["status"] = "pending"
        step["started_at"] = None
```

Also reset the run-level status from `"executing"` to `"executing"` (no-op) but log
that a resume is happening.

**Test:** Add a test in `tests/test_resume.py` that creates a state with one step in
`"executing"` status, calls `try_resume`, and asserts the step is reset to `"pending"`.

**Verify:** Run `python -m pytest tests/test_resume.py -v` — all tests pass.

---

## Section 6: Run full test suite and fix regressions

**Status: DONE**

**Problem:** Sections 1-5 touch core execution logic. A full test suite run is needed
to catch any regressions.

**Steps:**
1. Run `python -m pytest tests/ -v --tb=short 2>&1 | tail -60`
2. Fix any failures introduced by the changes in sections 1-5
3. Re-run until clean

**Verify:** `python -m pytest tests/ -v` exits with code 0.
