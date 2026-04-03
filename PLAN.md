# UAS Improvement Plan

Improvements to the UAS harness informed by Claude Code (`src/`) architecture patterns.
Each section is self-contained and designed to be completed in a single coding agent session.
Sections are ordered by dependency — earlier sections do not depend on later ones.

Mark a section complete by changing `[ ]` to `[x]` in its heading.

---

## Section 1: Token & Cost Tracking [x]

**Problem:** UAS has zero visibility into token consumption or estimated cost per run/step.
Claude Code tracks `totalCostUSD` and `modelUsage` (per-model input/output/cache token counts)
in `src/bootstrap/state.ts` and accumulates them across every API call.

**Goal:** Track input/output tokens and estimated cost for every LLM call, aggregate per-step
and per-run, persist in state, and surface in logs + the final report.

**Files to modify:**
- `orchestrator/llm_client.py` — Parse token usage from Claude CLI JSON output (the CLI's
  `-p` flag with `--output-format json` emits a JSON envelope containing `usage`). Switch to
  `--output-format json` and extract `usage.input_tokens`, `usage.output_tokens` from the
  response. Return a `(text, usage_dict)` tuple instead of a bare string. Callers that only
  need text can destructure or use a helper.
- `architect/state.py` — Add `token_usage` and `cost_usd` fields to the step dict and a
  run-level `total_tokens` / `total_cost_usd` accumulator. Update `save_state` to persist them.
- `architect/main.py` — After each LLM call (planner and executor), accumulate the returned
  usage into the step and run totals. Log a one-line summary after each step
  (`Step N used Xk input + Yk output tokens ≈ $Z`).
- `orchestrator/main.py` — Same accumulation for orchestrator-level LLM calls (build phase).
- `architect/report.py` — Add a "Cost" column to the steps table and a run-total row in the
  HTML report.
- `architect/dashboard.py` — Show running cost in the dashboard header.

**Cost model (hardcoded, easy to update later):**
```python
COST_PER_1K = {
    "claude-opus-4-6":   {"input": 0.015, "output": 0.075},
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5":  {"input": 0.0008, "output": 0.004},
}
```

**Acceptance criteria:**
- `state.json` contains `total_tokens` and `total_cost_usd` after a run.
- Each step in `state.json` has `token_usage: {input, output}` and `cost_usd`.
- Logs show per-step token/cost summary.
- HTML report shows cost breakdown.

---

## Section 2: Structured Error Classification [x]

**Problem:** `llm_client.py` uses flat substring lists (`TRANSIENT_PATTERNS`,
`_OVERLOADED_PATTERNS`) and a single retry loop. Claude Code (`src/services/api/errors.ts`)
categorizes errors into distinct types with dedicated recovery strategies:
`prompt_too_long`, `rate_limit`, `capacity`, `connection`, `auth`, `media_size`, etc.

**Goal:** Replace ad-hoc pattern matching with a structured error classifier that returns a
typed error with a recommended recovery action, enabling callers to respond differently to
different failures.

**Files to modify:**
- `orchestrator/llm_client.py` — Create an `LLMError` dataclass:
  ```python
  @dataclasses.dataclass
  class LLMError:
      category: str          # "rate_limit" | "capacity" | "auth" | "connection" | "timeout" | "prompt_too_long" | "output_truncated" | "unknown"
      message: str
      retryable: bool
      recommended_backoff: float  # seconds, 0 if not retryable
      raw_output: str
  ```
  Add a `classify_error(returncode, stdout, stderr) -> LLMError` function that replaces the
  inline `_is_transient` / `_is_overloaded` / `_is_auth_error` checks. Update the retry loop
  in `generate()` to use `LLMError.category` for branching:
  - `auth` → raise immediately, never retry
  - `rate_limit` → longer backoff (current `OVERLOADED_BACKOFF`)
  - `capacity` → moderate backoff, limited retries (3 max, matching Claude Code's `MAX_529_RETRIES`)
  - `connection` / `timeout` → short backoff, full retry budget
  - `prompt_too_long` → raise with flag so caller can attempt context reduction
  - `output_truncated` → return partial output (current behavior, now explicit)
  - `unknown` → raise
- `architect/main.py` — Update the `_call_with_rate_limit_retry` wrapper (or equivalent) to
  accept `LLMError` instead of pattern-matching on raw strings. Log the error category in
  structured events.
- `architect/events.py` — Add `LLM_ERROR` event type with `category` field.

**Acceptance criteria:**
- `classify_error` has unit-testable logic (pure function, no I/O).
- All retry decisions flow through `LLMError.category`.
- `_is_transient`, `_is_overloaded`, `_is_auth_error` functions are removed.
- Error category appears in event log entries.

---

## Section 3: Persistent Retry for Unattended Runs [x]

**Problem:** UAS has fixed retry counts (`MAX_RETRIES = 4` in llm_client, `MAX_RATE_LIMIT_RETRIES = 3`
in architect). For long-running unattended runs, a transient outage that exceeds the retry
budget kills the entire run. Claude Code (`src/services/api/withRetry.ts`) has a "persistent
retry" mode (`CLAUDE_CODE_UNATTENDED_RETRY`) with indefinite exponential backoff (capped at
5 min), heartbeat messages every 30 s, and a 6-hour reset cap.

**Goal:** Add a `UAS_PERSISTENT_RETRY` env var that, when enabled, switches the LLM client
to indefinite retry with capped exponential backoff and periodic heartbeat logging.

**Files to modify:**
- `orchestrator/llm_client.py` — When `UAS_PERSISTENT_RETRY=1`:
  - Remove the retry-count ceiling in `generate()`.
  - Cap backoff at `MAX_BACKOFF = 300` seconds (5 min).
  - Reset the backoff multiplier after 6 hours of continuous retrying.
  - Log a heartbeat every `HEARTBEAT_INTERVAL` (30 s) during backoff waits
    so the user/CI knows the process is alive.
  - Still raise immediately for non-retryable errors (`auth`, `unknown`).
  Depends on Section 2's `LLMError` — if Section 2 is not yet done, gate
  persistent retry on the existing `_is_transient` check instead.
- `architect/main.py` — Same persistent-retry logic for the architect-level rate-limit
  retry wrapper. Emit `PERSISTENT_RETRY_WAIT` events so the event log captures long waits.

**Acceptance criteria:**
- With `UAS_PERSISTENT_RETRY=1`, a simulated 429 error retries indefinitely with backoff.
- Backoff caps at 300 s.
- Heartbeat log lines appear during waits.
- Non-retryable errors still raise immediately.
- Without the env var, behavior is unchanged (fixed retry budget).

---

## Section 4: Layered Configuration System [x]

**Problem:** UAS configuration is entirely through env vars (~25 `UAS_*` variables). Claude
Code has a layered config system (`src/utils/config.ts`, `src/utils/settings/`): project-level
`.claude/config.json`, user-level `~/.claude/settings.json`, and env-var overrides. This gives
users persistent per-project defaults without polluting their shell environment.

**Goal:** Add a `uas.toml` (or `.uas/config.toml`) config file that provides defaults for all
current `UAS_*` env vars, with env vars taking precedence over the file. Use TOML for
readability.

**Files to create:**
- `uas/config.py` — New module:
  - `load_config() -> dict` — Looks for config in this order (later overrides earlier):
    1. Built-in defaults (hardcoded dict matching current env-var defaults)
    2. `~/.config/uas/config.toml` (user-global)
    3. `{workspace}/.uas/config.toml` (project-level)
    4. `UAS_*` env vars (highest priority)
  - `get(key, default=None)` — Convenience accessor.
  - Uses `tomllib` (stdlib in Python 3.11+) for parsing.
  - Config keys use snake_case matching the env var names without the `UAS_` prefix
    (e.g., `max_parallel`, `sandbox_mode`, `model`).

**Files to modify:**
- `architect/main.py` — Replace `os.environ.get("UAS_*")` calls at module level with
  `config.get("*")` calls. Import and initialize config at startup.
- `orchestrator/main.py` — Same replacement.
- `orchestrator/llm_client.py` — Same replacement for `UAS_MODEL`, `UAS_LLM_TIMEOUT`, etc.
- `architect/executor.py` — Same replacement for `UAS_SANDBOX_MODE`, `UAS_MAX_CONTEXT_LENGTH`.
- `architect/state.py` — Same replacement for `UAS_WORKSPACE`.

**Example `uas.toml`:**
```toml
model = "claude-sonnet-4-6"
model_planner = "claude-opus-4-6"
sandbox_mode = "local"
max_parallel = 4
max_error_length = 3000
workspace = "/workspace"
persistent_retry = false
```

**Acceptance criteria:**
- `load_config()` merges all 4 layers correctly (env > project > user > defaults).
- Existing env-var behavior is fully preserved (no breaking change).
- A sample `uas.example.toml` is created showing all keys with comments.
- All `os.environ.get("UAS_*")` calls in the listed files are replaced.

---

## Section 5: Run Artifact Lifecycle Management [x]

**Problem:** `.uas_state/runs/` accumulates artifacts forever with no cleanup. Claude Code
manages session storage with flush-on-shutdown and has bounded history. For UAS, a long-lived
workspace can accumulate gigabytes of run data (code versions, specs, event logs, reports).

**Goal:** Add a retention policy that automatically prunes old runs, plus a manual `uas prune`
command.

**Files to modify:**
- `architect/state.py` — Add:
  - `prune_old_runs(keep_last: int = 10, max_age_days: int = 30)` — Delete run directories
    older than `max_age_days` OR beyond `keep_last`, whichever is more aggressive. Always
    keep the latest run. Log each deleted run.
  - `get_run_disk_usage(run_id: str) -> int` — Sum file sizes in a run directory.
  - `list_runs_with_metadata() -> list[dict]` — Return run_id, created_at, status, disk usage.
- `architect/main.py` — Call `prune_old_runs()` at the start of every new run (not resume),
  using config values `keep_last_runs` and `max_run_age_days`.
- `uas/config.py` (from Section 4, or env vars if Section 4 is not yet done) — Add
  `keep_last_runs` (default 10) and `max_run_age_days` (default 30) config keys.
- `entrypoint.sh` — Add a `prune` subcommand that calls
  `python3 -m architect.state prune [--keep N] [--max-age DAYS]`.
- `architect/state.py` — Add `if __name__ == "__main__"` block to support the CLI invocation.

**Acceptance criteria:**
- After 15 runs, only the last 10 are retained (by default).
- Runs older than 30 days are pruned regardless of count.
- The latest run is never pruned even if it exceeds `max_age_days`.
- `prune` subcommand works standalone.
- Pruning logs which runs were deleted and how much space was freed.

---

## Section 6: Streaming Progress Callbacks [x]

**Problem:** During LLM generation, UAS only shows heartbeat logs ("LLM responding... 15s
elapsed"). Claude Code streams every token via `content_block_delta` events, giving real-time
visibility. UAS already streams stdout line-by-line in `_run_streaming`, but this is raw CLI
output — not parsed for progress.

**Goal:** Add optional progress callbacks that fire during LLM generation, enabling the
dashboard to show live generation status (tokens generated, elapsed time, current phase).

**Files to modify:**
- `orchestrator/llm_client.py` —
  - Add `--output-format stream-json` to the CLI command (if supported by the installed
    Claude CLI version; fall back to current behavior if not). This emits one JSON object
    per event on stdout.
  - Add a `progress_callback: Callable[[dict], None] | None = None` parameter to `generate()`.
  - In `_run_streaming`, when stream-json mode is active, parse each stdout line as JSON and:
    - On `content_block_delta` events: call `progress_callback({"type": "delta", "text": ..., "tokens": ...})`.
    - On `message_start` events: call `progress_callback({"type": "start", "model": ..., "usage": ...})`.
    - On `message_stop` / `result` events: extract final text and usage.
  - If the CLI doesn't support `stream-json` (older version), fall back to current line-by-line
    logging — detect this by checking the first line of output.
- `architect/executor.py` — Pass a progress callback from the dashboard (if dashboard is active)
  through to the orchestrator's LLM client calls.
- `architect/dashboard.py` — Add a `on_llm_progress(step_id, event)` method that updates the
  "Claude Output" panel with streaming text and a token counter.

**Acceptance criteria:**
- With `--output-format stream-json` available, the dashboard shows live LLM output.
- Without it (fallback), behavior is identical to current.
- Progress callback is optional — `None` means no callbacks, no overhead.
- Token counts from stream-json feed into Section 1's cost tracking (if available).

---

## Section 7: Step Dependency Safety Classification [x]

**Problem:** UAS parallelizes independent steps (no dependency edges), but doesn't consider
whether steps touch the same files or resources. Two "independent" steps that both write to
`config.json` will race. Claude Code (`src/services/tools/toolOrchestration.ts`) partitions
tool calls into read-only (concurrent) vs. write (serial) batches based on a `isConcurrencySafe`
check.

**Goal:** Add file-conflict detection to the parallel scheduler so that steps declaring
overlapping output files are serialized even if they have no explicit `depends_on` edge.

**Files to modify:**
- `architect/planner.py` — Extend the decomposition prompt to request an `outputs` field
  per step: a list of file paths/globs that the step will create or modify. Add this to the
  step schema validation.
- `architect/main.py` — In the parallel execution scheduler (the section that groups steps
  by topological level), add a conflict check:
  ```python
  def find_file_conflicts(steps: list[dict]) -> list[tuple[int, int]]:
      """Return pairs of step IDs whose declared outputs overlap."""
  ```
  Steps with overlapping outputs within the same execution level should be serialized
  (run sequentially within that level, or split into sub-levels).
- `architect/state.py` — Persist the `outputs` field in step dicts.

**Acceptance criteria:**
- Steps declaring overlapping output files are never run concurrently.
- Steps with disjoint outputs still run in parallel.
- The `outputs` field is present in decomposition output and state.json.
- A log message warns when file conflicts force serialization.

---

## Section 8: Hook System for Extensibility [ ]

**Problem:** UAS has a structured event system (`events.py`) but no way for users to attach
custom behavior to lifecycle events. Claude Code (`src/types/hooks.ts`, `src/utils/hooks.ts`)
has a full hook system where users register shell scripts that execute on events like
`PreToolUse`, `PostToolUse`, `SessionStart`, etc., with JSON I/O for bidirectional
communication.

**Goal:** Add a lightweight hook system where users can register scripts that run at key
lifecycle points, receiving event data as JSON on stdin and optionally returning control
directives on stdout.

**Files to create:**
- `uas/hooks.py` — New module:
  - `HookEvent` enum: `PRE_STEP`, `POST_STEP`, `PRE_REWRITE`, `POST_REWRITE`,
    `PRE_PLAN`, `POST_PLAN`, `RUN_START`, `RUN_COMPLETE`, `STEP_FAILED`.
  - `HookConfig` dataclass: `event: HookEvent`, `command: str`, `timeout: int = 30`.
  - `load_hooks(config_path: str) -> list[HookConfig]` — Load from `.uas/hooks.toml`
    or `hooks` section of `uas.toml` (if Section 4 is done).
  - `run_hook(event: HookEvent, data: dict, hooks: list[HookConfig]) -> dict | None` —
    Execute matching hooks. Pipe `data` as JSON to stdin. Parse stdout as JSON if non-empty.
    Return the merged hook output (or `None` if no hooks matched).
    Timeout enforcement. Stderr goes to logger.
  - Hook stdout can return `{"abort": true, "reason": "..."}` to halt the current operation.

**Files to modify:**
- `architect/main.py` — Call `run_hook(PRE_STEP, ...)` before each step execution and
  `run_hook(POST_STEP, ...)` after. If `PRE_STEP` returns `abort`, skip the step and mark
  it failed with the hook's reason. Call `RUN_START` / `RUN_COMPLETE` at run boundaries.
- `architect/planner.py` — Call `run_hook(PRE_PLAN, ...)` before decomposition and
  `run_hook(POST_PLAN, ...)` after, passing the plan as data. `POST_PLAN` can modify the
  plan (return a `steps` field to override).

**Example `.uas/hooks.toml`:**
```toml
[[hooks]]
event = "POST_STEP"
command = "python3 .uas/notify_slack.py"
timeout = 10

[[hooks]]
event = "PRE_PLAN"
command = ".uas/validate_plan.sh"
```

**Acceptance criteria:**
- Hooks fire at the correct lifecycle points.
- Hook scripts receive JSON on stdin with event type and relevant data.
- `abort` response from a `PRE_STEP` hook prevents step execution.
- `POST_PLAN` hook can modify the step list.
- Missing/failing hook scripts log a warning but don't crash the run.
- Without any hooks configured, zero overhead (no subprocess spawning).
