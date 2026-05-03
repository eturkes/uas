# PLAN — Phase 5: Policy & long-horizon UX

Phase reference: `ROADMAP.md` §Phase 5 — Policy & long-horizon UX
(under "Phase details", currently active per ROADMAP §"Current
phase").

Substrate references: `docs/orchestrator.md` (Phase 3 design
notes); `docs/substrate.md` (Phase 1 + Phase 2 substrate boundary
catalog); `docs/cut_bucket.md` (Phase 4 cut buckets and
rationale, including the "How to read this in the future" gate
any Phase 5 re-introduction proposal must pass);
`docs/cut_list.md` (Phase 4 closed cut list).

Phase 4 left the orchestrator + slim substrate as the entire
working UAS: `./uas-orchestrate start synthetic-multistep` runs
3/3 subtasks done with all four state artefacts populated; the
pause+resume cycle is intact; the eval substrate self-tests
green. Phase 5's job is to take that engine and (a) build the
user-facing layer that makes it usable for real long-horizon
work, and (b) validate the experience end-to-end on a real task
the project owner cares about.

This PLAN is a draft. Per the project's decision protocol, Phase
5 work does not begin until the project owner has reviewed and
approved this file.

## Phase exit criteria (from ROADMAP.md)

- One real multi-day task has been run via the orchestrator and
  the experience is good enough that the project owner intends
  to keep using it.
- Written-up findings are recorded under "Completed phases" at
  phase close.

## Scope discipline (non-negotiable)

- **Validation-driven, not design-driven.** Phase 5 is
  fundamentally validation work — the user-facing layer
  deliverables exist to support a specific real task, not as
  speculative scaffolding. §1 picks the real task; §2–§5 build
  exactly what that task needs and nothing more. Per CLAUDE.md
  core principle 1 ("No new mechanism without an eval-visible
  win") and core principle 6 ("Measure before you change"),
  every Phase 5 mechanism must trace back to a real-task need.
- **§1 is a user-input gate.** §1 closes only when the project
  owner has named a concrete real-task target. §2's design
  decisions cannot be made without that pick. The PLAN does not
  invent a task on the user's behalf.
- **§2–§5 steps are skeletal until §1 closes.** Each §2–§5
  section sketches its goal and acceptance criteria, but defers
  detailed step authoring until §1 surfaces the task's specific
  shape. This is consistent with CLAUDE.md's decision protocol
  ("Ask before executing — A PLAN step is ambiguous given state
  discovered during execution").
- **No re-introduction of Phase 4 cut content.** Phase 4 cut
  ~111 files explicitly. Any temptation to bring back a
  pre-prune mechanism (architect-style validation, llm_judge,
  layered config loader, etc.) must clear the three-step gate
  in `docs/cut_bucket.md` § "How to read this in the future"
  and explicitly land in this PLAN as a deliberate decision —
  not silently restored.
- **Real Claude Max budget will be spent.** Unlike Phase 4's
  synthetic-multistep verification (which spent ~$0.12 per
  end-to-end cycle on a trivial 3-subtask task), Phase 5 §6's
  real-task run will consume real budget across multiple
  windows. §1 must include a rough budget expectation; §6 must
  surface budget consumption in real time so the project owner
  can stop the experiment if cost diverges materially from
  expectation.
- **Sections are roughly linear but §3–§5 may interleave.**
  Implementation order follows the dependency graph: §1 (audit
  + task pick) → §2 (task spec extension) → §3 / §4 / §5
  (policy / resume summary / checkpoints — implementable
  independently or in any order) → §6 (real-task pre-flight +
  first run) → §7 (continuation + write-up) → §8 (phase
  close). PLAN sections stay numbered for clarity; the actual
  work order is decided at execution time.

## Section 1 — Scope audit + real-task selection gate

**Goal.** Read-only audit of what Phase 3 + Phase 4 left in
place vs what Phase 5's four user-facing deliverables actually
require, plus the user-input gate that picks the real-task
target. §1 closes only when both the audit doc exists and the
project owner has named a concrete task.

**Steps.**

1. Audit Phase 3 + Phase 4 deliverables against the four
   ROADMAP §Phase 5 user-facing deliverables. For each
   deliverable, record what already exists vs what is missing:
   - **Policy configuration interface** — what does
     `orchestrator/policy.{py,default.toml}` already support?
     What knobs are user-tunable? What's hardcoded? Per-task
     overrides via `<task>-policy.toml` exist; what scopes
     don't (per-stage? per-checkpoint? project-wide
     defaults)?
   - **Long-horizon task definition spec** — what schema does
     `orchestrator/cases/synthetic-multistep.toml` use? What
     scaling gaps exist for multi-day work (multi-stage
     decomposition? dependency edges? per-stage metadata?
     checkpoint declarations? resume hints)?
   - **Resume summary format** — what information is currently
     surfaced by `./uas-orchestrate status <task>`? What sits
     in `task_events.jsonl` / `rate_limits.jsonl` /
     `buffer.jsonl` / `policy.toml` that a user resuming after
     a multi-hour gap would want to see digested? What format
     (Markdown digest, JSON sidecar, both) maximises
     utility?
   - **Human-checkpoint design** — what decision-recording
     mechanism does `orchestrator/task.py:record_decision`
     already provide? How would explicit checkpoint types
     (review-plan, review-commit, review-regression) extend
     it? What UX surface lets the user acknowledge a
     checkpoint and resume?
2. Audit Phase 4 §7's `IS_SANDBOX=1` repair: what other
   substrate transitions (Phase 4 §6 surgery cleanup) might
   surface only on a real-task run? Catalogue suspicions so
   §6 isn't surprised by them. Specifically check:
   - Containerfile surgery — is anything else (besides
     `IS_SANDBOX=1`) image-baked that should be per-spawn?
   - `setup_auth.sh:34` — does the rewritten error message
     still fire the right control flow on a fresh-clone
     install?
   - `docs/substrate.md` § Open questions — does the
     percentage-cap question matter for the §1 task, and if
     yes, does the orchestrator need an interim workaround?
3. Audit `docs/cut_bucket.md` § "How to read this in the
   future" gate. Phase 5 design decisions that look like
   "should we add X back?" must be explicitly checked against
   the gate; record any such decisions in §1 Results.
4. Write `docs/phase5_scope.md` (new file): brief audit summary
   from steps 1–3, plus a "what real-task candidates make
   sense" enumeration with rough scope / timeline / budget
   estimates per candidate.
5. **Stop and ask the project owner:** which real-world task
   will Phase 5 validate against? Required input from the
   project owner:
   - Concrete task description (one paragraph).
   - Expected timeline (days).
   - Expected number of window boundaries (5-hour pauses) to
     traverse.
   - Rough budget expectation (paid-buffer dollars; helps §6
     decide when to abort if reality diverges).
   - Which decisions need human checkpoints (review-plan,
     review-commit, review-regression — or a different set).
   - What "done enough" looks like as the exit criterion.

**Acceptance.**

- `docs/phase5_scope.md` exists with the audit summary +
  candidate enumeration.
- Real-task target named, with rough scope / timeline /
  budget / checkpoint decisions / done-criterion captured.
- §1 Results subsection records the audit findings + the
  user's task decision verbatim (so §2–§7 can refer back to
  the explicit pick).

**Status:** pending

## Section 2 — Long-horizon task definition spec

**Goal.** Extend `orchestrator/cases/<task>.toml` schema as
needed to express the §1-picked task's structure. Anticipated
additions (concrete list authored after §1 closes):

- Multi-stage decomposition with dependency edges (current
  `synthetic-multistep` is linear).
- Per-stage policy overrides (independent of §3's broader
  policy work).
- Checkpoint declarations (review-plan, review-commit,
  review-regression — set authored from §1's user input).
- Per-stage expected-duration / expected-spend metadata (drives
  §6's real-time divergence detection).
- Resume hints (where to pick up if interrupted mid-stage).

**Constraints.**

- Schema additions must be backward-compatible with
  `synthetic-multistep.toml` so the Phase 4 §7 verification
  cycle still passes after §2.
- Implementation lives in `orchestrator/task.py` (Task /
  Subtask / Decision dataclasses) — extend, don't replace.
- Each schema field added must trace back to a §1 task
  requirement. Speculative fields not driven by §1 are out of
  scope.

**Steps.** Authored after §1 closes.

**Acceptance.**

- `orchestrator/cases/<§1-task>.toml` validates and loads.
- `synthetic-multistep` cases still load + run unchanged
  (regression gate).
- §2 Results subsection records: schema additions, lines
  changed in `task.py`, regression run for `synthetic-multistep`.

**Status:** pending

## Section 3 — Policy configuration refinements

**Goal.** Extend `orchestrator/policy.py` +
`policy.default.toml` for any per-stage / per-checkpoint
overrides the §1 task surfaces. Possible additions (TBD per
§1):

- Per-stage policy overrides (different soft-cap action
  during exploration vs execution).
- Project-level policy defaults (separate from per-task
  overrides).
- Policy inspection CLI (`./uas-orchestrate policy <task>`).
- Policy validation / linting at task-load time.

**Constraints.**

- Backward-compatible with Phase 3 §5's policy machine.
- `tests/test_orchestrator_policy.py` + Phase 4 §7's
  pause+resume cycle stay green.
- Each addition must trace back to a §1 task requirement.

**Steps.** Authored after §1 / §2 close.

**Acceptance.**

- `tests/test_orchestrator_policy.py` runs green.
- Phase 4 §7's pause+resume cycle still passes on
  `synthetic-multistep`.
- §3 Results subsection records additions + regression check.

**Status:** pending

## Section 4 — Resume summary writer

**Goal.** On soft-cap wrap-up and on explicit
`./uas-orchestrate resume`, write a human-readable digest of
the task state since last invocation. Format: Markdown for
human reading + (optionally) JSON sidecar for structured
access.

The digest answers, at minimum:

- What stages / subtasks completed since last invocation.
- What's currently pending vs in-flight vs failed.
- What decisions were recorded (including checkpoint
  acknowledgements).
- Total spend since invocation start, total spend cumulative.
- Suggested next action (resume immediately / wait for
  window / review checkpoint / etc.).

Persistence: written to
`orchestrator/state/<task>/resume_summary.md`; rewritten
on each soft-cap event so the file always reflects the
most recent wrap-up.

**Constraints.**

- Reads from existing `task_events.jsonl` / `rate_limits.jsonl`
  / `buffer.jsonl` / `policy.toml` only — no new persistent
  state.
- No new dependencies (Markdown is plain text; JSON via
  stdlib).
- `tests/test_orchestrator_*.py` stay green.

**Steps.** Authored after §1 / §2 close.

**Acceptance.**

- `resume_summary.md` written on first soft-cap event during
  a `synthetic-multistep` test run.
- Format spot-checked by project owner for readability /
  utility.
- §4 Results subsection records: writer entry point, format
  decision (Markdown only vs Markdown + JSON), test coverage.

**Status:** pending

## Section 5 — Human-checkpoint primitive

**Goal.** Add a checkpoint primitive to the policy / task
machinery. Checkpoint types declared in §2's task spec;
behaviour:

- Pause execution at the declared checkpoint point.
- Record a `decision` event in `task_events.jsonl` with
  checkpoint context (what was about to happen, why pausing).
- Surface the checkpoint via `./uas-orchestrate status <task>`
  (status output makes it obvious that user input is awaited).
- Resume requires explicit acknowledgement:
  `./uas-orchestrate resume <task> --ack-checkpoint <id>`
  (default `resume` without `--ack-checkpoint` refuses to
  proceed past the pending checkpoint).

**Constraints.**

- Backward-compatible with the Phase 3 §6 task model.
- `tests/test_orchestrator_task.py` + the Phase 4 §7
  pause+resume cycle stay green.
- Checkpoint types are declarative — the orchestrator does
  not invent checkpoint criteria; the §2 task spec declares
  them explicitly.

**Steps.** Authored after §1 / §2 close.

**Acceptance.**

- A `synthetic-checkpoint` test fixture demonstrates the
  full cycle: spawn → checkpoint → status → ack-resume → next
  spawn.
- `tests/test_orchestrator_task.py` extended with the
  checkpoint cycle.
- §5 Results subsection records: checkpoint types
  implemented, fixture details, test coverage.

**Status:** pending

## Section 6 — Real-task pre-flight + first window run

**Goal.** Project owner reviews the §2–§5 setup against the §1
task. Then start the task with `./uas-orchestrate start
<§1-task>`. Run until first window-boundary, first checkpoint,
or first natural pause. §6 is the first time real Claude Max
budget is consumed under Phase 5.

**Pre-flight checklist (run before `start`):**

- §2 task spec validates and matches §1's user description.
- §3–§5 deliverables are wired up and tested.
- Policy override (if any) is sensible for the task's
  expected scope / timeline / budget.
- Auth is fresh (`./setup_auth.sh` if needed).
- Container engine is up.
- Fresh state-root selected (or existing one explicitly
  reused — decision recorded).

**During the run:**

- Project owner observes; orchestrator runs autonomously.
- Real-time budget monitoring: every N spawns or every
  window event, check that cumulative spend matches §1's
  rough expectation (within reasonable variance).
- Hard abort condition: cumulative spend > 2× the §1
  expectation, OR project owner judges the task off-rails.

**Steps.** Authored after §2–§5 close.

**Acceptance.**

- Task started via `./uas-orchestrate start`.
- At least one window boundary, checkpoint, or natural pause
  encountered.
- All four state artefacts (`task_events.jsonl`,
  `rate_limits.jsonl`, `buffer.jsonl`, `policy.toml`)
  populated.
- §4's `resume_summary.md` written and reviewed by project
  owner.
- §6 Results subsection records: start time, spend at
  pause, what triggered the pause, project owner's
  observations.

**Status:** pending

## Section 7 — Real-task continuation + post-run write-up

**Goal.** Continue across window boundaries and checkpoints
until the §1 exit criterion is met (or project owner stops the
experiment). Capture findings.

**Steps (sketch — refined after §6):**

1. Loop: read §4's resume summary → decide continue / adjust
   / abort → if continue, `./uas-orchestrate resume <task>`
   (with `--ack-checkpoint <id>` if a checkpoint is pending).
2. If continue with adjustments: edit `<task>.toml` or
   `<task>-policy.toml` between resumes; record the
   adjustment as a `decision` event with rationale.
3. Stop condition: §1 exit criterion met, OR project owner
   judges the experiment complete (positive or negative
   outcome).
4. Post-run write-up (free-form Markdown, location TBD —
   suggest `docs/phase5_findings.md`):
   - What worked.
   - What didn't.
   - What new mechanisms or refinements are Phase 6+
     candidates (each must clear the §6+ standing rules:
     justified by real-task evidence, ablatable from day
     one, doesn't re-implement Claude Code natives).
   - Open questions for the next phase.

**Acceptance.**

- §1 exit criterion met OR experiment formally stopped with
  reasoning recorded.
- `docs/phase5_findings.md` exists with the four sub-sections
  above.
- §7 Results subsection records: total task duration, total
  spend, total window-boundary count, total checkpoint
  count, headline outcome ("worked" / "partially worked" /
  "didn't work" with one-paragraph elaboration).

**Status:** pending

## Section 8 — Phase 5 close

**Goal.** Standard phase-close: ROADMAP delta + PLAN removal.

**Steps.**

1. ROADMAP §"Current state of the codebase": append a
   "post-Phase-5" sub-paragraph if anything material changed
   (new modules, new test count, etc.). Phase 4's
   sub-paragraph stays verbatim per project convention.
2. ROADMAP §"Completed phases": new
   `### Phase 5 — Policy & long-horizon UX` entry recording
   §1–§7 deliverables, the chosen real task, the headline
   outcome, and substrate findings carried into Phase 6+.
3. ROADMAP §"Current phase": flip pointer Phase 5 → Phase 6+
   (active). ROADMAP "Phase plan" table: Phase 5 status
   completed; Phase 6+ status active.
4. Standard `Mark Section 8 completed; close Phase 5` commit
   covering all the above.
5. Standard `Remove completed PLAN file` final commit.

**Acceptance.**

- ROADMAP active pointer is Phase 6+.
- Phase 5 entry in "Completed phases" matches the project's
  established close-entry shape (Phase 0–4 entries are the
  template).
- PLAN.md no longer exists in tree.
- §8 Results recorded in PLAN.md before its removal commit.

**Status:** pending
