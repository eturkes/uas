"""Long-horizon task state model for Phase 3 §6 + §7 (extended in Phase 5 §2).

Defines the ``Task`` / ``Subtask`` / ``Stage`` / ``Decision``
dataclasses, the per-task append-only event log at
``<state_root>/<task_id>/task_events.jsonl``, the operations that
mutate ``Task`` in-memory state while writing one event row per
call, and the §7 ``load_task`` replay that reconstructs a ``Task``
from its persisted log.

Persistence layout mirrors §3 / §4: per-task directory, append-only
JSONL, every row stamped with
``capture_run_metadata(include_orchestrator_version=True)`` and a
synthetic ``event=...`` discriminator so this file coexists cleanly
with ``rate_limits.jsonl`` and ``buffer.jsonl`` in the same
directory.

Replay (``load_task``) reads the JSONL forward and reconstructs the
``Task`` by applying each event's mutation in order. Each operation
here is therefore designed so its persisted event carries enough
payload to reproduce its in-memory effect.

Event types written by this module:

- ``task_create``       — from ``Task.from_toml``; carries the
  full bootstrap metadata (``goal``, ``workspace_path``,
  ``created_at``, plus the Phase 5 §2 ``stages`` list) and
  doubles as the ``task_create`` Decision record.
- ``enqueue_subtask``   — from ``Task.enqueue_subtask``;
  ``{subtask_id, prompt, stage_id}`` (``stage_id`` added in
  Phase 5 §2; ``None`` for tasks without ``[[stages]]``).
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

Phase 5 §2 schema additions (additive; backward-compatible). The
TOML loader accepts an optional ``[[stages]]`` array of tables and
an optional ``stage_id`` field on each ``[[subtasks]]`` entry.
Tasks without ``[[stages]]`` parse exactly as before with
``Task.stages = []`` and every ``Subtask.stage_id = None``. The
``stages`` list is persisted in the ``task_create`` event so
``load_task`` reconstructs it across resume boundaries; per-subtask
``stage_id`` is persisted in the ``enqueue_subtask`` event for the
same reason. The §2 schema commits the declarative shape only —
runtime eligibility / dependency-aware ordering is left to later
phases.

Per-event resume gate (§7). Every persisted row carries a
``survives_git_sha_flip: bool`` field (default ``True``). On
replay, rows with ``survives_git_sha_flip == False`` whose recorded
``git_sha`` differs from the current commit are dropped with a
stderr note — the per-event escape hatch for events that
explicitly depend on tree state. ``True`` is the safe default per
``docs/substrate.md`` §6: a too-strict gate would erase progress
across normal long-horizon edits.
"""

import datetime
import json
import os
import sys
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


def _validate_stages(raw_stages, path: str) -> list[dict]:
    """Validate the optional ``[[stages]]`` array (Phase 5 §2).

    Each entry must be a table with a non-empty ``stage_id``;
    ``stage_id`` is unique across the task. Optional ``name`` is a
    string (default ``""``); optional ``depends_on`` is a list of
    non-empty strings each referencing another stage's ``stage_id``;
    optional ``expected_duration_seconds`` and
    ``expected_spend_usd`` are non-negative numbers (or absent).
    Self-references are rejected; the dependency graph is checked
    for cycles via three-color DFS.

    Returns a list of dicts mirroring the Stage dataclass field
    set; ``Task.from_toml`` constructs ``Stage(**rec)`` from each
    record and persists the same dicts in the ``task_create`` event
    payload so ``load_task`` can reconstruct in-memory ``Stage``
    instances on resume.
    """
    if not isinstance(raw_stages, list):
        raise TaskError(
            f"task TOML at {path}: [[stages]] must be an array of "
            f"tables; got {raw_stages!r}"
        )

    records: list[dict] = []
    seen_ids: set[str] = set()
    for raw in raw_stages:
        if not isinstance(raw, dict):
            raise TaskError(
                f"task TOML at {path}: each [[stages]] entry must be "
                f"a table; got {raw!r}"
            )
        sid = raw.get("stage_id")
        if not isinstance(sid, str) or not sid:
            raise TaskError(
                f"task TOML at {path}: each [[stages]].stage_id must "
                f"be a non-empty string; got {sid!r}"
            )
        if sid in seen_ids:
            raise TaskError(
                f"task TOML at {path}: duplicate stage_id {sid!r}"
            )
        seen_ids.add(sid)

        name = raw.get("name", "")
        if not isinstance(name, str):
            raise TaskError(
                f"task TOML at {path}: stage {sid!r} name must be a "
                f"string; got {name!r}"
            )

        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            raise TaskError(
                f"task TOML at {path}: stage {sid!r} depends_on must "
                f"be a list; got {depends_on!r}"
            )
        for dep in depends_on:
            if not isinstance(dep, str) or not dep:
                raise TaskError(
                    f"task TOML at {path}: stage {sid!r} depends_on "
                    f"entries must be non-empty strings; got {dep!r}"
                )

        expected_dur = raw.get("expected_duration_seconds")
        if expected_dur is not None and (
            isinstance(expected_dur, bool)
            or not isinstance(expected_dur, (int, float))
            or expected_dur < 0
        ):
            raise TaskError(
                f"task TOML at {path}: stage {sid!r} "
                f"expected_duration_seconds must be a non-negative "
                f"number or absent; got {expected_dur!r}"
            )

        expected_spend = raw.get("expected_spend_usd")
        if expected_spend is not None and (
            isinstance(expected_spend, bool)
            or not isinstance(expected_spend, (int, float))
            or expected_spend < 0
        ):
            raise TaskError(
                f"task TOML at {path}: stage {sid!r} "
                f"expected_spend_usd must be a non-negative number or "
                f"absent; got {expected_spend!r}"
            )

        records.append({
            "stage_id": sid,
            "name": name,
            "depends_on": list(depends_on),
            "expected_duration_seconds": (
                float(expected_dur) if expected_dur is not None else None
            ),
            "expected_spend_usd": (
                float(expected_spend) if expected_spend is not None else None
            ),
        })

    # depends_on reference + self-reference checks.
    for rec in records:
        for dep in rec["depends_on"]:
            if dep == rec["stage_id"]:
                raise TaskError(
                    f"task TOML at {path}: stage {rec['stage_id']!r} "
                    f"depends on itself"
                )
            if dep not in seen_ids:
                raise TaskError(
                    f"task TOML at {path}: stage {rec['stage_id']!r} "
                    f"depends on unknown stage_id {dep!r}"
                )

    # Cycle detection via three-color DFS.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {rec["stage_id"]: WHITE for rec in records}
    graph = {rec["stage_id"]: list(rec["depends_on"]) for rec in records}

    def visit(sid: str) -> None:
        if color[sid] == BLACK:
            return
        if color[sid] == GRAY:
            raise TaskError(
                f"task TOML at {path}: stage dependency cycle "
                f"detected at stage_id {sid!r}"
            )
        color[sid] = GRAY
        for dep in graph[sid]:
            visit(dep)
        color[sid] = BLACK

    for sid in graph:
        visit(sid)

    return records


@dataclass
class Stage:
    """One stage in a multi-stage long-horizon task (Phase 5 §2).

    Stages group ``Subtask``s and express dependency edges between
    groups so a task spec can declare e.g. "synthesis depends on the
    prior research stages" without duplicating the dependency on
    every subtask. The schema is additive — tasks that omit
    ``[[stages]]`` continue to parse with ``Task.stages = []`` and
    every ``Subtask.stage_id = None``.

    ``depends_on`` lists the ``stage_id`` of stages that must
    complete before this stage's subtasks become eligible. The
    ``Task.from_toml`` validator enforces uniqueness, reference
    integrity, and acyclicity at load time; the dataclass itself
    holds whatever the parser produced.

    ``expected_duration_seconds`` and ``expected_spend_usd`` are
    optional planning hints feeding §6's real-time divergence
    detection during the §1 long-horizon run; both default ``None``
    when absent.
    """

    stage_id: str
    name: str = ""
    depends_on: list[str] = field(default_factory=list)
    expected_duration_seconds: float | None = None
    expected_spend_usd: float | None = None


@dataclass
class Subtask:
    """One unit of work in a long-horizon task's queue.

    State transitions: pending → in_flight → done | failed. The
    transition operations on ``Task`` enforce this; direct
    construction is exposed for replay (§7) which sets fields
    explicitly from event payloads.

    ``stage_id`` (Phase 5 §2) optionally references a ``Stage`` in
    the parent ``Task.stages`` list; ``None`` means the subtask is
    not grouped (the pre-§2 default).
    """

    subtask_id: str
    prompt: str
    stage_id: str | None = None
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
    stages: list[Stage] = field(default_factory=list)
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

        Validates the TOML schema:

        - ``task_id`` and ``goal``: required non-empty strings.
        - ``[[stages]]``: optional array of tables (Phase 5 §2). Each
          entry needs a non-empty ``stage_id`` (unique across stages);
          optional ``name`` (string, defaults to ``""``);
          optional ``depends_on`` (list of strings each referencing
          another stage's ``stage_id``, no self-reference, no cycles);
          optional ``expected_duration_seconds`` /
          ``expected_spend_usd`` (non-negative numbers or absent).
        - ``[[subtasks]]``: optional array of tables. Each entry needs
          non-empty ``subtask_id`` (unique within the task) and
          ``prompt``; optional ``stage_id`` referencing a declared
          ``[[stages]].stage_id``.

        Computes ``workspace_path`` as ``<workspaces_dir>/<task_id>``;
        ``created_at`` is captured at call time.

        Persists exactly one ``task_create`` event (carrying
        ``goal`` / ``workspace_path`` / ``created_at`` / ``stages``
        plus the decision note) and one ``enqueue_subtask`` event per
        ``[[subtasks]]`` row (each carrying the optional
        ``stage_id``). Also appends a ``task_create`` ``Decision`` to
        the in-memory ``decisions`` list so the replay-equivalent
        timeline is unified.

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

        # Phase 5 §2: validate optional [[stages]] before constructing
        # the Task so the task_create event can persist them in the
        # bootstrap row that load_task replays.
        stage_records = _validate_stages(config.get("stages", []), path)
        seen_stage_ids = {rec["stage_id"] for rec in stage_records}

        sr = state_root if state_root is not None else DEFAULT_STATE_ROOT
        wd = workspaces_dir if workspaces_dir is not None else DEFAULT_WORKSPACES_DIR
        workspace_path = os.path.join(wd, task_id)
        created_at = _now_iso()

        task = cls(
            task_id=task_id,
            goal=goal,
            workspace_path=workspace_path,
            created_at=created_at,
            stages=[Stage(**rec) for rec in stage_records],
            state_root=sr,
        )

        # Single bootstrap event: it both initialises the on-disk log
        # and acts as the task_create Decision row. The
        # ``decision_note`` field carries the human-readable note so
        # §7's replay can append to ``decisions`` without
        # reconstructing it from the goal field. ``stages`` is
        # serialised as a list of dicts mirroring the Stage dataclass
        # so load_task can reconstruct the in-memory Stage objects
        # without having to re-parse the original TOML.
        decision_note = f"goal: {goal}"
        task._append_event(
            "task_create",
            {
                "goal": goal,
                "workspace_path": workspace_path,
                "created_at": created_at,
                "decision_note": decision_note,
                "stages": stage_records,
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
            stage_id = raw.get("stage_id")
            if stage_id is not None:
                if not isinstance(stage_id, str) or not stage_id:
                    raise TaskError(
                        f"task TOML at {path}: subtask {sid!r} stage_id "
                        f"must be a non-empty string or absent; got "
                        f"{stage_id!r}"
                    )
                if stage_id not in seen_stage_ids:
                    raise TaskError(
                        f"task TOML at {path}: subtask {sid!r} references "
                        f"unknown stage_id {stage_id!r}"
                    )
            task.enqueue_subtask(sid, prompt, stage_id=stage_id)

        return task

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _events_path(self) -> str:
        return os.path.join(self.state_root, self.task_id, "task_events.jsonl")

    def _append_event(
        self,
        event_type: str,
        payload: dict,
        *,
        survives_git_sha_flip: bool = True,
    ) -> None:
        """Append one event row to ``task_events.jsonl``.

        Stamps every row with ``capture_run_metadata(
        include_orchestrator_version=True)`` so each event carries
        the same provenance fingerprint §3 / §4 use, plus the
        ``event`` discriminator and ``task_id`` for cross-file
        correlation.

        ``survives_git_sha_flip`` is the per-event resume gate
        consumed by ``load_task``. Default ``True`` — events
        survive normal long-horizon code edits. Callers writing
        events that explicitly depend on tree state pass ``False``;
        ``load_task`` will drop those events on replay if the
        current ``git_sha`` differs from the recorded one.
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
            "survives_git_sha_flip": survives_git_sha_flip,
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

    def enqueue_subtask(
        self,
        subtask_id: str,
        prompt: str,
        *,
        stage_id: str | None = None,
    ) -> Subtask:
        """Append a new ``pending`` subtask; persist one event.

        ``stage_id`` (Phase 5 §2) is the optional reference into
        ``self.stages``. Cross-stage validation lives in
        ``Task.from_toml`` — this operation accepts any ``None`` /
        non-empty-string value so direct callers (and replay) don't
        have to maintain the stages set separately.
        """
        if not isinstance(subtask_id, str) or not subtask_id:
            raise TaskError(
                f"subtask_id must be a non-empty string; got {subtask_id!r}"
            )
        if not isinstance(prompt, str) or not prompt:
            raise TaskError(
                f"prompt must be a non-empty string; got {prompt!r}"
            )
        if stage_id is not None and (
            not isinstance(stage_id, str) or not stage_id
        ):
            raise TaskError(
                f"stage_id must be a non-empty string or None; got {stage_id!r}"
            )
        for existing in self.subtasks:
            if existing.subtask_id == subtask_id:
                raise TaskError(
                    f"duplicate subtask_id {subtask_id!r} on task "
                    f"{self.task_id!r}"
                )
        st = Subtask(
            subtask_id=subtask_id,
            prompt=prompt,
            stage_id=stage_id,
            status="pending",
        )
        self.subtasks.append(st)
        self._append_event(
            "enqueue_subtask",
            {
                "subtask_id": subtask_id,
                "prompt": prompt,
                "stage_id": stage_id,
            },
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


# ---------------------------------------------------------------------------
# §7 — Resume-from-state
# ---------------------------------------------------------------------------


def _events_path_for(state_root: str, task_id: str) -> str:
    return os.path.join(state_root, task_id, "task_events.jsonl")


def load_task(
    task_id: str,
    *,
    state_root: str | None = None,
    mark_resume: bool = True,
) -> Task:
    """Reconstruct a ``Task`` from its persisted ``task_events.jsonl``.

    Reads the log forward and applies each event's recorded
    mutation to a fresh ``Task`` in order. Replay is deliberately
    tolerant: blank / malformed / unknown-event lines are skipped,
    state transitions are applied directly without the write-path
    validators (``start_subtask`` does not require ``status ==
    'pending'`` here, etc.) so a normal start → kill → restart
    sequence does not crash on the original ``start_subtask``
    event.

    Per-event resume gate. Each row carries
    ``survives_git_sha_flip`` (default ``True`` for missing,
    matching pre-§7 events). Rows with the field set to ``False``
    are dropped when their recorded ``git_sha`` differs from the
    current commit; a stderr note records each drop.

    End-of-replay sweep. Any subtask still ``in_flight`` when the
    log is exhausted is re-enqueued as ``pending`` with
    ``started_at`` cleared. A ``task_resume`` decision is then
    appended to the in-memory ``decisions`` list AND persisted
    to the log so future replays observe the resumption boundary.
    Subsequent ``start_subtask`` events that would have re-set the
    same subtask to ``in_flight`` are tolerated by the
    no-validator replay.

    ``mark_resume=False`` skips the in-flight reset and the
    ``task_resume`` write, returning a read-only snapshot of the
    persisted state. Used by ``cmd_status`` so a status print does
    not pollute the log with a resume marker.

    Raises ``TaskError`` if the log is missing or contains no
    ``task_create`` event (an unrecoverable corruption).
    """
    if not isinstance(task_id, str) or not task_id:
        raise TaskError(f"task_id must be a non-empty string; got {task_id!r}")

    sr = state_root if state_root is not None else DEFAULT_STATE_ROOT
    events_path = _events_path_for(sr, task_id)
    if not os.path.isfile(events_path):
        raise TaskError(f"task_events.jsonl not found: {events_path}")

    current_sha = provenance._git_capture(["rev-parse", "HEAD"])

    task: Task | None = None
    skipped_sha_drift = 0

    with open(events_path, "r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Forgiving reader: malformed rows are dropped silently.
                # Same posture as RateLedger / BufferLedger.
                continue
            if not isinstance(row, dict):
                continue

            survives = row.get("survives_git_sha_flip", True)
            if not survives and row.get("git_sha") != current_sha:
                print(
                    f"[load_task] dropping event at line {lineno}: "
                    f"git_sha mismatch (recorded="
                    f"{row.get('git_sha')!r}, current="
                    f"{current_sha!r}, event={row.get('event')!r})",
                    file=sys.stderr,
                )
                skipped_sha_drift += 1
                continue

            event_type = row.get("event")

            if event_type == "task_create":
                if task is not None:
                    print(
                        f"[load_task] duplicate task_create event at "
                        f"line {lineno}; ignoring",
                        file=sys.stderr,
                    )
                    continue
                task = Task(
                    task_id=task_id,
                    goal=row.get("goal", ""),
                    workspace_path=row.get("workspace_path", ""),
                    created_at=row.get("created_at", ""),
                    state_root=sr,
                )
                # Phase 5 §2: reconstruct stages from the persisted
                # task_create row. Pre-§2 logs lack the field; the
                # forgiving reader leaves task.stages empty.
                raw_stages = row.get("stages", [])
                if isinstance(raw_stages, list):
                    for raw_stage in raw_stages:
                        if not isinstance(raw_stage, dict):
                            continue
                        sid = raw_stage.get("stage_id")
                        if not isinstance(sid, str) or not sid:
                            continue
                        name = raw_stage.get("name", "")
                        if not isinstance(name, str):
                            name = ""
                        deps_raw = raw_stage.get("depends_on", [])
                        deps = (
                            [d for d in deps_raw if isinstance(d, str) and d]
                            if isinstance(deps_raw, list)
                            else []
                        )
                        exp_dur = raw_stage.get("expected_duration_seconds")
                        if not isinstance(exp_dur, (int, float)) or isinstance(
                            exp_dur, bool
                        ):
                            exp_dur = None
                        exp_spend = raw_stage.get("expected_spend_usd")
                        if not isinstance(exp_spend, (int, float)) or isinstance(
                            exp_spend, bool
                        ):
                            exp_spend = None
                        task.stages.append(Stage(
                            stage_id=sid,
                            name=name,
                            depends_on=deps,
                            expected_duration_seconds=(
                                float(exp_dur) if exp_dur is not None else None
                            ),
                            expected_spend_usd=(
                                float(exp_spend) if exp_spend is not None else None
                            ),
                        ))
                task.decisions.append(
                    Decision(
                        timestamp=row.get("created_at", ""),
                        kind="task_create",
                        note=row.get("decision_note", ""),
                    ),
                )
                continue

            if task is None:
                # Events before the bootstrap row are unrecoverable in
                # isolation; skip until task_create lands.
                continue

            if event_type == "enqueue_subtask":
                sid = row.get("subtask_id")
                prompt = row.get("prompt")
                if not isinstance(sid, str) or not isinstance(prompt, str):
                    continue
                # Phase 5 §2: optional stage_id; pre-§2 rows lack it.
                stage_id = row.get("stage_id")
                if not (isinstance(stage_id, str) and stage_id):
                    stage_id = None
                task.subtasks.append(
                    Subtask(
                        subtask_id=sid,
                        prompt=prompt,
                        stage_id=stage_id,
                        status="pending",
                    ),
                )
            elif event_type == "start_subtask":
                st = _replay_lookup(task, row, lineno)
                if st is not None:
                    st.status = "in_flight"
                    st.started_at = row.get("started_at")
            elif event_type == "complete_subtask":
                st = _replay_lookup(task, row, lineno)
                if st is not None:
                    st.status = "done"
                    st.finished_at = row.get("finished_at")
                    st.result_summary = row.get("result_summary")
                    cost = row.get("cost_usd")
                    st.cost_usd = (
                        float(cost) if isinstance(cost, (int, float))
                        and not isinstance(cost, bool) else None
                    )
            elif event_type == "fail_subtask":
                st = _replay_lookup(task, row, lineno)
                if st is not None:
                    st.status = "failed"
                    st.finished_at = row.get("finished_at")
                    st.result_summary = row.get("result_summary")
            elif event_type == "decision":
                kind = row.get("kind")
                if kind in _VALID_DECISION_KINDS:
                    task.decisions.append(
                        Decision(
                            timestamp=row.get("decision_timestamp", ""),
                            kind=kind,
                            note=row.get("note", ""),
                        ),
                    )
            # else: unknown event_type — silently skip.

    if task is None:
        raise TaskError(
            f"task_events.jsonl at {events_path} contained no "
            f"task_create event; cannot reconstruct Task"
        )

    if not mark_resume:
        return task

    # End-of-replay sweep: re-enqueue any subtask still in_flight.
    re_enqueued: list[str] = []
    for st in task.subtasks:
        if st.status == "in_flight":
            st.status = "pending"
            st.started_at = None
            re_enqueued.append(st.subtask_id)

    if re_enqueued:
        note = (
            "resumed; re-enqueued in-flight subtasks: "
            + ", ".join(re_enqueued)
        )
    else:
        note = "resumed; no in-flight subtasks"
    if skipped_sha_drift:
        note += f" (dropped {skipped_sha_drift} git_sha-gated events)"

    task.record_decision("task_resume", note)
    return task


def _replay_lookup(task: Task, row: dict, lineno: int) -> Subtask | None:
    """Find ``row['subtask_id']`` in ``task.subtasks`` for replay.

    Returns ``None`` and logs a stderr note on miss — replay tolerates
    log corruption rather than crashing the whole reconstruction.
    """
    sid = row.get("subtask_id")
    for st in task.subtasks:
        if st.subtask_id == sid:
            return st
    print(
        f"[load_task] dropping event at line {lineno}: unknown "
        f"subtask_id={sid!r} for event={row.get('event')!r}",
        file=sys.stderr,
    )
    return None
