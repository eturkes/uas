"""Phase 5 §4 — human-readable resume summary writer.

Renders a Markdown digest of a long-horizon task's state suitable
for an operator deciding whether to resume / adjust / abort after a
multi-hour gap. Written to
``<state_root>/<task_id>/resume_summary.md`` at the end of every
CLI subcommand (``start`` / ``resume`` / ``status`` / ``pause`` /
``halt``) so the file always reflects the most recent invocation's
wrap-up. Per the §4 PLAN, a single attach point per CLI subcommand
keeps the writer logic out of ``_run_loop``'s multi-return body.

Inputs are the existing ``Task`` (live or reconstructed via
``load_task``) plus the on-disk ``buffer.jsonl`` ledger; nothing
new is persisted apart from the Markdown file itself. The
``rate_limits.jsonl`` ledger is intentionally not consulted —
soft-cap activity already surfaces via the ``policy_pause`` /
``policy_wrap_up`` decisions in the timeline.

Format decision (§4 PLAN explicit ask). Markdown only for v1; a
JSON sidecar was deferred until §6 surfaces a programmatic
consumer. Constraints honoured: stdlib only (no new dependencies),
reads only existing artefacts (``task_events.jsonl`` via the
in-memory ``Task`` and ``buffer.jsonl`` via the existing
``BufferLedger`` API).

"Since last invocation" boundary. Defined as the timestamp of the
most-recent ``task_resume`` decision; falls back to the
``task_create`` decision when the task has never been resumed; falls
back to ``task.created_at`` when neither is on the timeline (a
shape that should not occur in normal use but is tolerated). The
digest's "Decisions this invocation" and "Spend this invocation"
sections are scoped against this boundary.
"""

import datetime
import os

from orchestrator.buffer_ledger import BufferLedger
from orchestrator.task import Task

SUMMARY_FILENAME = "resume_summary.md"

_INVOCATION_BOUNDARY_KINDS = ("task_resume", "task_create")


def write_resume_summary(
    task: Task,
    *,
    state_root: str,
    buffer_ledger: BufferLedger | None = None,
) -> str:
    """Render the digest and write it to disk; return the file path.

    ``state_root`` matches the per-task convention used by every
    other artefact in ``<state_root>/<task_id>/``. Creates the
    directory if missing (the writer is callable on a fresh task
    that hasn't yet had a state directory built — e.g., a hypothetical
    operator running ``cmd_status`` against a partially-initialised
    task). ``buffer_ledger`` is injectable so tests can pre-seed
    spend rows; production callers pass ``None`` and accept the
    default ``BufferLedger(state_root=state_root)`` instance.
    """
    if buffer_ledger is None:
        buffer_ledger = BufferLedger(state_root=state_root)
    body = render_resume_summary(task, buffer_ledger=buffer_ledger)
    out_dir = os.path.join(state_root, task.task_id)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, SUMMARY_FILENAME)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return out_path


def render_resume_summary(
    task: Task,
    *,
    buffer_ledger: BufferLedger,
) -> str:
    """Return the Markdown body for the digest. Pure function.

    Separated from ``write_resume_summary`` so tests can assert
    content shape without round-tripping through the filesystem.
    The writer above is intentionally a thin wrapper.
    """
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    invocation_iso = _invocation_boundary(task)

    cumulative_reported = buffer_ledger.total_spent_reported(task.task_id)
    cumulative_local = buffer_ledger.total_spent(task.task_id)
    invocation_reported = buffer_ledger.total_spent_reported_since(
        task.task_id, since_iso=invocation_iso,
    )

    counts = _count_statuses(task.subtasks)

    lines: list[str] = []
    lines.append(f"# Resume summary — `{task.task_id}`")
    lines.append("")
    lines.append(f"_Generated: {generated_at}_")
    lines.append("")
    lines.append(f"**Goal.** {task.goal}")
    lines.append("")

    # Subtasks block.
    lines.append("## Subtasks")
    lines.append("")
    lines.append(
        f"- Total: {len(task.subtasks)} "
        f"({counts['pending']} pending, "
        f"{counts['in_flight']} in-flight, "
        f"{counts['done']} done, "
        f"{counts['failed']} failed)"
    )
    lines.append("")

    if task.stages:
        lines.append("### By stage")
        lines.append("")
        for stage in task.stages:
            stage_subtasks = [
                s for s in task.subtasks if s.stage_id == stage.stage_id
            ]
            if not stage_subtasks:
                lines.append(
                    f"- **`{stage.stage_id}`**"
                    f"{_stage_name_suffix(stage)} — no subtasks"
                )
                continue
            sc = _count_statuses(stage_subtasks)
            lines.append(
                f"- **`{stage.stage_id}`**{_stage_name_suffix(stage)}: "
                f"{sc['pending']} pending, "
                f"{sc['in_flight']} in-flight, "
                f"{sc['done']} done, "
                f"{sc['failed']} failed"
            )
        ungrouped = [s for s in task.subtasks if s.stage_id is None]
        if ungrouped:
            uc = _count_statuses(ungrouped)
            lines.append(
                f"- _ungrouped_: "
                f"{uc['pending']} pending, "
                f"{uc['in_flight']} in-flight, "
                f"{uc['done']} done, "
                f"{uc['failed']} failed"
            )
        lines.append("")

    failed = [s for s in task.subtasks if s.status == "failed"]
    if failed:
        lines.append("### Failed subtasks")
        lines.append("")
        for st in failed:
            summary = _truncate_first_line(st.result_summary)
            lines.append(f"- `{st.subtask_id}` — {summary}")
        lines.append("")

    in_flight = [s for s in task.subtasks if s.status == "in_flight"]
    if in_flight:
        lines.append("### In-flight subtasks")
        lines.append("")
        for st in in_flight:
            started = st.started_at or "(no start timestamp)"
            lines.append(f"- `{st.subtask_id}` — started {started}")
        lines.append("")

    pending = [s for s in task.subtasks if s.status == "pending"]
    if pending:
        nxt = pending[0]
        lines.append("### Next pending subtask")
        lines.append("")
        stage_suffix = (
            f" (stage `{nxt.stage_id}`)" if nxt.stage_id else ""
        )
        lines.append(f"- `{nxt.subtask_id}`{stage_suffix}")
        if len(pending) > 1:
            lines.append(f"- ... plus {len(pending) - 1} more pending")
        lines.append("")

    # Spend block.
    lines.append("## Spend")
    lines.append("")
    lines.append(
        f"- Cumulative (Claude-reported): ${cumulative_reported:.4f}"
    )
    lines.append(
        f"- Cumulative (locally priced): ${cumulative_local:.4f}"
    )
    lines.append(
        f"- This invocation (Claude-reported): ${invocation_reported:.4f}"
    )
    lines.append("")

    # Decisions block.
    lines.append("## Decisions this invocation")
    lines.append("")
    invocation_decisions = [
        d for d in task.decisions if d.timestamp >= invocation_iso
    ]
    if invocation_decisions:
        for d in invocation_decisions:
            lines.append(f"- `{d.kind}` — {d.note}")
    else:
        lines.append("(none recorded yet)")
    lines.append("")

    # Suggested next action.
    lines.append("## Suggested next action")
    lines.append("")
    lines.append(_suggest_next_action(task))
    lines.append("")

    return "\n".join(lines)


def _invocation_boundary(task: Task) -> str:
    """Return the ISO timestamp marking the start of this invocation.

    Walks the in-memory ``decisions`` list in reverse and picks the
    first ``task_resume`` or ``task_create``. Falls back to
    ``task.created_at`` when neither is present (a defensive guard;
    real tasks always have a ``task_create`` row at index 0).
    """
    for d in reversed(task.decisions):
        if d.kind in _INVOCATION_BOUNDARY_KINDS:
            return d.timestamp
    return task.created_at or "1970-01-01T00:00:00+00:00"


def _count_statuses(subtasks) -> dict:
    """Count subtasks by status; return a four-key dict."""
    return {
        "pending": sum(1 for s in subtasks if s.status == "pending"),
        "in_flight": sum(1 for s in subtasks if s.status == "in_flight"),
        "done": sum(1 for s in subtasks if s.status == "done"),
        "failed": sum(1 for s in subtasks if s.status == "failed"),
    }


def _stage_name_suffix(stage) -> str:
    """Return ``" — name"`` if the stage has a non-empty name, else ``""``."""
    return f" — {stage.name}" if stage.name else ""


def _truncate_first_line(text: str | None, *, limit: int = 200) -> str:
    """Return the first line of ``text``, truncated to ``limit`` chars."""
    if not text:
        return "(no summary)"
    first = text.splitlines()[0]
    if len(first) > limit:
        return first[:limit] + "..."
    return first


def _suggest_next_action(task: Task) -> str:
    """Return a single-line recommendation for the operator.

    Decision-priority order:

    1. No decisions at all → suggest ``start``.
    2. Last decision kind dictates the verb (pause / wrap_up /
       halt / fail / auto_resume each get a tailored line).
    3. Otherwise, fall through to subtask-state-derived advice
       (queue empty + nothing in flight + nothing failed → task
       complete; mixed states → pick the most informative
       recommendation).
    """
    if not task.decisions:
        return f"Run `./uas-orchestrate start {task.task_id}`."

    last = task.decisions[-1]
    pending_count = sum(1 for s in task.subtasks if s.status == "pending")
    in_flight_count = sum(1 for s in task.subtasks if s.status == "in_flight")
    failed_count = sum(1 for s in task.subtasks if s.status == "failed")

    if last.kind == "policy_pause":
        return (
            "Wait for the 5-hour window to drain, then run "
            f"`./uas-orchestrate resume {task.task_id}` "
            "(or rely on auto-resume if `[auto_resume] enabled = true` "
            "in the per-task `policy.toml`)."
        )
    if last.kind == "policy_wrap_up":
        return (
            "Weekly cap approaching; resume after the 7-day window "
            f"resets with `./uas-orchestrate resume {task.task_id}`."
        )
    if last.kind == "policy_halt":
        return (
            "Hard-stop reached. Review spend and the per-task "
            "`policy.toml` `hard_stop_usd` threshold before resuming "
            f"with `./uas-orchestrate resume {task.task_id}`."
        )
    if last.kind == "worker_fail":
        return (
            "Worker failed. Review the failure summary above; address "
            "the root cause, then either re-run with "
            f"`./uas-orchestrate resume {task.task_id}` or halt to "
            "investigate further."
        )
    if last.kind == "policy_auto_resume":
        return (
            "Auto-resume just woke the loop. The next iteration's "
            "policy verdict will determine whether the loop spawns or "
            "pauses again."
        )

    if (
        pending_count == 0
        and in_flight_count == 0
        and failed_count == 0
    ):
        return "Task complete. No pending subtasks remain."
    if pending_count == 0 and in_flight_count == 0:
        return (
            "All non-failed subtasks done; failed subtasks remain. "
            "Review failures before re-enqueuing."
        )
    if last.kind == "task_create":
        return f"Run `./uas-orchestrate start {task.task_id}`."
    return (
        f"Run `./uas-orchestrate resume {task.task_id}` to pick up "
        "the next pending subtask."
    )
