"""Architect Agent: autonomous planner and spec generator.

Takes an abstract human goal, decomposes it into atomic steps,
generates UAS-compliant specs, and drives the Orchestrator to execute them.
"""

import os
import sys

from .state import init_state, save_state, add_steps
from .planner import decompose_goal, rewrite_task
from .spec_generator import generate_spec, build_task_from_spec
from .executor import run_orchestrator, extract_sandbox_stdout

MAX_SPEC_REWRITES = 2
PROJECT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def get_goal() -> str:
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])
    goal = os.environ.get("UAS_GOAL")
    if goal:
        return goal
    print("Enter your goal (end with Ctrl+D):")
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
    blocker_path = os.path.join(PROJECT_ROOT, "ARCHITECT_BLOCKER.md")
    with open(blocker_path, "w") as f:
        f.write("# Architect Blocker\n\n")
        f.write(f"**Goal:** {state['goal']}\n\n")
        f.write(f"**Blocked at step {step['id']}:** {step['title']}\n\n")
        f.write("## Failure Details\n\n")
        f.write(f"The Orchestrator failed this step after all retries, and the "
                f"Architect exhausted {MAX_SPEC_REWRITES} spec rewrites.\n\n")
        f.write(f"**Last task description:**\n```\n{step['description']}\n```\n\n")
        f.write(f"**Last error:**\n```\n{step['error'][:2000]}\n```\n\n")
        f.write("## Required Action\n\n")
        f.write("A human must review the failure above and either:\n")
        f.write("1. Simplify the goal.\n")
        f.write("2. Provide missing credentials or resources.\n")
        f.write("3. Manually fix the failing step and re-run.\n")
    print(f"\nBlocker written to {blocker_path}")


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
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")

        # Generate UAS spec
        spec_file = generate_spec(step, total, context)
        print(f"  Spec written: {spec_file}")

        # Build task for Orchestrator
        task = build_task_from_spec(step, context)
        print(f"  Sending to Orchestrator...")

        # Execute
        step["status"] = "executing"
        save_state(state)

        result = run_orchestrator(task)

        print(f"  Orchestrator exit code: {result['exit_code']}")

        if result["exit_code"] == 0:
            step["status"] = "completed"
            step["output"] = extract_sandbox_stdout(result["stdout"])
            step["error"] = ""
            save_state(state)
            print(f"  Step {step['id']} SUCCEEDED.")
            if step["output"]:
                print(f"  Output: {step['output'][:200]}")
            return True

        # Failed
        error_info = result["stderr"] or result["stdout"] or "Unknown error"
        step["error"] = error_info
        step["status"] = "failed"
        save_state(state)

        print(f"  Step {step['id']} FAILED.")
        print(f"  Error: {error_info[:300]}")

        if spec_attempt < MAX_SPEC_REWRITES:
            print(f"  Rewriting spec (rewrite {spec_attempt + 1}/{MAX_SPEC_REWRITES})...")
            step["description"] = rewrite_task(
                step, result["stdout"], result["stderr"]
            )
            step["rewrites"] = spec_attempt + 1
            save_state(state)
        else:
            print(f"  Exhausted all spec rewrites for step {step['id']}.")

    return False


def main():
    goal = get_goal()
    if not goal:
        print("ERROR: No goal provided.", file=sys.stderr)
        sys.exit(1)

    print(f"Goal: {goal}")
    print()

    # Phase 1: Decompose
    print("Phase 1: Decomposing goal into atomic steps...")
    state = init_state(goal)
    try:
        steps = decompose_goal(goal)
    except Exception as e:
        print(f"ERROR: Failed to decompose goal: {e}", file=sys.stderr)
        state["status"] = "failed"
        save_state(state)
        sys.exit(1)

    state = add_steps(state, steps)
    print(f"  Decomposed into {len(steps)} step(s):")
    for s in state["steps"]:
        deps = f" (depends on {s['depends_on']})" if s["depends_on"] else ""
        print(f"    {s['id']}. {s['title']}{deps}")
    print()

    # Phase 2: Execute
    print("Phase 2: Executing steps via Orchestrator...")
    completed_outputs = {}

    for step in state["steps"]:
        success = execute_step(step, state, completed_outputs)
        if not success:
            state["status"] = "blocked"
            save_state(state)
            create_blocker(state, step)
            print(f"\nHALTED: Step {step['id']} failed irrecoverably.")
            sys.exit(1)
        completed_outputs[step["id"]] = step["output"]

    # All done
    state["status"] = "completed"
    save_state(state)
    print(f"\n{'='*60}")
    print("  ALL STEPS COMPLETED SUCCESSFULLY")
    print(f"{'='*60}")
    print(f"State saved to: {os.path.join('architect_state', 'plan_state.json')}")
    print(f"Specs saved to: {os.path.join('architect_state', 'specs')}/")


if __name__ == "__main__":
    main()
