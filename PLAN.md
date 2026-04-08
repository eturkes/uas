# PLAN: Phase 0 — Audit Current State

## Background

Per `ROADMAP.md`, the project has accumulated ~25 self-correction
mechanisms across 363 commits without a measurement instrument to
tell which of them actually improve task-completion reliability.
Phase 0 is the no-code-change audit that grounds Phase 1 (eval harness
design) in the codebase as it actually exists, not as the README
describes it.

**Scope constraints:**
- No code changes in this phase, even for obvious bugs noticed in
  passing. Log them in "Notes during execution" at the bottom of
  this file and defer.
- Read-only operations only. Grep and file reads, no Edit / Write
  / Bash side effects on tracked files.
- Results of each section are appended to the section itself under a
  `### Section N — Results` subheading. Do not create separate output
  files — keeping everything in this PLAN preserves context for the
  final Section 5 distillation.

**Order of execution:** Sections 1 → 2 → 3 → 4 → 5, strictly
sequential. Section 4 depends on Section 1's mechanism list;
Section 5 depends on all prior sections.

---

## Section 1 — Catalog self-correction mechanisms

**Status:** todo

**Goal:** produce an exhaustive table of every mechanism in the
codebase that detects, prevents, or recovers from LLM failures.
"Mechanism" here means any distinct piece of code whose removal
would measurably change behavior on a non-trivial task.

**Files to examine (read in full):**
- `architect/main.py` — main controller loop, retry/rewrite driver
- `architect/planner.py` — decomposition, multi-plan voting,
  complexity estimation
- `architect/executor.py` — workspace scanning, context propagation,
  orchestrator invocation
- `architect/spec_generator.py`
- `architect/state.py` — re-planning triggers, persistence
- `architect/events.py`, `architect/provenance.py`,
  `architect/code_tracker.py` — observability (not mechanisms, but
  note they exist for Section 3)
- `architect/explain.py`, `architect/report.py`, `architect/dashboard.py`,
  `architect/trace_export.py` — reporting (same)
- `orchestrator/main.py` — build-run-evaluate loop, best-of-N,
  `retry_clean`
- `orchestrator/llm_client.py`
- `orchestrator/sandbox.py`
- `orchestrator/parser.py`
- `orchestrator/claude_config.py`
- `uas/janitor.py` — formatting, linting pre/post-check
- `uas/fuzzy.py`, `uas/fuzzy_models.py`
- `uas_hooks.py`

**Steps:**
1. Read each file listed above in full. For large files, follow up
   with targeted greps for markers like `retry`, `rewrite`,
   `reflect`, `counterfactual`, `backtrack`, `stagnat`, `voting`,
   `best_of`, `guardrail`, `re_plan`, `replan`, `janitor`, `verify`,
   `validate`, `enrich`, `distill`.
2. For each mechanism discovered, record a row in the results table
   with columns:
   - **Name** — canonical name used in code or README (e.g.
     `counterfactual_root_cause_tracing`).
   - **Location** — `path/to/file.py:L<start>-L<end>`.
   - **Trigger** — when does it run? Call sites and conditions.
   - **Action** — what does it do in one sentence?
   - **Existing flag** — env var or config key that toggles it
     today, or `none`.
   - **Dependencies** — other mechanisms it reads state from.
   - **README reference** — the README bullet it corresponds to, or
     `undocumented`.
3. Cross-check against the README §Architect Agent and §Orchestrator
   sections to ensure no documented mechanism is missed.
4. Flag any README-documented mechanism with no corresponding code
   as "in README but not found in code" — this is either a naming
   drift or dead documentation, both worth knowing.
5. Flag any code-level mechanism with no README entry as "in code
   but not in README" — documentation debt.

**Definition of done:**
- `### Section 1 — Results` subheading appended below with the
  populated table.
- Row count ≥ 20 (the README implies ~25; significantly fewer
  suggests an incomplete audit).
- Every row has a non-empty Location field.
- Any discrepancies between README and code flagged explicitly.

### Section 1 — Results

*(Populated during execution.)*

---

## Section 2 — Audit existing eval infrastructure

**Status:** todo

**Goal:** determine what `integration/eval.py` and `prompts.json`
currently measure, what they don't, and decide whether Phase 1
extends the existing runner or replaces it.

**Files to examine:**
- `integration/eval.py` (359 lines)
- `integration/prompts.json`
- `integration/data/` — referenced via `setup_files`
- `integration/quick_test.sh`
- `tests/conftest.py`
- `tests/test_integration.py`
- `tests/test_*.py` (unit test modules — catalog at a high level
  only, don't deep-read each)
- `run_test_verification.py`, `verify_test_goal.py`, `test_goal_*.py`
  (loose scripts at repo root — are these part of any pipeline?)

**Known starting facts (from pre-audit skim during bootstrap session):**
- `eval.py` supports container (default) and `--local` modes, both
  invoking `python3 -P -m architect.main`.
- Three check types: `file_exists`, `file_contains` (regex), and
  `glob_exists`. All implemented in `run_check()` around L154.
- Results written to `integration/eval_results.json` — **overwritten
  each run, no history**.
- Workspaces live at `integration/workspace/<case_name>/`, wiped per
  run.
- Setup files copied from `integration/data/` into each workspace
  before execution.
- Only 4 prompt cases in `prompts.json`: `hello-file`,
  `two-step-pipeline`, `live-data-pipeline`, `fibonacci-json`. All
  trivial. `live-data-pipeline` depends on a live external API and
  is inherently flaky.
- No LLM-as-judge.
- No per-task metrics beyond `elapsed` (wall time).
- No multi-run averaging or noise bounds.
- No ablation-flag awareness.
- No git SHA / env capture for reproducibility.
- No tier system.

**Steps:**
1. Read `integration/eval.py` in full and confirm or correct each
   starting fact above.
2. Read `integration/prompts.json` and the contents of
   `integration/data/` (if any).
3. For each gap identified relative to Phase 1 requirements (see
   ROADMAP §"Phase 1 — Eval harness hardening"), record:
   - Gap description.
   - Scope of work to close it (estimate: add function / extend
     existing function / replace subsystem).
4. Decide and record: does Phase 1 extend `integration/eval.py` or
   replace it? Justify in 2–3 sentences.
5. Assess each of the 4 prompt cases: does any of them actually
   exercise the self-correction machinery (i.e. force a retry or
   rewrite under normal conditions)? Probably not — but confirm.
6. Read `tests/conftest.py` and `tests/test_integration.py` to
   understand the pytest-based path; these may or may not be
   reusable as benchmark infrastructure.
7. Check the loose root-level scripts (`run_test_verification.py`,
   `verify_test_goal.py`, `test_goal_*.py`, `test_goal_output.txt`,
   `test_verification_result.txt`): are they wired into any
   Makefile / CI / docs, or are they leftover artifacts? Flag
   accordingly for Phase 5 pruning consideration.

**Definition of done:**
- `### Section 2 — Results` subheading appended below.
- Starting facts list confirmed or corrected.
- Gap table: gap → scope → extend-or-replace.
- Extend-vs-replace decision with justification.
- Prompt-case assessment.
- Loose-script disposition table.

### Section 2 — Results

*(Populated during execution.)*

---

## Section 3 — Catalog feature flags and environment variables

**Status:** todo

**Goal:** enumerate every knob that already exists. Phase 3 needs
this to decide which ablation flags must be added and which already
exist in a reusable form. Ideally we discover that some significant
fraction of the Phase 3 work is already done.

**Files to examine:**
- `uas_config.py` (understand the TOML+env loader)
- `uas.example.toml`
- `README.md` §Environment Variables (approximately line 679
  onwards)
- `architect/` and `orchestrator/` directories (grep for knobs)

**Steps:**
1. Read `uas_config.py` in full. Understand the loading precedence
   (defaults → user-global → project → env) and the key-normalization
   rules (e.g. `UAS_MODEL` → `model`).
2. Read `uas.example.toml` and list every documented key.
3. Extract the full env-var table from `README.md` §Environment
   Variables.
4. Grep for call sites:
   - `Grep "UAS_" in architect/ and orchestrator/ and uas/ and
     uas_*.py` — every env var reference.
   - `Grep "config.get(" in architect/ and orchestrator/` — every
     config-key lookup.
   - `Grep "os.environ" and "getenv"` in the same scope.
5. Merge all three sources (TOML, README table, code grep) into a
   single inventory table. Flag:
   - **Documented-but-unused**: in README or TOML but no call site.
   - **Used-but-undocumented**: called in code but not in README or
     TOML.
   - **Partially-wired**: TOML key exists but code doesn't read it,
     or vice versa.
6. For each knob, classify:
   - **Tuning parameter** (float/int that adjusts behavior, e.g.
     `UAS_MAX_PARALLEL`).
   - **Feature toggle** (boolean that enables/disables a mechanism,
     e.g. `UAS_MINIMAL`, `UAS_NO_LLM_GUARDRAILS`).
   - **Configuration** (paths, models, credentials).
7. For each feature-toggle entry, record whether it cleanly disables
   a single mechanism (reusable for Phase 3) or a grouped bundle
   (needs decomposition before Phase 3 can use it).

**Definition of done:**
- `### Section 3 — Results` subheading appended below.
- Inventory table with every knob from all three sources merged.
- Classification (tuning / toggle / configuration) for each row.
- Explicit answer to: "How much of Phase 3 ablation flag work is
  already done, as a percentage of mechanisms from Section 1?"

### Section 3 — Results

*(Populated during execution.)*

---

## Section 4 — Mechanism dependency map

**Status:** todo

**Goal:** surface which mechanisms depend on which, so Phase 3
ablation design accounts for interactions and Phase 4 can interpret
single-flag ablation results correctly. If mechanisms A and B are
tightly coupled, disabling A alone will be misleading — the real
measurement is A+B together.

**Files to examine:** driven by the Section 1 output — specifically
the call sites and data flow for each catalogued mechanism.

**Steps:**
1. For each mechanism from Section 1, identify:
   - **State read** — what workspace / state.json / scratchpad keys
     or in-memory objects does it consume?
   - **State written** — what does it produce?
2. Build dependency edges: mechanism X depends on mechanism Y iff X
   reads state that only Y writes.
3. Express the graph as an adjacency list in markdown (one row per
   mechanism, list of downstream dependencies).
4. Identify strongly-coupled clusters — groups of 2+ mechanisms that
   all depend on each other transitively. These are the cases where
   single-flag ablation is meaningless without group ablation.
5. For each cluster, write a one-sentence summary of why the
   coupling exists and whether it's essential or incidental.

**Definition of done:**
- `### Section 4 — Results` subheading appended below.
- Adjacency list covering every mechanism from Section 1.
- Cluster identification with summaries.
- Explicit list of "group ablations" that Phase 4 must run in
  addition to single-mechanism ablations.

### Section 4 — Results

*(Populated during execution.)*

---

## Section 5 — Distill into ROADMAP

**Status:** todo

**Goal:** distill Sections 1–4 into the "Current state of the
codebase" section of `ROADMAP.md`, sized for future sessions to
read quickly at session start.

**Target length:** 3–5 paragraphs plus the following summary
elements:
- Total mechanism count from Section 1.
- Count of undocumented mechanisms (code-without-README).
- Count of documented-but-absent mechanisms (README-without-code).
- Eval infrastructure gap count from Section 2.
- Ablation-flag coverage percentage from Section 3.
- Number of strongly-coupled mechanism clusters from Section 4.

**Steps:**
1. Draft the "Current state of the codebase" summary in this file's
   "Notes during execution" area first (scratch pad), then refine.
2. Edit `ROADMAP.md`: replace the `*(Populated during Phase 0.)*`
   placeholder under `## Current state of the codebase` with the
   final summary.
3. Update the `## Phase plan` table: mark Phase 0 as `completed`,
   Phase 1 as `active`.
4. Update the `## Current phase` section at the top to point to
   Phase 1 and note that `PLAN.md` for Phase 1 is pending.
5. Commit 1: `Mark Phase 0 complete, populate current state section`
   — contains only `ROADMAP.md` changes.
6. Commit 2: `Remove completed PLAN file` — deletes `PLAN.md`,
   matching the existing project convention visible in commits
   `3ed3225`, `d532cc6`, `e289290`.

**Definition of done:**
- `ROADMAP.md` has a real "Current state of the codebase" section.
- Phase plan table reflects Phase 0 done, Phase 1 active.
- Two clean commits matching the pattern above.
- `PLAN.md` no longer in the tree.

### Section 5 — Results

*(Populated during execution.)*

---

## Notes during execution

*(Append observations, deferred items, bug flags, and scratch-pad
drafts here as work proceeds. Nothing added here is binding — this
is the scratch area, not the deliverable. The deliverable is the
"Results" subsection of each numbered section above.)*
