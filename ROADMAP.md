# UAS Roadmap

## Origin

UAS is a personal research harness built by @eturkes for exploring the
frontier of autonomous agent scaffolding on long-horizon, complex
tasks. Stated goal (from project owner): "the best harness
conceivable, at any cost and time expenditure" — optimizing for
reliability, auditability, provenance, and traceability, not for cost,
latency, or broad-audience usability.

It is explicitly NOT a product. There are no users to support, no
shipping deadline, no compromise budget. The only real constraint is
whether mechanisms demonstrably improve task-completion reliability.

## Why this roadmap exists

As of commit `597f9ba`, the project had 363 commits in ~3 months,
nearly all driven by post-hoc analysis of failed real-world runs.
Each cycle added a new self-correction mechanism: Reflexion,
counterfactual root-cause tracing, informed backtracking, verification
stagnation detection, best-of-N, multi-plan voting, tiered context
compression, context janitor, TDD gate, `retry_clean`, mid-execution
re-planning, guardrails, and more. The README lists ~25 distinct
mechanisms. The development loop had come to feel endless.

**Root-cause diagnosis.** Development was anecdote-driven. A single
failed run cannot distinguish a real failure pattern from a noise
artifact of that particular LLM sample. Every anecdote was promoted
to a mechanism, nothing was ever removed, and the scaffold's coupling
surface grew faster than its reliability. Without an instrument to
measure which mechanisms actually moved pass-rate, "best practices"
was indistinguishable from cargo cult.

This roadmap reframes the work around **measurement-first iteration**.
The instrument comes before the mechanisms it judges.

## Core thesis

**1. Scaffold ceiling.** The harness cannot exceed the underlying
model's capability on any given task. Every correction mechanism
trades coupling for reliability. Past a certain point, the trade is
net-negative: new mechanisms interact with existing ones and introduce
more failure modes than they prevent. The ceiling is set by the
frontier model, plus whatever marginal gains verification and sampling
buy you, minus whatever your scaffold's coupling costs you.

**2. Measurement bottleneck.** "Best possible" is not a verifiable
state without an eval that can detect regressions with known noise
bounds. Without measurement, improvements are indistinguishable from
noise, and there is no way to know which mechanisms are earning their
keep. The highest-leverage work in the project right now is building
the instrument.

**Corollary.** Until an eval harness with noise bounds exists, no new
self-correction mechanism should be added, and no existing one should
be removed. Observability, safety, structural refactors, and the eval
harness itself are the only appropriate work.

## Non-negotiable principles

1. No new mechanism without an eval-visible win.
2. Every mechanism must be ablatable.
3. Deletion is as valuable as addition.
4. Strong verification over strong correction.
5. The scaffold cannot exceed the model.
6. Measure before you change.

See `CLAUDE.md` for the session-level restatement.

## Current phase

**Phase 1 — Eval harness hardening** (active)

Phase 0 closed at commit `496d76f`-descendant. Its findings populate
the "Current state of the codebase" section below. The Phase 1 PLAN
is pending — draft it before executing any Phase 1 work, and pause
for user review before starting Section 1 (per the decision protocol
in `CLAUDE.md`).

## Phase plan

| # | Phase | Status | One-line goal |
|---|---|---|---|
| 0 | Audit | completed | Catalog mechanisms, eval infra, flags, dependencies. No code changes. |
| 1 | Eval harness hardening | **active** | Turn eval.py into canonical measurement tool. Curated benchmark. Deterministic + LLM-judge grading. Persistent results with noise bounds. |
| 2 | Baseline measurement | pending | Run harness 3× on main. Record numbers. Establish regression gate. |
| 3 | Ablation flags | pending | Put every mechanism behind a toggleable flag with documented dependencies. |
| 4 | Ablation study | pending | Measure marginal contribution of each mechanism. Produce keep/delete/investigate table. |
| 5 | Prune | pending | Delete mechanisms whose marginal contribution is zero or negative. |
| 6+ | Informed iteration | pending | Add new mechanisms responsibly, each gated on eval improvement. |

## Phase details

### Phase 0 — Audit

**Goal:** understand the current state of UAS without modifying code.
Produce a grounded "Current state of the codebase" section in this
roadmap so Phase 1 design is based on reality rather than README prose.

**Deliverables:**
- Catalog of every self-correction / retry / rewrite / guardrail
  mechanism in the tree (name, file, trigger, effect, existing flag
  if any).
- Assessment of `integration/eval.py` and `prompts.json`: what they
  measure, concrete gaps vs Phase 1 requirements.
- Inventory of every `UAS_*` env var, config key, and feature flag.
- Dependency map: mechanism X reads state written by mechanism Y.
- Summary appended to "Current state of the codebase" below.

**Exit criteria:** the "Current state of the codebase" section in this
roadmap is populated. Phase 0 PLAN is removed.

### Phase 1 — Eval harness hardening

**Goal:** build the measurement instrument. Everything downstream
depends on this working correctly and with known noise bounds.

**Deliverables:**
- Canonical `uas-eval` entry point. Likely extends existing
  `integration/eval.py`; may require significant rewrite.
- Curated benchmark set: 30–50 tasks, tiered by complexity
  (trivial / moderate / hard / open-ended). Mostly designed fresh —
  the existing 4 cases in `prompts.json` are trivial and won't
  stress the self-correction machinery.
- Hybrid grading:
  - Deterministic where possible: exit codes, file existence,
    content regex, pytest pass, file-shape checks.
  - LLM-as-judge with N=5 samples and majority vote for open-ended
    tasks. Cost is explicitly not a constraint.
- Persistent results log: append-only JSONL at
  `integration/eval_results.jsonl`. Each row carries timestamp, git
  SHA, per-task outcome, and all metrics.
- Per-task metrics: pass/fail, wall time, LLM time, sandbox time,
  attempt count, token count (input + output), step count, final
  workspace size.
- Multi-run variance: run the full benchmark 3× per measurement,
  report mean ± stdev per metric and per tier.
- Tiered reporting: separate pass-rate per complexity tier.
- Reproducibility: capture git SHA, relevant env vars, and a hash
  of active config at run start.

**Exit criteria:** `uas-eval` runs end-to-end, produces deterministic
pass/fail plus noise bounds on the full benchmark, and appends to the
persistent log. Running it twice on the same commit must produce
statistically indistinguishable results.

### Phase 2 — Baseline measurement

**Goal:** record current system performance with every existing
mechanism enabled.

**Deliverables:**
- 3 full benchmark runs on current `main`.
- Per-tier mean pass rate ± stdev recorded in "Baseline metrics"
  below.
- Per-metric means recorded (LLM time, sandbox time, attempts,
  tokens per task).
- Regression gate definition: any subsequent change that drops mean
  pass rate by more than 1 stdev without a compensating gain
  elsewhere is a regression until investigated.

**Exit criteria:** "Baseline metrics" section below is populated with
real numbers. A `baseline` git tag is placed at the measured commit.

### Phase 3 — Ablation flags

**Goal:** make every mechanism from the Phase 0 catalog individually
toggleable without rewriting the call sites.

**Deliverables:**
- One `UAS_DISABLE_<MECHANISM>` env var (or equivalent config key)
  per mechanism.
- Default behavior preserved (all mechanisms on).
- Documented dependencies between flags. Example: disabling
  `reflection_history` should auto-disable `counterfactual_tracing`
  because the latter consumes the former's state.
- Regression check: benchmark with no flags set must match Phase 2
  baseline within noise bounds.

**Exit criteria:** can run the benchmark with any subset of mechanisms
disabled. Default-all-enabled matches baseline.

### Phase 4 — Ablation study

**Goal:** measure marginal contribution of each mechanism.

**Deliverables:**
- Single-ablation runs: for each mechanism M, run benchmark with M
  disabled, record Δ vs baseline.
- Key pair ablations: for mechanisms hypothesized to interact (e.g.
  reflection + counterfactual, best-of-N + multi-plan voting), run
  with both disabled.
- Results table: mechanism → Δ pass rate (mean, stdev) → Δ wall time
  → Δ tokens → verdict (keep / delete / investigate).

**Exit criteria:** every mechanism in the Phase 0 catalog has a
verdict backed by numbers.

### Phase 5 — Prune

**Goal:** remove mechanisms that data doesn't support.

**Deliverables:**
- Delete code for every "delete" verdict from Phase 4.
- Shrink README to match reality.
- Re-run full benchmark, confirm no regression vs Phase 2.
- Update "Baseline metrics" below with post-prune numbers.

**Exit criteria:** codebase is smaller; benchmark pass rate unchanged
or improved.

### Phase 6+ — Informed iteration

**Goal:** add new capabilities responsibly.

**Standing rules** (enforced indefinitely, not just during Phase 6):

1. Any new mechanism runs a before/after benchmark and demonstrates
   a mean improvement greater than 1 stdev of the baseline noise.
2. Any new mechanism ships with its own ablation flag from day one.
3. New mechanisms are added to the Phase 0 catalog and the ablation
   study immediately.

**Candidate directions** (not commitments — explore once Phase 5 is
done and only if supported by evidence):

- Aggressive test-time compute (best-of-16+ on hard steps, o1-style
  deliberation budgets).
- Adversarial generator/critic setups with distinct LLM roles.
- Strategic human checkpoints at decomposition time (minimal human
  input ≠ zero human input).
- Verification strength: property-based tests, runtime invariants,
  formal specs where feasible.
- Task-class scoping — bounded reliability guarantees on a defined
  class of tasks beats vague ambition on an open set.

## Current state of the codebase

*(Populated by Phase 0 audit. Detailed mechanism table, eval-gap
analysis, flag inventory, and dependency adjacency lists are recorded
in the Phase 0 audit's `PLAN.md` history; the summary below is the
distillation Phase 1+ should read at session start.)*

UAS at the close of Phase 0 (363 commits, ~3 months) contains
**68 distinct mechanisms** across the architect / orchestrator / uas
tree, fitting the broad definition "any code whose removal would
measurably change behavior on a non-trivial task". The bulk live in
`architect/main.py` (6864 lines, the failure-handling and validation
core) and `architect/planner.py` (3700 lines, the decomposition and
replanning core); a smaller cluster lives in `orchestrator/main.py`
(2051 lines, best-of-N and pre-flight). The README's headline
"~25 mechanisms" undercounts by roughly 2.7×. **26 mechanisms exist
in code but not in README** (mostly LLM-driven failure-classification
helpers and orchestrator-internal best-of-N machinery), and
**3 README-listed concepts** (goal expansion, environment probe,
cross-run knowledge base) exist as inlined logic without distinct
call sites.

The eval infrastructure (`integration/eval.py`, 358 lines, plus 4
trivial prompt cases in `prompts.json`) is a **smoke test, not a
benchmark**. None of the 4 cases will exercise any of the 68
mechanisms catalogued above under normal LLM behavior, and one is
inherently flaky (depends on the open-notify.org live API). The
runner overwrites results on every run, captures only `elapsed` per
case, has no LLM-as-judge, no multi-run averaging, no tier system,
and no git-SHA / env capture for reproducibility. **9 distinct gaps**
exist between current state and Phase 1's hardened-eval requirements.
The runner code itself is sound; Phase 1 should **extend, not
replace** it. A separate ML-project-specific quality gate suite
(`integration/test_project_quality.py`, 245 lines) is a useful
template for Phase 1's deterministic check layer, scoped to one
project class.

The configuration surface is **53 distinct knobs** spread across a
layered loader (`uas_config.py`, defaults → user TOML → project TOML
→ env vars), `uas.example.toml`, the README env-var table, and
direct `os.environ` reads in hot-path modules. Documentation drift
is significant: 11 keys exist in the loader without README or TOML
entries (notably `tdd_enforce`, `fuzzy_enabled`,
`context_janitor.formatter`); 4 env vars are read directly by
`orchestrator/sandbox.py` without going through the loader at all
(`UAS_SANDBOX_IMAGE`, `UAS_SANDBOX_TIMEOUT`, `UAS_HOST_UID`,
`UAS_HOST_GID`); 3 are fully undocumented (`UAS_PROJECT_NAME`,
`UAS_FUZZY_MODEL`, `UAS_STEP_CONTEXT`); and 2 (`UAS_MAX_ERROR_LENGTH`,
`UAS_MINIMAL`) are read directly by `architect/planner.py` despite
having loader entries, silently bypassing TOML overrides.

Ablation-flag coverage of the 68 mechanisms is **5/68 ≈ 7% strict**
(clean single-mechanism flags) and **13/68 ≈ 19% loose** (clean flags
plus what `UAS_MINIMAL` bundles together). The remaining
**49/68 ≈ 72%** of mechanisms have no ablation control whatsoever.
The dominant existing toggle, `UAS_MINIMAL`, is exactly the wrong
shape for measurement: it bundles ~8 unrelated mechanisms behind one
switch, which means flipping it cannot attribute any per-mechanism
delta. Phase 3's first concrete deliverable should be replacing
`UAS_MINIMAL` with one flag per disable target it currently controls.

The mechanism dependency graph contains **10 strongly-coupled
clusters**: **7 essential** (cannot be decoupled without scaffolding
rewrite — the reflection-decision pipeline; counterfactual+backtrack;
validation cascade; coverage-driven planning; git checkpoint+rollback;
persistence+resume; cross-run learning loop) and **3 incidental**
(cleanly bundled but currently un-decomposed — best-of-N family,
TDD pair, step-DAG transform pipeline). The single largest essential
cluster is the reflection / retry-decision pipeline, in which 11
mechanisms all consume `reflection.error_type` or `reflection_history`
written by the "Generate reflection" mechanism. Disabling that one
mechanism alone would silently degrade 10 others with no error
raised. This cluster must be ablated as a group, not individually,
in Phase 4.

**Implications for Phase 1.** The eval harness needs to author a
fresh case set (the existing 4 cases cannot stress the
self-correction machinery), extend `eval.py` rather than replace it,
add tiered reporting + multi-run variance + LLM-as-judge + git-SHA
capture + per-task metrics beyond `elapsed`, and persist results to
an append-only JSONL log. Most of the surface area is additive — the
container/auth/workspace/case-loop scaffolding in `eval.py` is sound.

**Implications for Phase 3.** Roughly 50 new ablation flags need to
be created from scratch. ~5 existing flags can be reused as-is. ~8
mechanisms currently bundled inside `UAS_MINIMAL` need to be
decomposed into individual flags before Phase 4 single-flag ablation
results can be interpreted. The two `architect/planner.py` constants
that bypass `config.get()` need to be re-routed through the loader,
or any new flag they're meant to control will have no effect.

**Phase 0 summary stats:**

| Stat | Value |
|---|---|
| Total mechanism count | 68 |
| Code-without-README mechanisms | 26 |
| README-without-code mechanisms | 3 |
| Eval infrastructure gap count (vs Phase 1 requirements) | 9 |
| Ablation-flag coverage (strict, single mechanism) | 5 / 68 ≈ 7% |
| Ablation-flag coverage (loose, including `UAS_MINIMAL` bundle) | 13 / 68 ≈ 19% |
| Strongly-coupled clusters (essential + incidental) | 10 (7 + 3) |
| Phase 4 group ablations required | 11 |

## Baseline metrics

*(Populated during Phase 2. Format:)*

| Metric | Mean | Stdev | N |
|---|---|---|---|
| Overall pass rate | — | — | — |
| Trivial tier pass rate | — | — | — |
| Moderate tier pass rate | — | — | — |
| Hard tier pass rate | — | — | — |
| Open-ended tier pass rate | — | — | — |
| Wall time (s, per task avg) | — | — | — |
| LLM time (s, per task avg) | — | — | — |
| Sandbox time (s, per task avg) | — | — | — |
| Attempts (per task avg) | — | — | — |
| Input tokens (per task avg) | — | — | — |
| Output tokens (per task avg) | — | — | — |

## Completed phases

### Phase 0 — Audit

Closed as part of the same commit that populated "Current state of
the codebase" above. Deliverables completed: 68-row mechanism
catalog, 9-gap eval infrastructure assessment, 53-knob feature-flag
inventory with discrepancy classification, 10-cluster dependency
adjacency list with 11 required Phase 4 group ablations. No code
changes — read-only audit. The summary above is the headline-level
distillation; the full row-by-row tables live in
[`phase0_audit.md`](phase0_audit.md), which Phase 3 (ablation flags)
and Phase 4 (ablation study) read for the detail. The audit's
working file `PLAN.md` was removed on phase close per project
convention.

## Amending this roadmap

If a phase's scope or goals need to change, record the amendment in a
dedicated commit that touches only `ROADMAP.md`, with a subject like
`Amend ROADMAP: <reason>`. Do not retroactively rewrite completed-phase
entries — append a note to the relevant phase section instead.
