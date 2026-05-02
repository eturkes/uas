# PLAN — Phase 3: Orchestrator core

Phase reference: `ROADMAP.md` §Phase 3 — Orchestrator core (under
the May 2026 pivot — see ROADMAP §Pivot for context).

Substrate references: `docs/substrate.md` (the Phase 1 keep-list
plus the Phase 2 §1 rate-limit read pattern), `docs/cut_surface.md`
(what Phase 4 will remove and therefore what Phase 3 must avoid
depending on).

This phase exists to build the orchestrator that owns long-horizon
task state, spawns headless Claude Code workers, and enforces a
three-state policy machine against real Claude Max usage signals.
The end-state is a callable orchestrator that survives an invocation
boundary; not a polished UX (Phase 5) and not a slim codebase
(Phase 4).

This PLAN is a draft. Per the project's decision protocol, Phase 3
work does not begin until the project owner has reviewed and
approved this file.

## Phase exit criteria (from ROADMAP.md)

The orchestrator runs a defined long-horizon task end-to-end across
at least one window boundary (5h pause + resume, or weekly wrap +
resume next week — for §8 the boundary is simulated rather than
waited for) without human intervention, with all ledgers and state
cleanly persisted.

## Scope discipline (non-negotiable)

- **No Phase 4 deletions.** The architect / planner /
  cut-orchestrator modules from `docs/cut_surface.md` remain in the
  tree. Phase 3 must not import from them or grow new dependencies
  on them. Phase 4 deletes them.
- **No new self-correction mechanism.** The orchestrator is
  scheduling-and-budgeting infrastructure; it does not retry,
  reflect on, or re-plan worker outputs. Worker output is treated
  as authoritative for the duration of Phase 3.
- **Ablatability where it matters.** The policy machine ships with
  a config-controlled disable to satisfy CLAUDE.md core principle
  2; ledger and worker primitives are data / infrastructure rather
  than "mechanisms" in the Phase 0 sense and are not separately
  ablatable.
- **No real-task run in this phase.** §8 uses a synthetic
  multi-subtask task with a `--simulate-rate-status` override so
  the window boundary triggers within minutes. Real long-horizon
  runs are Phase 5's deliverable.
- **No README rewrite in this phase.** The README is on Phase 4's
  deliverable list; Phase 3 may add `docs/orchestrator.md` for
  design notes only.
- **Sections are linear.** §1 produces the skeleton + shared
  helpers; §2 the worker primitive; §3 + §4 the two ledgers
  consuming §2's output; §5 the policy machine consuming both
  ledgers; §6 + §7 the task-state and resume layer; §8 ties it
  together end-to-end.

## Section 1 — Skeleton & substrate extraction

**Goal.** Establish module layout, the entry-point wrapper, design
notes, and the two shared-helper modules (`auth.py`, `provenance.py`)
extracted from `integration/eval.py` per `docs/substrate.md` §2 and
§4. Resolves all "Phase-3 design start" decisions deferred from the
substrate doc.

**Decisions committed for execution (subject to project-owner
review at PLAN approval; revise here before §1 runs if any need
adjustment).**

- **Module location:** `orchestrator/` (top-level; coexists with
  `orchestrator/sandbox.py` (KEEP) and `orchestrator/main.py`
  (CUT-in-Phase-4) for the duration of Phase 3). New files do
  **not** import from `orchestrator/main.py`; Phase 4's deletion of
  that file leaves the new code intact. Long-term name-match with
  ROADMAP / substrate doc terminology.
- **Entry point:** `uas-orchestrate` shell wrapper at repo root,
  parallel to `uas-eval`. Wrapper sets `PYTHONPATH=<repo>` and
  `exec`s `python3 -P -m orchestrator.cli "$@"`. CLI subcommands:
  `start <task>`, `status <task>`, `resume <task>`, `pause <task>`,
  `halt <task>`. No long-running daemon process; each invocation
  runs to the next policy boundary or task completion.
- **Rate-limit read pattern:** option 1 (ternary-only) per
  `docs/substrate.md` §8 and ROADMAP §Pivot. The orchestrator
  parses `rate_limit_event` from each worker's own stream-json
  output. Options 2 (TUI companion) and 3 (internal accumulator)
  are deferred to Phase 5+ if real-task experience shows option 1's
  transitions feel too sharp.
- **Version key:** new `ORCHESTRATOR_VERSION = "phase3"` constant
  in `integration/provenance.py`, recorded as a separate provenance
  key alongside `harness_version`. `HARNESS_VERSION` stays at
  `"phase1"` so prior eval JSONL rows remain resume-eligible
  (substrate doc §4 + §6 gaps).
- **Auth / provenance lift target:** `integration/auth.py` and
  `integration/provenance.py` as peer modules in the existing
  `integration/` package. Avoids re-rooting; Phase 4 may relocate
  later. Eval imports from them; orchestrator imports from them.
- **NEEDS-PHASE-3-DECISION trio (`uas_config.py`, `uas_hooks.py`,
  `uas.example.toml`):** deferred to natural-trigger — decide when
  the first orchestrator section actually wants to consume one of
  them. If §2-§8 complete without consumption, all three default
  to CUT for Phase 4. (Initial expectation: orchestrator uses small
  task-specific TOML files and `tomllib`; the layered loader is
  not consumed; trio defaults to CUT.)

**Steps.**

1. Create `orchestrator/cli.py` stub with argparse skeleton for the
   five subcommands (`start`, `status`, `resume`, `pause`, `halt`).
   Each subcommand body raises `NotImplementedError` for now;
   `--help` works. Create `orchestrator/__init__.py` if missing.
2. Create the `uas-orchestrate` wrapper at repo root. Mirror
   `uas-eval`'s shape: shebang, `PYTHONPATH` export, `UAS_HOST_UID`
   / `UAS_HOST_GID` exports, `exec python3 -P -m orchestrator.cli
   "$@"`. `chmod +x`.
3. Create `integration/auth.py`. Move `_OAUTH_REFRESH_BUFFER`,
   `_DEFAULT_CLAUDE_CREDS`, `_OAUTH_TOKEN_ENDPOINT`,
   `_OAUTH_CLIENT_ID`, `_read_token_expiry`, `_self_refresh_oauth`,
   `_maybe_refresh_oauth` from `integration/eval.py`. Make
   `_OAUTH_CLIENT_ID` a module-level default with optional
   `UAS_OAUTH_CLIENT_ID` env override and a stderr warning if it
   diverges from the value embedded in
   `~/.claude/.credentials.json` metadata (substrate doc §2 Gap).
4. Create `integration/provenance.py`. Move `HARNESS_VERSION`,
   `_SECRET_ENV_PATTERN`, `_git_capture`, `_hash_active_config`,
   `capture_run_metadata` from `integration/eval.py`. Add
   `ORCHESTRATOR_VERSION = "phase3"` and a
   `capture_run_metadata(*, include_orchestrator_version=False)`
   keyword so eval keeps its existing schema and orchestrator
   callers opt in.
5. Update `integration/eval.py` to import the moved helpers from
   `integration.auth` and `integration.provenance` instead of
   defining them locally. Keep the import-site call signatures
   identical.
6. Run `./uas-eval` against the hello-file smoke case. Confirm a
   well-formed JSONL row + aggregate are produced (the amended
   Phase 1 exit criteria; `passed=false` for the case body itself
   is acceptable per §10's recorded scaffold-ceiling outcome — the
   regression we're checking for is the substrate plumbing).
7. Confirm any test under `tests/` that imports or monkeypatches
   the moved helpers' qualified names still resolves them.
   Grep-survey `tests/` for `_maybe_refresh_oauth`,
   `_self_refresh_oauth`, `_read_token_expiry`,
   `capture_run_metadata`, `_git_capture`, `_hash_active_config`,
   `HARNESS_VERSION`. Update tests that reference the old
   `integration.eval.<name>` paths to either import from the new
   modules or point at the eval re-exports.
8. Write `docs/orchestrator.md` (~50–100 lines): the
   decision-summary bullets from this section, the package-layout
   diagram, the subcommand catalog, the per-task state-directory
   layout (`<repo>/orchestrator/state/<task_id>/{task_events,
   rate_limits, buffer}.jsonl` plus optional `policy.toml`), and
   an open-questions list (initial entry: NEEDS-PHASE-3-DECISION
   trio fates).

**Acceptance.**

- `orchestrator/cli.py`, `uas-orchestrate`, `integration/auth.py`,
  `integration/provenance.py`, `docs/orchestrator.md` all exist.
- `./uas-eval` runs end-to-end and produces a well-formed JSONL
  row + aggregate (no plumbing regression from the substrate
  lift).
- `./uas-orchestrate --help` prints the subcommand list.
- `tests/` is green.

**Results.**

- All five new artefacts created: `orchestrator/cli.py` (97 lines,
  argparse skeleton, every subcommand body raises
  `NotImplementedError` with a §-tag), `uas-orchestrate` (executable
  shell wrapper mirroring `uas-eval`), `integration/auth.py` (~190
  lines — 7 OAuth helpers + the new `_check_client_id_divergence`
  warning fed by the `UAS_OAUTH_CLIENT_ID` env override per
  substrate §2 Gap), `integration/provenance.py` (~130 lines — 5
  helpers + the new `ORCHESTRATOR_VERSION = "phase3"` plus the
  `include_orchestrator_version=False` keyword), `docs/orchestrator.md`
  (119 lines — committed §1 decisions, package layout, subcommand
  catalog, per-task state-directory layout, open-questions list).
- `integration/eval.py` no longer defines the lifted helpers; it
  re-exports the new module names so existing callers and
  monkeypatch targets (`ev.HARNESS_VERSION`, `ev.capture_run_metadata`,
  `ev._git_capture`, `ev._hash_active_config`, `ev._maybe_refresh_oauth`,
  `ev._self_refresh_oauth`, `ev._read_token_expiry`,
  `ev._SECRET_ENV_PATTERN`, `ev._OAUTH_*`, `ev._DEFAULT_CLAUDE_CREDS`)
  resolve unchanged. No tests modified.
- `./uas-orchestrate --help` prints the five-subcommand list with
  task-id positional argument on each.
- Full `tests/` suite green: 1806 passed, 3 deselected, 4 min 51 s.
- `./uas-eval -k hello-file` end-to-end on commit `2f3f32b` (dirty,
  this PLAN edit + new files) produced one well-formed JSONL row +
  aggregate. Wallclock 726.0 s (12.1 min); LLM time 423.7 s; 4
  attempts; 36 input / 41,502 output tokens. Outcome
  `passed=False` (FAIL) — architect over-decomposed into 2 TDD
  steps both reporting `failed`, blocked status, never produced
  `hello.txt`. Same scaffold-ceiling failure mode §10 surfaced;
  acceptable per the substrate-plumbing-regression scope of this
  step. New JSONL row carries `harness_version="phase1"` only
  (`orchestrator_version` correctly absent from the eval row, per
  the include-flag opt-in design). OAuth refresh path
  (`integration.auth._maybe_refresh_oauth`) exercised cleanly
  end-to-end: stage-2 self-refresh hit a stale `invalid_grant`,
  stage-4 `claude -p ping` fallback succeeded → 4.2 h remaining.
- NEEDS-PHASE-3-DECISION trio still untouched (no §1 consumer);
  default-CUT trajectory holds for now per the §1 decision summary.

**Status:** completed

## Section 2 — Headless-worker primitive

**Goal.** Build the function that spawns a sandboxed headless
`claude --print --dangerously-skip-permissions --output-format
stream-json --verbose <prompt>` invocation, captures the stream-json
output line-by-line, parses the relevant events, and returns a
structured result. Per `docs/substrate.md` §1, Phase 3 calls
`podman run` directly with the engine image (`uas-engine:latest`);
it does not use `orchestrator/sandbox.py::run_in_sandbox`.

**Steps.**

1. Create `orchestrator/worker.py` with the public API:
   `spawn_worker(prompt: str, *, workspace: str, task_id: str,
   subtask_id: str, timeout_seconds: int | None = None) -> dict`.
2. Create `orchestrator/container.py` with
   `ensure_engine_image() -> None`. Builds `uas-engine:latest`
   from `Containerfile` if absent (substrate doc §1 Gap: "Phase 3
   needs an explicit 'image is ready' precondition"). Idempotent.
3. Refresh OAuth before the spawn by calling
   `integration.auth._maybe_refresh_oauth()`. Required for
   long-running orchestrator sessions whose first worker may fire
   well after the last refresh.
4. Spawn the worker via `subprocess.Popen` invoking `podman run`
   with: `--rm`, `--storage-driver=vfs`, `--network=host`
   (mirrors `orchestrator/sandbox.py` precedent),
   `--name uas-orchestrator-<task_id>-<subtask_id>-<8 hex>`,
   `--volume <workspace>:/workspace:Z` (the `:Z` SELinux relabel
   per substrate doc §5 Gap),
   `--volume <repo>/.uas_auth:/root/.claude:Z`,
   `uas-engine:latest`, then the
   `claude --print --dangerously-skip-permissions
   --output-format stream-json --verbose <prompt>` arguments.
5. Read stdout line-by-line. Each line is one JSON object
   (stream-json contract). Parse and accumulate:
   - `rate_limit_event` → list, returned as-is for §3.
   - `result` (terminal; carries `usage`, `total_cost_usd`,
     `terminal_reason`, `model`) → returned as-is for §4 / §5.
   - `assistant` / `user` / `tool_use` / `tool_result` →
     buffered for an output-text reconstruction.
   - Unknown event types → preserved in `raw_lines` but not
     parsed.
6. On timeout: fire-and-forget `podman kill <name>` and
   `podman rm -f <name>` (mirrors sandbox.py); set
   `result={"terminal_reason": "timeout", ...}`; return.
7. Return shape: `{"exit_code": int, "rate_limit_events":
   list[dict], "result": dict | None, "output": str, "raw_lines":
   list[str]}`. `exit_code` is the `claude` process exit;
   `result` is the parsed terminal `result` line (or `None` on
   hard failure); `output` is the reconstructed assistant-text
   body.
8. Unit test under `tests/test_orchestrator_worker.py`: spawn a
   `--print "Reply with the literal word: done"` worker against
   an ephemeral workspace; assert `exit_code == 0`,
   `result["usage"]["input_tokens"] > 0`,
   `len(rate_limit_events) >= 1`, `"done"` substring in `output`.
   Verify no stale containers survive
   (`podman ps -a --filter name=uas-orchestrator-*`).

**Acceptance.**

- `orchestrator/worker.py` and `orchestrator/container.py` exist.
- The unit test passes against a real Claude Code invocation
  (Opus 4.7).
- Test cleanup leaves no stale `uas-orchestrator-*` containers.
- Engine image is built on first call if absent and reused on
  subsequent calls.

**Results.**

- `orchestrator/container.py` (~80 lines) ships a narrow
  `ensure_engine_image()` that builds `uas-engine:latest` from
  `<repo>/Containerfile` only when the image is absent locally,
  plus `find_engine()` (podman preferred, docker fallback) and
  an `EngineUnavailable` error type. Staleness vs source-mtime
  is **not** rechecked here (eval.py's `_ensure_image` keeps
  that role for the eval harness); the orchestrator's narrower
  contract is "image is available before spawn".
- `orchestrator/worker.py` (~190 lines) ships `spawn_worker(
  prompt, *, workspace, task_id, subtask_id, timeout_seconds=
  None) -> dict` returning the documented shape `{exit_code,
  rate_limit_events, result, output, raw_lines}`. OAuth refresh
  fires before every spawn via
  `integration.auth._maybe_refresh_oauth()`; the engine-image
  precondition follows. The container name is
  `uas-orchestrator-<safe(task_id)>-<safe(subtask_id)>-<8 hex>`
  via a small `_safe_segment()` sanitiser. Stream-json output
  drains on a background thread so a long-running spawn cannot
  block the OS pipe buffer; on timeout the container is killed
  via fire-and-forget `kill` + `rm -f` (mirrors
  `orchestrator/sandbox.py`) and `result.terminal_reason` is
  set to `"timeout"`.
- **Engine-flag deviation from PLAN literal.** PLAN step 4
  prescribed `--storage-driver=vfs` "mirrors `orchestrator/
  sandbox.py` precedent". That flag is podman-specific; on this
  host only docker is on PATH and docker rejects the flag
  (whereas `integration/eval.py`'s container path already runs
  on docker without it). Resolved with a tiny `_engine_prefix()`
  helper that emits `--storage-driver=vfs` only when the binary
  basename is `podman`. Substrate doc §1 phrases this surface
  as "podman/docker run" (engine-agnostic) so the deviation
  preserves PLAN intent. New unit test
  `test_docker_omits_storage_driver_flag` pins the gate.
- **Worker entrypoint shape.** `Containerfile` declares
  `ENTRYPOINT ["/uas/entrypoint.sh"]`; that script only forwards
  argv when `UAS_TASK` / `UAS_GOAL` / `UAS_GOAL_FILE` is set
  (otherwise it launches interactive `claude` or runs
  `architect.main`). Worker overrides with `--entrypoint
  /bin/bash` plus a one-line shell payload that (a) installs a
  `chown -R $UAS_HOST_UID:$UAS_HOST_GID /workspace` EXIT trap
  when the host UID is non-root, mirroring entrypoint.sh, and
  (b) `exec`s `claude --print --dangerously-skip-permissions
  --output-format stream-json --verbose "$UAS_WORKER_PROMPT"`.
  The prompt is forwarded via env var rather than as a shell
  positional so callers do not have to worry about quoting
  arbitrary text. `cd /workspace` is set so claude defaults its
  cwd to the bind-mounted workspace.
- `tests/test_orchestrator_worker.py` (~140 lines, 8 tests).
  Pure-Python helper coverage (run on every CI invocation):
  `TestSafeSegment` × 4 (alnum passthrough, unsafe-run collapse,
  empty fallback, leading/trailing strip), `TestBuildCommand` ×
  3 (required flags, host UID forwarding, docker storage-driver
  gate). Live integration coverage (`@pytest.mark.integration`):
  `TestSpawnWorkerLive::test_trivial_done_prompt` spawns the
  PLAN-prescribed `Reply with the literal word: done` worker,
  asserts `exit_code == 0`, `usage.input_tokens > 0`,
  `len(rate_limit_events) >= 1`, `"done"` substring in
  `output.lower()`, and that no `uas-orchestrator-*` containers
  survive afterwards.
- **Live spawn confirmation (Opus 4.7).** Manual one-shot
  invocation against an ephemeral `tmp_path` workspace returned
  the documented shape end-to-end. Wallclock 2.89 s; claude
  self-reported `duration_ms=1475`, `duration_api_ms=2318`.
  Captured 4 raw stream-json lines (`system/init`,
  `rate_limit_event`, `assistant`, `result`). Terminal `result`
  carried `usage = {input_tokens: 6, cache_read_input_tokens:
  19903, output_tokens: 5, ...}` and `total_cost_usd =
  0.010512`. Single `rate_limit_event` with
  `rate_limit_info.status="allowed"`, `rateLimitType="five_hour"`,
  `isUsingOverage=false`, plus `overageStatus`/`overageResetsAt`
  fields — exactly the schema substrate doc §8 documents and §3
  will consume. Output text reconstructed cleanly to `'done'`.
- **§4 hand-off note.** `result["model"]` is `None`. Claude
  Code emits the active model id under `result["modelUsage"]`
  (keyed by model id, e.g. `claude-opus-4-7`) and on every
  `assistant` event's `message.model` field. §4's
  `BufferLedger.record` must therefore source the model id
  from `next(iter(result["modelUsage"]))` (or one of the
  assistant messages) rather than from a top-level `model`
  key. Worker's return shape is unchanged; the consumer in §4
  picks the right key.
- **Test outcomes.** `python3 -m pytest
  tests/test_orchestrator_worker.py -x -q` → 7 passed, 1
  deselected (the live test). `python3 -m pytest
  tests/test_orchestrator_worker.py::TestSpawnWorkerLive -m
  integration -s -v --timeout=600` → 1 passed in 2.66 s.
  `python3 -m pytest tests/ -q --timeout=120` → 1813 passed, 4
  deselected, 5 m 02 s — +7 over §1 baseline (1806) matching
  the seven new helper tests; no regression on existing tests.
  `docker ps -a --filter name=uas-orchestrator-` → empty after
  every run (cleanup contract honoured).
- **Engine image lifecycle.** Pre-existing
  `uas-engine:latest` image was reused; conftest.py's
  session-scoped `uas_engine` fixture rebuilt once during the
  integration session because new Phase 3 source files (the §2
  module additions) bumped the source mtime past the image's
  build time. `orchestrator.container.ensure_engine_image()`
  itself was a no-op because the image was present at every
  call site; the absent-build path is exercisable by removing
  the image (`docker rmi uas-engine:latest`) before the next
  run.
- **NEEDS-PHASE-3-DECISION trio status.** Still untouched —
  §2 consumed neither `uas_config.py`, `uas_hooks.py`, nor
  `uas.example.toml`. Default-CUT trajectory continues to hold
  per the §1 decision summary.

**Status:** completed

## Section 3 — Usage-limit ledger

**Goal.** Persist `rate_limit_event` payloads from §2's worker
into a JSONL ledger, expose `current_status()`, and implement the
message-count-divergence sanity check per ROADMAP.

**Steps.**

1. Create `orchestrator/rate_ledger.py`. Define a `RateLedger`
   class:
   - `record(events: list[dict], *, run_metadata: dict, task_id:
     str) -> None`. Appends one row per event to
     `<repo>/orchestrator/state/<task_id>/rate_limits.jsonl`,
     stamped with `run_metadata` (incl. `ORCHESTRATOR_VERSION`)
     and a synthetic `event="rate_limit"` discriminator
     (substrate doc §3 layout decision: per-task file rather
     than shared eval log; simpler resume).
   - `current_status(task_id: str) -> dict`. Reads the file
     (forward-only scan; small files, no need for reverse seek);
     returns the most recent event per `rateLimitType`. Shape:
     `{"five_hour": {status, resetsAt, isUsingOverage,
     overageStatus, overageResetsAt} | None, "seven_day": same |
     None}`. Missing-file → both keys `None`.
   - `internal_count(task_id: str) -> int`. Tally of recorded
     `rate_limit_event` rows since the most recent
     `current_status().five_hour.resetsAt` boundary. Used by the
     divergence check below.
2. Implement the divergence-check helper
   `check_divergence(task_id: str, *, threshold: int) -> bool`.
   Returns `True` if `internal_count` exceeds `threshold` while
   `current_status().five_hour.status` is still `"allowed"` —
   indicating the recorded rate-limit signal has fallen behind
   reality. Logs a stderr alert when triggered.
3. Wire §2's `spawn_worker` to call `rate_ledger.record(events,
   ...)` after each worker completes (regardless of exit code —
   events emitted before timeout are still useful).
4. Unit tests under `tests/test_orchestrator_rate_ledger.py`:
   - Synthetic event sequences (`five_hour` only; `five_hour`
     plus `seven_day`; mixed buffer states with
     `isUsingOverage`).
   - `current_status()` reflects the latest event per
     `rateLimitType`.
   - `internal_count()` resets correctly across `resetsAt`
     boundaries.
   - `check_divergence()` fires only when expected.

**Acceptance.**

- Per-event rows persisted under
  `<repo>/orchestrator/state/<task_id>/rate_limits.jsonl`.
- `current_status()` returns correct snapshots on synthetic
  fixtures.
- Divergence check triggers on synthetic test conditions.
- Tests pass.

**Results.**

- `orchestrator/rate_ledger.py` (~190 lines) ships the `RateLedger`
  class with `record`, `current_status`, `internal_count`,
  `check_divergence`, plus the `DEFAULT_STATE_ROOT` module
  constant and an internal `_iter_events` reader. The class is a
  stateless wrapper bound to a `state_root` path at construction
  time so tests can redirect persistence to a `tmp_path` rather
  than monkeypatch a module global.
- **Row schema decision (preserved verbatim).** Each persisted
  row is `{**run_metadata, "event": "rate_limit", "task_id":
  <id>, "rate_limit_event": <original event>}` — the upstream
  stream-json event nests under one key rather than being
  flattened, so the §2 schema (`type`, `uuid`, `session_id`,
  `rate_limit_info.{status,resetsAt,rateLimitType,overageStatus,
  overageResetsAt,isUsingOverage}`) round-trips intact and
  future schema additions land without a migration.
- **`internal_count` window semantics.** Counts five_hour events
  whose own `rate_limit_info.resetsAt` matches the most-recent-
  seen value among five_hour rows. When the 5h window flips (new
  event with a larger `resetsAt`), older-window rows drop out and
  the count effectively "resets" — the behaviour the PLAN's
  "internal_count() resets correctly across resetsAt boundaries"
  acceptance asks for. seven_day events are excluded from the
  tally (the divergence check is scoped to the 5h cap).
- **`check_divergence` is strict-greater-than (`>`).** PLAN
  wording was "exceeds threshold"; chose `>` not `>=`, pinned by
  `test_threshold_at_count_does_not_fire`. Fires only when both
  conditions hold: latest five_hour `status == "allowed"` AND
  in-window `internal_count > threshold`. Stderr alert format is
  `[rate_ledger] divergence: task_id=<id> internal_count=<n> >
  threshold=<t> while five_hour.status='allowed'; ...`.
- **Wire-in (PLAN step 3).** `orchestrator/worker.py::spawn_worker`
  gained a `state_root: str | None = None` kwarg and a single
  post-spawn call to `RateLedger(state_root=state_root).record(
  rate_limit_events, run_metadata=metadata, task_id=task_id)`
  with `metadata = provenance.capture_run_metadata(
  include_orchestrator_version=True)`. The call lives between the
  `proc.wait` / timeout block and the return statements so events
  emitted before a timeout (or before a hard failure with no
  terminal `result`) are still durably persisted — matches the
  PLAN's "regardless of exit code" wording. Worker rows therefore
  carry both `harness_version` and `orchestrator_version` (eval
  rows continue to carry `harness_version` only, per the
  Phase 3 §1 opt-in design).
- **§2 live test updated.** `tests/test_orchestrator_worker.py`'s
  `TestSpawnWorkerLive::test_trivial_done_prompt` now passes
  `state_root=str(tmp_path / "state")` so the live spawn no
  longer writes to `<repo>/orchestrator/state/`. No other §2
  tests touched; the helper-test surface is unchanged.
- **`.gitignore` updated.** Added `orchestrator/state/` under a
  new "Orchestrator per-task runtime state (Phase 3 §3+)"
  section. First time anything is persisted under that path, so
  the rule lands here rather than waiting for §6 / §7.
- `tests/test_orchestrator_rate_ledger.py` (~330 lines, 26
  tests across 5 classes): `TestRecord` × 6 (file creation, row
  schema, metadata stamping, empty-noop, append-across-calls,
  round-trip preservation, task isolation), `TestCurrentStatus`
  × 7 (missing file, five_hour-only, both rate types, latest-
  per-type, buffer-state preservation, malformed-line skip,
  unknown-rate-type ignore), `TestInternalCount` × 5 (missing,
  in-window, seven_day exclusion, single window flip, three-
  window walk), `TestCheckDivergence` × 6 (no data, below
  threshold, non-allowed status, fires + logs, equality boundary,
  window flip clears), `TestDefaultStateRoot` × 2 (default path
  shape + constructor wiring). All pure-Python, no engine.
- **NEEDS-PHASE-3-DECISION trio status.** Still untouched. §3
  consumed neither `uas_config.py`, `uas_hooks.py`, nor
  `uas.example.toml` — the per-task `state_root` parameterisation
  needed nothing from the layered config loader. Default-CUT
  trajectory holds for now per the §1 decision summary.
- **§4 hand-off note.** §4's `BufferLedger` will mirror this
  layout: per-task `buffer.jsonl` under the same
  `<state_root>/<task_id>/` directory, same `state_root`
  constructor parameter, same `event="..."` discriminator
  pattern (likely `event="buffer"`). The §2 Results' model-id
  hand-off note (`result["model"]` is None; the active model id
  lives under `result["modelUsage"]`'s key or any `assistant`
  event's `message.model`) remains the relevant pointer for §4.
- **Test outcomes.** `python3 -m pytest
  tests/test_orchestrator_rate_ledger.py -v` → 26 passed in
  0.13 s. `python3 -m pytest
  tests/test_orchestrator_worker.py -v` → 7 passed, 1 deselected
  (live), 0.07 s — confirms the §3 wire-in didn't break §2's
  helper coverage. `python3 -m pytest tests/ -q --timeout=120` →
  1839 passed, 4 deselected, 5 m 45 s — exactly +26 over the §2
  baseline (1813), no regressions. `python3 -m pytest
  tests/test_orchestrator_worker.py::TestSpawnWorkerLive -m
  integration -s -v --timeout=600` → 1 passed in 4.14 s; the
  live spawn exercised the wire-in path against a real worker
  and the assertion suite (rate_limit_events ≥ 1, etc.) held.
  Post-test check: `<repo>/orchestrator/state/` does not exist
  (state_root override worked, no repo pollution).

**Status:** completed

## Section 4 — Paid-buffer ledger

**Goal.** Extract per-call token usage from §2's worker results,
apply a pricing table, persist cumulative spend, expose
`total_spent()`.

**Steps.**

1. Create `orchestrator/pricing.py`. Single hard-coded dict
   keyed by model id. Initial entry:
   `claude-opus-4-7 -> {input_per_m, output_per_m,
   cache_read_per_m, cache_write_per_m}` populated from
   Anthropic's public pricing as of phase start. Module exposes
   `compute_cost(model_id: str, usage: dict) -> float`. Future
   model bumps update this dict in lockstep with the SDK pin
   bump (ROADMAP §Model policy). Unknown model id raises so the
   ledger can't silently zero-cost a call.
2. Create `orchestrator/buffer_ledger.py`. `BufferLedger` class:
   - `record(result: dict, *, run_metadata: dict, task_id: str,
     subtask_id: str) -> float`. Reads
     `result["model"]` and
     `result["usage"]["input_tokens"]` /
     `cache_creation_input_tokens` / `cache_read_input_tokens` /
     `output_tokens`; computes spend via
     `pricing.compute_cost`; appends one row to
     `<repo>/orchestrator/state/<task_id>/buffer.jsonl` carrying
     `{cost_usd, model, usage, subtask_id, timestamp_utc, ...}`;
     returns the call's cost.
   - `total_spent(task_id: str) -> float`. Sum of `cost_usd`
     across all rows in `buffer.jsonl`.
   - `total_spent_since(task_id: str, *, since_iso: str) ->
     float`. Filtered variant for window-scoped queries (used by
     §5's policy machine for "spend in the current 5h window").
3. Wire §2's `spawn_worker` to call `buffer_ledger.record(result,
   ...)` after every worker that produced a `result` line.
   Workers that hard-failed before the terminal `result` event
   are skipped (no usage data available; logged separately under
   `task_events.jsonl` in §6).
4. Unit tests under `tests/test_orchestrator_buffer_ledger.py`:
   synthetic `result.usage` payloads at known token counts;
   verify spend matches `pricing.compute_cost`; verify
   `total_spent()` sums rows; verify `total_spent_since()`
   filters by timestamp; verify unknown-model raises.

**Acceptance.**

- Per-call rows persisted under
  `<repo>/orchestrator/state/<task_id>/buffer.jsonl`.
- Cost computation matches the pricing table.
- `total_spent()` and `total_spent_since()` return correct sums
  on synthetic fixtures.
- Tests pass.

**Results.**

- `orchestrator/pricing.py` (119 lines) ships a hard-coded
  ``PRICING: dict[str, dict[str, float]]`` keyed by model id with
  four ``*_per_m`` rate fields per entry, plus
  ``compute_cost(model_id, usage) -> float`` and a
  ``UnknownModelError(KeyError)`` subclass. Sums input + output +
  cache_creation + cache_read tokens at their respective rates;
  missing keys (and ``None`` values, which some SDK versions emit
  for absent counts) are treated as 0. Subclassing ``KeyError``
  preserves backward compatibility for any caller that catches
  ``KeyError`` generically.
- **Pricing-table contents (PLAN-deviation, Haiku entry).** PLAN
  step 1 prescribed a single-entry table — ``claude-opus-4-7``
  only. The §4 live wire-in test against a real
  ``Reply with done`` worker surfaced a second model id:
  ``claude-haiku-4-5-20251001`` appears in
  ``result.modelUsage`` (and as the first key in some traces)
  even when the worker is launched with no ``--model`` flag.
  Claude Code 2026 routes some internal turns through Haiku
  regardless of the user-default Opus 4.7 setting; this is a
  Claude Code routing behaviour the orchestrator cannot opt out
  of, not a violation of ROADMAP §Model policy. Adding the
  Haiku 4.5 entry alongside Opus is the minimal deviation that
  lets the ledger function on real workers while preserving the
  PLAN's "raise on unknown model" safety property — and the
  pricing-table comment block explicitly calls this out so the
  next reader does not re-introduce a single-entry table by
  mistake.
- **Pricing-table approximations.** Two known imprecisions are
  documented inline in the module docstring rather than fixed,
  on the principle that the §5 policy machine wants an
  approximate dollar-headroom signal not a billing-grade figure:
  (i) Opus rates use the ≤200K-prompt tier; the >200K tier
  (1.5× input, 1.5× output) is not modelled separately —
  acceptable until long-context prompts become routine in the
  orchestrator's traffic mix. (ii) Cache-write rate uses the
  5-minute tier (1.25× input); the 1-hour tier (2× input) is
  not modelled separately even though real workers do hit it
  (the earlier-failing trace showed ``ephemeral_1h_input_tokens
  = 3760`` of 1h-tier cache writes). Under-bills 1h cache writes
  by ~40%; revisit if §5 buffer accuracy matters below the 10%
  margin.
- `orchestrator/buffer_ledger.py` (176 lines) ships the
  ``BufferLedger`` class with ``record``, ``total_spent``,
  ``total_spent_since``, plus the module-level
  ``DEFAULT_STATE_ROOT`` constant and an internal ``_iter_rows``
  reader. Layout mirrors §3's ``RateLedger`` exactly: same
  ``state_root`` constructor parameter, same per-task directory
  ``<state_root>/<task_id>/`` (filename ``buffer.jsonl``), same
  forgiving-reader posture (malformed lines, non-dict rows, blank
  lines all silently skipped). ``total_spent_since`` compares
  ISO-8601 timestamps lexicographically — safe because every
  row's ``timestamp_utc`` carries the ``+00:00`` suffix from
  ``capture_run_metadata``.
- **Row schema decision (preserved verbatim).** Each persisted
  row is ``{**run_metadata, "event": "buffer", "task_id",
  "subtask_id", "model", "usage", "cost_usd",
  "claude_reported_cost_usd"}``. ``cost_usd`` is the
  locally-computed figure via ``pricing.compute_cost``;
  ``claude_reported_cost_usd`` mirrors Claude's own
  ``result.total_cost_usd`` for traceability. The §5 policy
  machine reads ``cost_usd`` for its threshold checks (PLAN's
  intent — pricing table is the project-controlled ground
  truth); the parallel ``claude_reported_cost_usd`` field is for
  audit and divergence detection.
- **Model-id resolution.** The PLAN step 2 wording "Reads
  ``result["model"]``" is misleading on real Claude Code
  traces — ``result["model"]`` is always ``None`` per the §2
  hand-off note. ``_extract_model_id`` implements the documented
  fallback chain: top-level ``result["model"]`` if a non-empty
  string (legacy/older SDK), else the first key of
  ``result["modelUsage"]`` (Claude Code 2026's actual emission
  shape), else ``ValueError`` with a descriptive message. The
  ``ValueError`` failure mode is distinguishable from
  ``UnknownModelError`` (resolved id but no pricing row) so the
  policy machine can route them differently if §5 ever needs
  to.
- **Wire-in (PLAN step 3).** ``orchestrator/worker.py`` adds
  ``buffer_ledger`` to its imports and inserts a 9-line block
  immediately after ``rate_ledger.record``: instantiate
  ``BufferLedger(state_root=state_root)`` and call ``record(
  result, run_metadata=metadata, task_id=task_id, subtask_id=
  subtask_id)`` when ``state["result"]`` is non-None and carries
  a dict-typed ``usage`` field. Hard failures (no terminal
  ``result`` event at all) and timeouts (synthetic result dict
  with no ``usage`` key) skip the call — the audit trail for
  those failures lives in §6's ``task_events.jsonl``. The shared
  ``metadata`` dict from the rate-ledger call is reused so both
  ledgers stamp the same provenance snapshot for the same worker
  call.
- **Live spawn confirmation (Opus 4.7 + Haiku 4.5).** Manual
  one-shot ``spawn_worker`` invocation against an ephemeral
  ``tmp_path`` workspace produced one well-formed ``buffer.jsonl``
  row carrying ``model="claude-haiku-4-5-20251001"``,
  ``usage={input_tokens: 6, cache_read_input_tokens: 19903,
  output_tokens: 5, ...}``, ``cost_usd=0.0020213``,
  ``claude_reported_cost_usd=0.0105175``. Run wallclock 2.66 s
  including image rebuild (the Phase 3 §4 source additions
  bumped the source mtime past the engine image's build time,
  triggering one rebuild on first §4 live run). ``result.
  modelUsage`` carried both keys
  (``[claude-haiku-4-5-20251001, claude-opus-4-7]``);
  ``_extract_model_id`` returned the first key per design.
- **Cost-discrepancy finding (~5x under-bill on this trace).**
  Local ``cost_usd`` ($0.0020) vs Claude's
  ``claude_reported_cost_usd`` ($0.0105) diverged by ~5× on the
  trivial sample run. Likely root cause: (a) top-level
  ``result.usage`` reports the dominant-iteration token counts
  but ``modelUsage`` lists every model the trace touched, so
  applying one model's rates to the top-level totals
  systematically under-bills multi-model fan-out; and
  (b) Claude Code's ``total_cost_usd`` likely includes
  session-level overhead tokens (system init, prompt-cache
  bookkeeping, statusline pings) that never surface in the
  ``usage`` payload. Recording both numbers preserves the audit
  trail; §5 hand-off below pins the policy decision.
- `tests/test_orchestrator_buffer_ledger.py` (598 lines, 43
  tests across 8 classes): ``TestPricingTable`` × 3 (entry
  exists, all 4 fields present, ``UnknownModelError``
  subclasses ``KeyError``), ``TestComputeCost`` × 10 (per-rate
  unit math, mixed sums, zero/missing/None handling, unknown
  raise + KeyError compatibility), ``TestExtractModelId`` × 5
  (top-level model wins, modelUsage fallback, empty-string
  fallback, raises on neither, raises on empty modelUsage),
  ``TestRecord`` × 10 (file creation, return value, persisted
  cost matches compute_cost, model id, usage round-trip,
  claude_reported_cost handling, run-metadata stamping, append,
  task isolation), ``TestRecordErrors`` × 3 (unknown-model
  propagates with no row written, unresolvable id raises
  ValueError, missing usage dict treated as empty), ``TestTotalSpent``
  × 5 (missing file, single row, sum across rows, malformed
  line skip, non-numeric cost skip), ``TestTotalSpentSince``
  × 5 (missing file, ≥-since filter, equality boundary
  included, all-before returns 0, missing/None timestamp skip),
  ``TestDefaultStateRoot`` × 2 (default path shape +
  constructor wiring). All pure-Python, no engine.
- **§2 live test extended for §4 verification.**
  ``tests/test_orchestrator_worker.py``'s
  ``TestSpawnWorkerLive::test_trivial_done_prompt`` now also
  asserts that exactly one ``buffer.jsonl`` row was written
  under ``<tmp_path>/state/<task_id>/``, that ``cost_usd`` is
  numeric and positive, that ``model`` is a non-empty string,
  and that the persisted ``usage`` dict round-trips the
  worker's ``result.usage``. The 8-line assertion block lives
  inside the same ``@pytest.mark.integration`` test rather
  than as a separate test so the wire-in is verified in the
  same Claude call that exercises §2.
- **Test outcomes.** ``python3 -m pytest
  tests/test_orchestrator_buffer_ledger.py -v`` → 43 passed in
  0.13 s. ``python3 -m pytest
  tests/test_orchestrator_buffer_ledger.py
  tests/test_orchestrator_worker.py
  tests/test_orchestrator_rate_ledger.py -v --timeout=120`` →
  76 passed, 1 deselected (live), 0.19 s. ``python3 -m pytest
  tests/ -q --timeout=120`` → 1882 passed, 4 deselected,
  5 m 25 s — exactly +43 over the §3 baseline (1839), no
  regressions. ``python3 -m pytest
  tests/test_orchestrator_worker.py::TestSpawnWorkerLive -m
  integration -s -v --timeout=600`` → 1 passed in 3.64 s
  (excluding the engine rebuild step); both rate-ledger and
  buffer-ledger wire-ins exercised end-to-end against a real
  worker, ``buffer.jsonl`` assertions held. Post-test check:
  ``<repo>/orchestrator/state/`` does not exist (state_root
  override worked, no repo pollution).
- **NEEDS-PHASE-3-DECISION trio status.** Still untouched. §4
  consumed neither ``uas_config.py``, ``uas_hooks.py``, nor
  ``uas.example.toml`` — the pricing table is hard-coded in
  ``orchestrator/pricing.py``, the ledger paths come from a
  per-task ``state_root`` parameter. Default-CUT trajectory
  continues to hold per the §1 decision summary.
- **§5 hand-off note (cost source-of-truth).** §5's
  ``Policy.decide()`` reads ``buffer_total`` derived from
  ``BufferLedger.total_spent()``, which sums the
  locally-computed ``cost_usd``. Given the ~5× discrepancy
  found in §4's live trace, the policy machine's
  ``hard_stop_usd`` threshold will trigger far above the actual
  buffer drain unless §5 either (a) switches the source of
  truth to ``claude_reported_cost_usd`` (a parallel sum method
  on ``BufferLedger`` would be the minimal addition), or
  (b) sets the threshold against the locally-computed under-bill
  with intent — i.e. treat ``hard_stop_usd`` as a
  pricing-table-grounded ceiling, knowing real spend tracks
  higher. The PLAN's §5 step 1 default of
  ``buffer.hard_stop_usd = 200.00`` predates this finding and
  should be revisited at §5 start. Recommend option (a): add
  ``total_spent_reported(task_id) -> float`` to the ledger and
  switch §5's ``buffer_total`` source. Logged here so §5 does
  not silently inherit a 5×-off threshold semantics.

**Status:** completed

## Section 5 — Three-state policy machine

**Goal.** Implement the free / paid / hard-stop machine consuming
§3 + §4's ledgers; expose `decide()` returning the next-action
verdict; ship with a config-controlled disable per CLAUDE.md core
principle 2.

**Steps.**

1. Create `orchestrator/policy.py` plus
   `orchestrator/policy.default.toml` (committed defaults).
   `Policy` class:
   - Constructed from a TOML config: per-task override at
     `<repo>/orchestrator/state/<task_id>/policy.toml` falling
     back to the committed default.
   - Default thresholds (in `policy.default.toml`):
     - `[five_hour] soft_cap_action = "pause"`.
     - `[seven_day] soft_cap_action = "wrap_up"`.
     - `[buffer] hard_stop_usd = 200.00`.
     - `[buffer] warn_usd = 100.00`  (informational).
     - `[divergence] threshold = 50`  (rate-ledger sanity check).
     - `enabled = true`.
   - `decide(rate_status: dict, buffer_total: float, *, now:
     float) -> {"action": "go" | "pause_until" | "wrap_up" |
     "halt", "reason": str, "until": float | None}`. Action
     semantics:
     - `"go"`: spawn the next worker.
     - `"pause_until"`: orchestrator sleeps until `until` then
       re-evaluates.
     - `"wrap_up"`: do not spawn new workers; complete the
       current subtask only, then halt until next window.
     - `"halt"`: terminate orchestrator process; resume requires
       explicit operator intervention (`uas-orchestrate resume
       <task>`).
2. Define the transition rules (evaluated in this order; first
   match wins):
   - `buffer_total >= buffer.hard_stop_usd` → `halt`.
   - `rate_status.seven_day.status != "allowed"` and
     `seven_day.soft_cap_action == "wrap_up"` → `wrap_up`.
   - `rate_status.five_hour.status != "allowed"` and
     `five_hour.soft_cap_action == "pause"` →
     `pause_until = rate_status.five_hour.resetsAt`.
   - Else → `go`.
3. Ablation flag: `enabled = false` in TOML → `decide()` always
   returns `{"action": "go", "reason": "policy disabled",
   "until": None}`. Required by CLAUDE.md core principle 2.
   Unit-tested.
4. Unit tests under `tests/test_orchestrator_policy.py`: each
   transition cell of the (rate_status × buffer_total) table;
   ablation flag short-circuit; per-task TOML override of the
   committed default; unknown-action TOML value → load error.

**Acceptance.**

- `decide()` returns the documented action for every transition
  cell.
- Ablation flag works.
- Per-task TOML overrides the default committed file.
- Tests pass.

**Results.**

- `orchestrator/policy.py` (~360 lines) ships the `Policy` class
  with `load()` / `decide()` plus the `PolicyDecision` TypedDict,
  the `ActionVerdict` Literal, the `PolicyError(ValueError)`
  exception, and the four module-level helpers
  (`_parse_iso8601`, `_load_toml`, `_deep_merge`, plus per-field
  validators). Construction goes through `Policy.load(default_path,
  task_id, state_root)` so callers never bypass the validator;
  the `Policy.__init__` is exposed for tests that want to skip
  the TOML round-trip but is not the main entry point. Module
  docstring records the rule order, the time semantics
  (`resetsAt` ISO-8601 → unix epoch), and the §4 hand-off
  pointer to `total_spent_reported`.
- `orchestrator/policy.default.toml` (52 lines) holds the
  committed defaults exactly as the PLAN's "Default thresholds"
  list specifies — `enabled = true`,
  `[five_hour] soft_cap_action = "pause"`,
  `[seven_day] soft_cap_action = "wrap_up"`,
  `[buffer] hard_stop_usd = 200.00 / warn_usd = 100.00`,
  `[divergence] threshold = 50`. Inline comments explain each
  field's role and the §4 hand-off rationale for the
  `hard_stop_usd` figure; per-task overrides land at
  `<state_root>/<task_id>/policy.toml` and merge field-by-field
  via `_deep_merge`.
- **PLAN-deviation absorbed (§4 hand-off, cost source-of-truth).**
  PLAN §4 step 3 had documented but not implemented the §5
  recommendation that the policy machine read
  `claude_reported_cost_usd` instead of `cost_usd`. §5 absorbs
  it by adding `BufferLedger.total_spent_reported(task_id) ->
  float` (sums `claude_reported_cost_usd`; rows missing or
  carrying non-numeric values contribute 0) and pointing
  `Policy.decide()`'s `buffer_total` parameter at it via the
  module docstring. The local-cost `total_spent` method is
  preserved unchanged for audit / divergence detection. The
  PLAN's `hard_stop_usd = 200.00` default now means real
  Claude-reported buffer drain rather than a 5×-off figure
  against the locally-priced ledger.
- **Validator-strictness decision (preserved verbatim).** PLAN
  step 4 calls out "unknown-action TOML value → load error"
  without specifying the allowed set. Chose to restrict
  `five_hour.soft_cap_action` to `{"pause"}` and
  `seven_day.soft_cap_action` to `{"wrap_up"}` exactly — the
  only pairings the PLAN's step 2 rule list actually fires on.
  Loosening to also accept `"halt"` or cross-paired actions is
  left for §5+ informed iteration where a corresponding rule
  expansion would land. This guarantees the policy machine can
  never sit in a "valid TOML, rule does not fire, behaviour
  silently broken" state. The two `_VALID_*_ACTIONS` tuples in
  `policy.py` are the single source of truth; `_require_action`
  raises `PolicyError` on violations with a message naming the
  field and the allowed list.
- **Bool-on-numeric reject.** Python's `bool` subclasses `int`,
  so `hard_stop_usd = true` would silently coerce to `1.0` if
  the validator only checked `isinstance(value, (int, float))`.
  `_require_number` and `_require_int` explicitly reject
  `bool` first; tested by
  `TestPolicyValidator::test_bool_hard_stop_rejected`.
- **`now` parameter accepted but not consumed.** PLAN signature
  lists `now: float` on `decide()`. The current ruleset doesn't
  read it — the rule precedence is purely on
  `(rate_status, buffer_total, policy_config)`. The parameter
  is retained for forward compatibility (and so callers that
  already compute `time.time()` for logging do not have to
  change the call site if §5+ adds time-based rules). Docstring
  flags this explicitly and `decide()` body opens with a
  visible `del now` so future readers see the deliberate
  non-use.
- **`resetsAt` parsing fallback.** `_parse_iso8601` accepts the
  `Z` suffix Claude Code emits (rewrites to `+00:00`), accepts
  explicit offsets, treats naive timestamps as UTC, and returns
  `None` on missing / malformed input. When `decide()` is
  called for `pause_until` and the parser returns `None`, the
  function still returns `pause_until` but with `until=None` and
  prints a stderr alert flagging that the orchestrator's main
  loop must choose a fallback wake time. Decision deferred to
  §7/§8 where the loop is wired up.
- **Decision precedence (preserved verbatim).** The four-rule
  cascade in `decide()` is evaluated top-to-bottom; first match
  wins:
  1. `not self.enabled` → `go` (ablation short-circuit).
  2. `buffer_total >= hard_stop_usd` → `halt`.
  3. seven_day non-allowed + soft_cap_action == "wrap_up" → `wrap_up`.
  4. five_hour non-allowed + soft_cap_action == "pause" →
     `pause_until`.
  5. (default) → `go`.
  Tests assert the precedence cascade explicitly — buffer beats
  seven_day beats five_hour beats default — so future edits to
  rule order break a named test rather than silently changing
  policy.
- **Defensive snapshot handling.** `decide()` tolerates an
  empty `rate_status`, `rate_status` with `None` snapshots, and
  snapshots with `None` status fields — all flow to "go" as if
  no signal was emitted. This matches what
  `RateLedger.current_status()` returns on a fresh task with no
  recorded events; without the defensive checks the
  orchestrator would crash on first decide() call before the
  first worker had emitted anything.
- `tests/test_orchestrator_policy.py` (~660 lines, 60 tests
  across 10 classes): `TestParseISO8601` × 8 (Z suffix, +00:00
  suffix, non-UTC offset, naive→UTC, malformed→None,
  empty→None, None→None, non-string→None);
  `TestDeepMerge` × 6 (top-level override, missing-key add,
  nested merge, non-dict replaces dict, dict replaces non-dict,
  no input mutation); `TestPolicyLoad` × 8 (synthetic default,
  shipped default loadable, partial override merge,
  enabled-via-override, missing-override-falls-back,
  missing-default-raises, no-task-id-skips-override-lookup,
  malformed-TOML-raises); `TestPolicyValidator` × 15 (every
  required field type-checked + every soft_cap_action
  allow-list violation); `TestDecideGo` × 5 (all-allowed,
  empty rate_status, both-snapshots-None, status-None inside
  snapshot, buffer-just-below-hard_stop); `TestDecideHalt` × 4
  (at-boundary, far-above, halt-wins-over-7d,
  halt-wins-over-5h); `TestDecideWrapUp` × 3 (warning,
  limit_reached, 7d-wins-over-5h); `TestDecidePauseUntil` × 4
  (warning, limit_reached, missing-resetsAt, malformed-resetsAt
  — both stderr-asserted); `TestDecideAblation` × 5 (no-trigger,
  short-circuits-buffer, short-circuits-7d, short-circuits-5h,
  via-per-task-override); `TestPerTaskOverride` × 2
  (lower-hard-stop-triggers-earlier, divergence-threshold-override).
- `tests/test_orchestrator_buffer_ledger.py` extended with
  `TestTotalSpentReported` × 6 covering the new method:
  missing-file→0, single-row, sum-across-rows, skip-None-cost,
  skip-missing-field, divergence-from-`total_spent` on a
  controlled fixture (~5× ratio reproducing the §4 live-trace
  finding so the property the policy hand-off note relied on is
  pinned by a named test). Total
  `tests/test_orchestrator_buffer_ledger.py` test count rises
  to 49 (was 43 at §4 close).
- **NEEDS-PHASE-3-DECISION trio status.** Still untouched. §5
  consumed neither `uas_config.py`, `uas_hooks.py`, nor
  `uas.example.toml` — `policy.py` uses `tomllib` directly with
  the small per-section validator helpers it owns. The
  default-CUT trajectory continues to hold per the §1 decision
  summary; if §6–§8 also close without consumption, all three
  default to CUT for Phase 4.
- **§6 hand-off note (decision-event shape).** §6's
  `task_events.jsonl` will record `policy_pause` /
  `policy_wrap_up` / `policy_halt` decision rows. `decide()`'s
  return shape (`{action, reason, until}`) is the natural
  payload for those events — the `reason` string is already
  formatted for human consumption ("buffer_total 1.50 >=
  hard_stop_usd 1.50", "seven_day.status='warning' and ...");
  the `action` field maps 1:1 to the decision `kind`
  enumeration in the §6 PLAN. Recommend §6's `Decision.note`
  field carry the verbatim `reason` string from `decide()` so
  the timeline in `task_events.jsonl` is grep-friendly without
  an extra reconstruction step.
- **§5 lines added vs PLAN scope.** 360 (policy.py) + 52
  (policy.default.toml) + 660 (tests) + 38 (BufferLedger
  total_spent_reported addition + tests) ≈ 1110 net lines.
  Larger than §3's ~520 because the §5 surface is wider
  (validator + decide rules + helpers + ablation + override
  merge) and because every transition cell of the
  (rate_status × buffer_total) table got a named test per the
  PLAN's "each transition cell" acceptance bullet.
- **Test outcomes.** `python3 -m pytest
  tests/test_orchestrator_policy.py -v --timeout=60` → 60
  passed in 0.24 s. `python3 -m pytest
  tests/test_orchestrator_buffer_ledger.py -v --timeout=60` →
  49 passed in 0.21 s (was 43 at §4 close; +6 from the new
  `TestTotalSpentReported` class). `python3 -m pytest
  tests/test_orchestrator_buffer_ledger.py
  tests/test_orchestrator_policy.py
  tests/test_orchestrator_rate_ledger.py
  tests/test_orchestrator_worker.py -v --timeout=60` → 142
  passed, 1 deselected (live), 0.38 s. `python3 -m pytest
  tests/ -q --timeout=120` → **1948 passed, 4 deselected**
  in 305.34 s (5 m 05 s) — exactly +66 over the §4 baseline
  (1882), matching the 60 new policy tests + 6 new
  `TestTotalSpentReported` tests; no regressions on existing
  tests. Live integration test (`@pytest.mark.integration`)
  not re-run for §5 because §5's worker.py changes are nil
  (only the BufferLedger gained a new method; the §4 wire-in
  is unchanged); the §4 live trace's assertions on
  buffer.jsonl still hold.

**Status:** completed

## Section 6 — Task-state model

**Goal.** Define the long-horizon task structure (subtask queue,
status, decisions log) and its persistence schema; build the
`Task` / `Subtask` / `Decision` types and the per-task workspace
helper.

**Steps.**

1. Create `orchestrator/task.py` with dataclasses:
   - `Task`: `task_id: str`, `goal: str`, `subtasks:
     list[Subtask]`, `decisions: list[Decision]`,
     `created_at: str`, `workspace_path: str`.
   - `Subtask`: `subtask_id: str`, `prompt: str`, `status:
     Literal["pending", "in_flight", "done", "failed"]`,
     `started_at: str | None`, `finished_at: str | None`,
     `result_summary: str | None`, `cost_usd: float | None`.
   - `Decision`: `timestamp: str`, `kind: Literal[…]`,
     `note: str`. Kinds: `policy_pause`, `policy_wrap_up`,
     `policy_halt`, `worker_spawn`, `worker_complete`,
     `worker_fail`, `task_create`, `task_resume`.
2. Persistence:
   `<repo>/orchestrator/state/<task_id>/task_events.jsonl`.
   Append-only. Every state change writes one event stamped with
   `capture_run_metadata(include_orchestrator_version=True)` plus
   the event-specific payload. `event` discriminator key per
   substrate doc §3.
3. Workspace: per-task subdir at
   `<repo>/integration/workspace/<task_id>/` per substrate doc
   §5. Add `orchestrator/workspace.py::setup_task_workspace(
   task_id) -> str` which creates the dir without `rmtree` (the
   resume-safe variant per substrate doc §5 Gap; eval's
   `setup_workspace` stays unchanged).
4. Task-spec input format. TOML at
   `<repo>/orchestrator/cases/<task_id>.toml` declaring:
   - `task_id` (str, required).
   - `goal` (str, required).
   - `[[subtasks]]` (array of tables, optional —
     `{subtask_id, prompt}` pairs). §8's exit-criteria run uses
     a pre-seeded list for reproducibility; future tasks may
     generate subtasks on the fly.
5. Implement `Task.from_toml(path: str) -> Task` (initial seed)
   and `Task` operations (`enqueue_subtask`, `start_subtask`,
   `complete_subtask`, `fail_subtask`, `record_decision`) — each
   operation appends one event to `task_events.jsonl`. The
   in-memory dataclass stays in sync with the persisted log; the
   log is the source of truth on disk.
6. Unit tests under `tests/test_orchestrator_task.py`: TOML
   round-trip; subtask state transitions emit the correct
   events; decision log appends; workspace setup is idempotent
   under repeated calls and never destroys existing files.

**Acceptance.**

- `orchestrator/task.py` and `orchestrator/workspace.py` exist.
- Task TOML loads cleanly into a `Task` instance.
- State changes append events to JSONL with the correct schema.
- Workspace setup never destroys existing files.
- Tests pass.

**Results.**

- `orchestrator/task.py` (~457 lines) ships the three dataclasses
  (`Task`, `Subtask`, `Decision`), the `TaskError(ValueError)`
  exception, the `_VALID_DECISION_KINDS` frozenset (closed
  allow-list of 8 kinds), and six operations: `Task.from_toml`,
  `enqueue_subtask`, `start_subtask`, `complete_subtask`,
  `fail_subtask`, `record_decision`. Persistence layout mirrors
  §3 / §4 exactly: per-task directory at
  `<state_root>/<task_id>/`, append-only `task_events.jsonl`,
  every row stamped with
  `provenance.capture_run_metadata(include_orchestrator_version=True)`
  plus an `event="..."` discriminator and `task_id` for
  cross-file correlation.
- **Event schema (preserved verbatim).** Six event types written
  by §6 (§7 will add a seventh, `task_resume`):
  - `task_create` — from `Task.from_toml`; payload
    `{goal, workspace_path, created_at, decision_note}`. Doubles
    as the bootstrap event AND the `task_create` Decision row,
    so §7's replay reconstructs both effects from one line.
  - `enqueue_subtask` — `{subtask_id, prompt}`.
  - `start_subtask` — `{subtask_id, started_at}`.
  - `complete_subtask` — `{subtask_id, finished_at,
    result_summary, cost_usd}`.
  - `fail_subtask` — `{subtask_id, finished_at,
    result_summary}`.
  - `decision` — from `record_decision`;
    `{kind, note, decision_timestamp}`. `kind` is validated
    against `_VALID_DECISION_KINDS`; unknown kinds raise
    `TaskError` so the timeline cannot accumulate free-form
    strings that future tooling has to defend against.
- **Decision-kinds enum (preserved verbatim).** Eight kinds:
  `policy_pause`, `policy_wrap_up`, `policy_halt`,
  `worker_spawn`, `worker_complete`, `worker_fail`,
  `task_create`, `task_resume`. The `DecisionKind` Literal type
  alias and the `_VALID_DECISION_KINDS` frozenset both list the
  same eight; `TestModuleConstants::test_decision_kinds_match_plan`
  pins them to a closed set so a future drift breaks a named
  test.
- **PLAN-deviation absorbed (single-line `task_create`).** PLAN
  step 5 says "each operation appends one event to
  `task_events.jsonl`". `Task.from_toml` is a single operation
  but logically does two things: bootstraps the Task and records
  the inaugural `task_create` Decision. Resolved by encoding both
  effects in one event: the `task_create` row carries
  `goal` / `workspace_path` / `created_at` (bootstrap fields) AND
  `decision_note` (the human-readable note for the Decision
  mirror). §7's `load_task` parses one line and applies both
  effects. Avoids the alternative of writing two rows from one
  `from_toml` call.
- **Decision auto-mirror semantics.** Subtask state transitions
  (`enqueue_subtask` / `start_subtask` / `complete_subtask` /
  `fail_subtask`) write their own typed events but do **not**
  auto-create Decision rows in the in-memory list. The
  orchestrator's main loop (§7 / §8) chooses when to call
  `record_decision` for the high-level timeline (e.g. write a
  `worker_spawn` Decision when it spawns a worker for a subtask).
  The exception is `task_create`: `from_toml` mirrors a
  `Decision(kind="task_create")` into `decisions` because the
  bootstrap event canonically IS a decision row in the timeline.
- **Bool-on-numeric reject for `cost_usd`.** Mirrors the
  `_require_number` reject pattern from §5's `policy.py`: Python's
  `bool` subclasses `int`, so `cost_usd = True` would silently
  coerce to 1.0 if the validator only checked
  `isinstance(value, (int, float))`. Pinned by
  `test_bool_cost_rejected`. Companion test
  `test_int_cost_coerced_to_float` confirms `cost_usd = 2`
  (int) ends up as `2.0` (float) in both the in-memory
  `Subtask.cost_usd` and the persisted row.
- `orchestrator/workspace.py` (~49 lines) ships
  `setup_task_workspace(task_id, *, workspaces_dir=None) -> str`.
  Resume-safe variant per `docs/substrate.md` §5 Gap: never
  `rmtree`s, never destroys existing files. Path layout matches
  the eval harness convention (`<workspaces_dir>/<task_id>`) so
  the bind-mount contract used by §2's `spawn_worker` is
  unchanged. Tests pass `workspaces_dir=str(tmp_path /
  "workspaces")` to redirect from the canonical
  `<repo>/integration/workspace/`.
- **Why a separate file rather than `eval.setup_workspace` with
  a `reset=False` parameter.** Per `docs/substrate.md` §5
  Phase-3 consumption notes: "Phase 3 either parameterises
  `setup_workspace` (`reset=False` on resume) or writes a sibling
  `setup_task_workspace` and leaves the eval's variant alone."
  Picked the sibling — eval continues to be destructive by
  default (its regression case depends on a clean workspace) and
  the orchestrator's variant has the inverse default, with no
  shared code path. Cleaner separation than parameterising a
  helper that two consumers want with opposite defaults.
- `tests/test_orchestrator_task.py` (~838 lines, 66 tests
  across 9 classes):
  - `TestSetupTaskWorkspace` × 9 (creates dir, idempotent under
    repeated calls, non-destructive on existing files,
    preserves nested subdirectories, returns absolute path,
    task isolation, empty / non-string id rejected, default
    `DEFAULT_WORKSPACES_DIR` constant points under
    `integration/workspace`).
  - `TestTaskFromToml` × 16 (minimal TOML loads, single
    `task_create` event written, provenance fields stamped on
    the row, `task_create` Decision appended in-memory,
    `[[subtasks]]` enqueue in declared order, one event per
    `[[subtasks]]` row, missing file / `task_id` / `goal`
    raises with field-named match, malformed TOML raises,
    subtask validation paths — missing id / missing prompt /
    empty id, default `DEFAULT_STATE_ROOT` honoured via
    monkeypatch).
  - `TestEnqueueSubtask` × 7 (pending append, single event
    written with `event="enqueue_subtask"`, ordered across
    multiple calls, duplicate id rejected, empty id /
    empty prompt rejected, non-string id rejected).
  - `TestStartSubtask` × 5 (pending → in_flight transition +
    `started_at` ISO-8601 set, event written, double-start
    rejected, start-after-done rejected, unknown subtask
    raises).
  - `TestCompleteSubtask` × 9 (in_flight → done + finished_at /
    result_summary / cost_usd set, event payload round-trip,
    optional fields default to None, complete-when-pending
    rejected, double-complete rejected, type validation for
    `result_summary` (str | None) / `cost_usd` (numeric | None),
    bool rejected, int coerced to float).
  - `TestFailSubtask` × 5 (in_flight → failed transition,
    event written, fail-when-pending rejected, fail-when-done
    rejected, `result_summary` type validated).
  - `TestRecordDecision` × 6 (Decision appended to in-memory
    list, event row written with `event="decision"` and
    `decision_timestamp`, all 8 valid kinds accepted, unknown
    kind rejected, non-string note rejected, multi-decision
    order preserved).
  - `TestPersistence` × 5 (events_path under `state_root`,
    every row carries provenance + `event` + `task_id` +
    `orchestrator_version`, log is append-only across multiple
    operations, task isolation across separate task_ids,
    full-lifecycle 8-event sequence with two subtasks ending
    in `done`/`failed` and decisions list containing
    `task_create` + the explicit `policy_pause`).
  - `TestModuleConstants` × 4 (default state-root path, default
    workspaces-dir path, decision-kinds frozenset matches PLAN
    exactly, `Subtask` defaults to `pending` / None / None /
    None / None).
- **NEEDS-PHASE-3-DECISION trio status.** Still untouched. §6
  consumed neither `uas_config.py`, `uas_hooks.py`, nor
  `uas.example.toml` — `task.py` uses `tomllib` directly with
  field-by-field validators it owns (paralleling §5's
  `policy.py`). The default-CUT trajectory continues to hold per
  the §1 decision summary; if §7 and §8 also close without
  consumption, all three default to CUT for Phase 4.
- **§7 hand-off note (replay schema).** Each event type's
  payload is sufficient to reproduce its in-memory effect on
  replay:
  - `task_create` → construct fresh `Task` with `goal` /
    `workspace_path` / `created_at`; append
    `Decision(timestamp=created_at, kind="task_create",
    note=decision_note)` to the decisions list.
  - `enqueue_subtask` → append
    `Subtask(subtask_id, prompt, status="pending")`.
  - `start_subtask` → find subtask by id, set
    `status="in_flight"`, `started_at` from event payload.
  - `complete_subtask` → find subtask, set `status="done"`,
    `finished_at` / `result_summary` / `cost_usd`.
  - `fail_subtask` → find subtask, set `status="failed"`,
    `finished_at` / `result_summary`.
  - `decision` → append `Decision(timestamp=decision_timestamp,
    kind, note)` to decisions list.

  §7's `load_task` reads `task_events.jsonl` forward and applies
  these mutations in order. In-flight subtasks at end-of-replay
  get re-enqueued (status flipped back to `pending`) and a
  `task_resume` decision is logged — that's the new event type
  §7 introduces. The `survives_git_sha_flip` gate in PLAN §7
  step 2 lands as an additional row-level field defaulting to
  `True` for §6 events; §6's writer doesn't emit the field
  today, so §7 must default-True on missing.
- **Test outcomes.** `python3 -m pytest
  tests/test_orchestrator_task.py -v --timeout=60` → 66 passed
  in 0.98 s (pure-Python; no engine, no live Claude).
  `python3 -m pytest tests/ -q --timeout=120` → **2014 passed,
  4 deselected** in 357.34 s (5 m 57 s) — exactly +66 over the
  §5 baseline (1948), matching the 66 new §6 tests; no
  regressions on existing tests. Live integration test
  (`@pytest.mark.integration`) not re-run for §6 because §6's
  worker.py / ledger surface is unchanged (only new files
  added; no edits to `worker.py` / `rate_ledger.py` /
  `buffer_ledger.py` / `policy.py`); the §4 / §5 live trace's
  assertions still hold. Post-test pollution check:
  `<repo>/orchestrator/state/` does not exist;
  `<repo>/integration/workspace/` contains only the
  pre-existing `hello-file/` from prior eval runs (untouched).

**Status:** completed

## Section 7 — Resume-from-state

**Goal.** Reload task state from §6's JSONL on orchestrator
restart; pick up where the previous invocation left off; survive
the "long-horizon tasks edit code as they go" constraint per
substrate doc §6 Gap.

**Steps.**

1. In `orchestrator/task.py`, add `load_task(task_id: str) ->
   Task`. Reads `task_events.jsonl` end-to-end; replays events
   into a fresh `Task` (each event mutates the in-memory object
   exactly as it did at write time); returns the reconstructed
   task.
2. Implement the per-event resume gate. Each event records its
   own `survives_git_sha_flip: bool` field (default `True` for
   orchestrator events; `False` is reserved for future events
   that explicitly depend on tree state — currently none use
   `False`). On replay, events whose `survives_git_sha_flip ==
   False` and whose recorded `git_sha` differs from the current
   `git_sha` are dropped with a stderr note. The default-`True`
   shape directly addresses substrate doc §6's "too-strict gate
   erases progress" gap.
3. In-flight subtasks (status `in_flight` at replay end) are
   re-enqueued as `pending` and a `task_resume` decision is
   logged. Done / failed subtasks stay in their terminal state.
4. In `orchestrator/cli.py`, the `resume <task>` subcommand
   calls `load_task`, prints the recovered state summary
   (subtasks per status; total spend; last decision), and
   continues the main orchestrator loop from there. The `start`
   subcommand becomes a thin wrapper: if `task_events.jsonl`
   exists, route to `resume`; else create the task fresh.
5. Test under `tests/test_orchestrator_resume.py`: programmatic
   task spin-up; simulate a kill mid-subtask (write `in_flight`
   event, do not write `done`); call `load_task`; verify the
   in-flight subtask is re-enqueued (now `pending`) and the
   done / failed subtasks remain in their terminal states.

**Acceptance.**

- `load_task` reconstructs state from JSONL deterministically.
- `survives_git_sha_flip` gating works on synthetic events with
  drift.
- In-flight subtasks re-enqueue correctly on resume.
- `resume` subcommand prints a summary and continues the loop.
- Test passes.

**Results.**

- `orchestrator/task.py` net +230 lines (459 → 688). Module-level
  `load_task(task_id, *, state_root=None, mark_resume=True) ->
  Task` plus the small `_replay_lookup` helper and the
  `_events_path_for` filename builder. `Task._append_event` gained
  a `survives_git_sha_flip: bool = True` keyword-only parameter
  that lands as a top-level field on every persisted row from §7
  forward. The module docstring was updated to document the
  per-event resume gate (`survives_git_sha_flip` field) and to
  reframe the file as §6 + §7 rather than §6-only.
- `orchestrator/cli.py` net +143 lines (96 → 239). `cmd_start` /
  `cmd_resume` / `cmd_status` are wired against `Task.from_toml` /
  `task_mod.load_task` (plus `workspace_mod.setup_task_workspace`
  for the resume-safe per-task dir). `cmd_pause` / `cmd_halt`
  remain `NotImplementedError` stubs scoped to §8. New
  `--state-root` / `--cases-dir` / `--workspaces-dir` flags
  attached to `start` / `resume` / `status` via the shared
  `_add_path_flags` helper so tests can inject `tmp_path` and
  production runs leave them at the canonical defaults
  (`<repo>/orchestrator/state`, `<repo>/orchestrator/cases`,
  `<repo>/integration/workspace`). `_print_summary` ships the
  five-field recovered-state digest the PLAN named: task id, goal,
  per-status subtask counts, `${total_spend:.4f}` summed across
  subtasks, and the last decision.
- **Replay design (preserved verbatim).** Replay is deliberately
  tolerant — blank / malformed / non-dict / unknown-event rows are
  skipped silently (matching the RateLedger / BufferLedger
  forgiving-reader posture); state-mutation events apply directly
  without the write-path validators (`start_subtask` does not
  re-check `status == "pending"`, etc.) so a normal start → kill
  → restart sequence does not crash on the original
  `start_subtask` event. The write-path's transition checks remain
  in `Task.start_subtask` / `complete_subtask` / `fail_subtask`;
  validation lives in user code, replay is pure state
  reconstruction. Unknown subtask_ids in state events emit a
  stderr note via `_replay_lookup` and skip the mutation.
  Duplicate `task_create` events are tolerated (first wins, with
  stderr note). Decision events with kinds outside
  `_VALID_DECISION_KINDS` are silently dropped.
- **End-of-replay sweep (preserved verbatim).** Once the log is
  exhausted, any subtask still in `in_flight` is re-enqueued
  (status flipped to `pending`, `started_at` cleared) and a
  `task_resume` decision is appended in-memory AND persisted via
  `Task.record_decision`. The decision note enumerates the
  re-enqueued subtask ids when present (`"resumed; re-enqueued
  in-flight subtasks: s2, s3"`), or notes the absence
  (`"resumed; no in-flight subtasks"`). Drop counts from the
  `survives_git_sha_flip` gate are appended to the same note for
  durable audit. The `task_resume` decision row is itself written
  with `survives_git_sha_flip=True` (the default), so subsequent
  replays see the resumption boundary and reproduce the
  `Decision(kind="task_resume")` entry in `task.decisions`.
- **`mark_resume=False` decision (preserved verbatim).** PLAN
  step 4 names a `status` subcommand among the five
  argparse-stubbed commands at §1 close. §7 wired it as a
  read-only sibling of `resume`: same `load_task` call,
  `mark_resume=False`, no in-flight reset, no `task_resume`
  write. This split lets an operator inspect a paused task's
  state without nudging the log — distinct from `resume`'s
  intent of "pick up where we left off". Both subcommands share
  the same summary print so output format stays consistent.
- **Per-event resume gate (preserved verbatim).**
  `survives_git_sha_flip` is a row-level field defaulting to
  `True`. Replay reads `provenance._git_capture(["rev-parse",
  "HEAD"])` once, then for each row checks
  `row.get("survives_git_sha_flip", True)` — the missing-field
  default-True is the §6-pre-§7-row compatibility shim the §6
  hand-off note specified. When `survives == False` and the
  recorded `git_sha` differs from current, the row is dropped
  with a `[load_task] dropping event at line N: git_sha
  mismatch (recorded=..., current=..., event=...)` stderr note,
  and the drop count is summed into the `task_resume` decision
  note for visibility. Rows with `survives == True` (the default
  on every §7-and-later write) replay regardless of SHA drift,
  directly addressing `docs/substrate.md` §6's "too-strict gate
  erases progress" concern.
- **CLI flag-injection vs. monkeypatching.** Per-subcommand
  `--state-root` / `--cases-dir` / `--workspaces-dir` flags rather
  than a global flag: the wired subcommands (`start`, `resume`,
  `status`) accept them; the unwired ones (`pause`, `halt`) do
  not. Tests construct `argparse.Namespace` directly with the
  three fields (`_make_args` helper) rather than invoking the
  parser, since CLI flag-parsing is covered separately in
  `TestCliBuildParser`. This keeps test coverage of `cmd_*`
  behaviour decoupled from argparse internals.
- **`start` route-to-resume contract (preserved verbatim).**
  `cmd_start` checks `<state_root>/<task_id>/task_events.jsonl`
  presence and dispatches to `cmd_resume` if the file exists; only
  the no-log path goes through `Task.from_toml`. The unit test
  `test_start_routes_to_resume_when_log_exists` pins the gate by
  pre-priming an existing log without a corresponding case TOML
  — `cmd_start` invoked against this state must NOT raise
  FileNotFoundError, proving it never reached the
  `Task.from_toml(case_path)` branch.
- `tests/test_orchestrator_resume.py` (1088 lines, 59 tests
  across 11 classes):
  - `TestAppendEventGateField` × 2 — default-True on every
    event; explicit-False persists.
  - `TestLoadTaskBasic` × 7 — missing file / empty log /
    no-task_create-row raises; minimal round-trip;
    DEFAULT_STATE_ROOT honoured when state_root omitted;
    non-string and empty task_id raise.
  - `TestLoadTaskReplay` × 7 — subtasks replayed in order;
    done / failed / pending preservation; decision replay;
    unknown decision kind skipped; replay tolerant of
    repeated `start_subtask` events for the same id (the
    practical post-resume re-spawn case).
  - `TestInFlightReset` × 9 — single in-flight re-enqueued
    to pending; started_at cleared; task_resume decision
    appended in-memory; decision persisted to JSONL;
    no-in-flight note variant; done / failed preservation
    alongside in-flight reset; multiple in-flight all
    re-enqueued; `mark_resume=False` skips reset and skips
    write.
  - `TestGitShaGate` × 6 — default-True missing-field
    replays under SHA drift; explicit-True replays; False
    + matching-SHA replays; False + mismatched-SHA
    dropped; stderr note format; drop count summed into
    task_resume note.
  - `TestReaderTolerance` × 7 — blank-line skip, malformed
    JSON skip, non-dict-row skip, unknown-event skip,
    unknown-subtask-id stderr-and-skip, enqueue with
    missing/wrong-type fields skipped, duplicate
    task_create dropped with stderr note.
  - `TestRoundTripWithRealTask` × 4 — from_toml → load
    round-trip; kill-mid-subtask resume (the PLAN's
    headline test); one-done-one-in-flight resume
    preserves the done one and re-enqueues the in-flight;
    resumed Task is writable (start_subtask +
    complete_subtask succeed against the re-enqueued
    subtask, and a subsequent `load_task` observes the new
    state).
  - `TestCliStart` × 3 — no-log creates fresh; existing-log
    routes to resume (proven by absent case TOML);
    summary printed.
  - `TestCliResume` × 3 — calls load_task and prints
    summary; missing log raises; workspace setup is
    idempotent (pre-existing marker preserved).
  - `TestCliStatus` × 2 — no resume decision written;
    in-flight preserved in the printed summary.
  - `TestCliBuildParser` × 6 — argparse subcommand parsing
    for start / resume / status with flags; pause and halt
    still raise NotImplementedError.
  - `TestPrintSummary` × 3 — empty subtasks renders zero
    counts; total_spend sums Subtask.cost_usd; last
    decision rendered with kind+note.
- **NEEDS-PHASE-3-DECISION trio status.** Still untouched. §7
  consumed neither `uas_config.py`, `uas_hooks.py`, nor
  `uas.example.toml` — the CLI uses `argparse` directly,
  `load_task` uses `tomllib`-free JSONL replay, and no per-task
  config knob landed. The default-CUT trajectory continues to
  hold per the §1 decision summary; if §8 also closes without
  consumption, all three default to CUT for Phase 4.
- **§8 hand-off note (loop integration).** §8's
  `synthetic-multistep` run will exercise `cmd_start` →
  worker spawn loop → `--simulate-rate-status` policy fire →
  `cmd_pause` → process exit → `cmd_resume` → loop continues to
  completion. The §7 deliverables wire the recovery half of that
  cycle (`load_task`, `cmd_resume`, `cmd_status`) and the
  fresh-start half (`cmd_start` route to `Task.from_toml` when no
  log). The remaining §8 work is the actual loop body inside
  `cmd_start` / `cmd_resume` (after `_print_summary` returns):
  iterate pending subtasks, call `Task.start_subtask`,
  `worker.spawn_worker`, `Task.complete_subtask` /
  `fail_subtask`, then `Policy.decide()` between subtasks. §8
  also adds `cmd_pause` / `cmd_halt` bodies (currently
  `NotImplementedError`) and the `--simulate-rate-status` flag.
  `_print_summary` will likely move from "called once at the end
  of the §7 subcommand" to "called periodically by the loop"; the
  current implementation already accepts a `file` parameter so
  redirecting to a logger is trivial.
- **Pre-§7 row compatibility.** The §6 close baseline at commit
  `99f8fb2` had test_orchestrator_task.py producing event rows
  WITHOUT `survives_git_sha_flip`. §7's `_append_event` change
  makes new writes carry the field, but existing on-disk rows
  written before §7 (none in this repo, but possible in any
  future workflow that resumes across the §6 / §7 boundary)
  default-True on read. `TestGitShaGate::test_default_true_
  missing_field_replays` pins the property — strip the field
  from a synthetic bootstrap row, force a SHA mismatch, replay
  succeeds.
- **Test outcomes.** `python3 -m pytest
  tests/test_orchestrator_resume.py -v --timeout=60` → 59 passed
  in 0.64 s. `python3 -m pytest tests/ -q --timeout=120` →
  **2073 passed, 4 deselected** in 275.14 s (4 m 35 s) —
  exactly +59 over the §6 baseline (2014), matching the 59 new
  §7 tests; no regressions on existing tests. Live integration
  test (`@pytest.mark.integration`) not re-run for §7 because
  §7's `worker.py` surface is unchanged (no edits); the §4 / §5
  live trace's assertions still hold. Post-test pollution check:
  `<repo>/orchestrator/state/` does not exist;
  `<repo>/integration/workspace/` contains only the pre-existing
  `hello-file/` from prior eval runs (untouched).

**Status:** completed

## Section 8 — End-to-end window-boundary run

**Goal.** Exercise the full orchestrator on a synthetic task
with simulated rate-limit signals that trigger a window boundary
within minutes; demonstrate the pause + resume cycle completes
without human intervention.

**Steps.**

1. Author `orchestrator/cases/synthetic-multistep.toml` with 3–5
   pre-seeded subtasks. Each subtask is a trivial Claude prompt
   (single-file create / append / read against the per-task
   workspace).
2. Author
   `orchestrator/cases/synthetic-multistep-policy.toml` with low
   thresholds: `[buffer] hard_stop_usd = 1.50`,
   `[buffer] warn_usd = 0.50`, plus the default soft-cap actions
   for the rate windows.
3. Add a `--simulate-rate-status <state>` CLI flag to
   `orchestrator/cli.py` that overrides §3's `current_status()`
   read. Accepted state values mimic the stream-json schema:
   `allowed`, `five_hour_pause`, `seven_day_wrap_up`. The
   override is opt-in only; production runs do not pass it.
4. Run:
   `./uas-orchestrate start synthetic-multistep
   --simulate-rate-status allowed`. Let the orchestrator process
   subtasks 1 and 2.
5. Trigger the simulated 5h pause:
   `./uas-orchestrate pause synthetic-multistep
   --simulate-rate-status five_hour_pause`. The orchestrator
   should record a `policy_pause` decision and exit cleanly.
6. Restart with `./uas-orchestrate resume synthetic-multistep`
   (no `--simulate-rate-status` so the override clears).
   Confirm resume picks up correctly: any `in_flight` subtask is
   re-enqueued; remaining `pending` subtasks complete.
7. Inspect the artefacts under
   `<repo>/orchestrator/state/synthetic-multistep/`:
   - `task_events.jsonl` — full timeline of decisions.
   - `rate_limits.jsonl` — every worker's rate event.
   - `buffer.jsonl` — every worker's spend.
   - `policy.toml` — the per-task override (if used).
8. Document the run in §8's Results subsection: subtasks
   executed (count, names, durations), total spend, where the
   simulated pause triggered, where resume picked up, total
   wallclock, any surprises.

**Acceptance.**

- All three JSONL artefacts exist and are mutually consistent
  (subtask counts match between event log and ledgers; spend
  matches the sum of buffer rows; rate events line up with the
  simulated state transitions).
- Pause + resume cycle completes without operator intervention
  beyond the explicit `pause` and `resume` subcommand calls.
- Phase exit-criteria run is documented in §8 Results below.

**Status:** pending
