"""Tests for the Phase 3 §8 main orchestration loop.

Pure-Python: no container engine, no live Claude. ``worker.spawn_worker``
is monkeypatched to a stub that returns a synthetic terminal-result
dict so the loop's policy → spawn → complete → repeat path can be
exercised against ``tmp_path``-rooted state without spending real
paid-buffer dollars or building the engine image.

§7 CLI scaffolding tests live in ``tests/test_orchestrator_resume.py``;
this file adds §8 coverage for: ``--simulate-rate-status`` synthesis,
``_seed_policy_override`` file copy, ``_run_loop`` policy gating and
worker dispatch, ``cmd_pause`` / ``cmd_halt`` decision recording, and
the round-trip pause + resume cycle the §8 PLAN exit criteria call
out.
"""

import argparse
import json
import os

import pytest

from orchestrator import buffer_ledger as buffer_ledger_mod
from orchestrator import cli as cli_mod
from orchestrator import policy as policy_mod
from orchestrator import rate_ledger as rate_ledger_mod
from orchestrator.task import Task, load_task

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_root(tmp_path):
    return str(tmp_path / "state")


@pytest.fixture
def workspaces_dir(tmp_path):
    return str(tmp_path / "workspaces")


@pytest.fixture
def cases_dir(tmp_path):
    p = str(tmp_path / "cases")
    os.makedirs(p)
    return p


@pytest.fixture
def seeded_case(cases_dir):
    """Write a 2-subtask case TOML at <cases_dir>/t1.toml."""
    path = os.path.join(cases_dir, "t1.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_FULL_TOML)
    return path


@pytest.fixture
def stub_worker(monkeypatch):
    """Replace ``orchestrator.worker.spawn_worker`` with a success stub.

    Captures the arguments of every call so tests can assert dispatch
    order. Returns a synthetic terminal ``result`` dict shaped like
    Claude Code's stream-json — the loop reads ``result.usage`` and
    ``result.total_cost_usd`` from it. The §3 / §4 ledgers are NOT
    written by the stub; tests that care about ledger state seed
    those rows directly via the ledger APIs.
    """
    calls = []

    def stub(prompt, *, workspace, task_id, subtask_id,
             timeout_seconds=None, state_root=None):
        calls.append({
            "prompt": prompt,
            "workspace": workspace,
            "task_id": task_id,
            "subtask_id": subtask_id,
        })
        return {
            "exit_code": 0,
            "rate_limit_events": [],
            "result": {
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": 0.05,
                "modelUsage": {
                    "claude-opus-4-7": {
                        "inputTokens": 100, "outputTokens": 50,
                    },
                },
            },
            "output": "done",
            "raw_lines": [],
        }

    monkeypatch.setattr("orchestrator.worker.spawn_worker", stub)
    return calls


@pytest.fixture
def fail_worker(monkeypatch):
    """Stub that simulates a failing worker (exit_code != 0)."""
    calls = []

    def stub(prompt, *, workspace, task_id, subtask_id,
             timeout_seconds=None, state_root=None):
        calls.append({
            "prompt": prompt,
            "workspace": workspace,
            "task_id": task_id,
            "subtask_id": subtask_id,
        })
        return {
            "exit_code": 1,
            "rate_limit_events": [],
            "result": None,
            "output": "",
            "raw_lines": [],
        }

    monkeypatch.setattr("orchestrator.worker.spawn_worker", stub)
    return calls


@pytest.fixture
def timeout_worker(monkeypatch):
    """Stub that simulates a timeout (exit_code=-1, terminal_reason)."""
    calls = []

    def stub(prompt, *, workspace, task_id, subtask_id,
             timeout_seconds=None, state_root=None):
        calls.append({
            "prompt": prompt,
            "workspace": workspace,
            "task_id": task_id,
            "subtask_id": subtask_id,
        })
        return {
            "exit_code": -1,
            "rate_limit_events": [],
            "result": {
                "terminal_reason": "timeout",
                "subtask_id": subtask_id,
                "container_name": "stub",
            },
            "output": "",
            "raw_lines": [],
        }

    monkeypatch.setattr("orchestrator.worker.spawn_worker", stub)
    return calls


def _make_args(
    task: str,
    *,
    state_root: str | None = None,
    cases_dir: str | None = None,
    workspaces_dir: str | None = None,
    simulate_rate_status: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        task=task,
        state_root=state_root,
        cases_dir=cases_dir,
        workspaces_dir=workspaces_dir,
        simulate_rate_status=simulate_rate_status,
    )


def _read_rows(state_root: str, task_id: str) -> list[dict]:
    path = os.path.join(state_root, task_id, "task_events.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ---------------------------------------------------------------------------
# _simulated_rate_status
# ---------------------------------------------------------------------------


class TestSimulatedRateStatus:

    def test_allowed_returns_both_allowed(self):
        rs = cli_mod._simulated_rate_status("allowed")
        assert rs["five_hour"]["status"] == "allowed"
        assert rs["seven_day"]["status"] == "allowed"

    def test_five_hour_pause_only_five_hour_non_allowed(self):
        rs = cli_mod._simulated_rate_status("five_hour_pause")
        assert rs["five_hour"]["status"] != "allowed"
        assert rs["seven_day"]["status"] == "allowed"
        assert rs["five_hour"]["resetsAt"] is not None

    def test_seven_day_wrap_up_only_seven_day_non_allowed(self):
        rs = cli_mod._simulated_rate_status("seven_day_wrap_up")
        assert rs["five_hour"]["status"] == "allowed"
        assert rs["seven_day"]["status"] != "allowed"
        assert rs["seven_day"]["resetsAt"] is not None

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError, match="unknown simulate-rate-status"):
            cli_mod._simulated_rate_status("not-a-state")

    def test_shape_matches_current_status(self):
        # Every snapshot should carry the same five keys
        # RateLedger.current_status() emits.
        expected = {
            "status", "resetsAt", "isUsingOverage",
            "overageStatus", "overageResetsAt",
        }
        for state in cli_mod.SIMULATED_STATUSES:
            rs = cli_mod._simulated_rate_status(state)
            assert set(rs["five_hour"].keys()) == expected
            assert set(rs["seven_day"].keys()) == expected

    def test_policy_decides_go_on_allowed(self):
        # Round-trip the simulated value through Policy.decide() to
        # verify the synthetic shape is actually consumable.
        policy = policy_mod.Policy(
            enabled=True,
            five_hour_soft_cap_action="pause",
            seven_day_soft_cap_action="wrap_up",
            hard_stop_usd=10.0, warn_usd=5.0,
            divergence_threshold=50,
        )
        rs = cli_mod._simulated_rate_status("allowed")
        decision = policy.decide(rs, 0.0, now=0.0)
        assert decision["action"] == "go"

    def test_policy_decides_pause_on_five_hour_pause(self):
        policy = policy_mod.Policy(
            enabled=True,
            five_hour_soft_cap_action="pause",
            seven_day_soft_cap_action="wrap_up",
            hard_stop_usd=10.0, warn_usd=5.0,
            divergence_threshold=50,
        )
        rs = cli_mod._simulated_rate_status("five_hour_pause")
        decision = policy.decide(rs, 0.0, now=0.0)
        assert decision["action"] == "pause_until"

    def test_policy_decides_wrap_up_on_seven_day(self):
        policy = policy_mod.Policy(
            enabled=True,
            five_hour_soft_cap_action="pause",
            seven_day_soft_cap_action="wrap_up",
            hard_stop_usd=10.0, warn_usd=5.0,
            divergence_threshold=50,
        )
        rs = cli_mod._simulated_rate_status("seven_day_wrap_up")
        decision = policy.decide(rs, 0.0, now=0.0)
        assert decision["action"] == "wrap_up"


# ---------------------------------------------------------------------------
# _resolve_rate_status
# ---------------------------------------------------------------------------


class TestResolveRateStatus:

    def test_simulate_none_reads_from_ledger(
        self, state_root, monkeypatch,
    ):
        rate_l = rate_ledger_mod.RateLedger(state_root=state_root)
        sentinel = object()
        monkeypatch.setattr(
            rate_ledger_mod.RateLedger, "current_status",
            lambda self, task_id: sentinel,
        )
        assert cli_mod._resolve_rate_status(rate_l, "t1", None) is sentinel

    def test_simulate_set_synthesises(self, state_root):
        rate_l = rate_ledger_mod.RateLedger(state_root=state_root)
        rs = cli_mod._resolve_rate_status(rate_l, "t1", "five_hour_pause")
        assert rs["five_hour"]["status"] != "allowed"


# ---------------------------------------------------------------------------
# _seed_policy_override
# ---------------------------------------------------------------------------


class TestSeedPolicyOverride:

    def test_no_source_no_op(self, state_root, cases_dir, workspaces_dir):
        task = Task(
            task_id="t1", goal="g",
            workspace_path=os.path.join(workspaces_dir, "t1"),
            created_at="ts", state_root=state_root,
        )
        cli_mod._seed_policy_override(task, cases_dir)
        # No state directory was created by the no-op.
        assert not os.path.isfile(
            os.path.join(state_root, "t1", "policy.toml"),
        )

    def test_source_present_copies_to_state(
        self, state_root, cases_dir, workspaces_dir,
    ):
        src_path = os.path.join(cases_dir, "t1-policy.toml")
        with open(src_path, "w", encoding="utf-8") as fh:
            fh.write('[buffer]\nhard_stop_usd = 1.50\nwarn_usd = 0.50\n')
        task = Task(
            task_id="t1", goal="g",
            workspace_path=os.path.join(workspaces_dir, "t1"),
            created_at="ts", state_root=state_root,
        )
        cli_mod._seed_policy_override(task, cases_dir)
        dst_path = os.path.join(state_root, "t1", "policy.toml")
        assert os.path.isfile(dst_path)
        with open(dst_path, "r", encoding="utf-8") as fh:
            assert "hard_stop_usd = 1.50" in fh.read()

    def test_existing_dst_not_overwritten(
        self, state_root, cases_dir, workspaces_dir,
    ):
        src_path = os.path.join(cases_dir, "t1-policy.toml")
        with open(src_path, "w", encoding="utf-8") as fh:
            fh.write('[buffer]\nhard_stop_usd = 99.0\n')
        os.makedirs(os.path.join(state_root, "t1"))
        dst_path = os.path.join(state_root, "t1", "policy.toml")
        with open(dst_path, "w", encoding="utf-8") as fh:
            fh.write("preserved")
        task = Task(
            task_id="t1", goal="g",
            workspace_path=os.path.join(workspaces_dir, "t1"),
            created_at="ts", state_root=state_root,
        )
        cli_mod._seed_policy_override(task, cases_dir)
        with open(dst_path, "r", encoding="utf-8") as fh:
            # Pre-existing content survived; the seed did not clobber.
            assert fh.read() == "preserved"


# ---------------------------------------------------------------------------
# _run_loop — policy gating
# ---------------------------------------------------------------------------


class TestRunLoopPolicy:

    def _seed_task(self, state_root, cases_dir, workspaces_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        return Task.from_toml(
            path, state_root=state_root, workspaces_dir=workspaces_dir,
        )

    def test_allowed_drains_full_queue(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        assert all(s.status == "done" for s in task.subtasks)
        # Stub fired once per subtask, in order.
        assert [c["subtask_id"] for c in stub_worker] == ["s1", "s2"]

    def test_five_hour_pause_records_decision_and_skips_workers(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="five_hour_pause",
        )
        # No worker spawned.
        assert stub_worker == []
        # All subtasks remain pending.
        assert all(s.status == "pending" for s in task.subtasks)
        # A policy_pause decision was recorded.
        kinds = [d.kind for d in task.decisions]
        assert "policy_pause" in kinds

    def test_seven_day_wrap_up_records_decision(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="seven_day_wrap_up",
        )
        assert stub_worker == []
        kinds = [d.kind for d in task.decisions]
        assert "policy_wrap_up" in kinds

    def test_buffer_hard_stop_records_halt(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        # Pre-seed buffer.jsonl with rows totalling above the default
        # 200 USD hard_stop_usd ceiling so the very first policy.decide()
        # returns halt.
        buffer_l = buffer_ledger_mod.BufferLedger(state_root=state_root)
        os.makedirs(
            os.path.join(state_root, "t1"), exist_ok=True,
        )
        path = buffer_l._path("t1")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "event": "buffer", "task_id": "t1", "subtask_id": "s0",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10, "output_tokens": 10},
                "cost_usd": 0.0001,
                "claude_reported_cost_usd": 250.0,
                "timestamp_utc": "2026-05-01T00:00:00+00:00",
            }) + "\n")
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        assert stub_worker == []
        kinds = [d.kind for d in task.decisions]
        assert "policy_halt" in kinds

    def test_empty_queue_returns_silently(
        self, state_root, workspaces_dir, stub_worker,
    ):
        # Direct construction; no enqueue, no subtasks.
        task = Task(
            task_id="t1", goal="g",
            workspace_path=os.path.join(workspaces_dir, "t1"),
            created_at="ts", state_root=state_root,
        )
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # No decisions were appended (no policy stop, no spawns).
        assert task.decisions == []
        assert stub_worker == []


# ---------------------------------------------------------------------------
# _run_loop — worker dispatch
# ---------------------------------------------------------------------------


class TestRunLoopDispatch:

    def _seed_task(self, state_root, cases_dir, workspaces_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        return Task.from_toml(
            path, state_root=state_root, workspaces_dir=workspaces_dir,
        )

    def test_complete_subtask_records_cost_usd(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # Both subtasks' cost_usd = 0.05 from the stub's terminal result.
        assert task.subtasks[0].cost_usd == pytest.approx(0.05)
        assert task.subtasks[1].cost_usd == pytest.approx(0.05)

    def test_complete_subtask_carries_output_summary(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # Stub returns "done"; loop strips and trims.
        for s in task.subtasks:
            assert s.result_summary == "done"

    def test_decision_log_includes_spawn_and_complete(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        kinds = [d.kind for d in task.decisions]
        # task_create + (worker_spawn + worker_complete) * 2 = 5
        assert kinds.count("worker_spawn") == 2
        assert kinds.count("worker_complete") == 2

    def test_failure_records_worker_fail_and_stops(
        self, state_root, cases_dir, workspaces_dir, fail_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # First subtask failed; loop bailed; second remains pending.
        assert task.subtasks[0].status == "failed"
        assert task.subtasks[1].status == "pending"
        kinds = [d.kind for d in task.decisions]
        assert kinds.count("worker_spawn") == 1
        assert kinds.count("worker_fail") == 1
        assert kinds.count("worker_complete") == 0
        # Only one stub call before the bail.
        assert len(fail_worker) == 1

    def test_timeout_records_worker_fail_with_reason(
        self, state_root, cases_dir, workspaces_dir, timeout_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        assert task.subtasks[0].status == "failed"
        # Failure summary captures the terminal_reason.
        assert "timeout" in task.subtasks[0].result_summary

    def test_disabled_policy_drains_queue_regardless(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
        monkeypatch, tmp_path,
    ):
        # An override TOML with enabled=false short-circuits
        # Policy.decide() to "go" regardless of cap state. Verify
        # the loop honours it by draining the queue even with
        # five_hour_pause simulated.
        os.makedirs(os.path.join(state_root, "t1"), exist_ok=True)
        with open(
            os.path.join(state_root, "t1", "policy.toml"),
            "w", encoding="utf-8",
        ) as fh:
            fh.write("enabled = false\n")
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="five_hour_pause",
        )
        assert all(s.status == "done" for s in task.subtasks)


# ---------------------------------------------------------------------------
# cmd_pause / cmd_halt
# ---------------------------------------------------------------------------


class TestCmdPause:

    def _seed_log(self, state_root, cases_dir, workspaces_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        Task.from_toml(
            path, state_root=state_root, workspaces_dir=workspaces_dir,
        )

    def test_pause_records_policy_pause_decision(
        self, state_root, cases_dir, workspaces_dir,
    ):
        self._seed_log(state_root, cases_dir, workspaces_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_pause(args)
        assert rc == 0
        rows = _read_rows(state_root, "t1")
        assert rows[-1]["event"] == "decision"
        assert rows[-1]["kind"] == "policy_pause"
        assert "manual pause" in rows[-1]["note"]

    def test_pause_with_simulate_flag_carries_state_in_note(
        self, state_root, cases_dir, workspaces_dir,
    ):
        self._seed_log(state_root, cases_dir, workspaces_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="five_hour_pause",
        )
        cli_mod.cmd_pause(args)
        rows = _read_rows(state_root, "t1")
        assert rows[-1]["kind"] == "policy_pause"
        assert "five_hour_pause" in rows[-1]["note"]

    def test_pause_does_not_write_task_resume(
        self, state_root, cases_dir, workspaces_dir,
    ):
        self._seed_log(state_root, cases_dir, workspaces_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        cli_mod.cmd_pause(args)
        rows = _read_rows(state_root, "t1")
        kinds = [r.get("kind") for r in rows if r.get("event") == "decision"]
        # Only one decision row appended by pause; task_create is the
        # bootstrap decision recorded as event=task_create, not
        # event=decision.
        assert "task_resume" not in kinds

    def test_pause_missing_log_raises(
        self, state_root, cases_dir, workspaces_dir,
    ):
        # No log; load_task raises before pause records anything.
        args = _make_args(
            "ghost", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        with pytest.raises(Exception):
            cli_mod.cmd_pause(args)

    def test_pause_summary_printed(
        self, state_root, cases_dir, workspaces_dir, capsys,
    ):
        self._seed_log(state_root, cases_dir, workspaces_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        cli_mod.cmd_pause(args)
        out = capsys.readouterr().out
        assert "Last decision: policy_pause" in out


class TestCmdHalt:

    def _seed_log(self, state_root, cases_dir, workspaces_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        Task.from_toml(
            path, state_root=state_root, workspaces_dir=workspaces_dir,
        )

    def test_halt_records_policy_halt_decision(
        self, state_root, cases_dir, workspaces_dir,
    ):
        self._seed_log(state_root, cases_dir, workspaces_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_halt(args)
        assert rc == 0
        rows = _read_rows(state_root, "t1")
        assert rows[-1]["event"] == "decision"
        assert rows[-1]["kind"] == "policy_halt"
        assert "manual halt" in rows[-1]["note"]

    def test_halt_with_simulate_flag_carries_state_in_note(
        self, state_root, cases_dir, workspaces_dir,
    ):
        self._seed_log(state_root, cases_dir, workspaces_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="seven_day_wrap_up",
        )
        cli_mod.cmd_halt(args)
        rows = _read_rows(state_root, "t1")
        assert rows[-1]["kind"] == "policy_halt"
        assert "seven_day_wrap_up" in rows[-1]["note"]


# ---------------------------------------------------------------------------
# Round-trip: start (allowed) → pause → resume
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """End-to-end cycle in pure-Python: §8 PLAN steps 4 → 5 → 6 mocked.

    Confirms task_events.jsonl carries the full timeline and the
    pause + resume cycle re-enters cleanly without operator
    intervention beyond the explicit subcommand calls.
    """

    def _seed_case(self, cases_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        return path

    def test_start_pause_resume_cycle(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)

        # Step 4 — start in allowed state. Loop drains the 2 subtasks.
        start_args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        )
        rc = cli_mod.cmd_start(start_args)
        assert rc == 0
        # Step 5 — explicit pause records policy_pause.
        pause_args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="five_hour_pause",
        )
        rc = cli_mod.cmd_pause(pause_args)
        assert rc == 0
        # Step 6 — resume without simulation override.
        resume_args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        )
        rc = cli_mod.cmd_resume(resume_args)
        assert rc == 0

        rows = _read_rows(state_root, "t1")
        # All three artefacts exist: task_events.jsonl was written.
        decisions = [
            r for r in rows
            if r.get("event") == "decision"
        ]
        kinds = [d["kind"] for d in decisions]
        # Cycle markers must appear at least once each.
        assert "policy_pause" in kinds
        assert "task_resume" in kinds
        assert "worker_spawn" in kinds
        assert "worker_complete" in kinds

        # Task-state load reads back consistent state: 2 done.
        loaded = load_task(
            "t1", state_root=state_root, mark_resume=False,
        )
        statuses = {s.subtask_id: s.status for s in loaded.subtasks}
        assert statuses == {"s1": "done", "s2": "done"}
