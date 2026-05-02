"""Orchestrator CLI entry point (Phase 3 §1 skeleton, §7 wired, §8 loop).

§7 wired ``start`` / ``resume`` / ``status`` against
``orchestrator.task.load_task`` and ``Task.from_toml``; §8 plugs
the policy → worker → repeat loop into ``cmd_start`` / ``cmd_resume``
and implements ``cmd_pause`` / ``cmd_halt`` as decision-recording
exit subcommands.

The ``--state-root``, ``--cases-dir``, and ``--workspaces-dir``
flags exist on the wired subcommands so tests can redirect the
canonical paths to a ``tmp_path`` rather than monkeypatching
module globals; production runs leave them at their defaults.

The ``--simulate-rate-status <state>`` flag overrides §3's
``RateLedger.current_status()`` read with a synthesised rate_status
dict (one of ``allowed`` / ``five_hour_pause`` / ``seven_day_wrap_up``).
Opt-in only — production runs omit it. §8 uses the override to
trigger the policy machine's pause / wrap_up transitions in a
deterministic test rather than waiting for real Claude Max usage
limits to fire.
"""

import argparse
import os
import shutil
import sys
import time

from orchestrator import buffer_ledger as buffer_ledger_mod
from orchestrator import policy as policy_mod
from orchestrator import rate_ledger as rate_ledger_mod
from orchestrator import task as task_mod
from orchestrator import worker as worker_mod
from orchestrator import workspace as workspace_mod
from orchestrator.task import Subtask, Task

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CASES_DIR = os.path.join(SCRIPT_DIR, "cases")

SIMULATED_STATUSES: tuple[str, ...] = (
    "allowed", "five_hour_pause", "seven_day_wrap_up",
)


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


def _simulated_rate_status(state: str) -> dict:
    """Synthesise a rate_status dict mirroring ``RateLedger.current_status``.

    State values mimic the Phase 2 §1 stream-json schema:

    - ``allowed`` — both windows in the green; policy returns ``go``.
    - ``five_hour_pause`` — five_hour above ``"allowed"``; policy
      returns ``pause_until``.
    - ``seven_day_wrap_up`` — seven_day above ``"allowed"``; policy
      returns ``wrap_up`` (preempting any five_hour rule because §5's
      rule order checks seven_day first).

    Used only when ``--simulate-rate-status`` is set; production runs
    consult ``RateLedger.current_status()`` instead. The synthetic
    timestamps for ``resetsAt`` are epoch-distant placeholders — the
    §5 policy parses them but the §8 loop does not actually wait until
    they expire.
    """
    if state == "allowed":
        return {
            "five_hour": {
                "status": "allowed", "resetsAt": None,
                "isUsingOverage": False, "overageStatus": None,
                "overageResetsAt": None,
            },
            "seven_day": {
                "status": "allowed", "resetsAt": None,
                "isUsingOverage": False, "overageStatus": None,
                "overageResetsAt": None,
            },
        }
    if state == "five_hour_pause":
        return {
            "five_hour": {
                "status": "approaching_limit",
                "resetsAt": "2026-05-02T20:00:00Z",
                "isUsingOverage": False, "overageStatus": None,
                "overageResetsAt": None,
            },
            "seven_day": {
                "status": "allowed", "resetsAt": None,
                "isUsingOverage": False, "overageStatus": None,
                "overageResetsAt": None,
            },
        }
    if state == "seven_day_wrap_up":
        return {
            "five_hour": {
                "status": "allowed", "resetsAt": None,
                "isUsingOverage": False, "overageStatus": None,
                "overageResetsAt": None,
            },
            "seven_day": {
                "status": "approaching_limit",
                "resetsAt": "2026-05-09T00:00:00Z",
                "isUsingOverage": False, "overageStatus": None,
                "overageResetsAt": None,
            },
        }
    raise ValueError(
        f"unknown simulate-rate-status: {state!r}; "
        f"must be one of {list(SIMULATED_STATUSES)}"
    )


def _resolve_rate_status(
    rate_l: rate_ledger_mod.RateLedger,
    task_id: str,
    simulate: str | None,
) -> dict:
    if simulate is None:
        return rate_l.current_status(task_id)
    return _simulated_rate_status(simulate)


def _find_next_pending(task: Task) -> Subtask | None:
    for st in task.subtasks:
        if st.status == "pending":
            return st
    return None


def _seed_policy_override(task: Task, cases_dir: str) -> None:
    """Copy ``<cases_dir>/<task>-policy.toml`` to ``state/<task>/policy.toml``.

    Bridges the Phase 3 §8 convention — per-task policy overrides
    authored next to their case TOML in ``orchestrator/cases/`` —
    with §5's runtime-state convention that
    ``Policy.load(task_id=...)`` reads the override from
    ``<state_root>/<task_id>/policy.toml``. Fires on fresh-task
    bootstrap only (cmd_start with no log). Source missing → no-op;
    the policy machine falls back to the committed default.
    Idempotent: never overwrites an existing state-side override
    (rerun-safe, even though cmd_start short-circuits to cmd_resume
    when a log exists).
    """
    src = os.path.join(cases_dir, f"{task.task_id}-policy.toml")
    if not os.path.isfile(src):
        return
    dst_dir = os.path.join(task.state_root, task.task_id)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "policy.toml")
    if os.path.isfile(dst):
        return
    shutil.copyfile(src, dst)


def _run_loop(
    task: Task,
    *,
    state_root: str,
    workspaces_dir: str,
    simulate: str | None,
) -> None:
    """Drive policy → worker → repeat until policy stops or queue empty.

    Phase 3 §8 main orchestration loop. On each iteration consults
    ``Policy.decide()`` with the latest rate_status (real or
    simulated) and the buffer total reported by Claude (per the §4
    hand-off note); proceeds to spawn the next pending subtask only
    on a ``go`` verdict. Records one ``policy_pause`` / ``wrap_up``
    / ``halt`` decision when the policy stops the loop, and one
    ``worker_spawn`` plus one ``worker_complete`` / ``worker_fail``
    decision per spawn so the ``task_events.jsonl`` timeline is
    self-explanatory on resume.

    Worker failure (non-zero exit, missing terminal usage, or a
    timeout result) records ``worker_fail`` and stops the loop —
    Phase 3 scope is explicit that the orchestrator does not retry.
    Operators may re-enqueue manually or accept the failed subtask
    on a subsequent resume.
    """
    policy = policy_mod.Policy.load(
        task_id=task.task_id, state_root=state_root,
    )
    rate_l = rate_ledger_mod.RateLedger(state_root=state_root)
    buffer_l = buffer_ledger_mod.BufferLedger(state_root=state_root)
    workspace_path = workspace_mod.setup_task_workspace(
        task.task_id, workspaces_dir=workspaces_dir,
    )

    while True:
        rate_status = _resolve_rate_status(
            rate_l, task.task_id, simulate,
        )
        buffer_total = buffer_l.total_spent_reported(task.task_id)
        decision = policy.decide(
            rate_status, buffer_total, now=time.time(),
        )
        action = decision["action"]

        if action == "pause_until":
            task.record_decision("policy_pause", decision["reason"])
            return
        if action == "wrap_up":
            task.record_decision("policy_wrap_up", decision["reason"])
            return
        if action == "halt":
            task.record_decision("policy_halt", decision["reason"])
            return
        # action == "go" — proceed to next subtask.

        next_st = _find_next_pending(task)
        if next_st is None:
            return

        task.record_decision(
            "worker_spawn", f"spawn {next_st.subtask_id}",
        )
        task.start_subtask(next_st.subtask_id)

        result = worker_mod.spawn_worker(
            next_st.prompt,
            workspace=workspace_path,
            task_id=task.task_id,
            subtask_id=next_st.subtask_id,
            state_root=state_root,
        )

        terminal = result.get("result") or {}
        cost_usd = terminal.get("total_cost_usd")
        cost_for_subtask = (
            float(cost_usd)
            if isinstance(cost_usd, (int, float))
            and not isinstance(cost_usd, bool)
            else None
        )
        result_summary = (result.get("output") or "").strip()
        if len(result_summary) > 200:
            result_summary = result_summary[:200]

        if (
            result.get("exit_code") == 0
            and isinstance(terminal.get("usage"), dict)
        ):
            task.complete_subtask(
                next_st.subtask_id,
                result_summary=result_summary or None,
                cost_usd=cost_for_subtask,
            )
            task.record_decision(
                "worker_complete",
                f"completed {next_st.subtask_id}",
            )
            continue

        failure_summary = f"exit_code={result.get('exit_code')}"
        reason = terminal.get("terminal_reason")
        if reason:
            failure_summary += f"; reason={reason}"
        task.fail_subtask(
            next_st.subtask_id,
            result_summary=failure_summary,
        )
        task.record_decision(
            "worker_fail",
            f"failed {next_st.subtask_id}: {failure_summary}",
        )
        return


def cmd_start(args: argparse.Namespace) -> int:
    """Start a fresh task or route to ``cmd_resume`` if a log exists.

    ``task_events.jsonl`` presence is the route-to-resume signal. A
    fresh task loads the case TOML at
    ``<cases_dir>/<task_id>.toml``, seeds the optional policy
    override from ``<cases_dir>/<task_id>-policy.toml``, sets up the
    per-task workspace, runs the §8 main loop, and prints a final
    summary. ``--simulate-rate-status`` is forwarded into the loop
    so the §8 integration test can drive the policy machine's
    transitions deterministically.
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
    _seed_policy_override(task, cases_dir)
    _run_loop(
        task,
        state_root=state_root,
        workspaces_dir=workspaces_dir,
        simulate=getattr(args, "simulate_rate_status", None),
    )
    _print_summary(task)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Reload state via ``load_task``, set up workspace, drive the loop.

    Replays ``task_events.jsonl`` (resetting any ``in_flight``
    subtask back to ``pending`` and writing a ``task_resume``
    decision per §7), ensures the workspace exists (resume-safe per
    substrate doc §5), then runs the §8 main loop against the
    recovered state. ``--simulate-rate-status`` is forwarded into
    the loop the same way ``cmd_start`` does.
    """
    state_root, _cases_dir, workspaces_dir = _resolve_paths(args)
    task = task_mod.load_task(args.task, state_root=state_root)
    workspace_mod.setup_task_workspace(
        task.task_id, workspaces_dir=workspaces_dir,
    )
    _run_loop(
        task,
        state_root=state_root,
        workspaces_dir=workspaces_dir,
        simulate=getattr(args, "simulate_rate_status", None),
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
    """Record a ``policy_pause`` decision and exit cleanly.

    The pause subcommand is a manual signal from the operator: stop
    spawning workers so the current 5h or 7d window can drain. It
    does NOT spawn workers and does NOT call ``Policy.decide()`` —
    the operator's intent is authoritative. ``--simulate-rate-status``,
    when present, is recorded in the decision note for traceability
    only (the simulated state never leaks into the rate ledger).
    Loads the task with ``mark_resume=False`` so the pause itself
    does not also write a ``task_resume`` row.
    """
    state_root, _cases_dir, workspaces_dir = _resolve_paths(args)
    task = task_mod.load_task(
        args.task, state_root=state_root, mark_resume=False,
    )
    workspace_mod.setup_task_workspace(
        task.task_id, workspaces_dir=workspaces_dir,
    )
    simulate = getattr(args, "simulate_rate_status", None)
    if simulate:
        note = f"manual pause (simulate_rate_status={simulate})"
    else:
        note = "manual pause; current window drains naturally"
    task.record_decision("policy_pause", note)
    _print_summary(task)
    return 0


def cmd_halt(args: argparse.Namespace) -> int:
    """Record a ``policy_halt`` decision and exit cleanly.

    Mirrors ``cmd_pause`` but writes a ``policy_halt`` decision
    instead. Per the per-task help text, resume after a halt
    requires explicit operator intervention — the orchestrator
    will run the loop again on resume because halt is not a
    persistent gate, but the recorded decision flags the run for
    review.
    """
    state_root, _cases_dir, workspaces_dir = _resolve_paths(args)
    task = task_mod.load_task(
        args.task, state_root=state_root, mark_resume=False,
    )
    workspace_mod.setup_task_workspace(
        task.task_id, workspaces_dir=workspaces_dir,
    )
    simulate = getattr(args, "simulate_rate_status", None)
    if simulate:
        note = f"manual halt (simulate_rate_status={simulate})"
    else:
        note = "manual halt; resume requires explicit operator intervention"
    task.record_decision("policy_halt", note)
    _print_summary(task)
    return 0


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


def _add_simulate_flag(parser: argparse.ArgumentParser) -> None:
    """Attach the §8 ``--simulate-rate-status`` opt-in override."""
    parser.add_argument(
        "--simulate-rate-status",
        choices=SIMULATED_STATUSES,
        default=None,
        help=(
            "Override RateLedger.current_status() with a synthesised "
            "rate_status for this invocation only. Opt-in; production "
            "runs omit it. Used by Phase 3 §8 to exercise the policy "
            "machine's pause / wrap_up transitions deterministically."
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
    _add_simulate_flag(p_start)
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
    _add_simulate_flag(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_pause = subparsers.add_parser(
        "pause",
        help=(
            "Record a policy_pause decision and exit cleanly so the "
            "current 5h or 7d window can drain."
        ),
    )
    p_pause.add_argument("task", help="Task id.")
    _add_path_flags(p_pause)
    _add_simulate_flag(p_pause)
    p_pause.set_defaults(func=cmd_pause)

    p_halt = subparsers.add_parser(
        "halt",
        help=(
            "Record a policy_halt decision and stop the orchestrator. "
            "Resume requires explicit operator intervention."
        ),
    )
    p_halt.add_argument("task", help="Task id.")
    _add_path_flags(p_halt)
    _add_simulate_flag(p_halt)
    p_halt.set_defaults(func=cmd_halt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
