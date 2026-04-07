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

### Section 2: Restrict LLM tools so the orchestrator always gets a code block  [PENDING]

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
