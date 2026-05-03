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

Phase 5 §5 schema additions (additive; backward-compatible). The
TOML loader accepts an optional ``[[checkpoints]]`` array of
tables, each declaring a human-checkpoint pause point identified
by ``checkpoint_id`` and positioned via ``before_subtask`` (which
must reference a declared ``[[subtasks]].subtask_id``). The
orchestrator's main loop pauses with a ``checkpoint_pause``
decision when about to spawn a subtask whose id matches a pending
checkpoint's ``before_subtask``; resume requires explicit
acknowledgement via ``./uas-orchestrate resume <task>
--ack-checkpoint <id>``, which records a ``checkpoint_ack``
decision carrying ``checkpoint_id`` in its payload. The
``checkpoints`` list is persisted in the ``task_create`` event so
``load_task`` reconstructs it on resume; the in-memory
``Task.acked_checkpoint_ids`` set is rebuilt from the
``checkpoint_ack`` decisions encountered during replay so a
multi-window cycle's ack state survives invocation boundaries.
Pre-§5 logs lack both the ``checkpoints`` payload and any
checkpoint decisions; replay tolerates the absence and leaves
both ``Task.checkpoints`` and ``Task.acked_checkpoint_ids``
empty.

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
    "policy_auto_resume",
    "worker_spawn",
    "worker_complete",
    "worker_fail",
    "task_create",
    "task_resume",
    "checkpoint_pause",
    "checkpoint_ack",
]

# Single source of truth for the Decision kind allow-list. Mirrors
# the ``DecisionKind`` Literal above; runtime checks compare against
# the frozenset. Phase 5 §5 added ``checkpoint_pause`` /
# ``checkpoint_ack``; ``ORCHESTRATOR_VERSION`` stays at ``"phase5"``
# because §3 already bumped from ``"phase3"`` to ``"phase5"`` and
# both §3 and §5 additions are additive on the same schema marker.
_VALID_DECISION_KINDS: frozenset[str] = frozenset({
    "policy_pause",
    "policy_wrap_up",
    "policy_halt",
    "policy_auto_resume",
    "worker_spawn",
    "worker_complete",
    "worker_fail",
    "task_create",
    "task_resume",
    "checkpoint_pause",
    "checkpoint_ack",
})

# The Phase 5 §5 checkpoint decision kinds. Both require a
# ``checkpoint_id`` payload field (validated by ``record_decision``);
# all other kinds reject the kwarg. Kept as a tuple so call sites
# can ``in`` against it without copying the frozenset.
_CHECKPOINT_DECISION_KINDS: tuple[str, ...] = (
    "checkpoint_pause", "checkpoint_ack",
)


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


def _validate_checkpoints(
    raw_checkpoints,
    path: str,
    seen_subtask_ids: set[str],
) -> list[dict]:
    """Validate the optional ``[[checkpoints]]`` array (Phase 5 §5).

    Each entry must be a table with a non-empty ``checkpoint_id``
    (unique across the task) and a non-empty ``before_subtask``
    that references a declared ``[[subtasks]].subtask_id``.
    Optional ``description`` and ``kind`` are strings (default
    ``""``).

    Returns a list of dicts mirroring the ``Checkpoint`` dataclass
    field set; ``Task.from_toml`` constructs ``Checkpoint(**rec)``
    from each record and persists the same dicts in the
    ``task_create`` event payload so ``load_task`` can reconstruct
    in-memory ``Checkpoint`` instances on resume without re-parsing
    the original TOML. Cross-references are checked here rather
    than inline in the subtasks loop because checkpoints reference
    subtasks (not the other way around) — the caller pre-passes
    subtasks to populate ``seen_subtask_ids`` before invoking this
    validator.
    """
    if not isinstance(raw_checkpoints, list):
        raise TaskError(
            f"task TOML at {path}: [[checkpoints]] must be an array of "
            f"tables; got {raw_checkpoints!r}"
        )

    records: list[dict] = []
    seen_ids: set[str] = set()
    for raw in raw_checkpoints:
        if not isinstance(raw, dict):
            raise TaskError(
                f"task TOML at {path}: each [[checkpoints]] entry must "
                f"be a table; got {raw!r}"
            )
        cid = raw.get("checkpoint_id")
        if not isinstance(cid, str) or not cid:
            raise TaskError(
                f"task TOML at {path}: each [[checkpoints]].checkpoint_id "
                f"must be a non-empty string; got {cid!r}"
            )
        if cid in seen_ids:
            raise TaskError(
                f"task TOML at {path}: duplicate checkpoint_id {cid!r}"
            )
        seen_ids.add(cid)

        before_subtask = raw.get("before_subtask")
        if not isinstance(before_subtask, str) or not before_subtask:
            raise TaskError(
                f"task TOML at {path}: checkpoint {cid!r} before_subtask "
                f"must be a non-empty string; got {before_subtask!r}"
            )
        if before_subtask not in seen_subtask_ids:
            raise TaskError(
                f"task TOML at {path}: checkpoint {cid!r} references "
                f"unknown subtask_id {before_subtask!r}"
            )

        description = raw.get("description", "")
        if not isinstance(description, str):
            raise TaskError(
                f"task TOML at {path}: checkpoint {cid!r} description "
                f"must be a string; got {description!r}"
            )

        kind = raw.get("kind", "")
        if not isinstance(kind, str):
            raise TaskError(
                f"task TOML at {path}: checkpoint {cid!r} kind must "
                f"be a string; got {kind!r}"
            )

        records.append({
            "checkpoint_id": cid,
            "before_subtask": before_subtask,
            "description": description,
            "kind": kind,
        })

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
class Checkpoint:
    """One human-checkpoint declared in a task's TOML (Phase 5 §5).

    A checkpoint expresses an explicit pause point: when the
    orchestrator's main loop is about to spawn a subtask whose
    ``subtask_id`` matches ``before_subtask`` and no
    ``checkpoint_ack`` decision has been recorded yet for this
    ``checkpoint_id``, the loop records a ``checkpoint_pause``
    decision and exits. Resume requires explicit acknowledgement
    via ``./uas-orchestrate resume <task> --ack-checkpoint <id>``,
    which records a ``checkpoint_ack`` decision; subsequent
    iterations skip the now-acknowledged checkpoint's pause
    position.

    ``checkpoint_id`` must be unique within a task.
    ``before_subtask`` must reference a declared
    ``[[subtasks]].subtask_id``; the ``Task.from_toml`` validator
    enforces both at load time. ``description`` and ``kind`` are
    free-form annotations carried into the persisted log and the
    resume_summary digest for operator-readable context; the
    orchestrator's control flow does not consume them.
    """

    checkpoint_id: str
    before_subtask: str
    description: str = ""
    kind: str = ""


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
    checkpoints: list[Checkpoint] = field(default_factory=list)
    acked_checkpoint_ids: set[str] = field(default_factory=set)
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
        - ``[[checkpoints]]``: optional array of tables (Phase 5 §5).
          Each entry needs non-empty ``checkpoint_id`` (unique
          within the task) and non-empty ``before_subtask``
          referencing a declared ``[[subtasks]].subtask_id``;
          optional ``description`` / ``kind`` strings (default
          ``""``).

        Computes ``workspace_path`` as ``<workspaces_dir>/<task_id>``;
        ``created_at`` is captured at call time.

        Persists exactly one ``task_create`` event (carrying
        ``goal`` / ``workspace_path`` / ``created_at`` / ``stages``
        / ``checkpoints`` plus the decision note) and one
        ``enqueue_subtask`` event per ``[[subtasks]]`` row (each
        carrying the optional ``stage_id``). Also appends a
        ``task_create`` ``Decision`` to the in-memory ``decisions``
        list so the replay-equivalent timeline is unified.

        Phase 5 §5 restructure: subtasks are validated in a
        structural pre-pass that builds ``seen_subtask_ids`` for
        the checkpoint validator, then the bootstrap event is
        written carrying both stages and checkpoints, then the
        subtasks are enqueued (each writing its own
        ``enqueue_subtask`` event). Order matters because the
        checkpoint cross-references subtasks; doing the
        ``task_create`` write before checkpoints are validated
        would either leak partially-validated state or require a
        compensating delete on failure.

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

        # Phase 5 §2: validate optional [[stages]] before the
        # subtasks pre-pass so subtask_id-to-stage_id refs can be
        # checked inline.
        stage_records = _validate_stages(config.get("stages", []), path)
        seen_stage_ids = {rec["stage_id"] for rec in stage_records}

        # Phase 5 §5: pre-pass [[subtasks]] for structural validation
        # plus id collection. The pre-pass turns the pre-§5 one-shot
        # enqueue loop into a build-records-then-enqueue two-pass so
        # ``[[checkpoints]]`` cross-references can be validated
        # against ``seen_subtask_ids`` before the bootstrap event is
        # written.
        raw_subtasks = config.get("subtasks", [])
        if not isinstance(raw_subtasks, list):
            raise TaskError(
                f"task TOML at {path}: [[subtasks]] must be an array of "
                f"tables; got {raw_subtasks!r}"
            )
        subtask_records: list[dict] = []
        seen_subtask_ids: set[str] = set()
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
            if sid in seen_subtask_ids:
                raise TaskError(
                    f"task TOML at {path}: duplicate subtask_id {sid!r}"
                )
            seen_subtask_ids.add(sid)
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
            subtask_records.append({
                "subtask_id": sid,
                "prompt": prompt,
                "stage_id": stage_id,
            })

        # Phase 5 §5: validate [[checkpoints]] now that
        # ``seen_subtask_ids`` is fully populated.
        checkpoint_records = _validate_checkpoints(
            config.get("checkpoints", []), path, seen_subtask_ids,
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
            stages=[Stage(**rec) for rec in stage_records],
            checkpoints=[Checkpoint(**rec) for rec in checkpoint_records],
            state_root=sr,
        )

        # Single bootstrap event: it both initialises the on-disk log
        # and acts as the task_create Decision row. The
        # ``decision_note`` field carries the human-readable note so
        # §7's replay can append to ``decisions`` without
        # reconstructing it from the goal field. ``stages`` and
        # ``checkpoints`` are serialised as lists of dicts mirroring
        # their dataclass field sets so load_task can reconstruct the
        # in-memory objects without having to re-parse the original
        # TOML.
        decision_note = f"goal: {goal}"
        task._append_event(
            "task_create",
            {
                "goal": goal,
                "workspace_path": workspace_path,
                "created_at": created_at,
                "decision_note": decision_note,
                "stages": stage_records,
                "checkpoints": checkpoint_records,
            },
        )
        task.decisions.append(
            Decision(timestamp=created_at, kind="task_create", note=decision_note),
        )

        # Enqueue subtasks from the pre-validated records (each writes
        # its own enqueue_subtask event via the operation below).
        for rec in subtask_records:
            task.enqueue_subtask(
                rec["subtask_id"], rec["prompt"], stage_id=rec["stage_id"],
            )

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

    def record_decision(
        self,
        kind: str,
        note: str,
        *,
        checkpoint_id: str | None = None,
    ) -> Decision:
        """Append a Decision to ``decisions``; persist one event.

        ``kind`` must be one of the canonical Decision kinds
        (policy_pause / wrap_up / halt / auto_resume,
        worker_spawn / complete / fail, task_create / task_resume,
        checkpoint_pause / checkpoint_ack). The PLAN §6 enumeration
        plus the §3 / §5 additive bumps form the closed allow-list
        — unknown kinds raise ``TaskError`` so the timeline cannot
        accumulate free-form strings that future tooling has to
        defend against.

        ``checkpoint_id`` (Phase 5 §5) is required when ``kind``
        is ``checkpoint_pause`` or ``checkpoint_ack`` and rejected
        for every other kind. When provided it is persisted in the
        event payload so ``load_task`` can reconstruct
        ``acked_checkpoint_ids`` on replay; for ``checkpoint_ack``
        it is also added to the in-memory
        ``acked_checkpoint_ids`` set so ``pending_checkpoint``
        skips the acknowledged entry on subsequent calls.
        """
        if kind not in _VALID_DECISION_KINDS:
            raise TaskError(
                f"unknown decision kind {kind!r}; allowed: "
                f"{sorted(_VALID_DECISION_KINDS)}"
            )
        if not isinstance(note, str):
            raise TaskError(f"note must be a string; got {note!r}")
        if kind in _CHECKPOINT_DECISION_KINDS:
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                raise TaskError(
                    f"checkpoint_id required for kind={kind!r}; got "
                    f"{checkpoint_id!r}"
                )
        elif checkpoint_id is not None:
            raise TaskError(
                f"checkpoint_id only valid for {list(_CHECKPOINT_DECISION_KINDS)}; "
                f"got kind={kind!r} with checkpoint_id={checkpoint_id!r}"
            )
        timestamp = _now_iso()
        decision = Decision(timestamp=timestamp, kind=kind, note=note)
        self.decisions.append(decision)
        if kind == "checkpoint_ack":
            self.acked_checkpoint_ids.add(checkpoint_id)
        payload = {
            "kind": kind, "note": note, "decision_timestamp": timestamp,
        }
        if checkpoint_id is not None:
            payload["checkpoint_id"] = checkpoint_id
        self._append_event("decision", payload)
        return decision

    def pending_checkpoint(
        self, subtask_id: str,
    ) -> "Checkpoint | None":
        """Return the first declared checkpoint pending at ``subtask_id``.

        Phase 5 §5 helper consumed by the orchestrator's main loop.
        A checkpoint is "pending" when both:

        1. Its ``before_subtask`` matches the ``subtask_id``.
        2. Its ``checkpoint_id`` is not in
           ``self.acked_checkpoint_ids``.

        Returns ``None`` if no checkpoint is positioned at this
        subtask, or if every matching checkpoint has already been
        acknowledged. If multiple checkpoints share the same
        ``before_subtask``, they fire in declaration order — the
        first un-acked match wins; subsequent loops re-evaluate
        after the first is acked.

        ``subtask_id`` validation is intentionally lenient: an
        empty string or non-string returns ``None`` rather than
        raising, so call sites can pass through whatever the
        next-pending lookup produced without pre-validating.
        """
        if not isinstance(subtask_id, str) or not subtask_id:
            return None
        for ckpt in self.checkpoints:
            if (
                ckpt.before_subtask == subtask_id
                and ckpt.checkpoint_id not in self.acked_checkpoint_ids
            ):
                return ckpt
        return None


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
                # Phase 5 §5: reconstruct checkpoints from the
                # persisted task_create row. Pre-§5 logs lack the
                # field; the forgiving reader leaves
                # task.checkpoints empty (and the empty
                # acked_checkpoint_ids set follows from there).
                raw_checkpoints = row.get("checkpoints", [])
                if isinstance(raw_checkpoints, list):
                    for raw_ckpt in raw_checkpoints:
                        if not isinstance(raw_ckpt, dict):
                            continue
                        cid = raw_ckpt.get("checkpoint_id")
                        if not isinstance(cid, str) or not cid:
                            continue
                        bs = raw_ckpt.get("before_subtask")
                        if not isinstance(bs, str) or not bs:
                            continue
                        desc = raw_ckpt.get("description", "")
                        if not isinstance(desc, str):
                            desc = ""
                        ckpt_kind = raw_ckpt.get("kind", "")
                        if not isinstance(ckpt_kind, str):
                            ckpt_kind = ""
                        task.checkpoints.append(Checkpoint(
                            checkpoint_id=cid,
                            before_subtask=bs,
                            description=desc,
                            kind=ckpt_kind,
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
                    # Phase 5 §5: rebuild ``acked_checkpoint_ids`` so
                    # ``pending_checkpoint`` skips already-acked
                    # checkpoints across resume boundaries. Forgiving:
                    # missing / non-string ``checkpoint_id`` payload
                    # is silently ignored (the decision itself still
                    # replays as a Decision row).
                    if kind == "checkpoint_ack":
                        ckpt_id = row.get("checkpoint_id")
                        if isinstance(ckpt_id, str) and ckpt_id:
                            task.acked_checkpoint_ids.add(ckpt_id)
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
