"""Long-horizon task state model for Phase 3 §6.

Defines the ``Task`` / ``Subtask`` / ``Decision`` dataclasses, the
per-task append-only event log at
``<state_root>/<task_id>/task_events.jsonl``, and the operations
that mutate ``Task`` in-memory state while writing one event row
per call.

Persistence layout mirrors §3 / §4: per-task directory, append-only
JSONL, every row stamped with
``capture_run_metadata(include_orchestrator_version=True)`` and a
synthetic ``event=...`` discriminator so this file coexists cleanly
with ``rate_limits.jsonl`` and ``buffer.jsonl`` in the same
directory.

Replay (§7's ``load_task``, not in this file) reads the JSONL
forward and reconstructs the ``Task`` by applying each event's
mutation in order. Each operation here is therefore designed so
its persisted event carries enough payload to reproduce its
in-memory effect.

Event types written by this module:

- ``task_create``       — from ``Task.from_toml``; carries the
  full bootstrap metadata (``goal``, ``workspace_path``,
  ``created_at``) and doubles as the ``task_create`` Decision
  record.
- ``enqueue_subtask``   — from ``Task.enqueue_subtask``;
  ``{subtask_id, prompt}``.
- ``start_subtask``     — from ``Task.start_subtask``;
  ``{subtask_id, started_at}``.
- ``complete_subtask``  — from ``Task.complete_subtask``;
  ``{subtask_id, finished_at, result_summary, cost_usd}``.
- ``fail_subtask``      — from ``Task.fail_subtask``;
  ``{subtask_id, finished_at, result_summary}``.
- ``decision``          — from ``Task.record_decision``;
  ``{kind, note, decision_timestamp}``. ``kind`` enumerates the
  canonical Decision kinds (policy_pause / wrap_up / halt,
  worker_spawn / complete / fail, task_create / task_resume).

§7 will add an additional ``task_resume`` event when ``load_task``
re-enqueues in-flight subtasks.
"""

import datetime
import json
import os
import tomllib
from dataclasses import dataclass, field
from typing import Literal

from integration import provenance

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_STATE_ROOT = os.path.join(SCRIPT_DIR, "state")
DEFAULT_WORKSPACES_DIR = os.path.join(REPO_ROOT, "integration", "workspace")

SubtaskStatus = Literal["pending", "in_flight", "done", "failed"]
DecisionKind = Literal[
    "policy_pause",
    "policy_wrap_up",
    "policy_halt",
    "worker_spawn",
    "worker_complete",
    "worker_fail",
    "task_create",
    "task_resume",
]

# Single source of truth for the Decision kind allow-list. Mirrors
# the ``DecisionKind`` Literal above; runtime checks compare against
# the frozenset.
_VALID_DECISION_KINDS: frozenset[str] = frozenset({
    "policy_pause",
    "policy_wrap_up",
    "policy_halt",
    "worker_spawn",
    "worker_complete",
    "worker_fail",
    "task_create",
    "task_resume",
})


class TaskError(ValueError):
    """Raised on malformed task TOML or invalid state transitions."""


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with explicit ``+00:00`` offset.

    Matches the format ``capture_run_metadata`` stamps so all
    timestamps in a row share a normal form for lexicographic
    comparison (used by §4's ``total_spent_since`` and §7's
    chronology checks).
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class Subtask:
    """One unit of work in a long-horizon task's queue.

    State transitions: pending → in_flight → done | failed. The
    transition operations on ``Task`` enforce this; direct
    construction is exposed for replay (§7) which sets fields
    explicitly from event payloads.
    """

    subtask_id: str
    prompt: str
    status: SubtaskStatus = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    result_summary: str | None = None
    cost_usd: float | None = None


@dataclass
class Decision:
    """One entry in the task's runtime decision log.

    Decisions are the high-level events the orchestrator wants to
    surface in a human-readable timeline (policy transitions, task
    bootstrap, resume). They live alongside subtask state
    transitions in ``task_events.jsonl`` but maintain a separate
    in-memory list on ``Task.decisions`` so the timeline is easy to
    consume programmatically.
    """

    timestamp: str
    kind: DecisionKind
    note: str


@dataclass
class Task:
    """Long-horizon task aggregate.

    Holds the static task definition (``task_id``, ``goal``,
    ``workspace_path``, ``created_at``), the dynamic subtask queue
    (``subtasks``, mutated by enqueue/start/complete/fail), and the
    decisions log (``record_decision``). Persistence is to
    ``<state_root>/<task_id>/task_events.jsonl``; the in-memory
    aggregate is kept in sync with every persisted event.

    Construct via ``Task.from_toml`` for fresh tasks (also bootstraps
    the JSONL log) or via direct field-setting for replay (§7's
    ``load_task`` will use the constructor that way).
    """

    task_id: str
    goal: str
    workspace_path: str
    created_at: str
    subtasks: list[Subtask] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    state_root: str = field(default=DEFAULT_STATE_ROOT)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_toml(
        cls,
        path: str,
        *,
        state_root: str | None = None,
        workspaces_dir: str | None = None,
    ) -> "Task":
        """Load task definition from TOML; create fresh task on disk.

        Validates the TOML schema (``task_id`` and ``goal`` required,
        each non-empty string; ``[[subtasks]]`` optional, each entry
        a ``{subtask_id, prompt}`` table). Computes
        ``workspace_path`` as ``<workspaces_dir>/<task_id>``;
        ``created_at`` is captured at call time.

        Persists exactly one ``task_create`` event (carrying
        ``goal`` / ``workspace_path`` / ``created_at`` plus the
        decision note) and one ``enqueue_subtask`` event per
        ``[[subtasks]]`` row. Also appends a ``task_create``
        ``Decision`` to the in-memory ``decisions`` list so the
        replay-equivalent timeline is unified.

        Tests pass ``state_root`` (where ``task_events.jsonl`` is
        written) and ``workspaces_dir`` (where ``workspace_path`` is
        rooted) to redirect persistence to a ``tmp_path`` rather
        than the canonical repo locations.
        """
        if not isinstance(path, str) or not path:
            raise TaskError(f"path must be a non-empty string; got {path!r}")
        if not os.path.isfile(path):
            raise TaskError(f"task TOML not found: {path}")
        try:
            with open(path, "rb") as fh:
                config = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise TaskError(f"malformed task TOML at {path}: {exc}") from exc

        task_id = config.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise TaskError(
                f"task TOML at {path} must define a non-empty 'task_id' "
                f"string; got {task_id!r}"
            )
        goal = config.get("goal")
        if not isinstance(goal, str) or not goal:
            raise TaskError(
                f"task TOML at {path} must define a non-empty 'goal' "
                f"string; got {goal!r}"
            )

        sr = state_root if state_root is not None else DEFAULT_STATE_ROOT
        wd = workspaces_dir if workspaces_dir is not None else DEFAULT_WORKSPACES_DIR
        workspace_path = os.path.join(wd, task_id)
        created_at = _now_iso()

        task = cls(
            task_id=task_id,
            goal=goal,
            workspace_path=workspace_path,
            created_at=created_at,
            state_root=sr,
        )

        # Single bootstrap event: it both initialises the on-disk log
        # and acts as the task_create Decision row. The
        # ``decision_note`` field carries the human-readable note so
        # §7's replay can append to ``decisions`` without
        # reconstructing it from the goal field.
        decision_note = f"goal: {goal}"
        task._append_event(
            "task_create",
            {
                "goal": goal,
                "workspace_path": workspace_path,
                "created_at": created_at,
                "decision_note": decision_note,
            },
        )
        task.decisions.append(
            Decision(timestamp=created_at, kind="task_create", note=decision_note),
        )

        # Validate and enqueue [[subtasks]] entries (each writes its
        # own enqueue_subtask event via the operation below).
        raw_subtasks = config.get("subtasks", [])
        if not isinstance(raw_subtasks, list):
            raise TaskError(
                f"task TOML at {path}: [[subtasks]] must be an array of "
                f"tables; got {raw_subtasks!r}"
            )
        for raw in raw_subtasks:
            if not isinstance(raw, dict):
                raise TaskError(
                    f"task TOML at {path}: each [[subtasks]] entry must be "
                    f"a table; got {raw!r}"
                )
            sid = raw.get("subtask_id")
            prompt = raw.get("prompt")
            if not isinstance(sid, str) or not sid:
                raise TaskError(
                    f"task TOML at {path}: each [[subtasks]].subtask_id "
                    f"must be a non-empty string; got {sid!r}"
                )
            if not isinstance(prompt, str) or not prompt:
                raise TaskError(
                    f"task TOML at {path}: each [[subtasks]].prompt must "
                    f"be a non-empty string; got {prompt!r}"
                )
            task.enqueue_subtask(sid, prompt)

        return task

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _events_path(self) -> str:
        return os.path.join(self.state_root, self.task_id, "task_events.jsonl")

    def _append_event(self, event_type: str, payload: dict) -> None:
        """Append one event row to ``task_events.jsonl``.

        Stamps every row with ``capture_run_metadata(
        include_orchestrator_version=True)`` so each event carries
        the same provenance fingerprint §3 / §4 use, plus the
        ``event`` discriminator and ``task_id`` for cross-file
        correlation.
        """
        path = self._events_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        metadata = provenance.capture_run_metadata(
            include_orchestrator_version=True,
        )
        row = {
            **metadata,
            "event": event_type,
            "task_id": self.task_id,
            **payload,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def _find_subtask(self, subtask_id: str) -> Subtask:
        for st in self.subtasks:
            if st.subtask_id == subtask_id:
                return st
        raise TaskError(
            f"subtask not found: task_id={self.task_id!r} "
            f"subtask_id={subtask_id!r}"
        )

    def enqueue_subtask(self, subtask_id: str, prompt: str) -> Subtask:
        """Append a new ``pending`` subtask; persist one event."""
        if not isinstance(subtask_id, str) or not subtask_id:
            raise TaskError(
                f"subtask_id must be a non-empty string; got {subtask_id!r}"
            )
        if not isinstance(prompt, str) or not prompt:
            raise TaskError(
                f"prompt must be a non-empty string; got {prompt!r}"
            )
        for existing in self.subtasks:
            if existing.subtask_id == subtask_id:
                raise TaskError(
                    f"duplicate subtask_id {subtask_id!r} on task "
                    f"{self.task_id!r}"
                )
        st = Subtask(subtask_id=subtask_id, prompt=prompt, status="pending")
        self.subtasks.append(st)
        self._append_event(
            "enqueue_subtask",
            {"subtask_id": subtask_id, "prompt": prompt},
        )
        return st

    def start_subtask(self, subtask_id: str) -> None:
        """Transition ``pending`` → ``in_flight``; persist one event."""
        st = self._find_subtask(subtask_id)
        if st.status != "pending":
            raise TaskError(
                f"start_subtask requires status='pending'; subtask "
                f"{subtask_id!r} is {st.status!r}"
            )
        st.status = "in_flight"
        st.started_at = _now_iso()
        self._append_event(
            "start_subtask",
            {"subtask_id": subtask_id, "started_at": st.started_at},
        )

    def complete_subtask(
        self,
        subtask_id: str,
        *,
        result_summary: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Transition ``in_flight`` → ``done``; persist one event."""
        st = self._find_subtask(subtask_id)
        if st.status != "in_flight":
            raise TaskError(
                f"complete_subtask requires status='in_flight'; subtask "
                f"{subtask_id!r} is {st.status!r}"
            )
        if result_summary is not None and not isinstance(result_summary, str):
            raise TaskError(
                f"result_summary must be a string or None; got "
                f"{result_summary!r}"
            )
        if cost_usd is not None and (
            isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
        ):
            raise TaskError(
                f"cost_usd must be numeric or None; got {cost_usd!r}"
            )
        st.status = "done"
        st.finished_at = _now_iso()
        st.result_summary = result_summary
        st.cost_usd = (
            float(cost_usd) if cost_usd is not None else None
        )
        self._append_event(
            "complete_subtask",
            {
                "subtask_id": subtask_id,
                "finished_at": st.finished_at,
                "result_summary": result_summary,
                "cost_usd": st.cost_usd,
            },
        )

    def fail_subtask(
        self,
        subtask_id: str,
        *,
        result_summary: str | None = None,
    ) -> None:
        """Transition ``in_flight`` → ``failed``; persist one event."""
        st = self._find_subtask(subtask_id)
        if st.status != "in_flight":
            raise TaskError(
                f"fail_subtask requires status='in_flight'; subtask "
                f"{subtask_id!r} is {st.status!r}"
            )
        if result_summary is not None and not isinstance(result_summary, str):
            raise TaskError(
                f"result_summary must be a string or None; got "
                f"{result_summary!r}"
            )
        st.status = "failed"
        st.finished_at = _now_iso()
        st.result_summary = result_summary
        self._append_event(
            "fail_subtask",
            {
                "subtask_id": subtask_id,
                "finished_at": st.finished_at,
                "result_summary": result_summary,
            },
        )

    def record_decision(self, kind: str, note: str) -> Decision:
        """Append a Decision to ``decisions``; persist one event.

        ``kind`` must be one of the canonical Decision kinds
        (policy_pause / wrap_up / halt, worker_spawn / complete /
        fail, task_create / task_resume). The PLAN §6 enumeration
        is the closed allow-list — unknown kinds raise
        ``TaskError`` so the timeline cannot accumulate
        free-form strings that future tooling has to defend
        against.
        """
        if kind not in _VALID_DECISION_KINDS:
            raise TaskError(
                f"unknown decision kind {kind!r}; allowed: "
                f"{sorted(_VALID_DECISION_KINDS)}"
            )
        if not isinstance(note, str):
            raise TaskError(f"note must be a string; got {note!r}")
        timestamp = _now_iso()
        decision = Decision(timestamp=timestamp, kind=kind, note=note)
        self.decisions.append(decision)
        self._append_event(
            "decision",
            {"kind": kind, "note": note, "decision_timestamp": timestamp},
        )
        return decision
