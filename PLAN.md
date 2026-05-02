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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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
