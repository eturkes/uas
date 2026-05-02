"""Orchestrator CLI entry point (Phase 3 §1 skeleton, §7 wired).

§7 wires ``start`` / ``resume`` / ``status`` against
``orchestrator.task.load_task`` and ``Task.from_toml``. ``pause``
and ``halt`` remain stubs until §8.

The ``--state-root``, ``--cases-dir``, and ``--workspaces-dir``
flags exist on the wired subcommands so tests can redirect the
canonical paths to a ``tmp_path`` rather than monkeypatching
module globals; production runs leave them at their defaults.
"""

import argparse
import os
import sys

from orchestrator import task as task_mod
from orchestrator import workspace as workspace_mod
from orchestrator.task import Task

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CASES_DIR = os.path.join(SCRIPT_DIR, "cases")


def _print_summary(task: Task, *, file=None) -> None:
    """Print a compact recovered-state summary for ``cmd_resume`` /
    ``cmd_status`` / ``cmd_start`` (after fresh creation).

    Format: task id + goal, subtask counts per status, total spend
    across all subtasks, last decision (kind + note).
    """
    if file is None:
        file = sys.stdout
    counts = {"pending": 0, "in_flight": 0, "done": 0, "failed": 0}
    for st in task.subtasks:
        counts[st.status] = counts.get(st.status, 0) + 1
    total_spend = sum(
        (st.cost_usd or 0.0) for st in task.subtasks
    )
    last_decision = task.decisions[-1] if task.decisions else None

    print(f"Task: {task.task_id}", file=file)
    print(f"Goal: {task.goal}", file=file)
    print(
        f"Subtasks: {counts['pending']} pending, "
        f"{counts['in_flight']} in_flight, "
        f"{counts['done']} done, "
        f"{counts['failed']} failed",
        file=file,
    )
    print(f"Total spend: ${total_spend:.4f}", file=file)
    if last_decision is not None:
        print(
            f"Last decision: {last_decision.kind} — {last_decision.note}",
            file=file,
        )
    else:
        print("Last decision: (none)", file=file)


def _resolve_paths(args: argparse.Namespace) -> tuple[str, str, str]:
    state_root = (
        args.state_root if args.state_root is not None
        else task_mod.DEFAULT_STATE_ROOT
    )
    cases_dir = (
        args.cases_dir if args.cases_dir is not None
        else DEFAULT_CASES_DIR
    )
    workspaces_dir = (
        args.workspaces_dir if args.workspaces_dir is not None
        else task_mod.DEFAULT_WORKSPACES_DIR
    )
    return state_root, cases_dir, workspaces_dir


def cmd_start(args: argparse.Namespace) -> int:
    """Start a fresh task or route to ``cmd_resume`` if a log exists.

    Per §7: ``task_events.jsonl`` presence is the route-to-resume
    signal. A fresh task loads the case TOML at
    ``<cases_dir>/<task_id>.toml``, sets up the per-task workspace,
    and prints a one-shot summary. The actual main-loop drive lives
    in §8.
    """
    state_root, cases_dir, workspaces_dir = _resolve_paths(args)
    events_path = os.path.join(state_root, args.task, "task_events.jsonl")
    if os.path.isfile(events_path):
        return cmd_resume(args)

    case_path = os.path.join(cases_dir, f"{args.task}.toml")
    task = Task.from_toml(
        case_path, state_root=state_root, workspaces_dir=workspaces_dir,
    )
    workspace_mod.setup_task_workspace(
        task.task_id, workspaces_dir=workspaces_dir,
    )
    _print_summary(task)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Reload state via ``load_task``, set up workspace, print summary.

    The §7 deliverable. The "continues the main orchestrator loop
    from there" wording from PLAN step 4 is forward-looking — §8
    plugs the real loop in. For §7 the subcommand calls
    ``load_task``, ensures the workspace exists (resume-safe per
    substrate doc §5), and prints the recovered summary.
    """
    state_root, _cases_dir, workspaces_dir = _resolve_paths(args)
    task = task_mod.load_task(args.task, state_root=state_root)
    workspace_mod.setup_task_workspace(
        task.task_id, workspaces_dir=workspaces_dir,
    )
    _print_summary(task)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Read-only summary view; does NOT write a ``task_resume`` row.

    Distinct from ``cmd_resume`` — status calls ``load_task`` with
    ``mark_resume=False`` so the in-flight reset and the
    ``task_resume`` decision-write are both skipped. Lets the
    operator inspect a paused task's state without nudging the log.
    """
    state_root, _cases_dir, _workspaces_dir = _resolve_paths(args)
    task = task_mod.load_task(
        args.task, state_root=state_root, mark_resume=False,
    )
    _print_summary(task)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    raise NotImplementedError("pause: implemented in Section 8")


def cmd_halt(args: argparse.Namespace) -> int:
    raise NotImplementedError("halt: implemented in Section 8")


def _add_path_flags(parser: argparse.ArgumentParser) -> None:
    """Attach the canonical-path override flags shared by §7 commands."""
    parser.add_argument(
        "--state-root",
        default=None,
        help=(
            "Override the per-task state directory root "
            "(default: <repo>/orchestrator/state)."
        ),
    )
    parser.add_argument(
        "--cases-dir",
        default=None,
        help=(
            "Override the directory holding task TOML cases "
            "(default: <repo>/orchestrator/cases)."
        ),
    )
    parser.add_argument(
        "--workspaces-dir",
        default=None,
        help=(
            "Override the per-task workspace root "
            "(default: <repo>/integration/workspace)."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uas-orchestrate",
        description=(
            "Long-horizon task orchestrator. Spawns headless Claude "
            "Code workers in a sandbox, enforces a three-state "
            "free / paid / hard-stop policy machine against real "
            "Claude Max usage signals, and persists task state across "
            "invocation boundaries."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="subcommand", required=True, metavar="SUBCOMMAND",
    )

    p_start = subparsers.add_parser(
        "start", help="Start a new task from orchestrator/cases/<task>.toml.",
    )
    p_start.add_argument("task", help="Task id (matches the case file stem).")
    _add_path_flags(p_start)
    p_start.set_defaults(func=cmd_start)

    p_status = subparsers.add_parser(
        "status", help="Print recovered state summary for a task.",
    )
    p_status.add_argument("task", help="Task id.")
    _add_path_flags(p_status)
    p_status.set_defaults(func=cmd_status)

    p_resume = subparsers.add_parser(
        "resume",
        help="Resume a previously-paused task from its task_events.jsonl.",
    )
    p_resume.add_argument("task", help="Task id.")
    _add_path_flags(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_pause = subparsers.add_parser(
        "pause",
        help=(
            "Record a policy_pause decision and exit cleanly so the "
            "current 5h or 7d window can drain."
        ),
    )
    p_pause.add_argument("task", help="Task id.")
    p_pause.set_defaults(func=cmd_pause)

    p_halt = subparsers.add_parser(
        "halt",
        help=(
            "Record a policy_halt decision and stop the orchestrator. "
            "Resume requires explicit operator intervention."
        ),
    )
    p_halt.add_argument("task", help="Task id.")
    p_halt.set_defaults(func=cmd_halt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
