# Phase 4 cut list (closed set)

This is the Phase 4 §1 deliverable. It finalises the
`docs/cut_surface.md` Phase 2 §4 sized estimate into a closed
KEEP / CUT / DEFER classification at file granularity, with §3 / §4 /
§5 / §6 owner assignments and a dependency-ordered deletion plan.
Every tracked file in the repo appears exactly once; every Phase 0
catalog mechanism is mapped to at least one CUT-side or KEEP-side
file.

Sources: `git ls-files` (164 entries at PLAN-author commit
`87d80b4`), ROADMAP §Phase 4 — Prune (keep list + cut surface),
`docs/substrate.md` (8 Phase 1/2 substrate components),
`docs/cut_surface.md` (Phase 2 §4 sized estimate),
`docs/orchestrator.md` (Phase 3 design), `phase0_audit.md`
(68-mechanism catalog), `wc -l` against the live tree, and
`grep` of the import graph.

## Headline

| Bucket | Files | Lines (live tree) |
|---|---|---|
| **KEEP** | 53 | ≈ 15,300 (incl. ~460 lines of `integration/eval.py` cut tail still co-resident pending §5 surgery) |
| **DEFER → CUT under §6** | 3 | 510 |
| **CUT** | 108 | ≈ 46,254 (text) + 1 PNG (~260 KB binary) |
| **Total tracked** | 164 | ≈ 62,064 (text) |

Sum of `wc -l` over all CUT + DEFER files (text only):
**≈ 46,764 lines**. This is the pre-§5 figure — `integration/eval.py`'s
~460-line cut tail (the `invoke_architect`, `_import_llm_judge`, and
`llm_judge` dispatch sites; estimate per `cut_surface.md`) is still
co-resident in a KEEP file at this point and folds into the §5
shrinkage rather than the §3–§6 file-deletion shrinkage.

The Phase 0 mechanism catalog reconciles exactly: **0 of 68** map
to KEEP files (see "Mechanism reconciliation" below); **68 of 68**
map to CUT files.

## KEEP (53 files)

### Phase 1 substrate (per ROADMAP §Phase 4 keep list)

| Path | Lines | Substrate component |
|---|---|---|
| `orchestrator/sandbox.py` | 201 | #1 Sandbox primitive (Phase 4 surgery: none) |
| `Containerfile` | 53 | #1 Sandbox image — needs §6 surgery to drop `COPY uas_config.py`, `COPY uas_hooks.py`, `COPY architect/`, `COPY uas/`, and the `ENTRYPOINT ["/uas/entrypoint.sh"]` line (entrypoint.sh dispatches only to architect) |
| `integration/auth.py` | 199 | #2 OAuth 4-stage refresh (lifted from `eval.py` in Phase 3 §1) |
| `integration/eval.py` | 1,342 | Substrate slice (#2 re-export wrappers, #3 JSONL log, #5 workspace iso., #6 resume-from-JSONL, #7 `run_check` deterministic types + harness loop). §5 surgery removes the `invoke_architect` body, the `_import_llm_judge` dispatcher, and the `llm_judge` branch in `run_check` (~460 lines per `cut_surface.md`); the rest stays |
| `integration/provenance.py` | 130 | #4 Provenance metadata (lifted from `eval.py` in Phase 3 §1) |
| `integration/eval_results.jsonl` | 2 | Durable JSONL log (the §10 baseline row plus current header) |
| `integration/cases/trivial/hello-file.json` | 11 | #7 Hello-file smoke case |
| `integration/__init__.py` | 0 | Package marker |
| `uas-eval` | 13 | #7 Eval harness shell wrapper |

### Phase 3 deliverables (orchestrator daemon)

All Phase 3 §1–§8 modules + their fixture cases.

| Path | Lines | Phase 3 § |
|---|---|---|
| `orchestrator/__init__.py` | 0 | §1 (post-cleanup) |
| `orchestrator/cli.py` | 566 | §1 / §7 / §8 |
| `orchestrator/worker.py` | 268 | §2 |
| `orchestrator/container.py` | 75 | §2 |
| `orchestrator/rate_ledger.py` | 189 | §3 |
| `orchestrator/buffer_ledger.py` | 203 | §4 |
| `orchestrator/pricing.py` | 119 | §4 |
| `orchestrator/policy.py` | 359 | §5 |
| `orchestrator/policy.default.toml` | — | §5 |
| `orchestrator/task.py` | 688 | §6 / §7 |
| `orchestrator/workspace.py` | 49 | §6 |
| `orchestrator/cases/synthetic-multistep.toml` | — | §8 |
| `orchestrator/cases/synthetic-multistep-policy.toml` | — | §8 |
| `uas-orchestrate` | 11 | §1 |

### Tests (Phase 3 + Phase 1 eval coverage)

15 files. Subject-on-keep-list rule per PLAN §1 step 2.

| Path | Subject |
|---|---|
| `tests/__init__.py` | Package marker (empty) |
| `tests/conftest.py` | Shared fixtures — keeps `find_engine`, `_image_build_time`, `_latest_source_mtime`, `ensure_image`, `_has_valid_auth`, `require_auth`, `uas_engine` (used by `test_orchestrator_worker.py`); `tmp_workspace` fixture (lines 21–39, imports `architect.main`/`architect.state`) becomes dead-but-importable post-§3 (no kept test invokes it; the imports are inside the function body, not at module top, so collection still succeeds). PLAN's "no test mutation" rule keeps the dead fixture in place until a future cleanup. |
| `tests/test_eval_checks.py` | `integration/eval.py` `run_check` deterministic types |
| `tests/test_eval_metadata.py` | `integration/provenance.py` via `integration.eval` re-export — exercises `_hash_active_config` "unavailable" fallback (`TestHashActiveConfig`) |
| `tests/test_eval_persistence.py` | `integration/eval.py` JSONL append layer |
| `tests/test_eval_resume.py` | `integration/eval.py` resume-from-JSONL gate |
| `tests/test_eval_tiers.py` | `integration/eval.py` tier schema + reporting |
| `tests/test_eval_variance.py` | `integration/eval.py` multi-run aggregation |
| `tests/test_orchestrator_buffer_ledger.py` | `orchestrator/buffer_ledger.py` |
| `tests/test_orchestrator_loop.py` | `orchestrator/cli.py` end-to-end loop |
| `tests/test_orchestrator_policy.py` | `orchestrator/policy.py` |
| `tests/test_orchestrator_rate_ledger.py` | `orchestrator/rate_ledger.py` |
| `tests/test_orchestrator_resume.py` | `orchestrator/task.py` resume-replay |
| `tests/test_orchestrator_task.py` | `orchestrator/task.py` model + JSONL |
| `tests/test_orchestrator_worker.py` | `orchestrator/worker.py` (uses `require_auth` + `uas_engine` fixtures) |

### Documentation, configuration, build (root + docs)

| Path | Lines | Rationale |
|---|---|---|
| `README.md` | — | Placeholder until §8 rewrite from scratch |
| `ROADMAP.md` | — | Strategic direction (live document) |
| `PLAN.md` | — | Phase 4 working file (deleted at phase close per convention) |
| `CLAUDE.md` | — | Session protocol (live document) |
| `phase0_audit.md` | 670 | Historical record; keep per ROADMAP §"Current state of the codebase" |
| `docs/orchestrator.md` | — | Phase 3 design notes |
| `docs/substrate.md` | — | Phase 2 substrate boundary catalog |
| `docs/cut_surface.md` | — | Phase 2 §4 sized estimate this file finalises |
| `LICENSE` | 217 | Apache 2.0 |
| `pytest.ini` | 6 | Test runner config |
| `requirements.txt` | 15 | Python deps |
| `.gitignore` | 61 | Build artefacts + auth dir + runtime state |
| `.containerignore` | 17 | Build context filter |
| `setup_auth.sh` | 93 | OAuth setup (substrate keep-list per PLAN §6.3); the `install.sh first` error message at line 34 is stale once §6 cuts `install.sh` — drop or rewrite that line during §6 |
| `framework_settings.json` | 18 | Canonical Claude defaults consumed by `setup_auth.sh` |

## DEFER → CUT under §6

Phase 3 §1 design committed: "trio defaults to CUT for Phase 4 if
§2–§8 close without consumption." Phase 3 §1–§8 closed without
consuming the layered loader. PLAN §2 confirms the trio's
default-CUT verdict fires; this row records it.

| Path | Lines | Resolution (PLAN §2) |
|---|---|---|
| `uas_config.py` | 216 | CUT under §6. Importers: `architect/{executor,main,planner,state}.py`, `orchestrator/llm_client.py`, `orchestrator/main.py`, `uas/fuzzy.py`, `uas/janitor.py` — all in §3 / §4 cut buckets. `integration/provenance.py:_hash_active_config` accesses it via `importlib.util.spec_from_file_location` with an explicit `"unavailable"` return when the file is missing; fallback exercised by `tests/test_eval_metadata.py::TestHashActiveConfig::test_returns_hex_or_unavailable` |
| `uas_hooks.py` | 215 | CUT under §6. Importers: `architect/main.py`, `architect/planner.py`, `tests/test_hooks.py` — all in §3 / §6 cut buckets. No keep-list code references it |
| `uas.example.toml` | 79 | CUT under §6. Config-key discovery aid for the layered loader; cuts with `uas_config.py` |

## CUT (108 files)

Default verdict on any mechanism, file, or symbol is cut. Files
below are bucketed by the § that owns the deletion. Within each
bucket, deletion order is leaves-before-roots so imports resolve
and the kept-test subset passes after every section closes.

### §3 owner — `architect/` tree + architect-importing tests (77 files)

#### Source files (16)

| Path | Lines | Mechanism cluster (per `phase0_audit.md`) |
|---|---|---|
| `architect/main.py` | 6,915 | ~37 mechanisms: failure handling, validation cascade, retry decisions, git checkpoint per step (#31), 3-strike git rollback (#30), output quality (#32), input quality (#33), `UAS_RESULT` validation (#34), workspace cleanup (#36), nested duplication (#37), project manifest (#38), best-practice guardrails (#39, #40), cross-module import validation (#41), orphaned module detection (#42), TDD full-suite (#43), should-replan heuristic (#45), smoke-test entry point (#55), holistic validation (#56), post-run meta learning (#58), resume from saved state (#59), dry-run (#60), env probe (#61), KB read/write (#62), attempt history (#63), truncation detection (#64), step spec propagation (#65), data-quality classification (#66), test-files collection (#67), LLM retry-decision (#68), and the rate-limit / usage-limit retry mechanism (#29) |
| `architect/planner.py` | 3,700 | ~20 mechanisms: multi-plan voting (#1), complexity gate (#2), enforced minimum steps (#3), step merging trivial+LLM (#4, #5), split coupled (#6), integration checkpoints (#7), TDD planning gate (#8), coverage matrix (#9), fill gaps (#10), ensure goal coverage (#11), step enrichment (#12), goal expansion (#17), research phase (#18), project spec (#19), generate reflection (#20), classify error heuristic (#21), counterfactual root-cause (#25), reflect-and-rewrite (#27), decompose failing step (#28), replan remaining steps (#44), corrective steps (#57) |
| `architect/executor.py` | 1,091 | Per-step orchestrator-subprocess driver (supports the entire failure-handling pipeline) |
| `architect/dashboard.py` | 752 | TUI dashboard layer (consumed by `screenshot.png` README image) |
| `architect/explain.py` | 677 | Run-explanation post-processor |
| `architect/state.py` | 566 | `.uas_state/runs/<run_id>/` machinery, structured progress file (#16), scratchpad |
| `architect/git_state.py` | 340 | Git checkpoint / rollback substrate (#30, #31) |
| `architect/trace_export.py` | 331 | Perfetto trace export |
| `architect/report.py` | 276 | HTML run report |
| `architect/provenance.py` | 204 | Architect-internal provenance graph (distinct from `integration/provenance.py` keep-list helpers) |
| `architect/code_tracker.py` | 156 | Per-step diff tracking |
| `architect/events.py` | 138 | Event log (consumed by progress-file mechanism #16) |
| `architect/spec_generator.py` | 106 | Spec-rewrite payload composer |
| `architect/__main__.py` | 65 | CLI entrypoint |
| `architect/__init__.py` | 0 | Package marker |
| `architect/report_template.html` | (~?) | HTML template consumed by `architect/report.py` |

Subtotal: **15,649** lines. Architect mechanism mapping covers
catalog rows **#1–#45 except those listed under other clusters**
plus **#55–#68**. That is 47 of 68 catalog rows; rows #46–#54 live
in §4 (orchestrator legacy + uas/ tree).

#### Architect-importing tests (61)

All `tests/test_*.py` that contain `from architect` or
`import architect` per `grep -l 'from architect\|import architect' tests/test_*.py`.

```
tests/test_build_context.py
tests/test_cleanup_artifacts.py
tests/test_cli_optimization.py
tests/test_code_tracker.py
tests/test_commit_hygiene.py
tests/test_complexity_scaling.py
tests/test_context_engineering.py
tests/test_correction_loop.py
tests/test_coverage_matrix.py
tests/test_cross_imports.py
tests/test_dashboard.py
tests/test_dry_run.py
tests/test_events.py
tests/test_executor.py
tests/test_explain.py
tests/test_file_signatures.py
tests/test_fuzzy.py
tests/test_git_finalize.py
tests/test_git_repair.py
tests/test_git_state_integration.py
tests/test_git_state.py
tests/test_goal_expansion.py
tests/test_goal_file.py
tests/test_guardrails.py
tests/test_holistic_validation.py
tests/test_integration_checkpoints.py
tests/test_knowledge_base.py
tests/test_meta_learning.py
tests/test_module_api.py
tests/test_nested_duplication.py
tests/test_orphaned_modules.py
tests/test_output.py
tests/test_output_quality.py
tests/test_parallel.py
tests/test_pipeline_smoke.py
tests/test_planner.py
tests/test_planner_workspace_awareness.py
tests/test_progress.py
tests/test_project_manifest.py
tests/test_provenance.py
tests/test_reflexion.py
tests/test_replanning.py
tests/test_replan_protection.py
tests/test_report.py
tests/test_resume.py
tests/test_retry_budget.py
tests/test_rewrite.py
tests/test_section_integration.py
tests/test_smoke_test.py
tests/test_snapshot_cleanup.py
tests/test_spec_generator.py
tests/test_split_coupled.py
tests/test_state.py
tests/test_targeted_distill.py
tests/test_tdd_contract.py
tests/test_tdd_enforcement.py
tests/test_trace_export.py
tests/test_validation.py
tests/test_verification_loop.py
tests/test_voting.py
tests/test_workspace_validation.py
```

**Note on `tests/test_provenance.py`.** PLAN §1 step 2 enumerates
this file alongside `test_eval_*.py` and `test_orchestrator_*.py`
as KEEP. Reading the file shows it tests `architect.provenance`
(line 1: `"""Tests for architect.provenance module."""`, line 6:
`from architect.provenance import (...)`) — a CUT module — not
`integration.provenance` (the keep-list flavour). Under PLAN's
stated rule "KEEP only if its subject module is on the keep list",
this file is CUT in §3 alongside `architect/provenance.py`.
Leaving it KEEP would break pytest collection after `architect/`
deletes. The fallback path PLAN §2 step 4–5 expected this file to
exercise — `_hash_active_config` returning `"unavailable"` when
`uas_config.py` is missing — is actually exercised by
`tests/test_eval_metadata.py::TestHashActiveConfig` lines 126–134,
which accesses `ev._hash_active_config` via the
`integration.eval`-from-`integration.provenance` re-export
(`integration/eval.py:56–61`). PLAN §2's "the existing-fixture
coverage in `tests/test_provenance.py` exercises this path"
language should read "in `tests/test_eval_metadata.py`" when §2
executes; recording here so §2 picks up the correction.

#### Architect-importing test that also pulls in container plumbing (1)

```
tests/test_integration.py
```

Container-level smoke tests (Claude-CLI ping, architect
decomposition dry-run, orchestrator-only hello.txt). No `import
architect` line at module top, but every test invokes the
architect via the container's default `entrypoint.sh →
architect.main` dispatch path. After §3 cuts architect, the
container has no architect to dispatch to and these tests fail
end-to-end. Cut alongside §3 for that reason.

§3 test subtotal: **62 files**, **≈ 22,500 lines** (from `xargs wc -l`
across the architect-importing list plus `test_integration.py`;
exact figure folds into §3's Results subsection).

§3 grand total: 16 source + 62 tests = **78 files**.

### §4 owner — cut orchestrator legacy + `uas/` tree + their tests (19 files)

#### Source files (8)

| Path | Lines | Mechanism cluster |
|---|---|---|
| `orchestrator/main.py` | 2,051 | Best-of-N family (#46, #47, #48, #49), PyPI version resolution (#50), pre-flight LLM check (#51), code quality fuzzy (#52), retry-clean prompt (#53) |
| `orchestrator/llm_client.py` | 418 | LLM-call wrapper for the legacy orchestrator subprocess |
| `orchestrator/claude_config.py` | 246 | Claude-CLI configuration helpers (legacy) |
| `orchestrator/parser.py` | 220 | `UAS_RESULT` / output parsing |
| `uas/fuzzy.py` | 155 | Fuzzy-function system (supports #51, #52) |
| `uas/janitor.py` | 114 | Context janitor (#54) |
| `uas/fuzzy_models.py` | 65 | Fuzzy-function dataclasses |
| `uas/__init__.py` | 0 | Package marker |

Subtotal: **3,269** lines. Catalog mechanism mapping covers rows
**#46–#54** (best-of-N family + pre-flight + janitor cluster).

#### Cut-orchestrator / uas-importing tests (11)

Tests not importing architect, with subject in `orchestrator/main.py`,
`orchestrator/llm_client.py`, `orchestrator/parser.py`,
`orchestrator/claude_config.py`, `uas/fuzzy*.py`, or `uas/janitor.py`.

```
tests/test_best_of_n.py
tests/test_claude_config.py
tests/test_framework_layout.py
tests/test_janitor.py
tests/test_llm_client.py
tests/test_llm_isolation.py
tests/test_orchestrator_main.py
tests/test_parser.py
tests/test_pre_flight.py
tests/test_version_resolution.py
tests/test_fuzzy.py — already accounted for in §3 (also imports architect); listed here for traceability only, do not double-count
```

Effective §4 test count: **10** (test_fuzzy.py is already in §3's
delete batch via the architect-import filter; the §4 step that
runs `git rm tests/test_fuzzy.py` becomes a no-op if §3 already
removed it. PLAN §4 step 3's enumeration is therefore safe to
interpret as "the listed tests, skipping any §3 already removed").

`tests/test_framework_layout.py` is structural — it has no Python
imports from any cut module but its rationale (a structural
defense against same-name top-level shadowing of `uas_*`-prefixed
modules) dies once §6 cuts the trio. Cut in §4 to keep it bundled
with the orchestrator/uas-residue layer.

§4 grand total: 8 source + 10 unique tests = **18 files**, plus 1
already-counted (test_fuzzy.py) = **19** entries in the §4 delete
batch.

### §5 owner — `integration/llm_judge.py` + dispatch sites + ML-class quality gate (4 files + eval.py surgery)

| Path | Lines | Note |
|---|---|---|
| `integration/llm_judge.py` | 470 | LLM-as-judge module — Claude Code sub-agents fill this role natively in the post-pivot framing. Single import site outside this file is `integration/eval.py:485` (lazy import inside `_import_llm_judge`); `orchestrator/pricing.py:11` mentions it only in a docstring |
| `tests/test_llm_judge.py` | (~?) | Subject-on-cut-list. Cut alongside |
| `integration/test_project_quality.py` | 245 | ML-project-class quality gate (`TestModelQuality`, `TestNoDataLeakage`, `TestFeatureDataQuality`, `TestSubgroupAnalysis`, `TestNoHardcodedPaths`, `TestDashboardImport`). Skipped unless `PROJECT_WORKSPACE` env is set; not on keep list, not in keeper substrate. Cut in §5 alongside `llm_judge.py` per `cut_surface.md` table |
| `integration/quick_test.sh` | 113 | Smoke script that runs the architect via the container. Functionally cut once §3 deletes architect's dispatch path. Cut in §5 alongside the integration/ cleanup |

Plus `integration/eval.py` surgery (~460 lines removed in place,
file stays):

- `invoke_architect` body (eval.py L459–574) — entire architect
  subprocess invocation path
- `_import_llm_judge` helper (L685–719)
- `llm_judge` dispatch branch in `run_check` (within L720–1109)
- the architect-coupled portions of `main()` (L1361–1560 per
  `cut_surface.md`)

§5 grand total: **4 file deletions + 1 file surgery**.

### §6 owner — root-level cleanup + trio + `tools/` (11 files)

#### DEFER trio (3) — resolution per PLAN §2

```
uas_config.py
uas_hooks.py
uas.example.toml
```

(See "DEFER → CUT under §6" section above for line counts and
importer audit.)

#### Root-level scripts (5)

| Path | Lines | PLAN §6.3 verdict | Rationale |
|---|---|---|---|
| `install.sh` | 132 | CUT | No users; project is personal. The wrapper script it generates and the engine-image build path it kicks off are obsolete (`integration/eval.py:_ensure_image` already does lazy image build) |
| `run_local.sh` | 29 | CUT | Calls `python3 -P -m architect.main` — fully architect-coupled |
| `run_container.sh` | 62 | CUT | Runs `uas-engine:latest` as the architect entrypoint. Architect-coupled |
| `start_orchestrator.sh` | 87 | CUT | Builds the engine image and runs the architect via `entrypoint.sh`. Misnamed — has nothing to do with the new orchestrator |
| `entrypoint.sh` | 81 | CUT | Container ENTRYPOINT script. Every code path leads to `architect.main` or `architect.state`. Functionally CUT once §3 deletes architect; the `Containerfile`'s `ENTRYPOINT` line referring to it is removed during §6 Containerfile surgery |

PLAN §6.3 also lists `framework_settings.json` for review — KEEP
verdict above (consumed by `setup_auth.sh`, which is on the
substrate keep-list).

#### Hook-consuming test (1)

```
tests/test_hooks.py
```

Subject = `uas_hooks.py` (DEFER → CUT under §6). Cut alongside.

#### Static asset (1)

| Path | Notes |
|---|---|
| `screenshot.png` | 266 KB PNG, 1920×1168. Referenced only by `README.md` line 492 (`![Terminal Dashboard](screenshot.png)`). The README is rewritten from scratch in §8 and the post-prune system has no TUI dashboard (`architect/dashboard.py` deletes in §3). CUT |

#### Tool script (1)

| Path | Lines | Notes |
|---|---|---|
| `tools/statusline_probe.sh` | 27 | Phase 2 §1 statusline probe. Not in the ROADMAP §Phase 4 keep-list enumeration; PLAN §1's intro states "[the keep list] is finalised at §1 of this phase as a closed set." Default-cut applies. **Consequence:** `docs/substrate.md` component 8's "TUI companion read pattern via `tools/statusline_probe.sh`" path retires alongside this script. The natural-trigger capture plan for the §2 percentage-cap question (logged in `docs/substrate.md` § Open questions) loses its capture surface; the question stays open until a future phase reintroduces a probe surface or accepts the dual-behaviour design constraint indefinitely. `docs/substrate.md` is amended in §6 / §8 to reflect this |

§6 grand total: **11 files**.

## Bucket sizes

| § | Files cut | Source vs test split | Approximate lines (text) |
|---|---|---|---|
| §3 | 78 | 16 source + 62 tests | ≈ 38,200 |
| §4 | 18 | 8 source + 10 tests | ≈ 5,200 |
| §5 | 4 (+ in-place surgery on `eval.py`) | 2 Python source + 1 shell + 1 test | ≈ 1,650 (deletions only) + ≈ 460 (surgery) |
| §6 | 11 | 3 trio + 5 root scripts + 1 hooks test + 1 tool script + 1 PNG (~260 KB binary) | ≈ 1,254 (text) + 1 PNG |
| **Total** | **111 file deletions + 1 in-place surgery** | 27 Python source + 1 HTML template + 7 shell scripts + 1 TOML + 74 tests + 1 PNG | **≈ 46,254 (deletions, text) + ≈ 460 (surgery)** |

The line totals do not include the binary `screenshot.png` or
the surgery on `integration/eval.py`'s body. Every file in the
table appears exactly once across all four buckets.

## Deletion order (within each section, leaves before roots)

### §3 — `architect/` tree

`architect/__init__.py` re-exports nothing. Internal cross-imports
mean a `git rm -r architect/` is one atomic operation; subordering
within `architect/` is unnecessary. `tests/conftest.py`'s
`tmp_workspace` fixture imports `architect.main` and
`architect.state` lazily inside the function body, so the import
graph survives the directory deletion.

Order:

1. `git rm -r architect/` (16 files in one go)
2. `git rm tests/test_*.py` for every architect-importing test
   (61 files) and `tests/test_integration.py` (1 file)
3. Run `python3 -m pytest tests/ -q` and confirm green on the
   surviving suite
4. Run `./uas-eval` and confirm hello-file plumbing still emits a
   well-formed JSONL row (PLAN §3 step 4)

After §3:

- `tests/conftest.py:21–39` (`tmp_workspace` fixture) is dead but
  importable. Per PLAN's "no test mutation to make it pass after
  the cut" rule, it stays as-is until a future cleanup
- `tests/conftest.py:88–96` (`_latest_source_mtime` glob over
  `architect/*.py`) loses its glob target. Returns mtime over the
  surviving patterns (`Containerfile`, `requirements.txt`,
  `entrypoint.sh`, `orchestrator/*.py`). Functional, less
  defensive

### §4 — cut orchestrator legacy + `uas/`

`orchestrator/main.py` imports `uas.fuzzy`, `uas.fuzzy_models`,
and `uas.janitor`. `orchestrator/llm_client.py` imports
`uas.fuzzy`, `uas.fuzzy_models`. Cutting `uas/` first is safe iff
the `orchestrator/` legacy files are cut in the same operation
(they will not be imported anywhere after architect+legacy delete).

Order:

1. `git rm orchestrator/main.py orchestrator/llm_client.py
   orchestrator/parser.py orchestrator/claude_config.py`
2. `git rm -r uas/`
3. `git rm` the 10 cut-orchestrator/uas-importing tests (test_fuzzy
   already removed in §3; skip)
4. Run `python3 -m pytest tests/ -q` and confirm green
5. Run `./uas-orchestrate start synthetic-multistep
   --simulate-rate-status allowed --state-root /tmp/<unique>` and
   confirm the §8 cycle still works

### §5 — `integration/llm_judge.py` + eval.py surgery

`integration/eval.py:485` (`from integration.llm_judge import
judge as judge_fn`) is a lazy import inside `_import_llm_judge`.
Removing the helper, removing every dispatch site that calls it,
and removing `integration/llm_judge.py` is one logical operation.
The deterministic check types (`file_exists`, `file_contains`,
`pytest_pass`, `exit_code`, `file_shape`, `command_succeeds`,
`glob_exists`) and the `run_check` dispatcher around them are KEEP.

Order:

1. Read `integration/eval.py` end-to-end (PLAN §5 step 1)
2. Edit out `_import_llm_judge`, the `llm_judge` dispatch branch
   in `run_check`, the `invoke_architect` architect-coupled body,
   and the architect-coupled portions of `main()`
3. `git rm integration/llm_judge.py
   integration/test_project_quality.py integration/quick_test.sh
   tests/test_llm_judge.py`
4. Confirm `integration/cases/` contains only the hello-file
   fixture (per PLAN §5 step 4 — already true at PLAN-author commit)
5. Run `python3 -m pytest tests/test_eval_*.py
   tests/test_orchestrator_*.py -q` and `./uas-eval`

### §6 — root-level cleanup + trio + tools

The trio (`uas_config.py`, `uas_hooks.py`, `uas.example.toml`) is
imported only from §3 / §4 cut buckets per PLAN §2 audit. After
§3+§4+§5 land, no surviving Python file imports them, so deletion
is one atomic step. `Containerfile` surgery removes the
references it carries to the trio plus `architect/`, `uas/`, and
`entrypoint.sh`.

Order:

1. `git rm uas_config.py uas_hooks.py uas.example.toml`
2. `git rm tests/test_hooks.py`
3. `git rm install.sh run_local.sh run_container.sh
   start_orchestrator.sh entrypoint.sh`
4. `git rm tests/test_framework_layout.py` (if not already removed
   in §4)
5. `git rm screenshot.png`
6. `git rm tools/statusline_probe.sh`
7. **Edit** `Containerfile`: remove `COPY uas_config.py .`,
   `COPY uas_hooks.py .`, `COPY architect/ ./architect/`,
   `COPY uas/ ./uas/`, `COPY entrypoint.sh .`,
   `RUN chmod +x entrypoint.sh`, the `ENV IS_SANDBOX=1` /
   `ENV UAS_SANDBOX_MODE=local` lines (orchestrator workers set
   these themselves), and `ENTRYPOINT
   ["/uas/entrypoint.sh"]` (no kept dispatch target — the image
   becomes a base for `claude --print` workers, not a
   self-running script)
8. **Edit** `setup_auth.sh:34`: drop or rewrite the `Run
   install.sh first:` error message that points to the deleted
   script (suggestion: redirect to "build via `./uas-eval` or
   manually `podman build -t uas-engine:latest -f Containerfile
   .`")
9. **Edit** `docs/substrate.md` § Open questions and § Component
   8 to drop the `tools/statusline_probe.sh`-anchored
   natural-trigger capture path; record the percentage-cap
   question as deferred-without-capture-surface
10. Run `python3 -m pytest tests/ -q` and confirm green
11. §6 acceptance: surviving root-level file list matches the
    KEEP § of this document exactly (`README.md`, `ROADMAP.md`,
    `PLAN.md`, `CLAUDE.md`, `phase0_audit.md`, `LICENSE`,
    `Containerfile`, `pytest.ini`, `requirements.txt`,
    `.gitignore`, `.containerignore`, `setup_auth.sh`,
    `framework_settings.json`, `uas-eval`, `uas-orchestrate`)

## Mechanism reconciliation (Phase 0 catalog → cut files)

All 68 catalog rows from `phase0_audit.md` § 1 map to one or
more files in the §3 / §4 cut buckets. Zero map to KEEP files.
Aggregated by `phase0_audit.md` § 4 strongly-coupled cluster:

| Cluster | Catalog rows | Owning files | § |
|---|---|---|---|
| A. Reflection / retry decision | 11 (#20, #21, #22, #23, #27, #28, #29, #53, #64, #66, #68) | `architect/main.py`, `architect/planner.py`, `orchestrator/main.py` (#53) | §3 + §4 |
| B. Counterfactual + backtrack | 2 (#25, #26) | `architect/planner.py`, `architect/main.py` | §3 |
| C. Best-of-N family | 4 (#46, #47, #48, #49) | `orchestrator/main.py` | §4 |
| D. Validation cascade | 7 (#39, #40, #41, #42, #55, #56, #57) | `architect/main.py`, `architect/planner.py` (#57) | §3 |
| E. Coverage-driven planning + replan | 6 (#9, #10, #11, #44, #45, #57) | `architect/planner.py`, `architect/main.py` (#45) | §3 |
| F. TDD pair | 3 (#8, #43, #67) | `architect/planner.py`, `architect/main.py` | §3 |
| G. Git state pair | 2 (#30, #31) | `architect/main.py`, `architect/git_state.py` | §3 |
| H. Step DAG transforms | 4 (#4, #5, #6, #7) | `architect/planner.py` | §3 |
| I. Persistence + recovery | 3 (#16, #59, #63) | `architect/state.py`, `architect/main.py` | §3 |
| J. Post-run learning loop | 3 (#20, #58, #62) | `architect/planner.py`, `architect/main.py` | §3 |
| Unclustered / borderline | 25 (planning preludes #1–#3, per-step #12–#15 / #17–#19, validation atoms #32–#38, orchestrator pre-flight #50–#52, mode/probe/janitor/propagation #24, #54, #60, #61, #65) | `architect/planner.py`, `architect/main.py`, `orchestrator/main.py` (#50, #51, #52), `uas/janitor.py` (#54) | §3 + §4 |

Unique cluster members (A–J): 43; unclustered: 25; total: **68** ✓.

## §1 Results

**Acceptance.**

- ✅ `docs/cut_list.md` exists.
- ✅ Every tracked file in the repo (164 entries from `git
  ls-files`) appears exactly once under KEEP, CUT, or DEFER.
- ✅ Every Phase 0 catalog mechanism (68 entries) has at least one
  CUT-side or KEEP-side file mapped to it (in this case all 68 →
  CUT files; KEEP holds zero catalog mechanisms per `cut_surface.md`).
- ✅ DEFER bucket contains exactly three entries: `uas_config.py`,
  `uas_hooks.py`, `uas.example.toml`.
- ✅ §3 / §4 / §5 / §6 each have a non-empty CUT bucket whose
  contents match the §-by-§ deletion plan in PLAN.md (§3: 78
  files; §4: 18 unique files plus 1 already-removed crossover;
  §5: 4 deletions + in-place surgery; §6: 11 files).

**Counts.**

| Metric | Value |
|---|---|
| Total tracked files | 164 |
| KEEP count | 53 |
| DEFER count (resolves CUT under §6) | 3 |
| CUT count | 108 |
| Expected shrinkage (sum of `wc -l` over CUT + DEFER files, text only) | ≈ 46,764 lines |
| `integration/eval.py` in-place surgery (§5) | ≈ 460 lines (estimate per `cut_surface.md`) |
| `screenshot.png` binary | 1 file, ~266 KB |
| Total expected text-line shrinkage at §7 measurement | ≈ 47,224 lines |

**Deviations from PLAN to flag for §2 / §6 review.**

1. `tests/test_provenance.py` — PLAN §1 step 2 lists as KEEP, but
   the file imports from `architect.provenance` (CUT). Reclassified
   CUT under §3 owner per PLAN's "subject on keep list" rule.
   PLAN §2 step 4–5's reference to "the existing-fixture coverage
   in `tests/test_provenance.py`" should read
   "`tests/test_eval_metadata.py::TestHashActiveConfig`" when §2
   executes; that test (lines 126–134) is what actually exercises
   `_hash_active_config`'s `"unavailable"` fallback via the
   `integration.eval`-from-`integration.provenance` re-export.
2. `tools/statusline_probe.sh` — not in any keep-list enumeration.
   Default-cut applies; classified CUT under §6 owner. Consequence:
   `docs/substrate.md` § Open questions loses its natural-trigger
   capture surface for the percentage-cap question, and component
   8's TUI companion read pattern retires. §6 surgery on
   `docs/substrate.md` records the change.
3. `setup_auth.sh:34` — references `install.sh` (CUT). §6 surgery
   updates the error message to point users at the still-supported
   image build path (`./uas-eval` lazy build, or manual `podman build`).
4. `Containerfile` — KEEP with §6 surgery to drop cut-path COPYs
   and the architect-only `ENTRYPOINT` line.
5. `tests/conftest.py` — KEEP with two dead-but-harmless residues
   after §3 (the `tmp_workspace` fixture imports architect lazily;
   `_latest_source_mtime` globs `architect/*.py`). PLAN's "no test
   mutation" rule keeps both as-is.

**Status:** ready for §1 close; PLAN.md §1 to be marked completed
in the same commit that adds this file.
