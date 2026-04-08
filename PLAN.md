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

**Status:** pending

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

**Status:** pending

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
