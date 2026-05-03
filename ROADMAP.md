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

## Model policy

**Unified default-model policy (current).** Every Claude invocation
under the UAS framework — eval harness, LLM-as-judge, fuzzy-function
calls, orchestrator subprocess, interactive setup — uses Claude's
current default model (today: **Opus 4.7**, `claude-opus-4-7`) with
maximum effort and max output tokens. The CLI subprocess paths
achieve this by omitting the `--model` flag entirely so the CLI
selects its own default; the SDK paths (`integration/llm_judge.py`,
`uas/fuzzy.py`) hardcode `claude-opus-4-7` because the SDK requires
an explicit model id and must be bumped in lockstep with new Claude
releases. Per-role / per-shell pins remain available via
`UAS_MODEL` (or `UAS_MODEL_PLANNER` / `UAS_MODEL_CODER`) and
`uas.example.toml`'s `model` / `model_planner` / `model_coder` keys,
plus the per-case `model` field in `integration/cases/`. Cost is
explicitly not a constraint.

**Implication for measurement.** Earlier baselines and any planned
Phase 4 ablation deltas were intended to be Haiku-relative; under
the unified policy they will be Opus-relative instead. The
"Baseline metrics" section below was never populated, so no recorded
numbers are invalidated. Phase 2 and Phase 4 will produce Opus 4.7
numbers when run. Methodological consistency across phases is still
preserved — every measurement uses the same model — only the
specific model has changed. Mechanism contribution under Opus may
differ from contribution under Haiku, since some self-correction
mechanisms exist precisely to compensate for weaker single-shot
completions and may matter more on Haiku than on Opus, and vice
versa.

**Prior policy (superseded).** Previously the framework split into
two regimes — eval harness on Haiku 4.5 for cost, real-world task
execution on Opus 4.6 — with the eval-harness model pinned via
`integration/eval.py`'s `EVAL_MODEL_DEFAULT` constant and the
open-ended cases pinning Haiku 4.5 for the judge. That split is
removed; both regimes now follow the unified policy above. The
`EVAL_MODEL_DEFAULT` constant is removed; the per-case Haiku judge
pins are removed.

## Pivot (May 2026) — usage-limit-aware long-horizon orchestrator

After Phase 1 closed, project direction was reframed. The harness
exists; what it should be measuring has changed.

**Original framing.** "Best harness conceivable for autonomous agents
on long-horizon tasks." Operationally, this had become "build the
measurement instrument, then ablate the 68-mechanism scaffold to find
which mechanisms earn their keep." Phase 1 was the instrument; the
old Phases 2–5 were the ablation pipeline.

**Refined framing.** The actual goal is **a usage-limit-aware
orchestrator that maximizes productive use of a Claude Max
subscription on long-horizon tasks**. The 68-mechanism scaffold was
sized against an earlier Claude that had different gaps; under
Claude Code 2026 (sub-agents, hooks, MCP, skills, 1M context, native
session compaction, statusline JSON exposing rate-limit data) most of
those mechanisms duplicate or fight Claude Code's native capabilities.
§10's empirical run was direct evidence: Opus 4.7 plus all 68
mechanisms enabled spent 38 min and $16 over-decomposing a hello-file
task into TDD steps and never produced the file. The scaffold's
coupling cost dominates on at least the trivial tier.

The harness's job is no longer "improve subtask quality" — Claude
Code itself delivers that. The harness's job is **scheduling and
budgeting**: keep a Claude Code worker productively busy across a
multi-day task within the constraints of the Claude Max usage limits
(5-hour rolling window, weekly limit, paid buffer overflow) and the
user's preferences for when to pause vs. spend buffer.

**Architectural shape.** External daemon (Python) that:

- Spawns headless `claude --print --dangerously-skip-permissions`
  workers per subtask in a sandboxed workspace (Phase 1 substrate).
- Reads usage-limit ground truth from Claude Code's statusline JSON
  `rate_limits` field (added in Claude Code v2.1.80, March 2026):
  `five_hour.used_percentage` / `resets_at` and
  `seven_day.used_percentage` / `resets_at`.
- Computes paid-buffer spend per call from `usage.input_tokens` /
  `usage.output_tokens` × pricing table (buffer state isn't exposed
  in `rate_limits`; inferred locally).
- Maintains a three-state policy machine: free / paid / hard-stop,
  with policy-driven soft transitions at the 5h and weekly cap
  boundaries (default: pause at 5h, wrap up at weekly, hard-stop
  near $200 buffer; configurable per task).
- Persists task and ledger state across invocations via JSONL
  (Phase 1 substrate, extended).

**Substrate carried forward from Phase 1** (kept intact, treated as
foundation, not subject to the pivot's deletions): Docker sandbox,
OAuth 4-stage refresh, JSONL audit log + provenance metadata,
workspace isolation, resume-from-JSONL, eval harness as one tool
among several.

**Empirical TBDs to resolve at Phase 2 implementation start, not by
research:**

1. Does the statusline hook fire during headless `claude --print`
   invocations, or does the orchestrator need a different read
   pattern (separate brief `claude` invocation, alternative surface,
   etc.)?
2. Does `used_percentage` cap at 100% in buffer mode, or can it
   climb past 100% to signal overflow?

Both are ~5-min empirical questions once the orchestrator skeleton
exists; not blocking for ROADMAP planning.

**Implication for Phases 2–6+.** The original Phase 2 ("3 baseline
runs"), Phases 3–4 (ablation flags + ablation study), and Phase 5
(prune-by-evidence) are all superseded. The new phase plan replaces
the old Phases 2–6+. Phases 0 and 1 are unaffected; the substrate
they delivered is the foundation. The new prune phase moves up and
becomes more aggressive — most of the 68 mechanisms are now
delete-candidates because they don't serve the orchestrator's
scheduling-and-budgeting role.

## Current phase

**Phase 5 — Policy & long-horizon UX** (active)

Phase 4 closed at the same commit that added its "Completed
phases" entry below. The codebase shrank from 164 tracked files
to 55 (−66.5%) and from ≈ 62,064 text lines to 16,256 (−73.8%);
the orchestrator + slim substrate is the entire working UAS;
README rewritten from scratch in §8; `docs/cut_bucket.md`
records the Phase 4 cut buckets and rationale for future
readers. Phase 5 deliverables are listed under "Phase details"
below; Phase 5 PLAN is pending — draft it before executing
Section 1, and pause for user review before starting (per the
decision protocol in `CLAUDE.md`).

## Phase plan

| # | Phase | Status | One-line goal |
|---|---|---|---|
| 0 | Audit | completed | Catalog mechanisms, eval infra, flags, dependencies. No code changes. |
| 1 | Eval harness hardening | completed | Turn eval.py into canonical measurement tool. Curated benchmark. Deterministic + LLM-judge grading. Persistent results with noise bounds. |
| 2 | Substrate verification | completed | Verify empirical TBDs (statusline-during-print, percentage-cap behavior). Document substrate boundaries from Phase 1. Estimate cut surface. |
| 3 | Orchestrator core | completed | Build the daemon: usage-limit ledger from statusline JSON, paid-buffer ledger from per-call usage, headless-worker primitive, three-state policy machine, task-state surviving invocation boundaries. |
| 4 | Prune | completed | Delete most of the scaffold. Default verdict on any mechanism is **cut**; keep list is short and explicit (Docker sandbox, OAuth refresh, JSONL log, provenance, workspace isolation, resume-from-JSONL, eval harness substrate). README rewritten from scratch. |
| 5 | Policy & long-horizon UX | **active** | Policy configuration interface, task definition spec, resume-summary format, human-checkpoint design. End-to-end real long-horizon task with project owner observing. |
| 6+ | Informed iteration | pending | Add new orchestrator capabilities responsibly, each justified by real-task evidence not speculation. Slim discipline (no re-implementing what Claude Code does natively) holds indefinitely. |

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

**Exit criteria:** `uas-eval` runs end-to-end on the canonical case
suite, produces a deterministic pass/fail outcome with full
reproducibility metadata, and appends to the persistent log within
the ~10-minute budget defined under "Suite scope" below.

**Suite scope (amended after §9 close).** The original Phase 1 scope
called for 30–50 tasks across 4 tiers, run 3× per measurement, with
the canonical command being `uas-eval --runs 3` and a "two
consecutive runs produce statistically indistinguishable results"
exit gate. After §1–§9 of the Phase 1 PLAN closed, the project
owner directed a scope reduction to fit a ~10-minute wallclock
budget for the canonical run. The amended Phase 1 scope is:

- **One case** (`cases/trivial/hello-file.json`) — the smoke that
  also serves as the canonical regression gate. The §9-authored
  case set (5 trivial / 15 moderate / 10 hard / 5 open_ended)
  was deleted along with the `integration/data/` fixture
  directory; both are recoverable from `git log` if Phase 4
  needs them.
- **`--runs 1` default** (was `3`). Multi-run variance remains
  available via explicit `--runs N` (still tested by
  `tests/test_eval_variance.py`) but is opt-in. The "noise
  bounds on the full benchmark" deliverable is therefore
  retired; the multi-run plumbing stays.
- **Exit gate softened**: the harness must run end-to-end and
  produce well-formed artefacts. The "statistically
  indistinguishable" two-run check is dropped as incompatible
  with a 1-case × 1-run default.

The phase's measurement-first principle is unchanged. The
instrument exists, every code path that would re-stress the
mechanism catalogue is intact, and Phase 4 ablation will draw
fresh cases from the catalogue when it actually starts. The
amendment trades benchmark breadth for a fast-feedback loop —
consistent with "the scaffold cannot exceed the model" plus
"deletion is as valuable as addition" from the principles list.

### Phase 2 — Substrate verification

**Goal:** before standing up the orchestrator, confirm the substrate
actually supports it. Resolve the two empirical TBDs flagged in the
pivot section above, and document the boundaries of what Phase 1's
deliverables provide vs. what the orchestrator must add.

**Deliverables:**

- **Empirical TBD 1 (statusline-during-print):** verify whether
  `claude --print --dangerously-skip-permissions <prompt>` triggers
  the statusline hook and yields a JSON payload with `rate_limits`.
  If yes, document the read pattern. If no, design and prototype the
  alternative read pattern (separate brief `claude` invocation,
  parsing of intermediate state, or whatever works).
- **Empirical TBD 2 (percentage-cap behavior):** verify whether
  `rate_limits.five_hour.used_percentage` and
  `seven_day.used_percentage` cap at 100 in buffer mode, or climb
  past 100 to signal overflow. Run a controlled test pushing one of
  the limits past its cap on a cheap workload.
- **Substrate-boundary catalog:** explicit list of what Phase 1's
  deliverables (Docker sandbox, OAuth 4-stage refresh, JSONL audit
  log, provenance metadata, workspace isolation, resume-from-JSONL,
  eval harness) provide as APIs the orchestrator consumes vs.
  internal details. Documented in `docs/substrate.md` (new file).
- **Cut-surface preview:** rough first-pass of which existing
  modules / mechanisms from the Phase 0 catalog are expected to
  delete in Phase 4. Not a final cut list — Phase 4 makes those
  calls — but a sized estimate so the team knows the scale.

**Exit criteria:** both TBDs resolved with documented findings;
substrate-boundary doc exists; expected-delete list (estimated
scale) recorded.

### Phase 3 — Orchestrator core

**Goal:** build the daemon that owns long-horizon task state, spawns
headless Claude Code workers, and enforces the three-state policy
machine against real usage-limit signals.

**Deliverables:**

- External daemon. Long-running process, systemd-managed service, or
  CLI invoked by the user — design pending.
- Usage-limit ledger reading `rate_limits` from statusline JSON per
  Phase 2's resolved read pattern. Cached locally; refreshed per
  worker invocation; sanity-checked against an internal
  message-count tally. Divergence beyond a configured threshold
  raises an alert.
- Paid-buffer ledger computed from per-call `usage.input_tokens` /
  `usage.output_tokens` against a pricing table. Persisted to the
  JSONL audit log.
- Headless-worker primitive: spawn `claude --print
  --dangerously-skip-permissions <subtask spec>` in a sandboxed
  workspace, capture output, increment the ledgers, persist the
  result.
- Three-state policy machine (free / paid / hard-stop). Default
  policy: pause at 5h soft cap, wrap up at weekly soft cap,
  hard-stop near $200 buffer. Configurable per task via TOML or
  similar.
- Task-state model surviving invocation boundaries: the long-horizon
  task definition, the subtask queue, what's done / in-flight /
  pending, decisions log, resume hints.
- Resume-from-state across invocations (extending the Phase 1
  resume-from-JSONL mechanism).

**Exit criteria:** the orchestrator runs a defined long-horizon task
end-to-end across at least one window boundary (5h pause + resume,
or weekly wrap + resume next week) without human intervention, with
all ledgers and state cleanly persisted.

### Phase 4 — Prune

**Goal:** delete most of the existing scaffold. **Default verdict on
any given mechanism is cut.** The keep list is short and explicitly
justified; the migrate list is expected to be near-empty. Phase 0's
68-mechanism catalog is the deletion checklist, not a verdict
matrix. This phase has moved up from Phase 5 in the original plan
and is much more aggressive than the original "ablate first, prune
what failed" model — this plan prunes anything that doesn't fit the
orchestrator's job description, then verifies the slimmed system
still works.

**Keep list (current best estimate, finalized at phase start):**

1. Docker sandbox + `Sandbox.Dockerfile` (`orchestrator/sandbox.py`).
2. OAuth 4-stage refresh logic (currently in `integration/eval.py`).
3. JSONL audit log primitive + write/append logic (Phase 1 §5).
4. Provenance metadata capture — `git_sha`, `git_branch`,
   `git_dirty`, `harness_version`, `config_hash`, `env_snapshot`,
   `timestamp_utc` (Phase 1 §4).
5. Workspace isolation pattern — per-task
   `integration/workspace/<task>/` directory convention.
6. Resume-from-JSONL logic (commit `d81e42e`).
7. Eval harness shell wrapper + deterministic check types
   (`file_exists`, `file_contains`, `pytest_pass`, `exit_code`,
   `file_shape`, `command_succeeds`, content regex) + the
   `hello-file` smoke case. The LLM-as-judge module
   (`integration/llm_judge.py`) is *not* on the keep list — Claude
   Code sub-agents fill that role natively.

Possibly-keep, decided at phase start based on what Phase 3
actually consumes: the relevant subset of `uas_config.py`'s loader,
scoped to keys the orchestrator actually reads.

**Migrate list:** expected to be empty. Patterns worth preserving
(property-based verification, structured human checkpoints,
adversarial paired-Claude review) are net-new code in Phase 3 or
configured via Claude Code primitives (skills / sub-agents / hooks
/ MCP), not ports of existing UAS modules.

**Cut surface (sized estimate, not a final list):**

- `architect/main.py` (6864 lines) — essentially total. Failure
  handling, validation cascade, spec-rewrite loops, the
  `/workspace`-literal validator, TDD gate, all gone.
- `planner/main.py` (3700 lines) — essentially total. The
  coverage-driven decomposition is replaced by budget-aware
  decomposition in the new orchestrator.
- `orchestrator/main.py` (2051 lines, despite the name) — mostly
  total. Best-of-N + pre-flight aren't part of the pivot. The new
  orchestrator daemon is a new module, not a refactor.
- All 26 README-undocumented LLM-driven failure-classification
  helpers.
- Reflection / counterfactual / multi-plan-voting / step-DAG /
  cross-run-learning / `UAS_MINIMAL` and the rest of the bundled
  toggles.
- `integration/llm_judge.py`.

**Deliverables:**

- Codebase shrinkage measured (lines removed, files removed,
  modules collapsed).
- README rewritten from scratch to match the slim reality. The
  existing README's framing is too far from the post-pivot system
  to repair piecewise.
- Smoke run on the Phase 1 eval harness to confirm the kept set is
  unbroken.
- Brief notes file recording the cut bucket and the rationale, so
  future readers don't re-introduce things the pivot intentionally
  removed.

**Exit criteria:** the codebase is materially smaller; the
orchestrator + slim substrate is the entire working UAS; the new
README is accurate; the keep list is a closed set with no
"shouldn't this stay?" items outstanding.

### Phase 5 — Policy & long-horizon UX

**Goal:** make the orchestrator usable for real long-horizon work.
Phase 3 builds the engine; this phase builds the user-facing layer
and validates the experience end-to-end on a real task.

**Deliverables:**

- Policy configuration interface (TOML or similar): pause
  thresholds, buffer-spend limits, per-task overrides.
- Long-horizon task definition spec: how the user describes "build
  me X over the next week" in a way the orchestrator can decompose
  and execute against.
- Resume summary format: human-readable digest of what the
  orchestrator did during a window, what's pending, what decisions
  were made — written when wrapping up against a soft cap.
- Human-checkpoint design: explicit checkpoint types (review plan
  before exec, review critical commit, review on detected
  regression). Configurable per task.
- One realistic long-horizon task run end-to-end with the project
  owner observing. Real task, real Claude Max subscription,
  multi-day duration.
- Post-run write-up: what worked, what didn't, what to fix.

**Exit criteria:** one real multi-day task has been run via the
orchestrator and the experience is good enough that the project
owner intends to keep using it. Written-up findings are recorded
under "Completed phases" below at phase close.

### Phase 6+ — Informed iteration

**Goal:** add new orchestrator capabilities responsibly.

**Standing rules** (enforced indefinitely):

1. Any new orchestrator capability is justified by a real
   long-horizon task experience, not by speculation.
2. Any new capability that adds behavior under failure conditions
   ships with a way to disable it from day one.
3. New capabilities are added to the substrate-boundary catalog and
   the slim-system README at the same time as the code.
4. The slim discipline holds: if Claude Code itself can do X
   natively, configure it via skills / sub-agents / hooks / MCP
   rather than re-implementing X in UAS.

**Candidate directions** (not commitments — explore once Phase 5 is
done and only if supported by real-task evidence):

- Adversarial paired-Claude verification (two distinct workers, one
  producing, one critiquing).
- Verification by re-derivation (run a task twice with different
  decompositions; only accept on output equivalence or property-test
  pass).
- Task-density routing (different task types route to different
  scaffolding densities; cure for the §10 over-decomposition mode).
- Persistent project memory (structured facts updated by run
  outcomes, surfaced into next session's context — different from
  reflection traces).
- Multi-machine / multi-account orchestration.

## Current state of the codebase

*(Populated by Phase 0 audit. Detailed mechanism table, eval-gap
analysis, flag inventory, and dependency adjacency lists are recorded
in the Phase 0 audit's `PLAN.md` history; the summary below is the
distillation Phase 1+ should read at session start.)*

*(Note added under May 2026 pivot: under the new direction (see
"Pivot" section above), most of the 68 mechanisms catalogued below
are expected delete-candidates because they duplicate or fight
Claude Code 2026's native capabilities. The catalog still serves as
the input list for Phase 4's prune verdicts; only its framing has
changed from "ablate-then-prune" to "prune-aggressively-then-verify."
The "Implications for Phase 1/Phase 3" subsections below are
historical — see the Phase 2–6+ entries under "Phase details" for
the current direction.)*

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

**Post-Phase-4 mechanism count (added at Phase 4 close).** All
68 catalogued mechanisms were CUT during Phase 4 §3–§6; zero
remain. The post-prune system has no equivalent of the cluster
A–J machinery — the orchestrator delegates subtask execution to
headless `claude --print` workers and Claude Code 2026's native
features (sub-agents, hooks, MCP, skills, 1M context, native
session compaction) fill the roles the deleted clusters once
served. The "mechanism" count for the post-prune system is
better expressed as a substrate-component count (8 components
per `docs/substrate.md`) plus an orchestrator-module count (11
modules under `orchestrator/`) — a fundamentally different unit
than the Phase 0 catalog. The Phase 0 paragraph above is kept
verbatim as historical record; this sub-paragraph records the
Phase 4 close.

## Baseline metrics

*(Note added under May 2026 pivot: this section's metrics framework
is reshaped under the pivot. "Pass rate per tier on a benchmark" is
no longer the primary metric; the new measurement framework emerges
from Phase 5's real-task experience. Section retained as a
placeholder; new metrics, if any, recorded here when they exist.
Original empty table retained below for historical reference but
will not be populated under the pivot direction.)*

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

### Phase 1 — Eval harness hardening

Closed in the same commit that populated this entry. Deliverables
completed: canonical `uas-eval` wrapper at repo root, reduced 1-case
suite (`cases/trivial/hello-file.json`, per the "Suite scope (amended
after §9 close)" note above), hybrid grading (deterministic checks
covering `file_exists` / `file_contains` / `pytest_pass` / `exit_code`
/ `file_shape` / `command_succeeds` / regex, plus
`integration/llm_judge.py` with N-sample majority vote for open-ended
cases), append-only `integration/eval_results.jsonl`, per-task metrics
surfaced from `architect/main.py` (pass/fail, wall time, LLM time,
sandbox time, attempts, tokens, step-status counts, workspace size,
cost), opt-in multi-run variance via `--runs N` (tested by
`tests/test_eval_variance.py`), tiered reporting, and per-row
reproducibility metadata (`git_sha`, `git_branch`, `git_dirty`,
`harness_version`, `config_hash`, `env_snapshot`, `timestamp_utc`).

End-to-end §10 run on commit `fa1c8fe` (dirty) under the unified Opus
4.7 policy produced one well-formed JSONL row plus aggregate: 38.5
min wallclock, FAIL on a hello-file goal (architect over-decomposed
into 5 TDD steps and never produced `hello.txt`), $16.64 reported
cost; every harness plumbing path green. The fail is exactly the
scaffold-ceiling signal Phase 1 was built to surface; full per-step
detail lives in the JSONL log and in `PLAN.md` git history. The
phase's working file `PLAN.md` was removed on phase close per
project convention.

### Phase 2 — Substrate verification

Closed in the same commit that populated this entry. Deliverables
completed: §1 statusline-during-print resolved — headless `claude
--print --dangerously-skip-permissions` does **not** fire the
statusline hook; the working alternative is `claude --print
--output-format stream-json --verbose`, which emits a
`rate_limit_event` line carrying ternary `status` plus paid-buffer
fields (`overageStatus`, `isUsingOverage`, `overageResetsAt`). The
TUI statusline `rate_limits` schema (snake_case
`five_hour.used_percentage` / `resets_at` / `seven_day.*`) is
confirmed against the prior research's claimed shape but is
available only via the interactive surface. §2 percentage-cap
behaviour explicitly deferred to natural-trigger capture (variant B
per the PLAN's cost-vs-information decision step; full reasoning
preserved in `PLAN.md` git history; dual-behaviour design constraint
logged in `docs/substrate.md` § Open questions). §3 substrate-
boundary catalog written to `docs/substrate.md` (~600 lines, 8
components — 7 ROADMAP keep-list items plus the §1 rate-limit read
pattern — each with location / accepts / emits / lifecycle / Phase-3
consumption / gaps). §4 cut-surface preview written to
`docs/cut_surface.md` (~210 lines): 0 of 68 catalog mechanisms on
KEEP, all 10 Phase 0 strongly-coupled clusters CUT in their
entirety, 3 root-level files (`uas_config.py`, `uas_hooks.py`,
`uas.example.toml`) flagged NEEDS-PHASE-3-DECISION; totals reconcile
to the Phase 0 catalog scale.

New artefacts created during the phase: `tools/statusline_probe.sh`
(kept on disk to support §2's deferred natural-trigger capture path;
formal absorption-into-substrate decision deferred), plus
`docs/substrate.md` and `docs/cut_surface.md`. Scope was deliberately
bounded to verification + documentation — no orchestrator code, no
per-mechanism verdicts, no architectural commitments. The phase's
working file `PLAN.md` was removed on phase close per project
convention.

### Phase 3 — Orchestrator core

Closed in the same commit that populated this entry. Deliverables
completed: §1 skeleton + substrate-extraction (auth / provenance
lifted into `integration/`, `uas-orchestrate` shell wrapper,
`orchestrator/cli.py` argparse skeleton, `docs/orchestrator.md`
design notes); §2 headless-worker primitive
(`orchestrator/worker.py` spawning `claude --print
--dangerously-skip-permissions --output-format stream-json
--verbose` against `uas-engine:latest` with line-by-line capture,
container cleanup on timeout, OAuth refresh + image precondition);
§3 usage-limit ledger (`orchestrator/rate_ledger.py` consuming the
Phase 2 §1 stream-json `rate_limit_event` read pattern;
`current_status` / `internal_count` / `check_divergence`); §4
paid-buffer ledger (`orchestrator/buffer_ledger.py` +
`orchestrator/pricing.py` against the project's pricing table;
locally-priced `cost_usd` plus `claude_reported_cost_usd` carrying
Claude's own figure for policy threshold use per the live-trace
~5× divergence finding); §5 three-state policy machine
(`orchestrator/policy.py` decide-rules go / pause_until /
wrap_up / halt with rule-order seven_day → five_hour → buffer →
default; ablatable `enabled = false` short-circuit; per-task TOML
override merged over `orchestrator/policy.default.toml`); §6
task-state model (`orchestrator/task.py` Task / Subtask / Decision
dataclasses, `task_events.jsonl` per-task append-only log, write-
path operations enqueue / start / complete / fail / record_decision;
`orchestrator/workspace.py` resume-safe per-task workspace setup);
§7 resume-from-state replay (`load_task` reconstructs Task by
forward-replaying the JSONL, end-of-replay sweep re-enqueues
`in_flight` subtasks back to `pending` and writes a `task_resume`
decision, per-event `survives_git_sha_flip` gate; `cmd_start` /
`cmd_resume` / `cmd_status` wired); §8 end-to-end window-boundary
run (main orchestration loop driving policy → spawn → repeat;
`--simulate-rate-status` flag; `cmd_pause` / `cmd_halt`
decision-recording exit subcommands;
`orchestrator/cases/synthetic-multistep{,-policy}.toml`).

End-to-end §8 run on commit `4754488` (dirty, with §8 working
changes) under the unified Opus 4.7 policy — though the headless
`claude --print` path defaulted to Haiku 4.5 per the CLI's own
default for that subcommand — produced a clean 3-subtask run
(20.4 s wallclock, $0.1531 reported buffer drain, all four state
artefacts populated) plus a clean explicit pause + resume cycle
without operator intervention beyond the three subcommand calls.
Phase 3 exit criteria satisfied; full per-step detail lives in
the §8 Results subsection of `PLAN.md`'s git history. The phase's
working file `PLAN.md` was removed on phase close per project
convention.

Substrate findings to carry into Phase 4 / 5 / 6+: (a) the live
worker's `rate_limit_info.resetsAt` is a unix epoch integer in
real stream-json output, not the ISO-8601 string `policy.py`'s
docstring claims — `_parse_iso8601` returns `None` for numeric
input which is harmless under §8 because the orchestrator does
not consume `pause_until.until`, but the substrate-doc claim
needs an amendment when a real long-horizon run actually wants
to wait until `until`; (b) the OAuth four-stage refresh path
fired a `[oauth] Self-refresh HTTP 400: invalid_grant` then
recovered via fallback on first worker spawn — the
keep-listed substrate works as documented, surfacing because the
trace would otherwise look alarming.

### Phase 4 — Prune

Closed in the same commit that populated this entry. Phase 4
deleted most of the existing scaffold; default verdict on any
mechanism was CUT, with a short explicitly-justified keep list.

Deliverables completed: §1 closed cut list (`docs/cut_list.md`,
~615 lines, finalising `docs/cut_surface.md`'s Phase 2 §4 sized
estimate into a closed file-granularity classification); §2 trio
resolution (audit confirmed `uas_config.py` / `uas_hooks.py` /
`uas.example.toml` had zero hard importers in keep-list code,
verdict = CUT under §6); §3 architect/ tree cut (16 source +
62 architect-importing tests + 4 §4-bucket tests pulled forward
via the `orchestrator/main.py:25 → architect.git_state`
transitive edge — 82 files, 38,119 deletions); §4 cut
orchestrator legacy + `uas/` tree (8 source + 6 tests, 14 files,
4,917 deletions); §5 LLM-judge module + ML-class quality gate +
`integration/quick_test.sh` cut + `integration/eval.py` surgery
(file dropped from 1,342 to 1,090 lines, hello-file case
restructured to `setup_files`-driven substrate self-test, new
`integration/data/hello.txt` fixture); §6 root-level cleanup
(trio + 5 architect-runner shell scripts + `tests/test_hooks.py` +
`screenshot.png` + `tools/statusline_probe.sh`, plus
`Containerfile` / `setup_auth.sh` / `docs/substrate.md` surgery —
11 files + 1 binary + 3 surgeries); §7 smoke verification (full
pytest 403 passed/1 deselected; `./uas-eval` exit 0 on hello-file
with `config_hash="unavailable"` confirming §2's predicted
`integration/provenance.py:_hash_active_config` fallback;
`./uas-orchestrate start synthetic-multistep` 3/3 subtasks done
$0.1110 spend with all four state artefacts; pause+resume cycle
also verified, $0.1223 spend); §8 README rewrite from scratch +
`docs/cut_bucket.md` rationale doc + this ROADMAP delta.

§7 also surfaced and repaired one regression: §6's removal of
`ENV IS_SANDBOX=1` from the `Containerfile` (per cut_list.md's
"orchestrator workers set these themselves" note) was completed
on the Containerfile side but not on the worker side; first
post-§6 orchestrator run failed every spawn with Claude Code's
`--dangerously-skip-permissions cannot be used with root/sudo`
safety check. §7 added `-e IS_SANDBOX=1` to
`orchestrator/worker.py`'s spawn cmd; the regression note plus
the §6 substrate.md edits document the move.

Final shrinkage at §7 close: tracked file count 164 → 55
(−109, −66.5%); text lines 62,064 → 16,256 (−45,808, −73.8%);
git diff --stat 87d80b4..HEAD reports 121 files changed,
+1,317 / −46,306 = −44,989 net text lines (within ≈ 5% of the
§1 estimate of ≈ 47,224). Per-directory survivor breakdown:
15 root + 15 tests + 14 orchestrator + 7 integration + 4 docs.

Substrate findings carried forward to Phase 5+: (a) post-§6
the `config_hash` JSONL field is permanently `"unavailable"`
because `uas_config.py` is gone — the soft-load in
`integration/provenance.py:_hash_active_config` falls through
the file-not-found guard at L65–66 every time; (b) the
`tools/statusline_probe.sh` natural-trigger capture surface for
the percentage-cap question was retired in §6 — the question
remains open without a capture surface, requiring re-authoring
the probe before any future capture; (c) the orchestrator's
workers must continue passing `IS_SANDBOX=1` per spawn;
substrate.md component 1 already describes the post-prune image
and worker-side env injection accurately. The phase's working
file `PLAN.md` was removed on phase close per project
convention.

## Amending this roadmap

If a phase's scope or goals need to change, record the amendment in a
dedicated commit that touches only `ROADMAP.md`, with a subject like
`Amend ROADMAP: <reason>`. Do not retroactively rewrite completed-phase
entries — append a note to the relevant phase section instead.
