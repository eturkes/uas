"""Architect Agent: autonomous planner and spec generator.

Takes an abstract human goal, decomposes it into atomic steps,
generates UAS-compliant specs, and drives the Orchestrator to execute them.
"""

import argparse
import concurrent.futures
import logging
import os
import sys
import threading

from .state import init_state, save_state, load_state, add_steps
from .planner import decompose_goal, rewrite_task, topological_sort
from .spec_generator import generate_spec, build_task_from_spec
from .executor import run_orchestrator, extract_sandbox_stdout

MAX_SPEC_REWRITES = 2
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
    return parser.parse_args()


def get_goal(args) -> str:
    if args.goal:
        return " ".join(args.goal)
    goal = os.environ.get("UAS_GOAL")
    if goal:
        return goal
    print("Enter your goal (submit with Ctrl+D):", file=sys.stderr)
    return sys.stdin.read().strip()


def build_context(step: dict, completed_outputs: dict) -> str:
    """Build context string from outputs of dependency steps."""
    if not step["depends_on"]:
        return ""
    parts = []
    for dep_id in step["depends_on"]:
        output = completed_outputs.get(dep_id, "")
        if output:
            parts.append(f"Output from step {dep_id}: {output}")
    return "\n".join(parts)


def create_blocker(state: dict, step: dict):
    blocker_path = os.path.join(WORKSPACE, "ARCHITECT_BLOCKER.md")
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


def execute_step(step: dict, state: dict, completed_outputs: dict) -> bool:
    """Execute a single step, with spec rewrite retries.

    Returns True on success, False on unrecoverable failure.
    """
    total = len(state["steps"])
    context = build_context(step, completed_outputs)

    for spec_attempt in range(1 + MAX_SPEC_REWRITES):
        label = f"Step {step['id']}/{total}: {step['title']}"
        if spec_attempt > 0:
            label += f" (rewrite {spec_attempt}/{MAX_SPEC_REWRITES})"
        logger.info("\n%s", "=" * 60)
        logger.info("  %s", label)
        logger.info("%s", "=" * 60)

        # Generate UAS spec
        spec_file = generate_spec(step, total, context)
        logger.info("  Spec written: %s", spec_file)

        # Build task for Orchestrator
        task = build_task_from_spec(step, context)
        logger.info("  Sending to Orchestrator...")

        # Execute
        step["status"] = "executing"
        _save_state_threadsafe(state)

        result = run_orchestrator(task)

        logger.info("  Orchestrator exit code: %s", result["exit_code"])

        if result["exit_code"] == 0:
            step["status"] = "completed"
            step["output"] = extract_sandbox_stdout(result["stderr"])
            step["error"] = ""
            _save_state_threadsafe(state)
            logger.info("  Step %s SUCCEEDED.", step["id"])
            if step["output"]:
                logger.info("  Output: %s", step["output"][:OUTPUT_PREVIEW_LENGTH])
            return True

        # Failed
        error_info = result["stderr"] or result["stdout"] or "Unknown error"
        step["error"] = error_info
        step["status"] = "failed"
        _save_state_threadsafe(state)

        logger.error("  Step %s FAILED.", step["id"])
        logger.error("  Error: %s", error_info[:LOG_PREVIEW_LENGTH])

        if spec_attempt < MAX_SPEC_REWRITES:
            logger.info(
                "  Rewriting spec (rewrite %d/%d)...",
                spec_attempt + 1,
                MAX_SPEC_REWRITES,
            )
            step["description"] = rewrite_task(
                step, result["stdout"], result["stderr"]
            )
            step["rewrites"] = spec_attempt + 1
            _save_state_threadsafe(state)
        else:
            logger.error(
                "  Exhausted all spec rewrites for step %s.", step["id"]
            )

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

        # Phase 1: Decompose
        logger.info("Phase 1: Decomposing goal into atomic steps...")
        state = init_state(goal)
        try:
            steps = decompose_goal(goal)
        except Exception as e:
            logger.error("Failed to decompose goal: %s", e)
            state["status"] = "failed"
            save_state(state)
            sys.exit(1)

        state = add_steps(state, steps)
        logger.info("  Decomposed into %d step(s):", len(steps))
        for s in state["steps"]:
            deps = f" (depends on {s['depends_on']})" if s["depends_on"] else ""
            logger.info("    %s. %s%s", s["id"], s["title"], deps)

    # Phase 2: Execute (resume-aware, parallel where possible)
    logger.info("\nPhase 2: Executing steps via Orchestrator...")
    completed_outputs = {}
    step_by_id = {s["id"]: s for s in state["steps"]}
    levels = topological_sort(state["steps"])

    for level in levels:
        level_steps = [step_by_id[sid] for sid in level]

        # Separate already-completed from pending
        pending = []
        for step in level_steps:
            if step["status"] == "completed":
                logger.info("  Skipping step %s (already completed): %s",
                            step["id"], step["title"])
                completed_outputs[step["id"]] = step["output"]
            else:
                pending.append(step)

        if not pending:
            continue

        if len(pending) == 1:
            # Single step — no threading overhead needed
            step = pending[0]
            success = execute_step(step, state, completed_outputs)
            if not success:
                state["status"] = "blocked"
                save_state(state)
                create_blocker(state, step)
                logger.error("HALTED: Step %s failed irrecoverably.", step["id"])
                sys.exit(1)
            completed_outputs[step["id"]] = step["output"]
        else:
            # Multiple independent steps — run in parallel
            logger.info("  Running %d independent steps in parallel: %s",
                        len(pending), [s["id"] for s in pending])
            failed_step = None
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(pending),
            ) as executor:
                future_to_step = {
                    executor.submit(
                        execute_step, step, state, completed_outputs,
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
                        completed_outputs[step["id"]] = step["output"]
                    elif failed_step is None:
                        failed_step = step

            if failed_step is not None:
                state["status"] = "blocked"
                save_state(state)
                create_blocker(state, failed_step)
                logger.error("HALTED: Step %s failed irrecoverably.",
                             failed_step["id"])
                sys.exit(1)

    # All done
    state["status"] = "completed"
    save_state(state)
    logger.info("\n%s", "=" * 60)
    logger.info("  ALL STEPS COMPLETED SUCCESSFULLY")
    logger.info("%s", "=" * 60)
    logger.info(
        "State saved to: %s",
        os.path.join("architect_state", "plan_state.json"),
    )
    logger.info(
        "Specs saved to: %s/",
        os.path.join("architect_state", "specs"),
    )


if __name__ == "__main__":
    main()
