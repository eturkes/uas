"""Tests for ``orchestrator.task.load_task`` and ``orchestrator.cli``
resume/start/status wiring (Phase 3 §7).

Pure-Python tests: no container engine, no live Claude. Replay is
exercised against fixtures the test itself constructs (writing
synthetic ``task_events.jsonl`` rows directly OR by spinning up a
real ``Task.from_toml`` and then calling ``load_task`` on its
log). The ``state_root`` / ``workspaces_dir`` / ``cases_dir``
injection points keep the suite from ever writing into the
canonical repo paths.
"""

import argparse
import io
import json
import os
import re

import pytest

from orchestrator import cli as cli_mod
from orchestrator import task as task_mod
from orchestrator.task import (
    Decision,
    Stage,
    Subtask,
    Task,
    TaskError,
    load_task,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ISO_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$"
)


def _events_path(state_root: str, task_id: str) -> str:
    return os.path.join(state_root, task_id, "task_events.jsonl")


def _read_rows(state_root: str, task_id: str) -> list[dict]:
    with open(_events_path(state_root, task_id), "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_rows(state_root: str, task_id: str, rows: list[dict]) -> None:
    """Bypass the Task class to write controlled synthetic event rows."""
    path = _events_path(state_root, task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _baseline_meta(**overrides) -> dict:
    """Minimal provenance stamp for synthetic rows.

    Lets tests author ``task_events.jsonl`` rows without going
    through ``Task._append_event`` so the gate / tolerance
    semantics can be exercised in isolation.
    """
    base = {
        "git_sha": "abc123",
        "git_branch": "main",
        "git_dirty": False,
        "timestamp_utc": "2026-05-01T00:00:00+00:00",
        "env_snapshot": {},
        "config_hash": "deadbeef",
        "harness_version": "phase1",
        "orchestrator_version": "phase5",
        "task_id": "t1",
        "survives_git_sha_flip": True,
    }
    base.update(overrides)
    return base


def _bootstrap_row(**overrides) -> dict:
    """Synthetic ``task_create`` row baseline."""
    row = _baseline_meta(
        event="task_create",
        goal="hello world",
        workspace_path="/tmp/ws/t1",
        created_at="2026-05-01T00:00:00+00:00",
        decision_note="goal: hello world",
    )
    row.update(overrides)
    return row


@pytest.fixture
def state_root(tmp_path):
    return str(tmp_path / "state")


@pytest.fixture
def workspaces_dir(tmp_path):
    return str(tmp_path / "workspaces")


@pytest.fixture
def cases_dir(tmp_path):
    return str(tmp_path / "cases")


_FULL_TOML = """\
task_id = "t1"
goal = "hello world"

[[subtasks]]
subtask_id = "s1"
prompt = "first"

[[subtasks]]
subtask_id = "s2"
prompt = "second"
"""


@pytest.fixture
def seeded_task(tmp_path, state_root, workspaces_dir):
    """A real Task with two subtasks loaded via from_toml."""
    toml_path = str(tmp_path / "t1.toml")
    with open(toml_path, "w", encoding="utf-8") as fh:
        fh.write(_FULL_TOML)
    return Task.from_toml(
        toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
    )


# ---------------------------------------------------------------------------
# survives_git_sha_flip — write-path adds the field
# ---------------------------------------------------------------------------


class TestAppendEventGateField:
    """``_append_event`` stamps ``survives_git_sha_flip=True`` by default."""

    def test_default_true_on_every_event(self, seeded_task, state_root):
        seeded_task.start_subtask("s1")
        seeded_task.complete_subtask("s1", cost_usd=0.1)
        seeded_task.record_decision("policy_pause", "n")
        rows = _read_rows(state_root, "t1")
        for row in rows:
            assert row["survives_git_sha_flip"] is True, row

    def test_explicit_false_persists(self, state_root, workspaces_dir):
        # Direct construction skips the from_toml bootstrap; we want to
        # exercise the per-call kwarg without writing two events.
        t = Task(
            task_id="t1", goal="g",
            workspace_path=os.path.join(workspaces_dir, "t1"),
            created_at="2026-05-01T00:00:00+00:00",
            state_root=state_root,
        )
        t._append_event(
            "decision",
            {"kind": "policy_pause", "note": "x", "decision_timestamp": "ts"},
            survives_git_sha_flip=False,
        )
        rows = _read_rows(state_root, "t1")
        assert rows[-1]["survives_git_sha_flip"] is False


# ---------------------------------------------------------------------------
# load_task — basic happy paths
# ---------------------------------------------------------------------------


class TestLoadTaskBasic:

    def test_missing_file_raises(self, state_root):
        with pytest.raises(TaskError, match="not found"):
            load_task("nope", state_root=state_root)

    def test_empty_log_raises_no_task_create(self, state_root):
        os.makedirs(os.path.join(state_root, "t1"))
        path = _events_path(state_root, "t1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
        with pytest.raises(TaskError, match="no\\s+task_create event"):
            load_task("t1", state_root=state_root)

    def test_missing_task_create_raises(self, state_root):
        # A log with only an enqueue_subtask row (corrupted) cannot
        # reconstruct the Task because there's no goal / workspace_path.
        _write_rows(state_root, "t1", [
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s1", prompt="p",
            ),
        ])
        with pytest.raises(TaskError, match="no\\s+task_create event"):
            load_task("t1", state_root=state_root)

    def test_minimal_round_trip(self, state_root):
        _write_rows(state_root, "t1", [_bootstrap_row()])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.task_id == "t1"
        assert t.goal == "hello world"
        assert t.workspace_path == "/tmp/ws/t1"
        assert t.created_at == "2026-05-01T00:00:00+00:00"
        assert t.subtasks == []
        # task_create Decision is reconstructed from the bootstrap row.
        assert len(t.decisions) == 1
        assert t.decisions[0].kind == "task_create"
        assert t.decisions[0].note == "goal: hello world"

    def test_state_root_default_used_when_omitted(self, monkeypatch, tmp_path):
        # Point DEFAULT_STATE_ROOT at tmp_path; verify load_task uses it
        # when state_root kwarg is omitted.
        fake_root = str(tmp_path / "default-state")
        monkeypatch.setattr(task_mod, "DEFAULT_STATE_ROOT", fake_root)
        _write_rows(fake_root, "t1", [_bootstrap_row()])
        t = load_task("t1")  # no state_root kwarg
        assert t.task_id == "t1"

    def test_non_string_task_id_raises(self, state_root):
        with pytest.raises(TaskError, match="task_id"):
            load_task(42, state_root=state_root)  # type: ignore[arg-type]

    def test_empty_task_id_raises(self, state_root):
        with pytest.raises(TaskError, match="task_id"):
            load_task("", state_root=state_root)


# ---------------------------------------------------------------------------
# load_task — state replay
# ---------------------------------------------------------------------------


class TestLoadTaskReplay:

    def test_subtasks_replayed_in_order(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s1", prompt="first",
            ),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s2", prompt="second",
            ),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s3", prompt="third",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert [s.subtask_id for s in t.subtasks] == ["s1", "s2", "s3"]
        assert [s.prompt for s in t.subtasks] == ["first", "second", "third"]
        assert all(s.status == "pending" for s in t.subtasks)

    def test_done_subtask_preserved(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask",
                subtask_id="s1",
                started_at="2026-05-01T00:00:01+00:00",
            ),
            _baseline_meta(
                event="complete_subtask",
                subtask_id="s1",
                finished_at="2026-05-01T00:00:02+00:00",
                result_summary="ok",
                cost_usd=0.5,
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        st = t.subtasks[0]
        assert st.status == "done"
        assert st.started_at == "2026-05-01T00:00:01+00:00"
        assert st.finished_at == "2026-05-01T00:00:02+00:00"
        assert st.result_summary == "ok"
        assert st.cost_usd == 0.5

    def test_failed_subtask_preserved(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1",
                started_at="2026-05-01T00:00:01+00:00",
            ),
            _baseline_meta(
                event="fail_subtask", subtask_id="s1",
                finished_at="2026-05-01T00:00:02+00:00",
                result_summary="boom",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        st = t.subtasks[0]
        assert st.status == "failed"
        assert st.result_summary == "boom"

    def test_pending_subtask_preserved(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.subtasks[0].status == "pending"
        assert t.subtasks[0].started_at is None

    def test_decision_replayed_to_in_memory_list(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="decision", kind="policy_pause",
                note="5h cap",
                decision_timestamp="2026-05-01T00:01:00+00:00",
            ),
            _baseline_meta(
                event="decision", kind="worker_spawn",
                note="spawn s1",
                decision_timestamp="2026-05-01T00:02:00+00:00",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        # task_create + 2 explicit = 3 decisions.
        kinds = [d.kind for d in t.decisions]
        assert kinds == ["task_create", "policy_pause", "worker_spawn"]
        assert t.decisions[1].note == "5h cap"
        assert t.decisions[1].timestamp == "2026-05-01T00:01:00+00:00"

    def test_unknown_decision_kind_skipped(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="decision", kind="bogus",
                note="x", decision_timestamp="ts",
            ),
            _baseline_meta(
                event="decision", kind="policy_halt",
                note="real", decision_timestamp="ts",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        kinds = [d.kind for d in t.decisions]
        assert "bogus" not in kinds
        assert "policy_halt" in kinds

    def test_replay_tolerant_of_repeated_start_subtask(self, state_root):
        # Two start_subtask events for the same id (simulating a
        # post-resume re-spawn). Replay must NOT raise; the second
        # write wins.
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1",
                started_at="2026-05-01T00:00:01+00:00",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1",
                started_at="2026-05-01T00:00:50+00:00",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.subtasks[0].status == "in_flight"
        assert t.subtasks[0].started_at == "2026-05-01T00:00:50+00:00"


# ---------------------------------------------------------------------------
# load_task — in-flight reset (the resume contract)
# ---------------------------------------------------------------------------


class TestInFlightReset:

    def _setup_in_flight_log(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1",
                started_at="2026-05-01T00:00:01+00:00",
            ),
            # Note: no complete or fail — orchestrator died mid-flight.
        ])

    def test_in_flight_subtask_re_enqueued_to_pending(self, state_root):
        self._setup_in_flight_log(state_root)
        t = load_task("t1", state_root=state_root)
        assert t.subtasks[0].status == "pending"

    def test_started_at_cleared_on_re_enqueue(self, state_root):
        self._setup_in_flight_log(state_root)
        t = load_task("t1", state_root=state_root)
        assert t.subtasks[0].started_at is None

    def test_task_resume_decision_appended(self, state_root):
        self._setup_in_flight_log(state_root)
        t = load_task("t1", state_root=state_root)
        last = t.decisions[-1]
        assert last.kind == "task_resume"
        assert "s1" in last.note

    def test_task_resume_decision_persisted_to_jsonl(self, state_root):
        self._setup_in_flight_log(state_root)
        rows_before = _read_rows(state_root, "t1")
        load_task("t1", state_root=state_root)
        rows_after = _read_rows(state_root, "t1")
        # One additional row: the task_resume decision write.
        assert len(rows_after) == len(rows_before) + 1
        new_row = rows_after[-1]
        assert new_row["event"] == "decision"
        assert new_row["kind"] == "task_resume"
        assert "s1" in new_row["note"]

    def test_no_in_flight_writes_no_in_flight_note(self, state_root):
        # All subtasks already terminal at end-of-replay.
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1",
                started_at="2026-05-01T00:00:01+00:00",
            ),
            _baseline_meta(
                event="complete_subtask", subtask_id="s1",
                finished_at="2026-05-01T00:00:02+00:00",
                result_summary="ok", cost_usd=0.1,
            ),
        ])
        t = load_task("t1", state_root=state_root)
        assert t.decisions[-1].kind == "task_resume"
        assert "no in-flight subtasks" in t.decisions[-1].note

    def test_done_and_failed_preserved_on_resume(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s2", prompt="p",
            ),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s3", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1",
                started_at="ts",
            ),
            _baseline_meta(
                event="complete_subtask", subtask_id="s1",
                finished_at="ts", result_summary="ok", cost_usd=0.1,
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s2",
                started_at="ts",
            ),
            _baseline_meta(
                event="fail_subtask", subtask_id="s2",
                finished_at="ts", result_summary="boom",
            ),
            # s3 is left mid-flight.
            _baseline_meta(
                event="start_subtask", subtask_id="s3",
                started_at="ts",
            ),
        ])
        t = load_task("t1", state_root=state_root)
        statuses = {st.subtask_id: st.status for st in t.subtasks}
        assert statuses == {
            "s1": "done", "s2": "failed", "s3": "pending",
        }
        # Re-enqueue note lists s3 only.
        assert "s3" in t.decisions[-1].note
        assert "s1" not in t.decisions[-1].note
        assert "s2" not in t.decisions[-1].note

    def test_multiple_in_flight_all_re_enqueued(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s2", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s1", started_at="ts",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="s2", started_at="ts",
            ),
        ])
        t = load_task("t1", state_root=state_root)
        for st in t.subtasks:
            assert st.status == "pending"
        assert "s1" in t.decisions[-1].note
        assert "s2" in t.decisions[-1].note

    def test_mark_resume_false_skips_reset(self, state_root):
        self._setup_in_flight_log(state_root)
        t = load_task("t1", state_root=state_root, mark_resume=False)
        # In-flight preserved, no task_resume decision appended.
        assert t.subtasks[0].status == "in_flight"
        assert t.subtasks[0].started_at == "2026-05-01T00:00:01+00:00"
        kinds = [d.kind for d in t.decisions]
        assert "task_resume" not in kinds

    def test_mark_resume_false_does_not_write_to_log(self, state_root):
        self._setup_in_flight_log(state_root)
        rows_before = _read_rows(state_root, "t1")
        load_task("t1", state_root=state_root, mark_resume=False)
        rows_after = _read_rows(state_root, "t1")
        assert len(rows_after) == len(rows_before)


# ---------------------------------------------------------------------------
# load_task — survives_git_sha_flip gate
# ---------------------------------------------------------------------------


class TestGitShaGate:

    def test_default_true_missing_field_replays(self, state_root, monkeypatch):
        # Author a row WITHOUT the field; replay must apply it.
        bootstrap = _bootstrap_row()
        del bootstrap["survives_git_sha_flip"]
        _write_rows(state_root, "t1", [bootstrap])
        # Force current_sha != recorded so the gate would fire if the
        # default-True wasn't honoured.
        monkeypatch.setattr(
            "integration.provenance._git_capture",
            lambda *_a, **_kw: "different-sha",
        )
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.task_id == "t1"

    def test_explicit_true_replays_regardless_of_sha(
        self, state_root, monkeypatch,
    ):
        _write_rows(state_root, "t1", [
            _bootstrap_row(survives_git_sha_flip=True, git_sha="recorded"),
        ])
        monkeypatch.setattr(
            "integration.provenance._git_capture",
            lambda *_a, **_kw: "current-different",
        )
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.task_id == "t1"

    def test_false_with_matching_sha_replays(self, state_root, monkeypatch):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s1", prompt="p",
                git_sha="match",
                survives_git_sha_flip=False,
            ),
        ])
        monkeypatch.setattr(
            "integration.provenance._git_capture",
            lambda *_a, **_kw: "match",
        )
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert len(t.subtasks) == 1
        assert t.subtasks[0].subtask_id == "s1"

    def test_false_with_mismatched_sha_dropped(self, state_root, monkeypatch):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s_dropped", prompt="p",
                git_sha="old-sha",
                survives_git_sha_flip=False,
            ),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s_kept", prompt="p",
                git_sha="old-sha",
                survives_git_sha_flip=True,
            ),
        ])
        monkeypatch.setattr(
            "integration.provenance._git_capture",
            lambda *_a, **_kw: "new-sha",
        )
        t = load_task("t1", state_root=state_root, mark_resume=False)
        ids = [s.subtask_id for s in t.subtasks]
        assert ids == ["s_kept"]

    def test_dropped_event_emits_stderr_note(
        self, state_root, monkeypatch, capsys,
    ):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s1", prompt="p",
                git_sha="old",
                survives_git_sha_flip=False,
            ),
        ])
        monkeypatch.setattr(
            "integration.provenance._git_capture",
            lambda *_a, **_kw: "new",
        )
        load_task("t1", state_root=state_root, mark_resume=False)
        captured = capsys.readouterr()
        assert "load_task" in captured.err
        assert "git_sha mismatch" in captured.err
        assert "old" in captured.err
        assert "new" in captured.err

    def test_drop_count_appended_to_resume_note(
        self, state_root, monkeypatch,
    ):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s1", prompt="p",
                git_sha="old", survives_git_sha_flip=False,
            ),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s2", prompt="p",
                git_sha="old", survives_git_sha_flip=False,
            ),
        ])
        monkeypatch.setattr(
            "integration.provenance._git_capture",
            lambda *_a, **_kw: "new",
        )
        t = load_task("t1", state_root=state_root)
        # Both enqueue rows dropped; no subtasks; resume note records
        # the drop count.
        assert t.subtasks == []
        note = t.decisions[-1].note
        assert "dropped 2 git_sha-gated events" in note


# ---------------------------------------------------------------------------
# load_task — forgiving reader
# ---------------------------------------------------------------------------


class TestReaderTolerance:

    def test_blank_lines_skipped(self, state_root):
        os.makedirs(os.path.join(state_root, "t1"))
        path = _events_path(state_root, "t1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_bootstrap_row()) + "\n")
            fh.write("\n\n   \n")
            fh.write(json.dumps(_baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            )) + "\n")
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert [s.subtask_id for s in t.subtasks] == ["s1"]

    def test_malformed_json_skipped(self, state_root):
        os.makedirs(os.path.join(state_root, "t1"))
        path = _events_path(state_root, "t1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_bootstrap_row()) + "\n")
            fh.write("{this is not valid json\n")
            fh.write(json.dumps(_baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            )) + "\n")
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert [s.subtask_id for s in t.subtasks] == ["s1"]

    def test_non_dict_row_skipped(self, state_root):
        os.makedirs(os.path.join(state_root, "t1"))
        path = _events_path(state_root, "t1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(_bootstrap_row()) + "\n")
            fh.write(json.dumps([1, 2, 3]) + "\n")
            fh.write(json.dumps("just a string") + "\n")
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.task_id == "t1"

    def test_unknown_event_type_skipped(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(event="not_a_real_event", payload="x"),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert [s.subtask_id for s in t.subtasks] == ["s1"]

    def test_unknown_subtask_in_state_event_skipped(
        self, state_root, capsys,
    ):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
            _baseline_meta(
                event="start_subtask", subtask_id="phantom",
                started_at="ts",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        # s1 untouched (status pending), no crash.
        assert t.subtasks[0].status == "pending"
        captured = capsys.readouterr()
        assert "unknown subtask_id" in captured.err
        assert "phantom" in captured.err

    def test_enqueue_subtask_missing_fields_skipped(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            # Missing prompt.
            _baseline_meta(event="enqueue_subtask", subtask_id="s1"),
            # Missing subtask_id.
            _baseline_meta(event="enqueue_subtask", prompt="p"),
            # Wrong types.
            _baseline_meta(
                event="enqueue_subtask", subtask_id=123, prompt="p",
            ),
            # Valid:
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s_real", prompt="p",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert [s.subtask_id for s in t.subtasks] == ["s_real"]

    def test_duplicate_task_create_ignored(self, state_root, capsys):
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            # Second task_create with different goal — ignored, the
            # first one wins.
            _bootstrap_row(goal="different goal"),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.goal == "hello world"
        captured = capsys.readouterr()
        assert "duplicate task_create" in captured.err


# ---------------------------------------------------------------------------
# load_task — full lifecycle round-trip via real Task operations
# ---------------------------------------------------------------------------


class TestRoundTripWithRealTask:

    def test_from_toml_then_load_round_trip(
        self, seeded_task, state_root,
    ):
        # seeded_task wrote 1 task_create + 2 enqueue_subtask.
        loaded = load_task("t1", state_root=state_root, mark_resume=False)
        assert loaded.task_id == seeded_task.task_id
        assert loaded.goal == seeded_task.goal
        assert loaded.workspace_path == seeded_task.workspace_path
        assert loaded.created_at == seeded_task.created_at
        assert [s.subtask_id for s in loaded.subtasks] == ["s1", "s2"]
        # Decisions list preserved (just task_create).
        assert [d.kind for d in loaded.decisions] == ["task_create"]

    def test_load_after_kill_mid_subtask(self, seeded_task, state_root):
        # Original orchestrator runs until s1 is in_flight, then dies.
        seeded_task.start_subtask("s1")
        # No complete / fail event written.
        loaded = load_task("t1", state_root=state_root)
        statuses = {s.subtask_id: s.status for s in loaded.subtasks}
        assert statuses == {"s1": "pending", "s2": "pending"}
        # task_resume decision appended.
        assert loaded.decisions[-1].kind == "task_resume"
        # ISO-8601 timestamp on the resume decision.
        assert _ISO_PATTERN.match(loaded.decisions[-1].timestamp)

    def test_load_after_one_done_one_in_flight(
        self, seeded_task, state_root,
    ):
        seeded_task.start_subtask("s1")
        seeded_task.complete_subtask("s1", cost_usd=0.5)
        seeded_task.start_subtask("s2")
        # Kill before s2 completes.
        loaded = load_task("t1", state_root=state_root)
        statuses = {s.subtask_id: s.status for s in loaded.subtasks}
        assert statuses == {"s1": "done", "s2": "pending"}
        assert loaded.subtasks[0].cost_usd == 0.5
        assert "s2" in loaded.decisions[-1].note

    def test_resumed_task_is_writable(self, seeded_task, state_root):
        # After load_task, the returned Task can continue normally.
        seeded_task.start_subtask("s1")
        loaded = load_task("t1", state_root=state_root)
        # s1 is pending again; we can start_subtask + complete_subtask
        # without errors. This is the practical resume-write contract.
        loaded.start_subtask("s1")
        loaded.complete_subtask("s1", cost_usd=0.2)
        assert loaded.subtasks[0].status == "done"
        # Subsequent load_task observes the new state.
        reloaded = load_task("t1", state_root=state_root, mark_resume=False)
        s1 = next(s for s in reloaded.subtasks if s.subtask_id == "s1")
        assert s1.status == "done"
        assert s1.cost_usd == 0.2


# ---------------------------------------------------------------------------
# Phase 5 §2 — stages and per-subtask stage_id replay
# ---------------------------------------------------------------------------


class TestStagesReplay:
    """``load_task`` reconstructs Phase 5 §2 stages and per-subtask
    stage_id from the persisted JSONL log."""

    def test_stages_replayed_from_task_create_event(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(
                stages=[
                    {
                        "stage_id": "stage_a",
                        "name": "First",
                        "depends_on": [],
                        "expected_duration_seconds": 600.0,
                        "expected_spend_usd": 5.0,
                    },
                    {
                        "stage_id": "stage_b",
                        "name": "Second",
                        "depends_on": ["stage_a"],
                        "expected_duration_seconds": 900.0,
                        "expected_spend_usd": 7.5,
                    },
                ],
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert len(t.stages) == 2
        assert t.stages[0].stage_id == "stage_a"
        assert t.stages[0].expected_duration_seconds == 600.0
        assert t.stages[1].depends_on == ["stage_a"]
        assert t.stages[1].expected_spend_usd == 7.5

    def test_subtask_stage_id_replayed(self, state_root):
        _write_rows(state_root, "t1", [
            _bootstrap_row(stages=[
                {"stage_id": "stage_a", "name": "", "depends_on": []},
            ]),
            _baseline_meta(
                event="enqueue_subtask",
                subtask_id="s1", prompt="p", stage_id="stage_a",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.subtasks[0].stage_id == "stage_a"

    def test_pre_stage_log_replays_with_empty_stages(self, state_root):
        # Pre-§2 logs lack the ``stages`` field on task_create and
        # ``stage_id`` on enqueue_subtask. Replay must produce
        # ``Task.stages == []`` and ``Subtask.stage_id is None``.
        # ``_bootstrap_row()`` already omits both fields so it stands
        # in for a pre-§2 log row.
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.stages == []
        assert t.subtasks[0].stage_id is None

    def test_corrupted_stages_payload_dropped(self, state_root):
        # Forgiving reader: a non-list ``stages`` field on the
        # task_create row leaves task.stages empty rather than crashing.
        _write_rows(state_root, "t1", [
            _bootstrap_row(stages="not a list"),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.stages == []

    def test_corrupted_stage_entry_skipped(self, state_root):
        # One valid stage + several malformed entries. Replay keeps the
        # valid one, drops the rest.
        _write_rows(state_root, "t1", [
            _bootstrap_row(stages=[
                "not a dict",
                {"stage_id": ""},  # empty id
                {"stage_id": 42},  # wrong type
                {
                    "stage_id": "good",
                    "name": "valid",
                    "depends_on": ["a", "", 99, "b"],  # filtered
                },
            ]),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert len(t.stages) == 1
        assert t.stages[0].stage_id == "good"
        # Non-string / empty-string deps are filtered out by the
        # forgiving reader.
        assert t.stages[0].depends_on == ["a", "b"]

    def test_corrupted_stage_metadata_normalised_to_none(self, state_root):
        # Non-numeric expected_* fields (or bool, since bool ⊂ int in
        # Python) get reset to None on replay.
        _write_rows(state_root, "t1", [
            _bootstrap_row(stages=[
                {
                    "stage_id": "s",
                    "expected_duration_seconds": "not a number",
                    "expected_spend_usd": True,
                },
            ]),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.stages[0].expected_duration_seconds is None
        assert t.stages[0].expected_spend_usd is None

    def test_corrupted_subtask_stage_id_replays_as_none(self, state_root):
        # Forgiving reader: bad stage_id on enqueue_subtask becomes None.
        _write_rows(state_root, "t1", [
            _bootstrap_row(),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s1", prompt="p",
                stage_id=42,
            ),
            _baseline_meta(
                event="enqueue_subtask", subtask_id="s2", prompt="p",
                stage_id="",
            ),
        ])
        t = load_task("t1", state_root=state_root, mark_resume=False)
        assert t.subtasks[0].stage_id is None
        assert t.subtasks[1].stage_id is None

    def test_from_toml_then_load_round_trip_preserves_stages(
        self, tmp_path, state_root, workspaces_dir,
    ):
        toml_path = str(tmp_path / "t1.toml")
        with open(toml_path, "w", encoding="utf-8") as fh:
            fh.write("""\
task_id = "t1"
goal = "g"
[[stages]]
stage_id = "stage_a"
name = "First"
expected_duration_seconds = 600
expected_spend_usd = 5.0
[[subtasks]]
subtask_id = "s1"
stage_id = "stage_a"
prompt = "p"
""")
        t_orig = Task.from_toml(
            toml_path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        loaded = load_task("t1", state_root=state_root, mark_resume=False)
        assert len(loaded.stages) == 1
        assert loaded.stages[0].stage_id == t_orig.stages[0].stage_id
        assert loaded.stages[0].name == "First"
        assert loaded.stages[0].expected_duration_seconds == 600.0
        assert loaded.stages[0].expected_spend_usd == 5.0
        assert loaded.subtasks[0].stage_id == "stage_a"


# ---------------------------------------------------------------------------
# CLI — start / resume / status wiring
# ---------------------------------------------------------------------------


def _make_args(
    task: str,
    *,
    state_root: str | None = None,
    cases_dir: str | None = None,
    workspaces_dir: str | None = None,
    simulate_rate_status: str | None = None,
) -> argparse.Namespace:
    """Build a Namespace mirroring argparse output."""
    return argparse.Namespace(
        task=task,
        state_root=state_root,
        cases_dir=cases_dir,
        workspaces_dir=workspaces_dir,
        simulate_rate_status=simulate_rate_status,
    )


@pytest.fixture
def stub_loop(monkeypatch):
    """Disable ``cli._run_loop`` so §7 CLI tests verify the bootstrap /
    replay scaffolding without engaging the §8 main loop. §8-specific
    tests live in ``tests/test_orchestrator_loop.py`` and exercise the
    real loop with a stubbed worker.
    """
    monkeypatch.setattr(cli_mod, "_run_loop", lambda *a, **kw: None)


class TestCliStart:

    def test_start_creates_fresh_task_when_no_log(
        self, state_root, cases_dir, workspaces_dir, stub_loop,
    ):
        os.makedirs(cases_dir)
        with open(os.path.join(cases_dir, "t1.toml"), "w") as fh:
            fh.write(_FULL_TOML)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_start(args)
        assert rc == 0
        # task_events.jsonl was created with the bootstrap rows.
        rows = _read_rows(state_root, "t1")
        assert rows[0]["event"] == "task_create"
        # Workspace was set up.
        assert os.path.isdir(os.path.join(workspaces_dir, "t1"))

    def test_start_routes_to_resume_when_log_exists(
        self, state_root, cases_dir, workspaces_dir, stub_loop, capsys,
    ):
        # Prime an existing log without a corresponding case TOML —
        # if start tried to load TOML it would FileNotFound.
        _write_rows(state_root, "t1", [_bootstrap_row()])
        os.makedirs(cases_dir)  # empty
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_start(args)
        assert rc == 0
        # cmd_resume's path was taken: a task_resume decision was
        # written.
        rows = _read_rows(state_root, "t1")
        assert rows[-1]["event"] == "decision"
        assert rows[-1]["kind"] == "task_resume"

    def test_start_prints_summary(
        self, state_root, cases_dir, workspaces_dir, stub_loop, capsys,
    ):
        os.makedirs(cases_dir)
        with open(os.path.join(cases_dir, "t1.toml"), "w") as fh:
            fh.write(_FULL_TOML)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        cli_mod.cmd_start(args)
        out = capsys.readouterr().out
        assert "Task: t1" in out
        assert "Goal: hello world" in out
        assert "Subtasks:" in out
        assert "Total spend:" in out
        assert "Last decision:" in out


class TestCliResume:

    def test_resume_calls_load_task_and_prints_summary(
        self, state_root, workspaces_dir, stub_loop, capsys, seeded_task,
    ):
        seeded_task.start_subtask("s1")
        args = _make_args(
            "t1", state_root=state_root,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_resume(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Task: t1" in out
        # s1 was in_flight; resume re-enqueued; counts now show 2 pending.
        assert "2 pending" in out
        assert "0 in_flight" in out
        # Last decision should be the task_resume just written.
        assert "Last decision: task_resume" in out

    def test_resume_missing_log_raises(
        self, state_root, workspaces_dir,
    ):
        args = _make_args(
            "nope", state_root=state_root,
            workspaces_dir=workspaces_dir,
        )
        with pytest.raises(TaskError):
            cli_mod.cmd_resume(args)

    def test_resume_workspace_setup_idempotent(
        self, state_root, workspaces_dir, stub_loop, seeded_task,
    ):
        # Pre-populate workspace with a marker; resume must not destroy.
        ws = os.path.join(workspaces_dir, "t1")
        os.makedirs(ws, exist_ok=True)
        marker = os.path.join(ws, "preserved.txt")
        with open(marker, "w") as fh:
            fh.write("don't delete me")
        args = _make_args(
            "t1", state_root=state_root,
            workspaces_dir=workspaces_dir,
        )
        cli_mod.cmd_resume(args)
        assert os.path.isfile(marker)


class TestCliStatus:

    def test_status_does_not_write_resume_decision(
        self, state_root, workspaces_dir, seeded_task,
    ):
        seeded_task.start_subtask("s1")
        rows_before = _read_rows(state_root, "t1")
        args = _make_args(
            "t1", state_root=state_root,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_status(args)
        assert rc == 0
        rows_after = _read_rows(state_root, "t1")
        # No new rows.
        assert len(rows_after) == len(rows_before)

    def test_status_prints_in_flight_unmodified(
        self, state_root, workspaces_dir, capsys, seeded_task,
    ):
        seeded_task.start_subtask("s1")
        args = _make_args(
            "t1", state_root=state_root,
            workspaces_dir=workspaces_dir,
        )
        cli_mod.cmd_status(args)
        out = capsys.readouterr().out
        # status preserves the in_flight (no end-of-replay sweep).
        assert "1 in_flight" in out
        assert "1 pending" in out


class TestCliBuildParser:

    def test_help_lists_all_subcommands(self):
        parser = cli_mod.build_parser()
        # Sanity: subcommand-required argparse.
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_start_subcommand_accepts_flags(self):
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "start", "t1",
            "--state-root", "/tmp/sr",
            "--cases-dir", "/tmp/cd",
            "--workspaces-dir", "/tmp/wd",
        ])
        assert args.task == "t1"
        assert args.state_root == "/tmp/sr"
        assert args.cases_dir == "/tmp/cd"
        assert args.workspaces_dir == "/tmp/wd"

    def test_resume_subcommand_accepts_flags(self):
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "resume", "t1", "--state-root", "/tmp/sr",
        ])
        assert args.task == "t1"
        assert args.state_root == "/tmp/sr"

    def test_status_subcommand_accepts_flags(self):
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "status", "t1", "--state-root", "/tmp/sr",
        ])
        assert args.task == "t1"
        assert args.state_root == "/tmp/sr"

    def test_pause_subcommand_accepts_flags(self):
        # §8 wires pause/halt; their parser entries gained the same
        # path + simulate flags as start/resume.
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "pause", "t1",
            "--state-root", "/tmp/sr",
            "--simulate-rate-status", "five_hour_pause",
        ])
        assert args.task == "t1"
        assert args.state_root == "/tmp/sr"
        assert args.simulate_rate_status == "five_hour_pause"

    def test_halt_subcommand_accepts_flags(self):
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "halt", "t1",
            "--state-root", "/tmp/sr",
            "--simulate-rate-status", "seven_day_wrap_up",
        ])
        assert args.task == "t1"
        assert args.state_root == "/tmp/sr"
        assert args.simulate_rate_status == "seven_day_wrap_up"


# ---------------------------------------------------------------------------
# CLI _print_summary edge cases
# ---------------------------------------------------------------------------


class TestPrintSummary:

    def test_empty_subtasks_renders_zero_counts(self):
        t = Task(
            task_id="empty",
            goal="g",
            workspace_path="/tmp",
            created_at="ts",
        )
        buf = io.StringIO()
        cli_mod._print_summary(t, file=buf)
        out = buf.getvalue()
        assert "0 pending" in out
        assert "0 done" in out
        assert "Total spend: $0.0000" in out
        assert "Last decision: (none)" in out

    def test_total_spend_sums_subtask_costs(self):
        t = Task(
            task_id="t",
            goal="g",
            workspace_path="/tmp",
            created_at="ts",
            subtasks=[
                Subtask(
                    subtask_id="s1", prompt="p", status="done",
                    cost_usd=0.5,
                ),
                Subtask(
                    subtask_id="s2", prompt="p", status="done",
                    cost_usd=1.25,
                ),
                Subtask(subtask_id="s3", prompt="p", status="pending"),
            ],
        )
        buf = io.StringIO()
        cli_mod._print_summary(t, file=buf)
        assert "Total spend: $1.7500" in buf.getvalue()

    def test_last_decision_rendered(self):
        t = Task(
            task_id="t", goal="g", workspace_path="/tmp",
            created_at="ts",
            decisions=[
                Decision(timestamp="ts1", kind="policy_pause", note="cap"),
                Decision(timestamp="ts2", kind="task_resume", note="back"),
            ],
        )
        buf = io.StringIO()
        cli_mod._print_summary(t, file=buf)
        assert "Last decision: task_resume — back" in buf.getvalue()
