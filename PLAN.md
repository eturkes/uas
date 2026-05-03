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

**Status:** completed

### Results

**Steps 1–4 (audit + write-up).** Done. `docs/phase5_scope.md`
written; covers the four-deliverable gap analysis (steps 1),
the substrate-transition catalog (step 2), the cut_bucket
re-introduction gate audit (step 3), and the candidate
enumeration with rough scope / timeline / budget per
candidate (step 4 — 8 candidates A–H sketched). Audit-only;
no code changes.

**Headline findings to carry into §2–§7.**

1. All four user-facing deliverables are net-new code or
   clean extensions of Phase 3 §5/§6 infrastructure. None
   require re-introducing pre-prune mechanisms; the
   `docs/cut_bucket.md` gate clears trivially. The audit
   doc enumerates the tempting re-introductions §3–§5 step
   authoring must NOT drift toward.
2. Regression baseline for §3–§5 is `pytest 403 passed / 1
   deselected` (Phase 4 §7 close) plus the
   `synthetic-multistep` start + pause+resume cycle being
   green. §6 / §7 don't run pytest; the real-task gate is
   project-owner judgement on the multi-day run.
3. Substrate transitions worth flagging at §6 pre-flight:
   `IS_SANDBOX=1` worker-side injection (resolved Phase 4
   §7); `_parse_iso8601` returns `None` for numeric
   `resetsAt` (matters only if §1 task expects unattended
   auto-resume); `[oauth] invalid_grant` warning expected on
   first spawn (recovers transparently);
   `config_hash="unavailable"` cosmetic only;
   percentage-cap question still open but doesn't touch the
   `claude --print` headless path the orchestrator uses.
4. Newly surfaced (out of §1 scope to act on): `Containerfile:36`
   has a residual `COPY orchestrator/ ./orchestrator/` with no
   in-container consumer (architect-era); pre-prune scratch at
   repo root (`check_environment.py`, `run_test_verification.py`,
   `test_goal_*.py`, `verify_test_goal.py`,
   `test_verification_result.txt`, `test_goal_output.txt`,
   ~520 lines + 2 fixture txts) escaped Phase 4 §6 cleanup;
   empty `architect/` + `uas/` package dirs hold only stale
   `__pycache__`. All flagged in `docs/phase5_scope.md` §
   "Audit findings outside the four-deliverable scope" for
   project-owner decision (clean up at §1.5 mini-step / §6
   pre-flight / future housekeeping phase).

**Step 5 (user-input gate).** Closed. Project owner deferred
the pick to the assistant ("decide on a task yourself,
preferably something token heavy") in the same session as the
§1 audit. Pick + ask answers recorded verbatim below.

#### Real-task pick (verbatim assistant decision)

**Task description.** Comprehensive survey of LLM-based
autonomous coding agents (2023–2026): architecture patterns,
reliability mechanisms, evaluation methodologies, and open
problems. The orchestrator dispatches one subtask per topic
to a `claude --print` worker; each worker produces a
detailed Markdown section drawing on the model's training-data
knowledge of published work. A final synthesis subtask
consumes all prior section outputs and produces a single
consolidated `survey.md` long-form report. The task is
deliberately token-heavy: each subtask is a 5–10k-word
deep-dive, the synthesis subtask consumes the full prior
output corpus, and there are ~22 subtasks so total token
volume is in the hundreds of thousands.

This is candidate G in `docs/phase5_scope.md` step 4
(literature review / synthesis), adapted to a topic with
direct relevance to the project: surveying the design space
UAS itself sits in. The output has post-task value as
project context for Phase 6+ direction-setting; the
research-survey shape is also the cleanest token-heavy
candidate that needs no external data, no internet access,
no destructive code mutations, and no per-subtask domain
setup.

**Decomposition (~22 subtasks across 5 stages with one
dependency edge between stage 4 and stage 5).**

- *Stage 1 — Architecture patterns (8 subtasks):* one
  per pattern. ReAct, Reflexion, Tree-of-Thought, multi-
  agent systems, plan-and-execute, RAG-augmented coding
  agents, code-specific agents (e.g., SWE-agent), and
  autonomous coding workers (e.g., Devin-style).
- *Stage 2 — Reliability mechanisms (6 subtasks):*
  verification approaches, retry/backoff policies,
  checkpointing + rollback, validation cascades,
  multi-plan voting, and budget-aware scheduling.
- *Stage 3 — Evaluation methodologies (4 subtasks):*
  benchmark design, LLM-as-judge, deterministic /
  automated grading, human-in-the-loop evaluation.
- *Stage 4 — Open problems (3 subtasks):* long-horizon
  coordination, scaffold-vs-model trade-off, cost /
  budget management at scale.
- *Stage 5 — Final synthesis (1 subtask, depends on
  stages 1–4):* consume all prior outputs, produce
  `survey.md` (≥10k words, ≥30 sections, with
  cross-references back to each stage 1–4 contribution).

**Expected timeline.** 1–3 days wallclock. Most of the
time is window-boundary waiting; pure compute is on the
order of 60–120 minutes total.

**Expected number of 5h window boundaries.** 1–3.
22 subtasks at average ~3–5 minutes each = ~66–110
minutes of compute, well above one 5h window's
sustained-spend ceiling under typical Claude Max usage
patterns; expect at least one soft-cap pause; the
synthesis subtask alone is large enough to fall on the
other side of a window in some session shapes.

**Rough budget expectation.** $20–$80 paid-buffer (well
under the $200 default `hard_stop_usd`). Each subtask
is order-of-magnitude $1–$3 on Haiku 4.5 (the
`claude --print` default model); synthesis subtask is
$5–$10 because it consumes all prior outputs as input.

**Checkpoint set.** Zero in the real-task run. The
project owner is unavailable for mid-task input; gating
the run on user acks would block progress. The §5
checkpoint primitive is still implemented and validated
via the synthetic-checkpoint fixture per PLAN §5
acceptance criteria (which calls for a fixture cycle
specifically, not a real-task one). Phase 6+ may
re-evaluate adding checkpoints to the real-task shape
after §7's findings.

**Done-enough criterion.** All of:

- `survey.md` produced in `<workspace>/synthesis/`,
  ≥10k words, with sections corresponding to each of
  stages 1–4 plus an introduction + conclusion.
- ≥18 of 22 subtasks completed (≥80%); fewer than 5
  failed via `worker_fail`.
- Total spend stayed below $200 hard-stop (no
  `policy_halt` decision in the timeline).
- At least one window-boundary transition successfully
  traversed (`policy_pause` decision present + a later
  `task_resume` decision after operator re-invocation).

If the run satisfies all four, §6 is "passed" and §7
captures findings on the experience. Partial-pass
outcomes (e.g., synthesis produced but several stage
subtasks failed) get written up as "partially worked"
in `docs/phase5_findings.md` per §7 acceptance.

#### Audit-driven ask answers (assistant decisions)

a. **Out-of-scope cleanup (pre-prune scratch, empty
   package dirs, Containerfile residual `COPY`).**
   **Defer to §6 pre-flight checklist.** The residuals
   are functionally inert — they don't affect the
   orchestrator's behaviour or the real-task run — so
   spending §1.5 cycles on them adds no Phase 5 value.
   At §6 pre-flight a single batch cleanup commit can
   run before `start` if the project owner wants a
   clean image build for the real-task run; otherwise
   they wait until a future housekeeping phase. Logged
   in `phase5_scope.md` § "Audit findings outside the
   four-deliverable scope" so they don't get lost.

b. **Unattended auto-resume across window boundaries.**
   **Yes, the §1 task expects unattended auto-resume.**
   The project owner explicitly stated they're
   unavailable for input; running 22 token-heavy
   subtasks with manual re-invocation after every 5h
   pause defeats the validation. This makes
   `_parse_iso8601` numeric-handling a real gap §3 must
   close. §3 step authoring should include: detect the
   numeric `resetsAt` shape, parse epoch ints
   correctly, and add a polling-loop fallback in the
   orchestrator main so an unparseable timestamp
   doesn't strand the task indefinitely. The
   substrate-doc claim about ISO-8601 needs amending
   in the same change.

c. **Match against candidates A–H.** The pick is an
   adaptation of candidate G (literature review /
   synthesis), with topic = "LLM-based autonomous
   coding agents (2023–2026)" — chosen for direct
   relevance to UAS's own design space and for the
   "no external data needed" property that keeps the
   subtask shape simple. Verifiability is medium per
   the candidate sketch — spot-check claims against
   published literature; project owner's domain
   familiarity helps post-run assessment.

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

**Status:** completed

### Results

**Schema additions (TOML-level, all backward-compatible).**

1. Optional top-level `[[stages]]` array. Each entry:
   - `stage_id` (required, non-empty string, unique within task).
   - `name` (optional string, default `""`).
   - `depends_on` (optional list of `stage_id` strings;
     references must exist; self-reference rejected; dependency
     graph must be acyclic — three-color DFS at load time).
   - `expected_duration_seconds` (optional non-negative number;
     planning hint feeding §6 divergence detection).
   - `expected_spend_usd` (optional non-negative number; same).
2. Optional `stage_id` field on each `[[subtasks]]` entry. Must
   reference a declared `[[stages]].stage_id` when present;
   `None` (the default) means the subtask is not grouped — the
   pre-§2 shape that `synthetic-multistep` continues to use.

**Schema additions (dataclass / persistence layer).**

- `Stage` dataclass added (`task.py` L116–141): mirrors the
  TOML field set above with the same defaults.
- `Subtask.stage_id: str | None = None` added between `prompt`
  and `status`. Pre-§2 callers / replay paths produce `None`.
- `Task.stages: list[Stage] = field(default_factory=list)`
  added between `decisions` and `state_root`.
- `task_create` JSONL event now carries a `stages` payload (the
  stage records as plain dicts, parsed at replay back into
  `Stage` instances). Pre-§2 logs lacking the field replay with
  `Task.stages = []`.
- `enqueue_subtask` JSONL event now carries `stage_id` (or
  `null`). Pre-§2 logs lacking the field replay with
  `Subtask.stage_id = None`.
- `Task.enqueue_subtask` gains a kwarg-only `stage_id: str |
  None = None` parameter. Cross-stage validation lives in
  `Task.from_toml`; the operation accepts any `None` /
  non-empty-string value so direct callers (and replay) need
  not maintain the stages set separately.
- `load_task` reconstructs both new fields with a forgiving
  reader: malformed entries / non-list / wrong-type values
  default back to the pre-§2 shape rather than crashing the
  reconstruction.

**Implementation lines changed.**

- `orchestrator/task.py`: +346 / −23 (per `git diff --stat`),
  net ~+323 lines. Largest contributors are the new
  `_validate_stages` helper (~125 lines) and the `from_toml`
  extension that calls it. No behavioural changes to existing
  events / dataclass fields beyond the additive ones above.
- `tests/test_orchestrator_task.py`: +511 net — 25 new tests
  in two new classes (`TestStagesFromToml`,
  `TestEnqueueSubtaskStageId`) plus three new
  `TestModuleConstants` cases (`test_stage_default_fields`,
  `test_stage_full_construction`, `test_task_default_stages_empty`)
  plus an extension to the existing
  `test_subtask_default_status_is_pending`.
- `tests/test_orchestrator_resume.py`: +161 net — 8 new
  `TestStagesReplay` tests (round-trip, pre-§2 backward
  compat, malformed-payload tolerance for stages and
  per-subtask stage_id).
- New file `orchestrator/cases/agent-survey-2026.toml`: 22
  subtasks across 5 stages encoding the §1-picked real task;
  parses cleanly (5 stages with the
  stage5-synthesis ⇐ stages 1-4 dependency edge intact, 22
  subtasks each referencing a declared stage).

**Regression run for `synthetic-multistep`.**

The actual real-Claude spawn was deferred to §6 pre-flight
(consistent with PLAN's "real Claude Max budget will be
spent" gating; Phase 4 §7 already validated the spawn path on
this case). What §2 verified instead:

- Full `pytest` suite passes 443 / 1 deselected (Phase 4 §7
  baseline was 403 / 1 deselected; net +40 new tests, all
  green). The 13 surviving test modules — including
  `tests/test_orchestrator_loop.py`, which stubs
  `worker.spawn_worker` and exercises the real loop body —
  cover the load → policy → loop → record path.
- Static `Task.from_toml` of `synthetic-multistep.toml`
  produces the unchanged in-memory shape: 3 subtasks, no
  stages, every `Subtask.stage_id = None`. The persisted
  `task_create` event carries `stages: []`; each
  `enqueue_subtask` carries `stage_id: null`. Functionally
  equivalent to the pre-§2 schema.
- `./uas-orchestrate status synthetic-multistep` against the
  Phase 4 §7-vintage state log (commit `4754488`-era,
  pre-§2 schema with no `stages` / `stage_id` keys) replays
  cleanly through `load_task` and prints the recorded 3 done
  / 0 pending / `$0.1531` total spend / `task_resume` last
  decision. Backward-compat verified live on a real
  pre-§2 log.
- A fresh `./uas-orchestrate start agent-survey-2026
  --simulate-rate-status five_hour_pause` against a tmp
  state-root writes a 24-event log: 1 `task_create` (carrying
  the 5-stage list), 22 `enqueue_subtask` (each carrying its
  `stage_id`), 1 `policy_pause` decision; the simulated
  rate-status preempts the spawn step so no real worker
  fires. A subsequent `./uas-orchestrate status` against the
  same tmp state replays cleanly: 22 pending, 0 done, last
  decision `policy_pause`. End-to-end exercise of both the
  new write path and the new replay path.

**Substrate findings.** None new. The §1 Results' four
substrate-transition flags (IS_SANDBOX, `_parse_iso8601`
numeric resetsAt, OAuth invalid_grant noise, config_hash
"unavailable") are §3 / §6 territory — all out of §2 scope.

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

**Status:** completed

### Results

**§1-driven scope.** §1 Results' "Audit-driven ask answers" b
named the only concrete §3 deliverable: unattended auto-resume
across 5h soft-cap pauses, plus the polling-loop fallback for an
unparseable `resetsAt`. Every §3 change traces back to that
requirement; the speculative "possible additions" listed in the
PLAN sketch (per-stage policy overrides, project-level defaults,
policy inspection CLI, policy validation/linting) were
deliberately NOT implemented per the PLAN's "each addition must
trace back to a §1 task requirement" gate.

**Schema additions (TOML-level, all backward-compatible).**

1. New `[auto_resume]` section in
   `orchestrator/policy.default.toml`. Three keys, all required:
   - `enabled` (bool; default `false` — preserves the pre-§3
     "record + return" loop behaviour the
     `synthetic-multistep` pause+resume cycle relies on).
   - `fallback_seconds` (non-negative int; default `1800` =
     30 min — sleep duration when `decision["until"]` is `None`,
     i.e., `resetsAt` was missing or unparseable).
   - `max_wait_seconds` (non-negative int; default `21600` =
     6h — safety ceiling so a corrupted `until` cannot wedge the
     loop indefinitely).

2. New `orchestrator/cases/agent-survey-2026-policy.toml`
   per-task override file: `[auto_resume] enabled = true`. Other
   knobs keep the committed default. `_seed_policy_override`
   (existing Phase 3 §8 mechanism) copies it to
   `<state_root>/agent-survey-2026/policy.toml` on fresh-task
   bootstrap; `Policy.load(task_id="agent-survey-2026", ...)`
   deep-merges it over the default per the existing contract.

**Schema additions (`Policy` dataclass + helpers).**

- Three new fields on `Policy.__init__`:
  `auto_resume_enabled: bool = False`,
  `auto_resume_fallback_seconds: int = 1800`,
  `auto_resume_max_wait_seconds: int = 21600`. Defaults preserve
  pre-§3 construction shape (e.g., `test_orchestrator_loop.py`'s
  inline `Policy(...)` calls) without rewrites.
- Three new validator helpers in `orchestrator/policy.py`:
  `_require_non_negative_int` (rejects negative values) and
  `_require_subtable_bool` (sub-table bool variant of the
  existing top-level `_require_bool`); plus reuse of
  `_require_table` for the section itself.
- `Policy._from_config` extended to wire the three new fields
  via the new helpers.

**Schema additions (other code surfaces).**

- `_parse_iso8601` → `_parse_resets_at` rename in
  `orchestrator/policy.py`. The helper now accepts numeric
  (`int` / `float`, returned as `float`) **and** string
  (ISO-8601 with `Z` / `+HH:MM` / naive-UTC, parsed via
  `datetime.fromisoformat`) shapes. `bool` is rejected
  explicitly even though it subclasses `int`. Tests renamed
  `TestParseISO8601` → `TestParseResetsAt` and extended with
  five new numeric / bool / boundary cases.
- New `_compute_pause_sleep_seconds` helper in
  `orchestrator/cli.py`: pure function consumed by `_run_loop`'s
  `pause_until` branch when `auto_resume_enabled`. Computes
  sleep duration from `decision["until"]` and the policy's
  `fallback_seconds` / `max_wait_seconds`. Five test cases cover
  the four paths (until=None / until-in-past /
  until-in-future-under-cap / until-in-future-over-cap) plus
  fallback-greater-than-max-wait clamp.
- `_run_loop` extended with the auto-resume branch:
  `policy.auto_resume_enabled` true → record `policy_pause`
  carrying the sleep duration in the note, `time.sleep(...)`,
  record `policy_auto_resume`, `continue`. Disabled branch
  unchanged (record `policy_pause`, return).
- `policy_auto_resume` added to
  `orchestrator/task.py:_VALID_DECISION_KINDS` and the
  `DecisionKind` Literal.
- `integration/provenance.py:ORCHESTRATOR_VERSION` bumped from
  `"phase3"` → `"phase5"` per the existing
  "adding new kinds requires bumping ORCHESTRATOR_VERSION"
  contract. Five test pins updated in lockstep
  (`test_orchestrator_buffer_ledger.py`,
  `test_orchestrator_rate_ledger.py`,
  `test_orchestrator_resume.py`, `test_orchestrator_task.py`).
  Pre-§5 logs replay cleanly because the kind is purely
  additive — verified live by `./uas-orchestrate status
  synthetic-multistep` against the Phase 4 §7-vintage state log
  (commit `4754488`-era, `orchestrator_version: "phase3"`).

**Implementation lines changed.** `git diff --stat`:

| File | Diff |
|---|---|
| `orchestrator/policy.py` | +90 / −12 |
| `orchestrator/cli.py` | +52 / −1 |
| `orchestrator/task.py` | +2 / 0 |
| `orchestrator/policy.default.toml` | +33 / 0 |
| `orchestrator/cases/agent-survey-2026-policy.toml` | new (+24) |
| `integration/provenance.py` | +5 / −1 |
| `docs/substrate.md` | +12 / 0 |
| `docs/phase5_scope.md` | +48 / −18 |
| `tests/test_orchestrator_policy.py` | +201 / −22 |
| `tests/test_orchestrator_loop.py` | +271 / 0 |
| `tests/test_orchestrator_task.py` | +11 / −5 |
| `tests/test_orchestrator_{buffer,rate}_ledger.py` + `test_orchestrator_resume.py` | +5 / −5 (version-pin bumps only) |

Net ≈ +677 / −58 across 13 files. Largest contributors are the
two new test classes (`TestComputePauseSleepSeconds` ×6 +
`TestRunLoopAutoResume` ×4 in the loop file; ×8 new policy-load
+ validator + parse_resets_at numeric cases in the policy file).

**New test count delta.** Full pytest baseline rose 443 →
468 / 1 deselected (+25 new tests, all green; pre-§3 baseline
was Phase 4 §7's 403 + §2's +40 = 443).

- `TestParseResetsAt` (renamed): +5 tests (int/float/bool/
  zero/negative numeric coverage).
- `TestPolicyLoad`: +2 tests (per-task override flips
  auto_resume on; per-task override tunes
  fallback / max_wait).
- `TestPolicyValidator`: +8 tests (missing table,
  non-bool enabled, missing enabled, negative fallback,
  negative max_wait, float fallback, bool fallback,
  zero boundary).
- `TestComputePauseSleepSeconds` (new class): 6 tests.
- `TestRunLoopAutoResume` (new class): 4 tests
  (sleep + drain queue; pause-decision note carries sleep
  duration; auto_resume disabled records pause + returns;
  unparseable `resetsAt` uses fallback).

**Regression check.**

- Full `pytest` green: 468 passed / 1 deselected (5.66s).
  Confirms all 13 surviving test modules stay green under the
  schema additions.
- `./uas-orchestrate status synthetic-multistep` against the
  Phase 4 §7-vintage state log replays cleanly: 3 done /
  0 pending / `$0.1531` total spend / `task_resume` last
  decision. Backward-compat verified live on a real pre-§3 log
  (the log's rows still carry `orchestrator_version: "phase3"`
  but the additive `policy_auto_resume` kind doesn't appear so
  replay short-circuits cleanly).
- `agent-survey-2026-policy.toml` smoke-tested via Python
  (`Policy.load(task_id="agent-survey-2026", state_root=tmp)`
  after `_seed_policy_override`-equivalent copy):
  `auto_resume_enabled is True`, `fallback_seconds == 1800`,
  `max_wait_seconds == 21600`, `hard_stop_usd == 200.00`
  (default preserved).
- The actual real-Claude end-to-end run on `agent-survey-2026`
  is deferred to §6 pre-flight per the PLAN's "real Claude Max
  budget will be spent" gating; §3 verifies plumbing only.

**Substrate findings.**

- `docs/substrate.md` component 8: clarified `<unix ts>`
  precision is integer seconds-since-epoch (the live-worker
  shape Phase 3 §8 found, matched against the
  `synthetic-multistep/rate_limits.jsonl` artefacts: `resetsAt:
  1777724400`).
- `docs/phase5_scope.md` § "resetsAt parse mismatch" rewritten
  to record §3 closure: pre-§3 state preserved as historical
  context; post-§3 state reports the polymorphic helper plus
  the auto-resume primitive plus the substrate-doc amendment.
- The `[oauth] invalid_grant` first-spawn warning, the
  `config_hash="unavailable"` cosmetic field, the
  percentage-cap open question, and the `IS_SANDBOX=1`
  worker-side injection are all unchanged by §3 (they sit
  outside the policy / decision-kind surface this section
  touched).

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

**Status:** completed

### Results

**Writer entry point.** `orchestrator.resume_summary` (new
module, ~324 lines). Public API:

- `write_resume_summary(task, *, state_root, buffer_ledger=None)`
  — renders the digest and writes it to
  `<state_root>/<task_id>/resume_summary.md`; returns the file
  path. `buffer_ledger` is injectable for tests; production
  callers pass `None` and accept the default
  `BufferLedger(state_root=state_root)` instance.
- `render_resume_summary(task, *, buffer_ledger)` — pure
  function returning the Markdown body; separated from the
  writer so tests assert content shape without round-tripping
  through the filesystem.

Internal helpers (`_invocation_boundary`, `_count_statuses`,
`_stage_name_suffix`, `_truncate_first_line`,
`_suggest_next_action`) remain module-private.

**Format decision.** Markdown only for v1. Plain text, no new
external dependencies, renders in any editor or terminal pager.
A JSON sidecar was deferred until §6's real-task run surfaces
a programmatic consumer; the §4 PLAN explicitly listed JSON as
an optional second format and adding it speculatively would
violate "no new mechanism without an eval-visible win". The
decision is recoverable — `render_resume_summary` already
exposes the full computation as a pure function, so a JSON
sidecar can be a thin sibling later without reshaping the
writer.

**Digest sections (always emitted in this order).**

1. Header — `# Resume summary — <task_id>`, generated-at ISO
   timestamp, `**Goal.**` line.
2. `## Subtasks` — total count + per-status breakdown.
3. `### By stage` (only when `task.stages` non-empty) —
   per-stage counts; ungrouped subtasks appear under an
   `_ungrouped_` bullet if any.
4. `### Failed subtasks` (only when present) — first line of
   each failure's `result_summary`, 200-char cap.
5. `### In-flight subtasks` (only when present) — `started_at`
   timestamp.
6. `### Next pending subtask` (only when present) — first
   pending subtask's id with optional `stage_id` tag, plus a
   "... plus N more pending" tail.
7. `## Spend` — three lines: cumulative Claude-reported,
   cumulative locally-priced, and "this invocation"
   (Claude-reported, scoped to the invocation boundary).
8. `## Decisions this invocation` — every decision with
   `timestamp >= invocation_boundary`, in chronological
   order; "(none recorded yet)" placeholder when empty.
9. `## Suggested next action` — single sentence keyed off the
   last decision's kind (policy_pause → wait + resume,
   policy_wrap_up → wait for 7-day reset, policy_halt →
   review threshold, worker_fail → review failure,
   policy_auto_resume → in progress) with subtask-state
   fallbacks (queue empty + nothing in-flight + nothing
   failed → task complete; mixed → resume to continue).

**"Since last invocation" boundary.** Defined as the
timestamp of the most-recent `task_resume` decision, falling
back to `task_create` if the task has never been resumed,
falling back to `task.created_at` if neither is on the
timeline (defensive only — real tasks always have a
`task_create` row at index 0). The "Spend this invocation"
and "Decisions this invocation" sections are scoped against
this boundary; events at exactly the boundary timestamp are
included (so `task_resume` itself appears in the decision
list of the invocation it kicked off).

**Schema additions (`BufferLedger`).** One new method:
`total_spent_reported_since(task_id, *, since_iso)` — mirror
of the existing `total_spent_since` keyed on
`claude_reported_cost_usd` rather than `cost_usd`. Same
forgiving posture (missing/non-numeric
`claude_reported_cost_usd` rows contribute zero). Powers the
"this invocation" spend line; no other consumer.

**CLI wiring.** New helper
`orchestrator.cli._emit_resume_summary(task, state_root)`
calls the writer once per CLI invocation. Attached at the end
of `cmd_start`, `cmd_resume`, `cmd_status`, `cmd_pause`, and
`cmd_halt` after `_print_summary` returns. The attach point
is intentionally per-subcommand (rather than inside
`_run_loop`'s multi-return body) so the file is rewritten
exactly once per operator action regardless of how the loop
terminated. `cmd_status` remains read-only against
`task_events.jsonl` — the digest write is the only side
effect, mirroring how `cmd_status` already prints to stdout
without polluting the timeline.

**Implementation lines changed.** `git diff --numstat`:

| File | + / − |
|---|---|
| `orchestrator/resume_summary.py` | new (+324) |
| `orchestrator/cli.py` | +35 / −9 |
| `orchestrator/buffer_ledger.py` | +25 / 0 |
| `docs/orchestrator.md` | +2 / −1 |
| `tests/test_orchestrator_resume_summary.py` | new (+625) |
| `tests/test_orchestrator_loop.py` | +143 / 0 |
| `tests/test_orchestrator_buffer_ledger.py` | +89 / 0 |

Net ≈ +1243 / −10 across 7 files (PLAN.md update on top).

**Test count delta.** Full pytest baseline rose 468 → 518 / 1
deselected (+50 new tests, all green; pre-§4 baseline was
Phase 5 §3's 468).

- `TestTotalSpentReportedSince` (new class in
  `test_orchestrator_buffer_ledger.py`): 6 tests covering
  missing-file zero, equality-at-boundary, post-boundary
  inclusion, no-overlap zero, missing-cost-field skip,
  missing-timestamp skip.
- `TestRenderHeader` (new): 3 tests.
- `TestRenderSubtaskCounts` (new): 2 tests (mixed counts +
  zero-subtasks clean render).
- `TestRenderStages` (new): 5 tests covering stages-omitted /
  per-stage counts / no-name fallback / empty-stage row /
  ungrouped subtasks.
- `TestRenderSubtaskSections` (new): 7 tests across failed /
  in-flight / next-pending sections (including 200-char
  truncation + missing-summary fallback + stage-id tag).
- `TestRenderSpend` (new): 3 tests (zero spend, cumulative
  sums, invocation-scoped sums).
- `TestRenderDecisions` (new): 2 tests.
- `TestSuggestNextAction` (new): 10 tests across all decision
  kinds + queue-state fallbacks.
- `TestWriteResumeSummary` (new): 4 tests (correct path,
  state-dir auto-create, overwrite existing, default-ledger
  autoinstantiation).
- `TestInvocationBoundary` (new): 3 tests.
- `TestResumeSummaryWiring` (new in
  `test_orchestrator_loop.py`): 6 tests verifying each
  `cmd_*` writes the summary, plus a rewrite-on-each-invocation
  mtime check.
- `TestRoundTrip.test_start_pause_resume_cycle` (existing,
  extended): now also asserts `resume_summary.md` exists at
  cycle end and contains the expected header + counts.

**Acceptance verification.**

- *"`resume_summary.md` written on first soft-cap event during
  a `synthetic-multistep` test run."* Verified two ways:
  (a) `TestRoundTrip.test_start_pause_resume_cycle` (pure
  Python, stub worker) asserts file presence + header content
  after the start → pause → resume cycle; (b) live smoke run
  `./uas-orchestrate start synthetic-multistep --state-root
  <tmp> --simulate-rate-status five_hour_pause` against a
  fresh tmp state-root produces the file with the expected
  `policy_pause` decision, three pending subtasks, and the
  "Wait for the 5-hour window..." suggestion.
- *"Format spot-checked by project owner for readability /
  utility."* Two reference renders captured during §4 (both
  reproducible via `./uas-orchestrate status` against the
  existing state and via the smoke command above):
  (a) the existing-state `synthetic-multistep` digest under
  the Phase 4 §7-vintage state log — 3 done / 0 pending,
  cumulative $0.1531 reported / $0.0303 locally priced,
  invocation $0.00, suggestion "Task complete. No pending
  subtasks remain."; (b) the fresh-state `agent-survey-2026`
  digest with `--simulate-rate-status seven_day_wrap_up` —
  22 pending across 5 stages with the per-stage counts block
  rendering cleanly (8/6/4/3/1 across stages 1–5), suggestion
  "Weekly cap approaching; resume after the 7-day window
  resets...". Both renders demonstrate the §6 long-horizon
  run will see a per-stage progress view as the agent-survey
  subtasks complete; the read-only `cmd_status` path leaves
  `task_events.jsonl` untouched (verified by line-count
  comparison before/after the smoke).

**Substrate findings.** None new. The §4 wiring touches only
the CLI subcommand bodies and the new module; no rate-ledger
/ policy-machine / worker-spawn paths. The `IS_SANDBOX=1`
worker injection, `_parse_resets_at` polymorphism, OAuth
invalid_grant warning, and `config_hash="unavailable"`
cosmetic field are all unchanged. The added state file
`resume_summary.md` is reflected in `docs/orchestrator.md`
§ "Per-task state-directory layout".

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
