"""Architect Agent: autonomous planner and spec generator.

Takes an abstract human goal, decomposes it into atomic steps,
generates UAS-compliant specs, and drives the Orchestrator to execute them.
"""

import argparse
import ast
import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from .state import (
    init_state, save_state, load_state, add_steps,
    append_scratchpad, read_scratchpad,
    update_progress_file, read_progress_file,
    get_run_dir, get_specs_dir, _write_latest_run,
    append_knowledge, prune_old_runs,
)
from .planner import (
    decompose_goal,
    decompose_goal_with_voting,
    generate_project_spec,
    estimate_complexity,
    research_goal,
    reflect_and_rewrite,
    decompose_failing_step,
    generate_reflection,
    trace_root_cause,
    topological_sort,
    critique_and_refine_plan,
    merge_trivial_steps,
    merge_steps_with_llm,
    replan_remaining_steps,
    enrich_step_descriptions,
    ensure_coverage,
    verify_coverage,
    fill_coverage_gaps,
    split_coupled_steps,
    insert_integration_checkpoints,
    generate_corrective_steps,
    enforce_minimum_steps,
    MAX_CORRECTIVE_STEPS_PER_ROUND,
    MAX_CORRECTION_ROUNDS,
)
from .spec_generator import generate_spec, build_task_from_spec
from .executor import (
    run_orchestrator,
    extract_sandbox_stdout,
    extract_sandbox_stderr,
    extract_file_signatures,
    extract_workspace_files,
    parse_uas_result,
    scan_workspace_files,
    format_workspace_scan,
    MAX_CONTEXT_LENGTH,
)
from .git_state import rollback_to_checkpoint
from .events import EventType, get_event_log, reset_event_log
from .provenance import get_provenance_graph, reset_provenance_graph
from .code_tracker import get_code_tracker, reset_code_tracker
from .dashboard import Dashboard
from .report import generate_report
from .trace_export import TraceExporter
from .explain import RunExplainer, classify_failure, classify_failure_heuristic

import config
from hooks import HookEvent, load_hooks, run_hook

from orchestrator.llm_client import (
    estimate_cost, classify_error,
    PERSISTENT_RETRY, MAX_BACKOFF, PERSISTENT_RETRY_RESET,
    _sleep_with_heartbeat,
)

MAX_SPEC_REWRITES = 4
MAX_CONSECUTIVE_FAILURES = 3
MAX_PARALLEL = config.get("max_parallel")
WORKSPACE = config.get("workspace")
PROJECT_DIR = WORKSPACE
MINIMAL_MODE = config.get("minimal")

MAX_ERROR_LENGTH = config.get("max_error_length")

# Rate limit detection and backoff configuration.
_RATE_LIMIT_RESET_RE = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(?:am|pm)?\s*\(?utc\)?", re.IGNORECASE)
RATE_LIMIT_BASE_WAIT = config.get("rate_limit_wait")
RATE_LIMIT_MAX_WAIT = config.get("rate_limit_max_wait")
MAX_RATE_LIMIT_RETRIES = config.get("rate_limit_retries")

# Usage-limit patterns (account-level quota exhaustion, needs long waits).
_USAGE_LIMIT_PATTERNS = [
    "out of extra usage", "out of usage", "usage limit",
    "exceeded your limit", "plan limit",
]
USAGE_LIMIT_WAIT = config.get("usage_limit_wait")
MAX_USAGE_LIMIT_RETRIES = config.get("usage_limit_retries")


def _is_usage_limited(error_text: str) -> bool:
    """Return True if the error indicates account-level usage exhaustion."""
    lower = error_text.lower()
    return any(pat in lower for pat in _USAGE_LIMIT_PATTERNS)


# ---------------------------------------------------------------------------
# Section 7 (PLAN.md): File-conflict detection for parallel step safety
# ---------------------------------------------------------------------------

def _outputs_overlap(a: str, b: str) -> bool:
    """Return True if two output path patterns overlap.

    Handles exact matches and fnmatch-style globs (e.g. ``*.json``
    matching ``config.json``).
    """
    import fnmatch
    if a == b:
        return True
    # Check if either pattern matches the other as a glob
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    return False


def find_file_conflicts(steps: list[dict]) -> list[tuple[int, int]]:
    """Return pairs of step IDs whose declared outputs overlap.

    Only considers the ``outputs`` field on each step dict. Steps with
    empty or missing ``outputs`` never conflict.
    """
    conflicts: list[tuple[int, int]] = []
    for i, sa in enumerate(steps):
        outs_a = sa.get("outputs", [])
        if not outs_a:
            continue
        for j in range(i + 1, len(steps)):
            sb = steps[j]
            outs_b = sb.get("outputs", [])
            if not outs_b:
                continue
            for oa in outs_a:
                for ob in outs_b:
                    if _outputs_overlap(oa, ob):
                        conflicts.append((sa["id"], sb["id"]))
                        break
                else:
                    continue
                break  # already recorded this pair
    return conflicts


def _partition_by_conflicts(
    steps: list[dict],
    conflicts: list[tuple[int, int]],
) -> list[list[dict]]:
    """Split *steps* into sequential batches with no intra-batch conflicts.

    Steps within the same batch can run in parallel.  Batches must be
    executed sequentially.  Uses a greedy first-fit algorithm.
    """
    if not conflicts:
        return [steps]

    conflict_set: set[tuple[int, int]] = set()
    for a, b in conflicts:
        conflict_set.add((a, b))
        conflict_set.add((b, a))

    batches: list[list[dict]] = []
    for step in steps:
        placed = False
        for batch in batches:
            if not any((step["id"], s["id"]) in conflict_set for s in batch):
                batch.append(step)
                placed = True
                break
        if not placed:
            batches.append([step])
    return batches


# Section 8: Regex to extract package versions from pip/uv install output.
# Matches lines like "Successfully installed requests-2.31.0 pandas-2.1.4"
# and uv output like "Installed 3 packages in 120ms" followed by
# " + requests==2.31.0" lines.
_PIP_INSTALLED_RE = re.compile(
    r"Successfully installed\s+(.*)", re.IGNORECASE
)
_PKG_VERSION_RE = re.compile(r"(\S+?)-(\d[\w.]*)")
_UV_INSTALLED_RE = re.compile(r"^\s*\+\s*(\S+)==(\d[\w.]*)", re.MULTILINE)


def _extract_installed_packages(output: str) -> dict[str, str]:
    """Extract package->version mappings from pip or uv install stdout."""
    packages: dict[str, str] = {}
    for match in _PIP_INSTALLED_RE.finditer(output):
        for pkg_match in _PKG_VERSION_RE.finditer(match.group(1)):
            packages[pkg_match.group(1)] = pkg_match.group(2)
    for match in _UV_INSTALLED_RE.finditer(output):
        packages[match.group(1)] = match.group(2)
    return packages

RETRY_DECISION_PROMPT = """\
You are deciding whether to retry a failing step in an automated code generation pipeline.

<step_description>
{step_description}
</step_description>

<current_error>
Type: {error_type}
</current_error>

<attempt_info>
Current attempt: {attempt} of {max_attempts} maximum
</attempt_info>

<reflection_history>
{reflections_text}
</reflection_history>

Consider:
- Are the reflections showing progress toward a fix, or repeating the same ideas?
- Is this error type likely fixable with more retries?
- Has a genuinely different approach been suggested?

Return ONLY valid JSON: {{"continue": true, "reason": "..."}} or {{"continue": false, "reason": "..."}}
"""

EMERGENCY_COMPRESS_PROMPT = """\
You are compressing context for an automated code generation pipeline that has hit its context size limit.
Extract ONLY the information essential for the next step.

<next_step>
{next_step}
</next_step>

<target_length>
{target_length} characters maximum
</target_length>

<context_start>
{context_start}
</context_start>

<context_end>
{context_end}
</context_end>

Produce a compressed summary under {target_length} characters that preserves:
- The original goal
- File paths created or modified
- Error messages and their resolutions
- Key results needed by the next step
- Current plan state and progress

Omit verbose stdout/stderr, raw data, and redundant information.
Output ONLY the compressed summary text, nothing else.
"""


_GITIGNORE_CONTENT = """\
# Python
__pycache__/
*.py[cod]
*.so
.env
.venv/
venv/
dist/
*.egg-info/
.mypy_cache/
.pytest_cache/

# Node
node_modules/

# Data
data/

# UAS (auth contains credentials; state and goals are committed)
.uas_auth/
.claude/
"""


def ensure_git_repo(workspace: str) -> None:
    """Initialize a git repo in the workspace if it doesn't exist yet.

    After initialization, tags the initial commit as ``uas-main`` to mark
    the immutable pre-execution filesystem state, then creates and checks
    out a ``uas-wip`` branch so that per-step checkpoint commits stay off
    ``main``.  The wip branch is squash-merged back into ``main`` by
    :func:`finalize_git` at the end of a successful run.

    Initializes when the workspace has at least one non-dot entry, or
    when any subdirectory contains ``.py`` files.  Logs a warning if
    initialization fails.
    """
    try:
        git_dir = os.path.join(workspace, ".git")
        if os.path.isdir(git_dir):
            return

        # Init if workspace has any non-dot entry
        entries = [
            e for e in os.listdir(workspace)
            if not e.startswith(".")
        ]
        if len(entries) < 1:
            # Check subdirectories for .py files as a fallback
            has_py = False
            for root, dirs, files in os.walk(workspace):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                if any(f.endswith(".py") for f in files):
                    has_py = True
                    break
            if not has_py:
                return

        # Write .gitignore
        gitignore_path = os.path.join(workspace, ".gitignore")
        if not os.path.exists(gitignore_path):
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(_GITIGNORE_CONTENT)

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial workspace state"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        # Tag the initial commit as the immutable baseline for the run
        subprocess.run(
            ["git", "tag", "-f", "uas-main"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        # Create a wip branch for checkpoint commits
        subprocess.run(
            ["git", "checkout", "-b", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        logger.debug("Git repo initialized in %s (on uas-wip branch)", workspace)
    except Exception:
        logger.warning("Git init failed in %s", workspace, exc_info=True)


def _ensure_wip_branch(workspace: str) -> bool:
    """Ensure the current branch is ``uas-wip``, creating it if needed.

    Returns True if we are on the wip branch, False on any failure.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        current = result.stdout.strip()
        if current == "uas-wip":
            return True

        # Check if uas-wip already exists
        result = subprocess.run(
            ["git", "branch", "--list", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        if result.stdout.strip():
            subprocess.run(
                ["git", "checkout", "uas-wip"],
                cwd=workspace,
                capture_output=True,
                check=True,
            )
        else:
            subprocess.run(
                ["git", "checkout", "-b", "uas-wip"],
                cwd=workspace,
                capture_output=True,
                check=True,
            )
        return True
    except Exception:
        return False


def git_checkpoint(workspace: str, step_id: int, step_title: str) -> None:
    """Commit current workspace state as a checkpoint on the ``uas-wip`` branch.

    Checkpoint commits are kept off ``main`` so they can be squashed into
    a single commit by :func:`finalize_git` at the end of a successful run.

    Silently skips if the workspace is not a git repo, there are no changes,
    or any git operation fails.
    """
    try:
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(git_dir):
            return

        # Ensure we're on the wip branch
        _ensure_wip_branch(workspace)

        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        # Check if there are staged changes
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
        )
        if diff.returncode == 0:
            # No changes to commit
            return

        msg = f"Step {step_id}: {step_title}"
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        logger.debug("Git checkpoint (uas-wip): %s", msg)
    except Exception:
        logger.debug(
            "Git checkpoint skipped/failed for step %s", step_id,
            exc_info=True,
        )


def _ensure_gitignore_data_patterns(workspace: str) -> None:
    """Ensure ``.gitignore`` covers common data file patterns."""
    gitignore_path = os.path.join(workspace, ".gitignore")
    required_patterns = [
        "*.csv", "*.pkl", "*.parquet", "*.joblib", "*.npz",
        "*.h5", "*.hdf5", "*.feather", "*.arrow",
        "*.sqlite", "*.db",
        "models/",
    ]

    existing = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            existing = f.read()

    missing = [p for p in required_patterns if p not in existing]
    if not missing:
        return

    with open(gitignore_path, "a", encoding="utf-8") as f:
        f.write("\n# Data artifacts\n")
        for pattern in missing:
            f.write(f"{pattern}\n")


def _commit_all_on_main(workspace: str, msg: str) -> None:
    """Stage all files and commit on the current branch."""
    subprocess.run(
        ["git", "add", "-A"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=workspace,
        capture_output=True,
    )
    if diff.returncode == 0:
        return  # Nothing to commit
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    logger.debug("Committed on main: %s", msg)


_COMMIT_MSG_PROMPT = """\
Write a git commit message for the following completed project.

Goal: {goal}

Rules (strict):
- Subject line: imperative mood, ≤50 characters, no trailing period
- Blank line after subject
- Body: wrap each line at 72 characters, describe what was built
- Do NOT use markdown, bullet points, or formatting
- Return ONLY the commit message text, nothing else

Example:
Add user authentication module

Implement JWT-based auth with login, logout, and token
refresh endpoints. Include rate limiting and input
validation for all auth routes."""


def _build_commit_message(goal: str) -> str:
    """Generate a best-practice git commit message from the goal.

    Tries the LLM first for a well-crafted message, falls back to a
    mechanical derivation from the goal text.
    """
    try:
        from orchestrator.llm_client import get_llm_client
        client = get_llm_client(role="planner")
        prompt = _COMMIT_MSG_PROMPT.format(goal=goal)
        raw, _usage = client.generate(prompt)
        raw = raw.strip()
        # Strip markdown fences if the LLM wrapped it
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            raw = "\n".join(lines).strip()
        # Validate: subject must be ≤50 chars
        subject = raw.split("\n", 1)[0]
        if len(subject) <= 50:
            return raw
        # Subject too long — truncate it, keep the body
        parts = raw.split("\n", 1)
        subject = parts[0][:47] + "..."
        return subject + ("\n" + parts[1] if len(parts) > 1 else "")
    except Exception:
        pass

    # Mechanical fallback: derive from goal text
    subject = goal.strip().split("\n", 1)[0]
    if len(subject) > 50:
        subject = subject[:47] + "..."
    return subject


def finalize_git(workspace: str, goal: str) -> None:
    """Squash all ``uas-wip`` checkpoint commits into a single commit on ``main``.

    Called at the end of a successful run.  Produces a clean single-commit
    history on ``main`` with a message derived from *goal*.  If the squash
    merge fails, falls back to a regular commit of all changes on ``main``.
    When no ``uas-wip`` branch exists, commits any uncommitted changes
    directly on ``main``.
    """
    try:
        git_dir = os.path.join(workspace, ".git")
        if not os.path.isdir(git_dir):
            return

        # Build a best-practice commit message
        msg = _build_commit_message(goal)

        # Check if uas-wip branch exists
        result = subprocess.run(
            ["git", "branch", "--list", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        if not result.stdout.strip():
            # No wip branch -- commit any uncommitted changes on main
            _ensure_gitignore_data_patterns(workspace)
            _commit_all_on_main(workspace, msg)
            return

        # Ensure .gitignore covers data artifacts and commit on current branch
        _ensure_gitignore_data_patterns(workspace)
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace,
            capture_output=True,
        )
        pre_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
        )
        if pre_diff.returncode != 0:
            subprocess.run(
                ["git", "commit", "-m", "Update .gitignore for data artifacts"],
                cwd=workspace,
                capture_output=True,
            )

        # Switch to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        # Squash merge uas-wip into main
        merge_result = subprocess.run(
            ["git", "merge", "--squash", "uas-wip"],
            cwd=workspace,
            capture_output=True,
        )
        if merge_result.returncode != 0:
            logger.warning(
                "Git squash merge failed in %s: %s",
                workspace,
                merge_result.stderr.decode("utf-8", errors="replace")
                if merge_result.stderr else "unknown error",
            )
            # Abort the failed merge and fall back to regular commit
            subprocess.run(
                ["git", "reset", "--merge"],
                cwd=workspace,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", "uas-wip", "--", "."],
                cwd=workspace,
                capture_output=True,
            )
            _commit_all_on_main(workspace, msg)
            subprocess.run(
                ["git", "branch", "-D", "uas-wip"],
                cwd=workspace,
                capture_output=True,
            )
            return

        # Check if there are staged changes to commit
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
        )
        if diff.returncode == 0:
            # No changes (wip had no new commits beyond main)
            return

        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=workspace,
            capture_output=True,
            check=True,
        )

        # Clean up the wip branch
        subprocess.run(
            ["git", "branch", "-D", "uas-wip"],
            cwd=workspace,
            capture_output=True,
            check=True,
        )
        logger.debug("Git finalized: squashed wip commits into main")

        # Verify repository is clean
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if porcelain.stdout.strip():
            logger.warning(
                "Git repo still dirty after finalize:\n%s",
                porcelain.stdout[:500],
            )
    except Exception:
        logger.warning(
            "Git finalize failed in %s", workspace,
            exc_info=True,
        )


def _text_similarity(a: str, b: str) -> float:
    """Compute text similarity ratio between two strings (0.0 to 1.0)."""
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _should_continue_retrying_heuristic(step, spec_attempt, error_type, reflections):
    """Heuristic fallback for retry decisions using error budgets and text similarity."""
    _ERROR_RETRY_BUDGETS = {
        "dependency_error": MAX_SPEC_REWRITES,
        "logic_error": MAX_SPEC_REWRITES,
        "environment_error": 2,
        "network_error": 2,
        "timeout": 0,
        "format_error": 2,
        "unknown": MAX_SPEC_REWRITES,
    }

    attempts_so_far = spec_attempt + 1
    error_budget = _ERROR_RETRY_BUDGETS.get(error_type, MAX_SPEC_REWRITES)

    if len(reflections) >= 2:
        last = reflections[-1]
        prev = reflections[-2]
        same_type = last.get("error_type") == prev.get("error_type")
        similar_cause = _text_similarity(
            last.get("root_cause", ""), prev.get("root_cause", "")
        ) > 0.6
        if same_type and similar_cause:
            return False, (
                f"stagnation detected: same error_type '{last.get('error_type')}' "
                f"and similar root_cause across last 2 attempts"
            )

    if attempts_so_far <= error_budget:
        return True, f"within retry budget ({attempts_so_far}/{error_budget})"

    if len(reflections) >= 2:
        last_suggestion = reflections[-1].get("what_to_try_next", "").strip()
        if last_suggestion:
            prev_suggestions = [
                r.get("what_to_try_next", "") for r in reflections[:-1]
            ]
            all_different = not any(
                _text_similarity(last_suggestion, s) > 0.6
                for s in prev_suggestions
            )
            if all_different:
                return True, (
                    f"exceeded budget ({attempts_so_far}/{error_budget}) but "
                    f"reflection suggests novel approach"
                )

    return False, f"exceeded retry budget ({attempts_so_far}/{error_budget})"


def should_continue_retrying(step, spec_attempt, error_type, reflections):
    """Decide whether to continue retrying using LLM analysis with heuristic fallback.

    Returns (should_continue: bool, reason: str).
    """
    if spec_attempt >= MAX_SPEC_REWRITES:
        return False, f"reached max spec rewrites ({MAX_SPEC_REWRITES})"

    if not MINIMAL_MODE:
        try:
            from orchestrator.llm_client import get_llm_client

            reflections_text = ""
            for i, r in enumerate(reflections):
                reflections_text += (
                    f"Attempt {r.get('attempt', i + 1)}:\n"
                    f"  Error type: {r.get('error_type', 'unknown')}\n"
                    f"  Root cause: {r.get('root_cause', 'unknown')}\n"
                    f"  Next approach: {r.get('what_to_try_next', 'unknown')}\n\n"
                )
            if not reflections_text:
                reflections_text = "No previous reflections."

            prompt = RETRY_DECISION_PROMPT.format(
                step_description=step.get("description", ""),
                error_type=error_type,
                attempt=spec_attempt + 1,
                max_attempts=MAX_SPEC_REWRITES,
                reflections_text=reflections_text,
            )

            event_log = get_event_log()
            event_log.emit(EventType.LLM_CALL_START, data={"purpose": "retry_decision"})
            client = get_llm_client(role="planner")
            response, _usage = client.generate(prompt)
            event_log.emit(EventType.LLM_CALL_COMPLETE, data={"purpose": "retry_decision"})

            text = response.strip()
            fence_match = re.search(
                r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL,
            )
            if fence_match:
                text = fence_match.group(1)
            else:
                brace_match = re.search(r"\{.*\}", text, re.DOTALL)
                if brace_match:
                    text = brace_match.group(0)

            data = json.loads(text)
            should_cont = data.get("continue", True)
            reason = data.get("reason", "LLM decision")
            return bool(should_cont), reason
        except Exception:
            logger.debug("LLM retry decision failed, using heuristic fallback", exc_info=True)

    return _should_continue_retrying_heuristic(step, spec_attempt, error_type, reflections)


def _is_verification_stagnation(attempt_history: list[dict]) -> bool:
    """Detect repeated validation/verification failures suggesting upstream data issues.

    Returns True if the last 2+ attempts were validation failures (step code
    succeeded but verification/validation failed), indicating the step's code
    is fine but its input data is wrong.
    """
    if len(attempt_history) < 2:
        return False
    recent = attempt_history[-2:]
    return all(a.get("is_validation_failure", False) for a in recent)


logger = logging.getLogger(__name__)

_state_lock = threading.Lock()


def _save_state_threadsafe(state: dict):
    """Thread-safe wrapper around save_state for parallel execution."""
    with _state_lock:
        save_state(state)


def _accumulate_usage(state: dict, usage: dict, model: str | None = None,
                      step: dict | None = None):
    """Accumulate token usage and cost into run totals and optionally a step.

    Thread-safe: acquires ``_state_lock`` to update shared dicts.
    """
    if not usage:
        return
    inp = usage.get("input", 0)
    out = usage.get("output", 0)
    if inp == 0 and out == 0:
        return
    cost = estimate_cost(model or "claude-opus-4-6", usage)
    with _state_lock:
        totals = state.setdefault("total_tokens", {"input": 0, "output": 0})
        totals["input"] += inp
        totals["output"] += out
        state["total_cost_usd"] = state.get("total_cost_usd", 0.0) + cost
        if step is not None:
            su = step.setdefault("token_usage", {"input": 0, "output": 0})
            su["input"] += inp
            su["output"] += out
            step["cost_usd"] = step.get("cost_usd", 0.0) + cost


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
        help="Write a JSON results summary (default: .uas_state/runs/<run_id>/output.json)",
    )
    parser.add_argument(
        "--events", type=str, default=None, nargs="?", const="auto",
        help="Write event log to this path (default: .uas_state/runs/<run_id>/events.jsonl)",
    )
    parser.add_argument(
        "--report", type=str, default=None, nargs="?", const="auto",
        help="Generate HTML report at this path (default: .uas_state/runs/<run_id>/report.html)",
    )
    parser.add_argument(
        "--trace", type=str, default=None, nargs="?", const="auto",
        help="Export Perfetto trace to this path (default: .uas_state/runs/<run_id>/trace.json)",
    )
    parser.add_argument(
        "--explain", action="store_true", default=False,
        help="Print run explanation to stderr after completion",
    )
    parser.add_argument(
        "--goal-file", type=str, default=None,
        help="Read goal from a text file instead of command-line arguments",
    )
    return parser.parse_args()


def get_goal(args) -> str:
    if args.goal:
        return " ".join(args.goal)
    goal = config.get("goal")
    if goal:
        return goal
    goal_file = getattr(args, "goal_file", None) or config.get("goal_file")
    if goal_file:
        goal_file = os.path.expanduser(goal_file)
        if not os.path.isabs(goal_file):
            goal_file = os.path.join(WORKSPACE, goal_file)
        with open(goal_file, encoding="utf-8") as f:
            return f.read().strip()
    print("Enter your goal (submit with Ctrl+D):", file=sys.stderr)
    return sys.stdin.read().strip()


def _extract_json_keys(preview: str) -> str:
    """Extract nested key structure from a JSON preview string.

    Returns a compact representation of keys to 2 levels deep so the
    coder can see the actual schema (e.g. which sub-keys exist under
    each top-level key) rather than just the raw text beginning.
    """
    try:
        data = json.loads(preview)
    except (json.JSONDecodeError, ValueError):
        # Preview may be truncated — try adding closing braces to
        # recover at least the keys that were fully written.
        for suffix in ("}", "}}", "]}"):
            try:
                data = json.loads(preview.rsplit(",", 1)[0] + suffix)
                break
            except (json.JSONDecodeError, ValueError):
                continue
        else:
            return preview[:100]

    def _summarise(obj: object, depth: int = 0) -> str:
        """Recursively summarise JSON structure to *depth* 2."""
        if isinstance(obj, dict):
            if depth >= 2:
                return "{...}"
            parts = []
            for k, v in obj.items():
                parts.append(f"{k}: {_summarise(v, depth + 1)}")
            return "{" + ", ".join(parts) + "}"
        if isinstance(obj, list):
            if not obj:
                return "[]"
            return f"[{_summarise(obj[0], depth)}... ({len(obj)} items)]"
        if isinstance(obj, str):
            return "str"
        if isinstance(obj, (int, float)):
            return str(obj)
        if obj is None:
            return "null"
        return type(obj).__name__

    result = _summarise(data)
    # Cap length so the preview doesn't bloat the context.
    if len(result) > 1500:
        result = result[:1500] + "..."
    return result


def summarize_context(context: str, goal: str, max_length: int,
                      current_step_description: str = "") -> str:
    """Compress context using LLM when it exceeds the limit.

    Preserves: original goal, file paths, error messages, plan state.
    Falls back to simple truncation if LLM compression fails.

    If current_step_description is provided, the LLM prioritizes preserving
    information relevant to that step.
    """
    try:
        from orchestrator.llm_client import get_llm_client
        client = get_llm_client(role="planner")

        step_guidance = ""
        if current_step_description:
            step_guidance = (
                f"\nThe next step that will consume this context is: "
                f"{current_step_description}\n"
                "Prioritize preserving information relevant to that step.\n"
            )

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
            f"Goal: {goal}\n"
            f"{step_guidance}\n"
            f"Context to compress:\n{context}"
        )
        summary, _usage = client.generate(prompt)
        if len(summary) <= max_length:
            return summary
    except Exception:
        pass
    # Fallback: simple truncation
    return context[:max_length] + f"\n... [compressed, {len(context)} chars total]"


def _compress_context_regex(context: str, max_length: int) -> str:
    """Deterministic compression: remove previews, truncate stdout.

    Used as fallback when LLM summarization is unavailable.
    """
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
    return compressed


def compress_context(context: str, max_length: int,
                     goal: str = "",
                     progress_content: str = "",
                     current_step_description: str = "") -> str:
    """Tiered context compression (Section 4c / Section 5).

    Tier 1 (< 60% of limit): No compression, include everything.
    Tier 2+3 (>= 60%, < 100%): LLM-guided summarization with step-aware
        relevance filtering. Falls back to regex stripping on LLM failure.
    Tier 4 (>= 100%): Emergency truncation — progress file + tail of context.
    """
    if max_length <= 0:
        return context

    ratio = len(context) / max_length

    # Tier 1: No compression needed
    if ratio < 0.6:
        return context

    # Tier 2+3 (merged): LLM summarization with step-aware context,
    # falling back to deterministic regex stripping
    if ratio < 1.0:
        # Try LLM-guided summarization first
        try:
            from orchestrator.llm_client import get_llm_client
            client = get_llm_client(role="planner")

            step_guidance = ""
            if current_step_description:
                step_guidance = (
                    f"\nThe next step that will consume this context is: "
                    f"{current_step_description}\n"
                    "Prioritize preserving information relevant to that step.\n"
                )

            prompt = (
                f"Compress the following context to under {max_length} "
                "characters while preserving all essential information.\n\n"
                "MUST preserve:\n"
                "- Original goal and current plan state\n"
                "- All file paths touched\n"
                "- All error messages encountered\n"
                "- Key results and data summaries\n\n"
                "Remove: verbose stdout/stderr output, redundant information, "
                "raw data that can be referenced by file path.\n\n"
                f"Goal: {goal}\n"
                f"{step_guidance}\n"
                f"Context to compress:\n{context}"
            )
            summary, _usage = client.generate(prompt)
            if len(summary) <= max_length:
                return summary
        except Exception:
            pass

        # Fallback: deterministic regex compression
        compressed = _compress_context_regex(context, max_length)
        if len(compressed) <= max_length:
            return compressed
        # If still too long, fall through to Tier 4

    # Tier 4: Emergency truncation
    # Try LLM summarization before falling back to head/tail truncation
    if not MINIMAL_MODE:
        try:
            import concurrent.futures as _cf
            from orchestrator.llm_client import get_llm_client

            context_start = context[:len(context)//2]
            context_end = context[len(context)//2:]
            prompt = EMERGENCY_COMPRESS_PROMPT.format(
                next_step=current_step_description or "unknown",
                target_length=max_length,
                context_start=context_start,
                context_end=context_end,
            )

            event_log = get_event_log()
            event_log.emit(EventType.LLM_CALL_START,
                           data={"purpose": "emergency_compress"})
            client = get_llm_client(role="planner")

            with _cf.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.generate, prompt)
                summary = future.result(timeout=15)

            event_log.emit(EventType.LLM_CALL_COMPLETE,
                           data={"purpose": "emergency_compress"})

            if summary and len(summary) <= max_length:
                return summary
        except Exception:
            logger.debug("LLM emergency compression failed, using truncation fallback",
                         exc_info=True)

    # Fallback: progress file + tail of context
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


def extract_module_api(filepath: str) -> dict:
    """Extract public API from a Python module file.

    Parses the file with ast and returns top-level function names,
    class names, and module-level uppercase constant assignments.
    Returns empty dict on parse errors.
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except Exception:
        return {}

    functions = []
    classes = []
    constants = []
    variables = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                classes.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id.isupper():
                        constants.append(target.id)
                    elif not target.id.startswith("_"):
                        variables.append(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                if node.target.id.isupper():
                    constants.append(node.target.id)
                elif not node.target.id.startswith("_"):
                    variables.append(node.target.id)

    result = {}
    if functions:
        result["functions"] = functions
    if classes:
        result["classes"] = classes
    if constants:
        result["constants"] = constants
    if variables:
        result["variables"] = variables
    return result


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
        files_str = ", ".join(files_written)

    # Build key_outputs from summary or output
    key_outputs = summary
    if not key_outputs:
        if isinstance(output, dict):
            stdout = output.get("stdout", "")
            key_outputs = stdout or ""
        elif isinstance(output, str):
            key_outputs = output

    # Build relevant_data from raw output
    relevant_data = ""
    if isinstance(output, dict):
        stderr = output.get("stderr", "")
        if stderr:
            relevant_data = f"stderr: {stderr}"
    elif isinstance(output, str) and not summary:
        # Only include raw output as fallback when no structured summary
        relevant_data = output

    # Extract module APIs for .py files
    module_api_parts = []
    for fpath in files_written:
        if fpath.endswith(".py") and os.path.isfile(fpath):
            api = extract_module_api(fpath)
            if api:
                lines = []
                for kind in ("functions", "classes", "constants", "variables"):
                    if kind in api:
                        lines.append(f"      {kind}: {', '.join(api[kind])}")
                if lines:
                    module_api_parts.append(
                        f'    <module_api file="{fpath}">\n'
                        + "\n".join(lines)
                        + "\n    </module_api>"
                    )

    parts = [f'<dependency step="{dep_id}" title="{title}">']
    if files_str:
        parts.append(f"  <files_produced>{files_str}</files_produced>")
    if key_outputs:
        parts.append(f"  <key_outputs>{key_outputs}</key_outputs>")
    if relevant_data:
        parts.append(f"  <relevant_data>{relevant_data}</relevant_data>")
    if module_api_parts:
        parts.extend(module_api_parts)
    # Section 4: File signatures for richer dependency context
    file_sigs = extract_file_signatures(files_written)
    if file_sigs:
        parts.append(f"  <file_signatures>\n{file_sigs}\n  </file_signatures>")
    if verify:
        parts.append(f"  <verification>{verify}</verification>")
    parts.append("</dependency>")

    return "\n".join(parts)


DISTILL_PROMPT = """\
The following step just completed:
Step {dep_id} ({dep_title}): {dep_summary}
Files: {files}
Output: {output_preview}

The next step that will use this output:
Step {next_id} ({next_title}): {next_description}

Summarize ONLY the information from the completed step that is relevant \
to the next step. Be concise. Include file paths and key data."""


def distill_dependency_for_step(dep_id: int, dep_step: dict,
                                output: str | dict,
                                next_step: dict) -> str:
    if MINIMAL_MODE:
        return _distill_dependency_output(dep_id, dep_step, output)

    return _distill_dependency_output_llm(
        dep_id, dep_step, output,
        next_step.get("description", ""),
    )


TARGETED_DISTILL_PROMPT = """\
A completed step produced the following output. Extract ONLY the information \
that the consuming step needs.

<completed_step>
Step {dep_id} ({dep_title})
Files produced: {files}
Output:
{output_preview}
</completed_step>

{module_apis}
{file_signatures_block}
<consuming_step>
{consumer_desc}
</consuming_step>

Return a concise summary containing ONLY:
- File paths the consuming step will need to read or reference
- Data schemas, column names, or key structures if the consuming step processes data
- Function signatures with exact parameter names and types
- API responses, configuration values, or computed results the consuming step depends on
- Any error or warning information relevant to the consuming step

Module APIs and file signatures (exact exported names — downstream steps MUST use these):
Preserve all module API and file signature information exactly as provided above. \
Downstream steps must use these exact names, parameter types, and column names \
when importing or referencing.

Do NOT include generic status information or redundant details. Be as brief as possible."""


def _distill_dependency_output_llm(dep_id: int, dep_step: dict,
                                   output: str | dict,
                                   consumer_desc: str) -> str:
    try:
        from orchestrator.llm_client import get_llm_client

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "targeted_distill"})

        client = get_llm_client(role="planner")

        title = dep_step.get("title", f"Step {dep_id}")
        files_written = dep_step.get("files_written", [])

        if isinstance(output, dict):
            stdout = output.get("stdout", "")
            output_preview = stdout or ""
        elif isinstance(output, str):
            output_preview = output
        else:
            output_preview = ""

        summary = dep_step.get("summary", "")
        if summary:
            output_preview = f"{summary}\n{output_preview}"

        # Build module API info for .py files
        api_lines = []
        for fpath in files_written:
            if fpath.endswith(".py") and os.path.isfile(fpath):
                api = extract_module_api(fpath)
                if api:
                    parts_api = []
                    for kind in ("functions", "classes", "constants", "variables"):
                        if kind in api:
                            parts_api.append(
                                f"  {kind}: {', '.join(api[kind])}")
                    if parts_api:
                        api_lines.append(f"- {fpath}:")
                        api_lines.extend(parts_api)
        module_apis = ""
        if api_lines:
            module_apis = (
                "<module_apis>\n"
                + "\n".join(api_lines)
                + "\n</module_apis>"
            )

        # Section 4: File signatures for richer dependency context
        file_sigs = extract_file_signatures(files_written)
        file_signatures_block = ""
        if file_sigs:
            file_signatures_block = (
                "<file_signatures>\n"
                + file_sigs
                + "\n</file_signatures>"
            )

        prompt = TARGETED_DISTILL_PROMPT.format(
            dep_id=dep_id,
            dep_title=title,
            files=", ".join(files_written) if files_written else "(none)",
            output_preview=output_preview or "(no output)",
            consumer_desc=consumer_desc or "(no description)",
            module_apis=module_apis,
            file_signatures_block=file_signatures_block,
        )

        response, _usage = client.generate(prompt)

        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "targeted_distill"})

        if response and response.strip():
            verify = dep_step.get("verify", "")
            parts = [f'<dependency step="{dep_id}" title="{title}">']
            parts.append(f"  {response.strip()}")
            # Section 4: Include raw file signatures so exact names
            # are preserved even if the LLM summary paraphrases them.
            if file_sigs:
                parts.append(
                    f"  <file_signatures>\n{file_sigs}\n"
                    f"  </file_signatures>")
            if verify:
                parts.append(
                    f"  <verification>{verify}</verification>")
            parts.append("</dependency>")
            return "\n".join(parts)
    except Exception:
        pass

    return _distill_dependency_output(dep_id, dep_step, output)


REPLAN_CHECK_PROMPT = """\
A step in a multi-step plan just completed. Evaluate whether the remaining \
steps need adjustment based on the actual output.

## Completed Step
- ID: {step_id}
- Title: {step_title}
- Files produced: {files_written}
- Summary: {step_summary}
- UAS_RESULT: {uas_result}

## Dependent Steps (that consume this step's output)
{dependent_steps_block}

## Question
Based on the completed step's actual output, do any of the dependent steps \
need adjustment? Consider:
- Do referenced file names match what was actually produced?
- Is the data format/structure compatible with what downstream steps expect?
- Were expected outputs actually created?
- Is the output semantically sufficient for downstream steps?

Return ONLY valid JSON (no markdown fences):
{{"needs_replan": true/false, "reason": "brief explanation"}}
"""


def should_replan_heuristic(step: dict, remaining_steps: list[dict],
                            state: dict) -> tuple[bool, str]:
    """Check if remaining steps need re-planning (regex heuristic fallback).

    Compares the step's actual output (files_written, summary) against what
    downstream steps reference in their descriptions. If there's a mismatch,
    re-planning is recommended.

    Returns (needs_replan, detail) where detail describes the mismatch.

    Section 6a of PLAN.md (heuristic fallback).
    """
    if not remaining_steps:
        return False, ""

    files_written = step.get("files_written", [])
    uas_result = step.get("uas_result", {})

    # Collect basenames for matching
    actual_files = set()
    for f in files_written:
        # Handle both absolute and relative paths
        basename = os.path.basename(f)
        actual_files.add(basename)
        actual_files.add(f)

    # Check what downstream steps expect from this step
    mismatches = []
    step_id = step["id"]

    for rs in remaining_steps:
        # Only check steps that depend on the completed step
        if step_id not in rs.get("depends_on", []):
            continue

        desc = rs.get("description", "")
        # Look for file references in the description
        # Common patterns: "read X.csv", "from X.json", "load X.txt",
        # "X.py", etc.
        import re
        referenced_files = re.findall(
            r'(?:read|load|open|from|import|use|parse)\s+'
            r'(?:the\s+)?["\']?(\w+\.\w{1,5})["\']?',
            desc, re.IGNORECASE,
        )
        # Also match direct filename references like "data.csv"
        referenced_files += re.findall(
            r'\b(\w+\.(?:csv|json|txt|py|md|html|xml|yaml|yml|toml|db|sqlite'
            r'|parquet|xlsx|tsv|log))\b',
            desc, re.IGNORECASE,
        )
        referenced_files = list(set(referenced_files))

        for ref_file in referenced_files:
            if actual_files and ref_file not in actual_files:
                # Check if a similar file exists (fuzzy match by extension)
                ext = os.path.splitext(ref_file)[1]
                similar = [f for f in actual_files if f.endswith(ext)]
                if similar:
                    mismatches.append(
                        f"Step {rs['id']} references '{ref_file}' but step "
                        f"{step_id} produced {similar} instead"
                    )
                elif actual_files:
                    mismatches.append(
                        f"Step {rs['id']} references '{ref_file}' but step "
                        f"{step_id} produced {sorted(actual_files)}"
                    )

    # Check if step produced no files when downstream steps expect files
    if not files_written:
        for rs in remaining_steps:
            if step_id not in rs.get("depends_on", []):
                continue
            desc = rs.get("description", "").lower()
            if any(word in desc for word in ("read", "load", "open", "parse",
                                              "import from")):
                mismatches.append(
                    f"Step {rs['id']} expects to read files from step "
                    f"{step_id}, but no files were produced"
                )

    if mismatches:
        detail = "; ".join(mismatches[:5])
        return True, detail

    return False, ""


def should_replan_llm(step: dict, remaining_steps: list[dict],
                      state: dict) -> tuple[bool, str]:
    """Check if remaining steps need re-planning using LLM evaluation.

    Presents the completed step's actual output and dependent step
    descriptions to the LLM for semantic mismatch detection.

    Falls back to should_replan_heuristic() on LLM or parse failure.

    Returns (needs_replan, detail) where detail describes the mismatch.

    Section 6a of PLAN.md (LLM-steered).
    """
    if not remaining_steps:
        return False, ""

    step_id = step["id"]
    # Only consider dependent steps
    dependents = [
        rs for rs in remaining_steps
        if step_id in rs.get("depends_on", [])
    ]
    if not dependents:
        return False, ""

    try:
        from orchestrator.llm_client import get_llm_client
        client = get_llm_client(role="planner")

        files_written = step.get("files_written", [])
        uas_result = step.get("uas_result") or {}
        summary = step.get("summary", "")

        dep_lines = []
        for rs in dependents:
            dep_lines.append(
                f"- Step {rs['id']} ({rs.get('title', 'untitled')}): "
                f"{rs.get('description', 'no description')}"
            )
        dependent_steps_block = "\n".join(dep_lines) if dep_lines else "(none)"

        prompt = REPLAN_CHECK_PROMPT.format(
            step_id=step_id,
            step_title=step.get("title", "untitled"),
            files_written=", ".join(files_written) if files_written else "(none)",
            step_summary=summary or "(no summary)",
            uas_result=json.dumps(uas_result) if uas_result else "(none)",
            dependent_steps_block=dependent_steps_block,
        )

        response, _usage = client.generate(prompt)
        _accumulate_usage(state, _usage, model=client.model)

        # Parse JSON from response (handle possible markdown fences)
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Drop first and last fence lines
            lines = [l for l in lines if not l.startswith("```")]
            text = "\n".join(lines).strip()

        result = json.loads(text)
        needs = bool(result.get("needs_replan", False))
        reason = result.get("reason", "")
        return needs, reason

    except Exception:
        # Fallback to regex heuristic
        return should_replan_heuristic(step, remaining_steps, state)


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

    # Section 11d: Inject workspace name so generated scripts avoid nesting
    ws_name = os.path.basename(workspace_path) if workspace_path else ""
    if ws_name and ws_name != ".":
        parts.append(f"<workspace_name>{ws_name}</workspace_name>")

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
            distilled = distill_dependency_for_step(
                dep_id, dep_step, output, step,
            )
            # Only include if there's actual content
            if ("<key_outputs>" in distilled
                    or "<files_produced>" in distilled
                    or "dependency" in distilled):
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

    # Section 7: Extract data-quality mentions from dependency parts and
    # promote them to a top-level <data_quality_warnings> section so they
    # are not buried inside <dependency> blocks.
    _DQ_PATTERN = re.compile(
        r'(?:100%\s*NaN|all\s*NaN|entirely\s*NaN'
        r'|\b\d{2,3}%\s*NaN|\b\d{2,3}%\s*missing'
        r'|degenerate|constant\s*column|zero\s*variance'
        r'|no\s*valid\s*data|critical\s*missing)',
        re.IGNORECASE,
    )
    dq_lines: list[str] = []
    for part in parts:
        for line in part.split("\n"):
            if _DQ_PATTERN.search(line):
                cleaned = line.strip().lstrip("- ")
                if cleaned and cleaned not in dq_lines:
                    dq_lines.append(cleaned)
    if dq_lines:
        warning_block = "\n".join(f"- {w}" for w in dq_lines)
        parts.insert(
            0,
            "<data_quality_warnings>\n"
            + warning_block
            + "\n</data_quality_warnings>",
        )

    # Section 11: Enrichment context from completed upstream steps.
    # Stored in state["enrichment_context"] rather than baked into
    # descriptions, so compression logic can filter it.
    if state:
        enrichment_context = state.get("enrichment_context", {})
        step_enrichment = enrichment_context.get(step.get("id"), "")
        if step_enrichment:
            parts.append(
                f"<enrichment_context>\n{step_enrichment}\n"
                f"</enrichment_context>"
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
    current_run_id = state.get("run_id", "") if state else ""
    progress = read_progress_file(run_id=current_run_id)
    if progress:
        parts.append(f"<progress>\n{progress}\n</progress>")
    else:
        # Fallback to scratchpad if no progress file yet.
        # Filter by run_id so prior runs' entries don't leak.
        current_run_id = state.get("run_id", "") if state else ""
        scratchpad = read_scratchpad(run_id=current_run_id)
        if scratchpad:
            parts.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

    context = "\n\n".join(parts)

    # Section 4c / Section 5: Tiered context compression with step awareness
    if MAX_CONTEXT_LENGTH and len(context) > MAX_CONTEXT_LENGTH:
        context = compress_context(
            context, MAX_CONTEXT_LENGTH,
            goal=goal, progress_content=progress,
            current_step_description=step.get("description", ""),
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
        level_steps = [step_by_id[sid] for sid in level]
        conflicts = find_file_conflicts(level_steps)
        if conflicts:
            print(f"  ** File conflicts: {conflicts} — will be serialized",
                  file=sys.stderr)
        for sid in level:
            step = step_by_id[sid]
            deps = step["depends_on"]
            deps_str = f" [depends on: {deps}]" if deps else ""
            outs = step.get("outputs", [])
            outs_str = f" [outputs: {outs}]" if outs else ""
            print(f"  Step {sid}: {step['title']}{deps_str}{outs_str}",
                  file=sys.stderr)
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
    state_dir = os.path.join(WORKSPACE, ".uas_state")
    os.makedirs(state_dir, exist_ok=True)
    blocker_path = os.path.join(state_dir, "blocker.md")
    with open(blocker_path, "w") as f:
        f.write("# Architect Blocker\n\n")
        f.write(f"**Goal:** {state['goal']}\n\n")
        f.write(f"**Blocked at step {step['id']}:** {step['title']}\n\n")
        f.write("## Failure Details\n\n")
        actual_rewrites = step.get("rewrites", 0)
        f.write(f"The Orchestrator failed this step after all retries, and the "
                f"Architect used {actual_rewrites} of {MAX_SPEC_REWRITES} spec rewrites.\n\n")
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


def _probe_environment(run_id: str = ""):
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
    append_scratchpad("\n".join(lines), run_id=run_id)


_TRAILING_ANNOTATION_RE = re.compile(r'\s+\([^)]+\)\s*$')


def _sanitize_files_written(files: list[str]) -> list[str]:
    """Strip trailing parenthesized annotations from file paths.

    LLMs sometimes annotate entries like ``"data/file.csv (symlink)"``
    or ``"output/ (directory)"``.  Strip those annotations so downstream
    validation and path lookups use the real filesystem path.
    """
    cleaned: list[str] = []
    for f in files:
        f = _TRAILING_ANNOTATION_RE.sub('', f)
        f = f.strip()
        if f:
            cleaned.append(f)
    return cleaned


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
        if os.path.exists(fpath):
            continue

        # For absolute paths outside the workspace (e.g. /uas/... from sandbox),
        # try rebasing onto the workspace.
        if os.path.isabs(f) and not f.startswith(workspace):
            # Try the basename within the workspace tree
            basename = os.path.basename(f)
            rebased = os.path.join(workspace, basename)
            if os.path.exists(rebased):
                continue

        # Search subdirectories — scripts may report paths relative
        # to a project subdirectory rather than the workspace root.
        found = False
        found_path = None
        search_name = os.path.basename(f)
        for root, _dirs, files in os.walk(workspace):
            if search_name in files:
                candidate = os.path.join(root, search_name)
                if candidate.endswith(f.lstrip("/")):
                    found = True
                    found_path = candidate
                    break
            # Limit depth to avoid traversing .state, .git, etc.
            _dirs[:] = [
                d for d in _dirs
                if d not in (".uas_state", ".git", "__pycache__", "node_modules")
            ]
        if not found:
            return f"UAS_RESULT claims file '{f}' was written but it does not exist"

    return None


# General-purpose temporal tokens for data-leakage detection.
# When a feature's suffix is from one set and the target's suffix is
# from the other, they represent different timepoints of the same
# measure (e.g. baseline → outcome prediction), not data leakage.
_EARLY_TIME_TOKENS = frozenset({
    "admission", "baseline", "initial", "first", "pre", "t0",
    "start", "begin", "before", "early", "prior", "entry",
    "intake", "enrollment", "onset",
})
_LATE_TIME_TOKENS = frozenset({
    "outcome", "final", "last", "post", "end", "after",
    "late", "result", "endpoint", "discharge", "exit",
    "followup", "latest",
})


def _is_opposite_temporal(suffix_a: str, suffix_b: str) -> bool:
    """Return True if the two suffixes are recognised temporal tokens
    from opposite sides (early vs late), indicating different timepoints."""
    return (
        (suffix_a in _EARLY_TIME_TOKENS and suffix_b in _LATE_TIME_TOKENS)
        or (suffix_a in _LATE_TIME_TOKENS and suffix_b in _EARLY_TIME_TOKENS)
    )


def check_output_quality(step: dict, workspace: str) -> list[str]:
    """Validate quality of output files after successful execution.

    Checks that all claimed files exist, are non-empty, and have valid
    format for known file types (.json, .csv, .py).
    Returns a list of issue strings (empty = clean).
    """
    # Files that are legitimately empty by convention.
    _ALLOWED_EMPTY_BASENAMES = {
        "__init__.py", "__init__.pyi", "py.typed",
        ".gitkeep", ".keep", ".gitignore", ".nojekyll",
    }

    issues: list[str] = []
    files_written = step.get("files_written", [])

    for f in files_written:
        fpath = os.path.join(workspace, f) if not os.path.isabs(f) else f
        if not os.path.exists(fpath):
            # Already caught by validate_uas_result, skip here
            continue

        # Skip directories — they are structural, not content files.
        if os.path.isdir(fpath):
            continue

        # Check non-empty (allow conventionally-empty files)
        try:
            size = os.path.getsize(fpath)
        except OSError:
            continue
        if size == 0:
            if os.path.basename(fpath) in _ALLOWED_EMPTY_BASENAMES:
                continue
            issues.append(f"File '{f}' is empty (0 bytes)")
            continue

        # Format-specific checks
        lower = f.lower()
        if lower.endswith(".json"):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as jf:
                    data = json.load(jf)
                # Check accuracy vs baseline accuracy
                if isinstance(data, dict):
                    accuracy = data.get("accuracy")
                    baseline = data.get("baseline_accuracy")
                    if (
                        accuracy is not None
                        and baseline is not None
                        and isinstance(accuracy, (int, float))
                        and isinstance(baseline, (int, float))
                        and accuracy < baseline
                    ):
                        issues.append(
                            f"File '{f}': model accuracy ({accuracy:.4f}) is below "
                            f"baseline accuracy ({baseline:.4f}) — model is worse "
                            f"than trivial baseline"
                        )
            except (json.JSONDecodeError, OSError) as e:
                issues.append(f"File '{f}' has invalid JSON: {e}")

        elif lower.endswith(".csv"):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as cf:
                    first_line = cf.readline()
                if not first_line.strip():
                    issues.append(f"File '{f}' is a CSV with no header line")
                else:
                    # Check for columns that are 100% NaN
                    try:
                        import csv as _csv

                        with open(fpath, "r", encoding="utf-8", errors="replace") as cf:
                            reader = _csv.DictReader(cf)
                            headers = reader.fieldnames or []
                            if headers:
                                row_count = 0
                                nan_counts: dict[str, int] = {h: 0 for h in headers}
                                for row in reader:
                                    row_count += 1
                                    for h in headers:
                                        val = (row.get(h) or "").strip().lower()
                                        if val in ("", "nan", "none", "null", "na", "n/a"):
                                            nan_counts[h] += 1
                                if row_count > 0:
                                    for h in headers:
                                        if nan_counts[h] == row_count:
                                            issues.append(
                                                f"File '{f}' column '{h}' is 100% NaN/empty "
                                                f"({row_count} rows)"
                                            )
                    except Exception:
                        pass  # Column-level check is best-effort
            except OSError as e:
                issues.append(f"File '{f}' could not be read: {e}")

        elif lower.endswith(".py"):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as pf:
                    source = pf.read()
                compile(source, fpath, "exec")
                # Check for hardcoded /workspace paths
                for line_no, line in enumerate(source.splitlines(), 1):
                    stripped = line.strip()
                    # Skip comments
                    if stripped.startswith("#"):
                        continue
                    if '"/workspace' in line or "'/workspace" in line:
                        issues.append(
                            f"File '{f}' line {line_no}: hardcoded /workspace "
                            f"path detected — will break outside container"
                        )
                        break  # One warning per file is enough
            except SyntaxError as e:
                issues.append(f"File '{f}' has Python syntax error: {e}")
            except OSError as e:
                issues.append(f"File '{f}' could not be read: {e}")

    # Data leakage detection: when a step produces a model file AND a metrics
    # JSON, check whether any feature name shares a temporal prefix with the
    # target variable (e.g. "discharge" features predicting a "discharge" target).
    lower_files = [f.lower() for f in files_written]
    has_model_file = any(
        f.endswith(".joblib") or f.endswith(".pkl") or f.endswith("model.pickle")
        for f in lower_files
    )
    metrics_files = [
        f for f, lf in zip(files_written, lower_files)
        if lf.endswith(".json") and "metric" in lf
    ]
    if has_model_file and metrics_files:
        for mf in metrics_files:
            mpath = os.path.join(workspace, mf) if not os.path.isabs(mf) else mf
            try:
                with open(mpath, "r", encoding="utf-8", errors="replace") as jf:
                    mdata = json.load(jf)
                if not isinstance(mdata, dict):
                    continue
                feature_names = mdata.get("feature_names") or mdata.get("features") or []
                target_name = (
                    mdata.get("target") or mdata.get("target_variable") or ""
                )
                if not feature_names or not target_name:
                    continue
                # Extract temporal prefix from target (e.g. "discharge" from
                # "discharge_ais") — use the first underscore-delimited token.
                target_prefix = target_name.split("_")[0].lower()
                if len(target_prefix) < 3:
                    continue  # Too short to be a meaningful temporal indicator
                target_suffix = (
                    target_name.rsplit("_", 1)[-1].lower()
                    if "_" in target_name else ""
                )
                leaked = [
                    fn for fn in feature_names
                    if fn.lower().startswith(target_prefix + "_")
                    and fn.lower() != target_name.lower()
                    and not _is_opposite_temporal(
                        fn.rsplit("_", 1)[-1].lower() if "_" in fn else "",
                        target_suffix,
                    )
                ]
                if leaked:
                    issues.append(
                        f"File '{mf}': possible data leakage — features "
                        f"{leaked[:5]} share temporal prefix '{target_prefix}' "
                        f"with target '{target_name}'. These may be "
                        f"future-time variables that should not be used as "
                        f"predictors."
                    )
            except (json.JSONDecodeError, OSError, KeyError):
                pass  # Leakage check is best-effort

    return issues


# Patterns indicating data quality issues likely caused by upstream dependencies.
_DATA_QUALITY_PATTERNS = [
    "all nan", "100% nan", "no valid data", "constant column",
    "all values are nan", "entirely nan", "all missing",
]


def _has_data_quality_error(error_text: str) -> bool:
    """Return True if the error text indicates upstream data quality issues."""
    lower = error_text.lower()
    return any(pat in lower for pat in _DATA_QUALITY_PATTERNS)


def check_input_quality(step: dict, state: dict, workspace: str) -> list[str]:
    """Check quality of dependency outputs before code generation.

    Scans CSV files produced by dependency steps for columns that are >90%
    NaN/empty. Returns a list of warning strings (empty = clean).
    """
    warnings: list[str] = []
    step_by_id = {s["id"]: s for s in state.get("steps", [])}

    for dep_id in step.get("depends_on", []):
        dep_step = step_by_id.get(dep_id, {})
        files_written = dep_step.get("files_written", [])

        for f in files_written:
            if not f.lower().endswith(".csv"):
                continue
            fpath = os.path.join(workspace, f) if not os.path.isabs(f) else f
            if not os.path.isfile(fpath):
                continue
            try:
                import csv as _csv

                with open(fpath, "r", encoding="utf-8", errors="replace") as cf:
                    reader = _csv.DictReader(cf)
                    headers = reader.fieldnames or []
                    if not headers:
                        continue
                    row_count = 0
                    nan_counts: dict[str, int] = {h: 0 for h in headers}
                    for row in reader:
                        row_count += 1
                        for h in headers:
                            val = (row.get(h) or "").strip().lower()
                            if val in ("", "nan", "none", "null", "na", "n/a"):
                                nan_counts[h] += 1
                    if row_count > 0:
                        high_nan_cols = [
                            h for h in headers
                            if nan_counts[h] / row_count > 0.9
                        ]
                        if high_nan_cols:
                            pcts = [
                                f"{h} ({nan_counts[h]/row_count:.0%})"
                                for h in high_nan_cols
                            ]
                            warnings.append(
                                f"Dependency step {dep_id} file '{f}': "
                                f"columns >90% NaN/empty ({row_count} rows): "
                                f"{', '.join(pcts)}"
                            )
            except Exception:
                pass  # Input quality check is best-effort

    return warnings


def cleanup_workspace_artifacts(
    workspace: str,
    pre_step_files: set[str] | None = None,
    step_output_files: set[str] | None = None,
) -> list[str]:
    """Remove __pycache__ directories, .pyc files, and UAS script artifacts.

    Args:
        workspace: Path to the workspace directory.
        pre_step_files: Set of filenames that existed in the workspace root
            before the current step. When provided, new ``.py`` files whose
            content contains the ``UAS_RESULT`` marker are treated as
            leftover script artifacts and removed.
        step_output_files: Set of filenames that the current step claims to
            have written (from UAS_RESULT ``files_written``). These are
            protected from artifact cleanup even if they are new ``.py``
            files containing ``UAS_RESULT``.

    Returns:
        List of artifact filenames that were removed.
    """
    removed: list[str] = []
    protected = step_output_files or set()
    try:
        for root, dirs, files in os.walk(workspace):
            # Remove .pyc files
            for fname in files:
                if fname.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(root, fname))
                    except OSError:
                        pass
            # Remove __pycache__ dirs (and don't recurse into them)
            if "__pycache__" in dirs:
                pycache = os.path.join(root, "__pycache__")
                try:
                    import shutil
                    shutil.rmtree(pycache, ignore_errors=True)
                except Exception:
                    pass
                dirs.remove("__pycache__")
            # Skip .git and other internal dirs
            dirs[:] = [
                d for d in dirs
                if d not in (".git", ".uas_state", "node_modules")
            ]
    except OSError:
        pass

    # Section 7: Remove leftover UAS script artifacts from workspace root.
    if pre_step_files is not None:
        try:
            for fname in os.listdir(workspace):
                if not fname.endswith(".py"):
                    continue
                if fname in pre_step_files:
                    continue  # existed before this step — leave it alone
                if fname in protected:
                    continue  # claimed step output — leave it alone
                fpath = os.path.join(workspace, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                if "UAS_RESULT" in content:
                    try:
                        os.remove(fpath)
                        removed.append(fname)
                        logger.info("  Removed UAS script artifact: %s", fname)
                    except OSError:
                        pass
        except OSError:
            pass
    return removed


# ---------------------------------------------------------------------------
# Section 10: Workspace snapshot and recursive diff cleanup
# ---------------------------------------------------------------------------

_SNAPSHOT_SKIP_DIRS = {".git", ".uas_state", ".uas_auth", "__pycache__", "node_modules"}


def snapshot_workspace(workspace: str) -> set[str]:
    """Return the set of all relative file paths under *workspace*.

    Skips internal directories (.git, .uas_state, .uas_auth, __pycache__,
    node_modules) so only user-visible project files are tracked.
    """
    paths: set[str] = set()
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SNAPSHOT_SKIP_DIRS]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), workspace)
            paths.add(rel)
    return paths


def _remove_empty_dirs(workspace: str) -> None:
    """Remove empty directories under *workspace* (bottom-up).

    Skips the same internal directories as ``snapshot_workspace``.
    """
    for root, dirs, files in os.walk(workspace, topdown=False):
        dirs[:] = [d for d in dirs if d not in _SNAPSHOT_SKIP_DIRS]
        rel = os.path.relpath(root, workspace)
        if rel == ".":
            continue
        # Don't remove skip-listed directories themselves
        if os.path.basename(root) in _SNAPSHOT_SKIP_DIRS:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass


def cleanup_step_artifacts(
    workspace: str,
    pre_snapshot: set[str],
    step_output_files: set[str],
) -> list[str]:
    """Remove files created during a step that are not claimed outputs.

    Args:
        workspace: Path to the workspace directory.
        pre_snapshot: Set of relative paths that existed before the step
            (as returned by :func:`snapshot_workspace`).
        step_output_files: Set of relative paths the step claims to have
            written (from ``files_written``).

    Returns:
        Sorted list of relative paths that were removed.
    """
    post_snapshot = snapshot_workspace(workspace)
    new_files = post_snapshot - pre_snapshot
    claimed = {os.path.normpath(f) for f in step_output_files}
    artifacts = new_files - claimed
    removed: list[str] = []
    for rel in sorted(artifacts):
        fpath = os.path.join(workspace, rel)
        try:
            os.remove(fpath)
            removed.append(rel)
            logger.info("  Removed step artifact: %s", rel)
        except OSError:
            pass
    # Remove empty directories left behind
    _remove_empty_dirs(workspace)
    return removed


# ---------------------------------------------------------------------------
# Section 11: Detect and prevent nested project duplication
# ---------------------------------------------------------------------------

_NESTED_PROJECT_MARKERS = {"src", "scripts", "tests", "data"}


def detect_nested_duplication(workspace: str) -> str | None:
    """Detect a nested directory that mirrors the workspace structure.

    A generated script may create ``project_name/src/...`` inside a workspace
    that IS already the project root.  This function detects the pattern by
    looking for a top-level subdirectory that shares at least two "project
    marker" directories (src, scripts, tests, data) with the workspace root.

    Returns the subdirectory name if duplication is found, ``None`` otherwise.
    """
    try:
        root_entries = os.listdir(workspace)
    except OSError:
        return None
    root_dirs = {
        d for d in root_entries
        if os.path.isdir(os.path.join(workspace, d))
        and d not in _SNAPSHOT_SKIP_DIRS
    }
    root_markers = root_dirs & _NESTED_PROJECT_MARKERS
    if len(root_markers) < 2:
        return None
    for d in sorted(root_dirs - _NESTED_PROJECT_MARKERS):
        nested = os.path.join(workspace, d)
        try:
            nested_children = {
                c for c in os.listdir(nested)
                if os.path.isdir(os.path.join(nested, c))
                and c not in _SNAPSHOT_SKIP_DIRS
            }
        except OSError:
            continue
        if len(nested_children & _NESTED_PROJECT_MARKERS) >= 2:
            return d
    return None


def resolve_nested_duplication(workspace: str, nested_name: str) -> list[str]:
    """Promote a nested project copy to the workspace root.

    When duplication is detected (e.g. ``workspace/myapp/src/`` alongside
    ``workspace/src/``), this function merges the nested copy into the root.
    On conflicts the nested version wins (it is typically more recent and
    complete).  The nested directory is removed after promotion.

    Returns a sorted list of top-level items that were promoted.
    """
    nested = os.path.join(workspace, nested_name)
    if not os.path.isdir(nested):
        return []
    promoted: list[str] = []
    for item in sorted(os.listdir(nested)):
        src = os.path.join(nested, item)
        dst = os.path.join(workspace, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            shutil.rmtree(src)
        else:
            shutil.copy2(src, dst)
            os.remove(src)
        promoted.append(item)
    shutil.rmtree(nested, ignore_errors=True)
    return promoted


# ---------------------------------------------------------------------------
# Section 12: Project structure manifest with stale file detection
# ---------------------------------------------------------------------------


class ProjectManifest:
    """Track the canonical set of project files across steps.

    Every file claimed by a completed step is recorded with its originating
    step ID.  When a new step produces files that functionally replace earlier
    ones (same module name in a different location), the old files are flagged
    as superseded so they can be removed.
    """

    def __init__(self, files: dict[str, int] | None = None):
        # rel_path → step_id that created it
        self.files: dict[str, int] = dict(files) if files else {}

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, int]:
        """Serialise to a plain dict for state.json storage."""
        return dict(self.files)

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "ProjectManifest":
        """Reconstruct from a state.json dict."""
        return cls(files=data)

    # -- mutation ------------------------------------------------------------

    def add_step_output(self, step_id: int, files: list[str]) -> None:
        """Register files produced by *step_id*."""
        for f in files:
            self.files[os.path.normpath(f)] = step_id

    def remove(self, rel_path: str) -> None:
        """Remove a file from the manifest."""
        self.files.pop(os.path.normpath(rel_path), None)

    # -- supersession detection ----------------------------------------------

    def detect_superseded(self, new_files: list[str]) -> list[str]:
        """Find existing manifest files that are functionally replaced by *new_files*.

        A file is considered superseded when a new file has the **same
        basename** but lives at a **different relative path**.  For example,
        ``src/app/cleaner.py`` is superseded by
        ``src/app/data/cleaner.py``.
        """
        superseded: list[str] = []
        new_normed = {os.path.normpath(f) for f in new_files}
        new_basenames: dict[str, str] = {}
        for nf in new_normed:
            base = os.path.basename(nf)
            if base != "__init__.py":
                new_basenames[base] = nf
        for old_f in list(self.files.keys()):
            if old_f in new_normed:
                continue
            old_base = os.path.basename(old_f)
            if old_base in new_basenames and old_base != "__init__.py":
                superseded.append(old_f)
        return sorted(superseded)

    def detect_superseded_dirs(self, new_files: list[str]) -> list[tuple[str, str]]:
        """Detect when an entire directory is superseded by a new one.

        Returns a list of ``(old_dir, new_dir)`` pairs where *old_dir* and
        *new_dir* share overlapping module names but live at different paths.
        For example ``src/app/tabs/`` superseded by
        ``src/app/dashboard/``.
        """
        new_normed = [os.path.normpath(f) for f in new_files]
        # Collect module names per directory for new files
        new_dir_modules: dict[str, set[str]] = {}
        for nf in new_normed:
            d = os.path.dirname(nf)
            if not d:
                continue
            mod = os.path.splitext(os.path.basename(nf))[0]
            if mod != "__init__":
                new_dir_modules.setdefault(d, set()).add(mod)
        # Collect module names per directory for existing manifest files
        old_dir_modules: dict[str, set[str]] = {}
        for of in self.files:
            d = os.path.dirname(of)
            if not d:
                continue
            mod = os.path.splitext(os.path.basename(of))[0]
            if mod != "__init__":
                old_dir_modules.setdefault(d, set()).add(mod)
        pairs: list[tuple[str, str]] = []
        for new_d, new_mods in new_dir_modules.items():
            for old_d, old_mods in old_dir_modules.items():
                if new_d == old_d:
                    continue
                overlap = new_mods & old_mods
                # Flag if at least 2 overlapping module names and the overlap
                # covers a significant fraction of the old directory.
                if len(overlap) >= 2 and len(overlap) >= len(old_mods) // 2:
                    pairs.append((old_d, new_d))
        return sorted(pairs)


_SUPERSESSION_CONFIRM_PROMPT = """\
A project step produced a new file that may supersede an older one.

Old file: {old_path}  (created by step {old_step})
New file: {new_path}  (created by step {new_step})

Both share the same filename but live in different directories.
Does the new file functionally replace the old one?

Reply with ONLY a JSON object:
{{"superseded": true}}  or  {{"superseded": false}}
"""

_DIR_SUPERSESSION_CONFIRM_PROMPT = """\
A project step produced files in a new directory that may supersede an older
directory.

Old directory: {old_dir}  (modules: {old_modules})
New directory: {new_dir}  (modules: {new_modules})

Both directories contain overlapping module names: {overlap}.
Does the new directory functionally replace the old one?

Reply with ONLY a JSON object:
{{"superseded": true}}  or  {{"superseded": false}}
"""


def confirm_supersession_llm(
    old_path: str,
    old_step: int,
    new_path: str,
    new_step: int,
) -> bool:
    """Ask the LLM whether *new_path* functionally replaces *old_path*.

    Returns ``True`` if the LLM confirms supersession, ``False`` otherwise
    (including on any error).
    """
    try:
        from orchestrator.llm_client import get_llm_client

        client = get_llm_client(role="planner")
        prompt = _SUPERSESSION_CONFIRM_PROMPT.format(
            old_path=old_path,
            old_step=old_step,
            new_path=new_path,
            new_step=new_step,
        )
        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "supersession_confirm"})
        response, _usage = client.generate(prompt)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "supersession_confirm"})
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            text = text[brace_start:brace_end + 1]
        result = json.loads(text)
        return bool(result.get("superseded", False))
    except Exception as exc:
        logger.debug("Supersession LLM confirmation failed: %s", exc)
        return False


def confirm_dir_supersession_llm(
    old_dir: str,
    new_dir: str,
    old_modules: set[str],
    new_modules: set[str],
) -> bool:
    """Ask the LLM whether *new_dir* functionally replaces *old_dir*.

    Returns ``True`` if the LLM confirms supersession, ``False`` otherwise.
    """
    try:
        from orchestrator.llm_client import get_llm_client

        client = get_llm_client(role="planner")
        overlap = old_modules & new_modules
        prompt = _DIR_SUPERSESSION_CONFIRM_PROMPT.format(
            old_dir=old_dir,
            new_dir=new_dir,
            old_modules=", ".join(sorted(old_modules)),
            new_modules=", ".join(sorted(new_modules)),
            overlap=", ".join(sorted(overlap)),
        )
        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "dir_supersession_confirm"})
        response, _usage = client.generate(prompt)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "dir_supersession_confirm"})
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            text = text[brace_start:brace_end + 1]
        result = json.loads(text)
        return bool(result.get("superseded", False))
    except Exception as exc:
        logger.debug("Dir supersession LLM confirmation failed: %s", exc)
        return False


def remove_superseded_files(
    workspace: str,
    manifest: "ProjectManifest",
    new_step_id: int,
    new_files: list[str],
    use_llm: bool = True,
) -> list[str]:
    """Detect and remove files superseded by *new_files*.

    For each candidate returned by ``manifest.detect_superseded()``, ask the
    LLM to confirm (when *use_llm* is ``True``).  Confirmed superseded files
    are deleted from disk and removed from the manifest.

    Returns the list of removed relative paths.
    """
    # Detect both file-level and directory-level supersession BEFORE any
    # removals, so the manifest state is consistent for both checks.
    candidates = manifest.detect_superseded(new_files)
    dir_pairs = manifest.detect_superseded_dirs(new_files)
    # Snapshot directory-level old-file lists before file-level removal
    # modifies the manifest.
    dir_old_files: dict[str, list[str]] = {}
    dir_old_mods: dict[str, set[str]] = {}
    for old_dir, new_dir in dir_pairs:
        dir_old_files[(old_dir, new_dir)] = [
            f for f in list(manifest.files.keys())
            if os.path.dirname(f) == old_dir
        ]
        dir_old_mods[(old_dir, new_dir)] = {
            os.path.splitext(os.path.basename(f))[0]
            for f in manifest.files if os.path.dirname(f) == old_dir
            and os.path.basename(f) != "__init__.py"
        }

    removed: list[str] = []

    # File-level supersession
    for old_f in candidates:
        old_base = os.path.basename(old_f)
        # Find the new file that caused the supersession
        new_f = None
        for nf in new_files:
            if os.path.basename(os.path.normpath(nf)) == old_base:
                new_f = os.path.normpath(nf)
                break
        if new_f is None:
            continue
        old_step = manifest.files.get(old_f, 0)
        confirmed = True
        if use_llm and not MINIMAL_MODE:
            confirmed = confirm_supersession_llm(
                old_f, old_step, new_f, new_step_id,
            )
        if confirmed:
            fpath = os.path.join(workspace, old_f)
            try:
                os.remove(fpath)
                manifest.remove(old_f)
                removed.append(old_f)
                logger.info(
                    "  Removed stale file %s (superseded by %s in step %d)",
                    old_f, new_f, new_step_id,
                )
            except OSError:
                pass

    # Directory-level supersession
    for old_dir, new_dir in dir_pairs:
        new_mods = {
            os.path.splitext(os.path.basename(os.path.normpath(nf)))[0]
            for nf in new_files if os.path.dirname(os.path.normpath(nf)) == new_dir
            and os.path.basename(nf) != "__init__.py"
        }
        old_mods = dir_old_mods.get((old_dir, new_dir), set())
        dir_confirmed = True
        if use_llm and not MINIMAL_MODE:
            dir_confirmed = confirm_dir_supersession_llm(
                old_dir, new_dir, old_mods, new_mods,
            )
        if dir_confirmed:
            for of in dir_old_files.get((old_dir, new_dir), []):
                if of in removed:
                    continue
                fpath = os.path.join(workspace, of)
                try:
                    os.remove(fpath)
                    manifest.remove(of)
                    removed.append(of)
                    logger.info(
                        "  Removed stale file %s (directory %s superseded by %s)",
                        of, old_dir, new_dir,
                    )
                except OSError:
                    pass
            # Clean up empty directory
            _remove_empty_dirs(workspace)
    return sorted(removed)


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
    (_re.compile(r'\bgit\s+init\b'),
     "git init in generated script (version control is managed by the framework)", "warning"),
    (_re.compile(r'''["']git["']\s*,\s*["']init["']'''),
     "git init in generated script (version control is managed by the framework)", "warning"),
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


GUARDRAIL_REVIEW_PROMPT = """\
Review this Python script for security and best-practice violations:
```python
{code}
```

Check for:
- Hardcoded secrets, API keys, tokens
- SQL injection, command injection
- Unsafe deserialization (pickle, yaml.load without SafeLoader)
- Use of eval/exec on untrusted data
- Plain HTTP URLs (should be HTTPS, except localhost/127.0.0.1)
- Missing input validation on external data
- Bare except clauses
- subprocess with shell=True

Return ONLY valid JSON (no markdown fences):
{{"violations": [{{"line": N, "description": "...", "severity": "error or warning"}}], "clean": true_or_false}}

Use severity "error" only for hardcoded secrets/keys. Use "warning" for all \
other issues. If no issues are found, return {{"violations": [], "clean": true}}.
"""


def check_guardrails_llm(code: str) -> list[dict]:
    """Review code for security violations using LLM judgment.

    Calls the LLM with a security review prompt and parses the structured
    response. Falls back to regex-based check_guardrails() on failure.

    Runs by default. Set UAS_NO_LLM_GUARDRAILS=1 to opt out, or enable
    UAS_MINIMAL mode to skip LLM guardrails (regex only).
    """
    try:
        from orchestrator.llm_client import get_llm_client

        client = get_llm_client(role="planner")
        prompt = GUARDRAIL_REVIEW_PROMPT.format(code=code)

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "guardrail_review"})
        response, _usage = client.generate(prompt)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "guardrail_review"})

        # Parse JSON from response (handle possible markdown fences)
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()

        # Try to extract JSON object
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            text = text[brace_start:brace_end + 1]

        result = json.loads(text)
        raw_violations = result.get("violations", [])
        if not isinstance(raw_violations, list):
            logger.warning("LLM guardrail review returned non-list violations, "
                           "falling back to regex.")
            return check_guardrails(code)

        violations = []
        for v in raw_violations:
            if not isinstance(v, dict):
                continue
            severity = v.get("severity", "warning")
            if severity not in ("error", "warning"):
                severity = "warning"
            violations.append({
                "line": int(v.get("line", 0)),
                "match": "",
                "description": str(v.get("description", ""))[:200],
                "severity": severity,
            })
        return violations

    except Exception as exc:
        logger.warning("LLM guardrail review failed (%s), falling back to regex.",
                       exc)
        return check_guardrails(code)


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

    # Check for a dependency manifest (any ecosystem)
    has_deps = any(
        e in entries
        for e in (
            "pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock",
            "package.json", "Cargo.toml", "go.mod", "Gemfile",
            "build.gradle", "build.gradle.kts", "pom.xml",
            "pubspec.yaml", "Package.swift", "composer.json",
        )
    )
    if not has_deps:
        warnings.append(
            "Project has no dependency manifest "
            "(pyproject.toml, requirements.txt, package.json, etc.)"
        )

    # Check for orphaned modules
    orphaned = detect_orphaned_modules(workspace)
    for orph in orphaned:
        warnings.append(
            f"Module `{orph}` is never imported by any other module "
            f"in the project (orphaned code)"
        )

    return warnings


def detect_orphaned_modules(workspace: str) -> list[str]:
    """Detect Python modules that are never imported by any other module.

    Finds all ``.py`` files in *workspace* (skipping ``__init__.py``, test
    files, ``conftest.py``, and entry-point files) and checks whether each
    is referenced by at least one ``import`` or ``from … import`` statement
    in another ``.py`` file in the workspace.

    Returns a list of relative paths for orphaned (never-imported) modules.
    """
    skip_dirs = {".uas_state", ".git", "__pycache__", "venv", ".venv",
                 "node_modules", ".tox", ".eggs"}

    # Collect all .py files
    py_files: list[str] = []  # relative paths
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            if fname.endswith(".py"):
                rel = os.path.relpath(os.path.join(dirpath, fname), workspace)
                py_files.append(rel)

    if not py_files:
        return []

    # Identify entry points (well-known names + __main__ guard)
    entry_points: set[str] = set()
    for rel in py_files:
        basename = os.path.basename(rel)
        if basename in _ENTRY_POINT_NAMES:
            entry_points.add(rel)
            continue
        full = os.path.join(workspace, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            if re.search(r'''if\s+__name__\s*==\s*['"]__main__['"]''', source):
                entry_points.add(rel)
        except OSError:
            pass

    # Files to exclude from orphan detection
    _exclude_basenames = {"__init__.py", "conftest.py", "setup.py"}

    candidates: list[str] = []
    for rel in py_files:
        basename = os.path.basename(rel)
        if basename in _exclude_basenames:
            continue
        if basename.startswith("test_") or basename.endswith("_test.py"):
            continue
        if rel in entry_points:
            continue
        candidates.append(rel)

    if not candidates:
        return []

    # Build the set of module names that are imported from all .py files
    imported_modules: set[str] = set()
    for rel in py_files:
        full = os.path.join(workspace, rel)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=full)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)

    # For each candidate, check if any import could refer to it
    orphaned: list[str] = []
    for rel in candidates:
        # Build possible module names for this file
        # e.g. "src/utils.py" -> {"src.utils", "utils"}
        module_names: set[str] = set()
        no_ext = rel.rsplit(".py", 1)[0]  # strip .py
        dotted = no_ext.replace(os.sep, ".").replace("/", ".")
        module_names.add(dotted)
        # Also add just the basename without extension
        basename_no_ext = os.path.basename(no_ext)
        module_names.add(basename_no_ext)
        # Add all suffix segments: "a.b.c" -> {"a.b.c", "b.c", "c"}
        parts = dotted.split(".")
        for i in range(len(parts)):
            module_names.add(".".join(parts[i:]))

        is_imported = False
        for mod in imported_modules:
            # Check if any imported module matches or is a prefix of this file
            if mod in module_names:
                is_imported = True
                break
            # Also check if this file's dotted path starts with the import
            # (handles "from src import utils" where src.utils is the file)
            for mn in module_names:
                if mn.startswith(mod + ".") or mod.startswith(mn + "."):
                    is_imported = True
                    break
            if is_imported:
                break

        if not is_imported:
            orphaned.append(rel)

    return orphaned


def check_cross_module_imports(workspace: str) -> list[dict]:
    """Validate that cross-module imports between generated Python files resolve.

    Finds all .py files in the workspace, parses ImportFrom nodes, and checks
    that each imported name actually exists in the target module's top-level
    namespace.

    Returns a list of dicts with keys: file, line, imports, from_module,
    severity, description.
    """
    skip_dirs = {".uas_state", ".git", "__pycache__", "venv", ".venv",
                 "node_modules", ".tox", ".eggs"}
    py_files = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if fname.endswith(".py"):
                py_files.append(os.path.join(root, fname))

    errors = []
    for fpath in py_files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=fpath)
        except Exception:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue

            # Resolve the target module file
            target_path = _resolve_import_module(
                workspace, fpath, node.module, node.level or 0
            )
            if target_path is None:
                continue

            # Extract the target module's public API
            target_api = extract_module_api(target_path)
            if not target_api:
                # Module exists but has no extractable API (empty or parse
                # error) -- skip rather than false-positive.
                continue

            all_names = set()
            for names in target_api.values():
                all_names.update(names)

            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name not in all_names:
                    rel_file = os.path.relpath(fpath, workspace)
                    rel_target = os.path.relpath(target_path, workspace)
                    available = sorted(all_names)
                    errors.append({
                        "file": rel_file,
                        "line": node.lineno,
                        "imports": alias.name,
                        "from_module": node.module,
                        "severity": "error",
                        "description": (
                            f"name '{alias.name}' not found in "
                            f"{rel_target}; available: "
                            f"{', '.join(available)}"
                        ),
                    })

    return errors


def _resolve_import_module(
    workspace: str, importing_file: str, module: str, level: int
) -> str | None:
    """Resolve a module name to a .py file path in the workspace.

    Handles both relative imports (level > 0) and absolute imports that
    correspond to local workspace files/packages.

    Returns the resolved file path, or None if the module is not local.
    """
    if level > 0:
        # Relative import: resolve from the importing file's directory
        base_dir = os.path.dirname(importing_file)
        for _ in range(level - 1):
            base_dir = os.path.dirname(base_dir)
        parts = module.split(".") if module else []
    else:
        # Absolute import: resolve from workspace root
        base_dir = workspace
        parts = module.split(".")

    # Try <base>/<parts>.py
    candidate = os.path.join(base_dir, *parts) + ".py"
    if os.path.isfile(candidate):
        return candidate

    # Try <base>/<parts>/__init__.py (package)
    candidate = os.path.join(base_dir, *parts, "__init__.py")
    if os.path.isfile(candidate):
        return candidate

    return None


PROJECT_STRUCTURE_PROMPT = """\
You are reviewing a project workspace to assess whether the right project \
artifacts are present.

**Goal:** {goal}

**Files in workspace:**
{file_list}

**Steps completed:**
{step_summaries}

Given the specific type of project being built, assess which artifacts are \
missing or unnecessary. Consider the project complexity: a single-file script \
needs fewer artifacts than a multi-file application.

Return ONLY valid JSON (no markdown fences):
{{"warnings": ["missing artifact description", ...], "suggestions": ["optional improvement", ...]}}

If everything looks appropriate for this project type, return \
{{"warnings": [], "suggestions": []}}.
"""


def check_project_guardrails_llm(workspace: str, goal: str,
                                 steps: list[dict]) -> list[str]:
    try:
        from orchestrator.llm_client import get_llm_client

        try:
            entries = os.listdir(workspace)
        except OSError:
            return check_project_guardrails(workspace)

        file_list = "\n".join(
            f"- {e}" for e in sorted(entries) if not e.startswith(".")
        )
        if not file_list:
            file_list = "(empty workspace)"

        step_summaries = "\n".join(
            f"- Step {i+1}: {s.get('title', 'untitled')} "
            f"[{s.get('status', 'unknown')}]"
            for i, s in enumerate(steps)
        )
        if not step_summaries:
            step_summaries = "(no steps)"

        client = get_llm_client(role="planner")
        prompt = PROJECT_STRUCTURE_PROMPT.format(
            goal=goal,
            file_list=file_list,
            step_summaries=step_summaries,
        )

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "project_structure_review"})
        response, _usage = client.generate(prompt)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "project_structure_review"})

        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()

        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            text = text[brace_start:brace_end + 1]

        result = json.loads(text)
        warnings = result.get("warnings", [])
        if not isinstance(warnings, list):
            logger.warning("LLM project structure review returned non-list "
                           "warnings, falling back to heuristic.")
            return check_project_guardrails(workspace)

        return [str(w)[:200] for w in warnings if isinstance(w, str)]

    except Exception as exc:
        logger.warning("LLM project structure review failed (%s), "
                       "falling back to heuristic.", exc)
        return check_project_guardrails(workspace)


WORKSPACE_VALIDATION_PROMPT = """\
You are reviewing a project workspace to assess whether the original goal \
has been achieved.

**Goal:** {goal}

**Files in workspace (with sizes):**
{file_listing}

**File content previews:**
{file_previews}

**Steps completed:**
{step_summaries}

Assess whether the produced output actually satisfies the original goal. \
Consider whether the files contain correct, complete content — not just \
whether they exist.

Return ONLY valid JSON (no markdown fences):
{{"goal_satisfied": true/false, "confidence": "high"|"medium"|"low", \
"issues": ["description of any problem", ...], \
"summary": "brief assessment of the workspace"}}
"""


def validate_workspace_llm(state: dict, workspace: str) -> dict | None:
    try:
        from orchestrator.llm_client import get_llm_client

        goal = state.get("goal", "")
        if not goal:
            return None

        try:
            ws_entries = [
                e for e in os.listdir(workspace) if not e.startswith(".")
            ]
        except OSError:
            return None

        file_listing = ""
        for entry in sorted(ws_entries):
            path = os.path.join(workspace, entry)
            try:
                size = os.path.getsize(path)
                file_listing += f"- {entry} ({size} bytes)\n"
            except OSError:
                file_listing += f"- {entry} (size unknown)\n"
        if not file_listing:
            file_listing = "(empty workspace)"

        file_previews = ""
        preview_count = 0
        for entry in sorted(ws_entries):
            if preview_count >= 5:
                break
            path = os.path.join(workspace, entry)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read(200)
                file_previews += f"### {entry}\n```\n{content}\n```\n\n"
                preview_count += 1
            except OSError:
                continue
        if not file_previews:
            file_previews = "(no readable files)"

        step_summaries = "\n".join(
            f"- Step {i+1}: {s.get('title', 'untitled')} "
            f"[{s.get('status', 'unknown')}]"
            for i, s in enumerate(state.get("steps", []))
        )
        if not step_summaries:
            step_summaries = "(no steps)"

        client = get_llm_client(role="planner")
        prompt = WORKSPACE_VALIDATION_PROMPT.format(
            goal=goal,
            file_listing=file_listing,
            file_previews=file_previews,
            step_summaries=step_summaries,
        )

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "workspace_validation"})
        response, _usage = client.generate(prompt)
        _accumulate_usage(state, _usage, model=client.model)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "workspace_validation"})

        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()

        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            text = text[brace_start:brace_end + 1]

        result = json.loads(text)
        return {
            "goal_satisfied": bool(result.get("goal_satisfied", True)),
            "confidence": result.get("confidence", "low"),
            "issues": result.get("issues", []),
            "summary": result.get("summary", ""),
        }

    except Exception as exc:
        logger.warning("LLM workspace validation failed (%s), skipping.", exc)
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

    # Include the step description so the verification script knows the
    # exact rules the implementation follows (e.g. which column-name
    # patterns trigger specific cleaning rules).
    description_info = ""
    if step.get("description"):
        desc = step["description"]
        if len(desc) > 3000:
            desc = desc[:3000] + "\n... [truncated]"
        description_info = f"\n\nStep description (the spec the code was built from):\n{desc}"

    # Include source code of files produced by this step so the
    # verification script can write tests that match the actual
    # implementation (column-name patterns, function signatures, etc.).
    source_info = ""
    _MAX_SOURCE_CHARS = 12000
    _source_chars = 0
    if step.get("files_written"):
        source_parts = []
        for fpath in step["files_written"]:
            if not fpath.endswith(".py"):
                continue
            full = os.path.join(workspace, fpath)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as sf:
                    content = sf.read()
            except OSError:
                continue
            remaining = _MAX_SOURCE_CHARS - _source_chars
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[:remaining] + "\n... [truncated]"
            source_parts.append(f"\n--- {fpath} ---\n{content}")
            _source_chars += len(content)
        if source_parts:
            source_info = (
                "\n\nSource code of files produced by this step "
                "(use this to understand the ACTUAL implementation logic "
                "and write tests that match it — e.g. column-name patterns, "
                "conditional branches):"
                + "".join(source_parts)
            )

    # Build module availability context so verification scripts only
    # attempt to import functions that actually exist in the workspace.
    # Walk the full directory tree to discover modules in subpackages
    # (e.g. data/loader.py, dashboard/tabs/cohort.py) — not just root.
    module_info = ""
    _SKIP_SCAN_DIRS = {
        ".uas_state", ".git", "__pycache__", "node_modules", ".venv",
        "venv", ".tox", ".eggs", ".uas_auth",
    }
    try:
        module_lines = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [
                d for d in sorted(dirs) if d not in _SKIP_SCAN_DIRS
            ]
            for entry in sorted(files):
                if not entry.endswith(".py") or entry.startswith("."):
                    continue
                fpath = os.path.join(root, entry)
                if not os.path.isfile(fpath):
                    continue
                rel = os.path.relpath(fpath, workspace)
                api = extract_module_api(fpath)
                funcs = api.get("functions", [])
                classes = api.get("classes", [])
                exports = funcs + classes
                if exports:
                    module_lines.append(
                        f"  {rel}: exports {', '.join(exports)}"
                    )
                else:
                    module_lines.append(
                        f"  {rel}: no public functions or classes"
                    )
        if module_lines:
            module_info = (
                "\n\nWorkspace Python modules and their available exports:\n"
                + "\n".join(module_lines)
            )
    except OSError:
        pass

    task = (
        f"Write a Python verification script that checks the following:\n\n"
        f"Verification criteria: {verify}\n\n"
        f"Context:{files_info}{output_info}{description_info}{source_info}{module_info}\n\n"
        f"Requirements:\n"
        f"- Use workspace = os.environ.get('WORKSPACE', '/workspace')\n"
        f"- Print 'VERIFICATION PASSED' if all checks pass\n"
        f"- Print 'VERIFICATION FAILED: <reason>' and exit(1) if any check fails\n"
        f"- Be thorough but concise\n"
        f"- IMPORTANT: The verification script MUST be strictly READ-ONLY. "
        f"Do NOT write, modify, patch, or overwrite any source files. "
        f"Only read files and import modules to verify correctness.\n"
        f"- IMPORTANT: Only import functions that are listed in the module "
        f"exports above. If a function you need is NOT listed (not yet "
        f"implemented), build test data inline instead of importing it. "
        f"Never assume a function exists just because a module file is present.\n"
        f"- IMPORTANT: When building test data, use column names and values "
        f"that match the patterns defined in the step description and source "
        f"code. For example, if the code cleans 'FALSE' only in columns "
        f"matching a specific pattern, your test columns MUST match that "
        f"pattern. Do NOT put anomalies in columns where the code does not "
        f"handle them.\n"
        f"- IMPORTANT: When working with DataFrames, discover actual column "
        f"names from the data itself (e.g. df.columns.tolist()) rather than "
        f"guessing column names like 'PatientID'. Use the module exports "
        f"above to find the correct API to load data.\n"
    )

    result = run_orchestrator(task)

    stdout = extract_sandbox_stdout(result.get("stderr", ""))
    all_output = (stdout or "") + (result.get("stdout", "") or "")

    if result["exit_code"] == 0 and "VERIFICATION PASSED" in all_output:
        return None

    # If the orchestrator exited cleanly but produced no meaningful output,
    # the verification is inconclusive (e.g. LLM failed to generate a script,
    # sandbox produced no output).  Treat as a pass rather than penalising
    # the step for an infrastructure issue.  Non-zero exits (crashes,
    # timeouts) are still treated as failures.
    if (result["exit_code"] == 0
            and not all_output.strip()
            and "VERIFICATION FAILED" not in all_output):
        logger.warning(
            "  Verification produced no output — treating as inconclusive (pass)."
        )
        return None

    error = stdout or result.get("stderr", "") or "Verification script failed"
    return error[:MAX_ERROR_LENGTH or None]


# ---------------------------------------------------------------------------
# Entry-point smoke test
# ---------------------------------------------------------------------------

_ENTRY_POINT_NAMES = {"app.py", "main.py", "run.py", "server.py", "dashboard.py"}
_LAUNCHER_SCRIPTS = {"run.sh", "start.sh", "launch.sh"}
_SKIP_DIRS = {".uas_state", ".git", "__pycache__", "venv", ".venv", "node_modules",
              ".tox", ".eggs"}


def _find_entry_points(workspace: str) -> list[str]:
    """Identify likely application entry-point files in *workspace*.

    Detection order (first match wins within each category):
    1. File referenced in a launcher script (``run.sh`` etc.)
    2. Files with a ``if __name__ == "__main__"`` guard
    3. Well-known filenames (``app.py``, ``main.py``, …)
    """
    candidates: list[str] = []

    # 1. Check launcher scripts for referenced Python files
    for launcher in _LAUNCHER_SCRIPTS:
        launcher_path = os.path.join(workspace, launcher)
        if os.path.isfile(launcher_path):
            try:
                with open(launcher_path, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                for match in re.finditer(r'python[3]?\s+["\']?(\S+\.py)', content):
                    py_file = match.group(1)
                    full = os.path.join(workspace, py_file)
                    if os.path.isfile(full) and py_file not in candidates:
                        candidates.append(py_file)
            except OSError:
                pass

    # 2. Walk workspace looking for __main__ guards and well-known names
    main_guard_files: list[str] = []
    well_known_files: list[str] = []

    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fname), workspace)
            # Check for __main__ guard
            full = os.path.join(dirpath, fname)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
                if re.search(r'''if\s+__name__\s*==\s*['"]__main__['"]''', source):
                    if rel not in candidates:
                        main_guard_files.append(rel)
            except OSError:
                pass
            # Check well-known names
            if fname in _ENTRY_POINT_NAMES and rel not in candidates:
                well_known_files.append(rel)

    candidates.extend(main_guard_files)
    for wk in well_known_files:
        if wk not in candidates:
            candidates.append(wk)

    return candidates


def smoke_test_entry_point(workspace: str, state: dict) -> str | None:
    """Attempt a dry import of the project's entry point(s).

    Returns ``None`` if all entry points import successfully, or a string
    describing the first import failure encountered.
    """
    entry_points = _find_entry_points(workspace)
    if not entry_points:
        logger.debug("smoke_test_entry_point: no entry points found, skipping")
        return None

    for ep in entry_points:
        # Convert file path to module name (e.g. "src/app.py" → "src.app")
        module = ep.replace(os.sep, ".").replace("/", ".")
        if module.endswith(".py"):
            module = module[:-3]

        try:
            result = subprocess.run(
                [
                    sys.executable, "-c",
                    f"import sys; sys.path.insert(0, {workspace!r}); import {module}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                cwd=workspace,
            )
        except subprocess.TimeoutExpired:
            return f"Smoke test timed out importing {ep}"
        except OSError as exc:
            return f"Smoke test could not run for {ep}: {exc}"

        if result.returncode != 0:
            tb = (result.stderr or result.stdout or "unknown error").strip()
            return f"Failed to import {ep} (module '{module}'):\n{tb}"

    return None


def validate_workspace(state: dict, workspace: str, *,
                       state_root: str = "") -> dict:
    """Final validation after all steps complete.

    Checks that claimed files exist and workspace isn't empty.
    Writes VALIDATION.md to the workspace summarizing what was produced.

    Args:
        state: Run state dict.
        workspace: Path to inspect for project files.
        state_root: Path where ``.uas_state/`` lives.  Defaults to *workspace*.
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
    if not MINIMAL_MODE:
        bp_warnings = check_project_guardrails_llm(
            workspace, state.get("goal", ""), state.get("steps", [])
        )
    else:
        bp_warnings = check_project_guardrails(workspace)
    if bp_warnings:
        lines.append("## Best Practice Warnings\n\n")
        for w in bp_warnings:
            lines.append(f"- {w}\n")
        lines.append("\n")

    # Cross-module import validation
    cross_module_errors = check_cross_module_imports(workspace)
    if cross_module_errors:
        lines.append("## Cross-Module Import Errors\n\n")
        for err in cross_module_errors:
            lines.append(
                f"- `{err['file']}` line {err['line']}: "
                f"`from {err['from_module']} import {err['imports']}` — "
                f"{err['description']}\n"
            )
        lines.append("\n")

    # Entry-point smoke test — attempt a dry import of the application
    launch_test_error = smoke_test_entry_point(workspace, state)
    if launch_test_error:
        lines.append("## Launch Test\n\n")
        lines.append(f"Entry-point import failed:\n\n```\n{launch_test_error}\n```\n\n")
        logger.warning("Smoke test failed: %s", launch_test_error.splitlines()[0])

        # Remediation: if the failure is an ImportError, identify the step
        # that produced the broken module and flag it for re-execution.
        if "ImportError" in launch_test_error or "ModuleNotFoundError" in launch_test_error:
            # Extract the failing module from the traceback
            failing_module = None
            for tb_line in reversed(launch_test_error.splitlines()):
                m = re.search(r'File "([^"]+)"', tb_line)
                if m:
                    fpath = m.group(1)
                    try:
                        failing_module = os.path.relpath(fpath, workspace)
                    except ValueError:
                        failing_module = fpath
                    break

            if failing_module:
                for step in state.get("steps", []):
                    if failing_module in step.get("files_written", []):
                        lines.append(
                            f"**Remediation:** Step {step['id']} "
                            f"(\"{step['title']}\") produced `{failing_module}` "
                            f"which has a broken import. Consider re-running "
                            f"this step with the import error as context.\n\n"
                        )
                        logger.warning(
                            "  Broken module '%s' was produced by step %d (%s)",
                            failing_module, step["id"], step["title"],
                        )
                        break

    llm_validation = None
    if not MINIMAL_MODE:
        llm_validation = validate_workspace_llm(state, workspace)
        if llm_validation:
            lines.append("## Goal Assessment (LLM)\n\n")
            satisfied = llm_validation.get("goal_satisfied", True)
            confidence = llm_validation.get("confidence", "unknown")
            summary = llm_validation.get("summary", "")
            lines.append(
                f"- **Goal satisfied:** {'Yes' if satisfied else 'No'} "
                f"(confidence: {confidence})\n"
            )
            if summary:
                lines.append(f"- **Summary:** {summary}\n")
            issues = llm_validation.get("issues", [])
            if issues:
                lines.append("- **Issues:**\n")
                for issue in issues:
                    lines.append(f"  - {issue}\n")
            lines.append("\n")

    _val_state_root = state_root or workspace
    _val_state_dir = os.path.join(_val_state_root, ".uas_state")
    try:
        os.makedirs(_val_state_dir, exist_ok=True)
        validation_path = os.path.join(_val_state_dir, "validation.md")
        with open(validation_path, "w") as f:
            f.writelines(lines)
        logger.info("Validation report written to %s", validation_path)
    except OSError as e:
        logger.warning("Could not write validation.md: %s", e)

    validation_data = {
        "missing_files": missing_files,
        "workspace_empty": len(ws_entries) == 0,
        "best_practice_warnings": bp_warnings,
        "cross_module_errors": cross_module_errors,
        "launch_test_error": launch_test_error,
    }
    if llm_validation:
        validation_data["llm_assessment"] = llm_validation
    # Store validation data in state for programmatic access
    state["validation"] = validation_data
    return validation_data


# ---------------------------------------------------------------------------
# Holistic end-of-run workspace validation (Section 13 of PLAN.md)
# ---------------------------------------------------------------------------

def _check_readme_accuracy(workspace: str) -> list[str]:
    """Check that file paths and commands referenced in README actually exist."""
    issues: list[str] = []
    readme_path = None
    for name in ("README.md", "README.rst", "README.txt", "README"):
        candidate = os.path.join(workspace, name)
        if os.path.isfile(candidate):
            readme_path = candidate
            break
    if readme_path is None:
        return issues

    try:
        with open(readme_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return issues

    seen: set[str] = set()

    # 1. Shell commands: ``python script.py`` or ``python3 path/to/script.py``
    for m in re.finditer(r"python[3]?\s+[\"']?([^\s\"'`]+\.py)", content):
        script = m.group(1)
        if script in seen:
            continue
        seen.add(script)
        if not os.path.isfile(os.path.join(workspace, script)):
            issues.append(
                f"README references script `{script}` "
                f"(via python command) but it does not exist"
            )

    # 2. File paths in backticks that contain a directory separator
    for m in re.finditer(r"`([^`\n]+)`", content):
        candidate = m.group(1).strip()
        if not ("/" in candidate or os.sep in candidate):
            continue
        if candidate.startswith(("http://", "https://", "$", "#", "-")):
            continue
        path_part = candidate.split()[0] if " " in candidate else candidate
        if not re.match(r"^[\w./-]+\.\w+$", path_part):
            continue
        if path_part in seen:
            continue
        seen.add(path_part)
        if not os.path.isfile(os.path.join(workspace, path_part)):
            issues.append(
                f"README references path `{path_part}` but it does not exist"
            )

    return issues


def _check_import_resolution(workspace: str) -> list[str]:
    """Verify that local Python imports resolve to existing modules."""
    _skip = {".uas_state", ".git", "__pycache__", "venv", ".venv",
             "node_modules", ".tox", ".eggs"}
    issues: list[str] = []

    py_files: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _skip]
        for fname in files:
            if fname.endswith(".py"):
                py_files.append(os.path.join(root, fname))

    # Identify local top-level packages and modules
    local_packages: set[str] = set()
    try:
        entries = os.listdir(workspace)
    except OSError:
        return issues
    for entry in entries:
        pkg_init = os.path.join(workspace, entry, "__init__.py")
        if os.path.isdir(os.path.join(workspace, entry)) and os.path.isfile(pkg_init):
            local_packages.add(entry)
    for entry in entries:
        if entry.endswith(".py") and entry != "__init__.py":
            local_packages.add(entry[:-3])

    for fpath in py_files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source, filename=fpath)
        except Exception:
            continue

        rel_file = os.path.relpath(fpath, workspace)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            level = node.level or 0
            if level > 0:
                resolved = _resolve_import_module(
                    workspace, fpath, node.module, level
                )
                if resolved is None:
                    issues.append(
                        f"`{rel_file}` line {node.lineno}: relative import "
                        f"`from {'.' * level}{node.module}` cannot be resolved"
                    )
            else:
                top = node.module.split(".")[0]
                if top in local_packages:
                    resolved = _resolve_import_module(
                        workspace, fpath, node.module, 0
                    )
                    if resolved is None:
                        issues.append(
                            f"`{rel_file}` line {node.lineno}: import "
                            f"`from {node.module}` targets local package "
                            f"but module not found"
                        )

    return issues


def _check_orphaned_files(workspace: str, state: dict) -> list[str]:
    """Find files on disk that no step claims as output."""
    issues: list[str] = []

    claimed: set[str] = set()
    for step in state.get("steps", []):
        for f in step.get("files_written", []):
            claimed.add(os.path.normpath(f))

    expected_roots = {
        ".gitignore", "pyproject.toml", "setup.py", "setup.cfg",
        "requirements.txt", "Pipfile", "poetry.lock", "package.json",
        "Makefile", "Dockerfile", "docker-compose.yml", "Containerfile",
        "LICENSE", "MANIFEST.in", "tox.ini", ".flake8",
        ".pre-commit-config.yaml", "pytest.ini", "mypy.ini", ".mypy.ini",
    }
    expected_lower_prefixes = ("readme", "changelog", "contributing", "authors")

    _skip = {".git", ".uas_state", ".uas_auth", "__pycache__",
             "node_modules", "venv", ".venv", ".tox", ".eggs"}

    orphans: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _skip]
        for fname in files:
            rel = os.path.relpath(os.path.join(root, fname), workspace)
            normed = os.path.normpath(rel)
            if normed in expected_roots:
                continue
            if any(normed.lower().startswith(p) for p in expected_lower_prefixes):
                continue
            if fname.startswith(".") or fname.endswith(".pyc"):
                continue
            if normed not in claimed:
                orphans.append(rel)

    if orphans:
        shown = orphans[:10]
        suffix = f" (and {len(orphans) - 10} more)" if len(orphans) > 10 else ""
        issues.append(
            f"{len(orphans)} orphaned file(s) not claimed by any step: "
            + ", ".join(f"`{f}`" for f in shown) + suffix
        )

    return issues


def _check_entry_points(workspace: str) -> list[str]:
    """Verify entry points declared in pyproject.toml exist."""
    issues: list[str] = []

    pyproject_path = os.path.join(workspace, "pyproject.toml")
    if not os.path.isfile(pyproject_path):
        return issues

    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError:
                return _check_entry_points_regex(workspace, pyproject_path)
    except Exception:
        return issues

    try:
        with open(pyproject_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return issues

    for section_name in ("scripts", "gui-scripts"):
        scripts = data.get("project", {}).get(section_name, {})
        for name, target in scripts.items():
            module_part = target.split(":")[0] if ":" in target else target
            module_path = module_part.replace(".", os.sep)
            as_file = os.path.join(workspace, module_path + ".py")
            as_pkg = os.path.join(workspace, module_path, "__init__.py")
            if not os.path.isfile(as_file) and not os.path.isfile(as_pkg):
                issues.append(
                    f"pyproject.toml [{section_name}] entry `{name}` "
                    f"targets `{target}` but module `{module_part}` not found"
                )

    poetry_scripts = data.get("tool", {}).get("poetry", {}).get("scripts", {})
    for name, target in poetry_scripts.items():
        module_part = target.split(":")[0] if ":" in target else target
        module_path = module_part.replace(".", os.sep)
        as_file = os.path.join(workspace, module_path + ".py")
        as_pkg = os.path.join(workspace, module_path, "__init__.py")
        if not os.path.isfile(as_file) and not os.path.isfile(as_pkg):
            issues.append(
                f"pyproject.toml [tool.poetry.scripts] entry `{name}` "
                f"targets `{target}` but module `{module_part}` not found"
            )

    return issues


def _check_entry_points_regex(
    workspace: str, pyproject_path: str,
) -> list[str]:
    """Fallback entry-point check when no TOML parser is available."""
    issues: list[str] = []
    try:
        with open(pyproject_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return issues

    in_scripts = False
    for line in content.splitlines():
        stripped = line.strip()
        if re.match(r"\[project\.(?:gui-)?scripts\]", stripped):
            in_scripts = True
            continue
        if stripped.startswith("["):
            in_scripts = False
            continue
        if in_scripts:
            m = re.match(r'(\w+)\s*=\s*"([^"]+)"', stripped)
            if m:
                name, target = m.group(1), m.group(2)
                module_part = target.split(":")[0] if ":" in target else target
                module_path = module_part.replace(".", os.sep)
                as_file = os.path.join(workspace, module_path + ".py")
                as_pkg = os.path.join(workspace, module_path, "__init__.py")
                if not os.path.isfile(as_file) and not os.path.isfile(as_pkg):
                    issues.append(
                        f"pyproject.toml scripts entry `{name}` targets "
                        f"`{target}` but module `{module_part}` not found"
                    )

    return issues


def holistic_validation(workspace: str, state: dict) -> list[str]:
    """Check project-wide coherence after all steps complete.

    Returns a list of issue strings covering:
    - README references to non-existent files
    - Unresolvable local Python imports
    - Orphaned files not claimed by any step
    - Missing entry-point targets

    Section 13 of PLAN.md.
    """
    issues: list[str] = []
    issues.extend(_check_readme_accuracy(workspace))
    issues.extend(_check_import_resolution(workspace))
    issues.extend(_check_orphaned_files(workspace, state))
    issues.extend(_check_entry_points(workspace))
    return issues


META_LEARNING_PROMPT = """\
You are performing a post-run analysis of an automated code generation pipeline run.

<goal>
{goal}
</goal>

<step_outcomes>
{step_outcomes}
</step_outcomes>

<run_stats>
Total elapsed time: {total_elapsed:.1f} seconds
Replanning events: {replan_count}
</run_stats>

Analyze the run holistically. Identify systemic patterns — not per-step issues, \
but recurring themes across the run (e.g., "decomposition produced too many steps", \
"API integration steps consistently required multiple retries", \
"dependency errors were caused by version mismatches").

Return ONLY valid JSON (no markdown fences):
{{"systemic_lessons": [{{"pattern": "description of the pattern", \
"recommendation": "what to do differently next time"}}], \
"decomposition_feedback": "brief assessment of how well the goal was decomposed", \
"knowledge_to_persist": [{{"key": "short label", "value": "lesson learned"}}]}}
"""


def post_run_meta_learning(state: dict) -> dict | None:
    try:
        from orchestrator.llm_client import get_llm_client

        goal = state.get("goal", "")
        if not goal:
            return None

        steps = state.get("steps", [])
        if not steps:
            return None

        step_lines = []
        for i, s in enumerate(steps):
            error_types = []
            for r in s.get("reflections", []):
                et = r.get("error_type", "")
                if et:
                    error_types.append(et)
            step_lines.append(
                f"- Step {i+1}: {s.get('title', 'untitled')} | "
                f"status={s.get('status', 'unknown')} | "
                f"attempts={s.get('spec_attempt', 0)} | "
                f"errors=[{', '.join(error_types)}]"
            )
        step_outcomes = "\n".join(step_lines) if step_lines else "(no steps)"

        replan_count = sum(
            1 for e in get_event_log().events
            if hasattr(e, "event_type") and "replan" in str(getattr(e, "event_type", "")).lower()
        )

        client = get_llm_client(role="planner")
        prompt = META_LEARNING_PROMPT.format(
            goal=goal,
            step_outcomes=step_outcomes,
            total_elapsed=state.get("total_elapsed", 0.0),
            replan_count=replan_count,
        )

        event_log = get_event_log()
        event_log.emit(EventType.LLM_CALL_START,
                       data={"purpose": "meta_learning"})
        response, _usage = client.generate(prompt)
        _accumulate_usage(state, _usage, model=client.model)
        event_log.emit(EventType.LLM_CALL_COMPLETE,
                       data={"purpose": "meta_learning"})

        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.startswith("```")]
            text = "\n".join(lines).strip()

        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end != -1:
            text = text[brace_start:brace_end + 1]

        data = json.loads(text)

        lessons = data.get("systemic_lessons", [])
        knowledge_items = data.get("knowledge_to_persist", [])
        decomp_feedback = data.get("decomposition_feedback", "")

        for item in knowledge_items:
            key = item.get("key", "")
            value = item.get("value", "")
            if key and value:
                append_knowledge("lesson", {
                    "source": "meta_learning",
                    "key": key,
                    "value": value,
                })

        scratchpad_lines = ["### Post-Run Meta-Learning"]
        if decomp_feedback:
            scratchpad_lines.append(f"Decomposition: {decomp_feedback}")
        for lesson in lessons:
            pattern = lesson.get("pattern", "")
            rec = lesson.get("recommendation", "")
            if pattern:
                scratchpad_lines.append(f"- {pattern}: {rec}")
        append_scratchpad(
            "\n".join(scratchpad_lines),
            run_id=state.get("run_id", ""),
        )

        logger.info("Post-run meta-learning: %d lessons, %d knowledge items",
                     len(lessons), len(knowledge_items))

        return {
            "systemic_lessons": lessons,
            "decomposition_feedback": decomp_feedback,
            "knowledge_to_persist": knowledge_items,
        }

    except Exception as exc:
        logger.debug("Post-run meta-learning failed (%s), skipping.", exc)
        return None


def _finalize_code_tracking(run_id: str = ""):
    """Load code versions from disk and record provenance links."""
    tracker = get_code_tracker()
    if run_id:
        cv_dir = os.path.join(get_run_dir(run_id), "code_versions")
    else:
        cv_dir = os.path.join(WORKSPACE, ".uas_state", "code_versions")
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
                 backtracked_steps: set | None = None,
                 hooks: list | None = None) -> bool:
    """Execute a single step, with spec rewrite retries.

    Returns True on success, False on unrecoverable failure.

    Args:
        backtracked_steps: Set of step IDs already backtracked to (Section 3d).
            Used to limit backtracking depth to 1 and avoid re-backtracking.
        hooks: Hook configurations loaded from .uas/hooks.toml.
    """
    total = len(state["steps"])
    run_id = state.get("run_id", "")
    _hooks = hooks or []
    _probe_environment(run_id=run_id)
    context = build_context(step, completed_outputs, state=state,
                            workspace_path=PROJECT_DIR)

    # Section 3: Check input quality before code generation
    input_warnings = check_input_quality(step, state, PROJECT_DIR)
    if input_warnings:
        for iw in input_warnings:
            logger.info("  Input quality issue: %s", iw)
        warning_block = "\n".join(f"- {w}" for w in input_warnings)
        context = (
            "<data_quality_warnings>\n"
            "WARNING: Dependency output has quality issues:\n"
            f"{warning_block}\n"
            "Consider whether these indicate a bug in the upstream step. "
            "If the data is fundamentally broken, this step should report "
            "the issue rather than work around it.\n"
            "</data_quality_warnings>\n\n"
            + context
        )

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
        "workspace_name": os.path.basename(PROJECT_DIR),
    }

    event_log = get_event_log()
    prov = get_provenance_graph()

    # Section 8: PRE_STEP hook — may abort the step
    if _hooks:
        hook_result = run_hook(HookEvent.PRE_STEP, {
            "step_id": step["id"],
            "title": step["title"],
            "description": step.get("description", ""),
        }, _hooks)
        if hook_result and hook_result.get("abort"):
            reason = hook_result.get("reason", "aborted by PRE_STEP hook")
            logger.warning("  Step %d aborted by hook: %s", step["id"], reason)
            step["status"] = "failed"
            step["error"] = reason
            step["elapsed"] = 0.0
            event_log.emit(EventType.STEP_FAILED, step_id=step["id"],
                           data={"error": reason, "hook_abort": True})
            return False

    event_log.emit(EventType.STEP_START, step_id=step["id"],
                   data={"title": step["title"]})
    prev_error_entity = None
    attempt_history = []  # Track prior attempts for reflection (Section 1c)
    consecutive_failures = 0  # 3-strike rollback counter (Phase 3.5)

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
        specs_dir = get_specs_dir(run_id) if run_id else ""
        spec_file = generate_spec(step, total, context, specs_dir=specs_dir)
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
            "UAS_RUN_ID": run_id,
        }
        # Section 19: Signal truncation history to orchestrator so it can
        # add code-length guidance to the prompt.
        truncation_detected = any(
            r.get("error_type") == "format_error"
            and "truncat" in (r.get("root_cause", "") + r.get("lesson", "")).lower()
            for r in step.get("reflections", [])
        )
        if truncation_detected:
            extra_env["UAS_TRUNCATION_DETECTED"] = "1"
        # Pass step's environment/package requirements to the orchestrator
        # so build_prompt() can include explicit pip install instructions.
        if step.get("environment"):
            extra_env["UAS_STEP_ENVIRONMENT"] = json.dumps(step["environment"])
        # Section 10: Take a full recursive workspace snapshot before step
        # execution.  After the step completes, any new file not claimed by
        # the step's ``files_written`` is treated as an artifact and removed.
        pre_snapshot = snapshot_workspace(PROJECT_DIR)
        # Keep a flat root-level set for the legacy cleanup_workspace_artifacts
        # path (used as a fallback for UAS_RESULT script artifact removal).
        pre_step_files = {
            f for f in pre_snapshot if os.sep not in f and "/" not in f
        }
        # Scan workspace files for orchestrator prompt context (Section 1a)
        ws_files = scan_workspace_files(PROJECT_DIR)
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

        # Section 1: Parse orchestrator token usage from its stderr log.
        _orch_usage_marker = "__UAS_ORCH_USAGE__:"
        for _line in (result.get("stderr") or "").splitlines():
            if _orch_usage_marker in _line:
                try:
                    _payload = _line.split(_orch_usage_marker, 1)[1]
                    _ou = json.loads(_payload)
                    _accumulate_usage(state, {
                        "input": _ou.get("input", 0),
                        "output": _ou.get("output", 0),
                    }, step=step)
                except (json.JSONDecodeError, IndexError):
                    pass
                break

        logger.info("  Orchestrator exit code: %s (%.1fs)", result["exit_code"], orch_elapsed)
        if dashboard:
            status = "succeeded" if result["exit_code"] == 0 else "failed"
            dashboard.log(
                f"Step {step['id']} orchestrator {status} "
                f"(exit {result['exit_code']}, {orch_elapsed:.1f}s)"
            )

        # Rate-limit / usage-limit / capacity detection: if the
        # orchestrator failed due to a retryable error, classify it and
        # wait before retrying — without burning a spec rewrite attempt.
        rate_limit_retries = 0
        retry_start_time = time.monotonic()
        while result["exit_code"] != 0:
            combined_output = (
                (result.get("stderr") or "") + " " + (result.get("stdout") or "")
            )
            err = classify_error(
                result["exit_code"], result.get("stdout") or "",
                result.get("stderr") or "",
            )
            # Only retry retryable error categories.
            if not err.retryable:
                break

            # Map category to wait/limit config.
            is_usage = _is_usage_limited(combined_output)
            max_retries = MAX_USAGE_LIMIT_RETRIES if is_usage else MAX_RATE_LIMIT_RETRIES
            if not PERSISTENT_RETRY and rate_limit_retries >= max_retries:
                break
            rate_limit_retries += 1

            # Persistent retry: reset backoff multiplier after 6 hours.
            if PERSISTENT_RETRY and time.monotonic() - retry_start_time > PERSISTENT_RETRY_RESET:
                rate_limit_retries = 1
                retry_start_time = time.monotonic()
                logger.info("  Persistent retry: resetting backoff multiplier after 6h")

            # Emit structured error event.
            event_log.emit(
                EventType.LLM_ERROR,
                step_id=step["id"],
                data={"category": err.category, "message": err.message,
                      "retry": rate_limit_retries, "max_retries": max_retries},
            )

            if is_usage:
                wait = USAGE_LIMIT_WAIT
                label = f"Usage limit ({err.category})"
            else:
                wait = min(
                    RATE_LIMIT_BASE_WAIT * (2 ** (rate_limit_retries - 1)),
                    RATE_LIMIT_MAX_WAIT,
                )
                label = err.category.replace("_", " ").title()
            # Cap backoff in persistent retry mode.
            if PERSISTENT_RETRY:
                wait = min(wait, MAX_BACKOFF)
            logger.warning(
                "  %s detected for step %s. "
                "Waiting %ds before retry %d/%s...",
                label, step["id"], wait, rate_limit_retries,
                "∞" if PERSISTENT_RETRY else str(max_retries),
            )
            if PERSISTENT_RETRY:
                event_log.emit(
                    EventType.PERSISTENT_RETRY_WAIT,
                    step_id=step["id"],
                    data={"category": err.category, "wait_seconds": wait,
                          "retry": rate_limit_retries},
                )
            if dashboard:
                dashboard.set_step_activity(
                    step["id"],
                    f"{label} — waiting {wait}s...",
                )
            step["status"] = "pending"
            _save_state_threadsafe(state)
            if PERSISTENT_RETRY:
                _sleep_with_heartbeat(
                    wait, f"Persistent retry step {step['id']} ({err.category})")
            else:
                time.sleep(wait)
            # Re-run the orchestrator with the same spec
            logger.info("  Retrying orchestrator after %s wait...", err.category)
            if dashboard:
                dashboard.set_step_activity(step["id"], "Running orchestrator...")
            step["status"] = "executing"
            _save_state_threadsafe(state)
            result = run_orchestrator(task, extra_env=extra_env,
                                      output_callback=output_cb,
                                      step_context=step_context)

        if result["exit_code"] == 0:
            step["output"] = extract_sandbox_stdout(result["stderr"])
            step["stderr_output"] = extract_sandbox_stderr(result["stderr"])
            step["files_written"] = _sanitize_files_written(
                extract_workspace_files(result["stderr"])
            )
            # Parse structured UAS_RESULT if present
            uas_result = parse_uas_result(result["stderr"])
            if uas_result:
                if uas_result.get("files_written"):
                    uas_result["files_written"] = _sanitize_files_written(
                        uas_result["files_written"]
                    )
                step["uas_result"] = uas_result
                if uas_result.get("files_written"):
                    step["files_written"] = list(set(
                        step["files_written"] + uas_result["files_written"]
                    ))
                if uas_result.get("summary"):
                    step["summary"] = uas_result["summary"]

            # Post-execution validation
            failure_reason = validate_uas_result(step, PROJECT_DIR)

            # Section 16: Output quality checks
            if failure_reason is None:
                quality_issues = check_output_quality(step, PROJECT_DIR)
                if quality_issues:
                    for qi in quality_issues:
                        logger.info("  Output quality issue: %s", qi)
                    issue_list = "\n".join(f"- {qi}" for qi in quality_issues)
                    failure_reason = (
                        "Your script reported success but produced invalid output:\n"
                        f"{issue_list}\n"
                        "Fix the output and try again."
                    )

            # Section 10: Recursive diff cleanup — remove any new file that
            # the step did not claim as output.  This replaces the old
            # root-level-only cleanup_workspace_artifacts for artifact removal.
            _step_output_set = set(step.get("files_written", []))
            _artifact_removed = cleanup_step_artifacts(
                PROJECT_DIR,
                pre_snapshot=pre_snapshot,
                step_output_files=_step_output_set,
            )
            if _artifact_removed:
                logger.info("  Removed %d step artifact(s)", len(_artifact_removed))
            # Legacy: still clean __pycache__ / .pyc via the old function.
            cleanup_workspace_artifacts(PROJECT_DIR)

            if failure_reason is None and step.get("verify"):
                logger.info("  Verifying step output...")
                if dashboard:
                    dashboard.set_step_activity(step["id"], "Verifying output...")
                event_log.emit(EventType.VERIFICATION_START,
                               step_id=step["id"])
                failure_reason = verify_step_output(step, PROJECT_DIR)
                event_log.emit(
                    EventType.VERIFICATION_COMPLETE,
                    step_id=step["id"],
                    data={"passed": failure_reason is None},
                )
                # Section 10: Cleanup again after verification orchestrator
                # which may have created new script artifacts.
                _verify_removed = cleanup_step_artifacts(
                    PROJECT_DIR,
                    pre_snapshot=pre_snapshot,
                    step_output_files=_step_output_set,
                )
                if _verify_removed:
                    logger.info(
                        "  Removed %d verification artifact(s)",
                        len(_verify_removed),
                    )
                cleanup_workspace_artifacts(PROJECT_DIR)

            # Section 11: Detect and resolve nested project duplication.
            # A generated script may create project_name/src/ inside the
            # workspace that IS the project root, producing a nested copy.
            if failure_reason is None:
                _nested = detect_nested_duplication(PROJECT_DIR)
                if _nested:
                    logger.warning(
                        "  Nested duplication detected: %s/", _nested,
                    )
                    _promoted = resolve_nested_duplication(
                        PROJECT_DIR, _nested,
                    )
                    if _promoted:
                        logger.info(
                            "  Promoted %d item(s) from %s/ to root",
                            len(_promoted), _nested,
                        )
                        # Update files_written to strip the nested prefix
                        _prefix = _nested + "/"
                        step["files_written"] = [
                            f[len(_prefix):] if f.startswith(_prefix) else f
                            for f in step.get("files_written", [])
                        ]

            # Guardrail scan on workspace Python files
            if failure_reason is None:
                guardrail_warnings = []
                _use_llm_guardrails = (
                    not MINIMAL_MODE
                    and not config.get("no_llm_guardrails")
                )
                try:
                    for entry in os.listdir(PROJECT_DIR):
                        if entry.endswith(".py") and not entry.startswith("."):
                            fpath = os.path.join(PROJECT_DIR, entry)
                            if os.path.isfile(fpath):
                                with open(fpath, "r", errors="replace") as gf:
                                    code_content = gf.read()
                                violations = check_guardrails(code_content)
                                has_regex_errors = any(
                                    v["severity"] == "error"
                                    for v in violations
                                )
                                if _use_llm_guardrails and not has_regex_errors:
                                    violations = check_guardrails_llm(
                                        code_content
                                    )
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

            # Cross-module import check after per-file guardrails
            if failure_reason is None:
                try:
                    import_errors = check_cross_module_imports(PROJECT_DIR)
                    for err in import_errors:
                        if err["severity"] == "error":
                            failure_reason = (
                                f"Cross-module import error in {err['file']} "
                                f"line {err['line']}: {err['description']}"
                            )
                            break
                        guardrail_warnings.append(
                            f"{err['file']}:{err['line']}: "
                            f"{err['description']}"
                        )
                    if import_errors and failure_reason is None:
                        for w in guardrail_warnings:
                            logger.warning("  Import: %s", w)
                        step.setdefault("guardrail_warnings", []).extend(
                            guardrail_warnings
                        )
                except Exception as exc:
                    logger.debug("Cross-module import check failed: %s", exc)

            # Orphaned module check after cross-module import check
            if failure_reason is None:
                try:
                    orphaned = detect_orphaned_modules(PROJECT_DIR)
                    step_files = set(step.get("files_written", []))
                    for orph in orphaned:
                        if orph in step_files:
                            api = extract_module_api(
                                os.path.join(PROJECT_DIR, orph)
                            )
                            exports = []
                            for kind in ("functions", "classes", "constants", "variables"):
                                exports.extend(api.get(kind, []))
                            export_str = (
                                ", ".join(exports) if exports else "(none)"
                            )
                            msg = (
                                f"Orphaned module `{orph}` produced by step "
                                f"{step['id']} (\"{step['title']}\") is not "
                                f"imported by any other module; "
                                f"exports: {export_str}"
                            )
                            logger.warning("  Orphan: %s", msg)
                            step.setdefault("guardrail_warnings", []).append(
                                msg
                            )
                except Exception as exc:
                    logger.debug("Orphaned module check failed: %s", exc)

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
                # Section 1: Log per-step token/cost summary.
                _su = step.get("token_usage", {})
                _sc = step.get("cost_usd", 0.0)
                if _su.get("input") or _su.get("output"):
                    logger.info(
                        "  Step %s used %dk input + %dk output tokens ≈ $%.4f",
                        step["id"],
                        _su.get("input", 0) / 1000,
                        _su.get("output", 0) / 1000,
                        _sc,
                    )

                # Section 12: Update project manifest and remove stale files.
                _manifest = state.get("_manifest_obj")
                if _manifest is None:
                    _manifest_data = state.get("file_manifest", {})
                    _manifest = ProjectManifest.from_dict(_manifest_data)
                    state["_manifest_obj"] = _manifest
                _new_files = step.get("files_written", [])
                if _new_files:
                    _stale_removed = remove_superseded_files(
                        PROJECT_DIR, _manifest, step["id"], _new_files,
                    )
                    if _stale_removed:
                        logger.info(
                            "  Removed %d stale file(s) superseded by step %d",
                            len(_stale_removed), step["id"],
                        )
                    _manifest.add_step_output(step["id"], _new_files)
                    state["file_manifest"] = _manifest.to_dict()
                    _save_state_threadsafe(state)

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
                    f"Summary: {summary}",
                    run_id=run_id,
                )
                # Section 4a: Update structured progress file
                update_progress_file(
                    state,
                    event=f"Step {step['id']} ({step['title']}) completed successfully",
                )

                # Section 8: Record knowledge from successful execution
                # Section 18: Skip knowledge base updates in minimal mode.
                if not MINIMAL_MODE:
                    step_output = step.get("output", "") or ""
                    step_stderr = step.get("stderr_output", "") or ""
                    combined_output = step_output + "\n" + step_stderr
                    installed_pkgs = _extract_installed_packages(combined_output)
                    if installed_pkgs:
                        append_knowledge("package_version", installed_pkgs)
                    # Record lesson when a retry succeeded
                    if spec_attempt > 0:
                        reflections = step.get("reflections", [])
                        prev_error = (
                            reflections[-1].get("root_cause", "")
                            if reflections else ""
                        )
                        append_knowledge("lesson", {
                            "error_snippet": prev_error[:200],
                            "solution_snippet": step["description"][:200],
                            "step_title": step["title"],
                        })

                # Section 15: Git checkpoint after successful step
                # Section 18: Skip in minimal mode.
                if not MINIMAL_MODE:
                    git_checkpoint(WORKSPACE, step["id"], step["title"])

                # Section 8: POST_STEP hook
                if _hooks:
                    run_hook(HookEvent.POST_STEP, {
                        "step_id": step["id"],
                        "title": step["title"],
                        "status": "completed",
                        "elapsed": step.get("elapsed", 0.0),
                        "files_written": step.get("files_written", []),
                    }, _hooks)

                return True

            # Validation failed — treat as step failure
            error_info = failure_reason
            is_validation_failure = True
        else:
            # Execution failed
            error_info = result["stderr"] or result["stdout"] or "Unknown error"
            is_validation_failure = False

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
            f"Error: {error_info[:500]}",
            run_id=run_id,
        )
        # Section 4a: Update structured progress file
        update_progress_file(
            state,
            event=f"Step {step['id']} ({step['title']}) failed (attempt {spec_attempt + 1})",
        )

        # Section 3a: Generate structured reflection (before classification
        # so the reflection's LLM-generated error_type is available)
        # When the failure came from post-execution validation (e.g. the
        # verify_step_output check), error_info holds the verification
        # output.  Pass it explicitly so the reflection sees the *actual*
        # failure rather than the orchestrator's successful stderr.
        if is_validation_failure:
            _refl_stdout = step.get("output", "") or ""
            _refl_stderr = error_info
        else:
            _refl_stdout = result.get("stdout", "") or ""
            _refl_stderr = result.get("stderr", "") or error_info
        try:
            reflection = generate_reflection(
                step,
                _refl_stdout,
                _refl_stderr,
                attempt=spec_attempt + 1,
            )
        except Exception as e:
            logger.warning("  Reflection generation failed: %s", e)
            reflection = {
                "attempt": spec_attempt + 1,
                "error_type": classify_failure_heuristic(error_info),
                "root_cause": error_info[:200],
                "strategy_tried": f"attempt {spec_attempt + 1}",
                "lesson": "",
                "what_to_try_next": "",
            }

        # Store reflection in step state
        step.setdefault("reflections", []).append(reflection)
        _save_state_threadsafe(state)

        # Section 3b: Classify error using reflection's LLM-generated
        # error_type when available, falling back to keyword heuristic
        error_type = classify_failure(error_info, step_context=step)
        logger.info("  Error type: %s", error_type)

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
            f"lesson={reflection.get('lesson', '')[:150]}",
            run_id=run_id,
        )

        # Track attempt history for reflection (Section 1c)
        attempt_history.append({
            "attempt": spec_attempt + 1,
            "error": error_info[:300],
            "strategy": f"attempt {spec_attempt + 1}",
            "is_validation_failure": is_validation_failure,
        })

        # Phase 3.5: 3-strike rollback rule
        consecutive_failures += 1
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logger.info(
                "  Step %s hit %d consecutive failures. "
                "Rolling back to pre-step checkpoint.",
                step["id"], consecutive_failures,
            )
            rollback_to_checkpoint(WORKSPACE, step["id"])
            event_log.emit(EventType.STEP_FAILED, step_id=step["id"],
                           data={"error": "3-strike rollback",
                                 "consecutive_failures": consecutive_failures})
            break

        if spec_attempt < MAX_SPEC_REWRITES:
            # Section 3c: Root cause tracing and backtracking
            # Runs BEFORE retry budget check so backtracking is always
            # attempted even when stagnation is detected.
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

                # Handle missing dependency: add it and execute if needed
                if root_target == "missing_dependency" and dep_id is not None:
                    step_by_id = {s["id"]: s for s in state["steps"]}
                    missing_step = step_by_id.get(dep_id)
                    if missing_step:
                        logger.info(
                            "  Adding missing dependency: step %d (%s) "
                            "-> step %d depends_on.",
                            dep_id, missing_step.get("title", "?"),
                            step["id"],
                        )
                        step["depends_on"].append(dep_id)
                        _save_state_threadsafe(state)

                        # Execute the missing dependency if not completed
                        if missing_step["status"] != "completed":
                            logger.info(
                                "  Executing missing dependency step %d...",
                                dep_id,
                            )
                            if dashboard:
                                dashboard.set_step_activity(
                                    step["id"],
                                    f"Executing missing dep step {dep_id}...",
                                )
                            dep_success = execute_step(
                                missing_step, state, completed_outputs,
                                progress_counts, dashboard,
                                backtracked_steps, hooks=_hooks,
                            )
                            if dep_success:
                                completed_outputs[dep_id] = {
                                    "stdout": missing_step.get("output", ""),
                                    "stderr": missing_step.get(
                                        "stderr_output", ""),
                                    "files": missing_step.get(
                                        "files_written", []),
                                }
                                context = build_context(
                                    step, completed_outputs,
                                    state=state,
                                    workspace_path=PROJECT_DIR,
                                )
                                did_backtrack = True
                            else:
                                logger.warning(
                                    "  Missing dependency step %d failed.",
                                    dep_id,
                                )
                        else:
                            # Already completed, just refresh context
                            completed_outputs[dep_id] = {
                                "stdout": missing_step.get("output", ""),
                                "stderr": missing_step.get(
                                    "stderr_output", ""),
                                "files": missing_step.get(
                                    "files_written", []),
                            }
                            context = build_context(
                                step, completed_outputs,
                                state=state,
                                workspace_path=PROJECT_DIR,
                            )
                            did_backtrack = True

                # Determine which dependency to backtrack to:
                # either from root cause tracing or forced by stagnation
                backtrack_dep_id = None
                if (root_target == "dependency"
                        and dep_id is not None
                        and dep_id not in backtracked_steps):
                    backtrack_dep_id = dep_id
                elif (root_target == "self"
                        and _is_verification_stagnation(attempt_history)):
                    # Force backtracking: repeated validation failures
                    # with similar errors suggest upstream data is wrong,
                    # even though root cause tracing said SELF
                    force_dep_id = next(
                        (d for d in step["depends_on"]
                         if d not in backtracked_steps),
                        None,
                    )
                    if force_dep_id is not None:
                        logger.info(
                            "  Verification stagnation detected. "
                            "Force-backtracking to dep step %d...",
                            force_dep_id,
                        )
                        backtrack_dep_id = force_dep_id
                elif (root_target == "self"
                        and _has_data_quality_error(error_info)
                        and step["depends_on"]):
                    # Section 3: Data quality errors (all NaN, no valid
                    # data, constant column) strongly suggest the upstream
                    # dependency produced broken data. Backtrack immediately
                    # instead of waiting for stagnation.
                    force_dep_id = next(
                        (d for d in step["depends_on"]
                         if d not in backtracked_steps),
                        None,
                    )
                    if force_dep_id is not None:
                        logger.info(
                            "  Data quality issue detected in error. "
                            "Backtracking to dep step %d...",
                            force_dep_id,
                        )
                        backtrack_dep_id = force_dep_id

                # Section 3d: Backtracking (with informed description)
                if backtrack_dep_id is not None:
                    step_by_id = {s["id"]: s for s in state["steps"]}
                    dep_step = step_by_id.get(backtrack_dep_id)
                    if dep_step:
                        logger.info(
                            "  Root cause in dependency step %d. "
                            "Backtracking to re-execute...",
                            backtrack_dep_id,
                        )
                        backtracked_steps.add(backtrack_dep_id)
                        event_log.emit(EventType.BACKTRACK_START,
                                       step_id=step["id"],
                                       data={"backtrack_to": backtrack_dep_id})
                        if dashboard:
                            dashboard.set_step_activity(
                                step["id"],
                                f"Backtracking to step {backtrack_dep_id}...",
                            )
                            dashboard.log(
                                f"Step {step['id']}: root cause in "
                                f"step {backtrack_dep_id}, backtracking"
                            )

                        # Augment dependency description with downstream
                        # failure context so it knows what to fix
                        original_dep_desc = dep_step["description"]
                        verify_info = step.get("verify", "")
                        dep_step["description"] += (
                            f"\n\n--- DOWNSTREAM FAILURE FEEDBACK ---\n"
                            f"A downstream step (Step {step['id']}: "
                            f"{step['title']}) that consumes this step's "
                            f"output failed with:\n"
                            f"{error_info}\n"
                            + (f"Downstream verification criteria: "
                               f"{verify_info}\n"
                               if verify_info else "")
                            + "You MUST adjust your output to satisfy "
                            "these downstream requirements. This likely "
                            "requires a fundamentally different approach "
                            "(e.g., if generating simulated data, ensure "
                            "strong predictive signal between features "
                            "and target; if processing data, preserve "
                            "required structure and relationships).\n"
                            "--- END FEEDBACK ---"
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
                            backtracked_steps, hooks=_hooks,
                        )

                        # Restore original description
                        dep_step["description"] = original_dep_desc

                        event_log.emit(
                            EventType.BACKTRACK_COMPLETE,
                            step_id=step["id"],
                            data={"backtrack_to": backtrack_dep_id,
                                  "success": dep_success},
                        )

                        if dep_success:
                            # Update completed outputs from re-executed dep
                            completed_outputs[backtrack_dep_id] = {
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
                                workspace_path=PROJECT_DIR,
                            )
                            did_backtrack = True
                            logger.info(
                                "  Backtrack to step %d succeeded. "
                                "Retrying current step...",
                                backtrack_dep_id,
                            )
                        else:
                            logger.warning(
                                "  Backtrack to step %d also failed.",
                                backtrack_dep_id,
                            )

            if did_backtrack:
                # Retry current step with updated context (no rewrite)
                step["rewrites"] = spec_attempt + 1
                _save_state_threadsafe(state)
                continue

            # Section 4: Adaptive retry check (for rewrite path only;
            # backtracking is always attempted regardless of retry budget)
            should_retry, retry_reason = should_continue_retrying(
                step, spec_attempt, error_type, step.get("reflections", [])
            )
            if not should_retry:
                logger.info("  Stopping retries: %s", retry_reason)
                # For timeout or truncation errors, try decomposing once
                # before giving up.  Truncation (format_error with
                # truncation-related reflections) means the script is too
                # long for a single LLM generation pass; decomposing the
                # task description into explicit sub-phases helps the LLM
                # produce shorter, focused code.
                is_truncation = (
                    error_type == "format_error"
                    and not step.get("_decomposed")
                    and any(
                        "truncat" in (
                            r.get("root_cause", "")
                            + r.get("lesson", "")
                        ).lower()
                        for r in step.get("reflections", [])
                    )
                )
                is_timeout = error_type == "timeout" and spec_attempt == 0
                if is_timeout or is_truncation:
                    logger.info(
                        "  %s: decomposing step into sub-phases...",
                        "Truncation" if is_truncation else "Timeout",
                    )
                    step["description"] = decompose_failing_step(
                        step, result.get("stdout", ""), result.get("stderr", ""),
                        is_truncation=is_truncation,
                    )
                    step["_decomposed"] = True
                    step["rewrites"] = spec_attempt + 1
                    _save_state_threadsafe(state)
                    continue
                break

            # Standard rewrite path — LLM chooses strategy freely
            event_log.emit(EventType.REWRITE_START, step_id=step["id"],
                           attempt=spec_attempt + 1)
            logger.info(
                "  Rewriting spec (rewrite %d/%d)...",
                spec_attempt + 1,
                MAX_SPEC_REWRITES,
            )
            if dashboard:
                dashboard.set_step_activity(
                    step["id"], "Rewriting (LLM-driven)..."
                )
                dashboard.log(
                    f"Step {step['id']} failed (attempt {spec_attempt + 1}), "
                    f"rewriting with full history"
                )
            # Use validation error output for rewrites when the failure was
            # a post-execution validation/verification failure, so the
            # rewriter sees the actual problem instead of the orchestrator's
            # successful run output.
            if is_validation_failure:
                _rw_stdout = step.get("output", "") or ""
                _rw_stderr = error_info
            else:
                _rw_stdout = result["stdout"]
                _rw_stderr = result["stderr"]
            step["description"] = reflect_and_rewrite(
                step, _rw_stdout, _rw_stderr,
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

    # Section 8: STEP_FAILED hook
    if _hooks:
        run_hook(HookEvent.STEP_FAILED, {
            "step_id": step["id"],
            "title": step["title"],
            "error": step.get("error", "")[:500],
            "elapsed": step["elapsed"],
        }, _hooks)

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
    for step in state.get("steps", []):
        if step["status"] == "executing":
            logger.info(
                "Resetting interrupted step %s (%s) to pending.",
                step["id"], step["title"],
            )
            step["status"] = "pending"
            step["started_at"] = None
    return state


def main():
    args = parse_args()
    verbose = args.verbose or config.get("verbose")
    configure_logging(verbose)

    dry_run = args.dry_run or config.get("dry_run")

    # Collect flags
    output_flag = args.output or config.get("output") or None
    report_flag = args.report or config.get("report") or None
    trace_flag = args.trace or config.get("trace") or None
    explain_flag = args.explain or config.get("explain")
    events_flag = args.events or config.get("events") or None

    resume = (args.resume or config.get("resume")) and not args.fresh

    # Load hooks (Section 8 of PLAN.md)
    _hooks = load_hooks()

    # Determine run context: resume existing run or start fresh.
    # We need the run_id early so that event log, provenance, and
    # other per-run artifacts are written to the correct directory.
    state = None
    run_id = None
    if resume:
        state = try_resume()
        if state is not None:
            run_id = state.get("run_id", "")

    if not run_id:
        run_id = uuid.uuid4().hex[:12]

    # Per-run directory for all artifacts
    run_dir = get_run_dir(run_id)

    if output_flag:
        output_path = (
            os.path.join(run_dir, "output.json")
            if output_flag == "auto"
            else output_flag
        )
    else:
        output_path = None

    if events_flag:
        events_path = (
            os.path.join(run_dir, "events.jsonl")
            if events_flag == "auto"
            else events_flag
        )
        provenance_path = os.path.join(run_dir, "provenance.json")
    else:
        events_path = None
        provenance_path = None

    # Initialize singletons (with per-run paths)
    reset_event_log()
    reset_provenance_graph()
    reset_code_tracker()
    event_log = get_event_log(events_path=events_path)
    prov = get_provenance_graph(output_path=provenance_path)

    if state is not None:
        logger.info("Resuming goal: %s\n", state["goal"])
        _write_latest_run(run_id)
    else:
        # Prune old runs before starting a new one.
        try:
            prune_old_runs(
                keep_last=int(config.get("keep_last_runs", 10)),
                max_age_days=int(config.get("max_run_age_days", 30)),
            )
        except Exception as exc:
            logger.warning("Run pruning failed (non-fatal): %s", exc)

        # Fresh start
        goal = get_goal(args)
        if not goal:
            logger.error("No goal provided.")
            sys.exit(1)

        # Persist the goal file in .uas_goals/ so it is committed to Git
        # and serves as the canonical project brief.
        _goals_dir = os.path.join(WORKSPACE, ".uas_goals")
        os.makedirs(_goals_dir, exist_ok=True)
        _goal_file_src = (
            getattr(args, "goal_file", None)
            or config.get("goal_file")
        )
        if _goal_file_src:
            _goal_file_src = os.path.expanduser(_goal_file_src)
            if not os.path.isabs(_goal_file_src):
                _goal_file_src = os.path.join(WORKSPACE, _goal_file_src)
            _goal_dest = os.path.join(
                _goals_dir, os.path.basename(_goal_file_src)
            )
            if os.path.realpath(_goal_file_src) != os.path.realpath(_goal_dest):
                shutil.copy2(_goal_file_src, _goal_dest)
        else:
            # Goal was provided via CLI args or stdin — write it to a file.
            _goal_dest = os.path.join(_goals_dir, "GOAL.txt")
            if not os.path.exists(_goal_dest):
                with open(_goal_dest, "w", encoding="utf-8") as _gf:
                    _gf.write(goal + "\n")

        original_goal = goal

        logger.info("Goal: %s\n", goal)
        event_log.emit(EventType.GOAL_RECEIVED, data={"goal": goal})
        goal_entity = prov.add_entity("goal", content=goal)
        planner_agent = prov.add_agent("planner_llm")

        # Specification phase: estimate complexity, research domain for
        # medium/complex goals, then generate a structured project spec.
        research_context = ""
        complexity = None
        spec = ""
        if not MINIMAL_MODE:
            complexity = estimate_complexity(goal)
            if complexity in ("medium", "complex"):
                logger.info("Researching domain before planning...")
                event_log.emit(EventType.RESEARCH_START)
                research_context = research_goal(goal)
                event_log.emit(
                    EventType.RESEARCH_COMPLETE,
                    data={"length": len(research_context)},
                )
                if research_context:
                    logger.info(
                        "  Research complete (%d chars)", len(research_context)
                    )

            logger.info("Generating project specification...")
            spec = generate_project_spec(
                goal,
                research_context=research_context,
                complexity=complexity or "medium",
            )
            if spec:
                logger.info("  Spec generated (%d chars).", len(spec))
                # Persist spec alongside the goal file.
                _spec_path = os.path.join(
                    WORKSPACE, ".uas_goals", "SPEC.md",
                )
                os.makedirs(os.path.dirname(_spec_path), exist_ok=True)
                with open(_spec_path, "w", encoding="utf-8") as _sf:
                    _sf.write(spec + "\n")

        # Phase 1: Decompose (with multi-plan voting for complex goals)
        logger.info("Phase 1: Decomposing goal into atomic steps...")
        event_log.emit(EventType.DECOMPOSITION_START)
        decompose_start = time.monotonic()
        state = init_state(goal, run_id=run_id)
        state["original_goal"] = original_goal
        if spec:
            state["spec"] = spec
        if research_context:
            state["research_context"] = research_context
        try:
            steps = decompose_goal_with_voting(
                goal,
                spec=spec,
                complexity=complexity,
                hooks=_hooks,
            )
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
            steps = merge_steps_with_llm(goal, steps)
            if len(steps) < pre_merge:
                event_log.emit(
                    EventType.STEP_MERGE,
                    data={"before": pre_merge, "after": len(steps)},
                )

        # Section 7: Enforce minimum steps after merge (complexity may require more)
        if complexity:
            steps = enforce_minimum_steps(goal, steps, complexity)

        # Section 1: Goal-coverage matrix — verify all requirements are covered
        steps, requirements = ensure_coverage(goal, steps)
        if requirements:
            state["requirements"] = requirements

        # Section 3: Split coupled creation/integration steps
        steps = split_coupled_steps(steps)

        # Section 5: Insert integration checkpoints at phase boundaries
        steps = insert_integration_checkpoints(steps)

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

    # Initialize git repo before execution starts
    # Section 18: Skip in minimal mode.
    if not MINIMAL_MODE:
        ensure_git_repo(WORKSPACE)

    # Fire RUN_START hook
    if _hooks:
        run_hook(HookEvent.RUN_START, {
            "run_id": state.get("run_id", ""),
            "goal": state.get("goal", ""),
            "num_steps": len(state.get("steps", [])),
        }, _hooks)

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
    replanned_levels = set()  # Section 6b: track which levels have been re-planned

    def _post_step_replan_and_enrich(completed_step, level_idx):
        """Section 6: Post-step re-planning check and description enrichment.

        Called after each successful step completion. Enriches dependent step
        descriptions (6c) and checks if re-planning is needed (6a/6b).
        Returns the earliest pending level index if re-planning was performed,
        or False if no re-planning occurred.
        """
        nonlocal levels, step_by_id

        # Section 6c / Section 11: Build enrichment context for dependent steps.
        # Enrichment is stored in state rather than mutated into descriptions,
        # so it can be filtered/compressed by build_context().
        remaining = [
            s for s in state["steps"]
            if s["status"] not in ("completed",)
        ]
        dependents = [
            s for s in remaining
            if completed_step["id"] in s.get("depends_on", [])
        ]
        if dependents:
            enriched, enrichments = enrich_step_descriptions(
                completed_step, dependents,
                existing_enrichments=state.get("enrichment_context"),
                workspace=PROJECT_DIR,
            )
            if enriched:
                ec = state.setdefault("enrichment_context", {})
                for step_id, text in enrichments.items():
                    if step_id in ec:
                        ec[step_id] += "\n" + text
                    else:
                        ec[step_id] = text
                logger.info(
                    "  Enriched context for steps %s from step %d output.",
                    enriched, completed_step["id"],
                )
                event_log.emit(EventType.STEP_ENRICHED,
                               step_id=completed_step["id"],
                               data={"enriched_steps": enriched})
                _save_state_threadsafe(state)

        # Section 6a: Check if re-planning is needed
        if level_idx in replanned_levels:
            return False  # Already re-planned at this level

        event_log.emit(EventType.REPLAN_CHECK,
                       step_id=completed_step["id"],
                       data={"level": level_idx})

        needs_replan, detail = should_replan_llm(
            completed_step, remaining, state,
        )

        if not needs_replan:
            return False

        # Section 6b: Incremental re-planning
        logger.info(
            "  Re-planning triggered after step %d: %s",
            completed_step["id"], detail,
        )
        event_log.emit(EventType.REPLAN_TRIGGERED,
                       step_id=completed_step["id"],
                       data={"detail": detail[:200],
                             "level": level_idx})
        if dashboard:
            dashboard.log(
                f"Re-planning after step {completed_step['id']}: {detail[:100]}"
            )

        new_remaining = replan_remaining_steps(
            state.get("goal", ""),
            state,
            completed_step,
            detail,
            requirements=state.get("requirements"),
        )

        if new_remaining is None:
            logger.warning("  Re-planning failed, continuing with original plan.")
            return False

        # Replace pending steps with re-planned ones
        # Keep completed steps, replace pending/failed ones
        completed_steps = [
            s for s in state["steps"] if s["status"] == "completed"
        ]
        completed_ids_set = {s["id"] for s in completed_steps}
        max_completed_id = max(completed_ids_set, default=0)

        # Build mapping from any ID the LLM might have used for new
        # steps to the final ID we assign.  The LLM may use positional
        # 1-based indices, continuation IDs (max_completed + pos), or
        # explicit "id" fields.  We map all of these to the canonical
        # final IDs to fix depends_on references between new steps.
        n_new = len(new_remaining)
        dep_remap = {}
        for i, new_step in enumerate(new_remaining):
            final_id = max_completed_id + i + 1
            # Map positional (1-based) index
            dep_remap.setdefault(i + 1, final_id)
            # Map continuation ID
            dep_remap.setdefault(max_completed_id + i + 1, final_id)
            # Map LLM-assigned ID if present
            if "id" in new_step and new_step["id"] not in dep_remap:
                dep_remap[new_step["id"]] = final_id

        # Assign final IDs to new steps
        for i, new_step in enumerate(new_remaining):
            new_step["id"] = max_completed_id + i + 1
            new_step.setdefault("status", "pending")
            new_step.setdefault("spec_file", "")
            new_step.setdefault("rewrites", 0)
            new_step.setdefault("reflections", [])
            new_step.setdefault("output", "")
            new_step.setdefault("stderr_output", "")
            new_step.setdefault("error", "")
            new_step.setdefault("files_written", [])
            new_step.setdefault("uas_result", None)
            new_step.setdefault("summary", "")
            new_step.setdefault("outputs", [])

        # Remap depends_on references that point to other new steps.
        # Deps referencing completed steps stay unchanged.
        for new_step in new_remaining:
            new_step["depends_on"] = [
                d if d in completed_ids_set
                else dep_remap.get(d, d)
                for d in new_step.get("depends_on", [])
            ]

        state["steps"] = completed_steps + new_remaining

        # Section 1d: Re-verify coverage after replanning
        requirements = state.get("requirements", [])
        if requirements:
            matrix = verify_coverage(requirements, state["steps"])
            dropped = [
                e["requirement"] for e in matrix
                if not e.get("covered", True)
            ]
            if dropped:
                logger.info(
                    "  Re-plan dropped coverage for %d requirement(s), "
                    "filling gaps...", len(dropped),
                )
                gap_steps = fill_coverage_gaps(
                    state.get("goal", ""), dropped, state["steps"],
                )
                for gs in gap_steps:
                    gs_id = max(s["id"] for s in state["steps"]) + 1
                    gs["id"] = gs_id
                    gs.setdefault("status", "pending")
                    gs.setdefault("spec_file", "")
                    gs.setdefault("rewrites", 0)
                    gs.setdefault("reflections", [])
                    gs.setdefault("output", "")
                    gs.setdefault("stderr_output", "")
                    gs.setdefault("error", "")
                    gs.setdefault("files_written", [])
                    gs.setdefault("uas_result", None)
                    gs.setdefault("summary", "")
                    gs.setdefault("outputs", [])
                    state["steps"].append(gs)

        step_by_id = {s["id"]: s for s in state["steps"]}

        # Re-validate and re-sort
        try:
            levels = topological_sort(state["steps"])
        except ValueError as e:
            logger.warning(
                "  Re-planned steps have invalid dependencies: %s. "
                "Reverting to original plan.",
                e,
            )
            # Revert — this shouldn't happen with good LLM output
            state["steps"] = completed_steps + remaining
            step_by_id = {s["id"]: s for s in state["steps"]}
            levels = topological_sort(state["steps"])
            return False

        # Find earliest level with a pending step so the loop can
        # jump back and execute steps placed before the current level.
        completed_ids = {s["id"] for s in state["steps"]
                         if s["status"] == "completed"}
        earliest_pending_level = len(levels)
        for i, lvl in enumerate(levels):
            if any(sid not in completed_ids for sid in lvl):
                earliest_pending_level = i
                break

        # Level indices changed after re-sort — clear stale tracking.
        replanned_levels.clear()
        replanned_levels.add(earliest_pending_level)
        _save_state_threadsafe(state)

        logger.info(
            "  Re-planned: %d pending steps, %d levels remaining.",
            len(new_remaining), len(levels),
        )
        event_log.emit(EventType.REPLAN_COMPLETE,
                       step_id=completed_step["id"],
                       data={"new_step_count": len(new_remaining),
                             "new_level_count": len(levels)})
        if dashboard:
            dashboard.log(
                f"Re-plan complete: {len(new_remaining)} steps remaining"
            )
            dashboard.update(state)
        return earliest_pending_level

    failed_step_ids = set()  # Phase 3.5: track irrecoverably failed steps
    level_idx = 0
    while level_idx < len(levels):
        # Wait if user has paused execution via the dashboard
        dashboard.wait_if_paused()

        level = levels[level_idx]
        level_steps = [step_by_id[sid] for sid in level
                       if sid in step_by_id]

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
            elif any(d in failed_step_ids
                     for d in step.get("depends_on", [])):
                step["status"] = "failed"
                step["error"] = "Skipped: dependency failed"
                failed_step_ids.add(step["id"])
                progress_counts["failed"] += 1
                logger.warning(
                    "  Skipping step %s (%s): dependency failed.",
                    step["id"], step["title"],
                )
            else:
                pending.append(step)

        if not pending:
            level_idx += 1
            continue

        if len(pending) == 1:
            # Single step — no threading overhead needed
            step = pending[0]
            success = execute_step(step, state, completed_outputs,
                                   progress_counts, dashboard=dashboard,
                                   hooks=_hooks)
            if not success:
                progress_counts["failed"] += 1
                failed_step_ids.add(step["id"])
                logger.warning(
                    "  Step %s failed; continuing to next independent step.",
                    step["id"],
                )
                save_state(state)
                level_idx += 1
                continue
            progress_counts["completed"] += 1
            completed_outputs[step["id"]] = {
                "stdout": step.get("output", ""),
                "stderr": step.get("stderr_output", ""),
                "files": step.get("files_written", []),
            }

            # Section 6: Post-step re-planning and enrichment
            did_replan = _post_step_replan_and_enrich(step, level_idx)
            if did_replan is not False:
                # Levels were re-sorted; jump to earliest pending level
                # (completed steps will be skipped automatically)
                level_idx = did_replan
                continue
        else:
            # Multiple independent steps — run in parallel.
            # Section 7: Check for file-output conflicts and split into
            # sequential batches where conflicting steps are serialized.
            conflicts = find_file_conflicts(pending)
            batches = _partition_by_conflicts(pending, conflicts)
            if len(batches) > 1:
                conflict_ids = [(a, b) for a, b in conflicts]
                logger.warning(
                    "  File conflicts detected among steps %s — "
                    "serializing into %d sub-batches.",
                    conflict_ids, len(batches),
                )
                event_log.emit(
                    EventType.STEP_ENRICHED,
                    data={
                        "file_conflicts": conflict_ids,
                        "sub_batches": len(batches),
                    },
                )

            failed_in_level = []
            completed_in_level = []

            for batch_idx, batch in enumerate(batches):
                if len(batch) == 1:
                    # Single step in batch — no threading overhead
                    step = batch[0]
                    success = execute_step(
                        step, state, completed_outputs,
                        progress_counts, dashboard=dashboard,
                        hooks=_hooks,
                    )
                    if success:
                        progress_counts["completed"] += 1
                        completed_outputs[step["id"]] = {
                            "stdout": step.get("output", ""),
                            "stderr": step.get("stderr_output", ""),
                            "files": step.get("files_written", []),
                        }
                        completed_in_level.append(step)
                    else:
                        progress_counts["failed"] += 1
                        failed_in_level.append(step)
                    continue

                workers = min(len(batch), MAX_PARALLEL) if MAX_PARALLEL else len(batch)
                if len(batches) > 1:
                    logger.info(
                        "  Sub-batch %d/%d: running %d steps in parallel: %s",
                        batch_idx + 1, len(batches), len(batch),
                        [s["id"] for s in batch],
                    )
                else:
                    logger.info(
                        "  Running %d independent steps in parallel "
                        "(max %d workers): %s",
                        len(batch), workers, [s["id"] for s in batch],
                    )
                dashboard.log(
                    f"Running {len(batch)} steps in parallel: "
                    + ", ".join(f"#{s['id']}" for s in batch)
                )

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                ) as executor:
                    future_to_step = {
                        executor.submit(
                            execute_step, step, state, completed_outputs,
                            progress_counts, dashboard,
                            None, _hooks,
                        ): step
                        for step in batch
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
                            completed_in_level.append(step)
                        else:
                            progress_counts["failed"] += 1
                            failed_in_level.append(step)

            for fstep in failed_in_level:
                failed_step_ids.add(fstep["id"])
                logger.warning(
                    "  Step %s failed; continuing to next independent step.",
                    fstep["id"],
                )
            if failed_in_level:
                save_state(state)

            # Section 6: Post-level re-planning and enrichment
            did_replan = False
            for cstep in completed_in_level:
                did_replan = _post_step_replan_and_enrich(cstep, level_idx)
                if did_replan is not False:
                    break  # Re-plan once per level
            if did_replan is not False:
                level_idx = did_replan
                continue  # Re-sorted levels; jump to earliest pending

        level_idx += 1

    # All done
    state["total_elapsed"] = time.monotonic() - run_start

    unfinished = [s for s in state["steps"] if s["status"] != "completed"]
    if unfinished:
        ids = [s["id"] for s in unfinished]
        logger.error(
            "Execution loop finished but %d step(s) not completed: %s",
            len(unfinished), ids,
        )
        state["status"] = "blocked"
        save_state(state)
        if output_path:
            write_json_output(state, output_path)
        dashboard.finish(state)
        sys.exit(1)

    state["status"] = "completed"
    save_state(state)

    # Final workspace validation + correction loop (Section 6 of PLAN.md)
    validation = validate_workspace(state, PROJECT_DIR)
    if validation["missing_files"]:
        logger.warning(
            "  Some referenced files are missing: %s",
            ", ".join(validation["missing_files"]),
        )
    if validation["workspace_empty"]:
        logger.warning("  Warning: workspace is empty")
    for bp_warn in validation.get("best_practice_warnings", []):
        logger.warning("  Best practice: %s", bp_warn)

    # Section 6: Validation-driven correction loop
    # If confidence is below "high", generate and execute corrective steps.
    if not MINIMAL_MODE:
        correction_rounds_done = state.get("correction_rounds", 0)
        llm_assessment = validation.get("llm_assessment") or {}
        confidence = llm_assessment.get("confidence", "high")
        issues = llm_assessment.get("issues", [])

        while (
            confidence in ("medium", "low")
            and issues
            and correction_rounds_done < MAX_CORRECTION_ROUNDS
        ):
            correction_rounds_done += 1
            state["correction_rounds"] = correction_rounds_done
            logger.info(
                "\nCorrection round %d/%d: confidence=%s, %d issue(s)",
                correction_rounds_done, MAX_CORRECTION_ROUNDS,
                confidence, len(issues),
            )
            event_log.emit(EventType.LLM_CALL_START,
                           data={"purpose": "correction_loop",
                                 "round": correction_rounds_done})

            corrective_steps = generate_corrective_steps(
                state.get("goal", ""), issues, state,
            )

            if not corrective_steps:
                logger.warning("  No corrective steps generated, exiting loop.")
                break

            # Assign IDs and register in state
            max_id = max((s["id"] for s in state["steps"]), default=0)
            for i, cs in enumerate(corrective_steps):
                cs["id"] = max_id + i + 1
                cs.setdefault("status", "pending")
                cs.setdefault("spec_file", "")
                cs.setdefault("rewrites", 0)
                cs.setdefault("reflections", [])
                cs.setdefault("output", "")
                cs.setdefault("stderr_output", "")
                cs.setdefault("error", "")
                cs.setdefault("files_written", [])
                cs.setdefault("uas_result", None)
                cs.setdefault("summary", "")
                cs.setdefault("outputs", [])
                cs.setdefault("timing", {
                    "llm_time": 0.0, "sandbox_time": 0.0, "total_time": 0.0,
                })
                state["steps"].append(cs)

            save_state(state)

            logger.info(
                "  Generated %d corrective step(s): %s",
                len(corrective_steps),
                ", ".join(f"#{s['id']}" for s in corrective_steps),
            )

            # Execute corrective steps sequentially
            correction_failed = False
            for cs in corrective_steps:
                step_by_id[cs["id"]] = cs
                success = execute_step(
                    cs, state, completed_outputs,
                    progress_counts, dashboard=dashboard,
                    hooks=_hooks,
                )
                if success:
                    completed_outputs[cs["id"]] = {
                        "stdout": cs.get("output", ""),
                        "stderr": cs.get("stderr_output", ""),
                        "files": cs.get("files_written", []),
                    }
                else:
                    logger.warning(
                        "  Corrective step %d failed, stopping correction.",
                        cs["id"],
                    )
                    correction_failed = True
                    break

            save_state(state)

            if correction_failed:
                break

            # Re-validate after corrections
            validation = validate_workspace(state, PROJECT_DIR)
            llm_assessment = validation.get("llm_assessment") or {}
            confidence = llm_assessment.get("confidence", "high")
            issues = llm_assessment.get("issues", [])

            logger.info(
                "  Post-correction validation: confidence=%s, %d issue(s)",
                confidence, len(issues),
            )

            if confidence == "high":
                logger.info("  Confidence reached 'high', exiting correction loop.")
                break

        if correction_rounds_done > 0:
            state["correction_rounds"] = correction_rounds_done
            save_state(state)
            if confidence != "high":
                logger.warning(
                    "  Correction loop finished after %d round(s) "
                    "with confidence=%s.",
                    correction_rounds_done, confidence,
                )

    # Section 13: Holistic end-of-run workspace validation
    if not MINIMAL_MODE:
        holistic_issues = holistic_validation(PROJECT_DIR, state)
        if holistic_issues:
            for hi in holistic_issues:
                logger.warning("  Holistic: %s", hi)
            state.setdefault("holistic_issues", []).extend(holistic_issues)
            save_state(state)

            # Run one more correction round for critical issues
            critical = [
                i for i in holistic_issues
                if any(kw in i.lower() for kw in (
                    "does not exist", "cannot be resolved",
                    "not found",
                ))
            ]
            h_rounds = state.get("correction_rounds", 0)
            if critical and h_rounds < MAX_CORRECTION_ROUNDS + 1:
                logger.info(
                    "\nHolistic correction: %d critical issue(s)", len(critical),
                )
                h_corrective = generate_corrective_steps(
                    state.get("goal", ""), critical, state,
                )
                if h_corrective:
                    max_id = max(
                        (s["id"] for s in state["steps"]), default=0,
                    )
                    for i, cs in enumerate(h_corrective):
                        cs["id"] = max_id + i + 1
                        cs.setdefault("status", "pending")
                        cs.setdefault("spec_file", "")
                        cs.setdefault("rewrites", 0)
                        cs.setdefault("reflections", [])
                        cs.setdefault("output", "")
                        cs.setdefault("stderr_output", "")
                        cs.setdefault("error", "")
                        cs.setdefault("files_written", [])
                        cs.setdefault("uas_result", None)
                        cs.setdefault("summary", "")
                        cs.setdefault("outputs", [])
                        cs.setdefault("timing", {
                            "llm_time": 0.0, "sandbox_time": 0.0,
                            "total_time": 0.0,
                        })
                        state["steps"].append(cs)
                    save_state(state)
                    logger.info(
                        "  Generated %d holistic corrective step(s)",
                        len(h_corrective),
                    )
                    for cs in h_corrective:
                        step_by_id[cs["id"]] = cs
                        success = execute_step(
                            cs, state, completed_outputs,
                            progress_counts, dashboard=dashboard,
                            hooks=_hooks,
                        )
                        if success:
                            completed_outputs[cs["id"]] = {
                                "stdout": cs.get("output", ""),
                                "stderr": cs.get("stderr_output", ""),
                                "files": cs.get("files_written", []),
                            }
                        else:
                            logger.warning(
                                "  Holistic corrective step %d failed.",
                                cs["id"],
                            )
                            break
                    save_state(state)

    if not MINIMAL_MODE:
        post_run_meta_learning(state)

    # Squash wip checkpoint commits into a single commit on main
    if not MINIMAL_MODE:
        finalize_git(WORKSPACE, state.get("goal", ""))

    if output_path:
        write_json_output(state, output_path)

    event_log.emit(EventType.RUN_COMPLETE, data={
        "status": state["status"],
        "total_elapsed": state.get("total_elapsed", 0.0),
    })

    # Section 8: RUN_COMPLETE hook
    if _hooks:
        run_hook(HookEvent.RUN_COMPLETE, {
            "run_id": state.get("run_id", ""),
            "status": state["status"],
            "total_elapsed": state.get("total_elapsed", 0.0),
            "num_steps": len(state.get("steps", [])),
        }, _hooks)

    _finalize_code_tracking(run_id=state.get("run_id", ""))
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
        report_path = (
            os.path.join(run_dir, "report.html")
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
        trace_path = (
            os.path.join(run_dir, "trace.json")
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
    run_rel = os.path.join(".uas_state", "runs", run_id)
    logger.info("Run ID: %s", run_id)
    logger.info(
        "State saved to: %s",
        os.path.join(run_rel, "state.json"),
    )
    logger.info(
        "Specs saved to: %s/",
        os.path.join(run_rel, "specs"),
    )
    if events_path:
        logger.info("Events written to: %s", events_path)
    if provenance_path:
        logger.info("Provenance written to: %s", provenance_path)


if __name__ == "__main__":
    main()
