# UAS Improvement Plan

Based on real-world testing with a complex SCI Rehabilitation Analytics project
(`rehab/` directory, kept as reference). Each section is a self-contained unit of
work for a fresh Claude Code session.

Mark sections `[DONE]` as you complete them. Run `python3 -m pytest tests/ -x -q`
after each section to verify no regressions.

---

## Section 1: Disable goal expansion for specific goals `[TODO]`

**Problem:** `expand_goal()` in `architect/main.py` compressed a detailed 5,511-char
goal into a 1,759-char summary that read like a completion report ("All three phases
are complete...") rather than a specification. This lost critical requirements.

**Fix:** Skip expansion when the goal is already specific enough. Add a length or
detail heuristic — if the goal exceeds ~500 chars or contains structured formatting
(numbered lists, markdown headers, code blocks), it's already specific and should
not be expanded. The `expand_goal` prompt already says "If this goal is already clear
and specific, return it unchanged" but the LLM doesn't always comply.

**Files:** `architect/main.py` (the `expand_goal` function and its call site in `main()`)

**Test:** Write a test that passes a detailed multi-paragraph goal to `expand_goal`
and verifies the output preserves the original content (or is identical to it).

---

## Section 2: Coder role emits tool calls instead of code blocks `[TODO]`

**Problem:** The coder role passes `--tools ""` to Claude Code CLI to disable tools,
but the LLM sometimes responds with `<tool_call>` XML anyway. This wastes retry
attempts because `extract_code()` finds no Python fence block. Observed in the rehab
run: attempts 1 and 3 of the final orchestrator run for step 2.

**Fix:** In `orchestrator/main.py`, when `extract_code()` returns None, check if the
response contains `<tool_call>` or `tool_name` patterns. If so, set a more specific
`previous_error` message that tells the LLM: "Your response contained tool calls but
tools are disabled. You MUST respond with a single ```python code fence containing
your complete script." This is better than the generic "Failed to extract code block"
message which doesn't explain what went wrong.

**Files:** `orchestrator/main.py` (the code extraction failure handling in `main()`,
around the "Failed to extract code block" error path)

**Test:** Add a test case with a mock response containing `<tool_call>` XML and verify
the error message mentions tool calls specifically.

---

## Section 3: File modification corruption `[TODO]`

**Problem:** When UAS needs to modify an existing file (e.g., insert MCID code into
`analysis.py`), the generated script often corrupts the file by inserting at the wrong
indentation level or breaking function boundaries. In the rehab run, step 1 of the MCID
addition failed 3 times with format errors from corrupted insertions before succeeding.

**Fix:** Add guidance to the orchestrator's `build_prompt()` for tasks that reference
existing files. When the task description mentions modifying an existing file (detected
by phrases like "modify", "add to", "insert", "update", "extend" + a filename), append
a `<file_modification_guidance>` section to the prompt:

```
When modifying existing files:
1. Read the entire file first to understand its structure
2. Write the COMPLETE modified file, not just the diff or insertion
3. Use a write-then-verify pattern: write the file, then compile-check it
4. Never use string insertion by line number — it's fragile
```

This steers the LLM toward full-file rewrites (which are reliable) rather than surgical
insertions (which are error-prone in generated scripts).

**Files:** `orchestrator/main.py` (`build_prompt()` function)

**Test:** Verify the guidance appears when the task contains modification keywords and
doesn't appear for creation-only tasks.

---

## Section 4: Commit hygiene `[TODO]`

**Problem:** UAS creates messy git histories. The rehab MCID run produced 4 commits
("Step 1", "Step 2", "Step 3", then a summary) for what should have been 1 commit.
The git cleanup runs also produced duplicate commits with identical messages. The
`git_checkpoint()` function in `architect/main.py` commits after every step, leaking
internal process into project history.

**Fix:** Make `git_checkpoint()` create commits on a temporary `uas-wip` branch (or
use `git stash`), and only create a commit on `main` at the end of a successful run.
Add a `finalize_git()` function called at the end of `main()` that squashes all
checkpoint commits into a single commit with a meaningful message derived from the
goal summary. If the run fails, the checkpoints remain on the wip branch for recovery.

**Files:** `architect/main.py` (`git_checkpoint()`, `ensure_git_repo()`, and the
end of `main()`)

**Test:** Mock a multi-step run and verify only one commit appears on `main` at the
end, with checkpoint commits on the wip branch.

---

## Section 5: Add a research phase to the planner `[TODO]`

**Problem:** The decomposition prompt has a `<research>` section encouraging web
research, but in practice no research happens. The rehab project embedded clinical
patterns from internal knowledge only — no literature citations, no web lookups, no
verification of ISNCSCI scoring standards against published sources. The planner has
tools enabled but doesn't use them for research during decomposition.

**Fix:** Add an explicit research step to the architect's `main()` flow, between goal
expansion and decomposition. When the goal is classified as "medium" or "complex":

1. Send the goal to the planner LLM with a research-specific prompt: "Before planning
   implementation, research this domain. Use web search to find current best practices,
   relevant standards, and authoritative sources. Return a structured research summary
   with citations."
2. Store the research output in the state as `research_context`
3. Pass `research_context` into the decomposition prompt so the planner can reference
   specific findings when writing step descriptions

This separates research from planning, making research observable and its results
reusable across steps.

**Files:** `architect/main.py` (new function + call site in `main()`),
`architect/planner.py` (modify `DECOMPOSITION_PROMPT` to accept research context)

**Test:** Mock the LLM client and verify that for a "complex" goal, a research call
is made before decomposition, and its output appears in the decomposition prompt.

---

## Section 6: Workspace path confusion `[TODO]`

**Problem:** Generated scripts sometimes create a project subdirectory inside the
workspace (`/workspace/rehab/`) rather than using the workspace root as the project
root. This causes the `rehab/rehab/` nesting seen in the test project. The planner
prompt now forbids hardcoded paths (from our earlier fix), but the LLM still tends
to create a subdirectory when the goal mentions a project name.

**Fix:** Two changes:

1. In `orchestrator/main.py` `build_prompt()`, when workspace files already exist
   (i.e., this is an iterative run on an existing project), add explicit guidance:
   "The workspace IS the project root. Do not create a subdirectory for the project.
   Write files directly to `os.path.join(workspace, ...)`."

2. In `validate_uas_result()` in `architect/main.py`, when a file is found via
   `os.walk` in a subdirectory but not at the workspace root, log a warning that
   the script may have created an unnecessary subdirectory.

**Files:** `orchestrator/main.py` (`build_prompt()`), `architect/main.py`
(`validate_uas_result()`)

**Test:** Verify the guidance appears when workspace_files is non-empty. Verify the
validation warning triggers when files are in a subdirectory.

---

## Section 7: Leftover script artifacts `[TODO]`

**Problem:** UAS leaves behind script artifacts like `fix_git_structure.py` in the
workspace. These are the generated Python scripts that the sandbox runs, but sometimes
they get written to the workspace instead of `/tmp`.

**Fix:** The `cleanup_workspace_artifacts()` function in `architect/main.py` already
exists (Section 16 in the code). Extend it to also remove any `.py` files in the
workspace root that match UAS naming patterns (e.g., files containing "UAS_RESULT" in
their content, or files that weren't in the workspace before the step started). Use
the pre-step workspace scan to know which files are new.

**Files:** `architect/main.py` (`cleanup_workspace_artifacts()` and its call site
after step completion)

**Test:** Create a temp workspace with a script artifact containing "UAS_RESULT",
run cleanup, verify it's removed. Verify legitimate project `.py` files are not removed.
