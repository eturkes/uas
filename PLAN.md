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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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

**Status:** pending

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
