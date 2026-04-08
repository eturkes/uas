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

**Phase 0 — Audit** (active)

See active `PLAN.md` for tactical sections. Phase 0 is a no-code-change
audit that grounds the rest of the plan in the actual current state of
the codebase.

## Phase plan

| # | Phase | Status | One-line goal |
|---|---|---|---|
| 0 | Audit | **active** | Catalog mechanisms, eval infra, flags, dependencies. No code changes. |
| 1 | Eval harness hardening | pending | Turn eval.py into canonical measurement tool. Curated benchmark. Deterministic + LLM-judge grading. Persistent results with noise bounds. |
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

*(Populated during Phase 0.)*

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

*(None yet.)*

## Amending this roadmap

If a phase's scope or goals need to change, record the amendment in a
dedicated commit that touches only `ROADMAP.md`, with a subject like
`Amend ROADMAP: <reason>`. Do not retroactively rewrite completed-phase
entries — append a note to the relevant phase section instead.
