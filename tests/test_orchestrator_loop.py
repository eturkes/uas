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
    ack_checkpoint: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        task=task,
        state_root=state_root,
        cases_dir=cases_dir,
        workspaces_dir=workspaces_dir,
        simulate_rate_status=simulate_rate_status,
        ack_checkpoint=ack_checkpoint,
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
# _compute_pause_sleep_seconds (Phase 5 §3)
# ---------------------------------------------------------------------------


class TestComputePauseSleepSeconds:
    """Pure helper, no I/O. Exercises the four paths: until-None,
    until-in-past, until-in-future-under-cap, until-over-cap."""

    def _policy(
        self,
        *,
        fallback: int = 1800,
        max_wait: int = 21600,
        enabled: bool = True,
    ):
        return policy_mod.Policy(
            enabled=True,
            five_hour_soft_cap_action="pause",
            seven_day_soft_cap_action="wrap_up",
            hard_stop_usd=10.0, warn_usd=5.0,
            divergence_threshold=50,
            auto_resume_enabled=enabled,
            auto_resume_fallback_seconds=fallback,
            auto_resume_max_wait_seconds=max_wait,
        )

    def test_until_none_uses_fallback(self):
        decision = {"action": "pause_until", "reason": "x", "until": None}
        result = cli_mod._compute_pause_sleep_seconds(
            decision, self._policy(fallback=600), now=1000.0,
        )
        assert result == 600.0

    def test_until_none_fallback_clamped_to_max_wait(self):
        # fallback > max_wait — operator misconfiguration; clamp
        # rather than oversleep.
        decision = {"action": "pause_until", "reason": "x", "until": None}
        result = cli_mod._compute_pause_sleep_seconds(
            decision, self._policy(fallback=10_000, max_wait=300),
            now=1000.0,
        )
        assert result == 300.0

    def test_until_in_past_returns_zero(self):
        decision = {
            "action": "pause_until", "reason": "x", "until": 999.0,
        }
        result = cli_mod._compute_pause_sleep_seconds(
            decision, self._policy(), now=1000.0,
        )
        assert result == 0.0

    def test_until_equals_now_returns_zero(self):
        decision = {
            "action": "pause_until", "reason": "x", "until": 1000.0,
        }
        result = cli_mod._compute_pause_sleep_seconds(
            decision, self._policy(), now=1000.0,
        )
        assert result == 0.0

    def test_until_in_future_under_cap(self):
        decision = {
            "action": "pause_until", "reason": "x", "until": 1500.0,
        }
        result = cli_mod._compute_pause_sleep_seconds(
            decision, self._policy(max_wait=21600), now=1000.0,
        )
        assert result == 500.0

    def test_until_in_future_over_cap_is_clamped(self):
        # Year-2050 corruption case — clamp to max_wait so the loop
        # keeps polling rather than wedging indefinitely.
        decision = {
            "action": "pause_until", "reason": "x",
            "until": 2_524_608_000.0,  # ~2050-01-01
        }
        result = cli_mod._compute_pause_sleep_seconds(
            decision, self._policy(max_wait=3600), now=1000.0,
        )
        assert result == 3600.0


# ---------------------------------------------------------------------------
# _run_loop — auto-resume primitive
# ---------------------------------------------------------------------------


class TestRunLoopAutoResume:
    """Phase 5 §3 — when ``policy.auto_resume_enabled`` is true,
    the loop sleeps on a ``pause_until`` verdict and re-evaluates
    rather than recording the pause and exiting.
    """

    _AUTO_RESUME_TOML = (
        "[auto_resume]\n"
        "enabled = true\n"
        "fallback_seconds = 30\n"
        "max_wait_seconds = 60\n"
    )

    def _seed_task(self, state_root, cases_dir, workspaces_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        return Task.from_toml(
            path, state_root=state_root, workspaces_dir=workspaces_dir,
        )

    def _seed_override(self, state_root, body=_AUTO_RESUME_TOML):
        os.makedirs(os.path.join(state_root, "t1"), exist_ok=True)
        with open(
            os.path.join(state_root, "t1", "policy.toml"),
            "w", encoding="utf-8",
        ) as fh:
            fh.write(body)

    def test_auto_resume_sleeps_then_drains_queue(
        self, state_root, cases_dir, workspaces_dir,
        stub_worker, monkeypatch,
    ):
        """First iteration → pause → sleep → second iteration → go."""
        self._seed_override(state_root)
        task = self._seed_task(state_root, cases_dir, workspaces_dir)

        # Mutable state the stub-sleep flips to release the loop on
        # second iteration.
        sim_state = {"current": "five_hour_pause"}
        sleep_calls: list[float] = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            sim_state["current"] = "allowed"

        def state_aware_resolve(rate_l, task_id, simulate):
            return cli_mod._simulated_rate_status(sim_state["current"])

        monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)
        monkeypatch.setattr(
            cli_mod, "_resolve_rate_status", state_aware_resolve,
        )

        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="five_hour_pause",
        )

        # Sleep fired once, queue drained on the second iteration.
        assert len(sleep_calls) == 1
        assert all(s.status == "done" for s in task.subtasks)
        # Both spawn/complete pairs ran post-wake.
        assert [c["subtask_id"] for c in stub_worker] == ["s1", "s2"]
        # Decision timeline: policy_pause + policy_auto_resume
        # surrounding the spawn/complete pairs.
        kinds = [d.kind for d in task.decisions]
        assert kinds.count("policy_pause") == 1
        assert kinds.count("policy_auto_resume") == 1
        # Order: pause precedes auto_resume which precedes spawns.
        idx_pause = kinds.index("policy_pause")
        idx_resume = kinds.index("policy_auto_resume")
        idx_first_spawn = kinds.index("worker_spawn")
        assert idx_pause < idx_resume < idx_first_spawn

    def test_auto_resume_pause_decision_carries_sleep_duration(
        self, state_root, cases_dir, workspaces_dir,
        stub_worker, monkeypatch,
    ):
        self._seed_override(state_root)
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        sim_state = {"current": "five_hour_pause"}

        def fake_sleep(seconds):
            sim_state["current"] = "allowed"

        def state_aware_resolve(rate_l, task_id, simulate):
            return cli_mod._simulated_rate_status(sim_state["current"])

        monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)
        monkeypatch.setattr(
            cli_mod, "_resolve_rate_status", state_aware_resolve,
        )

        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="five_hour_pause",
        )
        # Pause-decision note records the sleep duration so a reader
        # of task_events.jsonl can audit the wait without correlating
        # against rate_limits.jsonl.
        pause = next(d for d in task.decisions if d.kind == "policy_pause")
        assert "auto-resume after" in pause.note
        assert "s sleep" in pause.note
        resume = next(
            d for d in task.decisions if d.kind == "policy_auto_resume"
        )
        assert "woke after" in resume.note
        assert "re-evaluating" in resume.note

    def test_auto_resume_disabled_records_pause_and_returns(
        self, state_root, cases_dir, workspaces_dir,
        stub_worker, monkeypatch,
    ):
        """Default-off branch: explicit-operator behaviour preserved."""
        # No override → committed default has auto_resume.enabled = false.
        task = self._seed_task(state_root, cases_dir, workspaces_dir)

        sleep_calls: list[float] = []
        monkeypatch.setattr(
            cli_mod.time, "sleep",
            lambda s: sleep_calls.append(s),
        )

        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="five_hour_pause",
        )

        # No sleep fired; no auto_resume decision recorded.
        assert sleep_calls == []
        kinds = [d.kind for d in task.decisions]
        assert "policy_pause" in kinds
        assert "policy_auto_resume" not in kinds
        # All subtasks remain pending — the loop exited.
        assert all(s.status == "pending" for s in task.subtasks)

    def test_auto_resume_with_unparseable_resets_at_uses_fallback(
        self, state_root, cases_dir, workspaces_dir,
        stub_worker, monkeypatch,
    ):
        """``until = None`` (resetsAt malformed) → fallback_seconds."""
        self._seed_override(state_root)
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        sim_state = {"current": "malformed"}
        sleep_calls: list[float] = []

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            sim_state["current"] = "allowed"

        # Synthesise a rate_status whose five_hour.resetsAt is a
        # value Policy._parse_resets_at refuses (a dict; Phase 5 §3
        # _parse_resets_at rejects non-numeric, non-string types).
        def state_aware_resolve(rate_l, task_id, simulate):
            if sim_state["current"] == "allowed":
                return cli_mod._simulated_rate_status("allowed")
            base = cli_mod._simulated_rate_status("five_hour_pause")
            base["five_hour"]["resetsAt"] = {"corrupted": True}
            return base

        monkeypatch.setattr(cli_mod.time, "sleep", fake_sleep)
        monkeypatch.setattr(
            cli_mod, "_resolve_rate_status", state_aware_resolve,
        )

        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir,
            simulate="five_hour_pause",
        )

        # The §3 override sets fallback_seconds=30. Policy emits
        # until=None for the corrupted resetsAt; the helper falls
        # back to 30s (under max_wait=60).
        assert sleep_calls == [30.0]
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

        # Phase 5 §4 acceptance: resume_summary.md written during the
        # cycle. The most recent CLI subcommand was cmd_resume; its
        # post-loop call to write_resume_summary leaves a digest on
        # disk reflecting the final state.
        summary_path = os.path.join(
            state_root, "t1", "resume_summary.md",
        )
        assert os.path.isfile(summary_path)
        with open(summary_path, "r", encoding="utf-8") as fh:
            digest = fh.read()
        assert "# Resume summary — `t1`" in digest
        assert "Total: 2 (0 pending, 0 in-flight, 2 done, 0 failed)" in digest


# ---------------------------------------------------------------------------
# Resume summary integration (Phase 5 §4)
# ---------------------------------------------------------------------------


class TestResumeSummaryWiring:
    """Each cmd_* subcommand must rewrite ``resume_summary.md`` after
    its decision/loop work completes. These tests verify the attach
    points without re-asserting the digest's content (covered in
    ``tests/test_orchestrator_resume_summary.py``).
    """

    def _summary_path(self, state_root: str) -> str:
        return os.path.join(state_root, "t1", "resume_summary.md")

    def _seed_case(self, cases_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)

    def test_cmd_start_writes_summary(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)
        args = _make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        )
        cli_mod.cmd_start(args)
        assert os.path.isfile(self._summary_path(state_root))

    def test_cmd_resume_writes_summary(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)
        # Bootstrap the log via cmd_start first (cmd_resume requires it).
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        # Remove the start-time summary so we can prove cmd_resume rewrites it.
        os.remove(self._summary_path(state_root))
        cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        assert os.path.isfile(self._summary_path(state_root))

    def test_cmd_status_writes_summary(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        os.remove(self._summary_path(state_root))
        cli_mod.cmd_status(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        assert os.path.isfile(self._summary_path(state_root))

    def test_cmd_pause_writes_summary(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        os.remove(self._summary_path(state_root))
        cli_mod.cmd_pause(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        path = self._summary_path(state_root)
        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
        # The just-recorded pause should drive the suggestion.
        assert "Wait for the 5-hour window" in body

    def test_cmd_halt_writes_summary(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        os.remove(self._summary_path(state_root))
        cli_mod.cmd_halt(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        path = self._summary_path(state_root)
        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()
        assert "Hard-stop reached" in body

    def test_summary_rewritten_on_each_invocation(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        """Same path; second invocation overwrites the first."""
        self._seed_case(cases_dir)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        path = self._summary_path(state_root)
        first_mtime = os.path.getmtime(path)
        # Force a new mtime by sleeping briefly.
        import time
        time.sleep(0.01)
        cli_mod.cmd_status(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        second_mtime = os.path.getmtime(path)
        assert second_mtime > first_mtime


# ---------------------------------------------------------------------------
# Phase 5 §5 — checkpoint primitive
# ---------------------------------------------------------------------------


_CHECKPOINT_TOML = """\
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


class TestRunLoopCheckpoints:
    """Phase 5 §5 — the loop's checkpoint gate pauses before the
    declared subtask and stays paused until the operator acks."""

    def _seed_case(self, cases_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_CHECKPOINT_TOML)
        return path

    def _seed_task(self, state_root, cases_dir, workspaces_dir):
        self._seed_case(cases_dir)
        return Task.from_toml(
            os.path.join(cases_dir, "t1.toml"),
            state_root=state_root, workspaces_dir=workspaces_dir,
        )

    def test_loop_pauses_before_declared_subtask(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # s1 spawned, s2 paused at checkpoint, s3 still pending.
        assert task.subtasks[0].status == "done"
        assert task.subtasks[1].status == "pending"
        assert task.subtasks[2].status == "pending"
        # Stub fired exactly once for s1.
        assert [c["subtask_id"] for c in stub_worker] == ["s1"]
        # Decisions include exactly one checkpoint_pause carrying
        # the declared id and before_subtask in the note.
        kinds = [d.kind for d in task.decisions]
        assert kinds.count("checkpoint_pause") == 1
        pause = next(d for d in task.decisions if d.kind == "checkpoint_pause")
        assert "review-after-s1" in pause.note
        assert "s2" in pause.note

    def test_loop_resumes_after_ack(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # Operator acks the checkpoint.
        task.record_decision(
            "checkpoint_ack", "ack", checkpoint_id="review-after-s1",
        )
        # Re-enter the loop.
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        assert all(s.status == "done" for s in task.subtasks)
        assert [c["subtask_id"] for c in stub_worker] == ["s1", "s2", "s3"]

    def test_plain_resume_re_pauses_at_same_checkpoint(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        # First iteration spawns s1 and pauses at checkpoint.
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        # Second iteration without ack must re-pause; s2 / s3 stay pending.
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        assert task.subtasks[1].status == "pending"
        assert task.subtasks[2].status == "pending"
        kinds = [d.kind for d in task.decisions]
        # Two pause decisions; one per loop iteration.
        assert kinds.count("checkpoint_pause") == 2
        # Stub still fired exactly once (only s1).
        assert len(stub_worker) == 1

    def test_no_checkpoint_declared_loop_drains_normally(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        """Synthetic-multistep regression: tasks without
        ``[[checkpoints]]`` exhibit the pre-§5 drain-the-queue
        behaviour exactly."""
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)  # no checkpoints declared
        task = Task.from_toml(
            path, state_root=state_root, workspaces_dir=workspaces_dir,
        )
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        assert all(s.status == "done" for s in task.subtasks)
        kinds = [d.kind for d in task.decisions]
        assert "checkpoint_pause" not in kinds

    def test_checkpoint_pause_persists_id_in_event_payload(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        task = self._seed_task(state_root, cases_dir, workspaces_dir)
        cli_mod._run_loop(
            task, state_root=state_root,
            workspaces_dir=workspaces_dir, simulate="allowed",
        )
        rows = _read_rows(state_root, "t1")
        pause_rows = [
            r for r in rows
            if r.get("event") == "decision"
            and r.get("kind") == "checkpoint_pause"
        ]
        assert len(pause_rows) == 1
        assert pause_rows[0]["checkpoint_id"] == "review-after-s1"


class TestCmdResumeAckCheckpoint:
    """Phase 5 §5 — ``cmd_resume`` validates ``--ack-checkpoint``
    against declared checkpoints and the in-memory ack set."""

    def _seed_case(self, cases_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_CHECKPOINT_TOML)

    def _bootstrap(self, state_root, cases_dir, workspaces_dir):
        # Run cmd_start → loop pauses at checkpoint → state log
        # carries one checkpoint_pause decision.
        self._seed_case(cases_dir)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))

    def test_ack_unknown_id_errors_and_returns_two(
        self, state_root, cases_dir, workspaces_dir, stub_worker, capsys,
    ):
        self._bootstrap(state_root, cases_dir, workspaces_dir)
        rc = cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            ack_checkpoint="not-a-real-id",
        ))
        assert rc == 2
        err = capsys.readouterr().err
        assert "does not reference a declared checkpoint" in err
        assert "not-a-real-id" in err

    def test_ack_already_acked_id_errors_and_returns_two(
        self, state_root, cases_dir, workspaces_dir, stub_worker, capsys,
    ):
        self._bootstrap(state_root, cases_dir, workspaces_dir)
        # First successful ack:
        rc1 = cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            ack_checkpoint="review-after-s1",
        ))
        assert rc1 == 0
        # Second attempt to ack the same id must error cleanly.
        rc2 = cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            ack_checkpoint="review-after-s1",
        ))
        assert rc2 == 2
        err = capsys.readouterr().err
        assert "already" in err.lower() or "acknowledged" in err.lower()

    def test_ack_records_checkpoint_ack_decision(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._bootstrap(state_root, cases_dir, workspaces_dir)
        cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            ack_checkpoint="review-after-s1",
        ))
        rows = _read_rows(state_root, "t1")
        ack_rows = [
            r for r in rows
            if r.get("event") == "decision"
            and r.get("kind") == "checkpoint_ack"
        ]
        assert len(ack_rows) == 1
        assert ack_rows[0]["checkpoint_id"] == "review-after-s1"

    def test_ack_lets_loop_drain_queue(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._bootstrap(state_root, cases_dir, workspaces_dir)
        rc = cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
            ack_checkpoint="review-after-s1",
        ))
        assert rc == 0
        loaded = load_task(
            "t1", state_root=state_root, mark_resume=False,
        )
        assert all(s.status == "done" for s in loaded.subtasks)

    def test_plain_resume_does_not_drain_past_checkpoint(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._bootstrap(state_root, cases_dir, workspaces_dir)
        # Resume without --ack-checkpoint must not bypass the
        # checkpoint; the loop re-pauses at the same position.
        rc = cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        assert rc == 0
        loaded = load_task(
            "t1", state_root=state_root, mark_resume=False,
        )
        statuses = {s.subtask_id: s.status for s in loaded.subtasks}
        assert statuses["s1"] == "done"
        assert statuses["s2"] == "pending"
        assert statuses["s3"] == "pending"

    def test_resume_subparser_accepts_ack_flag(self):
        # Argparse plumbing — the flag must be on the resume parser.
        parser = cli_mod.build_parser()
        args = parser.parse_args([
            "resume", "t1",
            "--ack-checkpoint", "review-after-s1",
        ])
        assert args.task == "t1"
        assert args.ack_checkpoint == "review-after-s1"

    def test_resume_default_ack_checkpoint_is_none(self):
        parser = cli_mod.build_parser()
        args = parser.parse_args(["resume", "t1"])
        assert args.ack_checkpoint is None


class TestRoundTripCheckpoint:
    """Phase 5 §5 acceptance — full cycle: start → checkpoint pause →
    status → ack-resume → next spawn → completion. The cycle is
    end-to-end through the CLI surface, not just _run_loop."""

    def _seed_case(self, cases_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_CHECKPOINT_TOML)

    def test_full_checkpoint_cycle(
        self, state_root, cases_dir, workspaces_dir, stub_worker,
    ):
        self._seed_case(cases_dir)

        # Step 1: start → s1 spawns → loop pauses at checkpoint.
        rc = cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        assert rc == 0

        # Step 2: status confirms paused state without polluting log.
        rc = cli_mod.cmd_status(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        assert rc == 0

        # Step 3: resume with --ack-checkpoint → loop drains.
        rc = cli_mod.cmd_resume(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
            ack_checkpoint="review-after-s1",
        ))
        assert rc == 0

        # All three subtasks done; stub fired in declaration order.
        loaded = load_task(
            "t1", state_root=state_root, mark_resume=False,
        )
        assert [s.status for s in loaded.subtasks] == [
            "done", "done", "done",
        ]
        assert [c["subtask_id"] for c in stub_worker] == [
            "s1", "s2", "s3",
        ]

        # Decision timeline contains exactly one pause and one ack.
        kinds = [d.kind for d in loaded.decisions]
        assert kinds.count("checkpoint_pause") == 1
        assert kinds.count("checkpoint_ack") == 1
        assert kinds.count("worker_complete") == 3

        # Phase 5 §4 acceptance: resume_summary.md was written and
        # reflects the cleared checkpoint state.
        summary_path = os.path.join(
            state_root, "t1", "resume_summary.md",
        )
        assert os.path.isfile(summary_path)
        with open(summary_path, "r", encoding="utf-8") as fh:
            digest = fh.read()
        assert "Total: 3 (0 pending, 0 in-flight, 3 done, 0 failed)" in digest
        # No checkpoint should still be pending after the ack-resume cycle.
        assert "## Pending checkpoint" not in digest


class TestCheckpointPrintSummary:
    """Phase 5 §5 — _print_summary surfaces a pending checkpoint."""

    def _seed_case(self, cases_dir):
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_CHECKPOINT_TOML)

    def test_status_renders_pending_checkpoint_line(
        self, state_root, cases_dir, workspaces_dir, stub_worker, capsys,
    ):
        self._seed_case(cases_dir)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        capsys.readouterr()  # drain start's stdout
        cli_mod.cmd_status(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        out = capsys.readouterr().out
        assert "Pending checkpoint: review-after-s1" in out
        assert "before s2" in out
        assert "Review s1 output before s2." in out

    def test_status_omits_line_when_no_pending_checkpoint(
        self, state_root, cases_dir, workspaces_dir, stub_worker, capsys,
    ):
        # synthetic-multistep doesn't declare checkpoints; status
        # output must not include the new line.
        path = os.path.join(cases_dir, "t1.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_FULL_TOML)
        cli_mod.cmd_start(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
            simulate_rate_status="allowed",
        ))
        capsys.readouterr()
        cli_mod.cmd_status(_make_args(
            "t1", state_root=state_root, cases_dir=cases_dir,
            workspaces_dir=workspaces_dir,
        ))
        out = capsys.readouterr().out
        assert "Pending checkpoint" not in out
