"""Architect Agent: autonomous planner and spec generator.

Takes an abstract human goal, decomposes it into atomic steps,
generates UAS-compliant specs, and drives the Orchestrator to execute them.
"""

import argparse
import concurrent.futures
import json
import logging
import os
import sys
import threading
import time

from .state import init_state, save_state, load_state, add_steps, append_scratchpad, read_scratchpad
from .planner import (
    decompose_goal,
    reflect_and_rewrite,
    decompose_failing_step,
    topological_sort,
    critique_and_refine_plan,
    merge_trivial_steps,
)
from .spec_generator import generate_spec, build_task_from_spec
from .executor import (
    run_orchestrator,
    extract_sandbox_stdout,
    extract_sandbox_stderr,
    extract_workspace_files,
    parse_uas_result,
    scan_workspace_files,
    MAX_CONTEXT_LENGTH,
)
from .events import EventType, get_event_log, reset_event_log
from .provenance import get_provenance_graph, reset_provenance_graph
from .dashboard import Dashboard
from .report import generate_report

MAX_SPEC_REWRITES = 4
MAX_PARALLEL = int(os.environ.get("UAS_MAX_PARALLEL", "4"))
WORKSPACE = os.environ.get("UAS_WORKSPACE", "/workspace")

MAX_GOAL_LENGTH = 10000
MAX_ERROR_LENGTH = int(os.environ.get("UAS_MAX_ERROR_LENGTH", "2000"))
LOG_PREVIEW_LENGTH = 300
OUTPUT_PREVIEW_LENGTH = 200

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()


def _save_state_threadsafe(state: dict):
    """Thread-safe wrapper around save_state for parallel execution."""
    with _state_lock:
        save_state(state)


def configure_logging(verbose: bool = False):
    """Configure logging: INFO by default, DEBUG with --verbose. Logs go to stderr."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, stream=sys.stderr, format="%(message)s")


def parse_args():
    parser = argparse.ArgumentParser(description="UAS Architect Agent")
    parser.add_argument("goal", nargs="*", help="Goal to accomplish")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug output"
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Resume from last saved state instead of starting fresh",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="Force a clean start, ignoring any saved state",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show the decomposition plan without executing it",
    )
    parser.add_argument(
        "-o", "--output", type=str, default=None,
        help="Write a JSON results summary to this file",
    )
    parser.add_argument(
        "--events", type=str, default=None, nargs="?", const="auto",
        help="Write event log to this path (default: .state/events.jsonl)",
    )
    parser.add_argument(
        "--report", type=str, default=None, nargs="?", const="auto",
        help="Generate HTML report at this path (default: .state/report.html)",
    )
    return parser.parse_args()


def get_goal(args) -> str:
    if args.goal:
        return " ".join(args.goal)
    goal = os.environ.get("UAS_GOAL")
    if goal:
        return goal
    print("Enter your goal (submit with Ctrl+D):", file=sys.stderr)
    return sys.stdin.read().strip()


FULL_OUTPUT_DEPS = 2  # Number of most-recent deps to keep in full


def _extract_json_keys(preview: str) -> str:
    """Extract top-level keys/schema from a JSON preview string."""
    try:
        data = json.loads(preview)
        if isinstance(data, dict):
            return str(list(data.keys()))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return f"list of {len(data)} items, keys: {list(data[0].keys())}"
        return type(data).__name__
    except (json.JSONDecodeError, IndexError):
        return preview[:100]


def summarize_context(context: str, goal: str, max_length: int) -> str:
    """Compress context using LLM when it exceeds the limit.

    Preserves: original goal, file paths, error messages, plan state.
    Falls back to simple truncation if LLM compression fails.
    """
    try:
        from orchestrator.llm_client import get_llm_client
        client = get_llm_client()
        prompt = (
            f"Compress the following context to under {max_length} characters "
            "while preserving all essential information.\n\n"
            "MUST preserve:\n"
            "- Original goal and current plan state\n"
            "- All file paths touched\n"
            "- All error messages encountered\n"
            "- Key results and data summaries\n\n"
            "Remove: verbose stdout/stderr output, redundant information, "
            "raw data that can be referenced by file path.\n\n"
            f"Goal: {goal}\n\n"
            f"Context to compress:\n{context}"
        )
        summary = client.generate(prompt)
        if len(summary) <= max_length:
            return summary
    except Exception:
        pass
    # Fallback: simple truncation
    return context[:max_length] + f"\n... [compressed, {len(context)} chars total]"


def build_context(step: dict, completed_outputs: dict,
                  state: dict | None = None,
                  workspace_path: str | None = None) -> str:
    """Build structured XML context from outputs of dependency steps.

    Uses observation masking: the most recent FULL_OUTPUT_DEPS dependencies
    get full output; older ones are replaced with summaries. Includes
    workspace file info and verification criteria from completed steps.

    Each entry in completed_outputs can be a plain string or a dict
    with 'stdout', 'stderr', and 'files' keys.
    """
    if not step["depends_on"]:
        return ""

    parts = []
    dep_ids = sorted(step["depends_on"])

    # Build step lookup for verify fields
    step_by_id = {}
    goal = ""
    if state:
        step_by_id = {s["id"]: s for s in state.get("steps", [])}
        goal = state.get("goal", "")

    # Determine which deps get full output vs masked
    full_deps = set(dep_ids[-FULL_OUTPUT_DEPS:])

    for dep_id in dep_ids:
        output = completed_outputs.get(dep_id, "")
        dep_step = step_by_id.get(dep_id, {})
        verify = dep_step.get("verify", "")

        if dep_id in full_deps:
            # Full output with XML tags
            lines = []
            if isinstance(output, dict):
                stdout = output.get("stdout", "")
                stderr = output.get("stderr", "")
                files = output.get("files", [])
                if stdout:
                    lines.append(f"stdout: {stdout}")
                if stderr:
                    lines.append(f"stderr: {stderr}")
                if files:
                    lines.append(f"files: {', '.join(files)}")
            elif output:
                lines.append(output)

            if verify:
                lines.append(f"<verification>{verify}</verification>")

            if lines:
                content = "\n".join(lines)
                parts.append(
                    f"<previous_step_output step=\"{dep_id}\">\n"
                    f"{content}\n"
                    f"</previous_step_output>"
                )
        else:
            # Masked summary (observation masking)
            files_info = ""
            if isinstance(output, dict):
                files = output.get("files", [])
                if files:
                    files_info = f" - produced files: {', '.join(files)}"
            parts.append(
                f"<step_summary step=\"{dep_id}\">"
                f"[Step {dep_id} output omitted{files_info}]"
                f"</step_summary>"
            )

    # Workspace files section
    if workspace_path:
        try:
            ws_files = scan_workspace_files(workspace_path)
        except Exception:
            ws_files = {}
        if ws_files:
            ws_lines = []
            for fname, info in sorted(ws_files.items()):
                line = f"  {fname} ({info['size']} bytes, {info['type']})"
                preview = info.get("preview", "")
                if preview:
                    if fname.endswith(".json"):
                        line += f"\n    keys: {_extract_json_keys(preview)}"
                    else:
                        line += f"\n    preview: {preview[:200]}"
                ws_lines.append(line)
            parts.append(
                "<workspace_files>\n"
                + "\n".join(ws_lines)
                + "\n</workspace_files>"
            )

    # Scratchpad section
    scratchpad = read_scratchpad()
    if scratchpad:
        parts.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

    context = "\n\n".join(parts)

    # Context length management
    if len(context) > MAX_CONTEXT_LENGTH:
        context = summarize_context(context, goal, MAX_CONTEXT_LENGTH)

    return context


def print_plan(state: dict):
    """Print the step DAG to stderr with titles, descriptions, and dependencies."""
    steps = state["steps"]
    levels = topological_sort(steps)
    step_by_id = {s["id"]: s for s in steps}

    print(f"Goal: {state['goal']}\n", file=sys.stderr)
    print(f"Steps: {len(steps)}", file=sys.stderr)
    print(f"Execution levels: {len(levels)}\n", file=sys.stderr)

    for level_idx, level in enumerate(levels, 1):
        print(f"--- Level {level_idx} (parallel) ---", file=sys.stderr)
        for sid in level:
            step = step_by_id[sid]
            deps = step["depends_on"]
            deps_str = f" [depends on: {deps}]" if deps else ""
            print(f"  Step {sid}: {step['title']}{deps_str}", file=sys.stderr)
            print(f"    {step['description']}", file=sys.stderr)
        print(file=sys.stderr)


def report_progress(step: dict, total: int, completed: int, failed: int, attempt: int = 1):
    """Print a compact progress status line to stderr."""
    print(
        f"[{step['id']}/{total}] Step {step['id']}: \"{step['title']}\" "
        f"(attempt {attempt}, {completed} completed, {failed} failed)",
        file=sys.stderr,
    )


def print_summary(state: dict):
    """Print a summary table of all steps with status, elapsed time, and timing breakdown."""
    steps = state["steps"]
    print(file=sys.stderr)
    print(
        f"{'Step':>4}  {'Title':<40}  {'Status':<12}  {'Elapsed':>8}  {'LLM':>8}  {'Sandbox':>8}",
        file=sys.stderr,
    )
    print(
        f"{'─' * 4}  {'─' * 40}  {'─' * 12}  {'─' * 8}  {'─' * 8}  {'─' * 8}",
        file=sys.stderr,
    )
    for s in steps:
        elapsed = s.get("elapsed", 0.0)
        timing = s.get("timing", {})
        llm_t = timing.get("llm_time", 0.0)
        sandbox_t = timing.get("sandbox_time", 0.0)
        title = s["title"][:40]
        print(
            f"{s['id']:>4}  {title:<40}  {s['status']:<12}  "
            f"{elapsed:>7.1f}s  {llm_t:>7.1f}s  {sandbox_t:>7.1f}s",
            file=sys.stderr,
        )
    total_elapsed = state.get("total_elapsed", 0.0)
    print(
        f"{'─' * 4}  {'─' * 40}  {'─' * 12}  {'─' * 8}  {'─' * 8}  {'─' * 8}",
        file=sys.stderr,
    )
    print(
        f"{'':>4}  {'TOTAL':<40}  {'':12}  {total_elapsed:>7.1f}s",
        file=sys.stderr,
    )


def write_json_output(state: dict, output_path: str):
    """Write a structured JSON summary of the run to the given path."""
    summary = {
        "goal": state.get("goal", ""),
        "status": state.get("status", "unknown"),
        "steps": [
            {
                "id": s["id"],
                "title": s["title"],
                "status": s["status"],
                "elapsed": s.get("elapsed", 0.0),
                "timing": s.get("timing", {}),
            }
            for s in state.get("steps", [])
        ],
        "total_elapsed": state.get("total_elapsed", 0.0),
    }
    parent = os.path.dirname(output_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(summary, f, indent=2)
    logger.info("JSON output written to %s", output_path)


def create_blocker(state: dict, step: dict):
    blocker_path = os.path.join(WORKSPACE, "BLOCKER.md")
    with open(blocker_path, "w") as f:
        f.write("# Architect Blocker\n\n")
        f.write(f"**Goal:** {state['goal']}\n\n")
        f.write(f"**Blocked at step {step['id']}:** {step['title']}\n\n")
        f.write("## Failure Details\n\n")
        f.write(f"The Orchestrator failed this step after all retries, and the "
                f"Architect exhausted {MAX_SPEC_REWRITES} spec rewrites.\n\n")
        f.write(f"**Last task description:**\n```\n{step['description']}\n```\n\n")
        f.write(f"**Last error:**\n```\n{step['error'][:MAX_ERROR_LENGTH]}\n```\n\n")
        f.write("## Required Action\n\n")
        f.write("A human must review the failure above and either:\n")
        f.write("1. Simplify the goal.\n")
        f.write("2. Provide missing credentials or resources.\n")
        f.write("3. Manually fix the failing step and re-run.\n")
    logger.info("Blocker written to %s", blocker_path)


_env_probed = False


def _probe_environment():
    """Run a lightweight environment probe and write results to scratchpad."""
    global _env_probed
    if _env_probed:
        return
    _env_probed = True
    import subprocess as _sp
    lines = ["Environment discovery:"]
    try:
        ver = _sp.run(
            [sys.executable, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        lines.append(f"- Python: {ver.stdout.strip()}")
    except Exception:
        lines.append("- Python version: unknown")
    try:
        pip = _sp.run(
            [sys.executable, "-m", "pip", "list", "--format=columns"],
            capture_output=True, text=True, timeout=15,
        )
        pkg_lines = pip.stdout.strip().split("\n")
        lines.append(f"- Installed packages: {len(pkg_lines) - 2}")
        for p in pkg_lines[:12]:
            lines.append(f"  {p}")
        if len(pkg_lines) > 12:
            lines.append(f"  ... and {len(pkg_lines) - 12} more")
    except Exception:
        lines.append("- Installed packages: unknown")
    try:
        df = _sp.run(["df", "-h", WORKSPACE], capture_output=True, text=True, timeout=5)
        df_lines = df.stdout.strip().split("\n")
        if len(df_lines) >= 2:
            lines.append(f"- Disk: {df_lines[1]}")
    except Exception:
        pass
    append_scratchpad("\n".join(lines))


def validate_uas_result(step: dict, workspace: str) -> str | None:
    """Validate a parsed UAS_RESULT against reality.

    Checks the status field and verifies that claimed files exist on disk.
    Returns None if validation passed, or an error message string if failed.
    """
    uas_result = step.get("uas_result")
    if not uas_result:
        return None

    if uas_result.get("status") == "error":
        error = uas_result.get("error", "unknown error")
        return f"UAS_RESULT reports error: {error}"

    for f in uas_result.get("files_written", []):
        fpath = os.path.join(workspace, f) if not os.path.isabs(f) else f
        if not os.path.exists(fpath):
            return f"UAS_RESULT claims file '{f}' was written but it does not exist"

    return None


def verify_step_output(step: dict, workspace: str) -> str | None:
    """Verify step output against the step's verify criteria.

    Generates a verification task and runs it through the orchestrator.
    Returns None if verification passed, or an error message if failed.
    """
    verify = step.get("verify", "")
    if not verify:
        return None

    files_info = ""
    if step.get("files_written"):
        files_info = f"\nFiles created by this step: {', '.join(step['files_written'])}"

    output_info = ""
    if step.get("output"):
        output_info = f"\nStep stdout (last 500 chars): {step['output'][-500:]}"

    task = (
        f"Write a Python verification script that checks the following:\n\n"
        f"Verification criteria: {verify}\n\n"
        f"Context:{files_info}{output_info}\n\n"
        f"Requirements:\n"
        f"- Use workspace = os.environ.get('WORKSPACE', '/workspace')\n"
        f"- Print 'VERIFICATION PASSED' if all checks pass\n"
        f"- Print 'VERIFICATION FAILED: <reason>' and exit(1) if any check fails\n"
        f"- Be thorough but concise\n"
    )

    result = run_orchestrator(task)

    stdout = extract_sandbox_stdout(result.get("stderr", ""))
    all_output = (stdout or "") + (result.get("stdout", "") or "")

    if result["exit_code"] == 0 and "VERIFICATION PASSED" in all_output:
        return None

    error = stdout or result.get("stderr", "") or "Verification script failed"
    return error[:MAX_ERROR_LENGTH]


def validate_workspace(state: dict, workspace: str) -> dict:
    """Final validation after all steps complete.

    Checks that claimed files exist and workspace isn't empty.
    Writes VALIDATION.md to the workspace summarizing what was produced.
    """
    all_files = []
    missing_files = []

    for step in state["steps"]:
        for f in step.get("files_written", []):
            all_files.append(f)
            fpath = os.path.join(workspace, f) if not os.path.isabs(f) else f
            if not os.path.exists(fpath):
                missing_files.append(f)

    try:
        ws_entries = [e for e in os.listdir(workspace) if not e.startswith(".")]
    except OSError:
        ws_entries = []

    lines = ["# Workspace Validation Report\n\n"]
    lines.append(f"**Goal:** {state.get('goal', 'N/A')}\n\n")
    completed = sum(1 for s in state["steps"] if s["status"] == "completed")
    lines.append(f"**Steps completed:** {completed}/{len(state['steps'])}\n\n")
    lines.append("## Workspace Contents\n\n")
    lines.append(f"- Files in workspace: {len(ws_entries)}\n")
    lines.append(f"- Files referenced by steps: {len(all_files)}\n\n")

    if ws_entries:
        lines.append("### Files\n\n")
        for entry in sorted(ws_entries):
            path = os.path.join(workspace, entry)
            try:
                size = os.path.getsize(path)
                lines.append(f"- `{entry}` ({size} bytes)\n")
            except OSError:
                lines.append(f"- `{entry}` (size unknown)\n")
        lines.append("\n")

    if missing_files:
        lines.append("## Missing Files\n\n")
        lines.append(
            "The following files were reported as written but do not exist:\n\n"
        )
        for f in missing_files:
            lines.append(f"- `{f}`\n")
        lines.append("\n")

    if not ws_entries:
        lines.append(
            "## Warning\n\nWorkspace is empty — no output files were produced.\n"
        )

    try:
        validation_path = os.path.join(workspace, "VALIDATION.md")
        with open(validation_path, "w") as f:
            f.writelines(lines)
        logger.info("Validation report written to %s", validation_path)
    except OSError as e:
        logger.warning("Could not write VALIDATION.md: %s", e)

    return {
        "missing_files": missing_files,
        "workspace_empty": len(ws_entries) == 0,
    }


def execute_step(step: dict, state: dict, completed_outputs: dict,
                 progress_counts: dict | None = None,
                 dashboard: Dashboard | None = None) -> bool:
    """Execute a single step, with spec rewrite retries.

    Returns True on success, False on unrecoverable failure.
    """
    total = len(state["steps"])
    _probe_environment()
    context = build_context(step, completed_outputs, state=state,
                            workspace_path=WORKSPACE)
    counts = progress_counts or {"completed": 0, "failed": 0}
    step_start = time.monotonic()

    event_log = get_event_log()
    prov = get_provenance_graph()
    event_log.emit(EventType.STEP_START, step_id=step["id"],
                   data={"title": step["title"]})
    prev_error_entity = None

    for spec_attempt in range(1 + MAX_SPEC_REWRITES):
        if dashboard:
            dashboard.report_progress(step, total, counts["completed"],
                                      counts["failed"],
                                      attempt=spec_attempt + 1)
        else:
            report_progress(step, total, counts["completed"], counts["failed"],
                            attempt=spec_attempt + 1)
        label = f"Step {step['id']}/{total}: {step['title']}"
        if spec_attempt > 0:
            label += f" (rewrite {spec_attempt}/{MAX_SPEC_REWRITES})"
        logger.info("\n%s", "=" * 60)
        logger.info("  %s", label)
        logger.info("%s", "=" * 60)

        event_log.emit(EventType.CONTEXT_BUILT, step_id=step["id"],
                       data={"context_length": len(context)})

        # Generate UAS spec
        spec_file = generate_spec(step, total, context)
        logger.info("  Spec written: %s", spec_file)

        # Build task for Orchestrator
        task = build_task_from_spec(step, context)
        logger.info("  Sending to Orchestrator...")

        # Execute
        step["status"] = "executing"
        _save_state_threadsafe(state)
        if dashboard:
            dashboard.update(state)

        event_log.emit(EventType.LLM_CALL_START, step_id=step["id"],
                       attempt=spec_attempt + 1)
        orch_start = time.monotonic()
        result = run_orchestrator(task)
        orch_elapsed = time.monotonic() - orch_start
        event_log.emit(EventType.LLM_CALL_COMPLETE, step_id=step["id"],
                       attempt=spec_attempt + 1, duration=orch_elapsed,
                       data={"exit_code": result["exit_code"]})

        # Accumulate per-step timing
        timing = step.setdefault("timing", {
            "llm_time": 0.0, "sandbox_time": 0.0, "total_time": 0.0,
        })
        timing["total_time"] += orch_elapsed
        # Approximate split: sandbox_time from result if available, else all is total
        sandbox_t = result.get("sandbox_time", 0.0)
        timing["sandbox_time"] += sandbox_t
        timing["llm_time"] += max(orch_elapsed - sandbox_t, 0.0)

        logger.info("  Orchestrator exit code: %s (%.1fs)", result["exit_code"], orch_elapsed)

        if result["exit_code"] == 0:
            step["output"] = extract_sandbox_stdout(result["stderr"])
            step["stderr_output"] = extract_sandbox_stderr(result["stderr"])
            step["files_written"] = extract_workspace_files(
                result["stderr"]
            )
            # Parse structured UAS_RESULT if present
            uas_result = parse_uas_result(result["stderr"])
            if uas_result:
                step["uas_result"] = uas_result
                if uas_result.get("files_written"):
                    step["files_written"] = list(set(
                        step["files_written"] + uas_result["files_written"]
                    ))
                if uas_result.get("summary"):
                    step["summary"] = uas_result["summary"]

            # Post-execution validation
            failure_reason = validate_uas_result(step, WORKSPACE)
            if failure_reason is None and step.get("verify"):
                logger.info("  Verifying step output...")
                event_log.emit(EventType.VERIFICATION_START,
                               step_id=step["id"])
                failure_reason = verify_step_output(step, WORKSPACE)
                event_log.emit(
                    EventType.VERIFICATION_COMPLETE,
                    step_id=step["id"],
                    data={"passed": failure_reason is None},
                )

            if failure_reason is None:
                # All validation passed
                step["status"] = "completed"
                step["error"] = ""
                step["elapsed"] = time.monotonic() - step_start
                _save_state_threadsafe(state)
                if dashboard:
                    dashboard.update(state)
                logger.info("  Step %s SUCCEEDED.", step["id"])

                # Record provenance for successful step
                orchestrator_agent = prov.add_agent("orchestrator_llm")
                prompt_entity = prov.add_entity(
                    f"prompt_step{step['id']}", content=task,
                )
                orch_activity = prov.add_activity(
                    f"orchestrate_step{step['id']}",
                    content=f"step{step['id']}_attempt{spec_attempt}",
                )
                prov.used(orch_activity, prompt_entity)
                prov.was_associated_with(orch_activity, orchestrator_agent)
                output_content = step.get("output", "") or ""
                result_entity = prov.add_entity(
                    f"result_step{step['id']}",
                    content=output_content[:500],
                )
                prov.was_generated_by(result_entity, orch_activity)
                if prev_error_entity:
                    prov.was_derived_from(result_entity, prev_error_entity)

                event_log.emit(
                    EventType.STEP_COMPLETE, step_id=step["id"],
                    duration=step["elapsed"],
                    data={"files_written": step.get("files_written", [])},
                )
                if step["output"]:
                    logger.info("  Output: %s", step["output"][:OUTPUT_PREVIEW_LENGTH])
                # Scratchpad: record success
                files_info = ""
                if step.get("files_written"):
                    files_info = f"\nFiles created: {', '.join(step['files_written'])}"
                summary = step.get("summary", step["output"][:200] if step["output"] else "")
                append_scratchpad(
                    f"Step {step['id']} ({step['title']}) SUCCEEDED "
                    f"in {step['elapsed']:.1f}s.{files_info}\n"
                    f"Summary: {summary}"
                )
                return True

            # Validation failed — treat as step failure
            error_info = failure_reason
        else:
            # Execution failed
            error_info = result["stderr"] or result["stdout"] or "Unknown error"

        step["error"] = error_info
        step["status"] = "failed"
        _save_state_threadsafe(state)
        if dashboard:
            dashboard.update(state)

        # Record error provenance for cross-attempt linking
        prev_error_entity = prov.add_entity(
            f"error_step{step['id']}_attempt{spec_attempt}",
            content=error_info[:500],
        )

        logger.error("  Step %s FAILED.", step["id"])
        logger.error("  Error: %s", error_info[:LOG_PREVIEW_LENGTH])

        # Scratchpad: record failure
        append_scratchpad(
            f"Step {step['id']} ({step['title']}) FAILED "
            f"(attempt {spec_attempt + 1}).\n"
            f"Error: {error_info[:500]}"
        )

        if spec_attempt < MAX_SPEC_REWRITES:
            event_log.emit(EventType.REWRITE_START, step_id=step["id"],
                           attempt=spec_attempt + 1)
            logger.info(
                "  Rewriting spec (rewrite %d/%d)...",
                spec_attempt + 1,
                MAX_SPEC_REWRITES,
            )
            if spec_attempt == 2:
                # 3rd failure: decompose into sub-phases
                logger.info("  Escalation: decomposing step into sub-phases...")
                step["description"] = decompose_failing_step(
                    step, result["stdout"], result["stderr"]
                )
            else:
                # 1st, 2nd, 4th failure: reflection-based rewrite
                step["description"] = reflect_and_rewrite(
                    step, result["stdout"], result["stderr"],
                    escalation_level=spec_attempt,
                )
            step["rewrites"] = spec_attempt + 1
            _save_state_threadsafe(state)
            if dashboard:
                dashboard.update(state)
            event_log.emit(EventType.REWRITE_COMPLETE, step_id=step["id"],
                           attempt=spec_attempt + 1)
        else:
            logger.error(
                "  Exhausted all spec rewrites for step %s.", step["id"]
            )

    event_log.emit(EventType.STEP_FAILED, step_id=step["id"],
                   data={"error": step.get("error", "")[:200]})
    step["elapsed"] = time.monotonic() - step_start
    return False


def try_resume() -> dict | None:
    """Attempt to load and validate saved state for resumption.

    Returns the state dict if valid and resumable, None otherwise.
    """
    state = load_state()
    if state is None:
        logger.info("No saved state found, starting fresh.")
        return None
    if state.get("status") == "completed":
        logger.info("Previous run already completed, starting fresh.")
        return None
    if not state.get("steps"):
        logger.info("Saved state has no steps, starting fresh.")
        return None
    return state


def main():
    args = parse_args()
    verbose = args.verbose or os.environ.get("UAS_VERBOSE", "").lower() in (
        "1", "true", "yes",
    )
    configure_logging(verbose)

    dry_run = args.dry_run or os.environ.get("UAS_DRY_RUN", "").lower() in (
        "1", "true", "yes",
    )

    output_path = args.output or os.environ.get("UAS_OUTPUT") or None

    # Report flag
    report_flag = args.report or os.environ.get("UAS_REPORT") or None

    # Initialize event log and provenance graph
    events_flag = args.events or os.environ.get("UAS_EVENTS") or None
    if events_flag:
        state_dir = os.path.join(WORKSPACE, ".state")
        events_path = (
            os.path.join(state_dir, "events.jsonl")
            if events_flag == "auto"
            else events_flag
        )
        provenance_path = os.path.join(state_dir, "provenance.json")
    else:
        events_path = None
        provenance_path = None
    reset_event_log()
    reset_provenance_graph()
    event_log = get_event_log(events_path=events_path)
    prov = get_provenance_graph(output_path=provenance_path)

    resume = (args.resume or os.environ.get("UAS_RESUME", "").lower() in (
        "1", "true", "yes",
    )) and not args.fresh

    # Try to resume from saved state
    state = None
    if resume:
        state = try_resume()

    if state is not None:
        logger.info("Resuming goal: %s\n", state["goal"])
    else:
        # Fresh start
        goal = get_goal(args)
        if not goal:
            logger.error("No goal provided.")
            sys.exit(1)

        if len(goal) > MAX_GOAL_LENGTH:
            logger.warning(
                "Goal is very long (%d chars, max recommended %d). "
                "Consider simplifying.",
                len(goal),
                MAX_GOAL_LENGTH,
            )

        logger.info("Goal: %s\n", goal)
        event_log.emit(EventType.GOAL_RECEIVED, data={"goal": goal})
        goal_entity = prov.add_entity("goal", content=goal)
        planner_agent = prov.add_agent("planner_llm")

        # Phase 1: Decompose
        logger.info("Phase 1: Decomposing goal into atomic steps...")
        event_log.emit(EventType.DECOMPOSITION_START)
        decompose_start = time.monotonic()
        state = init_state(goal)
        try:
            steps = decompose_goal(goal)
        except Exception as e:
            logger.error("Failed to decompose goal: %s", e)
            state["status"] = "failed"
            save_state(state)
            sys.exit(1)
        decompose_elapsed = time.monotonic() - decompose_start

        decompose_activity = prov.add_activity(
            "decompose", content=json.dumps([s.get("title", "") for s in steps]),
        )
        prov.used(decompose_activity, goal_entity)
        prov.was_associated_with(decompose_activity, planner_agent)
        plan_entity = prov.add_entity(
            "plan", content=json.dumps(steps),
        )
        prov.was_generated_by(plan_entity, decompose_activity)
        prov.was_derived_from(plan_entity, goal_entity)
        event_log.emit(
            EventType.DECOMPOSITION_COMPLETE,
            duration=decompose_elapsed,
            data={"num_steps": len(steps)},
        )

        # Critique and refine if multi-step plan
        if len(steps) > 1:
            logger.info("  Critiquing plan...")
            event_log.emit(EventType.PLAN_CRITIQUE, data={"num_steps": len(steps)})
            steps = critique_and_refine_plan(goal, steps)

        # Merge trivial steps to reduce LLM calls
        if len(steps) > 1:
            pre_merge = len(steps)
            steps = merge_trivial_steps(steps)
            if len(steps) < pre_merge:
                event_log.emit(
                    EventType.STEP_MERGE,
                    data={"before": pre_merge, "after": len(steps)},
                )

        state = add_steps(state, steps)
        logger.info("  Decomposed into %d step(s):", len(steps))
        for s in state["steps"]:
            deps = f" (depends on {s['depends_on']})" if s["depends_on"] else ""
            logger.info("    %s. %s%s", s["id"], s["title"], deps)

    # Create dashboard
    dashboard = Dashboard(state)

    # Dry-run: show the plan and exit
    if dry_run:
        dashboard.print_plan(state)
        sys.exit(0)

    # Phase 2: Execute (resume-aware, parallel where possible)
    logger.info("\nPhase 2: Executing steps via Orchestrator...")
    dashboard.set_phase("executing")
    dashboard.start()
    completed_outputs = {}
    step_by_id = {s["id"]: s for s in state["steps"]}
    levels = topological_sort(state["steps"])
    progress_counts = {"completed": 0, "failed": 0}
    run_start = time.monotonic()

    for level in levels:
        level_steps = [step_by_id[sid] for sid in level]

        # Separate already-completed from pending
        pending = []
        for step in level_steps:
            if step["status"] == "completed":
                logger.info("  Skipping step %s (already completed): %s",
                            step["id"], step["title"])
                completed_outputs[step["id"]] = {
                    "stdout": step.get("output", ""),
                    "stderr": step.get("stderr_output", ""),
                    "files": step.get("files_written", []),
                }
            else:
                pending.append(step)

        if not pending:
            continue

        if len(pending) == 1:
            # Single step — no threading overhead needed
            step = pending[0]
            success = execute_step(step, state, completed_outputs,
                                   progress_counts, dashboard=dashboard)
            if not success:
                progress_counts["failed"] += 1
                state["total_elapsed"] = time.monotonic() - run_start
                state["status"] = "blocked"
                save_state(state)
                create_blocker(state, step)
                if output_path:
                    write_json_output(state, output_path)
                event_log.emit(EventType.RUN_COMPLETE, data={
                    "status": "blocked",
                    "total_elapsed": state["total_elapsed"],
                })
                prov.save()
                dashboard.finish(state)
                logger.error("HALTED: Step %s failed irrecoverably.", step["id"])
                sys.exit(1)
            progress_counts["completed"] += 1
            completed_outputs[step["id"]] = {
                "stdout": step.get("output", ""),
                "stderr": step.get("stderr_output", ""),
                "files": step.get("files_written", []),
            }
        else:
            # Multiple independent steps — run in parallel
            workers = min(len(pending), MAX_PARALLEL)
            logger.info("  Running %d independent steps in parallel (max %d workers): %s",
                        len(pending), workers, [s["id"] for s in pending])
            failed_step = None
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
            ) as executor:
                future_to_step = {
                    executor.submit(
                        execute_step, step, state, completed_outputs,
                        progress_counts, dashboard,
                    ): step
                    for step in pending
                }
                for future in concurrent.futures.as_completed(future_to_step):
                    step = future_to_step[future]
                    try:
                        success = future.result()
                    except Exception as exc:
                        logger.error("  Step %s raised exception: %s",
                                     step["id"], exc)
                        step["status"] = "failed"
                        step["error"] = str(exc)
                        success = False
                    if success:
                        progress_counts["completed"] += 1
                        completed_outputs[step["id"]] = {
                            "stdout": step.get("output", ""),
                            "stderr": step.get("stderr_output", ""),
                            "files": step.get("files_written", []),
                        }
                    elif failed_step is None:
                        progress_counts["failed"] += 1
                        failed_step = step

            if failed_step is not None:
                state["total_elapsed"] = time.monotonic() - run_start
                state["status"] = "blocked"
                save_state(state)
                create_blocker(state, failed_step)
                if output_path:
                    write_json_output(state, output_path)
                event_log.emit(EventType.RUN_COMPLETE, data={
                    "status": "blocked",
                    "total_elapsed": state["total_elapsed"],
                })
                prov.save()
                dashboard.finish(state)
                logger.error("HALTED: Step %s failed irrecoverably.",
                             failed_step["id"])
                sys.exit(1)

    # All done
    state["total_elapsed"] = time.monotonic() - run_start
    state["status"] = "completed"
    save_state(state)

    # Final workspace validation
    validation = validate_workspace(state, WORKSPACE)
    if validation["missing_files"]:
        logger.warning(
            "  Some referenced files are missing: %s",
            ", ".join(validation["missing_files"]),
        )
    if validation["workspace_empty"]:
        logger.warning("  Warning: workspace is empty")

    if output_path:
        write_json_output(state, output_path)

    event_log.emit(EventType.RUN_COMPLETE, data={
        "status": state["status"],
        "total_elapsed": state.get("total_elapsed", 0.0),
    })
    prov.save()

    # Generate HTML report if requested
    if report_flag:
        state_dir = os.path.join(WORKSPACE, ".state")
        report_path = (
            os.path.join(state_dir, "report.html")
            if report_flag == "auto"
            else report_flag
        )
        try:
            events_data = [e.to_dict() for e in event_log.events]
            prov_data = prov.to_dict()
            generate_report(state, events_data, prov_data, report_path)
            logger.info("HTML report written to: %s", report_path)
        except Exception as e:
            logger.warning("Failed to generate HTML report: %s", e)

    logger.info("\n%s", "=" * 60)
    logger.info("  ALL STEPS COMPLETED SUCCESSFULLY")
    logger.info("%s", "=" * 60)
    dashboard.finish(state)
    logger.info(
        "State saved to: %s",
        os.path.join(".state", "state.json"),
    )
    logger.info(
        "Specs saved to: %s/",
        os.path.join(".state", "specs"),
    )
    if events_path:
        logger.info("Events written to: %s", events_path)
    if provenance_path:
        logger.info("Provenance written to: %s", provenance_path)


if __name__ == "__main__":
    main()
