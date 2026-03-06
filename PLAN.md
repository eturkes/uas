# PLAN: Usability, Traceability, and Transparency Enhancements

This plan addresses six areas where UAS can provide deeper insight into the
transformations that occur from a user's goal to the final result. Each section
is designed to be implemented in a single Claude Code session.

Do not commit or push to Git at the end of each section. The user will verify
changes before committing.

---

## Section 1: Structured Event & Provenance System -- DONE

**Goal:** Replace ad-hoc logging with a structured event system that captures
every significant action as a typed event, and build a provenance graph that
tracks the full transformation chain from goal to result.

**Why:** Currently, UAS uses Python's `logging` module with a bare
`%(message)s` format. Events are unstructured strings written to stderr. There
is no machine-readable record of *what happened* during a run beyond
`state.json` (which captures final state, not the journey). The scratchpad is
informal and append-only. This makes it impossible to answer questions like
"what prompt produced this code?" or "how did the error in step 2 influence
the rewrite of step 3?"

**Implementation:**

1. Create `architect/events.py` with:
   - An `Event` dataclass: `timestamp`, `event_type`, `step_id` (optional),
     `attempt` (optional), `duration` (optional), `data` (dict of arbitrary
     metadata).
   - Event types enum: `GOAL_RECEIVED`, `DECOMPOSITION_START`,
     `DECOMPOSITION_COMPLETE`, `PLAN_CRITIQUE`, `STEP_MERGE`, `STEP_START`,
     `LLM_CALL_START`, `LLM_CALL_COMPLETE`, `CODE_EXTRACTED`,
     `SANDBOX_START`, `SANDBOX_COMPLETE`, `STEP_COMPLETE`, `STEP_FAILED`,
     `REWRITE_START`, `REWRITE_COMPLETE`, `VERIFICATION_START`,
     `VERIFICATION_COMPLETE`, `CONTEXT_BUILT`, `RUN_COMPLETE`.
   - An `EventLog` class that:
     - Appends events to a list in memory.
     - Writes each event as a JSON line to `.state/events.jsonl`.
     - Provides `emit(event_type, **kwargs)` as the primary API.
     - Provides `query(event_type=None, step_id=None)` for retrieval.
   - A module-level singleton accessor `get_event_log()`.

2. Create `architect/provenance.py` with:
   - A W3C PROV-inspired model using three node types:
     - `Entity`: data artifacts (goal text, plan JSON, spec markdown, prompt
       text, generated code, sandbox stdout, UAS_RESULT).
     - `Activity`: transformations (decompose, critique, generate_spec,
       llm_call, sandbox_run, rewrite, verify).
     - `Agent`: the actor (planner_llm, orchestrator_llm, sandbox).
   - Each node gets a content-addressed ID (truncated SHA-256 of its content).
   - Edges: `wasGeneratedBy(entity, activity)`,
     `used(activity, entity)`, `wasAssociatedWith(activity, agent)`,
     `wasDerivedFrom(entity, entity)`.
   - A `ProvenanceGraph` class that accumulates nodes and edges and serializes
     to `.state/provenance.json`.
   - Cross-attempt linking: when a step fails and gets rewritten, the new
     attempt's activity is linked to the previous attempt's error entity via
     `wasDerivedFrom`.

3. Instrument existing code to emit events and record provenance:
   - `architect/main.py`: Emit events at each phase boundary. Wrap
     `execute_step` to record provenance edges.
   - `architect/planner.py`: Emit events around `decompose_goal`,
     `critique_and_refine_plan`, `reflect_and_rewrite`. Record the
     goal -> plan derivation.
   - `architect/executor.py`: Emit events around `run_orchestrator`. Record
     prompt -> response -> code -> output chains.
   - Preserve all existing stderr logging behavior. The event system is
     additive, not a replacement for human-readable output.

4. Add `--events` / `UAS_EVENTS` flag to write the event log path (defaults
   to `.state/events.jsonl` when any output mode is active).

**Files modified:** `architect/events.py` (new), `architect/provenance.py`
(new), `architect/main.py`, `architect/planner.py`, `architect/executor.py`,
`architect/state.py`.

**Testing:** Add `tests/test_events.py` and `tests/test_provenance.py` with
unit tests for event emission, provenance graph construction, serialization,
and content-addressed IDs.

---

## Section 2: Rich Terminal Dashboard -- DONE

**Goal:** Replace print-based progress reporting with a Rich Live terminal
display that shows the DAG structure, step statuses, timing, and errors in
real time.

**Why:** Currently, progress is reported via `report_progress()` (a single
status line per step) and `print_summary()` (a post-run table). During a
long-running execution with parallel steps, the user sees interleaved log
lines with no spatial structure. There is no way to see at a glance which
steps are running, which are queued, and which have completed.

**Implementation:**

1. Create `architect/dashboard.py` with:
   - A `Dashboard` class that wraps `rich.live.Live`.
   - Internal state: reference to the architect state dict, current phase
     string, list of active step IDs.
   - A `render()` method that builds a `rich.layout.Layout` containing:
     - **Header panel**: Goal text (truncated), current phase, elapsed time.
     - **DAG tree** (`rich.tree.Tree`): Steps organized by execution level.
       Each node shows: `[status_icon] Step N: "title" (Xs)` where
       status_icon is a Rich markup color/symbol:
       - pending: `dim` `[ ]`
       - executing: `bold cyan` `[>]` with a spinner
       - completed: `green` `[+]`
       - failed: `red` `[x]`
       - Dependencies shown as tree structure (children under parents).
     - **Active step detail** (when a step is executing): Current attempt
       number, phase (generating/executing/verifying), elapsed time for
       current attempt, last error preview if retrying.
     - **Timing footer** (`rich.table.Table`): Compact summary of completed
       steps with LLM/sandbox/total time columns.
   - `update(state)` method: Called after every state change. Refreshes the
     Live display.
   - `finish()` method: Stops the Live display and prints the final summary
     table (replaces `print_summary`).
   - Graceful degradation: If stdout is not a TTY (piped or redirected),
     falls back to the existing print-based progress reporting.

2. Integrate into `architect/main.py`:
   - Instantiate `Dashboard` before the execution loop.
   - Call `dashboard.update(state)` at each state-change point (currently
     where `_save_state_threadsafe` is called).
   - Replace `print_plan()`, `report_progress()`, and `print_summary()` calls
     with dashboard methods.
   - The dashboard writes to stderr (same as current logging) so stdout
     remains clean.

3. Thread safety: The `Dashboard.update()` method must be safe to call from
   parallel step execution threads. Use a lock around the render cycle,
   matching the existing `_state_lock` pattern.

4. Add `rich` to `requirements.txt` (it has no compiled dependencies and is
   pure Python).

**Files modified:** `architect/dashboard.py` (new), `architect/main.py`,
`requirements.txt`.

**Testing:** Add `tests/test_dashboard.py` with tests for render output
generation (using `rich.console.Console(file=StringIO())` for capture),
TTY detection fallback, and thread-safe update behavior.

---

## Section 3: Interactive HTML Run Report -- DONE

**Goal:** Generate a self-contained HTML report after each run that provides
interactive exploration of the execution: DAG visualization, execution
timeline, per-step details with syntax-highlighted code, and summary
statistics.

**Why:** The current JSON output (`-o results.json`) contains raw data but no
visualization. `VALIDATION.md` is a minimal text summary. For complex
multi-step runs (like the sci-rehab-dashboard example that took 19 minutes
across 2 steps with rewrites), there is no way to visually understand what
happened, compare timing across steps, or inspect the generated code without
manually reading log files.

**Implementation:**

1. Create `architect/report.py` with:
   - A `generate_report(state, events, provenance, output_path)` function.
   - Uses Jinja2 (stdlib-compatible: use `string.Template` if Jinja2 is not
     available, but prefer Jinja2 and add to requirements.txt) to render an
     HTML template.
   - The HTML file is fully self-contained: all CSS, JS, and data are inlined.
     No external dependencies at runtime. The file can be opened in any
     browser.

2. The HTML report contains these sections (as a tabbed interface using
   vanilla JS):

   **Tab 1 - Overview:**
   - Goal text, overall status, total elapsed time.
   - Summary metrics: steps completed/failed, total LLM time, total sandbox
     time, number of rewrites.
   - Mermaid.js DAG diagram (embedded via CDN-free inline JS): Nodes colored
     by status, edges showing dependencies. Mermaid is chosen over d3-dag
     because it renders from a simple text DSL that Python can trivially
     generate, requires no build step, and the JS library can be inlined.

   **Tab 2 - Timeline:**
   - Plotly.js Gantt chart (inline via CDN-free Plotly bundle, or use a
     lightweight SVG-based approach if size is a concern).
   - X-axis: wall-clock time from run start.
   - Each step is a bar, subdivided into LLM time (one color) and sandbox
     time (another color).
   - Parallel steps appear on separate rows.
   - Rewrites shown as additional segments within a step's row.

   **Tab 3 - Steps:**
   - Expandable accordion for each step.
   - Each step panel contains:
     - Metadata: title, description, dependencies, status, elapsed time.
     - Prompt sent to the Orchestrator (from provenance, collapsible).
     - Generated code (syntax-highlighted using a lightweight inline
       highlighter like Prism.js or a CSS-only approach).
     - Sandbox output (stdout/stderr, collapsible).
     - UAS_RESULT parsed and formatted.
     - If rewrites occurred: a sub-accordion showing each attempt with its
       prompt, code, and error.

   **Tab 4 - Provenance:**
   - Interactive provenance graph rendered with Mermaid.js.
   - Nodes: entities (data), activities (transformations), agents.
   - Clicking a node shows its content in a detail panel.
   - Shows the full causal chain from goal to each output file.

3. Add `--report` / `UAS_REPORT` flag to `architect/main.py` that triggers
   report generation after the run completes (or fails). Default output path:
   `.state/report.html`.

4. The report generator reads from: `state.json`, `events.jsonl`,
   `provenance.json`, and spec files in `.state/specs/`.

**Files modified:** `architect/report.py` (new),
`architect/report_template.html` (new, Jinja2 template), `architect/main.py`,
`requirements.txt` (add `jinja2`).

**Testing:** Add `tests/test_report.py` that generates a report from fixture
data and verifies the HTML contains expected elements (step titles, code
blocks, timeline data).

---

## Section 4: Code Evolution Tracking -- DONE

**Goal:** Track every version of generated code across retries and rewrites,
compute diffs between consecutive attempts, and surface these in the HTML
report and provenance graph.

**Why:** When a step fails and gets rewritten (up to 4 times at the Architect
level, each with 3 Orchestrator retries), the system may produce up to 12
different code versions for a single step. Currently, only the final code is
implicitly captured in sandbox output. There is no record of *how* the code
evolved in response to errors, which is critical for understanding whether the
self-correction mechanism is effective and what patterns of failure occur.

**Implementation:**

1. Create `architect/code_tracker.py` with:
   - A `CodeVersion` dataclass: `step_id`, `spec_attempt` (0-based),
     `orch_attempt` (0-based), `code` (full source), `prompt_hash`,
     `exit_code`, `error_summary` (first 200 chars of error), `timestamp`.
   - A `CodeTracker` class that:
     - Stores versions in memory and writes to
       `.state/code_versions/{step_id}.json`.
     - Provides `record(step_id, spec_attempt, orch_attempt, code, ...)`.
     - Provides `get_versions(step_id)` returning all versions in order.
     - Provides `get_diff(step_id, from_idx, to_idx)` returning a unified
       diff (using `difflib.unified_diff`).
   - A module-level singleton accessor `get_code_tracker()`.

2. Instrument code extraction in `orchestrator/main.py`:
   - After `extract_code(response)` succeeds, call
     `code_tracker.record(...)` with the current attempt context.
   - This requires passing step context into the Orchestrator. Add optional
     `UAS_STEP_ID` and `UAS_SPEC_ATTEMPT` environment variables that the
     Architect sets when invoking the Orchestrator.

3. Instrument the Architect's rewrite loop in `architect/main.py`:
   - After each `run_orchestrator` call in `execute_step`, collect the code
     versions from the Orchestrator's output (parse from the verbose log or
     from the code tracker's persisted files).

4. Enhance the HTML report (Section 3's template):
   - In the Steps tab, for steps with multiple attempts, add a "Code
     Evolution" sub-panel.
   - Display a side-by-side or unified diff view using a CSS-styled
     `<pre>` block with added/removed line highlighting (green/red
     backgrounds).
   - Show the error that triggered each rewrite alongside the diff.
   - Add a "retry effectiveness" metric per step: did the code converge
     toward a solution? (Measured by: errors decreasing in severity, code
     changes becoming smaller, eventual success.)

5. Connect to provenance: Each `CodeVersion` becomes an Entity in the
   provenance graph, linked by `wasDerivedFrom` to the previous version
   and `wasGeneratedBy` to the LLM call activity.

**Files modified:** `architect/code_tracker.py` (new),
`orchestrator/main.py`, `architect/main.py`, `architect/executor.py`,
`architect/report.py` (template additions).

**Testing:** Add `tests/test_code_tracker.py` with tests for version
recording, diff generation, and serialization.

---

## Section 5: Execution Trace Export (Perfetto-compatible)

**Goal:** Export execution data in Chrome Trace Event JSON format, viewable in
Perfetto (ui.perfetto.dev) for microsecond-precision timeline analysis of the
entire run.

**Why:** The HTML report provides a high-level timeline, but for performance
analysis of the system itself (e.g., "why did this run take 19 minutes?",
"how much time is spent in LLM calls vs sandbox execution vs overhead?"),
a dedicated profiling tool provides much richer interaction. Perfetto is a
state-of-the-art trace viewer that can zoom into any time range, filter by
category, and compute aggregate statistics -- all from a simple JSON file
that UAS can generate with no external dependencies.

**Implementation:**

1. Create `architect/trace_export.py` with:
   - A `TraceExporter` class that converts the event log into Chrome Trace
     Event format (JSON array of trace events).
   - Mapping from UAS events to trace spans:
     - Process 1: "Architect" with threads per execution level.
     - Process 2: "Orchestrator" with threads per step.
     - Process 3: "Sandbox" with threads per step.
   - Event mapping:
     - `STEP_START` / `STEP_COMPLETE|STEP_FAILED` -> Duration event (X type)
       on the Architect process.
     - `LLM_CALL_START` / `LLM_CALL_COMPLETE` -> Duration event on the
       Orchestrator process.
     - `SANDBOX_START` / `SANDBOX_COMPLETE` -> Duration event on the Sandbox
       process.
     - `REWRITE_START` / `REWRITE_COMPLETE` -> Nested duration event.
     - `DECOMPOSITION_START` / `DECOMPOSITION_COMPLETE` -> Duration event.
   - Metadata as event args: step title, attempt number, exit code, error
     type, prompt length, code length.
   - Counter events for cumulative metrics: total LLM calls, total sandbox
     runs, cumulative LLM time.

2. Add `--trace` / `UAS_TRACE` flag to `architect/main.py`. Default output:
   `.state/trace.json`.

3. The trace file can be opened directly at `ui.perfetto.dev` (drag and drop)
   or loaded with `chrome://tracing`.

**Files modified:** `architect/trace_export.py` (new), `architect/main.py`.

**Testing:** Add `tests/test_trace_export.py` that generates a trace from
fixture event data and validates the JSON structure against the Chrome Trace
Event schema (correct `ph`, `ts`, `dur`, `pid`, `tid` fields).

---

## Section 6: Decision Explanation Layer

**Goal:** Add an explanation system that uses the provenance graph and event
log to generate human-readable explanations of what happened during a run,
answering "why" questions about the execution.

**Why:** Even with visualization, understanding a complex multi-step run
requires expertise in reading DAGs and trace timelines. An explanation layer
that can answer natural-language questions about the run using only the
recorded data (no additional LLM calls) bridges the gap between raw data
and understanding. This is the XAI component: making the agent's decisions
interpretable.

**Implementation:**

1. Create `architect/explain.py` with:
   - A `RunExplainer` class initialized with state, events, provenance, and
     code tracker data.
   - Pre-computed analyses (run once on initialization):
     - **Critical path**: The longest chain of dependent steps determining
       total wall-clock time. Computed from the DAG + elapsed times.
     - **Bottleneck identification**: Steps or activities that consumed the
       most time. Breakdown: LLM generation time, sandbox execution time,
       overhead (context building, spec generation).
     - **Failure taxonomy**: Classify each failure by type (dependency_error,
       logic_error, environment_error, network_error, timeout, format_error)
       using keyword matching on error messages.
     - **Rewrite effectiveness**: For each rewrite, did it move closer to
       success? Measure by: error type changed, error message changed, code
       diff size, eventual outcome.
     - **Context influence**: For each step with dependencies, which
       dependency outputs were actually referenced in the generated code?
       (Heuristic: check if filenames from dependency outputs appear in the
       generated code.)

   - Query methods:
     - `explain_run()` -> Natural-language summary of the entire run.
     - `explain_step(step_id)` -> Why this step succeeded/failed, what it
       consumed and produced, how long it took and why.
     - `explain_failure(step_id)` -> Root cause analysis of a failed step:
       error taxonomy, rewrite history, what was tried.
     - `explain_critical_path()` -> Which steps determined wall-clock time
       and why.
     - `explain_cost()` -> Time and resource breakdown: where was time spent,
       which steps were expensive, what could be parallelized.

   - Output format: Returns structured text (markdown) that can be printed
     to terminal or included in the HTML report.

2. Add `--explain` / `UAS_EXPLAIN` flag to `architect/main.py`. When enabled,
   prints the run explanation to stderr after the summary table. Can be
   combined with `--report` to include explanations in the HTML report.

3. Integrate into the HTML report (Section 3):
   - Add a "Tab 5 - Explanation" that renders the output of `explain_run()`
     and provides per-step explanation on click.

4. Add a standalone explanation mode:
   - `python3 -m architect.explain [workspace_path]` reads saved state,
     events, provenance, and code versions from a previous run and prints
     the explanation. This allows post-hoc analysis without re-running.

**Files modified:** `architect/explain.py` (new), `architect/__main__.py`
(new, for standalone mode), `architect/main.py`, `architect/report.py`
(template additions).

**Testing:** Add `tests/test_explain.py` with tests for critical path
computation, failure taxonomy, rewrite effectiveness scoring, and explanation
text generation from fixture data.

---

## Dependency Order

Sections must be implemented in order:

```
Section 1 (Events & Provenance)
    |
    +---> Section 2 (Rich Dashboard)  [uses events for update triggers]
    |
    +---> Section 3 (HTML Report)     [reads events + provenance]
              |
              +---> Section 4 (Code Evolution)  [enhances report + provenance]
              |
              +---> Section 5 (Trace Export)    [reads events]
              |
              +---> Section 6 (Explanations)    [reads all data sources]
```

Section 2 can be done in parallel with Section 3 since they modify different
parts of `architect/main.py` (dashboard modifies the execution loop, report
modifies the post-run output). However, for simplicity in sequential sessions,
follow the numbered order.

---

## New Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `rich` | >=13.0 | Terminal dashboard (Section 2) |
| `jinja2` | >=3.1 | HTML report templating (Section 3) |

Both are pure Python with no compiled dependencies. They will be added to
`requirements.txt` and installed in the container via the existing
`pip install` in the Containerfile.

---

## Files Created (Summary)

| File | Section | Purpose |
|------|---------|---------|
| `architect/events.py` | 1 | Structured event system |
| `architect/provenance.py` | 1 | Provenance graph model |
| `architect/dashboard.py` | 2 | Rich terminal dashboard |
| `architect/report.py` | 3 | HTML report generator |
| `architect/report_template.html` | 3 | Jinja2 HTML template |
| `architect/code_tracker.py` | 4 | Code version tracking |
| `architect/trace_export.py` | 5 | Perfetto trace export |
| `architect/explain.py` | 6 | Decision explanation layer |
| `architect/__main__.py` | 6 | Standalone explanation CLI |
| `tests/test_events.py` | 1 | Event system tests |
| `tests/test_provenance.py` | 1 | Provenance graph tests |
| `tests/test_dashboard.py` | 2 | Dashboard tests |
| `tests/test_report.py` | 3 | HTML report tests |
| `tests/test_code_tracker.py` | 4 | Code tracker tests |
| `tests/test_trace_export.py` | 5 | Trace export tests |
| `tests/test_explain.py` | 6 | Explanation layer tests |
