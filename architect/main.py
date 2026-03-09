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

from .state import (
    init_state, save_state, load_state, add_steps,
    append_scratchpad, read_scratchpad,
    update_progress_file, read_progress_file,
)
from .planner import (
    decompose_goal,
    decompose_goal_with_voting,
    reflect_and_rewrite,
    decompose_failing_step,
    generate_reflection,
    trace_root_cause,
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
    format_workspace_scan,
    MAX_CONTEXT_LENGTH,
)
from .events import EventType, get_event_log, reset_event_log
from .provenance import get_provenance_graph, reset_provenance_graph
from .code_tracker import get_code_tracker, reset_code_tracker
from .dashboard import Dashboard
from .report import generate_report
from .trace_export import TraceExporter
from .explain import RunExplainer, classify_failure

MAX_SPEC_REWRITES = 4
MAX_PARALLEL = int(os.environ.get("UAS_MAX_PARALLEL", "0"))
WORKSPACE = os.environ.get("UAS_WORKSPACE", "/workspace")

MAX_ERROR_LENGTH = int(os.environ.get("UAS_MAX_ERROR_LENGTH", "0"))

# Section 3b: Error-type-adaptive retry budgets.
# Maps error type to max retries before early escalation.
# Does not reduce MAX_SPEC_REWRITES — just exits the loop early for
# error types where additional retries are unlikely to help.
_ERROR_RETRY_BUDGETS = {
    "dependency_error": 1,
    "logic_error": MAX_SPEC_REWRITES,
    "environment_error": 1,
    "network_error": 2,
    "timeout": 0,
    "format_error": 2,
    "unknown": MAX_SPEC_REWRITES,
}

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
        "-o", "--output", type=str, default=None, nargs="?", const="auto",
        help="Write a JSON results summary (default: .state/output.json)",
    )
    parser.add_argument(
        "--events", type=str, default=None, nargs="?", const="auto",
        help="Write event log to this path (default: .state/events.jsonl)",
    )
    parser.add_argument(
        "--report", type=str, default=None, nargs="?", const="auto",
        help="Generate HTML report at this path (default: .state/report.html)",
    )
    parser.add_argument(
        "--trace", type=str, default=None, nargs="?", const="auto",
        help="Export Perfetto trace to this path (default: .state/trace.json)",
    )
    parser.add_argument(
        "--explain", action="store_true", default=False,
        help="Print run explanation to stderr after completion",
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
        client = get_llm_client(role="planner")
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


def compress_context(context: str, max_length: int,
                     goal: str = "",
                     progress_content: str = "") -> str:
    """Tiered context compression (Section 4c).

    Tier 1 (< 60% of limit): No compression, include everything.
    Tier 2 (60-80%): Remove file previews, truncate stdout per step.
    Tier 3 (80-100%): Summarize dep outputs via LLM; keep progress file.
    Tier 4 (> 100%): Emergency truncation — progress file + last dep only.
    """
    if max_length <= 0:
        return context

    ratio = len(context) / max_length

    # Tier 1: No compression needed
    if ratio < 0.6:
        return context

    # Tier 2: Deterministic compression — remove previews, truncate stdout
    if ratio < 0.8:
        import re
        compressed = context
        # Remove preview lines (indented lines starting with "preview:")
        compressed = re.sub(r'\n    preview: [^\n]*', '', compressed)
        # Remove JSON key lines
        compressed = re.sub(r'\n    keys: [^\n]*', '', compressed)
        # Truncate stdout within dependency blocks to last 500 chars
        def _truncate_stdout(m):
            text = m.group(1)
            if len(text) > 500:
                return f"stdout: ...{text[-500:]}"
            return m.group(0)
        compressed = re.sub(
            r'stdout: (.*?)(?=\n(?:stderr:|files:|</)|$)',
            _truncate_stdout,
            compressed,
            flags=re.DOTALL,
        )
        if len(compressed) <= max_length:
            return compressed
        # If still too long, fall through to Tier 3

    # Tier 3: LLM summarization of dependency outputs, keep progress file
    if ratio < 1.0 or (ratio >= 0.8 and ratio < 1.0):
        try:
            return summarize_context(context, goal, max_length)
        except Exception:
            pass
        # Fall through to Tier 4

    # Tier 4: Emergency truncation — progress file + tail of context
    if progress_content:
        budget = max_length - len(progress_content) - 50
        if budget > 200:
            return (
                progress_content + "\n\n"
                + "... [emergency truncation]\n"
                + context[-budget:]
            )
        return progress_content[:max_length]

    return context[:max_length] + f"\n... [truncated, {len(context)} chars total]"


def _distill_dependency_output(dep_id: int, dep_step: dict,
                               output: str | dict) -> str:
    """Distill a dependency's output into structured XML (Section 4d).

    Uses the step's summary/UAS_RESULT as primary info, falling back
    to raw stdout only when no structured summary is available.
    """
    title = dep_step.get("title", f"Step {dep_id}")
    summary = dep_step.get("summary", "")
    files_written = dep_step.get("files_written", [])
    verify = dep_step.get("verify", "")

    # Build files_produced line
    files_str = ""
    if files_written:
        files_str = ", ".join(files_written[:10])

    # Build key_outputs from summary or output
    key_outputs = summary
    if not key_outputs:
        if isinstance(output, dict):
            stdout = output.get("stdout", "")
            key_outputs = stdout[:300] if stdout else ""
        elif isinstance(output, str):
            key_outputs = output[:300]

    # Build relevant_data from raw output (truncated)
    relevant_data = ""
    if isinstance(output, dict):
        stderr = output.get("stderr", "")
        if stderr:
            relevant_data = f"stderr: {stderr[:200]}"
    elif isinstance(output, str) and not summary:
        # Only include raw output as fallback when no structured summary
        relevant_data = output[:500]

    parts = [f'<dependency step="{dep_id}" title="{title}">']
    if files_str:
        parts.append(f"  <files_produced>{files_str}</files_produced>")
    if key_outputs:
        parts.append(f"  <key_outputs>{key_outputs}</key_outputs>")
    if relevant_data:
        parts.append(f"  <relevant_data>{relevant_data}</relevant_data>")
    if verify:
        parts.append(f"  <verification>{verify}</verification>")
    parts.append("</dependency>")

    return "\n".join(parts)


def build_context(step: dict, completed_outputs: dict,
                  state: dict | None = None,
                  workspace_path: str | None = None) -> str:
    """Build structured XML context from outputs of dependency steps.

    Uses distilled dependency summaries (Section 4d), structured progress
    file (Section 4a), recursive workspace scan (Section 4b), and tiered
    compression (Section 4c).

    Each entry in completed_outputs can be a plain string or a dict
    with 'stdout', 'stderr', and 'files' keys.
    """
    if not step["depends_on"]:
        return ""

    parts = []
    dep_ids = sorted(step["depends_on"])

    # Build step lookup
    step_by_id = {}
    goal = ""
    if state:
        step_by_id = {s["id"]: s for s in state.get("steps", [])}
        goal = state.get("goal", "")

    # Section 4d: Distilled dependency outputs
    for dep_id in dep_ids:
        output = completed_outputs.get(dep_id, "")
        dep_step = step_by_id.get(dep_id, {})

        # Use distilled output if we have step metadata
        if dep_step:
            distilled = _distill_dependency_output(dep_id, dep_step, output)
            # Only include if there's actual content
            if "<key_outputs>" in distilled or "<files_produced>" in distilled:
                parts.append(distilled)
                continue

        # Fallback: legacy format for plain string/dict outputs
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

        verify = dep_step.get("verify", "")
        if verify:
            lines.append(f"<verification>{verify}</verification>")

        if lines:
            content = "\n".join(lines)
            parts.append(
                f"<previous_step_output step=\"{dep_id}\">\n"
                f"{content}\n"
                f"</previous_step_output>"
            )

    # Section 4b: Recursive workspace files section
    if workspace_path:
        try:
            ws_files = scan_workspace_files(workspace_path)
        except Exception:
            ws_files = {}
        if ws_files:
            formatted = format_workspace_scan(
                ws_files, json_key_extractor=_extract_json_keys
            )
            if formatted:
                parts.append(
                    "<workspace_files>\n"
                    + formatted
                    + "\n</workspace_files>"
                )

    # Section 4a: Structured progress file (replaces raw scratchpad)
    progress = read_progress_file()
    if progress:
        parts.append(f"<progress>\n{progress}\n</progress>")
    else:
        # Fallback to scratchpad if no progress file yet
        scratchpad = read_scratchpad()
        if scratchpad:
            parts.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

    context = "\n\n".join(parts)

    # Section 4c: Tiered context compression
    if MAX_CONTEXT_LENGTH and len(context) > MAX_CONTEXT_LENGTH:
        context = compress_context(
            context, MAX_CONTEXT_LENGTH,
            goal=goal, progress_content=progress,
        )

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
    state_dir = os.path.join(WORKSPACE, ".state")
    os.makedirs(state_dir, exist_ok=True)
    blocker_path = os.path.join(state_dir, "blocker.md")
    with open(blocker_path, "w") as f:
        f.write("# Architect Blocker\n\n")
        f.write(f"**Goal:** {state['goal']}\n\n")
        f.write(f"**Blocked at step {step['id']}:** {step['title']}\n\n")
        f.write("## Failure Details\n\n")
        f.write(f"The Orchestrator failed this step after all retries, and the "
                f"Architect exhausted {MAX_SPEC_REWRITES} spec rewrites.\n\n")
        f.write(f"**Last task description:**\n```\n{step['description']}\n```\n\n")
        f.write(f"**Last error:**\n```\n{step['error'][:MAX_ERROR_LENGTH or None]}\n```\n\n")
        f.write("## Required Action\n\n")
        f.write("A human must review the failure above and either:\n")
        f.write("1. Simplify the goal.\n")
        f.write("2. Provide missing credentials or resources.\n")
        f.write("3. Manually fix the failing step and re-run.\n")
    # Store blocker info in state for programmatic access
    state["blocker"] = {
        "step_id": step["id"],
        "title": step["title"],
        "error": step["error"][:MAX_ERROR_LENGTH or None],
    }
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


import re as _re

# Patterns that indicate best-practice violations in generated code.
_GUARDRAIL_CHECKS = [
    # (pattern, description, severity)
    # severity: "error" triggers rewrite, "warning" is logged but allowed
    (_re.compile(r'\bexcept\s*:', _re.MULTILINE),
     "bare except: clause (use specific exception types)", "warning"),
    (_re.compile(r"""(?:['"])(?:sk-[a-zA-Z0-9]{20,}|AKIA[A-Z0-9]{16}|ghp_[a-zA-Z0-9]{36})['"]"""),
     "possible hardcoded secret/API key", "error"),
    (_re.compile(r'\beval\s*\('),
     "use of eval() is a security risk", "warning"),
    (_re.compile(r'\bexec\s*\('),
     "use of exec() is a security risk", "warning"),
    (_re.compile(r'shell\s*=\s*True'),
     "subprocess with shell=True is a security risk", "warning"),
    (_re.compile(r'''http://(?!localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])'''),
     "plain HTTP URL detected (use HTTPS)", "warning"),
    (_re.compile(r'\bgit\s+init\b(?!.*-b\s)'),
     "git init without -b flag (should use git init -b main)", "warning"),
    (_re.compile(r'''["']git["']\s*,\s*["']init["'](?!.*["']-b["'])'''),
     "git init without -b flag (should use git init -b main)", "warning"),
]


def check_guardrails(code: str) -> list[dict]:
    """Scan generated code for best-practice violations.

    Returns a list of dicts with keys: line, pattern, description, severity.
    """
    violations = []
    for pattern, description, severity in _GUARDRAIL_CHECKS:
        for match in pattern.finditer(code):
            line_num = code[:match.start()].count("\n") + 1
            violations.append({
                "line": line_num,
                "match": match.group()[:80],
                "description": description,
                "severity": severity,
            })
    return violations


def check_project_guardrails(workspace: str) -> list[str]:
    """Check workspace-level best practices after step execution.

    Returns a list of warning strings for any issues found.
    Checks are only applied when the workspace looks like a project
    (has multiple Python files or a setup file).
    """
    warnings = []

    try:
        entries = os.listdir(workspace)
    except OSError:
        return warnings

    # Detect if this workspace looks like a project (not a one-off script)
    py_files = [e for e in entries if e.endswith(".py") and not e.startswith(".")]
    has_setup = any(e in entries for e in ("pyproject.toml", "setup.py", "setup.cfg"))
    is_project = len(py_files) > 1 or has_setup

    if not is_project:
        return warnings

    # Check for git repo with correct branch
    git_dir = os.path.join(workspace, ".git")
    if os.path.isdir(git_dir):
        head_path = os.path.join(git_dir, "HEAD")
        try:
            with open(head_path, "r") as f:
                head_content = f.read().strip()
            if "refs/heads/master" in head_content:
                warnings.append(
                    "Git repo uses 'master' as default branch; "
                    "best practice is 'main' (use git init -b main)"
                )
        except OSError:
            pass
    else:
        warnings.append(
            "Project has multiple files but no Git repository; "
            "initialize with git init -b main"
        )

    # Check for .gitignore
    if os.path.isdir(git_dir) and not os.path.isfile(
        os.path.join(workspace, ".gitignore")
    ):
        warnings.append("Git repository exists but no .gitignore file found")

    # Check for README
    has_readme = any(
        e.lower().startswith("readme") for e in entries
    )
    if not has_readme:
        warnings.append("Project has no README file")

    # Check for requirements.txt or pyproject.toml
    has_deps = any(
        e in entries
        for e in ("requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock")
    )
    if not has_deps:
        warnings.append(
            "Project has no dependency file "
            "(requirements.txt or pyproject.toml)"
        )

    return warnings


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
    return error[:MAX_ERROR_LENGTH or None]


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

    # Project-level best-practice checks
    bp_warnings = check_project_guardrails(workspace)
    if bp_warnings:
        lines.append("## Best Practice Warnings\n\n")
        for w in bp_warnings:
            lines.append(f"- {w}\n")
        lines.append("\n")

    state_dir = os.path.join(workspace, ".state")
    try:
        os.makedirs(state_dir, exist_ok=True)
        validation_path = os.path.join(state_dir, "validation.md")
        with open(validation_path, "w") as f:
            f.writelines(lines)
        logger.info("Validation report written to %s", validation_path)
    except OSError as e:
        logger.warning("Could not write validation.md: %s", e)

    validation_data = {
        "missing_files": missing_files,
        "workspace_empty": len(ws_entries) == 0,
        "best_practice_warnings": bp_warnings,
    }
    # Store validation data in state for programmatic access
    state["validation"] = validation_data
    return validation_data


def _finalize_code_tracking():
    """Load code versions from disk and record provenance links."""
    tracker = get_code_tracker()
    cv_dir = os.path.join(WORKSPACE, ".state", "code_versions")
    if os.path.isdir(cv_dir):
        tracker.load_from_dir(cv_dir)
    prov = get_provenance_graph()
    for step_id, versions in tracker.get_all_versions().items():
        prev_entity_id = None
        for i, ver in enumerate(versions):
            entity_id = prov.add_entity(
                f"code_step{step_id}_v{i}",
                content=ver.code[:500],
            )
            if prev_entity_id:
                prov.was_derived_from(entity_id, prev_entity_id)
            prev_entity_id = entity_id


def execute_step(step: dict, state: dict, completed_outputs: dict,
                 progress_counts: dict | None = None,
                 dashboard: Dashboard | None = None,
                 backtracked_steps: set | None = None) -> bool:
    """Execute a single step, with spec rewrite retries.

    Returns True on success, False on unrecoverable failure.

    Args:
        backtracked_steps: Set of step IDs already backtracked to (Section 3d).
            Used to limit backtracking depth to 1 and avoid re-backtracking.
    """
    total = len(state["steps"])
    _probe_environment()
    context = build_context(step, completed_outputs, state=state,
                            workspace_path=WORKSPACE)
    counts = progress_counts or {"completed": 0, "failed": 0}
    step_start = time.monotonic()
    if backtracked_steps is None:
        backtracked_steps = set()

    # Build step context for dynamic CLAUDE.md (Section 1d)
    completed_steps_info = [
        {
            "id": s["id"],
            "title": s["title"],
            "summary": s.get("summary", ""),
            "files": s.get("files_written", []),
        }
        for s in state["steps"] if s["status"] == "completed"
    ]
    step_context = {
        "step_number": step["id"],
        "total_steps": total,
        "step_title": step["title"],
        "dependencies": step["depends_on"],
        "prior_steps": completed_steps_info,
    }

    event_log = get_event_log()
    prov = get_provenance_graph()
    event_log.emit(EventType.STEP_START, step_id=step["id"],
                   data={"title": step["title"]})
    prev_error_entity = None
    attempt_history = []  # Track prior attempts for reflection (Section 1c)

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
        if dashboard:
            dashboard.set_step_activity(step["id"], "Generating spec...")
        spec_file = generate_spec(step, total, context)
        logger.info("  Spec written: %s", spec_file)

        # Build task for Orchestrator
        task = build_task_from_spec(step, context)
        logger.info("  Sending to Orchestrator...")
        if dashboard:
            dashboard.set_step_activity(step["id"], "Running orchestrator...")

        # Execute
        step["status"] = "executing"
        _save_state_threadsafe(state)
        if dashboard:
            dashboard.update(state)

        event_log.emit(EventType.LLM_CALL_START, step_id=step["id"],
                       attempt=spec_attempt + 1)
        orch_start = time.monotonic()
        extra_env = {
            "UAS_STEP_ID": str(step["id"]),
            "UAS_SPEC_ATTEMPT": str(spec_attempt),
        }
        # Scan workspace files for orchestrator prompt context (Section 1a)
        ws_files = scan_workspace_files(WORKSPACE)
        if ws_files:
            ws_listing = "\n".join(
                f"  {fname} ({info['size']} bytes, {info['type']})"
                for fname, info in sorted(ws_files.items())
            )
            extra_env["UAS_WORKSPACE_FILES"] = ws_listing
        output_cb = None
        if dashboard and dashboard.use_rich:
            output_cb = lambda line: dashboard.add_output_line(line)
        result = run_orchestrator(task, extra_env=extra_env,
                                  output_callback=output_cb,
                                  step_context=step_context)
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
        if dashboard:
            status = "succeeded" if result["exit_code"] == 0 else "failed"
            dashboard.log(
                f"Step {step['id']} orchestrator {status} "
                f"(exit {result['exit_code']}, {orch_elapsed:.1f}s)"
            )

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
                if dashboard:
                    dashboard.set_step_activity(step["id"], "Verifying output...")
                event_log.emit(EventType.VERIFICATION_START,
                               step_id=step["id"])
                failure_reason = verify_step_output(step, WORKSPACE)
                event_log.emit(
                    EventType.VERIFICATION_COMPLETE,
                    step_id=step["id"],
                    data={"passed": failure_reason is None},
                )

            # Guardrail scan on workspace Python files
            if failure_reason is None:
                guardrail_warnings = []
                try:
                    for entry in os.listdir(WORKSPACE):
                        if entry.endswith(".py") and not entry.startswith("."):
                            fpath = os.path.join(WORKSPACE, entry)
                            if os.path.isfile(fpath):
                                with open(fpath, "r", errors="replace") as gf:
                                    code_content = gf.read()
                                violations = check_guardrails(code_content)
                                for v in violations:
                                    if v["severity"] == "error":
                                        failure_reason = (
                                            f"Guardrail violation in {entry} "
                                            f"line {v['line']}: {v['description']}"
                                        )
                                        break
                                    guardrail_warnings.append(
                                        f"{entry}:{v['line']}: {v['description']}"
                                    )
                            if failure_reason:
                                break
                except OSError:
                    pass
                if guardrail_warnings:
                    for w in guardrail_warnings:
                        logger.warning("  Guardrail: %s", w)
                    step.setdefault("guardrail_warnings", []).extend(
                        guardrail_warnings
                    )

            if failure_reason is None:
                # All validation passed
                step["status"] = "completed"
                step["error"] = ""
                step["elapsed"] = time.monotonic() - step_start
                _save_state_threadsafe(state)
                if dashboard:
                    dashboard.update(state)
                if dashboard:
                    dashboard.set_step_activity(step["id"], "")
                    summary = step.get("summary", "")
                    files = step.get("files_written", [])
                    parts = []
                    if summary:
                        parts.append(summary[:80])
                    if files:
                        parts.append(f"Files: {', '.join(files[:3])}")
                    if parts:
                        dashboard.log(f"Step {step['id']} done: {'; '.join(parts)}")
                    else:
                        dashboard.log(f"Step {step['id']} completed successfully")
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
                    logger.info("  Output: %s", step["output"])
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
                # Section 4a: Update structured progress file
                update_progress_file(
                    state,
                    event=f"Step {step['id']} ({step['title']}) completed successfully",
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
        logger.error("  Error: %s", error_info)

        # Scratchpad: record failure
        append_scratchpad(
            f"Step {step['id']} ({step['title']}) FAILED "
            f"(attempt {spec_attempt + 1}).\n"
            f"Error: {error_info[:500]}"
        )
        # Section 4a: Update structured progress file
        update_progress_file(
            state,
            event=f"Step {step['id']} ({step['title']}) failed (attempt {spec_attempt + 1})",
        )

        # Section 3b: Classify error for adaptive retry budgets
        error_type = classify_failure(error_info)
        logger.info("  Error type: %s", error_type)

        # Section 3a: Generate structured reflection
        try:
            reflection = generate_reflection(
                step,
                result.get("stdout", "") or "",
                result.get("stderr", "") or error_info,
                attempt=spec_attempt + 1,
            )
        except Exception as e:
            logger.warning("  Reflection generation failed: %s", e)
            reflection = {
                "attempt": spec_attempt + 1,
                "error_type": error_type,
                "root_cause": error_info[:200],
                "strategy_tried": f"attempt {spec_attempt + 1}",
                "lesson": "",
                "what_to_try_next": "",
            }

        # Store reflection in step state
        step.setdefault("reflections", []).append(reflection)
        _save_state_threadsafe(state)

        event_log.emit(EventType.REFLECTION_GENERATED,
                       step_id=step["id"],
                       attempt=spec_attempt + 1,
                       data={"error_type": reflection["error_type"],
                             "root_cause": reflection["root_cause"][:100]})

        # Write reflection to scratchpad for cross-step learning
        append_scratchpad(
            f"Reflection for step {step['id']} (attempt {spec_attempt + 1}): "
            f"error_type={reflection['error_type']}, "
            f"root_cause={reflection['root_cause'][:150]}, "
            f"lesson={reflection.get('lesson', '')[:150]}"
        )

        # Track attempt history for reflection (Section 1c)
        strategy = {
            0: "initial attempt",
            1: "alternative strategy",
            2: "decompose into sub-phases",
            3: "final defensive rewrite",
        }.get(spec_attempt, f"rewrite attempt {spec_attempt}")
        attempt_history.append({
            "attempt": spec_attempt + 1,
            "error": error_info[:300],
            "strategy": strategy,
        })

        # Section 3b: Check error-type-adaptive retry budget
        error_budget = _ERROR_RETRY_BUDGETS.get(error_type, MAX_SPEC_REWRITES)
        attempts_so_far = spec_attempt + 1  # 1-indexed count of attempts done
        if attempts_so_far > error_budget and spec_attempt < MAX_SPEC_REWRITES:
            logger.info(
                "  Error type '%s' exceeded retry budget (%d/%d). "
                "Escalating early.",
                error_type, attempts_so_far, error_budget,
            )
            # For timeout errors, try decomposing once before giving up
            if error_type == "timeout" and spec_attempt == 0:
                logger.info("  Timeout: decomposing step into sub-phases...")
                step["description"] = decompose_failing_step(
                    step, result.get("stdout", ""), result.get("stderr", "")
                )
                step["rewrites"] = spec_attempt + 1
                _save_state_threadsafe(state)
                continue
            break

        if spec_attempt < MAX_SPEC_REWRITES:
            # Section 3c: Counterfactual root cause tracing
            did_backtrack = False
            if step["depends_on"]:
                event_log.emit(EventType.ROOT_CAUSE_TRACED,
                               step_id=step["id"],
                               data={"checking": True})
                root_target, dep_id = trace_root_cause(
                    step, error_info, completed_outputs, state,
                )
                event_log.emit(EventType.ROOT_CAUSE_TRACED,
                               step_id=step["id"],
                               data={"target": root_target,
                                     "dep_id": dep_id})

                # Section 3d: Backtracking
                if (root_target == "dependency"
                        and dep_id is not None
                        and dep_id not in backtracked_steps):
                    step_by_id = {s["id"]: s for s in state["steps"]}
                    dep_step = step_by_id.get(dep_id)
                    if dep_step:
                        logger.info(
                            "  Root cause in dependency step %d. "
                            "Backtracking to re-execute...",
                            dep_id,
                        )
                        backtracked_steps.add(dep_id)
                        event_log.emit(EventType.BACKTRACK_START,
                                       step_id=step["id"],
                                       data={"backtrack_to": dep_id})
                        if dashboard:
                            dashboard.set_step_activity(
                                step["id"],
                                f"Backtracking to step {dep_id}...",
                            )
                            dashboard.log(
                                f"Step {step['id']}: root cause in "
                                f"step {dep_id}, backtracking"
                            )

                        # Reset dependency step for re-execution
                        dep_step["status"] = "pending"
                        dep_step["error"] = ""
                        _save_state_threadsafe(state)
                        if dashboard:
                            dashboard.update(state)

                        dep_success = execute_step(
                            dep_step, state, completed_outputs,
                            progress_counts, dashboard,
                            backtracked_steps,
                        )
                        event_log.emit(
                            EventType.BACKTRACK_COMPLETE,
                            step_id=step["id"],
                            data={"backtrack_to": dep_id,
                                  "success": dep_success},
                        )

                        if dep_success:
                            # Update completed outputs from re-executed dep
                            completed_outputs[dep_id] = {
                                "stdout": dep_step.get("output", ""),
                                "stderr": dep_step.get(
                                    "stderr_output", ""),
                                "files": dep_step.get(
                                    "files_written", []),
                            }
                            # Rebuild context with updated dep output
                            context = build_context(
                                step, completed_outputs,
                                state=state,
                                workspace_path=WORKSPACE,
                            )
                            did_backtrack = True
                            logger.info(
                                "  Backtrack to step %d succeeded. "
                                "Retrying current step...",
                                dep_id,
                            )
                        else:
                            logger.warning(
                                "  Backtrack to step %d also failed.",
                                dep_id,
                            )

            if did_backtrack:
                # Retry current step with updated context (no rewrite)
                step["rewrites"] = spec_attempt + 1
                _save_state_threadsafe(state)
                continue

            # Standard rewrite path
            event_log.emit(EventType.REWRITE_START, step_id=step["id"],
                           attempt=spec_attempt + 1)
            logger.info(
                "  Rewriting spec (rewrite %d/%d)...",
                spec_attempt + 1,
                MAX_SPEC_REWRITES,
            )
            if dashboard:
                rewrite_label = {
                    0: "Reflecting on failure...",
                    1: "Trying alternative strategy...",
                    2: "Decomposing into sub-phases...",
                    3: "Final defensive rewrite...",
                }.get(spec_attempt, "Rewriting...")
                dashboard.set_step_activity(step["id"], rewrite_label)
                dashboard.log(
                    f"Step {step['id']} failed (attempt {spec_attempt + 1}), "
                    f"rewriting ({spec_attempt + 1}/{MAX_SPEC_REWRITES})"
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
                    previous_attempts=attempt_history,
                    reflections=step.get("reflections", []),
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

    output_flag = args.output or os.environ.get("UAS_OUTPUT") or None
    if output_flag:
        state_dir = os.path.join(WORKSPACE, ".state")
        output_path = (
            os.path.join(state_dir, "output.json")
            if output_flag == "auto"
            else output_flag
        )
    else:
        output_path = None

    # Report flag
    report_flag = args.report or os.environ.get("UAS_REPORT") or None

    # Trace flag
    trace_flag = args.trace or os.environ.get("UAS_TRACE") or None

    # Explain flag
    explain_flag = args.explain or os.environ.get("UAS_EXPLAIN", "").lower() in (
        "1", "true", "yes",
    )

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
    reset_code_tracker()
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

        logger.info("Goal: %s\n", goal)
        event_log.emit(EventType.GOAL_RECEIVED, data={"goal": goal})
        goal_entity = prov.add_entity("goal", content=goal)
        planner_agent = prov.add_agent("planner_llm")

        # Phase 1: Decompose (with multi-plan voting for complex goals)
        logger.info("Phase 1: Decomposing goal into atomic steps...")
        event_log.emit(EventType.DECOMPOSITION_START)
        decompose_start = time.monotonic()
        state = init_state(goal)
        try:
            steps = decompose_goal_with_voting(goal)
        except Exception as e:
            logger.error("Failed to decompose goal: %s", e)
            state["status"] = "failed"
            save_state(state)
            sys.exit(1)
        decompose_elapsed = time.monotonic() - decompose_start

        # Store estimated complexity in state
        complexity = getattr(decompose_goal_with_voting, "last_complexity", None)
        if isinstance(complexity, str):
            state["complexity"] = complexity

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
    dashboard.log(f"Starting execution: {len(state['steps'])} steps, "
                  f"{len(levels)} levels")
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
                _finalize_code_tracking()
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
            workers = min(len(pending), MAX_PARALLEL) if MAX_PARALLEL else len(pending)
            logger.info("  Running %d independent steps in parallel (max %d workers): %s",
                        len(pending), workers, [s["id"] for s in pending])
            dashboard.log(
                f"Running {len(pending)} steps in parallel: "
                + ", ".join(f"#{s['id']}" for s in pending)
            )
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
                _finalize_code_tracking()
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
    for bp_warn in validation.get("best_practice_warnings", []):
        logger.warning("  Best practice: %s", bp_warn)

    if output_path:
        write_json_output(state, output_path)

    event_log.emit(EventType.RUN_COMPLETE, data={
        "status": state["status"],
        "total_elapsed": state.get("total_elapsed", 0.0),
    })
    _finalize_code_tracking()
    prov.save()

    # Build shared data for report and explanation
    events_data = [e.to_dict() for e in event_log.events]
    prov_data = prov.to_dict()
    tracker = get_code_tracker()
    code_versions = {
        sid: [v.to_dict() for v in versions]
        for sid, versions in tracker.get_all_versions().items()
    }

    # Build explanation if needed (for --explain or --report)
    explanation_text = None
    if explain_flag or report_flag:
        try:
            explainer = RunExplainer(state, events_data, prov_data, code_versions)
            explanation_text = explainer.explain_run()
        except Exception as e:
            logger.warning("Failed to generate explanation: %s", e)

    # Print explanation to stderr if requested
    if explain_flag and explanation_text:
        print("\n" + explanation_text, file=sys.stderr)

    # Generate HTML report if requested
    if report_flag:
        state_dir = os.path.join(WORKSPACE, ".state")
        report_path = (
            os.path.join(state_dir, "report.html")
            if report_flag == "auto"
            else report_flag
        )
        try:
            generate_report(state, events_data, prov_data, report_path,
                            code_versions=code_versions,
                            explanation=explanation_text)
            logger.info("HTML report written to: %s", report_path)
        except Exception as e:
            logger.warning("Failed to generate HTML report: %s", e)

    # Export Perfetto trace if requested
    if trace_flag:
        state_dir = os.path.join(WORKSPACE, ".state")
        trace_path = (
            os.path.join(state_dir, "trace.json")
            if trace_flag == "auto"
            else trace_flag
        )
        try:
            events_data = [e.to_dict() for e in event_log.events]
            exporter = TraceExporter(events_data)
            exporter.export_json(trace_path)
            logger.info("Perfetto trace written to: %s", trace_path)
        except Exception as e:
            logger.warning("Failed to export Perfetto trace: %s", e)

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
