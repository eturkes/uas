# PLAN: Fix Architect Planner Blindness to Pre-existing Workspace Files

## Background

The `rehab` project failed when run with `uas --goal-file goal_001.txt`. All
35 generated steps failed (run id `12f634a8f886`). The cascade traces back
to two distinct root causes, both visible in
`rehab/.uas_state/runs/12f634a8f886/state.json`.

### Root cause A — Planner blindness (PRIMARY)

The Architect's planning phase (`research_goal` →
`generate_project_spec` → `decompose_goal_with_voting`) is invoked with
only the goal text. None of those functions inspects the workspace
directory before generating the plan.

For rehab, the user had already placed `simulation_spec.json` in the
project root (the goal explicitly says "the only file a user must
manually place after cloning this repository is `simulation_spec.json`").
The actual file's top-level keys are
`['metadata', 'time_points', 'constant_columns', 'patient_columns',
'time_columns', 'sensory', 'motor', 'scim', 'anomalies']` and it has
exactly 9 anomaly types.

But the planner generated step descriptions that hallucinated a
different schema. Step 3 in state.json reads:

> "test_top_level_keys: Has keys 'structure', 'columns', 'anomalies',
> 'temporal_patterns'."
> "test_columns_has_motor: columns contains 'motor' with 10 spinal
> levels (C5-T1, L2-S1)."
> "test_columns_has_sensory: columns contains 'sensory_lt' and
> 'sensory_pp' each with 28 dermatomes."
> "test_anomalies_min_types: anomalies has >= 10 distinct anomaly
> types."

None of those keys exist in the actual file, and the >=10 threshold
exceeds the real count of 9. The coder LLM dutifully wrote tests
matching the planner's invented schema, pytest reported six failures,
and the step was marked failed. After 3 attempts the orchestrator gave
up.

The planner also generated step 4 ("Simulation specification JSON")
to *create* a `simulation_spec.json` that already existed — it would
have overwritten the user's spec if step 3 had not blocked it first.

The cascade then blocked all 33 remaining steps (`Skipped: dependency
failed`) and the run terminated with `status: blocked`.

This bug is fully general: any project that asks UAS to act on
pre-existing data files, schemas, configs, or source code will hit
the same failure mode because the planner has no way to read those
files when it builds the DAG.

### Root cause B — Module name shadowing (SECONDARY)

`/home/eturkes/pro/uas/config.py` is the framework's own config module,
imported as a top-level name (`import config`) by `architect/state.py`,
`architect/executor.py`, `architect/main.py`, `orchestrator/llm_client.py`,
and `orchestrator/main.py`.

`architect/executor.py:_run_local()` invokes the orchestrator with
`cwd=workspace` and `PYTHONPATH=framework_root`. Python's `-m` mode
prepends `''` (the CWD) to `sys.path`, so when the workspace contains a
`config.py` (which step 1 of rehab tried to create), Python imports
`<workspace>/config.py` *instead of* the framework's `config.py`. The
workspace module lacks `get()`, `persistent_retry`, `task`, etc., which
breaks every subsequent orchestrator invocation.

This bug is also fully general: any user project with a `config.py`
at its root will trigger it. It's strictly secondary to root cause A
in the rehab failure (root cause A blocks the cascade before B fully
manifests), but it must be fixed for UAS to handle real-world projects
that ship their own `config.py`.

---

## Section 1 — Add a planner-side workspace scan helper

**Status:** completed

**Goal:** Provide a single helper that produces a planner-ready summary
of pre-existing workspace files using the existing
`scan_workspace_files` / `format_workspace_scan` infrastructure in
`architect/executor.py`. The helper must be importable from both
`architect/main.py` (where it is invoked) and `architect/planner.py`
(future, kept loose for now).

**Files to edit**

- `architect/executor.py`

**Changes**

1. Add a new public function near the existing
   `format_workspace_scan` (around line 793):

   ```python
   def build_planner_workspace_context(workspace_path: str,
                                        max_chars: int = 6000) -> str:
       """Return a planner-ready summary of pre-existing workspace files.

       Wraps scan_workspace_files() + format_workspace_scan() with the
       JSON key extractor used elsewhere in the codebase, then caps the
       result to *max_chars* characters. Returns an empty string when
       the workspace is empty, missing, or contains only hidden /
       framework-managed entries.

       The output is intended to be embedded inside a <workspace_files>
       XML-style block in planner prompts so the LLM can ground its
       step descriptions in real file contents instead of invented
       schemas.
       """
   ```

   The body should:
   - Call `scan_workspace_files(workspace_path)`.
   - Lazily import `_extract_json_keys` from `architect.main` to avoid a
     circular import. Wrap the import in a `try/except ImportError` and
     fall back to `None` (the formatter handles `None` gracefully).
   - Call `format_workspace_scan(ws_files, json_key_extractor=...)`.
   - Strip and truncate to `max_chars`, appending
     `"\n... [planner workspace scan truncated]"` when truncated.
   - Return `""` on any exception so the planner gracefully degrades.

2. Re-export the helper from `architect/main.py`'s existing executor
   import block (the multi-line `from .executor import (...)` around
   line 58) by appending `build_planner_workspace_context,` to the
   list. No other call sites are added in this section.

**Tests to add (`tests/test_executor.py`)**

- `test_build_planner_workspace_context_empty` — empty dir returns `""`.
- `test_build_planner_workspace_context_with_json` — a temp dir with
  one JSON file produces output containing the file name and the
  `keys:` line from `_extract_json_keys`.
- `test_build_planner_workspace_context_truncation` — passes a tiny
  `max_chars` value and asserts the truncation marker appears.
- `test_build_planner_workspace_context_circular_import_safety` —
  patches `architect.main` to raise `ImportError` on attribute access
  for `_extract_json_keys` and asserts the helper still returns a
  non-empty string for a non-empty workspace.

**Definition of done**

- New function exists, is exported, has docstring matching this plan.
- All four new unit tests pass.
- Running `python -m pytest tests/test_executor.py -q` from the repo
  root reports zero failures.
- No other call sites have been added yet; the helper is dormant.

---

## Section 2 — Thread workspace context through planner functions

**Status:** completed

**Goal:** Add a `workspace_context: str = ""` parameter to the four
planner entry points so they can receive (but not yet emit) a
workspace summary. Keeping default `""` preserves backward compatibility
for every existing test.

**Files to edit**

- `architect/planner.py`

**Functions to modify (signature only — prompt wiring is Section 3)**

1. `research_goal(goal: str, workspace_context: str = "")` — line 166.
2. `generate_project_spec(goal, research_context="", complexity="medium",
   workspace_context: str = "")` — line 89.
3. `decompose_goal(goal, spec="", hooks=None,
   workspace_context: str = "")` — line 844.
4. `decompose_goal_with_voting(goal, n_samples=3, spec="",
   complexity=None, hooks=None, workspace_context: str = "")` —
   line 1281. The internal `_generate_plan` closure must also accept
   and forward the value to the prompt template (deferred to
   Section 3).

For each function, update the docstring `Args:` block to mention the
new parameter ("Optional formatted summary of pre-existing workspace
files. When non-empty, embedded into the prompt under
`<workspace_files>` so the planner can ground decisions in real file
contents.").

Do **not** modify the prompt templates yet — they continue to use the
existing format keys. The new parameter is plumbed in but ignored. This
is to keep the diff small and reviewable in isolation.

**Tests to add (`tests/test_planner.py`)**

- `test_research_goal_accepts_workspace_context_kwarg` — call
  `research_goal("noop", workspace_context="ignored")` with the LLM
  client patched to return a fixed string and assert it returns that
  string. Verifies the kwarg does not blow up.
- Equivalent smoke tests for `generate_project_spec`,
  `decompose_goal`, and `decompose_goal_with_voting` (the last one
  with `complexity="trivial"` so it short-circuits to
  `decompose_goal`).

**Definition of done**

- All four signatures updated.
- All four new smoke tests pass.
- `python -m pytest tests/test_planner.py -q` reports zero failures.
- `git grep -n "workspace_context" architect/planner.py` shows the
  parameter on every targeted function.

---

## Section 3 — Inject workspace context into planner prompt templates

**Status:** completed

**Goal:** Make the planner LLM actually see and act on the workspace
summary by adding a `<workspace_files>` block to the three prompt
templates and instructing the planner to honour pre-existing files.

**Files to edit**

- `architect/planner.py`

**Changes**

1. **`RESEARCH_PROMPT`** (line 137) — add a new format placeholder
   `{workspace_section}` immediately after the `<goal>` line. In
   `research_goal()` build the section as:

   ```python
   workspace_section = ""
   if workspace_context:
       workspace_section = (
           "\n<workspace_files>\n"
           "The following files already exist in the project workspace. "
           "Treat them as authoritative — your research must not assume "
           "different file names, schemas, or contents than what is "
           "shown here.\n"
           f"{workspace_context}\n"
           "</workspace_files>\n"
       )
   ```

   Then `prompt = RESEARCH_PROMPT.format(goal=goal,
   workspace_section=workspace_section)`.

2. **`SPEC_GENERATION_PROMPT`** (line 21) — same pattern. The
   instructional copy should additionally say:

   "If a file in `<workspace_files>` already provides a contract
   (schema, columns, keys, value distributions), section 5 (Data Model)
   MUST quote those exact names rather than inventing alternatives."

3. **`DECOMPOSITION_PROMPT`** (line 190) — same pattern. The
   instructional copy should additionally say:

   "If a file in `<workspace_files>` already exists, do NOT generate
   a step to create it. Generate steps that *use* the file as-is,
   referencing the exact keys, columns, and values shown in the
   summary. Test steps that validate such files MUST assert against
   the actual schema visible in `<workspace_files>`, never against
   an invented one."

   Add a new rule to the `<rules>` section (rule 17):

   "17. Pre-existing files in `<workspace_files>` are immutable
   inputs unless the goal explicitly requests overwriting them.
   Step descriptions that touch these files must reference the
   exact field names, key paths, and value ranges visible in the
   scan."

4. Wire the new parameter through `decompose_goal_with_voting`'s
   `_generate_plan` closure so each parallel voting variant receives
   the same `workspace_section`.

5. Add a defensive truncation: if `workspace_context` exceeds
   `max_chars=4000`, truncate before embedding (the helper already caps
   at 6000 by default; this is a belt-and-braces guard against giant
   inputs from future callers).

**Tests to add (`tests/test_planner.py`)**

- `test_decompose_goal_includes_workspace_files_block` — patch
  `get_llm_client` so `client.generate` records the prompt it received,
  call `decompose_goal("noop", workspace_context="<file_x.json>")`,
  assert the recorded prompt contains both `<workspace_files>` and the
  literal `<file_x.json>` substring.
- `test_decompose_goal_omits_block_when_context_empty` — same but with
  default empty `workspace_context`, assert `<workspace_files>` is
  NOT present.
- Mirror tests for `research_goal` and `generate_project_spec`.

**Definition of done**

- All three prompt templates contain a `{workspace_section}` placeholder
  filled at call time.
- All six new prompt-injection tests pass.
- All previously-existing planner tests still pass
  (`python -m pytest tests/test_planner.py -q`).
- `python -m pytest tests/ -q -k "planner or executor"` reports zero
  failures.

---

## Section 4 — Wire workspace context into the architect's main flow

**Status:** completed

**Goal:** Call the new helper exactly once at the start of the
planning phase in `architect/main.py` and pass the result through all
four planner entry points.

**Files to edit**

- `architect/main.py`

**Changes**

1. Locate the planning phase block (around line 5941, just after
   `goal_entity = prov.add_entity("goal", content=goal)`).

2. Immediately before the `if not MINIMAL_MODE:` block, compute the
   workspace context:

   ```python
   workspace_context = ""
   try:
       workspace_context = build_planner_workspace_context(WORKSPACE)
       if workspace_context:
           logger.info(
               "  Pre-existing workspace files visible to planner "
               "(%d chars).",
               len(workspace_context),
           )
   except Exception as exc:
       logger.warning(
           "  Could not scan workspace for planner context "
           "(non-fatal): %s",
           exc,
       )
   ```

3. Forward `workspace_context` to:
   - `research_goal(goal, workspace_context=workspace_context)`
   - `generate_project_spec(goal, research_context=research_context,
     complexity=complexity or "medium",
     workspace_context=workspace_context)`
   - `decompose_goal_with_voting(goal, spec=spec,
     complexity=complexity, hooks=_hooks,
     workspace_context=workspace_context)`

4. Persist the captured context on the run state so resumed runs can
   reuse it without re-scanning a possibly-changed workspace:

   ```python
   if workspace_context:
       state["planner_workspace_context"] = workspace_context
   ```

5. Add a single integration-style test in
   `tests/test_workspace_validation.py` (or a new
   `tests/test_planner_workspace_awareness.py` if that file does not
   already cover this area):

   - Create a temp dir with a `simulation_spec.json` whose top-level
     keys are exactly `["metadata", "anomalies"]`.
   - Patch `WORKSPACE` to point at the temp dir.
   - Patch the planner LLM client to capture prompts.
   - Invoke the planning phase via a small wrapper that mirrors the
     real `main.py` call sequence (or call the four planner functions
     directly with the helper output).
   - Assert that the captured `decompose_goal` prompt contains the
     literal string `metadata` and does NOT contain `structure` or
     `temporal_patterns` (i.e. the planner sees real keys, not
     hallucinated ones).

**Definition of done**

- `architect/main.py` calls `build_planner_workspace_context` exactly
  once per fresh-start planning phase.
- The result is forwarded to all three planner entry points and
  persisted on the state when non-empty.
- The new integration test passes.
- `python -m pytest tests/ -q` reports zero failures.

---

## Section 5 — Validate the fix on the rehab project

**Status:** pending

**Goal:** Confirm the patched UAS plans the rehab project against the
real `simulation_spec.json` schema. We will not run the full 35-step
DAG (too costly) — only verify that the planning phase produces a
schema-aware plan.

**Steps**

1. Move the existing failed run to an archive so it does not collide
   with the new run:

   ```bash
   mv /home/eturkes/pro/uas/rehab/.uas_state/runs/12f634a8f886 \
      /home/eturkes/pro/uas/rehab/.uas_state/runs/12f634a8f886.failed.bak
   rm /home/eturkes/pro/uas/rehab/.uas_state/latest_run
   ```

2. Run the architect in dry-run mode so only the planning phase
   executes (no orchestrator subprocesses). From the rehab dir:

   ```bash
   cd /home/eturkes/pro/uas/rehab
   UAS_DRY_RUN=1 python3 -m architect.main --goal-file goal_001.txt
   ```

   (If `--dry-run` is the supported flag, prefer it: `python3 -m
   architect.main --dry-run --goal-file goal_001.txt`. Confirm via
   `python3 -m architect.main --help` first.)

3. Inspect the resulting state.json under `.uas_state/runs/<new_id>`:

   ```bash
   python3 -c "
   import json, glob
   latest = sorted(glob.glob('.uas_state/runs/*/state.json'))[-1]
   data = json.load(open(latest))
   for s in data['steps']:
       desc = s.get('description', '')
       if 'simulation_spec' in desc.lower() or 'metadata' in desc:
           print(f\"Step {s['id']}: {s['title']}\")
           print(f'  {desc[:400]}')
   "
   ```

   **Pass criteria** (all must hold):
   - At least one step description references `metadata` or
     `time_points` (the real top-level keys).
   - No step description references `structure`, `temporal_patterns`,
     `sensory_lt`, or `sensory_pp` (the hallucinated keys from the
     failed run).
   - No step proposes *creating* `simulation_spec.json` — only steps
     that *read* it should appear.
   - Anomaly-related assertions, if any, reference a count `<= 9`
     (the real number of anomaly types in the spec).

4. If any pass criterion fails, capture the offending step text and
   return to Sections 1–4 to refine the prompt wording. Do not
   advance the section status.

5. On pass, archive the new dry-run state alongside the original
   failure for future regression checks:

   ```bash
   cp -r .uas_state/runs/<new_id> \
      .uas_state/runs/<new_id>.dryrun_pass
   ```

**Definition of done**

- A fresh dry-run plan exists in `rehab/.uas_state/runs/`.
- All four pass criteria above are satisfied.
- The dry-run plan is preserved alongside the original failure.

**Blocker:** Dry-run executed successfully and produced
`rehab/.uas_state/runs/eb08b692444f/state.json` (53 steps). Pass
criteria 1, 3, and 4 are clearly satisfied:

- Criterion 1: real top-level keys are referenced — `time_points`
  (1 step), `constant_columns` (5), `patient_columns` (5),
  `time_columns` (2), `sensory` (3), `motor` (3), `scim` (2),
  `anomalies` (1). `metadata` is not referenced but the criterion
  reads "metadata OR time_points".
- Criterion 3: 0 steps create `simulation_spec.json`; 10 steps
  reference it as a read-only input. The actual JP column names
  from the spec (`性別`, `年齢`, `外傷性`, `対麻痺`, `損傷部位`,
  `ALLEN分類`, `mFrankel`, `IDNumber`, `TIMES`) appear across
  multiple steps, proving the planner is reading real contents.
- Criterion 4: no anomaly threshold > 9 found.

Criterion 2 is ambiguous as written. The hallucinated keys
`temporal_patterns`, `sensory_lt`, and `sensory_pp` are completely
absent (0 matches). The fourth key `structure` appears in 11 steps,
but every match is the generic English noun in titles like
"Project skeleton structure", "Translations YAML structure",
"Cache model script structure", "layout structure" — never as a
JSON key reference (`spec['structure']`, `'structure' in keys`,
etc.). A targeted search for JSON-key usage of `structure`
returned 0 matches.

Strict literal reading of criterion 2 ("No step description
references `structure`") FAILS because "structure" is too common
an English word. Intent-based reading (the parenthetical "the
hallucinated keys from the failed run") PASSES because no
step invents `structure` as a `simulation_spec.json` key.

Suggested resolution: tighten criterion 2 to detect JSON-key
usage specifically (e.g. `spec['structure']`, `keys 'structure'`,
top-level key lists), or accept the intent-based interpretation
and mark Section 5 completed manually. The dry-run state is
preserved at `rehab/.uas_state/runs/eb08b692444f/` and the
original failed run is at `rehab/.uas_state/runs/12f634a8f886.failed.bak/`
for regression comparison.

---

## Reusable agent prompt

When picking up this PLAN.md in a fresh coding agent session, paste the
following prompt verbatim:

> Open `/home/eturkes/pro/uas/PLAN.md`. Find the first section whose
> `**Status:**` line is `pending`. Read the entire section, then
> implement every change it specifies in the listed files. Run the
> tests the section names as part of its definition of done and
> confirm zero failures. When the definition of done is fully
> satisfied, edit `PLAN.md` to change that section's `**Status:**`
> line from `pending` to `completed` and stop. Do not start the next
> section in the same session. If anything in the section is unclear
> or impossible as written, leave the status as `pending`, append a
> short `**Blocker:**` note under the section, and stop without
> editing code.
