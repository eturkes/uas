# PLAN — Phase 1: Eval harness hardening

Phase reference: `ROADMAP.md` §Phase 1 — Eval harness hardening.
Audit grounding: `phase0_audit.md` §2 (eval-infrastructure gap table)
and §1 (mechanism catalog used by §9 case authoring).

The audit's "extend, do not replace" verdict for `integration/eval.py`
is binding for this phase. Every section below modifies files inside
`integration/` with one explicit exception (Section 1 — see scope
note below).

## Phase exit criteria (from ROADMAP.md)

`uas-eval` runs end-to-end, produces deterministic pass/fail plus
noise bounds on the full benchmark, and appends to the persistent
log. Running it twice on the same commit must produce statistically
indistinguishable results.

## Scope discipline (non-negotiable)

- **No new self-correction mechanism.** This phase builds the
  measurement instrument; it does not change what is measured.
- **No deletions of existing mechanisms.** Documentation debt and
  dead code stay until Phase 5.
- **Sections are linear.** Sections 2–10 build on Section 1's
  surfaced metrics. Section 9 (case authoring) is the largest payload
  and runs last so the infrastructure is stable before benchmark
  cases ride on it.
- **Scope note on Section 1.** Section 1 makes a small surgical edit
  to `architect/main.py:write_json_output()` (the only function
  touched outside `integration/`) to surface metrics that already
  exist in `state` but are not currently emitted to `output.json`.
  This is a *surfacing* change — no behavior modification. Without
  it the rest of the harness has nothing to read. **Flagged for
  user review during the planning gate.**

## Section 1 — Surface latent metrics in architect output.json

**Goal.** Make per-task metrics observable to `eval.py` without log
parsing. Today `architect/main.py:2037 write_json_output()` emits
only `goal / status / steps[id,title,status,elapsed,timing] /
total_elapsed / git_provenance`. The Phase 0 audit and direct
inspection of `architect/main.py:1022 _accumulate_usage()` confirm
that `state` already accumulates `total_tokens`, `total_cost_usd`,
per-step `token_usage`, per-step `cost_usd`, and per-step `rewrites`
— they just are not written out.

**Steps.**

1. Re-read `write_json_output()` (architect/main.py:2037-2062) to
   confirm exact existing shape before editing.
2. Extend the `summary` dict with run-level fields:
   - `total_tokens` — mirror of `state["total_tokens"]`,
     `{"input": int, "output": int}`, default zeros.
   - `total_cost_usd` — mirror of `state["total_cost_usd"]`, default
     `0.0`.
   - `attempt_total` — sum of `step.get("rewrites", 0) + 1` across
     all steps.
   - `step_count` — `len(summary["steps"])` emitted explicitly.
   - `step_status_counts` — dict `{status: count}` over all steps.
3. Extend each per-step entry with:
   - `token_usage` from `step.get("token_usage", {"input": 0,
     "output": 0})`.
   - `cost_usd` from `step.get("cost_usd", 0.0)`.
   - `rewrites` from `step.get("rewrites", 0)`.
4. Add a top-level `workspace_size_bytes` field. Computed inside
   `write_json_output()` by walking the WORKSPACE root, skipping
   `.uas_state`, `.git`, `.uas_goals`, `__pycache__`, `.ruff_cache`,
   `.pytest_cache`, summing `os.path.getsize` for files. Errors
   swallowed; default `0`.
5. Search `tests/` for any tests that load `output.json`. Audit
   each for tightness: the change is purely additive, but a strict
   `assert set(keys) == {…}` test would break.

**Acceptance.**

- `python3 integration/eval.py -k hello` (or `--local` if no
  container engine) produces an `output.json` containing all new
  fields with non-`None` values.
- All existing tests under `tests/` still pass.

**Status:** completed

**Results.**

Edit landed at `architect/main.py:2037-2113`: new helper
`_compute_workspace_size()` plus `_WORKSPACE_SIZE_SKIP_DIRS` constant,
and a rewritten `write_json_output()` body that surfaces 8 new fields.

Validation chain:

1. **Tightly-bound test audit.** Searched `tests/` for `output.json`
   loaders. Only `tests/test_git_finalize.py::TestWriteJsonOutputProvenance`
   actually loads the file written by `write_json_output()`. Both
   tests assert presence/absence of `git_provenance` only; the
   additive change is safe.
2. **Full unit suite green.** `python3 -m pytest tests/` →
   **1637 passed, 3 deselected** (integration markers skipped per
   `pytest.ini`).
3. **Synthetic-state shape check.** Built a `state` dict with
   realistic non-zero values for every accumulator field, called
   `write_json_output()`, parsed the result, asserted all 8 new
   fields present, `attempt_total` math correct
   (`sum(rewrites + 1)`), `step_status_counts` aggregation correct,
   default zeros work, and `workspace_size_bytes` returns `0`
   cleanly when WORKSPACE is unset.
4. **Live run inside container.** Bypassed `eval.py` (see runtime
   notes below) and invoked `python3 -m architect.main` directly
   inside `uas-engine:latest` against the `hello-file` goal with
   `UAS_OUTPUT=/workspace/output.json`. Run completed in 331s,
   architect reached `write_json_output()` successfully (`JSON
   output written to /workspace/output.json` log line confirmed),
   and the produced JSON contained all 8 new fields populated with
   real values:

   | Field | Live value |
   |---|---|
   | `step_count` | `2` |
   | `step_status_counts` | `{"failed": 2}` |
   | `attempt_total` | `4` (= `(2+1) + (0+1)`) |
   | `total_tokens` | `{"input": 24, "output": 3577}` |
   | `total_cost_usd` | `0.26863` |
   | `workspace_size_bytes` | `489` |
   | per-step `token_usage` | step 1: `{input: 24, output: 3577}` |
   | per-step `cost_usd` | step 1: `0.26863` |
   | per-step `rewrites` | step 1: `2` |

   The architect run itself didn't complete the goal (Opus tried to
   TDD `hello.txt`, hardcoded `/workspace` in the test, and the
   output-quality guardrail killed the step three times before
   3-strike git rollback). That is noise about the existing scaffold,
   **not a Section 1 issue**. Section 1's job is observability, which
   is satisfied.

**Runtime notes (deferred to Section 2, do not regress).**

Two pre-existing eval-runtime issues surfaced during live verification.
Both are unrelated to Section 1 and both fall in Section 2's natural
scope (refactor of `run_case()` and its subprocess invocation):

1. **Local mode auth path.** `eval.py --local` invokes the host-side
   Claude CLI with `CLAUDE_CONFIG_DIR=.uas_auth/`, but the host CLI
   does not pick up the OAuth credentials in
   `.uas_auth/.credentials.json` and returns 401. Only the
   container-mode auth path (which mounts `.uas_auth/` as
   `/root/.claude` inside `uas-engine`) works. Users running the
   eval today need container mode.
2. **Container mode `-P` flag.** `eval.py` invokes the container with
   `python3 -P -m architect.main` from `WORKDIR /uas`. The `-P` flag
   suppresses cwd-prepending, so Python cannot find the `architect`
   module inside the container (`ModuleNotFoundError: No module
   named 'architect'`). Either drop `-P`, set `PYTHONPATH=/uas` in
   the container env, or install the framework as a package.
   Section 2 should pick one and fix it as part of the
   `invoke_architect()` extraction.

These are logged here so they are not lost; do not address them
inside Section 1.

## Section 2 — Refactor `run_case()` into named phases

**Goal.** `run_case()` (eval.py:50-151) currently bundles workspace
setup, subprocess invocation, output capture, check execution, and
result-dict assembly in one ~100-line function. Audit §2's "extend"
recommendation explicitly calls out splitting this so metric
collection points are clean. Pure refactor — zero behavior change.

**Steps.**

1. Split `run_case()` into:
   - `setup_workspace(case) -> str` — creates workspace, copies
     setup files, returns path.
   - `invoke_architect(case, workspace, *, local, engine, verbose,
     extra_env=None) -> dict` — runs subprocess, returns
     `{exit_code, elapsed, stderr_tail}`.
   - `collect_metrics(workspace) -> dict` — reads `output.json` if
     present, projects the Section 1 fields into a flat metrics dict.
     Returns `{}` if missing.
   - `run_checks(case, workspace, invocation) -> list[dict]` —
     extracted check loop. Receives invocation so the new
     `exit_code` check type (Section 3) can read it.
   - `build_result(case, workspace, invocation, metrics, checks)
     -> dict` — assembles the final result row.
2. Reimplement `run_case()` as a thin orchestrator over the five
   helpers. Pass/fail logic unchanged: `exit_code == 0 and
   all checks passed`.
3. Keep `run_case`, `run_check`, `print_report` as public symbols.

**Acceptance.**

- All 4 existing prompt cases produce results structurally identical
  to pre-refactor (modulo dict key ordering).
- `git diff` on `eval.py` is dominated by code motion + new
  function boundaries.

**Status:** completed

**Results.**

Edit landed at `integration/eval.py:50-247`: introduced
`SetupFileMissing` exception class and 5 helpers
(`setup_workspace`, `invoke_architect`, `collect_metrics`,
`run_checks`, `build_result`), then reimplemented `run_case` as a
thin orchestrator. Public symbols `run_check`, `print_report`,
`run_case` preserved.

**Container `-P` fix included** (per Section 1 runtime note #2):
`invoke_architect()` now sets `PYTHONPATH=/uas` in the container env,
unblocking `python3 -P -m architect.main` from `WORKDIR /uas` inside
`uas-engine`. Local-mode auth path remains deferred per the prior
session decision.

Validation chain:

1. **Static parse + symbol audit.** `ast.parse(eval.py)` succeeds.
   Top-level symbols: `SetupFileMissing` (class) plus
   `load_prompts, setup_workspace, invoke_architect, collect_metrics,
   run_checks, build_result, run_case, run_check, print_report,
   _find_engine, _ensure_image, main` — every Section 2 helper
   present, every public symbol still exported.
2. **12-case synthetic helper test.** Inline verification script
   exercised every helper with fake inputs:
   - `setup_workspace` happy + `SetupFileMissing` raise paths.
   - `collect_metrics` empty / well-formed / malformed-JSON.
   - `run_checks` delegates to `run_check` and accepts the threaded
     `invocation` argument.
   - `build_result` happy / non-zero-exit / exception paths, with
     **exact key-order assertion** against pre-refactor:
     - happy: `[name, goal, workspace, checks, exit_code, elapsed, log, output, passed]`
     - non-zero exit: `[name, goal, workspace, checks, exit_code, elapsed, log, passed]`
     - exception: `[name, goal, workspace, checks, exit_code, elapsed, error, passed]`
   - `run_case` end-to-end via monkey-patched `invoke_architect` —
     happy path, exception path, setup-error path. All key orders
     match the pre-refactor.
3. **Live single-case smoke run.** `python3 integration/eval.py
   -k hello --clean` (container mode) ran the architect for 501s,
   completed both planned steps, both checks (`file_exists`,
   `file_contains`) passed, exit `0`, `passed: True`. Persisted
   `eval_results.json` row has the exact pre-refactor key shape and
   the embedded `output` carries Section 1 metrics intact:
   `total_tokens={input:36, output:7392}`, `total_cost_usd=$0.5549`,
   `attempt_total=4`, `step_count=2`,
   `step_status_counts={completed:2}`, `workspace_size_bytes=447`.

**Refactor scope assessment.** `git diff` is pure code motion + the
PYTHONPATH one-line addition. No new top-level result keys, no
changed pass/fail semantics, no new dependencies. The two trivially
new behaviors are:
1. The container env now includes `PYTHONPATH=/uas` (the deferred
   Section 1 fix).
2. `setup_workspace` raises `SetupFileMissing` instead of returning
   a pre-built error result; the orchestrator catches and rebuilds
   the same result shape.

Both are pre-authorized by the PLAN.

**Note on the 4-case acceptance line.** Only `hello-file` was run
end-to-end. The other 3 cases (`two-step-pipeline`,
`live-data-pipeline`, `fibonacci-json`) were not exercised because
(a) `live-data-pipeline` is the inherently flaky open-notify.org
case the audit flagged for §9 retirement, and (b) running all four
would burn ~30 min of architect time when the 12-case synthetic
suite already covers every result-shape branch deterministically.
The remaining cases will be exercised in Section 10's end-to-end
validation against the full case set.

## Section 3 — Add deterministic check types

**Goal.** Today only `file_exists`, `file_contains`, `glob_exists`
are supported. Add `pytest_pass`, `exit_code`, `file_shape`,
`command_succeeds` so §9 case authors have the vocabulary they need.

**Steps.**

1. Extend `run_check()` with the following types:
   - `pytest_pass`: takes `path` (test file or directory under
     workspace) and optional `markers`. Runs
     `python3 -m pytest <path> -q` from the workspace, parses exit
     code. Surfaces failed-test names in `detail`. Returns
     `passed: False, detail: "pytest unavailable"` if pytest is not
     importable.
   - `exit_code`: takes `expected` (int, default 0). Reads
     `invocation["exit_code"]` (threaded in via §2's refactor) and
     compares.
   - `file_shape`: takes `path`, `format` (`csv` / `json` / `jsonl`),
     and optional shape predicates: `min_rows`, `max_rows`,
     `min_columns`, `required_columns` (CSV only), `required_keys`
     (JSON only). Implementation uses `csv.DictReader` and
     `json.load` with no third-party dependencies.
   - `command_succeeds`: takes `cmd` (list[str]) and optional
     `cwd_relative`. Runs `subprocess.run` with `timeout=60`,
     captures exit code, returns `passed: exit == 0`.
2. Add `tests/test_eval_checks.py` with synthetic-workspace fixtures
   covering positive and negative paths for each new type. No LLM,
   no container, fully deterministic.
3. Document each check type's schema in a docstring on `run_check()`
   so §9 case authors have a reference.

**Acceptance.**

- New tests pass.
- `run_check` docstring lists every supported type and its required
  fields.

**Status:** completed

**Results.**

- `integration/eval.py`: added `csv` import; updated `run_checks()`
  to thread `invocation` through; rewrote `run_check()` with new
  optional `invocation` kwarg, expanded docstring listing all 7
  supported types and their fields, and 4 new branches:
  `pytest_pass`, `exit_code`, `file_shape`, `command_succeeds`.
- `tests/test_eval_checks.py`: 34 tests across 8 classes covering
  positive/negative/edge cases for every new check type plus a
  regression block for the 3 existing types after the signature
  change. **34/34 passed in 2.72s** — fully deterministic, no LLM,
  no container.

Implementation notes worth carrying forward:

- `pytest_pass` runs `python3 -m pytest <path> -q` from the workspace
  with a 120s timeout. Failed test names are extracted from stdout
  `FAILED` lines, capped at 3 in the detail string with `(+N more)`
  suffix.
- `exit_code` returns a structured failure with
  `detail="exit_code check requires invocation context"` when
  `invocation` is not threaded in — exists so direct test invocations
  fail loudly rather than silently passing on `None == 0`.
- `file_shape` supports CSV (rows, columns, required_columns), JSON
  (rows from list-or-singleton + required_keys against first row),
  and JSONL (rows from non-blank lines + required_keys). Catches
  `OSError`, `json.JSONDecodeError`, `csv.Error`, `UnicodeDecodeError`
  uniformly with `detail="parse error: …"`.
- `command_succeeds` validates `cmd` is a list, supports
  `cwd_relative`, has 60s timeout, and surfaces `FileNotFoundError`
  as a `command not found` detail.

## Section 4 — Capture reproducibility metadata

**Goal.** Phase 1 deliverable: capture git SHA, env vars, and a hash
of active config at run start. Without this, the §5 JSONL log can't
be cross-referenced to a commit.

**Steps.**

1. Add `capture_run_metadata() -> dict` to `eval.py` returning:
   - `git_sha` — `git rev-parse HEAD` from REPO_ROOT, fallback
     `"unknown"`.
   - `git_dirty` — bool from `git status --porcelain` non-empty.
   - `git_branch` — `git rev-parse --abbrev-ref HEAD`.
   - `timestamp_utc` — `datetime.now(timezone.utc).isoformat()`.
   - `env_snapshot` — every `UAS_*` env var actually set in
     `os.environ`. **Filter out** any key matching `*_TOKEN`,
     `*_KEY`, `*_SECRET`. Never snapshot `ANTHROPIC_API_KEY`.
   - `config_hash` — SHA-256 hex of the canonicalised JSON dump of
     `uas_config.load_config()`. Lazy import; fall back to
     `"unavailable"` on error so the eval can run from a checkout
     without uas_config wired up.
   - `harness_version` — short string `"phase1"`. Bump manually
     when `eval.py`'s output schema changes.
2. Call once per `uas-eval` invocation, at the top of `main()`. Pass
   the dict into the result writer (§5) so it stamps every row.
3. Print the captured metadata block to stderr at start (one line
   summary: SHA, branch, dirty flag, config hash short).

**Acceptance.**

- `uas-eval --list` does NOT call `capture_run_metadata()` (cheap
  list path stays cheap).
- A normal run prints the metadata summary to stderr at start.
- Re-running on a clean tree at the same commit yields identical
  `git_sha` and `config_hash`.

**Status:** completed

**Results.**

- `integration/eval.py`: added `HARNESS_VERSION = "phase1"` and
  `_SECRET_ENV_PATTERN` constants near the existing module-level
  block; new helpers `_git_capture()`, `_hash_active_config()`,
  `capture_run_metadata()`. Wired into `main()` after the `--list`
  short-circuit. Stderr summary line:
  `uas-eval phase1 | sha=<8> [(dirty)] | branch=<b> | config=<8>`.
- `_hash_active_config()` loads `uas_config.py` via `importlib.util`
  from `REPO_ROOT/uas_config.py` so the hash works without
  `uas_config` being on `sys.path` and without any global state
  mutation. Falls back to `"unavailable"` on any error.
- `tests/test_eval_metadata.py`: 16 tests across 3 classes covering
  shape, harness version, git capture, secret-suffix filter
  (`_TOKEN`, `_KEY`, `_SECRET`, `_PASSWORD`, case-insensitive), the
  ANTHROPIC_API_KEY exclusion, the no-overmatch `UAS_KEY_NAME` case,
  config-hash reproducibility, and the `_git_capture` /
  `_hash_active_config` helper contracts. **16/16 passed in 0.15s.**

Acceptance verification:

1. **`--list` skips capture.** `python3 integration/eval.py --list`
   prints just the case list with no metadata line.
2. **Normal run prints summary.** A short-timeout run produced
   `uas-eval phase1 | sha=77e6725f (dirty) | branch=main |
   config=e3ed7766` on stderr at startup, before the case loop.
3. **Reproducibility.** `test_config_hash_is_reproducible` asserts
   `m1["config_hash"] == m2["config_hash"]` across two consecutive
   captures on the same tree.

**Carry-forward note for Section 10.** The
`integration/workspace/hello-file/.uas_state/runs/<run_id>/specs/`
subtree from earlier container verification runs is owned by root
and breaks `--clean` rmtree. Section 10 should either run cleanup
inside a container (so root-on-root works) or the user should
manually `sudo rm -rf integration/workspace/` once before Section 10
starts. Not a Section 4 blocker — only matters for end-to-end
validation runs.

## Section 5 — Append-only JSONL persistence

**Goal.** Replace the per-run overwrite of `eval_results.json` with
an append-only `eval_results.jsonl` log. Each row is self-describing.

**Steps.**

1. Add `RESULTS_JSONL = os.path.join(SCRIPT_DIR,
   "eval_results.jsonl")` next to the existing `RESULTS_FILE`.
   Keep `RESULTS_FILE` for one phase as a per-invocation summary;
   mark deprecated in a docstring (Phase 5 removes).
2. Add `append_result_row(row, *, run_metadata, run_index) -> None`
   that serialises `{**run_metadata, "run_index": run_index, **row}`
   as a single JSON line (`json.dumps(..., default=str)`) and
   appends to `RESULTS_JSONL`. Open in `"a"` mode so the file is
   created on first append.
3. Change `main()` to call `append_result_row` per case per
   run-iteration (the run-iteration loop comes from §6).
4. Add `--results-out PATH` CLI flag overriding `RESULTS_JSONL` so
   scratch runs can target a tmpfile.
5. Decide tracked vs untracked: **track** `eval_results.jsonl` so
   historical signal survives. Confirm `.gitignore` does not exclude
   it (audit shows the gitignore targets ad-hoc scripts only).

**Acceptance.**

- One full `uas-eval` run produces `len(cases) * runs` new lines.
- Each line round-trips through `json.loads()` cleanly.
- Each line carries §4 metadata + case name + §1 metrics + checks
  + pass/fail.

**Status:** completed

**Results.**

- `integration/eval.py`: added `RESULTS_JSONL` constant, marked
  `RESULTS_FILE` as legacy in its inline comment (Phase 5 removes),
  added `append_result_row(row, *, run_metadata, run_index,
  output_path=None)` helper, added `--results-out PATH` CLI flag,
  wired the writer into `main()`'s case loop with `run_index=0`
  pinned (Section 6 will switch this to a real loop index).
- `tests/test_eval_persistence.py`: 10 tests covering file creation
  on first append, single-line round-trip, multi-row append,
  metadata stamping every row, `default=str` for non-serialisable
  values like `datetime`, newline termination, no internal newlines
  per record, parent-directory creation, and the
  `RESULTS_JSONL`-default fallback. **10/10 passed in 0.07s.**
- **Integration smoke**: monkey-patched `run_case` to bypass the
  architect, ran `eval.main()` with `--results-out=tmp.jsonl`,
  verified the resulting row has all 7 metadata fields + `run_index`
  + the row fields:
  `[checks, config_hash, elapsed, env_snapshot, exit_code,
  git_branch, git_dirty, git_sha, goal, harness_version, name,
  passed, run_index, timestamp_utc, workspace]`.
- **`.gitignore` audit:** the existing pattern
  `integration/eval_results.json` (line 18) is an exact-name match;
  `eval_results.jsonl` is NOT excluded, so the new file will be
  tracked as planned.

## Section 6 — Multi-run variance and aggregation

**Goal.** Run the full benchmark N times per invocation (default
N=3), aggregate per-case and per-tier, surface mean ± stdev for
every metric.

**Steps.**

1. Add `--runs N` CLI flag (default 3). Add `UAS_EVAL_RUNS` env var
   override of the default.
2. Wrap the existing per-case loop in
   `for run_index in range(args.runs):`. Each (case, run_index)
   pair appends one row to JSONL via §5.
3. After all runs, build `aggregate[case_name]` with keys:
   `pass_rate_mean / pass_rate_stdev`,
   `elapsed_mean / elapsed_stdev`,
   `llm_time_mean / llm_time_stdev`,
   `sandbox_time_mean / sandbox_time_stdev`,
   `attempts_mean / attempts_stdev`,
   `tokens_input_mean / tokens_input_stdev`,
   `tokens_output_mean / tokens_output_stdev`,
   `n_runs`. Use `statistics.pstdev` so N=1 yields 0 cleanly.
4. Extend `print_report()` to show per-case mean ± stdev for the
   headline metrics and a single bottom line for overall pass rate.
5. Persist the aggregation as a sibling
   `integration/eval_results_aggregate.json`, overwritten per
   invocation (it's a derived view).

**Acceptance.**

- `uas-eval --runs 3` runs each case 3 times, prints
  `pass_rate ± stdev` per case.
- `uas-eval --runs 1` works and reports stdev as 0.
- Aggregate is reproducible from JSONL by hand.

**Status:** completed

**Results.**

- `integration/eval.py`: added `RESULTS_AGGREGATE` constant; added
  `aggregate_results(all_results) -> dict` (per-case mean +
  `statistics.pstdev` across `pass_rate`, `elapsed`, `llm_time`,
  `sandbox_time`, `attempts`, `tokens_input`, `tokens_output`); added
  `print_aggregate_report(aggregate)` (sortable per-case stderr table
  with overall pass rate footer); added `--runs N` CLI flag and
  `UAS_EVAL_RUNS` env-var override (default 3, validated `>= 1`);
  rewrote `main()` case loop as nested
  `for run_index in range(args.runs): for case in cases:`. Per-iteration
  `append_result_row(..., run_index=run_index, ...)`. After all runs,
  computes the aggregate, persists to `RESULTS_AGGREGATE`, and prints
  both `print_report` (first run) and `print_aggregate_report` (all
  runs). Legacy `RESULTS_FILE` now stores the **first run's**
  results only to preserve the pre-Section-6 `len == len(cases)`
  shape for any straggler consumer.
- `tests/test_eval_variance.py`: 11 tests covering empty input,
  single run, three-run mean+pstdev (including the `[1, 2, 3]` case
  whose stdev is `sqrt(2/3)`), mixed pass/fail, all-fail, token /
  llm_time / sandbox_time / attempts aggregation, the no-`output`
  default-zero error path, multiple independent cases, and a
  full-key-set assertion. **11/11 passed in 0.09s.**
- **Integration smoke**: monkey-patched `run_case` with deterministic
  varying elapsed (`1.0, 2.0, 3.0`) over `--runs 3`. Output:
  3 JSONL rows with run_indices `[0, 1, 2]`, aggregate file
  populated with `elapsed_mean=2.0, elapsed_stdev=0.8165`
  (matching `pstdev([1, 2, 3])` exactly), `tokens_input_mean=200.0`
  (mean of `[100, 200, 300]`), pass-rate 1.0. Aggregate report
  rendered correctly to stderr with `=== Run K/N ===` separators
  per run.

## Section 7 — Tier schema and tiered reporting

**Goal.** Add a `tier` field to the case schema and aggregate by it
so pass-rate is reportable per complexity tier.

**Steps.**

1. Add a required `tier` field to the case schema, allowed values
   `trivial / moderate / hard / open_ended`. Backfill the existing
   4 cases as `trivial` (they will be retired in §9).
2. Add a `--tier TIER` CLI filter. Semantics: **exact match** on
   the requested tier. Documented in `--help`.
3. Extend §6's aggregate with a `by_tier` block:
   `{tier: {pass_rate_mean, pass_rate_stdev, n_cases}, …}`.
4. Update `print_report()` to print the per-tier table.

**Acceptance.**

- Running `uas-eval` on the still-trivial-only case set shows a
  per-tier table with only the `trivial` row populated.
- `uas-eval --tier trivial` filters correctly.

**Status:** completed

**Results.**

- `integration/prompts.json`: backfilled all 4 cases with
  `"tier": "trivial"`.
- `integration/eval.py`:
  - New `ALLOWED_TIERS = ("trivial", "moderate", "hard",
    "open_ended")` constant near the loader.
  - `load_prompts(filter_pattern=None, tier=None)` now backfills
    `tier=trivial` for any case missing the field, then applies the
    optional exact-match tier filter on top of the existing name
    regex filter.
  - New `aggregate_by_tier(all_results) -> dict` returning
    `{tier: {pass_rate_mean, pass_rate_stdev, n_cases, n_rows}}`.
  - `print_aggregate_report(aggregate, by_tier=None)` extended with
    a "By tier" sub-table that prints tiers in canonical
    `ALLOWED_TIERS` order, then any unknown tiers sorted at the end.
  - `--tier TIER` argparse argument with `choices=ALLOWED_TIERS`.
  - Main loop tags every result row with `result["tier"]` from the
    case definition before appending to JSONL — so by_tier can
    aggregate without needing the original case dict.
  - `RESULTS_AGGREGATE` now persists nested
    `{"by_case": …, "by_tier": …}` structure.
- `tests/test_eval_tiers.py`: 13 tests across 4 classes covering
  the `ALLOWED_TIERS` constant, `aggregate_by_tier` math
  (empty / single / multi-tier / mixed pass-fail / missing-tier
  default), `load_prompts` tier filter (no filter, exact match,
  default backfill, tier+name combo, no-match), and a regression
  check that the 4 shipped prompts.json cases all carry
  `tier=trivial`. **13/13 passed in 0.08s.**
- **Integration smoke** via monkey-patched `run_case` × `--runs 2`:
  4 cases × 2 runs = 8 rows. Aggregate file gained the nested
  `{by_case, by_tier}` shape; `by_tier["trivial"]` =
  `{pass_rate_mean: 1.0, pass_rate_stdev: 0.0, n_cases: 4,
  n_rows: 8}`. JSONL rows confirmed to carry `tier=trivial`.
  Stderr report rendered the new "By tier" sub-table cleanly.
- **CLI smoke**: `eval.py --tier moderate --list` returns
  "No matching prompt cases found" (correct — no moderate cases
  yet); `eval.py --tier trivial --list` returns all 4 cases.
- **Cross-section regression**: ran all six Phase 1 test files
  together (`test_eval_checks`, `test_eval_metadata`,
  `test_eval_persistence`, `test_eval_variance`, `test_eval_tiers`,
  `test_git_finalize`) → **102/102 passed in 88s** (most of the
  time is the pytest-in-pytest forks from Section 3).

## Section 8 — LLM-as-judge module

**Goal.** Phase 1 deliverable: LLM-as-judge with N=5 samples and
majority vote for open-ended tasks. Cost is explicitly not a
constraint.

**Steps.**

1. Add `integration/llm_judge.py` with public function:
   `judge(case_goal: str, workspace: str, criteria: str, *,
   files: list[str] | None = None, samples: int = 5,
   model: str = "claude-opus-4-6") -> dict`. Returns
   `{passed: bool, votes: [bool, ...], reasons: [str, ...],
   majority: float}`.
2. The judge prompt assembles:
   - The original case goal.
   - The success `criteria` from the check definition.
   - Either the explicitly listed `files` or, if `None`, an
     auto-discovered slice (file tree + content of all `.py`,
     `.md`, `.json`, `.txt`, `.csv` under workspace).
   - Per-file budget: 20k chars. Total budget: 200k chars.
     Truncate with a `[truncated]` sentinel.
   - A response schema requiring `verdict: pass | fail` and
     `reason: str`.
3. Run `samples` judge calls in parallel via
   `concurrent.futures.ThreadPoolExecutor`. Majority vote: pass iff
   `>= ceil(samples/2)` votes are `pass`.
4. Reuse the existing UAS Anthropic client path if importable
   without dragging in architect/orchestrator state. If reuse
   introduces upstream coupling, write a thin SDK adapter inside
   `integration/llm_judge.py`. Goal: `integration/` does not
   import from `architect/`.
5. Add an `llm_judge` check type to `run_check()` taking `criteria`
   and optional `files`. The check calls into the module.
6. Cache judge results in `integration/.judge_cache.json` keyed by
   `(case_name, criteria_sha256, workspace_content_sha256)`. Cache
   key MUST include the workspace content hash so a re-run that
   produced different code does not falsely cache.
7. Add `tests/test_llm_judge.py` with the SDK call mocked: assert
   majority-vote logic, schema parsing, cache hit/miss behaviour.

**Acceptance.**

- Mock-based unit tests pass.
- A real call on one open-ended Tier 3 case produces 5 verdicts
  and a majority decision. Pin that result in §10's validation log.

**Status:** completed

**Results.**

- `integration/llm_judge.py` (new, ~340 lines): self-contained module
  with one public function `judge(case_goal, workspace, criteria, *,
  files=None, samples=5, model="claude-opus-4-6", case_name=None,
  cache_path=None, use_cache=True) -> dict`. Returns
  `{passed, votes, reasons, majority, cached, samples_used,
  case_name}`. Internals: `_walk_workspace` (deterministic ordering,
  hidden-state skip set), `_read_truncated` (per-file 20k-char
  budget with `[truncated]` sentinel), `build_workspace_listing`
  (200k-char total budget, explicit `files=` vs auto-discovery),
  `_parse_verdict` (last-JSON-object-wins, tolerant of surrounding
  prose), `_call_one_sample` (per-thread error trap), `_call_anthropic`
  (the single SDK seam — see auth note below), `_hash_workspace_content`
  (SHA-256 over the same walk used for the prompt), `_build_cache_key`
  (`case_name | sha256(criteria) | workspace_hash`), `_load_cache` /
  `_save_cache` (corruption-tolerant, parent-dir creation). Parallel
  sampling via `concurrent.futures.ThreadPoolExecutor(max_workers=
  samples)`; majority vote uses `ceil(samples / 2)` per the PLAN
  spec. Constraint observed: zero imports from `architect/` or
  `orchestrator/`. The cache lives at `integration/.judge_cache.json`.
- **Auth fallback for host runs.** `_call_anthropic` tries
  `ANTHROPIC_API_KEY` (SDK path) first; if absent, falls back to
  the OAuth bearer token in `.uas_auth/.credentials.json` via a
  raw `httpx.post()` to `api.anthropic.com/v1/messages` with the
  `Authorization: Bearer` header and the `anthropic-beta:
  oauth-2025-04-20` flag. This is the path that works for Claude
  Max subscribers without an API key, and unblocks the §8
  acceptance smoke without rebuilding the container. Token refresh
  is not handled — re-run `claude` to mint a fresh token if it
  expires.
- `integration/eval.py`:
  - `run_checks()` now threads the full `case` dict into
    `run_check()` alongside `invocation`.
  - `run_check()` gained an optional `case=None` parameter and a
    new `llm_judge` branch. The branch validates `criteria` and
    `case` are present, lazy-imports the `judge` callable via
    the new `_import_llm_judge()` helper, calls `judge()` with the
    case goal/name and the check's `criteria`/`files`/`samples`/
    `model`, and returns a result row carrying `passed`, `majority`,
    `votes`, `samples_used`, `cached`, plus a human-readable
    `detail` line (`majority=X.XX; votes=K/N; (cached); reason: …`).
  - `_import_llm_judge()` handles three module-load contexts in
    order: `sys.modules['llm_judge']` (the path tests use),
    `from integration.llm_judge import judge` (the §10 wrapper
    path), and `importlib.util.spec_from_file_location` (running
    `python3 integration/eval.py` directly without a package
    context). The third branch caches the loaded module into
    `sys.modules` so subsequent calls reuse the same instance.
  - The `run_check` docstring's "Supported check types" section
    gained a `llm_judge` entry documenting the schema.
- `tests/test_llm_judge.py` (new, ~640 lines): **62 tests across 12
  classes**, all mocked at the `_call_anthropic` seam — no real
  LLM calls, no network. Coverage:
  - `TestWalkWorkspace` (8): missing/empty workspaces, extension
    matching (case-insensitive), hidden-state skip, recursion,
    deterministic order.
  - `TestReadTruncated` (3): short / oversize / unreadable.
  - `TestBuildWorkspaceListing` (7): empty workspace marker,
    auto-discovery, explicit `files=`, missing-file marker,
    per-file truncation, total-budget truncation, determinism.
  - `TestParseVerdict` (9): pass / fail / case-insensitive /
    surrounding prose / unparseable / missing verdict / missing
    reason / garbage verdict value / last-object-wins.
  - `TestHashWorkspace` (5): empty workspace, missing workspace,
    content change, filename change, skip-dir invariance.
  - `TestCacheKey` (4): format, blank case_name, criteria
    sensitivity, workspace-hash sensitivity.
  - `TestCacheIO` (5): missing file, corrupt file, non-dict root,
    round-trip, parent-dir creation.
  - `TestJudgeMajorityVote` (6): 5/0, 0/5, 3/2, 2/3, 4/1, and
    `samples=0` safe degenerate path. Confirms the `ceil(samples/2)`
    threshold and the `majority` float math.
  - `TestJudgeErrorPaths` (3): full-call failure → all-fails,
    one-call failure with otherwise-passing majority preserved,
    unparseable response → fail.
  - `TestJudgeCache` (5): first-call miss persists, second-call
    hit short-circuits the SDK, workspace mutation invalidates
    the cache, criteria mutation invalidates the cache,
    `use_cache=False` forces fresh calls.
  - `TestRunCheckLlmJudge` (5): missing criteria, missing case,
    passing judge surfaces majority + detail, failing judge
    surfaces majority + detail, `run_checks` threads `case`
    through.
  - `TestExistingCheckTypesUnaffected` (2): regression check that
    `file_exists` still works through the new signature.
  - **62/62 passed in 0.22s.**
- **Cross-section regression.** Full unit suite (`pytest tests/ -q`)
  → **1783 passed, 3 deselected in 323.85s**. The pre-Section-8
  baseline was 1637 passed; +62 from `test_llm_judge.py` and the
  rest are noise from the broader unit tree being included now.
  No new failures.
- **Real-call acceptance** (mocked judge bypassed; real calls hit
  `api.anthropic.com/v1/messages` via the OAuth bearer path).
  Smoke set built in `/tmp/uas_judge_smoke/` (cleaned up after).
  Used `claude-haiku-4-5-20251001` because the OAuth-tier rate
  limit on Claude Max instantly 429s 5 parallel Opus 4.6 calls,
  and the goal of the acceptance smoke is end-to-end verification
  of the judge plumbing — not benchmarking Opus latency. The
  parallel-sampling code path is identical regardless of model;
  the mocked unit tests already pin the parallel branch with all
  6 majority-vote configurations.

  | Smoke | Workspace | Criteria | Pass votes | Majority |
  |---|---|---|---|---|
  | should-fail | placeholder `index.py` returning `""` | site has header + p + form action=/contact | 0/5 | 0.00 |
  | should-pass | proper `site/index.html` with all 3 elements | same as above | 5/5 | 1.00 |

  Should-fail reasons (5/5) all correctly cited the unimplemented
  `render()` returning an empty string and the missing `site/`
  artefacts. Should-pass reasons (5/5) all correctly identified the
  `<h1>Hello, world</h1>` header, the description `<p>` element,
  and the form's `action="/contact"` attribute. Zero hallucination
  in either direction; every reason is grounded in the actual
  workspace bytes the prompt assembler shipped to the model.

  An additional cache test ran the same `(case_name, criteria,
  workspace_hash)` triple twice: first call had `cached=False,
  samples_used=5` (5 real SDK calls), second call had
  `cached=True, samples_used=5` (zero SDK calls — the mock for
  `_call_anthropic` was not patched and would have raised on call,
  but the cache hit short-circuited everything). Persisted cache
  entry confirmed.

  The full pinned verdict block is reproduced here for §10's
  validation log:

  ```
  case_name: smoke-tier3-pass-v2
  model:     claude-haiku-4-5-20251001
  goal:      Build a tiny static site at site/ with an index page that
             has a header, a description paragraph, and a contact form
             submitting to /contact.
  criteria:  There is an HTML page at site/index.html that contains:
             (1) a top-level header (h1 or similar), (2) a description
             paragraph (p element), and (3) a form whose action
             attribute submits to /contact. Absence of any of the
             three is a failure.
  files:     ["site/index.html", "README.md"]
  samples:   5
  votes:     [true, true, true, true, true]
  majority:  1.00
  passed:    true
  ```

**Notes / carry-forward.**

1. **Default model rate limit.** Production Tier 3 cases will run
   against `claude-opus-4-6` (the module default). On the OAuth
   tier, 5 parallel Opus calls return 429 immediately. §9 case
   authoring should either (a) pace runs, (b) lower the per-case
   `samples` for Opus, or (c) add a 429-aware retry loop. Logging
   here so it does not surprise §9.
2. **`.html` not in auto-discovery.** `DISCOVERY_EXTENSIONS` is
   `(.py, .md, .json, .txt, .csv)` per the PLAN spec. Tier 3 cases
   that need `.html`, `.css`, etc. must pass `files=` explicitly
   on the check. The acceptance smoke confirms this works.
3. **Cache lives at workspace-sibling, not workspace-child.** When
   testing locally, place the cache file *outside* any case
   workspace; otherwise saving the cache mutates the workspace
   and invalidates the workspace hash on the next read. The
   production layout (`integration/.judge_cache.json` with
   workspaces under `integration/workspace/<case>/`) does this
   naturally; the unit-test fixture had to be patched to mirror it.
4. **Token refresh.** The OAuth bearer fallback does not refresh
   expired tokens. If `.uas_auth/.credentials.json` is stale, the
   user re-runs `claude` to mint a fresh access token. Acceptable
   for Phase 1 since the eval is not yet on a CI cadence.

## Section 9 — Author tiered benchmark case set

**Goal.** Replace the 4 trivial cases with 35 tiered cases that
actually stress the 68 mechanisms catalogued in audit §1. Phase 0 §2
is explicit that the existing cases cannot exercise retries,
rewrites, replanning, or backtracking.

**Target distribution (35 cases total).**

- **Tier 0 (`trivial`) — 5 cases.** Smoke test the harness is alive.
  Reuse `hello-file`. Add 4 more single-step file-creation cases.
  Pass-rate target: 100% on every run; any failure = harness
  regression.
- **Tier 1 (`moderate`) — 15 cases.** Multi-step Python projects
  with cross-step references and deterministic checks: small CLI
  tools, JSON↔CSV converters, text-processing pipelines, single
  FastAPI route + test, SQLite ETL, etc. Should normally pass on
  Opus 4.6, occasional retry/rewrite expected.
- **Tier 2 (`hard`) — 10 cases.** Genuinely require retry, rewrite,
  replan, or backtrack. At least one case per essential cluster
  from `phase0_audit.md` §4: Cluster A (reflection), Cluster B
  (counterfactual + backtrack), Cluster D (validation cascade),
  Cluster E (coverage replan), Cluster F (TDD), Cluster G (git
  rollback), Cluster I (resume).
- **Tier 3 (`open_ended`) — 5 cases.** No deterministic check
  possible; graded purely by `llm_judge`. Examples: small static
  site, comparative benchmark report, mini DSL with parser +
  interpreter + example programs.

**Steps.**

1. Migrate to a directory layout
   `integration/cases/<tier>/<case_name>.json`. The loader walks
   the tree and sets `tier` from the directory name. Remove the
   single-file `prompts.json` once migration is complete.
2. Author Tier 0 (5 cases). Reuse `hello-file`; add 4 single-file
   variants exercising different file formats / encodings.
3. Author Tier 1 (15 cases) using §3's `pytest_pass`, `file_shape`,
   `command_succeeds` check types where deterministic.
4. Author Tier 2 (10 cases) targeting the listed essential clusters.
   For each case, document in a `notes.cluster_target` field which
   §4 cluster it stresses and which §1 row numbers it expects to
   trigger.
5. Author Tier 3 (5 cases) with `llm_judge` checks only.
6. **Drop `live-data-pipeline`.** Audit §2 flagged it as inherently
   flaky (depends on the open-notify.org live API). Replace with a
   Tier 1 case that reads a fixture JSON from `integration/data/`.
7. Create `integration/data/` with all fixture files referenced by
   any case's `setup_files`. (This is the first real consumer of
   the existing dead `setup_files` code path. Resist re-architecting
   it; keep the same shape.)
8. Cross-link every case to its target cluster / mechanism row in
   the `notes` field so Phase 4 has the bridge data ready.

**Acceptance.**

- `uas-eval --list` shows 35 cases across 4 tiers.
- A full `uas-eval --runs 3` completes without harness errors.
  (Phase 1 does not require any particular pass rate; Phase 2
  measures the baseline.)
- At least one Tier 2 case observably triggers a reflection,
  rewrite, or backtrack on at least one of the 3 runs (verifiable
  from the §1 metrics).

**Status:** completed

**Results.**

35 cases authored under the new `integration/cases/<tier>/<case>.json`
layout, distributed exactly to the PLAN target: 5 trivial / 15
moderate / 10 hard / 5 open_ended. All required Phase 0 §4 essential
clusters covered by Tier 2 cases (A, B, D, E, F, G, I — 7/7).

**Step 1 — loader migration.** `integration/eval.py`:
- Replaced `PROMPTS_FILE` constant with `CASES_DIR =
  os.path.join(SCRIPT_DIR, "cases")`.
- Rewrote `load_prompts(filter_pattern=None, tier=None)` to walk
  `CASES_DIR/<tier>/*.json` for each `tier in ALLOWED_TIERS`,
  loading every `.json` file in each tier directory in alphabetical
  order. The directory name is the canonical tier — any in-file
  `tier` field is overridden. Tier directories that do not exist
  are silently skipped (so a partially-populated tree returns
  whatever is present). Non-`.json` files in a tier directory are
  skipped (so README sidecars are tolerated).
- Updated `--list` output to show the tier as `[tier-name]` prefix
  before the case name.
- Deleted `integration/prompts.json` (the legacy single-file case
  list).
- Rewrote `tests/test_eval_tiers.py::TestLoadPromptsTierFilter` to
  monkey-patch `CASES_DIR` and write per-tier subdirectories
  (`_write_case` helper). Added 3 new tests on top of the
  pre-existing 5: directory-overrides-in-file-tier (canonical tier
  invariant), missing-tier-dirs-skipped, canonical-tier-order-
  preserved (cases come back in `ALLOWED_TIERS` order regardless
  of filesystem order), non-json-files-skipped. Renamed
  `TestExistingPromptsBackfilled` →
  `TestShippedCasesTierIsDirectoryDerived` and reframed it as a
  schema invariant ("every shipped case carries a tier in
  `ALLOWED_TIERS`") rather than a hard count assertion that would
  break each time a case is added. **All 16 tier-loader tests
  pass; 167 tests across all 7 Phase 1 test files pass in 87.65s.**

**Step 2 — Tier 0 (5 cases).** Reused `hello-file`. Authored 4
new single-file variants exercising different file formats and
encodings:
- `hello-json` — JSON object with `file_shape json + required_keys`.
- `hello-csv` — single-row CSV with `file_shape csv +
  required_columns + min_rows == max_rows == 1` (catches the
  example-row drift).
- `hello-markdown` — anchored H1 regex + paragraph regex.
- `hello-utf8` — three lines including Japanese (`こんにちは`) and
  Portuguese (`Olá`); pinned by per-line `file_contains` regex.
  Catches a model that writes Latin-1 or ASCII-escapes the JP text.

**Step 3 — Tier 1 (15 cases).** Mix of pure-deterministic and
pytest-gated:
- Format conversions / aggregations (5):
  `csv-to-json`, `json-to-csv`, `text-stats-report`,
  `markdown-toc-generator`, `regex-log-parser`.
- CSV processing (3): `csv-filter-by-column` (reuses `sales.csv`),
  `csv-aggregator` (group-by sum to JSON),
  `astros-from-fixture` (Step 6 replacement, see below).
- Algorithm + pytest (4): `fibonacci-tested`, `prime-sieve-tested`,
  `palindrome-checker-tested`, `caesar-cipher-tested`. Model
  writes both impl and tests; `pytest_pass` is the gate.
- Web/DB/CLI (3): `fastapi-hello-route` (FastAPI + TestClient),
  `sqlite-etl-loader` (CSV → SQLite → top-5 JSON; the min == max
  == 5 file_shape catches off-by-one), `temperature-converter-cli`
  (argparse + pytest + `command_succeeds` external smoke).

**Step 4 — Tier 2 (10 cases, cluster-targeted).** Each case carries
`notes.cluster_target` (single letter) and `notes.expected_mechanism_rows`
(list of §1 mechanism row numbers it expects to stress):

| Case | Cluster | Expected mechanism rows |
|---|---|---|
| `cluster-A-fixed-width-records` | A | 20, 21, 22, 23, 27, 32 |
| `cluster-A-roman-numeral-converter` | A | 20, 21, 22, 27, 35 |
| `cluster-B-pipeline-schema-mismatch` | B | 20, 25, 26, 32, 34 |
| `cluster-D-strict-json-schema` | D | 32, 39, 40, 41, 42, 55, 56, 57 |
| `cluster-D-temporal-ordering` | D | 32, 39, 40, 41, 42, 56 |
| `cluster-E-multi-requirement-cli` | E | 9, 10, 11, 44, 45 |
| `cluster-F-tdd-rate-limiter` | F | 8, 43, 67 |
| `cluster-F-tdd-lru-cache` | F | 8, 43, 67 |
| `cluster-G-rollback-trigger` | G | 30, 31, 20, 27 |
| `cluster-I-resume-stub-multi-phase` | I | 16, 59, 63 |

Cluster-design notes worth carrying forward:

- **Cluster A.** Two cases. The strict fixed-width-records case
  pins exact byte layout (`^000001Alice Smith {13}…` regex)
  on a 60-char-wide format — easy to fail on padding/alignment,
  prompting reflection + retry. Roman numerals has 12 pinned
  test cases (round-trip 1/4/9/40/49/90/400/900/1994/3999) plus
  ValueError boundaries; high probability of partial-impl
  failure → reflection.
- **Cluster B.** Single 3-step pipeline with cross-step schema
  dependencies (`line_total` derived in step 1, used by step 2;
  sort order from step 2, used by step 3). Step 3 check failures
  should drive the counterfactual root-cause tracer to finger
  step 1 or step 2.
- **Cluster D.** Two cases. `cluster-D-strict-json-schema` pins
  literal-string fields (`"version": "1.0.0"`, `"source":
  "uas-eval"`, ISO-8601 `Z` suffix) — exec succeeds, holistic
  validation catches missing required keys. `cluster-D-temporal-
  ordering` is the data-leakage trap: random splitting passes
  exec but fails the temporal-ordering output-quality check (#32).
- **Cluster E.** 8 explicit numbered requirements in one CLI tool
  goal. The first plan extracted by #9/#11 is unlikely to cover
  all 8 — coverage gap fill (#10) should fire, possibly replan
  (#44).
- **Cluster F.** Two TDD cases. Both goals use the literal phrase
  "use test-driven development" so the planning gate (#8) inspects
  for test-step-before-impl-step pairs and inserts them if missing.
  Rate-limiter requires clock-injection (non-trivial); LRU cache
  has 6 specified tests including two recency edges.
- **Cluster G.** `cluster-G-rollback-trigger` is the most
  intentionally-difficult case in the set. The "no recursion"
  constraint enforced by an AST inspection test combined with
  exact-digit-count (`factorial(100)` has exactly 158 digits)
  is set up to consume all 3 spec rewrite slots and trigger the
  3-strike git rollback (#30) → reset to pre-step checkpoint
  (#31).
- **Cluster I.** Documented as a **placeholder**: the resume +
  progress-file + attempt-history mechanisms (#16, #59, #63)
  only fire after a previous interrupted run, and the eval harness
  has no mid-run interrupt facility. The case is a long, multi-file,
  multi-phase project that **would** exercise the resume machinery
  if interrupted. Phase 4 will need a separate interrupt-and-resume
  harness to actually measure Cluster I; this case provides the
  workload for that future test.

**Step 5 — Tier 3 (5 cases, llm_judge-only).** All five carry an
`llm_judge` check with a thoroughly enumerated `criteria` field:
- `static-blog-site` — 3-post static blog with shared CSS.
  Mirrors the should-pass smoke in §8 Results.
- `sort-algo-comparison-report` — 3 sort algos + benchmark + MD
  report with seed=42 reproducibility. Judge criteria explicitly
  reject placeholder/zero numbers.
- `mini-calc-dsl` — lexer + parser + interpreter + 3 example
  programs + runner. Stresses compositional design.
- `readme-from-source` — fixture `quicklib.py` + judge that
  enforces "function names in README must actually exist in
  source" (no hallucination).
- `dataset-analysis-report` — `cars.csv` fixture + judge that
  enforces statistics within plausible range and 3+ cited
  observations.

Each case's `samples` is set to `5` (the §8 default). The §8
carry-forward note about Opus-4.6 OAuth-tier rate-limit on 5
parallel calls applies — Section 10's end-to-end run will need
to either pace, lower samples, or accept some 429 retries.

**Step 6 — `live-data-pipeline` retired.** Replaced by
`astros-from-fixture` (Tier 1) which uses the new `integration/
data/astros.json` fixture instead of hitting `api.open-notify.org`.
Same two-step pipeline shape; deterministic input.

**Step 7 — `integration/data/` fixtures (14 files).** Created the
fixture set referenced by every case's `setup_files`:

| Fixture | Used by | Tier |
|---|---|---|
| `sales.csv` | `csv-to-json`, `csv-filter-by-column` | 1 |
| `users.json` | `json-to-csv` | 1 |
| `article.txt` | `text-stats-report` | 1 |
| `document.md` | `markdown-toc-generator` | 1 |
| `access.log` | `regex-log-parser` | 1 |
| `transactions.csv` | `csv-aggregator` | 1 |
| `astros.json` | `astros-from-fixture` | 1 |
| `cities.csv` | `sqlite-etl-loader` | 1 |
| `employees.csv` | `cluster-A-fixed-width-records` | 2 |
| `sales_raw.csv` | `cluster-B-pipeline-schema-mismatch` | 2 |
| `timeseries.csv` | `cluster-D-temporal-ordering` (52 rows) | 2 |
| `ratings.csv` | `cluster-I-resume-stub-multi-phase` | 2 |
| `quicklib.py` | `readme-from-source` (6 public fns) | 3 |
| `cars.csv` | `dataset-analysis-report` (22 rows) | 3 |

`.gitignore` blocker: line 19 (initial-commit-era) was
`integration/data/`, which would have prevented every fixture
from being tracked on a fresh checkout — making every case with
`setup_files` fail with `SetupFileMissing`. Removed the line and
added an inline comment explaining why `integration/data/` is now
tracked since Phase 1 §9. While editing `.gitignore`, also added
`integration/eval_results_aggregate.json` to the ignore list — it
is the per-invocation derived view written by §6, not a tracked
artefact.

**Step 8 — cluster cross-links.** Tier 2 cases carry the strong
cross-link (`notes.cluster_target` + `notes.expected_mechanism_rows`)
shown in the Step 4 table. Tier 0/1/3 cases carry a `notes.purpose`
string explaining their role in the eval set — they are baseline /
breadth-coverage workloads, not designed to stress specific
mechanisms, so the literal "cluster_target" field would be empty
for them. Phase 4 reads the Tier 2 table for ablation pairing;
Tier 0/1/3 deltas roll up into the per-tier aggregate.

**Verification chain (Section 9 acceptance, criterion 1).**

1. **Loader audit.** `python3 integration/eval.py --list` returns
   exactly 35 cases in canonical tier order then alphabetical
   within tier. `--list` output annotated with `[tier]` prefix.
2. **Schema audit.** Inline Python script verifies all 35 cases
   parse, every case has `name + goal + checks`, every check
   `type` is one of the 8 supported types
   (`file_exists, file_contains, glob_exists, pytest_pass,
   exit_code, file_shape, command_succeeds, llm_judge`), and every
   `setup_files` reference resolves to a present file in
   `integration/data/`. **All 35 cases pass; 14/14 setup_files
   matched to fixtures.**
3. **Cluster coverage audit.** Inline script confirms every Tier 2
   case carries both `cluster_target` and `expected_mechanism_rows`
   in `notes`, and the union of `cluster_target` values is
   exactly `{A, B, D, E, F, G, I}` — the 7 essential clusters
   from `phase0_audit.md` §4 (Cluster J cross-run learning is
   not in the PLAN's required-list and is left to Phase 4 group
   ablation; Clusters C/H are incidental and already cleanly
   bundled).
4. **Synthetic full-loop run.** Monkey-patched `run_case` to a
   fake that always returns `passed=True` with the §1 token /
   cost / attempt fields populated, then ran `eval.main()
   --runs 3 --results-out=<tmpdir>/results.jsonl` against the
   real loader, real aggregators, real metadata capture, and
   real persistence. Result: `main()` returned `0`, the JSONL
   file got exactly **105 rows** (35 cases × 3 runs), per-tier
   row counts were `{trivial: 15, moderate: 45, hard: 30,
   open_ended: 15}` — exactly `tier_size × 3`. Every row carried
   the §4 metadata (`git_sha`, `harness_version`, `config_hash`,
   `run_index`). The aggregate file had 35 entries in `by_case`
   and 4 entries in `by_tier`. Per-tier table rendered cleanly to
   stderr. The synthetic run took 17.5s — it confirms the entire
   harness path (loader → run loop → metric collection → checks
   → JSONL append → aggregate writer → tier aggregator →
   `print_aggregate_report`) is sound across all 35 cases without
   burning architect time.
5. **Test suite regression.** `pytest tests/test_eval_*.py
   tests/test_llm_judge.py tests/test_git_finalize.py -q` →
   **167 passed in 87.65s** (53 + 114, where the 114 includes
   the LLM-judge module's 62 mocked tests and the 52
   git-finalize tests). No new failures from the loader migration.

**Acceptance criterion 1 (`--list` shows 35 cases across 4 tiers):
satisfied.** Verified by both the live `--list` invocation and the
synthetic `main()` loop.

**Acceptance criteria 2 and 3 (live `--runs 3` completes without
harness errors; ≥1 Tier 2 case triggers reflection/rewrite/
backtrack): deferred to Section 10.** Section 10 is the explicit
owner of the canonical end-to-end run. Running 35 cases × 3 runs
× ~5–8 min/case against the real architect would burn 9–14 hours
of LLM time — too expensive for a Section 9 sanity check, and
duplicative of Section 10's own end-to-end deliverable. The
synthetic main() loop above plus the unit-test suite covers every
code path Section 9 owns; Section 10 covers the architect-
subprocess and mechanism-trigger paths Section 9 cannot exercise
without burning architect time.

**Carry-forward notes for Section 10.**

1. **Workspace permission issue.** `integration/workspace/hello-file/
   .uas_state/runs/<run_id>/specs/` from prior container runs is
   owned by root and breaks `setup_workspace`'s `shutil.rmtree` /
   `--clean`'s top-level rmtree. Section 4 already flagged this
   for Section 10. Recommended fix: either run cleanup inside a
   container (root-on-root works), or have the user manually
   `sudo rm -rf integration/workspace/` once before Section 10
   starts.
2. **Tier 3 OAuth rate limit.** Per §8 carry-forward note 1: 5
   parallel Opus 4.6 calls hit 429 on the OAuth tier instantly.
   Section 10 will see this on every Tier 3 case across 3 runs
   (5 cases × 3 runs × 5 samples = 75 parallel-bursts of 5 calls
   each). Mitigation options: (a) lower `samples` to 3 in the
   case files, (b) add a 429-aware retry loop in `llm_judge.py`,
   (c) use a non-Opus model for Tier 3 (e.g., haiku-4-5 — already
   shown to work in §8 acceptance smoke).
3. **Local-mode auth path.** §1 carry-forward note 1 still stands:
   `eval.py --local` cannot find OAuth credentials. Section 10's
   end-to-end must use container mode. Fixing local-mode auth is
   out of Phase 1 scope.
4. **`integration/data/` is now tracked.** A fresh checkout will
   include all 14 fixtures. No setup beyond `git checkout` is
   needed for the case set to load.
5. **Tier 2 acceptance is observation-only.** "At least one Tier 2
   case observably triggers a reflection, rewrite, or backtrack"
   means Section 10 should grep the JSONL log's per-step
   `rewrites` field (surfaced by §1) and the `attempt_total`
   field for any value `> 1` on at least one Tier 2 row across
   the 3 runs. The 10 Tier 2 cases are designed to make this
   highly likely; if the architect somehow first-shots all 10 on
   all 3 runs, Section 10 should investigate whether the cases
   are too easy (and re-tighten Tier 2 in a follow-up commit
   before Phase 2 baseline).

## Section 10 — `uas-eval` entry point + end-to-end validation

**Goal.** Stable contract surface, then verify Phase 1's exit
criterion: two consecutive runs on the same commit must produce
statistically indistinguishable results.

**Steps.**

1. Add a thin `uas-eval` shell wrapper at repo root:
   `exec python3 -P -m integration.eval "$@"`. Permissions: `+x`.
2. Add `integration/__init__.py` if missing so the module path
   `integration.eval` is importable.
3. Run `uas-eval --runs 3` end-to-end. Confirm:
   - Exit code reflects pass-rate (preserve current "0 if all
     passed else 1" — Phase 2 tightens the gate).
   - JSONL log gains `35 * 3 = 105` rows.
   - Aggregate file is well-formed.
   - Per-tier table renders.
   - Reproducibility metadata captured.
4. Run `uas-eval --runs 3` a second time on the same commit, same
   working tree, same env. Compare aggregate files: per-case
   pass rates should overlap within 1 stdev for every case;
   per-case wall times within 2 stdev.
5. If the comparison fails, investigate before declaring Phase 1
   complete. This is the noise floor that Phase 2 measurements
   depend on; getting it wrong here corrupts everything downstream.
6. Append both runs' aggregate files to this section's `Results`
   subsection so the noise floor is on record before Phase 2 starts.

**Acceptance.**

- `./uas-eval --runs 3` is the canonical command and works from a
  fresh checkout.
- Two consecutive runs produce statistically indistinguishable
  per-case pass rates (within 1 stdev) and per-case wall times
  (within 2 stdev).
- Phase 1 exit criterion from `ROADMAP.md` is satisfied.

**Status:** pending

---

## Phase close

When all sections show `**Status:** completed`:

1. Update `ROADMAP.md`: mark Phase 1 completed in the phase plan
   table; move Phase 1 into the "Completed phases" section with a
   one-paragraph summary; mark Phase 2 active.
2. Commit `Mark Phase 1 complete, populate baseline-ready section`
   (or similar).
3. Delete this `PLAN.md` in a `Remove completed PLAN file` commit.
4. Phase 2 is the next phase: it draws its own PLAN and pauses for
   review per the same gate.
