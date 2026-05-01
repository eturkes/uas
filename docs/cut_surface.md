# Phase 4 cut-surface preview (sized estimate)

This document is the Phase 2 §4 deliverable. It buckets the
architect / orchestrator / uas tree plus `integration/` and the
small root-level support files into KEEP / CUT /
NEEDS-PHASE-3-DECISION at file granularity and reports headline
line counts. **It is not a per-mechanism verdict pass** — that is
Phase 4's job. Phase 3 reads this so the orchestrator design
doesn't accidentally depend on cut surface.

Sources: Phase 0 audit (`phase0_audit.md`), ROADMAP §Phase 4 —
Prune (keep list + cut surface), `docs/substrate.md` (component
boundaries on the keep list), `wc -l` against the live tree.

## Headline

| Bucket | Mechanisms (of 68) | Files | Lines (approx) |
|---|---|---|---|
| **KEEP** | 0 | 4 + substrate slice of `integration/eval.py` | ≈ 1,400 |
| **CUT** | 68 | 26 + cut tail of `integration/eval.py` | ≈ 19,800 |
| **NEEDS-PHASE-3-DECISION** | 0 | 3 | ≈ 510 |

- Mechanism total reconciles exactly to the Phase 0 catalog (68).
- Architect + orchestrator + uas trees alone are 18,787 lines;
  the three big modules in the catalog
  (`architect/main.py` 6,915 + `architect/planner.py` 3,700 +
  `orchestrator/main.py` 2,051 = 12,666) match ROADMAP's "~13k
  lines" headline. Adding `integration/` (2,275) and the
  decision-bucket root files (510) brings in-scope to ≈ 21,650.
- **Zero mechanisms land on KEEP.** The Phase 4 keep list is
  Phase-1 substrate code (`orchestrator/sandbox.py`,
  `Containerfile`, `uas-eval`, `integration/eval.py` substrate
  helpers, the hello-file smoke case) — none of which is a §1
  catalog row. The 68-row catalog covers pre-Phase-1 architect /
  orchestrator / uas work; everything in it is delete-by-default
  per ROADMAP §Phase 4.

## KEEP (≈ 1,400 lines, 4 files + substrate slice of `integration/eval.py`)

Phase 4 keep list per ROADMAP §Phase 4, with line counts confirmed
against `docs/substrate.md`:

| Path | Lines | Substrate component |
|---|---|---|
| `orchestrator/sandbox.py` | 201 | #1 Sandbox primitive |
| `Containerfile` | 53 | #1 Sandbox primitive (engine image) |
| `uas-eval` | 13 | #7 Eval harness shell wrapper |
| `integration/cases/trivial/hello-file.json` | 11 | #7 Hello-file smoke case |
| `integration/eval.py` (substrate slice) | ≈ 1,100 | #2 OAuth refresh, #3 JSONL log, #4 Provenance, #5 Workspace iso., #6 Resume-from-JSONL, #7 `run_check` + harness loop |

The `eval.py` substrate slice is the union of components 2–7
from `docs/substrate.md`: OAuth helpers and constants
(L1–388), `load_prompts` + `setup_workspace` + `SetupFileMissing`
(L389–458), `run_checks` + `build_result` (L606–651), the
`run_check` deterministic-check dispatcher (L720–1109),
aggregation + report helpers (L1110–1272), and the engine
helpers `_find_engine` / `_ensure_image` (L1313–1360). Phase 4
performs the line-by-line split; this estimate suffices for
Phase 3 planning.

## CUT (≈ 19,800 lines, 26 files + cut tail of `integration/eval.py`)

Default verdict for the entire architect / orchestrator / uas
trees minus the sandbox primitive, plus the explicit
`integration/llm_judge.py` cut and the ML-class quality gate.
All 68 catalog mechanisms land here.

### `architect/` tree (15,317 lines, 14 files all CUT)

| File | Lines | Notes |
|---|---|---|
| `main.py` | 6,915 | Hosts ≈ 37 catalog mechanisms (failure handling, validation, retry decisions). |
| `planner.py` | 3,700 | Hosts ≈ 20 catalog mechanisms (decomposition, coverage, reflection generation, replan). |
| `executor.py` | 1,091 | Per-step orchestrator-subprocess driver. |
| `dashboard.py` | 752 | Run dashboard / live-view layer. |
| `explain.py` | 677 | Run-explanation post-processor. |
| `state.py` | 566 | `.uas_state/runs/<run_id>/` machinery; substrate doc explicitly lists nothing here as carrying forward. |
| `git_state.py` | 340 | Git checkpoint + rollback (mechanisms #30, #31). |
| `trace_export.py` | 331 | Perfetto trace export. |
| `report.py` | 276 | HTML run report. |
| `provenance.py` | 204 | Distinct from `eval.py`'s `capture_run_metadata`; architect-internal flavour. |
| `code_tracker.py` | 156 | Per-step diff tracking. |
| `events.py` | 138 | Event log (consumed by progress-file mechanism #16). |
| `spec_generator.py` | 106 | Spec-rewrite payload composer. |
| `__main__.py` | 65 | CLI entrypoint. |

### `orchestrator/` tree (2,935 of 3,136 lines CUT, 4 files CUT)

| File | Lines | Notes |
|---|---|---|
| `main.py` | 2,051 | Best-of-N family + pre-flight (mechanisms #46–#53). |
| `llm_client.py` | 418 | LLM-call wrapper for the orchestrator subprocess. |
| `claude_config.py` | 246 | Claude-CLI configuration helpers. |
| `parser.py` | 220 | Output / `UAS_RESULT` parsing. |

### `uas/` tree (334 lines, 3 files all CUT)

| File | Lines | Notes |
|---|---|---|
| `fuzzy.py` | 155 | Fuzzy-function system. |
| `janitor.py` | 114 | Context janitor (mechanism #54). |
| `fuzzy_models.py` | 65 | Fuzzy-function dataclasses. |

### `integration/` cut tail (≈ 1,175 lines, 2 files + cut tail of `eval.py`)

| File | Lines | Notes |
|---|---|---|
| `llm_judge.py` | 470 | Explicit cut per ROADMAP §Phase 4 — Claude Code sub-agents fill this role natively. |
| `test_project_quality.py` | 245 | ML-project-class quality gate; not on keep list, not in keeper substrate. |
| `eval.py` (cut tail) | ≈ 460 | `invoke_architect` (L459–574), `_import_llm_judge` (L685–719), llm_judge dispatch sites, the architect-coupled portions of `main()` (L1361–1560). Phase 4 surgery. |

### Mechanism distribution across the cut

All 68 catalog mechanisms are CUT. By Phase 0 §4 strongly-coupled
cluster:

| Cluster | Members | Verdict |
|---|---|---|
| A. Reflection / retry decision | #20, #21, #22, #23, #27, #28, #29, #53, #64, #66, #68 (11) | CUT |
| B. Counterfactual + backtrack | #25, #26 (2) | CUT |
| C. Best-of-N family | #46, #47, #48, #49 (4) | CUT |
| D. Validation cascade | #39, #40, #41, #42, #55, #56, #57 (7) | CUT |
| E. Coverage-driven planning + replan | #9, #10, #11, #44, #45, #57† (6, #57 dual-listed with D) | CUT |
| F. TDD pair | #8, #43, #67 (3) | CUT |
| G. Git state pair | #30, #31 (2) | CUT |
| H. Step DAG transforms | #4, #5, #6, #7 (4) | CUT |
| I. Persistence + recovery | #16, #59, #63 (3) | CUT |
| J. Post-run learning loop | #20†, #58, #62 (3, #20 dual-listed with A) | CUT |
| Unclustered / borderline | 25 mechanisms (planning preludes #1–#3, per-step processing #12–#15 / #17–#19, validation atoms #32–#38, orchestrator pre-flight #50–#52, mode/probe/janitor/propagation #24, #54, #60, #61, #65) | CUT |

Unique cluster members (A–J): 43. Unclustered: 25. Total: 68. ✓

The orchestrator's resume-across-restart needs are served by the
KEEP-list resume-from-JSONL substrate, not by the architect's
state-keyed resume (#59). Quota awareness is served by the
§1-resolved rate-limit read pattern, not by mechanism #29
(rate-limit / usage-limit retry). Verification needs are served
by Claude Code sub-agents and the deterministic check dispatcher
in `run_check`, not by the validation-cascade family. See
`docs/substrate.md` for the consumption shapes.

## NEEDS-PHASE-3-DECISION (≈ 510 lines, 3 files)

Items the ROADMAP either explicitly defers ("possibly-keep") or
that the orchestrator design will resolve at Phase 3 start.

| File | Lines | Question |
|---|---|---|
| `uas_config.py` | 216 | Possibly-keep per ROADMAP §Phase 4 ("scoped to keys the orchestrator actually reads"). The four-layer TOML / env loader is reusable; the question is which keys survive. |
| `uas_hooks.py` | 215 | Hook entry points wired into the architect's lifecycle. Phase 3 may want a similar shape for orchestrator policy hooks; verbatim reuse is unlikely. |
| `uas.example.toml` | 79 | Follows the `uas_config.py` decision. If the loader survives in slimmed form, the example file rewrites accordingly. |

The `integration/eval.py` keep / cut split point is also a
Phase 4 surgery question rather than a Phase 3 decision per se;
it appears in the KEEP and CUT tables above with approximate
line counts but the exact partition is not committed here.

## Out of scope for this preview

- **`tests/` tree (≈ 70 unit modules).** Most exercise CUT
  mechanisms with mocked LLM calls. They are not benchmark
  infrastructure (Phase 0 audit §2). Each module follows its
  primary mechanism's verdict at Phase 4 implementation time.
- **README rewrite.** ROADMAP §Phase 4 specifies a from-scratch
  rewrite, not a prune of existing text — not a CUT/KEEP item.
- **Loose root-level shell scripts** (`entrypoint.sh`,
  `install.sh`, `run_container.sh`, `run_local.sh`,
  `setup_auth.sh`, `start_orchestrator.sh`). Small (each <100
  lines), follow their primary code's verdict at Phase 4 time.
- **Loose root-level Python ad-hoc scripts**
  (`check_environment.py`, `run_test_verification.py`,
  `verify_test_goal.py`, `test_goal_verify.py`,
  `test_goal_result.py`, plus the stale `*.txt` outputs).
  Gitignored personal utilities per Phase 0 audit §2 — out of
  the prune scope entirely.
- **`tools/statusline_probe.sh`.** Phase 2 §1 probe; deletes at
  phase close per PLAN scope-discipline note unless absorbed
  into substrate.

## Reconciliation to Phase 0 catalog scale

| Source | Mechanisms | Lines |
|---|---|---|
| Phase 0 catalog (`phase0_audit.md` §1) | 68 | "~13k" headline (the three big modules sum to 12,666) |
| This preview, KEEP + CUT + DECISION (architect + orchestrator + uas only) | 68 (all CUT) | 18,787 (15,317 + 2,935 + 334 CUT + 201 KEEP) |
| This preview, full in-scope (incl. `integration/` + decision-bucket root files) | 68 | ≈ 21,650 (1,400 KEEP + 19,800 CUT + 510 DECISION) |

Mechanism count matches exactly. Line-count headline matches
ROADMAP's "~13k" at the three-big-modules level; the ≈ 21,650
figure is the full in-scope tree once helpers and `integration/`
are included. Phase 4's actual prune is expected to delete the
bulk of the 21k, retain the ≈ 1.4k substrate slice, and leave
the ≈ 510-line DECISION lines for Phase 3 to scope.

## Caveats

- **This is a sized estimate, not Phase 4's verdict pass.**
  Per-row decisions on the 68 mechanisms (whether any is
  salvageable into Phase 3 as substrate, single-flag vs group
  ablation handling for §1's residual evidence, etc.) live in
  Phase 4.
- **Line counts are `wc -l` against the live tree at Phase 2 §4
  authoring time.** They drift; ROADMAP's snapshot of
  `architect/main.py` was 6,864 lines and the live file is now
  6,915. Phase 4 should re-confirm any number it depends on
  before relying on it for a verdict.
- **The `integration/eval.py` keep / cut split is approximate.**
  The substrate.md mapping into eval.py line ranges sums to
  ≈ 1,100 lines KEEP and ≈ 460 lines CUT, but the exact byte
  positions of the split depend on Phase 4's choice of how to
  factor the file (in place, into `auth.py` / `provenance.py` /
  `runner.py` modules, or some other shape).
