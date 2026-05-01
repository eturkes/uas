"""Orchestrator CLI entry point (Phase 3 skeleton).

Subcommand bodies raise NotImplementedError until later sections wire
them up. ``--help`` works at every level so the CLI shape is reviewable
before any behaviour exists.
"""

import argparse
import sys


def cmd_start(args: argparse.Namespace) -> int:
    raise NotImplementedError("start: implemented in Section 7")


def cmd_status(args: argparse.Namespace) -> int:
    raise NotImplementedError("status: implemented in Section 7")


def cmd_resume(args: argparse.Namespace) -> int:
    raise NotImplementedError("resume: implemented in Section 7")


def cmd_pause(args: argparse.Namespace) -> int:
    raise NotImplementedError("pause: implemented in Section 8")


def cmd_halt(args: argparse.Namespace) -> int:
    raise NotImplementedError("halt: implemented in Section 8")


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
    p_start.set_defaults(func=cmd_start)

    p_status = subparsers.add_parser(
        "status", help="Print recovered state summary for a task.",
    )
    p_status.add_argument("task", help="Task id.")
    p_status.set_defaults(func=cmd_status)

    p_resume = subparsers.add_parser(
        "resume",
        help="Resume a previously-paused task from its task_events.jsonl.",
    )
    p_resume.add_argument("task", help="Task id.")
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
