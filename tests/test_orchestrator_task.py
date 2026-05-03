"""Tests for ``orchestrator.task`` and ``orchestrator.workspace`` (Phase 3 §6).

Pure-Python tests: no container engine, no live Claude. Each test
exercises the JSONL persistence contract, the dataclass field
contracts, the TOML loader, and the resume-safe workspace primitive
against fixtures the test itself constructs. The ``state_root`` and
``workspaces_dir`` injection points (mirroring §3 / §4 / §5) keep
the suite from ever writing into the canonical
``<repo>/orchestrator/state`` or ``<repo>/integration/workspace``
directories.
"""

import json
import os
import re

import pytest

from orchestrator import task as task_mod
from orchestrator import workspace as workspace_mod
from orchestrator.task import (
    Checkpoint, Decision, Stage, Subtask, Task, TaskError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ISO_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$"
)


def _read_rows(state_root: str, task_id: str) -> list[dict]:
    path = os.path.join(state_root, task_id, "task_events.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _events_path(state_root: str, task_id: str) -> str:
    return os.path.join(state_root, task_id, "task_events.jsonl")


def _write_toml(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


@pytest.fixture
def state_root(tmp_path):
    return str(tmp_path / "state")


@pytest.fixture
def workspaces_dir(tmp_path):
    return str(tmp_path / "workspaces")


@pytest.fixture
def fresh_task(state_root, workspaces_dir):
    """A bare Task initialised directly (no TOML round-trip)."""
    return Task(
        task_id="t1",
        goal="hello world",
        workspace_path=os.path.join(workspaces_dir, "t1"),
        created_at="2026-05-01T00:00:00+00:00",
        state_root=state_root,
    )


# ---------------------------------------------------------------------------
# orchestrator/workspace.py
# ---------------------------------------------------------------------------


class TestSetupTaskWorkspace:
    """Resume-safe workspace primitive: idempotent, non-destructive."""

    def test_creates_directory_when_absent(self, workspaces_dir):
        path = workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        assert path == os.path.join(workspaces_dir, "t1")
        assert os.path.isdir(path)

    def test_idempotent_under_repeated_calls(self, workspaces_dir):
        path1 = workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        path2 = workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        assert path1 == path2
        assert os.path.isdir(path1)

    def test_does_not_destroy_existing_files(self, workspaces_dir):
        path = workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        marker = os.path.join(path, "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("preserved across resume")
        # Second call must not blow this away.
        workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        assert os.path.isfile(marker)
        with open(marker, "r", encoding="utf-8") as fh:
            assert fh.read() == "preserved across resume"

    def test_preserves_nested_subdirectories(self, workspaces_dir):
        path = workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        nested = os.path.join(path, "src", "module")
        os.makedirs(nested)
        with open(os.path.join(nested, "f.py"), "w", encoding="utf-8") as fh:
            fh.write("print('x')")
        workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        assert os.path.isfile(os.path.join(nested, "f.py"))

    def test_returns_absolute_path(self, workspaces_dir):
        path = workspace_mod.setup_task_workspace(
            "t1", workspaces_dir=workspaces_dir,
        )
        assert os.path.isabs(path)

    def test_isolates_tasks(self, workspaces_dir):
        a = workspace_mod.setup_task_workspace(
            "ta", workspaces_dir=workspaces_dir,
        )
        b = workspace_mod.setup_task_workspace(
            "tb", workspaces_dir=workspaces_dir,
        )
        assert a != b
        with open(os.path.join(a, "x"), "w", encoding="utf-8") as fh:
            fh.write("a")
        assert not os.path.exists(os.path.join(b, "x"))

    def test_empty_task_id_rejected(self, workspaces_dir):
        with pytest.raises(ValueError):
            workspace_mod.setup_task_workspace(
                "", workspaces_dir=workspaces_dir,
            )

    def test_non_string_task_id_rejected(self, workspaces_dir):
        with pytest.raises(ValueError):
            workspace_mod.setup_task_workspace(
                42, workspaces_dir=workspaces_dir,  # type: ignore[arg-type]
            )

    def test_default_workspaces_dir_under_integration(self):
        assert workspace_mod.DEFAULT_WORKSPACES_DIR.endswith(
            os.path.join("integration", "workspace")
        )


# ---------------------------------------------------------------------------
# Task.from_toml — TOML loader & bootstrap event
# ---------------------------------------------------------------------------


_MINIMAL_TOML = """\
task_id = "t1"
goal = "build a thing"
"""

_FULL_TOML = """\
task_id = "t1"
goal = "build a thing"

[[subtasks]]
subtask_id = "s1"
prompt = "first subtask"

[[subtasks]]
subtask_id = "s2"
prompt = "second subtask"
"""


class TestTaskFromToml:
    """TOML round-trip: validation, persistence, in-memory state."""

    def test_minimal_loads(self, tmp_path, state_root, workspaces_dir):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert isinstance(t, Task)
        assert t.task_id == "t1"
        assert t.goal == "build a thing"
        assert t.workspace_path == os.path.join(workspaces_dir, "t1")
        assert t.subtasks == []
        assert _ISO_PATTERN.match(t.created_at)

    def test_writes_single_task_create_event(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        rows = _read_rows(state_root, "t1")
        assert len(rows) == 1
        assert rows[0]["event"] == "task_create"
        assert rows[0]["task_id"] == "t1"
        assert rows[0]["goal"] == "build a thing"
        assert rows[0]["workspace_path"] == os.path.join(workspaces_dir, "t1")
        assert _ISO_PATTERN.match(rows[0]["created_at"])
        assert rows[0]["decision_note"] == "goal: build a thing"

    def test_task_create_event_carries_provenance(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        row = _read_rows(state_root, "t1")[0]
        # The full provenance fingerprint from §1's
        # capture_run_metadata is stamped at row root.
        assert "git_sha" in row
        assert row["harness_version"] == "phase1"
        assert row["orchestrator_version"] == "phase5"
        assert "timestamp_utc" in row
        assert "config_hash" in row

    def test_decision_appended_to_decisions_list(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert len(t.decisions) == 1
        d = t.decisions[0]
        assert isinstance(d, Decision)
        assert d.kind == "task_create"
        assert d.note == "goal: build a thing"
        assert d.timestamp == t.created_at

    def test_full_toml_enqueues_subtasks_in_order(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _FULL_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert [s.subtask_id for s in t.subtasks] == ["s1", "s2"]
        assert [s.prompt for s in t.subtasks] == [
            "first subtask",
            "second subtask",
        ]
        assert all(s.status == "pending" for s in t.subtasks)

    def test_full_toml_writes_one_event_per_subtask(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _FULL_TOML)
        Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        rows = _read_rows(state_root, "t1")
        # 1 task_create + 2 enqueue_subtask events.
        assert len(rows) == 3
        assert rows[0]["event"] == "task_create"
        assert rows[1]["event"] == "enqueue_subtask"
        assert rows[1]["subtask_id"] == "s1"
        assert rows[2]["event"] == "enqueue_subtask"
        assert rows[2]["subtask_id"] == "s2"

    def test_missing_file_raises(self, state_root, workspaces_dir, tmp_path):
        with pytest.raises(TaskError, match="not found"):
            Task.from_toml(
                str(tmp_path / "does-not-exist.toml"),
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_missing_task_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, 'goal = "x"\n')
        with pytest.raises(TaskError, match="task_id"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_empty_task_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, 'task_id = ""\ngoal = "x"\n')
        with pytest.raises(TaskError, match="task_id"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_missing_goal_raises(self, tmp_path, state_root, workspaces_dir):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, 'task_id = "t1"\n')
        with pytest.raises(TaskError, match="goal"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_empty_goal_raises(self, tmp_path, state_root, workspaces_dir):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, 'task_id = "t1"\ngoal = ""\n')
        with pytest.raises(TaskError, match="goal"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_malformed_toml_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, 'this is not = valid toml = syntax\n')
        with pytest.raises(TaskError, match="malformed"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_subtask_missing_subtask_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(
            toml_path,
            'task_id = "t1"\ngoal = "x"\n[[subtasks]]\nprompt = "p"\n',
        )
        with pytest.raises(TaskError, match="subtask_id"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_subtask_missing_prompt_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(
            toml_path,
            'task_id = "t1"\ngoal = "x"\n[[subtasks]]\nsubtask_id = "s1"\n',
        )
        with pytest.raises(TaskError, match="prompt"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_subtask_empty_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(
            toml_path,
            'task_id = "t1"\ngoal = "x"\n[[subtasks]]\n'
            'subtask_id = ""\nprompt = "p"\n',
        )
        with pytest.raises(TaskError, match="subtask_id"):
            Task.from_toml(
                toml_path,
                state_root=state_root,
                workspaces_dir=workspaces_dir,
            )

    def test_default_state_root_used_when_omitted(
        self, tmp_path, monkeypatch, workspaces_dir,
    ):
        # Prevent the test from writing into the canonical state dir
        # by redirecting DEFAULT_STATE_ROOT for this test only.
        sandbox = str(tmp_path / "default-state")
        monkeypatch.setattr(task_mod, "DEFAULT_STATE_ROOT", sandbox)
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        t = Task.from_toml(
            toml_path, workspaces_dir=workspaces_dir,
        )
        assert t.state_root == sandbox
        assert os.path.isfile(_events_path(sandbox, "t1"))


# ---------------------------------------------------------------------------
# enqueue_subtask
# ---------------------------------------------------------------------------


class TestEnqueueSubtask:

    def test_appends_pending_subtask(self, fresh_task):
        st = fresh_task.enqueue_subtask("s1", "do thing 1")
        assert isinstance(st, Subtask)
        assert st.subtask_id == "s1"
        assert st.prompt == "do thing 1"
        assert st.status == "pending"
        assert st.started_at is None
        assert st.finished_at is None
        assert fresh_task.subtasks == [st]

    def test_writes_one_enqueue_event(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "do thing 1")
        rows = _read_rows(state_root, "t1")
        assert len(rows) == 1
        assert rows[0]["event"] == "enqueue_subtask"
        assert rows[0]["subtask_id"] == "s1"
        assert rows[0]["prompt"] == "do thing 1"
        assert rows[0]["task_id"] == "t1"
        assert rows[0]["orchestrator_version"] == "phase5"

    def test_preserves_order_across_calls(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p1")
        fresh_task.enqueue_subtask("s2", "p2")
        fresh_task.enqueue_subtask("s3", "p3")
        assert [s.subtask_id for s in fresh_task.subtasks] == ["s1", "s2", "s3"]

    def test_duplicate_id_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "first")
        with pytest.raises(TaskError, match="duplicate"):
            fresh_task.enqueue_subtask("s1", "second")

    def test_empty_subtask_id_raises(self, fresh_task):
        with pytest.raises(TaskError, match="subtask_id"):
            fresh_task.enqueue_subtask("", "p")

    def test_empty_prompt_raises(self, fresh_task):
        with pytest.raises(TaskError, match="prompt"):
            fresh_task.enqueue_subtask("s1", "")

    def test_non_string_id_raises(self, fresh_task):
        with pytest.raises(TaskError, match="subtask_id"):
            fresh_task.enqueue_subtask(42, "p")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# start_subtask
# ---------------------------------------------------------------------------


class TestStartSubtask:

    def test_pending_to_in_flight_transition(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        st = fresh_task.subtasks[0]
        assert st.status == "in_flight"
        assert _ISO_PATTERN.match(st.started_at or "")
        assert st.finished_at is None

    def test_writes_start_event(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        rows = _read_rows(state_root, "t1")
        # 1 enqueue + 1 start.
        assert len(rows) == 2
        start_row = rows[-1]
        assert start_row["event"] == "start_subtask"
        assert start_row["subtask_id"] == "s1"
        assert _ISO_PATTERN.match(start_row["started_at"])

    def test_double_start_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        with pytest.raises(TaskError, match="status='pending'"):
            fresh_task.start_subtask("s1")

    def test_start_after_done_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask("s1")
        with pytest.raises(TaskError, match="status='pending'"):
            fresh_task.start_subtask("s1")

    def test_unknown_subtask_raises(self, fresh_task):
        with pytest.raises(TaskError, match="not found"):
            fresh_task.start_subtask("nope")


# ---------------------------------------------------------------------------
# complete_subtask
# ---------------------------------------------------------------------------


class TestCompleteSubtask:

    def test_in_flight_to_done_transition(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask(
            "s1", result_summary="all good", cost_usd=0.42,
        )
        st = fresh_task.subtasks[0]
        assert st.status == "done"
        assert _ISO_PATTERN.match(st.finished_at or "")
        assert st.result_summary == "all good"
        assert st.cost_usd == 0.42

    def test_writes_complete_event_with_payload(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask(
            "s1", result_summary="ok", cost_usd=1.5,
        )
        rows = _read_rows(state_root, "t1")
        # 1 enqueue + 1 start + 1 complete.
        assert len(rows) == 3
        evt = rows[-1]
        assert evt["event"] == "complete_subtask"
        assert evt["subtask_id"] == "s1"
        assert evt["result_summary"] == "ok"
        assert evt["cost_usd"] == 1.5
        assert _ISO_PATTERN.match(evt["finished_at"])

    def test_optional_fields_default_none(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask("s1")
        st = fresh_task.subtasks[0]
        assert st.result_summary is None
        assert st.cost_usd is None
        evt = _read_rows(state_root, "t1")[-1]
        assert evt["result_summary"] is None
        assert evt["cost_usd"] is None

    def test_complete_when_pending_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        with pytest.raises(TaskError, match="status='in_flight'"):
            fresh_task.complete_subtask("s1")

    def test_double_complete_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask("s1")
        with pytest.raises(TaskError, match="status='in_flight'"):
            fresh_task.complete_subtask("s1")

    def test_invalid_result_summary_type_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        with pytest.raises(TaskError, match="result_summary"):
            fresh_task.complete_subtask("s1", result_summary=42)  # type: ignore[arg-type]

    def test_invalid_cost_type_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        with pytest.raises(TaskError, match="cost_usd"):
            fresh_task.complete_subtask("s1", cost_usd="lots")  # type: ignore[arg-type]

    def test_bool_cost_rejected(self, fresh_task):
        # ``bool`` subclasses ``int`` in Python; reject it explicitly
        # so ``cost_usd = True`` doesn't silently coerce to 1.0.
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        with pytest.raises(TaskError, match="cost_usd"):
            fresh_task.complete_subtask("s1", cost_usd=True)  # type: ignore[arg-type]

    def test_int_cost_coerced_to_float(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask("s1", cost_usd=2)
        st = fresh_task.subtasks[0]
        assert isinstance(st.cost_usd, float)
        assert st.cost_usd == 2.0


# ---------------------------------------------------------------------------
# fail_subtask
# ---------------------------------------------------------------------------


class TestFailSubtask:

    def test_in_flight_to_failed_transition(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.fail_subtask("s1", result_summary="boom")
        st = fresh_task.subtasks[0]
        assert st.status == "failed"
        assert _ISO_PATTERN.match(st.finished_at or "")
        assert st.result_summary == "boom"

    def test_writes_fail_event(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.fail_subtask("s1", result_summary="boom")
        evt = _read_rows(state_root, "t1")[-1]
        assert evt["event"] == "fail_subtask"
        assert evt["subtask_id"] == "s1"
        assert evt["result_summary"] == "boom"
        assert _ISO_PATTERN.match(evt["finished_at"])

    def test_fail_when_pending_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        with pytest.raises(TaskError, match="status='in_flight'"):
            fresh_task.fail_subtask("s1")

    def test_fail_when_done_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask("s1")
        with pytest.raises(TaskError, match="status='in_flight'"):
            fresh_task.fail_subtask("s1")

    def test_invalid_result_summary_type_raises(self, fresh_task):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        with pytest.raises(TaskError, match="result_summary"):
            fresh_task.fail_subtask("s1", result_summary=99)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# record_decision
# ---------------------------------------------------------------------------


class TestRecordDecision:

    def test_appends_decision_to_in_memory_list(self, fresh_task):
        d = fresh_task.record_decision("policy_pause", "5h cap reached")
        assert isinstance(d, Decision)
        assert d.kind == "policy_pause"
        assert d.note == "5h cap reached"
        assert _ISO_PATTERN.match(d.timestamp)
        assert fresh_task.decisions[-1] is d

    def test_writes_decision_event(self, fresh_task, state_root):
        fresh_task.record_decision("policy_pause", "5h cap reached")
        evt = _read_rows(state_root, "t1")[-1]
        assert evt["event"] == "decision"
        assert evt["kind"] == "policy_pause"
        assert evt["note"] == "5h cap reached"
        assert _ISO_PATTERN.match(evt["decision_timestamp"])

    def test_all_kinds_accepted(self, fresh_task):
        for kind in (
            "policy_pause",
            "policy_wrap_up",
            "policy_halt",
            "worker_spawn",
            "worker_complete",
            "worker_fail",
            "task_create",
            "task_resume",
        ):
            fresh_task.record_decision(kind, f"note for {kind}")
        kinds = [d.kind for d in fresh_task.decisions]
        # 8 record_decision calls plus no implicit task_create
        # (fresh_task is a bare Task, not via from_toml).
        assert kinds == [
            "policy_pause",
            "policy_wrap_up",
            "policy_halt",
            "worker_spawn",
            "worker_complete",
            "worker_fail",
            "task_create",
            "task_resume",
        ]

    def test_unknown_kind_raises(self, fresh_task):
        with pytest.raises(TaskError, match="unknown decision kind"):
            fresh_task.record_decision("not_a_real_kind", "x")

    def test_non_string_note_raises(self, fresh_task):
        with pytest.raises(TaskError, match="note"):
            fresh_task.record_decision("policy_pause", 42)  # type: ignore[arg-type]

    def test_multiple_decisions_ordered(self, fresh_task):
        fresh_task.record_decision("policy_pause", "first")
        fresh_task.record_decision("policy_halt", "second")
        notes = [d.note for d in fresh_task.decisions]
        assert notes == ["first", "second"]


# ---------------------------------------------------------------------------
# Persistence: provenance, append-only, task isolation
# ---------------------------------------------------------------------------


class TestPersistence:

    def test_events_path_under_state_root(
        self, fresh_task, state_root,
    ):
        fresh_task.enqueue_subtask("s1", "p")
        expected = os.path.join(state_root, "t1", "task_events.jsonl")
        assert os.path.isfile(expected)

    def test_every_row_carries_provenance_and_event_type(
        self, fresh_task, state_root,
    ):
        fresh_task.enqueue_subtask("s1", "p")
        fresh_task.start_subtask("s1")
        fresh_task.complete_subtask("s1", cost_usd=0.1)
        fresh_task.record_decision("policy_pause", "n")
        rows = _read_rows(state_root, "t1")
        assert len(rows) == 4
        for r in rows:
            assert "git_sha" in r
            assert r["harness_version"] == "phase1"
            assert r["orchestrator_version"] == "phase5"
            assert r["task_id"] == "t1"
            assert r["event"] in {
                "enqueue_subtask",
                "start_subtask",
                "complete_subtask",
                "fail_subtask",
                "decision",
                "task_create",
            }

    def test_log_is_append_only(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p")
        first_size = os.path.getsize(_events_path(state_root, "t1"))
        fresh_task.enqueue_subtask("s2", "p")
        second_size = os.path.getsize(_events_path(state_root, "t1"))
        assert second_size > first_size

    def test_task_isolation(self, state_root, workspaces_dir, tmp_path):
        ta = Task(
            task_id="ta",
            goal="g",
            workspace_path=os.path.join(workspaces_dir, "ta"),
            created_at="2026-05-01T00:00:00+00:00",
            state_root=state_root,
        )
        tb = Task(
            task_id="tb",
            goal="g",
            workspace_path=os.path.join(workspaces_dir, "tb"),
            created_at="2026-05-01T00:00:00+00:00",
            state_root=state_root,
        )
        ta.enqueue_subtask("s1", "p")
        tb.enqueue_subtask("s1", "p")
        tb.enqueue_subtask("s2", "p")
        assert len(_read_rows(state_root, "ta")) == 1
        assert len(_read_rows(state_root, "tb")) == 2

    def test_full_lifecycle_persists_seven_events(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _FULL_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        t.start_subtask("s1")
        t.complete_subtask("s1", cost_usd=0.5)
        t.start_subtask("s2")
        t.fail_subtask("s2", result_summary="oops")
        t.record_decision("policy_pause", "5h cap")

        rows = _read_rows(state_root, "t1")
        # task_create + 2x enqueue + start + complete + start + fail
        # + decision = 8 events.
        assert [r["event"] for r in rows] == [
            "task_create",
            "enqueue_subtask",
            "enqueue_subtask",
            "start_subtask",
            "complete_subtask",
            "start_subtask",
            "fail_subtask",
            "decision",
        ]
        assert t.subtasks[0].status == "done"
        assert t.subtasks[1].status == "failed"
        # decisions list contains task_create + the explicit pause.
        assert [d.kind for d in t.decisions] == [
            "task_create",
            "policy_pause",
        ]


# ---------------------------------------------------------------------------
# Phase 5 §2 — stages and per-subtask stage_id
# ---------------------------------------------------------------------------


_STAGES_TOML = """\
task_id = "t1"
goal = "staged"

[[stages]]
stage_id = "stage_a"
name = "First stage"
depends_on = []
expected_duration_seconds = 600
expected_spend_usd = 5.0

[[stages]]
stage_id = "stage_b"
name = "Second stage"
depends_on = ["stage_a"]
expected_duration_seconds = 900
expected_spend_usd = 7.5

[[subtasks]]
subtask_id = "s1"
stage_id = "stage_a"
prompt = "first"

[[subtasks]]
subtask_id = "s2"
stage_id = "stage_b"
prompt = "second"
"""


class TestStagesFromToml:
    """Phase 5 §2 — [[stages]] schema parsing and validation."""

    def test_stages_default_empty_when_absent(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert t.stages == []

    def test_full_stages_loaded(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _STAGES_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert len(t.stages) == 2
        a, b = t.stages
        assert a.stage_id == "stage_a"
        assert a.name == "First stage"
        assert a.depends_on == []
        assert a.expected_duration_seconds == 600.0
        assert a.expected_spend_usd == 5.0
        assert b.stage_id == "stage_b"
        assert b.depends_on == ["stage_a"]

    def test_stages_persisted_in_task_create_event(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _STAGES_TOML)
        Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        rows = _read_rows(state_root, "t1")
        bootstrap = rows[0]
        assert bootstrap["event"] == "task_create"
        stages_payload = bootstrap["stages"]
        assert isinstance(stages_payload, list)
        assert len(stages_payload) == 2
        assert stages_payload[0]["stage_id"] == "stage_a"
        assert stages_payload[1]["depends_on"] == ["stage_a"]
        assert stages_payload[1]["expected_spend_usd"] == 7.5

    def test_subtask_stage_id_loaded(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _STAGES_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert t.subtasks[0].stage_id == "stage_a"
        assert t.subtasks[1].stage_id == "stage_b"

    def test_subtask_stage_id_persisted_in_enqueue_event(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _STAGES_TOML)
        Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        rows = _read_rows(state_root, "t1")
        # rows[0] = task_create, rows[1] = enqueue s1, rows[2] = enqueue s2
        assert rows[1]["event"] == "enqueue_subtask"
        assert rows[1]["stage_id"] == "stage_a"
        assert rows[2]["stage_id"] == "stage_b"

    def test_subtask_without_stage_id_defaults_to_none(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _FULL_TOML)  # no stage_id on subtasks
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        for st in t.subtasks:
            assert st.stage_id is None
        # Persisted as null in the enqueue event.
        rows = _read_rows(state_root, "t1")
        for r in rows:
            if r["event"] == "enqueue_subtask":
                assert r["stage_id"] is None

    def test_duplicate_stage_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "dup"
[[stages]]
stage_id = "dup"
""")
        with pytest.raises(TaskError, match="duplicate stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stage_missing_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
name = "no id"
""")
        with pytest.raises(TaskError, match="stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stage_empty_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = ""
""")
        with pytest.raises(TaskError, match="stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stage_invalid_name_type_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
name = 42
""")
        with pytest.raises(TaskError, match="name"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stage_depends_on_must_be_list(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
depends_on = "not a list"
""")
        with pytest.raises(TaskError, match="depends_on"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stage_depends_on_unknown_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
depends_on = ["does-not-exist"]
""")
        with pytest.raises(TaskError, match="unknown stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stage_depends_on_self_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
depends_on = ["s1"]
""")
        with pytest.raises(TaskError, match="depends on itself"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_two_stage_cycle_detected(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "a"
depends_on = ["b"]
[[stages]]
stage_id = "b"
depends_on = ["a"]
""")
        with pytest.raises(TaskError, match="cycle"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_three_stage_cycle_detected(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "a"
depends_on = ["c"]
[[stages]]
stage_id = "b"
depends_on = ["a"]
[[stages]]
stage_id = "c"
depends_on = ["b"]
""")
        with pytest.raises(TaskError, match="cycle"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_diamond_dependency_accepted(
        self, tmp_path, state_root, workspaces_dir,
    ):
        # a → b, a → c, b → d, c → d (DAG, no cycle).
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "a"
depends_on = []
[[stages]]
stage_id = "b"
depends_on = ["a"]
[[stages]]
stage_id = "c"
depends_on = ["a"]
[[stages]]
stage_id = "d"
depends_on = ["b", "c"]
""")
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert [s.stage_id for s in t.stages] == ["a", "b", "c", "d"]

    def test_invalid_expected_duration_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
expected_duration_seconds = "not a number"
""")
        with pytest.raises(TaskError, match="expected_duration_seconds"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_negative_expected_duration_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
expected_duration_seconds = -10
""")
        with pytest.raises(TaskError, match="expected_duration_seconds"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_invalid_expected_spend_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "s1"
expected_spend_usd = -1.0
""")
        with pytest.raises(TaskError, match="expected_spend_usd"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_stages_must_be_list(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
stages = "not an array"
""")
        with pytest.raises(TaskError, match="\\[\\[stages\\]\\]"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_subtask_unknown_stage_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "real"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
stage_id = "fake"
""")
        with pytest.raises(TaskError, match="unknown stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_subtask_invalid_stage_id_type_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "real"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
stage_id = 42
""")
        with pytest.raises(TaskError, match="stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_subtask_empty_stage_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "real"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
stage_id = ""
""")
        with pytest.raises(TaskError, match="stage_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_real_task_toml_loads(self, state_root, workspaces_dir):
        """The Phase 5 §1 real-task TOML must load and validate."""
        repo_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        case_path = os.path.join(
            repo_root, "orchestrator", "cases", "agent-survey-2026.toml",
        )
        t = Task.from_toml(
            case_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert t.task_id == "agent-survey-2026"
        assert len(t.stages) == 5
        assert len(t.subtasks) == 22
        # Stage 5 depends on the four prior stages.
        synthesis = next(s for s in t.stages if s.stage_id == "stage5-synthesis")
        assert set(synthesis.depends_on) == {
            "stage1-architectures",
            "stage2-reliability",
            "stage3-evaluation",
            "stage4-open-problems",
        }
        # Every subtask references a declared stage.
        declared = {s.stage_id for s in t.stages}
        for st in t.subtasks:
            assert st.stage_id in declared


class TestEnqueueSubtaskStageId:
    """Phase 5 §2 — direct enqueue_subtask call exercises stage_id kwarg."""

    def test_stage_id_kwarg_propagates_to_subtask(self, fresh_task):
        st = fresh_task.enqueue_subtask("s1", "p", stage_id="stage_a")
        assert st.stage_id == "stage_a"

    def test_stage_id_persisted_in_event(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p", stage_id="stage_a")
        row = _read_rows(state_root, "t1")[-1]
        assert row["event"] == "enqueue_subtask"
        assert row["stage_id"] == "stage_a"

    def test_omitted_stage_id_persists_as_null(self, fresh_task, state_root):
        fresh_task.enqueue_subtask("s1", "p")
        row = _read_rows(state_root, "t1")[-1]
        assert row["stage_id"] is None

    def test_invalid_stage_id_type_raises(self, fresh_task):
        with pytest.raises(TaskError, match="stage_id"):
            fresh_task.enqueue_subtask(
                "s1", "p", stage_id=42,  # type: ignore[arg-type]
            )

    def test_empty_stage_id_raises(self, fresh_task):
        with pytest.raises(TaskError, match="stage_id"):
            fresh_task.enqueue_subtask("s1", "p", stage_id="")


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Sanity checks on default paths and the kinds enum."""

    def test_default_state_root_under_orchestrator(self):
        assert task_mod.DEFAULT_STATE_ROOT.endswith(
            os.path.join("orchestrator", "state")
        )

    def test_default_workspaces_dir_under_integration(self):
        assert task_mod.DEFAULT_WORKSPACES_DIR.endswith(
            os.path.join("integration", "workspace")
        )

    def test_decision_kinds_match_plan(self):
        # Closed allow-list — adding new kinds requires bumping
        # ORCHESTRATOR_VERSION per provenance.py's contract. Phase 5
        # §3 added ``policy_auto_resume`` and bumped the version
        # constant from "phase3" → "phase5" in lockstep. Phase 5 §5
        # added ``checkpoint_pause`` / ``checkpoint_ack`` on top of
        # the same ``"phase5"`` marker because both §3 and §5
        # additions are additive on the same schema variant.
        assert task_mod._VALID_DECISION_KINDS == frozenset({
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

    def test_subtask_default_status_is_pending(self):
        st = Subtask(subtask_id="s1", prompt="p")
        assert st.status == "pending"
        assert st.started_at is None
        assert st.finished_at is None
        assert st.result_summary is None
        assert st.cost_usd is None
        # Phase 5 §2: stage_id defaults to None for un-staged subtasks.
        assert st.stage_id is None

    def test_stage_default_fields(self):
        s = Stage(stage_id="stage1")
        assert s.stage_id == "stage1"
        assert s.name == ""
        assert s.depends_on == []
        assert s.expected_duration_seconds is None
        assert s.expected_spend_usd is None

    def test_stage_full_construction(self):
        s = Stage(
            stage_id="s2",
            name="Reliability mechanisms",
            depends_on=["s1"],
            expected_duration_seconds=2700.0,
            expected_spend_usd=12.0,
        )
        assert s.depends_on == ["s1"]
        assert s.expected_duration_seconds == 2700.0
        assert s.expected_spend_usd == 12.0

    def test_task_default_stages_empty(self):
        t = Task(
            task_id="t",
            goal="g",
            workspace_path="/tmp",
            created_at="ts",
        )
        assert t.stages == []

    def test_checkpoint_default_fields(self):
        c = Checkpoint(checkpoint_id="ckpt-1", before_subtask="s1")
        assert c.checkpoint_id == "ckpt-1"
        assert c.before_subtask == "s1"
        assert c.description == ""
        assert c.kind == ""

    def test_checkpoint_full_construction(self):
        c = Checkpoint(
            checkpoint_id="ckpt-1",
            before_subtask="s2",
            description="Review the change before proceeding.",
            kind="review-commit",
        )
        assert c.description == "Review the change before proceeding."
        assert c.kind == "review-commit"

    def test_task_default_checkpoints_empty(self):
        t = Task(
            task_id="t",
            goal="g",
            workspace_path="/tmp",
            created_at="ts",
        )
        assert t.checkpoints == []
        assert t.acked_checkpoint_ids == set()


# ---------------------------------------------------------------------------
# Phase 5 §5 — checkpoints schema and pending-checkpoint helper
# ---------------------------------------------------------------------------


_CHECKPOINTS_TOML = """\
task_id = "t1"
goal = "checkpointed"

[[subtasks]]
subtask_id = "s1"
prompt = "first"

[[subtasks]]
subtask_id = "s2"
prompt = "second"

[[subtasks]]
subtask_id = "s3"
prompt = "third"

[[checkpoints]]
checkpoint_id = "review-after-s1"
before_subtask = "s2"
description = "Review s1 output before s2."
kind = "review-commit"
"""


class TestCheckpointsFromToml:
    """Phase 5 §5 — [[checkpoints]] schema parsing and validation."""

    def test_checkpoints_default_empty_when_absent(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _MINIMAL_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert t.checkpoints == []
        assert t.acked_checkpoint_ids == set()

    def test_full_checkpoint_loaded(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _CHECKPOINTS_TOML)
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert len(t.checkpoints) == 1
        ckpt = t.checkpoints[0]
        assert ckpt.checkpoint_id == "review-after-s1"
        assert ckpt.before_subtask == "s2"
        assert ckpt.description == "Review s1 output before s2."
        assert ckpt.kind == "review-commit"

    def test_checkpoints_persisted_in_task_create_event(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, _CHECKPOINTS_TOML)
        Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        rows = _read_rows(state_root, "t1")
        bootstrap = rows[0]
        assert bootstrap["event"] == "task_create"
        ckpts_payload = bootstrap["checkpoints"]
        assert isinstance(ckpts_payload, list)
        assert len(ckpts_payload) == 1
        assert ckpts_payload[0]["checkpoint_id"] == "review-after-s1"
        assert ckpts_payload[0]["before_subtask"] == "s2"
        assert ckpts_payload[0]["description"] == "Review s1 output before s2."
        assert ckpts_payload[0]["kind"] == "review-commit"

    def test_optional_fields_default_to_empty_string(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = "c1"
before_subtask = "s1"
""")
        t = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        ckpt = t.checkpoints[0]
        assert ckpt.description == ""
        assert ckpt.kind == ""

    def test_duplicate_checkpoint_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[subtasks]]
subtask_id = "s2"
prompt = "p"
[[checkpoints]]
checkpoint_id = "dup"
before_subtask = "s1"
[[checkpoints]]
checkpoint_id = "dup"
before_subtask = "s2"
""")
        with pytest.raises(TaskError, match="duplicate checkpoint_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_missing_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
before_subtask = "s1"
""")
        with pytest.raises(TaskError, match="checkpoint_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_empty_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = ""
before_subtask = "s1"
""")
        with pytest.raises(TaskError, match="checkpoint_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_non_string_id_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = 42
before_subtask = "s1"
""")
        with pytest.raises(TaskError, match="checkpoint_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_missing_before_subtask_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = "c1"
""")
        with pytest.raises(TaskError, match="before_subtask"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_unknown_before_subtask_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = "c1"
before_subtask = "does-not-exist"
""")
        with pytest.raises(TaskError, match="unknown subtask_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_empty_before_subtask_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = "c1"
before_subtask = ""
""")
        with pytest.raises(TaskError, match="before_subtask"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoints_must_be_list(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
checkpoints = "not an array"
""")
        with pytest.raises(TaskError, match=r"\[\[checkpoints\]\]"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_invalid_description_type_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = "c1"
before_subtask = "s1"
description = 42
""")
        with pytest.raises(TaskError, match="description"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_checkpoint_invalid_kind_type_raises(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "s1"
prompt = "p"
[[checkpoints]]
checkpoint_id = "c1"
before_subtask = "s1"
kind = 42
""")
        with pytest.raises(TaskError, match="kind"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )

    def test_synthetic_checkpoint_case_loads(self, state_root, workspaces_dir):
        """The Phase 5 §5 fixture must load and validate."""
        repo_root = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
        case_path = os.path.join(
            repo_root, "orchestrator", "cases",
            "synthetic-checkpoint.toml",
        )
        t = Task.from_toml(
            case_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        assert t.task_id == "synthetic-checkpoint"
        assert len(t.subtasks) == 3
        assert len(t.checkpoints) == 1
        ckpt = t.checkpoints[0]
        assert ckpt.checkpoint_id == "review-after-step-1"
        assert ckpt.before_subtask == "step-2"
        assert ckpt.kind == "review-commit"

    def test_duplicate_subtask_id_raises_pre_pass(
        self, tmp_path, state_root, workspaces_dir,
    ):
        # The §5 restructure made duplicate-subtask_id detection a
        # TOML-loader concern (was previously caught only by
        # enqueue_subtask). Verify the pre-pass surfaces it.
        toml_path = str(tmp_path / "t1.toml")
        _write_toml(toml_path, """\
task_id = "t1"
goal = "g"
[[subtasks]]
subtask_id = "dup"
prompt = "first"
[[subtasks]]
subtask_id = "dup"
prompt = "second"
""")
        with pytest.raises(TaskError, match="duplicate subtask_id"):
            Task.from_toml(
                toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
            )


class TestPendingCheckpoint:
    """Phase 5 §5 — Task.pending_checkpoint helper."""

    def _seed(self, fresh_task):
        fresh_task.checkpoints = [
            Checkpoint(
                checkpoint_id="c1", before_subtask="s2",
                description="first checkpoint",
            ),
            Checkpoint(
                checkpoint_id="c2", before_subtask="s5",
            ),
        ]
        return fresh_task

    def test_no_checkpoints_returns_none(self, fresh_task):
        assert fresh_task.pending_checkpoint("s1") is None

    def test_matching_unack_returns_checkpoint(self, fresh_task):
        self._seed(fresh_task)
        ckpt = fresh_task.pending_checkpoint("s2")
        assert ckpt is not None
        assert ckpt.checkpoint_id == "c1"

    def test_matching_after_ack_returns_none(self, fresh_task):
        self._seed(fresh_task)
        fresh_task.acked_checkpoint_ids.add("c1")
        assert fresh_task.pending_checkpoint("s2") is None

    def test_non_matching_subtask_returns_none(self, fresh_task):
        self._seed(fresh_task)
        assert fresh_task.pending_checkpoint("s1") is None
        assert fresh_task.pending_checkpoint("s9") is None

    def test_empty_subtask_id_returns_none(self, fresh_task):
        self._seed(fresh_task)
        assert fresh_task.pending_checkpoint("") is None

    def test_non_string_subtask_id_returns_none(self, fresh_task):
        self._seed(fresh_task)
        assert fresh_task.pending_checkpoint(None) is None  # type: ignore[arg-type]

    def test_multiple_at_same_position_first_wins(self, fresh_task):
        # Two checkpoints share a position; the first declared
        # fires first. Acking it surfaces the second.
        fresh_task.checkpoints = [
            Checkpoint(checkpoint_id="c1", before_subtask="s2"),
            Checkpoint(checkpoint_id="c2", before_subtask="s2"),
        ]
        first = fresh_task.pending_checkpoint("s2")
        assert first is not None
        assert first.checkpoint_id == "c1"
        fresh_task.acked_checkpoint_ids.add("c1")
        second = fresh_task.pending_checkpoint("s2")
        assert second is not None
        assert second.checkpoint_id == "c2"
        fresh_task.acked_checkpoint_ids.add("c2")
        assert fresh_task.pending_checkpoint("s2") is None


class TestRecordDecisionCheckpoint:
    """Phase 5 §5 — record_decision checkpoint_id kwarg behaviour."""

    def test_checkpoint_pause_requires_id(self, fresh_task):
        with pytest.raises(TaskError, match="checkpoint_id required"):
            fresh_task.record_decision("checkpoint_pause", "n")

    def test_checkpoint_ack_requires_id(self, fresh_task):
        with pytest.raises(TaskError, match="checkpoint_id required"):
            fresh_task.record_decision("checkpoint_ack", "n")

    def test_empty_checkpoint_id_rejected(self, fresh_task):
        with pytest.raises(TaskError, match="checkpoint_id required"):
            fresh_task.record_decision(
                "checkpoint_pause", "n", checkpoint_id="",
            )

    def test_non_string_checkpoint_id_rejected(self, fresh_task):
        with pytest.raises(TaskError, match="checkpoint_id required"):
            fresh_task.record_decision(
                "checkpoint_pause", "n",
                checkpoint_id=42,  # type: ignore[arg-type]
            )

    def test_checkpoint_id_rejected_for_other_kinds(self, fresh_task):
        with pytest.raises(TaskError, match="checkpoint_id only valid"):
            fresh_task.record_decision(
                "policy_pause", "n", checkpoint_id="c1",
            )

    def test_checkpoint_ack_updates_in_memory_set(self, fresh_task):
        fresh_task.record_decision(
            "checkpoint_ack", "ack", checkpoint_id="c1",
        )
        assert "c1" in fresh_task.acked_checkpoint_ids

    def test_checkpoint_pause_does_not_update_set(self, fresh_task):
        fresh_task.record_decision(
            "checkpoint_pause", "pause", checkpoint_id="c1",
        )
        assert "c1" not in fresh_task.acked_checkpoint_ids

    def test_checkpoint_id_persisted_in_event(self, fresh_task, state_root):
        fresh_task.record_decision(
            "checkpoint_pause", "pause", checkpoint_id="c1",
        )
        evt = _read_rows(state_root, "t1")[-1]
        assert evt["event"] == "decision"
        assert evt["kind"] == "checkpoint_pause"
        assert evt["checkpoint_id"] == "c1"

    def test_non_checkpoint_event_omits_id_field(
        self, fresh_task, state_root,
    ):
        fresh_task.record_decision("policy_pause", "p")
        evt = _read_rows(state_root, "t1")[-1]
        assert "checkpoint_id" not in evt

    def test_decision_appended_to_in_memory_list(self, fresh_task):
        d = fresh_task.record_decision(
            "checkpoint_pause", "pause note", checkpoint_id="c1",
        )
        assert isinstance(d, Decision)
        assert d.kind == "checkpoint_pause"
        assert d.note == "pause note"
        assert fresh_task.decisions[-1] is d
