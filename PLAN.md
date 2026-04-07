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

---

### Section 4: Stop the LLM from creating files via Bash redirection  [PENDING]

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
