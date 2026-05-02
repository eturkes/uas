# PLAN — Phase 4: Prune

Phase reference: `ROADMAP.md` §Phase 4 — Prune (under the May 2026
pivot — see ROADMAP §Pivot for context).

Substrate references: `docs/substrate.md` (the Phase 1 keep-list
plus the Phase 2 §1 rate-limit read pattern); `docs/cut_surface.md`
(the Phase 2 §4 sized estimate this phase finalises into a closed
cut list); `phase0_audit.md` (the 68-mechanism catalog that becomes
the deletion checklist); `docs/orchestrator.md` (the Phase 3 design
notes, all kept).

This phase deletes most of the existing scaffold. **Default verdict
on any mechanism is cut.** The keep list — Docker sandbox, OAuth
4-stage refresh, JSONL audit log primitive, provenance metadata,
workspace isolation, resume-from-JSONL, eval harness's deterministic
check types, plus the entire Phase 3 orchestrator (`cli.py`,
`worker.py`, `container.py`, `rate_ledger.py`, `buffer_ledger.py`,
`policy.py`, `policy.default.toml`, `task.py`, `workspace.py`,
`pricing.py`, `sandbox.py`, `cases/`) — is finalised at §1 of this
phase as a closed set. Cuts proceed in dependency order so
intermediate revisions never break imports or tests.

This PLAN is a draft. Per the project's decision protocol, Phase 4
work does not begin until the project owner has reviewed and
approved this file.

## Phase exit criteria (from ROADMAP.md)

- Codebase is materially smaller — measured (lines removed, files
  removed, modules collapsed) and recorded.
- Orchestrator + slim substrate is the entire working UAS.
- README is accurate; rewritten from scratch to match the slim
  reality.
- Keep list is a closed set with no "shouldn't this stay?" items
  outstanding.

## Scope discipline (non-negotiable)

- **Default verdict is cut.** No mechanism, file, or symbol
  survives without an explicit § entry justifying its keep. Phase
  0's 68-mechanism catalog is the deletion checklist, not a
  verdict matrix; absence of a keep-list entry means cut.
- **No new mechanism, no new feature.** Phase 4 is delete-only.
  No refactors that introduce new behaviour, no migrations that
  preserve cut code under a new name, no "improvements" attached
  to deletions.
- **Dependency-order deletes.** Each § cuts leaves before roots.
  Imports must resolve and the kept-test subset must pass at the
  end of every §. A § does not close until the post-cut state is
  green.
- **Tests follow code.** Every cut module's test file is cut in
  the same §. The kept-test subset (orchestrator §3–§8 tests +
  eval-harness tests + provenance test) shrinks only by removal,
  never by editing — no test mutation to "make it pass after the
  cut".
- **README only at §8.** The README is rewritten from scratch
  exactly once, after the cut surface has settled and the smoke
  gate has passed.
- **Smoke gate at §7.** Both `uas-eval` (the Phase 1 hello-file
  case) and `uas-orchestrate start synthetic-multistep
  --simulate-rate-status allowed` (the Phase 3 §8 fixture) must
  run cleanly end-to-end on the post-prune tree before §8's
  README rewrite begins.
- **Sections are linear.** §1 finalises the cut list; §2 resolves
  the NEEDS-PHASE-3-DECISION trio; §3–§6 cut in dependency order;
  §7 verifies; §8 documents.

## Section 1 — Cut-list finalisation

**Goal.** Author the closed cut list `docs/cut_list.md` that §3–§6
consume. Read-only — no deletions in this section. The cut list
becomes the deletion-order ground truth and the source of the
shrinkage measurement at §7. After §1 closes, every file in the
repo (excluding gitignored runtime artefacts) is classified
KEEP / CUT, and every Phase 0 catalog mechanism has a verdict.

**Steps.**

1. Inventory every tracked file in the tree with `git ls-files`.
   For each file, classify as KEEP, CUT, or DEFER (only for the
   §2 trio). Cross-check against:
   - The ROADMAP §Phase 4 keep list (the seven primary entries).
   - `docs/substrate.md` (the eight Phase 1/2 substrate
     components).
   - The Phase 3 deliverables (`orchestrator/cli.py`,
     `worker.py`, `container.py`, `rate_ledger.py`,
     `buffer_ledger.py`, `policy.py`, `policy.default.toml`,
     `task.py`, `workspace.py`, `pricing.py`, `sandbox.py`,
     `cases/`).
   - `phase0_audit.md` (68-mechanism catalog) — every entry maps
     to one or more files; verify each gets a verdict.
2. For tests: classify each `tests/test_*.py` as KEEP only if its
   subject module is on the keep list. Eval-harness tests
   (`test_eval_*.py`), Phase 3 orchestrator tests
   (`test_orchestrator_{buffer_ledger,loop,policy,rate_ledger,
   resume,task,worker}.py`), and `test_provenance.py` are KEEP;
   everything else defaults to CUT.
3. Identify dependency edges among CUT files so §3–§6 can delete
   leaves before roots without breaking intermediate test runs.
   Capture the deletion order in `docs/cut_list.md`.
4. For root-level scripts (`install.sh`, `run_local.sh`,
   `run_container.sh`, `start_orchestrator.sh`, `setup_auth.sh`,
   `entrypoint.sh`, `framework_settings.json`, `Containerfile`,
   `pytest.ini`, `requirements.txt`): classify each. Keep only
   what the post-prune tree actually needs.
5. Write `docs/cut_list.md` with sections KEEP / CUT (grouped by
   §3 / §4 / §5 / §6 owner) / DEFER (§2 trio). Each entry
   carries the file path; CUT entries also carry a one-line
   reason.

**Acceptance.**

- `docs/cut_list.md` exists. Every tracked file in the repo
  appears exactly once under KEEP, CUT, or DEFER.
- Every Phase 0 catalog mechanism (68 entries) has at least one
  CUT-side or KEEP-side file mapped to it. None are unaccounted
  for.
- DEFER bucket contains exactly three entries: `uas_config.py`,
  `uas_hooks.py`, `uas.example.toml`.
- §3 / §4 / §5 / §6 each have a non-empty CUT bucket whose
  contents match the §-by-§ deletion plan in this PLAN.
- §1 Results subsection records: total tracked files, KEEP count,
  CUT count, expected shrinkage in lines (sum of `wc -l` over
  CUT files; precise figure for the §7 measurement).

**Status:** pending

## Section 2 — NEEDS-PHASE-3-DECISION trio resolution

**Goal.** Resolve the fates of `uas_config.py`, `uas_hooks.py`,
and `uas.example.toml`. The Phase 3 design notes
(`docs/orchestrator.md` § Decisions committed at §1 approval)
specified: "trio defaults to CUT for Phase 4 if §2–§8 close
without consumption." Phase 3 §1–§8 closed without consuming the
layered loader (every Phase 3 module reads its own task-specific
TOML via `tomllib` directly), so the trio's default-CUT verdict
fires unless §1's audit surfaces a concrete consumption-edge that
flips it.

**Steps.**

1. Confirm `uas_config` consumption: every importer is in the §3
   / §4 cut bucket (`architect/*`, `orchestrator/llm_client.py`,
   `orchestrator/main.py`, `uas/fuzzy.py`, `uas/janitor.py`)
   except `integration/provenance.py`, which loads it via a soft
   `importlib.util` path with an explicit `"unavailable"`
   fallback when the file is missing.
2. Confirm `uas_hooks` consumption: every importer is in the §3
   cut bucket (`architect/main.py`, `architect/planner.py`,
   `tests/test_hooks.py`).
3. Confirm `uas.example.toml` consumption: it is a config-key
   discovery aid for `uas_config`'s layered loader; cuts with
   `uas_config`.
4. Record the trio's verdict in `docs/cut_list.md`: all three
   move from DEFER to CUT under §6 ownership (root-level
   cleanup). `integration/provenance.py` is NOT modified —
   `_hash_active_config` already returns `"unavailable"` when
   `uas_config.py` is missing; the existing-fixture coverage in
   `tests/test_provenance.py` exercises this path.
5. §2 Results subsection records: each trio member's importer
   list, the post-cut consumer count (zero in keep-list code),
   and confirmation that `provenance.py`'s fallback is exercised
   by the existing test.

**Acceptance.**

- All three trio members listed in `docs/cut_list.md` under §6.
- `provenance.py`'s fallback path verified by reading
  `tests/test_provenance.py` — no test edits.
- §2 Results subsection records the verdict + reasoning in this
  PLAN's git history (preserved across the §6 deletion).

**Status:** pending

## Section 3 — Delete architect/ tree

**Goal.** Cut the largest single subsystem: `architect/` plus the
tests covering it. `architect/main.py` (6864 lines) is the core
of the pre-pivot scaffold's failure-handling and validation
cascade; `architect/planner.py` (3700 lines) is the
coverage-driven decomposition the new orchestrator's
budget-aware decomposition replaces; the rest of `architect/`
(executor, code_tracker, dashboard, events, explain, git_state,
provenance, report, spec_generator, state, trace_export) supports
those two and goes with them.

**Steps.**

1. `git rm -r architect/`.
2. `git rm` every `tests/test_*.py` that imports from `architect`
   (the §1 cut bucket lists them).
3. Run `python3 -m pytest tests/ -q` and confirm green. The
   surviving suite at this point is Phase 1 eval + Phase 3
   orchestrator + provenance + a handful of layout / smoke /
   integration tests that may or may not import architect — any
   that do are in the same delete batch.
4. Run `./uas-eval` once to confirm the eval harness still
   exercises the kept set unbroken.
5. §3 Results subsection records: files deleted, line count
   removed (from §1's pre-cut measurement), test-suite line
   delta, smoke-run pass.

**Acceptance.**

- `architect/` no longer exists in the tree.
- No tracked file imports `architect`.
- `tests/` runs green (excluding the integration-marked tests
  the existing `pytest.ini` deselects by default).
- `./uas-eval` exits cleanly on the hello-file case.
- §3 Results subsection records counts.

**Status:** pending

## Section 4 — Delete cut orchestrator legacy and uas/ tree

**Goal.** Cut the pre-Phase-3 `orchestrator/` files
(`orchestrator/main.py` 2051 lines, `claude_config.py`,
`llm_client.py`, `parser.py`) and the `uas/` package (`fuzzy.py`,
`fuzzy_models.py`, `janitor.py`). After this section the
`orchestrator/` directory contains only the Phase 3 deliverables
plus `sandbox.py` (Phase 1 substrate keep-list).

**Steps.**

1. `git rm orchestrator/main.py orchestrator/claude_config.py
   orchestrator/llm_client.py orchestrator/parser.py`.
2. `git rm -r uas/`.
3. `git rm tests/test_orchestrator_main.py tests/test_llm_client.py
   tests/test_claude_config.py tests/test_parser.py
   tests/test_fuzzy.py tests/test_janitor.py` and any other §1
   cut-bucket tests still standing.
4. Run `python3 -m pytest tests/ -q` and confirm green.
5. Run `./uas-orchestrate start synthetic-multistep
   --simulate-rate-status allowed` against a fresh state-root
   (use `--state-root /tmp/<unique>` to avoid clobbering the §8
   real-run state preserved in
   `orchestrator/state/synthetic-multistep/`). Confirm the
   end-to-end cycle from §8 still works with no architect / cut
   orchestrator / uas dependencies.
6. §4 Results subsection records: files deleted, line count
   removed, test-suite line delta, smoke runs pass.

**Acceptance.**

- `orchestrator/` contains only the Phase 3 keep-list files plus
  `sandbox.py` and `__init__.py`.
- `uas/` no longer exists.
- No tracked file imports the cut modules.
- `tests/` runs green.
- `./uas-orchestrate start synthetic-multistep
   --simulate-rate-status allowed` runs end-to-end.
- §4 Results subsection records counts.

**Status:** pending

## Section 5 — Delete integration/llm_judge.py and tighten eval.py

**Goal.** Cut `integration/llm_judge.py` and remove its call
sites from `integration/eval.py`. The Phase 4 keep list explicitly
excludes the LLM-as-judge module — Claude Code sub-agents fill
that role natively in the post-pivot framing. Eval retains its
deterministic check types (`file_exists`, `file_contains`,
`pytest_pass`, `exit_code`, `file_shape`, `command_succeeds`,
content regex) plus the hello-file smoke case; everything LLM-
judge-shaped goes.

**Steps.**

1. Read `integration/eval.py` end-to-end. Identify every code
   path that imports / calls `llm_judge`.
2. Delete those code paths. Acceptable shapes: a verdict-handler
   branch that now only deterministic-checks, a CLI flag that
   becomes inert and is removed, a config-key lookup that becomes
   dead and is removed.
3. `git rm integration/llm_judge.py tests/test_llm_judge.py`.
4. Walk `integration/cases/`. Cases authored as open_ended
   (LLM-judged) tier are now unsupported; per ROADMAP §Suite
   scope (amended after §9 close), Phase 1's amended scope is the
   single hello-file case anyway — open_ended cases exist only
   in git history and were already removed when the §10 amendment
   landed. Confirm the case directory contains only the
   hello-file fixture; if any others remain, `git rm` them.
5. Run `python3 -m pytest tests/test_eval_*.py tests/test_provenance.py -q`
   and confirm green.
6. Run `./uas-eval` and confirm hello-file passes.
7. §5 Results subsection records: lines removed from `eval.py`,
   `llm_judge.py` line count, eval smoke pass.

**Acceptance.**

- `integration/llm_judge.py` no longer exists.
- `integration/eval.py` does not import `llm_judge`.
- `integration/cases/` contains only the hello-file fixture.
- Eval harness tests run green.
- `./uas-eval` exits cleanly.
- §5 Results subsection records counts.

**Status:** pending

## Section 6 — Root-level cleanup

**Goal.** Cut the NEEDS-PHASE-3-DECISION trio (resolved CUT in
§2) and the root-level scripts / config files that supported the
cut subsystems. After this section the repo root contains only
files the post-prune tree actually needs.

**Steps.**

1. `git rm uas_config.py uas_hooks.py uas.example.toml`.
2. `git rm tests/test_hooks.py` (the only remaining hook
   consumer; if any other tests break on import, they are §3 / §4
   leftovers and go in their respective sections, not here).
3. Decide and execute on each root-level script per §1's
   classification:
   - `install.sh` — CUT (no users, project is personal).
   - `run_local.sh` — CUT (replaced by `uas-eval` /
     `uas-orchestrate`).
   - `run_container.sh` — CUT or KEEP per §1 audit; if no
     keep-list code references it, CUT.
   - `start_orchestrator.sh` — CUT (replaced by `uas-orchestrate`).
   - `setup_auth.sh` — KEEP (OAuth setup is on the substrate
     keep-list).
   - `entrypoint.sh` — KEEP iff the `Containerfile` references it
     for the kept image build path; otherwise CUT.
   - `framework_settings.json` — CUT iff no keep-list code
     references it.
   - `Containerfile` — KEEP (sandbox keep-list); review for
     references to cut paths and remove those lines.
   - `pytest.ini`, `requirements.txt` — KEEP.
4. `git rm` standalone helpers no longer reachable from the
   keep set: `verify_test_goal.py`, `test_goal_*.py`,
   `run_test_verification.py`, `check_environment.py` (the last
   four are gitignored but if any are tracked, remove). Confirm
   `git status` after each removal.
5. Run `python3 -m pytest tests/ -q` and confirm green.
6. §6 Results subsection records: trio cuts confirmed, root-level
   cuts itemised with reasons, surviving root file list.

**Acceptance.**

- Trio (`uas_config.py`, `uas_hooks.py`, `uas.example.toml`) no
  longer exists.
- Root-level surviving file list matches §1's KEEP classification
  exactly.
- `tests/` runs green.
- §6 Results subsection records itemised cuts.

**Status:** pending

## Section 7 — Smoke verification

**Goal.** Confirm the post-prune tree is functionally intact:
both keep-list smoke surfaces run end-to-end, and the shrinkage
delta is measured against the pre-§3 baseline.

**Steps.**

1. Run `python3 -m pytest tests/ -q` and confirm green for the
   full surviving test suite.
2. Run `./uas-eval` and confirm the hello-file case passes.
3. Run `./uas-orchestrate start synthetic-multistep
   --simulate-rate-status allowed` against a fresh state root and
   confirm the §8 cycle still works (3 subtasks done, all
   artefacts populated). Optional: re-run pause + resume to
   confirm the boundary cycle is also intact.
4. Measure shrinkage: `git diff --stat` between the §1
   pre-cut commit and HEAD; record total lines removed, files
   removed, modules collapsed. Compare with §1's pre-cut estimate.
5. §7 Results subsection records: pytest summary, eval pass,
   orchestrator run cost / wallclock / artefact counts, shrinkage
   delta with comparison to estimate.

**Acceptance.**

- Full pytest suite green (excluding integration-marked tests).
- `./uas-eval` exits cleanly on hello-file.
- `./uas-orchestrate start synthetic-multistep
  --simulate-rate-status allowed` exits cleanly with all four
  state artefacts populated.
- Shrinkage delta recorded in §7 Results.

**Status:** pending

## Section 8 — README rewrite + cut-bucket notes

**Goal.** Rewrite `README.md` from scratch to match the slim
post-prune system. Author `docs/cut_bucket.md` recording the cut
buckets and their rationales so future readers do not
re-introduce things the pivot intentionally removed. Update
ROADMAP §"Current state of the codebase" if the post-prune
statistics differ materially from the Phase 0 figures.

**Steps.**

1. Draft a fresh `README.md`. Sections to include:
   - One-paragraph summary of what UAS now is (the Phase 3
     orchestrator + slim substrate, personal research harness).
   - Quickstart: prerequisites (container engine, Claude Max
     OAuth via `setup_auth.sh`), `./uas-orchestrate start
     <task>`, `./uas-eval` for the smoke gate.
   - Layout: orchestrator/ + integration/ + tests/ + docs/ +
     entrypoint scripts.
   - Configuration: per-task TOML cases at
     `orchestrator/cases/<task>.toml` and optional per-task policy
     overrides at `orchestrator/cases/<task>-policy.toml`.
   - Pointers to ROADMAP for direction, CLAUDE.md for session
     protocol, docs/{substrate,orchestrator,cut_bucket}.md for
     internals.
2. Author `docs/cut_bucket.md`. Sections: Phase 4 cut buckets
   (§3 architect, §4 cut orchestrator + uas, §5 llm_judge, §6
   trio + scripts), each with the rationale ROADMAP §Phase 4
   already states for that group, expanded with the deletion
   counts §1–§7 measured. Cross-link to `docs/cut_surface.md`
   (the Phase 2 §4 sized estimate) for traceability.
3. Update ROADMAP §"Current state of the codebase" only if the
   post-prune mechanism count materially differs from Phase 0's
   68. Likely outcome: Phase 0 catalog is preserved as historical
   record (the 68-mechanism count is the pre-prune figure;
   post-prune is the orchestrator + 7 keep-list items). A new
   sub-paragraph noting "post-Phase-4 mechanism count: <N>"
   suffices; do NOT delete the original Phase 0 paragraph.
4. §8 Results subsection records: README word count, cut_bucket
   line count, ROADMAP delta (additions only).

**Acceptance.**

- `README.md` exists, is internally consistent, points only to
  surviving files, and matches the slim post-prune reality.
- `docs/cut_bucket.md` exists with §-by-§ rationale and counts.
- ROADMAP §"Current state of the codebase" reflects the
  post-prune statistics (additive update, not a rewrite).
- Phase 4 exit criteria documented in §8 Results below.

**Status:** pending
