# Phase 0 Audit — Detailed Findings

This is the persistent reference for Phase 0's row-by-row deliverables.
The high-level summary lives in `ROADMAP.md` under
"Current state of the codebase" (~5 paragraphs + headline stats).
This document holds the detail downstream phases need:

- **Phase 3** (ablation flags) needs the row-by-row mechanism catalog
  in §1 to know what to flag and the existing-flag column to know
  what already exists.
- **Phase 4** (ablation study) needs the dependency clusters in §4 to
  know which group ablations to run alongside single-flag ablations.
- **Phase 5** (prune) needs §1's documentation-debt notes and §3's
  discrepancy summary to find dead/undocumented code worth deleting.

The original audit lived in `PLAN.md`, which was deleted on phase
close per project convention. This file preserves the populated
content verbatim — same tables, same wording — just relocated to
a permanent home so it survives future PLAN file lifecycles.

---

## 1. Mechanism catalog

**Methodology.** All architect / orchestrator / uas source files
were read in full (or in chunks for large files), supplemented by
tree-wide greps for the markers `retry`, `rewrite`, `reflect`,
`counterfactual`, `backtrack`, `stagnat`, `voting`, `best_of`,
`guardrail`, `re_plan`, `replan`, `janitor`, `verify`, `validate`,
`enrich`, `distill`, `reflexion`, `tdd`, `compress`, `multi_plan`.
README sections "Architect Agent", "Orchestrator", and "Implicit
Intelligence" were used as the documented-mechanism reference. Two
location claims were spot-checked against the live source
(`architect/main.py:5603` informed backtracking, `architect/planner.py:1370`
multi-plan voting); both matched. File sizes confirmed:
`architect/main.py` 6864 lines, `architect/planner.py` 3700 lines,
`orchestrator/main.py` 2051 lines, `uas/janitor.py` 114 lines.

**Headline counts.**

- Total mechanism rows: **68**
- Documented in README and present in code: **39**
- In code but not in README (documentation debt): **26**
- In README but not present as a distinct code mechanism: **3**
  (goal expansion, environment probe, cross-run knowledge base —
  each exists as inlined logic without a clean call site)

**Caveat on "mechanism" definition.** The audit's working definition
was broad: "any distinct piece of code whose removal would measurably
change behavior on a non-trivial task." The catalog therefore
includes some borderline entries that are not strictly self-correction
(e.g. `PyPI version resolution`, `system state collection`,
`step description enrichment`). Phase 4 ablation may legitimately
scope down to the strictly-corrective subset if interaction analysis
shows the borderline ones don't move pass-rate.

**Mechanism table.** Line ranges are approximate centers of the
relevant function/block, recorded by the audit during reads.
Phase 3 should re-confirm any range it depends on before adding
flags.

| # | Name | Location | Trigger | Action | Existing flag | Dependencies | README ref |
|---|---|---|---|---|---|---|---|
| 1 | Multi-plan voting (decomposition) | architect/planner.py:1370-1450 | Complexity gate on medium/complex goals | Generates 3 plans in parallel with different strategy biases (default, simplicity, robustness), scores each, selects highest | `UAS_COMPLEXITY` gate | Complexity estimation | §Architect Agent — multi-plan voting |
| 2 | Complexity estimation gate | architect/planner.py:1021-1043 | Called before planning on all goals | Quick LLM call classifies goal as trivial/simple/medium/complex; gates voting and decomposition depth | none | none | §Architect Agent — complexity estimation gate |
| 3 | Enforced minimum steps | architect/planner.py:1138-1277 | After initial decomposition | Verifies plan meets MINIMUM_STEPS for complexity; if under, LLM generates additional steps | none | Complexity estimate | §Architect Agent (PLAN.md §7a) |
| 4 | Step merging (trivial) | architect/planner.py:3070-3175 | After decomposition, before execution | Merges consecutive steps in same execution level whose descriptions are short (<200 chars) | none | Topological sort | §Architect Agent (PLAN.md §2b) |
| 5 | Step merging (LLM-guided) | architect/planner.py:2894-3067 | After trivial merging for medium/complex goals | LLM proposes semantic merges of parallel-level steps; falls back to trivial if LLM fails | none | Topological sort, trivial merge fallback | §Architect Agent (PLAN.md §2b) |
| 6 | Split coupled steps | architect/planner.py:1864-1983 | After merging, before integration checkpoints | LLM detects steps that couple creation+integration and splits them into creation + integration phases | none | LLM split-detection call | PLAN.md §3b |
| 7 | Insert integration checkpoints | architect/planner.py:2069-2181 | After step splitting on plans with ≥7 steps | Detects phase boundaries (≥3 execution levels) and inserts checkpoint steps to validate cross-module interfaces | none (auto on step count) | Topological sort, phase boundary detection | PLAN.md §5a |
| 8 | TDD enforcement (planning gate) | architect/planner.py:736-814 | During plan validation | Validates that every non-exempt implementation step depends on a preceding `test:` step; LLM repairs violations | `UAS_TDD_ENFORCE` | Test step contract validator | PLAN.md §4 |
| 9 | Coverage requirements matrix | architect/planner.py:1641-1701 | During re-planning when requirements present | LLM evaluates which steps cover extracted natural-language requirements; returns coverage matrix | none | Requirement extraction | PLAN.md §1b |
| 10 | Fill coverage gaps | architect/planner.py:1704-1764 | After re-planning when coverage check fails | LLM generates new steps to address uncovered requirements; appended to plan | none | Coverage verification | PLAN.md §2a |
| 11 | Ensure goal coverage | architect/planner.py:1765-1857 | During Phase 1 planning | Extracts NL requirements from goal; runs coverage matrix; fills gaps if needed | none | Coverage matrix, fill gaps | PLAN.md §1a-c |
| 12 | Step description enrichment | architect/planner.py:3479-3556 | After each step completes, before downstream steps | Appends concrete output summaries (files produced, key outputs, schemas) to dependent steps' descriptions without LLM re-planning | none | File schema extraction | PLAN.md §6c / §11 |
| 13 | Distill dependency output (heuristic) | architect/main.py:1397-1482 | When building context for downstream steps | Heuristic extraction of key outputs, file lists, and summaries from dependency stdout/stderr | none | none | PLAN.md §1b |
| 14 | Distill dependency output (LLM) | architect/main.py:1527-1649 | Fallback when heuristic distillation insufficient | LLM-based summarization of dependency output into `<dependency>` XML blocks | none | Heuristic distillation | PLAN.md §1b |
| 15 | Tiered context compression | architect/main.py:1235-1342 | Before sending context to Orchestrator when context exceeds budget | Tier 1 pass-through; Tier 2-3 LLM summarization with regex fallback; Tier 4 emergency truncation (progress + tail) | `UAS_MAX_CONTEXT_LENGTH` | LLM fallback, regex fallback, progress file | PLAN.md §4c / §5 |
| 16 | Structured progress file | architect/state.py + main.py | After every step completion/failure and at plan boundaries | Replaces flat scratchpad with markdown sections (goal, state, decisions, completed, lessons) for LLM-free compression and recovery | none | Event log, scratchpad | PLAN.md §4a |
| 17 | Goal expansion | architect/planner.py + main.py | During Phase 1, before decomposition | LLM clarifies vague goals with concrete success criteria; generates structured spec | none | Research phase | §Implicit Intelligence — goal expansion |
| 18 | Research phase | architect/planner.py:218-249 | Before decomposition (non-minimal mode) | LLM researches best practices, standards, tool versions for the goal's domain | none | Project spec generation | §Architect Agent — research-first prompts |
| 19 | Project specification | architect/planner.py:133-216 | During Phase 1 planning | LLM generates multi-section spec (Overview, Goals, Non-Goals, Architecture, Data Model, API Contract, Implementation Notes) | none | Workspace context | PLAN.md §1 |
| 20 | Generate reflection | architect/planner.py:2426-2492 | After each step failure | LLM produces structured JSON with error_type, root_cause, lesson, what_to_try_next; falls back to keyword heuristic | none | Failure classifier | PLAN.md §1c / §3a |
| 21 | Classify error (heuristic) | architect/main.py + planner.py | Used when reflection LLM fails | Keyword-based classifier (dependency, logic, environment, network, timeout, format, unknown) | none | Reflection generator | PLAN.md §3b |
| 22 | Error-type-adaptive retry budget | architect/main.py:890-937 | After reflection, before rewrite | Maps error_type to retry budget (timeout=0, logic=full, dependency=1) | `MAX_SPEC_REWRITES=4` | Reflection error_type | PLAN.md §3b / §3e |
| 23 | Stagnation detection (text similarity) | architect/main.py:882-937 | During retry decision | Heuristic: if last 2 reflections share error_type and root_cause cosine >0.6, stop retrying | none | Reflection history | PLAN.md §3e |
| 24 | Verification stagnation (repeated validation failure) | architect/main.py:998-1008 | During step failure handling | Detects ≥2 consecutive attempts that pass execution but fail validation with same issues; triggers forced backtracking | none | Attempt-history validation flags | PLAN.md §3d |
| 25 | Counterfactual root-cause tracing | architect/planner.py:2495-2578 | When step has dependencies and fails | LLM analyzes whether root cause is in current step or a dependency; can identify missing dependencies; returns (target, dep_id) tuple | none | Step dependency graph | PLAN.md §3c |
| 26 | Informed backtracking | architect/main.py:5603-5698 | When root cause tracing fingers a dependency | Augments failing dependency description with downstream error context, re-executes dependency and current step; depth-1 limit | none | Root cause tracing | PLAN.md §3d |
| 27 | Reflect-and-rewrite (full-history LLM rewrite) | architect/planner.py:2754-2853 | After adaptive retry check passes; before next attempt | LLM freely chooses strategy from full previous_attempts and reflection_history; includes red-flag resampling | none | Adaptive retry decision, reflection history | PLAN.md §1c / §3e |
| 28 | Decompose failing step | architect/planner.py:3559-3584 | Retry budget exhausted on timeout/truncation | LLM decomposes task into 2-3 sequential sub-phases; handles truncation specially (brevity guidance) | none | Error classification | PLAN.md §3e |
| 29 | Rate-limit / usage-limit retry (persistent) | architect/main.py:4923-5006 | Orchestrator exits non-zero with retryable error | Exponential backoff capped by MAX_BACKOFF; optional indefinite mode with 6h reset | `UAS_RATE_LIMIT_WAIT`, `UAS_RATE_LIMIT_MAX_WAIT`, `UAS_RATE_LIMIT_RETRIES`, `UAS_USAGE_LIMIT_WAIT`, `UAS_USAGE_LIMIT_RETRIES`, `PERSISTENT_RETRY`, `MAX_BACKOFF` | Error classification | PLAN.md §1 |
| 30 | 3-strike git rollback | architect/main.py:5463-5475 + git_state.py | After 3 consecutive spec rewrite failures on a step | Hard `git reset` to pre-step checkpoint; breaks retry loop | none (hardcoded) | Git checkpoint | PLAN.md §Phase 3.5 |
| 31 | Git checkpoint per step | architect/main.py:551-600, 5343-5351 | After every successful step (non-minimal) | Creates git commit with step number/title; promotes uas/step-N branch to uas-wip | `UAS_MINIMAL` (disables) | Git state machine | PLAN.md §15 |
| 32 | Output quality checks | architect/main.py:2230-2413 | After step code succeeds, before verification | Regex + LLM checks for data leakage (temporal ordering), missing required outputs, unexpected output format | none | Step description, UAS_RESULT validation | PLAN.md §16 |
| 33 | Input quality checks | architect/main.py:2420-2475 | Before orchestrator invocation | Scans dependency outputs for all-NaN, no-valid-rows, constant columns; appends warnings to context | none | Completed steps' UAS_RESULT | PLAN.md §3 |
| 34 | Validate UAS_RESULT | architect/main.py:2154-2202 | After orchestrator exits 0 | Checks status field and verifies claimed files exist on disk; fails if either missing | none | UAS_RESULT JSON parser | PLAN.md §1 |
| 35 | Verify-step-output (orchestrator-driven) | architect/main.py:3661-3816 | If step has `verify` field and validation passed | Generates verification script (read-only, with step description + source + module exports); runs via Orchestrator; failure re-enters rewrite loop | Per-step `verify` field | Orchestrator invocation | PLAN.md §1 |
| 36 | Workspace cleanup (recursive diff) | architect/main.py:2597-2637 | After each step completes (success or failure) | Pre-execution snapshot vs post-execution diff; removes files not in step's `files_written` | none | Workspace snapshot | PLAN.md §10 |
| 37 | Nested duplication resolution | architect/main.py:2639-2707 | After artifact cleanup if nested project structure detected | Promotes nested_project/src to workspace root; updates files_written | none | Nested-structure heuristic | PLAN.md §11 |
| 38 | Project manifest + supersession detection | architect/main.py:2709-3047 | After successful step and artifact cleanup | Tracks all files produced by all steps; LLM detects superseded files from earlier steps; removes stale versions | none | File metadata, LLM evaluation | PLAN.md §12 |
| 39 | Best-practice guardrails (regex) | architect/main.py:3048-3089 | After step code succeeds, workspace clean | Regex scanner: hardcoded API keys (error), bare `except`, `eval()`, `shell=True`, plain HTTP (warning) | `UAS_NO_LLM_GUARDRAILS` (disables only LLM path) | Code violation regex | §Architect Agent — best-practice guardrails |
| 40 | Best-practice guardrails (LLM review) | architect/main.py:3090-3152 | If regex found no errors and not minimal mode | LLM reviews code against best practices for security, style, patterns | `UAS_NO_LLM_GUARDRAILS`, `UAS_MINIMAL` | Regex guardrails fallback | §Architect Agent — best-practice guardrails |
| 41 | Cross-module import validation | architect/main.py:3348-3420 | After guardrails | Detects imports from non-existent modules, circular imports, missing dependencies | none | Module API extraction | undocumented |
| 42 | Orphaned module detection | architect/main.py:3235-3348 | During guardrail phase | Identifies modules with public exports not imported anywhere; flags as warnings | none | Module API extraction | undocumented |
| 43 | TDD full-suite runner | architect/main.py:5206-5221 | After non-test step succeeds, if `UAS_TDD_ENFORCE=1` | Runs full pytest suite to detect regressions; failure triggers rewrite | `UAS_TDD_ENFORCE` | pytest availability | PLAN.md §4.6 |
| 44 | Replan remaining steps (mid-execution) | architect/planner.py:3347-3443 + main.py | When completed step's output mismatches downstream references | LLM adjusts pending steps based on actual outputs; verifies coverage and fills gaps | none | Coverage verification, gap filling, requirement extraction | PLAN.md §6a / §6b |
| 45 | Should-replan heuristic | architect/main.py:1649-1735 | After step completion, before replan LLM | Regex compares step's actual files against downstream step descriptions; flags name mismatches | none | Regex file reference patterns | PLAN.md §6a |
| 46 | Best-of-N code generation (orchestrator) | orchestrator/main.py:1230-1519 | On attempt 2+ if `UAS_BEST_OF_N` > 1 | Generates N parallel code samples with different prompt hints; executes all; selects best by score or LLM ranking | `UAS_BEST_OF_N` (default 1, max 3) | Score guidance LLM, LLM evaluation | §Orchestrator — best-of-N |
| 47 | Best-of-N budget (LLM-guided) | orchestrator/main.py:1270-1309 | On attempt 2+ if budget decision enabled | LLM advises whether to generate 1, 2, or 3 samples based on error ambiguity | none | Best-of-N scoring | undocumented |
| 48 | Score result (execution-based ranking) | orchestrator/main.py:1372-1416 | When selecting among N candidates | Ranks by exit code (+1000 success), UAS_RESULT richness (+100-150), stdout informativeness (+0-50); LLM-reweighted by task priorities | none | Parse UAS output, score guidance | undocumented |
| 49 | Evaluate candidates (LLM ranking) | orchestrator/main.py:1439-1519 | When ≥2 valid candidates exist and not minimal | LLM ranks code samples by correctness/output/robustness/approach; falls back to score_result heuristic | none | Score result | undocumented |
| 50 | PyPI version resolution | orchestrator/main.py:160-198 | During prompt building for unversioned packages | Concurrent PyPI HTTP fetches for current stable versions; cached process-lifetime; injected into prompts | none | PyPI HTTP, version cache | §Implicit Intelligence — PyPI version resolution |
| 51 | Pre-flight LLM check | orchestrator/main.py:202-287 | Before sandbox execution (non-minimal) | LLM or regex reviews generated code for missing imports, bare paths, missing UAS_RESULT, infinite loops, input() | none | Code quality assessment | undocumented |
| 52 | Code quality assessment (fuzzy) | orchestrator/main.py:93-107 | During pre-flight | LLM evaluates code for UAS_RESULT presence, input() calls, file modification, missing imports | none | Fuzzy function system | undocumented |
| 53 | Retry-clean prompt section | orchestrator/main.py:753-846 | On attempt 2+ in build_prompt() | Embeds prior error output in prompt with guidance to try a different approach; immutable spec, reflections, error truncation | none | Prior error classification | PLAN.md §6.8 |
| 54 | Context janitor (format + lint) | uas/janitor.py:43-114 | In orchestrator post-execution | Runs ruff format (or black) on .py files; runs ruff check --select=F for Pyflakes-only fatal errors | `UAS_CONTEXT_JANITOR_FORMATTER` (ruff/black/none) | Code formatter, linter | PLAN.md (Phase 5 post-edit) |
| 55 | Smoke-test entry point | architect/main.py:3886-3925 | During final workspace validation | Dry-imports entry-point modules to catch ImportError/ModuleNotFoundError before declaring success | none | Workspace scan | undocumented |
| 56 | Holistic workspace validation | architect/main.py:3926-4353 | After all steps complete (Phase 2) | Validates files exist; project guardrails (git branch, .gitignore, README, dependencies); cross-module imports; smoke tests; generates validation.md | `UAS_MINIMAL` (disables) | Guardrails, imports, smoke test | PLAN.md §13 |
| 57 | Corrective steps generation | architect/planner.py:3625-3750 | After validation returns issues | LLM generates new steps to fix validation issues (missing files, import errors, test failures); appended for execution | none | Validation checks | PLAN.md §6b |
| 58 | Post-run meta learning | architect/main.py:4401-4502 | After successful completion (non-minimal) | Records lessons (package versions, error→solution pairs) to cross-run knowledge base for future runs | `UAS_MINIMAL` (disables) | Reflection history, knowledge base | undocumented |
| 59 | Resume from saved state | architect/main.py:5804-5827 | On `--resume` or `UAS_RESUME=1` | Loads .uas_state/runs/{run_id}/state.json; resets interrupted steps to pending; reuses completed step outputs | `UAS_RESUME` | State persistence | §Quick Start — Resuming a Run |
| 60 | Dry-run mode (preview plan) | architect/main.py | On `--dry-run` or `UAS_DRY_RUN=1` | Runs decomposition + plan refinement; prints step DAG; exits without executing | `UAS_DRY_RUN` | Decomposition, planning | §Quick Start — Dry-Run Mode |
| 61 | Environment probe | architect/main.py:2096-2137 (called from 4727) | Before each step execution | Records Python version, installed packages, disk space; injected into context | none | none | §Implicit Intelligence — environment probe |
| 62 | Cross-run knowledge base (read+write) | architect/main.py:4401+, 5321-5341 | Before each run (read), after success (write) | Persists package versions, error→solution pairs across runs | `UAS_MINIMAL` (disables write) | Post-run meta learning | §Implicit Intelligence — cross-run KB (under-documented) |
| 63 | Attempt history tracking | architect/main.py:4788, 5455-5461 | Throughout step execution | Tracks all prior attempts for a single step with error messages and validation flags | none | none (consumed by reflection/rewrite) | undocumented |
| 64 | Truncation detection | architect/main.py:4845-4851 | After reflection generation | Detects format_error with "truncat" in reflections; signals orchestrator to add code-length guidance | none | Reflection history | undocumented |
| 65 | Step spec propagation to orchestrator | architect/main.py:4836-4842, 5838 | When invoking orchestrator | Forwards immutable step spec to subprocess so retry_clean can ground context | none | none | undocumented |
| 66 | Data quality error classification | architect/main.py:2414-2419, 5584-5601 | During error handling | Detects all-NaN, no-valid-rows, constant-column errors; triggers immediate backtracking | none | Output quality checks | undocumented |
| 67 | Collect test files for step | architect/main.py:4615-4658 | When invoking orchestrator | Scans workspace for test files changed since last uas-wip checkpoint; injects into prompt | none | git_state | undocumented |
| 68 | LLM retry-decision prompt | architect/main.py:963-995 | During retry decision | Structured LLM call decides "continue retrying?" given reflection history; fallback to heuristic | none | Stagnation detection | undocumented |

**README-only (documented but no distinct code mechanism).**

- *Goal expansion* — README implies a distinct phase. In code it lives
  inlined in Phase 1 planning (research_goal + project spec
  generation). Listed as row 17 above for completeness, but it has no
  clean call site or flag.
- *Environment probe* — README presents as automatic. Code reality is
  `_probe_environment()` at architect/main.py:2096-2137 called once
  per step from line 4727. Listed as row 61, under-documented.
- *Cross-run knowledge base* — README mentions "lessons learned
  persisted across runs" but the read/write paths are spread across
  main.py:4401+ and 5321-5341 with no top-level documentation. Listed
  as row 62, under-documented.

**Code-only (in code but not in README).** 26 mechanisms in the table
above carry `undocumented` in the README ref column. Highlights:

- Cross-module import validation (row 41)
- Orphaned module detection (row 42)
- Smoke-test entry point (row 55)
- Best-of-N budget LLM (row 47), score result (row 48), evaluate
  candidates LLM (row 49) — three orchestrator-internal mechanisms
  layered on best-of-N
- Pre-flight LLM check (row 51), code quality assessment (row 52)
- Post-run meta learning (row 58)
- Attempt history tracking (row 63), truncation detection (row 64),
  step spec propagation (row 65), data quality error classification
  (row 66), test-file collection (row 67), LLM retry-decision prompt
  (row 68)

The bulk of documentation debt is concentrated in
`architect/main.py` (LLM-driven failure classification and retry
decisions) and `orchestrator/main.py` (best-of-N internals).

**Cross-check status.** The README's headline mechanism list
(Reflexion / counterfactual / informed backtracking / verification
stagnation / best-of-N / multi-plan voting / tiered context
compression / context janitor / TDD gate / `retry_clean` /
mid-execution re-planning / guardrails) all map to concrete rows
above. No headline README mechanism is missing from the catalog.

---

## 2. Eval infrastructure assessment

**Starting-facts confirmation.** Every starting fact from the audit
PLAN was verified against the live source. All confirmed; one minor
addition and one drift noted:

| # | Starting fact | Status | Source |
|---|---|---|---|
| 1 | Container default + `--local`, both invoke `python3 -P -m architect.main` | ✅ confirmed | eval.py:99, 120 |
| 2 | Three check types `file_exists`, `file_contains`, `glob_exists` | ✅ confirmed | eval.py:154-199 |
| 3 | Results overwritten each run at integration/eval_results.json | ✅ confirmed | eval.py:32, 350-351 (`open(..., "w")`) |
| 4 | Workspaces wiped per run at integration/workspace/`<case>`/ | ✅ confirmed | eval.py:56-58 (`shutil.rmtree`) |
| 5 | Setup files copied from integration/data/ | ⚠️ code path exists; **directory does not exist on disk** and no current case declares `setup_files`. Dead code. | eval.py:60-70; `ls integration/data` → ENOENT |
| 6 | 4 prompt cases, all trivial | ✅ confirmed | prompts.json |
| 7 | live-data-pipeline depends on api.open-notify.org | ✅ confirmed | prompts.json |
| 8 | No LLM-as-judge | ✅ confirmed | full read |
| 9 | No per-task metrics beyond `elapsed` | ✅ confirmed | eval.py:129 — only `elapsed` recorded |
| 10 | No multi-run averaging or noise bounds | ✅ confirmed | full read |
| 11 | No ablation-flag awareness | ✅ confirmed | full read |
| 12 | No git SHA / env capture for reproducibility | ✅ confirmed | full read |
| 13 | No tier system | ✅ confirmed | full read |

**New finding (not in starting facts).** `integration/test_project_quality.py`
exists (245 lines) — a pytest-based reusable post-run quality gate
suite. Skipped unless `PROJECT_WORKSPACE` env var points at a
workspace dir. Tests are ML-project-class-specific:

- `TestModelQuality` — `model_metrics.json` exists, accuracy ≥ baseline
- `TestNoDataLeakage` — no feature shares the target's prefix
- `TestFeatureDataQuality` — feature CSV NaN rate <50% per column
- `TestSubgroupAnalysis` — subgroup results contain p-values / CIs / etc.
- `TestNoHardcodedPaths` — no `/workspace` literals in committed `.py`
- `TestDashboardImport` — dashboard `app.py` imports without error

This is structurally similar to what Phase 1's deterministic check
layer wants, but it is **scoped to one project class (ML pipelines)**
rather than to general benchmarks. Phase 1 should consider whether to
generalize this pattern or keep it as a per-task check module that
benchmark cases can opt into.

**Gap table (relative to ROADMAP §Phase 1 — Eval harness hardening
deliverables).**

| Gap | Phase 1 deliverable affected | Scope | Extend or replace? |
|---|---|---|---|
| Persistent results: only `eval_results.json` overwritten per run | Append-only JSONL log with timestamp + git SHA + per-task outcome | Add output writer; keep summary `print_report` | extend |
| No multi-run support: `eval.py` runs each case once per invocation | Run benchmark 3× per measurement, mean ± stdev | Wrap `run_case()` in N-loop; aggregate metrics; report variance | extend |
| No tiered benchmark: 4 trivial cases all in one bucket | 30-50 tasks tiered trivial / moderate / hard / open-ended | Add tier field to prompts.json schema; rewrite case set | extend (schema) + new case authoring |
| No deterministic check coverage for non-trivial tasks: only file_exists / file_contains / glob_exists | Deterministic checks: exit codes, file existence, content regex, pytest pass, file-shape checks | Add `pytest_pass`, `exit_code`, `file_shape`, `command_succeeds` check types in `run_check()` | extend |
| No LLM-as-judge | LLM-judge with N=5 samples and majority vote for open-ended tasks | New `llm_judge` check type; new judge module | extend |
| No reproducibility capture | git SHA, env vars, config hash recorded at run start | Single helper that snapshots `git rev-parse HEAD`, `os.environ`, hash of active config; embed in JSONL row | extend |
| No metrics beyond `elapsed`: missing LLM time, sandbox time, attempts, tokens, step count, workspace size | Per-task: pass/fail, wall, LLM time, sandbox time, attempts, input/output tokens, step count, final workspace size | Need orchestrator+architect to surface these via output JSON (`output.json` is read at L139); extend `result` dict | extend (requires upstream support from architect/orchestrator) |
| No tiered reporting: pass-rate not separated by tier | Per-tier pass rate | Aggregate by tier in `print_report()` and JSONL | extend |
| Container engine duplication: image-build/auth/rebuild logic duplicated between eval.py and tests/conftest.py | (cleanliness, not Phase 1 deliverable) | Extract shared module | deferred — flag for Phase 5 pruning consideration |
| Dead code: `setup_files` copy path with no `data/` dir | (cleanliness) | Either delete the code path or create `data/` and use it | deferred — flag for Phase 5 |

**Extend-vs-replace decision.** **Extend, do not replace.**
Justification: `eval.py` already encapsulates the most painful parts
(container detection + image staleness + auth seeding + workspace
lifecycle + case loop) in 358 lines. Replacing means re-doing that
wiring for zero gain. Every Phase 1 gap above is additive — new check
types, a new metrics dict, a new output writer, an outer N-loop, a
new tier field. None of them require a structural rewrite. The one
architectural change worth making during the extension is **factoring
`run_case()` into smaller pieces** so that the metric collection
points are clean: `setup_workspace`, `invoke_architect`,
`collect_metrics`, `run_checks`, `record_result`. That can happen
during Phase 1 implementation.

The canonical Phase 1 entry point name in the ROADMAP is `uas-eval`.
Recommendation: keep `integration/eval.py` as the implementation
file and add a thin `uas-eval` console-script entry point (or shell
wrapper) so the contract surface is stable independent of file moves.

**Prompt-case assessment.** None of the 4 cases will exercise the
self-correction machinery in normal conditions:

| Case | Goal | Will it trigger retries / rewrites / backtracking under normal LLM behavior? | Notes |
|---|---|---|---|
| `hello-file` | Create `hello.txt` with literal text | No — single-step, single-file write. Frontier LLM completes first try. | Pure smoke test. |
| `two-step-pipeline` | `step1.txt` then `step2.txt` reading step1 | No — trivial sequential chain. | Tests dependency wiring more than correction. |
| `live-data-pipeline` | Fetch JSON from open-notify, summarize count | Sometimes — only if the live API is unreachable, in which case the network failure is the LLM's fault and might trigger reflection. **Inherently flaky** because of external dependency. | Should not be in any benchmark that needs noise bounds. |
| `fibonacci-json` | First 20 Fibonacci as JSON array | No — pure compute, single file. | Smoke test. |

Conclusion: the existing prompt set is a **smoke test, not a
benchmark**. It cannot stress any of the 68 mechanisms catalogued in
§1. Phase 1 must author a fresh case set with at least
moderate / hard / open-ended tiers if the eval is going to detect
which mechanisms matter. Reusing `hello-file` as a Tier 0 sanity case
is fine; the rest should be redesigned or retired.

**`tests/conftest.py` and `tests/test_integration.py` reusability.**

- `tests/conftest.py` (226 lines) provides container helpers
  (`find_engine`, `_image_build_time`, `ensure_image`,
  `run_in_container`) and a `tmp_workspace` fixture that monkeypatches
  module-level state. The container helpers **duplicate** logic in
  `eval.py:_find_engine` / `_ensure_image` — strong candidate to
  extract into a shared `integration/runner.py` during Phase 1.
- `tests/test_integration.py` (108 lines) has 3
  `@pytest.mark.integration` smoke tests: Claude-CLI ping, Phase 1
  decomposition dry-run on a trivial goal, orchestrator-only
  hello.txt run. These are **smoke tests, not a benchmark** — no
  pass-rate aggregation, no multi-run, no metrics. They serve a
  legitimate "is the container plumbing alive" purpose and should be
  kept distinct from `uas-eval`. Not reusable as benchmark
  infrastructure.
- The wider `tests/` tree contains ~70 unit-style modules
  (`test_voting.py`, `test_replanning.py`, `test_reflexion.py`,
  `test_best_of_n.py`, `test_correction_loop.py`, …). These exercise
  individual mechanisms in isolation with mocked LLM calls. They
  validate logic, not pass-rate. Not benchmark infrastructure.

**Loose root-level script disposition.**

Confirmed via `git ls-files`: **none of the loose scripts are
git-tracked.** They are listed in `.gitignore` lines 4-11 under
"Ad-hoc test/verification scripts" and in
`tests/test_framework_layout.py` `_AD_HOC_SCRIPTS = {…}` (lines
21-27) as an explicit allowlist exempting them from the framework's
naming-convention test. They are intentional personal-utility scratch
that lives in the working tree but never enters version control.

| Path | Tracked? | Wired into pipeline? | Disposition |
|---|---|---|---|
| `run_test_verification.py` | no (gitignored) | no | leave alone — personal utility, not a Phase 5 concern |
| `verify_test_goal.py` | no (gitignored) | no | leave alone — but **near-identical duplicate** of `test_goal_verify.py` (only print-prefix differs); user may want to delete one locally |
| `test_goal_verify.py` | no (gitignored) | no | leave alone — duplicate of above |
| `test_goal_result.py` | no (gitignored) | no | leave alone — personal utility |
| `test_goal_output.txt` | no (gitignored) | no | stale output of `verify_test_goal.py` / `test_goal_verify.py` runs |
| `test_verification_result.txt` | no (gitignored) | no | stale output of `run_test_verification.py` runs (records 88 tests passed across 4 test files) |
| `check_environment.py` | no (gitignored) | no | also in the `_AD_HOC_SCRIPTS` allowlist; personal utility, not in PLAN's check list but worth noting |

**Phase 5 implication.** None of these enter the prune-list. They
are out-of-scope for the codebase audit because they are out-of-tree
from git's perspective. If the user wants to clean local scratch,
that's a separate manual housekeeping action.

---

## 3. Configuration knob inventory

**Loader behavior (`uas_config.py`).** Four layers, later overrides
earlier:

1. Built-in defaults — `DEFAULTS` dict at uas_config.py:28-91 (47 keys)
2. User-global TOML — `~/.config/uas/config.toml`
3. Project TOML — `{workspace}/.uas/config.toml`
4. `UAS_*` env vars — checked **live at access time** (not cached)

Key normalization: env var `UAS_FOO_BAR` ↔ config key `foo_bar`.
Dotted keys (`context_janitor.formatter`) flatten to
`UAS_CONTEXT_JANITOR_FORMATTER` (uas_config.py:185, 191). Type
coercion uses the default value's type as reference
(uas_config.py:117-132): bool from `1/true/yes`, int/float parsed,
string passthrough.

**Inconsistency observed.** Some hot-path modules bypass
`config.get()` and read `os.environ` directly, even when the same
key has a default in the layered loader. This means project-level
TOML overrides for these keys are silently ignored:

| Module | Direct env read | Has DEFAULTS entry? | Effect |
|---|---|---|---|
| `architect/planner.py:13` | `UAS_MAX_ERROR_LENGTH` | yes (`max_error_length`) | TOML override of `max_error_length` ignored by planner module-level constant |
| `architect/planner.py:24` | `UAS_MINIMAL` | yes (`minimal`) | TOML override of `minimal` ignored by planner module-level constant |
| `orchestrator/sandbox.py:23` | `UAS_SANDBOX_IMAGE` | no | Not exposed via config layer at all |
| `orchestrator/sandbox.py:25` | `UAS_SANDBOX_TIMEOUT` | no | Not exposed via config layer at all |
| `orchestrator/sandbox.py:27` | `UAS_SANDBOX_MODE` | yes (`sandbox_mode`) | Bypasses TOML; functional duplicate |
| `orchestrator/sandbox.py:28` | `UAS_WORKSPACE` | yes (`workspace`) | Bypasses TOML; sandbox-internal |
| `orchestrator/sandbox.py:29` | `UAS_PROJECT_NAME` | no | Not exposed anywhere; **fully undocumented** |
| `orchestrator/sandbox.py:157-158` | `UAS_HOST_UID`, `UAS_HOST_GID` | no | Not exposed via config layer |
| `orchestrator/main.py:746` | `UAS_TASK` (fallback) | yes (`task`) | Last-resort fallback path |
| `uas_hooks.py:71` | `UAS_WORKSPACE` | yes (`workspace`) | Reasonable — runs before config loaded |
| `uas_config.py:168` | `UAS_WORKSPACE` | yes | Used to find the project TOML — necessarily bootstrap |

Phase 3 implication: making any of these knobs into a clean ablation
flag will need to either re-route through `config.get()` or add a
shim. The hottest two — `planner.py` reading `UAS_MINIMAL` directly —
are the highest-risk for "I disabled the flag and nothing changed"
debugging surprises during Phase 4 ablation runs.

**Merged inventory (47 DEFAULTS keys + 6 env-only knobs = 53 total).**
Markers below:

- `D` = present in `DEFAULTS` dict (`uas_config.py`)
- `T` = present in `uas.example.toml` (commented or active)
- `R` = present in README §Environment Variables table
- `C` = at least one call site in code (`config.get` or `os.environ.get`)

| # | Knob | D | T | R | C | Class | Notes |
|---|---|---|---|---|---|---|---|
| 1 | `workspace` / `UAS_WORKSPACE` | ✓ | ✓ | ✓ | ✓ | configuration | Multi-source, hot-path |
| 2 | `model` / `UAS_MODEL` | ✓ | ✓ | ✓ | ✓ | configuration | Default model |
| 3 | `model_planner` / `UAS_MODEL_PLANNER` | ✓ | ✓ | ✓ | ✓ | configuration | Role override |
| 4 | `model_coder` / `UAS_MODEL_CODER` | ✓ | ✓ | ✓ | ✓ | configuration | Role override |
| 5 | `sandbox_mode` / `UAS_SANDBOX_MODE` | ✓ | ✓ | ✓ | ✓ | configuration | container/local |
| 6 | `max_parallel` / `UAS_MAX_PARALLEL` | ✓ | ✓ | ✓ | ✓ | tuning | 0=auto |
| 7 | `max_context_length` / `UAS_MAX_CONTEXT_LENGTH` | ✓ | ✓ | ✓ | ✓ | tuning | Tier-compression trigger |
| 8 | `max_error_length` / `UAS_MAX_ERROR_LENGTH` | ✓ | ✓ | ✓ | ✓ | tuning | **Bypassed by planner.py:13** |
| 9 | `llm_timeout` / `UAS_LLM_TIMEOUT` | ✓ | ✓ | ✓ | ✓ | tuning | Per-call cap |
| 10 | `persistent_retry` / `UAS_PERSISTENT_RETRY` | ✓ | ✓ |  | ✓ | toggle | Indefinite-retry mode; **not in README** |
| 11 | `rate_limit_wait` / `UAS_RATE_LIMIT_WAIT` | ✓ | ✓ | ✓ | ✓ | tuning | Backoff base |
| 12 | `rate_limit_max_wait` / `UAS_RATE_LIMIT_MAX_WAIT` | ✓ | ✓ | ✓ | ✓ | tuning | Backoff cap |
| 13 | `rate_limit_retries` / `UAS_RATE_LIMIT_RETRIES` | ✓ | ✓ | ✓ | ✓ | tuning | Retry budget |
| 14 | `usage_limit_wait` / `UAS_USAGE_LIMIT_WAIT` | ✓ | ✓ | ✓ | ✓ | tuning | Quota backoff |
| 15 | `usage_limit_retries` / `UAS_USAGE_LIMIT_RETRIES` | ✓ | ✓ | ✓ | ✓ | tuning | Quota retry budget |
| 16 | `best_of_n` / `UAS_BEST_OF_N` | ✓ | ✓ | ✓ | ✓ | toggle/tuning | 1 disables; 2-3 enables |
| 17 | `keep_last_runs` / `UAS_KEEP_LAST_RUNS` | ✓ | ✓ | ✓ | ✓ | tuning | State retention |
| 18 | `max_run_age_days` / `UAS_MAX_RUN_AGE_DAYS` | ✓ | ✓ | ✓ | ✓ | tuning | State retention |
| 19 | `fuzzy_enabled` / `UAS_FUZZY_ENABLED` | ✓ |  |  | ✓ | toggle | Disables fuzzy function system globally; **not in README/TOML** |
| 20 | `tdd_enforce` / `UAS_TDD_ENFORCE` | ✓ |  |  | ✓ | toggle | Disables TDD planning gate AND full-suite runner; **not in README/TOML** |
| 21 | `context_janitor.formatter` / `UAS_CONTEXT_JANITOR_FORMATTER` | ✓ |  |  | ✓ | toggle/configuration | "ruff"/"black"/"none" — clean disable via "none"; **not in README/TOML** |
| 22 | `minimal` / `UAS_MINIMAL` | ✓ | ✓ | ✓ | ✓ | toggle (bundle) | Disables many mechanisms at once |
| 23 | `verbose` / `UAS_VERBOSE` | ✓ | ✓ | ✓ | ✓ | configuration | Logging level |
| 24 | `dry_run` / `UAS_DRY_RUN` | ✓ | ✓ | ✓ | ✓ | toggle | Plan-only mode |
| 25 | `explain` / `UAS_EXPLAIN` | ✓ |  | ✓ | ✓ | configuration | Run explanation output |
| 26 | `resume` / `UAS_RESUME` | ✓ |  | ✓ | ✓ | toggle | Resume saved state |
| 27 | `no_llm_guardrails` / `UAS_NO_LLM_GUARDRAILS` | ✓ | ✓ | ✓ | ✓ | toggle | Disables LLM guardrail review (regex still runs) |
| 28 | `goal` / `UAS_GOAL` | ✓ |  | ✓ | ✓ | configuration | Architect input |
| 29 | `goal_file` / `UAS_GOAL_FILE` | ✓ |  | ✓ | ✓ | configuration | Architect input from file |
| 30 | `task` / `UAS_TASK` | ✓ |  | ✓ | ✓ | configuration | Orchestrator input |
| 31 | `output` / `UAS_OUTPUT` | ✓ |  | ✓ | ✓ | configuration | JSON results path |
| 32 | `report` / `UAS_REPORT` | ✓ |  | ✓ | ✓ | configuration | HTML report path |
| 33 | `trace` / `UAS_TRACE` | ✓ |  | ✓ | ✓ | configuration | Perfetto trace path |
| 34 | `events` / `UAS_EVENTS` | ✓ |  | ✓ | ✓ | configuration | Event log path |
| 35 | `run_id` / `UAS_RUN_ID` | ✓ |  |  | ✓ | configuration (internal) | Subprocess identity |
| 36 | `step_id` / `UAS_STEP_ID` | ✓ |  |  | ✓ | configuration (internal) | Subprocess identity |
| 37 | `spec_attempt` / `UAS_SPEC_ATTEMPT` | ✓ |  |  | ✓ | configuration (internal) | Subprocess identity |
| 38 | `workspace_files` / `UAS_WORKSPACE_FILES` | ✓ |  |  | ✓ | configuration (internal) | Architect→orchestrator handoff |
| 39 | `step_environment` / `UAS_STEP_ENVIRONMENT` | ✓ |  |  | ✓ | configuration (internal) | Architect→orchestrator handoff |
| 40 | `step_spec` / `UAS_STEP_SPEC` | ✓ |  |  | ✓ | configuration (internal) | Architect→orchestrator handoff |
| 41 | `host_workspace` / `UAS_HOST_WORKSPACE` | ✓ |  |  | ✓ | configuration (internal) | Architect→orchestrator handoff |
| 42 | `truncation_detected` / `UAS_TRUNCATION_DETECTED` | ✓ |  |  | ✓ | configuration (internal) | Architect→orchestrator signal |
| 43 | `test_files` / `UAS_TEST_FILES` | ✓ |  |  | ✓ | configuration (internal) | Architect→orchestrator handoff |
| 44 | `UAS_SANDBOX_IMAGE` |  |  | ✓ | ✓ | configuration | Direct env in sandbox.py:23 — bypasses config layer |
| 45 | `UAS_SANDBOX_TIMEOUT` |  |  | ✓ | ✓ | tuning | Direct env in sandbox.py:25 — bypasses config layer |
| 46 | `UAS_HOST_UID` |  |  | ✓ | ✓ | configuration | Direct env in sandbox.py:157 |
| 47 | `UAS_HOST_GID` |  |  | ✓ | ✓ | configuration | Direct env in sandbox.py:158 |
| 48 | `UAS_PROJECT_NAME` |  |  |  | ✓ | configuration | Direct env in sandbox.py:29 — **fully undocumented** |
| 49 | `UAS_FUZZY_MODEL` |  |  |  | ✓ | configuration | Comment in fuzzy.py:43; reads `config.get("fuzzy_model")` but `fuzzy_model` not in DEFAULTS — **partially wired** |
| 50 | `UAS_STEP_CONTEXT` |  |  |  | ✓ (test only) | configuration (internal) | Test reference at test_pipeline_smoke.py:349 — possibly dead/test-only |
| 51 | `ANTHROPIC_API_KEY` |  |  | ✓ | (external) | configuration | Anthropic SDK env, not a UAS knob |

**Marker constants (NOT knobs).** These are output-protocol or
log-marker strings, not configuration:

- `UAS_RESULT` — sandbox stdout success/failure marker (parsed at
  `orchestrator/main.py:83`, `architect/executor.py:567-568`)
- `__UAS_ORCH_USAGE__` — internal usage telemetry marker
- `__UAS_ORCH_SANDBOX__` — internal sandbox-time telemetry marker

**Discrepancy summary.**

| Class | Count | Examples |
|---|---|---|
| Documented in all 3 sources (D + T + R) | 22 | model, sandbox_mode, max_parallel, best_of_n, rate_limit_*, minimal |
| In DEFAULTS + README, missing from TOML | 9 | goal, goal_file, task, output, report, trace, events, explain, resume |
| In DEFAULTS + TOML, missing from README | 1 | persistent_retry |
| In DEFAULTS only (missing TOML and README) | 11 | fuzzy_enabled, tdd_enforce, context_janitor.formatter, plus 8 internal handoff vars (run_id, step_id, spec_attempt, workspace_files, step_environment, step_spec, host_workspace, truncation_detected, test_files) |
| In README only (env-direct, missing DEFAULTS and TOML) | 4 | UAS_SANDBOX_IMAGE, UAS_SANDBOX_TIMEOUT, UAS_HOST_UID, UAS_HOST_GID |
| In code only (missing DEFAULTS, TOML, README) | 3 | UAS_PROJECT_NAME, UAS_FUZZY_MODEL, UAS_STEP_CONTEXT |
| Documentation drift (planner.py reads env directly despite DEFAULTS entry) | 2 | UAS_MAX_ERROR_LENGTH, UAS_MINIMAL |

**The three highest-priority gaps for Phase 3 documentation/wiring
hygiene:**

1. `tdd_enforce`, `fuzzy_enabled`, `context_janitor.formatter` —
   exist as toggles but are not in README or TOML. These are exactly
   the kind of feature toggles Phase 3 wants to standardize.
2. `UAS_PROJECT_NAME` — fully undocumented; either delete (if dead)
   or document.
3. `UAS_FUZZY_MODEL` — partially wired; either add `fuzzy_model` to
   DEFAULTS or remove the comment.

**Phase 3 ablation-flag coverage analysis.**

Cross-referencing the 68-row §1 mechanism catalog against the
inventory above:

| Coverage class | Mechanism count | Examples |
|---|---|---|
| **Clean single-mechanism flag** (one knob, one mechanism, no side effects) | **5** | TDD enforcement (#8 + #43, shared `UAS_TDD_ENFORCE`), Best-of-N (#46, `UAS_BEST_OF_N=1`), Context janitor (#54, `UAS_CONTEXT_JANITOR_FORMATTER=none`), Guardrails LLM (#40, `UAS_NO_LLM_GUARDRAILS` — partial: regex still runs) |
| **Bundled disable** via `UAS_MINIMAL` (group toggle with no per-mechanism control) | **8** | Research phase (#18), Git checkpoint (#31), Best-of-N evaluate-LLM (#49), Pre-flight LLM (#51), Holistic validation (#56), Post-run meta learning (#58), Cross-run KB write (#62), and the LLM half of guardrails (#40 dual-counted) |
| **Tunable but cannot be cleanly disabled** | **4** | Tiered context compression (#15 — `UAS_MAX_CONTEXT_LENGTH`), Adaptive retry budget (#22 — `MAX_SPEC_REWRITES` constant), Rate-limit retry (#29 — multiple knobs, no on/off), Verify-step-output (#35 — per-step `verify` field) |
| **Orthogonal mode flags** (not really ablations) | **2** | Resume (#59 — `UAS_RESUME`), Dry-run (#60 — `UAS_DRY_RUN`) |
| **No flag at all** | **49** | (see §1 catalog rows without an "Existing flag" entry) |

**Answer to the explicit Phase 3 question.**

> "How much of Phase 3 ablation flag work is already done, as a
> percentage of mechanisms from §1?"

- **Strict** (clean single-mechanism flag, ablation actually
  isolates one mechanism without disabling others):
  **5 / 68 ≈ 7%**.
- **Loose** (clean flag OR bundled in `UAS_MINIMAL`):
  **13 / 68 ≈ 19%**.
- The remaining **49 / 68 ≈ 72%** of mechanisms have **no
  ablation control whatsoever**.

`UAS_MINIMAL` is the dominant existing knob and it is exactly the
shape Phase 3 needs to decompose: it bundles ~8 unrelated mechanisms
behind one switch, making it impossible to attribute any
per-mechanism delta. Phase 3's first concrete deliverable should be
replacing `UAS_MINIMAL` with one flag per disable target it currently
controls.

**Phase 3 work estimate.** ~50 new flags need to be created from
scratch. ~5 existing flags can be reused as-is. ~8 bundled-in-MINIMAL
mechanisms need decomposition (1 flag → many flags) before Phase 4
ablation can interpret single-flag results meaningfully.

---

## 4. Mechanism dependency map

**Method.** "Mechanism X depends on Y" iff X reads state, files, env
vars, or data structures that only Y writes — i.e. silently disabling
Y degrades or breaks X. Edges below are derived from the §1 catalog
plus the call-site reads performed during §1 and §3. Pure call-graph
relationships ("X is invoked from inside Y") are *not* dependencies
in this sense; only data-flow coupling is. Helpers that aren't
standalone §1 mechanisms (e.g. "module API extraction", "git state
machine", "fuzzy function system") are noted in parentheses but not
treated as ablation targets.

**Adjacency list (mechanism → mechanisms it consumes).**

Format: `#N <name>` → comma-separated list of dependencies, or
`(none)`. Helper-only deps are bracketed.

*Planning pipeline (Cluster 1):*

- #2 Complexity gate → (none)
- #1 Multi-plan voting → #2
- #3 Enforced minimum steps → #2
- #17 Goal expansion → #18
- #18 Research phase → #19
- #19 Project specification → [workspace context helper]
- #4 Step merging trivial → [topo sort]
- #5 Step merging LLM → #4 (fallback), [topo sort]
- #6 Split coupled steps → (none — pure LLM call)
- #7 Insert integration checkpoints → #4, #5, #6 (operates on merged/split DAG), [topo sort]
- #9 Coverage requirements matrix → [requirement extractor]
- #10 Fill coverage gaps → #9
- #11 Ensure goal coverage → #9, #10
- #8 TDD enforcement (planning gate) → [test step contract validator]

*Reflection / retry decision pipeline (Cluster 2 — strongest coupling):*

- #20 Generate reflection → #21 (heuristic fallback)
- #21 Classify error (heuristic) → #20 (used as fallback when #20 LLM fails)
- #22 Adaptive retry budget → #20
- #23 Stagnation detection (text similarity) → #20, #63
- #68 LLM retry-decision prompt → #20, #23
- #64 Truncation detection → #20
- #27 Reflect-and-rewrite → #20, #22, #23, #68 (full reflection history + retry decision)
- #28 Decompose failing step → #21
- #29 Rate-limit retry → #21
- #66 Data quality error classification → #32

*Counterfactual + backtrack (Cluster 3 — depends on Cluster 2):*

- #25 Counterfactual root-cause tracing → #20, [step dependency graph]
- #26 Informed backtracking → #25
- #24 Verification stagnation → #63

*Per-step execution + cleanup:*

- #61 Environment probe → (none)
- #50 PyPI version resolution → (none — external HTTP)
- #51 Pre-flight LLM check → #52
- #52 Code quality assessment → [fuzzy system]
- #53 Retry-clean prompt section → #21
- #65 Step spec propagation → (none — internal handoff)
- #67 Collect test files → [git_state]
- #13 Distill dependency output (heuristic) → (none — operates on raw stdout)
- #14 Distill dependency output (LLM) → #13 (fallback path)
- #12 Step description enrichment → [file schema extractor]
- #16 Structured progress file → [event log], [scratchpad]
- #15 Tiered context compression → #16, [LLM summarizer], [regex stripper]

*Validation / guardrails (Cluster 4):*

- #34 Validate UAS_RESULT → [UAS_RESULT parser]
- #32 Output quality checks → #34
- #33 Input quality checks → #34 (reads upstream UAS_RESULT)
- #35 Verify-step-output → #34, [orchestrator invoker]
- #36 Workspace cleanup (recursive diff) → [workspace snapshot]
- #37 Nested duplication resolution → #36, [nested-structure heuristic]
- #38 Project manifest + supersession → #36, #37, [LLM]
- #39 Best-practice guardrails (regex) → (none — pure regex over code)
- #40 Best-practice guardrails (LLM) → #39 (LLM only runs if regex found no errors)
- #41 Cross-module import validation → [module API extractor]
- #42 Orphaned module detection → [module API extractor]
- #55 Smoke-test entry point → [workspace scan]
- #56 Holistic workspace validation → #39, #40, #41, #42, #55
- #57 Corrective steps generation → #56

*Mid-execution replanning (Cluster 5):*

- #45 Should-replan heuristic → [regex file ref patterns]
- #44 Replan remaining steps → #45 (gate), #9, #10, [requirement extractor]

*Best-of-N (Cluster 6):*

- #46 Best-of-N code generation → #48 (scoring)
- #47 Best-of-N budget LLM → #46 (gates how many candidates), #48
- #48 Score result → [UAS_RESULT parser], [score guidance cache]
- #49 Evaluate candidates LLM → #48 (fallback)

*State / persistence (Cluster 7):*

- #31 Git checkpoint per step → [git state machine]
- #30 3-strike git rollback → #31
- #59 Resume from saved state → [state.json], #16, #63
- #58 Post-run meta learning → #20, [knowledge base]
- #62 Cross-run knowledge base → #58 (write side); read side stands alone
- #63 Attempt history tracking → (none — consumed by #23, #24, #27)

*TDD ecosystem (Cluster 8):*

- #43 TDD full-suite runner → [pytest], #67
- (#8 already listed — pre-execution gate, same flag)

*Orchestrator post-edit:*

- #54 Context janitor → [ruff/black/linter]

*Modes (orthogonal, not really mechanisms):*

- #59 Resume → already listed
- #60 Dry-run mode → #1–#11 (planning pipeline runs to completion, then exits)

*External-API:*

- #50 PyPI version resolution → (already listed)

**Strongly-coupled clusters.** A "strong cluster" is a set of
mechanisms where disabling any one in isolation produces meaningless
or misleading data because the others silently fail-soft to a
degraded state. Phase 4 must run **group ablations** for these in
addition to single-flag ablations.

| # | Cluster | Members | Why coupled | Essential or incidental? |
|---|---|---|---|---|
| A | **Reflection / retry decision** | #20, #21, #22, #23, #27, #28, #29, #53, #64, #66, #68 | Every member reads `reflection.error_type` or `reflection_history`. If #20 is silently disabled, #21 becomes the sole signal source (heuristic-only) and #22, #23, #27, #28, #64, #68 receive degraded data without raising. **#21 is intentionally a fallback for #20** — they're a redundant pair, not a chain. | Essential — the reflection contract is the single source of truth for failure analysis. Decoupling would require duplicating the data-flow scaffolding. |
| B | **Counterfactual + backtrack** | #25, #26, depends on Cluster A | #25 reads reflection (#20). #26 only fires if #25 returns a dependency target. Disabling #25 silently disables #26. Disabling #20 silently disables both. | Essential — the backtrack target is computed from reflection's root_cause. |
| C | **Best-of-N family** | #46, #47, #48, #49 | All four are gated by `UAS_BEST_OF_N`. Setting it to 1 disables all four cleanly. The scoring (#48), LLM evaluation (#49), and budget LLM (#47) are inert without the parent #46. | Incidental but tight — already cleanly bundled behind one flag. Phase 4 should ablate at the #46 boundary; finer-grained ablation needs new flags. |
| D | **Validation cascade** | #39, #40, #41, #42, #55, #56, #57 | #56 invokes #39, #40, #41, #42, #55 internally. #57 only runs when #56 returns issues. Disabling #56 (only path: `UAS_MINIMAL`) silently disables 5 child mechanisms. #40 also depends on #39 (only runs if regex found no errors). | Essential at the #56 → children level; the #40 → #39 fallback is incidental. |
| E | **Coverage-driven planning + replan** | #9, #10, #11, #44, #45, #57 (re-used) | #11 invokes #9 + #10. #44 invokes #9 + #10 + #45-as-gate. #57 also reuses the same coverage scaffolding. Disabling #9 silently breaks #10, #11, #44, and possibly #57. | Essential — coverage matrix is the shared data structure. |
| F | **TDD pair** | #8, #43, #67 | All three are gated by `UAS_TDD_ENFORCE` (planning gate, post-step suite, test-file collection for orchestrator context). Already cleanly bundled. | Incidental — already a clean group. |
| G | **Git state pair** | #30, #31 | #30 reads the checkpoint #31 created. Disabling #31 silently breaks #30 (the rollback would have nothing to roll back to). | Essential — the git state machine is the single source. |
| H | **Step DAG transforms** | #4, #5, #6, #7 | Each operates on the output of the previous. Order-sensitive: e.g. #7 (integration checkpoint insertion) only meaningfully runs after #6 (split coupled) has expanded the DAG. | Incidental — could be re-ordered, but disabling early ones silently changes what later ones see. |
| I | **Persistence + recovery** | #16, #59, #63 | #59 resumes from state that #16 (progress file) and #63 (attempt history) wrote. Disabling either writer silently breaks the resume path. | Essential — resume is the only consumer; producers are independent. |
| J | **Post-run learning loop** | #20, #58, #62 | #58 reads #20's reflection history and writes it to #62's KB. Cross-run KB is consumed by future runs at startup. | Essential — single linear write path. |

**Phase 4 group-ablation list.** In addition to single-flag ablations
of every mechanism that has a flag (or every new flag added in
Phase 3), Phase 4 must run these group ablations to interpret
single-flag results correctly:

| Group ablation name | Disables together | Rationale |
|---|---|---|
| **`group_no_reflection`** | #20, #21 | Cluster A — disable the reflection contract entirely; everything downstream falls back to "no information" path. Establishes the "what does it cost to have no reflection at all?" baseline. |
| **`group_no_failure_decision`** | #20, #21, #22, #23, #27, #28, #64, #66, #68 | Cluster A complete — all of the failure-decision pipeline. Compare against `group_no_reflection` to isolate the *decision* layer's contribution from the *signal* layer's contribution. |
| **`group_no_backtrack`** | #25, #26 | Cluster B — fingers are kept off backtracking entirely. |
| **`group_no_validation_cascade`** | #39, #40, #41, #42, #55, #56, #57 | Cluster D — disable holistic validation and all its children together. Match this against the `UAS_MINIMAL` baseline. |
| **`group_no_replan`** | #44, #45, #57 (replan path) | Cluster E — disable mid-execution replanning. #9, #10 are kept enabled because they're also used at planning time (Cluster 1); only the replan invocation is disabled. |
| **`group_no_best_of_n`** | #46, #47, #48, #49 | Cluster C — clean group disable via `UAS_BEST_OF_N=1`. Already supported. |
| **`group_no_tdd`** | #8, #43, #67 | Cluster F — clean via `UAS_TDD_ENFORCE=0`. Already supported. |
| **`group_no_git_state`** | #30, #31 | Cluster G — disable git checkpointing AND the rollback that depends on it together. Single-flag #30 ablation is meaningless. |
| **`group_no_persistence`** | #16, #63 | Cluster I — disable progress file and attempt history. #59 (resume) becomes a no-op naturally. |
| **`group_no_dag_transforms`** | #4, #5, #6, #7 | Cluster H — disable all step-DAG mutations. The raw decomposition output is what gets executed. Establishes the planner-only baseline. |
| **`group_no_cross_run_learning`** | #58, #62 | Cluster J — disable both the writer and the read consumer. #20 still runs (used elsewhere). |

Single-flag ablations of any *individual* mechanism inside Clusters
A, B, D, E, G, I, J should be paired with the corresponding group
ablation in the same row of the Phase 4 results table — otherwise the
delta is unattributable. Clusters C, F, H are already cleanly
bundled and can be ablated as groups without surprises.

**Cluster summary by coupling type.**

- **Essential coupling** (cannot be decoupled without scaffolding
  rewrite): A, B, D (parent→children), E, G, I, J — **7 clusters**.
- **Incidental coupling** (cleanly bundled, decouplable in principle
  but not currently): C, F, H, D (#40→#39 sub-coupling) — **3-4
  clusters**.

The **7 essential clusters** are the ones Phase 4 must mandatorily
run as group ablations even after Phase 3 adds per-mechanism flags,
because the data-flow scaffolding cannot be flagged away without
rewriting the consumers.
