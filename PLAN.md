# PLAN: Project directory conventions & commit hygiene

## Context

When UAS runs in a directory, that directory is the project root. It holds
both project files and UAS meta-directories (`.uas_auth`, `.uas_state`,
`.uas_goals`). Git is initialized there. On completion, UAS produces a
best-practice commit.

**Secrets check:** `.uas_state` contains only execution metadata (state.json,
specs, code versions, progress, scratchpad, knowledge base, events,
provenance, reports, traces). No secrets. Safe to commit.
`.uas_auth` contains Claude CLI credentials. MUST be gitignored.

---

### Section 1: Rename `.state` → `.uas_state`

~150 references across ~31 files. Mechanical rename.

**1a. Core definition** — `architect/state.py`:
- `STATE_DIR`: `".state"` → `".uas_state"`
- Docstrings referencing `.state/`

**1b. `architect/main.py`** (~20 hardcoded refs):
- `_GITIGNORE_CONTENT`: remove `.state/` (Section 2 handles)
- `create_blocker()`: hardcoded `.state` path
- Skip-dir sets (~4): `".state"` → `".uas_state"`
- `validate_workspace()`: hardcoded `.state` path
- Code versions dir, protected names set, help text, `run_rel`

**1c. Other architect modules:**
- `explain.py` (~5 refs), `executor.py` (skip-dirs),
  `planner.py` (skip-dirs), `spec_generator.py` (docstring)

**1d. Orchestrator** — `orchestrator/main.py` (~3 refs):
- Skip-dirs set, code_versions path

**1e. Config files:**
- Root `.gitignore`: remove `.state/`, `integration/.state/`
  (`.uas_state` should NOT be gitignored)
- `VALIDATION.md` entry: remove (lives inside `.uas_state/`)

**1f. Tests** (~30+ refs across ~10 files):
- `conftest.py`, `test_state.py`, `test_explain.py`,
  `test_resume.py`, `test_cli_optimization.py`,
  `test_knowledge_base.py`, `test_orphaned_modules.py`,
  `test_smoke_test.py`, `test_cross_imports.py`,
  `test_verification_loop.py`, `test_events.py`,
  `test_provenance.py`

**1g. Documentation** — `README.md`: all `.state/` → `.uas_state/`

- [x] Done

---

### Section 2: Gitignore overhaul

**2a. `_GITIGNORE_CONTENT` template** in `architect/main.py`:
- `.uas_auth/` — MUST be gitignored
- `.claude/` — keep gitignored (UAS-internal)
- `data/` — gitignored (conventional data directory)
- Remove `.state/` (now `.uas_state/`, committed)
- Remove hardcoded `*.joblib`, `*.npz` (already in
  `_ensure_gitignore_data_patterns()`)

**2b. Root `.gitignore`:**
- Remove `.state/`, `VALIDATION.md`, `integration/.state/`
- Keep `.uas_auth`
- Add `integration/.uas_state/` if integration tests still
  produce state

**2c. Planner / CLAUDE.md instructions:**
- Update gitignore guidance to reflect new conventions
- Instruct to place data in `data/` directory
- Instruct to tailor gitignore to project type per goal

- [x] Done

---

### Section 3: Goal file → `.uas_goals/`

**3a. `architect/main.py`** goal file copy logic (~lines 4302-4320):
- Create `.uas_goals/` directory
- Copy goal file into `.uas_goals/` instead of workspace root
- When goal from CLI/stdin, write to `.uas_goals/GOAL.txt`

**3b. Tests** — `tests/test_goal_file.py`:
- Update expected destination paths

- [x] Done

---

### Section 4: Final commit message format

**4a. `finalize_git()`** in `architect/main.py`:
Current: `f"UAS: {summary}"` truncated to 77 chars.
New: proper git commit message format:
- Subject line: imperative voice, ≤50 characters
- Blank line separator
- Body: wrapped at 72 characters, describes what was built
- Generate from goal + completed step summaries using LLM
- Fallback: derive mechanically if LLM unavailable

**4b. Tests:**
- `tests/test_commit_hygiene.py`
- `tests/test_git_finalize.py`

- [x] Done

---

### Section 5: Verification

- Run full test suite
- Verify `.uas_state/` is committed (not gitignored)
- Verify `.uas_auth` IS gitignored
- Verify goal files land in `.uas_goals/`
- Verify final commit follows best practices format
