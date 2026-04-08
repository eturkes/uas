# UAS Bind-Mount Workspace Recovery Plan

## Background

A run of `uas --goal-file goal_001.txt` from `/home/eturkes/pro/uas/rehab/`
failed during the very first step. Investigation traced two independent
bugs that compound each other:

1. **Git "dubious ownership" inside the container.** The container runs as
   root, but `/workspace` is bind-mounted from the host (UID 1000). When
   `architect.main.ensure_git_repo()` runs, `git init -b main` succeeds
   (init bypasses the safe.directory check), but the very next command
   `git add -A` fails with `fatal: detected dubious ownership in repository
   at '/workspace'`, exit status 128. The function catches the exception
   and returns silently, leaving `.git/` half-initialized (post-`init`,
   pre-`commit`, no `uas-wip` branch). On every subsequent step,
   `architect.git_state.create_attempt_branch()` calls `git branch --list
   uas-wip` which also fails with exit 128 for the same reason. The step
   fails after 3 attempts.

2. **LLM emits no extractable code block.** The orchestrator invokes
   Claude Code CLI as the LLM with all tools enabled. The prompt instructs
   the LLM to "use tools freely" AND to "generate a complete, self-contained
   Python script in a ```python code fence". When the LLM has Write/Edit
   tools available it sometimes performs the task by writing files directly
   and replies with prose ("Created tests/test_config.py with 6 test
   functions"), so `orchestrator.parser.extract_code()` finds nothing to
   extract and the attempt is wasted. This was reproduced across all three
   attempts in the failed run.

## What was already fixed

The primary git failure was fixed in commit/working-tree by patching the
`Containerfile`:

- Added `git config --system --add safe.directory '*'`
- Added `git config --system user.email 'uas@local'`
- Added `git config --system user.name 'UAS Orchestrator'`
- Added `git config --system init.defaultBranch main`

Once the container image is rebuilt, fresh runs of `uas` against any
bind-mounted host workspace will no longer hit the dubious-ownership trap
and `git commit` inside the workspace will work without per-project
configuration.

Verification: this was reproduced in `docker run alpine/git ...` with a
host-owned bind mount, both before and after applying the equivalent git
config, and confirmed that `git add -A` / `git commit` go from exit 128
back to exit 0.

## Remaining sections

Each section below is an independent, self-contained chunk that can be
completed in a fresh coding-agent session. Mark each section as
`[COMPLETED]` in this file when finished, leaving the rest of the section
text intact for posterity.

---

### Section 1: Make `ensure_git_repo` repair partial git state  [COMPLETED]

**Why:** The Containerfile fix prevents NEW runs from hitting the dubious-
ownership trap, but any workspace that already has a half-initialized
`.git/` directory from a prior failed run (such as the existing `rehab/`)
will still skip initialization because `ensure_git_repo` returns early
when `.git/` exists. The function needs to detect partial state and finish
the work it didn't get to last time. This change is general-purpose and
benefits any user who upgrades the image after a previously failed run.

**Files to modify:**

- `architect/main.py`, function `ensure_git_repo` (around line 299)

**What "partial state" means here:** any of the following indicates a
broken repo that needs repair, not an already-set-up one to be left alone:

- `.git/HEAD` exists but the branch it references has no commit yet
  (i.e. `git rev-parse HEAD` exits non-zero or returns nothing useful).
- The `uas-wip` branch does not exist (`git show-ref --verify
  refs/heads/uas-wip` fails) AND there is no `uas-main` tag.
- `git status --porcelain` works but `git log -1` fails — i.e. an empty
  repo.

A repo with at least one commit AND a `uas-wip` branch (or where uas-wip
is intentionally absent because finalize_git already squashed it back into
main and removed it) should be left alone.

**Required behavior after change:**

1. If `.git/` does not exist → current behavior (run full init).
2. If `.git/` exists and `git log -1` succeeds AND `git show-ref --verify
   refs/heads/uas-wip` succeeds → return without doing anything (current
   behavior for healthy repos).
3. If `.git/` exists but the repo has no commits → re-run the missing
   steps: `git add -A`, `git commit -m "Initial workspace state"`,
   `git tag -f uas-main`, `git checkout -b uas-wip` (creating the wip
   branch only if it doesn't already exist; if it does exist already,
   just `git checkout uas-wip`).
4. If `.git/` exists and has commits but `uas-wip` does not exist AND
   `uas-main` tag is missing → this indicates the previous run was
   interrupted between init and wip creation. Create `uas-wip` from the
   current HEAD and tag the initial commit as `uas-main` if no other tag
   exists. If `uas-main` already exists or finalize_git was already called
   (squash-merged into main and uas-wip deleted), leave it alone — that's
   a healthy "between-runs" state.
5. All git operations must continue to use `subprocess.run(..., cwd=
   workspace, capture_output=True)` and tolerate non-zero exits without
   raising past the function boundary; the existing `try/except Exception`
   wrapper plus per-command error handling is the model to follow.

**Tests to add or update:**

- `tests/test_git_state.py` or a new dedicated test file. Use the same
  monkeypatched `GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars the existing
  tests use.
- New test cases:
  1. Half-initialized repo (init only, no commit) → after
     `ensure_git_repo` runs the repo has at least one commit and a
     `uas-wip` branch.
  2. Healthy repo on `uas-wip` with one commit → `ensure_git_repo` is a
     no-op (commit count and branch unchanged).
  3. Repo where `finalize_git` already squashed `uas-wip` away (only
     `main` exists, `uas-wip` and `uas-main` tag are absent) →
     `ensure_git_repo` recognizes this as the "post-finalize" state and
     re-creates `uas-wip` from current HEAD without re-tagging
     `uas-main`.

**Acceptance criteria:**

- All existing `tests/test_git_state*.py`, `tests/test_git_finalize.py`,
  and `tests/test_commit_hygiene.py` continue to pass.
- The three new test cases pass.
- No hard-coded references to `rehab` or any other specific project name.

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]`.

---

### Section 2: Restrict LLM tools so the orchestrator always gets a code block  [COMPLETED]

**Why:** Even with git fixed, the orchestrator's "TEXT-only" code-generation
contract is fragile. The LLM client invokes Claude Code with all tools
enabled, and the prompt simultaneously tells the LLM "USE TOOLS FREELY"
and "generate a complete, self-contained Python script in a ```python code
fence". When Write/Edit tools are available, the LLM sometimes does the
task with tools and replies with prose. The parser then has nothing to
extract and the attempt is wasted. This was the secondary cause of the
rehab run failure: every one of the three attempts produced ~1850 output
tokens with no extractable code.

The fix is to disable file-modification tools in the LLM subprocess so the
LLM has no choice but to put the script in its text response. Read-only
research tools (Read, Grep, Glob, WebSearch, WebFetch) and Bash should
remain available so the LLM can still verify package versions, read API
docs, and run quick checks.

**Files to modify:**

- `orchestrator/llm_client.py`, method `ClaudeCodeClient.generate`
  (around line 152). The cmd-list construction is around line 166.
- `orchestrator/claude_config.py` if you also want to remove the
  contradictory CLAUDE.md instructions ("ALL TOOLS ENABLED" vs. "Do NOT
  use Write, Edit, or Bash tools to create files") — clean it up so it
  matches the new tool restriction.

**Required change:**

Add `--disallowed-tools` to the cmd list, e.g.:

```python
cmd.extend(["--disallowed-tools", "Write", "Edit", "NotebookEdit"])
```

(Verify the exact arg-list shape against `claude --help` — the flag
takes either comma- or space-separated tool names.) Do NOT block `Bash`,
`Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, or `Task`. The intent is
to prevent the LLM from creating files in the response cycle, not to
prevent it from researching.

If `--tools` is preferred over `--disallowed-tools`, an explicit allowlist
also works:

```python
cmd.extend(["--tools", "Bash", "Read", "Grep", "Glob",
            "WebSearch", "WebFetch", "Task"])
```

Pick whichever produces a clearer failure when the LLM tries to call a
blocked tool (the user-visible error matters because it affects how the
reflexion loop classifies the error).

**Tests to add or update:**

- `tests/test_llm_isolation.py` already asserts properties of the CLAUDE.md
  template; extend it to assert that `ClaudeCodeClient.generate` builds a
  cmd that includes the disallowed-tools flag (use a unit test that
  monkeypatches `subprocess.run` and inspects the captured `cmd` list).
- Add a regression test for `parser.extract_code` against a sample LLM
  response that contains only prose and no code block — confirm it returns
  None so the orchestrator's existing `previous_error = "Failed to extract
  code block from LLM response."` path keeps working as a safety net.

**Acceptance criteria:**

- All existing tests pass.
- New test asserts the cmd list contains the disallowed/allowed tool flag.
- A live integration test (e.g. `integration/quick_test.sh` or the
  `integration/test_project_quality.py` flow) demonstrates that the LLM
  produces a `​```python` code block at least 95% of the time on the
  first attempt across a small sample.
- The CLAUDE.md template no longer says "ALL TOOLS ENABLED" if tools are
  in fact restricted — the contradiction must be resolved one way or the
  other.

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]`.

---

### Section 3: Clean up the broken rehab workspace and re-run end-to-end  [PENDING]

**Why:** The existing `rehab/.git` directory is in the half-initialized
state described in the Background section. Without removing it, even after
Section 1 lands the user's existing run state will still need a manual
nudge before a fresh `uas` invocation will succeed against `rehab/`. This
section is the verification step that proves Sections 1 and 2 actually
fix the problem on the original failing project.

**Steps:**

1. Rebuild the container image so it picks up the Containerfile fix:
   ```
   bash /home/eturkes/pro/uas/install.sh
   ```
2. Inspect `/home/eturkes/pro/uas/rehab/.git/`. If it is still in the
   half-initialized state (no objects, no refs/heads/main), remove it and
   the failed-run state so the next `uas` run starts cleanly:
   ```
   rm -rf /home/eturkes/pro/uas/rehab/.git
   rm -rf /home/eturkes/pro/uas/rehab/.uas_state
   ```
   Confirm with the user before deleting. If they want to keep the failed
   run state for forensics, copy it aside first.
3. From inside the rehab directory, re-run:
   ```
   cd /home/eturkes/pro/uas/rehab
   uas --goal-file goal_001.txt
   ```
4. Watch the first 2-3 steps. Success criteria:
   - No "Failed to create attempt branch" warnings.
   - No "Failed to extract code block from LLM response" errors.
   - At least one step completes successfully and the orchestrator advances.
5. If new failures appear, capture them and add a Section 4 to this plan
   describing the new failure mode. Do NOT bypass the new failure with
   workarounds — root-cause it.

**Acceptance criteria:**

- `rehab/.uas_state/runs/<new_run_id>/progress.md` shows at least one
  completed step.
- The git failure described in the Background section does not recur.
- The "Failed to extract code block" failure described in the Background
  section does not recur on attempt 1 of any step.

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]` and append a one-paragraph result summary at the bottom of
the section.

**Status note (2026-04-07):** Verification was attempted end-to-end. The
container image was rebuilt via `install.sh`, the broken `rehab/.git/` and
`rehab/.uas_state/` were moved aside (to
`/tmp/uas-rehab-backup/.uas_failed_run_backup_20260407_044821/`), and `uas
--goal-file goal_001.txt` was launched against `rehab/`. Two of the three
acceptance criteria are now verifiable:

- **Git failure: FIXED.** After resuming the run with the backup directory
  moved out of the workspace, `ensure_git_repo` correctly entered its
  Section 1 "half-initialized repair" branch, ran
  `add`/`commit`/`tag uas-main`/`checkout -b uas-wip`, and the orchestrator
  successfully created `refs/heads/uas/step-1/attempt-{1,2,3}` with no
  "Failed to create attempt branch" warnings. Section 1's repair logic is
  confirmed working in practice.
- **Code-block extraction: STILL FAILING.** All three attempts of step 1
  failed with the exact error from the Background section: `Failed to
  extract code block from LLM response.` Acceptance criterion 3 is not
  met. Per step 5 of this section ("If new failures appear, capture them
  and add a Section 4..."), the root cause is captured below in
  Section 4. Section 3 stays `[PENDING]` until Section 4's fix lands and
  this verification can be re-attempted.

A discovery during the verification: putting the backup `.git/` directory
**inside** the workspace (e.g.
`rehab/.uas_failed_run_backup_*/`) causes the next `git add -A` to fail
with `error: '...does not have a commit checked out'` because git treats
embedded `.git/` directories as submodules. Backup forensics directories
must always be moved **outside** the workspace before re-running `uas`.
This is a process note for whoever re-runs Section 3, not a code bug.

**Status note (2026-04-07, after Section 4 landed):** Section 4 was
implemented and verified. The third acceptance criterion of Section 3
(`"Failed to extract code block" failure does not recur on attempt 1 of
any step`) is now **CONCLUSIVELY MET** for `rehab/`:

- Container rebuilt via `install.sh` after the Section 4 changes to
  `orchestrator/llm_client.py`, `orchestrator/main.py` (`build_prompt`,
  `_build_retry_clean_prompt`, `_contains_tool_calls`),
  `orchestrator/claude_config.py`, and `architect/planner.py`
  (`REFLECTION_GEN_PROMPT`).
- Re-run via `cd rehab && uas --resume --goal-file goal_001.txt`
  against the same `fa0d38fa9ef6` run state.
- All 3 attempts of step 1 produced extractable Python scripts. All 3
  scripts ran in the sandbox with `Exit code: 0` and printed
  `UAS_RESULT: {"status": "ok", ...}` listing the 11 expected files
  (`pyproject.toml`, `.python-version`, `.gitignore`, `CLAUDE.md`,
  `README.md`, and the 6 `__init__.py` files under `src/rehab/`).
- `grep -c "Failed to extract code block" /tmp/uas-section4-v3.log` =
  `0`. The original failure mode is gone.

However, Section 3's **first** acceptance criterion (`progress.md shows
at least one completed step`) is **STILL NOT MET**. After the
script-generation pipeline produces a usable code block and the sandbox
executes it successfully, the orchestrator's post-execution
**lint pre-check** rejects the workspace state with
`F401 [*] os imported but unused` pointing at
`rehab/tests/test_config.py:4`. That file is dated `Apr  7 03:58` —
it predates today's work and was left over from the user's original
failed run. The lint check sees a pre-existing file with unused imports
and marks the entire step's execution as `revert_needed: true,
error_category: lint_fatal`, which rolls the workspace back and the
attempt is recorded as failed even though the LLM-generated script ran
to success.

This is a **new, distinct failure mode** unrelated to anything in this
PLAN. Per step 5 of this section, it should be captured in a new
Section 5: "Stop the lint pre-check from rejecting pre-existing
workspace files." Until that section lands, Section 3 stays `[PENDING]`
because it cannot meet its first acceptance criterion. Sections 1, 2,
and 4 are conclusively complete.

**Status note (2026-04-07, after re-verification and Section 5 capture):**
The lint pre-check failure mode described above was re-verified and
formally captured as Section 5 of this PLAN. Re-verification details:

- The `rehab/.uas_state/runs/fa0d38fa9ef6/state.json` from the prior
  attempt still records all 3 step-1 attempts failing with
  `lint_fatal` against `tests/test_config.py:4` (`F401 [*] os imported
  but unused`) and `tests/test_config.py:7`
  (`F401 [*] pytest imported but unused`).
- `git log -- orchestrator/main.py uas/janitor.py` shows the most
  recent touch is commit `8169446` ("Force coder LLM to emit
  extractable code blocks") from Section 4. No fix has landed for the
  lint pre-check since the previous verification, so a fresh re-run
  would deterministically reproduce the same failure.
- Direct reproduction outside the container with
  `ruff check --select=F --no-fix --no-cache --quiet -- tests/test_config.py`
  (the same flags `uas/janitor.py:96` uses) prints both `F401` errors,
  confirming the file alone is sufficient to trigger the failure.
- `git ls-files` inside `rehab/` confirms `tests/test_config.py`,
  `src/rehab/config.py`, and `run_tests.py` are all part of the
  "Initial workspace state" commit on `uas-wip`, so every rollback
  restores them and re-poisons the next attempt.

Section 3 remains `[PENDING]` and is now formally blocked on Section 5.
Once Section 5 lands, re-run `cd rehab && uas --resume --goal-file
goal_001.txt` and confirm `progress.md` records at least one completed
step before flipping Section 3 to `[COMPLETED]` with a final result
paragraph.

**Status note (2026-04-07, after Section 5 landed — fourth attempt):**
Section 5 was committed as `6b1b721` and the container image was rebuilt
via `install.sh` (image timestamp `2026-04-07 09:22:29`, after the Section
5 commit at `09:19:14`). Re-ran end-to-end against the same `fa0d38fa9ef6`
run state by invoking the orchestrator subprocess directly (the
`~/.local/bin/uas` wrapper requires `-it` and is unusable from a
non-TTY context — this is a process note, not a code bug):

```
docker run -d --privileged --name uas-run-1aac882308ef \
  -e IS_SANDBOX=1 -e UAS_SANDBOX_MODE=local \
  -e UAS_HOST_UID=$(id -u) -e UAS_HOST_GID=$(id -g) \
  -v /home/eturkes/pro/uas/rehab/.uas_auth:/root/.claude:Z \
  -v /home/eturkes/pro/uas/rehab/.uas_auth/claude.json:/root/.claude.json:Z \
  -v /home/eturkes/pro/uas/rehab:/workspace:Z \
  -w /workspace uas-engine:latest --resume --goal-file goal_001.txt
```

Two acceptance criteria progressed; the third did not:

- **Git failure: STILL FIXED.** No "Failed to create attempt branch"
  warnings. The orchestrator successfully created
  `refs/heads/uas/step-1/attempt-1` from `uas-wip`. Section 1's repair
  logic still works.
- **Code-block extraction: STILL FIXED.** Step 1 attempt 1's main
  orchestrator subprocess returned exit code 0 in 69.2s after running the
  LLM-generated Python script in the sandbox. The script wrote all 11
  expected files (`pyproject.toml`, `.python-version`, `.gitignore`,
  `CLAUDE.md`, `README.md`, six `__init__.py` files under `src/rehab/`)
  and the lint pre-check from Section 5 correctly scoped to those files
  and passed. The "Failed to extract code block" failure does NOT recur.
- **First completed step: STILL NOT MET.** Immediately after the main
  script succeeded, the architect ran `verify_step_output` which spawns
  a second orchestrator subprocess to generate and run a verifier
  script. The verifier script printed `VERIFICATION PASSED` and exited
  0, but the verification orchestrator subprocess still exited with
  code 1 because the **same Section 5 lint pre-check** fired against
  the workspace's pre-existing `tests/test_config.py` and friends. The
  architect's `verify_step_output` saw `exit_code != 0` and returned
  the verifier's stdout (`"VERIFICATION PASSED"`) verbatim as the error
  string, which is why the architect log line reads
  `Step 1 FAILED. Error: VERIFICATION PASSED` — a confusing surface
  symptom of an invisible (to the architect) lint failure inside the
  verification orchestrator subprocess.

Direct reproduction of the verification-side failure outside the
end-to-end run, against the post-step-1 workspace:

```
docker run --rm -v /home/eturkes/pro/uas/rehab:/workspace:Z \
  -e IS_SANDBOX=1 -e UAS_SANDBOX_MODE=local \
  -e UAS_TASK="Write a Python verification script that checks: \
pyproject.toml exists. Print 'VERIFICATION PASSED' if all checks pass, \
exit 0. The script must be READ-ONLY." \
  -w /workspace --entrypoint /bin/bash uas-engine:latest \
  -c "PYTHONPATH=/uas python3 -m orchestrator.main"
```

prints (per attempt, all 3 attempts):

```
===STDOUT_START===
pyproject.toml exists at /workspace/pyproject.toml
VERIFICATION PASSED

===STDOUT_END===
Lint pre-check found 18 fatal error(s):
  F401 [*] `os` imported but unused
   --> tests/test_config.py:4:8
  ...
ExecutionResult: {"success":false,"revert_needed":true,
  "error_category":"lint_fatal", ...}
FAILED on attempt N.
```

After `MAX_RETRIES`, the orchestrator subprocess exits 1 and rolls back
the workspace to `uas-wip`, undoing the main step's file writes.

Root cause: Section 5's fix at `orchestrator/main.py:1908-1914` only
scopes lint to `py_files_written` when `parse_uas_result` returns a
non-None result. Verifier scripts have no reason to print `UAS_RESULT`
(the verifier prompt at `architect/main.py:3761-3787` doesn't ask for
it), so `parse_uas_result` returns None, `py_files_written` stays None,
and the orchestrator falls through to `lint_workspace(_workspace)` —
the legacy "lint everything" path Section 5 was supposed to retire.

This is a **new, distinct failure mode** unrelated to anything
previously captured. Per step 5 of this section, it is captured as
Section 6 below: "Stop the lint pre-check from re-poisoning verification
orchestrator runs". Section 3 stays `[PENDING]` until Section 6's fix
lands and this verification can be re-attempted. The current rehab
workspace is left on branch `uas/step-1/attempt-1` with the failed run
state intact for forensics; do NOT delete it without user confirmation.

**Status note (2026-04-07, after Section 6 landed — fifth attempt):**
Section 6 was committed as `41fe5cf` ("Scope lint pre-check via git
diff vs uas-wip"). Verified the container image at
`uas-engine:latest` (timestamp `2026-04-07 09:57:46 EDT`, after the
Section 6 commit) actually contains the new code:

- `docker run --rm --entrypoint /bin/bash uas-engine:latest -c "grep -l
  changed_py_files_since_uas_wip /uas/architect/git_state.py"` prints
  the path.
- `grep -n "changed_py_files_since_uas_wip\|files_to_lint"
  /uas/orchestrator/main.py` inside the container returns the
  Section-6 helper import at line 26 plus the rewritten
  `files_to_lint` block at lines 1914-1925.

Re-ran end-to-end against the same `fa0d38fa9ef6` run state by
invoking the engine directly (the `~/.local/bin/uas` wrapper still
requires `-it`):

```
docker run -d --privileged --name uas-run-section3-final \
  -e IS_SANDBOX=1 -e UAS_SANDBOX_MODE=local \
  -e UAS_HOST_UID=$(id -u) -e UAS_HOST_GID=$(id -g) \
  -v /home/eturkes/pro/uas/rehab/.uas_auth:/root/.claude:Z \
  -v /home/eturkes/pro/uas/rehab/.uas_auth/claude.json:/root/.claude.json:Z \
  -v /home/eturkes/pro/uas/rehab:/workspace:Z \
  -w /workspace uas-engine:latest --resume --goal-file goal_001.txt
```

Process artifact (not a code bug, but worth recording so the next
person doesn't waste an hour on it): the rehab workspace's OAuth
token at `rehab/.uas_auth/.credentials.json` had `expiresAt =
2026-04-07T14:06:23 UTC`, which is in the past relative to the run
time of `2026-04-07T23:30 UTC`. The first docker invocation died
within 60s with three `401 Invalid authentication credentials`
errors (one per orchestrator attempt) followed by the same in the
planner role. Remediation: copy the host's freshly-issued token
into the workspace via
`cp ~/.claude/.credentials.json
rehab/.uas_auth/.credentials.json`. The host token at the time of
this run was issued `Apr  7 19:17` with `expiresAt =
2026-04-08T07:17 UTC`. Re-running the docker command after the
copy proceeded past auth.

Five of the six things being verified worked exactly as the prior
sections promised. One did not:

- **Git failure (Section 1): STILL FIXED.** No "Failed to create
  attempt branch" warnings on the resumed run. The orchestrator
  subprocess for step 1 attempt 1 successfully created a fresh
  attempt branch from `uas-wip`.
- **Code-block extraction (Sections 2 + 4): STILL FIXED.** Step 1
  attempt 1 produced an extractable Python script. The script ran
  in the sandbox, performed `uv sync`, installed all 11 expected
  dependencies (pandas, numpy, lightgbm, scikit-learn, shap, dash,
  plotly, scipy, statsmodels, etc.), and printed
  `UAS_RESULT: {"status": "ok", "files_written": [...]}` listing
  the 11 expected scaffold files (`pyproject.toml`, `.python-version`,
  `.gitignore`, `CLAUDE.md`, `README.md`, six `__init__.py` files
  under `src/rehab/`).
- **Main-step lint pre-check (Section 5): STILL FIXED.** No
  `Lint pre-check found ... fatal error(s)` warning fired against
  `tests/test_config.py:4 F401`. The orchestrator's main step
  subprocess exited 0.
- **Verifier-side lint pre-check (Section 6): STILL FIXED.** The
  architect's `verify_step_output` spawned the verifier
  orchestrator subprocess; the verifier script printed
  `VERIFICATION PASSED`; the verifier orchestrator's lint
  pre-check correctly skipped (UAS_RESULT empty + no `.py` files
  changed since `uas-wip` → no files to lint); the verifier
  subprocess exited 0. The "Step N FAILED. Error: VERIFICATION
  PASSED" surface symptom from the fourth attempt does NOT recur.
- **First completed step (Section 3 acceptance criterion 1): STILL
  NOT MET.** Immediately after the verifier subprocess returned
  success and the architect logged
  `Removed 11866 step artifact(s)` (the `.venv/` files installed
  by `uv sync`, which the architect's artifact cleanup correctly
  identified as not part of the step's intended outputs), it
  proceeded to its next post-step gate: the **full pytest suite**.

That gate is implemented at `architect/main.py:5175-5190`:

```
# Phase 4.6: Run full test suite after corrections.
# Only for non-test steps — test steps just write tests.
if config.get("tdd_enforce") and failure_reason is None and not step.get(
    "title", ""
).strip().lower().startswith("test:"):
    logger.info("  Running full pytest suite...")
    ...
    full_suite_err = _run_full_pytest_suite(PROJECT_DIR)
    if full_suite_err:
        logger.error(
            "  Full test suite failed after step %s.", step["id"]
        )
        failure_reason = full_suite_err
```

`_run_full_pytest_suite` at `architect/main.py:4546-4581` calls
`_discover_all_test_files(workspace)` and runs `python -m pytest`
unconditionally on every test file the discoverer returns. There is
no scoping by which tests this attempt actually wrote, which tests
were committed to `uas-wip` from the user's earlier failed run, or
which modules the current step is supposed to have built.

In the rehab workspace, `tests/test_config.py` was committed to
`uas-wip` from the user's original run before `uas` was even
invoked again. It imports `from rehab.config import PROJECT_ROOT,
DATA_DIR, TRANSLATIONS_DIR` — but `rehab/config.py` is **not** part
of step 1's outputs; it is scheduled for a later step in the
plan. The full pytest suite collects `tests/test_config.py`,
runs it, and gets:

```
__________________________ test_project_root_is_path ___________________
tests/test_config.py:12: in test_project_root_is_path
    from rehab.config import PROJECT_ROOT
E   ModuleNotFoundError: No module named 'rehab'
... (5 more identical-root-cause failures)
6 failed in 0.04s
```

The architect interprets the pytest failure as step 1's failure,
records the error verbatim
(`"Full test suite FAILED after this step's corrections..."`), and
enters its rewrite loop. I observed two consecutive rewrites
(`rewrite 1/4`, `rewrite 2/4`) re-execute step 1, each producing a
clean main-step + verifier pass and then dying on the same
`_run_full_pytest_suite` failure for the same reason. After the
fourth rewrite + retry budget exhausted, the architect set
`state["status"] = "blocked"` and `step 1 status = "failed"`,
deleted the `uas/step-1/attempt-N` branches, hard-reset the
working tree to `uas-wip`, and the entrypoint subprocess exited 1.

Final state.json snapshot recorded in
`rehab/.uas_state/runs/fa0d38fa9ef6/state.json`:

```
Run status: blocked
Step 1 status: failed
Step 1 rewrites: 2
Step 1 reflections: 9
error: "Full test suite FAILED after this step's corrections.\n
        pytest exit code: 1\n
        stdout (last 3000 chars):\nFFFFFF ...\n
        ModuleNotFoundError: No module named 'rehab'\n..."
```

Section 3's first acceptance criterion (`progress.md shows at
least one completed step`) is **STILL NOT MET**, because the
architect's full-pytest gate is now the deterministic blocker on
this workspace. Section 3's second criterion (no git failure) is
met. Section 3's third criterion (no extract-code-block failure)
is met.

This is a **new, distinct failure mode** unrelated to anything
previously captured. Per step 5 of this section, it is captured
as **Section 7** below: "Stop the architect's full-pytest gate
from failing on pre-existing test files referencing not-yet-built
modules". Section 3 stays `[PENDING]` until Section 7's fix lands
and this verification can be re-attempted.

Note for whoever ships Section 7: the architect's
`try_resume()` at `architect/main.py:5773-5796` only resets steps
in `executing` status to `pending`. The current rehab state has
step 1 in `failed` status and run status `blocked`. A clean
re-verification will likely require either (a) extending
`try_resume` to also reset `failed` → `pending` when the run is
`blocked`, or (b) manually clearing
`rehab/.uas_state/runs/fa0d38fa9ef6/state.json`'s step 1 status
back to `pending` before re-running. This is a follow-up concern,
not a Section 7 design requirement, but worth flagging.

The current rehab workspace was rolled back **by the architect's
own failure handler** to `uas-wip` (clean working tree, no orphan
attempt branches), with only `state.json` carrying the failed run
record. Do not delete `rehab/.uas_state/runs/fa0d38fa9ef6/`
without user confirmation.

---

### Section 4: Stop the LLM from creating files via Bash redirection  [COMPLETED]

**Why:** Section 2 added `--disallowed-tools Write Edit NotebookEdit` to
the LLM subprocess and updated `CLAUDE.md` to instruct the LLM to put its
script in a fenced code block. Verification in Section 3 shows the fix is
**insufficient**: the LLM still bypasses the restriction by using `Bash`
to create files via shell redirection (`echo > file`, `cat <<EOF`,
`uv sync`, etc.) and then replies with prose ("I created the files"). The
orchestrator's `extract_code()` finds nothing, every attempt is wasted,
and the failure mode from the Background section recurs verbatim.

This was confirmed by inspecting the LLM isolation directory of the
running container (`/tmp/uas_llm_<rand>/`) during a live attempt:

```
/tmp/uas_llm__p0g8af9/src/rehab/__init__.py
/tmp/uas_llm__p0g8af9/src/rehab/data/__init__.py
/tmp/uas_llm__p0g8af9/src/rehab/dashboard/__init__.py
/tmp/uas_llm__p0g8af9/pyproject.toml
/tmp/uas_llm__p0g8af9/.python-version
/tmp/uas_llm__p0g8af9/.gitignore
/tmp/uas_llm__p0g8af9/CLAUDE.md
/tmp/uas_llm__p0g8af9/README.md
/tmp/uas_llm__p0g8af9/.venv/CACHEDIR.TAG
... etc.
```

Every artifact step 1 was supposed to produce was actually created — by
Bash inside the throwaway isolation dir, then discarded when the LLM
client returned. The LLM completed the task perfectly; the orchestrator
just had no way to capture it.

The root design contradiction Section 2 was supposed to resolve is still
there: keeping `Bash` enabled for "research" (verifying package versions,
reading docs, environment introspection) is incompatible with "the LLM
must not create files in this generation step", because Bash can write
files via shell built-ins. Restricting Write/Edit/NotebookEdit while
leaving Bash unconstrained fixes the *symptom name* but not the
*capability boundary*.

**Files to modify:**

- `orchestrator/llm_client.py` `ClaudeCodeClient.generate` (around line
  181 — the `--disallowed-tools` extension).
- `orchestrator/claude_config.py` — the `CLAUDE.md` template the LLM
  reads at the top of the conversation.
- `orchestrator/main.py` `_contains_tool_calls` (line 354) — currently
  hard-coded to `return False`, which means the orchestrator no longer
  detects "LLM responded with tool actions instead of code". Whatever
  detection strategy Section 4 picks should re-enable this signal.

**Possible fixes (pick one or combine):**

1. **Constrain Bash to read-only commands using claude's tool-arg
   filter syntax.** `claude --help` documents that `--disallowed-tools`
   accepts entries like `Bash(git:*)`. We can deny the file-writing
   subset of bash:
   ```python
   cmd.extend([
       "--disallowed-tools",
       "Write", "Edit", "NotebookEdit",
       "Bash(>:*)", "Bash(>>:*)", "Bash(tee:*)",
       "Bash(cat:*<<*)",  # heredoc
       "Bash(touch:*)", "Bash(mkdir:*)", "Bash(cp:*)",
       "Bash(mv:*)", "Bash(rm:*)", "Bash(uv:sync*)",
       "Bash(uv:pip*install*)", "Bash(pip:install*)",
       "Bash(npm:install*)",
   ])
   ```
   Verify the exact match syntax against `claude --help` and test that
   each entry actually denies the intended invocation. The match
   patterns are claude-specific and may not support arbitrary glob
   forms; if shell redirection cannot be matched at all, fall through
   to option 3 or 4.

2. **Switch to an explicit allowlist with `--allowed-tools`.** Instead
   of trying to enumerate every dangerous Bash invocation, list only
   the read-only research tools that are safe:
   ```python
   cmd.extend([
       "--allowed-tools",
       "Read", "Grep", "Glob", "WebSearch", "WebFetch",
   ])
   ```
   This drops `Bash` entirely from the LLM's toolbox. The LLM loses the
   ability to run `python -c "import foo; print(foo.__version__)"` for
   environment checks, but it keeps `WebFetch` for docs and `Read` for
   inspecting on-disk files. Most version checks can be done via
   WebFetch against PyPI/registry pages instead. This is the simplest
   fix and the easiest to test.

3. **Stronger CLAUDE.md prompt that names the failure mode
   explicitly.** Add a paragraph near the top of the template that
   says, in the model's voice: "Files I create with Bash in this
   session are written to a throwaway temp directory and then deleted.
   They are not visible to the orchestrator and they do not count
   toward task completion. The ONLY thing the orchestrator reads from
   me is a single \`\`\`python fenced code block in my text response.
   If I do not produce that block, my work is lost." Empirically,
   models follow strong negative consequence framing better than
   abstract "do not" rules. This is the lowest-risk change but may not
   be sufficient on its own — pair with option 1 or 2.

4. **Detect "LLM did the work via Bash" in the orchestrator and
   recover.** When `extract_code()` returns nothing, scan the LLM
   response for Bash invocation patterns (`<bash>`, `Tool: Bash`, etc.)
   and either (a) re-prompt with a sharper instruction, or (b) extract
   the bash commands and synthesize an equivalent Python script. This
   is the most fragile of the four because it depends on response
   formatting that may change between claude versions. Avoid unless
   options 1–3 prove infeasible.

   While doing this, also fix `_contains_tool_calls` in
   `orchestrator/main.py` line 354 — it currently returns `False`
   unconditionally with the comment "tool calls are expected and
   handled by the CLI", which is no longer true after Section 2.

**Recommendation:** Start with option **2** (`--allowed-tools` with no
Bash) because it is the smallest, most testable change and matches the
semantic guarantee Section 2 was supposed to provide. If integration
tests show the LLM losing essential research capability (e.g. it can't
verify a package version that has only just been published and isn't in
its training data), add option 3 (prompt strengthening) on top. Treat
options 1 and 4 as fallbacks.

**Tests to add or update:**

- Extend `tests/test_llm_isolation.py` to assert the cmd uses
  `--allowed-tools` (not `--disallowed-tools`) with the correct
  read-only tool list, or that the disallowed list now blocks the
  Bash file-write subset.
- Add a regression test that mocks an LLM response containing a Bash
  tool invocation and asserts `extract_code()` correctly returns None
  AND the orchestrator surfaces a clear "LLM bypassed code-block
  contract via Bash" error so future failures are diagnosable from the
  log alone.
- Update `tests/test_llm_isolation.py` to assert that the CLAUDE.md
  template explicitly tells the LLM that tool-created files are
  discarded, not just that Write/Edit are disabled.

**Acceptance criteria:**

- All existing `tests/test_llm_isolation.py` tests still pass with the
  new flag shape.
- The new regression test passes.
- A re-run of `cd rehab && uas --resume --goal-file goal_001.txt`
  (against the same `fa0d38fa9ef6` run state) reaches step 1
  attempt 1 and produces a usable code block on the first try, with
  no "Failed to extract code block" entries in the log.
- Section 3's third acceptance criterion is then re-verifiable; Section
  3 can be marked `[COMPLETED]` once a step finishes successfully.

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]`, then re-run Section 3's verification and update Section 3
accordingly.

**Result (2026-04-07):** Verified end-to-end against `rehab/`. The fix
turned out to require **four** coordinated changes, not just the one
Section 4 originally proposed:

1. **`orchestrator/llm_client.py:175-186`** — `--disallowed-tools` was
   extended from `Write Edit NotebookEdit` to
   `Write Edit NotebookEdit Bash Task`. Empirical testing of `claude
   --help` against the live binary established that `--allowed-tools`
   (option 2 in this section's recommendation) is **silently ignored**
   when combined with `--dangerously-skip-permissions`, so the deny-list
   was the only mechanism that actually worked. `Task` had to be added
   alongside `Bash` because the LLM was using `Task` to spawn subagents
   whose toolset is NOT bounded by the parent's `--disallowed-tools`,
   and the subagents were creating files. Without blocking `Task`, the
   LLM bypassed the entire restriction by delegating to a subagent.
2. **`orchestrator/main.py:354-401` (`_contains_tool_calls`)** — replaced
   the `return False` stub with a regex-based detector for bash/shell
   code fences, tool-use markup, and "I created the files" prose. The
   error message at the call site (`orchestrator/main.py:1762-1774`) was
   updated to surface "LLM bypassed code-block contract via Bash or tool
   actions" so future format failures are diagnosable from the log alone.
3. **`orchestrator/main.py` `build_prompt()` lines 962-1003 and 1051-1106
   (the `<output_format>`, `<environment>`, and `<role>` blocks)** —
   the actual root cause of the recurring failure. Section 2 had updated
   `claude_config.py`'s `CLAUDE.md` template, but the orchestrator runs
   the LLM with `cwd=isolation_dir` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`,
   which means the workspace `.claude/CLAUDE.md` is **never read by the
   coder LLM**. The only prompt the LLM actually sees is the one
   `build_prompt()` constructs in Python — and that prompt advertised
   "ALL TOOLS ENABLED", "FULL TOOL ACCESS", "USE TOOLS FREELY", and
   "bash execution" as available, directly contradicting the
   `--disallowed-tools` flag. The LLM dutifully tried to use Bash, was
   blocked, and fell back to prose or bash code fences (per its training
   prior). Removing the contradictions, listing only the actually-allowed
   tools (`Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`), naming the
   disabled tools, and adding the "throwaway temp directory / files are
   discarded" framing fixed it.
4. **`orchestrator/main.py` `_build_retry_clean_prompt()` lines 800-839
   and `architect/planner.py` `REFLECTION_GEN_PROMPT` lines 2247-2257**
   — the retry_clean prompt (used for attempts 2+) had no output-format
   instructions at all, relying purely on the LLM's training prior. After
   attempt 1 produced a clean code block, the architect's reflection LLM
   was generating bad recovery suggestions (`"Produce a single
   self-contained bash script enclosed in a fenced code block
   ```bash ...```"`) because the reflection prompt didn't know the
   orchestrator extracts Python, not bash. Both prompts were updated to
   explicitly say "the orchestrator extracts ```python only" and to list
   the available/disabled tools.

A discovery during verification: the `CLAUDE.md` template I updated
under `orchestrator/claude_config.py` is functionally **dead code** for
the orchestrator's coder LLM path. It is still written to
`workspace/.claude/CLAUDE.md` by `architect/executor.ensure_claude_md`,
but the coder LLM never reads it because (a) the LLM subprocess runs
with `cwd=/tmp/uas_llm_<rand>` not the workspace, and
(b) `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` is set in
`orchestrator/llm_client.py:196` to prevent nested-session detection.
The CLAUDE.md template is still useful for users who run `claude`
interactively against the workspace, so I left the Section 4 wording in
it for parity with the build_prompt language. Whoever picks up the
follow-up "remove dead code paths" sweep should consider either deleting
the template entirely or wiring it through `--append-system-prompt` so
it actually reaches the coder LLM.

Live verification against `rehab/` after rebuild via `install.sh`:
- **Run 1 (after fixes 1+2 only):** All 3 attempts of step 1 still
  failed with "Failed to extract code block from LLM response."
  Inspecting `orchestrator/main.py:build_prompt` revealed the
  contradiction described in fix 3 above.
- **Run 2 (after fixes 1+2+3):** Attempt 1 produced a complete Python
  script, the sandbox ran it with `Exit code: 0` and `UAS_RESULT:
  {"status": "ok", ...}`, and the script created all 11 expected
  files plus ran `uv sync` successfully. **Zero "Failed to extract
  code block" entries in attempt 1's log.** Attempts 2 and 3 still
  failed with the format error because the retry_clean path had no
  output-contract instructions — fix 4 then handled that.
- **Run 3 (after fixes 1+2+3+4):** All 3 attempts produce extractable
  Python scripts; all 3 sandbox executions succeed with
  `Exit code: 0` and `UAS_RESULT: ok`. `grep -c "Failed to extract
  code block" log = 0` across the entire run. Section 4's third
  acceptance criterion is conclusively met.

Step 1 still does not show as "completed" because the orchestrator's
post-execution lint pre-check (a separate code path) fails with
`F401 [*] os imported but unused` against `rehab/tests/test_config.py`
— a **pre-existing** file dated `Apr  7 03:58` left in the workspace
from before any of today's work began. This is unrelated to Section 4.
The script-generation pipeline is fully fixed; the lint-strictness
issue against pre-existing workspace files is a separate failure mode
that warrants its own section. Section 3's first acceptance criterion
("at least one completed step") therefore remains unmet pending that
follow-up.

Tests added: `tests/test_orchestrator_main.py` gained
`test_no_all_tools_enabled_contradiction`,
`test_disabled_tools_called_out_in_prompt`,
`test_allowed_research_tools_listed_in_prompt`,
`test_python_code_fence_required_in_prompt`,
`test_throwaway_directory_warning_in_prompt`,
`test_retry_clean_includes_output_format_section`, plus an entirely
new `TestToolCallDetection` covering bash/shell fences, `<tool_use>`
markup, and first-person prose. `tests/test_llm_isolation.py` was
extended with the `Bash`/`Task` blocked-tool assertions and CLAUDE.md
template assertions for the throwaway/discarded language.
`tests/test_parser.py` got `test_bash_tool_bypass_response_returns_none`.
Total: 1581 tests pass, 0 failures (vs. 1575 before Section 4).

---

### Section 5: Stop the lint pre-check from rejecting pre-existing workspace files  [COMPLETED]

**Why:** Sections 1, 2, and 4 fixed the original Background failure modes
(half-initialized git, missing code-block extraction, Bash bypass). After
those fixes landed, verification of Section 3 against `rehab/` revealed a
**third, distinct** failure mode that still blocks Section 3's first
acceptance criterion ("at least one completed step").

The orchestrator's lint pre-check
(`orchestrator/main.py:1880-1896` calling `lint_workspace` from
`uas/janitor.py:79-114`) globs **every** `*.py` file in the workspace and
fails the entire attempt if any of them has a fatal Pyflakes error
(`F401`, `F811`, etc.). This is wrong for two related reasons:

1. The current attempt's LLM-generated script may not have touched the
   offending file at all. The script is blamed for errors it did not
   cause and the attempt is reverted.
2. Files committed to `uas-wip` from a prior failed run (or from the
   user's pre-uas working tree) get restored on every rollback. They
   therefore re-poison every subsequent attempt forever — there is no
   self-healing path; the orchestrator will loop until it exhausts the
   attempt budget no matter what the LLM does.

Concrete failure observed during Section 3 verification on `rehab/`:

```
Lint pre-check found 18 fatal error(s):
  F401 [*] `os` imported but unused
   --> tests/test_config.py:4:8
  F401 [*] `pytest` imported but unused
   --> tests/test_config.py:7:8
ExecutionResult: {"success":false,"revert_needed":true,"error_category":"lint_fatal",...}
FAILED on attempt 1.
Rolled back workspace to uas-wip checkpoint.
```

`tests/test_config.py` is dated `2026-04-07 03:58` (4 hours before today's
uas runs began). It is committed to `uas-wip` as part of "Initial workspace
state". The LLM-generated step-1 script never touches it — the script's
own `UAS_RESULT.files_written` only lists the 11 expected scaffold files
(`pyproject.toml`, `.python-version`, `.gitignore`, `CLAUDE.md`,
`README.md`, six `__init__.py` files). Despite that, all three attempts
fail with the same lint error and step 1 never completes.

I reproduced the same lint failure outside the container by running the
same ruff invocation `lint_workspace` uses
(`ruff check --select=F --no-fix --no-cache --quiet -- tests/test_config.py`),
confirming the failure is deterministic from the file contents alone and
will recur on every uas run against any workspace that contains a Python
file with unused imports.

**Files to modify:**

- `orchestrator/main.py` lines 1880-1896 — the lint pre-check call site.
  It currently calls `lint_workspace(_workspace)` with no `files`
  argument, which globs everything.
- `uas/janitor.py` lines 79-114 — `lint_workspace` already accepts an
  optional `files: list[str] | None` argument; only the call site needs
  to populate it. No janitor change is required for option 1; option 2
  may need a helper.

**Possible fixes (pick one or combine):**

1. **Lint only the files the script reported writing.** Parse
   `UAS_RESULT` from the sandbox stdout BEFORE the lint pre-check
   (currently it is parsed inside the `if exec_result.success:` branch
   at line 1955), pull out `files_written`, filter to `*.py` entries,
   and pass them to `lint_workspace(_workspace, files=...)`. Smallest
   change; matches the existing data flow. Risk: if the LLM lies about
   `files_written` or omits a file it created, that file is silently
   exempt from lint. Mitigate by combining with option 2 as a sanity
   check.

2. **Lint files changed in this attempt according to git.** After the
   sandbox runs, compute
   `git diff --name-only HEAD -- '*.py'`
   inside the workspace (the attempt branch was just created from
   `uas-wip`, so HEAD's parent is the previous attempt or `uas-wip`).
   Pass that file list to `lint_workspace`. Pro: doesn't trust the LLM.
   Con: needs the attempt branch's pre-script HEAD to be captured
   somewhere accessible at line 1885, which means a small refactor.

3. **Skip the pre-check entirely when the offending file is not in the
   set of files the attempt touched.** Run lint as today, but for each
   error, parse the file path and discard errors whose file is unchanged
   versus the attempt branch's parent. This preserves the
   "lint everything" behavior for cases where the script does touch the
   file but allows pre-existing errors to slide. More fragile because it
   couples to ruff's output format.

4. **Auto-fix unused imports with `ruff --fix`.** Bad: silently rewriting
   pre-existing user files crosses a line the orchestrator should not
   cross. Reject this option.

5. **`.uas_lintignore` allowlist.** Maintain a per-workspace list of files
   the lint pre-check should skip. Bad: requires user maintenance and
   papers over the bug instead of fixing it. Reject.

**Recommendation:** Start with option **1**. It is the smallest change,
the data is already available downstream of where lint runs, and the
fix is essentially a 5-line refactor: extract `parse_uas_result(stdout)`
to run before the lint pre-check, then pass `files_written` (filtered to
`.py`) to `lint_workspace`. Add option **2** as a defensive fallback if
empirical testing shows scripts under-reporting `files_written`.

**Tests to add or update:**

- `tests/test_janitor.py` — add (or assert existing) test that
  `lint_workspace(workspace, files=[a.py])` only inspects `a.py` and
  does NOT flag errors in unrelated files like `b.py`.
- `tests/test_orchestrator_main.py` — new regression test that
  monkeypatches `run_in_sandbox` to return a stdout containing
  `UAS_RESULT: {"status":"ok","files_written":["a.py"]}` and a
  workspace where `b.py` has an unused import. Assert that the lint
  pre-check passes (because `b.py` is not in `files_written`) and the
  step is recorded as successful.
- New end-to-end test (or integration smoke) that creates a workspace
  with a pre-existing `tests/test_config.py` containing unused imports
  and runs the orchestrator's per-attempt loop with a fake script that
  writes only `pyproject.toml`. Assert the attempt is NOT rolled back
  for `lint_fatal`.

**Acceptance criteria:**

- All existing `tests/test_janitor.py` and `tests/test_orchestrator_main.py`
  tests still pass.
- New regression test passes.
- A re-run of `cd rehab && uas --resume --goal-file goal_001.txt`
  against the existing `fa0d38fa9ef6` run state reaches step 1
  attempt 1 and the lint pre-check no longer reports
  `F401 ... os imported but unused` against `tests/test_config.py`.
- Step 1 of the rehab goal records as a completed step in
  `progress.md` (which finally satisfies Section 3's first
  acceptance criterion).

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]`, then re-run Section 3's verification and update Section 3
accordingly.

**Result (2026-04-07):** Implemented option **1** (the recommended fix):
the orchestrator now parses `UAS_RESULT` once before the lint pre-check,
filters `files_written` to `.py` entries, and forwards that list to
`lint_workspace(_workspace, files=...)`. When the script reports zero
`.py` files written, the lint pre-check is skipped entirely (nothing
this attempt could have broken). When `UAS_RESULT` cannot be parsed at
all, the orchestrator falls back to the legacy "lint the whole
workspace" behavior so older scripts that predate the contract still
get checked. The duplicate `parse_uas_result` call inside the success
branch was removed and the variable is reused, so the fuzzy LLM
fallback inside `parse_uas_result` is invoked at most once per attempt.

Code changes:
- `orchestrator/main.py:1880-1913` — new pre-check scoping logic.
- `orchestrator/main.py:1978-1983` — removed redundant `parse_uas_result`
  call in the success branch (uses the variable from the earlier parse).

Tests added:
- `tests/test_janitor.py::TestFormatWorkspaceRealRuff::test_lint_files_filter_ignores_unrelated_files`
  — exercises real `ruff` against two files (`a.py` clean, `b.py` with
  `F401`) and asserts `lint_workspace(files=["a.py"])` returns `[]`
  while the unfiltered call sees the `b.py` errors.
- `tests/test_orchestrator_main.py::TestLintPreCheckScopedToWrittenFiles`
  — four regression tests:
  1. `test_lint_called_with_files_written_from_uas_result` asserts the
     orchestrator forwards `files=["a.py"]` (filtered from
     `["a.py", "data.json"]`) to `lint_workspace`.
  2. `test_lint_skipped_when_uas_result_writes_no_python` asserts
     `lint_workspace` is not called when no `.py` file was written.
  3. `test_lint_falls_back_to_full_workspace_without_uas_result`
     asserts `lint_workspace(workspace)` (no `files=` kwarg) is the
     legacy behavior when `UAS_RESULT` cannot be parsed.
  4. `test_pre_existing_unused_import_does_not_fail_attempt` is the
     end-to-end regression: a workspace containing
     `tests/test_config.py` with `import os; import pytest` (the exact
     `rehab/` reproduction) plus a sandbox stdout reporting only
     `pyproject.toml` as written must result in `SystemExit(0)`. This
     test uses real `ruff` and is skipped if the binary is missing.

Verification:
- `python3 -m pytest tests/test_janitor.py tests/test_orchestrator_main.py`
  → **144 passed**.
- `python3 -m pytest tests/` (full suite) → **1586 passed,
  3 deselected** in 350s.
- Sanity check: temporarily reverted only the `orchestrator/main.py`
  change (kept the new tests) and re-ran
  `tests/test_orchestrator_main.py::TestLintPreCheckScopedToWrittenFiles`
  → **3 of 4 tests failed**, including
  `test_pre_existing_unused_import_does_not_fail_attempt` which
  reproduced the exact PLAN.md failure
  (`Lint pre-check found 16 fatal error(s): F401 [*] os imported but
  unused --> tests/test_config.py:1:8`). This confirms the new tests
  would catch a regression of this bug.

The end-to-end re-verification of Section 3 (running
`cd rehab && uas --resume --goal-file goal_001.txt` against the
existing `fa0d38fa9ef6` run state) is still owed, and is the final
acceptance criterion of Section 3 — not Section 5. Section 5's own
acceptance criteria (existing tests pass, new regression test passes)
are met. Section 3 stays `[PENDING]` until the rehab end-to-end run is
re-attempted with this fix in the rebuilt container.

---

### Section 6: Stop the lint pre-check from re-poisoning verification orchestrator runs  [COMPLETED]

**Why:** Section 5 fixed the lint pre-check for the **main step**
orchestrator path by scoping `lint_workspace` to the `.py` files the
script reported in `UAS_RESULT.files_written`. The fix preserves a
fallback at `orchestrator/main.py:1913-1914`: when `parse_uas_result`
returns None, lint the entire workspace. The fallback's stated purpose
in the Section 5 comment is "so older scripts that predate the
UAS_RESULT contract still get checked".

The fallback re-introduces the exact bug Section 5 was meant to fix
whenever the orchestrator subprocess is invoked for a script that has
no reason to print `UAS_RESULT`. The `verify_step_output` path in
`architect/main.py:3789` is one such caller: it spawns a fresh
orchestrator subprocess with a verifier task whose prompt
(`architect/main.py:3761-3787`) explicitly asks the script to print
`VERIFICATION PASSED`/`VERIFICATION FAILED`, never `UAS_RESULT`.

End-to-end consequence (observed during the Section 3 re-verification
described in Section 3's fourth status note):

1. Step 1's main orchestrator subprocess runs the LLM-generated Python
   script. It prints `UAS_RESULT: {"status":"ok","files_written":[...]}`
   listing the 11 expected scaffold files. Section 5's fix scopes lint
   to those files, lint passes, orchestrator exits 0.
2. Architect runs `verify_step_output(step, PROJECT_DIR)`.
3. A second orchestrator subprocess is launched with the verifier task.
   The LLM emits a Python script that does
   `assert (workspace / "pyproject.toml").exists()` and prints
   `VERIFICATION PASSED`. Sandbox exit code 0.
4. Lint pre-check at `orchestrator/main.py:1880-1914` runs.
   `parse_uas_result(stdout)` returns None because the verifier script
   doesn't emit `UAS_RESULT`. `py_files_written` stays None. Falls into
   `else: lint_errors = lint_workspace(_workspace)` at line 1913-1914.
5. `lint_workspace` globs every `*.py` and finds 18 `F401` errors in
   `tests/test_config.py` (and other pre-existing files committed to
   `uas-wip` from before the user's first uas run).
6. Verification orchestrator subprocess marks the attempt as
   `lint_fatal`, rolls back the workspace to `uas-wip` (undoing the
   main step's writes from step 1!), retries 3 times, then exits 1.
7. Architect's `verify_step_output` sees `result["exit_code"] != 0`
   and returns `stdout or stderr or "Verification script failed"`.
   `stdout` (extracted via `extract_sandbox_stdout`) contains the
   verifier's actual output: `VERIFICATION PASSED`.
8. The architect logs `Step 1 FAILED. Error: VERIFICATION PASSED` —
   a confusing surface symptom because the architect cannot see the
   orchestrator subprocess's stderr-only logger output where the
   `Lint pre-check found 18 fatal error(s)` warning was emitted.
9. Step 1 is recorded as `failed`; the next architect attempt
   regenerates the spec and starts over. The cycle repeats forever
   because the lint pre-check is deterministic against pre-existing
   files.

Direct reproduction (against the current `rehab/` workspace, which
still contains `tests/test_config.py` with `import os`/`import pytest`
unused):

```
docker run --rm \
  -v /home/eturkes/pro/uas/rehab:/workspace:Z \
  -e IS_SANDBOX=1 -e UAS_SANDBOX_MODE=local \
  -e UAS_TASK="Write a Python verification script that checks: \
pyproject.toml exists. Print 'VERIFICATION PASSED' if all checks pass, \
exit 0. The script must be READ-ONLY." \
  -w /workspace --entrypoint /bin/bash uas-engine:latest \
  -c "PYTHONPATH=/uas python3 -m orchestrator.main"
```

reproduces the failure deterministically: 3 attempts each print
`VERIFICATION PASSED` from the verifier sandbox followed by
`Lint pre-check found 18 fatal error(s)` and
`ExecutionResult: {"success":false,...,"error_category":"lint_fatal"}`,
ending in `FAILED after 3 attempts.`

**Files to modify:**

- `orchestrator/main.py` lines 1880-1914 — the lint pre-check scoping
  block from Section 5. The `else: lint_errors = lint_workspace(_workspace)`
  fallback is the bug. Decide what scoping to apply when the script does
  not emit `UAS_RESULT`.
- `architect/main.py` lines 3761-3787 — `verify_step_output`'s task
  template. Optionally add a `UAS_RESULT` instruction if option 3 is
  chosen.
- `orchestrator/main.py` near `_run_local`/`run_orchestrator` env
  setup, OR `architect/executor.py:230-271` — if option 4 is chosen,
  thread an env var that disables the lint pre-check for caller-opted
  paths (verification, future read-only orchestrators).
- `tests/test_orchestrator_main.py::TestLintPreCheckScopedToWrittenFiles`
  — must be updated/extended to cover the new behavior. Section 5's
  `test_lint_falls_back_to_full_workspace_without_uas_result` currently
  asserts the buggy fallback. It needs to be replaced with a test that
  asserts the **new** behavior (whichever option is chosen).

**Possible fixes (pick one or combine):**

1. **Skip lint pre-check entirely when `UAS_RESULT` is missing.**
   Smallest change: replace
   `else: lint_errors = lint_workspace(_workspace)` with `pass`. The
   rationale: a script that doesn't emit `UAS_RESULT` has not made any
   claim about what it wrote, so the orchestrator has no way to scope
   the check fairly. Better to skip than to run a check guaranteed to
   blame pre-existing errors on the current attempt. Risk: a script
   that genuinely creates a broken `.py` file but forgets `UAS_RESULT`
   slips through. Mitigate by combining with option 2.

2. **Lint files changed in this attempt according to git.** Use
   `git diff --name-only HEAD -- '*.py'` (or `HEAD~1` against the
   attempt branch's parent, which is `uas-wip`) to get the actual
   list of `.py` files this attempt touched, then pass that to
   `lint_workspace(files=...)`. Pro: doesn't trust the LLM to populate
   `UAS_RESULT`, doesn't blame pre-existing files, works for both the
   main path and the verification path uniformly. Con: needs the
   orchestrator to know what the attempt's parent commit is, which is
   currently implicit in the branch name `uas/step-N/attempt-M`.

3. **Make the verifier task emit `UAS_RESULT`.** Add to the task at
   `architect/main.py:3761-3787`:
   `Print 'UAS_RESULT: {"status":"ok","files_written":[]}' as the last
   line of stdout.` This makes verifier scripts honor the same contract
   as main-step scripts, so Section 5's existing scoping logic skips
   lint (empty `files_written` → no `.py` files → lint skipped). Pro:
   smallest behavioral change for the orchestrator. Con: papers over
   the underlying issue — any other future caller of the orchestrator
   subprocess that doesn't emit `UAS_RESULT` re-trips the bug.

4. **Add an env var (`UAS_SKIP_LINT_PRECHECK=1`) the architect can set
   before invoking the verification orchestrator.** Explicit
   opt-out, no behavior change for callers that don't set it. Con:
   adds another orchestrator config knob; doesn't fix the bug for
   any future architect-side caller that forgets to set the env var.

5. **Combine options 1 and 2.** Default to "lint files git says were
   touched" (option 2). Fall back to "skip lint entirely" (option 1)
   only when git says no `.py` files were touched OR when the workspace
   is not a git repo.

**Recommendation:** Start with option **2** (git-diff scoping). It is
the most defensible because it does not trust the LLM (neither for
`UAS_RESULT` nor for the script's behavior), it works uniformly for
the main path AND the verification path, and it gives the right answer
even for callers that don't know about the lint pre-check. Section 5's
`UAS_RESULT`-based scoping can stay as a sanity-check overlay (lint the
union of "git-changed `.py` files" and "files_written-claimed `.py`
files"), or be removed entirely in favor of the git approach.

If option 2 turns out to require more refactoring than expected (the
orchestrator subprocess needs to know the attempt branch's parent
commit), fall back to option **1** (skip when no `UAS_RESULT`) which
is a one-line change. The risk of option 1 is bounded: scripts that
create broken `.py` files without `UAS_RESULT` will still be caught
later by the architect's guardrail scan
(`architect/main.py:5078-5114`) and the full pytest suite
(`architect/main.py:5175-5190`).

**Tests to add or update:**

- `tests/test_orchestrator_main.py::TestLintPreCheckScopedToWrittenFiles::test_lint_falls_back_to_full_workspace_without_uas_result`
  — currently asserts `lint_workspace(workspace)` is called as the
  legacy fallback. Replace with a test that asserts the new behavior:
  for option 1, that `lint_workspace` is NOT called when `UAS_RESULT`
  is missing; for option 2, that it is called with the git-diff file
  list; for option 3, no orchestrator change is needed but a new test
  in `tests/test_verification_loop.py` should assert the verifier
  prompt template includes `UAS_RESULT` instructions.
- `tests/test_orchestrator_main.py` — new regression test that
  reproduces the verification scenario end-to-end: monkeypatch
  `run_in_sandbox` to return a sandbox stdout containing
  `VERIFICATION PASSED` (no `UAS_RESULT`), seed the workspace with a
  pre-existing `tests/test_config.py` containing `import os` unused,
  and assert the orchestrator subprocess exits 0 (lint skipped /
  scoped, no `lint_fatal`). This is the direct analogue of Section 5's
  `test_pre_existing_unused_import_does_not_fail_attempt` but for the
  no-`UAS_RESULT` path.
- `tests/test_verification_loop.py` — add an end-to-end-ish test that
  exercises `verify_step_output` against a workspace with pre-existing
  unused-import files. Assert the function returns None (success).

**Acceptance criteria:**

- `python3 -m pytest tests/test_orchestrator_main.py tests/test_janitor.py
  tests/test_verification_loop.py` passes including the new regression
  test(s).
- Direct manual reproduction (the `docker run ... -e UAS_TASK="Write a
  Python verification script..."` command in the **Why** section above)
  no longer produces `Lint pre-check found 18 fatal error(s)` and the
  orchestrator subprocess exits 0 on attempt 1.
- Re-run `cd rehab && uas --resume --goal-file goal_001.txt` against
  the existing `fa0d38fa9ef6` run state. Step 1 records as completed
  in `progress.md`. The architect log no longer shows
  `Error: VERIFICATION PASSED`. (This is the same final acceptance
  criterion as Section 3's #1; flipping Section 6 to `[COMPLETED]`
  and re-running this verification is what finally unblocks Section 3.)

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]`, append a one-paragraph result summary at the bottom of
the section, then re-run Section 3's verification and update Section 3
accordingly.

**Result (2026-04-07):** Implemented option 5 (combine git-diff
scoping with skip-fallback) per the recommendation. Added a new
helper `architect.git_state.changed_py_files_since_uas_wip(workspace)`
that returns the sorted union of (a) tracked `.py` files differing
between the working tree and `refs/heads/uas-wip` and (b) untracked
`.py` files via `git ls-files --others --exclude-standard`. The
helper returns `None` when the workspace is not a git repo or has no
`uas-wip` ref so callers can distinguish "scoping unavailable" from
"nothing changed". Rewrote the lint pre-check block at
`orchestrator/main.py:1903-1937` to compute a `files_to_lint` set as
the union of `UAS_RESULT.files_written` (filtered to `.py`) and the
git-diff helper's result. When the set is non-empty, lint exactly
those files; when both signals report empty (i.e., a positive "this
attempt touched no `.py` files" answer), skip lint entirely; only
when BOTH signals are unavailable does the legacy
`lint_workspace(_workspace)` fallback fire (e.g., a non-git workspace
running a script that doesn't speak `UAS_RESULT`). The verifier
scripts spawned by `architect.verify_step_output` — the original
trigger — now correctly skip lint because git diff against `uas-wip`
reports zero changed `.py` files for a read-only verifier. Tests:
replaced `test_lint_falls_back_to_full_workspace_without_uas_result`
with five new test methods on
`TestLintPreCheckScopedToWrittenFiles`
(`test_lint_skipped_when_no_uas_result_and_git_clean`,
`test_lint_uses_git_changed_files_when_uas_result_missing`,
`test_lint_falls_back_to_full_workspace_when_no_git`, plus the
unchanged `test_pre_existing_unused_import_does_not_fail_attempt`
and a new `test_verifier_script_pre_existing_unused_imports_does_not_fail`
that exercises the exact verifier-stdout-only scenario from this
section); added a new `TestChangedPyFilesSinceUasWip` class with 8
unit tests in `tests/test_git_state.py` covering the helper's
edge cases (no git, no uas-wip, clean workspace, pre-existing F401
files committed to uas-wip, untracked `.py`, modified tracked `.py`,
non-`.py` files, sorted-unique output); and added
`TestVerifyStepOutputSection6Regression` in
`tests/test_verification_loop.py` with two end-to-end-ish tests
that build a real git workspace with pre-existing F401 files and
assert both that the helper returns `[]` and that
`verify_step_output` returns `None` (success). Acceptance criterion
1 met: `python3 -m pytest tests/test_orchestrator_main.py
tests/test_janitor.py tests/test_verification_loop.py` (172 tests)
plus `tests/test_git_state.py` (27 tests) all pass — 199 total.
Acceptance criterion 2 met: rebuilt the container image via
`docker build -t uas-engine:latest -f Containerfile .` (cached, ~3s)
and ran the exact docker reproduction from the **Why** section
above against `/home/eturkes/pro/uas/rehab` (which still contains
`tests/test_config.py` with `import os`/`import pytest` unused);
the orchestrator subprocess exited with `Exit code: 0`, printed
`SUCCESS on attempt 1.`, and produced **zero**
`Lint pre-check found ... fatal error(s)` warnings — the bug is
gone. Acceptance criterion 3 (re-run `cd rehab && uas --resume
--goal-file goal_001.txt` and confirm `progress.md` records a
completed step) is the same as Section 3's first criterion and is
left for Section 3's verification re-run, per the workflow note
("flipping Section 6 to `[COMPLETED]` and re-running this
verification is what finally unblocks Section 3"). Section 3
remains `[PENDING]` until that end-to-end re-run is performed.

---

### Section 7: Stop the architect's full-pytest gate from failing on pre-existing test files referencing not-yet-built modules  [PENDING]

**Why:** Section 6 fixed the orchestrator's lint pre-check so that
pre-existing `.py` files committed to `uas-wip` from a prior failed
run are no longer blamed on the current attempt. With Sections 1, 2,
4, 5, and 6 all in place, the rehab workspace's step 1 main
orchestrator subprocess and verifier orchestrator subprocess BOTH
now run cleanly to exit code 0 (verified during the fifth attempt
at Section 3, see Section 3's fifth status note above).

But the architect has a third post-step validation gate that has
the same architectural bug Sections 5 and 6 fixed for the
orchestrator's lint pre-check: `architect/main.py:5175-5190` calls
`_run_full_pytest_suite(PROJECT_DIR)` after every non-test step,
and `_run_full_pytest_suite` at `architect/main.py:4546-4581` runs
`python -m pytest` unconditionally on every test file
`_discover_all_test_files(workspace)` finds in the workspace.
There is no scoping by:

- Which test files are part of the *current* step's outputs.
- Which test files were committed to `uas-wip` from a prior failed
  run before the current run started.
- Which modules the current step is supposed to have built.
- Whether a failing test was passing before the current step (which
  would indicate a regression genuinely caused by this step) or
  was already broken (referencing a module from a not-yet-run step).

End-to-end consequence (observed during the Section 3
fifth status note's verification re-run):

1. Step 1's main orchestrator subprocess runs the LLM-generated
   Python script. It creates the project skeleton, runs
   `uv sync`, prints `UAS_RESULT: {"status":"ok",...}`. Section
   5's lint scoping passes. Orchestrator exits 0.
2. Architect logs `Removed 11866 step artifact(s)` (the `.venv/`
   files installed by `uv sync` — correctly cleaned up by the
   architect's artifact filter).
3. Architect runs `verify_step_output(step, PROJECT_DIR)`. The
   verifier orchestrator subprocess generates a Python script that
   asserts `pyproject.toml` exists, prints `VERIFICATION PASSED`,
   exits 0. Section 6's lint scoping passes (no `.py` files
   changed since `uas-wip`). Verifier subprocess exits 0.
4. Architect proceeds to `_run_full_pytest_suite(PROJECT_DIR)` at
   `architect/main.py:5185`.
5. `_discover_all_test_files` returns `tests/test_config.py`
   (and potentially others) — files committed to `uas-wip` from
   the user's original `uas` run before step 1 was even attempted
   in the prior failed runs.
6. Pytest collects `tests/test_config.py`, which contains:
   ```python
   import os
   from pathlib import Path
   import pytest

   def test_project_root_is_path():
       from rehab.config import PROJECT_ROOT
       ...
   ```
   The `from rehab.config import PROJECT_ROOT` import fails with
   `ModuleNotFoundError: No module named 'rehab'` because step 1
   ("Project skeleton and dependency installation") only creates
   the empty `src/rehab/__init__.py` files — the actual
   `rehab/config.py` module is scheduled for a later step in the
   plan.
7. Six tests fail with the same root cause. Pytest exits 1.
8. `_run_full_pytest_suite` returns the formatted error string
   `"Full test suite FAILED after this step's corrections..."`.
9. Architect's `failure_reason = full_suite_err`. Step 1 is marked
   as failed even though both the main pipeline and the
   verification pipeline succeeded.
10. Architect logs `Step 1 FAILED. Error: Full test suite
    FAILED after this step's corrections.` and enters the rewrite
    loop. The planner LLM rewrites the step description; the
    rewritten attempt re-executes the same script (which writes
    the same files); the same pre-existing test still fails the
    same way; the cycle repeats until `MAX_SPEC_REWRITES` is
    exhausted; then the architect marks
    `state["status"] = "blocked"`, `step 1 status = "failed"`,
    rolls back the workspace to `uas-wip`, and exits 1.

The cycle is deterministic. As long as `tests/test_config.py`
exists in `uas-wip` (which it does — it was committed by
`ensure_git_repo`'s `_init_fresh_git_repo` flow on the user's
very first `uas` invocation), every step that runs before
`rehab/config.py` is built will trip this gate. Step 1 will
**never** complete on this workspace.

Direct reproduction (against the current `rehab/` workspace):

```
docker run --rm -v /home/eturkes/pro/uas/rehab:/workspace:Z \
  -e IS_SANDBOX=1 -e UAS_SANDBOX_MODE=local \
  -w /workspace --entrypoint /bin/bash uas-engine:latest \
  -c "cd /workspace && python3 -m pytest tests/test_config.py --tb=short -q"
```

prints (deterministically):

```
FFFFFF                                                          [100%]
=================================== FAILURES ====================
__________________________ test_project_root_is_path ___________
tests/test_config.py:12: in test_project_root_is_path
    from rehab.config import PROJECT_ROOT
E   ModuleNotFoundError: No module named 'rehab'
... (5 more identical-root-cause failures)
6 failed in 0.04s
```

The `tests/test_config.py` file is part of the `uas-wip` initial
commit (verified via `git ls-tree -r uas-wip | grep test_config`)
and predates today's work.

**Files to modify:**

- `architect/main.py:4546-4581` — `_run_full_pytest_suite`. The
  helper that runs pytest unconditionally on every discovered test
  file. Decide what scoping to apply.
- `architect/main.py:5175-5190` — the call site. May need to
  thread additional context (e.g., the current step's
  `files_written`, the dependency graph, the `uas-wip` commit
  reference) into the helper.
- `architect/main.py:4520-?` — `_discover_all_test_files`. May
  need to be replaced or augmented with a "test files this attempt
  actually touched" discoverer that mirrors
  `architect.git_state.changed_py_files_since_uas_wip` (Section 6).
- `architect/git_state.py` — possibly add a sibling helper
  `changed_test_files_since_uas_wip(workspace) -> list[str] | None`
  that returns the union of (a) tracked `tests/**/*.py` files
  differing between the working tree and `refs/heads/uas-wip` and
  (b) untracked `tests/**/*.py` files via
  `git ls-files --others --exclude-standard`. Same return-type
  contract as `changed_py_files_since_uas_wip` (returns `None`
  when scoping is unavailable).
- `tests/test_main.py` (or wherever `_run_full_pytest_suite` is
  exercised — see also `tests/test_orchestrator_main.py` and
  `tests/test_verification_loop.py` for analogous Section 5/6
  tests). Add a regression test that builds a temporary git
  workspace with a pre-existing `tests/test_orphan.py` importing
  a non-existent module, asserts the helper does not fail the
  step.
- `architect/main.py:5773-5796` — `try_resume`. Currently only
  resets `executing` → `pending`. The current rehab state has
  `Run status: blocked` and `Step 1 status: failed`. Re-running
  `--resume` against this state will refuse to retry step 1
  unless `try_resume` also handles the `blocked`/`failed`
  combination, OR the verifier of Section 7 manually clears
  `state.json` first. Decide whether to expand `try_resume` or
  document the manual reset as a process step.

**Possible fixes (pick one or combine):**

1. **Skip the full pytest gate when no test files were written
   by this step.** Mirrors Section 6's option 1 (skip lint when
   `UAS_RESULT.files_written` reports no `.py` files). Smallest
   change. Risk: a step that modifies a source module without
   adding/changing a test could break a previously-passing test
   and the gate would not catch it. Mitigate by combining with
   option 3.

2. **Scope pytest to test files that import modules built by
   this step or its already-completed dependencies.** Most
   semantically correct, but expensive: requires building an
   import graph between every `tests/**/*.py` and every
   `src/**/*.py`, and intersecting with the current step's
   `files_written` plus the transitive closure of completed
   step outputs. Risk: dynamic imports and `importlib` calls are
   not statically analyzable.

3. **Use the same git-diff-against-`uas-wip` approach Section 6
   used.** Add `changed_test_files_since_uas_wip(workspace)` to
   `architect/git_state.py`, returning `git diff --name-only
   uas-wip -- 'tests/**/*.py'` plus untracked `tests/**/*.py`.
   Pass that file list to pytest instead of every discovered
   test file. Pro: mirrors Section 6's pattern, doesn't require
   trusting the LLM, doesn't require an import graph. Con: a
   pre-existing test file that the current step *should* be
   making pass (because the step is supposed to build the
   missing module) won't be checked.

4. **Distinguish "pre-existing broken test" from "regression."**
   At the start of every run, snapshot the result of the full
   pytest suite (which tests pass, which fail). Treat tests that
   were already failing on `uas-wip` as "expected failures —
   ignore" until a step's `files_written` overlaps with the
   modules they import. Treat tests that were passing on
   `uas-wip` and now fail as genuine regressions. Pro: most
   accurate. Con: most complex; needs persistent snapshot
   storage in `.uas_state/runs/<run_id>/`.

5. **Quarantine pre-existing test files at run start.** During
   `ensure_git_repo`/`try_resume`, scan `tests/**/*.py` for
   files that fail to import on `uas-wip`'s clean checkout. Move
   them to a `.uas_state/quarantine/` directory until a later
   step's `files_written` provides their missing dependencies,
   then move them back. Pro: pytest's discoverer doesn't even
   see them so option 1's risk is mitigated. Con: invasive
   (touches the workspace), needs careful state management.

**Recommendation:** Start with option **3** (git-diff scoping)
plus option **1** as a fallback. This mirrors Section 6's exact
pattern for the analogous bug, keeps the change small, and reuses
the helper-style infrastructure already in
`architect/git_state.py`. Specifically:

- Add `changed_test_files_since_uas_wip(workspace) -> list[str] |
  None` to `architect/git_state.py`, modeled exactly on
  `changed_py_files_since_uas_wip` but with the path filter
  `'tests/**/*.py'` (or a configurable glob).
- Rewrite `_run_full_pytest_suite` to take a `step` argument or
  call the new helper directly. When the helper returns a
  non-empty list, run pytest on those files only. When it
  returns an empty list (no test files changed by this attempt),
  skip pytest entirely. When it returns `None` (scoping
  unavailable — no git repo or no `uas-wip` ref), fall back to
  the legacy `_discover_all_test_files` behavior.
- Update the call site at `architect/main.py:5175-5190` to pass
  the step / workspace context the helper needs.

If option 3 turns out to require more refactoring than expected,
fall back to option **1** (skip when no `UAS_RESULT.files_written`
overlaps with `tests/**/*.py`). The risk of option 1 is bounded:
the architect's existing per-step verifier (`verify_step_output`)
plus the next step's own pytest gate (when it runs after building
the previously-missing module) will catch genuine regressions.

**Tests to add or update:**

- `tests/test_main.py` — new
  `TestRunFullPytestSuiteSection7Regression` class. At minimum:
  - `test_pre_existing_test_referencing_unbuilt_module_does_not_fail_step`
    — build a temp git workspace, commit a `tests/test_orphan.py`
    that does `from notyetbuilt.module import X` to `uas-wip`,
    invoke `_run_full_pytest_suite` (or its replacement), assert
    it returns `None` (success).
  - `test_pre_existing_passing_test_still_runs_after_step` — same
    setup but the test imports something that exists; assert the
    helper does run pytest and reports success.
  - `test_test_file_added_by_step_runs_pytest` — assert that a
    `tests/test_new.py` written by the current step's script (so
    it shows up as untracked or as a diff vs `uas-wip`) IS
    included in the pytest run.
  - `test_no_git_falls_back_to_full_discovery` — assert the
    legacy fallback path still works for non-git workspaces.
- `tests/test_git_state.py` — add a `TestChangedTestFilesSinceUasWip`
  class mirroring the existing `TestChangedPyFilesSinceUasWip`
  Section 6 tests, exercising the new helper across the same
  edge cases (no git, no uas-wip, clean workspace, pre-existing
  test files committed to uas-wip, untracked test files,
  modified tracked test files, non-test `.py` files, sorted
  unique output).
- `tests/test_verification_loop.py` — add a Section 7 regression
  test that builds a real git workspace with a pre-existing
  `tests/test_orphan.py` importing a non-existent module, runs
  the architect's full step lifecycle (or a stripped-down
  version), and asserts the step completes rather than failing.

**Acceptance criteria:**

- `python3 -m pytest tests/test_main.py tests/test_git_state.py
  tests/test_orchestrator_main.py tests/test_verification_loop.py`
  passes including the new regression test(s).
- Direct manual reproduction (the `python3 -m pytest
  tests/test_config.py` command in the **Why** section above) is
  no longer the architect's authoritative gate — the architect
  runs only the test files actually changed by this attempt and
  the step does not fail when the unchanged-pre-existing test
  file fails on its own.
- Re-run `cd rehab && uas --resume --goal-file goal_001.txt`
  against the existing `fa0d38fa9ef6` run state (after
  manually clearing or extending `try_resume` to handle
  `blocked`/`failed`, see "Files to modify" above). Step 1
  records as completed in `.uas_state/runs/fa0d38fa9ef6/progress.md`.
  The architect log no longer shows
  `Error: Full test suite FAILED after this step's corrections.`
  (This is the same final acceptance criterion as Section 3's
  #1; flipping Section 7 to `[COMPLETED]` and re-running this
  verification is what finally unblocks Section 3.)

**When complete:** change the section header from `[PENDING]` to
`[COMPLETED]`, append a one-paragraph result summary at the
bottom of the section, then re-run Section 3's verification and
update Section 3 accordingly.

---

## Out of scope

- Removing the hard-coded references to `dashboard/translations.py`,
  `data_loader.py`, and `feature_engineering.py` in
  `orchestrator/claude_config.py` lines 200-205. These are leftover from a
  specific project and pollute generic CLAUDE.md output. They are unrelated
  to the failure being fixed here, but worth addressing in a separate PR.
- Refactoring the orchestrator's "use tools freely" / "output a code
  block" prompt contradiction. Section 2 papers over it by hard-blocking
  the conflicting tools; the deeper redesign is a larger discussion.
