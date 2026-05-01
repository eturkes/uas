# Substrate boundary catalog

This document catalogs the substrate the Phase 3 orchestrator
consumes: the Phase 1 deliverables that ROADMAP §Phase 4 — Prune
designates as "keep", plus the Claude Code v2.1.80+ rate-limit read
surfaces resolved by Phase 2 §1.

Each component entry below records six fields:

- **Location** — file paths plus the key functions / classes /
  fixtures that anchor the component.
- **Accepts** — inputs the orchestrator (or any caller) supplies:
  positional / keyword arguments, configuration knobs, environment
  variables.
- **Emits** — return values, files written, side effects, log
  events.
- **Lifecycle** — when initialised, when torn down, what state (if
  any) persists across invocations.
- **Phase-3 consumption pattern** — how the orchestrator daemon is
  expected to use the component (library function, subprocess
  spawn, file-based handoff, etc.).
- **Gaps** — what the orchestrator must add or wrap rather than
  consume as-is.

The component table at the head of the doc is the one-screen
summary; the per-section detail follows.

## Components at a glance

| # | Component | Location | One-line role |
|---|---|---|---|
| 1 | Sandbox primitive | `orchestrator/sandbox.py`, `Containerfile` | Run untrusted Python in a Podman container or local subprocess; uniform `{exit_code, stdout, stderr}` return. |
| 2 | OAuth 4-stage refresh | `integration/eval.py` (`_maybe_refresh_oauth`, `_self_refresh_oauth`, `_read_token_expiry`) | Keep eval-harness Claude Max OAuth token >1 h alive; survives detached / headless processes. |
| 3 | JSONL audit log | `integration/eval.py` (`append_result_row`, `RESULTS_JSONL`) | Append-only durable log of `(case × run)` rows; one self-describing JSON object per line. |
| 4 | Provenance capture | `integration/eval.py` (`capture_run_metadata`, `_git_capture`, `_hash_active_config`, `HARNESS_VERSION`) | Per-invocation git / config / env / harness fingerprint stamped onto every JSONL row. |
| 5 | Workspace isolation | `integration/eval.py` (`WORKSPACES_DIR`, `setup_workspace`, `SetupFileMissing`); `integration/workspace/<case>/` directory convention | Per-task scratch directory rebuilt on every run; container mount point. |
| 6 | Resume-from-JSONL | `integration/eval.py` (`load_prior_rows`, `--no-resume` CLI flag) | Skip `(run_index, case)` pairs already recorded under the same `git_sha` / `git_dirty` / `harness_version`. |
| 7 | Eval shell wrapper + check types + smoke case | `uas-eval`; `integration/eval.py` (`run_check`); `integration/cases/trivial/hello-file.json` | Single repo-root entrypoint, six deterministic check types, one canonical hello-file regression case. |
| 8 | Claude Code rate-limit read pattern | Stream-json `rate_limit_event` (default); TUI statusline JSON `rate_limits` field (companion) | Phase 3 quota state-machine input; resolved by Phase 2 §1. |

## 1. Sandbox primitive

**Location.** `orchestrator/sandbox.py` (201 lines) plus
`Containerfile` (53 lines) at the repo root. Public API:
`run_in_sandbox(code: str, timeout: int | None = None) -> dict`,
`run_pytest_in_sandbox(test_files: list[str], timeout: int | None
= None) -> dict`, and `get_total_sandbox_time() -> float`. Two
internal helpers select the execution mode: `_run_local` (subprocess
fallback) and `_run_container` (nested Podman invocation).

**Accepts.**

- `code` (str) — Python source to execute. `run_pytest_in_sandbox`
  generates a wrapper that pip-installs pytest then invokes it
  against `test_files`.
- `timeout` (int | None) — wall-clock seconds before the run is
  killed.
- Environment variables read at module import: `UAS_SANDBOX_IMAGE`
  (default `docker.io/library/python:3.12-slim`),
  `UAS_SANDBOX_TIMEOUT` (default unset → no per-call timeout),
  `UAS_SANDBOX_MODE` (default `container`; flip to `local` for
  subprocess-only mode), `UAS_WORKSPACE` (default `/workspace`),
  `UAS_PROJECT_NAME` (optional subdir under `UAS_WORKSPACE`),
  `UAS_HOST_UID` / `UAS_HOST_GID` (forwarded to the container's
  `--user` flag when host UID is non-root).
- The `Containerfile` builds `uas-engine:latest` (Python 3.12 +
  Node + `@anthropic-ai/claude-code` global + system-wide git
  identity). It sets `IS_SANDBOX=1` and `UAS_SANDBOX_MODE=local` —
  i.e., when the architect runs *inside* the engine container, the
  sandbox runs steps as local subprocesses (the engine container
  itself is the sandbox; no nested-Podman recursion).

**Emits.**

- Return dict: `{"exit_code": int, "stdout": str, "stderr": str}`.
  Timeout returns `exit_code=-1` and a `stderr` of `"Execution
  timed out after {n} seconds."`. No exceptions cross the API
  boundary in the normal path (`subprocess.CalledProcessError` and
  `TimeoutExpired` are caught internally).
- Side effects: writes / unlinks a temp file under `/tmp/`; in
  container mode, runs `podman --storage-driver=vfs run --rm
  --network=host --name uas-sandbox-<8 hex>` and (on timeout)
  fires-and-forgets `podman kill` + `podman rm -f` to clean up.
- Module-global accumulator: `_sandbox_total_time` increments on
  every call; surfaced via `get_total_sandbox_time()`.

**Lifecycle.** Stateless per call (the temp script is unlinked in
the `finally` block; the container is `--rm`). The
`_sandbox_total_time` counter is the only process-lived state and
exists for the architect dashboard's "Sandbox" timing column. The
`uas-engine:latest` image is built / refreshed by the Phase 1 eval
wrapper (`_ensure_image` in `eval.py`); see component 7.

**Phase-3 consumption pattern.** Phase 3 will not run untrusted
Python through this primitive — Claude Code workers operate inside
their own sandbox boundaries via `--dangerously-skip-permissions`
and the workspace mount. The sandbox primitive remains relevant in
two narrower roles:

1. **Engine-container substrate.** `Containerfile` continues to be
   the build target the orchestrator spawns each headless worker
   inside (workspace bind-mount, `.uas_auth` mount, OAuth creds in
   place, `claude` CLI on `$PATH`). Phase 3 will call
   `podman/docker run` directly — not via `run_in_sandbox` — but
   the `Containerfile` is the canonical engine image and stays
   put.
2. **Verification side-channel (optional).** If Phase 3's policy
   machine ever needs to evaluate a determinant Python predicate
   against a worker's output (e.g., regex-grade a file), it can
   reuse `run_in_sandbox` rather than re-implement subprocess
   plumbing.

**Gaps.**

- No structured-output mode. Callers parse `stdout` / `stderr`
  themselves.
- `--network=host` (container mode) trusts the wrapped code;
  acceptable today, but Phase 3 should not feed unsanitised LLM
  output through this path without re-evaluating.
- No checkpoint / resume of in-flight container runs; a kill
  during execution discards the run.
- Image build is implicit in `_ensure_image`; Phase 3 needs an
  explicit "image is ready" precondition or a guaranteed cold-build
  path.

## 2. OAuth 4-stage refresh

**Location.** `integration/eval.py` constants + private helpers,
all near the top of the file:

- `_OAUTH_REFRESH_BUFFER = 3600` (refresh threshold, seconds).
- `_DEFAULT_CLAUDE_CREDS = ~/.claude/.credentials.json`.
- `_OAUTH_TOKEN_ENDPOINT = https://console.anthropic.com/v1/oauth/token`.
- `_OAUTH_CLIENT_ID` (hardcoded constant — the Anthropic client id
  Claude Code authenticates as).
- `_read_token_expiry(creds_path) -> float` (seconds remaining on
  the access token, `0.0` on any error).
- `_self_refresh_oauth(creds_path) -> bool` (POSTs `grant_type =
  refresh_token` to the OAuth endpoint via `httpx`; rewrites
  `creds_path` in place on success).
- `_maybe_refresh_oauth() -> None` (the four-stage orchestrator;
  called from `main()` between cases at line 1509).

**Accepts.**

- Implicit: existence of a credentials file at `UAS_AUTH_DIR`
  (`<repo>/.uas_auth/.credentials.json`) and optionally at
  `~/.claude/.credentials.json`.
- No formal arguments — the function is a side-effecting refresh
  loop driven entirely off filesystem state.

**Emits.**

- Side effect: rewrites `<UAS_AUTH_DIR>/.credentials.json` with a
  new `claudeAiOauth.{accessToken,refreshToken,expiresAt}` triple.
- May spawn `claude -p ping` (stage 4) to force-refresh the
  default credential file.
- May `shutil.copy2(_DEFAULT_CLAUDE_CREDS, eval_creds)` (stages 3
  / 4) to mirror a refreshed default token into the eval auth
  dir.
- Logging: every branch prints a `[oauth]` status line to stderr.
- No return value; no exception propagation.

**Lifecycle.** Stateless per call — every invocation re-reads the
credential files and re-evaluates the four stages:

1. **Eval token already valid (>1 h).** No-op.
2. **Self-refresh.** Exchange `claudeAiOauth.refreshToken` at the
   OAuth endpoint; rewrite the eval creds in place. Works in
   detached `nohup` processes.
3. **Borrow `~/.claude/`.** If the default-CLI creds are valid for
   >1 h, copy them into the eval dir.
4. **Force-refresh `~/.claude/` then borrow.** Run `claude -p
   ping` (120 s timeout) against the default creds to make the
   CLI refresh them, then copy.

Stages 2–4 fall through to a stderr "Could not obtain valid
token" log if all three fail. The function does not raise.

**Phase-3 consumption pattern.** The orchestrator will reuse the
exact four-stage logic: every headless `claude --print` worker
needs a valid OAuth token, multi-day tasks span multiple 8 h
token cycles, and the orchestrator may be a long-lived daemon
launched well before its first worker invocation. Two consumption
shapes are equivalent and Phase 3 picks one:

- Lift `_self_refresh_oauth` / `_maybe_refresh_oauth` /
  `_read_token_expiry` into a small `auth.py` module the eval
  harness *and* the orchestrator both import. Eval becomes the
  thin caller; the module is the substrate.
- Leave the helpers in `eval.py` and `from integration.eval import
  _maybe_refresh_oauth` from the orchestrator. Cheaper but
  couples the eval harness's import surface to the orchestrator.

The first shape is preferred because Phase 4 may delete other
parts of `eval.py`; pulling auth into its own module makes that
prune-pass cleaner.

**Gaps.**

- The `_OAUTH_CLIENT_ID` constant is a hardcoded value that must
  match whatever Claude Code uses. If Anthropic rotates the
  client id, every UAS process refresh path breaks silently.
  Phase 3 should expose this as a config-controlled value with a
  warning if it drifts from the `~/.claude/.credentials.json`
  metadata.
- No quota awareness yet — the refresh logic is independent of
  the Phase 3 rate-limit ledger. The two state machines
  (auth-validity, rate-limit-status) are orthogonal and both must
  hold for a worker to fire.
- The 120 s `claude -p ping` timeout in stage 4 is inherited from
  the eval-harness budget; orchestrator background refresh might
  want a longer or shorter ceiling.

## 3. JSONL audit log

**Location.** `integration/eval.py`:

- Constant `RESULTS_JSONL =
  <repo>/integration/eval_results.jsonl`.
- `append_result_row(row, *, run_metadata, run_index,
  output_path=None) -> None`.
- CLI override `--results-out <path>` parsed in `main()` and
  threaded through `args.results_out or RESULTS_JSONL`.

**Accepts.**

- `row` (dict) — the per-`run_case` output: case name, goal,
  workspace path, list of check results, exit code, elapsed,
  optional log / output / error fields, `passed`, `tier`.
- `run_metadata` (dict) — output of `capture_run_metadata` (see
  component 4).
- `run_index` (int) — multi-run sweep index; `0` when a single
  run executes.
- `output_path` (str | None) — overridable target file; defaults
  to `RESULTS_JSONL`.

**Emits.**

- Appends one line of canonical JSON to `output_path`. The line is
  the merge of `run_metadata`, `{"run_index": run_index}`, and
  `row` (in that key order, with `row` last so case-level keys win
  on collision — none currently exist). Serialised via
  `json.dumps(record, default=str)` so non-JSON-native types
  (`datetime`, `pathlib.Path`) round-trip as strings.
- Side effect: `os.makedirs(parent, exist_ok=True)` if the parent
  directory is missing. The file is created by `open(..., "a")`
  on first append; never truncated.
- No return value, no exception suppression.

**Lifecycle.** File is append-only across the entire harness
history. No rotation, no compaction. `--clean` does not delete it
(it deletes `WORKSPACES_DIR` only; see component 5). Operators
delete or move `eval_results.jsonl` by hand if they want a fresh
log.

**Phase-3 consumption pattern.** The orchestrator extends the same
log shape — append-only, one self-describing JSON object per line,
metadata-stamped — for its own audit needs:

- Per-call `usage.input_tokens` / `usage.output_tokens` events
  (paid-buffer ledger source-of-truth).
- Per-worker spawn / completion events.
- Policy-state transitions (free → paid → hard-stop).
- Resume hints (active task, last completed subtask).

Two reasonable layouts:

- **Single shared log** stamped with an `event` discriminator key
  (`event=eval_run` vs `event=orchestrator_call` vs
  `event=policy_transition`). Easier to correlate in time;
  noisier per-eval-run.
- **Separate logs** under `integration/eval_results.jsonl`,
  `orchestrator/<task>/calls.jsonl`,
  `orchestrator/<task>/policy.jsonl`. Cleaner separation; Phase
  3's resume code reads three files instead of one.

Phase 3 chooses based on what its task-state model needs for
resume; both reuse `append_result_row`'s contract verbatim
(merge-and-serialise) without changing the schema.

**Gaps.**

- No schema validation on the writer side. A bad row wedges into
  the log without complaint; downstream readers (`load_prior_rows`,
  `aggregate_results`) defend defensively.
- No structured event-type discriminator yet. If Phase 3 chooses
  the single-shared-log layout, an `event` key needs adding and
  every existing eval reader needs to default it to `eval_run`.
- No rotation — multi-day orchestrator runs may grow the log
  meaningfully. Acceptable for now (project owner is the only
  consumer); revisit at Phase 5+.

## 4. Provenance capture

**Location.** `integration/eval.py`:

- `HARNESS_VERSION = "phase1"` — bumped manually whenever the
  output schema changes.
- `_SECRET_ENV_PATTERN = re.compile(r"(_TOKEN|_KEY|_SECRET|
  _PASSWORD)$", re.IGNORECASE)` — anchored env-var filter.
- `_git_capture(args, default="unknown")` — best-effort
  `git -C REPO_ROOT <args>` wrapper that swallows OSError /
  SubprocessError / non-zero exit.
- `_hash_active_config()` — `importlib.util` loads
  `<repo>/uas_config.py`, calls `load_config()`, returns
  SHA-256 of `json.dumps(cfg, sort_keys=True, default=str)`.
- `capture_run_metadata() -> dict` — composes the seven-key
  provenance payload.

**Accepts.** No arguments. Reads filesystem (`git`, `uas_config.py`)
and the live `os.environ`.

**Emits.**

- Returns:

  ```
  {
    "git_sha":         <full SHA from rev-parse HEAD, or "unknown">,
    "git_branch":      <abbrev-ref HEAD, or "unknown">,
    "git_dirty":       <bool: true iff `git status --porcelain` non-empty>,
    "timestamp_utc":   <ISO-8601 UTC at capture time>,
    "env_snapshot":    <{ k: v for k, v in os.environ if k.startswith("UAS_") and not _SECRET_ENV_PATTERN.search(k) }>,
    "config_hash":     <SHA-256 hex of canonical config dump, or "unavailable">,
    "harness_version": "phase1",
  }
  ```

- No side effects. No exception path — every helper degrades to
  a sentinel string on failure.

**Lifecycle.** Captured once per `main()` invocation, immediately
after `--list` short-circuits. The dict is stamped onto every
`append_result_row` call for that invocation. No persistence
beyond the JSONL it's stamped into.

**Phase-3 consumption pattern.** The orchestrator stamps the same
payload onto every event it writes (per-call, per-worker, policy
transitions). Two changes:

- Bump `HARNESS_VERSION` to `"phase3"` (or a richer scheme like
  `"phase3.0"`) so Phase 3 events are distinguishable from Phase
  1 eval rows in the same JSONL log.
- Optionally add an `orchestrator_version` key alongside
  `harness_version` to track substrate revision separately from
  application revision. Decide at Phase 3 design time.

Reuse `capture_run_metadata` verbatim from a shared module (see
component 2's "Phase-3 consumption" notes; the same `auth.py`
factoring suggests a parallel `provenance.py` for these helpers,
with `eval.py` becoming the thin caller).

**Gaps.**

- `HARNESS_VERSION` is a single string; no compatibility envelope.
  Phase 3 has to commit to either reusing the string for both
  layers or introducing a second key.
- `env_snapshot` filters by suffix only. New non-standard secret
  env names (e.g., `UAS_FOO_API`) leak; rare but possible.
- `config_hash` swallows all errors as `"unavailable"`. If
  `uas_config.load_config` starts raising during a refactor,
  every JSONL row goes unprovenanced silently.

## 5. Workspace isolation

**Location.** `integration/eval.py`:

- Constant `WORKSPACES_DIR = <repo>/integration/workspace`.
- `setup_workspace(case) -> str` — `WORKSPACES_DIR/<case_name>`
  is `rmtree`'d (if it exists) then `os.makedirs`'d, and any
  declared `setup_files` are copied in from
  `<repo>/integration/data/<filename>`.
- `SetupFileMissing(Exception)` — raised by `setup_workspace`
  when a declared setup file is absent. (`integration/data/` was
  removed at Phase 1 §9 close along with the case-set
  reduction; the exception path remains live for any case that
  re-introduces `setup_files`.)
- `--clean` CLI flag in `main()` → `shutil.rmtree(WORKSPACES_DIR)`
  before the loop runs.
- The container invocation in `invoke_architect` mounts
  `<workspace>:/workspace:Z` and sets `UAS_WORKSPACE=/workspace`
  inside the container.

**Accepts.**

- `case` (dict) — must have a `name` (used as the directory
  basename) and may have `setup_files` (list of basenames under
  `integration/data/`).

**Emits.**

- Returns the absolute workspace path
  (`<repo>/integration/workspace/<case_name>`).
- Side effects: deletes the prior per-case directory (if any) and
  recreates it; copies any declared setup files.
- Raises `SetupFileMissing(filename)` if a declared setup file is
  absent.

**Lifecycle.** Per-case scratch directory. Recreated from scratch
on every run. The architect populates it in-process: `output.json`
at the workspace root, plus `.uas_state/` (run-id-keyed runs,
scratchpad, knowledge base) and `.uas_goals/GOAL.txt`. Persists
across runs unless the next run nukes it via `setup_workspace` or
the operator passes `--clean`.

**Phase-3 consumption pattern.** Every long-horizon task gets its
own workspace under the same convention. Likely shape:

- `<repo>/integration/workspace/<task>/` — the long-horizon
  task's mount point. Each headless worker subprocess mounts it
  with the same `UAS_WORKSPACE=/workspace` contract used by
  `invoke_architect`.
- The architect's `.uas_state/` layout (latest_run, runs/<id>/,
  scratchpad.md, knowledge.json) does **not** carry over — it
  belongs to the architect, which Phase 4 deletes. The
  orchestrator owns its own state directory shape (Phase 3
  design).
- `setup_workspace` may need to **not** unconditionally `rmtree`:
  resume-from-state semantics mean a multi-day task's workspace
  must survive process restarts. Phase 3 either parameterises
  `setup_workspace` (`reset=False` on resume) or writes a sibling
  `setup_task_workspace` and leaves the eval's variant alone.

**Gaps.**

- `setup_workspace` is destructive by default; resume-from-state
  needs the inverse default.
- `integration/data/` was deleted at Phase 1 §9 close; the
  `SetupFileMissing` path is now reachable only by reintroducing a
  `setup_files`-bearing case. Phase 3 likely doesn't need it; the
  exception type stays for future cases.
- The `:Z` mount option in `invoke_architect` is a SELinux relabel
  hint; portable to fedora-family hosts but may need tweaking on
  other distributions.

## 6. Resume-from-JSONL

**Location.** `integration/eval.py`:

- `load_prior_rows(path, run_metadata) -> list` (lines 184–223).
- Two-line gate at the top of the per-case loop in `main()`
  (lines 1480–1508): build `resumable_keys = {(run_index, name)
  for r in prior_rows}`, skip cases whose `(run_index, name)`
  pair is in the set.
- CLI flag `--no-resume` (default off → resume is **on** by
  default).
- Test coverage: 15 unit tests in `tests/test_eval_resume.py`
  (added together with the feature in commit `d81e42e`).

**Accepts.**

- `path` (str | None) — JSONL file to read.
- `run_metadata` (dict) — must contain `git_sha`, `git_dirty`,
  `harness_version`. The conjunction of these three keys is the
  resume gate.

**Emits.**

- Returns a list of dicts: every row in `path` that matches the
  three-key gate **and** carries `run_index` + `name` keys.
  Mismatched / corrupt / older-schema rows are skipped with a
  one-line stderr warning per skip. Missing file → empty list.
- No file writes.

**Lifecycle.** Read once at the head of `main()`. The matched
rows seed `all_results` so aggregations include them, and a
`resumable_keys` set short-circuits the per-case loop. New rows
written this run are appended to the same JSONL via
`append_result_row` so the next resume sees the merged history.

**Phase-3 consumption pattern.** The orchestrator's task-state
recovery uses an identical shape:

- Resume gate at task-state level: re-execute only what isn't
  recorded under the current `git_sha` × `harness_version` × task
  ID. Subtasks already written stay written.
- Stricter / looser gate variants. The orchestrator may want to
  resume across `git_dirty` flips (long-running task spans many
  edits); decide per Phase 3 task-spec design.
- Replace `(run_index, name)` keying with `(task_id, subtask_id)`.

`load_prior_rows` itself isn't reusable verbatim — the keying is
eval-specific — but its three-line filter logic ("match the gate
fields, skip corrupt lines with a warning, return what survives")
is the canonical pattern Phase 3 mirrors.

**Gaps.**

- The conservative three-key gate ignores everything from a
  different commit. Phase 3 long-horizon tasks edit code as they
  go; a too-strict gate erases progress. The orchestrator either
  (a) records its gate keys differently from eval's or (b)
  introduces a per-event "I survive a `git_sha` flip" flag.
- No row-level migration. A schema bump (`harness_version`
  change) silently invalidates the entire prior log. Acceptable
  during phase transitions; needs a migration story before Phase
  5 ships real-task runs.

## 7. Eval shell wrapper + check types + smoke case

**Location.**

- `<repo>/uas-eval` (13-line bash wrapper at repo root). Sets
  `PYTHONPATH=<repo>` and `UAS_HOST_UID` / `UAS_HOST_GID`, then
  `exec python3 -P -m integration.eval "$@"`.
- `integration/eval.py` (1560 lines) — the harness body. The
  Phase 4 keep list refers to the deterministic check layer in
  `run_check` (lines 720–1107 minus the `llm_judge` branch); the
  rest of `eval.py` is shared substrate covered by components
  2–6.
- `integration/cases/trivial/hello-file.json` — the canonical
  smoke case. The 35-case set authored at Phase 1 §9 was deleted
  at §9 close; this single case is what the keep list retains.

**Accepts.**

- CLI surface: `-k <pattern>`, `-v`, `--list`, `--local`,
  `--clean`, `--results-out <path>`, `--runs <N>` (env override
  `UAS_EVAL_RUNS`), `--tier <trivial|moderate|hard|open_ended>`,
  `--no-resume`. Detailed help text in `main()`'s argparse setup.
- Case schema (per case JSON file):
  - `name` (str, required) — also the workspace dirname.
  - `goal` (str, required) — passed as `UAS_GOAL` to the architect
    subprocess.
  - `checks` (list of dicts, required) — see check-type catalog
    below.
  - `setup_files` (list of strings, optional) — basenames under
    `integration/data/` to copy into the workspace before
    invocation. (`integration/data/` was deleted at Phase 1 §9
    close; uses since then leave this list empty.)
  - `notes` (free-form, optional) — purpose / reasoning, not
    consumed by the runner.
  - `tier` directory placement: the case's parent directory name
    (one of `trivial / moderate / hard / open_ended`) overrides
    any in-file `tier` field — directory layout is the canonical
    source.

**Emits.**

- Returns nonzero exit iff any case in any run failed, zero
  otherwise.
- Files written:
  - `integration/eval_results.json` — first-run-only legacy
    summary, overwritten each run.
  - `integration/eval_results.jsonl` — append-only durable log
    (component 3).
  - `integration/eval_results_aggregate.json` — per-case ×
    per-tier rollup; overwritten each run.
- Stdout / stderr — the human-readable per-case progress and
  final report.

**Lifecycle.** One process per invocation. Builds / refreshes
`uas-engine:latest` lazily via `_ensure_image`. Refreshes OAuth
between cases via component 2. Loads resumable rows up front via
component 6. The Phase 1 §10 canonical run completes inside the
~10 minute wallclock budget noted in ROADMAP §Suite scope (amended
after §9 close).

**Deterministic check-type catalog** (defined in
`run_check`; the `llm_judge` branch is **not** on the keep list and
is being deleted in Phase 4):

| Check type | Required fields | Optional fields | Pass condition |
|---|---|---|---|
| `file_exists` | `path` | — | `os.path.exists(workspace/path)`. |
| `file_contains` | `path`, `pattern` | — | `re.search(pattern, content, re.MULTILINE)` matches the file body. (This is the keep-list "content regex" check.) |
| `glob_exists` | `pattern` | — | At least one `glob.glob(pattern, recursive=True)` match. |
| `pytest_pass` | `path` | `markers` | `python3 -m pytest <path> -q [-m markers]` exits 0 within 120 s. |
| `exit_code` | — | `expected` (default 0) | Architect subprocess exit code equals `expected`. |
| `file_shape` | `path`, `format` (`csv` / `json` / `jsonl`) | `min_rows`, `max_rows`, `min_columns`, `required_columns` (csv), `required_keys` (json / jsonl) | All supplied predicates hold. |
| `command_succeeds` | `cmd` (list) | `cwd_relative` | `subprocess.run(cmd, timeout=60)` exits 0. |

Each check returns
`{"type": ctype, "passed": bool, "detail": str, ... echoed-input
fields ...}`. `case` short-circuits with `passed=False` if a check
type is unrecognised.

**Hello-file smoke case.** `cases/trivial/hello-file.json`
declares two checks (`file_exists path=hello.txt`,
`file_contains path=hello.txt pattern="Hello from UAS"`). The
PLAN's intended behaviour is "exit 0; both checks pass; full
JSONL row emitted" — Phase 1 §10 confirmed plumbing works
end-to-end; the `passed=False` outcome (architect over-decomposed
into 5 TDD steps and never produced the file) is the
scaffold-ceiling signal that motivated the May 2026 pivot. The
case stays as the canonical regression gate for Phase 4's
post-prune verification.

**Phase-3 consumption pattern.** The orchestrator does **not**
invoke the architect. It uses the eval substrate two ways:

1. **Regression gate.** After every Phase 4 deletion batch, run
   `./uas-eval` against the hello-file case to confirm the kept
   set is unbroken. Regression is observable as: bash wrapper
   exits 0, JSONL row emitted, `passed=true` ideally — but the
   §10 result establishes that `passed=false` for a `hello.txt`
   case is currently a model+architect-coupling outcome, not a
   substrate failure. Phase 4's regression gate is "the harness
   ran end-to-end and produced a well-formed row" (matching the
   amended Phase 1 exit criteria), not "passed=true".
2. **Check primitives.** The seven deterministic check types are
   useful for verifying orchestrator-side properties (e.g., a
   subtask was supposed to write a file at `<workspace>/foo.csv`
   with `min_rows=10`). Phase 3 may either import `run_check`
   directly or copy it into a thinner module. Importing is fine
   today; copying is justified once Phase 4 deletes the rest of
   `eval.py`'s body and the import surface shrinks.

**Gaps.**

- No structured per-tier API. Tiers exist as directory names;
  there is no "evaluate against tier X" library entry point apart
  from `--tier`.
- The `architect/main.py` invocation path is itself the
  largest cut surface in Phase 4. Phase 3 must make sure every
  check type it consumes lives in `run_check` and not in a
  helper that depends on architect internals.
- `run_check` mixes the keep-list types with `llm_judge`. Phase 4
  prunes `llm_judge`; the dispatcher needs a clean cut without
  re-flowing the surrounding control flow.

## 8. Claude Code rate-limit read pattern

**Location.** Two surfaces coexist (Phase 2 §1 Results in
`PLAN.md` is the primary reference for both):

- **Headless default — stream-json `rate_limit_event`.** Emitted
  by `claude --print --output-format stream-json --verbose
  --dangerously-skip-permissions <prompt>`. Built-in event;
  `--include-hook-events` is **not** required.
- **TUI companion — statusline JSON `rate_limits` field.**
  Delivered to a configured `statusLine` script via stdin during
  interactive `claude` sessions. Probe lives at
  `tools/statusline_probe.sh`; wired via the
  `.claude/settings.local.json` `statusLine` block (see PLAN.md
  §Section 1 — Results §Probe artefacts for the exact JSON
  block to re-add when needed).

**Accepts.**

- Headless surface: standard `claude --print` invocation arguments
  — same flags any worker would use.
- Companion surface: a working `statusLine` configuration in
  `.claude/settings.local.json` plus an interactive `claude`
  session running in some terminal (any process; not the worker
  itself). The probe script reads the JSON from stdin and writes
  it to `/tmp/uas_statusline_probes/`.

**Emits.**

- Headless: a JSON object on stdout with shape

  ```
  {
    "type": "rate_limit_event",
    "rate_limit_info": {
      "status":          "allowed" | <other ternary states>,
      "resetsAt":        <unix ts>,
      "rateLimitType":   "five_hour" | "seven_day",
      "overageStatus":   "allowed" | <other states>,
      "overageResetsAt": <unix ts>,
      "isUsingOverage":  <bool>
    },
    "uuid":       <string>,
    "session_id": <string>
  }
  ```

  Phase 2 §1 observed only `five_hour` events; whether `seven_day`
  also fires is left to Phase 3 iteration.

- TUI companion: full statusline JSON payload, including

  ```
  "rate_limits": {
    "five_hour":  {"used_percentage": <int 0..?>, "resets_at": <unix ts>},
    "seven_day":  {"used_percentage": <int 0..?>, "resets_at": <unix ts>}
  }
  ```

  Plus surrounding context fields (`cost.total_cost_usd`,
  `context_window.used_percentage`, `model.id`,
  `model.display_name`, `version`, `effort.level`,
  `thinking.enabled`, `fast_mode`) — useful budget signals for
  Phase 3.

**Lifecycle.**

- Headless event fires once per worker invocation; Phase 3 parses
  it from the worker's own stream-json output (no extra calls).
- TUI companion is a long-running interactive session managed
  separately from the workers; the probe script writes a fresh
  payload every time Claude Code refreshes the statusline (every
  few seconds during active interaction).

**Phase-3 consumption pattern.** Three options, decided at Phase
3 design start:

1. **Ternary-only.** Read `rate_limit_info.status` /
   `overageStatus` / `isUsingOverage` from each worker's
   stream-json. Sufficient for the free / paid / hard-stop
   policy machine. Simplest plumbing, no extra processes.
2. **TUI companion read.** Spawn a long-running
   `claude` interactive session as a percentage-progress side
   channel. Adds a process and a probe-file watcher; gives
   `used_percentage` for nicer policy ramps (e.g., "wrap up at
   80% of weekly").
3. **Internal usage accumulator.** Track per-call `usage.*`
   tokens against a learned cap; never read the rate-limit
   surface directly. Most decoupled; relies on accurate per-call
   accounting and a stable cap.

The default expectation per ROADMAP §Pivot is option 1 with
optional ramp-up to option 2 if option 1's transitions feel too
sharp during Phase 5 real-task use.

**Gaps.**

- Statusline hook does **not** fire under headless `--print`
  (Phase 2 §1, finding 1). Confirmed via `--debug-file`:
  "Registered 0 hooks from 0 plugins" in headless startup.
  `used_percentage` is unreachable from the worker subprocess
  itself.
- Schema differences between the two surfaces:
  `resetsAt` (camelCase, headless) vs `resets_at` (snake_case,
  statusline); ternary `status` only headless; `used_percentage`
  only statusline; `overageStatus` / `isUsingOverage` only
  headless.
- **`used_percentage` cap behaviour at 100% is unresolved** —
  see "Open questions" below. Phase 3 must support both the
  caps-at-100 and the climbs-past-100 hypotheses if it adopts
  the TUI companion option.
- `seven_day` `rate_limit_event` emission unobserved in §1; left
  to Phase 3 iteration to confirm.

## Open questions

### `used_percentage` cap behaviour at the 5-hour / weekly limit

**Status:** unresolved. Phase 2 §2 deferred to natural-trigger
capture rather than running the deliberate-push probe. See PLAN.md
§Section 2 — Results for the rationale.

**The question.** When the 5-hour or weekly Claude Max window is
exhausted and paid buffer engages, does
`rate_limits.{five_hour,seven_day}.used_percentage` (read from the
TUI statusline JSON payload — see Phase 2 §1 Results, finding 2):

- **Cap at 100%** — the field clamps and buffer state must be
  detected via the stream-json `rate_limit_event`'s `overageStatus`
  / `isUsingOverage` (see §1 Results, finding 3) instead?
- **Climb past 100%** — the field is uncapped and the
  >100 threshold itself signals buffer entry, with magnitude
  readable directly from the percentage value?

**Why it's not blocking.** Headless `claude --print` workers — the
Phase 3 orchestrator's primary execution surface — do not see
`used_percentage` at all (Phase 2 §1, finding 1). They see the
ternary `rate_limit_event` only. `used_percentage` is reachable
only via the optional TUI-statusline companion read pattern, which
is one of three Phase 3 design options (ternary-only;
TUI-statusline companion; internal usage accumulator). If Phase 3
picks ternary-only or internal accumulator, this question is moot.

**Constraint on Phase 3 design.** If Phase 3 picks the
TUI-statusline companion option, its state machine must support
both interpretations until the wild observation arrives — i.e.,
treat any reading at or above 100 as "buffer engaged" without
attempting to read overflow magnitude from the percentage value
unless the climb-past-100 hypothesis is confirmed.

**Natural-trigger capture plan.** When the project owner
organically crosses the 5-hour cap during real-task use (Phase 5+),
capture one TUI statusline payload at that moment via
`tools/statusline_probe.sh` (the §1 probe; re-wire via the
`statusLine` block in `.claude/settings.local.json` per the JSON
snippet in PLAN.md §Section 1 — Results §Probe artefacts) and
append the finding here. No Phase 2 re-open required.
