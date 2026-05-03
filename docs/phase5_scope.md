# Phase 5 scope audit + real-task candidate enumeration

Audit artefact for `PLAN.md` §1. Captures what Phase 3 + Phase 4
left in place vs what Phase 5's four user-facing deliverables
require, the substrate transitions worth flagging before §6, the
`docs/cut_bucket.md` re-introduction gate check, and a candidate
enumeration the project owner picks the §1 real-task target from.

Read this once at §2 start to know which schema/code surfaces are
extending vs net-new. Re-read at §6 pre-flight against the actual
task pick so any gap that didn't matter under the §1 task surfaces
before the run starts.

## Step 1 — Phase 5 deliverable audit

### Deliverable 1 — Policy configuration interface

**Exists.** `orchestrator/policy.py` (Phase 3 §5) +
`orchestrator/policy.default.toml`.

- Top-level `enabled` (master ablation flag, principle 2).
- `[five_hour] soft_cap_action` — validator restricts to `"pause"`
  only (single accepted value).
- `[seven_day] soft_cap_action` — validator restricts to
  `"wrap_up"` only.
- `[buffer] hard_stop_usd` (policy `halt` threshold).
- `[buffer] warn_usd` (informational only — `Policy.decide()`
  does NOT change action when crossed; flagged in
  `policy.default.toml` L40–43).
- `[divergence] threshold` for `RateLedger.check_divergence`.
- Per-task overrides at `<state_root>/<task_id>/policy.toml`,
  deep-merged over the committed default
  (`Policy._deep_merge` is recursive on nested dicts).
- Rule order in `Policy.decide()`: hard-stop → seven_day →
  five_hour → default (`policy.py` L267–359). First match wins.
- Policy verdict shape: `{"action": "go"|"pause_until"|
  "wrap_up"|"halt", "reason": str, "until": float|None}`.

**Gaps for Phase 5.**

- No per-stage policy overrides. Single policy object applies
  uniformly across the task.
- No per-checkpoint overrides.
- No CLI inspection (`./uas-orchestrate policy <task>` does not
  exist; `cli.py:build_parser` has no `policy` subparser).
- `warn_usd` has no consumer. Currently a config knob with no
  surface; `_run_loop` reads only the verdict's `action`, never
  cross-references the warn threshold.
- The `soft_cap_action` validator restricts each cap to one
  string value. Adding a new action verdict (e.g.,
  `"checkpoint_pause"` for §5's checkpoint primitive) requires
  extending both `_VALID_*_ACTIONS` and the rule body in
  `Policy.decide()`.
- `Policy.load()` schema-validates each load; no separate
  load-time linter.

### Deliverable 2 — Long-horizon task definition spec

**Exists.** `orchestrator/cases/<task>.toml` schema, parsed by
`orchestrator/task.py:Task.from_toml`.

Per `synthetic-multistep.toml` and `Task.from_toml`'s validator
(L200–283):

- `task_id` (required, non-empty string).
- `goal` (required, non-empty string).
- `[[subtasks]]` array of tables, each with non-empty
  `subtask_id` and `prompt`. Order = execution order.

That is the whole schema. Subtasks are linear, opaque to the
policy machine, no metadata.

**Gaps for Phase 5.**

- No multi-stage decomposition. Subtasks execute strictly in TOML
  order; no grouping.
- No dependency edges. `s2-append` cannot declare it depends on
  `s1-create`'s output.
- No per-stage metadata (expected duration, expected spend,
  expected token budget).
- No checkpoint declarations at the task-spec level.
- No resume hints. Current behaviour is "re-enqueue any
  in_flight subtask back to pending"
  (`task.py:load_task` L651–657); no way to declare richer
  resume semantics per stage.
- No per-stage policy override pointer (deliverable 1 gap also
  shows up here — the spec has no place to point at one).
- `Task.from_toml` validator + `Task` dataclass are cleanly
  extensible; each new field needs explicit validator code (not
  free-form pass-through).

### Deliverable 3 — Resume summary format

**Exists.** `orchestrator/cli.py:_print_summary` (L45–78). Stdout
only. Called by `cmd_status` / `cmd_start` (after fresh creation)
/ `cmd_resume` / `cmd_pause` / `cmd_halt`.

Surfaces:

- Task id + goal.
- Subtask counts per status (pending / in_flight / done /
  failed).
- Total spend across subtasks (sum of `Subtask.cost_usd` —
  matches Claude's reported cost since `_run_loop` populates
  `cost_usd` from `terminal.total_cost_usd`).
- Last decision (kind + note).

Underlying state available for any digest:

- `<state_root>/<task_id>/task_events.jsonl` — full timeline
  (task_create / enqueue / start / complete / fail / decision
  events). Authoritative.
- `<state_root>/<task_id>/rate_limits.jsonl` — one
  `rate_limit_event` row per worker spawn from stream-json.
- `<state_root>/<task_id>/buffer.jsonl` — paid-buffer ledger
  rows from per-call usage × pricing table (locally computed)
  plus `claude_reported_cost_usd` (Claude's own figure).
- `<state_root>/<task_id>/policy.toml` — active per-task policy
  snapshot.

**Gaps for Phase 5.**

- No persistent file output. Soft-cap pause / wrap_up / halt
  exits with no on-disk digest beyond the JSONL events
  themselves; user has to inspect raw JSONL or re-run
  `./uas-orchestrate status`.
- No "what happened since last invocation" view. Current summary
  is total-state-to-date, not delta.
- No suggested-next-action surfacing (e.g., "wait until
  `<resetsAt>` then `./uas-orchestrate resume <task>`",
  "review checkpoint X via `--ack-checkpoint`").
- `rate_limits.jsonl` data is not surfaced anywhere
  user-readable.

### Deliverable 4 — Human-checkpoint design

**Exists.** `orchestrator/task.py:record_decision` (L453–478) +
the `_VALID_DECISION_KINDS` frozenset (L81–90).

Closed allow-list of decision kinds:

- `policy_pause`, `policy_wrap_up`, `policy_halt` — written by
  `_run_loop` when the policy machine stops the loop.
- `worker_spawn`, `worker_complete`, `worker_fail` — written by
  `_run_loop` per spawn.
- `task_create` — written by `Task.from_toml` on bootstrap.
- `task_resume` — written by `load_task` on replay (the
  resume marker, suppressible via `mark_resume=False`).

Unknown kinds raise `TaskError` so the timeline cannot accumulate
free-form strings.

**Gaps for Phase 5.**

- No `checkpoint_*` decision kinds in the allow-list.
- No "checkpoint pending" state on `Task` / `Subtask`.
- No `--ack-checkpoint <id>` flag on `cmd_resume` / `cmd_start`.
- No way for a task spec (deliverable 2) to declare that a
  checkpoint should fire (e.g., before `s3-deploy` spawns).
- No checkpoint type enumeration (PLAN sketches `review-plan`,
  `review-commit`, `review-regression` — not implemented).
- `_print_summary` does not surface a pending-checkpoint state
  distinctly; "Last decision" would show it but the gate is not
  obvious from a `status` print.

### Cross-deliverable observation: regression gates

The Phase 4 §7 close reports full pytest as `403 passed / 1
deselected`. The 13 surviving test modules cover: 6 eval
modules (`test_eval_*.py`) + 7 orchestrator modules
(`test_orchestrator_*.py`). Phase 5 §2–§5 acceptance gates
should keep this at green; §6 / §7 don't run pytest as their
gate (real Claude Max budget is the gate).

The synthetic-multistep cycle is the canonical end-to-end gate
(per Phase 4 §7's smoke verification). §3 / §5 changes that
extend the policy / task model must keep
`./uas-orchestrate start synthetic-multistep` and the
pause+resume cycle green.

## Step 2 — Substrate transitions to watch on the real run

### IS_SANDBOX=1 worker-side injection (Phase 4 §7 repair)

**State.** Resolved in Phase 4 §7. `worker.py:101` injects
`IS_SANDBOX=1` per spawn (cut_bucket §6 documents the move from
image-baked to per-spawn). `setup_auth.sh:67` also has it for the
interactive auth flow. Substrate doc § component 1 already
describes the post-prune image and worker-side env injection.

### Containerfile residual COPY of orchestrator/

**Finding (new).** `Containerfile:36` runs
`COPY orchestrator/ ./orchestrator/` into the in-container `/uas/`
workdir. Under the post-pivot model the orchestrator runs on the
host; only the `claude` CLI runs inside the container (worker.py
L108–124 supplies `--entrypoint /bin/bash` and
`exec claude --print …`). The in-container copy of `orchestrator/`
has no consumer — it is an architect-era residual. Harmless
(adds image bytes, no functional effect), but worth flagging for
a future "Phase 4.5 cleanup" or §6 pre-flight if the task pick
involves rebuilding the image.

Decision deferred — flagging only, not in §1 scope to remove.

### setup_auth.sh fresh-clone error message (cut_bucket §6 surgery)

**State.** `setup_auth.sh:34–37` prints "Container image
'uas-engine:latest' not found. Build it first:" and offers two
build paths: `./uas-eval` (lazy build via the eval harness) and
`podman build -t uas-engine:latest -f Containerfile .` (manual).
On a fresh clone: user runs `./setup_auth.sh` → hits the error →
runs one of the two build paths → re-runs `./setup_auth.sh`. Path
exists; control flow correct. ✓ For §6 pre-flight: confirm the
auth dir from the prior session is still present (`ls
.uas_auth/.credentials.json`); if not, re-run setup before §6
start.

### percentage-cap question (substrate.md § Open questions)

**Status (recap).** Unresolved — Phase 2 §2 deferred to
natural-trigger capture, and Phase 4 §6 cut
`tools/statusline_probe.sh` so capture requires re-authoring the
probe.

**Does it bite §1?** Headless `claude --print` workers (the
orchestrator's only execution surface) emit only the ternary
`rate_limit_event` (`status: "allowed" | "approaching_limit" |
"limit_reached"`). They do NOT see `used_percentage`. The policy
machine consumes only the ternary. So percentage-cap-vs-climb
behaviour does not touch any Phase 5 code path so long as the §1
task is executed by `claude --print` workers (which is the only
worker primitive Phase 3 built).

**Conclusion.** Not blocking. Re-flag if Phase 6+ adds a
TUI-statusline companion read pattern.

### resetsAt parse mismatch (Phase 3 §8 hand-off finding (a))

**State (post-§3).** Closed in Phase 5 §3. `Policy._parse_iso8601`
was renamed to `_parse_resets_at` and made polymorphic over
int / float (unix epoch, the actual live-worker shape) and
ISO-8601 string (the legacy doc claim, retained as a
forward-compatibility path). The orchestrator's main loop now
consumes `decision["until"]` via the §3 auto-resume primitive.
Substrate-doc shape line in `docs/substrate.md` component 8 was
amended to spell out that `<unix ts>` is an integer
seconds-since-epoch.

**Pre-§3 state (preserved for context).** `_parse_iso8601`
returned `None` for numeric `resetsAt`. Phase 3 §8 declared this
harmless because `_run_loop` recorded the pause decision and
exited — it did not consume `pause_until.until`.

**Did it bite §1?** Yes — the §1 task acceptance criteria
include unattended multi-window operation (project owner
unavailable for input across the multi-day run; per §1 Results'
ask "b"). §3 closed the gap so the §1 task's
`agent-survey-2026-policy.toml` can opt into
`[auto_resume] enabled = true` and have the loop sleep until
`resetsAt` (or the `fallback_seconds = 1800` polling-loop
fallback if the timestamp is unparseable) without operator
intervention.

**§6 pre-flight implication.** Auto-resume sleep durations
beyond the typical 5h cap window are clamped to
`max_wait_seconds = 21600` (6h) so a corrupted `resetsAt` cannot
wedge the loop. If the live trace shows a sleep of exactly the
clamp value, that's the malformed-timestamp branch firing —
inspect `rate_limits.jsonl` for the offending event.

### OAuth invalid_grant noise on first spawn (Phase 3 §8 finding (b))

**State.** `[oauth] Self-refresh HTTP 400: invalid_grant` may
appear in stderr on the first worker spawn of a session; the
4-stage refresh fallback recovers transparently. Documented
output, not a failure mode.

**§1 implication.** If the project owner watches the live trace
during §6, expect this on the first spawn and ignore unless a
subsequent spawn also fails.

### config_hash="unavailable" (Phase 4 §7 finding)

**State.** `integration/provenance.py:_hash_active_config` falls
through to "unavailable" because `uas_config.py` is gone. JSONL
rows record `config_hash: "unavailable"` permanently.

**§1 implication.** No effect on policy or scheduling. Cosmetic
only — the field is preserved for forward-compat / future
config-source restoration.

## Step 3 — `docs/cut_bucket.md` re-introduction gate audit

The gate (§ "How to read this in the future") requires three
checks for any "should we add X back?": cut_list classification,
relevant § rationale, and the Phase 6+ standing rules.

Walking each Phase 5 deliverable against the gate:

| Deliverable | Gate verdict | Reasoning |
|---|---|---|
| Resume summary writer | **Pass — net-new** | Reads existing JSONL state; writes a NEW Markdown digest file. Nothing in cut_bucket maps to it. |
| Human-checkpoint primitive | **Pass — net-new** | Cut Cluster F (TDD pair) was distinct (architect-side TDD enforcement). Checkpoint = pause-and-ack on declared boundaries; orchestrator-side concept that didn't exist pre-prune. |
| Policy refinements (per-stage / per-checkpoint overrides) | **Pass — extension** | Extends Phase 3 §5 post-pivot infrastructure, not a re-intro. Cluster D (validation cascade) was conceptually adjacent but architect-coupled and did not consume rate-limit signals. |
| Task spec extensions (multi-stage, deps, metadata) | **Pass — extension** | Post-pivot model is human-authored decomposition. Adding deps to `[[subtasks]]` lets the user express richer manual structure; does NOT re-introduce automated decomposition (`architect/planner.py`'s 20 mechanisms). |

**Tempting re-introductions Phase 5 must NOT propose without
explicit gate clearance** (catalogued so §3–§5 step authoring
doesn't accidentally drift in):

- LLM-as-judge for run verification → re-introduces
  `integration/llm_judge.py` (cut §5).
- Coverage-driven decomposition / multi-plan voting / TDD
  planning → re-introduces `architect/planner.py` mechanisms
  (cut §3).
- TDD enforcement at execution time → re-introduces
  `architect/main.py` Cluster F (cut §3).
- Reflection / counterfactual / classify-error for failure
  analysis → re-introduces Cluster A / B (cut §3).
- Best-of-N spawn (multiple workers per subtask, vote on output)
  → re-introduces Cluster C (cut §4).
- Layered config loader → re-introduces `uas_config.py` (cut §6).
- Lifecycle hook system → re-introduces `uas_hooks.py` (cut §6).
- Architect-runner shell scripts → re-introduces the 5 shell
  scripts (cut §6).

If any §3–§5 step drift toward these, stop and route through the
gate explicitly.

## Step 4 — Real-task candidate enumeration

Each candidate sketches scope / timeline / budget. Numbers are
order-of-magnitude estimates from looking at the orchestrator's
per-spawn cost on the synthetic-multistep cycle (~$0.04 / subtask
on Haiku 4.5, the CLI default for `claude --print`); real tasks
will differ by 10× or more depending on context size and
verification overhead.

The project owner picks one (or names a different task entirely).

| # | Candidate | Decomposition shape | Est. timeline | Est. budget | Verifiability |
|---|---|---|---|---|---|
| A | Migrate a small open-source service from Flask → FastAPI in a sandbox repo | 5–10 subtasks: scope analysis → endpoint translation per route → DI migration → test rewrite → smoke validation | 2–3 days, 2–3 window boundaries | $5–$15 | High — original tests must still pass |
| B | Add a CRUD feature end-to-end in a test project (DB schema → API → frontend wiring → tests) | 8–15 subtasks across DB, backend, frontend, tests | 2–4 days, 3–5 window boundaries | $10–$25 | High — feature smoke test |
| C | Generate per-module docstrings + an architecture README for an undocumented small repo | 1 subtask per module, parallel-friendly serial execution | 1–2 days, 1–2 window boundaries | $3–$10 | Medium — `pydocstyle` / lint pass + human review of README |
| D | Reproduce a small ML paper's experiments (data → model → eval → report) | 4–8 subtasks: env → data → model → train → eval → write-up | 2–5 days, 2–5 window boundaries | $15–$40 | Medium — numbers within reported tolerance |
| E | Bug-fix campaign across N issues from an open-source tracker (each subtask = 1 issue) | N subtasks, N = 5–15 | 3–7 days, 4–10 window boundaries | $10–$50 | High — CI green per PR |
| F | UAS-on-UAS: implement Phase 5 §3–§5 (policy refinements / resume summary / checkpoint primitive) using the orchestrator itself | 3–6 subtasks aligned with §3–§5 sections | 1–2 days, 1–3 window boundaries | $5–$15 | High — `pytest` 403 + new section tests must pass |
| G | Long literature review / synthesis on a research topic (one paper per subtask, final synthesis subtask) | 8–20 subtasks | 2–4 days, 2–4 window boundaries | $20–$60 | Low — human judgement on synthesis quality |
| H | Refactor campaign across UAS itself (e.g., extract a shared helper, rename across files) | 3–8 subtasks | 1 day, 0–1 window boundaries | $2–$8 | High — full pytest stays green |

**Selection criteria the project owner should weigh.**

1. **Multi-day duration** is required by ROADMAP §Phase 5 exit
   criteria — at least one window boundary, ideally
   weekly-cap-adjacent.
2. **Verifiability matters** — the experience write-up needs a
   concrete "did this work?" signal. Low-verifiability
   candidates (G) are valid but the post-run write-up will
   lean on subjective assessment.
3. **Budget tractability** — $200 hard-stop is the policy
   default; pick a candidate whose expected budget sits
   comfortably below that with margin for cost overruns.
4. **Recursivity caveat** — F (UAS-on-UAS) is elegant but risks
   a chicken-and-egg failure mode (orchestrator bug surfaces as
   task failure with no clear attribution). Worth doing
   eventually but possibly not as the first Phase 5 validation.
5. **Sandbox scope** — every candidate should run against a
   workspace that is NOT a checked-out copy of UAS itself
   (workspace isolation pattern is per-task; let the
   orchestrator's per-task workspace be the only writeable
   surface).

## Audit findings outside the four-deliverable scope

These came up during the read-only audit and are not in §1's
scope to fix. Logged here so they're visible and can be
addressed (or explicitly deferred) at §6 pre-flight or in a
later phase.

### Pre-prune scratch files at repo root

`check_environment.py`, `run_test_verification.py`,
`test_goal_result.py`, `test_goal_verify.py`,
`verify_test_goal.py`, `test_verification_result.txt`,
`test_goal_output.txt` (~520 lines + 2 fixture txts).

All architect-era — references to `WORKSPACE` env var,
`UAS_RESULT` JSON convention, `coverage_matrix` /
`replan_protection` / `split_coupled` / `planner` test files
(none of which survive). Not on `cut_list.md` (a Phase 4 §6
omission). No keep-list rationale found in `cut_bucket.md`.

**Recommendation.** Default-cut under the same Phase 6+ rule
that drove §6 (no consumer post-prune). Out of §1 scope; flag
to project owner at the §1 user-input gate to confirm before
removal in a follow-up commit (could be a §1.5 mini-cleanup
before §2, or could wait until a future Phase 4.5 / Phase 6+
dedicated cleanup).

### Empty `architect/` and `uas/` package directories

Both contain only stale `__pycache__/`. Trivial cleanup;
out of §1 scope.

### Documentation file count

`docs/` carries 5 files now (`substrate.md`,
`orchestrator.md`, `cut_bucket.md`, `cut_list.md`,
`cut_surface.md`). `cut_surface.md` is Phase 2 §4's sized
estimate, finalised by `cut_list.md` in Phase 4 §1; the two
files have overlapping content. Not in scope to consolidate;
flag for Phase 6+ housekeeping.
